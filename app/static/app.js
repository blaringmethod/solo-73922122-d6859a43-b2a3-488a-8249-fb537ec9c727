"use strict";

const form = document.getElementById("backtestForm");
const submitBtn = document.getElementById("submitBtn");
const cutoffInput = document.getElementById("cutoff");
const resultCard = document.getElementById("resultCard");
const resultTitle = document.getElementById("resultTitle");
const resultBadge = document.getElementById("resultBadge");
const errorCard = document.getElementById("errorCard");
const errorTitle = document.getElementById("errorTitle");
const errorList = document.getElementById("errorList");
const resultBody = document.getElementById("resultBody");
const detailCard = document.getElementById("detailCard");
const detailBadge = document.getElementById("detailBadge");
const detailBody = document.getElementById("detailBody");
const historyBody = document.getElementById("historyBody");
const toast = document.getElementById("toast");
const serviceStatus = document.getElementById("serviceStatus");
const fullUploads = document.getElementById("fullUploads");
const recoveredUploads = document.getElementById("recoveredUploads");
const formNote = document.getElementById("formNote");
const recoverNote = document.getElementById("recoverNote");
const cutoffHint = document.getElementById("cutoffHint");
const demoBtn = document.getElementById("demoBtn");

let toastTimer = null;
let currentMode = "full";

const MODE_COPY = {
  full: {
    submit: "校验并运行回测",
    busy: "校验与计算中…",
    cutoffHint: "只使用 issue_time ≤ 截止时间的最新一版预测。",
  },
  recovered: {
    submit: "校验并恢复结果",
    busy: "核对与复算中…",
    cutoffHint: "填写当时那次评估的截止时间；早于截止时间的预测才可能被选中。",
  },
};

function showToast(message, bad = false) {
  toast.textContent = message;
  toast.classList.toggle("bad", bad);
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 3200);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function fmtNumber(value, maxDigits = 6) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString("zh-CN", { maximumFractionDigits: maxDigits });
}

function fmtPercent(value) {
  return `${(Number(value) * 100).toFixed(1)}%`;
}

function fmtTime(iso) {
  return iso ? iso.replace("T", " ").replace("Z", " UTC") : "—";
}

/* ---------------- file inputs & drop zones ---------------- */

function setupDropzone(zone) {
  const input = zone.querySelector('input[type="file"]');
  const fileName = zone.querySelector("[data-file-name]");
  const defaultText = fileName.textContent;

  const update = () => {
    if (input.files && input.files.length > 0) {
      fileName.textContent = `${input.files[0].name} · ${(input.files[0].size / 1024).toFixed(1)} KB`;
      zone.classList.add("has-file");
    } else {
      fileName.textContent = defaultText;
      zone.classList.remove("has-file");
    }
  };

  input.addEventListener("change", update);

  ["dragenter", "dragover"].forEach((eventName) => {
    zone.addEventListener(eventName, (event) => {
      event.preventDefault();
      zone.classList.add("dragover");
    });
  });
  ["dragleave", "drop"].forEach((eventName) => {
    zone.addEventListener(eventName, (event) => {
      event.preventDefault();
      zone.classList.remove("dragover");
    });
  });
  zone.addEventListener("drop", (event) => {
    const files = event.dataTransfer?.files;
    if (!files || !files.length) return;
    const transfer = new DataTransfer();
    transfer.items.add(files[0]);
    input.files = transfer.files;
    update();
  });
}

["predictionZone", "actualZone", "samplesZone", "summaryZone"].forEach((id) =>
  setupDropzone(document.getElementById(id))
);

/* ---------------- mode switch ---------------- */

