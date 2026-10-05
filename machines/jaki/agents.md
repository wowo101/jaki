# Running an agent harness on jaki

**Status: current (2026-10-05).** One harness, Pi 1.0.2, runs on jaki on the reference machine,
with the settings below. No other harness has been tried. The limits, overrides and errors any
client meets are in [`clients.md`](clients.md); read that first.

## What any harness needs

| setting | value |
|---|---|
| provider type | OpenAI-compatible chat completions |
| base URL | `http://<your address>:9090/v1`, the address jaki was installed with; on the machine itself too, because `127.0.0.1` answers nothing |
| API key | any non-empty string |
| model | `qnext` |
| context window | 65,536 tokens |
| reasoning | yes, returned as `reasoning_content`; send `reasoning_effort` `low` or nothing, since `low` is the evaluated setting and the server's default |
| output tokens per turn | at least 4,000, or a tool call can be lost inside the reasoning |
| agents at once | 2; a third waits for one of them to finish |

The small model, `qwen3.5-4b`, can take mechanical sub-tasks, with a 16,384-token window. It
does not reason and its tool use has never been measured, so do not run an agent on it.

A harness that speaks only Anthropic's Messages API, such as Claude Code, cannot use jaki, which
serves no `/v1/messages`.

## Pi

`./jaki client-config pi <address>` prints both files below from `engine/models.toml`, so the
model ids and context windows always match what is served. The provider, in
`~/.pi/agent/models.json`:

```json
{
  "providers": {
    "jaki": {
      "baseUrl": "http://<your address>:9090/v1",
      "api": "openai-completions",
      "apiKey": "none",
      "models": [
        { "id": "qnext", "contextWindow": 65536, "reasoning": true },
        { "id": "qwen3.5-4b", "contextWindow": 16384 }
      ]
    }
  }
}
```

The defaults, thinking level and compaction, in `~/.pi/agent/settings.json`:

```json
{
  "defaultProvider": "jaki",
  "defaultModel": "qnext",
  "defaultThinkingLevel": "low",
  "compaction": { "reserveTokens": 3072, "keepRecentTokens": 6000 }
}
```

**Set the thinking level.** For a model marked `reasoning: true`, Pi sends `reasoning_effort` on
every request, `medium` unless told otherwise, and that overrides the server's `low`.
`defaultThinkingLevel` or `--thinking low` makes Pi send `low`. Pi asks for 16,384 output tokens
per turn, which is enough for any tool call.

A run without `--model` uses `defaultProvider` and `defaultModel`; so does Pi started by an
editor over ACP. Pi compacts the conversation when it reaches the context window minus
`reserveTokens`. Pi's default reserve is 16,384, the small model's whole window, so Pi would
compact after every turn. With 3,072, compaction starts near 62k tokens on `qnext`.

jaki reads earlier turns from cache whether or not the harness sends their reasoning back.

A run without a terminal:

```bash
pi --model jaki/qnext -p "<your question>" < /dev/null
```

Without `< /dev/null`, Pi waits on the open standard input and the run never finishes.

**Sub-agents.** With the `pi-subagents` package, set `globalConcurrencyLimit` to 2 in
`~/.pi/agent/extensions/subagent/config.json`, so no more agents run than jaki answers at once.
After the first child, children that share a system prompt read it from cache, so give parallel
children the same system prompt and put what differs in the task.

## Concurrency and delegation

- **Run at most two agents at once.** Two answer at about 26 tokens a second each, and the
  engine reads one prompt at a time. In a measured run, four children sharing a system prompt
  finished 12 % sooner in parallel than one after another.
- **Send mechanical work to the small model by name.** Nothing picks a model for you. A sub-task
  that needs no reasoning names `qwen3.5-4b`, which reads and writes about one and a half times
  as fast as `qnext`.
