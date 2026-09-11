# ais — Google AI Studio CLI (agent-native, zero cost)

Token-efficient CLI that operates **Google AI Studio** from the terminal with **no paid API**.
Two backends, both free:

| Backend | Auth | Cost | Covers |
|---|---|---|---|
| `web` | Your existing AI Studio browser session (`SAPISIDHASH`) | free | account/workspace RPC — models, quota, projects, API keys, promos, preferences + ~100 RPC methods via `ais call` |
| `api` | The account's own **free-tier** AI Studio API key, auto-provisioned from the signed-in account | free tier, **no billing** | generation — chat, streaming, multi-turn, embeddings, files, images, TTS, video, tokens, cached content |

Both credentials are derived from a browser session you already have. The CLI never asks
for a password and never needs a paid key.

## Install

```bash
git clone https://github.com/lesterppo/hermes-aistudio-cli ~/aistudio-cli
cd ~/aistudio-cli && ./install.sh
ais auth            # verifies both backends
```

Requirements: Python 3.9+ (stdlib only) and a browser signed in to
<https://aistudio.google.com>. `playwright` is optional — only needed for
`ais auth --capture` (first-time API-key provisioning).

## Output contract

stdout is one compact JSON line; payloads land on disk:

```json
{"ok":true,"f":"~/.aistudio-cli/out/20260912-012916-9f6084.md","s":9,"m":"gemini-2.5-flash","c":9,"t_ms":1172,"u":{"in":10,"out":3,"total":43}}
{"ok":false,"err":"RATE_LIMIT","msg":"...","retry":true}
```

`--json` prints the payload inline instead. Errors are always JSON — never a traceback.

## Commands

### Auth / account
```bash
ais auth                      # verify web + api backends
ais auth --capture            # (re)provision the free-tier API key from the browser
ais account                   # email context, plan, project, model counts
ais key --show                # print the provisioned free-tier key
```

### Web RPC (cookie auth — no API key at all)
```bash
ais models --filter 3.5       # full AI Studio model catalog (40 models, descriptions, limits)
ais quota                     # per-tier entitlements for every model group
ais promos                    # what's new in AI Studio
ais projects                  # cloud + imported projects
ais call GetLoggingContext --json       # raw $rpc escape hatch (any of ~100 methods)
ais methods                   # list the known RPC method names
```

### Generation (free-tier API key)
```bash
ais chat -p "explain vector search"
ais chat -p "..." --stream                    # SSE streaming to stderr
ais chat -c mysession -p "..."                # multi-turn (persists per session name)
ais chat -s "You are terse." -p "..."         # system instruction
ais chat -f report.pdf -p "summarise"         # file attachment (uploaded)
ais chat -i diagram.png -p "describe"         # image attachment (vision)
ais chat -p "..." --search                    # Google Search grounding
ais chat -p "..." --json-mode --schema s.json # structured output
ais chat -p "..." --think high                # thinking budget/level
ais chat -p "..." --code                      # emit only fenced code blocks
ais chat -p "..." -m gemini-3.5-flash         # pick a model
ais research --wait "state of the evidence on X"   # deep research agent (Interactions API)
ais research --status <id>                    # poll a running research job
ais image "a red cube"                        # image generation
ais tts "hello"                               # text to speech -> wav
ais video "a cat walking" --wait              # Veo long-running
ais embed "text"                              # embeddings (single or batch)
ais tokens -f article.txt                     # token counting
ais files list|upload|get|delete
ais cache list|create|get|delete
```

## What was reverse-engineered

AI Studio's web client talks to an internal gRPC-JSON service:

```
POST https://alkalimakersuite-pa.clients6.google.com/$rpc/
     google.internal.alkali.applications.makersuite.v1.MakerSuiteService/<Method>
Authorization: SAPISIDHASH <ts>_<sha1(ts + " " + SAPISID + " " + origin)>
Content-Type: application/json+protobuf
X-Goog-Api-Key: <public browser key from the app HTML>
Origin / Referer: https://aistudio.google.com     <-- MANDATORY (401 without them)
```

Key findings:

- **`Origin` + `Referer` are mandatory.** Without them the RPC host answers
  `401 invalid authentication credentials`, which looks like an auth problem but is not.
- **`SAPISIDHASH` is computed locally** from the `SAPISID` cookie — no browser needed at runtime.
- **The web app's API key rotates** and is a public constant embedded in the page HTML.
  The CLI scrapes it and caches it, refreshing automatically on `API_KEY_INVALID`.
