"use strict";

const STAGES = [
  ["extraction", "轉換 JSON"],
  ["duplicate", "重複偵測"],
  ["priority", "優先級判定"],
  ["assignee", "分派負責人"],
  ["localization", "錯誤定位"],
];
const STATUS_TEXT = {
  pending: "尚未執行",
  running: "執行中…",
  done: "完成",
  fallback: "完成，使用備援",
  skipped: "略過",
  failed: "失敗",
};
const TICKET_STATUS = {
  new: ["尚未開始", ""],
  running: ["處理中", "chip-running"],
  awaiting_duplicate_review: ["等你確認是否重複", "chip-gate"],
  ready_to_continue: ["準備繼續", "chip-running"],
  duplicate: ["已判定為重複", ""],
  processed: ["已完成", "chip-done"],
  failed: ["失敗", "chip-failed"],
};
const SIGNAL_TEXT = {
  component_history: "同元件處理紀錄",
  product_component_history: "同產品＋元件紀錄",
  text_similarity: "內容相似紀錄",
  component_owner_mapping: "元件負責人對照",
  global_prior: "整體處理量",
  file_ownership: "檔案負責人",
  component_ownership: "元件 ownership",
};
const FACTOR_TEXT = {
  textual: "文本因子",
  temporal: "時間因子",
  author: "作者因子",
  related_report: "相關報告因子",
  severity: "嚴重性因子",
  product_component: "產品／元件因子",
  textual_factor: "文本因子",
  temporal_factor: "時間因子",
  author_factor: "作者因子",
  related_report_factor: "相關報告因子",
  severity_factor: "嚴重性因子",
  component_factor: "元件因子",
};
const FIELD_LABELS = [
  ["ticket_id", "編號"], ["product", "產品"], ["component", "元件"], ["severity", "嚴重度"],
  ["bug_type", "問題類型"], ["os", "作業系統"], ["version", "版本"], ["reporter", "回報者"],
  ["environment", "環境"], ["error_message", "錯誤訊息", "wide mono"], ["description", "描述", "wide"],
  ["steps_to_reproduce", "重現步驟", "wide"], ["expected_behavior", "預期結果", "wide"],
  ["actual_behavior", "實際結果", "wide"], ["logs", "Log", "wide mono"],
];

const state = { tickets: [], selected: null, detail: null, filter: "all", query: "", homeSignature: null, projects: [], defaultProject: "", pollTimer: null };
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString("zh-TW", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}
function num(value, digits = 3) {
  return typeof value === "number" && isFinite(value) ? value.toFixed(digits) : "—";
}
function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), 3200);
}
async function api(path, options = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}
function post(path, body = {}) {
  return api(path, { method: "POST", body: JSON.stringify(body) });
}

/* ------------------------------------------------------------------ inbox */
function bucket(t) {
  if (t.busy || t.status === "running" || t.status === "ready_to_continue") return "running";
  if (t.status === "awaiting_duplicate_review" || (t.attention || []).some((a) => a.includes("負責人"))) return "review";
  if (t.status === "processed" || t.status === "duplicate") return "done";
  return "other";
}

