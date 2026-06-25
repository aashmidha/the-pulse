#!/usr/bin/env python3
"""
Newsroom Dashboard — live refresh.

Pulls REAL subscriber counts from the Piano VX Publisher API (app-wide
subscription/list, validated to match the weekly report), folds in the two
manual weekly numbers, computes the weekly delta vs a stored baseline, and
writes public/metrics.json — exactly the shape the dashboard consumes.

Run:  python3 scripts/refresh_metrics.py
Cron: */15 * * * *  (or hook into the Cloudflare Worker for production)

Reads creds from ../../BART List/enricher_settings.json (same as your tools),
or from PIANO_AID / PIANO_API_TOKEN env vars.

Panels still mocked (clearly flagged in output): conversions (needs Piano
Analytics goal IDs) and BART top-reads (needs the BART articles endpoint).
"""
import json, os, time, csv, io, re, urllib.parse, urllib.request, urllib.error
from html import unescape
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = Path.home() / "BART List" / "enricher_settings.json"

def creds():
    aid = os.environ.get("PIANO_AID"); tok = os.environ.get("PIANO_API_TOKEN")
    base = os.environ.get("PIANO_BASE_URL", "https://api-eu.piano.io")
    if not (aid and tok) and SETTINGS.exists():
        s = json.loads(SETTINGS.read_text())
        aid = aid or s["piano_aid"]; tok = tok or s["piano_token"]; base = s.get("piano_base", base)
    if not (aid and tok):
        raise SystemExit("Set PIANO_AID and PIANO_API_TOKEN (or provide enricher_settings.json).")
    return aid, tok, base

AID, TOK, BASE = creds()
_S = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
BART_BASE = (os.environ.get("BART_BASE_URL") or _S.get("bart_base") or "https://bonnieraws.dergan.net").rstrip("/")
BART_KEY = os.environ.get("BART_KEY") or _S.get("bart_key") or ""
RFV_THRESHOLD = 19       # subscriber counts as "engaged" if RFV strictly above this
REG_RFV_THRESHOLD = 1    # registered (non-sub) counts as "engaged" if RFV strictly above this
NOW = int(time.time())
# Single shared window: rolling last 7 days, ENDING YESTERDAY (no partial today).
# Used identically by Piano VX (epoch) and Piano Analytics (iso dates) below.
_MIDNIGHT = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
WIN_END = int(_MIDNIGHT.timestamp())                       # 00:00 today = end of yesterday
WIN_START = int((_MIDNIGHT - timedelta(days=7)).timestamp())  # 00:00 seven days ago

def get(path, params, retries=6):
    p = dict(params, aid=AID, api_token=TOK)
    url = f"{BASE}{path}?{urllib.parse.urlencode(p)}"
    for a in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=90) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and a < retries - 1:    # rate-limited → back off
                time.sleep(min(90, 8 * (2 ** a))); continue
            raise
        except Exception:
            if a < retries - 1:
                time.sleep(5); continue
            raise

def term_name(s):
    t = s.get("term")
    return ((t.get("name") if isinstance(t, dict) else s.get("term_name")) or "").strip().lower()
def term_type(s):
    t = s.get("term")
    return ((t.get("type") if isinstance(t, dict) else s.get("term_type")) or "").strip().lower()
def status(s): return (s.get("status_name_in_reports") or s.get("status") or "").strip().lower()
def is_reg(s): return term_type(s) == "registration" or "new account creation" in term_name(s)
def is_active(s):
    """Counted as a live subscriber: status 'active' OR 'won't renew' (cancelled
    auto-renew but still active until term end). 'renew' substring is robust to the
    apostrophe character Piano uses."""
    st = status(s)
    return st == "active" or "renew" in st

