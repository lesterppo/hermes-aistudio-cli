#!/usr/bin/env python3
"""Live regression suite for the `ais` CLI.

Exercises the real AI Studio backends (cookie RPC + free-tier API key).
No mocking. Usage:  python3 tests/live_test.py [--quick]

Assertions are on observable behaviour: exit status, the JSON pointer
contract, and payload contents that can only come from a real round trip.
"""
import json
import os
import subprocess
import sys
import time

AIS = os.environ.get("AIS_BIN", "ais")
QUICK = "--quick" in sys.argv

PASS, FAIL, SKIP = [], [], []


def run(args, timeout=240, stdin=None):
    t = time.time()
    p = subprocess.run([AIS] + args, capture_output=True, text=True,
                       timeout=timeout, input=stdin)
    dt = time.time() - t
    out = (p.stdout or "").strip().splitlines()
    try:
        d = json.loads(out[-1]) if out else {}
    except Exception:
        d = {"ok": False, "err": "UNPARSEABLE", "raw": (p.stdout or "")[-200:]}
    return d, dt, p


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}  {detail}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


def skip(name, why):
    SKIP.append(name)
    print(f"  SKIP  {name}  ({why})")


def readptr(d):
    if d.get("f") and os.path.exists(d["f"]):
        return open(d["f"], encoding="utf-8").read()
    return d.get("text", "")