/* Search: every word must appear somewhere in the ticket's id, title, project, component, assignee, ... */
function searchTerms() {
  return state.query.toLowerCase().split(/\s+/).filter(Boolean);
}
function haystack(t) {
  const [label] = TICKET_STATUS[t.busy ? "running" : t.status] || [t.status];
  return [t.ticket_id, t.key, t.title, t.project, t.product, t.component, t.assignee, t.priority, label, t.duplicate_of, t.snippet]
    .filter(Boolean).join(" ").toLowerCase();
}
function markTerms(text, terms) {
  const value = String(text ?? "");
  if (!terms.length) return esc(value);
  const pattern = new RegExp(`(${terms.map((x) => x.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "gi");
  return value.split(pattern).map((part, i) => (i % 2 ? `<mark>${esc(part)}</mark>` : esc(part))).join("");
}

/* Cover colour follows what the ticket needs; the blob layout is seeded by the ticket id so each card is recognisable. */
function coverTone(t) {
  const b = bucket(t);
  if (t.status === "failed") return "failed";
  if (b === "review") return "gate";
  if (b === "running") return "running";
  if (t.status === "processed") return "done";
  return "quiet";
}
function seed(text) {
  let h = 2166136261;
  for (const ch of String(text)) { h ^= ch.codePointAt(0); h = Math.imul(h, 16777619); }
  return h >>> 0;
}

const PIN_ICON = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 3h6l-1 6 4 4H6l4-4z"/><path d="M12 13v8" fill="none" stroke-width="2" stroke-linecap="round"/></svg>`;

function tile(t, terms) {
  const [label] = TICKET_STATUS[t.busy ? "running" : t.status] || [t.status];
  const h = seed(t.ticket_id || t.key);
  const blobs = `--x1:${15 + (h % 55)}%;--y1:${10 + ((h >>> 6) % 45)}%;--r1:${38 + ((h >>> 12) % 30)}px;--x2:${45 + ((h >>> 17) % 50)}%;--y2:${45 + ((h >>> 22) % 45)}%;--r2:${22 + ((h >>> 27) % 22)}px`;
  const pebbles = STAGES.map(([key, name]) => {
    const s = t.current_stage === key && (t.busy || t.status === "running") ? "running" : t.stage_status[key];
    return `<i data-s="${esc(s)}" title="${esc(name)}：${esc(STATUS_TEXT[s] || s)}"></i>`;
  }).join("");
  const attention = (t.attention || []).find((a) => !a.includes("備援"));
  const meta = [
    t.component ? `<span class="tag">${markTerms(t.component, terms)}</span>` : "",
    t.assignee ? `<span class="tag tag-person ${t.assignee_confirmed ? "is-confirmed" : ""}" title="${t.assignee_confirmed ? "已確認負責人" : "建議負責人"}">${markTerms(t.assignee, terms)}</span>` : "",
    t.duplicate_of ? `<span class="tag">重複 ${markTerms(t.duplicate_of, terms)}</span>` : "",
  ].join("");
  return `<li class="tile" data-tone="${coverTone(t)}" data-key="${esc(t.key)}">
    <div class="tile-cover" style="${blobs}">
      <span class="tile-status">${esc(label)}</span>
      <span class="pebbles" aria-label="處理進度">${pebbles}</span>
      ${t.priority ? `<span class="sticker" data-p="${esc(t.priority)}" title="優先級 ${esc(t.priority)}">${esc(t.priority)}</span>` : ""}
    </div>
    <div class="tile-body">
      <div class="tile-top"><span class="tile-id">${markTerms(t.ticket_id, terms)}</span><time>${esc(fmtTime(t.created_at))}</time></div>
      <a class="tile-link" href="#/t/${encodeURIComponent(t.key)}"><span class="tile-title">${markTerms(t.title, terms)}</span></a>
      ${meta ? `<div class="tile-meta">${meta}</div>` : ""}
      ${attention ? `<p class="tile-attention">${esc(attention)}</p>` : ""}
    </div>
    <button type="button" class="pin-btn ${t.pinned ? "is-on" : ""}" data-pin="${esc(t.key)}" aria-pressed="${t.pinned}" title="${t.pinned ? "取消釘選" : "釘選到上方"}" aria-label="${t.pinned ? "取消釘選" : "釘選"} ${esc(t.ticket_id)}">${PIN_ICON}</button>
  </li>`;
}

function visibleTickets() {
  const terms = searchTerms();
  const matched = terms.length ? state.tickets.filter((t) => { const hay = haystack(t); return terms.every((x) => hay.includes(x)); }) : state.tickets;
  return { terms, matched, shown: matched.filter((t) => state.filter === "all" || bucket(t) === state.filter) };
}

function renderInbox() {
  const { terms, matched, shown } = visibleTickets();
  const signature = JSON.stringify([state.tickets, state.filter, state.query]);
  if (signature === state.homeSignature) return; // nothing changed: keep hover and focus
  state.homeSignature = signature;

  const counts = { all: matched.length, review: 0, running: 0, done: 0 };
  matched.forEach((t) => { const b = bucket(t); if (b in counts) counts[b] += 1; });
  $$("[data-count]").forEach((el) => (el.textContent = counts[el.dataset.count]));

  const pinnedAll = state.tickets.filter((t) => t.pinned);
  const pinned = shown.filter((t) => t.pinned).sort((a, b) => String(a.pinned_at).localeCompare(String(b.pinned_at)));
  const others = shown.filter((t) => !t.pinned);
  const none = state.tickets.length === 0;
  $("#inboxEmpty").hidden = !none;
  $$(".shelf").forEach((el) => (el.hidden = none));
  $(".home-bar").hidden = none;

  $("#pinnedCount").textContent = pinned.length || "";
  $("#otherCount").textContent = others.length || "";
  $("#pinnedTiles").innerHTML = pinned.length ? pinned.map((t) => tile(t, terms)).join("")
    : pinnedAll.length ? `<li class="tile-note">釘選的 Ticket 裡沒有符合條件的。</li>`
    : `<li class="tile-hint">點任一張卡片右上角的圖釘，常看的 Ticket 就會固定在這裡。</li>`;
  $("#otherTiles").innerHTML = others.length ? others.map((t) => tile(t, terms)).join("")
    : `<li class="tile-note">${terms.length || state.filter !== "all" ? "沒有符合條件的 Ticket。" : "所有 Ticket 都已釘選。"}</li>`;
  $("#searchSummary").innerHTML = terms.length
    ? `「${esc(state.query.trim())}」找到 ${shown.length} 張 <button type="button" class="link-btn" data-clear-search>清除搜尋</button>` : "";
}

async function loadTickets() {
  const data = await api("/api/tickets");
  state.tickets = data.tickets;
  renderInbox();
  if (state.selected && !state.tickets.some((t) => t.key === state.selected)) {
    toast("這張 Ticket 已不存在");
    goHome();
  }
}

async function togglePin(key) {
  const t = state.tickets.find((x) => x.key === key) || (state.detail && state.detail.key === key ? state.detail : null);
  if (!t) return;
  const pinned = !t.pinned;
  try {
    await post(`/api/tickets/${encodeURIComponent(key)}/pin`, { pinned });
    toast(pinned ? `已釘選 ${t.ticket_id}` : `已取消釘選 ${t.ticket_id}`);
    await refresh();
  } catch (err) { toast(err.message); }
}

/* ----------------------------------------------------------------- routing */
function goHome() {
  if (location.hash && location.hash !== "#/") location.hash = "#/";
  else route();
}
async function route() {
  const match = location.hash.match(/^#\/t\/(.+)$/);
  const key = match ? decodeURIComponent(match[1]) : null;
  $("#homeView").hidden = Boolean(key);
  $("#detail").hidden = !key;
  if (!key) {
    state.selected = null;
    state.detail = null;
    state.detailSignature = null;
    document.title = "Ticket 工作台";
    state.homeSignature = null;
    renderInbox();
    return;
  }
  if (state.selected === key && state.detail) return;
  state.selected = key;
  try {
    await loadDetail(key, { reset: true });
    document.title = `${state.detail.ticket_id} · Ticket 工作台`;
    $("#detail").scrollTop = 0;
  } catch (err) {
    toast(err.message);
    goHome();
  }
}

/* ----------------------------------------------------------------- detail */
async function loadDetail(key, { reset = false, force = false } = {}) {
  const data = await api(`/api/tickets/${encodeURIComponent(key)}`);
  const signature = JSON.stringify(data);
  if (!reset && !force && signature === state.detailSignature) return; // nothing changed: keep open panels and scroll
  const previous = state.detail;
  state.detail = data;
  state.detailSignature = signature;
  if (reset || !previous || previous.key !== data.key) {
    state.tabPinned = false;
    state.tab = suggestedTab(data);
  } else if (!state.tabPinned) {
    state.tab = suggestedTab(data); // follow the flow until the user picks a tab
  }
  renderDetail();
}

function stageStatus(record, key) {
  const stage = record.stages[key] || {};
  if ((record.busy || record.status === "running") && record.current_stage === key) return "running";
  return stage.status || "pending";
}

/* The tab that needs attention: the open gate, the running stage, or the last finished stage. */
function suggestedTab(r) {
  if (r.status === "awaiting_duplicate_review" || r.status === "duplicate") return "duplicate";
  if ((r.busy || r.status === "running" || r.status === "failed") && r.current_stage) return r.current_stage;
  if (r.status === "new") return "extraction";
  const finished = STAGES.map(([k]) => k).filter((k) => !["pending", "running"].includes(stageStatus(r, k)));
  return finished.length ? finished[finished.length - 1] : "extraction";
}

function selectTab(tab, { pinned = true } = {}) {
  state.tab = tab;
  state.tabPinned = pinned;
  renderDetail();
  $("#detail").scrollTop = 0;
  const panel = $("#tabPanel");
  if (panel) panel.focus({ preventScroll: true });
}

function renderDetail() {
  const body = $("#detailBody");
  const r = state.detail;
  if (!state.selected || !r) { body.innerHTML = ""; return; }
  const [label, cls] = TICKET_STATUS[r.busy ? "running" : r.status] || [r.status, ""];
  const awaiting = r.status === "awaiting_duplicate_review";
  const tab = state.tab || "extraction";
  const doneCount = STAGES.filter(([key]) => ["done", "fallback", "skipped"].includes(stageStatus(r, key))).length;
  const tabs = STAGES.map(([key, name]) => {
    const s = stageStatus(r, key);
    const gate = key === "duplicate" && awaiting ? ' data-gate="open"' : "";
    const selected = key === tab;
    return `<li data-s="${s}"${gate}><button type="button" role="tab" id="tab-${key}" aria-controls="tabPanel" aria-selected="${selected}" class="${selected ? "is-active" : ""}" data-tab="${key}">
      <span class="node"></span><span class="label">${name}</span><span class="state">${esc(STATUS_TEXT[s] || s)}</span></button></li>`;
  }).join("");
  const logSelected = tab === "log";
  const projectOptions = state.projects.map((p) => `<option value="${esc(p.project)}" ${p.project === r.project ? "selected" : ""}>${esc(p.project)}</option>`).join("");

  let hint = "";
  if (awaiting && tab !== "duplicate") hint = `<div class="pin-hint pin-gate"><span>重複偵測正在等你確認，確認後才會繼續後面的步驟。</span><button class="btn btn-gate btn-small" data-tab="duplicate">前往確認</button></div>`;
  else if (r.status === "failed") hint = `<div class="pin-hint pin-failed"><span>流程在「${esc((STAGES.find((x) => x[0] === r.current_stage) || ["", r.current_stage])[1])}」停止：${esc(r.error)}</span></div>`;
  else if (r.status === "duplicate") hint = `<div class="pin-hint pin-info"><span>已判定為 <b class="mono">${esc((r.decisions.duplicate || {}).duplicate_of || "")}</b> 的重複，流程結束。</span></div>`;
  else if (r.busy || r.status === "running") hint = `<div class="pin-hint pin-info"><span>正在執行「${esc((STAGES.find((x) => x[0] === r.current_stage) || ["", "處理"])[1])}」，完成後會自動更新。</span></div>`;

  let panel;
  if (logSelected) {
    panel = `<section class="stage"><div class="stage-head"><h3>處理紀錄</h3></div><div class="stage-body"><ol class="timeline">
      ${(r.events || []).slice().reverse().map((e) => `<li><time>${esc(fmtTime(e.at))}</time>${esc(e.message)}</li>`).join("")}
    </ol></div></section>`;
  } else {
    const index = STAGES.findIndex(([key]) => key === tab);
    const [key, name] = STAGES[index];
    const gate = key === "duplicate" && awaiting
      ? `<div class="gate"><p><strong>需要你判斷：</strong>找到 ${(r.stages.duplicate.output.top_k_candidates || []).length} 張相似的歷史 Ticket。點任一張候選會開啟並排比對，可以在比對視窗裡標註「可能重複」或直接確認是哪一張；如果都不是，按右邊的按鈕繼續。</p><button class="btn btn-gate" data-action="not-duplicate">不是重複，繼續處理</button></div>`
      : "";
    const prev = STAGES[index - 1];
    const next = STAGES[index + 1];
    const pager = `<nav class="pager" aria-label="切換步驟">
        ${prev ? `<button class="btn btn-quiet btn-small" data-tab="${prev[0]}">上一步：${prev[1]}</button>` : "<span></span>"}
        ${next ? `<button class="btn btn-quiet btn-small" data-tab="${next[0]}">下一步：${next[1]}</button>` : `<button class="btn btn-quiet btn-small" data-tab="log">查看處理紀錄</button>`}
      </nav>`;
    panel = gate + renderStage(r, key, name, index + 1, projectOptions) + pager;
  }

  body.innerHTML = `
    <div class="pinned"><div class="pin-card">
      <div class="pin-head">
        <a class="back-link" href="#/" title="回到所有 Ticket" aria-label="回到所有 Ticket"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14.5 6 8.5 12l6 6" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg></a>
        <div class="pin-title">
          <span class="id">${esc(r.ticket_id)}</span>
          <h2 title="${esc(r.title)}">${esc(r.title)}</h2>
        </div>
        <div class="pin-meta">
          <button type="button" class="pin-btn pin-btn-inline ${r.pinned ? "is-on" : ""}" data-action="pin" aria-pressed="${Boolean(r.pinned)}" title="${r.pinned ? "取消釘選" : "釘選到首頁上方"}" aria-label="${r.pinned ? "取消釘選" : "釘選"}">${PIN_ICON}</button>
          <span class="chip ${cls}">${esc(label)}</span>
          <span class="progress-count">${doneCount} / ${STAGES.length} 步完成</span>
          <details class="more">
            <summary class="btn btn-quiet btn-small">更多</summary>
            <div class="more-menu">
              <p>專案 <b>${esc(r.project || "未指定")}</b></p>
              <p>來源 <b>${esc(r.source)}</b></p>
              <p>匯入 <b>${esc(fmtTime(r.created_at))}</b></p>
              <a class="btn btn-quiet btn-small" href="/api/tickets/${encodeURIComponent(r.key)}/artifact/ticket.json">下載完整結果 JSON</a>
              <button class="btn btn-quiet btn-small" data-action="rerun" ${r.busy ? "disabled" : ""}>${r.status === "new" ? "開始處理" : "從頭重新執行"}</button>
              <button class="btn btn-quiet btn-small" data-action="delete" ${r.busy ? "disabled" : ""}>刪除這張 Ticket</button>
            </div>
          </details>
        </div>
      </div>
      <div class="tabs-row">
        <ol class="track" role="tablist" aria-label="處理流程">${tabs}</ol>
        <button type="button" class="log-tab ${logSelected ? "is-active" : ""}" role="tab" aria-selected="${logSelected}" data-tab="log">處理紀錄</button>
      </div>
      ${hint}
    </div></div>
    <div class="tab-panel" id="tabPanel" role="tabpanel" tabindex="-1" aria-labelledby="tab-${esc(tab)}">${panel}</div>`;
}

function stageFrame(r, key, name, n, inner) {
  const stage = r.stages[key] || {};
  const s = stageStatus(r, key);
  const chipCls = { done: "chip-done", fallback: "chip-fallback", failed: "chip-failed", running: "chip-running" }[s] || "";
  const warnings = (stage.warnings || []).length ? `<ul class="warn-list">${stage.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : "";
  const foot = stage.module ? `<div class="stage-foot">
      <span>使用模組 <b>${esc(stage.module)}</b></span>
      <span>程式 <code>${esc(stage.implementation)}</code></span>
      ${stage.duration_ms != null ? `<span>耗時 ${(stage.duration_ms / 1000).toFixed(1)} 秒</span>` : ""}
      ${stage.fallback_reason ? `<span>備援原因：${esc(stage.fallback_reason)}</span>` : ""}
      ${stage.details && stage.details.reference_metrics ? `<span>參考成績：${esc(stage.details.reference_metrics)}</span>` : ""}
    </div>` : "";
  let content;
  if (s === "pending") content = `<p class="stage-pending">${key === "localization" || key === "priority" || key === "assignee" ? "等前面的步驟完成後執行。" : "尚未執行。"}</p>`;
  else if (s === "running") content = `<p class="stage-pending">正在執行，完成後會自動更新。</p>`;
  else if (s === "failed") content = `<p class="err">${esc(stage.error)}</p>`;
  else content = warnings + inner(stage.output || {}, stage);
  return `<section class="stage" id="stage-${key}">
    <div class="stage-head"><h3><span class="num">${n}</span>${name}</h3><span class="chip ${chipCls}">${esc(STATUS_TEXT[s] || s)}</span></div>
    <div class="stage-body">${content}</div>${s === "pending" || s === "running" ? "" : foot}</section>`;
}

function renderStage(r, key, name, n, projectOptions) {
  const renderers = {
    extraction: (o, st) => renderExtraction(r, o, st),
    duplicate: (o) => renderDuplicate(r, o),
    priority: (o) => renderPriority(o),
    assignee: (o) => renderAssignee(r, o),
    localization: (o) => renderLocalization(r, o, projectOptions),
  };
  let html = stageFrame(r, key, name, n, renderers[key]);
  if (key === "localization" && stageStatus(r, key) === "failed" && !r.busy) {
    html = html.replace('</div><div class="stage-foot">', `${locControls(r, projectOptions)}</div><div class="stage-foot">`);
  }
  return html;
}

function renderExtraction(r, o, st) {
  const t = o.structured_ticket || r.structured_ticket || {};
  const raw = r.raw || {};
  const llm = st.status === "done" && o.predicted_json;
  const cells = FIELD_LABELS.map(([k, label, cls = ""]) => {
    let v = t[k];
    if (Array.isArray(v)) v = v.map((x, i) => `${i + 1}. ${x}`).join("\n");
    if (v == null || v === "" || v === "unknown") return "";
    const fromModel = llm && (raw[k] == null || raw[k] === "" || (Array.isArray(raw[k]) && !raw[k].length));
    return `<div class="${cls.replace("mono", "")} ${fromModel ? "from-model" : ""}"><dt>${label}</dt><dd class="${cls.includes("mono") ? "mono" : ""} ${cls.includes("wide") ? "text-block" : ""}">${esc(v)}</dd></div>`;
  }).join("");
  const rawOutput = st.details && st.details.llm_raw_output ? `<details class="raw"><summary>Code Llama 原始輸出</summary><pre>${esc(st.details.llm_raw_output)}</pre></details>` : "";
  return `<dl class="fields">${cells}</dl>
    <details class="raw"><summary>原始匯入內容</summary><pre>${esc(JSON.stringify(raw, null, 2))}</pre></details>${rawOutput}`;
}

function scoreRow(label, value) {
  const v = typeof value === "number" ? Math.max(0, Math.min(1, value)) : 0;
  return `<div class="score"><span>${label}</span><span class="bar"><i style="width:${(v * 100).toFixed(1)}%"></i></span><b>${num(value)}</b></div>`;
}

function renderDuplicate(r, o) {
  const cands = o.top_k_candidates || [];
  const decision = (r.decisions || {}).duplicate;
  const awaiting = r.status === "awaiting_duplicate_review" && !r.busy;
  const marks = r.review_marks || {};
  const decisionLine = decision ? `<p class="confirmed-line">${decision.decision === "duplicate"
      ? `已確認為 <b class="mono">${esc(decision.duplicate_of || "")}</b> 的重複`
      : "已確認不是重複"}（${esc({ engineer: "工程師確認", auto_threshold: "超過自動門檻", no_candidates: "沒有可比對的歷史資料", below_review_threshold: "低於複核門檻" }[decision.by] || decision.by)}，${esc(fmtTime(decision.at))}）</p>` : "";
  if (!cands.length) return `<p>${esc(o.decision_policy || "沒有找到相似的歷史 Ticket。")}</p>${decisionLine}`;
  const item = (c, index) => {
    const matches = Object.entries(c.metadata_match || {}).filter(([, v]) => v).map(([k]) => ({ product: "同產品", component: "同元件", severity: "同嚴重度" }[k]));
    const picked = decision && decision.decision === "duplicate" && decision.duplicate_of === c.ticket_id;
    const mark = marks[c.ticket_id] || {};
    const markChip = mark.mark === "possible" ? `<span class="mark-chip mark-possible">已標註：可能重複</span>`
      : mark.mark === "not" ? `<span class="mark-chip mark-not">已標註：不是這張</span>` : "";
    return `<li class="cand cand-open ${picked ? "is-picked" : ""} ${mark.mark ? `is-${mark.mark}` : ""}" data-compare="${index}">
      <div class="cand-title"><span class="id">${esc(c.ticket_id)}</span><button type="button" data-compare="${index}" title="並排比對內容">${esc(c.title)}</button></div>
      <div class="cand-actions">${markChip}<button class="btn btn-quiet btn-small" data-compare="${index}">並排比對</button></div>
      <div class="cand-scores">${scoreRow("相似度", c.similarity)}<span class="sub-score">標題 <b>${num(c.title_similarity)}</b></span><span class="sub-score">內容 <b>${num(c.content_similarity)}</b></span></div>
      ${c.description ? `<p class="cand-desc">${esc(c.description)}</p>` : ""}
      ${mark.note ? `<p class="mark-note">備註：${esc(mark.note)}</p>` : ""}
      <div class="cand-meta"><span>第 ${c.rank} 名</span>${c.component ? `<span>元件 ${esc(c.component)}</span>` : ""}${c.priority ? `<span>優先級 ${esc(c.priority)}</span>` : ""}${c.assignee ? `<span>負責人 ${esc(c.assignee)}</span>` : ""}${c.status ? `<span>狀態 ${esc(c.status)}</span>` : ""}${matches.map((m) => `<span class="match">${m}</span>`).join("")}</div>
    </li>`;
  };
  const head = cands.slice(0, 3).map((c, i) => item(c, i)).join("");
  const rest = cands.slice(3);
  const restMarked = rest.some((c) => (marks[c.ticket_id] || {}).mark);
  const more = rest.length ? `<details class="more-cands" ${restMarked ? "open" : ""}><summary>顯示其餘 ${rest.length} 張候選</summary><ol class="cands">${rest.map((c, i) => item(c, i + 3)).join("")}</ol></details>` : "";
  const possible = cands.filter((c) => (marks[c.ticket_id] || {}).mark === "possible");
  const summary = possible.length
    ? `<p class="marked-summary">你標註為可能重複的：${possible.map((c) => `<b class="mono">${esc(c.ticket_id)}</b>`).join("、")}。打開並排比對後，可以在視窗裡確認是哪一張。</p>` : "";
  const hint = awaiting ? "點任一張候選，會並排顯示新 Ticket 和那張歷史 Ticket 的完整內容。" : "";
  return `<p class="hint">${esc(o.decision_policy || "")}；比對了 ${o.history_size ?? "?"} 張歷史 Ticket。${hint}</p>${summary}<ol class="cands" style="margin-top:10px">${head}</ol>${more}${decisionLine}`;
}

/* ---------------------------------------------------- compare dialog */
const COMPARE_FIELDS = [
  ["標題", ["title"], ["title", "summary"], "text"],
  ["描述", ["description"], ["description", "content", "body"], "text"],
  ["錯誤訊息", ["error_message"], ["error_message"], "mono"],
  ["重現步驟", ["steps_to_reproduce"], ["steps_to_reproduce"], "text"],
  ["預期結果", ["expected_behavior"], ["expected_behavior", "expected_result"], "text"],
  ["實際結果", ["actual_behavior"], ["actual_behavior", "actual_result"], "text"],
  ["Log", ["logs"], ["logs", "log"], "mono"],
  ["產品", ["product"], ["product"], "meta"],
  ["元件", ["component"], ["component"], "meta"],
  ["嚴重度", ["severity"], ["severity"], "meta"],
  ["優先級", ["__priority"], ["priority"], "meta"],
  ["問題類型", ["bug_type"], ["bug_type"], "meta"],
  ["環境", ["environment", "os", "version"], ["environment", "os", "version", "platform"], "meta"],
  ["回報者", ["reporter"], ["reporter", "creator"], "meta"],
  ["建立時間", ["created_at"], ["created_at", "creation_time"], "meta"],
  ["負責人", [], ["assignee", "assigned_to"], "meta"],
  ["處理狀態", [], ["status", "resolution"], "meta"],
  ["標示的重複對象", [], ["duplicate_of", "dupe_of"], "meta"],
];
const STOP = new Set("the and for with when that this from into have has are was were not but you your its can will after before then than also there their them they what which while only into onto over under about would could should does did doing been being some more most such each just very other".split(" "));
const compare = { index: 0, history: {} };

function pickValue(obj, keys) {
  const values = [];
  for (const k of keys) {
    let v = obj ? obj[k] : undefined;
    if (Array.isArray(v)) v = v.map((x, i) => `${i + 1}. ${x}`).join("\n");
    if (v == null) continue;
    v = String(v).trim();
    if (!v || v.toLowerCase() === "unknown" || v === "--") continue;
    if (!values.includes(v)) values.push(v);
  }
  return values.join(" / ");
}
function tokens(text) {
  return new Set((String(text).toLowerCase().match(/[a-z_][a-z0-9_]{2,}|[0-9]{3,}/g) || []).filter((t) => !STOP.has(t)));
}
function highlight(text, shared) {
  if (!shared.size) return esc(text);
  return String(text).split(/([A-Za-z_][A-Za-z0-9_]{2,}|[0-9]{3,})/).map((part, i) =>
    i % 2 === 1 && shared.has(part.toLowerCase()) ? `<mark>${esc(part)}</mark>` : esc(part)).join("");
}

async function openCompare(index) {
  const r = state.detail;
  const cands = ((r.stages.duplicate || {}).output || {}).top_k_candidates || [];
  if (!cands[index]) return;
  compare.index = index;
  const c = cands[index];
  if (!compare.history[c.ticket_id]) {
    try {
      compare.history[c.ticket_id] = (await api(`/api/history/${encodeURIComponent(c.ticket_id)}`)).ticket;
    } catch {
      compare.history[c.ticket_id] = { ...c, __partial: true };
    }
  }
  renderCompare();
  const dialog = $("#compareDialog");
  if (!dialog.open) dialog.showModal();
  $("#compareGrid").scrollTop = 0;
}

function renderCompare() {
  const r = state.detail;
  const cands = ((r.stages.duplicate || {}).output || {}).top_k_candidates || [];
  const c = cands[compare.index];
  if (!c) return;
  const hist = compare.history[c.ticket_id] || c;
  const fresh = { ...(r.structured_ticket || {}) };
  fresh.__priority = ((r.stages.priority || {}).output || {}).predicted_priority || fresh.priority || "";
  const mark = (r.review_marks || {})[c.ticket_id] || {};
  const awaiting = r.status === "awaiting_duplicate_review" && !r.busy;

  const rows = COMPARE_FIELDS.map(([label, newKeys, oldKeys, kind]) => [label, pickValue(fresh, newKeys), pickValue(hist, oldKeys), kind])
    .filter(([, a, b]) => a || b);
  const textA = rows.filter((x) => x[3] !== "meta").map((x) => x[1]).join(" ");
  const textB = rows.filter((x) => x[3] !== "meta").map((x) => x[2]).join(" ");
  const shareA = tokens(textA), shareB = tokens(textB);
  const shared = new Set([...shareA].filter((t) => shareB.has(t)));

  $("#comparePos").textContent = `候選 ${compare.index + 1} / ${cands.length}　第 ${c.rank} 名`;
  $("#compareTitle").innerHTML = `<span class="mono">${esc(r.ticket_id)}</span> 對照 <span class="mono">${esc(c.ticket_id)}</span>`;
  $("#compareScores").innerHTML = `<span>相似度 <b>${num(c.similarity)}</b></span><span>標題 <b>${num(c.title_similarity)}</b></span><span>內容 <b>${num(c.content_similarity)}</b></span><span>TF-IDF <b>${num(c.tfidf_similarity)}</b></span><span>共同字詞 <b>${shared.size}</b> 個</span>${hist.__partial ? "<span>（找不到完整的歷史資料，只顯示候選摘要）</span>" : ""}`;

  const cell = (value, kind, cls, same) => {
    if (!value) return `<div class="${cls} c-empty${same ? " is-same" : ""}" role="cell">未提供</div>`;
    const body = kind === "meta" ? esc(value) : highlight(value, shared);
    return `<div class="${cls}${same ? " is-same" : ""}" role="cell"><span class="${kind === "meta" ? "" : "c-text"} ${kind === "mono" ? "c-mono" : ""}">${body}</span></div>`;
  };
  $("#compareGrid").innerHTML = `
    <div class="c-head c-corner" role="columnheader"></div>
    <div class="c-head c-new" role="columnheader">新 Ticket<small>${esc(r.ticket_id)}</small></div>
    <div class="c-head c-old" role="columnheader">歷史 Ticket<small>${esc(c.ticket_id)}</small></div>
    ${rows.map(([label, a, b, kind]) => {
      const same = kind === "meta" && a && b && a.toLowerCase() === b.toLowerCase();
      return `<div class="c-label" role="rowheader">${esc(label)}${same ? ' <span class="same-badge">相同</span>' : ""}</div>${cell(a, kind, "c-new", same)}${cell(b, kind, "c-old", same)}`;
    }).join("")}`;

  $("#compareNote").value = mark.note || "";
  $("#compareMark").innerHTML = mark.mark === "possible" ? '<span class="mark-chip mark-possible">目前標註：可能重複</span>'
    : mark.mark === "not" ? '<span class="mark-chip mark-not">目前標註：不是這張</span>' : "";
  $$("[data-compare-mark]").forEach((b) => {
    const on = mark.mark === b.dataset.compareMark;
    b.classList.toggle("is-on", on);
    b.textContent = b.dataset.compareMark === "possible" ? (on ? "取消可能重複標註" : "標註可能重複") : (on ? "取消「不是這張」" : "不是這張");
  });
  const confirmBtn = $("#compareConfirm");
  confirmBtn.hidden = !awaiting;
  confirmBtn.textContent = `確認是 ${c.ticket_id} 的重複`;
  $$("[data-compare-nav]").forEach((b) => {
    const target = compare.index + Number(b.dataset.compareNav);
    b.disabled = target < 0 || target >= cands.length;
  });
}

async function onCompareClick(event) {
  const r = state.detail;
  const cands = ((r.stages.duplicate || {}).output || {}).top_k_candidates || [];
  const c = cands[compare.index];
  const path = `/api/tickets/${encodeURIComponent(r.key)}`;
  if (event.target.closest("[data-compare-close]")) { $("#compareDialog").close(); return; }
  const nav = event.target.closest("[data-compare-nav]");
  if (nav) { await openCompare(compare.index + Number(nav.dataset.compareNav)); return; }
  const markBtn = event.target.closest("[data-compare-mark]");
  if (markBtn && c) {
    const current = ((r.review_marks || {})[c.ticket_id] || {}).mark;
    const mark = current === markBtn.dataset.compareMark ? "" : markBtn.dataset.compareMark;
    try {
      await post(`${path}/mark`, { candidate_id: c.ticket_id, mark, note: $("#compareNote").value });
      toast(mark === "possible" ? `已把 ${c.ticket_id} 標註為可能重複` : mark === "not" ? `已標註 ${c.ticket_id} 不是重複` : `已清除 ${c.ticket_id} 的標註`);
      await loadDetail(r.key, { reset: false, force: true });
      renderCompare();
    } catch (err) { toast(err.message); }
    return;
  }
  if (event.target.closest("[data-compare-confirm]") && c) {
    try {
      const note = $("#compareNote").value.trim();
      if (note) await post(`${path}/mark`, { candidate_id: c.ticket_id, mark: "possible", note });
      await post(`${path}/duplicate`, { decision: "duplicate", duplicate_of: c.ticket_id, note });
      $("#compareDialog").close();
      toast(`已確認為 ${c.ticket_id} 的重複，流程結束`);
      await refresh();
    } catch (err) { toast(err.message); }
  }
}

function renderPriority(o) {
  const dist = o.class_probabilities || {};
  const keys = ["P1", "P2", "P3", "P4", "P5"];
  const max = Math.max(0.0001, ...keys.map((k) => dist[k] || 0));
  const distHtml = Object.keys(dist).length ? `<div class="dist" aria-label="各級機率">${keys.map((k) => `<div class="${k === o.predicted_priority ? "is-max" : ""}" title="${k} ${num(dist[k])}"><span style="height:${(((dist[k] || 0) / max) * 100).toFixed(0)}%"></span>${k}</div>`).join("")}</div>` : "";
  let factors = "";
  if (o.factor_values) {
    factors = Object.entries(o.factor_values).map(([name, vals]) => {
      if (name === "textual") return `<div class="factor"><h4>${esc(FACTOR_TEXT[name])}</h4><p class="factor-text">清洗後摘要：${esc(vals.summary_terms || "")}</p></div>`;
      return `<div class="factor"><h4>${esc(FACTOR_TEXT[name] || name)}</h4><dl>${Object.entries(vals).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(typeof v === "number" ? (Number.isInteger(v) ? v : v.toFixed(3)) : v)}</dd>`).join("")}</dl></div>`;
    }).join("");
  } else if (o.factor_scores) {
    factors = Object.entries(o.factor_scores).map(([k, v]) => `<div class="factor"><h4>${esc(FACTOR_TEXT[k] || k)}</h4>${scoreRow("分數", v)}</div>`).join("");
  }
  return `<div class="priority-main">
      <div class="p-badge">${esc(o.predicted_priority || "—")}<small>${esc(o.priority_name || "")}${o.confidence != null ? `，信心 ${num(o.confidence, 2)}` : ""}</small></div>
      ${distHtml}
      ${o.reported_priority ? `<span class="chip">回報者填寫 ${esc(o.reported_priority)}</span>` : ""}
    </div>
    <div class="factor-grid">${factors}</div>`;
}

