# jaki

**Status: current (2026-10-08).** Everything on this page is deployed and exercised on one
machine; the chat model moved to the gufo engine on 2026-09-28. Nobody has yet reproduced the
install from scratch on a second one.

*jaki* (say *YAH-kee*) is a local AI server. It runs on one machine you own and serves text,
images, and speech in and out on one OpenAI-compatible address, with a chat surface for the
browser. Every request is answered on the machine itself. Any program that talks to OpenAI's API
can use it instead.

The name is Jaki Liebezeit's, the drummer of Can. He kept time so the others could go anywhere,
and was never the front; that is the relation this server has to the person using it. It also
expands to *just another KI*, KI being the German for AI. Lowercase, always.

> “A guy came to me and said, ‘You must play monotonous.’ … I started thinking about it. To play
> monotonous, what did he mean? Monotonous. So I started to repeat things.”
>
> – Jaki Liebezeit, in *Krautrock: The Rebirth of Germany*, BBC Four, 2009

## What you get

| ask for | you get | runs on |
|---|---|---|
| `qnext` | chat, drafting, long documents, and it reads images | GPU |
| `qwen3.5-4b` | short mechanical jobs – tagging, cleaning up a transcript – fast and shallow | GPU |
| `zimage` | an image from a written prompt | GPU |
| `deepdml/faster-whisper-large-v3-turbo-ct2` | a transcript of an audio file, German or English | CPU |
| `speaches-ai/Kokoro-82M-v1.0-ONNX` | English speech from text | CPU |
| `speaches-ai/piper-de_DE-thorsten-medium` | German speech from text | CPU |

The chat surface lists only `qnext`. A program reaches the other five by naming them.

Measured between 2026-09-10 and 2026-10-04 on the reference machine:

| | |
|---|---|
| chat, tokens per second | 35 at the start of a conversation, 37 with 29k tokens of context, 33 at 59k |
| reading a long prompt | about 1,200 tokens per second at any length |
| one 768×1024 image | about 30 seconds |
| speech in or out, a short sentence | under 3 seconds |
| memory | 89 GiB of the GPU's 124 for the chat model at its 131k window; 115 at the peak with every model busy at once |
| weights on disk | 120 GiB |

## What you need

- **An AMD Strix Halo machine with 128 GB of unified memory** (the Ryzen AI Max series). jaki is
  measured on one class of machine at a time, and this is it. The chat model alone holds 89 GiB.
- **Linux with a current kernel.** Kernel 6.18.4 or newer and linux-firmware 20260110 or newer;
  older ones carry a bug on this GPU. Any rolling distribution qualifies. The reference machine
  runs CachyOS; the container images also run on Fedora.
- **Three kernel parameters**, so the GPU may use most of the memory: `amd_iommu=off
  amdgpu.gttsize=126976 ttm.pages_limit=32505856`. In the BIOS, set the dedicated graphics
  memory (UMA frame buffer, VRAM carveout) to its smallest value, 512 MB. Without the parameters
  the GPU sees 63 GB and the chat model does not load. `amd_iommu=off` also switches off the NPU
  and PCI passthrough to virtual machines. If you need either, drop that one parameter; prompt
  processing runs a few percent slower without it.
- **Nothing to install for ROCm.** The chat model's engine, gufo, runs in a container that
  carries ROCm 7.2.3; the machine only needs the amdgpu driver's `/dev/kfd` and `/dev/dri`,
  which a current kernel provides. The other GPU pieces are Vulkan builds with their own drivers
  bundled.
- **Packages:** `git`, `python` 3.11 or newer, `curl`, `tar`, `unzip`, `podman`. On Arch-based
  systems `sudo pacman -S git python curl tar unzip podman`; on Fedora `sudo dnf install git
  python3 curl tar unzip podman`.
- **Subordinate user ids for rootless podman:** your user needs a line in `/etc/subuid` and in
  `/etc/subgid`. Most distributions add one when the user is created; if not, `sudo usermod
  --add-subuids 100000-165535 --add-subgids 100000-165535 $USER`. The install checks.
- **Services that outlive your login:** `loginctl enable-linger $USER`. jaki runs as user
  services and comes up at boot only with this set.
- **150 GB of free disk:** 129 GB (120 GiB) for the weights and about 13 GB for container
  images, with some room to spare.
