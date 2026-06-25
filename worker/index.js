/**
 * Business Post — Newsroom Dashboard API (Cloudflare Worker)
 * ----------------------------------------------------------
 * GET /api/metrics  → JSON payload consumed by public/index.html
 *
 * Design (matches the agreed architecture):
 *   • Heavy Piano subscriber COUNTS are NOT computed per request. A scheduled
 *     job (cron) writes them to KV; this endpoint just reads KV + assembles.
 *   • The two manual weekly figures (corporate seats, telesales/direct) live in
 *     KV too, updated COB Friday (Google Sheet sync or a tiny admin POST).
 *   • A Friday SNAPSHOT in KV is the baseline for the 7-day deltas.
 *   • If KV is empty (fresh deploy / local dev), we fall back to MOCK so the
 *     screen never blanks and you can demo immediately.
 *
 * Bindings (wrangler.toml):
 *   KV namespace  DASH_KV
 *   secrets:      PIANO_API_TOKEN, PIANO_AID,
 *                 PA_ACCESS_KEY, PA_SECRET_KEY, PA_SITE_ID
 */

const PIANO_BASE = "https://api-eu.piano.io";          // EU publisher API
const PA_BASE    = "https://api.atinternet.io/v3/data/getData"; // Piano Analytics

const MOCK = {
  weekLabel: "Week 23 · Jun 1 – Jun 7",
  subscribers: {
    piano:{value:10644}, b2cd:{value:46}, totalB2C:{value:10690},
    totalB2B:{value:2258}, totalPaid:{value:12948, deltaWeek:77}
  },
  movement:{ newSubs:84, cancellations:7, net:77, prevNet:61 },
  registeredUsers:{ last7:1342, prev7:1187 },
  conversions:{ status:"pending", subscribers:[], registrations:[] },
  topReads7d:[
    {headline:"ECB signals June rate cut as inflation cools across eurozone", reads:4820},
    {headline:"Inside the boardroom battle at Ireland's biggest builder", reads:3910},
    {headline:"Exclusive: State eyes stake in semiconductor plant", reads:3460}
  ],
  topReads24h:[
    {headline:"Exclusive: State eyes stake in semiconductor plant", reads:1180},
    {headline:"ECB signals June rate cut as inflation cools across eurozone", reads:940},
    {headline:"Housing targets slip again as completions fall short", reads:760}
  ]
};

export default {
  // ---- HTTP -------------------------------------------------------------
  async fetch(req, env) {
    const url = new URL(req.url);
    if (url.pathname !== "/api/metrics") {
      return new Response("Not found", { status: 404 });
    }
    const cors = {
      "content-type": "application/json",
      "access-control-allow-origin": "*",
      "cache-control": "public, max-age=60",
    };
    try {
      const payload = await assemble(env);
      return new Response(JSON.stringify(payload), { headers: cors });
    } catch (err) {
      // Never blank the screen — serve last-good cache, else mock.
      const cached = env.DASH_KV && (await env.DASH_KV.get("last_good", "json"));
      const body = cached || MOCK;
      body._warning = "served fallback: " + (err?.message || "error");
      return new Response(JSON.stringify(body), { headers: cors });
    }
  },

  // ---- CRON (set in wrangler.toml: e.g. */15 * * * *) -------------------
  // This is where the heavy Piano aggregation runs, off the request path.
  async scheduled(event, env, ctx) {
    ctx.waitUntil(refreshCounts(env));
  },
};

/**
 * Assemble the response from KV (fast). KV is populated by refreshCounts().
 * Falls back to MOCK fields when a key is missing so partial setups still work.
 */
