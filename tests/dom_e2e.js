// DOM-level end-to-end test: loads the real static assets into jsdom and
// drives clicks against the live server. No browser binary required.
const fs = require("fs");
const path = require("path");
let JSDOM, VirtualConsole;
try {
  ({ JSDOM, VirtualConsole } = require("jsdom"));
} catch {
  ({ JSDOM, VirtualConsole } = require("/tmp/node_modules/jsdom"));
}

const BASE = process.argv[2] || "http://127.0.0.1:8123";
const SAMPLES = process.argv[3] || "/workspace/samples";
const STATIC = path.join(__dirname, "..", "server", "static");

const failures = [];
function check(cond, message) {
  if (!cond) failures.push(message);
  console.log((cond ? "  ✓ " : "  ✗ ") + message);
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function waitFor(fn, label, timeout = 12000, interval = 100) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    try {
      const val = await fn();
      if (val) return val;
    } catch { /* keep polling */ }
    await sleep(interval);
  }
  throw new Error("timeout waiting for " + label);
}

(async () => {
  const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8").replace(
    '<script src="/app.js" type="module"></script>',
    ""
  );
  const virtualConsole = new VirtualConsole();
  const consoleErrors = [];
  virtualConsole.on("jsdomError", e => consoleErrors.push(String(e)));
  virtualConsole.on("error", (...args) => consoleErrors.push(args.join(" ")));

  const dom = new JSDOM(html, { url: BASE + "/", runScripts: "dangerously", virtualConsole });
  const { window } = dom;
  window.fetch = (url, opts) => fetch(new URL(url, BASE).toString(), opts);
  window.HTMLElement.prototype.scrollIntoView = () => {};
  if (!window.Element.prototype.animate) window.Element.prototype.animate = () => ({ cancel() {} });

  const script = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
  window.eval(script);

  const doc = window.document;
  const $ = sel => doc.querySelector(sel);
  const $$ = sel => Array.from(doc.querySelectorAll(sel));
  const text = sel => ($(sel)?.textContent || "").trim();
  const click = sel => {
    const el = $(sel);
    if (!el) throw new Error("missing element: " + sel);
    el.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  };

  console.log("loading sample through the sample button…");
  const baselineReleaseId = Number(
    (await (await fetch(BASE + "/api/releases")).json()).latest_release_id
  );
  await waitFor(() => $('[data-action="load-sample"][data-scenario="initial"]'), "sidebar sample button");
  click('[data-action="load-sample"][data-scenario="initial"]');
  await waitFor(() => $(".stepper"), "stepper");
  // Sample loading then jumps to the seal step.
  await waitFor(() => $('[data-action="seal"]'), "seal button");
  check(doc.body.textContent.includes("演示 · 混乱批次"), "sample batch selected in sidebar");

  console.log("sealing invalid batch…");
  click('[data-action="seal"]');
  await waitFor(() => $$(".issue-list .issue").length >= 4, "seal issues", 15000);
  const issues = $$(".issue-list .issue");
  check(issues.length === 4, `renders 4 constraint issues (got ${issues.length})`);
  const issueText = $(".issue-list").textContent;
  check(issueText.includes("pk.duplicate"), "shows primary-key duplicate rule");
  check(issueText.includes("fk.missing_contract"), "shows cross-table reference rule");
  check(issueText.includes("amount.contract_cap_exceeded"), "shows contract amount ceiling rule");
  check(issueText.includes("customers.csv:5"), "issue located at customers.csv:5");
  check(issueText.includes("invoices.csv:6"), "issue located at invoices.csv:6");
  check(!!$('a[download][href*="rejections.csv"]'), "rejection CSV download link present");

  // Filter chips work.
  click('[data-action="issue-filter"][data-filter="warning"]');
  await sleep(50);
  check($(".issue-empty") || !$(".issue-list"), "warning filter empties the list (no warnings in sample)");
  click('[data-action="issue-filter"][data-filter="error"]');
  await sleep(50);
  check($$(".issue-list .issue").length === 4, "error filter restores all 4 issues");
  click('[data-action="issue-scope"][data-scope="cross"]');
  await sleep(50);
  check($$(".issue-list .issue").length === 4, "cross-table filter keeps 4 issues (pk + fk + amount)");
  click('[data-action="issue-scope"][data-scope="all"]');

  console.log("checking blocked publish…");
  click('[data-action="goto-step"][data-step="publish"]');
  await waitFor(() => $('[data-action="publish"]'), "publish button");
  check($('[data-action="publish"]').dataset.disabled === "true", "publish disabled while seal invalid");
  check($("#main-content").textContent.includes("封口未通过"), "publish page explains the blocker");

  console.log("preview tab…");
  click('[data-action="goto-step"][data-step="preview"]');
  await waitFor(() => $("table.data"), "preview table", 15000);
  const previewText = $("table.data").textContent;
  check(previewText.includes("customer_id"), "preview shows customer columns");
  check($$("tbody tr").length >= 5, "preview lists every candidate row including duplicates");
  check(previewText.includes("结构正常"), "preview marks structurally valid rows");
  click('[data-action="preview-kind"][data-kind="contracts"]');
  await waitFor(() => $("table.data").textContent.includes("contract_id"), "contract preview");
  check($(".file-strip").textContent.includes("contracts.csv"), "file strip shows contracts.csv");

  console.log("revising three files via modal uploads…");
  for (const kind of ["customers", "contracts", "invoices"]) {
    const file = fs.readFileSync(path.join(SAMPLES, "scenario_fixed", `${kind}.csv`), "utf8");
    // Exercise the real revision modal (its file input then triggers doUpload).
    click('[data-action="goto-step"][data-step="upload"]');
    await waitFor(() => $(`input[data-upload-kind="${kind}"]`), "upload input " + kind);
    const input = $(`input[data-upload-kind="${kind}"]`);
    const previousMeta = $(`.upload-card[data-drop-kind="${kind}"] .upload-current .meta`)?.textContent || "";
    const fileObj = new window.File([file], `${kind}.csv`, { type: "text/csv" });
    Object.defineProperty(input, "files", { value: [fileObj], configurable: true });
    input.dispatchEvent(new window.Event("change", { bubbles: true }));
    await waitFor(
      () => {
        if ($$(".spinner").length) return false;
        const meta = $(`.upload-card[data-drop-kind="${kind}"] .upload-current .meta`);
        return meta && meta.textContent !== previousMeta;
      },
      "upload settles for " + kind,
      20000
    );
    await sleep(200);
  }

  console.log("reselling…");
  click('[data-action="goto-step"][data-step="seal"]');
  await waitFor(() => $('[data-action="seal"]'), "seal button again", 15000);
  click('[data-action="seal"]');
  await waitFor(
    () => $("#main-content").textContent.includes("全部约束通过"),
    "valid seal verdict",
    20000
  );
  check($("#main-content").textContent.includes("全部约束通过"), "reseal reports all constraints pass");

  console.log("publishing with simulated interruption…");
  click('[data-action="goto-step"][data-step="publish"]');
  await waitFor(() => $("#interrupt-mode"), "interrupt selector", 15000);
  $("#interrupt-mode").value = "after_stage";
  $("#interrupt-mode").dispatchEvent(new window.Event("change", { bubbles: true }));
  await sleep(50);
  check($('[data-action="publish"]').dataset.disabled !== "true", "publish enabled after valid seal");
  click('[data-action="publish"]');
  await waitFor(() => $('[data-action="recover-op"]'), "recover buttons", 20000);
  const pubText = $("#main-content").textContent;
  check(pubText.includes("已中断"), "op row shows interrupted status");
  check(pubText.includes("暂存"), "explains staged snapshot is not yet live");

  console.log("public query before recovery…");
  click('[data-action="view"][data-view="query"]');
  await waitFor(() => $("table.data, .issue-empty"), "query table", 15000);
  const beforeText = $("#main-content").textContent;
  const liveReleaseBefore = (beforeText.match(/R(\d+)\s*·/) || [])[1];
  check(liveReleaseBefore === String(baselineReleaseId), `public query still points at release R${baselineReleaseId}`);
  // Only the result table — the version-history table below also uses .cell-id,
  // and when there are no rows the first table on the page is that history table.
  const resultTable = $$("table.data")[0];
  const beforeIds = $(".issue-empty") || !resultTable
    ? []
    : Array.from(resultTable.querySelectorAll("tbody td.cell-id"))
        .map(el => el.textContent.trim())
        .filter(id => !/^R\d+$/.test(id));
  if (baselineReleaseId === 0) {
    check(beforeIds.length === 0, "baseline query shows no rows before recovery");
  } else {
    check(!beforeIds.includes("CUST-1001"), "staged new rows are not visible before recovery");
  }

  console.log("resuming…");
  click('[data-action="view"][data-view="upload"]');
  click('[data-action="goto-step"][data-step="publish"]');
  await waitFor(() => $('[data-action="recover-op"][data-mode="resume"]'), "resume button", 15000);
  click('[data-action="recover-op"][data-mode="resume"]');
  await waitFor(() => $('.modal [data-action="modal-confirm"]'), "confirm modal");
  click('.modal [data-action="modal-confirm"]');
  await waitFor(
    () => !$('[data-action="recover-op"][data-mode="resume"]'),
    "recovery completes",
    20000
  );

  console.log("public query after recovery…");
  click('[data-action="view"][data-view="query"]');
  await waitFor(
    () => {
      const txt = $("#main-content").textContent;
      const m = txt.match(/R(\d+)\s*·/);
      return m && Number(m[1]) > baselineReleaseId;
    },
    "release pointer advanced",
    15000
  );
  const afterText = $("#main-content").textContent;
  check(afterText.includes("CUST-0001"), "published rows visible after recovery");
  check(!afterText.includes("CUST-1001"), "only the recovered batch's rows are present");

  console.log("query contract tab…");
  click('[data-action="query-kind"][data-kind="contracts"]');
  await waitFor(() => $("#main-content").textContent.includes("CTR-0001"), "contract query rows", 15000);
  check($$(".cell-money").length > 0, "money column rendered for contracts");
  check($("#main-content").textContent.includes("12,000.00"), "money formatted with thousands separator");

  console.log("duplicate seal click is idempotent…");
  click('[data-action="view"][data-view="upload"]');
  click('[data-action="goto-step"][data-step="seal"]');
  await waitFor(() => $('[data-action="seal"]'), "seal button for idempotency", 15000);
  const sealCountBefore = $$(".version-item").length;
  click('[data-action="seal"]');
  await sleep(900);
  const sealCountAfter = $$(".version-item").length;
  check(sealCountAfter === sealCountBefore, `repeat seal adds no history row (${sealCountBefore} → ${sealCountAfter})`);

  check(consoleErrors.length === 0, "no uncaught browser errors: " + (consoleErrors[0] || ""));

  dom.window.close();
  console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL DOM CHECKS PASSED");
  process.exit(failures.length ? 1 : 0);
})().catch(err => {
  console.error("E2E FAILED:", err.message);
  process.exit(1);
});
