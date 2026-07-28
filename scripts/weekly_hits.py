#!/usr/bin/env python3
"""
Weekly HITs report.

Every Saturday: pull the BART 7-day subscriber-reads report, keep the articles read by
more than 1,000 *unique subscribers* (diff_users — the same "reads" number the dashboard's
"Most Read — 7 Days" panel shows) — the "HITs" the team currently tallies by hand — and
append them to the working Google Sheet via its Apps Script web app (a running log, one row
per hit, tagged with the ISO week number). Total volume is still logged for reference.

Env:
  BART_SESSION_COOKIE, BART_NAME  — BART auth (same session cookie the dashboard uses)
  HITS_WEBHOOK_URL                — the Google Apps Script web-app URL (append endpoint)
  HITS_WEBHOOK_KEY                — shared secret, must match the SECRET in the script
Run: python3 scripts/weekly_hits.py   (prints the hits; only writes to the sheet if
                                        HITS_WEBHOOK_URL is set)
"""
import json, os, re, urllib.parse, urllib.request
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

THRESHOLD = 1000        # Total volume (tot_views) strictly over this = a HIT
BART_URL = "https://bart.finance.si/master.php"


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


def fetch_hits():
    """All 7-day subscriber-read articles over the HIT threshold, biggest first."""
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
    hits = []
    for m in re.finditer(r'<tr data-id="([^"]*)"[^>]*>([\s\S]*?)</tr>', htm):
        aid, row = m.group(1), m.group(2)
        url_m = re.search(r'href="([^"]+)"', row)
        # title = the plain-text align="left" cell (the article-id cell has an <a>, so [^<]+ skips it)
        title_m = re.search(r'</td>\s*<td[^>]*align="left"[^>]*>([^<]+)</td>', row)
        du = re.search(r'data-key="diff_users">(\d+)', row)
        tv = re.search(r'data-key="tot_views">(\d+)', row)
        reads = int(du.group(1)) if du else 0   # unique subscribers who read it (the dashboard "reads")
        tot = int(tv.group(1)) if tv else 0     # total volume (counts repeat opens) — kept for reference
        title = unescape(title_m.group(1).strip()) if title_m else ""
        # A HIT = read by more than THRESHOLD *unique* subscribers, matching the
        # "Most Read — 7 Days" panel on the dashboard (not total volume).
        if reads > THRESHOLD and title:
            hits.append({"articleId": aid, "url": url_m.group(1) if url_m else "",
                         "title": title, "uniqueUsers": reads, "totalVolume": tot})
    hits.sort(key=lambda h: h["uniqueUsers"], reverse=True)
    return hits


def main():
    now = datetime.now(timezone.utc)
    week = now.isocalendar()[1]
    hits = fetch_hits()
    print(f"Week {week} — {len(hits)} HIT(s) over {THRESHOLD} subscriber reads:")
    for h in hits:
        print(f"  {h['uniqueUsers']:>5} reads · {h['totalVolume']:>5} vol  {h['title'][:60]}")

    url = dev_var("HITS_WEBHOOK_URL")
    if not url:
        print("\n(HITS_WEBHOOK_URL not set — printed only, nothing written to the sheet.)")
        return
    payload = {"key": dev_var("HITS_WEBHOOK_KEY") or "", "week": week,
               "date": now.date().isoformat(), "hits": hits}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=45) as r:
        print("\nSheet response:", r.read().decode()[:200])


if __name__ == "__main__":
    main()