function renderAssignee(r, o) {
  const details = o.candidate_details || [];
  const confirmed = (r.decisions || {}).assignee;
  const scores = details.map((d) => d.score || 0);
  const top = Math.max(0.0001, ...scores);
  const canConfirm = !r.busy && r.status === "processed";
  const rows = details.map((d, i) => `<li class="assignee-row ${confirmed && confirmed.assignee === d.assignee ? "is-confirmed" : ""}">
      <span class="rank">${i + 1}</span>
      <div><div class="who">${esc(d.assignee)}</div><div class="sig">${(d.signals || []).map((s) => esc(SIGNAL_TEXT[s] || s)).join("、")}</div></div>
      <div style="width:140px">${scoreRow("分數", (d.score || 0) / top).replace(/<b>.*<\/b>/, `<b>${num(d.score, 2)}</b>`)}</div>
      ${canConfirm ? `<button class="btn btn-quiet btn-small" data-action="assign" data-who="${esc(d.assignee)}" data-decision="${i === 0 ? "accept" : "override"}">${i === 0 ? "採用建議" : "改派給他"}</button>` : "<span></span>"}
    </li>`).join("");
  const routing = { assigned: "已依模型分派", assigned_by_fallback: "以 fallback 規則分派", needs_manual_triage: "建議人工分派" }[o.routing_status] || o.routing_status || "";
  return `<p style="margin-bottom:10px">${esc(routing)}${o.suggested_assignee ? `：建議 <b>${esc(o.suggested_assignee)}</b>` : ""}${o.confidence != null ? `（信心 ${num(o.confidence, 2)}）` : ""}</p>
    ${o.reason ? `<p class="hint" style="margin-bottom:10px">${esc(o.reason)}</p>` : ""}
    ${rows ? `<ol class="assignee-list">${rows}</ol>` : "<p class=\"hint\">沒有可推薦的候選人。</p>"}
    ${canConfirm ? `<div class="manual-assign"><input id="manualAssignee" placeholder="或輸入其他負責人的 email／帳號" aria-label="指定負責人"><button class="btn btn-quiet btn-small" data-action="assign-manual">指定這位負責人</button></div>` : ""}
    ${confirmed ? `<p class="confirmed-line">已確認負責人：<b>${esc(confirmed.assignee)}</b>（${esc(fmtTime(confirmed.at))}）</p>` : ""}`;
}