async function assemble(env) {
  const kv = env.DASH_KV;
  if (!kv) return MOCK; // local dev / no binding yet

  const [counts, manual, snapshot, conv, reads] = await Promise.all([
    kv.get("sub_counts", "json"),     // {piano, active, b2b, newSubs7d, cancels7d, registered7d, registeredPrev7d}
    kv.get("manual", "json"),         // {corporate, telesalesDirect}  ← COB Friday
    kv.get("friday_snapshot", "json"),// {totalPaid, net} from last Friday
    kv.get("conversions", "json"),    // {status, subscribers:[...], registrations:[...]}
    kv.get("top_reads", "json"),      // {d7:[{headline,reads}], h24:[...]}
  ]);

  if (!counts) return MOCK; // nothing computed yet → demo data

  const corporate = manual?.corporate ?? MOCK.subscribers.totalB2B.value;
  const telesales = manual?.telesalesDirect ?? MOCK.subscribers.b2cd.value;

  const totalB2C   = counts.piano + telesales;
  const totalPaid  = totalB2C + corporate;
  const deltaWeek  = snapshot ? totalPaid - snapshot.totalPaid : counts.newSubs7d - counts.cancels7d;

  const payload = {
    weekLabel: weekLabel(),
    subscribers: {
      piano:    { value: counts.piano },
      b2cd:     { value: telesales },
      totalB2C: { value: totalB2C },
      totalB2B: { value: corporate },
      totalPaid:{ value: totalPaid, deltaWeek },
    },
    movement: {
      newSubs: counts.newSubs7d,
      cancellations: counts.cancels7d,
      net: counts.newSubs7d - counts.cancels7d,
      prevNet: snapshot?.net ?? null,
    },
    registeredUsers: { last7: counts.registered7d, prev7: counts.registeredPrev7d },
    conversions: conv || { status: "pending", subscribers: [], registrations: [] },
    topReads7d: reads?.d7 || MOCK.topReads7d,
    topReads24h: reads?.h24 || MOCK.topReads24h,
    updatedISO: new Date().toISOString(),
  };

  await kv.put("last_good", JSON.stringify(payload), { expirationTtl: 86400 });
  return payload;
}

/* =====================================================================
 * SCHEDULED AGGREGATION  (the heavy lifting, off the request path)
 * Mirrors the logic already in piano_bart_export.py / bart_enricher.py.
 * Fill these in once you wire credentials; until then KV stays on MOCK.
 * ===================================================================== */
async function refreshCounts(env) {
  const counts = await pianoSubscriberCounts(env);     // status counts + 7d new/cancel
  const reg    = await pianoNewRegistered(env);        // registered in last 7d / prev 7d
  const conv   = await pianoConversionArticles(env);   // ⚠ needs goal configured
  const reads  = await bartTopReads(env);              // top reads by subscriber

  await env.DASH_KV.put("sub_counts", JSON.stringify({ ...counts, ...reg }));
  await env.DASH_KV.put("conversions", JSON.stringify(conv));
  await env.DASH_KV.put("top_reads", JSON.stringify(reads));
}

// Piano publisher API helper (token-based, EU endpoint).
async function piano(env, path, params = {}) {
  const qs = new URLSearchParams({ ...params, api_token: env.PIANO_API_TOKEN, aid: env.PIANO_AID });
  const r = await fetch(`${PIANO_BASE}${path}?${qs}`);
  if (!r.ok) throw new Error(`Piano ${path} ${r.status}`);
  return r.json();
}

/**
 * Subscriber status counts + 7-day new/cancellations.
 * Uses subscription/list paginated (same fields piano_bart_export.py reads:
 * status ∈ active/cancelled/expired/terminated, term.name, start_date).
 * B2B = term name starts with "Corp"; everything else paid = B2C.
 */
