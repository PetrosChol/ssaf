// Renders docs/installation-guide.html to docs/installation-guide.pdf.
//   node docs/build-installation-guide.js
// Needs Playwright with Chromium (`npm i -D playwright && npx playwright install chromium`).
const path = require("path");
const { chromium } = require("playwright");

(async () => {
  const html = path.join(__dirname, "installation-guide.html");
  const pdf = path.join(__dirname, "installation-guide.pdf");
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto("file://" + html);
  await page.pdf({
    path: pdf,
    format: "A4",
    preferCSSPageSize: true,
    printBackground: true,
    displayHeaderFooter: true,
    headerTemplate: "<span></span>",
    footerTemplate:
      '<div style="width:100%;font:7.5pt Arial,sans-serif;color:#8a92a3;padding:0 17mm;' +
      'display:flex;justify-content:space-between"><span>SSSF installation guide</span>' +
      '<span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>',
  });
  await browser.close();
  console.log("wrote " + pdf);
})();
