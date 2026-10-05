#!/usr/bin/env bash
# test-gpu-lease.sh — the lease's contention, crash and sweep semantics, without a GPU.
#
# The sweep is process/cgroup/systemd logic, not GPU logic, so a stand-in
# executable named `llama-server` exercises every line of it: pgrep -x matches
# on comm, cgroup attribution is identical, and the assertion that clears a
# holder is "the pid is gone". What this cannot test is a real model's memory
# coming back, which the box run covers.
#
#   ./test-gpu-lease.sh            all tests
#   ./test-gpu-lease.sh 3 7        only tests 3 and 7
#
# Isolated: its own HATCH_GPU_RUNDIR, its own stand-in process name, and a
# resident unit name that exists nowhere — so it can never touch a real lease
# or a real llama-server.

set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")"

# GPU_LEASE_BIN points the whole suite at another implementation: the assertions are the
# contract, and both the bash verb and the Python app must pass them unchanged.
LEASE="${GPU_LEASE_BIN:-$PWD/gpu-lease}"
# The arbiter's source, for the one textual check (test 20): the verb, plus the app
# directory once it exists. grep -r reads a directory as readily as a file.
LEASE_SRC=("$PWD/gpu-lease")
[[ -d "$PWD/../../apps/gpu-lease" ]] && LEASE_SRC+=("$PWD/../../apps/gpu-lease")
TMP="$(mktemp -d)"
export HATCH_GPU_RUNDIR="$TMP/run"
export HATCH_GPU_PROC="fake-llama"
export HATCH_RESIDENT_UNIT="gpu-lease-test-resident.service"
export HATCH_RESIDENT_PORT="18081"
# Declare this machine the GPU machine: the harness runs wherever it is checked
# out, and off the box the verb would otherwise (correctly) refuse every `run`.
export HATCH_GPU_HOST="$(uname -n)"
# The box runs a real llama-swap, and `--resident stop` now asks the router to unload.
# A fake unit name and an unroutable address keep every test off it.
export HATCH_ROUTER_UNIT="gpu-lease-test-router.service"
export HATCH_ROUTER_ADDR="127.0.0.1:1"
# EMPTY, deliberately. Left unset, the verb falls through to engine/lib/gpu-binaries.env
# and the box's real ids (`qnext qwen3.5-4b`) appear in this harness's output – which is
# how a test starts asserting against production configuration without anyone deciding to.
# A test that wants the router-held shape sets this itself, for its own scope.
export HATCH_ROUTER_RESIDENTS=""

PASS=0; FAIL=0; ONLY=("$@")

cleanup_all() {
  systemctl --user stop 'gpu-lease-test-*' 'gpu-lease-t29-*' 'gpu-lease-t30-*' 2>/dev/null
  # The fake routers of tests 25, 29 and 30 outlive an interrupted run otherwise: each is a
  # python http.server in its own process group, and only the happy path kills them.
  kill "${R25_PID:-}" "${R29_PID:-}" "${R30_PID:-}" 2>/dev/null
  pkill -x "$HATCH_GPU_PROC" 2>/dev/null
  pkill -x fake-sd 2>/dev/null
  pkill -x fake-t20-srv 2>/dev/null
  pkill -x fake-t23-srv 2>/dev/null
  rm -rf "$TMP"
}
trap cleanup_all EXIT

# A stand-in for llama-server. It has to be a COPIED BINARY, not a shebang
# script: a script's comm is its interpreter, so `pgrep -x fake-llama` would
# match nothing and every sweep assertion would pass by finding no work to do.
mkdir -p "$TMP/bin"
cp "$(command -v bash)" "$TMP/bin/$HATCH_GPU_PROC"
FAKE=("$TMP/bin/$HATCH_GPU_PROC" -c 'trap "exit 0" TERM INT; while :; do sleep 1; done' fake-llama)

# Spawning the stand-in needs its own script: the -c payload contains spaces and
# quotes, so passing it through an inner `bash -c` as a string word-splits into
# something systemd-run cannot run — and a stand-in that never starts makes
# every sweep assertion pass by having no work to do.
cat > "$TMP/bin/spawn-fake" <<EOS
#!/usr/bin/env bash
# spawn-fake <unit> <port> [model.gguf] — start the stand-in and prove it is running.
# The optional model lands in the cmdline as \`-m\`, which is where the verb reads it
# from to tell one of our own resident models from a stranger.
systemd-run --user --unit="\$1" --collect \\
  "$TMP/bin/$HATCH_GPU_PROC" -c 'trap "exit 0" TERM INT; while :; do sleep 1; done' fake-llama --port "\$2" \${3:+-m /models/\$3} \\
  >/dev/null 2>&1 || { echo "spawn-fake: systemd-run failed" >&2; exit 1; }
# The unit's own MainPID, not the first stand-in in the process table: with a resident
# already up, pgrep's first hit is that one's pid, not the process just started.
for i in \$(seq 1 20); do
  pid=\$(systemctl --user show -p MainPID --value "\$1" 2>/dev/null)
  [[ -n "\$pid" && "\$pid" != 0 ]] && kill -0 "\$pid" 2>/dev/null && { echo "\$pid"; exit 0; }
  sleep 0.5
done
echo "spawn-fake: the stand-in never appeared in the process table" >&2
exit 1
EOS
chmod +x "$TMP/bin/spawn-fake"

