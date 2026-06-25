#!/usr/bin/env python3
"""
Real-Time screen refresh — pulls LIVE Piano Analytics data and writes
public/live.json (which public/live.html polls).

Honest about what's real: Piano Analytics has NO true "concurrent readers"
metric, so this uses real, defensible numbers instead:
  • visits today so far / page views today / last completed hour
  • visits by hour (today vs yesterday)
  • top stories today by visits
  • sources / devices / countries (today)

Run:  python3 scripts/refresh_live.py        (schedule every ~5-15 min for "live")
Creds: read from ../.dev.vars (PA_ACCESS_KEY / PA_SECRET_KEY / PA_SITE_ID).
"""
import json, re, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path
from html import unescape

ROOT = Path(__file__).resolve().parent.parent
PA_URL = "https://api.atinternet.io/v3/data/getData"
EXCLUDE = {"N/A", "Home Page", "Subscribe", "Search", "search"}
SUB_METRIC = "m_vx_subscriptions"
REG_METRIC = "m_experience_regist_converted_visitors"

def creds():
    env = {}
    f = ROOT / ".dev.vars"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1); env[k.strip()] = v.strip().strip('"').strip("'")
    ak, sk = env.get("PA_ACCESS_KEY"), env.get("PA_SECRET_KEY")
    site = (env.get("PA_SITE_ID") or "").replace(" ", "").split(",")[0]
    if not (ak and sk and site):
        raise SystemExit("Missing PA creds in .dev.vars")
    return ak, sk, int(site)

AK, SK, SITE = creds()
HDR = {"x-api-key": f"{AK}_{SK}", "Content-Type": "application/json"}

def q(columns, d1, d2, sort, maxr=20):
    body = {"space": {"s": [SITE]}, "period": {"p1": [{"type": "D", "start": d1, "end": d2}]},
            "columns": columns, "sort": [sort], "max-results": maxr, "page-num": 1}
    req = urllib.request.Request(PA_URL, data=json.dumps(body).encode(), headers=HDR, method="POST")
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode()).get("DataFeed", {}).get("Rows", [])

def iso(days_ago=0):
    return (datetime.now(timezone.utc).date() - timedelta(days=days_ago)).isoformat()

def metrics_new_subs_today():
    """Actual new subscriptions today (VX start_date) — computed by the dashboard refresh."""
    try:
        return json.loads((ROOT / "public" / "metrics.json").read_text()).get("newSubsToday")
    except Exception:
        return None

def dev_var(name):
    f = ROOT / ".dev.vars"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip() == name: return v.strip().strip('"').strip("'")
    return None

def bart_subscriber_reads(op="atom-toparticles24h", limit=5):
    """Top articles by subscriber reads (titled) from bart.finance.si — same as the dashboard."""
    cookie = dev_var("BART_SESSION_COOKIE"); name = dev_var("BART_NAME") or ""
    if not cookie: return []
    body = urllib.parse.urlencode({"op": op, "group": "BPIE_ALL", "view": "group",
        "filter_by": "user_param_userstatus", "filter_val": "Subscriber", "limit": str(limit * 3)}).encode()
    req = urllib.request.Request("https://bart.finance.si/master.php", data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Origin": "https://bart.finance.si", "Referer": "https://bart.finance.si/?view=group&group=BPIE_ALL",
        "Cookie": f"bartdemo={cookie}; bartdemo2={urllib.parse.quote(name)}"})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            d = json.loads(r.read().decode())
    except Exception as e:
        print("  ! BART reads failed:", str(e)[:100]); return []
    htm = d.get("html", "") if isinstance(d, dict) else ""
    out = []
    for m in re.finditer(r'<tr data-id="([^"]*)"[^>]*>([\s\S]*?)</tr>', htm):
        row = m.group(2)
        br = re.search(r'<br\s*/?>\s*([^<]+)', row)
        sep = re.search(r'</td>\s*<td[^>]*align="left"[^>]*>([^<]+)</td>', row)
        title = unescape((br.group(1) if br else (sep.group(1) if sep else "")).strip())
        du = re.search(r'data-key="diff_users">(\d+)', row)
        if title:
            out.append({"title": title, "count": int(du.group(1)) if du else 0})
        if len(out) >= limit: break
    return out

def main():
    today, yest = iso(0), iso(1)

    tot = q(["m_visits", "m_page_loads"], today, today, "-m_visits", 1)
    visits_today = int(tot[0]["m_visits"]) if tot else 0
    pv_today = int(tot[0].get("m_page_loads", 0)) if tot else 0

    def hourly(day):
        rows = q(["event_hour", "m_visits"], day, day, "event_hour", 24)
        by = {int(r["event_hour"]): int(r.get("m_visits", 0) or 0) for r in rows if r.get("event_hour") is not None}
        return [by.get(h, 0) for h in range(24)]
    h_today, h_yest = hourly(today), hourly(yest)
    nonzero = [v for v in h_today if v > 0]
    last_hour = nonzero[-1] if nonzero else 0

    # Top stories: by visits TODAY (live).
    pages = q(["page", "m_visits"], today, today, "-m_visits", 40)
    top_stories = []
    for r in pages:
        pg = r.get("page")
        if not pg or pg in EXCLUDE: continue
        top_stories.append({"title": unescape(pg), "visits": int(r.get("m_visits", 0) or 0)})
        if len(top_stories) >= 8: break

    # TODAY's conversion totals (subscriptions + registrations) — single-day, includes today
    tt = q([SUB_METRIC, REG_METRIC], today, today, f"-{SUB_METRIC}", 1)
    regs_today = int(tt[0].get(REG_METRIC, 0)) if tt else 0       # analytics registration conversions
    # Subscriptions = ACTUAL new subs today (VX start_date), from the dashboard scan;
    # fall back to the analytics conversion count if metrics.json isn't available.
    real_subs = metrics_new_subs_today()
    subs_today = real_subs if real_subs is not None else (int(tt[0].get(SUB_METRIC, 0)) if tt else 0)

    # Top articles by subscriber reads — 24h (live, titled, from BART)
    sub_reads = bart_subscriber_reads("atom-toparticles24h", 5)

    payload = {
        "visitsToday": visits_today, "pageViewsToday": pv_today, "lastHour": last_hour,
        "hourlyToday": h_today, "hourlyYesterday": h_yest,
        "topStories": top_stories,
        "subsToday": subs_today, "regsToday": regs_today, "subReads24h": sub_reads,
        "updatedISO": datetime.now(timezone.utc).isoformat(),
    }
    out = ROOT / "public" / "live.json"
    out.write_text(json.dumps(payload, indent=2))
    print(f"✓ wrote {out}")
    print(f"  Visits today: {visits_today:,} | page views {pv_today:,} | last hour {last_hour:,}")
    print(f"  Top story today: {top_stories[0]['title'][:50]!r} ({top_stories[0]['visits']})" if top_stories else "  no stories")
    print(f"  Subs today: {subs_today} | Regs today: {regs_today} | sub-reads 24h: {len(sub_reads)} (top {sub_reads[0]['title'][:35]!r} {sub_reads[0]['count']})" if sub_reads else f"  Subs today: {subs_today} | Regs today: {regs_today} | sub-reads: none")

if __name__ == "__main__":
    main()
