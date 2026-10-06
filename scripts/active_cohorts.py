#!/usr/bin/env python3
"""
Daily ACTIVE-subscriber cohort metrics, straight from BART (no Piano, no manual CSV).

For B2C (=B2C+B2Cd) and B2B, for one DAY (default: yesterday, Irish time — the latest complete
day, which is also the day the RFV snapshot's R=0 refers to):
  - active users  = subscribers who read that day: Userstatus=Subscriber, SubStatus in cohort,
                    R = 0 AND F > 0   (counted server-side via /ma/segments)
  - avg pageviews = that day's article reads per active user, from each user's BART read history
                    (/user/UID/articles) — complete, no attribution gap (unlike Piano Analytics).
                    NOT the RFV V, which is a ~90-day total.

Writes one row/day to the "B2C eng." and "B2B eng." tabs (Date · Active users · Avg pageviews),
upsert by date. Env: BART_KEY/BART_BASE_URL(/BART_GROUP), HITS_WEBHOOK_URL, HITS_WEBHOOK_KEY.
Run: python3 scripts/active_cohorts.py   (prints; writes only if HITS_WEBHOOK_URL is set)

Backfill past days: python3 scripts/active_cohorts.py --backfill 2026-09-29 2026-10-05 [--write]
  (prints only unless --write AND HITS_WEBHOOK_URL is set; CI: backfill-cohorts.yml)
  The R=0 segment only describes the latest day, so for past days "active" = current cohort
  subscribers with >=1 read that day (same definition, but uses today's subscriber list).
"""
import csv, io, json, os, sys, time, urllib.parse, urllib.request, urllib.error
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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


def _subscriber_criteria(substatus):
    return _active_criteria(substatus)[:2]


# /user/UID/articles returns only the newest 100 reads unless `limit` is raised — over half of
# active B2B users exceed that, so ask for far more than anyone reads in the history window.
HISTORY_LIMIT = 5000


def reads_by_day(uid):
    """One user's BART read history as Counter({'YYYY-MM-DD': reads}). date_local is Irish time."""
    url = (f"{BART_BASE}/user/{urllib.parse.quote(uid)}/articles?"
           + urllib.parse.urlencode({"key": BART_KEY, "limit": HISTORY_LIMIT}))
    last = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                d = json.loads(r.read().decode())
            return Counter(h["date_local"][:10] for h in d.get("hits", []) if h.get("date_local"))
        except Exception as e:
            last = e; time.sleep(2)
    raise last


def histories(uids):
    """{uid: Counter} for many users, fetched concurrently (~2,300 B2C users in about a minute)."""
    uids = sorted(uids)
    with ThreadPoolExecutor(max_workers=8) as ex:
        return dict(zip(uids, ex.map(reads_by_day, uids)))


def cohort_uids(substatuses, criteria):
    uids = set()
    for ss in substatuses:
        _, csv_url = _segment(criteria(ss))
        if csv_url:
            uids |= _uids_from_csv(csv_url)
    return uids


def post_row(tab, date, active, avg_pv):
    post_rows(tab, [[date, active, avg_pv]])


def post_rows(tab, rows):
    url = dev_var("HITS_WEBHOOK_URL")
    if not url:
        return
    payload = {"key": dev_var("HITS_WEBHOOK_KEY") or "", "tab": tab,
               "header": ["Date", "Active users", "Average pageviews"],
               "upsertCol": 0, "rows": rows}
    body = json.dumps(payload).encode()
    for _ in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    url, data=body, headers={"Content-Type": "application/json"}, method="POST"), timeout=45) as r:
                print(f"  {tab}: {r.read().decode()[:80]}"); return
        except urllib.error.HTTPError:
            time.sleep(3)


def main():
    day = (datetime.now(ZoneInfo("Europe/Dublin")).date() - timedelta(days=1)).isoformat()
    failed = False
    for tab, substatuses in COHORTS.items():
        uids = cohort_uids(substatuses, _active_criteria)
        hist = histories(uids)
        active = len(uids)
        readers = sum(1 for c in hist.values() if c[day])
        avg_pv = round(sum(c[day] for c in hist.values()) / active, 2) if active else 0
        print(f"{day}  {tab:10}  active={active:>5}  avg_pageviews={avg_pv}  (readers that day: {readers})")
        # R=0 should mean "read on `day`". If most of the segment didn't, the RFV snapshot hasn't
        # rolled over yet — fail so the daily job's marker stays unset and it retries next run.
        if active and readers / active < 0.9:
            print(f"  ! only {readers}/{active} active users have reads on {day} — snapshot not "
                  f"updated yet? not writing.")
            failed = True
            continue
        post_row(tab, day, active, avg_pv)
    if not dev_var("HITS_WEBHOOK_URL"):
        print("\n(HITS_WEBHOOK_URL not set — printed only, nothing written to the sheet.)")
    if failed:
        sys.exit(1)


def backfill(start, end, write=False):
    days = []
    d = date.fromisoformat(start)
    while d <= date.fromisoformat(end):
        days.append(d.isoformat()); d += timedelta(days=1)
    for tab, substatuses in COHORTS.items():
        hist = histories(cohort_uids(substatuses, _subscriber_criteria))
        print(f"{tab}  ({len(hist)} subscribers)")
        rows = []
        for day in days:
            counts = [c[day] for c in hist.values() if c[day]]
            avg_pv = round(sum(counts) / len(counts), 2) if counts else 0
            print(f"  {day}  active={len(counts):>5}  avg_pageviews={avg_pv}")
            rows.append([day, len(counts), avg_pv])
        if write:
            post_rows(tab, rows)


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--backfill":
        backfill(sys.argv[2], sys.argv[3], write="--write" in sys.argv[4:])
    else:
        main()
