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
/* Priority factor explanations. Definitions follow bug-priority-drone/scripts/build_features.py
   (add_temporal_features, add_author_features, build_related_feature_rows) and
   bug_tracking_llm_system/src/modules/priority_classifier.py for the fallback formula. */
const SEVERITY_TEXT = {
  blocker: "阻斷", critical: "嚴重", major: "重大", normal: "一般", minor: "輕微", trivial: "細微", enhancement: "功能需求", unknown: "未填",
};
const OLD_HISTORY_NOTE = "這一項只看這張 Ticket 建立時間往前推的一段期間。參考的歷史資料（設定檔的 priority.history_csv）如果比這張 Ticket 早很多，這段期間內就找不到任何回報，數值會是 0。組內目前的 Eclipse 歷史資料是 2001–2005 年的回報，新 Ticket 的建立時間是現在，所以通常是 0。這不是程式錯誤，但代表這一項沒有提供資訊。";
const NO_DATA_NOTE = "0 代表沒有資料，不是「P0」。優先級的數字是 P1 = 1 到 P5 = 5。";
const FACTOR_GROUP_HELP = {
  textual: ["從回報的文字判斷緊急程度", "文本因子看的是回報寫了什麼。PRI 把標題和描述轉成 TF-IDF 字詞特徵（單字與相鄰兩字），也會偵測 crash、hang、data loss、security、regression 等代表嚴重問題的關鍵字，以及 typo、enhancement 等通常比較不急的字詞。"],
  temporal: ["最近一段時間整體的回報量", "時間因子看的是這張 Ticket 建立前，整個專案最近有多少回報。短時間內大量回報，常代表新版本出問題或有問題大量爆發，同時也會影響工程師能分配給每張 Ticket 的時間。"],
  author: ["回報者過去的回報紀錄", "作者因子看的是同一位回報者以前回報過多少張、那些回報平均被排成什麼優先級。經驗多的回報者，或過去常回報緊急問題的人，回報的優先級往往有一定的傾向。"],
  related_report: ["最像的歷史回報被排成什麼優先級", "相關報告因子先用 REP- 相似度找出和這張最像的歷史回報，再看它們被排成什麼優先級。REP- 比對標題與描述的字詞（BM25F，單字與相鄰兩字），並加上同產品、同元件的加權。內容很像的問題，優先級通常也接近。"],
  severity: ["回報者填寫的嚴重度", "嚴重度是「影響有多大」，優先級是「要多快處理」，兩者相關但不相同。例如 blocker 通常很急，但也可能因為只影響少數人而排在後面。"],
  product_component: ["同產品、同元件最近的狀況", "產品／元件因子看的是同一個產品或元件最近有多少回報、被排成什麼優先級。有些元件（例如核心模組）的問題通常比較急，最近問題很多的元件也值得注意。"],
};
const FACTOR_HELP = {
  summary_terms: {
    label: "標題清洗結果", group: "textual",
    short: "標題清洗後的文字，模型用它算字詞特徵",
    detail: "PRI 先把標題轉成小寫，把網址、Email、十六進位碼換成 <URL>、<EMAIL>、<HEX>，移除過長的 log 殘段與重複符號。之後模型再去掉 the、and 這類停用詞、做簡單的詞幹化（例如 crashes 變成 crash），把字詞轉成 TF-IDF 特徵。這裡顯示的是清洗後、轉成特徵前的文字。",
  },
  past_7d_count: {
    label: "過去 7 天的回報數", group: "temporal",
    short: "建立前 7 天內，整個專案共有幾張回報",
    detail: "以這張 Ticket 的建立時間為準，往前推 7 天，數歷史資料中建立時間落在這段期間的回報有幾張。不分產品、元件或嚴重度，反映整個專案最近的回報量。",
    zero: OLD_HISTORY_NOTE,
  },
  past_30d_count: {
    label: "過去 30 天的回報數", group: "temporal",
    short: "建立前 30 天內，整個專案共有幾張回報",
    detail: "和「過去 7 天的回報數」一樣，但期間拉長到 30 天，比較能看出一段時間的整體趨勢，不容易受單日暴增影響。",
    zero: OLD_HISTORY_NOTE,
  },
  past_30d_same_or_higher_severity_count: {
    label: "過去 30 天同等或更嚴重的回報數", group: "temporal",
    short: "建立前 30 天內，嚴重度和這張相同或更高的回報有幾張",
    detail: "嚴重度由高到低是 blocker、critical、major、normal、minor、trivial、enhancement。例如這張是 major，就數過去 30 天內 blocker、critical、major 的回報。數字大代表同一段時間有很多同樣嚴重、甚至更嚴重的問題在排隊。",
    zero: OLD_HISTORY_NOTE,
  },
  author_past_count: {
    label: "回報者過去的回報數", group: "author",
    short: "同一位回報者在這張之前回報過幾張",
    detail: "用回報者帳號（歷史資料的 creator 欄位，通常是 email）比對，數他在這張 Ticket 之前回報過幾張。",
    zero: "歷史資料裡找不到這位回報者。新系統的回報者通常不在 Eclipse 歷史資料裡，所以多半是 0。",
  },
  author_past_mean_priority: {
    label: "回報者過去的平均優先級", group: "author",
    short: "這位回報者以前的回報平均被排成多少（1 = P1）",
    detail: "把這位回報者以前每張回報的優先級換成數字（P1 = 1 到 P5 = 5）再取平均。數字越小，代表他過去回報的問題越常被當成緊急。",
    zero: NO_DATA_NOTE + "這一項是 0 表示歷史資料裡找不到這位回報者。",
  },
  related_top1_priority: {
    label: "最相似回報的優先級", group: "related_report",
    short: "和這張最像的那張歷史回報是 P 幾",
    detail: "用 REP- 相似度在所有更早的歷史回報裡找出最像的一張，取它的優先級（P1 = 1 到 P5 = 5）。比對時看標題與描述的字詞，同產品、同元件會加分。",
    zero: NO_DATA_NOTE + "只有歷史資料是空的時候才會是 0。",
  },
  related_top5_avg_priority: {
    label: "最相似 5 張的平均優先級", group: "related_report",
    short: "最像的 5 張歷史回報，優先級平均是多少",
    detail: "取 REP- 相似度最高的 5 張歷史回報，把它們的優先級（P1 = 1 到 P5 = 5）取平均。比只看最像的一張穩定，不容易被單一例外影響。",
    zero: NO_DATA_NOTE + "只有歷史資料是空的時候才會是 0。",
  },
  related_similarity_top1: {
    label: "和最相似回報的相似度", group: "related_report",
    short: "和最像那張的 REP- 相似度，越大越像",
    detail: "這是 REP- 分數，不是 0 到 1 的百分比，也不能直接換算成「幾成像」。它只能拿來比較：分數越大代表越像。分數很低時，上面兩項「最相似回報的優先級」參考價值也比較低。",
  },
  severity: {
    label: "嚴重度", group: "severity",
    short: "回報者填寫的嚴重度（blocker 到 enhancement）",
    detail: "回報者填寫，或由轉換 JSON 步驟從內容抽取的嚴重度。等級由高到低是 blocker（阻斷）、critical（嚴重）、major（重大）、normal（一般）、minor（輕微）、trivial（細微）、enhancement（功能需求）。unknown 表示沒有填，模型會把它當成一個獨立的類別。",
  },
  product_past_30d_count: {
    label: "同產品過去 30 天的回報數", group: "product_component",
    short: "建立前 30 天內，同一個產品有幾張回報",
    detail: "只數和這張 Ticket 同一個產品（product 欄位）的回報。某個產品最近回報特別多，通常代表它正在出問題。",
    zero: OLD_HISTORY_NOTE + "另外，新 Ticket 的產品名稱若和 Eclipse 資料（例如 JDT、Platform）不同，也會是 0。",
  },
  component_past_30d_mean_priority: {
    label: "同元件過去 30 天的平均優先級", group: "product_component",
    short: "同一個元件最近 30 天的回報，平均被排成多少（1 = P1）",
    detail: "取建立前 30 天內、同一個元件（component 欄位）的回報，把優先級（P1 = 1 到 P5 = 5）取平均。數字越小，代表這個元件最近的問題越常被當成緊急。",
    zero: NO_DATA_NOTE + "這一項是 0 表示這 30 天內沒有同元件的回報，原因和「同元件過去 30 天的回報數」相同。",
  },
  component_past_30d_count: {
    label: "同元件過去 30 天的回報數", group: "product_component",
    short: "建立前 30 天內，同一個元件有幾張回報",
    detail: "只數和這張 Ticket 同一個元件（component 欄位）的回報。範圍比產品更小，更能看出是不是同一塊程式最近一直出問題。",
    zero: OLD_HISTORY_NOTE + "另外，新 Ticket 的元件名稱若和 Eclipse 資料（例如 UI、Debug、Core）不同，也會是 0。",
  },
};
/* Fallback formula (主系統 PriorityClassifier): six 0–1 scores combined with fixed weights. */
const FALLBACK_FACTOR_HELP = {
  textual_factor: ["24%", "標題與描述中有沒有特定字詞", "出現 data loss、security、crash、cannot login、production、outage 等字詞給 0.88；exception、timeout、failed、regression 等給 0.65；typo、cosmetic、wording 等給 0.15；都沒有給 0.45。"],
  temporal_factor: ["10%", "最近類似回報的數量", "取 Ticket 的 recent_similar_count 除以 10，最多 1。工作台匯入的 Ticket 通常沒有這個欄位，所以多半是 0。"],
  author_factor: ["8%", "回報者過去的回報數", "取 Ticket 的 reporter_past_bug_count 除以 20，最多 1。工作台匯入的 Ticket 通常沒有這個欄位，所以多半是 0。"],
  related_report_factor: ["18%", "重複偵測找到的相似 Ticket", "取重複偵測前 5 名候選的優先級（換成 0 到 1 的分數，越急越高）平均乘 0.75，加上相似度平均乘 0.25；候選都沒有優先級時，改用相似度平均。"],
  severity_factor: ["28%", "嚴重度與問題類型", "blocker、critical，或問題類型是 security、crash 時給 0.95；major、normal，或 runtime_error、build_error 給 0.70；minor、trivial、enhancement，或 ui_bug 給 0.25；其他給 0.50。"],
  component_factor: ["12%", "元件名稱", "元件名稱含 security、auth、payment、database、core 給 0.82；含 ui、frontend、docs 給 0.35；其他給 0.50。"],
};

