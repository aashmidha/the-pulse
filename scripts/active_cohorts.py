#!/usr/bin/env python3
"""
Daily ACTIVE-subscriber cohort metrics, straight from BART (no Piano, no manual CSV).

For B2C (=B2C+B2Cd) and B2B, per day:
  - active users  = subscribers who read that day: Userstatus=Subscriber, SubStatus in cohort,
                    R = 0 AND F > 0   (counted server-side via /ma/segments)
  - avg pageviews = mean V (Volume) of just those active users (segment gives their UIDs;
                    we join to the /rfv feed's V and average)

Writes one row/day to the "B2C eng." and "B2B eng." tabs (Date · Active users · Avg pageviews),
upsert by date. Env: BART_KEY/BART_BASE_URL(/BART_GROUP), HITS_WEBHOOK_URL, HITS_WEBHOOK_KEY.
Run: python3 scripts/active_cohorts.py   (prints; writes only if HITS_WEBHOOK_URL is set)
"""
import csv, io, json, os, time, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COHORTS = {"B2C eng.": ["B2C", "B2Cd"], "B2B eng.": ["B2B"]}


def dev_var(name):
    v = os.environ.get(name)
    if v:
        return v
    f = ROOT / ".dev.vars"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line and line.split("=", 1)[0].strip() == name:
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    # fall back to the BART settings file used by refresh_metrics
    sf = Path.home() / "BART List" / "enricher_settings.json"
    if sf.exists():
        s = json.loads(sf.read_text())
        return s.get({"BART_KEY": "bart_key", "BART_BASE_URL": "bart_base",
                      "BART_GROUP": "bart_group"}.get(name, ""), None)
    return None


BART_BASE = (dev_var("BART_BASE_URL") or "https://bonnieraws.dergan.net").rstrip("/")
BART_KEY = dev_var("BART_KEY") or ""
BART_GROUP = dev_var("BART_GROUP") or "BPIE_ALL"


def _segment(criteria):
    """POST /ma/segments -> (count, uid_csv_url). Light retries for transient errors."""
    body = json.dumps({"key": BART_KEY, "criteria": criteria}).encode()
    url = f"{BART_BASE}/ma/segments?group={BART_GROUP}"
    last = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    url, data=body, headers={"Content-Type": "application/json"}, method="POST"), timeout=45) as r:
                d = json.loads(r.read().decode())
                return d.get("count"), d.get("url")
        except Exception as e:
            last = e; time.sleep(3)
    raise last


def _active_criteria(substatus):
    return [{"what": "userdata", "which": "Userstatus", "op": "=", "val": "Subscriber"},
            {"what": "userdata", "which": "SubStatus", "op": "=", "val": substatus},
            {"what": "rfv", "which": "R", "op": "=", "val": "0"},
            {"what": "rfv", "which": "F", "op": ">", "val": "0"}]


def _uids_from_csv(url):
    """The segment CSV has some log lines, then a 'User ID' header line, then one UID per line."""
    with urllib.request.urlopen(url, timeout=45) as r:
        text = r.read().decode("utf-8", "replace")
    uids, started = set(), False
    for row in csv.reader(io.StringIO(text)):
        cell = (row[0].strip() if row else "")
        if not started:
            if cell.lower().replace(" ", "") == "userid":
                started = True
            continue
        if cell:
            uids.add(cell)
    return uids


def rfv_volumes():
    """Full /rfv feed as {uid: V}."""
    vol, page = {}, 1
    while True:
        url = f"{BART_BASE}/rfv?" + urllib.parse.urlencode({"key": BART_KEY, "page": page})
        with urllib.request.urlopen(url, timeout=60) as r:
            d = json.loads(r.read().decode())
        for h in d.get("hits", []):
            u, v = h.get("uid"), h.get("v")
            if u is not None and isinstance(v, (int, float)):
                vol[u] = v
        if not d.get("next_page"):
            break
        page += 1
    return vol


def post_row(tab, date, active, avg_pv):
    url = dev_var("HITS_WEBHOOK_URL")
    if not url:
        return
    payload = {"key": dev_var("HITS_WEBHOOK_KEY") or "", "tab": tab,
               "header": ["Date", "Active users", "Average pageviews"],
               "upsertCol": 0, "rows": [[date, active, avg_pv]]}
    body = json.dumps(payload).encode()
    for _ in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    url, data=body, headers={"Content-Type": "application/json"}, method="POST"), timeout=45) as r:
                print(f"  {tab}: {r.read().decode()[:80]}"); return
        except urllib.error.HTTPError:
            time.sleep(3)


def main():
    today = datetime.now(timezone.utc).date().isoformat()
    vol = rfv_volumes()   # fetched once, shared across cohorts
    for tab, substatuses in COHORTS.items():
        uids = set()
        for ss in substatuses:
            _, csv_url = _segment(_active_criteria(ss))
            if csv_url:
                uids |= _uids_from_csv(csv_url)
        active = len(uids)
        vs = [vol[u] for u in uids if u in vol]
        avg_pv = round(sum(vs) / len(vs), 1) if vs else 0
        print(f"{today}  {tab:10}  active={active:>5}  avg_pageviews={avg_pv}")
        post_row(tab, today, active, avg_pv)
    if not dev_var("HITS_WEBHOOK_URL"):
        print("\n(HITS_WEBHOOK_URL not set — printed only, nothing written to the sheet.)")


if __name__ == "__main__":
    main()