def main():
    print("== auth / account ==")
    d, dt, _ = run(["auth"])
    check("auth", d.get("ok") and (d.get("web_ok") or d.get("api_ok")), f"{dt:.1f}s {d}")

    d, _, _ = run(["account"])
    check("account", d.get("ok") and d.get("web_models"), f"models={d.get('web_models')} plan={d.get('plan')}")
    check("account free tier", d.get("plan") == "free", f"plan={d.get('plan')}")

    d, _, _ = run(["key"])
    check("key pointer", d.get("ok") and d.get("suffix") and "key" not in d, f"suffix={d.get('suffix')}")

    print("== web RPC (cookie auth, no API key) ==")
    d, dt, _ = run(["models"])
    check("models web", d.get("ok") and d.get("n", 0) > 20, f"n={d.get('n')} {dt:.1f}s")
    d, _, _ = run(["models", "--filter", "3.5", "--json"])
    check("models filter+json", d.get("ok") and d.get("n", 99) < 40 and "3.5" in json.dumps(d))

    d, _, _ = run(["quota"])
    check("quota", d.get("ok") and d.get("n", 0) > 10, f"groups={d.get('n')}")

    d, _, _ = run(["promos"])
    check("promos", d.get("ok") and d.get("n", 0) >= 0, f"n={d.get('n')}")

    d, _, _ = run(["projects"])
    check("projects", d.get("ok") and d.get("cloud"), f"{d.get('cloud')}")

    d, _, _ = run(["call", "GetLoggingContext", "--json"])
    check("rpc escape hatch", d.get("ok") and d.get("status") == 200)

    d, _, _ = run(["methods"])
    check("methods catalog", d.get("ok") and d.get("n", 0) > 80, f"n={d.get('n')}")

    print("== generation (free-tier API key) ==")
    d, dt, _ = run(["chat", "-p", "Reply with exactly this token and nothing else: AIS_T1"])
    body = readptr(d)
    check("chat single turn", d.get("ok") and "AIS_T1" in body, f"{dt:.1f}s out={body[:40]!r}")
    check("chat usage metadata", isinstance(d.get("u"), dict) and d["u"].get("total"), f"{d.get('u')}")

    d, dt, _ = run(["chat", "-p", "Count from 1 to 4", "--stream", "--raw"])
    check("chat streaming", d.get("ok") and readptr(d).strip(), f"{dt:.1f}s out={readptr(d)[:40]!r}")
    check("stream is fast", dt < 20, f"{dt:.1f}s (1-byte-read regression guard)")

    run(["chat", "-c", "livetest", "--new", "-p", "My access code is ORCA-4417. Reply OK."])
    d, _, _ = run(["chat", "-c", "livetest", "-p", "What is my access code? Answer with just the code."])
    check("multi-turn memory", d.get("ok") and "ORCA-4417" in readptr(d), f"out={readptr(d)[:40]!r}")

    d, _, _ = run(["chat", "-s", "You are a pirate. End every reply with ARRR.", "-p", "Say hi."])
    check("system instruction", d.get("ok") and "ARRR" in readptr(d).upper(), f"out={readptr(d)[:40]!r}")

    d, _, _ = run(["chat", "-p", 'Return {"ok":true}', "--json-mode"])
    got = readptr(d)
    try:
        json.loads(got)
        valid = True
    except Exception:
        valid = False
    check("json mode", d.get("ok") and valid, f"out={got[:50]!r}")

    d, _, _ = run(["chat", "--code", "-p", "Write only a fenced python code block defining add(a,b)."])
    got = readptr(d)
    check("code extraction", d.get("ok") and "def add" in got and "```" not in got, f"out={got[:50]!r}")

    d, _, _ = run(["chat", "--think", "off", "-p", "say THINK_OFF"])
    check("think off", d.get("ok") and (d.get("u") or {}).get("think") in (None, 0), f"think={(d.get('u') or {}).get('think')}")

    print("== attachments ==")
    p = "/tmp/ais_test_secret.txt"
    open(p, "w").write("The vault code is ZEBRA-7731.\n")
    d, _, _ = run(["chat", "-f", p, "-p", "What is the vault code? Answer with the code only."])
    check("file attach (upload+ground)", d.get("ok") and "ZEBRA-7731" in readptr(d), f"out={readptr(d)[:40]!r}")

    img = "/tmp/ais_test_shapes.png"
    try:
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (220, 120), "white")
        dr = ImageDraw.Draw(im)
        dr.ellipse((20, 20, 90, 90), fill="red")
        dr.rectangle((120, 35, 200, 85), fill="blue")
        im.save(img)
        d, _, _ = run(["chat", "-i", img, "-p", "List the shapes and their colours, comma separated."])
        got = readptr(d).lower()
        check("image attach (vision)", d.get("ok") and "red" in got and "blue" in got, f"out={readptr(d)[:60]!r}")
    except ImportError:
        skip("image attach (vision)", "PIL not installed")

    print("== utility commands ==")
    d, _, _ = run(["tokens", "hello world this is a token test"])
    check("countTokens", d.get("ok") and isinstance(d.get("tokens"), int) and d["tokens"] > 0, f"tokens={d.get('tokens')}")

    d, _, _ = run(["embed", "hello world"])
    check("embed", d.get("ok") and d.get("dim", 0) > 100, f"dim={d.get('dim')}")

    d, _, _ = run(["embed", "a", "b", "c"])
    check("batch embed", d.get("ok") and d.get("n") == 3, f"n={d.get('n')}")

    d, _, _ = run(["files", "list"])
    check("files list", d.get("ok") and isinstance(d.get("files"), list), f"n={d.get('n')}")

    d, _, _ = run(["files", "upload", p])
    check("files upload", d.get("ok") and d.get("uri"), f"{d.get('name')}")

    d, _, _ = run(["research", "What is 2+2?"], timeout=120)
    check("deep research submit", d.get("ok") and d.get("id") and d.get("status"),
          f"id={(d.get('id') or '')[:24]} status={d.get('status')}")
    if d.get("id") and not QUICK:
        d2, _, _ = run(["research", "--status", d["id"], "--json"], timeout=120)
        check("deep research poll", d2.get("ok") and d2.get("status"), f"status={d2.get('status')}")

    d, _, _ = run(["tts", "Short test."])
    ok_tts = d.get("ok") and d.get("bytes", 0) > 1000
    if ok_tts:
        import wave
        w = wave.open(d["wav"])
        check("tts (valid wav)", w.getframerate() == 24000 and w.getnframes() > 0,
              f"{w.getnframes()/w.getframerate():.1f}s @{w.getframerate()}Hz")
    else:
        skip("tts", d.get("err", "unavailable"))

    print("== error handling ==")
    d, _, p = run(["chat", "-p", "hi", "-m", "gemini-2.5-nonexistent"])
    check("bad model -> NOT_FOUND", d.get("ok") is False and d.get("err") in ("NOT_FOUND", "BAD_REQUEST", "HTTP_404"),
          f"{d.get('err')}")
    check("errors are json not traceback", "Traceback" not in (p.stdout + p.stderr))

    d, _, p = run(["chat", "-f", "/tmp/definitely_missing.txt", "-p", "hi"])
    check("missing file -> BAD_FILE", d.get("ok") is False and d.get("err") == "BAD_FILE", f"{d.get('err')}")

    d, _, _ = run(["chat"])
    check("empty prompt rejected", d.get("ok") is False and d.get("err") in ("NO_PROMPT", "EMPTY"), f"{d.get('err')}")

    d, _, p = run(["call", "NoSuchMethodAnywhere", "[]"])
    check("bad rpc method -> json error", d.get("ok") is False and "Traceback" not in (p.stdout + p.stderr),
          f"{d.get('err')}")

    print("== pointer contract ==")
    d, _, p = run(["chat", "-p", "say PTR_OK"])
    check("stdout is single json line", len([l for l in p.stdout.strip().splitlines() if l.strip()]) == 1)
    check("payload on disk, not stdout", d.get("f") and os.path.getsize(d["f"]) > 0 and "PTR_OK" not in p.stdout)
    check("stderr clean on success", p.stderr.strip() == "", repr(p.stderr[:60]))

    print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed, {len(SKIP)} skipped ====")
    if FAIL:
        print("failed: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
