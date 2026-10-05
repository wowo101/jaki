# `hatch` – the machine provisioner

**Status: current (2026-10-05).**

`hatch` sets up a machine: it works out what this host is from its `machines/<hostname>.toml`,
deploys the commands, apps, services and plugins that file lists, and writes the resolved config
every deployed piece reads. In a jaki checkout, use `./jaki install`: it writes that file and
runs `hatch install` for you, and `./hatch init` refuses there, because the template it copies
is not part of jaki.

It is Python 3.11 or newer with the standard library only, so nothing needs installing before it
runs.

## Setting up a fresh machine

1. **Install the prerequisites:** `git` and Python 3.11 or newer, plus Node and pnpm if the
   machine builds plugins. On Arch-based systems `sudo pacman -S git python nodejs`; on macOS
   `brew install git node python`.
2. **Clone** the repository and `cd` into it.
3. **`./hatch init`** writes `machines/<hostname>.toml` from the template. Review it and set the
   role and endpoints.
4. **`./hatch install`** builds and deploys what this machine's toml lists and writes the
   resolved config. It also puts the `hatch` command in `~/.local/bin`, so after this first run
   `hatch <subcommand>` works from anywhere.

`./hatch doctor` checks the machine at any time and changes nothing.

**Taking over an earlier install.** `install` refuses to overwrite a regular file where it would
put a symlink, because someone may have placed it by hand. If the file is left over from a copy
made before `hatch` managed the path, `hatch install --adopt` replaces it with the symlink.
`install` always refuses a real directory, such as a hand-installed plugin; remove it yourself
if `hatch` should own the path.

## Subcommands

| command | effect |
|---|---|
| `hatch doctor` | reports the machine's health and one line per declared artefact; changes nothing |
| `hatch init` | writes `machines/<hostname>.toml` from the template |
| `hatch install` | deploys the `hatch` command and the commands, apps, services and plugins this machine lists; writes `engine/.hatch/resolved.env` and each plugin's `data.json`. Safe to repeat; `--adopt` replaces leftover regular files |
| `hatch status` | shows this machine's resolved config |

## What `doctor` reports

`doctor` repairs nothing; `hatch install` is safe to repeat and is the repair. It reports on two
things.

**The machine:** Python 3.11 or newer; `machines/<host>.toml` present and parsing; `git`;
`node`; `~/.local/bin` on PATH; the `hatch` command deployed and resolving, which nothing else
checks because no machine lists it; whether DEVONthink is available; and whether
`engine/.hatch/resolved.env` exists and still matches what the machine's toml resolves to. A
toml edited without a re-install is reported as stale, because every deployed piece keeps
reading the old file until `install` runs again.

**The artefacts.** For each entry under `verbs`, `apps`, `infra` and `plugins` in the machine's
toml, `doctor` loads its `provision.toml`. The list an entry is in says where it lives; the
manifest's `kind` says how it deploys. They agree except for `infra`, whose components live in
`engine/infra/<name>` and deploy like apps. For each artefact it checks:

- **Required binaries.** A missing `requires` entry prints `·` and names the binary. The
  artefact is not deployed on this machine on purpose, so this is not a failure and does not
  change the exit code.
- **Deployed files.** Every symlink `install` writes – a command on PATH, an app's shims,
  systemd user units, `[[files]]` destinations, a plugin's link into each vault and its build
  output – must exist, resolve, and point at the expected source. A dangling link would
  otherwise go unnoticed until something used it.
- **PATH.** The first match for the name must be the file `hatch` deployed, not an earlier one
  that hides it. Two entries linked to the same source do not count.
- **`needs`.** The `resolved.env` keys the artefact reads must be present and non-empty. There
  are three failures, each with its own fix: a key `resolve()` never writes is a fault in the
  manifest; a key missing from the file needs a re-install; an empty value needs the machine's
  toml edited. The empty case matters most, because `resolve()` writes an empty string for every
  endpoint the toml leaves out, so the file looks complete while the feature is off.
- **Unused fields.** A manifest field the artefact's kind never reads, such as `units` on a
  `verb`, which deploys nothing and raises no error.

A **drift** section then lists symlinks into this repository that no declared artefact claims,
in `~/.local/bin`, `~/.config/systemd/user` and every directory a `[[files]]` entry writes into.
Removing an artefact from a machine's toml leaves its symlink behind, because `hatch` has no
uninstall and `install` visits only what is declared; this section is the only place that shows
it.

`doctor` sees symlinks only. A hand-copied file carries no link back to the repository and
cannot be attributed; since everything `hatch` places is a symlink, a regular file at a deployed
path is reported against the artefact that claims it. Links into other repositories, such as a
dotfiles checkout, are ignored on purpose.

Exit 0 when everything passes or is a declared missing binary; exit 1 otherwise.

## Tests

The standard library's `unittest`, with no other dependencies. From the repository root:

```bash
python3 -m unittest discover -s engine/provisioner/tests -t .
```

**The 3.11 floor is checked by compiling the package with a real Python 3.11**, not by the
interpreter running the suite. `tomllib` sets the floor, and a syntax feature newer than 3.11
passes every test on a newer Python and then makes every `hatch` subcommand a `SyntaxError` on a
3.11 machine; f-strings in the 3.12 style have done that. `ast.parse(feature_version=(3, 11))`
does not catch it, because the change is in the tokenizer. `test_floor.py` finds a 3.11 on PATH
or through `uv python find 3.11` (`uv python install 3.11` provides one). Without one, the test
skips with a warning that the floor is unverified, instead of passing.