def aggregate_subscribers():
    """Page all subscriptions. Returns active B2C/B2B counts, gross new subs in the
    7d window (by start_date), and the SET of active subscription IDs (for the
    daily-diff cancellation calc)."""
    offset, LIMIT, total = 0, 1000, None
    active_b2c = active_b2b = new_subs_7d = new_subs_today = 0
    cancelled_uids = set()     # distinct users whose subscription end_date is in the 7d window
    b2c_uids = set(); b2b_uids = set()   # current-subscriber uids, split for engagement
    while True:
        d = get("/api/v3/publisher/subscription/list", {"offset": offset, "limit": LIMIT})
        subs = d.get("subscriptions", []); total = d.get("total", d.get("count", total))
        if not subs: break
        for s in subs:
            if is_reg(s): continue
            try: sd = int(s.get("start_date", 0) or 0)
            except (TypeError, ValueError): sd = 0
            try: ed = int(s.get("end_date", 0) or 0)
            except (TypeError, ValueError): ed = 0
            if WIN_START <= sd < WIN_END: new_subs_7d += 1        # new paid sub in 7d window
            if sd >= WIN_END: new_subs_today += 1                 # started today (>= midnight)
            if WIN_START <= ed < WIN_END:                         # subscription ENDED in 7d window
                cancelled_uids.add((s.get("user") or {}).get("uid") or s.get("subscription_id"))
            # current subscriber: active/won't-renew AND paid term hasn't ended yet
            if is_active(s) and (ed == 0 or ed >= WIN_END):
                uid = (s.get("user") or {}).get("uid")
                if term_name(s).startswith("corp"):
                    active_b2b += 1
                    if uid: b2b_uids.add(uid)
                else:
                    active_b2c += 1
                    if uid: b2c_uids.add(uid)
        offset += LIMIT
        if total and offset >= total: break
        time.sleep(0.05)
    return {"active_b2c": active_b2c, "active_b2b": active_b2b, "new_subs_7d": new_subs_7d,
            "new_subs_today": new_subs_today, "cancellations_7d": len(cancelled_uids),
            "b2c_uids": b2c_uids, "b2b_uids": b2b_uids}

def aggregate_registrations():
    """Count NEW registered users in the last 7d and the previous 7d (by create_date).
    Piano has no server-side create_date sort/filter, so we page the full user list
    once (~188 calls) and bucket. Reads only create_date — no PII retained."""
    week1, week2 = NOW - 7 * 86400, NOW - 14 * 86400
    offset, LIMIT, total = 0, 1000, None
    last7 = prev7 = 0
    while True:
        d = get("/api/v3/publisher/user/list", {"offset": offset, "limit": LIMIT})
        users = d.get("users") or d.get("data") or []
        total = d.get("total", d.get("count", total))
        if not users:
            break
        for u in users:
            try:
                cd = int(u.get("create_date", 0))
            except (TypeError, ValueError):
                continue
            if cd >= week1:
                last7 += 1
            elif cd >= week2:
                prev7 += 1
        offset += LIMIT
        if total and offset >= total:
            break
        time.sleep(0.03)
    return last7, prev7

# --------------------------------------------------------------------------- #
# Piano Analytics — conversions (Subscriptions + Registration conversions)     #
# --------------------------------------------------------------------------- #
PA_URL = "https://api.atinternet.io/v3/data/getData"
SUB_METRIC = "m_vx_subscriptions"                        # "Subscriptions" (the correct VX metric)
REG_METRIC = "m_experience_regist_converted_visitors"    # "Experience registration converted visitors"
VISITS_METRIC = "m_visits"                               # daily "Visits" for the sparkline
# Utility pages to always exclude from the article lists (real articles have full
# headlines; section fronts don't pollute the correct subscriptions metric).
EXCLUDE_PAGES = ["Home Page", "Subscribe", "Search", "search", "N/A"]
DEVVARS = ROOT / ".dev.vars"

def pa_creds():
    env = {}
    if DEVVARS.exists():
        for line in DEVVARS.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1); env[k.strip()] = v.strip().strip('"').strip("'")
    ak = env.get("PA_ACCESS_KEY") or os.environ.get("PA_ACCESS_KEY")
    sk = env.get("PA_SECRET_KEY") or os.environ.get("PA_SECRET_KEY")
    site = (env.get("PA_SITE_ID") or os.environ.get("PA_SITE_ID", "")).replace(" ", "").split(",")[0]
    return ak, sk, (int(site) if site else None)