function locControls(r, projectOptions) {
  if (!state.projects.length) return `<p class="hint" style="margin-top:10px">設定檔裡還沒有任何專案，請在 integration_config.json 的 projects 加入 repo_path。</p>`;
  return `<div class="loc-controls"><label>專案 <select id="locProject">${projectOptions}</select></label>
    <button class="btn btn-quiet btn-small" data-action="localize">${r.stages.localization.status === "done" ? "重新定位" : "執行錯誤定位"}</button></div>`;
}

function renderLocalization(r, o, projectOptions) {
  if (!o.ranked_symbols) return r.busy ? "" : locControls(r, projectOptions);
  const summary = o.summary || {};
  const level = { high: "高", medium: "中", low: "低" }[o.confidence_level] || o.confidence_level || "—";
  const symbols = (o.ranked_symbols || []).map((s) => {
    const lines = String(s.code || "").split("\n").map((line) => `<span>${esc(line)}</span>`).join("");
    return `<li><details class="symbol" ${s.rank === 1 ? "open" : ""}><summary>
        <span class="rank">${s.rank}</span>
        <span class="path">${esc(s.file_path)} <b>${esc(s.symbol_qualified_name || "")}</b></span>
        <span class="lines">第 ${s.start_line}–${s.end_line} 行，分數 ${num(s.score)}</span>
        <span class="why">${esc(s.reason || "")}</span></summary>
        <pre class="code" style="--start:${Math.max(0, (s.start_line || 1) - 1)}">${lines}</pre></details></li>`;
  }).join("");
  return `<div class="loc-summary">
      <span>定位信心 <span class="chip ${o.confidence_level === "high" ? "chip-done" : o.confidence_level === "low" ? "chip-failed" : "chip-fallback"}">${esc(level)}</span></span>
      <span>分數 <b class="mono">${num((o.confidence || {}).confidence_score)}</b></span>
      <span>${o.should_manual_review ? "需要人工確認" : "可直接參考"}</span>
      <span>Repository <b>${esc(o.repository || o.project)}</b> <code>${esc(String(o.base_commit || "").slice(0, 10))}</code></span>
    </div>
    ${summary.message ? `<p class="hint" style="margin-bottom:10px">${esc(summary.message)}</p>` : ""}
    ${symbols ? `<ol class="symbols">${symbols}</ol>` : `<p>沒有找到候選位置。</p>`}
    ${(o.engineering_guidance || []).length ? `<ul class="guidance">${o.engineering_guidance.map((g) => `<li>${esc(g)}</li>`).join("")}</ul>` : ""}
    ${r.busy ? "" : locControls(r, projectOptions)}`;
}

