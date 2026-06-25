# The Pulse — Handover & Cloudflare Deployment Brief

**Goal of next session:** deploy this to Cloudflare so the two screens stay current automatically (no laptop, no manual refresh).

Everything below is built and working **locally**. The remaining job is **deployment + scheduling**.

---

## 1. What this is

Two TV/wall screens for the Business Post newsroom, served as static HTML that polls JSON:

| Screen | File | URL (local) | Theme |
|---|---|---|---|
| **The Pulse** (dashboard) | `public/index.html` | `http://localhost:8765/index.html` | light |
| **Real-Time** (live) | `public/live.html` | `http://localhost:8765/live.html` | dark |

Project root: `~/Desktop/newsroom-dashboard/`

```
public/
  index.html      # The Pulse dashboard (polls metrics.json every 5s)
  live.html       # Real-time screen (polls live.json every 60s)
  metrics.json    # written by scripts/refresh_metrics.py
  live.json       # written by scripts/refresh_live.py
scripts/
  refresh_metrics.py   # THE dashboard pipeline (source of truth for logic)
  refresh_live.py      # the live-screen pipeline
data/
  manual.json     # cached last-good sheet values (corporate, telesalesDirect)
  history.json    # daily totalPaid snapshots (for rolling-7d delta)
  eng_history.json
worker/index.js   # OUTDATED Cloudflare Worker scaffold — DO NOT trust; logic lives in the .py
wrangler.toml     # Cloudflare config scaffold (assets + KV + cron)
.dev.vars         # SECRETS (git-ignored) — see §4
.dev.vars.example
```

> ⚠️ `worker/index.js` was written early before most logic existed. **The Python scripts are the source of truth.** Deployment = port their logic, or run the Python on a schedule and publish the JSON.

---

## 2. How it runs locally (today)

```bash
# serve (leave running)
cd ~/Desktop/newsroom-dashboard && nohup python3 -m http.server 8765 --directory public >/tmp/dash.log 2>&1 &

# refresh data — dashboard FIRST (writes newSubsToday), then live
cd ~/Desktop/newsroom-dashboard && python3 scripts/refresh_metrics.py && python3 scripts/refresh_live.py
```
- `refresh_metrics.py` takes ~1–2 min (paginates ~41k Piano subscriptions). Has 429 back-off.
- `refresh_live.py` is fast. It **reads `newSubsToday` from `metrics.json`**, so order matters.
- Pages auto-poll; the *data* only changes when a refresh script runs (no cron yet).

---

## 3. Data sources (all live, direct HTTP — NO MCP dependency)

| Source | Endpoint / auth | Powers |
|---|---|---|
| **Piano VX Publisher** | `https://api-eu.piano.io` `/api/v3/publisher/subscription/list` (paginated, app-wide). Auth: `aid` + `api_token` (query params). | Subscriber counts, new subs, cancellations, B2C/B2B uids, newSubsToday |
| **Piano Analytics** | `https://api.atinternet.io/v3/data/getData` (POST). Auth header `x-api-key: ACCESS_SECRET`. | Visits, conversions, registrations |
| **BART — RFV** | `https://bonnieraws.dergan.net/rfv?key=bpiealldemo` (GET) | Engagement (RFV scores per uid) |
| **BART — article reads** | `https://bart.finance.si/master.php` (POST, **session cookie**) | "Most Read by Subscribers" 7d/24h |
| **Google Sheet** | published-CSV URL (the "Live feed" tab) | B2B (corporate) + B2Cd/Telesales manual weeklies |

### Piano Analytics quirks (important)
- Property names are **bare** (`m_visits`, `page`, `m_vx_subscriptions`) — **NOT** the legacy `m:visits` form.
- Payload uses a top-level `period: {p1:[{type:"D",start,end}]}` block (not `d1/d2` inside `space`), and **a `sort` is required**.
- **A date range that includes *today* cannot exceed 48h** (`InvalidPeriod_MixDataRealtimeHistorical`). So "rolling 7 days" = `today-7 … today-1` (ends yesterday).
- Site ID `625851`. Key metrics: `m_vx_subscriptions` ("Subscriptions"), `m_experience_regist_converted_visitors` ("Registrations"), `m_visits`, dimension `page`.