def iso(days_ago): return (datetime.now(timezone.utc).date() - timedelta(days=days_ago)).isoformat()

def pa_query(ak, sk, site, columns, d1, d2, sort, maxr=5, flt=None):
    payload = {"space": {"s": [site]}, "period": {"p1": [{"type": "D", "start": d1, "end": d2}]},
               "columns": columns, "sort": [sort], "max-results": maxr, "page-num": 1}
    if flt: payload["filter"] = flt
    req = urllib.request.Request(PA_URL, data=json.dumps(payload).encode(),
          headers={"x-api-key": f"{ak}_{sk}", "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode()).get("DataFeed", {}).get("Rows", [])

def aggregate_conversions():
    """Returns hero registration totals (7d + prev 7d) and top-5 articles for each
    conversion type, or None if Piano Analytics creds are missing/unavailable."""
    ak, sk, site = pa_creds()
    if not (ak and sk and site):
        return None
    d1, d2 = iso(7), iso(1)            # last 7 full days
    pd1, pd2 = iso(14), iso(8)         # the 7 days before that
    flt = {"property": {"page": {"$nin": EXCLUDE_PAGES}}}
    def top(rows, metric):
        out = []
        for r in rows:
            pg = r.get("page")
            if not pg or pg in EXCLUDE_PAGES: continue       # drop utility/section pages
            out.append({"headline": pg, "count": int(r.get(metric, 0) or 0)})
            if len(out) >= 5: break
        return out
    try:
        tot  = pa_query(ak, sk, site, [SUB_METRIC, REG_METRIC], d1, d2, f"-{SUB_METRIC}", 1)
        ptot = pa_query(ak, sk, site, [SUB_METRIC, REG_METRIC], pd1, pd2, f"-{REG_METRIC}", 1)
        sub_rows = pa_query(ak, sk, site, ["page", SUB_METRIC, REG_METRIC], d1, d2, f"-{SUB_METRIC}", 15, flt)
        reg_rows = pa_query(ak, sk, site, ["page", SUB_METRIC, REG_METRIC], d1, d2, f"-{REG_METRIC}", 15, flt)
        vis  = pa_query(ak, sk, site, ["date", VISITS_METRIC], d1, d2, "date", 10)
        visp = pa_query(ak, sk, site, ["date", VISITS_METRIC], pd1, pd2, "date", 10)
        visits_daily = [{"date": r.get("date"), "visits": int(r.get(VISITS_METRIC, 0) or 0)}
                        for r in vis if r.get("date")]
        visits_prev  = [{"date": r.get("date"), "visits": int(r.get(VISITS_METRIC, 0) or 0)}
                        for r in visp if r.get("date")]
        return {
            "visitsDailyPrev": visits_prev,
            "subs_7d":  int(tot[0].get(SUB_METRIC, 0)) if tot else 0,
            "regs_7d":  int(tot[0].get(REG_METRIC, 0)) if tot else 0,
            "regs_prev": int(ptot[0].get(REG_METRIC, 0)) if ptot else 0,
            "subscribers":   top(sub_rows, SUB_METRIC),
            "registrations": top(reg_rows, REG_METRIC),
            "visitsDaily":   visits_daily,
        }
    except Exception as e:
        print("  ! Piano Analytics pull failed:", str(e)[:140]); return None

BART_READS_URL = "https://bart.finance.si/master.php"
BART_OPS = {"d7": "atom-toparticles-7", "h24": "atom-toparticles24h"}

def fetch_bart_reads(limit=5):
    """Top articles by SUBSCRIBER reads (titled), 7d + 24h, from bart.finance.si.
    Uses the session cookie (BART_SESSION_COOKIE / BART_NAME in .dev.vars) and parses
    the HTML table — same approach as the COMET app."""
    cookie = dev_var("BART_SESSION_COOKIE"); name = dev_var("BART_NAME") or ""
    if not cookie: return None
    def one(op):
        body = urllib.parse.urlencode({"op": op, "group": "BPIE_ALL", "view": "group",
            "filter_by": "user_param_userstatus", "filter_val": "Subscriber", "limit": str(limit * 3)}).encode()
        req = urllib.request.Request(BART_READS_URL, data=body, method="POST", headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Origin": "https://bart.finance.si", "Referer": "https://bart.finance.si/?view=group&group=BPIE_ALL",
            "Cookie": f"bartdemo={cookie}; bartdemo2={urllib.parse.quote(name)}"})
        with urllib.request.urlopen(req, timeout=45) as r:
            d = json.loads(r.read().decode())
        htm = d.get("html", "") if isinstance(d, dict) else ""
        out = []
        for m in re.finditer(r'<tr data-id="([^"]*)"[^>]*>([\s\S]*?)</tr>', htm):
            row = m.group(2)
            br = re.search(r'<br\s*/?>\s*([^<]+)', row)
            sep = re.search(r'</td>\s*<td[^>]*align="left"[^>]*>([^<]+)</td>', row)
            title = unescape((br.group(1) if br else (sep.group(1) if sep else "")).strip())
            du = re.search(r'data-key="diff_users">(\d+)', row)
            if title:
                out.append({"headline": title, "reads": int(du.group(1)) if du else 0})
            if len(out) >= limit: break
        return out
    try:
        return {"d7": one(BART_OPS["d7"]), "h24": one(BART_OPS["h24"])}
    except Exception as e:
        print("  ! BART reads fetch failed:", str(e)[:120]); return None

def _rfv_files():
    """All BART RFV exports in ~/Downloads, newest date first: [(date, path), ...]."""
    dl = Path.home() / "Downloads"
    if not dl.exists(): return []
    out = []
    for p in dl.glob("*rfv*all*.csv"):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", p.name)
        if m: out.append((m.group(1), p))
    return sorted(out, reverse=True)

def _read_rfv_csv(path):
    rfv = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            rd = csv.reader(fh); header = next(rd)
            uid_i = next((i for i, h in enumerate(header) if h.strip().lower() == "userid"), None)
            rfv_i = next((i for i, h in enumerate(header) if "rfv" in h.strip().lower()), None)
            if uid_i is None or rfv_i is None: return {}
            for row in rd:
                if len(row) > max(uid_i, rfv_i):
                    try: rfv[row[uid_i]] = float(row[rfv_i])
                    except ValueError: pass
    except Exception as e:
        print("  ! RFV file read failed:", str(e)[:100])
    return rfv

def load_prior_rfv():
    """Newest RFV export at least ~a week old — baseline for the engagement delta."""
    target = (datetime.now(timezone.utc).date() - timedelta(days=6)).isoformat()
    for dt, p in _rfv_files():
        if dt <= target:
            rfv = _read_rfv_csv(p)
            if rfv: return rfv, dt
    return None, None

def load_latest_rfv():
    """Newest RFV export with REAL data — used when the live RFV feed is empty.
    Skips degenerate exports (e.g. all-zero scores like the 2026-06-24 file)."""
    for dt, p in _rfv_files():
        rfv = _read_rfv_csv(p)
        if rfv and max(rfv.values(), default=0) > 0:
            return rfv, dt
    return None, None

def fetch_engagement(b2c_uids, b2b_uids):
    """North-star metric: % of current subscribers with a BART RFV score strictly
    above RFV_THRESHOLD. Pulls BART /rfv (daily-computed), joins on uid, and reports
    overall + a B2C(+B2Cd) vs B2B split. Denominator = ALL subscribers in each group
    (no-score subs count as below threshold). B2Cd has no isolable uids → folds into B2C."""
    all_uids = b2c_uids | b2b_uids
    if not (BART_KEY and all_uids):
        return None
    try:
        rfv = {}; page = 1; created = None
        while True:
            url = f"{BART_BASE}/rfv?" + urllib.parse.urlencode({"key": BART_KEY, "page": page})
            for a in range(4):
                try:
                    with urllib.request.urlopen(url, timeout=60) as r:
                        d = json.loads(r.read().decode()); break
                except Exception:
                    if a < 3: time.sleep(4); continue
                    raise
            created = d.get("created")
            for h in d.get("hits", []):
                u, v = h.get("uid"), h.get("rfv")
                if u is not None and isinstance(v, (int, float)): rfv[u] = v
            if not d.get("next_page"): break
            page += 1
        source = "live"
        if not rfv:                                  # live RFV feed empty → newest real RFV export
            frfv, fdate = load_latest_rfv()
            if frfv:
                rfv = frfv; source = f"file:{fdate}"
                print(f"  live RFV feed empty — using RFV export {fdate}")
        if not rfv:
            print("  ! no RFV data anywhere — engagement unavailable")
            return None
        def grp(uids):
            n = len(uids)
            above = sum(1 for u in uids if u in rfv and rfv[u] > RFV_THRESHOLD)
            scored = sum(1 for u in uids if u in rfv)
            return {"pctAbove": round(above / n * 100, 1) if n else 0.0, "above": above, "scored": scored, "subs": n}
        # Baseline from last week's RFV file (same subscriber set, prior scores)
        prior = None
        prfv, pdate = load_prior_rfv()
        if prfv:
            n = len(all_uids)
            pabove = sum(1 for u in all_uids if u in prfv and prfv[u] > RFV_THRESHOLD)
            prior = {"pctAbove": round(pabove / n * 100, 1) if n else 0.0, "date": pdate}
        total_above = sum(1 for v in rfv.values() if v > RFV_THRESHOLD)   # ALL engaged users
        # registered (non-subscriber) readers engaged above the registered threshold
        reg_engaged = sum(1 for u, v in rfv.items() if v > REG_RFV_THRESHOLD and u not in all_uids)
        return {"overall": grp(all_uids), "b2c": grp(b2c_uids), "b2b": grp(b2b_uids),
                "threshold": RFV_THRESHOLD, "prior": prior, "totalAbove": total_above,
                "rfvUsers": len(rfv), "regEngaged": reg_engaged, "source": source,
                "computedISO": (datetime.fromtimestamp(created, timezone.utc).isoformat() if created else None)}
    except Exception as e:
        print("  ! BART RFV pull failed:", str(e)[:120]); return None

def load_json(p, default):
    p = ROOT / "data" / p
    return json.loads(p.read_text()) if p.exists() else default
def save_json(p, obj):
    (ROOT / "data" / p).write_text(json.dumps(obj, indent=2))

def dev_var(name):
    if DEVVARS.exists():
        for line in DEVVARS.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip() == name:
                    return v.strip().strip('"').strip("'")
    return os.environ.get(name)

def _sheet_csv_url(url):
    """Accepts a Publish-to-web CSV link (recommended — only the published tab is
    exposed, rest of the sheet stays private) and uses it as-is. Also tolerates a
    plain edit URL (would need link-view sharing)."""
    url = url.strip()
    if "output=csv" in url or "format=csv" in url:
        return url                       # already a published/export CSV link
    m = re.search(r"/spreadsheets/d/(?:e/)?([A-Za-z0-9-_]+)", url)
    if not m:
        return url
    sid = m.group(1)
    g = re.search(r"[#&?]gid=([0-9]+)", url)
    gid = g.group(1) if g else "0"
    return f"https://docs.google.com/spreadsheets/d/{sid}/gviz/tq?tqx=out:csv&gid={gid}"

def read_manual():
    """Telesales/Direct (Maddie) + Corporate (Shane) weekly figures.
    Reads them live from a Google Sheet (MANUAL_SHEET_URL in .dev.vars); a 2-column
    key/value sheet, e.g.:  corporate,2258  /  telesalesDirect,46
    Falls back to the last good value cached in data/manual.json, then to defaults."""
    default = {"corporate": 2258, "telesalesDirect": 46}
    url = dev_var("MANUAL_SHEET_URL")
    if url:
        try:
            with urllib.request.urlopen(_sheet_csv_url(url), timeout=30) as r:
                text = r.read().decode("utf-8", "replace")
            corp = None; tele = 0; tele_seen = False
            for row in csv.reader(io.StringIO(text)):
                if len(row) < 2:
                    continue
                key = row[0].strip().lower()
                digits = "".join(ch for ch in row[1] if ch.isdigit())
                if not digits:
                    continue
                n = int(digits)
                if "b2cd" in key or "telesales" in key or "direct" in key:
                    tele += n; tele_seen = True           # SUM B2Cd + Telesales/Direct rows
                elif "corp" in key or "b2b" in key:
                    corp = n                              # B2B
            if corp is not None and tele_seen:
                vals = {"corporate": corp, "telesalesDirect": tele}
                save_json("manual.json", vals)            # cache last-good
                print(f"  manual from sheet: corp {corp} · B2Cd+Telesales {tele}")
                return vals
            print("  ! sheet read but couldn't find both rows — using fallback")
        except Exception as e:
            print("  ! manual sheet read failed:", str(e)[:120])
    return load_json("manual.json", default)

def main():
    subs = aggregate_subscribers()
    active_b2c, active_b2b = subs["active_b2c"], subs["active_b2b"]
    new_subs_7d, cancellations_7d = subs["new_subs_7d"], subs["cancellations_7d"]
    new_subs_today = subs["new_subs_today"]
    eng = fetch_engagement(subs["b2c_uids"], subs["b2b_uids"])
    reads = fetch_bart_reads()

    # Registrations + conversions come from Piano Analytics (editorial's "registration
    # converted visitors" number). If analytics is unavailable, fall back to the slower
    # Piano VX new-account count and leave the conversion panels pending.
    conv = aggregate_conversions()
    if conv:
        reg_last7, reg_prev7 = conv["regs_7d"], conv["regs_prev"]
        conversions = {"status": "live", "subscribers": conv["subscribers"], "registrations": conv["registrations"]}
        visits_daily = conv.get("visitsDaily", [])
        visits_prev = conv.get("visitsDailyPrev", [])
        live = {"subscribers": True, "registrations": True, "conversions": True,
                "visits": bool(visits_daily), "reads": False}
    else:
        reg_last7, reg_prev7 = aggregate_registrations()
        conversions = {"status": "pending", "subscribers": [], "registrations": []}
        visits_daily = []
        visits_prev = []
        live = {"subscribers": True, "registrations": True, "conversions": False,
                "visits": False, "reads": False}

    # Manual weekly numbers (Shane = corporate seats, Maddie = telesales/direct).
    manual = read_manual()
    piano_b2c = active_b2c                     # the live "10,644" line
    telesales = manual["telesalesDirect"]
    corporate = manual["corporate"]
    total_b2c = piano_b2c + telesales
    total_paid = total_b2c + corporate

    # Rolling 7-day delta: today's total minus the total recorded ~7 days ago.
    # We keep a daily snapshot log; the delta is null until ~7 days of history exist.
    today = datetime.now(timezone.utc)
    today_str = today.date().isoformat()
    target_str = (today.date() - timedelta(days=7)).isoformat()
    history = load_json("history.json", {})
    history[today_str] = total_paid
    past = None
    for dstr in sorted(history):                      # most recent snapshot on/before 7 days ago
        if dstr <= target_str:
            past = history[dstr]
    delta_week = (total_paid - past) if past is not None else None
    cutoff = (today.date() - timedelta(days=45)).isoformat()
    save_json("history.json", {k: v for k, v in history.items() if k >= cutoff})

    # Engagement (NORTH STAR): % of subscribers with RFV > threshold, + rolling 7-day delta.
    eng_pct = eng["overall"]["pctAbove"] if eng else None
    eng_hist = load_json("eng_history.json", {})
    if eng_pct is not None: eng_hist[today_str] = eng_pct
    save_json("eng_history.json", {k: v for k, v in eng_hist.items() if k >= cutoff})
    # Baseline: last week's RFV file (preferred); else daily history.
    baseline = baseline_date = None
    prior = eng.get("prior") if eng else None
    if prior:
        baseline, baseline_date = prior["pctAbove"], prior["date"]
    else:
        for dstr in sorted(eng_hist):
            if dstr <= target_str: baseline, baseline_date = eng_hist[dstr], dstr
    eng_delta = round(eng_pct - baseline, 1) if (eng_pct is not None and baseline is not None) else None
    engagement = {"pctAbove": eng_pct, "threshold": RFV_THRESHOLD,
                  "deltaWeek": eng_delta, "deltaSince": baseline_date,
                  "source": (eng.get("source") if eng else None),
                  "above": (eng["overall"]["above"] if eng else None),
                  "subs": (eng["overall"]["subs"] if eng else None),
                  "b2c": (eng["b2c"] if eng else None), "b2b": (eng["b2b"] if eng else None)}
    live["engagement"] = bool(eng)
    live["reads"] = bool(reads)
    # Engaged registered users = non-subscriber readers with RFV > REG_RFV_THRESHOLD.
    reg_engaged = eng["regEngaged"] if eng else None

    payload = {
        "weekLabel": today.strftime("Week %V · %b %-d"),
        "live": live,
        "subscribers": {
            "piano":    {"value": piano_b2c},
            "b2cd":     {"value": telesales},
            "totalB2C": {"value": total_b2c},
            "totalB2B": {"value": corporate},
            "totalPaid":{"value": total_paid, "deltaWeek": delta_week},
        },
        "movement": {"newSubs7d": new_subs_7d, "cancellations7d": cancellations_7d},
        "newSubsToday": new_subs_today,
        "engagement": engagement,
        "registeredUsers": {"last7": reg_last7, "prev7": reg_prev7, "engaged": reg_engaged},
        "conversions": conversions,
        "visitsDaily": visits_daily,
        "visitsDailyPrev": visits_prev,
        "topReads7d": (reads["d7"] if reads else []),
        "topReads24h": (reads["h24"] if reads else []),
        "updatedISO": today.isoformat(),
    }
    out = ROOT / "public" / "metrics.json"
    out.write_text(json.dumps(payload, indent=2))
    print(f"✓ wrote {out}")
    print(f"  Total Paid: {total_paid:,}  (B2C live {piano_b2c:,} + telesales {telesales} + corp {corporate})")
    print(f"  Rolling 7d Δ: {('%+d'%delta_week) if delta_week is not None else 'building'}   |   new subs 7d: {new_subs_7d}   |   cancellations 7d (end_date): {cancellations_7d}")
    print(f"  Registrations (analytics): last7 {reg_last7:,}  prev7 {reg_prev7:,}  (Δ {reg_last7-reg_prev7:+,})")
    if eng: print(f"  ENGAGEMENT: {eng_pct}% of {eng['overall']['subs']:,} subs RFV>{RFV_THRESHOLD} | B2C {eng['b2c']['pctAbove']}% · B2B {eng['b2b']['pctAbove']}% | engaged registered RFV>{REG_RFV_THRESHOLD} (non-sub): {reg_engaged:,}")
    else:   print("  ENGAGEMENT: BART RFV unavailable")
    if reads and reads["d7"]: print(f"  BART READS: 7d top {reads['d7'][0]['headline'][:45]!r} ({reads['d7'][0]['reads']}) · {len(reads['d7'])}×7d {len(reads['h24'])}×24h")
    else:   print("  BART READS: unavailable (check BART_SESSION_COOKIE)")
    if conv:
        print(f"  Conversions LIVE — top sub article: {conv['subscribers'][0]['headline'][:45] if conv['subscribers'] else 'n/a'!r}")
        print(f"                     top reg article: {conv['registrations'][0]['headline'][:45] if conv['registrations'] else 'n/a'!r}")
        print(f"  Visits/day ({len(visits_daily)} days): {[v['visits'] for v in visits_daily]}")
    print(f"  live flags: {live}")

if __name__ == "__main__":
    main()
