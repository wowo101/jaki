# `gpu-lease` – sharing the one GPU

**Status: current (2026-10-05).** An advisory lease over the machine's single GPU. The command
is a thin shim over the Python app in
[`engine/apps/gpu-lease/`](../../apps/gpu-lease/README.md).

The machine has one GPU and 124 GB of unified memory, and the models that matter take 32 to 118
GB, so one large model fits at a time. Take the lease before you take the GPU.

```bash
# a job with an end: a probe, a benchmark, an evaluation
hatch gpu-lease launch --who bench --why "context ladder" --resident stop -- bash ./ladder.sh

# an open-ended session
hatch gpu-lease acquire --who batch --why "nightly tagging" --resident serve --ttl 45m
hatch gpu-lease release --who batch

# a variable the job needs inside its unit; nothing else crosses over from this shell
hatch gpu-lease launch --who bench --why n1-probe --resident stop --setenv N=1 -- bash probe.sh

# who holds the GPU, who is waiting, who is using the standing models, who holds it unleased
hatch gpu-lease status

# the one question a script asks in a conditional: has a holder taken the GPU?
hatch gpu-lease status --takes-box   # prints yes or no, exits 0 or 1

# a user of the standing models that holds no lease, making itself visible
hatch gpu-lease using --who nightly-tagger --why "tagging sweep" --ttl 10m
hatch gpu-lease done  --who nightly-tagger
```

## Which subcommand

| you have | use | why |
|---|---|---|
| a command that ends | `launch` | runs it in a transient unit, with cleanup that survives a crash |
| a command that ends, already inside a unit | `run` | `launch`'s inner half; use it only when something else provides the unit |
| a session with no single command | `acquire` … `release` | a unit holds the lock, so it outlives your ssh connection |
| a lease whose holder was killed | `restore` | finishes its cleanup and retries a failed reload; safe to repeat |
| requests to the standing models, no lease | `using` … `done` | shows you in `status`; expires on its own (default `--ttl 15m`), so `done` is a courtesy |

**`launch` and `acquire` start from the user manager's environment, not your shell's.**
`systemd-run --user` inherits nothing from the calling shell, so an exported variable is absent
inside the job, and nothing says so. `--setenv KEY[=VALUE]`, repeatable, is the one way in;
`KEY` alone copies the caller's value. There is no option to copy the whole environment, because
unit properties are readable through `systemctl show`.

**Prefer `launch` to `run`.** Over ssh, logind refuses the idle inhibitor a lease takes, because
an ssh login has no seat. A bare `run` over ssh therefore cannot stop the machine powering off
under the job, and says so. `launch` takes the inhibitor from the user manager, where it
succeeds.

## `status` shows demand as well as the holder

`status` lists four things: the holder; anyone waiting; anyone registered with `using`; and any
process holding the GPU without a lease. A job blocked on the lock registers as waiting while it
blocks. A job refused with `--wait 0` registers as a poller, and repeated polls keep the time it
was first seen, so a consumer that retries every few minutes shows how long it has waited.

These registrations are labels in tmpfs (`gpu.wait.d/`, `gpu.users.d/`). Each is removed when
its owner is gone: a waiter when its pid exits, a poller when it stops retrying, a user when its
`--ttl` runs out. They never change who runs next; the lock decides. `status --json` carries
them as `waiting` and `users`.

## It answers about the GPU machine, wherever you run it

The lock lives in the GPU machine's tmpfs. Asked on another machine, a lease question would read
a different lock and answer `lease: free`, which looks plausible and is wrong. So the command
works out which machine has the GPU, in this order: `--host H`, then `HATCH_GPU_HOST`, then
`HATCH_SERVE_URL` in `engine/.hatch/resolved.env`, which `hatch install` writes. A name, address
or `localhost` that this machine answers to means this machine.

- `status`, `acquire`, `release`, `restore`, `using` and `done` run on the GPU machine over ssh.
  From another machine they need no flags.
- `run` and `launch` run a command here, so they cannot be passed on. Off the GPU machine they
  refuse and say where the GPU is.
- `--local` forces this machine, for a caller that wants to describe only what it can see.
- `status` names the machine it describes, in text and in JSON.

Both machines need the same version deployed (`git pull && hatch install` on each). A delegated
call runs the remote copy, so a flag the remote copy does not know fails there.

## Ask the lease a question; never parse its output

`status --takes-box` prints `yes` or `no`, and exits 0 or 1, for one question: is a lease held
whose holder took the GPU from the standing models? It reads only the lock and its label file,
because its caller, `hatch-serving-guard`, asks it on every model load under a 5 s timeout.

`--restoring` is the guard's other question, and the only one that lets it skip a lease's unit.
The guard scans active units for GPU binaries; a lease's unit wraps a command that names them,
so without the question the guard would refuse the lease's own reload of the standing models.
`--takes-box` would be wrong here: it answers `no` for `--resident keep` and `serve` leases,
whose servers are down between steps, and that gap is exactly what the scan watches for.

