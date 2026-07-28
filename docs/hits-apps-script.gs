/**
 * Weekly HITs log — Google Apps Script (append endpoint for the newsroom hits report).
 *
 * SETUP (one time):
 *   1. Create a new Google Sheet (this is where the hits will land).
 *   2. Extensions > Apps Script. Delete anything there and paste this whole file in.
 *   3. Change SECRET below to any phrase you like (keep it private).
 *   4. Deploy > New deployment > gear icon > Web app.
 *        - Description: "Weekly HITs"
 *        - Execute as: Me
 *        - Who has access: Anyone
 *      Click Deploy, authorise when prompted, and copy the "Web app" URL.
 *   5. Send me the Web app URL and the SECRET. That's it.
 *
 * The Saturday job then POSTs each week's hits here and this appends them to a
 * "HITs" tab as a running log (one row per article, tagged with the week number).
 */

const SECRET = "CHOOSE_A_SECRET";   // must match HITS_WEBHOOK_KEY on the job side
const TAB = "HITs";

function doPost(e) {
  const data = JSON.parse(e.postData.contents);
  if (data.key !== SECRET) {
    return ContentService.createTextOutput("forbidden");
  }
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(TAB) || ss.insertSheet(TAB);
  if (sheet.getLastRow() === 0) {
    sheet.appendRow(["Week", "Date", "Rank", "Article ID", "Title", "Unique users", "Total volume", "URL"]);
  }
  const hits = data.hits || [];
  hits.forEach(function (h, i) {
    sheet.appendRow([data.week, data.date, i + 1, h.articleId, h.title, h.uniqueUsers, h.totalVolume, h.url]);
  });
  return ContentService.createTextOutput("added " + hits.length + " hits for week " + data.week);
}
