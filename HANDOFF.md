# The Newsroom Live — project handoff

Read this to pick up the project in a fresh Claude Code session. It's the current
state as of **2026-10-06**. Runbook detail lives in `DEPLOY.md`; this is the orientation
+ "what's done / what's pending" + the gotchas that will bite you.

---

## 1. What this is

"The Newsroom Live" (formerly "The Pulse") — Business Post newsroom wall dashboards, plus
a set of daily/weekly metrics that get logged to a working Google Sheet.

- **Repo:** https://github.com/aashmidha/the-pulse (public — code only, no numbers/secrets committed)
- **Local:** `~/Desktop/newsroom-dashboard`
- **Live URLs:** https://tv.businesspost.group/ (Subscribers dashboard) · `/live.html` (Today/Live screen).
  Also served at `the-pulse-3er.pages.dev` (same Cloudflare Pages project `the-pulse`).

Two static HTML screens (`public/index.html`, `public/live.html`) read JSON
(`metrics.json`, `live.json`) that Python generates and publishes.

## 2. Architecture (Option B)

Python runs in **GitHub Actions**, publishes JSON to **Cloudflare Pages**. Rolling state
lives in **Cloudflare KV**. Scheduling is driven by a **Cloudflare Worker** (GitHub's own
cron is unreliable) that fires `workflow_dispatch`.

- **Cloudflare Pages** project: `the-pulse`. A deploy is a FULL snapshot → every deploy must
  contain BOTH `metrics.json` and `live.json`, so each job pulls the other's JSON from KV
  first (KV = single source of truth; avoids clobber races).
- **Cloudflare KV** namespace: `the-pulse-state`, id `204897e26b5945eb8ff91e38bda7f223`.
- **Worker** `the-pulse-trigger` (`trigger-worker/`) crons:
  - `*/10 * * * *` → `refresh-live.yml`
  - `*/15 * * * *` → `refresh-dashboard.yml`
  - `5 7 * * *` → `refresh-engagement.yml` (redundant backup)
  - (the weekly HITs cron `0 8 * * 6` was **retired**)

## 3. Data sources & where credentials live

| Source | Keys (GitHub repo secrets) | Feeds |
|---|---|---|
| Piano VX | `PIANO_AID`, `PIANO_API_TOKEN`, `PIANO_BASE_URL` | subscribers, new subs, cancellations |
| Piano Analytics | `PA_ACCESS_KEY`, `PA_SECRET_KEY`, `PA_SITE_ID` | conversions, registrations, visits |
| BART RFV | `BART_KEY`, `BART_BASE_URL` (`bonnieraws.dergan.net`) | engagement, RFV, cohorts, per-user reads |
| BART reads | `BART_SESSION_COOKIE`, `BART_NAME` | top stories / HITs (`bart.finance.si/master.php`) |
| Cloudflare | `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_KV_ID` | deploy + KV |
| Sheet webhook | `HITS_WEBHOOK_URL`, `HITS_WEBHOOK_KEY` | writing to the Google Sheet |
| Worker PAT | `GH_TOKEN` (set ON the Worker, not the repo) | lets the Worker dispatch workflows |

- **Local dev:** `.dev.vars` (git-ignored) has `PA_*`, `MANUAL_SHEET_URL`, `BART_SESSION_COOKIE`,
  `BART_NAME`. `~/BART List/enricher_settings.json` has `bart_key`, `bart_base`, and Piano creds —
  `refresh_metrics.py` reads it as `_S` at import, so scripts run locally.
- **Sheet webhook secret value:** `Aashna is the best`. **BART partner group** for `/ma/segments`: `BPIE_ALL`.
- API keys status: last verified **2026-10-05 all green** (every `live` flag true in `metrics.json`).
- ⚠ The Worker's `GH_TOKEN` is a fine-grained PAT (Actions: R+W) **with an expiry** — if it lapses,
  ALL auto-refreshes silently stop. Renew with `wrangler secret put GH_TOKEN --config trigger-worker/wrangler.toml`.

## 4. Scripts (`scripts/`)

- **`refresh_metrics.py`** → `metrics.json` (dashboard). Subscribers (Piano VX, upgrade-excluded),
  engagement (see §6), conversions (Piano Analytics), reads (BART), manual B2B/B2Cd (Google Sheet
  `MANUAL_SHEET_URL`; falls back to KV `manual.json` then hardcoded defaults).
- **`refresh_live.py`** → `live.json` (live screen). Visits today/hourly, `subsToday`
  (from `metrics.json newSubsToday`), `regsToday` (Piano Analytics), top stories, newsletter traffic.
