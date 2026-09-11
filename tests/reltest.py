#!/usr/bin/env python3
"""Probe free-tier model reliability/latency, to choose the CLI default."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ais

MODELS = ["gemini-2.5-flash", "gemini-flash-lite-latest", "gemini-3.5-flash",
          "gemini-3.1-flash-lite", "gemini-3.6-flash", "gemini-3.8-flash"]
N = 4
api = ais.Api(timeout=35)
body = {"contents": [{"role": "user", "parts": [{"text": "Say OK"}]}]}
for m in MODELS:
    ok, errs, lat = 0, {}, []
    for _ in range(N):
        t = time.time()
        try:
            api.gen(m, body, timeout=35)
            ok += 1
            lat.append(round(time.time() - t, 2))
        except ais.AisError as e:
            errs[e.err] = errs.get(e.err, 0) + 1
    avg = round(sum(lat) / len(lat), 2) if lat else None
    print(f"{m:26s} {ok}/{N} avg={avg}s lat={lat} errs={errs}", flush=True)