### BART article reads (the method that works)
POST to `bart.finance.si/master.php`, form body:
`op=atom-toparticles-7` (7d) / `atom-toparticles24h` (24h) / `atom-toparticles-30` (30d), `group=BPIE_ALL`, `view=group`, `filter_by=user_param_userstatus`, `filter_val=Subscriber`, `limit=…`. Header `Cookie: bartdemo=<BART_SESSION_COOKIE>; bartdemo2=<BART_NAME>`. Response is `{html:"…"}`; parse the table for title / `diff_users` (subscriber reads) / `tot_views`. (This is COMET's approach — see `~/comet-main/functions/lib/bart.ts`.)

---

## 4. Secrets — `.dev.vars` (git-ignored)

Referenced by name only. Already filled in locally:
```
PA_ACCESS_KEY=…          PA_SECRET_KEY=…          PA_SITE_ID=625851
MANUAL_SHEET_URL=…       (published-CSV link to the Live feed tab)
BART_SESSION_COOKIE=…    BART_NAME=Midha          (bart.finance.si session)
```
Piano VX `aid`/`token` + BART RFV `bart_base`/`bart_key` are currently read from **`~/BART List/enricher_settings.json`** (keys: `piano_aid`, `piano_token`, `piano_base`, `bart_base`, `bart_key`). For Cloudflare, move these into secrets too (`PIANO_AID`, `PIANO_API_TOKEN`, etc. — the scripts already check env vars first).

**Expiring creds:** `BART_SESSION_COOKIE` is a logged-in session cookie — it **expires** (reads panels go blank when it does). Refresh it from the browser: DevTools → Network → `master.php` → Request Headers → `Cookie: bartdemo=…`.

---

## 5. Metric definitions (locked decisions — keep these)

- **B2C** = subscriptions that are `active` OR `won't renew`, **non-corp**, **paid term not ended** (`end_date` absent or ≥ today). Live from Piano VX. ~10,700. (Counting only `active` undercounts by ~the "won't renew" set; the end-date guard drops already-lapsed ones.)
- **B2Cd** = **sum** of the sheet's `B2Cd` + `Telesales` rows (currently 90).
- **B2B** = `corporate` from the sheet (2,279). NOTE: Piano's own corp count (~2,081 subscriptions) differs — site-licence *seats* ≠ subscriptions, so **the sheet is the source of truth** for B2B.
- **Total Paid** = B2C + B2Cd + B2B.
- **New subs (7d)** = `start_date` in `[today-7, today-1]`. **Cancellations (7d)** = distinct users with `end_date` in that window. **newSubsToday** = `start_date` ≥ midnight today.
- **Registrations** = Piano Analytics `m_experience_regist_converted_visitors` (analytics *conversions*, not VX account headcount — the user chose this), rolling 7d ending yesterday.
- **Engagement (NORTH STAR)** = % of **all current subscribers** with **RFV > 19**. Split: B2C+B2Cd (non-corp uids) vs B2B (corp uids). **Engaged registered** = non-subscriber readers with **RFV > 1**. Thresholds are constants `RFV_THRESHOLD=19`, `REG_RFV_THRESHOLD=1`. **RFV source:** live BART `/rfv` feed if available; if empty, falls back to the **newest real RFV export** in `~/Downloads` (`load_latest_rfv`, skips all-zero exports like 2026-06-24) — shown with a "· RFV file <date>" note. Only truly unavailable if no valid export exists.
- **Conversion panels** = `m_vx_subscriptions` / `m_experience_regist_converted_visitors` by `page`, 7d, excluding `Home Page / Subscribe / N/A / Search`.
- **Live screen "Subscriptions today"** = VX `newSubsToday` (real headcount, from metrics.json). **"Registrations today"** = analytics conversions today.

---

## 6. Current live status / known issues

- **Everything is live.** The `bonnieraws.dergan.net/rfv` live feed is currently **empty** (`all_hit_count: 0`) and today's export (2026-06-24) is all-zeros, so **engagement is computed from the newest real export, `BPIE_ALL_rfv-all-2026-06-03.csv`**, and labelled "· RFV file 2026-06-03". It auto-returns to the live feed when BART's RFV repopulates. To freshen: drop a newer valid RFV export into `~/Downloads`.
- BART reads (bart.finance.si) work but depend on the session cookie (expires).
- Live screen's 3rd right-column panel is a **"Reserved"** placeholder (intentionally undecided).
- Two BART systems exist — don't confuse them: `bonnieraws.dergan.net` (RFV, demo key) vs `bart.finance.si` (article reads, session cookie). The earlier `bonnieraws /top/articles` endpoint only returns IDs (no titles) — that's why reads use `bart.finance.si`.

---

## 7. THE DEPLOYMENT TASK (Cloudflare)

Static screens are trivial on Cloudflare Pages. The real work is **producing `metrics.json` + `live.json` on a schedule**. Cloudflare can't run Python natively, so pick one:

**Option A — Cloudflare-native (cleanest, more work):** Port `refresh_metrics.py` + `refresh_live.py` to a **Cloudflare Worker** (JS/TS) with **cron triggers** (metrics ~15 min, live ~5 min). Worker computes and writes results to **KV** (or R2); the Pages site (or the same Worker) serves `/api/metrics` + `/api/live`; the HTML fetches those instead of the JSON files. Reference architecture: `~/comet-main` (the COMET app — Cloudflare Pages + Functions + `workers/sync-cron` + D1/KV; `wrangler.jsonc`). The BART-reads logic is already in JS there (`functions/lib/bart.ts`).
- Watch Worker **subrequest limits**: the subscriber scan is ~40 paginated calls + ~5 BART + a few analytics ≈ ~50 subrequests — fine on the **paid** plan (1000), tight on free (50). The 187k-user registrations scan is **avoided** (we use analytics).
- Store the **daily snapshot** (`history.json` equivalent) and last-good values in KV for the rolling-7d delta and resilience.

**Option B — keep Python, schedule externally (fastest):** Run the two Python scripts on a schedule via **GitHub Actions cron** (or a small always-on box), and have them **push `metrics.json` + `live.json`** to Cloudflare (Pages deploy, or upload to **R2/KV** that a tiny Worker serves). Zero logic rewrite. Secrets go in the Actions/host environment.

Either way:
- Set secrets via `wrangler secret put` (PA_ACCESS_KEY, PA_SECRET_KEY, PA_SITE_ID, PIANO_AID, PIANO_API_TOKEN, BART_SESSION_COOKIE, BART_NAME, MANUAL_SHEET_URL).
- The existing `wrangler.toml` has an `[assets]` + KV + `crons` scaffold to build on.
- Keep the **dashboard-before-live** ordering (live reads metrics' `newSubsToday`).

**Recommendation:** Start with **Option B** to get it live fast (Python is the tested source of truth), then optionally migrate to Option A (Worker cron) for a fully serverless setup. The COMET repo proves the Worker path works for this exact data.

---

## 8. Quick-reference commands
```bash
# serve
cd ~/Desktop/newsroom-dashboard && python3 -m http.server 8765 --directory public

# refresh both (dashboard first)
cd ~/Desktop/newsroom-dashboard && python3 scripts/refresh_metrics.py && python3 scripts/refresh_live.py

# list Piano subscription term names (helper)
cd ~/"BART List" && python3 - <<'PY'  # see chat history for the snippet
PY
```

*Built with: Piano VX, Piano Analytics, BART (RFV + article reads), a published Google Sheet. No MCP in the deliverable.*
