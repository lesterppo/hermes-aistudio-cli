#!/usr/bin/env python3
"""ais - AI-agent-native, token-efficient CLI for Google AI Studio.

Two zero-cost backends, no paid API:

  web  Cookie session (SAPISIDHASH) -> AI Studio internal $rpc (MakerSuiteService).
       Covers the account/workspace surface: models, quota, projects, API keys,
       promos, preferences and ~248 RPC methods via `ais call`.

  api  The account's own FREE-TIER AI Studio API key (auto-provisioned from the
       signed-in account, no billing) -> generativelanguage.googleapis.com.
       Covers generation: chat, streaming, multi-turn, embeddings, files,
       images, TTS, video, token counting, cached content.

Contract (agent-native):
  stdout is a single compact JSON pointer; payloads go to disk.
      {"ok":true,"f":"/path/out.md","s":1234,"m":"gemini-2.5-flash","t_ms":820}
      {"ok":false,"err":"AUTH_EXPIRED","msg":"...","retry":true}
  --json prints the payload inline instead of writing it to disk.
"""

import argparse
import base64
import gzip
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

HOME = Path(os.environ.get("AIS_HOME", Path.home() / ".aistudio-cli"))
OUTDIR = HOME / "out"
COOKIE_FILE = HOME / "cookies.json"
KEY_FILE = HOME / "apikey"
SESSION_DIR = HOME / "sessions"

ORIGIN = "https://aistudio.google.com"
RPC_HOST = "https://alkalimakersuite-pa.clients6.google.com/$rpc/"
SVC = "google.internal.alkali.applications.makersuite.v1.MakerSuiteService"
GEN_HOST = "https://generativelanguage.googleapis.com/v1beta"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")

# The AI Studio web client uses a public browser key embedded in its page HTML.
# It is not a secret and it rotates, so it is DISCOVERED at runtime rather than
# hardcoded: see _discover_web_key(). This is only the last-resort fallback.
WEB_APP_KEY = os.environ.get("AIS_WEB_KEY", "")
WEB_KEY_FILE = HOME / "webkey"

# Defaults chosen from the measured free-tier reliability probe (tests/reltest.py):
# gemini-3.5-flash 4/4 @2.0s, flash-lite-latest 4/4 @1.6s, 3.6-flash 1/4 (flaky).
DEFAULT_MODEL = os.environ.get("AIS_MODEL", "gemini-3.5-flash")

RPC_METHODS = """AcceptTerms AcceptTermsOfService CancelInteraction CheckImage CheckUserStatus
ClaimGdpCredits CountTokens CreateAgent CreateCloudProject CreateDataset CreateEnvironment
CreateFeelingLuckyImage CreateInteraction CreateInteractionStream CreatePrompt CreateTrigger
CreateVoice DeleteAgent DeleteCloudApiKey DeleteDataset DeleteEnvironment DeletePrompt
DeleteTrigger EnhancePrompt EvaluateTargeting ExportDataset FetchApiKeyNames
FetchMetricTimeSeries GenerateAccessToken GenerateCloudApiKey GenerateCodeAssistantSuggestionChips
GenerateContent GenerateDesignTheme GenerateDesignVariations GenerateImage GenerateTitle
GenerateVideo GetAgent GetAiStudioBenefitTier GetAppletGalleryConfig GetCloudProjectQuotaTier
GetCredential GetDataset GetDefaultCloudProject GetDefaultDesignThemes GetGdpProfileAndBenefits
GetGenerateVideoOperation GetInteractionStream GetLoggingContext GetModel GetModelQuota
GetPrepayEligibility GetProjectUsageLimit GetPrompt GetUserPreferences GetUserRestrictions
GetWorkspaceAppConfig ImprovePrompt ListAgents ListBillingAccounts ListCloudApiKeys
ListCloudProjects ListDatasets ListFunctionNames ListImportedProjects ListIncidentsHistory
ListLinkedProjects ListModelRateLimits ListModels ListPromos ListPrompts ListQuotaModels
ListSessionTurns ListTriggers ListUserCustomDomains ListVoices ProxyStreamedCall ProxyUnaryCall
ProxyUnaryFileApiCall QueryCodeSearch ResolveDriveResource StartComparisonGeneration
StreamExtractVideoFrames ToggleExperimentsOptIn UpdateAgent UpdateCloudApiKey UpdateCloudProject
UpdateDataset UpdatePrompt UpdateUserPreferences UploadScs""".split()

# Model field numbers in the MakerSuiteService GenerateContentRequest proto,
# recovered from the AI Studio web bundle:
#   1 model  2 contents  3 safetySettings  4 generationConfig
#   6 systemInstruction  7 tools  12 flag  15 toolConfig
MRS_PART_TEXT = "2"
MRS_CONTENT_PARTS = "1"
MRS_CONTENT_ROLE = "2"


# --------------------------------------------------------------------------
# errors / output
# --------------------------------------------------------------------------

class AisError(Exception):
    def __init__(self, err, msg="", retry=False, **extra):
        super().__init__(msg or err)
        self.err, self.msg, self.retry, self.extra = err, msg, retry, extra


def emit(obj, pretty=False):
    sys.stdout.write(json.dumps(obj, indent=2 if pretty else None,
                                ensure_ascii=False, separators=None if pretty else (",", ":")) + "\n")
    sys.stdout.flush()


def fail(err, msg="", retry=False, **extra):
    emit(dict({"ok": False, "err": err, "msg": msg[:600], "retry": retry}, **extra))
    sys.exit(1)


def ok(file=None, size=0, **extra):
    d = {"ok": True}
    if file:
        d["f"] = str(file)
        d["s"] = size
    d.update(extra)
    emit(d)
    return d


def slurp(path):
    p = Path(path)
    if not p.exists():
        fail("BAD_FILE", f"not found: {path}")
    return p.read_text(encoding="utf-8", errors="replace")


def save(text, name=None, ext=".md"):
    OUTDIR.mkdir(parents=True, exist_ok=True)
    if name:
        p = OUTDIR / name
    else:
        p = OUTDIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}{ext}"
    p.write_text(text, encoding="utf-8")
    return p


# --------------------------------------------------------------------------
# cookie session
# --------------------------------------------------------------------------

def _ff_cookie_dbs():
    bases = []
    if os.environ.get("FF_COOKIES_DB"):
        bases.append(Path(os.environ["FF_COOKIES_DB"]))
    bases += [Path.home() / "snap/firefox/common/.mozilla/firefox",
              Path.home() / ".mozilla/firefox"]
    # Firefox profiles living on a mounted Windows drive (WSL / dual boot).
    # Discovered generically so no username is hardcoded.
    for drive in ("/mnt/c", "/mnt/windows"):
        if not os.path.isdir(drive):
            continue
        try:
            users = list((Path(drive) / "Users").iterdir())
        except OSError:
            continue
        for u in users:
            if u.name in ("Public", "Default", "Default User", "All Users"):
                continue
            bases.append(u / "AppData/Roaming/Mozilla/Firefox/Profiles")
    out = []
    for base in bases:
        if not base.exists():
            continue
        if base.name.endswith(".default") or base.name.endswith(".default-release"):
            out.append(base / "cookies.sqlite")
        else:
            for child in sorted(base.iterdir()):
                db = child / "cookies.sqlite"
                if db.exists():
                    out.append(db)
    return [d for d in out if d.exists()]