function setMode(mode) {
  currentMode = mode;
  document.querySelectorAll(".mode").forEach((button) => {
    const active = button.dataset.mode === mode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  fullUploads.hidden = mode !== "full";
  recoveredUploads.hidden = mode !== "recovered";
  formNote.hidden = mode !== "full";
  recoverNote.hidden = mode !== "recovered";
  demoBtn.textContent = mode === "full" ? "填充示例数据" : "填充恢复示例";
  cutoffHint.textContent = MODE_COPY[mode].cutoffHint;
  submitBtn.querySelector(".label").textContent = MODE_COPY[mode].submit;
}

document.querySelectorAll(".mode").forEach((button) => {
  button.addEventListener("click", () => setMode(button.dataset.mode));
});

/* ---------------- submit ---------------- */

function toCutoffIso(raw) {
  if (!raw) return "";
  // datetime-local yields "YYYY-MM-DDTHH:MM" (or with :ss). The workbench
  // interprets the entered wall-clock time as UTC.
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(raw)) return `${raw}:00Z`;
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/.test(raw)) return `${raw}Z`;
  return raw;
}

function setBusy(busy) {
  submitBtn.disabled = busy;
  submitBtn.classList.toggle("busy", busy);
  submitBtn.querySelector(".label").textContent = busy
    ? MODE_COPY[currentMode].busy
    : MODE_COPY[currentMode].submit;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorCard.hidden = true;
  resultCard.hidden = true;
  detailCard.hidden = true;

  const predictionInput = document.getElementById("predictionCsv");
  const actualInput = document.getElementById("actualCsv");
  const samplesInput = document.getElementById("samplesCsv");
  const summaryInput = document.getElementById("summaryCsv");

  if (currentMode === "full") {
    if (!predictionInput.files.length || !actualInput.files.length) {
      showToast("请选择预测 CSV 和实际值 CSV", true);
      return;
    }
  } else if (!samplesInput.files.length || !summaryInput.files.length) {
    showToast("请选择明细 CSV 和汇总 CSV", true);
    return;
  }
  if (!cutoffInput.value) {
    showToast("请填写评估截止时间（UTC）", true);
    cutoffInput.focus();
    return;
  }

  const body = new FormData();
  body.append("cutoff", toCutoffIso(cutoffInput.value));
  let endpoint = "/api/backtests";
  if (currentMode === "full") {
    body.append("prediction_csv", predictionInput.files[0]);
    body.append("actual_csv", actualInput.files[0]);
  } else {
    endpoint = "/api/backtests/recover";
    body.append("samples_csv", samplesInput.files[0]);
    body.append("summary_csv", summaryInput.files[0]);
  }

  setBusy(true);
  try {
    const response = await fetch(endpoint, { method: "POST", body });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      renderErrors(
        payload.errors,
        payload.total_errors,
        currentMode === "recovered" ? "恢复被拒绝" : "上传被拒绝",
        "未保存"
      );
      return;
    }
    showResult(
      payload.run,
      payload,
      currentMode === "recovered"
        ? { title: "恢复结果", badge: "仅存结果", badgeClass: "warn" }
        : { title: "回测结果", badge: "已保存", badgeClass: "success" }
    );
    showToast(currentMode === "recovered" ? "结果已恢复（仅存结果）" : "回测已保存");
    loadHistory();
  } catch (error) {
    renderErrors([`网络错误：${error.message}`], 1, "请求失败", "未保存");
  } finally {
    setBusy(false);
  }
});

