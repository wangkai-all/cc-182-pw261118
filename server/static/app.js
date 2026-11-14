// 入仓台前端：无构建步骤，直接调用本地 JSON API。

const state = {
  view: "upload",            // upload | query
  step: "upload",            // upload | preview | seal | publish
  batches: [],
  batch: null,
  previewKind: "customers",
  preview: {},
  fileFilter: "all",         // all | error | warning
  issueScope: "all",         // all | cross | file
  interruptMode: "none",
  query: { releaseId: "", kind: "customers", search: "", limit: 50, offset: 0, data: null },
  busy: new Set(),
  releaseInfo: null,
};

const KIND_META = {
  customers: { label: "客户", icon: "◍", file: "customers.csv", desc: "customer_id · name · region · contact_email" },
  contracts: { label: "合同", icon: "▤", file: "contracts.csv", desc: "contract_id · customer_id · title · amount · signed_on" },
  invoices: { label: "发票", icon: "⎘", file: "invoices.csv", desc: "invoice_id · contract_id · amount · issued_on" },
};

const KINDS = ["customers", "contracts", "invoices"];

const BATCH_STATUS = {
  open: { text: "收件中", cls: "pill-neutral" },
  revising: { text: "修订中", cls: "pill-info" },
  sealed_valid: { text: "封口通过", cls: "pill-ok" },
  sealed_invalid: { text: "封口未通过", cls: "pill-danger" },
  sealed_valid_republish: { text: "可再发布", cls: "pill-ok" },
  published: { text: "已发布", cls: "pill-ok" },
};

const RULE_GROUPS = {
  "pk.duplicate": "跨表 / 主键",
  "fk.missing_customer": "跨表 / 引用",
  "fk.missing_contract": "跨表 / 引用",
  "amount.contract_cap_exceeded": "跨表 / 金额",
  "file.missing": "文件 / 完整性",
  "file.header_mismatch": "文件 / 结构",
  "file.empty": "文件 / 结构",
  "file.no_data": "文件 / 结构",
  "file.too_large": "文件 / 结构",
  "file.too_many_rows": "文件 / 结构",
  "file.unreadable_csv": "文件 / 结构",
  "row.required": "行 / 必填",
  "row.id_format": "行 / 格式",
  "row.money_format": "行 / 格式",
  "row.date_format": "行 / 格式",
  "row.email_format": "行 / 格式",
  "row.missing_columns": "行 / 结构",
  "row.extra_columns": "行 / 结构",
  "row.empty": "行 / 结构",
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

// ------------------------------------------------------------------ helpers
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[ch]);
}

