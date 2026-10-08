# jaki

*jaki* (pronounced *YAH-kee*) is a local AI server.

It runs on affordable hardware and serves text,
images, and speech in and out on one OpenAI-compatible address, with a chat surface for the
browser. It's suitable for individuals or small teams. Requests are answered by an open-weights LLM with near-frontier capabilities (as of 10/2026) and a few specialised models, on a machine you own. Any program that talks to OpenAI's API
can use jaki instead.

If you're new to local AI or wonder why you should be interested in it in the first place, here are a [few](https://www.youtube.com/watch?v=a-Lj9moBlqE) [good](https://www.youtube.com/watch?v=CVeZfM0pVyU) [intros](https://www.youtube.com/watch?v=SLwuR7xFXUI). Short version: LLMs and LLM-based agents are powerful technology that shouldn't be enclosed and controlled by [broligarchs](https://en.wikipedia.org/wiki/Broligarchy) - you should be able to use the technology under your conditions and not be dependent on the goodwill of a platform.

If you wonder about the name: It's [Jaki Liebezeit's](https://en.wikipedia.org/wiki/Jaki_Liebezeit), the drummer of Can and inventor of the "motorik" beat. He kept time so the others could go anywhere, never at the front, but always helping "the musicians come to that one point where the beat is. And play like they come together and make a unit." That is the relation jaki aspires to have with the people using it. (jaki also
expands to *just another KI*, KI being the German for AI.)

## Installation

jaki runs on Linux on [**AMD Strix Halo**](https://strixhalo.wiki/) machines
with 128 GB of unified memory, the currently most affordable platform for running models with near-frontier capabilities at usable speeds.

To install jaki, in the directory that should hold its checkout, run:

```bash
git clone https://github.com/wowo101/jaki.git
cd jaki
./jaki install <your address>
```

`<your address>` is how your other devices reach this machine: an IP address or a hostname, for
example `192.168.1.20`, or a Tailscale address, which starts with `100.`. No port needs to be declared;
jaki serves on 9090 and the chat surface on 3000. `127.0.0.1` and `localhost` are refused,
because no other device could reach them.

The install ends by downloading 120 GiB of weights, which can take hours. Once they are in,
`jaki check` and `jaki start` finish the job. The install puts `jaki` in `~/.local/bin`; until
that is on your PATH, run it as `./jaki` from the checkout. The
[guide](machines/jaki/README.md) walks through every step.

## Concurrent use

jaki serves a few people taking turns, or one agent run with some chat beside it. Requests
waiting for the chat model are answered first come, first served, whoever sent them.

| model | job | answers at once | when it is busy |
|---|---|---|---|
| `qnext` | chat, drafting, long documents, reading images, agent runs | 2, at about 26 tokens a second each (one alone: about 35) | up to 16 more requests wait; further ones are turned away |
| `qwen3.5-4b` | short mechanical jobs: tagging, cleaning up a transcript | 1 | up to 9 more wait; further ones are turned away |
| `zimage` | an image from a written prompt | 1, about 30 seconds an image | up to 9 more wait; further ones are turned away |
| speech models, through `speaches` | Whisper for transcripts of German or English audio; Kokoro for English speech, Piper for German | not measured | up to 10 at a time, shared by all three; further ones are turned away |

You can read more on what this means for chat, agent runs and batch jobs, and what a program sees when it is turned
away in the [*Sharing it* section of the guide](machines/jaki/README.md#sharing-it).

## Performance

All numbers are for the main model (Qwen3.8 Flash Next) plus specialised models where needed for a task, and measured on my Strix Halo mini-PC with 128 GB RAM, jaki's reference machine.

| | |
|---|---|
| Response generation | ~35 tokens per second, stable across context lengths |
| Prompt processing | ~1,200 tokens per second at any length |
| 768×1024 image | ~30 seconds |
| Speech in or out (a short sentence) | under 3 seconds |
| Memory use | 89 GiB of the GPU's 124 for the chat model and its 131k window; 115 at the peak with every model busy at once |
| Weights on disk | 120 GiB |

## Architecture

One router, [llama-swap](https://github.com/mostlygeek/llama-swap), holds the
address and starts each model on its first request.

* The main model, [Qwen3.8-Flash-Next](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF), runs on
[gufo](https://github.com/gufo-org/gufo), a HIP engine written for the Strix Halo GPU, in a
[podman](https://github.com/podman-container-tools/podman) container pinned by digest. 
* A complementary small
model for delegated and batch tasks, [Qwen3.5-4B](https://huggingface.co/unsloth/Qwen3.5-4B-GGUF), runs on [a llama.cpp build tuned for Strix
Halo](https://github.com/Nathanw1014/strix-halo-llamacpp), and the image model, [Z-Image-Turbo](https://huggingface.co/leejet/Z-Image-Turbo-GGUF),
on [stable-diffusion.cpp](https://github.com/leejet/stable-diffusion.cpp); both are Vulkan
binaries on the host.
* Speech in and out is [speaches](https://github.com/speaches-ai/speaches)
on the CPU, and the chat surface is [Open WebUI](https://github.com/open-webui/open-webui).

A guard refuses any GPU model load the memory cannot hold and pauses the small model when needed, e.g. while an
image generates.

All components used in jaki are Open Source so you have full transparency about what's happening "behind the scenes" and with your data.

## Further documents

| document | for |
|---|---|
| [`machines/jaki/README.md`](machines/jaki/README.md) | installing: what the machine needs, the install, the checks, one request per modality, what is inside, what to do when something does not come up |
| [`machines/jaki/clients.md`](machines/jaki/clients.md) | writing a program against jaki |
| [`machines/jaki/agents.md`](machines/jaki/agents.md) | running an agent harness, such as Pi, on jaki |
| [`engine/infra/model-serving/README.md`](engine/infra/model-serving/README.md) | running it day to day: memory, the guard, what each setting costs |
| [`engine/models.toml`](engine/models.toml) | every version, flag and measured number, with the evidence for each; it wins wherever a document disagrees |

## Project context

jaki is exported from **hatch**, an exercise in [*keep engineering*](https://keep.engineering) and my private knowledge workspace in which I work with research
documents, cross-linked notes, conversations and transcriptions. jaki is the server that workspace runs on, and independent from its other tools.
This repository is a snapshot of jaki's current configuration in hatch with no history carried over.

## License, support and contributions

This repository is in the public domain under a [CC0 license](LICENSE); the pieces jaki is
built from keep their own licences.

**Important caveat:** Since I develop jaki as part of a personal project, I can't offer any official support for it: no releases, no compatibility promise,
and nobody is obliged to answer an issue.

If you want to **contribute** a fix or improvement, send a patch as an issue. I'll check it and, if it passes, apply it in
the workspace jaki is exported from. You'll be sure of my gratitude, and the change arrives here with the next snapshot.
