"use strict";

const form = document.getElementById("backtestForm");
const submitBtn = document.getElementById("submitBtn");
const cutoffInput = document.getElementById("cutoff");
const resultCard = document.getElementById("resultCard");
const errorCard = document.getElementById("errorCard");
const errorList = document.getElementById("errorList");
const resultBody = document.getElementById("resultBody");
const historyBody = document.getElementById("historyBody");
const toast = document.getElementById("toast");
const serviceStatus = document.getElementById("serviceStatus");

let toastTimer = null;

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

["predictionZone", "actualZone"].forEach((id) => setupDropzone(document.getElementById(id)));

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
  submitBtn.querySelector(".label").textContent = busy ? "校验与计算中…" : "校验并运行回测";
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorCard.hidden = true;
  resultCard.hidden = true;

  const predictionInput = document.getElementById("predictionCsv");
  const actualInput = document.getElementById("actualCsv");

  if (!predictionInput.files.length || !actualInput.files.length) {
    showToast("请选择预测 CSV 和实际值 CSV", true);
    return;
  }
  if (!cutoffInput.value) {
    showToast("请填写评估截止时间（UTC）", true);
    cutoffInput.focus();
    return;
  }

  const body = new FormData();
  body.append("prediction_csv", predictionInput.files[0]);
  body.append("actual_csv", actualInput.files[0]);
  body.append("cutoff", toCutoffIso(cutoffInput.value));

  setBusy(true);
  try {
    const response = await fetch("/api/backtests", { method: "POST", body });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 422 && payload.errors) {
        renderErrors(payload.errors, payload.total_errors);
      } else {
        renderErrors([payload.detail || `请求失败（HTTP ${response.status}）`], 1);
      }
      return;
    }
    renderResult(payload);
    showToast("回测已保存");
    loadHistory();
  } catch (error) {
    renderErrors([`网络错误：${error.message}`], 1);
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

function renderErrors(errors, total) {
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

/* ---------------- result rendering ---------------- */

function renderResult(payload) {
  const run = payload.run;
  const horizons = run.horizons || [];
  const misses = run.misses || [];
  const samples = run.samples || [];
  const warnings = run.warnings || [];
  const rows = run.submitted_rows || {};

  const totalSamples = horizons.reduce((sum, item) => sum + item.sample_count, 0);
  const totalCovered = horizons.reduce((sum, item) => sum + item.covered_count, 0);
  const overall = totalSamples ? totalCovered / totalSamples : 0;

  const meta = `
    <div class="meta-grid">
      <div class="meta"><span>评估截止 (UTC)</span><strong>${escapeHtml(fmtTime(run.cutoff))}</strong></div>
      <div class="meta"><span>选中预测</span><strong>${fmtNumber(rows.selected_predictions)}</strong></div>
      <div class="meta"><span>评估样本</span><strong>${fmtNumber(rows.evaluated_samples)}</strong></div>
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

  resultBody.innerHTML = `
    <div class="download-bar">
      <a href="${escapeHtml(payload.download_url)}">⬇ 下载完整样本 CSV</a>
      <a href="${escapeHtml(payload.summary_url)}">⬇ 下载 horizon 汇总 CSV</a>
      <a href="/api/backtests/${encodeURIComponent(run.id)}/download/input/prediction.csv">⬇ 原始预测 CSV</a>
      <a href="/api/backtests/${encodeURIComponent(run.id)}/download/input/actual.csv">⬇ 原始实际值 CSV</a>
    </div>
    ${meta}
    ${warningBlock}
    <div class="section-title"><h3>各 horizon 统计</h3><span>lead time = issue_time → target_time 的实际时长</span></div>
    ${horizonTable}
    <div class="section-title"><h3>区间外样本</h3><span>${misses.length} 条</span></div>
    ${missTable}
    <div class="section-title"><h3>全部评估样本</h3><span>${samples.length} 条</span></div>
    ${sampleTable}
    <p class="empty" style="margin-top:12px">记录 ID：<code>${escapeHtml(run.id)}</code></p>
  `;
  revealPanel(resultCard);
}

/* ---------------- history ---------------- */

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
    historyBody.innerHTML = runs
      .map((run) => {
        const rows = run.submitted_rows || {};
        const topHorizon = (run.horizons || [])
          .map((h) => `${h.horizon} ${fmtPercent(h.coverage)}`)
          .join(" · ");
        return `<div class="history-item">
          <div class="row1">
            <strong>截止 ${escapeHtml(fmtTime(run.cutoff))}</strong>
            <time>${escapeHtml(fmtTime(run.created_at))}</time>
          </div>
          <div class="row2">${fmtNumber(rows.evaluated_samples)} 个样本 · ${escapeHtml(topHorizon || "无 horizon")}</div>
          <div class="row2"><code>${escapeHtml(run.id)}</code></div>
          <div class="links">
            <a href="/api/backtests/${encodeURIComponent(run.id)}/download/samples.csv">样本 CSV</a>
            <a href="/api/backtests/${encodeURIComponent(run.id)}/download/summary.csv">汇总 CSV</a>
          </div>
        </div>`;
      })
      .join("");
  } catch (error) {
    historyBody.innerHTML = `<p class="empty">加载历史失败：${escapeHtml(error.message)}</p>`;
  }
}

document.getElementById("refreshHistory").addEventListener("click", loadHistory);

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

function attachFile(inputId, filename, content) {
  const input = document.getElementById(inputId);
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

document.getElementById("demoBtn").addEventListener("click", () => {
  if (attachFile("predictionCsv", "demo-predictions.csv", DEMO_PREDICTIONS)) {
    attachFile("actualCsv", "demo-actuals.csv", DEMO_ACTUALS);
    cutoffInput.value = "2026-11-20T12:00";
    showToast("已填充示例数据，可直接运行");
  }
});

/* ---------------- init ---------------- */

checkHealth();
loadHistory();