- **An address the machine keeps and your other devices can reach.** Not `127.0.0.1`. A
  Tailscale address is the simplest and is what the reference machine uses: jaki binds it and is
  then reachable from every device on your tailnet and from nothing else. A fixed LAN address
  also works; jaki is then reachable from your LAN, over plain HTTP, and its API checks no key.
- **Time.** The weights are 120 GiB. On a slow link that is overnight; the fetcher resumes
  byte-exact across restarts and reboots, so leaving it alone is fine.

## Install

In the directory that should hold the jaki checkout, run:

```bash
git clone https://github.com/wowo101/jaki.git
cd jaki
./jaki install <your address>
```

`<your address>` is how your other devices reach this machine: an IP address or a hostname, for
example `192.168.1.20`, or a Tailscale address, which starts with `100.`. No port is needed;
jaki serves on 9090 and the chat surface on 3000. `127.0.0.1` and `localhost` are refused,
because no other device could reach them. The script prints each command before it runs it, so
it doubles as the manual install. It:

1. writes `machines/<your-hostname>.toml`, which names the address and the two components this
   machine runs, and enables lingering for your user;
2. runs `./hatch install`, the repository's installer. It links four services and one config
   file from the checkout into place and puts its commands in `~/.local/bin`;
3. fetches the small model's engine, the router and the image server against pinned digests
   (about 90 MB), and the chat model's engine as a container image pinned by digest (about 2.9
   GB); then the speech server's image and its three models (about 3 GB), then the chat
   surface's image (about 4 GB);
4. starts the weights download as a service, 120 GiB, which resumes across restarts and reboots
   and runs again at every boot.

Put `~/.local/bin` on your `PATH` if it is not already; the script says so if it is not. `jaki`
itself is among the commands it puts there, so from here on it runs from any directory.

Watch the weights arrive with `tail -f ~/.local/share/box-model-eval/download.log`. On a link
where `podman pull` keeps dying, `pull-image-resumable <image>` fetches the chat surface's image
blob by blob and resumes where it stopped; run it until it prints `DONE`, then `./jaki install`
again. The install prints the image name when the pull fails.

When the log says the store matches:

```bash
jaki check     # every weight, binary, speech model and deployed file, by name
jaki start     # the router and the chat surface
```

`start` refuses while weights are missing, so a half-fetched store never serves. The chat
surface takes 10 to 20 minutes on its first start, and its port stays closed until it is ready.
Then open `http://<your address>:3000` and **create the first account, which becomes the
admin.** Sign-up stays open for other people on your network; their accounts wait for an admin to
approve them. An approved user sees no model until an admin grants access to it: in Admin
Settings, under Models, open `qnext` and make it public or give it to a group. That setting is
kept in the chat surface's database and survives restarts.

That is the whole install. There is nothing to configure in the browser. `jaki status` shows
the services, what is loaded, and where the download is; `jaki stop` stops the server.

## Check it

`jaki check` runs four checks: every weight file at its exact size, the three binaries and the
chat model's engine image against their pinned digests, the three speech models in the cache,
and every deployed command, service and config file by name.

Then exercise each modality. `jaki smoke` sends one request to each – chat, the small model,
an image, speech out and back in – and checks the content of each answer. By hand, with `$JAKI`
standing for `http://<your address>:9090`:

```bash
# text
curl -sS $JAKI/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"qnext","messages":[{"role":"user","content":"Name three knots."}]}'

# an image – count the decoded bytes, because an empty result is still a 200
curl -sS $JAKI/v1/images/generations -H 'Content-Type: application/json' \
  -d '{"model":"zimage","prompt":"a sloop at anchor","size":"768x1024","n":1}' \
  | python3 -c 'import json,sys,base64; print(len(base64.b64decode(json.load(sys.stdin)["data"][0]["b64_json"])), "bytes of PNG")'

# speech out, then speech in, on the same words. A transcript that says what you
# said exercises both directions and the router in between.
curl -sS $JAKI/v1/audio/speech -o /tmp/en.wav -H 'Content-Type: application/json' \
  -d '{"model":"speaches-ai/Kokoro-82M-v1.0-ONNX","voice":"af_heart",
       "input":"The tide turns at four.","response_format":"wav"}'
curl -sS $JAKI/v1/audio/transcriptions \
  -F model=deepdml/faster-whisper-large-v3-turbo-ct2 -F file=@/tmp/en.wav
```

