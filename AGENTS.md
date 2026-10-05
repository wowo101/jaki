# AGENTS.md

This repository is jaki, a local AI server: one OpenAI-compatible address on port 9090 in front
of a chat model, a small model, an image model and speech in and out, all on one AMD Strix Halo
machine.

## If you are asked to use jaki

- Base URL `http://<address>:9090/v1`, any non-empty API key. The address is the one jaki was
  installed with (`serve_url` in `machines/<hostname>.toml`); `127.0.0.1` answers nothing, even
  on the machine itself.
- The `model` field is required and is the route. A wrong or missing id answers `404`.
- `qnext`: chat, tools, images in, 131,072 tokens, two requests at once. Give a tool-calling turn
  `max_tokens` of 4,000 or more. Send `reasoning_effort` `low` or none; `low` is what was
  evaluated.
- `qwen3.5-4b`: short mechanical jobs, 16,384 tokens, no reasoning, one request at a time.
- `zimage`: `/v1/images/generations`, `b64_json`.
- Speech: `/v1/audio/transcriptions` with `deepdml/faster-whisper-large-v3-turbo-ct2`;
  `/v1/audio/speech` with `speaches-ai/Kokoro-82M-v1.0-ONNX` and voice `af_heart`.
- Ask `/running` whether a model is loaded. `/health` answers 200 whatever is loaded.
- Treat a `200` with empty `content` as a failure. Retry `429`, `503` and a `5xx` from the small
  model.

Full contract and examples: `machines/jaki/clients.md`. Harness configuration (Pi):
`machines/jaki/agents.md`.

## If you are asked to work on this repository

- `engine/models.toml` is authoritative for every model, version, flag and measured number. Its
  `[rejected.*]` settings were measured and did worse; do not add them back.
- What to edit and what to run for each kind of change – a model, a flag, an engine, the address
  – is the table under *Changing something* in `machines/jaki/README.md`. Follow it row by row:
  a new engine needs its fetcher and `./hatch install` before the restart, or the old one keeps
  serving.
- Check with `./jaki check`. After changing a model entry, load it once and check the answer's
  content, never only the status code.
- Operating it: `engine/infra/model-serving/README.md`. Installing it:
  `machines/jaki/README.md`.