function factorValueText(key, value) {
  if (key === "severity") return `${value}（${SEVERITY_TEXT[String(value).toLowerCase()] || "未知"}）`;
  if (typeof value !== "number") return String(value ?? "—");
  const text = Number.isInteger(value) ? String(value) : value.toFixed(3);
  if (/_priority$/.test(key) && value === 0) return `${text}（無資料）`;
  if (/_priority$/.test(key) && Number.isInteger(value) && value >= 1 && value <= 5) return `${text}（P${value}）`;
  return text;
}

const FIELD_LABELS = [
  ["ticket_id", "編號"], ["product", "產品"], ["component", "元件"], ["severity", "嚴重度"],
  ["bug_type", "問題類型"], ["os", "作業系統"], ["version", "版本"], ["reporter", "回報者"],
  ["environment", "環境"], ["error_message", "錯誤訊息", "wide mono"], ["description", "描述", "wide"],
  ["steps_to_reproduce", "重現步驟", "wide"], ["expected_behavior", "預期結果", "wide"],
  ["actual_behavior", "實際結果", "wide"], ["logs", "Log", "wide mono"],
];

const state = { tickets: [], selected: null, detail: null, filter: "all", folder: null, selecting: false, picked: new Set(), query: "", homeSignature: null, projects: [], defaultProject: "", pollTimer: null };
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