The first request to each model waits for it to load: 25 to 30 seconds for the chat model when
its weights are in the page cache, 65 to 95 seconds after a boot, and a few seconds for the
speech server. After that each model stays loaded, except the image model, which unloads after
15 idle minutes; the next image then takes about 45 seconds, load included.

The chat surface has one model in its picker, a microphone button, a read-aloud button, and file
uploads that go into the model's context whole.

## Using it from your own programs and agents

Point any OpenAI client at `http://<your address>:9090/v1` with any non-empty API key, and name
a model from the table above. What a program may set per request, what it meets when the machine
is busy, and an example per modality: [`clients.md`](clients.md). Running Pi or another agent
harness on jaki: [`agents.md`](agents.md).

## Sharing it

jaki serves a few people taking turns, or one agent run with some chat beside it.

| model | answers at once | when it is busy |
|---|---|---|
| `qnext` | 2, at about 26 tokens a second each (one alone: about 35) | up to 16 more requests wait; further ones are turned away |
| `qwen3.5-4b` | 1 | up to 9 more wait; further ones are turned away |
| `zimage` | 1, about 30 seconds an image | up to 9 more wait; further ones are turned away |
| speech models, through `speaches` | not measured | up to 10 at a time, shared by all three; further ones are turned away |

**A request that is turned away gets HTTP status `429`, Too Many Requests.** Nothing was queued
for it: the program has to wait and send it again. The router counts the requests being
answered and the ones waiting together, and allows 18 of them for `qnext`, 10 for `qwen3.5-4b`, 10 for `zimage`,
and 10 for the three speech models together; [`clients.md`](clients.md) lists every status a program can meet.

- **Everyone shares one queue.** Requests wait first come, first served, whoever sent them. The
  chat model sees every request as coming from the router, so it cannot take turns between
  people.
- **Chat.** Each answer in the chat surface is one request to the chat model. A conversation's
  title and tags come from the small model, and follow-up suggestions are off. Two people whose answers generate at the
  same moment get about 26 tokens a second each; a third person's answer starts when one of
  them finishes.
- **Agent runs.** A run with sub-agents fills both places (see [`agents.md`](agents.md)). Chat
  then waits behind the run's requests: in one measured run, short requests waited 40 to 230
  seconds behind long prompts. Run one agent run at a time.
- **Batch jobs.** Send mechanical work to `qwen3.5-4b`. A batch on `qnext` takes both places
  from everyone else.
- **Long conversations.** A conversation the memory cache no longer holds comes back from the
  disk cache in under a second. After an agent harness compacts a conversation, its start has
  changed, and the whole prompt is read again: about 50 seconds at 60,000 tokens.
- **Taking the GPU** with a `--resident stop` lease unloads every model, for everyone, until it
  is released.

## Before you rely on it

**The chat model's German is weak.** At its served reasoning effort, one German answer in five
carried a Chinese word in place of a German one, and the German around it reads stiff. English
is unaffected, and speech in and out work in both languages.

**When memory is short, the machine refuses an image.** With every model loaded and busy, memory
use comes within a few GiB of the limit. The small model pauses for each image; an image that
still does not fit gets `503` and a message saying how much memory is free, so the caller can
retry once the other work has finished. An idle image model is unloaded when free memory falls
below 8 GiB.

**The chat surface's settings live only in its service file.** `ENABLE_PERSISTENT_CONFIG=False`
in `open-webui.service` is what makes setup in the browser unnecessary. A change made in Admin
Settings applies at once and is gone at the next restart. To keep a change, edit the service
file and run `systemctl --user restart open-webui`.

## Changing something