function revealPanel(panel) {
  panel.hidden = false;
  if (typeof panel.scrollIntoView === "function") {
    panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

function renderErrors(errors, total, title = "上传被拒绝", badge = "未保存") {
  errorTitle.textContent = title;
  errorCard.querySelector(".badge").textContent = badge;
  errorList.innerHTML = errors
    .map((message) => `<li>${escapeHtml(message)}</li>`)
    .join("");
  if (total && total > errors.length) {
    errorList.insertAdjacentHTML(
      "beforeend",
      `<li>…共 ${escapeHtml(total)} 条错误，已显示前 ${errors.length} 条。</li>`
    );
  }
  revealPanel(errorCard);
}

/* ---------------- shared result rendering ---------------- */

function runStats(run) {
  const horizons = run.horizons || [];
  const misses = run.misses || [];
  const samples = run.samples || [];
  const warnings = run.warnings || [];
  const rows = run.submitted_rows || {};
  const totalSamples = horizons.reduce((sum, item) => sum + item.sample_count, 0);
  const totalCovered = horizons.reduce((sum, item) => sum + item.covered_count, 0);
  return {
    horizons,
    misses,
    samples,
    warnings,
    rows,
    totalSamples,
    overall: totalSamples ? totalCovered / totalSamples : 0,
  };
}

function renderRunTables(run) {
  const { horizons, misses, samples, warnings, rows, totalSamples, overall } = runStats(run);
  const recovered = run.kind === "recovered";
  const selectedLabel = recovered ? "恢复样本" : "选中预测";
  const selectedValue = recovered
    ? rows.evaluated_samples || totalSamples
    : rows.selected_predictions;

  const meta = `
    <div class="meta-grid">
      <div class="meta"><span>评估截止 (UTC)</span><strong>${escapeHtml(fmtTime(run.cutoff))}</strong></div>
      <div class="meta"><span>${selectedLabel}</span><strong>${fmtNumber(selectedValue)}</strong></div>
      <div class="meta"><span>评估样本</span><strong>${fmtNumber(rows.evaluated_samples || totalSamples)}</strong></div>
      <div class="meta"><span>总体覆盖率</span><strong>${fmtPercent(overall)}</strong></div>
      <div class="meta"><span>区间外样本</span><strong>${fmtNumber(misses.length)}</strong></div>
    </div>`;

  const warningBlock = warnings.length
    ? `<ul class="warn-list">${warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("")}</ul>`
    : "";

  const horizonTable = `
    <div class="table-wrap">
      <table class="data-table">
        <thead>
          <tr>
            <th>Horizon</th>
            <th class="num">样本数</th>
            <th class="num">覆盖数</th>
            <th class="num">覆盖率</th>
            <th class="num">平均区间宽度</th>
          </tr>
        </thead>
        <tbody>
          ${horizons
            .map(
              (h) => `<tr>
                <td><strong>${escapeHtml(h.horizon)}</strong></td>
                <td class="num">${fmtNumber(h.sample_count)}</td>
                <td class="num">${fmtNumber(h.covered_count)}</td>
                <td class="num">${fmtPercent(h.coverage)}</td>
                <td class="num">${fmtNumber(h.mean_width, 4)}</td>
              </tr>`
            )
            .join("")}
        </tbody>
      </table>
    </div>`;

  const missTable = misses.length
    ? `<div class="table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th>Series</th>
              <th>Target</th>
              <th>Horizon</th>
              <th class="num">Lower</th>
              <th class="num">Upper</th>
              <th class="num">Actual</th>
              <th class="num">超出</th>
            </tr>
          </thead>
          <tbody>
            ${misses
              .map((m) => {
                const overshoot =
                  m.actual > m.upper ? m.actual - m.upper : m.lower - m.actual;
                return `<tr>
                  <td>${escapeHtml(m.series_id)}</td>
                  <td>${escapeHtml(fmtTime(m.target_time))}</td>
                  <td>${escapeHtml(m.horizon)}</td>
                  <td class="num">${fmtNumber(m.lower, 4)}</td>
                  <td class="num">${fmtNumber(m.upper, 4)}</td>
                  <td class="num">${fmtNumber(m.actual, 4)}</td>
                  <td class="num covered-no">${fmtNumber(overshoot, 4)}</td>
                </tr>`;
              })
              .join("")}
          </tbody>
        </table>
      </div>`
    : `<p class="empty">所有样本都落在区间内。</p>`;

  const sampleTable = `
    <div class="table-wrap">
      <table class="data-table">
        <thead>
          <tr>
            <th>Series</th>
            <th>Issue</th>
            <th>Target</th>
            <th>Horizon</th>
            <th class="num">Lower</th>
            <th class="num">Upper</th>
            <th class="num">Actual</th>
            <th>覆盖</th>
          </tr>
        </thead>
        <tbody>
          ${samples
            .map(
              (s) => `<tr>
                <td>${escapeHtml(s.series_id)}</td>
                <td>${escapeHtml(fmtTime(s.issue_time))}</td>
                <td>${escapeHtml(fmtTime(s.target_time))}</td>
                <td>${escapeHtml(s.horizon)}</td>
                <td class="num">${fmtNumber(s.lower, 4)}</td>
                <td class="num">${fmtNumber(s.upper, 4)}</td>
                <td class="num">${fmtNumber(s.actual, 4)}</td>
                <td class="${s.covered ? "covered-yes" : "covered-no"}">${s.covered ? "是" : "否"}</td>
              </tr>`
            )
            .join("")}
        </tbody>
      </table>
    </div>`;

  return `
    ${meta}
    ${warningBlock}
    <div class="section-title"><h3>各 horizon 统计</h3><span>lead time = issue_time → target_time 的实际时长</span></div>
    ${horizonTable}
    <div class="section-title"><h3>区间外样本</h3><span>${misses.length} 条</span></div>
    ${missTable}
    <div class="section-title"><h3>全部评估样本</h3><span>${samples.length} 条</span></div>
    ${sampleTable}
  `;
}

function downloadBar(run, payload, labels = {}) {
  const id = encodeURIComponent(run.id);
  const links = [
    `<a href="${escapeHtml(payload?.download_url || `/api/backtests/${id}/download/samples.csv`)}">⬇ ${labels.samples || "下载完整样本 CSV"}</a>`,
    `<a href="${escapeHtml(payload?.summary_url || `/api/backtests/${id}/download/summary.csv`)}">⬇ ${labels.summary || "下载 horizon 汇总 CSV"}</a>`,
  ];
  if (run.has_inputs) {
    // A recovered run's inputs are labelled server-side as 复算输入.
    const prefix = run.input_label ? `${escapeHtml(run.input_label)}：` : "";
    links.push(
      `<a href="/api/backtests/${id}/download/input/prediction.csv">⬇ ${prefix}${labels.prediction || "原始预测 CSV"}</a>`,
      `<a href="/api/backtests/${id}/download/input/actual.csv">⬇ ${prefix}${labels.actual || "原始实际值 CSV"}</a>`
    );
  }
  return `<div class="download-bar">${links.join("")}</div>`;
}

function runBadge(run) {
  if (run.kind === "recovered") {
    return run.input_status === "complete"
      ? { text: "仅存结果 · 已补交复算输入", cls: "warn" }
      : { text: "仅存结果", cls: "warn" };
  }
  return { text: "已保存", cls: "success" };
}

function showResult(run, payload, options = {}) {
  resultTitle.textContent = options.title || "回测结果";
  resultBadge.textContent = options.badge || "已保存";
  resultBadge.className = `badge ${options.badgeClass || "success"}`;
  resultBadge.hidden = false;
  const recovered = run.kind === "recovered";
  resultBody.innerHTML = `
    ${recovered ? `<p class="note-inline">该记录由导出的明细与汇总 CSV 恢复，原始输入未随迁库保留，仅可查看与下载结果。</p>` : ""}
    ${downloadBar(run, payload, recovered ? { samples: "下载明细 CSV（结果）", summary: "下载汇总 CSV（结果）" } : {})}
    ${renderRunTables(run)}
    <p class="empty" style="margin-top:12px">记录 ID：<code>${escapeHtml(run.id)}</code></p>
  `;
  revealPanel(resultCard);
}

/* ---------------- history ---------------- */

function historyItem(run) {
  const rows = run.submitted_rows || {};
  const badge = runBadge(run);
  const topHorizon = (run.horizons || [])
    .map((h) => `${h.horizon} ${fmtPercent(h.coverage)}`)
    .join(" · ");
  return `<div class="history-item" data-run-id="${escapeHtml(run.id)}">
    <div class="row1">
      <strong>截止 ${escapeHtml(fmtTime(run.cutoff))}</strong>
      <span class="badge ${badge.cls}">${escapeHtml(badge.text)}</span>
    </div>
    <div class="row2">${fmtNumber(rows.evaluated_samples)} 个样本 · ${escapeHtml(topHorizon || "无 horizon")}</div>
    <div class="row2"><time>${escapeHtml(fmtTime(run.created_at))}</time> · <code>${escapeHtml(run.id)}</code></div>
    <div class="links">
      <a href="/api/backtests/${encodeURIComponent(run.id)}/download/samples.csv">样本 CSV</a>
      <a href="/api/backtests/${encodeURIComponent(run.id)}/download/summary.csv">汇总 CSV</a>
      <button type="button" class="link-button" data-open-run="${escapeHtml(run.id)}">查看详情</button>
    </div>
  </div>`;
}

async function loadHistory() {
  try {
    const response = await fetch("/api/backtests?limit=20");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    const runs = payload.runs || [];
    if (!runs.length) {
      historyBody.innerHTML = `<p class="empty">暂无记录。</p>`;
      return;
    }
    historyBody.innerHTML = runs.map(historyItem).join("");
    historyBody.querySelectorAll("[data-open-run]").forEach((button) => {
      button.addEventListener("click", () => openRun(button.dataset.openRun));
    });
  } catch (error) {
    historyBody.innerHTML = `<p class="empty">加载历史失败：${escapeHtml(error.message)}</p>`;
  }
}

document.getElementById("refreshHistory").addEventListener("click", loadHistory);
document.getElementById("closeDetail").addEventListener("click", () => {
  detailCard.hidden = true;
});

/* ---------------- detail & backfill ---------------- */

function backfillForm(run) {
  if (run.kind !== "recovered" || run.input_status !== "awaiting") return "";
  return `
    <div class="backfill">
      <div class="section-title"><h3>补交候选预测与实际值</h3><span>沿用截止 ${escapeHtml(fmtTime(run.cutoff))}</span></div>
      <p class="form-note">
        用恢复时的截止时间重跑正常回测，明细与汇总都吻合才会一次性登记两份文件，
        标为「复算输入」供下载；编号与结果保持不变。并发补交以首个成功登记的一对为准。
      </p>
      <form id="backfillForm" novalidate>
        <div class="uploads two-col">
          <label class="dropzone">
            <input type="file" data-role="prediction" accept=".csv,text/csv" />
            <strong>候选预测 CSV</strong>
            <span class="drop-meta">series_id, issue_time, target_time, lower, upper</span>
            <span class="file-name" data-file-name>点击选择文件</span>
          </label>
          <label class="dropzone">
            <input type="file" data-role="actual" accept=".csv,text/csv" />
            <strong>候选实际值 CSV</strong>
            <span class="drop-meta">series_id, target_time, actual</span>
            <span class="file-name" data-file-name>点击选择文件</span>
          </label>
        </div>
        <div class="actions">
          <button type="submit" class="primary" id="backfillBtn">
            <span class="spinner" aria-hidden="true"></span>
            <span class="label">核对并登记复算输入</span>
          </button>
        </div>
      </form>
    </div>`;
}

function wireBackfill(run) {
  const backfill = document.getElementById("backfillForm");
  if (!backfill) return;
  backfill.querySelectorAll(".dropzone").forEach(setupDropzone);

  backfill.addEventListener("submit", async (event) => {
    event.preventDefault();
    errorCard.hidden = true;
    const prediction = backfill.querySelector('input[data-role="prediction"]');
    const actual = backfill.querySelector('input[data-role="actual"]');
    if (!prediction.files.length || !actual.files.length) {
      showToast("请选择候选预测 CSV 和实际值 CSV", true);
      return;
    }
    const button = document.getElementById("backfillBtn");
    button.disabled = true;
    button.classList.add("busy");
    button.querySelector(".label").textContent = "核对与登记中…";

    const body = new FormData();
    body.append("prediction_csv", prediction.files[0]);
    body.append("actual_csv", actual.files[0]);

    try {
      const response = await fetch(
        `/api/backtests/${encodeURIComponent(run.id)}/inputs`,
        { method: "POST", body }
      );
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        renderErrors(
          payload.errors || [payload.detail || `请求失败（HTTP ${response.status}）`],
          payload.total_errors,
          "补交未登记",
          "未保存"
        );
        return;
      }
      showToast("复算输入已登记");
      showDetail(payload.run, payload);
      loadHistory();
    } catch (error) {
      renderErrors([`网络错误：${error.message}`], 1, "补交未登记", "未保存");
    } finally {
      button.disabled = false;
      button.classList.remove("busy");
      button.querySelector(".label").textContent = "核对并登记复算输入";
    }
  });
}

function showDetail(run, payload) {
  const badge = runBadge(run);
  detailBadge.textContent = badge.text;
  detailBadge.className = `badge ${badge.cls}`;

  detailBody.innerHTML = `
    ${downloadBar(run, payload, run.kind === "recovered"
      ? { samples: "下载明细 CSV（结果）", summary: "下载汇总 CSV（结果）" }
      : {})}
    ${renderRunTables(run)}
    <p class="empty" style="margin-top:12px">记录 ID：<code>${escapeHtml(run.id)}</code>${
      run.kind === "recovered" ? "（恢复时编号，保持不变）" : ""
    }</p>
    ${backfillForm(run)}
  `;
  wireBackfill(run);
  revealPanel(detailCard);
}

async function openRun(runId) {
  try {
    const response = await fetch(`/api/backtests/${encodeURIComponent(runId)}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const run = await response.json();
    showDetail(run, {});
  } catch (error) {
    showToast(`加载详情失败：${error.message}`, true);
  }
}

/* ---------------- service status ---------------- */

async function checkHealth() {
  try {
    const response = await fetch("/healthz");
    if (!response.ok) throw new Error();
    serviceStatus.className = "status ok";
    serviceStatus.innerHTML = `<span class="dot"></span> 服务正常 · UTC`;
  } catch {
    serviceStatus.className = "status bad";
    serviceStatus.innerHTML = `<span class="dot"></span> 服务不可用`;
  }
}

/* ---------------- demo data ---------------- */

const DEMO_CUTOFF = "2026-11-20T12:00:00Z";
const DEMO_PREDICTIONS = [
  "series_id,issue_time,target_time,lower,upper",
  "alpha,2026-11-20T11:00:00Z,2026-11-20T13:00:00Z,9.5,12.0",
  "alpha,2026-11-20T11:30:00Z,2026-11-20T13:00:00Z,10.0,12.0",
  "alpha,2026-11-20T12:30:00Z,2026-11-20T13:00:00Z,99.0,100.0",
  "alpha,2026-11-20T11:00:00Z,2026-11-20T14:00:00Z,10.0,12.0",
  "alpha,2026-11-20T12:00:00Z,2026-11-20T14:00:00Z,10.5,12.0",
  "beta,2026-11-20T11:00:00Z,2026-11-20T13:00:00Z,20.0,22.0",
  "beta,2026-11-20T12:00:00Z,2026-11-20T15:00:00Z,20.0,21.0",
].join("\n");

const DEMO_ACTUALS = [
  "series_id,target_time,actual",
  "alpha,2026-11-20T13:00:00Z,11.2",
  "alpha,2026-11-20T14:00:00Z,12.6",
  "beta,2026-11-20T13:00:00Z,21.4",
  "beta,2026-11-20T15:00:00Z,20.6",
].join("\n");

function runDemoBacktest() {
  // Compute the export client-side so the recovery demo matches the demo
  // backtest exactly: same cutoff, latest-issue selection, lead-time grouping.
  const cutoff = Date.parse(DEMO_CUTOFF);
  const toIso = (value) => new Date(value).toISOString().replace(".000Z", "Z");
  const formatDuration = (seconds) => {
    const days = Math.floor(seconds / 86400);
    let rem = seconds % 86400;
    const hours = Math.floor(rem / 3600);
    rem %= 3600;
    const minutes = Math.floor(rem / 60);
    const secs = rem % 60;
    const parts = [];
    if (days) parts.push(`${days}d`);
    if (hours) parts.push(`${hours}h`);
    if (minutes) parts.push(`${minutes}m`);
    if (secs && !days) parts.push(`${secs}s`);
    return parts.join(" ") || "0s";
  };
  const parseRows = (text) => {
    const [header, ...lines] = text.trim().split("\n");
    const columns = header.split(",");
    return lines.map((line) =>
      Object.fromEntries(line.split(",").map((value, index) => [columns[index], value]))
    );
  };

  const selected = new Map();
  for (const row of parseRows(DEMO_PREDICTIONS)) {
    const issue = Date.parse(row.issue_time);
    if (issue > cutoff) continue;
    const key = `${row.series_id}|${row.target_time}`;
    const current = selected.get(key);
    if (!current || issue > Date.parse(current.issue_time)) {
      selected.set(key, row);
    }
  }
  const actuals = new Map(
    parseRows(DEMO_ACTUALS).map((row) => [`${row.series_id}|${row.target_time}`, Number(row.actual)])
  );

  const samples = [...selected.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([key, row]) => {
      const issue = Date.parse(row.issue_time);
      const target = Date.parse(row.target_time);
      const lead = Math.round((target - issue) / 1000);
      const lower = Number(row.lower);
      const upper = Number(row.upper);
      const actual = actuals.get(key);
      return {
        series_id: row.series_id,
        issue_time: toIso(issue),
        target_time: toIso(target),
        horizon: formatDuration(lead),
        lead_seconds: lead,
        lower,
        upper,
        width: upper - lower,
        actual,
        covered: lower <= actual && actual <= upper,
      };
    });

  const groups = new Map();
  for (const sample of samples) {
    const bucket = groups.get(sample.lead_seconds) || {
      lead_seconds: sample.lead_seconds,
      horizon: sample.horizon,
      sample_count: 0,
      covered_count: 0,
      width_total: 0,
    };
    bucket.sample_count += 1;
    bucket.covered_count += sample.covered ? 1 : 0;
    bucket.width_total += sample.width;
    groups.set(sample.lead_seconds, bucket);
  }
  const summaryRows = [...groups.values()].map((bucket) => [
    bucket.horizon,
    bucket.sample_count,
    bucket.covered_count,
    (bucket.covered_count / bucket.sample_count).toFixed(6),
    (bucket.width_total / bucket.sample_count).toFixed(6),
  ]);

  return {
    samples: [
      "series_id,issue_time,target_time,horizon,lead_seconds,lower,upper,width,actual,covered",
      ...samples.map((s) =>
        [
          s.series_id,
          s.issue_time,
          s.target_time,
          s.horizon,
          s.lead_seconds,
          s.lower,
          s.upper,
          s.width,
          s.actual,
          s.covered ? "true" : "false",
        ].join(",")
      ),
    ].join("\n"),
    summary: ["horizon,sample_count,covered_count,coverage,mean_width", ...summaryRows.map((r) => r.join(","))].join("\n"),
  };
}

function attachFile(inputId, filename, content) {
  const input = document.getElementById(inputId);
  if (!input) return false;
  const file = new File([content], filename, { type: "text/csv" });
  if (typeof DataTransfer === "function") {
    const transfer = new DataTransfer();
    transfer.items.add(file);
    input.files = transfer.files;
  } else {
    // Older Safari/Firefox lack the DataTransfer constructor.
    showToast("当前浏览器不支持自动填充文件，请手动选择 samples/ 下的示例 CSV", true);
    return false;
  }
  input.dispatchEvent(new Event("change"));
  return true;
}

demoBtn.addEventListener("click", () => {
  if (currentMode === "full") {
    if (attachFile("predictionCsv", "demo-predictions.csv", DEMO_PREDICTIONS)) {
      attachFile("actualCsv", "demo-actuals.csv", DEMO_ACTUALS);
      cutoffInput.value = "2026-11-20T12:00";
      showToast("已填充示例数据，可直接运行");
    }
    return;
  }
  const demo = runDemoBacktest();
  if (attachFile("samplesCsv", "demo-samples.csv", demo.samples)) {
    attachFile("summaryCsv", "demo-summary.csv", demo.summary);
    cutoffInput.value = "2026-11-20T12:00";
    showToast("已填恢复示例（与示例回测同源）");
  }
});

/* ---------------- init ---------------- */

setMode("full");
checkHealth();
loadHistory();