/* Queue: where a busy ticket stands and roughly how long it still needs. */
function fmtEta(seconds) {
  if (seconds == null) return "";
  if (seconds < 60) return "不到 1 分鐘";
  return `約 ${Math.round(seconds / 60)} 分鐘`;
}
const QUEUE_PHASE = {
  review: "轉換 JSON 與重複偵測",
  priority_batch: "優先級判定",
  after: "分派負責人與錯誤定位",
  localize: "錯誤定位",
};
function queueText(q, { short = false } = {}) {
  if (!q) return "";
  if (q.state === "running") {
    const what = q.phase === "priority_batch" && q.batch_size > 1 ? `優先級判定（${q.batch_size} 張一起算）` : QUEUE_PHASE[q.phase] || "處理";
    return short ? `${what}，${fmtEta(q.eta_seconds)}` : `正在執行「${what}」，預計還要${fmtEta(q.eta_seconds)}，完成後會自動更新。`;
  }
  const next = QUEUE_PHASE[q.phase] || "處理";
  return short
    ? `前面還有 ${q.ahead} 張，${fmtEta(q.eta_seconds)}`
    : `排隊中：前面還有 ${q.ahead} 張，預計${fmtEta(q.eta_seconds)}後完成「${next}」。${q.phase === "priority_batch" && q.batch_size > 1 ? `等待中的 ${q.batch_size} 張會一起計算優先級。` : ""}`;
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

function tile(t, terms, showProject = false) {
  const [label] = t.queue && t.queue.state === "waiting" ? ["排隊中"] : TICKET_STATUS[t.busy ? "running" : t.status] || [t.status];
  const h = seed(t.ticket_id || t.key);
  const blobs = `--x1:${15 + (h % 55)}%;--y1:${10 + ((h >>> 6) % 45)}%;--r1:${38 + ((h >>> 12) % 30)}px;--x2:${45 + ((h >>> 17) % 50)}%;--y2:${45 + ((h >>> 22) % 45)}%;--r2:${22 + ((h >>> 27) % 22)}px`;
  const pebbles = STAGES.map(([key, name]) => {
    const s = t.current_stage === key && (t.busy || t.status === "running") ? "running" : t.stage_status[key];
    return `<i data-s="${esc(s)}" title="${esc(name)}：${esc(STATUS_TEXT[s] || s)}"></i>`;
  }).join("");
  const attention = (t.attention || []).find((a) => !a.includes("備援"));
  const meta = [
    showProject ? `<span class="tag tag-project" title="專案">${markTerms(projectName(projectKey(t)), terms)}</span>` : "",
    t.component ? `<span class="tag">${markTerms(t.component, terms)}</span>` : "",
    t.assignee ? `<span class="tag tag-person ${t.assignee_confirmed ? "is-confirmed" : ""}" title="${t.assignee_confirmed ? "已確認負責人" : "建議負責人"}">${markTerms(t.assignee, terms)}</span>` : "",
    t.duplicate_of ? `<span class="tag">重複 ${markTerms(t.duplicate_of, terms)}</span>` : "",
  ].join("");
  const picked = state.selecting && state.picked.has(t.key);
  return `<li class="tile ${state.selecting ? "is-selecting" : ""} ${picked ? "is-picked" : ""}" data-tone="${coverTone(t)}" data-key="${esc(t.key)}">
    ${state.selecting ? `<span class="pick" aria-hidden="true">${picked ? "✓" : ""}</span>` : ""}
    <div class="tile-cover" style="${blobs}">
      <span class="tile-status">${esc(label)}</span>
      <span class="pebbles" aria-label="處理進度">${pebbles}</span>
      ${t.priority ? `<span class="sticker" data-p="${esc(t.priority)}" title="優先級 ${esc(t.priority)}">${esc(t.priority)}</span>` : ""}
    </div>
    <div class="tile-body">
      <div class="tile-top"><span class="tile-id">${markTerms(t.ticket_id, terms)}</span><time>${esc(fmtTime(t.created_at))}</time></div>
      <a class="tile-link" href="#/t/${encodeURIComponent(t.key)}"><span class="tile-title">${markTerms(t.title, terms)}</span></a>
      ${meta ? `<div class="tile-meta">${meta}</div>` : ""}
      ${t.queue ? `<p class="tile-queue">${esc(queueText(t.queue, { short: true }))}</p>` : attention ? `<p class="tile-attention">${esc(attention)}</p>` : ""}
    </div>
    ${state.selecting ? "" : `<button type="button" class="pin-btn ${t.pinned ? "is-on" : ""}" data-pin="${esc(t.key)}" aria-pressed="${t.pinned}" title="${t.pinned ? "取消釘選" : "釘選到上方"}" aria-label="${t.pinned ? "取消釘選" : "釘選"} ${esc(t.ticket_id)}">${PIN_ICON}</button>`}
  </li>`;
}

/* Projects are the folders of the home page. A ticket's project is the repository chosen at import
   (integration_config.json → projects); tickets without one go to the "未指定專案" folder. */
const NO_PROJECT = "__none__";
const projectKey = (t) => t.project || NO_PROJECT;
const projectName = (key) => (key === NO_PROJECT ? "未指定專案" : key);
const folderHref = (key) => `#/p/${encodeURIComponent(key)}`;

function folderList() {
  const configured = state.projects.map((p) => p.project);
  const used = [...new Set(state.tickets.map(projectKey))];
  const keys = [...configured, ...used.filter((k) => k !== NO_PROJECT && !configured.includes(k))];
  if (used.includes(NO_PROJECT)) keys.push(NO_PROJECT);
  return keys.map((key) => {
    const config = state.projects.find((p) => p.project === key);
    const tickets = state.tickets.filter((t) => projectKey(t) === key);
    const counts = { review: 0, running: 0, done: 0 };
    tickets.forEach((t) => { const b = bucket(t); if (b in counts) counts[b] += 1; });
    const latest = tickets.reduce((acc, t) => (String(t.updated_at || "") > acc ? String(t.updated_at || "") : acc), "");
    return { key, config, tickets, counts, latest, pinned: tickets.filter((t) => t.pinned).length };
  });
}

function folderCard(f) {
  const name = projectName(f.key);
  const desc = f.key === NO_PROJECT ? "匯入時沒有選專案，這些 Ticket 會略過錯誤定位。"
    : f.config ? (f.config.description || f.config.repo_path || "") : "設定檔沒有這個專案的 Repository，這些 Ticket 會略過錯誤定位。";
  // Up to three papers peek out of the folder, coloured by the status of the most recent tickets.
  const papers = f.tickets.slice(0, 3).map((t, i) => `<i data-tone="${coverTone(t)}" style="--i:${i}"></i>`).join("");
  const stats = [
    `<span class="fstat">共 <b>${f.tickets.length}</b> 張</span>`,
    f.counts.review ? `<span class="fstat fstat-gate">等你確認 <b>${f.counts.review}</b></span>` : "",
    f.counts.running ? `<span class="fstat fstat-running">處理中 <b>${f.counts.running}</b></span>` : "",
    f.counts.done ? `<span class="fstat fstat-done">已完成 <b>${f.counts.done}</b></span>` : "",
  ].join("");
  return `<li class="folder ${f.tickets.length ? "" : "is-empty"}" data-folder="${esc(f.key)}">
    <div class="folder-back"><span class="folder-tab"></span><span class="papers">${papers}</span></div>
    <div class="folder-front">
      <div class="folder-top">
        <a class="folder-link" href="${folderHref(f.key)}"><h3>${esc(name)}</h3></a>
        ${f.pinned ? `<span class="folder-pins" title="${f.pinned} 張釘選">${PIN_ICON}${f.pinned}</span>` : ""}
      </div>
      <p class="folder-desc" title="${esc(desc)}">${esc(desc)}</p>
      <div class="folder-stats">${stats}</div>
      <p class="folder-foot">${f.tickets.length ? `最近更新 ${esc(fmtTime(f.latest))}` : "還沒有 Ticket"}</p>
    </div>
  </li>`;
}

function visibleTickets() {
  const terms = searchTerms();
  const scope = state.folder === null ? state.tickets : state.tickets.filter((t) => projectKey(t) === state.folder);
  const matched = terms.length ? scope.filter((t) => { const hay = haystack(t); return terms.every((x) => hay.includes(x)); }) : scope;
  return { terms, scope, matched, shown: matched.filter((t) => state.filter === "all" || bucket(t) === state.filter) };
}

function renderInbox() {
  const { terms, scope, matched, shown } = visibleTickets();
  const signature = JSON.stringify([state.tickets, state.projects, state.filter, state.query, state.folder, state.selecting, [...state.picked]]);
  if (signature === state.homeSignature) return; // nothing changed: keep hover and focus
  state.homeSignature = signature;

  const atRoot = state.folder === null;
  const showFolders = atRoot && !terms.length;
  const none = state.tickets.length === 0 && state.projects.length === 0;
  $("#inboxEmpty").hidden = !none;
  $("#folderSection").hidden = !showFolders || none;
  $$(".shelf").forEach((el) => (el.hidden = showFolders || none));
  $(".home-bar").hidden = showFolders || none;

  if (atRoot) {
    const folders = folderList();
    const review = state.tickets.filter((t) => bucket(t) === "review").length;
    $("#homeHead").innerHTML = terms.length
      ? `<h1 class="home-title">搜尋全部專案</h1>`
      : `<h1 class="home-title">專案</h1><p class="home-sub">${folders.length} 個專案，共 ${state.tickets.length} 張 Ticket${review ? `，<b class="gate-text">${review} 張等你確認</b>` : ""}。點資料夾查看裡面的 Ticket。</p>`;
    $("#folderGrid").innerHTML = folders.map(folderCard).join("");
    if (showFolders) return;
  } else {
    const config = state.projects.find((p) => p.project === state.folder);
    const desc = state.folder === NO_PROJECT ? "匯入時沒有選專案，這些 Ticket 會略過錯誤定位。" : config ? [config.description, config.repo_path].filter(Boolean).join("　") : "設定檔沒有這個專案的 Repository，這些 Ticket 會略過錯誤定位。";
    $("#homeHead").innerHTML = `<div class="folder-head">
        <a class="back-link" href="#/" title="回到所有專案" aria-label="回到所有專案"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14.5 6 8.5 12l6 6" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg></a>
        <div class="folder-head-text"><p class="crumb"><a href="#/">所有專案</a></p><h1 class="home-title">${esc(projectName(state.folder))}</h1>${desc ? `<p class="home-sub">${esc(desc)}</p>` : ""}</div>
        <div class="folder-head-actions">
          <button type="button" class="btn btn-quiet btn-small" data-open-import>匯入到這個專案</button>
          <button type="button" class="btn btn-danger-quiet btn-small" data-delete-project>刪除專案</button>
        </div>
      </div>`;
  }

  const counts = { all: matched.length, review: 0, running: 0, done: 0 };
  matched.forEach((t) => { const b = bucket(t); if (b in counts) counts[b] += 1; });
  $$("[data-count]").forEach((el) => (el.textContent = counts[el.dataset.count]));

  // Inside a folder: pinned shelf on top, the rest below. Searching all projects: one result list.
  const pinnedAll = scope.filter((t) => t.pinned);
  const pinned = atRoot ? [] : shown.filter((t) => t.pinned).sort((a, b) => String(a.pinned_at).localeCompare(String(b.pinned_at)));
  const others = atRoot ? shown : shown.filter((t) => !t.pinned);
  $(".shelf-pinned").hidden = atRoot || none;
  const showProject = atRoot; // search across projects: say which folder each card is in
  if (!$("#homeView").hidden) {
    // The ticket page's previous/next buttons follow exactly what this view shows.
    state.navKeys = [...pinned, ...others].map((t) => t.key);
    state.navLabel = terms.length ? `「${state.query.trim()}」的搜尋結果` : state.filter !== "all" ? FILTER_TEXT[state.filter] : "";
  }
  $("#pinnedCount").textContent = pinned.length || "";
  $("#otherCount").textContent = others.length || "";
  $("#otherTitle").firstChild.textContent = atRoot ? "搜尋結果 " : "其他 Ticket ";
  $("#pinnedTiles").innerHTML = pinned.length ? pinned.map((t) => tile(t, terms, showProject)).join("")
    : pinnedAll.length ? `<li class="tile-note">釘選的 Ticket 裡沒有符合條件的。</li>`
    : atRoot ? `<li class="tile-note">沒有符合的釘選 Ticket。</li>`
    : `<li class="tile-hint">點任一張卡片右上角的圖釘，常看的 Ticket 就會固定在這裡。</li>`;
  $("#otherTiles").innerHTML = others.length ? others.map((t) => tile(t, terms, showProject)).join("")
    : `<li class="tile-note">${!scope.length ? "這個專案還沒有 Ticket，按右上角「匯入到這個專案」開始。" : terms.length || state.filter !== "all" ? "沒有符合條件的 Ticket。" : "所有 Ticket 都已釘選。"}</li>`;
  $("#searchSummary").innerHTML = terms.length
    ? `${atRoot ? "" : `在「${esc(projectName(state.folder))}」`}找到 ${shown.length} 張 <button type="button" class="link-btn" data-clear-search>清除搜尋</button>${atRoot ? "" : ` <a class="link-btn" href="#/">改搜尋全部專案</a>`}` : "";
}

async function loadTickets() {
  const data = await api("/api/tickets");
  state.tickets = data.tickets;
  const q = data.queue || {};
  const pill = $("#queuePill");
  if (pill) {
    const running = (q.running || []).length;
    pill.hidden = !running && !q.waiting;
    const batch = q.priority_batch || 0;
    pill.innerHTML = `<i></i>${running ? `處理中 ${running} 張${batch > 1 ? `（${batch} 張一起算優先級）` : ""}` : ""}${q.waiting ? `${running ? "，" : ""}排隊 ${q.waiting} 張` : ""}`;
  }
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

/* ------------------------------------------------- previous / next ticket */
const FILTER_TEXT = { review: "等你確認", running: "處理中", done: "已完成" };

/* Tickets of the same project in the order the folder showed them (pinned first). If the ticket was
   opened from a link instead, fall back to the folder's default order. */
function navContext(r) {
  const project = projectKey(r);
  const inProject = (key) => { const t = state.tickets.find((x) => x.key === key); return t && projectKey(t) === project; };
  let keys = (state.navKeys || []).filter(inProject);
  let label = state.navLabel || "";
  if (!keys.includes(r.key)) {
    const all = state.tickets.filter((t) => projectKey(t) === project);
    const pinned = all.filter((t) => t.pinned).sort((a, b) => String(a.pinned_at).localeCompare(String(b.pinned_at)));
    keys = [...pinned, ...all.filter((t) => !t.pinned)].map((t) => t.key);
    label = "";
  }
  return { keys, index: keys.indexOf(r.key), label };
}

function ticketNav(r) {
  const { keys, index, label } = navContext(r);
  if (keys.length < 2 || index < 0) return "";
  const title = (key) => { const t = state.tickets.find((x) => x.key === key); return t ? `${t.ticket_id}：${t.title}` : key; };
  const prev = keys[index - 1];
  const next = keys[index + 1];
  const arrow = (d) => `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${d}" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
  return `<nav class="ticket-nav" aria-label="同專案的其他 Ticket">
    <button type="button" class="tn-btn" data-goto="${prev ? esc(prev) : ""}" ${prev ? "" : "disabled"} title="${prev ? `上一張（[）${esc(title(prev))}` : "已經是第一張"}" aria-label="上一張">${arrow("M14.5 6 8.5 12l6 6")}</button>
    <span class="tn-pos">${label ? `<small>${esc(label)}</small>` : ""}${index + 1} / ${keys.length}</span>
    <button type="button" class="tn-btn" data-goto="${next ? esc(next) : ""}" ${next ? "" : "disabled"} title="${next ? `下一張（]）${esc(title(next))}` : "已經是最後一張"}" aria-label="下一張">${arrow("M9.5 6l6 6-6 6")}</button>
  </nav>`;
}

function gotoTicket(step) {
  if (!state.detail) return;
  const { keys, index } = navContext(state.detail);
  const target = keys[index + step];
  if (target) location.hash = `#/t/${encodeURIComponent(target)}`;
}

/* ------------------------------------------------------------ deleting */
function setSelecting(on, { render = true } = {}) {
  state.selecting = on;
  state.picked = new Set();
  const button = $("#selectModeButton");
  if (button) button.textContent = on ? "完成" : "選取";
  renderPickBar();
  if (render) { state.homeSignature = null; renderInbox(); }
}

function renderPickBar() {
  $("#pickBar").hidden = !state.selecting;
  $("#pickCount").textContent = state.picked.size;
  $("[data-pick-delete]").disabled = state.picked.size === 0;
  $("[data-pick-rerun]").disabled = state.picked.size === 0;
}

async function rerunPicked() {
  const keys = [...state.picked];
  if (!keys.length) return;
  if (!confirm(`把選取的 ${keys.length} 張 Ticket 從頭重新執行？\n\n每張都會重新跑五個步驟，原本的處理結果會被新的結果取代。`)) return;
  let started = 0, skipped = 0;
  for (const key of keys) {
    try { await post(`/api/tickets/${encodeURIComponent(key)}/run`); started += 1; } catch { skipped += 1; }
  }
  toast(`已開始重新執行 ${started} 張${skipped ? `；${skipped} 張正在處理，略過` : ""}`);
  setSelecting(false);
  await refresh();
}

async function deletePicked() {
  const keys = [...state.picked];
  if (!keys.length) return;
  const ids = keys.map((key) => (state.tickets.find((t) => t.key === key) || {}).ticket_id || key);
  const preview = ids.slice(0, 5).join("、") + (ids.length > 5 ? ` 等 ${ids.length} 張` : "");
  if (!confirm(`刪除 ${preview}？\n\n這些 Ticket 的處理結果會一起刪除，無法復原。`)) return;
  try {
    const result = await post("/api/delete-tickets", { keys });
    toast(`已刪除 ${result.deleted.length} 張${result.skipped.length ? `；${result.skipped.length} 張正在處理，沒有刪除` : ""}`);
    setSelecting(false);
    await refresh();
  } catch (err) { toast(err.message); }
}

async function openDeleteProject() {
  const name = state.folder;
  if (name === null) return;
  const label = projectName(name);
  const usage = await api(`/api/projects/${encodeURIComponent(name)}/usage`);
  const option = (key, checked, disabled, text) =>
    `<label class="check del-option"><input type="checkbox" data-del="${key}" ${checked ? "checked" : ""} ${disabled ? "disabled" : ""}><span>${text}</span></label>`;
  $("#deleteProjectTitle").textContent = `刪除專案「${label}」`;
  $("#deleteProjectBody").innerHTML = `
    <p class="hint">勾選要刪除的內容。刪除後無法復原。</p>
    ${option("tickets", usage.tickets > 0, usage.tickets === 0, `刪除這個專案的 <b>${usage.tickets}</b> 張 Ticket（含處理結果）`)}
    ${name === NO_PROJECT ? "" : option("history", usage.history_rows > 0, usage.history_rows === 0, `刪除這個專案的 <b>${usage.history_rows}</b> 筆比對歷史（重複偵測與負責人推薦用的資料）`)}
    ${usage.configured ? option("config", true, false, `從設定檔移除這個專案（程式碼與版本快照不會刪除；設定檔會先備份成 <code>.bak</code>）`) : ""}
    ${usage.busy.length ? `<p class="form-error">有 ${usage.busy.length} 張 Ticket 正在處理或排隊，請等它們處理完再刪除。</p>` : ""}
    ${!usage.configured && name !== NO_PROJECT ? `<p class="hint">這個專案不在設定檔裡；Ticket 和比對歷史都刪除後，資料夾就會消失。</p>` : ""}`;
  $("#deleteProjectConfirm").disabled = usage.busy.length > 0;
  $("#deleteProjectDialog").showModal();
}

async function confirmDeleteProject() {
  const name = state.folder;
  const picked = (key) => { const box = $(`#deleteProjectBody [data-del="${key}"]`); return Boolean(box && box.checked && !box.disabled); };
  const body = { tickets: picked("tickets"), history: picked("history"), config: picked("config") };
  if (!body.tickets && !body.history && !body.config) { toast("沒有勾選要刪除的內容"); return; }
  const button = $("#deleteProjectConfirm");
  try {
    button.disabled = true;
    const result = await post(`/api/projects/${encodeURIComponent(name)}/delete`, body);
    $("#deleteProjectDialog").close();
    const parts = [
      result.tickets_removed.length ? `${result.tickets_removed.length} 張 Ticket` : "",
      result.history_rows_removed ? `${result.history_rows_removed} 筆比對歷史` : "",
    ].filter(Boolean);
    const removedConfig = result.config_removed ? `已從設定檔移除「${projectName(name)}」` : "";
    toast([parts.length ? `已刪除 ${parts.join("、")}` : "", removedConfig].filter(Boolean).join("，") || "沒有需要刪除的內容");
    goHome();
    await refresh();
  } catch (err) {
    toast(err.message);
  } finally {
    button.disabled = false;
  }
}

/* ----------------------------------------------------------------- routing */
/* #/ = project folders, #/p/<project> = tickets in one project, #/t/<key> = one ticket. */
function goHome(folder = null) {
  const target = folder === null ? "#/" : folderHref(folder);
  if (location.hash !== target && !(target === "#/" && !location.hash)) location.hash = target;
  else route();
}
async function route() {
  const ticket = location.hash.match(/^#\/t\/(.+)$/);
  const folder = location.hash.match(/^#\/p\/(.+)$/);
  const key = ticket ? decodeURIComponent(ticket[1]) : null;
  $("#homeView").hidden = Boolean(key);
  $("#detail").hidden = !key;
  if (!key) {
    const nextFolder = folder ? decodeURIComponent(folder[1]) : null;
    if (nextFolder !== state.folder) state.filter = "all";
    if (nextFolder !== state.folder || state.selecting) setSelecting(false, { render: false });
    state.folder = nextFolder;
    $$(".filter").forEach((x) => { const on = x.dataset.filter === state.filter; x.classList.toggle("is-active", on); x.setAttribute("aria-selected", on); });
    state.selected = null;
    state.detail = null;
    state.detailSignature = null;
    document.title = state.folder === null ? "Ticket 工作台" : `${projectName(state.folder)} · Ticket 工作台`;
    state.homeSignature = null;
    renderInbox();
    $("#homeView").scrollTop = 0;
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
  else if (r.queue) hint = `<div class="pin-hint pin-info"><span>${esc(queueText(r.queue))}</span></div>`;
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
        <a class="back-link" href="${folderHref(projectKey(r))}" title="回到「${esc(projectName(projectKey(r)))}」" aria-label="回到「${esc(projectName(projectKey(r)))}」"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14.5 6 8.5 12l6 6" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg></a>
        <div class="pin-title">
          <a class="pin-project" href="${folderHref(projectKey(r))}">${esc(projectName(projectKey(r)))}</a>
          <span class="id">${esc(r.ticket_id)}</span>
          <h2 title="${esc(r.title)}">${esc(r.title)}</h2>
        </div>
        ${ticketNav(r)}
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
  return `<p class="hint">${esc(o.decision_policy || "")}；比對了 ${o.history_size ?? "?"} 張${o.project_scope ? `「${esc(o.project_scope)}」及未標專案的` : ""}歷史 Ticket${[o.other_project_rows_skipped ? `其他專案 ${o.other_project_rows_skipped} 張` : "", o.later_rows_skipped ? `比這張晚建立的 ${o.later_rows_skipped} 張` : ""].filter(Boolean).length ? `（已略過${[o.other_project_rows_skipped ? `其他專案 ${o.other_project_rows_skipped} 張` : "", o.later_rows_skipped ? `比這張晚建立的 ${o.later_rows_skipped} 張` : ""].filter(Boolean).join("、")}）` : ""}。${hint}</p>${summary}<ol class="cands" style="margin-top:10px">${head}</ol>${more}${decisionLine}`;
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
  const groupHead = (name, helpKey, short) => `<h4>${esc(FACTOR_TEXT[name] || name)}<button type="button" class="fx-q" data-factor-help="${esc(helpKey)}" data-tip="${esc(short)}" aria-label="${esc(FACTOR_TEXT[name] || name)}說明">?</button></h4>`;
  if (o.factor_values) {
    factors = Object.entries(o.factor_values).map(([name, vals]) => {
      const head = groupHead(name, `group:${name}`, (FACTOR_GROUP_HELP[name] || [""])[0]);
      if (name === "textual") {
        const h = FACTOR_HELP.summary_terms;
        return `<div class="factor">${head}<button type="button" class="fx" data-factor-help="summary_terms" data-tip="${esc(h.short)}">${esc(h.label)}</button><p class="factor-text">${esc(vals.summary_terms || "（空白）")}</p></div>`;
      }
      return `<div class="factor">${head}<dl>${Object.entries(vals).map(([k, v]) => {
        const h = FACTOR_HELP[k];
        const label = h ? `${esc(h.label)}<small>${esc(k)}</small>` : esc(k);
        return `<dt><button type="button" class="fx" data-factor-help="${esc(k)}" data-tip="${esc(h ? h.short : "點一下看說明")}">${label}</button></dt><dd>${esc(factorValueText(k, v))}</dd>`;
      }).join("")}</dl></div>`;
    }).join("");
  } else if (o.factor_scores) {
    factors = Object.entries(o.factor_scores).map(([k, v]) => {
      const h = FALLBACK_FACTOR_HELP[k];
      return `<div class="factor">${groupHead(k, `score:${k}`, h ? `${h[1]}（權重 ${h[0]}）` : "點一下看說明")}${scoreRow("分數", v)}</div>`;
    }).join("");
  }
  if (factors) factors += `<p class="hint factor-hint">滑鼠移到項目上可以看簡短說明，點一下會開啟每一項的詳細解釋。</p>`;
  return `<div class="priority-main">
      <div class="p-badge">${esc(o.predicted_priority || "—")}<small>${esc(o.priority_name || "")}${o.confidence != null ? `，信心 ${num(o.confidence, 2)}` : ""}</small></div>
      ${distHtml}
      ${o.reported_priority ? `<span class="chip">回報者填寫 ${esc(o.reported_priority)}</span>` : ""}
    </div>
    <div class="factor-grid">${factors}</div>`;
}

/* ------------------------------------------- priority factor explanations */
function openFactorHelp(target) {
  const o = ((state.detail || {}).stages.priority || {}).output || {};
  let html;
  if (o.factor_values) {
    let historyNoteShown = false;
    const zeroNote = (text) => {
      if (!text.startsWith(OLD_HISTORY_NOTE)) return text;
      if (!historyNoteShown) { historyNoteShown = true; return text; }
      return "原因和前面相同：參考的歷史資料比這張 Ticket 早很多，這段期間內找不到任何回報。" + text.slice(OLD_HISTORY_NOTE.length);
    };
    html = `<p class="fx-intro">這些數字是 PRI 模型的<b>輸入特徵</b>：模型從 Eclipse 歷史資料學會怎麼組合它們來判斷優先級，所以單看某一項的大小，不能直接看出它把結果推向哪一級，也不是各因子的貢獻比例。畫面只列出幾項代表性的特徵，模型實際用到的更多，例如過去 1／3 天的回報數、各優先級所占比例、錯誤關鍵字。</p>` +
      Object.entries(o.factor_values).map(([name, vals]) => {
        const [, groupDetail] = FACTOR_GROUP_HELP[name] || ["", ""];
        const items = Object.entries(vals).map(([k, v]) => {
          const h = FACTOR_HELP[k] || { label: k, detail: "這一項沒有說明。" };
          const isZero = v === 0 || v === "0";
          return `<article class="fx-item" data-help-key="${esc(k)}">
            <header><h4>${esc(h.label)}</h4><code>${esc(k)}</code><span class="fx-value">${k === "summary_terms" ? "見下方" : `這張：<b>${esc(factorValueText(k, v))}</b>`}</span></header>
            <p>${esc(h.detail)}</p>
            ${k === "summary_terms" ? `<p class="fx-sample">${esc(v || "（空白）")}</p>` : ""}
            ${isZero && h.zero ? `<p class="fx-zero"><b>為什麼是 0？</b>${esc(zeroNote(h.zero))}</p>` : ""}
          </article>`;
        }).join("");
        return `<section class="fx-group" data-help-key="group:${esc(name)}"><h3>${esc(FACTOR_TEXT[name] || name)}</h3><p class="fx-group-desc">${esc(groupDetail)}</p>${items}</section>`;
      }).join("");
  } else if (o.factor_scores) {
    html = `<p class="fx-intro">這張 Ticket 的優先級是用<b>備援公式</b>算的（主系統的 PriorityClassifier），不是 PRI 訓練好的模型。六個因子各算出 0 到 1 的分數，越接近 1 越緊急，再依下面的權重加總：0.85 以上是 P1，0.65 以上是 P2，0.40 以上是 P3，以此類推。</p>` +
      Object.entries(o.factor_scores).map(([k, v]) => {
        const [weight, short, detail] = FALLBACK_FACTOR_HELP[k] || ["—", k, "這一項沒有說明。"];
        return `<article class="fx-item" data-help-key="score:${esc(k)}">
          <header><h4>${esc(FACTOR_TEXT[k] || k)}</h4><code>${esc(k)}</code><span class="fx-value">權重 <b>${esc(weight)}</b>，這張 <b>${esc(num(v, 2))}</b></span></header>
          <p><b>${esc(short)}。</b>${esc(detail)}</p></article>`;
      }).join("");
  } else {
    return;
  }
  $("#factorBody").innerHTML = html;
  const dialog = $("#factorDialog");
  if (!dialog.open) dialog.showModal();
  const focus = $(`#factorBody [data-help-key="${CSS.escape(target)}"]`);
  $$("#factorBody .is-focus").forEach((el) => el.classList.remove("is-focus"));
  if (focus) {
    focus.classList.add("is-focus");
    requestAnimationFrame(() => focus.scrollIntoView({ block: target.startsWith("group:") ? "start" : "center" }));
  } else {
    $("#factorDialog .sheet-inner").scrollTop = 0;
  }
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

/* Localization runs in the code of the ticket's own project. Using another project's code is a
   deliberate, confirmed move: the ticket changes folder, and it only makes sense when the ticket
   really belongs to that project. */
function locControls(r) {
  if (!state.projects.length) return `<p class="hint" style="margin-top:10px">設定檔裡還沒有任何專案，請在 integration_config.json 的 projects 加入 repo_path。</p>`;
  const current = r.project || "";
  const hasCode = state.projects.some((p) => p.project === current);
  const others = state.projects.filter((p) => p.project !== current);
  const rerun = hasCode
    ? `<div class="loc-controls"><button class="btn btn-quiet btn-small" data-action="localize" data-project="${esc(current)}">${r.stages.localization.status === "done" ? "重新定位" : "執行錯誤定位"}</button><span class="hint">使用「${esc(current)}」的程式碼</span></div>`
    : `<p class="loc-note">${current ? `要定位這張 Ticket，請在設定檔 projects 加入「${esc(current)}」的 repo_path，再按「執行錯誤定位」。` : "要定位這張 Ticket，請先指定它屬於哪個專案。"}如果它其實屬於下面某個專案，也可以手動改過去。</p>`;
  const options = `<option value="">請選擇專案</option>` + others.map((p) => `<option value="${esc(p.project)}">${esc(p.project)}</option>`).join("");
  const other = others.length ? `<details class="loc-other">
      <summary>${hasCode ? "改用其他專案的程式碼定位…" : "手動指定專案…"}</summary>
      <div class="loc-controls"><select id="locProject" aria-label="要改到哪個專案">${options}</select>
        <button class="btn btn-quiet btn-small" data-action="localize-other">改到這個專案並定位</button></div>
      <p class="hint">只在這張 Ticket 真的屬於那個專案時使用。Ticket 會移到那個專案的資料夾，並用那份程式碼重新定位。</p>
    </details>` : "";
  return `<div class="loc-box">${rerun}${other}</div>`;
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
  const go = event.target.closest("[data-goto]");
  if (go) { if (go.dataset.goto) location.hash = `#/t/${encodeURIComponent(go.dataset.goto)}`; return; }
  const help = event.target.closest("[data-factor-help]");
  if (help && state.detail) { openFactorHelp(help.dataset.factorHelp); return; }
  const tabBtn = event.target.closest("[data-tab]");
  if (tabBtn && state.detail) { selectTab(tabBtn.dataset.tab); return; }
  const btn = event.target.closest("[data-action]");
  if (!btn || !state.detail) return;
  const key = state.detail.key;
  const path = `/api/tickets/${encodeURIComponent(key)}`;
  if (btn.dataset.action === "pin") { await togglePin(key); return; }
  try {
    btn.disabled = true;
    if (["rerun", "not-duplicate", "is-duplicate", "localize", "localize-other"].includes(btn.dataset.action)) state.tabPinned = false;
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
        await post(`${path}/localize`, { project: btn.dataset.project });
        toast("已開始錯誤定位");
        break;
      case "localize-other": {
        const target = ($("#locProject") || {}).value;
        if (!target) { toast("請先選擇專案"); btn.disabled = false; return; }
        const from = state.detail.project || "未指定專案";
        if (!confirm(`會把這張 Ticket 從「${from}」移到「${target}」資料夾，並用「${target}」的程式碼做錯誤定位。\n\n只有這張 Ticket 真的屬於「${target}」時才這樣做。確定嗎？`)) { btn.disabled = false; return; }
        await post(`${path}/localize`, { project: target });
        toast(`已移到「${target}」並開始錯誤定位`);
        break;
      }
      case "delete":
        if (!confirm(`刪除 ${state.detail.ticket_id}？\n\n這張 Ticket 的處理結果會一起刪除，無法復原。`)) { btn.disabled = false; return; }
        await api(path, { method: "DELETE" });
        toast(`已刪除 ${state.detail.ticket_id}`);
        goHome(projectKey(state.detail));
        break;
    }
    await refresh();
  } catch (err) {
    toast(err.message);
    btn.disabled = false;
  }
}

async function refresh() {
  try { const pj = await api("/api/projects"); state.projects = pj.projects; } catch { /* keep the last list */ }
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
let importFileProjects = null; // {project: count} for records that name their own project

function recordsFromText(text, name) {
  const trimmed = text.trim();
  if (!trimmed || /\.csv$/i.test(name)) return [];
  try {
    const data = JSON.parse(trimmed);
    if (Array.isArray(data)) return data;
    if (data && Array.isArray(data.tickets)) return data.tickets;
    return [data];
  } catch {
    return trimmed.split(/\r?\n/).flatMap((line) => {
      try { return line.trim() ? [JSON.parse(line)] : []; } catch { return []; }
    });
  }
}

function importProjectOptions(history) {
  const select = $("#importProject");
  if (select.dataset.history === String(history) && select.options.length) return;
  const previous = select.dataset.history ? select.value : null;
  const preferred = previous ?? (state.folder && state.folder !== NO_PROJECT ? state.folder : state.folder === NO_PROJECT ? "" : state.defaultProject);
  const none = history ? "不指定（和所有專案的 Ticket 比對）" : state.projects.length ? "不指定專案（略過錯誤定位）" : "（設定檔沒有專案，將略過錯誤定位）";
  const options = state.projects.map((p) => [p.project, history ? p.project : `${p.project}　${p.repo_path}`]);
  if (history || !state.projects.length || preferred === "") options.push(["", none]);
  select.innerHTML = options.map(([value, label]) => `<option value="${esc(value)}">${esc(label)}</option>`).join("");
  select.value = options.some(([value]) => value === preferred) ? preferred : options[0][0];
  select.dataset.history = String(history);
}

// The project dropdown only applies to tickets that do not name a project
// themselves, so it is hidden when every record in the file already does.
function renderImportProjectNote() {
  const note = $("#importProjectNote");
  const history = importMode === "file" && $("#asHistory").checked;
  importProjectOptions(history);
  const counts = importMode === "file" ? importFileProjects : null;
  const named = counts ? Object.entries(counts).filter(([p]) => p) : [];
  const unnamed = counts ? counts[""] || 0 : 0;
  $(".project-field").hidden = named.length > 0 && !unnamed;
  const base = history ? "這批歷史 Ticket 屬於哪個專案（只會和這個專案的 Ticket 比對）" : "錯誤定位要用的專案（Repository）";
  $("#importProjectLabel").textContent = named.length && unnamed
    ? `其餘 ${unnamed} 張沒寫專案的${history ? "歷史 Ticket 屬於哪個專案" : " Ticket，錯誤定位要用的專案（Repository）"}`
    : base;
  if (!named.length) { note.hidden = true; note.innerHTML = ""; return; }
  const known = new Set(state.projects.map((p) => p.project));
  const list = named.map(([p, n]) => `<li><b>${esc(p)}</b> ${n} 張${known.has(p) ? "" : `<span class="note-warn">（設定檔沒有這個專案，錯誤定位會略過）</span>`}</li>`).join("");
  note.innerHTML = history
    ? `<p>${unnamed ? "這個檔案有些" : "這個檔案的"}歷史 Ticket 已經寫好自己的專案，只會和同一個專案的新 Ticket 比對：</p><ul>${list.replace(/<span class="note-warn">.*?<\/span>/g, "")}</ul>`
    : `<p>${unnamed ? "這個檔案有些" : "這個檔案的"} Ticket 已經寫好自己的專案，會照檔案分到各自的資料夾，錯誤定位也用各自專案的程式碼：</p><ul>${list}</ul>`;
  note.hidden = false;
}

function openImport() {
  importFile = null;
  importFileProjects = null;
  $("#dropFile").textContent = "";
  $("#importError").textContent = "";
  $("#asHistory").checked = false;
  $("#importForm").reset();
  delete $("#importProject").dataset.history;
  importProjectOptions(false);
  setImportMode("file");
  $("#importDialog").showModal();
}

function setImportMode(mode) {
  importMode = mode;
  $$(".seg").forEach((b) => b.classList.toggle("is-active", b.dataset.mode === mode));
  $$("[data-pane]").forEach((p) => (p.hidden = p.dataset.pane !== mode));
  const history = mode === "file" && $("#asHistory").checked;
  $("#importSubmit").textContent = history ? "加入歷史資料庫" : "匯入並開始處理";
  renderImportProjectNote();
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
      else {
        const named = importMode === "file" && importFileProjects ? Object.keys(importFileProjects) : [];
        const target = named.length > 1 ? null : (named[0] || body.project || NO_PROJECT);
        goHome(target);
      }
    }
    await refresh();
  } catch (err) {
    $("#importError").textContent = err.message;
  } finally {
    $("#importSubmit").disabled = false;
  }
}

async function pickFile(file) {
  importFile = file;
  importFileProjects = null;
  $("#dropFile").textContent = file ? `${file.name}（${Math.ceil(file.size / 1024)} KB）` : "";
  if (file) {
    try {
      const records = recordsFromText(await file.text(), file.name);
      const counts = {};
      for (const r of records) {
        if (!r || typeof r !== "object") continue;
        const p = String(r.project || "");
        counts[p] = (counts[p] || 0) + 1;
      }
      if (importFile === file) importFileProjects = counts;
    } catch { /* the server reports parse errors on import */ }
  }
  renderImportProjectNote();
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
    if (e.target.closest("[data-open-import]")) { openImport(); return; }
    if (e.target.closest("[data-delete-project]")) { openDeleteProject(); return; }
    if (e.target.closest("[data-select-mode]")) { setSelecting(!state.selecting); return; }
    if (state.selecting) {
      const picked = e.target.closest(".tile");
      if (picked) {
        e.preventDefault();
        state.picked.has(picked.dataset.key) ? state.picked.delete(picked.dataset.key) : state.picked.add(picked.dataset.key);
        renderInbox();
        renderPickBar();
      }
      return;
    }
    const folder = e.target.closest(".folder");
    if (folder && !e.target.closest("a")) { location.hash = folderHref(folder.dataset.folder); return; }
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
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.target.closest("input, textarea, select, [contenteditable]") || document.querySelector("dialog[open]")) return;
    if ((e.key === "[" || e.key === "]") && state.selected) { e.preventDefault(); gotoTicket(e.key === "]" ? 1 : -1); return; }
    if (e.key !== "/") return;
    e.preventDefault();
    search.focus();
    search.select();
  });
  window.addEventListener("hashchange", route);
  $("#pickBar").addEventListener("click", (e) => {
    if (e.target.closest("[data-pick-cancel]")) setSelecting(false);
    else if (e.target.closest("[data-pick-delete]")) deletePicked();
    else if (e.target.closest("[data-pick-rerun]")) rerunPicked();
    else if (e.target.closest("[data-pick-all]")) {
      $$("#homeView .tile").forEach((tile) => state.picked.add(tile.dataset.key));
      state.homeSignature = null;
      renderInbox();
      renderPickBar();
    }
  });
  $("#deleteProjectConfirm").addEventListener("click", confirmDeleteProject);
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
