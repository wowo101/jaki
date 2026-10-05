#!/usr/bin/env python3
"""Probe what a client may set per request on jaki's two text models.

    env JAKI=http://<address>:9090 python3 -u engine/infra/model-serving/client-contract-probe.py

Each probe pairs a treatment with a control, so an ignored setting reads as
"no difference" instead of passing:

  max_tokens        a cap of 24 against no cap
  temperature       three runs at 0 against three at the server's default
  reasoning_effort  low, medium and xhigh, top-level and in chat_template_kwargs,
                    by the prompt's token count; and enable_thinking false
  response_format   a JSON schema against the same prompt without one; json_object is
                    checked for valid JSON only, with no control

Runs on the standing tier through the router; needs no lease. Prints what it saw.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("JAKI", "").rstrip("/")
if not BASE:
    sys.exit("set JAKI=http://<address>:9090")


def chat(model, messages, timeout=600, **extra):
    body = {"model": model, "messages": messages, **extra}
    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    t = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.load(r)
    except urllib.error.HTTPError as e:
        return {"_status": e.code, "_body": e.read().decode(errors="replace")[:400]}
    except (urllib.error.URLError, OSError) as e:
        return {"_status": None, "_body": str(e)}
    out["_s"] = round(time.monotonic() - t, 1)
    return out


def msg(out):
    if "_status" in out:
        return {"error": out["_status"], "body": out["_body"]}
    c = out["choices"][0]
    m = c["message"]
    return {
        "content": m.get("content") or "",
        "reasoning": m.get("reasoning_content") or "",
        "finish": c.get("finish_reason"),
        "usage": out.get("usage", {}),
        "s": out["_s"],
    }


def user(text):
    # A unique marker keeps one probe's answer from being a cache artefact of another's.
    return [{"role": "user", "content": f"[{time.time_ns()}] {text}"}]


def probe_max_tokens(model):
    capped = msg(chat(model, user("Describe a harbour at dawn in about 200 words."), max_tokens=24))
    free = msg(chat(model, user("Describe a harbour at dawn in about 200 words.")))
    ok = (
        capped.get("finish") == "length"
        and capped.get("usage", {}).get("completion_tokens", 1e9) <= 24
        and free.get("usage", {}).get("completion_tokens", 0) > 24
    )
    print(f"  max_tokens       {'HONOURED' if ok else 'NOT SHOWN'}  "
          f"capped: finish={capped.get('finish')} tokens={capped.get('usage', {}).get('completion_tokens')}  "
          f"uncapped: finish={free.get('finish')} tokens={free.get('usage', {}).get('completion_tokens')}")


def probe_temperature(model):
    prompt = "Invent a name for a lighthouse. Reply with the name only."
    # The same prompt each time, unmarked, so any difference comes from sampling.
    fixed = [{"role": "user", "content": prompt}]
    zero = [msg(chat(model, fixed, temperature=0, max_tokens=2000)).get("content", "").strip() for _ in range(3)]
    dflt = [msg(chat(model, fixed, max_tokens=2000)).get("content", "").strip() for _ in range(3)]
    ok = all(zero) and all(dflt) and len(set(zero)) == 1 and len(set(dflt)) > 1
    print(f"  temperature      {'HONOURED' if ok else 'NOT SHOWN'}  at 0: {zero}  default: {dflt}")


def probe_effort(model):
    # The effort is a sentence the chat template adds to the prompt (none at medium), so the
    # prompt's token count says which effort the server applied; reasoning length is too noisy.
    def pt(**extra):
        out = chat(model, [{"role": "user", "content": "Say OK."}], max_tokens=200, **extra)
        return out.get("usage", {}).get("prompt_tokens")
    rows = {"default": pt()}
    for effort in ("low", "medium", "xhigh"):
        rows[effort] = pt(reasoning_effort=effort)
        rows[f"kwargs {effort}"] = pt(chat_template_kwargs={"reasoning_effort": effort})
    off = msg(chat(model, user("Say OK."), max_tokens=200, chat_template_kwargs={"enable_thinking": False}))
    on = msg(chat(model, user("Say OK."), max_tokens=200))
    ok = None not in rows.values() and len({rows["low"], rows["medium"], rows["xhigh"]}) == 3 and all(
        rows[e] == rows[f"kwargs {e}"] for e in ("low", "medium", "xhigh"))
    print(f"  reasoning_effort {'HONOURED' if ok else 'NOT SHOWN'}  prompt tokens {rows}")
    off_ok = off.get("reasoning") == "" and off.get("content") and on.get("reasoning")
    print(f"  thinking off     {'HONOURED' if off_ok else 'NOT SHOWN'}  "
          f"reasoning chars off={len(off.get('reasoning', ''))} default={len(on.get('reasoning', ''))}")


SCHEMA = {
    "type": "object",
    "properties": {
        "colour": {"type": "string", "enum": ["red", "green", "blue"]},
        "count": {"type": "integer"},
    },
    "required": ["colour", "count"],
    "additionalProperties": False,
}


def conforms(text):
    try:
        v = json.loads(text)
    except ValueError:
        return False
    return (isinstance(v, dict) and set(v) == {"colour", "count"}
            and v["colour"] in ("red", "green", "blue") and isinstance(v["count"], int))


def probe_schema(model):
    q = "Tell me about the sea in two sentences."
    plain = msg(chat(model, user(q), max_tokens=4000))
    rf = {"type": "json_schema", "json_schema": {"name": "probe", "strict": True, "schema": SCHEMA}}
    shaped = msg(chat(model, user(q), max_tokens=4000, response_format=rf))
    jo = msg(chat(model, user(q + " Reply in JSON."), max_tokens=4000, response_format={"type": "json_object"}))
    def j(t):
        try:
            json.loads(t)
            return True
        except ValueError:
            return False
    print(f"  json_schema      {'HONOURED' if conforms(shaped.get('content', '')) and not conforms(plain.get('content', '')) else 'NOT SHOWN'}  "
          f"with: {shaped.get('content', shaped)!r:.160}  without: {plain.get('content', plain)!r:.80}")
    print(f"  json_object      {'VALID JSON' if j(jo.get('content', '')) else 'NOT JSON'}  {jo.get('content', jo)!r:.160}")


if __name__ == "__main__":
    which = sys.argv[1:] or ["qnext", "qwen3.5-4b"]
    for model in which:
        print(model)
        probe_max_tokens(model)
        probe_temperature(model)
        if model == "qnext":
            probe_effort(model)
        probe_schema(model)
