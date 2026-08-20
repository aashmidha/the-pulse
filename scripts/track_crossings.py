#!/usr/bin/env python3
"""
Article HIT crossings tracker (replaces the weekly HITs roster).

Once a day: pull BART's 7-day subscriber-reads feed and log each article the FIRST time
its unique subscriber reads (diff_users — the same "reads" as the old HITs) cross 1,000.
Each article is logged exactly once, tagged with the date we detected the crossing, into
a "Crossings" tab on the working Google Sheet.

State: a "seen" set of article IDs already logged, kept in data/crossings_seen.json (the
dashboard job restores/saves it to Cloudflare KV). On the very first run (no seen file),
every article already over 1,000 is recorded once as a BASELINE — we can't know when they
actually crossed — and tracking is purely forward-looking from then on.

Env:
  BART_SESSION_COOKIE, BART_NAME  — BART auth (same session cookie the dashboard uses)
  HITS_WEBHOOK_URL                — the shared Apps Script web-app URL (append endpoint)
  HITS_WEBHOOK_KEY                — shared secret, must match the SECRET in the script
Run: python3 scripts/track_crossings.py   (prints crossings; only writes to the sheet if
                                            HITS_WEBHOOK_URL is set)
"""
import json, os, re, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta
from html import unescape
from pathlib import Path

THRESHOLD = 1000        # unique subscriber reads (diff_users) strictly over this = a HIT
BART_URL = "https://bart.finance.si/master.php"
TAB = "Crossings"
ROOT = Path(__file__).resolve().parent.parent
SEEN_FILE = ROOT / "data" / "crossings_seen.json"
PRUNE_DAYS = 90         # forget IDs older than this (well past the 7-day window; keeps state small)
HEADER = ["Date", "Article ID", "Title", "Subscriber reads", "URL", "Note"]


def dev_var(name):
    v = os.environ.get(name)
    if v:
        return v
    f = ROOT / ".dev.vars"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, val = line.split("=", 1)
                if k.strip() == name:
                    return val.strip().strip('"').strip("'")
    return None


def fetch_reads():
    """Every article in the 7-day subscriber-reads feed: [{articleId, url, title, reads}]."""
    cookie = dev_var("BART_SESSION_COOKIE"); name = dev_var("BART_NAME") or ""
    if not cookie:
        raise SystemExit("Missing BART_SESSION_COOKIE")
    body = urllib.parse.urlencode({
        "op": "atom-toparticles-7", "group": "BPIE_ALL", "view": "group",
        "filter_by": "user_param_userstatus", "filter_val": "Subscriber", "limit": "100",
    }).encode()
    req = urllib.request.Request(BART_URL, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://bart.finance.si", "Referer": "https://bart.finance.si/?view=group&group=BPIE_ALL",
        "Cookie": f"bartdemo={cookie}; bartdemo2={urllib.parse.quote(name)}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        htm = json.loads(r.read().decode()).get("html", "")
    out = []
    for m in re.finditer(r'<tr data-id="([^"]*)"[^>]*>([\s\S]*?)</tr>', htm):
        aid, row = m.group(1), m.group(2)
        url_m = re.search(r'href="([^"]+)"', row)
        title_m = re.search(r'</td>\s*<td[^>]*align="left"[^>]*>([^<]+)</td>', row)
        du = re.search(r'data-key="diff_users">(\d+)', row)
        reads = int(du.group(1)) if du else 0
        title = unescape(title_m.group(1).strip()) if title_m else ""
        if aid and title:
            out.append({"articleId": aid, "url": url_m.group(1) if url_m else "",
                        "title": title, "reads": reads})
    return out


def post_rows(rows):
    url = dev_var("HITS_WEBHOOK_URL")
    if not url:
        print("\n(HITS_WEBHOOK_URL not set — printed only, nothing written to the sheet.)")
        return
    if not rows:
        return
    payload = {"key": dev_var("HITS_WEBHOOK_KEY") or "", "tab": TAB, "header": HEADER, "rows": rows}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=45) as r:
        print("Sheet response:", r.read().decode()[:200])


def main():
    today = datetime.now(timezone.utc).date().isoformat()
    cold_start = not SEEN_FILE.exists()
    seen = {} if cold_start else json.loads(SEEN_FILE.read_text())

    over = [a for a in fetch_reads() if a["reads"] > THRESHOLD]
    over.sort(key=lambda a: a["reads"], reverse=True)

    fresh = [a for a in over if a["articleId"] not in seen]
    note = "baseline" if cold_start else ""
    rows = [[today, a["articleId"], a["title"], a["reads"], a["url"], note] for a in fresh]

    for a in fresh:
        seen[a["articleId"]] = {"date": today, "reads": a["reads"], "title": a["title"]}
    # prune long-past IDs so the state file stays small
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=PRUNE_DAYS)).isoformat()
    seen = {k: v for k, v in seen.items() if v.get("date", today) >= cutoff}
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen))

    if cold_start:
        print(f"{today} — cold start: {len(fresh)} article(s) already over {THRESHOLD} reads "
              f"(logged as baseline). Tracking new crossings from here.")
    else:
        print(f"{today} — {len(fresh)} new crossing(s) over {THRESHOLD} subscriber reads:")
    for a in fresh:
        print(f"  {a['reads']:>5} reads  {a['title'][:60]}")
    post_rows(rows)


if __name__ == "__main__":
    main()