/* ---------------------------------------------------------------- actions */
async function onDetailClick(event) {
  const opener = event.target.closest("[data-compare]");
  if (opener && state.detail && !event.target.closest("a")) { await openCompare(Number(opener.dataset.compare)); return; }
  const tabBtn = event.target.closest("[data-tab]");
  if (tabBtn && state.detail) { selectTab(tabBtn.dataset.tab); return; }
  const btn = event.target.closest("[data-action]");
  if (!btn || !state.detail) return;
  const key = state.detail.key;
  const path = `/api/tickets/${encodeURIComponent(key)}`;
  if (btn.dataset.action === "pin") { await togglePin(key); return; }
  try {
    btn.disabled = true;
    if (["rerun", "not-duplicate", "is-duplicate", "localize"].includes(btn.dataset.action)) state.tabPinned = false;
    switch (btn.dataset.action) {
      case "rerun":
        await post(`${path}/run`);
        toast("已開始處理");
        break;
      case "not-duplicate":
        await post(`${path}/duplicate`, { decision: "not_duplicate" });
        toast("已確認不是重複，繼續判定優先級、負責人與錯誤定位");
        break;
      case "is-duplicate":
        if (!confirm(`確認這張 Ticket 是 ${btn.dataset.id} 的重複？流程會在這裡結束。`)) { btn.disabled = false; return; }
        await post(`${path}/duplicate`, { decision: "duplicate", duplicate_of: btn.dataset.id });
        toast(`已標記為 ${btn.dataset.id} 的重複`);
        break;
      case "assign":
        await post(`${path}/assignee`, { assignee: btn.dataset.who, decision: btn.dataset.decision });
        toast(`負責人已確認為 ${btn.dataset.who}`);
        break;
      case "assign-manual": {
        const who = $("#manualAssignee").value.trim();
        if (!who) { toast("請先輸入負責人"); btn.disabled = false; return; }
        await post(`${path}/assignee`, { assignee: who, decision: "manual" });
        toast(`負責人已確認為 ${who}`);
        break;
      }
      case "localize":
        await post(`${path}/localize`, { project: ($("#locProject") || {}).value });
        toast("已開始錯誤定位");
        break;
      case "delete":
        if (!confirm("刪除這張 Ticket 的處理紀錄？")) { btn.disabled = false; return; }
        await api(path, { method: "DELETE" });
        toast(`已刪除 ${state.detail.ticket_id}`);
        goHome();
        break;
    }
    await refresh();
  } catch (err) {
    toast(err.message);
    btn.disabled = false;
  }
}

