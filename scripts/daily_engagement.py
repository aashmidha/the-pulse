#!/usr/bin/env python3
"""
Daily engagement log.

The engagement metric (the North Star: % of B2C+B2Cd subscribers with RFV > 19) is
recomputed every dashboard refresh but only ever *displayed* — it is not stored anywhere,
so there is no history of it. This job reads the exact number the dashboard is showing
(straight from the published metrics.json) once a day and appends one row to an
"Engagement" tab on the same Google Sheet, via the shared Apps Script endpoint.

Env:
  PAGES_URL         — dashboard base URL (defaults to the .pages.dev origin)
  HITS_WEBHOOK_URL  — the Google Apps Script web-app URL (shared append endpoint)
  HITS_WEBHOOK_KEY  — shared secret, must match the SECRET in the script
Run: python3 scripts/daily_engagement.py   (prints the value; only writes to the sheet
                                             if HITS_WEBHOOK_URL is set)
"""
import json, os, urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PAGES_URL = "https://the-pulse-3er.pages.dev"
TAB = "Engagement"
HEADER = ["Date", "Engagement %", "Subscribers above (RFV>19)",
          "Total subscribers", "Threshold (RFV)", "Δ vs prev week (pts)"]


def dev_var(name):
    v = os.environ.get(name)
    if v:
        return v
    f = Path(__file__).resolve().parent.parent / ".dev.vars"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, val = line.split("=", 1)
                if k.strip() == name:
                    return val.strip().strip('"').strip("'")
    return None


def fetch_engagement():
    base = (dev_var("PAGES_URL") or DEFAULT_PAGES_URL).rstrip("/")
    url = base + "/metrics.json"
    req = urllib.request.Request(url, headers={"User-Agent": "daily-engagement"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode()).get("engagement") or {}


def main():
    today = datetime.now(timezone.utc).date().isoformat()
    eng = fetch_engagement()
    pct = eng.get("pctAbove")
    if pct is None:
        print(f"{today} — engagement unavailable (feed empty / NA). Nothing logged.")
        return
    above, subs = eng.get("above"), eng.get("subs")
    thr, delta = eng.get("threshold"), eng.get("deltaWeek")
    print(f"{today} — engagement {pct}% ({above} of {subs} subscribers over RFV {thr}, "
          f"{delta:+} pts vs prev week)")

    url = dev_var("HITS_WEBHOOK_URL")
    if not url:
        print("\n(HITS_WEBHOOK_URL not set — printed only, nothing written to the sheet.)")
        return
    payload = {
        "key": dev_var("HITS_WEBHOOK_KEY") or "",
        "tab": TAB,
        "header": HEADER,
        "upsertCol": 0,   # replace any existing row for the same Date (idempotent per day)
        "rows": [[today, pct, above, subs, thr, delta]],
    }
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=45) as r:
        print("\nSheet response:", r.read().decode()[:200])


if __name__ == "__main__":
    main()