want()   { [[ ${#ONLY[@]} -eq 0 ]] || [[ " ${ONLY[*]} " == *" $1 "* ]]; }
ok()     { PASS=$((PASS+1)); printf '  ok    %s\n' "$1"; }
bad()    { FAIL=$((FAIL+1)); printf '  FAIL  %s\n     saw: %s\n' "$1" "$2"; }
check()  { if [[ "$2" == *"$3"* ]]; then ok "$1"; else bad "$1" "$(printf '%s' "$2" | tr '\n' '|' | head -c 400)"; fi; }
checkeq(){ if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1" "expected '$3', got '$2'"; fi; }

# The lock file is the only authority on held/free — tests assert against it,
# never against the verb's own status output.
lock_free() { flock -n "$HATCH_GPU_RUNDIR/gpu.lock" true 2>/dev/null; }

t() { printf '\n%s\n' "── $1"; }

# ── 1. a free lease reports free, and a lock is really released after a run ──
if want 1; then
  t "1. run holds the lock and gives it back"
  out=$("$LEASE" run --who t1 --why "basic run" --resident keep -- bash -c 'flock -n '"$HATCH_GPU_RUNDIR"'/gpu.lock true && echo LOCK-WAS-FREE || echo LOCK-WAS-HELD' 2>&1)
  check "the lock is held while the command runs" "$out" "LOCK-WAS-HELD"
  if lock_free; then ok "the lock is free after the command returns"; else bad "the lock is free after the command returns" "still held"; fi
  [[ -e "$HATCH_GPU_RUNDIR/gpu.lease" ]] && bad "the sidecar is removed" "still present" || ok "the sidecar is removed"
fi

# ── 2. exit codes pass through ───────────────────────────────────────────────
if want 2; then
  t "2. the wrapped command's exit code survives the wrapper"
  "$LEASE" run --who t2 --why "exit code" --resident keep -- bash -c 'exit 42' >/dev/null 2>&1
  checkeq "exit 42 propagates" "$?" "42"
  "$LEASE" run --who t2 --why "exit code" --resident keep -- true >/dev/null 2>&1
  checkeq "exit 0 propagates" "$?" "0"
fi

# ── 3. the crash that started this stream ────────────────────────────────────
if want 3; then
  t "3. the job dies on an unbound variable (the 02:35 orphan's own construction)"
  # Verbatim from box-model-eval: bash expands $cfg before `local` assigns it,
  # so `set -u` aborts at runtime. Invisible to bash -n.
  out=$("$LEASE" run --who t3 --why "unbound var" --resident keep -- \
        bash -c 'set -u; f() { local cfg=$1 rung=$2 tag="$cfg-$rung"; echo "$tag"; }; f a b' 2>&1)
  rc=$?
  [[ $rc -ne 0 ]] && ok "the run reports the crash (exit $rc)" || bad "the run reports the crash" "exit 0"
  if lock_free; then ok "the lock is released despite the crash"; else bad "the lock is released despite the crash" "still held"; fi
  [[ -e "$HATCH_GPU_RUNDIR/gpu.lease" ]] && bad "the sidecar is cleared" "still present" || ok "the sidecar is cleared"
fi

# ── 4. contention: the second job queues, it does not evict ──────────────────
if want 4; then
  t "4. a second job queues behind the first"
  "$LEASE" run --who t4a --why "first holder" --resident keep -- sleep 6 >/dev/null 2>&1 &
  first=$!
  sleep 1.5
  out=$("$LEASE" run --who t4b --why "second" --resident keep --wait 0 -- true 2>&1); rc=$?
  checkeq "--wait 0 exits 75 (lease unavailable)" "$rc" "75"
  out0s=$("$LEASE" run --who t4b --why "second" --resident keep --wait 0s -- true 2>&1)
  check "--wait 0s behaves like --wait 0" "$out0s" "do not queue"
  check "the refusal names the holder" "$out" "t4a"
  check "the refusal names why" "$out" "first holder"
  start=$SECONDS
  "$LEASE" run --who t4c --why "queued" --resident keep --wait 30s -- true >/dev/null 2>&1; rc=$?
  waited=$(( SECONDS - start ))
  checkeq "a waiting job eventually gets the lease" "$rc" "0"
  [[ $waited -ge 3 ]] && ok "it genuinely waited (${waited}s) rather than running concurrently" \
                       || bad "it genuinely waited" "only ${waited}s"
  wait $first 2>/dev/null
fi

# ── 5. status: derived from the lock, not from the sidecar ───────────────────
if want 5; then
  t "5. status reads the lock, and a leftover sidecar reads STALE"
  "$LEASE" run --who t5 --why "status probe" --resident keep -- sleep 4 >/dev/null 2>&1 &
  sleep 1.5
  out=$("$LEASE" status 2>&1)
  check "a live lease reports HELD" "$out" "HELD by t5"
  wait
  out=$("$LEASE" status 2>&1)
  check "a released lease reports free" "$out" "lease: free"
  # A sidecar with nothing holding the lock is exactly what a SIGKILLed holder
  # leaves behind. It must never read as held.
  printf 'who=ghost\nwhy=killed holder\nmode=run\nresident=keep\nttl=\npid=999999\nunit=\nhost=x\nsince=%s\nbaseline=\n' \
    "$(date +%s)" > "$HATCH_GPU_RUNDIR/gpu.lease"
  out=$("$LEASE" status 2>&1)
  check "a sidecar with no lock reports STALE" "$out" "STALE"
  out=$("$LEASE" restore 2>&1)
  check "restore clears the stale lease" "$out" "stale lease cleared"
  out=$("$LEASE" status 2>&1)
  check "status is free again" "$out" "lease: free"
fi

# ── 6. the sweep: an orphan server the job left behind ────────────────────────
if want 6; then
  t "6. the sweep clears a GPU holder the job left running"
  # The job starts a server unit, records its pid, then dies the 02:35 death.
  out=$("$LEASE" run --who t6 --why "leaves an orphan" --resident keep -- bash -c "
      $TMP/bin/spawn-fake gpu-lease-test-orphan 19999 > $TMP/orphan.pid || exit 9
      set -u; echo \$nope" 2>&1)
  orphan_pid="$(cat "$TMP/orphan.pid" 2>/dev/null)"
  # Guard against a vacuous pass: if the orphan never ran, the sweep assertions
  # below would all succeed by finding nothing.
  if [[ -n "$orphan_pid" ]]; then ok "the orphan really started (pid $orphan_pid)"
  else bad "the orphan really started" "spawn-fake produced no pid — the rest of this test would be vacuous"; fi
  check "the sweep names the orphan" "$out" "sweeping orphan GPU holder"
  if kill -0 "$orphan_pid" 2>/dev/null; then
    bad "the orphan process is gone" "pid $orphan_pid still alive"
  else ok "the orphan process is gone"; fi
  systemctl --user is-active --quiet gpu-lease-test-orphan.service \
    && bad "the orphan unit is stopped" "still active" || ok "the orphan unit is stopped"
fi

# ── 7. pre-flight: refuse when something already owns the GPU without a lease ─
if want 7; then
  t "7. acquire refuses when the GPU is already held by a non-adopter"
  squatter_pid="$("$TMP/bin/spawn-fake" gpu-lease-test-squatter 19998)"
  [[ -n "$squatter_pid" ]] && ok "the squatter really started (pid $squatter_pid)" \
                           || bad "the squatter really started" "no pid"
  out=$("$LEASE" run --who t7 --why "should refuse" --resident keep -- true 2>&1); rc=$?
  checkeq "the run refuses with 75" "$rc" "75"
  check "the refusal says why" "$out" "already owns the GPU"
  check "the refusal names the squatter's unit" "$out" "gpu-lease-test-squatter"
  out=$("$LEASE" status 2>&1)
  check "status flags a GPU held with no lease" "$out" "GPU held with NO lease"
  out=$("$LEASE" run --who t7 --why "forced" --resident keep --force -- echo forced-through 2>&1); rc=$?
  checkeq "--force overrides it" "$rc" "0"
  check "--force says what it overrode" "$out" "proceeding although the GPU is already held"
  # The squatter predates the lease, so the sweep must NOT have killed it:
  # only the delta is swept, which is what stops a lease eating another agent's work.
  if systemctl --user is-active --quiet gpu-lease-test-squatter.service; then
    ok "a pre-existing holder survives the lease (delta-only sweep)"
  else bad "a pre-existing holder survives the lease" "it was killed"; fi
  systemctl --user stop gpu-lease-test-squatter.service 2>/dev/null
fi

# ── 8. the idle inhibitor ────────────────────────────────────────────────────
if want 8; then
  t "8. the lease holds the block-mode idle inhibitor the box counts as activity"
  # Every assertion here is scoped to OUR OWN --who. "A block-mode idle inhibitor
  # exists" is a proxy: on the box a sibling stream's live lease satisfies it, so
  # the check passes for the wrong reason and then sends the branch below the
  # wrong way.
  mine() { systemd-inhibit --list --no-pager 2>/dev/null |
             awk '$1=="t8" && $NF=="block" && $6 ~ /(idle|shutdown)/ {f=1} END{exit !f}'; }
  "$LEASE" run --who t8 --why "inhibitor check" --resident keep -- sleep 5 >/dev/null 2>&1 &
  sleep 2
  if mine; then
    ok "our own lock is held, under hatch-idle-poweroff's exact predicate"
    systemd-inhibit --list --no-pager 2>/dev/null | grep -q 'inhibitor check' \
      && ok "the taskboard label (--why) is on it" \
      || bad "the taskboard label is on it" "why not found"
    wait
    mine && bad "the inhibitor is released with the lease" "still held" \
         || ok "the inhibitor is released with the lease"
  else
    # logind refuses it outside a systemd unit (no seat), which is the expected
    # result over ssh. What must hold then is that the verb SAYS so.
    wait
    out=$("$LEASE" run --who t8b --why "inhibitor refusal" --resident keep -- true 2>&1)
    check "when the inhibitor is refused, the lease says so loudly" "$out" "could NOT take the idle inhibitor"
    check "and it names the fix" "$out" "gpu-lease launch"
  fi
fi


# ── 9. usage discipline ──────────────────────────────────────────────────────
if want 9; then
  t "9. the flags that must not be guessable"
  out=$("$LEASE" run --who t9 --why "no resident policy" -- true 2>&1); rc=$?
  check "--resident is required" "$out" "--resident stop|keep|serve is required"
  out=$("$LEASE" run --who "bad tag" --why x --resident keep -- true 2>&1)
  check "--who must be a plain token" "$out" "must be a plain token"
  out=$("$LEASE" run --why x --resident keep -- true 2>&1)
  check "--who is required" "$out" "--who is required"
  out=$("$LEASE" run --who t9 --resident keep -- true 2>&1)
  check "--why is required" "$out" "--why is required"
fi

# ── 10. locality: a lease question must be about the GPU machine ─────────────
if want 10; then
  t "10. the GPU host is resolved, not assumed"
  # A lease answered about the wrong machine does not fail - it answers
  # plausibly and wrongly, which is how `status` reported free from the laptop
  # while the box's lease was held, and `acquire` printed HELD for a GPU that
  # belonged to someone else.
  out=$(HATCH_GPU_HOST=nonexistent-gpu-host "$LEASE" run --who t10 --why "wrong machine" \
        --resident keep -- true 2>&1)
  check "run refuses when the GPU is elsewhere" "$out" "runs its command on THIS machine"
  check "and it names where the GPU is" "$out" "nonexistent-gpu-host"
  out=$(HATCH_GPU_HOST=nonexistent-gpu-host "$LEASE" launch --who t10 --why "wrong machine" \
        --resident keep -- true 2>&1)
  check "launch refuses too" "$out" "runs its command on THIS machine"
  out=$(HATCH_GPU_HOST=nonexistent-gpu-host "$LEASE" run --who t10 --why "forced local" \
        --resident keep --local -- echo ran-locally 2>&1)
  check "--local overrides the resolution" "$out" "ran-locally"
  out=$("$LEASE" status --local 2>&1)
  check "status names the machine it describes" "$out" "machine:  $(uname -n)"
  out=$("$LEASE" status --local --json 2>&1)
  check "so does --json" "$out" "\"machine\":\"$(uname -n)\""
fi

# ── 11. the delegated payload is well-formed ─────────────────────────────────
if want 11; then
  t "11. what actually gets sent to the GPU machine"
  # Tests 1-10 all run with this machine AS the GPU machine, so none of them
  # exercise delegation - which is how an empty forwarded flag shipped: printf
  # with an empty array still emits one line, and the far end saw
  # `unknown option: `. A stub ssh makes the payload assertable without a
  # second machine.
  cat > "$TMP/bin/ssh" <<'EOS'
#!/usr/bin/env bash
# stub ssh: ignore the host, print the payload it would have run
shift $(( $# > 0 ? $# : 0 ))
cat
EOS
  chmod +x "$TMP/bin/ssh"
  run_delegated() { PATH="$TMP/bin:$PATH" HATCH_GPU_HOST=fakebox "$LEASE" "$@" 2>&1; }

  out=$(run_delegated status)
  check "a bare status delegates" "$out" "gpu-lease status --local"
  [[ "$out" == *"''"* ]] && bad "no empty flag is forwarded" "payload contains ''" \
                         || ok "no empty flag is forwarded"
  out=$(run_delegated status --json)
  check "--json is forwarded" "$out" "--json"
  out=$(run_delegated acquire --who t11 --why "quoted reason here" --resident serve --ttl 45m)
  check "acquire delegates" "$out" "gpu-lease acquire --local"
  check "a --why with spaces survives quoting" "$out" "'quoted reason here'"
  check "--resident survives" "$out" "'--resident' 'serve'"
  check "--ttl survives" "$out" "'--ttl' '45m'"
  out=$(run_delegated release --who t11)
  check "release delegates" "$out" "gpu-lease release --local '--who' 't11'"
  # --host must win over the configured host, and still delegate.
  out=$(PATH="$TMP/bin:$PATH" "$LEASE" status --host otherbox 2>&1)
  check "--host overrides the configured GPU host" "$out" "gpu-lease status --local"
fi

# ── 12. demand visibility: waiters register, refresh, and clear ──────────────
if want 12; then
  t "12. waiters are visible while they wait, and gone when they stop"
  WAITD="$HATCH_GPU_RUNDIR/gpu.wait.d"
  "$LEASE" run --who t12hold --why "holder" --resident keep -- sleep 7 >/dev/null 2>&1 &
  holder=$!
  sleep 1.5
  # A refused poller registers; a second refusal preserves first-seen.
  "$LEASE" run --who t12poll --why "poll waiter" --resident keep --wait 0 -- true >/dev/null 2>&1
  [[ -f "$WAITD/t12poll" ]] && ok "a --wait 0 refusal registers the waiter" \
                            || bad "a --wait 0 refusal registers the waiter" "no file in gpu.wait.d"
  since1="$(sed -n 's/^since=//p' "$WAITD/t12poll" 2>/dev/null)"
  sleep 1.1
  "$LEASE" run --who t12poll --why "poll waiter" --resident keep --wait 0 -- true >/dev/null 2>&1
  since2="$(sed -n 's/^since=//p' "$WAITD/t12poll" 2>/dev/null)"
  checkeq "a repeat poll preserves first-seen" "$since2" "$since1"
  # A blocking waiter is visible while queued on the lock.
  "$LEASE" run --who t12block --why "queued waiter" --resident keep --wait 30s -- true >/dev/null 2>&1 &
  blocker=$!
  sleep 1.5
  out=$("$LEASE" status 2>&1)
  check "status lists the poller" "$out" "t12poll (poll waiter) — polling"
  check "status lists the queued waiter" "$out" "t12block (queued waiter) — queued on the lock"
  out=$("$LEASE" status --json 2>&1)
  check "--json carries the waiters" "$out" '"who":"t12block"'
  wait "$holder" "$blocker" 2>/dev/null
  # The blocker acquired and released; its registration must be gone.
  [[ -f "$WAITD/t12block" ]] && bad "a waiter that got the lease is deregistered" "file remains" \
                             || ok "a waiter that got the lease is deregistered"
  # A dead blocking waiter's file is pruned by pid, not by age.
  printf 'who=t12dead\nkind=block\nwhy=killed waiter\nsince=%s\nlast=%s\npid=999999\n' \
    "$(date +%s)" "$(date +%s)" > "$WAITD/t12dead"
  out=$("$LEASE" status 2>&1)
  [[ "$out" == *t12dead* ]] && bad "a dead blocking waiter is pruned" "still listed" \
                            || ok "a dead blocking waiter is pruned"
  rm -f "$WAITD/t12poll"
fi

# ── 13. demand visibility: resident users ────────────────────────────────────
if want 13; then
  t "13. resident users register, expire, and clear"
  "$LEASE" using --who t13 --why "sweep pass" --ttl 30s >/dev/null 2>&1
  out=$("$LEASE" status 2>&1)
  check "a registered user is listed" "$out" "using the resident: t13 (sweep pass)"
  out=$("$LEASE" status --json 2>&1)
  check "--json carries the user with expiry" "$out" '"who":"t13"'
  check "and the expires field" "$out" '"expires":'
  "$LEASE" done --who t13 >/dev/null 2>&1
  out=$("$LEASE" status 2>&1)
  [[ "$out" == *"using the resident: t13 "* ]] && bad "done deregisters" "still listed" \
                                               || ok "done deregisters"
  "$LEASE" using --who t13b --why "brief" --ttl 1s >/dev/null 2>&1
  sleep 2
  out=$("$LEASE" status 2>&1)
  [[ "$out" == *t13b* ]] && bad "an expired user is pruned" "still listed" \
                         || ok "an expired user is pruned"
  out=$("$LEASE" using --who t13c --ttl 5s 2>&1)
  check "using requires --why" "$out" "--why is required"
fi

# ── 14. using/done delegate like the other read-write verbs ──────────────────
if want 14; then
  t "14. using and done reach the GPU machine"
  cat > "$TMP/bin/ssh" <<'EOS'
#!/usr/bin/env bash
shift $(( $# > 0 ? $# : 0 ))
cat
EOS
  chmod +x "$TMP/bin/ssh"
  run_delegated2() { PATH="$TMP/bin:$PATH" HATCH_GPU_HOST=fakebox "$LEASE" "$@" 2>&1; }
  out=$(run_delegated2 using --who t14 --why "sweeping now" --ttl 5m)
  check "using delegates" "$out" "gpu-lease using --local"
  check "its --why survives quoting" "$out" "'sweeping now'"
  check "its --ttl survives" "$out" "'--ttl' '5m'"
  out=$(run_delegated2 done --who t14)
  check "done delegates" "$out" "gpu-lease done --local '--who' 't14'"
fi

# ── 15. the resident tier is a LIST (co-residency, 2026-08-17) ───────────────
if want 15; then
  t "15. stop takes every standing resident and gives back exactly the ones it took"
  # Two declared residents, only the FIRST running. A lease must stop what is up and
  # must NOT start what was deliberately down — starting the co-resident nobody asked
  # for would cost 30 GiB on a box whose whole problem is memory.
  R1=gpu-lease-test-res1; R2=gpu-lease-test-res2
  RESLIST="$R1.service:18081:ModelOne.gguf $R2.service:18083:ModelTwo.gguf"
  res1_pid="$("$TMP/bin/spawn-fake" "$R1" 18081 ModelOne.gguf)"
  [[ -n "$res1_pid" ]] && ok "resident one is running (pid $res1_pid)" \
                       || bad "resident one is running" "spawn-fake produced no pid"
  out=$(HATCH_RESIDENTS="$RESLIST" HATCH_HEALTH_TIMEOUT=6 \
        "$LEASE" run --who t15 --why "two residents" --resident stop -- true 2>&1)
  check "it stops the resident that was up" "$out" "stopping resident $R1.service"
  [[ "$out" == *"stopping resident $R2.service"* ]] && bad "it leaves the one that was down alone" "it stopped it" \
                                                    || ok "it leaves the one that was down alone"
  if kill -0 "$res1_pid" 2>/dev/null; then bad "the stopped resident's process is gone" "pid alive"
  else ok "the stopped resident's process is gone"; fi
  check "it gives back the one it took" "$out" "restoring $R1.service"
  [[ "$out" == *"restoring $R2.service"* ]] && bad "it does not start a resident that was never up" "it started it" \
                                            || ok "it does not start a resident that was never up"

  t "15b. a standing resident is not a foreign holder (the false alarm co-residency would double)"
  res2_pid="$("$TMP/bin/spawn-fake" "$R2" 18083 ModelTwo.gguf)"
  out=$(HATCH_RESIDENTS="$RESLIST" "$LEASE" run --who t15b --why "coexists" --resident keep -- echo ran-anyway 2>&1); rc=$?
  checkeq "a lease taken beside two live residents succeeds" "$rc" "0"
  [[ "$out" == *"already owns the GPU"* ]] && bad "no refusal blaming a resident" "it refused" \
                                           || ok "no refusal blaming a resident"

  t "15c. a refusal distinguishes one of ours from a stranger"
  # Same model file, running outside any resident unit — which is exactly how the
  # co-resident runs until its unit is stood up.
  ours_pid="$("$TMP/bin/spawn-fake" gpu-lease-test-ourmodel 18099 ModelTwo.gguf)"
  out=$(HATCH_RESIDENTS="$RESLIST" "$LEASE" run --who t15c --why "refusal" --resident keep -- true 2>&1)
  check "it names it as a resident model" "$out" "a resident model"
  check "and prints which model" "$out" "ModelTwo.gguf"
  systemctl --user stop gpu-lease-test-ourmodel.service 2>/dev/null
  stranger_pid="$("$TMP/bin/spawn-fake" gpu-lease-test-stranger 18098 SomeoneElse.gguf)"
  out=$(HATCH_RESIDENTS="$RESLIST" "$LEASE" run --who t15c --why "refusal" --resident keep -- true 2>&1)
  check "an unknown model is a stranger" "$out" "a stranger"
  check "and it is named too" "$out" "SomeoneElse.gguf"
  systemctl --user stop gpu-lease-test-stranger.service 2>/dev/null

  t "15d. serve maintains what is enabled, and says so when there is nothing to maintain"
  # The assertion is that an active resident is not REPLACED — its pid survives. The
  # stand-in serves no /health, so serve reports it as active-but-not-answering, which
  # is the honest reading of a unit that is up and still loading.
  before1=$(pgrep -x "$HATCH_GPU_PROC" | sort | tr '\n' ' ')
  out=$(HATCH_RESIDENTS="$RESLIST" HATCH_HEALTH_TIMEOUT=6 \
        "$LEASE" run --who t15d --why "serve with both up" --resident serve -- true 2>&1)
  after1=$(pgrep -x "$HATCH_GPU_PROC" | sort | tr '\n' ' ')
  checkeq "an already-active resident is not replaced" "$after1" "$before1"
  check "and serve distinguishes active from healthy" "$out" "not yet answering"
  # One unit per call: systemctl rejects the whole request when any unit it names is not
  # loaded, and R1 is transient and already collected here, so a joint stop left R2 running
  # into the next assertion as "a stranger".
  for u in "$R1" "$R2"; do systemctl --user stop "$u.service" 2>/dev/null; done
  out=$(HATCH_RESIDENTS="gpu-lease-test-absent.service:18097:X.gguf" HATCH_HEALTH_TIMEOUT=6 \
        "$LEASE" run --who t15d --why "nothing enabled" --resident serve -- true 2>&1)
  check "serve is loud when nothing is enabled or active" "$out" "brought nothing up"

  t "15e. status and --json describe every resident"
  out=$(HATCH_RESIDENTS="$RESLIST" "$LEASE" status 2>&1)
  check "status lists resident one" "$out" "$R1.service"
  check "status lists resident two" "$out" "$R2.service"
  out=$(HATCH_RESIDENTS="$RESLIST" "$LEASE" status --json 2>&1)
  check "--json carries the residents array" "$out" '"residents":[{"unit":"'"$R1"
fi

# ── 16. restore gives back the recorded set after a killed holder ─────────────
if want 16; then
  t "16. a killed holder's restore gives back exactly what the sidecar recorded"
  # This is the ExecStopPost path. A sidecar naming one unit must restore that one and
  # not the whole list; an OLD sidecar with no stopped= line must still restore the
  # primary rather than nothing at all.
  RESLIST="gpu-lease-test-res1.service:18081:ModelOne.gguf gpu-lease-test-res2.service:18083:ModelTwo.gguf"
  printf 'who=ghost\nwhy=killed mid-lease\nmode=run\nresident=stop\nttl=\npid=999999\nunit=\nsince=%s\nbaseline=\nstopped=gpu-lease-test-res2.service:18083\n' \
    "$(date +%s)" > "$HATCH_GPU_RUNDIR/gpu.lease"
  out=$(HATCH_RESIDENTS="$RESLIST" HATCH_HEALTH_TIMEOUT=6 "$LEASE" restore 2>&1)
  check "it restores the recorded unit" "$out" "restoring gpu-lease-test-res2.service"
  [[ "$out" == *"restoring gpu-lease-test-res1.service"* ]] && bad "it restores only that one" "it started the other too" \
                                                            || ok "it restores only that one"
  printf 'who=ghost\nwhy=stopped nothing\nmode=run\nresident=stop\nttl=\npid=999999\nunit=\nsince=%s\nbaseline=\nstopped=none\n' \
    "$(date +%s)" > "$HATCH_GPU_RUNDIR/gpu.lease"
  out=$(HATCH_RESIDENTS="$RESLIST" HATCH_HEALTH_TIMEOUT=6 "$LEASE" restore 2>&1)
  [[ "$out" == *restoring* ]] && bad "'stopped=none' restores nothing" "it started something" \
                              || ok "'stopped=none' restores nothing"
  printf 'who=ghost\nwhy=old sidecar\nmode=run\nresident=stop\nttl=\npid=999999\nunit=\nsince=%s\nbaseline=\n' \
    "$(date +%s)" > "$HATCH_GPU_RUNDIR/gpu.lease"
  out=$(HATCH_RESIDENTS="$RESLIST" HATCH_HEALTH_TIMEOUT=6 "$LEASE" restore 2>&1)
  check "a sidecar predating the stopped-set restores the primary" "$out" "restoring gpu-lease-test-res1.service"
fi

# ── 17. the review's findings, locked in (2026-08-18) ────────────────────────
if want 17; then
  t "17. --json survives free text, and a broken document is worse than a missing field"
  # Every consumer wraps json.loads in a bare except and degrades to "no lease
  # information" — so invalid JSON does not surface as an error, it silently removes
  # the NO LEASE flag from the taskboard. One quote in a --why used to do it.
  nasty='he said "hi" and \ then left'
  "$LEASE" run --who t17 --why "$nasty" --resident keep -- sleep 4 >/dev/null 2>&1 &
  sleep 1.5
  out=$("$LEASE" status --json 2>&1)
  if printf '%s' "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["why"])' >/dev/null 2>&1; then
    ok "a --why containing quotes and a backslash still parses"
  else bad "a --why containing quotes and a backslash still parses" "$out"; fi
  wait
  # A registration missing its numeric fields must not emit `"since":,`
  mkdir -p "$HATCH_GPU_RUNDIR/gpu.users.d"
  printf 'who=torn\nkind=user\nwhy=partial write\nexpires=%s\n' "$(( $(date +%s) + 300 ))" \
    > "$HATCH_GPU_RUNDIR/gpu.users.d/torn"
  out=$("$LEASE" status --json 2>&1)
  printf '%s' "$out" | python3 -c 'import json,sys; json.load(sys.stdin)' >/dev/null 2>&1 \
    && ok "a registration missing since/last still yields valid JSON" \
    || bad "a registration missing since/last still yields valid JSON" "$out"
  rm -f "$HATCH_GPU_RUNDIR/gpu.users.d/torn"

  t "17b. a --who that is a path does not reach rm"
  printf 'sentinel' > "$TMP/victim"
  out=$("$LEASE" done --who "../../..$TMP/victim" 2>&1); rc=$?
  [[ -f "$TMP/victim" ]] && ok "the file outside the registration dir survives" \
                         || bad "the file outside the registration dir survives" "it was deleted"
  check "and it refuses with the token rule" "$out" "must be a plain token"

  t "17c. a newline in --why cannot inject a field"
  "$LEASE" using --who t17c --why "$(printf 'harmless\nexpires=9999999999')" --ttl 1s >/dev/null 2>&1
  grep -c '^expires=' "$HATCH_GPU_RUNDIR/gpu.users.d/t17c" 2>/dev/null | grep -q '^1$' \
    && ok "the registration has exactly one expires field" \
    || bad "the registration has exactly one expires field" "$(cat "$HATCH_GPU_RUNDIR/gpu.users.d/t17c" 2>/dev/null | tr '\n' '|')"
  sleep 2
  out=$("$LEASE" status 2>&1)
  [[ "$out" == *t17c* ]] && bad "the injected expiry does not make it immortal" "still listed" \
                         || ok "the injected expiry does not make it immortal"

  t "17d. a mistyped duration fails loudly instead of queueing for ever"
  out=$("$LEASE" run --who t17d --why "typo" --resident keep --wait bogus -- true 2>&1); rc=$?
  checkeq "--wait bogus exits 1" "$rc" "1"
  check "and says what it wanted" "$out" "not a duration"
  out=$("$LEASE" acquire --who t17d --why "typo" --resident keep --ttl 45minutes 2>&1)
  check "--ttl is validated too" "$out" "not a duration"

  t "17e. the stopped set is recorded BEFORE anything is stopped"
  # A holder killed part-way through a multi-resident stop must still owe back the set
  # it was taking; recording it afterwards left an empty record and a dead endpoint.
  R1=gpu-lease-test-res1
  RESLIST="$R1.service:18081:ModelOne.gguf gpu-lease-test-res2.service:18083:ModelTwo.gguf"
  "$TMP/bin/spawn-fake" "$R1" 18081 ModelOne.gguf >/dev/null
  HATCH_RESIDENTS="$RESLIST" HATCH_HEALTH_TIMEOUT=6 \
    "$LEASE" run --who t17e --why "record first" --resident stop \
    -- bash -c "grep '^stopped=' '$HATCH_GPU_RUNDIR/gpu.lease' > $TMP/stopped.seen" >/dev/null 2>&1
  check "the sidecar names the set while the job runs" "$(cat "$TMP/stopped.seen" 2>/dev/null)" "$R1.service:18081"
  systemctl --user stop "$R1.service" 2>/dev/null

  t "17f. serve says so per-resident rather than falling silent"
  out=$(HATCH_RESIDENTS="gpu-lease-test-absent.service:18097:X.gguf" HATCH_HEALTH_TIMEOUT=6 \
        "$LEASE" run --who t17f --why "nothing enabled" --resident serve -- true 2>&1)
  check "it names the resident it did not start" "$out" "neither enabled nor active"
fi

# ── 18. a second GPU binary, and the router's holders are not violations ─────
if want 18; then
  t "18. sd-server counts as a holder; llama-swap's children are sanctioned (2026-08-21)"
  # GPU_PROC is an ERE alternation — prove pgrep -x actually matches BOTH names,
  # against two stand-ins with different comms. The sd stand-in runs under the
  # router unit (overridden so the test never touches a real llama-swap) and
  # carries only --diffusion-model, sd-server's split-weights form with no -m.
  cp "$(command -v bash)" "$TMP/bin/fake-sd"
  BOTH="fake-llama|fake-sd"
  RTR="gpu-lease-test-router.service"
  "$TMP/bin/spawn-fake" gpu-lease-test-t18 18201 Stranger.gguf >/dev/null
  systemd-run --user --unit=gpu-lease-test-router --collect \
    "$TMP/bin/fake-sd" -c 'trap "exit 0" TERM INT; while :; do sleep 1; done' fake-sd \
    --diffusion-model /models/z-image.gguf --port 18202 >/dev/null 2>&1
  for i in $(seq 1 20); do pgrep -x fake-sd >/dev/null && break; sleep 0.5; done

  out=$(HATCH_GPU_PROC="$BOTH" HATCH_ROUTER_UNIT="$RTR" "$LEASE" status 2>&1)
  checkeq "status exits 0 with holders present" "$?" "0"
  check "the alternation sees the llama stand-in" "$out" "port 18201"
  check "and the sd stand-in beside it" "$out" "port 18202"
  check "the sd holder's model is read from --diffusion-model" "$out" "z-image.gguf"
  check "the router's child is labelled, not accused" "$out" "the router has models loaded"
  check "the stranger still headlines as the violation" "$out" "NO lease"
  json=$(HATCH_GPU_PROC="$BOTH" HATCH_ROUTER_UNIT="$RTR" "$LEASE" status --json 2>/dev/null)
  check "JSON flags the unleased stranger" "$json" '"unleased_gpu":true'

  systemctl --user stop gpu-lease-test-t18.service 2>/dev/null
  pkill -x "$HATCH_GPU_PROC" 2>/dev/null; sleep 1
  out=$(HATCH_GPU_PROC="$BOTH" HATCH_ROUTER_UNIT="$RTR" "$LEASE" status 2>&1)
  checkeq "status exits 0 when nothing is a violation" "$?" "0"
  json=$(HATCH_GPU_PROC="$BOTH" HATCH_ROUTER_UNIT="$RTR" "$LEASE" status --json 2>/dev/null)
  [[ "$out" == *"NO lease"* ]] && bad "the router's child alone is no violation" "still accused" \
                               || ok "the router's child alone is no violation"
  check "but it is still listed — memory is genuinely held" "$out" "port 18202"
  check "and JSON agrees" "$json" '"unleased_gpu":false'
  systemctl --user stop gpu-lease-test-router.service 2>/dev/null
  pkill -x fake-sd 2>/dev/null
fi

# ── 19. the chat surface is listed, never blocks, and is never swept ─────────
if want 19; then
  t "19. sanctioned holders: visible, non-blocking, not swept (2026-08-22)"
  # The pushback that produced this: llama-swap's holders were treated two ways
  # depending only on whether the entry ran in a toolbox — a container one was
  # hidden entirely, a host one was listed AND refused a lease. One tier, one
  # treatment. The stand-in runs under the fake router unit, which is the host
  # shape; the container shape shares the same predicate.
  RTR=gpu-lease-test-router
  systemd-run --user --unit=$RTR --collect \
    "$TMP/bin/$HATCH_GPU_PROC" -c 'trap "exit 0" TERM INT; while :; do sleep 1; done' \
    fake-llama --port 18301 -m /models/ChatModel.gguf >/dev/null 2>&1
  for i in $(seq 1 20); do pgrep -x "$HATCH_GPU_PROC" >/dev/null && break; sleep 0.5; done
  rpid=$(pgrep -x "$HATCH_GPU_PROC" | head -1)

  out=$("$LEASE" status 2>&1)
  check "it is listed, not hidden" "$out" "port 18301"
  [[ "$out" == *"NO lease"* ]] && bad "it is not called a violation" "still accused" \
                               || ok "it is not called a violation"

  # The load-bearing change: this used to exit 75 and bounce every eval for the
  # length of the chat entry's ttl.
  out=$("$LEASE" run --who t19 --why "not blocked" --resident keep --wait 0 -- true 2>&1); rc=$?
  checkeq "a lease is NOT refused while the chat surface holds a model" "$rc" "0"
  check "but the refusal-free run still says what is loaded" "$out" "sanctioned, not a blocker"

  # A chat model that loads DURING the lease is the case the delta baseline does not
  # cover — it is new, so the sweep reaches it, and it must still be left alone. A
  # pre-existing one is already safe by the baseline rule, so it tests nothing here.
  systemctl --user stop $RTR.service 2>/dev/null; sleep 1
  out=$("$LEASE" run --who t19b --why "sweep safety" --resident keep -- \
        bash -c "systemd-run --user --unit=$RTR --collect \
          '$TMP/bin/$HATCH_GPU_PROC' -c 'trap \"exit 0\" TERM INT; while :; do sleep 1; done' \
          fake-llama --port 18301 -m /models/ChatModel.gguf >/dev/null 2>&1; sleep 2" 2>&1)
  check "the sweep says it left the router's model alone" "$out" "leaving the router's model alone"
  rpid=$(pgrep -x "$HATCH_GPU_PROC" | head -1)
  if [[ -n "$rpid" ]] && kill -0 "$rpid" 2>/dev/null; then
    ok "a chat model that loaded mid-lease survives the sweep"
  else
    bad "a chat model that loaded mid-lease survives the sweep" "the sweep killed it"
  fi

  # The partition must not swallow real violators standing beside it.
  "$TMP/bin/spawn-fake" gpu-lease-test-t19s 18302 Stranger.gguf >/dev/null
  out=$("$LEASE" run --who t19c --why "stranger present" --resident keep --wait 0 -- true 2>&1); rc=$?
  checkeq "a stranger beside the chat surface still refuses" "$rc" "75"
  check "and the refusal names the stranger, not the chat model" "$out" "Stranger.gguf"
  systemctl --user stop gpu-lease-test-t19s.service 2>/dev/null

  # --resident stop reclaims through the router's API. The harness points
  # ROUTER_ADDR at a closed port, so this asserts the loud-not-fatal path.
  out=$(HATCH_HEALTH_TIMEOUT=6 "$LEASE" run --who t19d --why "reclaim" --resident stop -- true 2>&1)
  check "an unreachable router is reported, not swallowed" "$out" "may still be committed"
  systemctl --user stop $RTR.service 2>/dev/null
fi

# ── 20. one source defines what holds the GPU, and each consumer READS it ────
# Behavioural, not textual: an assertion that greps for a source line goes red on a
# reflow and stays green when a consumer hardcodes a name beside the variable. Each
# check here points HATCH_GPU_BINARIES at a throwaway file naming a binary and a
# container that exist nowhere else, then makes the consumer act on it.
if want 20; then
  t "20. gpu-lease, hatch-serving-guard and the taskboard act on gpu-binaries.env"
  ENVF="$PWD/../../lib/gpu-binaries.env"
  GUARD="$PWD/../../infra/model-serving/hatch-serving-guard"
  [[ -r "$ENVF" ]] && ok "engine/lib/gpu-binaries.env exists" \
                   || bad "engine/lib/gpu-binaries.env exists" "not readable at $ENVF"

  # The stand-in server for this test carries a comm no consumer names in its own
  # source, so a consumer that matches it can only have read the file.
  T20SRV=fake-t20-srv
  cp "$(command -v bash)" "$TMP/bin/$T20SRV"

  export FAKE_CONTAINER_NAME=t20-chat-container
  mkdir -p "$TMP/fake"
  cat > "$TMP/fake/podman" <<'EOS'
#!/usr/bin/env bash
# Harness podman: `inspect --format '{{.Id}}' NAME…` answers only for the one container
# the test names, so a consumer that never asks gets nothing to match on.
[[ "${1:-}" == inspect ]] || exit 0
shift
while (( $# )); do
  case "$1" in --format) shift 2; continue ;; esac
  [[ "$1" == "$FAKE_CONTAINER_NAME" ]] && printf '%s\n' "$FAKE_CONTAINER_ID"
  shift
done
exit 0
EOS
  # The guard calls `hatch gpu-lease …`. On the box that would reach the REAL lease and
  # the real GPU; this keeps it inside the harness rundir.
  cat > "$TMP/fake/hatch" <<EOS
#!/usr/bin/env bash
[[ "\$1" == gpu-lease ]] || { echo "harness hatch: only gpu-lease" >&2; exit 1; }
shift
exec "$LEASE" "\$@"
EOS
  chmod +x "$TMP/fake/podman" "$TMP/fake/hatch"

  mkenv() {  # servers containers → path of a throwaway env file
    local f="$TMP/gpu-binaries-$RANDOM.env"
    printf 'HATCH_GPU_SERVERS="%s"\nHATCH_GPU_TOOLS="fake-t20-bench"\nHATCH_CHAT_CONTAINERS="%s"\n' \
      "$1" "$2" > "$f"
    printf '%s' "$f"
  }
  ENV_STRANGER=$(mkenv "$T20SRV" "")
  ENV_OURS=$(mkenv "$T20SRV" "$FAKE_CONTAINER_NAME")

  # NOT a systemd unit: a unit whose ExecStart names a GPU binary is an eval driver to
  # the guard, which is correct and is a different check — a chat entry is launched by
  # llama-swap, not by a user unit. Backgrounded here, so it is only ever a holder.
  "$TMP/bin/$T20SRV" -c 'trap "exit 0" TERM INT; while :; do sleep 1; done' \
    fake-t20 --port 18401 -m /models/T20.gguf >/dev/null 2>&1 &
  t20pid=""
  for _ in $(seq 1 20); do t20pid=$(pgrep -x "$T20SRV" | head -1); [[ -n "$t20pid" ]] && break; sleep 0.5; done
  # The fake container's "id" is a real substring of that process's own cgroup path, so
  # the consumer's real cgroup match is what decides — the wiring under test is
  # file → variable → podman inspect → match, not a string the harness planted twice.
  export FAKE_CONTAINER_ID="$(awk -F/ 'END{print $NF}' /proc/${t20pid:-self}/cgroup 2>/dev/null)"

  if [[ -z "$t20pid" ]]; then
    bad "the stand-in server for test 20 started" "it never appeared in the process table"
  else
    ok "the stand-in server for test 20 started (pid $t20pid)"

    # The guard: a binary it can only know from the file must read as a foreign owner.
    out=$(PATH="$TMP/fake:$PATH" HATCH_GPU_BINARIES="$ENV_STRANGER" "$GUARD" true 2>&1); rc=$?
    checkeq "hatch-serving-guard refuses a holder named only in the file" "$rc" "97"
    check "and the refusal names its pid" "$out" "pid $t20pid"
    # …and the container list from the same file is what makes the same process ours.
    out=$(PATH="$TMP/fake:$PATH" HATCH_GPU_BINARIES="$ENV_OURS" "$GUARD" true 2>&1); rc=$?
    checkeq "the file's container list makes the same holder ours (guard execs)" "$rc" "0"
    [[ "$out" != *REFUSING* ]] && ok "and the guard does not refuse" \
                               || bad "and the guard does not refuse" "$out"

    # The lease: same file, same two arms. HATCH_GPU_PROC is unset for these, or the
    # harness's own override would answer instead of the file.
    out=$(env -u HATCH_GPU_PROC PATH="$TMP/fake:$PATH" HATCH_GPU_BINARIES="$ENV_STRANGER" \
          "$LEASE" status --local 2>&1)
    check "gpu-lease flags the same holder as unleased" "$out" "GPU held with NO lease"
    check "and names it by the model it is serving" "$out" "T20.gguf"
    out=$(env -u HATCH_GPU_PROC PATH="$TMP/fake:$PATH" HATCH_GPU_BINARIES="$ENV_OURS" \
          "$LEASE" status --local 2>&1)
    check "the file's container list makes it the router's instead" "$out" "the router has models loaded"
    [[ "$out" != *"GPU held with NO lease"* ]] && ok "and it is no longer a violation" \
                                                || bad "and it is no longer a violation" "$out"
  fi
  pkill -x "$T20SRV" 2>/dev/null

  # The taskboard reads the same file for the same names.
  if command -v python3 >/dev/null 2>&1; then
    out=$(HATCH_GPU_BINARIES="$ENV_STRANGER" python3 -c "
import sys; sys.path.insert(0, '../../apps/taskboard')
import probe_serves; print(sorted(probe_serves._server_comms()))" 2>&1)
    check "the taskboard reads the server names from it" "$out" "$T20SRV"
    out=$(HATCH_GPU_BINARIES=/nonexistent/x.env python3 -c "
import sys; sys.path.insert(0, '../../apps/taskboard')
import probe_serves; print(sorted(probe_serves._server_comms()))" 2>&1)
    checkeq "and reports an empty set rather than a guess when it cannot" "$out" "[]"
  else
    ok "the taskboard checks are skipped (no python3 here)"
  fi

  # The retired engine is in none of them: it was removed from the host on 2026-08-18
  # and re-added from a stale list, which is the mistake the one file exists to stop.
  # Not the env file itself: it names the retirement in a comment, deliberately.
  hits=$(grep -rl --exclude-dir=__pycache__ 'ds4-server' "${LEASE_SRC[@]}" "$GUARD" \
           ../../apps/taskboard/probe_serves.py 2>/dev/null | tr '\n' ' ')
  [[ -z "$hits" ]] && ok "the retired ds4-server is in none of the consumers" \
                   || bad "the retired ds4-server is in none of the consumers" "$hits"

  # A consumer that cannot read the file fails in its own direction, and says which file.
  out=$(HATCH_GPU_BINARIES=/nonexistent/x.env "$LEASE" status --local 2>&1); rc=$?
  checkeq "gpu-lease refuses without it" "$rc" "1"
  check "and names the file" "$out" "cannot read"
  out=$(HATCH_GPU_BINARIES=/nonexistent/x.env "$GUARD" true 2>&1); rc=$?
  checkeq "hatch-serving-guard refuses without it" "$rc" "97"
  check "and names the file" "$out" "cannot read"
fi

# ── 21. the lease answers `--takes-box`, and the guard asks it ───────────────
# The guard used to grep cmd_status's JSON byte layout, which would have disarmed
# silently on a reflow. It now asks a question the lease owns, and these are the
# assertions that pin the answer.
if want 21; then
  t "21. status --takes-box, and hatch-serving-guard consuming it"
  GUARD="$PWD/../../infra/model-serving/hatch-serving-guard"

  out=$("$LEASE" status --takes-box --local 2>&1); rc=$?
  checkeq "a free lease answers no" "$out" "no"
  checkeq "and exits 1" "$rc" "1"

  out=$("$LEASE" run --who t21a --why "takes the box" --resident stop -- \
        "$LEASE" status --takes-box --local 2>/dev/null); rc=$?
  check "a --resident stop holder answers yes" "$out" "yes"
  checkeq "and exits 0" "$rc" "0"

  out=$("$LEASE" run --who t21b --why "co-tenant" --resident keep -- \
        "$LEASE" status --takes-box --local 2>/dev/null); rc=$?
  check "a --resident keep holder answers no" "$out" "no"
  checkeq "and exits 1" "$rc" "1"

  # A sidecar with no lock is STALE: its holder is gone, so nothing is about to
  # reload a model and the chat surface must not stay closed on it.
  printf 'who=t21c\nwhy=stale\nmode=run\nresident=stop\npid=1\nsince=%s\n' "$(date +%s)" \
    > "$HATCH_GPU_RUNDIR/gpu.lease"
  out=$("$LEASE" status --takes-box --local 2>&1)
  checkeq "a stale sidecar with resident=stop answers no" "$out" "no"
  rm -f "$HATCH_GPU_RUNDIR/gpu.lease"

  out=$("$LEASE" status --takes-box --json --local 2>&1); rc=$?
  checkeq "--takes-box and --json refuse to combine" "$rc" "1"
  check "and say why" "$out" "does not combine"

  # The guard's half. The temp env names a server nothing is running as, so the
  # lease signal is the only thing that can decide the refusal.
  if [[ -d "$TMP/fake" ]]; then FAKEDIR="$TMP/fake"; else
    mkdir -p "$TMP/fake"
    cat > "$TMP/fake/hatch" <<EOS
#!/usr/bin/env bash
[[ "\$1" == gpu-lease ]] || { echo "harness hatch: only gpu-lease" >&2; exit 1; }
shift
exec "$LEASE" "\$@"
EOS
    chmod +x "$TMP/fake/hatch"; FAKEDIR="$TMP/fake"
  fi
  ENV21="$TMP/gpu-binaries-21.env"
  printf 'HATCH_GPU_SERVERS="fake-t21-srv"\nHATCH_GPU_TOOLS="fake-t21-bench"\nHATCH_CHAT_CONTAINERS=""\n' > "$ENV21"

  out=$("$LEASE" run --who t21d --why "guard sees this" --resident stop -- \
        env PATH="$FAKEDIR:$PATH" HATCH_GPU_BINARIES="$ENV21" "$GUARD" true 2>&1); rc=$?
  checkeq "the guard refuses while a --resident stop lease is held" "$rc" "97"
  check "and says it is the lease that decided" "$out" "took the box"

  out=$("$LEASE" run --who t21e --why "guard allows this" --resident keep -- \
        env PATH="$FAKEDIR:$PATH" HATCH_GPU_BINARIES="$ENV21" "$GUARD" true 2>&1); rc=$?
  checkeq "and allows chat while a --resident keep lease is held" "$rc" "0"

  # The discriminating arm: a lease whose `--json` is reformatted (a space after each
  # colon) but whose `--takes-box` answer is unchanged. A guard that reads the JSON's
  # byte layout decides wrongly here and lets the chat load through; one that asks the
  # question decides the same as before. This is what pins the fix rather than the flag.
  mkdir -p "$TMP/fake-reformat"
  cat > "$TMP/fake-reformat/hatch" <<EOS
#!/usr/bin/env bash
[[ "\$1" == gpu-lease ]] || exit 1
shift
if [[ " \$* " == *" --json "* ]]; then
  "$LEASE" "\$@" | sed 's/":"/": "/g'
  exit "\${PIPESTATUS[0]}"
fi
exec "$LEASE" "\$@"
EOS
  chmod +x "$TMP/fake-reformat/hatch"
  out=$("$LEASE" run --who t21f --why "json reformatted" --resident stop -- \
        env PATH="$TMP/fake-reformat:$PATH" HATCH_GPU_BINARIES="$ENV21" "$GUARD" true 2>&1); rc=$?
  checkeq "the decision survives a reformat of status --json" "$rc" "97"

  # An answer that is not `yes`/`no` is "no answer", never "no lease" — the case an
  # exit status alone cannot express, and the reason the word is what gets tested.
  mkdir -p "$TMP/fake-old"
  cat > "$TMP/fake-old/hatch" <<'EOS'
#!/usr/bin/env bash
echo "gpu-lease: unknown option: --takes-box" >&2
exit 1
EOS
  chmod +x "$TMP/fake-old/hatch"
  out=$(PATH="$TMP/fake-old:$PATH" HATCH_GPU_BINARIES="$ENV21" "$GUARD" true 2>&1); rc=$?
  checkeq "a lease too old to know the flag does not brick the surface" "$rc" "0"
  check "and the missing signal is logged, not swallowed" "$out" "proceeding without the lease signal"
fi

# ── 22. the board classifies holders the way the lease does ─────────────────
# The resident, the chat surface and a real violator, told apart by the same
# artefacts: the lease's own `status --json` for the resident tier, and the shared
# file's container list for the chat surface. Only /proc is stubbed.
if want 22 && command -v python3 >/dev/null 2>&1; then
  t "22. probe_serves: residents and the chat surface are not NO LEASE rows"
  mkdir -p "$TMP/fake22"
  ENV22="$TMP/gpu-binaries-22.env"
  printf 'HATCH_GPU_SERVERS="fake-t22-srv"\nHATCH_GPU_TOOLS="fake-t22-bench"\nHATCH_CHAT_CONTAINERS="t22-chat"\n' > "$ENV22"
  cat > "$TMP/fake22/gpu-lease" <<'EOS'
#!/usr/bin/env bash
# A lease that is free, standing one ACTIVE resident on :18081.
printf '{"machine":"t22","state":"free","who":"","residents":[{"unit":"t22-resident.service","port":"18081","state":"active"}],"waiting":[],"users":[],"unleased_gpu":true}\n'
EOS
  cat > "$TMP/fake22/podman" <<'EOS'
#!/usr/bin/env bash
[[ "${1:-}" == inspect ]] || exit 0
shift
while (( $# )); do
  case "$1" in --format) shift 2; continue ;; esac
  [[ "$1" == t22-chat ]] && echo c0ffee0000000000
  shift
done
EOS
  chmod +x "$TMP/fake22/gpu-lease" "$TMP/fake22/podman"

  out=$(PATH="$TMP/fake22:$PATH" HATCH_GPU_BINARIES="$ENV22" python3 -c "
import sys; sys.path.insert(0, '../../apps/taskboard')
import probe_serves
# /proc is the one thing stubbed: these pids do not exist, and the cgroup each would
# have is the input the classification actually reads.
cg = {1: 'libpod-c0ffee0000000000.scope', 2: 'some-eval.service',
      3: 'llama-swap.service', 4: 'libpod-deadbeef00000000.scope', 5: 'young.service'}
probe_serves._unit = lambda pid: cg[pid]
def proc(pid, port, age=9999):
    return {'pid': pid, 'comm': 'fake-t22-srv', 'etimes': age,
            'args': f'fake-t22-srv --port {port} -m /models/T22.gguf'}
rows = probe_serves.poll([proc(1, 18201), proc(2, 18202), proc(3, 18203),
                          proc(4, 18081), proc(5, 18204, age=1)])
for r in rows:
    print(r['id'].split(':')[1], '|', r['detail'])
" 2>&1)

  check "a model in its own container is the router's" "$out" "libpod-c0ffee0000000000.scope | libpod-c0ffee0000000000.scope · :18201 · the router (standing tier, or unloads on ttl)"
  check "so is one under llama-swap's own cgroup" "$out" "llama-swap.service | llama-swap · :18203 · the router (standing tier, or unloads on ttl)"
  check "and a real violator is still NO LEASE" "$out" "some-eval.service | some-eval · :18202 · NO LEASE"
  [[ "$out" != *18081* ]] && ok "the resident on an active resident's port gets no row" \
                          || bad "the resident on an active resident's port gets no row" "$out"
  [[ "$out" != *18204* ]] && ok "and a process younger than MIN_AGE_S gets none either" \
                          || bad "and a process younger than MIN_AGE_S gets none either" "$out"
fi

# ── 23. the resident tier comes from the file too ───────────────────────────
# Same shape as test 20, for the other list that was hand-kept in three places: a
# resident named only in a throwaway file must be exempted by the guard, excluded
# from the lease's holders, and known to the board.
if want 23; then
  t "23. gpu-lease, hatch-serving-guard and the taskboard act on HATCH_RESIDENTS"
  GUARD="$PWD/../../infra/model-serving/hatch-serving-guard"
  T23SRV=fake-t23-srv
  T23UNIT=gpu-lease-test-t23-res
  cp "$(command -v bash)" "$TMP/bin/$T23SRV"
  mkdir -p "$TMP/fake23"
  # Nothing in this test may reach the machine's real lease.
  cat > "$TMP/fake23/hatch" <<EOS
#!/usr/bin/env bash
[[ "\$1" == gpu-lease ]] || exit 1
shift
exec "$LEASE" "\$@"
EOS
  chmod +x "$TMP/fake23/hatch"

  mkresenv() {  # port-of-the-resident → env file naming exactly that resident
    local f="$TMP/gpu-binaries-t23-$1.env"
    { printf 'HATCH_GPU_SERVERS="%s"\nHATCH_GPU_TOOLS="fake-t23-bench"\nHATCH_CHAT_CONTAINERS=""\n' "$T23SRV"
      printf 'HATCH_RESIDENTS="%s.service:%s:T23.gguf:"\n' "$T23UNIT" "$1"; } > "$f"
    printf '%s' "$f"
  }
  ENV_IS_RESIDENT=$(mkresenv 18501)     # names the port the stand-in binds
  ENV_NOT_RESIDENT=$(mkresenv 18502)    # names a different one: same process, a stranger

  # The unit has to be ACTIVE for the exemption — the guard and the lease both require
  # it, so that anything binding a resident's port while its unit is down stays visible.
  systemd-run --user --unit="$T23UNIT" --collect sleep 300 >/dev/null 2>&1
  "$TMP/bin/$T23SRV" -c 'trap "exit 0" TERM INT; while :; do sleep 1; done' \
    fake-t23 --port 18501 -m /models/T23.gguf >/dev/null 2>&1 &
  t23pid=""
  for _ in $(seq 1 20); do t23pid=$(pgrep -x "$T23SRV" | head -1); [[ -n "$t23pid" ]] && break; sleep 0.5; done

  if [[ -z "$t23pid" ]]; then
    bad "the stand-in server for test 23 started" "it never appeared in the process table"
  else
    ok "the stand-in server for test 23 started (pid $t23pid)"
    # The guard. HATCH_RESIDENTS is cleared from the environment so the FILE is what
    # answers — that is the derivation under test.
    out=$(env -u HATCH_RESIDENTS PATH="$TMP/fake23:$PATH" HATCH_GPU_BINARIES="$ENV_IS_RESIDENT" \
          "$GUARD" true 2>&1); rc=$?
    checkeq "the guard exempts a resident the file names by port" "$rc" "0"
    out=$(env -u HATCH_RESIDENTS PATH="$TMP/fake23:$PATH" HATCH_GPU_BINARIES="$ENV_NOT_RESIDENT" \
          "$GUARD" true 2>&1); rc=$?
    checkeq "and refuses the same process when the file names another port" "$rc" "97"
    check "naming it as a foreign holder" "$out" "pid $t23pid"

    # The lease. Its own resident-tier env overrides are cleared for the same reason.
    out=$(env -u HATCH_GPU_PROC -u HATCH_RESIDENTS -u HATCH_RESIDENT_UNIT -u HATCH_RESIDENT_PORT \
          HATCH_GPU_BINARIES="$ENV_IS_RESIDENT" "$LEASE" status --local 2>&1)
    check "gpu-lease reports the file's resident tier" "$out" "$T23UNIT.service active (:18501)"
    [[ "$out" != *"GPU held with NO lease"* ]] && ok "and does not flag its own resident" \
                                               || bad "and does not flag its own resident" "$out"
    out=$(env -u HATCH_GPU_PROC -u HATCH_RESIDENTS -u HATCH_RESIDENT_UNIT -u HATCH_RESIDENT_PORT \
          HATCH_GPU_BINARIES="$ENV_NOT_RESIDENT" "$LEASE" status --local 2>&1)
    check "and flags the same process when the file names another port" "$out" "GPU held with NO lease"
  fi
  pkill -x "$T23SRV" 2>/dev/null
  systemctl --user stop "$T23UNIT.service" 2>/dev/null

  if command -v python3 >/dev/null 2>&1; then
    out=$(env -u HATCH_RESIDENTS HATCH_GPU_BINARIES="$ENV_IS_RESIDENT" python3 -c "
import sys; sys.path.insert(0, '../../apps/taskboard')
import probe_serves; print(sorted(probe_serves._resident_units()))" 2>&1)
    check "the taskboard reads the tier's unit names from it" "$out" "$T23UNIT.service"
  fi

  # An override in the environment still beats the file — the harness's own isolation
  # depends on it, and a file value winning would point every test at the real units.
  out=$(HATCH_RESIDENTS="gpu-lease-test-t23-override.service:18599:X.gguf:" \
        HATCH_GPU_BINARIES="$ENV_IS_RESIDENT" "$LEASE" status --local 2>&1)
  check "HATCH_RESIDENTS in the environment beats the file (lease)" "$out" "gpu-lease-test-t23-override.service"
  out=$(HATCH_RESIDENTS="gpu-lease-test-t23-override.service:18599:X.gguf:" \
        HATCH_GPU_BINARIES="$ENV_IS_RESIDENT" python3 -c "
import sys; sys.path.insert(0, '../../apps/taskboard')
import probe_serves; print(sorted(probe_serves._resident_units()))" 2>&1)
  check "and in the taskboard" "$out" "gpu-lease-test-t23-override.service"
fi

# ── 24. a router-held standing model is tier, in both shapes and in neither ──
# The box has no resident units left: every standing model is a llama-swap entry, so
# HATCH_RESIDENTS is empty and HATCH_ROUTER_RESIDENTS carries the ids. Three things have
# to hold for that to be a deployed state rather than a hole – the verb must report the
# router's models, it must read `loaded` off the router's own /running and not off a
# process list, and it must still REFUSE when neither list names anything, because it
# cannot then tell an idle box from one holding 90 GiB.
if want 24; then
  t "24. HATCH_ROUTER_RESIDENTS: the standing tier with no units"

  # A stand-in router: /running answers whatever this file says. The real one is a
  # systemd unit, so the address override is what keeps the test off it.
  ROUTER_STATE="$TMP/router-running.json"
  echo '{"running":[]}' > "$ROUTER_STATE"
  python3 - "$ROUTER_STATE" > "$TMP/router.port" <<'EOS' &
import http.server, socket, sys, threading
state = sys.argv[1]
class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_GET(self):
        body = open(state, "rb").read()
        self.send_response(200); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    do_POST = do_GET
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
print(port, flush=True)
http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
EOS
  ROUTER_PID=$!
  for _ in $(seq 1 50); do [[ -s "$TMP/router.port" ]] && break; sleep 0.1; done
  RPORT="$(cat "$TMP/router.port")"

  # The router unit must read ACTIVE or router_loaded returns early – llama-swap.service
  # is the one unit really running on the box, so the test borrows a unit it can start.
  systemd-run --user --unit=gpu-lease-test-router --collect sleep 300 >/dev/null 2>&1
  export HATCH_ROUTER_ADDR="127.0.0.1:$RPORT"

  out=$(env -u HATCH_RESIDENT_UNIT HATCH_RESIDENTS="" \
        HATCH_ROUTER_RESIDENTS="qtest qtest-small" "$LEASE" status --local 2>&1)
  check "an empty unit list plus router ids is a working configuration" "$out" "lease: free"
  check "and both ids are listed as the standing tier" "$out" "qtest unloaded"
  check "naming the router they live in" "$out" "in gpu-lease-test-router"

  # THE DISCRIMINATING OBSERVATION: same command, same processes, only /running differs.
  # A check that read a process list instead would answer the same in both worlds.
  echo '{"running":[{"model":"qtest","state":"ready"}]}' > "$ROUTER_STATE"
  out=$(env -u HATCH_RESIDENT_UNIT HATCH_RESIDENTS="" \
        HATCH_ROUTER_RESIDENTS="qtest qtest-small" "$LEASE" status --local 2>&1)
  check "a model the router reports running reads loaded" "$out" "qtest loaded"
  check "and one it does not still reads unloaded" "$out" "qtest-small unloaded"

  out=$(env -u HATCH_RESIDENT_UNIT HATCH_RESIDENTS="" \
        HATCH_ROUTER_RESIDENTS="qtest" "$LEASE" status --local --json 2>&1)
  check "JSON names it by swap_id, not by a port it does not have" "$out" '"swap_id":"qtest"'
  check "and reports its state from the router" "$out" '"state":"loaded"'

  # Both empty: the one state that must refuse rather than report an idle box.
  out=$(env -u HATCH_RESIDENT_UNIT HATCH_RESIDENTS="" HATCH_ROUTER_RESIDENTS="" \
        "$LEASE" status --local 2>&1); rc=$?
  checkeq "both lists empty is a refusal, not a quiet empty tier" "$rc" "1"
  check "and it says which keys are missing" "$out" "HATCH_ROUTER_RESIDENTS"

  kill "$ROUTER_PID" 2>/dev/null
  systemctl --user stop gpu-lease-test-router 2>/dev/null
  unset HATCH_ROUTER_ADDR
  export HATCH_ROUTER_ADDR="127.0.0.1:1"
fi

# ── 25. a lease does not refuse its own restore ─────────────────────────────
# Since every standing model moved into llama-swap, `start_residents` loads through
# `hatch-serving-guard` – and the guard refuses while a `--resident stop` lease holds the box.
# That is this lease refusing the restore it owes. Measured on the box 2026-09-08: the unload
# was correct (77.2 -> 0.5 GiB) and both reloads then 500'd for the full 300 s timeout,
# leaving nothing loaded.
#
# THE OBSERVATION IS TAKEN FROM INSIDE THE RESTORE, by the fake router, at the moment the
# real guard would ask. DO NOT SIMPLIFY THIS to writing `restoring=1` with a printf and
# reading it back: that proves only that `--takes-box` reads the flag, never touches
# `start_residents` – where the flag is set and cleared – and deleting both of those lines
# leaves the test passing three of three with the bug reinstated under a green run.
if want 25; then
  t "25. --takes-box says no during the restore, and yes outside it"

  ASK="$TMP/asked-during-restore"; rm -f "$ASK"
  python3 - "$LEASE" "$ASK" "$HATCH_GPU_RUNDIR" > "$TMP/r25.port" <<'EOS' &
import http.server, json, os, socket, subprocess, sys
lease, ask, rundir = sys.argv[1], sys.argv[2], sys.argv[3]
class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _send(self, body):
        self.send_response(200); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        # qtest reads as loaded, so `--resident stop` records it and therefore OWES it back.
        # Report it: with nothing here no restore runs at all, and the test still finds
        # its string in the log and passes.
        self._send(json.dumps({"running": [{"model": "qtest", "state": "ready"}]}).encode())
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        # Only the RESTORE's own load request. The unload POST that precedes it is a
        # different moment with the opposite correct answer, and recording both would make
        # the assertion depend on ordering rather than on the window.
        if self.path.endswith("/chat/completions"):
            env = {**os.environ, "HATCH_GPU_RUNDIR": rundir}
            out = subprocess.run([lease, "status", "--takes-box", "--local"],
                                 capture_output=True, text=True, env=env).stdout.strip()
            with open(ask, "a") as f:
                f.write(out + "\n")
        self._send(b'{"choices":[{"message":{"content":"ok"}}]}')
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
print(port, flush=True)
http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
EOS
  R25_PID=$!
  for _ in $(seq 1 50); do [[ -s "$TMP/r25.port" ]] && break; sleep 0.1; done
  systemd-run --user --unit=gpu-lease-test-router --collect sleep 300 >/dev/null 2>&1

  out=$(env -u HATCH_RESIDENT_UNIT HATCH_RESIDENTS="" HATCH_ROUTER_RESIDENTS="qtest" \
        HATCH_ROUTER_ADDR="127.0.0.1:$(cat "$TMP/r25.port")" \
        "$LEASE" run --who t25 --why "restore window" --resident stop -- true 2>&1)

  check "the restore actually ran through the router" "$out" "qtest serving"
  checkeq "and the lease answered the guard's question once, from inside it" \
    "$(wc -l < "$ASK" 2>/dev/null | tr -d ' ')" "1"
  checkeq "and answered no, so the guard lets its own restore past" \
    "$(cat "$ASK" 2>/dev/null)" "no"

  # The other half of the pair. Without it this passes just as well against a verb that
  # always answers `no`, which is the guard disarmed.
  out=$("$LEASE" run --who t25b --why "held" --resident stop -- \
        "$LEASE" status --takes-box --local 2>&1)
  check "a held stop-lease outside the restore still takes the box" "$out" "yes"

  # And the flag does not outlive the restore that set it.
  checkeq "the sidecar is gone once the lease releases" \
    "$(ls "$HATCH_GPU_RUNDIR/gpu.lease" 2>/dev/null | wc -l | tr -d ' ')" "0"

  kill "$R25_PID" 2>/dev/null
  systemctl --user stop gpu-lease-test-router 2>/dev/null
fi

# ── 26. a router that cannot answer is a question, never a zero ─────────────
# `/running` empty and `/running` unreachable read identically on stdout, and they have
# opposite consequences: `--resident stop` records the loaded set BEFORE unloading, and the
# unload runs regardless because it queries the address itself. Read as "nothing was
# loaded", an unreachable router costs the box its models with nothing owed back.
if want 26; then
  t "26. an unanswerable router does not read as an empty one"
  systemd-run --user --unit=gpu-lease-test-router --collect sleep 120 >/dev/null 2>&1
  # A port nothing binds: the router unit is up, the address is not answerable. That pair
  # is the state the real box reaches when llama-swap is mid-restart.
  DEAD="127.0.0.1:$(python3 -c "import socket
s = socket.socket(); s.bind(('127.0.0.1', 0)); print(s.getsockname()[1]); s.close()")"

  out=$(env -u HATCH_RESIDENT_UNIT HATCH_RESIDENTS="" HATCH_ROUTER_RESIDENTS="qtest" \
        HATCH_ROUTER_ADDR="$DEAD" "$LEASE" status --local 2>&1)
  check "status says unknown, not unloaded" "$out" "unknown (the router did not answer)"
  out=$(env -u HATCH_RESIDENT_UNIT HATCH_RESIDENTS="" HATCH_ROUTER_RESIDENTS="qtest" \
        HATCH_ROUTER_ADDR="$DEAD" "$LEASE" status --local --json 2>&1)
  check "and JSON says so too" "$out" '"state":"unknown"'

  # The consequence that matters: a stop-lease must still owe back what it took.
  out=$(env -u HATCH_RESIDENT_UNIT HATCH_RESIDENTS="" HATCH_ROUTER_RESIDENTS="qtest" \
        HATCH_ROUTER_ADDR="$DEAD" "$LEASE" run --who t26 --why "dead router" \
        --resident stop -- true 2>&1)
  check "a stop-lease says out loud that it could not ask" "$out" "could not ask"
  check "and assumes them loaded so the restore is still owed" "$out" "so this lease owes them back"
  check "and tries the restore rather than silently skipping it" "$out" "loading qtest through the router"

  systemctl --user stop gpu-lease-test-router 2>/dev/null
fi

# ── 27. a STOPPED router is knowledge, not silence ──────────────────────────
# Three states, not two. Unreachable is "could not ask" and falls back to assuming all
# loaded, so the lease owes them back. STOPPED is different: the router's children died
# with it, so nothing is loaded and nothing is owed. Answering that one "could not ask"
# made a lease invent residents the box was deliberately running without – the single
# thing `stop_residents` says a lease must never do – and each phantom then cost
# `router_load` a full HEALTH_TIMEOUT on release.
if want 27; then
  t "27. a stopped router reports nothing loaded, and owes nothing back"
  systemctl --user stop gpu-lease-test-router 2>/dev/null   # make sure it is NOT running

  out=$(env -u HATCH_RESIDENT_UNIT HATCH_RESIDENTS="" HATCH_ROUTER_RESIDENTS="qtest" \
        "$LEASE" status --local 2>&1)
  check "status says unloaded, not unknown" "$out" "qtest unloaded"
  [[ "$out" != *"did not answer"* ]] && ok "and does not claim the router failed to answer" \
    || bad "and does not claim the router failed to answer" "$out"

  # The consequence. A stop-lease over a stopped router must take nothing and give nothing
  # back, and must not spend HEALTH_TIMEOUT per id discovering that.
  start=$SECONDS
  out=$(env -u HATCH_RESIDENT_UNIT HATCH_RESIDENTS="" HATCH_ROUTER_RESIDENTS="qtest qtest2" \
        "$LEASE" run --who t27 --why "stopped router" --resident stop -- true 2>&1)
  elapsed=$(( SECONDS - start ))
  check "it says there was no resident running" "$out" "no resident was running"
  [[ "$out" != *"loading qtest through the router"* ]] && ok "and invents no resident to restore" \
    || bad "and invents no resident to restore" "$out"
  (( elapsed < 30 )) && ok "and releases promptly (${elapsed}s), not per-id HEALTH_TIMEOUT" \
    || bad "and releases promptly, not per-id HEALTH_TIMEOUT" "${elapsed}s"
fi

# ── 28. the lock is the wrapper's, and no child of the job inherits it ───────
# A job's orphan used to keep the lease for ever: `run` holds the lock on fd 9, which
# is not close-on-exec, so every child of the wrapped command inherited it. The sweep
# could not clear it either – it only looks at GPU holders, and this orphan holds no
# GPU. `status` then read the lock as held while the sidecar named a pid that had
# exited, which is the 2026-08-27 "status can name a dead holder" report.
if want 28; then
  t "28. an orphan of the job does not inherit the lease"
  # setsid, so the orphan survives the wrapper's process group going away – which is
  # what a real crashed job leaves behind, and what makes this more than a cosmetic fd.
  # The orphan reports its own pid to a file. Scanning the process table for `sleep 45`
  # would match another tenant's process on the box, and this suite runs there too.
  rm -f "$TMP/t28.pid"
  "$LEASE" run --who t28 --why "orphan fd" --resident keep -- \
    bash -c "setsid sleep 45 </dev/null >/dev/null 2>&1 & echo \$! > $TMP/t28.pid; exit 0" >/dev/null 2>&1
  orphan="$(cat "$TMP/t28.pid" 2>/dev/null)"
  kill -0 "${orphan:-0}" 2>/dev/null || orphan=""
  # Assert the orphan exists before asserting anything about it: a stand-in that never
  # started would make every assertion below pass by having nothing to inherit the fd.
  if [[ -n "$orphan" ]]; then ok "the job left an orphan behind (pid $orphan)"
  else bad "the job left an orphan behind" "no 'sleep 45' in the process table"; fi
  if lock_free; then ok "and the lease is free anyway"
  else bad "and the lease is free anyway" "the lock is still held after the wrapper exited"; fi
  if [[ -n "$orphan" ]]; then
    held=""
    for fd in /proc/$orphan/fd/*; do
      [[ "$(readlink "$fd" 2>/dev/null)" == "$HATCH_GPU_RUNDIR/gpu.lock" ]] && held="$(basename "$fd")"
    done
    [[ -z "$held" ]] && ok "the orphan holds no descriptor on the lock file" \
      || bad "the orphan holds no descriptor on the lock file" "fd $held points at gpu.lock"
    # Asked WHILE the orphan is alive. After killing it the lock is free either way, so
    # the assertion would pass against the broken code and prove nothing.
    out=$("$LEASE" status --local 2>&1)
    check "and status says free rather than HELD by a pid that has exited" "$out" "lease: free"
    kill -9 "$orphan" 2>/dev/null
  fi
fi

# ── 29. the guard lets a leased job's own restore load a model ───────────────
# Test 25 asks the LEASE the question. This asks the GUARD, which is what actually decides,
# from the same moment. The two answers came apart: `--takes-box` said `no` correctly, and the
# guard refused anyway on its driver scan, because a `launch`ed lease's transient unit is still
# ACTIVE while it restores and the scan source-greps the script that unit wraps. Any eval script
# names a GPU binary. So a `--resident stop` lease could not bring its residents back, the unit
# could not exit until the restore landed, and the box sat with no resident.
#
# Run against the pre-fix guard first: with the `cmd=${es#*path=}` skip removed it records 97
# here, so this test is known to be able to fail.
#
# The binary names come from a throwaway gpu-binaries.env, so the assertion does not move when
# the real portfolio gains an engine. The wrapped job names one of them in command position -
# that is the only property of a leased job this needs.
if want 29; then
  t "29. hatch-serving-guard lets a leased job's own restore through"
  GUARD="$PWD/../../infra/model-serving/hatch-serving-guard"

  if [[ ! -d "$TMP/fake" ]]; then
    mkdir -p "$TMP/fake"
    cat > "$TMP/fake/hatch" <<EOS
#!/usr/bin/env bash
[[ "\$1" == gpu-lease ]] || { echo "harness hatch: only gpu-lease" >&2; exit 1; }
shift
exec "$LEASE" "\$@"
EOS
    chmod +x "$TMP/fake/hatch"
  fi
  ENV29="$TMP/gpu-binaries-29.env"
  printf 'HATCH_GPU_SERVERS="fake-t29-srv"\nHATCH_GPU_TOOLS="fake-t29-bench"\nHATCH_CHAT_CONTAINERS=""\n' > "$ENV29"

  # The wrapped job, named in command position exactly as an eval driver names its server.
  cat > "$TMP/t29-job.sh" <<'EOS'
#!/usr/bin/env bash
# fake-t29-srv -m /nonexistent.gguf   <- what the driver scan greps for
sleep 1
EOS
  chmod +x "$TMP/t29-job.sh"

  RC29="$TMP/t29-guard-rc"; rm -f "$RC29"
  python3 - "$GUARD" "$RC29" "$TMP/fake" "$ENV29" > "$TMP/r29.port" <<'EOS' &
import http.server, json, os, socket, subprocess, sys
guard, rcfile, fakedir, envfile = sys.argv[1:5]
class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _send(self, body):
        self.send_response(200); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        self._send(json.dumps({"running": [{"model": "qtest", "state": "ready"}]}).encode())
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        # Only the restore's own load. The unload POST before it is a different moment.
        if self.path.endswith("/chat/completions"):
            # HATCH_RESIDENTS is left alone: set to an empty string it also reaches the
            # LEASE the guard shells out to, and that reads as "no standing models defined"
            # and answers nothing at all. The throwaway file defines no residents anyway.
            env = {**os.environ, "PATH": fakedir + os.pathsep + os.environ["PATH"],
                   "HATCH_GPU_BINARIES": envfile}
            r = subprocess.run([guard, "true"], capture_output=True, text=True, env=env)
            with open(rcfile, "a") as f:
                f.write("%d\n%s" % (r.returncode, r.stderr))
        self._send(b'{"choices":[{"message":{"content":"ok"}}]}')
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
print(port, flush=True)
http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
EOS
  R29_PID=$!
  for _ in $(seq 1 50); do [[ -s "$TMP/r29.port" ]] && break; sleep 0.1; done
  systemd-run --user --unit=gpu-lease-test-router --collect sleep 300 >/dev/null 2>&1

  # `launch`, not `run`: the transient unit is the whole point. Its environment comes from the
  # user manager, so every harness variable has to be handed over explicitly.
  "$LEASE" launch --who t29 --why "restore under the guard" --resident stop \
    --setenv "HATCH_GPU_RUNDIR=$HATCH_GPU_RUNDIR" \
    --setenv "HATCH_GPU_HOST=$HATCH_GPU_HOST" \
    --setenv "HATCH_GPU_PROC=$HATCH_GPU_PROC" \
    --setenv "HATCH_RESIDENT_UNIT=$HATCH_RESIDENT_UNIT" \
    --setenv "HATCH_ROUTER_UNIT=$HATCH_ROUTER_UNIT" \
    --setenv "HATCH_ROUTER_RESIDENTS=qtest" \
    --setenv "HATCH_ROUTER_ADDR=127.0.0.1:$(cat "$TMP/r29.port")" \
    -- bash "$TMP/t29-job.sh" > "$TMP/t29-launch.out" 2>&1
  for _ in $(seq 1 120); do [[ -s "$RC29" ]] && break; sleep 0.5; done
  # The launched unit outlives the observation: its restore is still polling. Wait for it, or
  # the second arm's `run` queues on the lock for up to HEALTH_TIMEOUT and reads as a hang,
  # and its transient unit runs on into test 30 writing the marker test 30 asserts on.
  for _ in $(seq 1 120); do
    systemctl --user list-units --state=active --plain --no-legend "gpu-lease-t29-*" 2>/dev/null \
      | grep -q . || break
    sleep 0.5
  done
  systemctl --user stop 'gpu-lease-t29-*' 2>/dev/null

  # Existence before value: a restore that never ran leaves an empty file, and reading that as
  # "the guard did not refuse" is the vacuous pass this test exists to avoid.
  if [[ -s "$RC29" ]]; then ok "the guard was asked from inside the restore"
  else bad "the guard was asked from inside the restore" \
    "no exit code recorded; launch said: $(tr '\n' '|' < "$TMP/t29-launch.out" | head -c 300)"; fi
  checkeq "and it let the load through instead of refusing" \
    "$(head -1 "$RC29" 2>/dev/null)" "0"
  # A failing check must print what it saw: the guard's own stderr is the diagnosis.
  [[ "$(head -1 "$RC29")" == 0 ]] || { echo "    --- guard stderr ---"; sed 1d "$RC29" | sed 's/^/    /'; }

  # The other half of the pair. Without it this passes against a guard that never refuses.
  out=$("$LEASE" run --who t29b --why "held, not restoring" --resident stop -- \
        env PATH="$TMP/fake:$PATH" HATCH_GPU_BINARIES="$ENV29" "$GUARD" true 2>&1); rc=$?
  checkeq "a held stop-lease outside its restore still refuses" "$rc" "97"

  # THE ARM THAT KEEPS THE SKIP HONEST. A `--resident keep` or `serve` lease also drives the
  # GPU and its server is DOWN between arms, which is the window this scan exists for. Both
  # policies answer `no` to --takes-box by design, so a skip conditioned on anything but
  # `--restoring` leaves that window with no signal and lets a chat load into the OOM.
  cat > "$TMP/t29-keep.sh" <<'EOS'
#!/usr/bin/env bash
# fake-t29-srv -m /nonexistent.gguf   <- a leased job between its arms
sleep 20
EOS
  chmod +x "$TMP/t29-keep.sh"
  "$LEASE" launch --who t29k --why "keep, between arms" --resident keep \
    --setenv "HATCH_GPU_RUNDIR=$HATCH_GPU_RUNDIR" \
    --setenv "HATCH_GPU_HOST=$HATCH_GPU_HOST" \
    --setenv "HATCH_GPU_PROC=$HATCH_GPU_PROC" \
    --setenv "HATCH_ROUTER_UNIT=$HATCH_ROUTER_UNIT" \
    -- bash "$TMP/t29-keep.sh" > "$TMP/t29k-launch.out" 2>&1
  for _ in $(seq 1 60); do
    systemctl --user list-units --state=active --plain --no-legend "gpu-lease-t29k-*" 2>/dev/null \
      | grep -q . && break
    sleep 0.5
  done
  out=$(env PATH="$TMP/fake:$PATH" HATCH_GPU_BINARIES="$ENV29" "$GUARD" true 2>&1); rc=$?
  checkeq "a --resident keep lease's driver unit still closes the surface" "$rc" "97"
  check "and the refusal still names it a driver" "$out" "eval driver unit"
  systemctl --user stop 'gpu-lease-t29k-*' 2>/dev/null

  kill "$R29_PID" 2>/dev/null
  systemctl --user stop gpu-lease-test-router 2>/dev/null
  systemctl --user reset-failed 'gpu-lease-t29-*' 'gpu-lease-t29k-*' 2>/dev/null
fi

# ── 30. a restore that fails leaves a mark, and `restore` retries it ─────────
# `start_residents` has always returned False when a resident did not come back, and
# `finish_lease` dropped it: the lease unlinked its sidecar, logged `lease released` and
# exited with the JOB's status, so a box with no resident looked exactly like a clean run.
# The only complaint lived in a `--collect`ed transient unit's journal. Exit status stays the
# job's - that is the caller's contract, pinned by test 3 - so the box's state gets its own
# channel: a marker file, a headline in `status`, a field in `--json`, and a retry.
#
# The router answers 500 to the load, which is what the real one did during the deadlock.
# HATCH_HEALTH_TIMEOUT keeps the failing wait to seconds instead of five minutes.
#
# Against the pre-fix code the first assertion fails: no marker is written at all.
if want 30; then
  t "30. a failed restore is visible afterwards, and retryable"

  MODE30="$TMP/t30.mode"; echo fail > "$MODE30"
  python3 - "$MODE30" > "$TMP/r30.port" <<'EOS' &
import http.server, json, socket, sys
mode = sys.argv[1]
class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _send(self, code, body):
        self.send_response(code); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        self._send(200, json.dumps({"running": [{"model": "qtest", "state": "ready"}]}).encode())
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path.endswith("/chat/completions") and open(mode).read().strip() == "fail":
            self._send(500, b'{"error":"upstream command exited prematurely"}')
            return
        self._send(200, b'{"choices":[{"message":{"content":"ok"}}]}')
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
print(port, flush=True)
http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
EOS
  R30_PID=$!
  for _ in $(seq 1 50); do [[ -s "$TMP/r30.port" ]] && break; sleep 0.1; done
  systemd-run --user --unit=gpu-lease-test-router --collect sleep 300 >/dev/null 2>&1
  R30ENV=(env HATCH_RESIDENTS="" HATCH_ROUTER_RESIDENTS="qtest" HATCH_HEALTH_TIMEOUT=6
          HATCH_ROUTER_ADDR="127.0.0.1:$(cat "$TMP/r30.port")")

  # An earlier section's failed restore leaves an identical `owed=router:qtest`, which would
  # satisfy the next two assertions without this lease writing anything.
  rm -f "$HATCH_GPU_RUNDIR/gpu.restore-failed"
  out=$(env -u HATCH_RESIDENT_UNIT "${R30ENV[@]}" \
        "$LEASE" run --who t30 --why "restore fails" --resident stop -- true 2>&1); rc=$?
  checkeq "the job's own exit status is still what the caller gets" "$rc" "0"
  check "and the lease says the box has no resident" "$out" "THE BOX HAS NO RESIDENT"

  MARK="$HATCH_GPU_RUNDIR/gpu.restore-failed"
  if [[ -s "$MARK" ]]; then ok "a marker outlives the lease that failed"
  else bad "a marker outlives the lease that failed" "no $MARK"; fi
  check "and it records what was owed" "$(cat "$MARK" 2>/dev/null)" "owed=router:qtest"

  out=$(env -u HATCH_RESIDENT_UNIT "${R30ENV[@]}" "$LEASE" status --local 2>&1)
  check "status headlines it, with no lease held" "$out" "THE BOX HAS NO RESIDENT"
  out=$(env -u HATCH_RESIDENT_UNIT "${R30ENV[@]}" "$LEASE" status --json --local 2>&1)
  # The OWED SET, not the key: with the fix removed the field is still emitted as an empty
  # object, so matching `"restore_failed":{` passes against the bug.
  check "and --json carries it for the taskboard" "$out" '"owed":"router:qtest"'

  # The retry the status line prescribes. Without this arm the marker would be a dead end:
  # the sidecar is already gone, so plain `restore` used to answer "nothing to do".
  # The guard names the holder by reading `status | head -1`. A headline above the lease line
  # empties that diagnostic at the one moment it is worth having.
  out=$(env -u HATCH_RESIDENT_UNIT "${R30ENV[@]}" "$LEASE" status --local 2>&1 | head -1)
  check "the lease line is still line 1, so the guard can still name the holder" "$out" "lease: "

  # A stop-lease that stopped NOTHING must not clear the alarm: after a failed restore the box
  # is empty, so `active_residents()` records an empty set and `start_residents([])` returns
  # True by having no work to do. That deleted the marker without restoring anything.
  #
  # The empty set has to come from a STOPPED ROUTER, not from empty resident keys: both keys
  # empty is the one state the verb refuses, so the lease would die before reaching the
  # restore and the assertion would pass against the bug. A stopped router answers [] as
  # knowledge, which is the shape this is about.
  systemctl --user stop gpu-lease-test-router 2>/dev/null
  out=$(env -u HATCH_RESIDENT_UNIT "${R30ENV[@]}" \
        "$LEASE" run --who t30b --why "stops nothing" --resident stop -- true 2>&1)
  check "the stop-lease ran and had nothing to stop" "$out" "no resident was running"
  if [[ -s "$MARK" ]]; then ok "and it left the marker alone"
  else bad "and it left the marker alone" "the marker was cleared without restoring anything"; fi
  systemd-run --user --unit=gpu-lease-test-router --collect sleep 300 >/dev/null 2>&1

  # Every transient unit's ExecStopPost is `restore --quiet`. Chasing the marker there makes
  # each unit's stop retry a failure another lease recorded, under a policy that may have said
  # not to touch residents, and blocks the stop for up to HEALTH_TIMEOUT per id.
  echo ok > "$MODE30"
  out=$(env -u HATCH_RESIDENT_UNIT "${R30ENV[@]}" "$LEASE" restore --quiet 2>&1)
  if [[ -s "$MARK" ]]; then ok "restore --quiet leaves the marker to an explicit retry"
  else bad "restore --quiet leaves the marker to an explicit retry" "$out"; fi

  out=$(env -u HATCH_RESIDENT_UNIT "${R30ENV[@]}" "$LEASE" restore 2>&1)
  check "restore retries the recorded set" "$out" "qtest serving"
  if [[ ! -e "$MARK" ]]; then ok "and the marker is gone once they are back"
  else bad "and the marker is gone once they are back" "$(cat "$MARK")"; fi

  kill "$R30_PID" 2>/dev/null
  systemctl --user stop gpu-lease-test-router 2>/dev/null
fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
