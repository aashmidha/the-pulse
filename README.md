# Business Post — Newsroom Dashboard

TV-ready dashboard for the newsroom screens. Live subscriber numbers from Piano,
conversion attribution, net-new/churn, and the subscriber-engagement feed from BART.

```
newsroom-dashboard/
├── public/index.html   ← the screen (static, self-contained, no build step)
├── worker/index.js     ← Cloudflare Worker API → GET /api/metrics
├── wrangler.toml       ← Pages assets + KV + cron config
└── README.md
```

## Run the screen locally
Open `public/index.html` in a browser — it runs on mock data immediately
(numbers gently "tick" so you can see the live behaviour). Nothing to install.

## Metrics on screen
| Panel | Source | Live? |
|---|---|---|
| **Total Paid Subscribers** + weekly Δ | Piano subscription counts + manual weekly | ✅ 90% live |
| Breakdown: Piano / Telesales+Direct / B2C / Corporate | Piano + manual (Fri) | ✅ |
| **Net New Subs (7d)** | Piano `start_date` in last 7d | ✅ |
| **Cancellations (7d)** | Piano cancelled/terminated in last 7d | ✅ |
| **New Registered Users (7d)** | Piano `create_date` + `registered` | ✅ |
| **Articles Driving New Subscribers** | Piano Analytics conversion goal × `d:page` | ⚠️ needs goal configured |
| **Top Reads by Subscribers** | BART | ✅ (your existing feed) |

> ⚠️ **Conversion attribution** only works once a Piano Analytics conversion goal
> (subscription / registration) is tagged with the originating article. Until then
> the panel shows "awaiting Piano goal config". Confirm with whoever owns Piano Composer.

## Architecture (why it's split this way)
- Counting every subscriber is a **heavy** Piano pull — so it does **not** run on
  each screen refresh. A **cron** (`scheduled()` in the Worker) aggregates counts
  and writes them to **KV**; `/api/metrics` just reads KV and assembles fast.
- The **two manual weekly numbers** (corporate seats, telesales/direct) live in KV,
  updated **COB Friday** (Google-Sheet sync or a one-line `wrangler kv key put`).
- A **Friday snapshot** in KV is the baseline for the 7-day deltas.
- If KV is empty, the Worker serves **mock** data → the screen never blanks.

## Going live
1. **Data layer** — the real Piano logic is already written in `worker/index.js`
   (mirrors `piano_bart_export.py`). Set secrets:
   ```bash
   npx wrangler secret put PIANO_API_TOKEN
   npx wrangler secret put PIANO_AID
   npx wrangler secret put PA_ACCESS_KEY
   npx wrangler secret put PA_SECRET_KEY
   npx wrangler secret put PA_SITE_ID
   ```
2. **KV** — create the namespace and drop the id into `wrangler.toml`:
   ```bash
   npx wrangler kv namespace create DASH_KV
   npx wrangler kv key put --binding=DASH_KV manual '{"corporate":2258,"telesalesDirect":46}'
   ```
3. **Front-end** — in `public/index.html`, swap `fetchMetrics()` to:
   ```js
   const r = await fetch('/api/metrics'); if(!r.ok) throw new Error(r.status); return r.json();
   ```
   and delete the `setInterval(refresh, 5000)` demo-jitter line.
4. **Deploy**:
   ```bash
   npx wrangler deploy
   ```
   Point a screen's browser at the URL in kiosk/full-screen mode.

## Still to confirm (scoping session)
- Piano **conversion goal** for attribution (the only ⚠️ above).
- Exact `user/list` filter params for new-registered windows.
- How the manual numbers flow in: Google Sheet sync vs. weekly `kv key put`.
- Friday-snapshot job timing (locks the 7-day baseline).