- **Bodies are positional JSPB arrays, not JSON objects.** Top-level braces are rejected
  (`JSPB Fava message don't accept top-level braces`). Field numbers were recovered from
  the web bundle: `1 model · 2 contents · 3 safetySettings · 4 generationConfig ·
  6 systemInstruction · 7 tools · 15 toolConfig`; `Content{1 parts, 2 role}`; `Part{2 text}`.
- **Cookie-only generation is gated on free accounts** — the RPC returns
  `403 The caller does not have permission`, matching the app's own banner
  ("only available via a Google AI Plan or an API key"). The CLI therefore uses the
  account's **free-tier API key** (no billing configured) for generation, and cookies
  for everything else.
- **The Interactions API is a separate surface** from `generateContent`:
  `POST /v1beta/interactions` with an `agent` field (not `model`) and `"background": true`.
  Supplying a research model in `model` returns
  `... refers to an agent, but was provided in the 'model' field`; omitting `background`
  returns `background=true is required for agent interactions`. This is what powers
  `ais research` (deep research agents, polled by interaction id).
- **Free-tier generation works** for text models (`gemini-2.5-flash`, `gemini-3.5-flash`,
  `gemini-3.1-flash-lite`, `gemini-flash-lite-latest`, `gemma-4-*`) plus TTS, embeddings,
  files and token counting. Image (`limit: 0`) and Veo are not available on this free tier;
  the CLI reports that honestly instead of failing silently.

## Free-tier model reliability (measured)

`tests/reltest.py` — 4 calls per model, identical prompt, one session:

| model | success | avg latency | failure mode |
|---|---|---|---|
| `gemini-flash-lite-latest` | 4/4 | 1.62 s | – |
| `gemini-3.5-flash` **(default)** | 4/4 | 1.96 s | – |
| `gemini-3.1-flash-lite` | 4/4 | 3.25 s | – |
| `gemini-3.8-flash` | 3/4 | 24.14 s | 1x `NETWORK` — too slow for a default |
| `gemini-3.6-flash` | 1/4 | 2.88 s | 3x `NETWORK` — connection drops |
| `gemini-2.5-flash` | 0/4 | – | 4x `RATE_LIMIT` — daily quota exhausted |

Fallback chain: `gemini-flash-lite-latest → gemini-3.1-flash-lite → gemini-2.5-flash`.
Transient `503 high demand` and dropped connections are routine on the free tier, so the
CLI retries with backoff and then walks that chain, reporting `"fb":true` in the pointer
when a fallback answered. Never loop-retry a failing model yourself — re-run
`tests/reltest.py` before changing the default, behaviour shifts.

## Coverage vs. the Gemini web CLI

`ais` covers the Gemini web CLI's feature set on the AI Studio surface, plus extras:

| Gemini web CLI | `ais` | status |
|---|---|---|
| single-turn chat | `ais chat -p` | works |
| multi-turn (`-c`) | `ais chat -c <name>` | works |
| streaming (`--stream`) | `ais chat --stream` | works |
| model list | `ais models` / `ais models --backend api` | works |
| file upload (`-f`) | `ais chat -f` / `ais files upload` | works |
| image upload (`-i`) | `ais chat -i` | works |
| deep research (`--deep-research`) | `ais research --wait` | works (Interactions API) |
| code extraction (`--extract-code`) | `ais chat --code` | works |
| save images (`--save-images`) | `ais chat --save-images` | works |
| account status | `ais account` | works |
| `--brief` / `--raw` / `-t` | same flags | works |
| image generation (`--img`) | `ais image` | free tier blocks it (`limit: 0`) |
| Gem CRUD | AI Studio prompts/agents via `ais call` | free tier needs OAuth bearer |
| server-side chat history | local sessions only | AI Studio does not expose it over RPC |

Beyond the web CLI: `ais quota`, `ais promos`, `ais projects`, `ais tokens`, `ais embed`,
`ais tts`, `ais cache`, `ais files`, `ais video`, and `ais call <Method>` (~100 RPC methods).

## Files

```
ais.py              single-file CLI (stdlib only)
install.sh          symlink into ~/.local/bin + dirs
tests/live_test.py  live regression suite (real backends, no mocks)
tests/reltest.py    free-tier model reliability/latency probe
```

Runtime state lives in `~/.aistudio-cli/` (`cookies.json`, `apikey`, `webkey`,
`out/`, `sessions/`), all `0600`/user-private.

## License

MIT