| you want to | change | then |
|---|---|---|
| a different model, or different flags | `engine/models.toml`, then `engine/infra/model-serving/llama-swap.yaml` | `systemctl --user restart llama-swap` |
| the German voice in the browser | `AUDIO_TTS_MODEL` to `speaches-ai/piper-de_DE-thorsten-medium` and `AUDIO_TTS_VOICE` to `de_DE-thorsten-medium` in `engine/infra/model-serving/open-webui.service` | `systemctl --user restart open-webui` |
| the machine's address | `serve_url` in your machine's toml | `./hatch install`, then `jaki stop` and `jaki start` |
| a newer small-model engine, router or image server | the row in `[[binaries]]` in `engine/models.toml` | `serving-binaries`, then `./hatch install` (the router config reads the binary's path from what install writes), then `systemctl --user restart llama-swap` |
| a newer chat-model engine | `digest` and `version` of the row in `[[images]]` in `engine/models.toml` | `serving-binaries`, then `./hatch install` (the router config names the image by the digest install writes), then `systemctl --user restart llama-swap` |
| another speech model | `[models.speech].model_ids` in `engine/models.toml` and the `aliases:` list of the `speech` entry in `llama-swap.yaml` | `speaches-models`, then restart `llama-swap` |

The service files and the router config are symlinks into the checkout, so editing the file in
the repository is the deployment. The router reads its config only at start, so restart it after
every edit.

## If it does not come up

| symptom | cause |
|---|---|
| `llama-swap` fails to start, log says `Failed to load environment files` | `hatch install` has not run, so `~/.config/hatch/env` does not exist |
| `llama-swap` fails to start with status 78 | the address is empty; set `serve_url` and re-run `./hatch install` |
| `llama-swap` will not bind | `serve_url` names an address this machine does not hold |
| `jaki check` says `[STALE]` for a binary | the binary there was not unpacked from the pinned archive; run `serving-binaries` |
| a request answers `404 no router for requested model` | the id is not an entry or an alias in `llama-swap.yaml` |
| a model start fails with `upstream command exited prematurely` | a binary, the engine image or a weight file is missing at the path the config names; run `serving-binaries --check` and `download-candidates --check` |
| the chat model never becomes ready | read its own log (below): `/dev/kfd` missing or not writable, or too little memory for its 89 GiB |
| the microphone or read-aloud button fails | `jaki check`, then `curl $JAKI/running` for the `speech` entry |
| the chat surface's port is closed for the first 20 minutes | first-start database build; this is normal |
| nothing runs after a reboot | `loginctl enable-linger $USER` was not set |
| `speaches-models` exits with a permission error on its cache | the `speaches-hf-cache` volume is not owned by the container's user; `podman unshare chown -R 1000:1000 "$(podman volume inspect speaches-hf-cache --format '{{.Mountpoint}}')"`, then run it again |

Logs: `journalctl --user -u llama-swap`, `journalctl --user -u open-webui`, the chat model's
engine `journalctl --user CONTAINER_NAME=hatch-qnext`, the memory watcher `journalctl --user -u
hatch-pressure-watch`, and for the weights `~/.local/share/box-model-eval/download.log`. The
small model and the image server log to `~/.local/state/llama-swap-logs/`.

## What is inside

| piece | what it is | upstream |
|---|---|---|
| the router | one address in front of every model; starts each on its first request | [llama-swap](https://github.com/mostlygeek/llama-swap) v246 |
| the chat model's engine | gufo, a HIP engine written for this GPU, in a podman container | [gufo-org/gufo](https://github.com/gufo-org/gufo) 0.7.0 |
| the small model's engine | llama.cpp, in a build tuned for this GPU | [Nathanw1014/strix-halo-llamacpp](https://github.com/Nathanw1014/strix-halo-llamacpp) v0.7.6.1 |
| the image server | stable-diffusion.cpp, Vulkan build | [leejet/stable-diffusion.cpp](https://github.com/leejet/stable-diffusion.cpp) |
| the speech server | faster-whisper in, Kokoro and Piper out, on the CPU | [speaches](https://github.com/speaches-ai/speaches) 0.8.3 |
| the chat surface | Open WebUI, in a podman container | [open-webui](https://github.com/open-webui/open-webui) |
| the models | Qwen3.8-Flash-Next and Qwen3.5-4B (Unsloth GGUF), Z-Image-Turbo, whisper large-v3-turbo, Kokoro-82M, Piper thorsten | Hugging Face, one row each in `engine/models.toml` |

Each piece is under its own licence. The fetchers check the binaries and the container image
against pinned digests and the weights against pinned sizes, and refuse a file that does not
match.

Every version, flag and measured number is in `engine/models.toml`, which wins wherever a
document disagrees with it. Operating the server – memory, the guard, what each setting costs –
is [`engine/infra/model-serving/README.md`](../../engine/infra/model-serving/README.md).
