// Reliable scheduler for The Pulse.
// Cloudflare cron fires this; it calls GitHub's workflow_dispatch API to run
// the existing refresh jobs. Keeps all the Python in GitHub Actions unchanged.

const REPO = "aashmidha/the-pulse";

async function dispatch(workflowFile, token) {
  const res = await fetch(
    `https://api.github.com/repos/${REPO}/actions/workflows/${workflowFile}/dispatches`,
    {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${token}`,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "the-pulse-trigger",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: "main" }),
    }
  );
  // GitHub returns 204 No Content on success.
  if (res.status !== 204) {
    throw new Error(`${workflowFile} -> HTTP ${res.status}: ${await res.text()}`);
  }
}

export default {
  // Cloudflare invokes this once per matching cron expression.
  async scheduled(event, env, ctx) {
    const file =
      event.cron === "*/15 * * * *" ? "refresh-dashboard.yml"  : // dashboard, 15 min
      event.cron === "0 8 * * 6"    ? "refresh-hits.yml"       : // weekly HITs, Sat 08:00 UTC
      event.cron === "5 7 * * *"    ? "refresh-engagement.yml" : // engagement log, daily 07:05 UTC
                                      "refresh-live.yml";         // live, 10 min
    ctx.waitUntil(dispatch(file, env.GH_TOKEN));
  },

  // Manual test endpoint, gated by TRIGGER_KEY so it can't be abused:
  //   curl "https://the-pulse-trigger.<sub>.workers.dev/trigger?key=KEY&wf=dashboard"
  async fetch(req, env) {
    const url = new URL(req.url);
    if (url.pathname === "/trigger") {
      if (!env.TRIGGER_KEY || url.searchParams.get("key") !== env.TRIGGER_KEY) {
        return new Response("forbidden\n", { status: 403 });
      }
      const wfParam = url.searchParams.get("wf");
      const wf =
        wfParam === "dashboard"  ? "refresh-dashboard.yml" :
        wfParam === "hits"       ? "refresh-hits.yml" :
        wfParam === "engagement" ? "refresh-engagement.yml" :
                                   "refresh-live.yml";
      try {
        await dispatch(wf, env.GH_TOKEN);
        return new Response(`dispatched ${wf}\n`);
      } catch (e) {
        return new Response(String(e) + "\n", { status: 500 });
      }
    }
    return new Response("the-pulse trigger worker — ok\n");
  },
};
