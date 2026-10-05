# Using jaki from your own programs

**Status: current (2026-10-05).** The examples, the per-request settings, tool calls, streaming
and the voice list were tested on the reference machine that day. The settings were tested with
[`client-contract-probe.py`](../../engine/infra/model-serving/client-contract-probe.py), which
compares each with a request that leaves it out. The limits, waits and error bodies come from
the router config and the services behind it. For an agent harness, see
[`agents.md`](agents.md).

jaki speaks OpenAI's API. Point a client at `http://<your address>:9090/v1`, give it any
non-empty API key, and name a model. `./jaki client-config openai <address>` prints the two
environment variables most OpenAI clients read. This page says what a client can rely on, what
it may set per request, and what it meets when the machine is busy.

## The address

**jaki checks no key.** Anyone who can reach the address can use every model, and can unload
them through the router's `POST /api/models/unload`. The address you installed with decides who
can reach it: a Tailscale address is reachable from your tailnet alone, a LAN address from your
LAN, over plain HTTP. The router can check keys (`apiKeys` in `llama-swap.yaml`, on every route
except `/health`). jaki sets none, because its own services call the router without a key.

## The models

The `model` field is the route. A request that names no model, or one the router does not have,
gets `404`, so a misconfigured client fails at once and never gets another model's answer. Only
`qnext` appears in `/v1/models`; a client reaches the others by naming them.

| model | for | context | at once | the server sets |
|---|---|--:|---|---|
| `qnext` | chat, drafting, long documents, tool calls, images in | 65,536 | 2 | reasoning effort `low`; temperature 1.0, top_p 0.95, top_k 20 |
| `qwen3.5-4b` | short mechanical jobs: tagging, cleanup, extraction | 16,384 | 1 | thinking off; a system message only as the first message |
| `zimage` | an image from a prompt | – | 1 | 8 steps, CFG scale 1.0 |
| `deepdml/faster-whisper-large-v3-turbo-ct2` | a transcript, German or English | – | – | – |
| `speaches-ai/Kokoro-82M-v1.0-ONNX` | English speech, voice `af_heart` | – | – | – |
| `speaches-ai/piper-de_DE-thorsten-medium` | German speech, voice `de_DE-thorsten-medium` | – | – | – |

`tts-1` and `tts-1-hd` route to Kokoro. `whisper-1` does not route.

**A request may override what the server sets.** On both text models it may set `max_tokens`,
`temperature` and `response_format`; at temperature 0 the same build gives the same answer each
time. On `qnext` it may also set `reasoning_effort` to `low`, `medium` or `xhigh`, either as a
top-level field or inside `chat_template_kwargs`. `minimal` counts as `low`, and `high` and
`max` as `xhigh`. `chat_template_kwargs: {"enable_thinking": false}` turns `qnext`'s thinking
off. The server's sampling values are the model card's; two alternatives were measured and did
worse (`[rejected.presence_penalty]` and `[rejected.low_temperature]` in `engine/models.toml`).

**Reasoning comes back separately**, as `reasoning_content` on the message, and as a
`reasoning_content` field in streamed deltas. A client that displays only `content` shows the
answer without the reasoning. Sending earlier turns back with or without their
`reasoning_content` makes no difference to speed: either way the whole conversation is read from
cache.

**`qnext` answers two requests at once**, at about 26 tokens a second each. Further requests
wait in a queue, and with 16 waiting the next gets `429`. Every request reaches the model
through the router, so all callers share that queue.

## What is served, and what is not

| route | |
|---|---|
| `/v1/chat/completions` | the text models, streaming or not, with tools, images and `response_format` |
| `/v1/images/generations` | `zimage`, answering `b64_json` |
| `/v1/audio/transcriptions` | multipart upload; `model` is a form field |
| `/v1/audio/speech` | `model`, `voice`, `input`, `response_format` |
| `/v1/audio/voices?model=<id>` | the voices of one speech model; without `model` it cannot be routed |
| `/v1/models` | `qnext` only |
| `/running` | what is loaded now; the readiness check |
| `/health` | that the router is up, whatever it holds; never a readiness check |

Not served: embeddings, reranking, `/v1/completions`, and Anthropic's `/v1/messages`.

## When the machine is busy

