// Browser E2E (optional). Runs the same journey as dom_e2e.js when a real
// Chromium binary is reachable; skips cleanly otherwise.
//
// Usage: node tests/e2e.js [baseUrl] [sampleDir] [outDir]
// Needs `playwright-core` plus a chromium headless shell. Set
// PW_CHROMIUM=/path/to/chrome-headless-shell to point at one.

const fs = require("fs");
const path = require("path");

const BASE = process.argv[2] || "http://127.0.0.1:8080";
const SAMPLES = process.argv[3] || path.join(__dirname, "..", "samples");
const OUT = process.argv[4] || "/tmp/shots";

let chromium;
try {
  ({ chromium } = require("playwright-core"));
} catch {
  try {
    ({ chromium } = require("../node_modules/playwright-core"));
  } catch {
    console.log("SKIP: playwright-core not installed; use tests/dom_e2e.js instead");
    process.exit(0);
  }
}

const executablePath = process.env.PW_CHROMIUM;
if (!executablePath || !fs.existsSync(executablePath)) {
  console.log("SKIP: set PW_CHROMIUM=/path/to/chrome-headless-shell to run the browser test");
  process.exit(0);
}

fs.mkdirSync(OUT, { recursive: true });
const errors = [];
const shot = (page, name) => page.screenshot({ path: path.join(OUT, name + ".png"), fullPage: true });

(async () => {
  const browser = await chromium.launch({ executablePath, args: ["--no-sandbox"] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.on("console", m => m.type() === "error" && errors.push("console: " + m.text()));
  page.on("pageerror", e => errors.push("pageerror: " + e.message));

  await page.goto(BASE, { waitUntil: "networkidle" });
  await shot(page, "01-empty");

  await page.click('[data-action="load-sample"][data-scenario="initial"]');
  await page.waitForSelector(".stepper", { timeout: 15000 });
  await shot(page, "02-sample-loaded");

  await page.click('[data-action="seal"]');
  await page.waitForSelector(".issue-list .issue", { timeout: 15000 });
  await page.waitForTimeout(300);
  await shot(page, "03-seal-invalid");

  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.click('a[download][href*="rejections.csv"]'),
  ]);
  await download.saveAs(path.join(OUT, "rejections.csv"));

  await page.click('[data-action="goto-step"][data-step="preview"]');
  await page.waitForSelector("table.data", { timeout: 8000 });
  await shot(page, "04-preview");

  await page.click('[data-action="goto-step"][data-step="seal"]');
  for (const kind of ["customers", "contracts", "invoices"]) {
    await page.click('[data-action="goto-step"][data-step="upload"]');
    await page
      .locator(`input[data-upload-kind="${kind}"]`)
      .setInputFiles(path.join(SAMPLES, "scenario_fixed", `${kind}.csv`));
    await page.waitForTimeout(700);
  }
  await page.click('[data-action="goto-step"][data-step="seal"]');
  await page.waitForFunction(
    () => document.querySelector("#main-content")?.textContent.includes("全部约束通过"),
    { timeout: 15000 }
  );
  await shot(page, "05-seal-valid");

  await page.click('[data-action="goto-step"][data-step="publish"]');
  await page.selectOption("#interrupt-mode", "after_stage");
  await page.click('[data-action="publish"]');
  await page.waitForSelector('[data-action="recover-op"]', { timeout: 15000 });
  await shot(page, "06-interrupted");

  await page.click('[data-action="view"][data-view="query"]');
  await page.waitForTimeout(800);
  await shot(page, "07-query-before-recover");

  await page.click('[data-action="view"][data-view="upload"]');
  await page.click('[data-action="goto-step"][data-step="publish"]');
  await page.click('[data-action="recover-op"][data-mode="resume"]');
  await page.click('.modal [data-action="modal-confirm"]');
  await page.waitForSelector('[data-action="recover-op"]', { state: "detached", timeout: 15000 });

  await page.click('[data-action="view"][data-view="query"]');
  await page.waitForTimeout(800);
  await shot(page, "08-query-after-recover");

  await page.setViewportSize({ width: 420, height: 900 });
  await shot(page, "09-mobile");

  await browser.close();
  if (errors.length) {
    console.error("BROWSER ERRORS:\n" + errors.join("\n"));
    process.exit(1);
  }
  console.log("E2E OK");
})().catch(err => {
  console.error("E2E FAILED:", err.message);
  if (errors.length) console.error(errors.join("\n"));
  process.exit(1);
});
