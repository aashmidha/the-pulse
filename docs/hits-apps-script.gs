/**
 * Newsroom Sheet endpoint — Google Apps Script (shared append/upsert endpoint).
 *
 * One web app that the newsroom jobs POST to. Two payload shapes:
 *
 *   Weekly HITs (legacy shape, unchanged):
 *     { key, week, date, hits: [ {articleId, title, uniqueUsers, totalVolume, url}, ... ] }
 *     -> appends to a "HITs" tab, one row per hit.
 *
 *   Generic metric (Engagement, and anything future):
 *     { key, tab, header: [...], rows: [[...], ...], upsertCol?: <0-based col index> }
 *     -> appends rows to <tab> (writing <header> first if the tab is empty).
 *        If upsertCol is given, any existing rows whose value in that column matches an
 *        incoming row are removed first — so a re-run replaces that key instead of
 *        duplicating it (e.g. one row per Date for the Engagement log).
 *
 * SETUP / UPDATE:
 *   1. Extensions > Apps Script. Replace everything with this file.
 *   2. Set SECRET below to your existing secret (must match HITS_WEBHOOK_KEY on the jobs).
 *   3. Deploy > Manage deployments > (pencil/edit) > Version: New version > Deploy.
 *      This keeps the SAME web-app URL — no need to resend it.
 */

const SECRET = "CHOOSE_A_SECRET";   // must match HITS_WEBHOOK_KEY on the job side

function getOrCreate(name) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  return ss.getSheetByName(name) || ss.insertSheet(name);
}

function doPost(e) {
  const data = JSON.parse(e.postData.contents);
  if (data.key !== SECRET) {
    return ContentService.createTextOutput("forbidden");
  }

  // --- Legacy weekly-HITs payload -----------------------------------------
  if (data.hits) {
    const sheet = getOrCreate("HITs");
    if (sheet.getLastRow() === 0) {
      sheet.appendRow(["Week", "Date", "Rank", "Article ID", "Title",
                       "Unique users", "Total volume", "URL"]);
    }
    data.hits.forEach(function (h, i) {
      sheet.appendRow([data.week, data.date, i + 1, h.articleId, h.title,
                       h.uniqueUsers, h.totalVolume, h.url]);
    });
    return ContentService.createTextOutput("added " + data.hits.length +
                                           " hits for week " + data.week);
  }

  // --- Generic tab / rows payload (with optional upsert) -------------------
  const tab = data.tab || "Log";
  const sheet = getOrCreate(tab);
  if (sheet.getLastRow() === 0 && data.header) {
    sheet.appendRow(data.header);
  }
  const rows = data.rows || [];

  if (data.upsertCol != null && rows.length && sheet.getLastRow() > 1) {
    const keys = {};
    rows.forEach(function (r) { keys[String(r[data.upsertCol])] = true; });
    const nCols = sheet.getLastColumn();
    const existing = sheet.getRange(2, 1, sheet.getLastRow() - 1, nCols).getValues();
    for (let i = existing.length - 1; i >= 0; i--) {   // bottom-up so row indexes stay valid
      if (keys[String(existing[i][data.upsertCol])]) {
        sheet.deleteRow(i + 2);
      }
    }
  }

  rows.forEach(function (r) { sheet.appendRow(r); });
  return ContentService.createTextOutput("added " + rows.length + " row(s) to " + tab);
}