def harvest_cookies(domain="google.com"):
    """Read .google.com cookies from the local Firefox profiles (WAL-safe copy)."""
    for db in _ff_cookie_dbs():
        tmp = Path("/tmp") / f"ais_ck_{os.getpid()}_{uuid.uuid4().hex[:6]}.sqlite"
        try:
            shutil.copy2(db, tmp)
            for suffix in ("-wal", "-shm"):
                s = Path(str(db) + suffix)
                if s.exists():
                    shutil.copy2(s, str(tmp) + suffix)
            con = sqlite3.connect(str(tmp))
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            rows = con.execute(
                "SELECT name, value FROM moz_cookies WHERE host LIKE ?", (f"%{domain}%",)).fetchall()
            con.close()
            ck = {}
            for n, v in rows:
                if n.startswith("__Host"):
                    continue          # host-only cookies cannot be injected/replayed
                ck[n] = str(v).strip('"')
            if ck.get("SAPISID") or ck.get("__Secure-1PSID"):
                return ck
        except Exception:
            continue
        finally:
            for f in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
                try:
                    f.unlink()
                except OSError:
                    pass
    return {}


def _discover_web_key(ck):
    """Scrape the AI Studio app HTML for the public browser key(s) and return
    the one that the MakerSuite RPC host accepts. Browser-free."""
    h = web_headers(ck, key="")
    h["Accept"] = "text/html"
    st, html = http(ORIGIN + "/prompts/new_chat", headers=h, timeout=45)
    if st != 200:
        return None
    cands = []
    for m in re.finditer(r"AIza[0-9A-Za-z_\-]{30,45}", html or ""):
        if m.group(0) not in cands:
            cands.append(m.group(0))
    for k in cands:
        payload = json.dumps([]).encode()
        st, txt = http(f"{RPC_HOST}{SVC}/ListModels", payload, web_headers(ck, key=k), method="POST", timeout=45)
        if st == 200:
            try:
                HOME.mkdir(parents=True, exist_ok=True)
                WEB_KEY_FILE.write_text(k)
            except Exception:
                pass
            return k
    return None


def web_key(ck, force=False):
    if not force and WEB_KEY_FILE.exists():
        k = WEB_KEY_FILE.read_text().strip()
        if k:
            return k
    return _discover_web_key(ck) or WEB_APP_KEY


def load_cookies(force=False):
    if not force and COOKIE_FILE.exists():
        try:
            ck = json.loads(COOKIE_FILE.read_text())
            if ck.get("SAPISID") and (time.time() - COOKIE_FILE.stat().st_mtime) < 6 * 3600:
                return ck
        except Exception:
            pass
    ck = harvest_cookies()
    if ck:
        HOME.mkdir(parents=True, exist_ok=True)
        COOKIE_FILE.write_text(json.dumps(ck))
        os.chmod(COOKIE_FILE, 0o600)
    return ck


def sapisidhash(sapisid, origin=ORIGIN, ts=None):
    ts = ts or int(time.time())
    return f"SAPISIDHASH {ts}_{hashlib.sha1(f'{ts} {sapisid} {origin}'.encode()).hexdigest()}"


def auth_header(ck):
    parts = [sapisidhash(ck.get("SAPISID", ""))]
    for nm, tag in (("__Secure-1PAPISID", "SAPISID1PHASH"), ("__Secure-3PAPISID", "SAPISID3PHASH")):
        if ck.get(nm):
            parts.append(sapisidhash(ck[nm]).replace("SAPISIDHASH", tag))
    return " ".join(parts)


def web_headers(ck, key=WEB_APP_KEY):
    return {
        "Authorization": auth_header(ck),
        "Content-Type": "application/json+protobuf",
        "X-Goog-Api-Key": key,
        "X-Goog-AuthUser": "0",
        # Origin + Referer are MANDATORY: without them the RPC host answers 401.
        "Origin": ORIGIN,
        "Referer": ORIGIN + "/",
        "User-Agent": UA,
        "Cookie": "; ".join(f"{k}={v}" for k, v in ck.items()),
        "accept": "*/*",
    }