Test the printed word, not only the exit status. A missing command, a five-second timeout and a
deployed copy too old to know the flag all exit non-zero, and “no answer” must not read as “no
lease”. A `STALE` label answers `no`, since its holder is gone. A lease that is reloading the
standing models answers `no` too, since it is giving the GPU back. Test 21 feeds the guard a
reformatted `--json` and asserts the guard decides the same, so a guard that parsed the JSON's
layout would fail.

## `--resident` is required, and has no default

Every lease says what happens to the standing models, so the destructive choice is visible where
the lease is taken.

- **`stop`** stops every standing model now, and at release reloads exactly the ones that were
  running, waiting for each to answer. Probes and benchmarks want this. Stopping all of them is
  the safe default, because a lease must guarantee free GPU memory and only the caller knows how
  much it needs; reloading only what it stopped means a lease never starts a model that was
  deliberately off. For a model the router holds, the stop is the router's own unload and the
  reload is a one-token request. The reload can take up to `HEALTH_TIMEOUT` per model, 300 s, so
  600 s for both standing models when the chat model's weights are not in the page cache; a warm
  reload took 23 to 25 s.
- **`serve`** makes sure the standing models are loaded and answering, and leaves them loaded.
  Batch jobs want this, because they need a model answering before they start. For a model the
  router holds, it sends a one-token completion: llama-swap has no load endpoint, and a request
  is also the only proof that the entry serves. For a model with its own unit, it starts the
  unit and polls until the server answers `{"status":"ok"}`. It does not start a unit that is
  neither enabled nor active; if that leaves nothing running, it says so.
- **`--resident keep`** leaves the standing models alone.

### The standing models are a list, in two forms

On the reference machine the router on `:9090` holds both standing models, `qnext` and
`qwen3.5-4b`, each with `ttl: 0`. Neither has its own unit or a fixed port; llama-swap assigns
ports at load time. [`engine/lib/gpu-binaries.env`](../../lib/gpu-binaries.env) declares them,
in two keys that the app and `hatch-serving-guard` read:

| key | form | names |
|---|---|---|
| `HATCH_RESIDENTS` | `unit:port:gguf:container` | a model with its own systemd unit. Each reader takes the fields it needs. Empty on the reference machine. |
| `HATCH_ROUTER_RESIDENTS` | llama-swap model ids | a model the router holds. The id is its whole identity, and `/running` says whether it is loaded. |

A value in the environment overrides the file: either key as a whole, or `HATCH_RESIDENT_UNIT`,
`HATCH_RESIDENT_PORT` and `HATCH_RESIDENT_GGUF` for a machine with one standing model and for
the tests. An override set to empty counts: “this machine has no standing units” is a real
statement.

**Both keys empty is refused.** The lease could then not tell an idle GPU from one holding 90
GiB. Either key alone describes a valid machine.

**A unit named as a standing model is never swept.** The sweep removes orphans, and standing
models are not orphans, so a job that starts one must stop it itself.

### How the lease treats the router's models

The router's processes run in one of two cgroups: a container entry in its container's
(`HATCH_CHAT_CONTAINERS`; the chat model, in `hatch-qnext`), a host binary in
`llama-swap.service`'s (the small model and the image server). Both are treated the same way:

- **listed** in `status` and in refusals, because a holder left out of the list is invisible;
- **never a reason to refuse a lease**: the standing models are meant to be up, the others
  unload on their `ttl`, and the guard refuses loads in the other direction;
- **never swept**, including a model that loads during a lease;
- **unloaded by `--resident stop`** through the router's `POST /api/models/unload`; the lease
  logs GPU memory before and after. The lease reads the router's address from the unit's
  `--listen`, because the router does not bind loopback. It asks the router to let go and never
  kills processes the router would still believe were running.

**The router has three states.** Answering `/running` is the ordinary case. Running but not
answering – a timeout, a torn or non-200 reply – means the lease cannot know what is loaded, so
a `--resident stop` lease assumes every standing model was loaded and reloads them all at
release; a needless reload costs time and shows in the log, while a model left unloaded without
a word would go unnoticed. Stopped means nothing is loaded and nothing is owed, so stopping
llama-swap for maintenance does not make every release wait for a reload.

`persistent: true` on a llama-swap group does not keep a model loaded: on v246 a persistent
entry survives another group's exclusive load, and its own `ttl` still applies. `ttl: 0` keeps a
standing model loaded; `HATCH_ROUTER_RESIDENTS` tells the lease and the guard about it.

**A GPU holder is any `llama-server`, `sd-server` or `gufo` process.** The holder scan matches
these names, and a binary it does not name is invisible: missing from refusals, from `status`
and from the sweep. A new GPU binary, or a router entry with its own container, goes in
`HATCH_GPU_SERVERS` or `HATCH_CHAT_CONTAINERS` in `engine/lib/gpu-binaries.env`. Test 20 points
each reader at a throwaway copy of that file with a name that appears nowhere else, so a reader
that hard-codes a name fails.

## What the lease guarantees

