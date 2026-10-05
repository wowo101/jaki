# `model-store` – the weights on disk, kept at what `engine/models.toml` declares

**Status: current (2026-10-05).** The `[[weights]]` rows in `engine/models.toml` are the
manifest: one row per file in the store, with its source, its exact byte size and, where known,
its digest. `download-candidates.sh` reads that manifest and makes the store match it; it keeps
no list of its own.

```
download-candidates            fetch every `serving` file that is missing
download-candidates --check    compare the store with the manifest and download nothing
```

`hatch install` puts `download-candidates.sh` on PATH without its extension, and
`download-candidates.service` runs the first form at every boot.

## Two states

| `state` | means | fetched? |
|---|---|---|
| `serving` | the deployed configuration loads it; these ten files are jaki's weights | yes, if missing |
| `archived` | kept on disk and loaded by nothing, so a re-test needs no download | never |

`archived` rows live in a separate file, `engine/models.archive.toml`, beside
`engine/models.toml`. `registry-query.py` adds them when that file is present, so a machine
without it is asked about nothing it never had.

**To retire a file, change its row; deleting the file is not enough.** A `serving` row whose
file is gone is downloaded again at the next boot.

**A file in the store with no row is reported as `undeclared`.** It is not an error, but it is
disk nothing accounts for, and a row deleted instead of marked `archived` leaves exactly this.

## How the fetcher downloads

**It uses `curl -C -`, which resumes a partial file across restarts and reboots.** A partial
download lives at `<name>.part`; only a file at its exact declared size gets its final name. `hf
download` (huggingface_hub 1.x) cannot resume a partial download after its process dies, so on
an unreliable link it starts multi-GB files from zero each time. A file counts as complete at
its exact byte size; a valid GGUF header proves nothing about the rest of the file.

**A file at its final name with the wrong size is quarantined, never resumed.** It was complete
once, so a mismatch means the pin has changed or the file is damaged. It is renamed to
`<name>.badsize`, and the run says what to do. Resuming would be wrong: against a shorter
upstream file, curl 8.22 returns success having written nothing, and the working file would end
up under a name nothing loads. Upstream quantisers replace files in place under the same name,
so a pinned size can stop matching.

**A download that cannot succeed gives up.** A permanent HTTP error leaves nothing to resume,
and retrying it every 15 s forever would hold the machine's sleep inhibitor forever. The fetcher
tries twenty times, then fails and prints the URL. The unit stops after five failed runs in an
hour.

## Digests

`sha256` is the file's LFS object id on Hugging Face. It is checked after every fresh download
for a row that declares one, and on every run only where the row sets `verify_always`.
Re-reading 100 GiB of weights at every boot would cost minutes of disk reads for files nothing
can have changed.

One row sets `verify_always`: `flux-ae.safetensors`. It comes from a third-party mirror, and a
re-upload at the same size would pass a size check. A mismatch quarantines it as `.badhash`, so
the next boot downloads it again and says so. Its original repository, from black-forest-labs,
requires accepting terms in the browser and answers 401 to a plain download.

## Install

```
hatch install
systemctl --user enable --now download-candidates
```

The script is deployed as a symlink into the checkout, so it finds `engine/models.toml` beside
itself; editing the manifest in the checkout is the deployment.

**Nothing on the machine may rewrite either file.** Both are symlinks into the checkout, so a
script that edits one leaves the checkout with local changes, and the next `git pull --ff-only`
refuses.