async function refresh() {
  await loadTickets();
  if (state.selected) await loadDetail(state.selected);
  schedulePoll();
}

function schedulePoll() {
  clearTimeout(state.pollTimer);
  const busy = state.tickets.some((t) => t.busy || t.status === "running" || t.status === "ready_to_continue");
  state.pollTimer = setTimeout(refresh, busy ? 1200 : 6000);
}

/* ---------------------------------------------------------------- import */
let importFile = null;
let importMode = "file";

function openImport() {
  importFile = null;
  $("#dropFile").textContent = "";
  $("#importError").textContent = "";
  $("#asHistory").checked = false;
  $("#importForm").reset();
  $("#importProject").innerHTML = state.projects.length
    ? state.projects.map((p) => `<option value="${esc(p.project)}" ${p.project === state.defaultProject ? "selected" : ""}>${esc(p.project)}　${esc(p.repo_path)}</option>`).join("")
    : `<option value="">（設定檔沒有專案，將略過錯誤定位）</option>`;
  setImportMode("file");
  $("#importDialog").showModal();
}

function setImportMode(mode) {
  importMode = mode;
  $$(".seg").forEach((b) => b.classList.toggle("is-active", b.dataset.mode === mode));
  $$("[data-pane]").forEach((p) => (p.hidden = p.dataset.pane !== mode));
  const history = mode === "file" && $("#asHistory").checked;
  $("#importSubmit").textContent = history ? "加入歷史資料庫" : "匯入並開始處理";
  $(".project-field").hidden = history;
}