async function pianoSubscriberCounts(env) {
  const ACTIVE = new Set(["active"]); // + paid intro offers per your rules
  const now = Date.now() / 1000, weekAgo = now - 7 * 86400;
  let offset = 0, limit = 1000, piano = 0, b2b = 0, newSubs7d = 0, cancels7d = 0;

  for (;;) {
    const data = await piano(env, "/api/v3/publisher/subscription/list", { offset, limit });
    const subs = data.subscriptions || [];
    for (const s of subs) {
      const term = s.term?.name || "";
      const isCorp = term.startsWith("Corp");
      if (ACTIVE.has(s.status)) {
        if (isCorp) b2b++; else piano++;
      }
      if (s.start_date >= weekAgo) newSubs7d++;
      if ((s.status === "cancelled" || s.status === "terminated") &&
          (s.cancel_date ?? s.expire_date ?? 0) >= weekAgo) cancels7d++;
    }
    if (subs.length < limit) break;
    offset += limit;
  }
  // NOTE: `piano` here is total active B2C from Piano. The dashboard subtracts
  // site-licence/corp already (counted into b2b), matching "10,644".
  return { piano, b2b, newSubs7d, cancels7d };
}

/** New registered users in last 7d and the previous 7d (for the delta). */
async function pianoNewRegistered(env) {
  // user/list supports create_date filtering; bucket into the two windows.
  // Stubbed to mock until the exact filter params are confirmed.
  return { registered7d: MOCK.registeredUsers.last7, registeredPrev7d: MOCK.registeredUsers.prev7 };
}

/**
 * ⚠ CONVERSION ATTRIBUTION — top 3 articles driving new SUBSCRIPTIONS and the
 * top 3 driving new REGISTRATIONS (both weekly). Each needs its own Piano
 * Analytics conversion goal, tagged with the originating article (d:page).
 * If a goal isn't configured, that list stays empty and status is "pending".
 */
async function pianoConversionArticles(env) {
  if (!env.PA_ACCESS_KEY) return { status: "pending", subscribers: [], registrations: [] };

  // Set these once the goals exist in Piano Composer:
  const SUB_GOAL = env.PA_SUB_GOAL_ID;   // e.g. subscription conversion goal id
  const REG_GOAL = env.PA_REG_GOAL_ID;   // e.g. registration conversion goal id

  async function topArticles(goalId) {
    if (!goalId) return null;
    const today = new Date(), d2 = today.toISOString().slice(0, 10);
    const d1 = new Date(today - 7 * 86400e3).toISOString().slice(0, 10);
    const r = await fetch(PA_BASE, {
      method: "POST",
      headers: { "x-api-key": `${env.PA_ACCESS_KEY}_${env.PA_SECRET_KEY}`, "content-type": "application/json" },
      body: JSON.stringify({
        columns: ["m:conversions", "d:page"],
        space: { s: [Number(env.PA_SITE_ID)], d1, d2 },
        filter: { property: { "d:goal_id": { $eq: goalId } } },
        sort: ["-m:conversions"], "max-results": 3, "page-num": 1,
      }),
    });
    if (!r.ok) throw new Error("PA " + r.status);
    const j = await r.json();
    return (j.DataFeed?.Rows || []).map(row => ({
      headline: row.Cells?.[1]?.Value, count: Number(row.Cells?.[0]?.Value || 0),
    }));
  }

  try {
    const [subscribers, registrations] = await Promise.all([topArticles(SUB_GOAL), topArticles(REG_GOAL)]);
    const ready = (subscribers && subscribers.length) || (registrations && registrations.length);
    return {
      status: ready ? "live" : "pending",
      subscribers: subscribers || [],
      registrations: registrations || [],
    };
  } catch {
    return { status: "pending", subscribers: [], registrations: [] };
  }
}

/** Top reads by subscriber — from BART (7d + 24h windows). */
async function bartTopReads(env) {
  // Plug in your BART endpoint here; mock until wired.
  return { d7: MOCK.topReads7d, h24: MOCK.topReads24h };
}

function weekLabel() {
  // ISO week number + range; demo keeps it simple.
  const now = new Date();
  const opts = { day: "numeric", month: "short" };
  const start = new Date(now); start.setDate(now.getDate() - 6);
  return `${start.toLocaleDateString("en-IE", opts)} – ${now.toLocaleDateString("en-IE", opts)}`;
}
