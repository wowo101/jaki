# `gpu-lease` – the app behind the command

**Status: current (2026-10-05).** It passes the command's test suite, 215 of 215 assertions on
2026-10-05.

This app implements the lease: the lock, the label file beside it, the waiting and using
registrations, finding who holds the GPU by cgroup and command line, stopping, reloading and
serving the standing models, and passing a question asked on another machine to the GPU machine.
Nothing calls it directly. The command, `engine/tools/gpu-lease/gpu-lease`, is a bash shim that
finds its own path, exports it as `HATCH_GPU_LEASE_VERB`, and runs `gpu_lease.py` with the same
arguments. `launch` and `acquire` write that exported path into their transient units, so a unit
names the command and never this directory. What the lease does and how to use it:
[`engine/tools/gpu-lease/README.md`](../../tools/gpu-lease/README.md).

It uses only the standard library and has no lockfile. The shim runs `python3` directly, not `uv
run --script`: uv would stay in memory as the unit's main process and double the start-up time,
which the guard pays on every model load, for a dependency list that is empty.

| module | owns |
|---|---|
| `gpu_lease.py` | the command line: flags, the subcommands, usage |
| `config.py` | paths, `gpu-binaries.env`, the two keys that declare the standing models and how overrides take precedence, the router's unit and address, timeouts, the processes never stopped, `log` and `die` |
| `holders.py` | who holds the GPU: a process's cgroup unit, its command-line flags, its model name, whether it is sanctioned, `foreign_holders`, GPU memory and MemAvailable |
| `residents.py` | the standing models: which are loaded, stopping, reloading and serving them; the router's `/running`, unload and one-token load |
| `lease.py` | the lock, the label file, the registrations, the sweep, the checks before taking the lease, the inhibitor, taking and finishing |
| `remote.py` | which machine has the GPU, delegation over ssh, passing flags on |

Three rules a change here must keep:

- **The lock's file descriptor is never inheritable.** Python opens it close-on-exec, so no
  child of a wrapped command keeps the `flock` after the wrapper is gone. `pass_fds` or
  `os.set_inheritable` on it would let an orphan hold the lease forever, with `status` saying
  `HELD by unknown`. Test 28.
- **`status --takes-box` reads only the lock and the label file.** The guard calls it on every
  model load with a 5 s timeout. The `restoring=1` line written while a lease reloads the
  standing models is what lets a `--resident stop` lease load them through the guard. Tests 21
  and 25. `urllib.request` is imported only when needed for the same reason; importing it costs
  more than starting the interpreter.
- **`start_residents` returns False on failure, and the caller must act on it.**
  `lease.record_restore` writes `gpu.restore-failed` when a reload fails and removes it when one
  succeeds. A caller that ignores the return value leaves the machine with no standing model,
  exits with the job's status, and records the failure only in the journal of a transient unit
  that is collected when it ends. Test 30.

Tests: `engine/tools/gpu-lease/test-gpu-lease.sh`, which takes the binary to test as
`GPU_LEASE_BIN` (default: the command). The suite is the contract and is not repeated here.
