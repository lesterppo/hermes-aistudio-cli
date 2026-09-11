# AGENTS.md — instructions for AI coding agents

`ais` is an agent-native CLI for **Google AI Studio**. Zero cost: no paid API, no API-key
purchase. If you found this repo, this is how to drive it.

## Setup check (do this first)

```bash
ais auth
# {"ok":true,"cookies":52,"sapisid":true,"web_ok":true,"web_models":40,"key":"AIza...xxxx","api_ok":true}
```

- `web_ok:false` — the browser session behind the cookie scan expired. Ask the user to sign
  in to <https://aistudio.google.com>, then re-run. Never ask for credentials.
- `api_ok:false` — run `ais auth --capture` to provision the free-tier API key.

If `ais` is not on PATH: `./install.sh` (symlinks into `~/.local/bin`, creates state dirs).

## The contract

Every command prints **one compact JSON line** on stdout and writes payloads to disk:

```json
{"ok":true,"f":"~/.aistudio-cli/out/<ts>.md","s":1234,"m":"gemini-3.5-flash","c":1234,"t_ms":1316,"u":{"in":10,"out":3,"total":77}}
{"ok":false,"err":"RATE_LIMIT","msg":"...","retry":true}
```

Always: check `ok`, then read the file at `f`. `--json` prints the payload inline instead.

Error categories: `AUTH_EXPIRED`, `RATE_LIMIT`, `NOT_FOUND`, `BAD_REQUEST`, `SERVER`,
`NETWORK`, `NO_KEY`, `BAD_FILE`, `NO_PROMPT`, `NO_AUDIO`, `TIMEOUT`, `BAD_PAYLOAD`.
`retry:true` means transient, so back off and try again. Errors are always JSON, never a
traceback — if you see a traceback, that is a bug worth reporting.

## Preferred invocations (token-efficient)

```bash
ais chat -p "<prompt>"                 # read the file at f, NOT stdout
ais chat -p "..." --stream --raw       # live tokens on stderr, pointer on stdout
ais chat -p "..." --code               # only fenced code blocks, no prose
ais chat -p "..." --json-mode          # guaranteed JSON body
ais chat -c <session> -p "..."         # multi-turn; reuse the same session name
ais chat -f <file> -p "summarize"      # file grounding (PDFs, docs, text)
ais chat -i <image> -p "describe"      # vision
ais models --json                      # discover models
ais quota --json                       # per-tier entitlements before assuming a model works
```

## Choosing a model

The default and the fallback chain come from a measured free-tier probe
(`tests/reltest.py`). Do not override them on a hunch:

| model | success | avg latency | notes |
|---|---|---|---|
| `gemini-flash-lite-latest` | 4/4 | 1.62 s | fastest, first fallback |
| `gemini-3.5-flash` | 4/4 | 1.96 s | **default** |
| `gemini-3.1-flash-lite` | 4/4 | 3.25 s | |
| `gemini-3.8-flash` | 3/4 | 24.14 s | too slow for a default |
| `gemini-3.6-flash` | 1/4 | 2.88 s | connection drops |
| `gemini-2.5-flash` | 0/4 | — | quota exhausted; last resort |

**Do not retry a failing model in a loop.** The CLI already retries with backoff and then
walks the fallback chain, reporting `"fb":true` in the pointer when a fallback answered.
Re-run `tests/reltest.py` before changing `DEFAULT_MODEL` or `FALLBACK_CHAIN`.

## What works, and what does not (free tier)

Works: chat, streaming, multi-turn, system instructions, JSON mode, response schema,
thinking budgets/levels, Google Search grounding, file upload, image understanding, TTS
(24 kHz PCM, WAV-wrapped), embeddings, token counting, cached content, and the whole web
RPC surface (models, quota, promos, projects, preferences).

Deep research is a **separate API** and works via `ais research --wait "<question>"`:
`POST /v1beta/interactions` with `agent` (not `model`) plus `background:true`, polled by id.
Reports arrive in a few minutes with grounded citation links. Treat those links as
provenance, not as citable references — verify before using the output clinically,
academically, or anywhere a citation has to hold up.

Not available on a free-tier account: image *generation* (`limit: 0`) and Veo video
generation. The CLI returns `RATE_LIMIT` with the quota text. That is an account
entitlement limit, not a bug. **Do not switch to a paid key without the user's explicit
say-so.**

## Conventions in this repo

- The shipped CLI is pure standard library HTTP. `playwright` is optional and only used by
  `ais auth --capture` and the internal web-key discovery fallback.
- `tests/live_test.py` exercises the real backends; there are no mocks. It needs a signed-in
  browser session, so it is a local/manual suite, not a CI gate.
- Fixtures are written under `~/.aistudio-cli/ais-test-*`, never bare `/tmp` (files there
  have been observed vanishing mid-session, which fails checks spuriously).
- Runtime state stays in `~/.aistudio-cli/` and is gitignored. Never commit it.

## Escape hatch

Any of roughly a hundred AI Studio internal RPC methods is reachable:

```bash
ais methods                                  # list the known method names
ais call GetLoggingContext --json            # bodies are positional JSPB arrays
ais call ListQuotaModels --json
```

Bodies are **positional JSON arrays**; a top-level `{}` is rejected by the server. For the
`GenerateContent` proto the field numbers are
`1 model, 2 contents, 3 safetySettings, 4 generationConfig, 6 systemInstruction, 7 tools`.

## Do not

- Do not print, log, or commit the API key. `ais key --show` is for local use only; `ais key`
  prints a masked suffix.
- Do not commit `~/.aistudio-cli/` contents (cookies, keys, sessions).
- Do not assume cookie auth can generate content. It is gated to `403` on free accounts;
  generation requires the free-tier API key.