| you see | because | do |
|---|---|---|
| a long wait on the first request | nothing loads until asked: `qnext` takes 25–30 s with its weights in the page cache, 65–95 s after a boot; `zimage` about 45 s after 15 idle minutes | set a client timeout of several minutes; the router holds a request up to 30 minutes for a load |
| `429` from `qnext` | more than 16 requests waiting | back off and retry |
| `503` with `"type": "server_busy"` on an image | the image would not fit beside what is running; the message says how much memory is free | retry once the other work finishes |
| a slow answer from `qwen3.5-4b`, now and then a `5xx` | the small model pauses while an image generates; a request in that time waits for the image and a reload, and one arriving in the instant it unloads is cut | retry a `5xx`, up to three times |
| `upstream command exited prematurely` | a model could not start: missing files, or no memory | `./jaki check`, then the logs in the guide |
| `200` with empty content | the model spent `max_tokens` on reasoning, or the engine lost its GPU context | check `finish_reason`; treat empty content as a failure, never a success |

Before a batch job, ask `/running` whether its model is loaded, or send a one-token request and
wait for the answer. `/health` answers ready while nothing is loaded, so a job that checks only
`/health` spends its own timeout waiting for the load.

## Examples

With the `openai` Python package. Replace the address.

```python
from openai import OpenAI

jaki = OpenAI(base_url="http://<your address>:9090/v1", api_key="none", timeout=600)

# chat; the reasoning is beside the answer
r = jaki.chat.completions.create(
    model="qnext", messages=[{"role": "user", "content": "Name three knots."}])
print(r.choices[0].message.content)
print(getattr(r.choices[0].message, "reasoning_content", None))

# streaming
for chunk in jaki.chat.completions.create(
        model="qnext", stream=True,
        messages=[{"role": "user", "content": "Describe a harbour in one line."}]):
    delta = chunk.choices[0].delta
    print(delta.content or "", end="", flush=True)

# a mechanical job on the small model, held to a JSON schema
schema = {"type": "object", "additionalProperties": False, "required": ["year", "tags"],
          "properties": {"year": {"type": "integer"},
                         "tags": {"type": "array", "items": {"type": "string"}}}}
r = jaki.chat.completions.create(
    model="qwen3.5-4b", temperature=0,
    messages=[{"role": "user", "content": "Tag this: 'Tide tables for the Baltic, 1987.'"}],
    response_format={"type": "json_schema",
                     "json_schema": {"name": "tags", "strict": True, "schema": schema}})
print(r.choices[0].message.content)

# an image in: a data URL in the message
import base64
png = base64.b64encode(open("page.png", "rb").read()).decode()
r = jaki.chat.completions.create(model="qnext", messages=[{"role": "user", "content": [
    {"type": "text", "text": "What does this page say?"},
    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png}"}}]}])

# an image out
img = jaki.images.generate(model="zimage", prompt="a sloop at anchor", size="768x1024")
open("sloop.png", "wb").write(base64.b64decode(img.data[0].b64_json))

# speech out, then back in
speech = jaki.audio.speech.create(model="speaches-ai/Kokoro-82M-v1.0-ONNX", voice="af_heart",
                                  input="The tide turns at four.", response_format="wav")
open("tide.wav", "wb").write(speech.content)
text = jaki.audio.transcriptions.create(model="deepdml/faster-whisper-large-v3-turbo-ct2",
                                        file=open("tide.wav", "rb"))
print(text.text)
```

## Tool calls

Both text models return OpenAI-shaped `tool_calls` when given `tools`. Use `qnext`. It reasons
before it calls a tool, and if `max_tokens` runs out during that reasoning, the response ends
with `finish_reason: "length"` and no call, so give a tool-calling turn at least 4,000 tokens.
`qwen3.5-4b` also returns calls, but nobody has measured how well it picks a tool or fills in
the arguments.

## What to expect of the answers

`qnext`'s German is weak. At the server's reasoning effort, one German answer in five carried a
Chinese word in place of a German one; at other efforts it has not been measured. English is
unaffected, and speech in and out work in both languages.

`qwen3.5-4b` translates German titles into English and has invented authors, so check what it
extracts before you rely on it. Both models' measurements and caveats are in
[`engine/models.toml`](../../engine/models.toml).