- **`daily_engagement.py`** → logs engagement to the **"Engagement"** sheet tab (reads `metrics.json`,
  upsert by date). Handles `deltaWeek == None` without crashing (see §8).
- **`track_crossings.py`** → logs each article the day it first crosses **1,000 unique subscriber
  reads** (`diff_users`) to the **"Crossings"** tab. Dedup via KV `crossings_seen.json`. Replaced weekly HITs.
- **`active_cohorts.py`** → logs **"B2C eng."** / **"B2B eng."** tabs (`Date · Day · Active users · Avg pageviews`).
  Active = `R=0 & F>0` subscribers by SubStatus via `/ma/segments`. **Avg pageviews = that day's UNIQUE articles
  read per active user** (same-day re-opens count once, like RFV V; same-second app bursts kept as-is) from BART `/user/UID/articles` (built 2026-10-06; replaced the 90-day RFV `V`). Rows are
  dated by the **read day (yesterday, Irish time)**, not the run date. Fails (→ daily marker retries) if <90%
  of the R=0 segment read that day (snapshot not rolled over). ~40s for both cohorts (8 threads).
  `--backfill START END` prints past days (active = current subscribers with ≥1 read that day).
- **`weekly_hits.py`** → RETIRED (manual-only; `refresh-hits.yml` schedule removed).

## 5. Google Sheet (the "Hits log") + Apps Script

One working sheet with tabs this project writes: **HITs** (old weekly, now manual), **Engagement**
(daily %), **Crossings**, **B2C eng.**, **B2B eng.** (plus the user's own Subscribers tab etc.).

Writes go through a Google **Apps Script web app** (`docs/hits-apps-script.gs`) — a generic
append/upsert endpoint: `{key, tab, header, rows, upsertCol}` (+ a legacy HITs payload). `upsertCol`
replaces rows whose key-column matches. URL + secret are the `HITS_WEBHOOK_*` secrets. **No Apps Script
change is needed to add a new tab** — just POST a new `tab` name.

## 6. Engagement (North Star) — rewritten 2026-09-30, BART-only, no Piano

`% of active B2C+B2Cd subscribers with RFV > 19`. `bart_engagement()` in `refresh_metrics.py`
computes it from BART's user-data table via **`/ma/segments`** (POST, `group=BPIE_ALL`, AND'd criteria:
`Userstatus=Subscriber` + `SubStatus` + `rfv>19`). **B2Cd is included** (the old Piano `b2c_uids` join
silently dropped ~110 B2Cd). Verified: denominator matches the RFV CSV export exactly (10,736); ~37.2%
vs the old Piano 36.6%. **Computed once/day and cached in KV `bart_eng.json`** → stable daily value,
no intraday wobble, light BART load; on a failed recompute the cache is kept.
The old `fetch_engagement()` + the `~/Downloads` RFV-CSV readers (`load_prior_rfv` etc.) are **dead code**.

## 7. Scheduling the daily sheet jobs (the reliable pattern)

Engagement + crossings + cohort tabs run from the **every-15-min `refresh-dashboard.yml`**, ONCE/day,
on the **first run at/after 07:00 UTC**, gated by a KV marker `daily_done.json`
(`H -ge 7 && LAST != TODAY`). NOT a narrow `07:00–07:14` window — Cloudflare drops single low-frequency
fires (it did Aug 26–30, silently losing engagement). The step persists its own state
(`crossings_seen.json`, `daily_done.json`) immediately via `kv put --remote`, and only sets the marker
when all scripts succeed (so a failure retries next run).

## 8. Gotchas / hard-won learnings (READ THESE)

- **`wrangler kv key get/put` default to LOCAL state in v4 — ALWAYS pass `--remote`.** This caused a
  silent "metrics.json = {}" bug once.
- **BART `/rfv` feed has only `uid,r,f,v,rfv`** — NO `SubStatus`/`Userstatus`. Those are user-data-table
  attributes, exposed via **`/ma/segments`** (counts + a UID CSV) and **`/rfv/intervals?group_by=SubStatus`**.
  So cohort metrics are fully API-automatable — the manual CSV download is NOT needed.
- **Piano Analytics has a `user_id` dimension, BUT ~85% of pageviews are unattributed (`'N/A'`)** —
  consent/privacy/app. A per-user PA join badly undercounts (matched only 190/458 active B2B on Oct 5).
  **Do not use PA for per-user cohort pageviews.**