function money(cents) {
  if (cents == null || cents === "") return "—";
  const n = Number(cents);
  return (n / 100).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function pct(n, total) {
  if (!total) return "0%";
  return Math.round((n / total) * 100) + "%";
}

function timeAgo(iso) {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  const diff = Math.max(0, Date.now() - then);
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return Math.floor(diff / 60_000) + " 分钟前";
  if (diff < 86_400_000) return Math.floor(diff / 3_600_000) + " 小时前";
  return new Date(iso).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function fmtTime(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

function ruleGroup(rule) {
  return RULE_GROUPS[rule] || (rule.startsWith("pk.") ? "跨表 / 主键" : rule.startsWith("fk.") ? "跨表 / 引用" : "其他");
}

function kindLabel(kind) {
  return KIND_META[kind]?.label || kind || "—";
}

function severityLabel(sev) {
  return sev === "error" ? "错误" : "警告";
}

// --------------------------------------------------------------------- API
async function api(path, options = {}) {
  const init = { headers: {}, ...options };
  if (init.body && !(init.body instanceof FormData)) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(init.body);
  }
  const res = await fetch(path, init);
  let payload = null;
  const type = res.headers.get("Content-Type") || "";
  if (type.includes("application/json")) {
    payload = await res.json().catch(() => null);
  }
  if (!res.ok) {
    const err = new Error(payload?.message || `请求失败（HTTP ${res.status}）`);
    err.status = res.status;
    err.code = payload?.error || "http_error";
    err.payload = payload;
    throw err;
  }
  return payload;
}

async function busy(key, fn) {
  if (state.busy.has(key)) return null;
  state.busy.add(key);
  renderBusy();
  try {
    return await fn();
  } finally {
    state.busy.delete(key);
    renderBusy();
  }
}

// -------------------------------------------------------------------- toast
function toast(message, tone = "info", title = "") {
  const root = $("#toast-root");
  const node = document.createElement("div");
  node.className = `toast ${tone === "error" ? "is-error" : tone === "ok" ? "is-ok" : ""}`;
  node.innerHTML = `<span aria-hidden="true">${tone === "error" ? "⚠" : tone === "ok" ? "✓" : "ℹ"}</span>
    <div>${title ? `<strong>${esc(title)}</strong><br>` : ""}${esc(message)}</div>`;
  root.appendChild(node);
  setTimeout(() => {
    node.style.transition = "opacity .2s";
    node.style.opacity = "0";
    setTimeout(() => node.remove(), 220);
  }, tone === "error" ? 5600 : 3600);
}

// -------------------------------------------------------------------- modal
function openModal({ title, body, confirmText = "确认", cancelText = "取消", tone = "primary", onConfirm }) {
  const root = $("#modal-root");
  root.innerHTML = `
    <div class="modal-backdrop" data-action="modal-backdrop">
      <div class="modal" role="dialog" aria-modal="true" aria-label="${esc(title)}">
        <div class="modal-head">
          <h2>${esc(title)}</h2>
          <button class="close-btn" data-action="modal-close" aria-label="关闭">×</button>
        </div>
        <div class="modal-body">${body}</div>
        <div class="modal-foot">
          <button class="btn" data-action="modal-close">${esc(cancelText)}</button>
          <button class="btn ${tone === "danger" ? "btn-danger" : "btn-primary"}" data-action="modal-confirm">${esc(confirmText)}</button>
        </div>
      </div>
    </div>`;
  const close = () => { root.innerHTML = ""; };
  $("#modal-root [data-action='modal-backdrop']").addEventListener("click", ev => {
    if (ev.target.dataset.action === "modal-backdrop") close();
  });
  $$("#modal-root [data-action='modal-close']").forEach(btn => btn.addEventListener("click", close));
  const confirm = $("#modal-root [data-action='modal-confirm']");
  confirm.addEventListener("click", async () => {
    const keep = await onConfirm?.(() => close);
    if (keep !== false) close();
  });
  const focusTarget = $("#modal-root input, #modal-root textarea");
  focusTarget?.focus();
  return { close };
}

// ------------------------------------------------------------------- render
function renderBusy() {
  $$("[data-busy-key]").forEach(el => {
    const key = el.dataset.busyKey;
    const isBusy = state.busy.has(key);
    el.disabled = isBusy || el.dataset.disabled === "true";
    if (el.tagName === "INPUT") return;  // file inputs are hidden; no spinner inside
    if (isBusy) {
      if (!el.dataset.label) el.dataset.label = el.innerHTML;
      el.innerHTML = `<span class="spinner"></span> 处理中…`;
    } else if (el.dataset.label) {
      el.innerHTML = el.dataset.label;
      delete el.dataset.label;
    }
  });
}

function statusPill(status) {
  const meta = BATCH_STATUS[status] || { text: status, cls: "pill-neutral" };
  return `<span class="pill ${meta.cls}">${esc(meta.text)}</span>`;
}

function renderShell() {
  $("#page-title").textContent = state.view === "query" ? "对外查询" : "批处理";
  $("#page-subtitle").textContent = state.view === "query"
    ? "查询只读取已发布的完整版本。发布过程中的暂存版本对外不可见。"
    : "按文件到达顺序暂存候选数据，封口后统一校验，全部通过才发布。";

  $$(".nav-item").forEach(btn => btn.classList.toggle("is-active", btn.dataset.view === state.view));

  const latest = state.releaseInfo;
  $("#release-badge").innerHTML = latest
    ? `<span class="pill pill-ok">对外版本 R${latest.id}</span>
       <span class="muted small">${esc(latest.label)}</span>`
    : "";

  renderBatchList();
}

function renderBatchList() {
  const list = $("#batch-list");
  if (!state.batches.length) {
    list.innerHTML = `<div class="empty-hint">暂无批次<br>新建批次或载入样例</div>`;
    return;
  }
  list.innerHTML = state.batches.map(b => {
    const active = state.batch?.id === b.id;
    const status = BATCH_STATUS[b.status] || { text: b.status, cls: "pill-neutral" };
    return `<button class="batch-item ${active ? "is-active" : ""}" data-action="select-batch" data-id="${esc(b.id)}">
      <div class="batch-item-name"><span>${esc(b.label)}</span>${b.is_sample ? '<span class="pill pill-mono">样例</span>' : ""}</div>
      <div class="batch-item-meta">
        <span>${b.file_count}/3 文件</span>
        <span>·</span>
        <span>${b.candidate_rows} 行</span>
        <span class="right pill ${status.cls}">${esc(status.text)}</span>
      </div>
    </button>`;
  }).join("");
}

function renderSteps() {
  const b = state.batch;
  const fileCount = Object.keys(b.files || {}).length;
  const seal = b.latest_seal;
  const hasRows = b.candidate_rows > 0 || Object.values(b.files || {}).some(f => f.row_count > 0);
  const validSeal = seal && seal.valid && b.seal_matches;

  const steps = [
    { key: "upload", label: "上传", state: `${fileCount}/3 文件`, done: fileCount === 3 },
    { key: "preview", label: "候选预览", state: fileCount ? "查看暂存行" : "待上传", done: fileCount > 0 },
    { key: "seal", label: "封口校验", state: !seal ? "未封口" : validSeal ? `0 错误` : `${seal.error_count} 错误`, done: !!validSeal, blocked: !!seal && !seal.valid },
    { key: "publish", label: "发布", state: b.published_release_id ? `已发布 R${b.published_release_id}` : validSeal ? "可发布" : "待校验", done: !!b.published_release_id },
  ];

  const active = state.step;
  return `<div class="stepper">${steps.map((s, i) => `
    <button class="step ${s.key === active ? "is-active" : ""} ${s.done && s.key !== active ? "is-done" : ""} ${s.blocked && s.key !== active ? "is-blocked" : ""}"
      data-action="goto-step" data-step="${s.key}">
      <span class="step-num">${s.done && s.key !== active ? "✓" : i + 1}</span>
      <span class="step-text">
        <span class="step-label">${s.label}</span>
        <span class="step-state">${esc(s.state)}</span>
      </span>
    </button>`).join("")}</div>`;
}

function renderBatchHeader() {
  const b = state.batch;
  const seal = b.latest_seal;
  const release = b.published_release_id
    ? (b.batch_releases || []).find(r => r.id === b.published_release_id)
    : null;
  const intro = b.status === "published"
    ? `本批次已作为 R${b.published_release_id} 发布；对外查询读取该完整版本。`
    : seal
      ? (seal.valid
        ? (b.seal_matches ? "封口校验全部通过，可以发布。" : "封口后候选文件已变更，需要重新封口。")
        : "封口校验未通过，发布已阻止。修订文件后重新封口即可。")
      : "文件可任意顺序到达；外键与主键唯一性在封口时统一核对。";

  const stats = seal ? `
    <div class="stats">
      <div class="stat"><span class="k">校验错误</span><span class="v ${seal.error_count ? "is-bad" : "is-ok"}">${seal.error_count}</span></div>
      <div class="stat"><span class="k">警告</span><span class="v ${seal.warning_count ? "is-warn" : ""}">${seal.warning_count}</span></div>
      <div class="stat"><span class="k">封口时间</span><span class="v" style="font-size:13px;font-weight:550">${esc(timeAgo(seal.created_at))}</span></div>
      <div class="stat"><span class="k">封口版本</span><span class="v mono" style="font-size:12px;font-weight:500">${esc(seal.id.slice(0, 10))}</span></div>
      ${release ? `<div class="stat"><span class="k">已发布</span><span class="v is-ok" style="font-size:13px">R${release.id}</span></div>` : ""}
    </div>` : "";

  return `<div class="card">
    <div class="card-head">
      <div>
        <div class="inline"><h2>${esc(b.label)}</h2>${statusPill(b.status)}${b.is_sample ? '<span class="pill pill-mono">样例</span>' : ""}</div>
        <p>${esc(intro)}</p>
      </div>
      <div class="inline">
        ${b.latest_seal ? `<a class="btn btn-sm" href="/api/batches/${encodeURIComponent(b.id)}/rejections.csv?seal_id=${encodeURIComponent(b.latest_seal.id)}" download>⤓ 拒绝原因 CSV</a>` : ""}
        <button class="btn btn-sm" data-action="refresh">↻ 刷新</button>
      </div>
    </div>
    ${stats}
  </div>`;
}

// ---------------------------------------------------------------- step: upload
function renderUploadStep() {
  const b = state.batch;
  return `<div class="card">
    <div class="card-head">
      <div><h2>候选文件上传</h2><p>可选择任意顺序上传，也可反复覆盖同一类型文件。每次覆盖都会生成新版本，旧版本仅作审计保留。</p></div>
    </div>
    <div class="card-body">
      <div class="upload-grid">
        ${KINDS.map(kind => renderUploadCard(kind, b.files?.[kind])).join("")}
      </div>
      <div style="margin-top:14px" class="callout callout-info">
        <span class="callout-icon">ⓘ</span>
        <div>
          <strong>为什么暂不报外键错误？</strong>
          <p>批次内文件允许任意顺序到达。上传只做逐行的格式检查；主键唯一、跨表引用与合同金额上限统一在“封口校验”阶段核对，避免因到达顺序产生误报。</p>
        </div>
      </div>
    </div>
  </div>`;
}

function renderUploadCard(kind, file) {
  const meta = KIND_META[kind];
  const key = `upload:${kind}`;
  return `<div class="upload-card" data-drop-kind="${kind}">
    <div class="upload-card-head">
      <div class="upload-card-title"><span class="upload-icon" aria-hidden="true">${meta.icon}</span>${meta.label}</div>
      ${file ? `<span class="pill pill-info">v${file.version}</span>` : `<span class="pill pill-neutral">缺失</span>`}
    </div>
    <div class="upload-desc mono">${esc(meta.desc)}</div>
    ${file ? `
      <div class="upload-current">
        <div class="filename">${esc(file.filename)}</div>
        <div class="meta">${file.row_count} 行 · ${esc(timeAgo(file.uploaded_at))} · ${esc(file.content_hash.slice(0, 8))}</div>
      </div>` : `<div class="upload-current muted small">尚未上传</div>`}
    <div class="upload-actions">
      <label class="btn btn-sm btn-primary" style="flex:1;cursor:pointer">
        ${file ? "覆盖为新版本" : "选择 CSV"}
        <input type="file" accept=".csv,text/csv" data-upload-kind="${kind}" data-busy-key="${key}">
      </label>
    </div>
    <div class="upload-actions">
      <button class="btn btn-sm btn-ghost" data-action="load-sample-file" data-kind="${kind}" data-scenario="initial">载入混乱版样例</button>
      <button class="btn btn-sm btn-ghost" data-action="load-sample-file" data-kind="${kind}" data-scenario="fixed">载入修正版样例</button>
    </div>
    <div class="dropzone" data-dropzone="${kind}">
      <strong>拖放 CSV 到此</strong>
      <span>或点击选择 · UTF-8 · 最大 512 KiB</span>
    </div>
  </div>`;
}

// --------------------------------------------------------------- step: preview
function renderPreviewStep() {
  const b = state.batch;
  const fileCount = Object.keys(b.files || {}).length;
  if (!fileCount) {
    return `<div class="card"><div class="empty-state">
      <div class="icon">⇥</div>
      <h3>还没有候选数据</h3>
      <p>先上传至少一份 CSV，预览会展示暂存行与逐行校验结果。跨表检查在封口时才执行。</p>
      <div class="actions"><button class="btn btn-primary" data-action="goto-step" data-step="upload">去上传</button></div>
    </div></div>`;
  }

  const current = state.preview[state.previewKind];
  const tabs = KINDS.map(kind => {
    const file = b.files?.[kind];
    return `<button class="kind-tab ${state.previewKind === kind ? "is-active" : ""}" data-action="preview-kind" data-kind="${kind}">
      ${KIND_META[kind].label}<span class="count">${file ? file.row_count : 0}</span>
    </button>`;
  }).join("");

  return `<div class="card">
    <div class="card-head">
      <div><h2>候选预览</h2><p>这里展示的是批次内的暂存版本，对外不可见。带红色底纹的行存在结构或格式错误。</p></div>
      <div class="kind-tabs">${tabs}</div>
    </div>
    ${renderFileStrip(current)}
    <div class="table-wrap">${renderCandidateTable(current)}</div>
  </div>`;
}

function renderFileStrip(preview) {
  if (!preview?.file) {
    return `<div class="file-strip muted">该类型尚未上传文件。</div>`;
  }
  const f = preview.file;
  const issues = preview.file_issues || [];
  const errors = issues.filter(i => i.severity === "error").length;
  const warnings = issues.filter(i => i.severity === "warning").length;
  return `<div class="file-strip">
    <span class="mono">${esc(f.filename)}</span>
    <span class="pill pill-info">v${f.version}</span>
    <span class="muted">${preview.rows.length} 行</span>
    ${errors ? `<span class="pill pill-danger">${errors} 错误</span>` : `<span class="pill pill-ok">无错误</span>`}
    ${warnings ? `<span class="pill pill-warn">${warnings} 警告</span>` : ""}
    <span class="right muted mono">${esc(f.content_hash.slice(0, 12))}</span>
  </div>`;
}

function candidateColumns(kind) {
  return {
    customers: ["customer_id", "name", "region", "contact_email"],
    contracts: ["contract_id", "customer_id", "title", "amount", "signed_on"],
    invoices: ["invoice_id", "contract_id", "amount", "issued_on"],
  }[kind];
}

function renderCandidateTable(preview) {
  if (!preview?.rows?.length) {
    return `<div class="issue-empty">该文件没有可展示的暂存行。</div>`;
  }
  const cols = candidateColumns(preview.kind);
  const rows = preview.rows;
  const badCount = rows.filter(r => r.issues?.some(i => i.severity === "error")).length;
  return `<table class="data">
    <thead><tr>
      <th style="width:64px">行号</th>
      ${cols.map(c => `<th>${esc(c)}</th>`).join("")}
      <th style="width:150px">状态</th>
    </tr></thead>
    <tbody>
      ${rows.map(row => {
        const errors = (row.issues || []).filter(i => i.severity === "error");
        const warnings = (row.issues || []).filter(i => i.severity === "warning");
        const moneyCols = new Set(["amount"]);
        return `<tr class="${errors.length ? "row-bad" : warnings.length ? "row-warn" : ""}">
          <td class="cell-line">${row.line_no}</td>
          ${cols.map(col => {
            const val = row.data?.[col] ?? row.raw?.[col] ?? "";
            const bad = errors.some(i => i.field === col);
            const moneyCol = moneyCols.has(col) && col === "amount" && preview.kind !== "customers";
            return `<td class="${bad ? "tiny-error" : ""} ${moneyCol ? "cell-money" : ""}">${
              moneyCol && row.data?.amount_cents != null ? esc(money(row.data.amount_cents)) : esc(val)
            }</td>`;
          }).join("")}
          <td>
            ${errors.length ? `<span class="pill pill-danger">${errors.length} 错误</span>`
              : warnings.length ? `<span class="pill pill-warn">${warnings.length} 警告</span>`
              : `<span class="pill pill-ok">结构正常</span>`}
            <div class="row-badges">${errors.slice(0, 2).map(i => `<span class="pill pill-danger pill-mono" title="${esc(i.message)}">${esc(i.rule)}</span>`).join("")}</div>
          </td>
        </tr>`;
      }).join("")}
    </tbody>
    <tfoot><tr><td colspan="${cols.length + 2}" class="muted small" style="padding:9px 12px">
      共 ${rows.length} 行，其中 ${badCount} 行存在结构错误。跨表规则将在封口阶段统一计算。
    </td></tr></tfoot>
  </table>`;
}

// ----------------------------------------------------------------- step: seal
function renderSealStep() {
  const b = state.batch;
  const seal = b.latest_seal;
  const files = Object.keys(b.files || {}).length;
  const missing = KINDS.filter(k => !b.files?.[k]);
  const sealedButStale = seal && !b.seal_matches;

  const actionCard = `<div class="card">
    <div class="card-head">
      <div><h2>封口校验</h2><p>封口会一次性核对主键唯一、跨表引用与合同金额上限。同一批候选重复封口不会产生新版本。</p></div>
      <div class="inline">
        <button class="btn btn-primary" data-action="seal" data-busy-key="seal" ${files === 0 ? "data-disabled='true'" : ""}>${seal ? "重新封口" : "封口并校验"}</button>
      </div>
    </div>
    <div class="card-body">
      ${missing.length ? `<div class="callout callout-warn">
        <span class="callout-icon">⚠</span>
        <div><strong>还缺 ${missing.length} 类文件</strong>
        <p>${missing.map(k => KIND_META[k].label).join("、")} 尚未上传。当前封口会记录“文件缺失”错误，发布将被阻止。</p></div>
      </div>` : `<div class="callout callout-ok">
        <span class="callout-icon">✓</span>
        <div><strong>三类文件齐备</strong><p>可以封口。封口只读取当前候选版本，覆盖上传后需重新封口。</p></div>
      </div>`}
      ${sealedButStale ? `<div class="callout callout-warn">
        <span class="callout-icon">↻</span>
        <div><strong>封口已过期</strong><p>封口之后又有候选文件被修订。当前封口结果仅作历史记录，发布前必须重新封口。</p></div>
      </div>` : ""}
    </div>
  </div>`;

  if (!seal) {
    return `${actionCard}
    <div class="card"><div class="empty-state">
      <div class="icon">▣</div>
      <h3>尚未封口</h3>
      <p>上传三类 CSV 后点击“封口并校验”。封口之前不会检查外键与金额，因此候选文件可以任意顺序到达。</p>
    </div></div>`;
  }

  const errors = seal.issues.filter(i => i.severity === "error");
  const warnings = seal.issues.filter(i => i.severity === "warning");
  const cross = seal.issues.filter(i => ruleGroup(i.rule).startsWith("跨表"));
  const fileLevel = seal.issues.filter(i => !ruleGroup(i.rule).startsWith("跨表"));
  const filtered = filterIssues(seal.issues);

  const verdict = seal.valid
    ? `<div class="callout callout-ok">
        <span class="callout-icon">✓</span>
        <div><strong>全部约束通过</strong><p>主键唯一、跨表引用、金额上限均无错误。${warnings.length ? `另有 ${warnings.length} 条警告不阻止发布。` : ""}</p></div>
      </div>`
    : `<div class="callout callout-danger">
        <span class="callout-icon">✕</span>
        <div><strong>${errors.length} 条错误阻止发布</strong><p>修订对应文件后重新封口。对外查询仍会读取上一个完整版本。</p></div>
      </div>`;

  return `${actionCard}
  <div class="card">
    <div class="card-head">
      <div><h2>约束明细</h2><p>定位到文件与行号。${sealedButStale ? "（这是过期封口的结果）" : ""}</p></div>
      <a class="btn btn-sm" href="/api/batches/${encodeURIComponent(b.id)}/rejections.csv?seal_id=${encodeURIComponent(seal.id)}" download>⤓ 下载拒绝原因</a>
    </div>
    <div class="card-body" style="padding-bottom:0">${verdict}</div>
    <div class="issue-toolbar">
      <div class="filter-chips">
        ${[["all", `全部 ${seal.issue_count}`], ["error", `错误 ${errors.length}`], ["warning", `警告 ${warnings.length}`]].map(([k, label]) =>
          `<button class="chip ${state.fileFilter === k ? "is-active" : ""}" data-action="issue-filter" data-filter="${k}">${esc(label)}</button>`).join("")}
      </div>
      <div class="filter-chips">
        ${[["all", "全部范围"], ["cross", `跨表 ${cross.length}`], ["file", `文件级 ${fileLevel.length}`]].map(([k, label]) =>
          `<button class="chip ${state.issueScope === k ? "is-active" : ""}" data-action="issue-scope" data-scope="${k}">${esc(label)}</button>`).join("")}
      </div>
      <div class="spacer"></div>
      <span class="muted small">封口 ${esc(seal.id.slice(0, 12))} · ${esc(fmtTime(seal.created_at))}</span>
    </div>
    ${filtered.length ? `<div class="issue-list">${filtered.map(renderIssue).join("")}</div>` : `<div class="issue-empty">当前筛选下没有记录。</div>`}
  </div>${renderSealHistory()}`;
}

function filterIssues(issues) {
  return issues.filter(i => {
    if (state.fileFilter !== "all" && i.severity !== state.fileFilter) return false;
    if (state.issueScope === "cross" && !ruleGroup(i.rule).startsWith("跨表")) return false;
    if (state.issueScope === "file" && ruleGroup(i.rule).startsWith("跨表")) return false;
    return true;
  });
}

function renderIssue(issue) {
  const loc = issue.filename
    ? `${esc(issue.filename)}${issue.line_no != null ? `:${issue.line_no}` : ""}`
    : "批次级";
  const related = issue.related?.first_file
    ? `首次出现：${esc(issue.related.first_file)}:${issue.related.first_line}`
    : issue.related?.invoices?.length
      ? `涉及发票：${issue.related.invoices.map(v => `${esc(v.filename)}:${v.line_no}`).join("，")}`
      : "";
  return `<div class="issue ${issue.severity === "error" ? "is-error" : "is-warning"}">
    <span class="severity-bar"></span>
    <div class="issue-main">
      <div class="issue-message">${esc(issue.message)}</div>
      <div class="issue-meta">
        <span class="pill ${issue.severity === "error" ? "pill-danger" : "pill-warn"}">${severityLabel(issue.severity)}</span>
        <span class="pill pill-neutral">${esc(ruleGroup(issue.rule))}</span>
        <span class="issue-loc">${loc}</span>
        ${issue.field ? `<span>字段 <span class="mono">${esc(issue.field)}</span></span>` : ""}
        ${related ? `<span>· ${related}</span>` : ""}
        <span class="issue-rule">${esc(issue.rule)}</span>
      </div>
    </div>
    <div class="issue-actions">
      ${issue.kind ? `<button class="btn btn-sm btn-ghost" data-action="revise-kind" data-kind="${issue.kind}">修订${kindLabel(issue.kind)}</button>` : ""}
    </div>
  </div>`;
}

function renderSealHistory() {
  const seals = (state.batch.seals || []).filter(s => s.id !== state.batch.latest_seal?.id);
  if (!seals.length) return "";
  return `<div class="card">
    <div class="card-head"><div><h2>历史封口</h2><p>修订文件会生成新的封口；旧封口保留用于审计与下载。</p></div></div>
    <div class="card-body" style="padding:12px 18px">
      <div class="version-list">
        ${seals.map(s => `<div class="version-item">
          <span class="mono">${esc(s.id.slice(0, 12))}</span>
          <span class="muted">${esc(fmtTime(s.created_at))}</span>
          <span class="pill ${s.valid ? "pill-ok" : "pill-danger"}">${s.valid ? "通过" : `${s.error_count} 错误`}</span>
          <span class="right"><a class="btn-link" href="/api/batches/${encodeURIComponent(state.batch.id)}/rejections.csv?seal_id=${encodeURIComponent(s.id)}" download>下载拒绝原因</a></span>
        </div>`).join("")}
      </div>
    </div>
  </div>`;
}

// -------------------------------------------------------------- step: publish
function renderPublishStep() {
  const b = state.batch;
  const seal = b.latest_seal;
  const blockers = [];
  if (!seal) blockers.push("尚未封口。");
  else if (!b.seal_matches) blockers.push("候选文件在封口后被修订，需要重新封口。");
  else if (!seal.valid) blockers.push(`封口未通过（${seal.error_count} 条错误）。`);
  const pendingOp = (b.ops || []).find(op => op.status === "running" || op.status === "interrupted");
  if (pendingOp) blockers.push("存在未完成的发布操作，需要先恢复或丢弃。");
  const staged = (b.batch_releases || []).find(r => r.state === "staged");
  if (staged && !pendingOp) blockers.push(`存在待处理暂存版本 R${staged.id}。`);

  const canPublish = blockers.length === 0;
  const latest = (b.batch_releases || []).find(r => r.id === b.current_release?.id);

  const readiness = blockers.length
    ? `<div class="callout callout-warn"><span class="callout-icon">⏸</span><div><strong>当前不可发布</strong><p>${blockers.map(esc).join("<br>")}</p></div></div>`
    : `<div class="callout callout-ok"><span class="callout-icon">✓</span><div><strong>发布就绪</strong><p>${seal.summary?.valid_counts ? `将写入 客户 ${seal.summary.valid_counts.customers} · 合同 ${seal.summary.valid_counts.contracts} · 发票 ${seal.summary.valid_counts.invoices} 条记录。` : ""}先落暂存，再原子切换对外版本指针。</p></div></div>`;

  return `<div class="card">
    <div class="card-head">
      <div><h2>发布</h2><p>两阶段发布：完整写入暂存版本 → 切换版本指针。任一步中断，对外查询继续读取上一个完整版本。</p></div>
      <div class="inline">
        <select id="interrupt-mode" aria-label="中断模拟">
          <option value="none">不模拟中断</option>
          <option value="after_stage">模拟：暂存完成后中断</option>
          <option value="mid_copy">模拟：复制到一半中断</option>
        </select>
        <button class="btn btn-primary" data-action="publish" data-busy-key="publish" ${!canPublish ? "data-disabled='true'" : ""}>发布到对外版本</button>
      </div>
    </div>
    <div class="card-body stack">
      ${readiness}
      ${renderPointerSummary()}
    </div>
  </div>
  ${renderOps()}
  ${renderPublishHistory()}`;
}

function renderPointerSummary() {
  const releases = state.batch?.batch_releases || [];
  const current = state.batch?.current_release;
  const sealed = releases.find(r => r.state === "active" && r.batch_id === state.batch.id);
  return `<div class="grid-2">
    <div class="revise-note">
      <div class="section-title" style="margin-bottom:6px"><h3>对外当前版本</h3></div>
      <div class="inline">
        <span class="pill pill-ok">R${current?.id ?? 0}</span>
        <span>${esc(current?.label || "基线空版本")}</span>
      </div>
      <div class="muted small" style="margin-top:6px">
        客户 ${current?.customer_count ?? 0} · 合同 ${current?.contract_count ?? 0} · 发票 ${current?.invoice_count ?? 0}
        ${current?.activated_at ? ` · ${esc(timeAgo(current.activated_at))}发布` : ""}
      </div>
    </div>
    <div class="revise-note">
      <div class="section-title" style="margin-bottom:6px"><h3>本批次发布记录</h3></div>
      ${sealed ? `<div class="inline"><span class="pill pill-ok">R${sealed.id}</span><span class="muted small">${esc(fmtTime(sealed.activated_at))}</span></div>`
        : `<span class="muted small">本批次尚未对外发布。</span>`}
      ${(state.batch?.batch_releases || []).filter(r => r.state === "staged").map(r =>
        `<div class="inline" style="margin-top:4px"><span class="pill pill-warn">R${r.id} 暂存</span><span class="muted small">指针未切换</span></div>`).join("")}
      ${(state.batch?.batch_releases || []).filter(r => r.state === "discarded").map(r =>
        `<div class="inline" style="margin-top:4px"><span class="pill pill-neutral">R${r.id} 已丢弃</span></div>`).join("")}
    </div>
  </div>`;
}

function renderOps() {
  const ops = (state.batch?.ops || []).filter(op => op.status !== "completed" || op === state.batch.ops[0]);
  if (!ops.length) return `<div class="card"><div class="empty-state">
    <div class="icon">⤴</div><h3>还没有发布操作</h3>
    <p>封口校验全部通过后，点击“发布到对外版本”。也可以在发布前选择模拟中断，演练恢复流程。</p>
  </div></div>`;

  return `<div class="card">
    <div class="card-head"><div><h2>发布操作</h2><p>中断的发布保留完整暂存版本；可恢复切换指针，或丢弃暂存数据。</p></div></div>
    <div>${ops.map(renderOp).join("")}</div>
  </div>`;
}

function renderOp(op) {
  const statusMeta = {
    running: { text: "进行中", cls: "pill-info", phases: ["copying", "staged", "activated"] },
    interrupted: { text: "已中断", cls: "pill-warn", phases: ["copying", "staged", "activated"] },
    completed: { text: "已完成", cls: "pill-ok", phases: ["copying", "staged", "activated"] },
    discarded: { text: "已丢弃", cls: "pill-neutral", phases: ["copying", "staged", "activated"] },
  }[op.status] || { text: op.status, cls: "pill-neutral", phases: [] };

  const phaseIdx = { copying: 0, staged: 1, activated: 2 }[op.phase] ?? -1;
  const isDone = op.status === "completed";
  const isFail = op.status === "interrupted";

  return `<div class="op-row">
    <span class="pill ${statusMeta.cls}">${statusMeta.text}</span>
    <div class="op-main">
      <div class="op-title">
        <span class="mono">${esc(op.id.slice(0, 14))}</span>
        <span class="muted">${esc(fmtTime(op.started_at))}</span>
        ${op.release_id ? `<span class="pill pill-neutral pill-mono">R${op.release_id}</span>` : ""}
        ${op.fault ? `<span class="pill pill-info pill-mono">故障=${esc(op.fault)}</span>` : ""}
      </div>
      <div class="op-sub">${esc(opSubtitle(op))}</div>      <div class="phase-track">
        ${statusMeta.phases.map((p, i) => {
          const cls = isDone ? "is-done"
            : isFail && i === phaseIdx ? "is-fail"
            : i < phaseIdx ? "is-done"
            : i === phaseIdx ? "is-current" : "";
          return `<div class="phase ${cls}" title="${p}"></div>`;
        }).join("")}
      </div>
    </div>
    <div class="op-actions">
      ${op.status === "interrupted" ? `
        <button class="btn btn-sm btn-primary" data-action="recover-op" data-op="${esc(op.id)}" data-mode="resume" data-busy-key="op:${esc(op.id)}">恢复发布</button>
        <button class="btn btn-sm btn-danger" data-action="recover-op" data-op="${esc(op.id)}" data-mode="discard" data-busy-key="op:${esc(op.id)}">丢弃暂存</button>` : ""}
      ${op.status === "running" ? `<button class="btn btn-sm btn-danger" data-action="recover-op" data-op="${esc(op.id)}" data-mode="discard" data-busy-key="op:${esc(op.id)}">中止并丢弃</button>` : ""}
    </div>
  </div>`;
}

function opSubtitle(op) {
  const note = op.note || {};
  if (op.status === "completed") {
    const c = { c: op.release_id ? "版本指针已切换" : "" };
    return `已完成发布并切换对外版本指针 ${op.release_id ? `R${op.release_id}` : ""}`.trim();
  }
  if (op.status === "discarded") return "暂存数据已清除，对外版本未受影响。";
  if (op.status === "interrupted") {
    const expected = note.expected ? `应有 ${note.expected.customers}/${note.expected.contracts}/${note.expected.invoices}` : "";
    const copied = note.copied ? `已暂存 ${note.copied.customers}/${note.copied.contracts}/${note.copied.invoices}` : "";
    const phaseText = { copying: "复制阶段中断", staged: "暂存完成、指针尚未切换", stale: "候选数据已变化", unknown: "进程异常退出" }[op.phase] || op.phase;
    const parts = [phaseText, expected, copied].filter(Boolean);
    if (note.partial) parts.push("数据不完整，只能丢弃");
    return parts.join(" · ");
  }
  return op.phase || "";
}

function renderPublishHistory() {
  const releases = (state.batch?.batch_releases || []).filter(r => r.state !== "staged");
  if (!releases.length) return "";
  return `<div class="card">
    <div class="card-head"><div><h2>版本历史（本批次）</h2><p>每个已发布版本都是完整快照，可继续被对外查询读取。</p></div></div>
    <div class="table-wrap"><table class="data">
      <thead><tr><th>版本</th><th>状态</th><th>客户</th><th>合同</th><th>发票</th><th>创建</th><th>生效</th></tr></thead>
      <tbody>${releases.map(r => `<tr>
        <td class="cell-id">R${r.id}</td>
        <td>${r.state === "active" ? '<span class="pill pill-ok">对外生效</span>' : r.state === "superseded" ? '<span class="pill pill-neutral">已被取代</span>' : `<span class="pill pill-warn">${esc(r.state)}</span>`}</td>
        <td>${r.customer_count}</td><td>${r.contract_count}</td><td>${r.invoice_count}</td>
        <td class="muted small">${esc(fmtTime(r.created_at))}</td>
        <td class="muted small">${esc(r.activated_at ? fmtTime(r.activated_at) : "—")}</td>
      </tr>`).join("")}</tbody>
    </table></div>
  </div>`;
}

// -------------------------------------------------------------- view: query
async function loadQuery() {
  const q = state.query;
  const params = new URLSearchParams({ kind: q.kind, limit: String(q.limit), offset: String(q.offset) });
  if (q.search) params.set("search", q.search);
  const path = q.releaseId ? `/api/releases/${q.releaseId}` : "/api/releases/latest";
  q.data = await api(`${path}?${params}`);
}

function renderQueryView() {
  const q = state.query;
  const d = q.data;
  if (!d) return `<div class="skeleton" style="height:180px"></div>`;
  const totalPages = Math.max(1, Math.ceil(d.total / d.limit));
  const page = Math.floor(d.offset / d.limit) + 1;
  const rows = d.rows;
  const cols = {
    customers: ["customer_id", "name", "region", "contact_email"],
    contracts: ["contract_id", "customer_id", "title", "amount_cents", "signed_on"],
    invoices: ["invoice_id", "contract_id", "amount_cents", "issued_on"],
  }[q.kind];

  return `<div class="card">
    <div class="card-head">
      <div>
        <div class="inline"><h2>只读查询</h2><span class="pill pill-ok">R${d.release.id} · ${esc(d.release.label)}</span>${d.is_latest ? '<span class="pill pill-info">最新完整版本</span>' : '<span class="pill pill-neutral">历史版本</span>'}</div>
        <p>此处只读取已发布快照；候选修订、暂存版本不会出现在查询结果中。</p>
      </div>
    </div>
    <div class="toolbar-row">
      <div class="seg">
        ${KINDS.map(k => `<button class="${q.kind === k ? "is-active" : ""}" data-action="query-kind" data-kind="${k}">${KIND_META[k].label}</button>`).join("")}
      </div>
      <input type="search" placeholder="搜索 ID / 名称…" value="${esc(q.search)}" data-query-search>
      <select data-query-release>
        <option value="">最新版本</option>
        ${(state.releases || []).map(r => `<option value="${r.id}" ${String(q.releaseId) === String(r.id) ? "selected" : ""}>R${r.id} · ${esc(r.label)}</option>`).join("")}
      </select>
      <span class="spacer"></span>
      <span class="muted small">${d.total} 条记录 · 第 ${page}/${totalPages} 页</span>
      <button class="btn btn-sm" data-action="query-page" data-dir="-1" ${page <= 1 ? "data-disabled='true'" : ""}>上一页</button>
      <button class="btn btn-sm" data-action="query-page" data-dir="1" ${page >= totalPages ? "data-disabled='true'" : ""}>下一页</button>
    </div>
    <div class="table-wrap">${rows.length ? `<table class="data">
      <thead><tr>${cols.map(c => `<th class="${c === "amount_cents" ? "cell-money" : ""}">${esc(c === "amount_cents" ? "amount" : c)}</th>`).join("")}<th>来源</th></tr></thead>
      <tbody>${rows.map(row => `<tr>
        ${cols.map(c => c === "amount_cents"
          ? `<td class="cell-money">${esc(money(row[c]))}</td>`
          : `<td class="${c.endsWith("_id") ? "cell-id" : ""}">${esc(row[c])}</td>`).join("")}
        <td class="provenance">${esc(row.source_file || "")}:${row.source_line ?? ""}</td>
      </tr>`).join("")}</tbody>
    </table>` : `<div class="issue-empty">没有匹配的记录。</div>`}</div>
  </div>
  <div class="card">
    <div class="card-head"><div><h2>版本列表</h2><p>发布失败不会改变下方的当前版本指针。</p></div></div>
    <div class="table-wrap"><table class="data">
      <thead><tr><th>版本</th><th>批次</th><th>状态</th><th>客户</th><th>合同</th><th>发票</th><th>生效时间</th></tr></thead>
      <tbody>${(state.releases || []).map(r => `<tr class="${r.id === d.release.id ? "row-warn" : ""}">
        <td class="cell-id">R${r.id}</td>
        <td>${esc(r.batch_label || "基线")}</td>
        <td>${r.is_latest ? '<span class="pill pill-ok">对外生效</span>' : '<span class="pill pill-neutral">历史</span>'}</td>
        <td>${r.customer_count}</td><td>${r.contract_count}</td><td>${r.invoice_count}</td>
        <td class="muted small">${esc(r.activated_at ? fmtTime(r.activated_at) : "—")}</td>
      </tr>`).join("")}</tbody>
    </table></div>
  </div>`;
}

// ------------------------------------------------------------------ content
function renderContent() {
  const root = $("#main-content");
  if (state.view === "query") {
    root.innerHTML = renderQueryView();
    return;
  }
  if (!state.batch) {
    root.innerHTML = renderNoBatch();
    return;
  }
  const header = renderBatchHeader();
  const steps = `<div class="card" style="margin-top:18px">${renderSteps()}</div>`;
  let body = "";
  if (state.step === "upload") body = renderUploadStep();
  else if (state.step === "preview") body = renderPreviewStep();
  else if (state.step === "seal") body = renderSealStep();
  else if (state.step === "publish") body = renderPublishStep();
  root.innerHTML = header + steps + body;
  const interruptSelect = $("#interrupt-mode");
  if (interruptSelect) {
    interruptSelect.value = state.interruptMode;
    interruptSelect.addEventListener("change", () => { state.interruptMode = interruptSelect.value; });
  }
}

function renderNoBatch() {
  return `<div class="card"><div class="empty-state">
    <div class="icon">▦</div>
    <h3>先创建一个批次</h3>
    <p>批次是候选数据的隔离容器。文件可按任意顺序上传，封口后统一校验主键、外键与合同金额；全部通过才会切换对外版本。</p>
    <div class="actions">
      <button class="btn btn-primary" data-action="new-batch">新建批次</button>
      <button class="btn" data-action="load-sample" data-scenario="initial">载入混乱样例</button>
    </div>
    <p class="muted small" style="margin-top:8px">样例含循环到达顺序、重复主键与跨表金额超限，全部为虚构数据。</p>
  </div></div>`;
}

function renderAll() {
  renderShell();
  renderContent();
  renderBusy();
}

// ----------------------------------------------------------------- actions
async function refreshBatches({ keepSelection = true } = {}) {
  const [batchData, overview, releaseData] = await Promise.all([
    api("/api/batches"),
    api("/api/overview"),
    api("/api/releases"),
  ]);
  state.batches = batchData.batches;
  state.releaseInfo = overview.latest_release;
  state.releases = releaseData.releases;
  if (keepSelection && state.batch) {
    const exists = state.batches.some(b => b.id === state.batch.id);
    if (!exists) state.batch = null;
  }
  if (!state.batch && state.view === "upload" && state.batches.length) {
    await selectBatch(state.batches[0].id, { silent: true });
    return;
  }
  renderAll();
}

async function selectBatch(batchId, { silent = false } = {}) {
  const batch = await api(`/api/batches/${encodeURIComponent(batchId)}`);
  state.batch = batch;
  state.preview = {};
  state.previewKind = "customers";
  state.step = defaultStep(batch);
  await ensurePreview(state.previewKind);
  renderAll();
  if (!silent) history.replaceState(null, "", `#batch=${batchId}`);
}

function defaultStep(batch) {
  if (batch.published_release_id) return "publish";
  if (batch.latest_seal) return batch.seal_matches && batch.latest_seal.valid ? "publish" : "seal";
  const count = Object.keys(batch.files || {}).length;
  return count ? "preview" : "upload";
}

async function ensurePreview(kind) {
  if (state.preview[kind]) return state.preview[kind];
  const data = await api(`/api/batches/${encodeURIComponent(state.batch.id)}/preview/${kind}`);
  state.preview[kind] = data;
  return data;
}

async function reloadBatch() {
  if (!state.batch) return;
  state.batch = await api(`/api/batches/${encodeURIComponent(state.batch.id)}`);
  state.preview = {};
  await ensurePreview(state.previewKind);
  renderAll();
}

async function doUpload(kind, file) {
  if (!state.batch) { toast("请先创建批次", "error"); return; }
  if (!file) return;
  if (!/\.csv$/i.test(file.name) && file.type !== "text/csv") {
    toast("只接受 .csv 文件", "error");
    return;
  }
  if (file.size > 512 * 1024) {
    toast("文件超过 512 KiB 上限", "error");
    return;
  }
  await busy(`upload:${kind}`, async () => {
    try {
      const text = await file.text();
      const result = await api(`/api/batches/${encodeURIComponent(state.batch.id)}/files`, {
        method: "POST",
        body: { kind, filename: file.name, content: text },
      });
      const replaced = result.replaced_file ? `（替换 v${result.replaced_file.version}）` : "";
      toast(`${KIND_META[kind].label} v${result.version} 已暂存${replaced}`, result.fatal ? "error" : "ok");
      if (result.fatal) toast("文件结构有误，请查看候选预览与封口明细", "error");
      state.batch = await api(`/api/batches/${encodeURIComponent(state.batch.id)}`);
      state.preview[state.previewKind] = await api(`/api/batches/${encodeURIComponent(state.batch.id)}/preview/${state.previewKind}`);
      await refreshBatchListOnly();
      renderAll();
    } catch (err) {
      toast(err.message, "error", "上传失败");
    }
  });
}

async function refreshBatchListOnly() {
  const data = await api("/api/batches");
  state.batches = data.batches;
}

async function loadSampleFile(kind, scenario = "initial") {
  if (!state.batch) return;
  await busy(`sample:${kind}`, async () => {
    try {
      const data = await api(`/api/samples/${scenario}/files/${kind}`);
      await api(`/api/batches/${encodeURIComponent(state.batch.id)}/files`, {
        method: "POST",
        body: { kind, filename: data.filename, content: data.content },
      });
      toast(`已载入 ${KIND_META[kind].label}·${scenario === "fixed" ? "修正版" : "混乱版"}样例文件`, "ok");
      await reloadBatch();
      await refreshBatchListOnly();
      renderAll();
    } catch (err) {
      toast(err.message, "error");
    }
  });
}

async function newBatch() {
  openModal({
    title: "新建批次",
    body: `<label class="field"><span class="small muted">批次名称</span>
      <input type="text" id="new-batch-label" placeholder="例如：2026-11 供应商对账单" maxlength="80"></label>
      <div class="revise-note">新建批次为空。可以任意顺序上传客户、合同、发票 CSV。</div>`,
    confirmText: "创建",
    onConfirm: async close => {
      const label = $("#new-batch-label").value.trim();
      if (!label) { toast("请输入批次名称", "error"); return false; }
      try {
        const batch = await api("/api/batches", { method: "POST", body: { label } });
        close();
        await refreshBatches({ keepSelection: false });
        await selectBatch(batch.id);
        toast("批次已创建", "ok");
      } catch (err) {
        toast(err.message, "error");
        return false;
      }
    },
  });
}

async function loadSample(scenario) {
  await busy(`sample:${scenario}`, async () => {
    try {
      const batch = await api(`/api/samples/${scenario}`, { method: "POST" });
      await refreshBatches({ keepSelection: false });
      await selectBatch(batch.id);
      const order = batch.arrival_order?.map(kindLabel).join(" → ");
      toast(`样例已载入${order ? `（到达顺序：${order}）` : ""}`, "ok", "样例批次");
      state.view = "upload";
      state.step = "seal";
      renderAll();
    } catch (err) {
      toast(err.message, "error", "载入失败");
    }
  });
}

async function doSeal() {
  if (!state.batch) return;
  await busy("seal", async () => {
    try {
      const seal = await api(`/api/batches/${encodeURIComponent(state.batch.id)}/seal`, { method: "POST", body: {} });
      state.batch = await api(`/api/batches/${encodeURIComponent(state.batch.id)}`);
      state.step = "seal";
      await refreshBatchListOnly();
      renderAll();
      if (seal.reused) toast("候选数据未变化，已复用上次封口结果，不会产生重复数据", "ok");
      else if (seal.valid) toast(`封口通过：0 错误 / ${seal.warning_count} 警告`, "ok", "可以发布");
      else toast(`封口未通过：${seal.error_count} 条错误阻止发布`, "error", "需要修订");
    } catch (err) {
      toast(err.message, "error", "封口失败");
    }
  });
}

async function doPublish() {
  if (!state.batch) return;
  const mode = $("#interrupt-mode")?.value || state.interruptMode || "none";
  await busy("publish", async () => {
    try {
      const result = await api(`/api/batches/${encodeURIComponent(state.batch.id)}/publish`, {
        method: "POST",
        body: { interrupt: mode },
      });
      await reloadBatch();
      await refreshBatches({ keepSelection: true });
      let message;
      if (result.status === "published") message = `R${result.release.id} 已发布，对外版本指针已切换`;
      else if (result.status === "already_published") message = `该封口已经发布为 R${result.release.id}，未重复写入`;
      else if (result.status === "interrupted") message = `已模拟中断（${result.op.fault}），暂存 R${result.release.id} 未对外生效`;
      toast(message, result.status === "interrupted" ? "info" : "ok", "发布");
      renderAll();
    } catch (err) {
      await reloadBatch().catch(() => {});
      toast(err.message, "error", "发布被阻止");
    }
  });
}

async function recoverOp(opId, mode) {
  const descriptions = {
    resume: "将把完整暂存版本切换为对外版本。系统会先核对数量、基线与封口指纹。",
    discard: "将删除暂存快照，对外版本保持不变。",
  };
  openModal({
    title: mode === "resume" ? "恢复中断发布" : "丢弃暂存版本",
    tone: mode === "discard" ? "danger" : "primary",
    body: `<div class="revise-note">${esc(descriptions[mode])}</div>
      <div class="small muted">操作：<span class="mono">${esc(opId)}</span></div>`,
    confirmText: mode === "resume" ? "恢复并切换指针" : "丢弃暂存",
    onConfirm: async close => {
      try {
        const result = await api(`/api/batches/${encodeURIComponent(state.batch.id)}/ops/${encodeURIComponent(opId)}`, {
          method: "POST",
          body: { action: mode },
        });
        close();
        await reloadBatch();
        await refreshBatches({ keepSelection: true });
        if (result.status === "published") toast(`已恢复并切换到 R${result.release.id}`, "ok");
        else if (result.status === "discarded") toast("暂存版本已丢弃，对外版本未变", "ok");
        else toast(result.status, "ok");
        renderAll();
      } catch (err) {
        toast(err.message, "error", "恢复失败");
        return false;
      }
    },
  });
}

async function reviseKind(kind) {
  const file = state.batch?.files?.[kind];
  openModal({
    title: `修订${KIND_META[kind].label}文件`,
    body: `<div class="revise-note">修订只会替换本批次的候选版本，不会改动已发布版本。覆盖后需要重新封口。</div>
      <div class="field"><label>当前文件</label><div class="mono">${esc(file?.filename || "尚未上传")} ${file ? `· v${file.version}` : ""}</div></div>
      <label class="field"><span class="small">新文件</span>
        <input type="file" accept=".csv" id="revise-input"></label>
      <div class="muted small">也可以直接拖放或选择文件覆盖。旧版本仍保留在版本历史中用于审计。</div>`,
    confirmText: "上传修订",
    onConfirm: async close => {
      const input = $("#revise-input");
      const picked = input?.files?.[0] || state._reviseDrop;
      state._reviseDrop = null;
      if (!picked) { toast("请选择 CSV 文件", "error"); return false; }
      close();
      await doUpload(kind, picked);
    },
  });
}

function openVersionHistory(kind) {
  if (!state.batch) return;
  api(`/api/batches/${encodeURIComponent(state.batch.id)}/files?kind=${kind}`).then(({ files }) => {
    openModal({
      title: `${KIND_META[kind].label}文件版本`,
      body: files.length ? `<div class="version-list">${files.map(f => `<div class="version-item ${f.is_current ? "is-current" : ""}">
        <span class="pill pill-mono">v${f.version}</span>
        <span class="mono">${esc(f.filename)}</span>
        <span class="muted">${f.row_count} 行 · ${f.issues} 条行级问题</span>
        <span class="right muted small">${f.is_current ? "当前候选" : `已归档 ${esc(fmtTime(f.replaced_at))}`}</span>
      </div>`).join("")}</div>` : `<div class="muted">还没有上传任何版本。</div>`,
      confirmText: "关闭",
      cancelText: "",
      onConfirm: close => close(),
    });
  }).catch(err => toast(err.message, "error"));
}

// -------------------------------------------------------------------- wiring
document.addEventListener("click", async ev => {
  const target = ev.target.closest("[data-action]");
  if (!target) return;
  const action = target.dataset.action;

  if (action === "new-batch") return newBatch();
  if (action === "load-sample") return loadSample(target.dataset.scenario);
  if (action === "refresh") {
    return busy("refresh", async () => {
      try {
        await reloadBatch();
        await refreshBatches({ keepSelection: true });
        toast("已刷新", "ok");
      } catch (err) { toast(err.message, "error"); }
    });
  }
  if (action === "view") {
    state.view = target.dataset.view;
    renderShell();
    if (state.view === "query") {
      try {
        if (!state.query.data) { await loadQuery(); }
        else await loadQuery();
      } catch (err) { toast(err.message, "error"); }
    }
    renderContent();
    renderBusy();
    return;
  }
  if (action === "select-batch") return selectBatch(target.dataset.id);
  if (action === "goto-step") {
    state.step = target.dataset.step;
    if (state.step === "preview") {
      try { await ensurePreview(state.previewKind); } catch (err) { toast(err.message, "error"); }
    }
    renderContent();
    return;
  }
  if (action === "preview-kind") {
    state.previewKind = target.dataset.kind;
    try { await ensurePreview(state.previewKind); } catch (err) { toast(err.message, "error"); }
    renderContent();
    return;
  }
  if (action === "seal") return doSeal();
  if (action === "publish") {
    if (target.dataset.disabled === "true") return;
    return doPublish();
  }
  if (action === "issue-filter") { state.fileFilter = target.dataset.filter; return renderContent(); }
  if (action === "issue-scope") { state.issueScope = target.dataset.scope; return renderContent(); }
  if (action === "revise-kind") return reviseKind(target.dataset.kind);
  if (action === "load-sample-file") return loadSampleFile(target.dataset.kind, target.dataset.scenario);
  if (action === "recover-op") return recoverOp(target.dataset.op, target.dataset.mode);
  if (action === "query-kind") {
    state.query.kind = target.dataset.kind;
    state.query.offset = 0;
    await loadQuery();
    return renderContent();
  }
  if (action === "query-page") {
    const dir = Number(target.dataset.dir);
    state.query.offset = Math.max(0, state.query.offset + dir * state.query.limit);
    await loadQuery();
    return renderContent();
  }
  if (action === "version-history") return openVersionHistory(target.dataset.kind);
});

document.addEventListener("change", async ev => {
  const fileInput = ev.target.closest("[data-upload-kind]");
  if (fileInput) {
    const kind = fileInput.dataset.uploadKind;
    const file = fileInput.files?.[0];
    fileInput.value = "";
    return doUpload(kind, file);
  }
  if (ev.target.matches("[data-query-release]")) {
    state.query.releaseId = ev.target.value;
    state.query.offset = 0;
    try { await loadQuery(); renderContent(); } catch (err) { toast(err.message, "error"); }
    return;
  }
  if (ev.target.matches("[data-query-search]")) {
    state.query.search = ev.target.value.trim();
    state.query.offset = 0;
    try { await loadQuery(); renderContent(); } catch (err) { toast(err.message, "error"); }
  }
});

document.addEventListener("input", ev => {
  if (ev.target.matches("[data-query-search]")) {
    clearTimeout(state._searchTimer);
    const value = ev.target.value.trim();
    state._searchTimer = setTimeout(async () => {
      state.query.search = value;
      state.query.offset = 0;
      try { await loadQuery(); renderContent(); } catch (err) { toast(err.message, "error"); }
    }, 260);
  }
});

// Drag & drop onto upload cards
document.addEventListener("dragover", ev => {
  const zone = ev.target.closest("[data-drop-kind]");
  if (!zone) return;
  ev.preventDefault();
  zone.classList.add("is-dragging");
});

document.addEventListener("dragleave", ev => {
  const zone = ev.target.closest("[data-drop-kind]");
  if (zone) zone.classList.remove("is-dragging");
});

document.addEventListener("drop", ev => {
  const zone = ev.target.closest("[data-drop-kind]");
  if (!zone) return;
  ev.preventDefault();
  zone.classList.remove("is-dragging");
  const kind = zone.dataset.dropKind;
  const file = ev.dataTransfer?.files?.[0];
  if (file) doUpload(kind, file);
});

// Drop onto revise modal
document.addEventListener("dragover", ev => {
  if ($("#revise-input") && ev.target.closest(".modal")) ev.preventDefault();
});
document.addEventListener("drop", ev => {
  if (!$("#revise-input") || !ev.target.closest(".modal")) return;
  ev.preventDefault();
  const file = ev.dataTransfer?.files?.[0];
  if (file) {
    state._reviseDrop = file;
    toast(`已选择 ${file.name}，点击“上传修订”继续`, "info");
  }
});

// --------------------------------------------------------------------- boot
(async function boot() {
  try {
    await refreshBatches({ keepSelection: false });
    const hash = location.hash.match(/batch=([A-Za-z0-9_-]+)/);
    if (hash) {
      try { await selectBatch(hash[1], { silent: true }); return; } catch { /* fall through */ }
    }
    const hasBatches = state.batches.length > 0;
    if (hasBatches) {
      await selectBatch(state.batches[0].id, { silent: true });
    } else {
      renderAll();
    }
  } catch (err) {
    $("#main-content").innerHTML = `<div class="card"><div class="empty-state">
      <div class="icon">⚠</div><h3>无法连接本地服务</h3>
      <p>${esc(err.message)}</p>
      <p class="small muted">请确认已运行 <span class="kbd">python3 -m server.app</span></p>
    </div></div>`;
  }
})();