- **A second job waits.** The lock blocks. `--wait 0` fails at once with exit 75 and names the
  holder; `--wait 30m` bounds the wait. `launch` names its unit `gpu-lease-<who>-<pid>`, so two
  jobs from one caller queue at the lock without colliding on the unit name. `acquire` uses
  `gpu-lease-<who>`, because `release --who` has to find it, and refuses while that unit is
  active.
- **Cleanup that survives the job.** The wrapper owns the cleanup, so a job that dies mid-run
  still releases the GPU. A stop signal that arrives before the job starts means it never
  starts; one that arrives during the job is passed on, and the wrapper waits for the job before
  cleaning up. `TimeoutStopSec` is 900 s, so a model reload can finish instead of being killed
  halfway, and `ExecStopPost` runs `restore` however the main process ended.
- **The lock belongs to the wrapper.** Its file descriptor is close-on-exec, so no child of the
  wrapped command inherits it, and an orphan that outlives the wrapper cannot keep the lease.
  Test 28 holds every implementation to this.
- **The sweep touches only what the lease added.** The lease records the GPU-holding processes
  when it is taken; at release it stops any that are new. A process that was there before is a
  refusal when the lease is taken, never a kill at release, so a lease cannot stop another job's
  work.
- **Stops are checked.** The sweep waits until the process is gone and escalates to SIGTERM,
  then SIGKILL, if stopping its unit did not end it; a process inside a container can survive
  its unit stopping while holding the GPU's memory. A process it may not signal, owned by
  another user, is reported and the cleanup goes on.
- **The lease blocks idle sleep.** It holds `systemd-inhibit --what=idle:sleep --mode=block`,
  labelled with your `--why`, so an idle power-off waits for it and anything that lists
  inhibitors shows who holds the GPU and why.

## What it does not do

It is advisory. A job that never takes the lease is unprotected; `status` flags a GPU held
without a lease, which makes that visible. A hard gate is not a goal.

The lock is not first-in, first-out, so a holder that releases and at once takes the lease again
can starve a waiter. There is no priority and no preemption. A long evaluation can make a batch
job wait; the fix for that is scheduling, and `status` shows the holder who is waiting on it.

## State

Everything lives in `$XDG_RUNTIME_DIR/hatch/`, which is tmpfs and is cleared at power-off, when
no lease can be held anyway.

- `gpu.lock` – the lease, and the only authority on held or free.
- `gpu.restore-failed` – `who`, `why`, `owed` and `when`, written when a `--resident stop` lease
  could not reload its standing models. It is a file of its own because the lease's exit status
  belongs to the job: a lease that reloaded nothing still exits with the job's status, and its
  complaint would end with its transient unit. `status` puts it first, `status --json` carries
  it as `restore_failed`, and `restore` retries the recorded models and removes the file.
  Nothing else removes it.
- `gpu.lease` – the label: `who`, `why`, `mode`, `resident`, `ttl`, `pid`, `unit`, `since`,
  `baseline`; `stopped`, the standing models this lease took down, written before it takes them,
  so a killed holder's `restore` reloads the right set; and `restoring=1` while it reloads them.
  `status` reads held or free from the lock, so a label left by a killed holder reads `STALE`,
  never held.
- `gpu.wait.d/<who>`, `gpu.users.d/<who>` – the registrations, written by atomic rename so a
  reader never sees half a file.

## Overrides

`--force` takes the lease although a process already holds the GPU without one, for a holder you
know is yours. It prints what it overrode.

`HATCH_GPU_RUNDIR`, `HATCH_GPU_PROC`, `HATCH_GPU_BINARIES`, `HATCH_ROUTER_UNIT`,
`HATCH_ROUTER_ADDR`, `HATCH_HEALTH_TIMEOUT`, `HATCH_RESIDENT_UNIT` and `HATCH_RESIDENT_PORT`
exist for the tests; nothing else should set them. `HATCH_ROUTER_ADDR` in particular keeps a
test run on the GPU machine from unloading the live router's models. The shim sets
`HATCH_GPU_LEASE_VERB`, the path a transient unit calls back into. `HATCH_REPO` overrides where
`resolved.env` and `gpu-binaries.env` are looked for.

## Tests

`./test-gpu-lease.sh` runs 30 tests and needs no GPU. On the GPU machine over ssh one assertion
is skipped: test 8's inhibitor branch, which logind refuses there. `GPU_LEASE_BIN=<path>` points
the suite at another implementation; the suite is the contract every implementation must pass.
Tests 19 to 30 each fail against the defect they cover, which shows they can fail.

The suite's stand-in GPU server is a copied binary with a server's name. A script would not do:
its process name is its interpreter's, `pgrep -x` would match nothing, and every sweep assertion
would pass by finding no work. Two assertions guard against that empty pass.

What the suite cannot test, a real model's memory coming back, is checked with a `--resident
stop` launch on the GPU machine: GPU memory 76.4 → 0.5 → 76.4 GiB, both standing models reloaded
through the guard, the lease released (last run 2026-09-10).