- **BART `/user/UID/articles` returns only the newest 100 reads unless you pass `limit`** (script uses 5000).
  Over half of active B2B users exceed 100, and a couple of B2B accounts read 160+ articles in ONE day
  (shared logins/monitoring?) — the cap silently undercounted B2B Oct 5 as 5.0/5.17 instead of 5.64.
- **Multireaders (shared accounts):** BART's `mr_30` multiread factor is NOT a user-data attribute (can't
  filter `/ma/segments` on it) and the live `/user/UID/multiread` `factor` does NOT reproduce it. It IS in the
  API's `/rfv/csv` export (same file as the manual RFV download), filled only for flagged users (~77: 20 B2B,
  28 B2C subs, 24 Staff). B2C + B2B tabs have an extra "excl. multireaders" column from this list.
- **BART `/user/UID/articles` = per-user read history** (`date_local`, `artid`, `url`) → COMPLETE per-day
  reads, any date incl. past, no attribution gap. Fast (458 users in ~60s). This is the right source for
  per-day pageviews. All 458 active B2B users read on Oct 5 per BART (100% vs PA's 41%).
- **RFV window ≈ 90 days** (`F` maxes at 89–90), so `V` is a 90-day total. **`R=0`** (latest snapshot =
  previous day) effectively means "read that day" (100% of `R=0 & F>0` users had that-day reads).
- Piano Analytics **MCP server** is currently disconnected — irrelevant, the pipeline uses the PA REST API
  (`api.atinternet.io/v3/data/getData`) directly, which works.
- KV restore can occasionally reset rolling state (once truncated `eng_history` → `deltaWeek=None` →
  `daily_engagement.py` crashed on `f"{delta:+}"`; now handled). Watch for it.
- A Pages deploy not landing is usually edge-cache lag — retry.

## 9. PENDING / in-progress

1. **Cohort tabs per-day avg pageviews — BUILT 2026-10-06** (see §4). Oct 5 (unique articles): B2C 3.22 (2,320 active),
   B2B 4.30 (459 active). Raw row counts (3.63/5.64) include same-day re-opens. App reads
   (`event_param1=app`, high ids) include same-second multi-article bursts (≈12% of B2C rows, up to 46
   articles/sec — likely prefetch/offline sync); user chose to KEEP them for now. Backfill Sep 22–Oct 5 computed (printed, NOT written — the sheet's date-upsert bug
   would duplicate rows; existing rows hold old V values dated by run date). Open: whether to exclude the
   160+/day B2B outlier accounts (they lift B2B ~4.9 → 5.6).
2. **Engagement tab tweaks — code done 2026-10-06, needs user actions:** logger now writes Day as col 2 (user inserts col B); Apps Script `upsertKey()` normalises Date cells (user DEPLOYED 2026-10-06; Day column in Engagement = user to insert col B). (a) add a **"Day"** (day-of-week) column as column 2 —
   the append endpoint can't insert a column, so the user inserts one and the logger writes it; (b) fix
   **triplicate rows** — root cause is the upsert comparing the Date cell as text while Sheets stores it
   as a date value, so it never matches and re-appends; fix = normalize dates in the Apps Script upsert →
   needs a redeploy. NOT done.
3. **`active_cohorts.py` CI confirmation:** wired into the daily job; the marker advancing implies success,
   but the exact CI run output wasn't captured. Tabs were seeded manually.
4. Signature on the wall screens — PAUSED (`public/signature.svg` staged, not deployed).
5. Old exposed Cloudflare token (`cfat_T…`) — user to revoke (already rotated to `cfat_jS…`).
6. Worker `GH_TOKEN` PAT renewal deadline (see §3).

## 10. Handy commands

```bash
# live dashboard JSON + per-source health flags
curl -s https://the-pulse-3er.pages.dev/metrics.json | python3 -m json.tool | head -40

# read KV state (ALWAYS --remote); namespace id below
KVID=204897e26b5945eb8ff91e38bda7f223
npx wrangler@4 kv key get bart_eng.json --namespace-id "$KVID" --remote

# trigger a job by hand / watch runs
gh workflow run refresh-dashboard.yml --repo aashmidha/the-pulse
gh run list --repo aashmidha/the-pulse --workflow refresh-dashboard.yml --limit 5

# deploy the scheduler Worker
npx wrangler@4 deploy --config trigger-worker/wrangler.toml
```

Persistent memory (`newsroom-live-deployment.md` in this user's Claude memory) carries a condensed
version of the above across sessions.