def http(url, data=None, headers=None, method=None, timeout=120, decode=True):
    h = dict(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        raw = r.read()
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
        return r.status, raw.decode("utf-8", "replace") if decode else raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
        return e.code, raw.decode("utf-8", "replace") if decode else raw
    except Exception as e:
        return -1, str(e)


class Web:
    """AI Studio internal $rpc client (cookie auth, no API key)."""

    def __init__(self, cookies=None):
        self.ck = cookies or load_cookies()
        if not self.ck.get("SAPISID"):
            raise AisError("NO_AUTH", "no SAPISID cookie; sign in to aistudio.google.com in Firefox", retry=True)
        self.key = web_key(self.ck)

    def call(self, method, body=None, timeout=90, _retry=True):
        payload = body if isinstance(body, str) else json.dumps(body if body is not None else [])
        st, txt = http(f"{RPC_HOST}{SVC}/{method}", payload.encode(),
                       web_headers(self.ck, self.key), method="POST", timeout=timeout)
        if _retry and st in (400, 401) and ("API key not valid" in txt or "API_KEY_INVALID" in txt):
            fresh = _discover_web_key(self.ck)
            if fresh:
                self.key = fresh
                return self.call(method, body, timeout, _retry=False)
        if _retry and st == 401 and "invalid authentication credentials" in txt.lower():
            ck = load_cookies(force=True)
            if ck.get("SAPISID"):
                self.ck = ck
                return self.call(method, body, timeout, _retry=False)
        return st, txt

    def j(self, method, body=None, timeout=90):
        st, txt = self.call(method, body, timeout)
        try:
            return st, json.loads(txt)
        except Exception:
            return st, txt

    def must(self, method, body=None, timeout=90):
        st, d = self.j(method, body, timeout)
        if st == 200:
            return d
        raise AisError(_rpc_err(st, d), _rpc_msg(d))

    def models(self):
        d = self.must("ListModels")
        out = []
        for m in d[0] if d and isinstance(d[0], list) else []:
            if isinstance(m, list) and m and isinstance(m[0], str):
                out.append({"id": m[0].replace("models/", ""),
                            "name": m[3] if len(m) > 3 else "",
                            "desc": (m[4] or "")[:160] if len(m) > 4 else "",
                            "in": m[5] if len(m) > 5 else None,
                            "out": m[6] if len(m) > 6 else None})
        return out


def _rpc_err(st, d):
    if st == 401:
        return "AUTH_EXPIRED"
    if st == 403:
        return "FORBIDDEN"
    if st == 429:
        return "RATE_LIMIT"
    if st == -1:
        return "NETWORK"
    if isinstance(d, str) and d.lstrip().startswith("<"):
        return "BAD_PAYLOAD"
    if isinstance(d, list) and len(d) > 1 and isinstance(d[1], int):
        return {3: "BAD_REQUEST", 5: "NOT_FOUND", 7: "FORBIDDEN", 8: "RATE_LIMIT",
                13: "INTERNAL", 16: "AUTH_EXPIRED"}.get(d[1], f"RPC_{d[1]}")
    return f"HTTP_{st}"


def _rpc_msg(d):
    try:
        if isinstance(d, list) and len(d) > 2 and isinstance(d[2], str):
            return d[2]
        if isinstance(d, list) and len(d) > 1 and isinstance(d[1], str):
            return d[1]
    except Exception:
        pass
    return ""


# --------------------------------------------------------------------------
# api key (free tier, auto-provisioned from the signed-in account)
# --------------------------------------------------------------------------

def load_key():
    for src in (os.environ.get("AIS_API_KEY"),
                os.environ.get("GEMINI_API_KEY"),
                KEY_FILE.read_text().strip() if KEY_FILE.exists() else None):
        if src:
            return src.strip()
    return None


def capture_key_via_browser(ck):
    """Grab the account's AI Studio API key from the API-keys page (CDP browser)."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        raise AisError("NO_PLAYWRIGHT", "playwright not available for --capture", retry=False)
    cdp = os.environ.get("AIS_CDP", "http://127.0.0.1:9223")
    inj = [{"name": k, "value": v, "domain": ".google.com", "path": "/",
            "secure": True, "httpOnly": False, "expires": -1} for k, v in ck.items()]
    found = {}
    with sync_playwright() as p:
        br = p.chromium.connect_over_cdp(cdp)
        ctx = br.contexts[0]
        try:
            ctx.add_cookies(inj)
        except Exception:
            pass
        pg = ctx.new_page()
        try:
            pg.on("response", lambda r: found.setdefault("key", r.text()) if "ListCloudApiKeys" in r.url else None)
            pg.goto("https://aistudio.google.com/api-keys", wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_timeout(14000)
            body = found.get("key", "")
            for e in json.loads(body)[0] if body else []:
                if isinstance(e, list) and len(e) > 2 and str(e[2]).startswith("AIza"):
                    return e[2], e[1]
        finally:
            try:
                pg.close()
            except Exception:
                pass
    raise AisError("KEY_NOT_FOUND", "could not read an API key from the AI Studio API-keys page")


def provision_key(ck, capture=False):
    k = load_key()
    if k and not capture:
        return k
    k, name = capture_key_via_browser(ck)
    HOME.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(k + "\n")
    os.chmod(KEY_FILE, 0o600)
    return k


# --------------------------------------------------------------------------
# generative REST client
# --------------------------------------------------------------------------

class Api:
    def __init__(self, key=None, timeout=180):
        self.key = key or load_key()
        if not self.key:
            raise AisError("NO_KEY", "no API key; run: ais auth --capture", retry=True)
        self.timeout = timeout

    def req(self, path, body=None, method="POST", timeout=None, stream_path=False):
        url = path if path.startswith("http") else f"{GEN_HOST}/{path}"
        data = json.dumps(body).encode() if body is not None else None
        h = {"x-goog-api-key": self.key, "Content-Type": "application/json"}
        return http(url, data, h, method=method, timeout=timeout or self.timeout)

    def gen(self, model, body, timeout=None):
        st, txt = self.req(f"models/{model}:generateContent", body, timeout=timeout)
        if st != 200:
            raise AisError(*_api_err(st, txt))
        return json.loads(txt)

    def stream(self, model, body, timeout=None):
        """Yield text deltas from streamGenerateContent?alt=sse."""
        url = f"{GEN_HOST}/models/{model}:streamGenerateContent?alt=sse"
        data = json.dumps(body).encode()
        h = {"x-goog-api-key": self.key, "Content-Type": "application/json", "accept": "text/event-stream"}
        req = urllib.request.Request(url, data=data, headers=h, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as e:
            raise AisError(*_api_err(e.code, e.read().decode("utf-8", "replace")))
        except Exception as e:
            raise AisError("NETWORK", str(e)[:200], retry=True)
        buf = b""
        for line in resp:                    # buffered readline - do NOT read(1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload in (b"", b"[DONE]"):
                continue
            try:
                yield json.loads(payload)
            except Exception:
                continue


FALLBACK_CHAIN = ["gemini-flash-lite-latest", "gemini-3.1-flash-lite", "gemini-2.5-flash"]


def gen_retry(api, model, body, a, tries=2):
    """Generate with backoff on transient errors and a model-fallback chain.

    A thinking config the model family rejects is dropped and the same model is
    retried (e.g. --think off against a non-thinking model).

    Returns (resp, model_used, fell_back). Raises AisError when everything fails.
    """
    chain = [model]
    if getattr(a, "fallback", True):
        chain += [m for m in FALLBACK_CHAIN if m != model]
    last = None
    for mi, m in enumerate(chain):
        for attempt in range(tries if mi == 0 else 2):
            try:
                return api.gen(m, body, timeout=getattr(a, "timeout", 180)), m, mi > 0
            except AisError as e:
                last = e
                gc = body.get("generationConfig")
                if e.err == "BAD_REQUEST" and gc and "thinkingConfig" in gc:
                    gc.pop("thinkingConfig", None)      # model rejects our thinking setting
                    if not gc:
                        body.pop("generationConfig", None)
                    continue                            # same model, without it
                if not e.retry:
                    raise
                if attempt < tries - 1:
                    time.sleep(1.5)          # fail fast, then walk the fallback chain
    raise last


def _api_err(st, txt):
    code, msg = None, ""
    try:
        j = json.loads(txt)
        code = j.get("error", {}).get("code")
        msg = j.get("error", {}).get("message", "")
        if st is None:
            st = code
    except Exception:
        msg = str(txt)[:300]
    m = {"AUTH_EXPIRED": ("401", "403"), "RATE_LIMIT": ("429",), "NOT_FOUND": ("404",),
         "BAD_REQUEST": ("400",), "SERVER": ("500", "503")}
    st_s = str(st if st and st > 0 else code or "")
    transient = any(s in msg for s in ("Remote end closed", "Connection reset", "timed out",
                                       "Timeout", "temporarily unavailable", "EOF occurred"))
    if transient or st_s in ("", "-1"):
        return "NETWORK", msg or "network error", True
    if st_s in ("401", "403"):
        if "quota" in msg.lower():
            return "RATE_LIMIT", msg, True
        return "AUTH_EXPIRED", msg, False
    if st_s == "429":
        return "RATE_LIMIT", msg, True
    if st_s == "404":
        return "NOT_FOUND", msg, False
    if st_s in ("400",):
        return "BAD_REQUEST", msg, False
    if st_s in ("500", "503"):
        return "SERVER", msg, True
    return f"HTTP_{st_s or '?'}", msg, False


# --------------------------------------------------------------------------
# payload builders
# --------------------------------------------------------------------------

def part_text(t):
    return {"text": t}


def contents_from(history, prompt, files=None, images=None):
    con = list(history)
    parts = []
    for f in files or []:
        parts.append({"fileData": {"mimeType": _mime(f), "fileUri": upload_file(f)[0]}})
    for i in images or []:
        parts.append({"inlineData": {"mimeType": _mime(i), "data": base64.b64encode(Path(i).read_bytes()).decode()}})
    if prompt:
        parts.append(part_text(prompt))
    con.append({"role": "user", "parts": parts})
    return con


def _mime(p):
    return mimetypes.guess_type(str(p))[0] or "application/octet-stream"


def gen_config(a):
    cfg = {}
    if a.temp is not None:
        cfg["temperature"] = a.temp
    if a.topp is not None:
        cfg["topP"] = a.topp
    if a.topk is not None:
        cfg["topK"] = a.topk
    if a.max is not None:
        cfg["maxOutputTokens"] = a.max
    if getattr(a, "json_mode", False):
        cfg["responseMimeType"] = "application/json"
    if getattr(a, "aspect", None):
        cfg["imageConfig"] = {"aspectRatio": a.aspect}
    if getattr(a, "voice", None):
        cfg["speechConfig"] = {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": a.voice}}}
    th = getattr(a, "think", None)
    if th:
        lvl = {"off": 0, "none": 0, "low": 1024, "medium": 8192, "high": 24576}
        model = (getattr(a, "model", None) or DEFAULT_MODEL).lower()
        legacy = (model.startswith("gemini-2.5") or "gemma" in model
                  or "robotics" in model or "computer-use" in model or "antigravity" in model)
        if th.isdigit():
            cfg["thinkingConfig"] = {"thinkingBudget": int(th)}
        elif legacy and th.lower() in lvl:
            cfg["thinkingConfig"] = {"thinkingBudget": lvl[th.lower()]}
        elif th.lower() in ("off", "none") and not legacy:
            pass          # 3.x cannot disable thinking; leave the model default
        else:
            cfg["thinkingConfig"] = {"thinkingLevel": th.upper()}
    return cfg


def tool_config(a):
    tools = []
    if getattr(a, "search", False):
        tools.append({"googleSearch": {}})
    if getattr(a, "url_context", False):
        tools.append({"urlContext": {}})
    return tools


def body_from(a, history, prompt, model):
    b = {"contents": contents_from(history, prompt, getattr(a, "files", None), getattr(a, "images", None))}
    sysi = getattr(a, "system", None)
    if sysi:
        b["systemInstruction"] = {"parts": [part_text(sysi)]}
    cfg = gen_config(a)
    if cfg:
        b["generationConfig"] = cfg
    tls = tool_config(a)
    if tls:
        b["tools"] = tls
    sch = getattr(a, "schema", None)
    if sch:
        b.setdefault("generationConfig", {})["responseMimeType"] = "application/json"
        b["generationConfig"]["responseSchema"] = json.loads(slurp(sch))
    return b


def extract_text(resp):
    out, thoughts = [], []
    for cand in resp.get("candidates", []) or []:
        for p in cand.get("content", {}).get("parts", []) or []:
            if p.get("thought"):
                thoughts.append(p.get("text", ""))
            elif p.get("text"):
                out.append(p["text"])
    return "\n".join(out).strip(), "\n".join(thoughts).strip()


def extract_images(resp):
    imgs = []
    for cand in resp.get("candidates", []) or []:
        for p in cand.get("content", {}).get("parts", []) or []:
            d = p.get("inlineData") or p.get("inline_data")
            if d and d.get("data"):
                imgs.append((d.get("mimeType") or d.get("mime_type") or "image/png", d["data"]))
    return imgs


def usage_of(resp):
    u = resp.get("usageMetadata") or {}
    return {"in": u.get("promptTokenCount"), "out": u.get("candidatesTokenCount"),
            "think": u.get("thoughtsTokenCount"), "total": u.get("totalTokenCount")}


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------

def upload_file(path):
    """Multipart upload to the Files API. Returns (uri, name)."""
    p = Path(path)
    if not p.exists():
        raise AisError("BAD_FILE", f"not found: {path}")
    key = load_key()
    if not key:
        raise AisError("NO_KEY", "no API key; run: ais auth --capture", retry=True)
    boundary = "----ais" + uuid.uuid4().hex
    meta = json.dumps({"file": {"display_name": p.name}})
    body = b""
    body += f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n".encode()
    body += (f"--{boundary}\r\nContent-Type: {_mime(p)}\r\n\r\n").encode()
    body += p.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    st, txt = http("https://generativelanguage.googleapis.com/upload/v1beta/files", body,
                   {"x-goog-api-key": key, "Content-Type": f"multipart/related; boundary={boundary}",
                    "X-Goog-Upload-Protocol": "multipart"}, timeout=300)
    if st != 200:
        raise AisError(*_api_err(st, txt))
    d = json.loads(txt).get("file", {})
    return d.get("uri"), d.get("name")


# --------------------------------------------------------------------------
# session handling (multi-turn)
# --------------------------------------------------------------------------

def session_path(arg):
    if not arg:
        return None
    p = Path(arg)
    if p.suffix != ".json":
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        p = SESSION_DIR / f"{arg}.json"
    return p


def load_session(arg):
    p = session_path(arg)
    if p and p.exists():
        try:
            return json.loads(p.read_text()), p
        except Exception:
            pass
    return {"model": None, "history": []}, p


def save_session(p, data):
    if not p:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False))


# --------------------------------------------------------------------------
# commands: auth / account
# --------------------------------------------------------------------------

def cmd_auth(a):
    ck = load_cookies(force=True)
    if not ck.get("SAPISID"):
        fail("NO_AUTH", "no .google.com cookies found; sign in to https://aistudio.google.com in Firefox")
    w = Web(ck)
    info = {"cookies": len(ck), "sapisid": bool(ck.get("SAPISID"))}
    try:
        ms = w.models()
        info["web_ok"] = True
        info["web_models"] = len(ms)
    except AisError as e:
        info["web_ok"] = False
        info["web_err"] = e.err
    try:
        k = provision_key(ck, capture=a.capture)
        info["key"] = k[:8] + "..." + k[-4:]
        st, txt = Api(k).req("models", method="GET")
        info["api_ok"] = st == 200
        if st != 200:
            info["api_err"] = _api_err(st, txt)[0]
    except AisError as e:
        info["api_ok"] = False
        info["api_err"] = e.err
    info = {k: v for k, v in info.items() if v is not None}
    if info.get("web_ok") or info.get("api_ok"):
        return ok(**info)
    fail("AUTH_FAILED", json.dumps(info))


def cmd_account(a):
    ck = load_cookies()
    out = {}
    try:
        w = Web(ck)
        out["web_models"] = len(w.models())
        ctx = w.must("GetLoggingContext")
        if isinstance(ctx, list) and len(ctx) > 11:
            out["region"] = ctx[11]
        tier = w.must("GetAiStudioBenefitTier")
        out["tier_raw"] = tier
        projs = w.must("ListCloudProjects")
        pl = []
        for p in (projs[0] if projs and isinstance(projs[0], list) else []):
            if isinstance(p, list) and p:
                pl.append({"id": p[0], "name": p[1] if len(p) > 1 else "",
                           "title": p[2] if len(p) > 2 else ""})
        out["projects"] = pl
        out["plan"] = "free" if not tier else "paid"
    except AisError as e:
        out["web_err"] = f"{e.err}: {e.msg}"
    k = load_key()
    if k:
        out["key"] = k[:8] + "..." + k[-4:]
        st, txt = Api(k).req("models", method="GET")
        out["api_ok"] = st == 200
        if st == 200:
            out["api_models"] = len(json.loads(txt).get("models", []))
    else:
        out["api_ok"] = False
    ok(**out)


def cmd_key(a):
    ck = load_cookies()
    if a.capture:
        k = provision_key(ck, capture=True)
        return ok(key_updated=True, key_sha=hashlib.sha256(k.encode()).hexdigest()[:12],
                  suffix=k[-6:], src=str(KEY_FILE))
    k = load_key()
    if not k:
        fail("NO_KEY", "no API key provisioned; run: ais auth --capture", retry=True)
    if a.show:
        return ok(key=k, suffix=k[-6:], free_tier=True)
    ok(suffix=k[-6:], saved_at=str(KEY_FILE), free_tier=True,
       **({"note": "use --show to print the full key"} if not a.show else {}))


def cmd_models(a):
    use_api = a.backend == "api"
    if use_api:
        st, txt = Api().req("models?pageSize=200", method="GET")
        if st != 200:
            e = _api_err(st, txt)
            fail(e[0], e[1], e[2])
        ms = json.loads(txt).get("models", [])
        rows = [{"id": m["name"].replace("models/", ""),
                 "name": m.get("displayName", ""),
                 "in": m.get("inputTokenLimit"), "out": m.get("outputTokenLimit"),
                 "methods": m.get("supportedGenerationMethods", [])} for m in ms]
    else:
        rows = Web(load_cookies()).models()
    if a.filter:
        f = a.filter.lower()
        rows = [r for r in rows if f in r["id"].lower() or f in (r["name"] or "").lower()]
    if a.json:
        return ok(models=rows, n=len(rows), backend="api" if use_api else "web")
    txt = "\n".join(f"{r['id']:44s} {r['name']}" for r in rows)
    p = save(txt, ext=".txt")
    ok(file=p, s=len(txt), n=len(rows), backend="api" if use_api else "web")


def cmd_quota(a):
    d = Web(load_cookies()).must("ListQuotaModels")
    rows = []
    for e in (d[0] if d and isinstance(d[0], list) else []):
        if not isinstance(e, list) or not e:
            continue
        models = []
        for m in (e[4] or []) if len(e) > 4 and isinstance(e[4], list) else []:
            if isinstance(m, list) and m:
                models.append({"id": str(m[0]).replace("models/", ""), "caps": m[1], "flags": m[2]})
        rows.append({"group": e[0], "tier": e[2] if len(e) > 2 else None,
                     "default": e[5] if len(e) > 5 else None, "models": models})
    if a.json:
        return ok(groups=rows, n=len(rows))
    lines = []
    for g in rows:
        lines.append(f"# {g['group']}  (default: {g['default']})")
        for m in g["models"]:
            lines.append(f"    {m['id']:44s} caps={m['caps']} flags={m['flags']}")
    txt = "\n".join(lines)
    p = save(txt, ext=".txt")
    ok(file=p, s=len(txt), n=len(rows))


def cmd_promos(a):
    d = Web(load_cookies()).must("ListPromos")
    items = []
    for e in (d[0] if d and isinstance(d[0], list) else []):
        if isinstance(e, list) and e:
            items.append({"title": e[0], "desc": (e[1] or "")[:300]})
    ok(promos=items, n=len(items))


def cmd_projects(a):
    w = Web(load_cookies())
    out = {}
    try:
        d = w.must("ListCloudProjects")
        out["cloud"] = [{"id": p[0], "name": p[1], "title": p[2]}
                        for p in (d[0] or []) if isinstance(p, list) and p]
    except AisError as e:
        out["cloud_err"] = e.err
    try:
        d = w.must("ListImportedProjects")
        out["imported"] = [x for x in (d[4] or []) if isinstance(x, list)] if len(d) > 4 else []
    except AisError as e:
        out["imported_err"] = e.err
    ok(**out)


def cmd_call(a):
    """Raw $rpc escape hatch: ais call <Method> '<json-array-body>'."""
    body = a.body
    if body:
        try:
            body = json.loads(body)
        except Exception:
            pass
    w = Web(load_cookies())
    st, txt = w.call(a.method, body)
    try:
        d = json.loads(txt)
    except Exception:
        d = {"raw": txt[:2000]}
    if st == 200:
        if a.json:
            return ok(status=st, data=d)
        p = save(json.dumps(d, indent=1, ensure_ascii=False), ext=".json")
        return ok(file=p, s=p.stat().st_size, status=st)
    fail(_rpc_err(st, d), _rpc_msg(d) or json.dumps(d)[:300])


def cmd_methods(a):
    ok(methods=RPC_METHODS, n=len(RPC_METHODS))


# --------------------------------------------------------------------------
# commands: generation
# --------------------------------------------------------------------------

def _finish(a, text, model, resp=None, started=None, extra=None):
    t_ms = int((time.time() - started) * 1000) if started else None
    d = {"m": model, "c": len(text)}
    if t_ms:
        d["t_ms"] = t_ms
    if resp is not None:
        d["u"] = usage_of(resp)
        fr = (resp.get("candidates") or [{}])[0].get("finishReason")
        if fr and fr != "STOP":
            d["finish"] = fr
    if extra:
        d.update(extra)
    if a.json:
        return ok(text=text, **d)
    p = save(text, ext=".md")
    return ok(file=p, s=len(text.encode()), **d)


def cmd_chat(a):
    prompt = a.prompt
    if not prompt and not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()
    if a.prompt_file:
        prompt = slurp(a.prompt_file)
    if not prompt and not (a.files or a.images):
        fail("NO_PROMPT", "provide a prompt (positional, -p, -f, or stdin)")
    sess, spath = load_session(a.session)
    if a.new:
        sess = {"model": None, "history": []}
    model = a.model or sess.get("model") or DEFAULT_MODEL
    a.model = model
    if a.brief and prompt:
        prompt = "Be concise.\n\n" + prompt
    history = [] if a.no_context else sess.get("history", [])
    api = Api(timeout=a.timeout)
    started = time.time()

    if a.stream:
        last = None
        body = body_from(a, history, prompt, model)
        used, fb = model, False
        chain = [model] + ([m for m in FALLBACK_CHAIN if m != model] if getattr(a, "fallback", True) else [])
        acc = ""
        done = False
        for mi, m in enumerate(chain):
            for attempt in range(2):          # 2nd attempt = same model minus thinkingConfig
                acc = ""
                try:
                    for chunk in api.stream(m, body, timeout=a.timeout):
                        last = chunk
                        t, _ = extract_text(chunk)
                        if t:
                            add = t[len(acc):] if t.startswith(acc) else t
                            if not a.raw:
                                sys.stderr.write(add)
                                sys.stderr.flush()
                            acc += add
                    used, fb, done = m, mi > 0, True
                    break
                except AisError as e:
                    gc = body.get("generationConfig")
                    if e.err == "BAD_REQUEST" and gc and "thinkingConfig" in gc:
                        gc.pop("thinkingConfig", None)
                        if not gc:
                            body.pop("generationConfig", None)
                        continue                  # retry the same model without thinking
                    if not e.retry or mi == len(chain) - 1:
                        raise
                    if not a.raw:
                        sys.stderr.write(f"\n[ais] {m} failed ({e.err}); retrying with fallback\n")
                    break                         # move to the next model
            if done:
                break
        if not done:
            raise AisError("STREAM_FAILED", "no model in the fallback chain streamed successfully")
        if not a.raw:
            sys.stderr.write("\n")
        final = acc.strip()
        resp = last
    else:
        body = body_from(a, history, prompt, model)
        resp, model, fb = gen_retry(api, model, body, a)
        final, thoughts = extract_text(resp)

    if a.stream:
        model = used
    extra = {"fb": True} if fb else {}
    if a.show_thoughts and not a.stream:
        _, th = extract_text(resp or {})
        if th:
            extra["thoughts"] = th
    if a.code:
        final = extract_code(final)
    if not final:
        fail("EMPTY", f"no text returned (finish={((resp or {}).get('candidates') or [{}])[0].get('finishReason')})")

    if spath:
        sess["model"] = model
        h = sess.get("history", []) if not a.no_context else []
        h = h + [{"role": "user", "parts": [{"text": prompt}]},
                 {"role": "model", "parts": [{"text": final}]}]
        if a.turns and len(h) > a.turns * 2:
            h = h[-a.turns * 2:]
        sess["history"] = h
        save_session(spath, sess)
        extra["sess"] = str(spath)
        extra["turns"] = len(h) // 2

    imgs = extract_images(resp or {})
    if imgs and a.save_images:
        Path(a.save_images).mkdir(parents=True, exist_ok=True)
        paths = []
        for i, (mt, b64) in enumerate(imgs):
            ext = "." + (mt.split("/")[-1].replace("jpeg", "jpg"))
            fp = Path(a.save_images) / f"{time.strftime('%Y%m%d-%H%M%S')}-{i}{ext}"
            fp.write_bytes(base64.b64decode(b64))
            paths.append(str(fp))
        extra["imgs"] = paths

    return _finish(a, final, model, resp, started, extra)


def extract_code(text):
    blocks = re.findall(r"```[ \t]*[\w+.-]*\n(.*?)```", text, re.S)
    return "\n\n".join(b.strip() for b in blocks) if blocks else text


def cmd_image(a):
    model = a.model or "gemini-2.5-flash-image"
    api = Api(timeout=a.timeout)
    started = time.time()
    body = {"contents": [{"role": "user", "parts": [part_text(a.prompt)]}]}
    cfg = {}
    if a.aspect:
        cfg["imageConfig"] = {"aspectRatio": a.aspect}
    if cfg:
        body["generationConfig"] = cfg
    resp = api.gen(model, body, timeout=a.timeout)
    imgs = extract_images(resp)
    text, _ = extract_text(resp)
    if not imgs:
        fail("NO_IMAGE", text or "model returned no inline image data")
    outdir = Path(a.out) if a.out else (OUTDIR / "images")
    outdir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, (mt, b64) in enumerate(imgs):
        ext = "." + mt.split("/")[-1].replace("jpeg", "jpg")
        fp = outdir / f"{time.strftime('%Y%m%d-%H%M%S')}-{i}{ext}"
        fp.write_bytes(base64.b64decode(b64))
        paths.append(str(fp))
    return ok(imgs=paths, n=len(paths), m=model, t_ms=int((time.time() - started) * 1000),
              txt=text[:200] if text else None)


def cmd_tts(a):
    model = a.model or "gemini-2.5-flash-preview-tts"
    voice = a.voice or "Kore"
    api = Api(timeout=a.timeout)
    started = time.time()
    body = {"contents": [{"role": "user", "parts": [part_text(a.text)]}],
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}}}
    resp = None
    out = None
    for attempt in range(3):                 # TTS occasionally returns an empty candidate
        resp = api.gen(model, body, timeout=a.timeout)
        for cand in resp.get("candidates", []) or []:
            for p in cand.get("content", {}).get("parts", []) or []:
                d = p.get("inlineData") or p.get("inline_data")
                if d and d.get("data"):
                    out = d
                    break
            if out:
                break
        if out:
            break
        time.sleep(2 + 2 * attempt)
    if not out:
        fail("NO_AUDIO", json.dumps(resp)[:250])
    mime = out.get("mimeType") or "audio/L16;rate=24000"
    raw = base64.b64decode(out["data"])
    outdir = Path(a.out) if a.out else (OUTDIR / "audio")
    outdir.mkdir(parents=True, exist_ok=True)
    pcm = outdir / f"{time.strftime('%Y%m%d-%H%M%S')}.pcm"
    pcm.write_bytes(raw)
    wav = pcm.with_suffix(".wav")
    _pcm_to_wav(raw, wav, mime)
    return ok(wav=str(wav), pcm=str(pcm), bytes=len(raw), voice=voice, m=model,
              t_ms=int((time.time() - started) * 1000))


def _pcm_to_wav(pcm, dest, mime):
    import struct
    rate, ch, bits = 24000, 1, 16
    m = re.search(r"rate=(\d+)", mime or "")
    if m:
        rate = int(m.group(1))
    n = len(pcm)
    with open(dest, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + n) + b"WAVEfmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, ch, rate, rate * ch * bits // 8, ch * bits // 8, bits))
        f.write(b"data" + struct.pack("<I", n) + pcm)


def cmd_video(a):
    model = a.model or "veo-3.1-fast-generate-preview"
    api = Api(timeout=a.timeout)
    started = time.time()
    body = {"instances": [{"prompt": a.prompt}],
            "parameters": {"aspectRatio": a.aspect or "16:9"}}
    st, txt = api.req(f"models/{model}:predictLongRunning", body, timeout=a.timeout)
    if st != 200:
        e = _api_err(st, txt)
        fail(e[0], e[1], e[2])
    op = json.loads(txt).get("name")
    if not a.wait:
        return ok(op=op, m=model, status="submitted")
    deadline = time.time() + a.wait_timeout
    while time.time() < deadline:
        time.sleep(12)
        st, txt = api.req(f"{op}", method="GET", timeout=60)
        if st != 200:
            continue
        d = json.loads(txt)
        if d.get("done"):
            vids = ((d.get("response") or {}).get("generateVideoResponse") or {}).get("generatedSamples") or []
            if not vids:
                fail("NO_VIDEO", json.dumps(d)[:300])
            uri = (vids[0].get("video") or {}).get("uri")
            out = Path(a.out) if a.out else (OUTDIR / "video")
            out.mkdir(parents=True, exist_ok=True)
            fp = out / f"{time.strftime('%Y%m%d-%H%M%S')}.mp4"
            st2, raw = http(uri + ("&" if "?" in uri else "?") + f"key={api.key}", decode=False, timeout=300)
            if st2 == 200:
                fp.write_bytes(raw)
                return ok(file=str(fp), s=len(raw), m=model, uri=uri,
                          t_ms=int((time.time() - started) * 1000))
            return ok(uri=uri, m=model, note="download failed, use uri")
    fail("TIMEOUT", f"video op not done in {a.wait_timeout}s", op=op)


def cmd_embed(a):
    model = a.model or "gemini-embedding-001"
    api = Api(timeout=a.timeout)
    texts = list(a.text or [])
    if not texts and not sys.stdin.isatty():
        texts = [t for t in sys.stdin.read().split("\n") if t.strip()]
    if not texts:
        fail("NO_INPUT", "provide text(s)")
    started = time.time()
    if len(texts) == 1:
        body = {"content": {"parts": [part_text(texts[0])]}}
        if a.task:
            body["taskType"] = a.task
        st, txt = api.req(f"models/{model}:embedContent", body)
    else:
        body = {"requests": [{"model": f"models/{model}", "content": {"parts": [part_text(t)]},
                              **({"taskType": a.task} if a.task else {})} for t in texts]}
        st, txt = api.req(f"models/{model}:batchEmbedContents", body)
    if st != 200:
        e = _api_err(st, txt)
        fail(e[0], e[1], e[2])
    d = json.loads(txt)
    vecs = [e.get("values") for e in d.get("embeddings", [])] or \
           [d.get("embedding", {}).get("values")]
    vecs = [v for v in vecs if v]
    if a.json:
        return ok(embeddings=vecs, n=len(vecs), dim=len(vecs[0]) if vecs else 0, m=model)
    p = save(json.dumps(vecs), ext=".json")
    return ok(file=p, s=p.stat().st_size, n=len(vecs), dim=len(vecs[0]) if vecs else 0, m=model,
              t_ms=int((time.time() - started) * 1000))


def cmd_tokens(a):
    model = a.model or DEFAULT_MODEL
    txt = a.text or (slurp(a.file) if a.file else (None if sys.stdin.isatty() else sys.stdin.read()))
    if txt is None:
        fail("NO_INPUT", "provide text or -f FILE")
    body = {"contents": [{"role": "user", "parts": [part_text(txt)]}]}
    api = Api(timeout=a.timeout)
    st, t = api.req(f"models/{model}:countTokens", body)
    if st != 200:
        e = _api_err(st, t)
        fail(e[0], e[1], e[2])
    d = json.loads(t)
    ok(tokens=d.get("totalTokens"), m=model, chars=len(txt), **({"json": d} if a.json else {}))


def cmd_files(a):
    api = Api(timeout=a.timeout)
    if a.action == "list":
        st, t = api.req("files?pageSize=100", method="GET")
        if st != 200:
            e = _api_err(st, t)
            fail(e[0], e[1], e[2])
        fs = [{"name": f.get("name"), "display": f.get("displayName"), "mime": f.get("mimeType"),
               "size": f.get("sizeBytes"), "state": f.get("state"),
               "uri": f.get("uri"), "expires": (f.get("expirationTime") or "")[:10]}
              for f in json.loads(t).get("files", [])]
        return ok(files=fs, n=len(fs))
    if a.action == "upload":
        uri, name = upload_file(a.target)
        return ok(uri=uri, name=name, file=a.target)
    if a.action == "get":
        st, t = api.req(a.target, method="GET")
        if st != 200:
            e = _api_err(st, t)
            fail(e[0], e[1], e[2])
        return ok(file=json.loads(t))
    if a.action == "delete":
        st, t = api.req(a.target, method="DELETE")
        if st != 200:
            e = _api_err(st, t)
            fail(e[0], e[1], e[2])
        return ok(deleted=a.target)
    fail("BAD_ACTION", "use list|upload|get|delete")


def cmd_cache(a):
    api = Api(timeout=a.timeout)
    if a.action == "list":
        st, t = api.req("cachedContents?pageSize=100", method="GET")
        if st != 200:
            e = _api_err(st, t)
            fail(e[0], e[1], e[2])
        cs = [{"name": c.get("name"), "model": c.get("model"),
               "tokens": c.get("usageMetadata", {}).get("totalTokenCount"),
               "expire": c.get("expireTime")} for c in json.loads(t).get("cachedContents", [])]
        return ok(caches=cs, n=len(cs))
    if a.action == "create":
        if not a.file:
            fail("NO_INPUT", "cache create needs -f FILE")
        body = {"model": f"models/{a.model or DEFAULT_MODEL}",
                "contents": [{"role": "user", "parts": [part_text(slurp(a.file))]}],
                "ttl": a.ttl or "3600s"}
        if a.system:
            body["systemInstruction"] = {"parts": [part_text(a.system)]}
        st, t = api.req("cachedContents", body)
        if st != 200:
            e = _api_err(st, t)
            fail(e[0], e[1], e[2])
        d = json.loads(t)
        return ok(cache=d.get("name"), tokens=(d.get("usageMetadata") or {}).get("totalTokenCount"),
                  expire=d.get("expireTime"))
    if a.action in ("get", "delete"):
        st, t = api.req(a.target, method="GET" if a.action == "get" else "DELETE")
        if st != 200:
            e = _api_err(st, t)
            fail(e[0], e[1], e[2])
        return ok(cache=a.target, action=a.action, data=(json.loads(t) if a.action == "get" else None))
    fail("BAD_ACTION", "use list|create|get|delete")


RESEARCH_AGENTS = ["deep-research-preview-04-2026", "deep-research-max-preview-04-2026",
                   "deep-research-pro-preview-12-2025"]


def _interaction_text(d):
    """Pull the assistant-facing text out of an Interactions API payload."""
    out = []

    def add(v):
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
        elif isinstance(v, dict):
            for k in ("text", "output", "content", "result"):
                if k in v:
                    add(v[k])
        elif isinstance(v, list):
            for x in v:
                add(x)

    for k in ("output", "outputs", "result", "response"):
        if k in d:
            add(d[k])
    if not out:
        for st in d.get("steps", []) or []:
            t = (st.get("type") or "").lower()
            if t in ("user_input", "user-input"):
                continue
            add(st.get("content") or st.get("output") or st.get("text"))
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return "\n\n".join(uniq).strip()


def cmd_research(a):
    """Deep research / agent interactions (AI Studio Interactions API)."""
    api = Api(timeout=a.timeout)
    agent = a.agent or RESEARCH_AGENTS[0]
    U = f"{GEN_HOST}/interactions"
    started = time.time()

    if a.status:
        st, txt = api.req(f"{U}/{a.status}", method="GET", timeout=60)
        if st != 200:
            e = _api_err(st, txt)
            fail(e[0], e[1], e[2])
        d = json.loads(txt)
        text = _interaction_text(d)
        if a.json:
            return ok(id=d.get("id"), status=d.get("status"), text=text, raw=d)
        p = save(text or json.dumps(d, indent=1), ext=".md")
        return ok(file=p, s=p.stat().st_size, id=d.get("id"), status=d.get("status"))

    body = {"agent": agent, "input": a.prompt, "background": True}
    st, txt = api.req(U, body, timeout=120)
    if st != 200:
        e = _api_err(st, txt)
        fail(e[0], e[1], e[2])
    d = json.loads(txt)
    iid = d.get("id")
    if not a.wait:
        return ok(id=iid, status=d.get("status"), agent=agent, poll=f"ais research --status {iid}")

    deadline = time.time() + a.wait_timeout
    last = d
    while time.time() < deadline:
        time.sleep(a.interval)
        st, txt = api.req(f"{U}/{iid}", method="GET", timeout=60)
        if st != 200:
            continue
        last = json.loads(txt)
        if last.get("status") in ("completed", "succeeded", "failed", "cancelled"):
            break
    status = last.get("status")
    text = _interaction_text(last)
    if not text:
        fail("NO_OUTPUT", f"status={status}", id=iid, raw=json.dumps(last)[:400])
    if a.json:
        return ok(id=iid, status=status, text=text, agent=agent,
                  t_ms=int((time.time() - started) * 1000))
    p = save(text, ext=".md")
    return ok(file=p, s=p.stat().st_size, id=iid, status=status, agent=agent,
              t_ms=int((time.time() - started) * 1000))


def cmd_code(a):
    txt = slurp(a.file) if a.file else (OUTDIR / "last.md").read_text() if (OUTDIR / "last.md").exists() else None
    if not txt:
        fail("NO_INPUT", "give a file, or run a chat first")
    c = extract_code(txt)
    if a.json:
        return ok(code=c, n=len(re.findall(r"```", txt)) // 2)
    p = save(c, ext=".txt")
    ok(file=p, s=len(c))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="ais", description="Google AI Studio CLI (free, no paid API)")
    p.add_argument("--json", action="store_true", help="print payload inline instead of a file pointer")
    p.add_argument("--pretty", action="store_true", help="pretty-print JSON output")
    sub = p.add_subparsers(dest="cmd", required=True)
    # allow --json/--pretty AFTER the subcommand too (agents type them there)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--pretty", action="store_true", default=argparse.SUPPRESS)

    def subp(name, **kw):
        return sub.add_parser(name, parents=[common], **kw)

    def gen_flags(sp):
        sp.add_argument("-m", "--model")
        sp.add_argument("-s", "--system")
        sp.add_argument("-c", "--session", help="multi-turn session name or .json path")
        sp.add_argument("--new", action="store_true", help="reset the session")
        sp.add_argument("--no-context", action="store_true", help="don't send history")
        sp.add_argument("--turns", type=int, help="trim history to N turns")
        sp.add_argument("-f", "--file", action="append", dest="files", help="attach file (uploaded)")
        sp.add_argument("-i", "--image", action="append", dest="images", help="attach image (inline)")
        sp.add_argument("--stream", action="store_true")
        sp.add_argument("--raw", action="store_true", help="no stderr progress")
        sp.add_argument("--json-mode", action="store_true", help="force JSON response")
        sp.add_argument("--schema", help="responseSchema JSON file")
        sp.add_argument("--temp", type=float)
        sp.add_argument("--topp", type=float)
        sp.add_argument("--topk", type=int)
        sp.add_argument("--max", type=int, help="maxOutputTokens")
        sp.add_argument("--think", help="thinking budget int or low|high")
        sp.add_argument("--search", action="store_true", help="Google Search grounding")
        sp.add_argument("--url-context", action="store_true")
        sp.add_argument("--brief", action="store_true")
        sp.add_argument("--code", action="store_true", help="emit only fenced code blocks")
        sp.add_argument("--show-thoughts", action="store_true")
        sp.add_argument("--save-images", help="dir for inline images in the response")
        sp.add_argument("-t", "--timeout", type=int, default=180)
        sp.add_argument("--no-fallback", action="store_false", dest="fallback", default=True,
                        help="do not fall back to another model on transient errors")

    c = subp("auth"); c.add_argument("--capture", action="store_true",
                                             help="provision the free API key from the browser"); c.set_defaults(fn=cmd_auth)
    c = subp("account"); c.set_defaults(fn=cmd_account)
    c = subp("key"); c.add_argument("--show", action="store_true"); c.add_argument("--capture", action="store_true"); c.set_defaults(fn=cmd_key)
    c = subp("models"); c.add_argument("--filter"); c.add_argument("--backend", choices=["web", "api"], default="web"); c.set_defaults(fn=cmd_models)
    c = subp("quota"); c.set_defaults(fn=cmd_quota)
    c = subp("promos"); c.set_defaults(fn=cmd_promos)
    c = subp("projects"); c.set_defaults(fn=cmd_projects)
    c = subp("call"); c.add_argument("method"); c.add_argument("body", nargs="?"); c.set_defaults(fn=cmd_call)
    c = subp("methods"); c.set_defaults(fn=cmd_methods)

    c = subp("chat"); c.add_argument("prompt", nargs="?"); c.add_argument("-p", "--prompt", dest="prompt_opt")
    c.add_argument("--prompt-file"); gen_flags(c); c.set_defaults(fn=cmd_chat)
    c = subp("image"); c.add_argument("prompt"); c.add_argument("-m", "--model"); c.add_argument("-o", "--out")
    c.add_argument("--aspect"); c.add_argument("-t", "--timeout", type=int, default=180); c.set_defaults(fn=cmd_image)
    c = subp("tts"); c.add_argument("text"); c.add_argument("-m", "--model"); c.add_argument("-v", "--voice")
    c.add_argument("-o", "--out"); c.add_argument("-t", "--timeout", type=int, default=180); c.set_defaults(fn=cmd_tts)
    c = subp("video"); c.add_argument("prompt"); c.add_argument("-m", "--model"); c.add_argument("-o", "--out")
    c.add_argument("--aspect"); c.add_argument("--wait", action="store_true"); c.add_argument("--wait-timeout", type=int, default=420)
    c.add_argument("-t", "--timeout", type=int, default=180); c.set_defaults(fn=cmd_video)
    c = subp("embed"); c.add_argument("text", nargs="*"); c.add_argument("-m", "--model"); c.add_argument("--task")
    c.add_argument("-t", "--timeout", type=int, default=120); c.set_defaults(fn=cmd_embed)
    c = subp("tokens"); c.add_argument("text", nargs="?"); c.add_argument("-f", "--file"); c.add_argument("-m", "--model")
    c.add_argument("-t", "--timeout", type=int, default=60); c.set_defaults(fn=cmd_tokens)
    c = subp("files"); c.add_argument("action", choices=["list", "upload", "get", "delete"]); c.add_argument("target", nargs="?")
    c.add_argument("-t", "--timeout", type=int, default=300); c.set_defaults(fn=cmd_files)
    c = subp("cache"); c.add_argument("action", choices=["list", "create", "get", "delete"]); c.add_argument("target", nargs="?")
    c.add_argument("-f", "--file"); c.add_argument("-m", "--model"); c.add_argument("-s", "--system"); c.add_argument("--ttl")
    c.add_argument("-t", "--timeout", type=int, default=180); c.set_defaults(fn=cmd_cache)
    c = subp("research")
    c.add_argument("prompt", nargs="?")
    c.add_argument("--agent", help="agent id (default deep-research-preview-04-2026)")
    c.add_argument("--status", help="poll an interaction id instead of starting one")
    c.add_argument("--wait", action="store_true", help="block until the research finishes")
    c.add_argument("--wait-timeout", type=int, default=900)
    c.add_argument("--interval", type=int, default=10)
    c.add_argument("-t", "--timeout", type=int, default=180)
    c.set_defaults(fn=cmd_research)
    c = subp("code"); c.add_argument("file", nargs="?"); c.set_defaults(fn=cmd_code)
    return p


def main():
    p = build_parser()
    a = p.parse_args()
    if getattr(a, "prompt_opt", None) and not getattr(a, "prompt", None):
        a.prompt = a.prompt_opt
    for d in (HOME, OUTDIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    try:
        a.fn(a)
    except AisError as e:
        fail(e.err, e.msg, e.retry, **e.extra)
    except BrokenPipeError:
        sys.exit(0)
    except KeyboardInterrupt:
        fail("INTERRUPTED", "cancelled")
    except Exception as e:
        fail("ERROR", f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
