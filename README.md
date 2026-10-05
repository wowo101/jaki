# jaki

**Status: current (2026-10-05).** A snapshot of a server that runs on one machine. Every number
here was measured on that machine, and nobody has yet installed jaki from scratch on a second
one.

*jaki* (say *YAH-kee*) is a local AI server. It runs on one machine you own and serves text,
images, and speech in and out on one OpenAI-compatible address, with a chat surface for the
browser. Every request is answered on the machine itself. Any program that talks to OpenAI's API
can use it instead.

The name is Jaki Liebezeit's, the drummer of Can. He kept time so the others could go anywhere,
and was never the front; that is the relation this server has to the person using it. It also
expands to *just another KI*, KI being the German for AI. Lowercase, always.

## Install it

Measured between 2026-09-10 and 2026-10-04 on the reference machine, an AMD Strix Halo mini-PC
with 128 GB of unified memory:

| | |
|---|---|
| chat, tokens per second | 35 at the start of a conversation, 37 with 29k tokens of context, 33 at 59k |
| reading a long prompt | about 1,200 tokens per second at any length |
| one 768×1024 image | about 31 seconds |
| speech in or out, a short sentence | under 3 seconds |
| memory | 89 GiB of the GPU's 124 for the two text models, about 99 with the image model loaded, 106 at the peak of an image, 107.5 with every model busy at once |
| weights on disk | 120 GiB |

In the directory that should hold the jaki checkout, run:

```bash
git clone https://github.com/wowo101/jaki.git
cd jaki
./jaki install <your address>     # then ./jaki check and ./jaki start
```

`<your address>` is how your other devices reach this machine: an IP address or a hostname, for
example `192.168.1.20`, or a Tailscale address, which starts with `100.`. No port is needed;
jaki serves on 9090 and the chat surface on 3000. `127.0.0.1` and `localhost` are refused,
because no other device could reach them.

**How it serves.** One router, [llama-swap](https://github.com/mostlygeek/llama-swap), holds the
address and starts each model on its first request. The chat model, Qwen3.8-Flash-Next, runs on
[gufo](https://github.com/gufo-org/gufo), a HIP engine written for this GPU, in a
[podman](https://github.com/podman-container-tools/podman) container pinned by digest. The small
model, Qwen3.5-4B, runs on [a llama.cpp build tuned for Strix
Halo](https://github.com/Nathanw1014/strix-halo-llamacpp), and the image model, Z-Image-Turbo,
on [stable-diffusion.cpp](https://github.com/leejet/stable-diffusion.cpp); both are Vulkan
binaries on the host. Speech in and out is [speaches](https://github.com/speaches-ai/speaches)
on the CPU, and the chat surface is [Open WebUI](https://github.com/open-webui/open-webui). A
guard refuses any GPU model load the memory cannot hold, and the small model pauses while an
image generates.

## The documents

| document | for |
|---|---|
| [`machines/jaki/README.md`](machines/jaki/README.md) | installing: what the machine needs, the install, the checks, one request per modality, what is inside, what to do when something does not come up |
| [`machines/jaki/clients.md`](machines/jaki/clients.md) | writing a program against jaki |
| [`machines/jaki/agents.md`](machines/jaki/agents.md) | running an agent harness, such as Pi, on jaki |
| [`engine/infra/model-serving/README.md`](engine/infra/model-serving/README.md) | running it day to day: memory, the guard, what each setting costs |
| [`engine/models.toml`](engine/models.toml) | every version, flag and measured number, with the evidence for each; it wins wherever a document disagrees |

This repository is in the public domain under CC0 ([`LICENSE`](LICENSE)); the pieces jaki is
built from keep their own licences. There is no support: no releases, no compatibility promise,
and nobody is obliged to answer an issue. Send a patch as an issue. The maintainer applies it in
the workspace jaki is exported from, and it arrives here with the next snapshot.

**One platform at a time.** jaki supports one platform: the cheapest hardware that runs all of
it. Every number is measured there. Today that is Strix Halo. Following each new platform costs
about one machine a year, which this project may not be able to spend; hardware sponsorship
would pay for it.

## What it is for

A model on your own machine sees everything you give it, and nothing leaves the machine. You can
put a whole document, a transcript or a photographed page into the context, and no provider can
change the price, retire the model or be compelled to hand over what you sent. jaki puts that on
one machine: the largest chat model that fits, a small model for mechanical work, an image model
and speech, all on one address.

Two rules govern its configuration. Every pinned setting has its evidence written beside it in
`engine/models.toml`, so a measured value can be told from a copied default; that is why the
file is long. And every feature that costs something says what it costs, in terms a user
notices: memory, seconds, or a refused request.

jaki is exported from **hatch**, a private workspace in which one person works with research
documents, notes, voice recordings and conversations. jaki is the server that workspace runs on.
This repository holds the part you can run without the rest, as a snapshot with no history.