async function submitImport(event) {
  if (event.submitter && event.submitter.value === "cancel") return;
  event.preventDefault();
  const body = { project: $("#importProject").value };
  if (importMode === "file") {
    if (!importFile) { $("#importError").textContent = "請先選擇檔案。"; return; }
    body.content = await importFile.text();
    body.filename = importFile.name;
    body.source = `匯入檔案 ${importFile.name}`;
    body.as_history = $("#asHistory").checked;
  } else {
    const form = new FormData($("#importForm"));
    const ticket = {};
    for (const [k, v] of form.entries()) if (String(v).trim()) ticket[k] = String(v).trim();
    if (!ticket.title) { $("#importError").textContent = "請填寫標題。"; return; }
    if (ticket.steps_to_reproduce) ticket.steps_to_reproduce = ticket.steps_to_reproduce.split("\n").map((s) => s.replace(/^\s*\d+[.)、]\s*/, "").trim()).filter(Boolean);
    body.ticket = ticket;
    body.source = "手動填寫";
  }
  try {
    $("#importSubmit").disabled = true;
    const result = await post("/api/import", body);
    $("#importDialog").close();
    if (result.history_imported != null) {
      toast(`已加入 ${result.history_imported} 張歷史 Ticket，資料庫共 ${result.history_total} 張`);
    } else {
      toast(`已匯入 ${result.created.length} 張 Ticket，開始處理`);
      if (result.created.length === 1) location.hash = `#/t/${encodeURIComponent(result.created[0])}`;
      else goHome();
    }
    await refresh();
  } catch (err) {
    $("#importError").textContent = err.message;
  } finally {
    $("#importSubmit").disabled = false;
  }
}

function pickFile(file) {
  importFile = file;
  $("#dropFile").textContent = file ? `${file.name}（${Math.ceil(file.size / 1024)} KB）` : "";
}

/* ---------------------------------------------------------------- status */
async function openStatus() {
  $("#statusBody").innerHTML = "<p class=\"hint\">讀取中…</p>";
  $("#statusDialog").showModal();
  const [report, projects] = await Promise.all([api("/api/status"), api("/api/projects")]);
  $("#configPath").textContent = report.config.config_path || "（未指定設定檔，使用預設值）";
  $("#statusBody").innerHTML = `<div class="status-grid">${STAGES.map(([key]) => {
    const s = report.stages[key];
    return `<div class="status-card"><h4>${esc(s.title)}<span class="chip ${s.ready ? "chip-done" : "chip-fallback"}">${s.ready ? "可用訓練模型／資料" : "會使用備援或略過"}</span></h4><ul>
      ${s.checks.map((c) => `<li><span class="${c.ok ? "ok" : c.required ? "ng" : "opt"}">${c.ok ? "✓" : c.required ? "✗" : "○"}</span><span>${esc(c.label)}</span>${c.detail ? `<span class="d">${esc(c.detail)}</span>` : ""}</li>`).join("")}
    </ul></div>`;
  }).join("")}</div>`;
  renderProjects(projects.projects);
}

function renderProjects(projects) {
  $("#projectBody").innerHTML = projects.length ? projects.map((p) => {
    const job = p.index_job || {};
    const jobText = job.status === "running" ? "建立中…" : job.status === "failed" ? `失敗：${job.error}` : job.status === "done" ? `已建立（${job.files} 個檔案，${(job.duration_ms / 1000).toFixed(1)} 秒）` : "";
    return `<div class="project-row"><div><b>${esc(p.project)}</b> <code>${esc(p.repo_path)}</code><br>
      <span class="hint">${p.index_exists ? `索引更新於 ${esc(p.index_updated_at)}（${p.index_size_kb} KB）` : p.code_index_path ? "尚未建立索引；第一次定位時會即時建立" : "未設定 code_index_path，每次啟動後第一次定位會即時建立索引"} ${esc(jobText)}</span></div>
      ${p.code_index_path ? `<button class="btn btn-quiet btn-small" data-index="${esc(p.project)}" ${job.status === "running" ? "disabled" : ""}>${p.index_exists ? "更新索引" : "建立索引"}</button>` : ""}</div>`;
  }).join("") : `<p class="hint">設定檔裡沒有專案。</p>`;
}

async function onProjectClick(event) {
  const btn = event.target.closest("[data-index]");
  if (!btn) return;
  btn.disabled = true;
  await post(`/api/projects/${encodeURIComponent(btn.dataset.index)}/index`);
  const timer = setInterval(async () => {
    const data = await api("/api/projects");
    state.projects = data.projects;
    renderProjects(data.projects);
    if (!data.projects.some((p) => p.index_job && p.index_job.status === "running")) clearInterval(timer);
  }, 1000);
}

/* ------------------------------------------------------------------ init */
async function init() {
  $("#homeView").addEventListener("click", (e) => {
    const pin = e.target.closest("[data-pin]");
    if (pin) { e.preventDefault(); togglePin(pin.dataset.pin); return; }
    if (e.target.closest("[data-clear-search]")) { setQuery(""); $("#search").focus(); return; }
    const card = e.target.closest(".tile");
    if (card && !e.target.closest("a")) location.hash = `#/t/${encodeURIComponent(card.dataset.key)}`;
  });
  $$(".filter").forEach((b) => b.addEventListener("click", () => {
    state.filter = b.dataset.filter;
    $$(".filter").forEach((x) => { x.classList.toggle("is-active", x === b); x.setAttribute("aria-selected", x === b); });
    renderInbox();
  }));
  const search = $("#search");
  const setQuery = (value) => {
    search.value = value;
    state.query = value;
    if (state.selected) goHome(); // searching always shows the card wall
    renderInbox();
  };
  search.addEventListener("input", () => setQuery(search.value));
  search.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { setQuery(""); return; }
    if (e.key === "Enter") {
      const first = $("#homeView .tile");
      if (first) location.hash = `#/t/${encodeURIComponent(first.dataset.key)}`;
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.target.closest("input, textarea, select, [contenteditable]") || document.querySelector("dialog[open]")) return;
    e.preventDefault();
    search.focus();
    search.select();
  });
  window.addEventListener("hashchange", route);
  $("#detail").addEventListener("click", onDetailClick);
  $("#compareDialog").addEventListener("click", onCompareClick);
  $("#compareDialog").addEventListener("close", () => { compare.history = {}; });
  $("#detail").addEventListener("keydown", (e) => {
    const current = e.target.closest('[role="tab"]');
    if (!current || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
    const tabs = $$('#detail [role="tab"]');
    let i = tabs.indexOf(current);
    if (e.key === "ArrowLeft") i = (i - 1 + tabs.length) % tabs.length;
    if (e.key === "ArrowRight") i = (i + 1) % tabs.length;
    if (e.key === "Home") i = 0;
    if (e.key === "End") i = tabs.length - 1;
    e.preventDefault();
    selectTab(tabs[i].dataset.tab);
    const target = $(`#detail [role="tab"][data-tab="${tabs[i].dataset.tab}"]`);
    if (target) target.focus();
  });
  $("#importButton").addEventListener("click", openImport);
  $$("[data-open-import]").forEach((b) => b.addEventListener("click", openImport));
  $("#statusButton").addEventListener("click", openStatus);
  $("#projectBody").addEventListener("click", onProjectClick);
  $$(".seg").forEach((b) => b.addEventListener("click", () => setImportMode(b.dataset.mode)));
  $("#asHistory").addEventListener("change", () => setImportMode(importMode));
  $("#importForm").addEventListener("submit", submitImport);
  $("#fileInput").addEventListener("change", (e) => pickFile(e.target.files[0]));
  const drop = $("#dropZone");
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("is-over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("is-over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("is-over"); pickFile(e.dataTransfer.files[0]); });

  const projects = await api("/api/projects");
  state.projects = projects.projects;
  state.defaultProject = projects.default_project;
  await refresh();
  await route();
}

init().catch((err) => toast(err.message));
