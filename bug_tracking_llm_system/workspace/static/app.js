"use strict";

const STAGES = [
  ["extraction", "轉換 JSON"],
  ["duplicate", "重複偵測"],
  ["priority", "優先級判定"],
  ["assignee", "分派負責人"],
  ["localization", "錯誤定位"],
  ["patch", "補丁生成"],
  ["commit", "提交訊息"],
];
/* Patch/commit only run when an engineer asks, so progress counts the first five automatic steps. */
const MAIN_STAGES = STAGES.filter(([key]) => !["patch", "commit"].includes(key));
const STATUS_STAGES = STAGES.filter(([key]) => key !== "commit");
const STATUS_TEXT = {
  pending: "尚未執行",
  running: "執行中…",
  done: "完成",
  fallback: "完成，資料有限",
  skipped: "略過",
  failed: "失敗",
};
const STAGE_PURPOSE = {
  extraction: "將回報內容整理成一致欄位，供後續流程使用。",
  duplicate: "比對歷史 Ticket，協助判斷是否為同一問題。",
  priority: "綜合回報內容與歷史資料，評估處理順序。",
  assignee: "依問題領域與過往處理紀錄，推薦適合的負責人。",
  localization: "從專案程式碼中找出最可能需要檢查的位置。",
  patch: "根據定位結果產生修正方案，並完成基本安全檢查。",
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
  product_history: "同產品處理紀錄",
  text_similarity: "內容相似紀錄",
  component_owner_mapping: "元件負責人對照",
  global_prior: "整體處理量",
  file_ownership: "檔案負責人",
  component_ownership: "元件負責範圍",
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
const OLD_HISTORY_NOTE = "這一項只看這張 Ticket 建立時間往前推的一段期間。如果可用的歷史資料較早，這段期間可能找不到回報，數值便會是 0。這不是程式錯誤，但代表這一項沒有提供足夠資訊。";
const NO_DATA_NOTE = "0 代表沒有資料，不是「P0」。優先級的數字是 P1 = 1 到 P5 = 5。";
const FACTOR_GROUP_HELP = {
  textual: ["從回報的文字判斷緊急程度", "系統會分析標題與描述中的重要詞語，辨識當機、資料遺失、安全問題、功能退步等可能需要優先處理的情況。"],
  temporal: ["最近一段時間整體的回報量", "時間因子看的是這張 Ticket 建立前，整個專案最近有多少回報。短時間內大量回報，常代表新版本出問題或有問題大量爆發，同時也會影響工程師能分配給每張 Ticket 的時間。"],
  author: ["回報者過去的回報紀錄", "作者因子看的是同一位回報者以前回報過多少張、那些回報平均被排成什麼優先級。經驗多的回報者，或過去常回報緊急問題的人，回報的優先級往往有一定的傾向。"],
  related_report: ["最像的歷史回報被排成什麼優先級", "系統會比較標題、描述、產品與元件，找出內容最接近的歷史回報，再參考它們過去的處理順序。"],
  severity: ["回報者填寫的嚴重度", "嚴重度是「影響有多大」，優先級是「要多快處理」，兩者相關但不相同。例如 blocker 通常很急，但也可能因為只影響少數人而排在後面。"],
  product_component: ["同產品、同元件最近的狀況", "產品／元件因子看的是同一個產品或元件最近有多少回報、被排成什麼優先級。有些元件（例如核心模組）的問題通常比較急，最近問題很多的元件也值得注意。"],
};
const FACTOR_HELP = {
  summary_terms: {
    label: "標題清洗結果", group: "textual",
    short: "系統整理後用來判斷優先級的標題文字",
    detail: "系統會統一大小寫，將網址、Email 與識別碼改成通用標記，並移除過長的紀錄片段與重複符號。這裡顯示整理後、用於判斷優先級的文字。",
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
    detail: "回報者填寫，或由轉換 JSON 步驟從內容辨識的嚴重度。等級由高到低是 blocker（阻斷）、critical（嚴重）、major（重大）、normal（一般）、minor（輕微）、trivial（細微）、enhancement（功能需求）。unknown 表示沒有填，系統會將它視為一個獨立類別。",
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

const state = { tickets: [], selected: null, detail: null, filter: "all", folder: null, selecting: false, picked: new Set(), query: "", homeSignature: null, projects: [], defaultProject: "", pollTimer: null, automationMode: "semi", automaticTarget: "commit", notificationsEnabled: false, notificationSnapshot: null, colorTheme: "light" };
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
  clearTimeout(toast.holdTimer);
  clearTimeout(toast.hideTimer);
  el.hidden = false;
  el.classList.remove("is-leaving");
  toast.holdTimer = setTimeout(() => {
    el.classList.add("is-leaving");
    toast.hideTimer = setTimeout(() => {
      el.hidden = true;
      el.classList.remove("is-leaving");
    }, 1250);
  }, 5000);
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

/* ---------------------------------------------------------- operation mode */
const MODE_KEY = "ticket-workspace-automation-mode";
const PREFERENCES_KEY = "ticket-workspace-preferences";

function applyPreferences(preferences = {}) {
  state.colorTheme = preferences.colorTheme === "dark" ? "dark" : "light";
  state.automaticTarget = ["localization", "patch", "commit"].includes(preferences.automaticTarget) ? preferences.automaticTarget : "commit";
  state.notificationsEnabled = Boolean(preferences.notificationsEnabled);
  document.documentElement.dataset.theme = state.colorTheme;
  try { localStorage.setItem(PREFERENCES_KEY, JSON.stringify({ colorTheme: state.colorTheme, automaticTarget: state.automaticTarget, notificationsEnabled: state.notificationsEnabled })); } catch { /* private browsing */ }
}

function applyAutomationMode(mode) {
  state.automationMode = mode === "automatic" ? "automatic" : "semi";
  try { localStorage.setItem(MODE_KEY, state.automationMode); } catch { /* private browsing */ }
}

function openSettings() {
  const selected = document.querySelector(`input[name="automationMode"][value="${state.automationMode}"]`);
  if (selected) selected.checked = true;
  const theme = document.querySelector(`input[name="colorTheme"][value="${state.colorTheme}"]`);
  if (theme) theme.checked = true;
  const target = document.querySelector(`input[name="automaticTarget"][value="${state.automaticTarget}"]`);
  if (target) target.checked = true;
  const notifications = document.querySelector('[name="notificationsEnabled"]');
  if (notifications) notifications.checked = state.notificationsEnabled;
  renderNotificationPermissionStatus();
  renderModeNotice();
  $("#settingsDialog").showModal();
}

function renderNotificationPermissionStatus() {
  const status = $("#notificationPermissionStatus");
  if (!status) return;
  status.textContent = !("Notification" in window)
    ? "這個瀏覽器不支援桌面通知"
    : `瀏覽器權限：${{ granted: "已允許", denied: "已封鎖", default: "尚未決定" }[Notification.permission]}`;
}

async function requestNotificationPermission() {
  if (!("Notification" in window)) return false;
  if (Notification.permission === "granted") return true;
  if (Notification.permission === "denied") return false;
  const granted = await Notification.requestPermission() === "granted";
  renderNotificationPermissionStatus();
  return granted;
}

function showDesktopNotification(title, options = {}) {
  if (!("Notification" in window) || Notification.permission !== "granted") return false;
  try {
    const notice = new Notification(title, options);
    return notice;
  } catch {
    return false;
  }
}

async function sendTestNotification() {
  const allowed = await requestNotificationPermission();
  if (!allowed) {
    toast("桌面通知未獲允許；網站內通知仍會顯示");
    return;
  }
  const notice = showDesktopNotification("Ticket 工作台測試通知", { body: "通知功能已連線。", tag: "ticket-workspace-test" });
  toast(notice ? "測試通知已送出" : "桌面通知送出失敗；網站內通知仍可使用");
}

function renderModeNotice() {
  const selected = document.querySelector('input[name="automationMode"]:checked');
  const mode = selected ? selected.value : state.automationMode;
  const notice = $("#modeNotice");
  notice.dataset.mode = mode;
  notice.textContent = mode === "automatic"
    ? "全自動不會等待人工確認：模糊的重複候選會視為非重複，建議負責人會自動採用。處理失敗時仍會停止並顯示原因。"
    : "半自動保留目前行為：需要你確認重複結果與負責人，補丁由你手動啟動。";
}

async function saveSettings(event) {
  if (event.submitter && event.submitter.value === "cancel") return;
  event.preventDefault();
  const selected = document.querySelector('input[name="automationMode"]:checked');
  applyAutomationMode(selected ? selected.value : "semi");
  const theme = document.querySelector('input[name="colorTheme"]:checked');
  const target = document.querySelector('input[name="automaticTarget"]:checked');
  let notificationsEnabled = document.querySelector('[name="notificationsEnabled"]').checked;
  let notificationDenied = false;
  if (notificationsEnabled) notificationsEnabled = await requestNotificationPermission();
  notificationDenied = document.querySelector('[name="notificationsEnabled"]').checked && !notificationsEnabled;
  applyPreferences({
    colorTheme: theme ? theme.value : "light",
    automaticTarget: target ? target.value : "commit",
    notificationsEnabled,
  });
  $("#settingsDialog").close();
  toast(notificationDenied ? "設定已儲存；瀏覽器未允許通知" : "設定已儲存");
}

function runPreferences() {
  return { automation_mode: state.automationMode, automatic_target: state.automaticTarget };
}

/* ------------------------------------------------------------------ inbox */
function bucket(t) {
  if (t.busy || t.status === "running" || t.status === "ready_to_continue") return "running";
  if (t.status === "awaiting_duplicate_review" || (t.attention || []).some((a) => a.includes("負責人"))) return "review";
  if (t.status === "processed" || t.status === "duplicate") return "done";
  return "other";
}

function ticketNotificationState(ticket) {
  if (ticket.status === "failed") return "failed";
  if (bucket(ticket) === "review") return "review";
  if (!ticket.busy && ["processed", "duplicate"].includes(ticket.status)) return "done";
  return "";
}

function notifyTicketChanges(tickets) {
  const next = new Map(tickets.map((ticket) => [ticket.key, ticketNotificationState(ticket)]));
  const previous = state.notificationSnapshot;
  state.notificationSnapshot = next;
  if (!previous || !state.notificationsEnabled) return;
  const copy = {
    done: ["Ticket 處理完成", "已完成處理。"],
    failed: ["Ticket 處理失敗", "處理已停止，請查看失敗原因。"],
    review: ["Ticket 等你確認", "流程需要你的決定才能繼續。"],
  };
  tickets.forEach((ticket) => {
    const status = next.get(ticket.key);
    if (!status || status === previous.get(ticket.key)) return;
    const [heading, message] = copy[status];
    const body = `${ticket.ticket_id || ticket.key} · ${ticket.title || message}`;
    toast(`${heading}：${ticket.ticket_id || ticket.key}`);
    const notice = showDesktopNotification(heading, { body, tag: `ticket-${ticket.key}-${status}` });
    if (notice) notice.onclick = () => { window.focus(); location.hash = `#/t/${encodeURIComponent(ticket.key)}`; notice.close(); };
  });
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
  patch: "產生補丁",
  commit: "產生提交訊息",
};
function queueText(q, { short = false } = {}) {
  if (!q) return "";
  if (q.state === "running" && q.stopping) {
    return short ? (q.deleting ? "這一步結束後刪除" : "這一步結束後停止") : `正在執行目前這一步，暫時無法中途停止；完成後會${q.deleting ? "刪除這張 Ticket" : "停止"}（${fmtEta(q.eta_seconds)}）。`;
  }
  if (q.state === "running") {
    const what = QUEUE_PHASE[q.phase] || "處理";
    return short ? `${what}，${fmtEta(q.eta_seconds)}` : `正在執行「${what}」，預計還要${fmtEta(q.eta_seconds)}，完成後會自動更新。`;
  }
  const next = QUEUE_PHASE[q.phase] || "處理";
  return short
    ? `前面還有 ${q.ahead} 張，${fmtEta(q.eta_seconds)}`
    : `排隊中：前面還有 ${q.ahead} 張，預計${fmtEta(q.eta_seconds)}後完成「${next}」。`;
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
  const pebbles = MAIN_STAGES.map(([key, name]) => {
    const s = t.current_stage === key && (t.busy || t.status === "running") ? "running" : t.stage_status[key];
    return `<i data-s="${esc(s)}" title="${esc(name)}：${esc(STATUS_TEXT[s] || s)}"></i>`;
  }).join("");
  const attention = (t.attention || []).find((a) => !a.includes("備援"));
  const meta = [
    showProject ? `<span class="tag tag-project" title="專案">${markTerms(projectName(projectKey(t)), terms)}</span>` : "",
    t.component ? `<span class="tag">${markTerms(t.component, terms)}</span>` : "",
    t.assignee ? `<span class="tag tag-person ${t.assignee_confirmed ? "is-confirmed" : ""}" title="${t.assignee_confirmed ? "已確認負責人" : "建議負責人"}">${markTerms(t.assignee, terms)}</span>` : "",
    t.duplicate_of ? `<span class="tag">重複 ${markTerms(t.duplicate_of, terms)}</span>` : "",
    t.patch && t.patch.official_resolved === true ? `<span class="tag tag-patch is-resolved" title="這個補丁有 SWE-bench 官方測試通過紀錄">官方測試通過</span>`
      : t.patch && t.patch.patch_status === "generated" ? `<span class="tag tag-patch" title="已產生補丁，待人工審查">有補丁</span>` : "",
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
  const desc = f.key === NO_PROJECT ? "匯入時沒有選擇專案，因此無法分析程式碼位置。"
    : f.config ? (f.config.description || "已連結專案程式碼") : "此專案尚未連結程式碼，因此無法分析程式碼位置。";
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
    const desc = state.folder === NO_PROJECT ? "匯入時沒有選擇專案，因此無法分析程式碼位置。" : config ? (config.description || "已連結專案程式碼") : "此專案尚未連結程式碼，因此無法分析程式碼位置。";
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
  notifyTicketChanges(data.tickets);
  state.tickets = data.tickets;
  const q = data.queue || {};
  const pill = $("#queuePill");
  if (pill) {
    const running = (q.running || []).length;
    pill.hidden = !running && !q.waiting;
    pill.innerHTML = `<i></i>${running ? `處理中 ${running} 張` : ""}${q.waiting ? `${running ? "，" : ""}排隊 ${q.waiting} 張` : ""}`;
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
  $("[data-pick-stop]").disabled = state.picked.size === 0;
}

async function rerunPicked() {
  const keys = [...state.picked];
  if (!keys.length) return;
  const automatic = state.automationMode === "automatic";
  const scope = automatic
    ? "每張會執行完整七個階段，包含補丁與提交訊息；補丁會依序使用 GPU，數量多時需要較長時間。"
    : "每張會重新跑前五個步驟，並在人工確認點停下來。";
  if (!confirm(`把選取的 ${keys.length} 張 Ticket 從頭重新執行？\n\n${scope}\n原本的處理結果會被新的結果取代。`)) return;
  let started = 0, skipped = 0;
  for (const key of keys) {
    try {
      await post(`/api/tickets/${encodeURIComponent(key)}/run`, runPreferences());
      started += 1;
    } catch { skipped += 1; }
  }
  toast(`已開始${automatic ? "全自動" : "半自動"}重新執行 ${started} 張${skipped ? `；${skipped} 張正在處理，略過` : ""}`);
  setSelecting(false);
  await refresh();
}

async function stopPicked() {
  const keys = [...state.picked];
  if (!keys.length) return;
  try {
    const result = await post("/api/stop-tickets", { keys });
    const parts = [result.removed.length ? `已取消 ${result.removed.length} 張` : "", result.stopping.length ? `${result.stopping.length} 張會在目前這一步結束後停止` : ""].filter(Boolean);
    toast(parts.join("；") || "選取的 Ticket 都沒有在處理");
    setSelecting(false);
    await refresh();
  } catch (err) { toast(err.message); }
}

async function deletePicked() {
  const keys = [...state.picked];
  if (!keys.length) return;
  const ids = keys.map((key) => (state.tickets.find((t) => t.key === key) || {}).ticket_id || key);
  const preview = ids.slice(0, 5).join("、") + (ids.length > 5 ? ` 等 ${ids.length} 張` : "");
  if (!confirm(`刪除 ${preview}？\n\n這些 Ticket 的處理結果會一起刪除，無法復原。`)) return;
  try {
    const result = await post("/api/delete-tickets", { keys });
    toast(`已刪除 ${result.deleted.length} 張${result.deferred.length ? `；${result.deferred.length} 張正在執行，這一步結束後刪除` : ""}${result.skipped.length ? `；${result.skipped.length} 張找不到` : ""}`);
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
    ${usage.busy.length ? `<p class="hint">有 ${usage.busy.length} 張 Ticket 正在處理或排隊：排隊中的會直接取消並刪除，正在執行的會在目前這一步結束後刪除。</p>` : ""}
    ${!usage.configured && name !== NO_PROJECT ? `<p class="hint">這個專案不在設定檔裡；Ticket 和比對歷史都刪除後，資料夾就會消失。</p>` : ""}`;
  $("#deleteProjectConfirm").disabled = false;
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
    if ((result.deferred || []).length) parts.push(`另外 ${result.deferred.length} 張正在執行，這一步結束後刪除`);
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

function flowStageState(r, key) {
  if (key === "patch") return patchTabState(r);
  if (key === "commit") return commitTabState(r);
  return stageStatus(r, key);
}

/* The tab that needs attention: the open gate, the running stage, or the last finished stage. */
function suggestedTab(r) {
  if (r.status === "awaiting_duplicate_review" || r.status === "duplicate") return "duplicate";
  if ((r.busy || r.status === "running" || r.status === "failed") && r.current_stage) return r.current_stage;
  if (r.status === "new") return "extraction";
  const finished = STAGES.map(([k]) => k).filter((k) => !["pending", "ready", "running"].includes(flowStageState(r, k)));
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
  PATCH_TEXTS.clear();
  COMMIT_TEXTS.clear();
  const body = $("#detailBody");
  const r = state.detail;
  if (!state.selected || !r) { body.innerHTML = ""; return; }
  const [label, cls] = TICKET_STATUS[r.busy ? "running" : r.status] || [r.status, ""];
  const awaiting = r.status === "awaiting_duplicate_review";
  const tab = state.tab || "extraction";
  const doneCount = MAIN_STAGES.filter(([key]) => ["done", "fallback", "skipped"].includes(stageStatus(r, key))).length;
  const tabs = STAGES.map(([key, name]) => {
    const s = flowStageState(r, key);
    const gate = key === "duplicate" && awaiting ? ' data-gate="open"' : "";
    const selected = key === tab;
    const text = key === "patch" ? PATCH_TAB_TEXT[s] : key === "commit" ? COMMIT_TAB_TEXT[s] : STATUS_TEXT[s];
    return `<li data-s="${s}"${gate}${["patch", "commit"].includes(key) ? " data-optional" : ""}><button type="button" role="tab" id="tab-${key}" aria-controls="tabPanel" aria-selected="${selected}" class="${selected ? "is-active" : ""}" data-tab="${key}">
      <span class="node"></span><span class="label">${name}</span><span class="state">${esc(text || s)}</span></button></li>`;
  }).join("");
  const logSelected = tab === "log";
  const projectOptions = state.projects.map((p) => `<option value="${esc(p.project)}" ${p.project === r.project ? "selected" : ""}>${esc(p.project)}</option>`).join("");

  let hint = "";
  if (awaiting && tab !== "duplicate") hint = `<div class="pin-hint pin-gate"><span>重複偵測正在等你確認，確認後才會繼續後面的步驟。</span><button class="btn btn-gate btn-small" data-tab="duplicate">前往確認</button></div>`;
  else if (r.status === "failed") {
    const where = (STAGES.find((x) => x[0] === r.current_stage) || ["", r.current_stage])[1];
    hint = `<div class="pin-hint pin-failed"><span>${where ? `流程在「${esc(where)}」停止` : "流程已停止"}：${esc(r.error)}</span></div>`;
  }
  else if (r.status === "duplicate") hint = `<div class="pin-hint pin-info"><span>已判定為 <b class="mono">${esc((r.decisions.duplicate || {}).duplicate_of || "")}</b> 的重複，流程結束。</span></div>`;
  else if (r.queue) hint = `<div class="pin-hint pin-info"><span>${esc(queueText(r.queue))}</span>${r.queue.stopping ? "" : `<button class="btn btn-quiet btn-small" data-action="stop">${r.queue.state === "running" ? "停止" : "取消排隊"}</button>`}</div>`;
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
          <span class="progress-count">${doneCount} / ${MAIN_STAGES.length} 步完成</span>
          <details class="more">
            <summary class="btn btn-quiet btn-small">更多</summary>
            <div class="more-menu">
              <p>專案 <b>${esc(r.project || "未指定")}</b></p>
              <p>來源 <b>${esc(r.source)}</b></p>
              <p>匯入 <b>${esc(fmtTime(r.created_at))}</b></p>
              <a class="btn btn-quiet btn-small" href="/api/tickets/${encodeURIComponent(r.key)}/artifact/ticket.json">下載完整結果 JSON</a>
              <button class="btn btn-quiet btn-small" data-action="rerun" ${r.busy ? "disabled" : ""}>${r.status === "new" ? "開始處理" : "從頭重新執行"}</button>
              <button class="btn btn-quiet btn-small" data-action="delete">刪除這張 Ticket</button>
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

function publicStageMessage(key, value, fallback = "處理失敗，請稍後重試；若持續發生，請聯絡系統管理員。") {
  const text = String(value || "").trim();
  if (!text) return fallback;
  const internal = /Ollama|Code Llama|gemma|codellama|repo_path|integration_config|git checkout|git repository|snapshot|base_commit|sha256|\bcommit\s+[0-9a-f]{7,40}\b|\bclone\b|\bAST\b|git apply|\bseed\b|TF-IDF|SBERT|BM25|\bPRI\b|routing gate|component-owner|SWE-bench|harness|模型\s*\S*\s*@/i;
  if (!internal.test(text)) return text;
  if (key === "localization" && /回報日期/.test(text)) {
    const day = text.match(/\d{4}-\d{2}-\d{2}/)?.[0];
    return `這張 Ticket 沒有指定程式碼版本，系統已使用${day ? `回報日期 ${day} ` : "回報日期"}當時的程式碼進行分析。`;
  }
  const messages = {
    extraction: "Ticket 內容轉換服務目前無法使用，系統已改用基本資料繼續處理。",
    duplicate: "部分歷史比對資料目前無法使用，系統已依現有資料完成判斷。",
    priority: "優先級判定所需的部分資料目前無法使用，系統已依現有資訊完成判斷。",
    assignee: "目前的分派依據不足，請確認建議負責人或改由人工分派。",
    localization: "此專案的程式碼尚未準備完成，請聯絡系統管理員後重試。",
    patch: "補丁生成所需的程式碼版本或服務尚未準備完成，請聯絡系統管理員後重試。",
    commit: "提交訊息產生失敗，請重新產生；若持續發生，請聯絡系統管理員。",
  };
  return messages[key] || fallback;
}

function stageFrame(r, key, name, n, inner) {
  const stage = r.stages[key] || {};
  const s = stageStatus(r, key);
  const chipCls = { done: "chip-done", fallback: "chip-fallback", failed: "chip-failed", running: "chip-running" }[s] || "";
  const stageWarnings = key === "assignee" && (r.decisions || {}).assignee ? [] : (stage.warnings || []);
  const warnings = stageWarnings.length ? `<ul class="warn-list">${stageWarnings.map((w) => `<li>${esc(publicStageMessage(key, w, "部分資料目前無法使用，系統已依現有資訊繼續處理。"))}</li>`).join("")}</ul>` : "";
  const foot = STAGE_PURPOSE[key] ? `<div class="stage-foot"><span>${esc(STAGE_PURPOSE[key])}</span></div>` : "";
  let content;
  if (s === "pending") content = `<p class="stage-pending">${key === "localization" || key === "priority" || key === "assignee" ? "等前面的步驟完成後執行。" : "尚未執行。"}</p>`;
  else if (s === "running") content = `<p class="stage-pending">正在執行，完成後會自動更新。</p>`;
  else if (s === "failed") content = `<p class="err">${esc(publicStageMessage(key, stage.error))}</p>`;
  else content = warnings + inner(stage.output || {}, stage);
  return `<section class="stage" id="stage-${key}">
    <div class="stage-head"><h3><span class="num">${n}</span>${name}</h3><span class="chip ${chipCls}">${esc(STATUS_TEXT[s] || s)}</span></div>
    <div class="stage-body">${content}</div>${s === "pending" || s === "running" ? "" : foot}</section>`;
}

function renderStage(r, key, name, n, projectOptions) {
  if (key === "patch") return renderPatchStage(r, n);
  if (key === "commit") return renderCommitStage(r, n);
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
  return `<dl class="fields">${cells}</dl>
    <details class="raw"><summary>查看原始匯入內容</summary><pre>${esc(JSON.stringify(raw, null, 2))}</pre></details>`;
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
      : "已確認不是重複"}（${esc({ engineer: "工程師確認", "workspace:auto": "系統自動確認", auto_threshold: "系統自動確認", no_candidates: "沒有可比對的歷史資料", below_review_threshold: "系統自動確認" }[decision.by] || "系統確認")}，${esc(fmtTime(decision.at))}）</p>` : "";
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
  return `<p class="hint">系統已比對 ${o.history_size ?? "?"} 張${o.project_scope ? `「${esc(o.project_scope)}」及未標專案的` : ""}歷史 Ticket${[o.other_project_rows_skipped ? `其他專案 ${o.other_project_rows_skipped} 張` : "", o.later_rows_skipped ? `比這張晚建立的 ${o.later_rows_skipped} 張` : ""].filter(Boolean).length ? `（未使用${[o.other_project_rows_skipped ? `其他專案 ${o.other_project_rows_skipped} 張` : "", o.later_rows_skipped ? `建立時間較晚的 ${o.later_rows_skipped} 張` : ""].filter(Boolean).join("、")}）` : ""}。${hint}</p>${summary}<ol class="cands" style="margin-top:10px">${head}</ol>${more}${decisionLine}`;
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
  $("#compareScores").innerHTML = `<span>整體相似度 <b>${num(c.similarity)}</b></span><span>標題 <b>${num(c.title_similarity)}</b></span><span>內容 <b>${num(c.content_similarity)}</b></span><span>共同字詞 <b>${shared.size}</b> 個</span>${hist.__partial ? "<span>（找不到完整的歷史資料，只顯示候選摘要）</span>" : ""}`;

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
        const label = h ? esc(h.label) : "其他參考資訊";
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
    html = `<p class="fx-intro">這些資料是系統判斷優先級時的參考依據。單一數值不代表最後結果，也不是各項因素的貢獻比例；系統會綜合文字內容、時間、回報者紀錄與相似歷史問題後判定。</p>` +
      Object.entries(o.factor_values).map(([name, vals]) => {
        const [, groupDetail] = FACTOR_GROUP_HELP[name] || ["", ""];
        const items = Object.entries(vals).map(([k, v]) => {
          const h = FACTOR_HELP[k] || { label: k, detail: "這一項沒有說明。" };
          const isZero = v === 0 || v === "0";
          return `<article class="fx-item" data-help-key="${esc(k)}">
            <header><h4>${esc(h.label)}</h4><span class="fx-value">${k === "summary_terms" ? "見下方" : `這張：<b>${esc(factorValueText(k, v))}</b>`}</span></header>
            <p>${esc(h.detail)}</p>
            ${k === "summary_terms" ? `<p class="fx-sample">${esc(v || "（空白）")}</p>` : ""}
            ${isZero && h.zero ? `<p class="fx-zero"><b>為什麼是 0？</b>${esc(zeroNote(h.zero))}</p>` : ""}
          </article>`;
        }).join("");
        return `<section class="fx-group" data-help-key="group:${esc(name)}"><h3>${esc(FACTOR_TEXT[name] || name)}</h3><p class="fx-group-desc">${esc(groupDetail)}</p>${items}</section>`;
      }).join("");
  } else if (o.factor_scores) {
    html = `<p class="fx-intro">目前可用的歷史資料有限，因此系統改用固定評分規則。六項因素各產生 0 到 1 的分數，越接近 1 代表越需要優先處理，再依權重合併為最後等級。</p>` +
      Object.entries(o.factor_scores).map(([k, v]) => {
        const [weight, short, detail] = FALLBACK_FACTOR_HELP[k] || ["—", k, "這一項沒有說明。"];
        return `<article class="fx-item" data-help-key="score:${esc(k)}">
          <header><h4>${esc(FACTOR_TEXT[k] || "其他參考資訊")}</h4><span class="fx-value">影響比重 <b>${esc(weight)}</b>，這張 <b>${esc(num(v, 2))}</b></span></header>
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

function historyNote(r) {
  const d = ((r.stages || {}).assignee || {}).details || {};
  if (!d.history_rows) return "";
  return `<p class="hint" style="margin-bottom:10px">參考了 ${d.history_rows} 筆歷史紀錄${d.later_rows_skipped ? `（比這張晚建立的 ${d.later_rows_skipped} 筆不使用）` : ""}。</p>`;
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
      <div class="assignee-score">${scoreRow("分數", (d.score || 0) / top).replace(/<b>.*<\/b>/, `<b>${num(d.score, 2)}</b>`)}</div>
      ${canConfirm ? `<button class="btn btn-quiet btn-small" data-action="assign" data-who="${esc(d.assignee)}" data-decision="${i === 0 ? "accept" : "override"}">${i === 0 ? "採用建議" : "改派給他"}</button>` : "<span></span>"}
    </li>`).join("");
  const routing = confirmed ? "已指定負責人" : ({ assigned: "已自動選出負責人", assigned_by_fallback: "已依領域與處理紀錄選出負責人", needs_manual_triage: "需要人工指定負責人" }[o.routing_status] || "負責人建議");
  const reason = confirmed
    ? "負責人已確認；候選名單保留供需要改派時參考。"
    : o.routing_status === "needs_manual_triage"
    ? "目前資料不足以自動確認，請從候選名單選擇或手動指定。"
    : "候選順序綜合問題領域、過往處理紀錄與內容相似度。";
  const displayedAssignee = confirmed ? confirmed.assignee : o.suggested_assignee;
  return `<p style="margin-bottom:10px">${esc(routing)}${displayedAssignee ? `：<b>${esc(displayedAssignee)}</b>` : ""}${!confirmed && o.confidence != null ? `（信心 ${num(o.confidence, 2)}）` : ""}</p>
    <p class="hint" style="margin-bottom:10px">${esc(reason)}</p>
    ${historyNote(r)}
    ${rows ? `<ol class="assignee-list">${rows}</ol>` : "<p class=\"hint\">沒有可推薦的候選人。</p>"}
    ${canConfirm ? `<div class="manual-assign"><input id="manualAssignee" placeholder="或輸入其他負責人的 email／帳號" aria-label="指定負責人"><button class="btn btn-quiet btn-small" data-action="assign-manual">指定這位負責人</button></div>` : ""}
    ${confirmed ? `<p class="confirmed-line">已確認負責人：<b>${esc(confirmed.assignee)}</b>（${esc(fmtTime(confirmed.at))}）</p>` : ""}`;
}

/* Localization runs in the code of the ticket's own project. Using another project's code is a
   deliberate, confirmed move: the ticket changes folder, and it only makes sense when the ticket
   really belongs to that project. */
function locControls(r) {
  if (!state.projects.length) return `<p class="hint" style="margin-top:10px">尚未連結任何專案程式碼，請聯絡系統管理員完成設定。</p>`;
  const current = r.project || "";
  const hasCode = state.projects.some((p) => p.project === current);
  const others = state.projects.filter((p) => p.project !== current);
  const rerun = hasCode
    ? `<div class="loc-controls"><button class="btn btn-quiet btn-small" data-action="localize" data-project="${esc(current)}">${r.stages.localization.status === "done" ? "重新定位" : "執行錯誤定位"}</button><span class="hint">使用「${esc(current)}」的程式碼</span></div>`
    : `<p class="loc-note">${current ? `「${esc(current)}」尚未連結程式碼，請聯絡系統管理員完成設定後再試。` : "要分析程式碼位置，請先指定這張 Ticket 所屬的專案。"}如果它屬於下面某個專案，也可以手動改過去。</p>`;
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
    const why = String(s.reason || "").toLowerCase();
    const reason = why.includes("stack") ? "符合錯誤紀錄中的檔案或行號"
      : why.includes("identifier") ? "符合回報中的函式或類別名稱"
      : "與回報內容及程式碼線索相符";
    return `<li><details class="symbol" ${s.rank === 1 ? "open" : ""}><summary>
        <span class="rank">${s.rank}</span>
        <span class="path">${esc(s.file_path)} <b>${esc(s.symbol_qualified_name || "")}</b></span>
        <span class="lines">第 ${s.start_line}–${s.end_line} 行，分數 ${num(s.score)}</span>
        <span class="why">${esc(reason)}</span></summary>
        <pre class="code" style="--start:${Math.max(0, (s.start_line || 1) - 1)}">${lines}</pre></details></li>`;
  }).join("");
  return `<div class="loc-summary">
      <span>定位信心 <span class="chip ${o.confidence_level === "high" ? "chip-done" : o.confidence_level === "low" ? "chip-failed" : "chip-fallback"}">${esc(level)}</span></span>
      <span>分數 <b class="mono">${num((o.confidence || {}).confidence_score)}</b></span>
      <span>${o.should_manual_review ? "需要人工確認" : "可直接參考"}</span>
      <span>分析專案 <b>${esc(o.repository || o.project)}</b></span>
    </div>
    <p class="hint" style="margin-bottom:10px">${o.confidence_level === "high" ? "定位結果較明確，請優先檢查排名最前的程式位置。" : "定位結果仍有不確定性，請比較候選位置後再決定修改範圍。"}</p>
    ${symbols ? `<ol class="symbols">${symbols}</ol>` : `<p>沒有找到候選位置。</p>`}
    ${symbols ? `<ul class="guidance"><li>請先檢查排名最前的程式位置，並將修改範圍控制在必要區域。</li></ul>` : ""}
    ${r.busy ? "" : locControls(r, projectOptions)}`;
}

/* ------------------------------------------------------------------ patch */
const PATCH_TAB_TEXT = { pending: "手動執行", ready: "可以產生", running: "產生中…", failed: "失敗", skipped: "略過", generated: "有補丁", none: "沒有補丁", resolved: "官方測試通過" };
const COMMIT_TAB_TEXT = { pending: "等待補丁", running: "產生中…", generated: "已產生", failed: "失敗", skipped: "略過", none: "沒有補丁" };
const PATCH_REASON = {
  no_op: "沒有實際改動",
  rewrite_no_change: "方案沒有包含實際修改",
  duplicate: "和前一個候選完全相同",
  signature_changed: "改動了函式的參數（簽名），不允許",
  rewrite_contract_violation: "輸出格式不符合要求",
  undefined_name: "用到不存在的名稱",
  context_budget_exceeded: "函式內容過長，無法在單次處理中完成",
  truncated_or_incomplete: "輸出被截斷，函式沒寫完",
  no_executable_change: "只修改了說明文字，沒有程式行為變更",
};

function patchTabState(r) {
  const s = stageStatus(r, "patch");
  if (s !== "done") return s === "pending" && stageStatus(r, "localization") === "done" ? "ready" : s;
  const o = (r.stages.patch || {}).output || {};
  if (o.official && o.official.official_resolved === true) return "resolved";
  return o.patch_status === "generated" ? "generated" : "none";
}

/* The current patch's commit message: step 7 data, or a draft made by the earlier version (ui-card-wall). */
function currentCommit(r) {
  const own = (((r.stages || {}).patch || {}).output || {}).commit_message;
  if (own) return own;
  const legacy = (r.commit_messages || {}).current;
  if (!legacy) return {};
  return legacy.status === "done" ? { ...legacy, status: "generated" } : legacy;
}

function commitTabState(r) {
  const patchState = patchTabState(r);
  if (!["generated", "resolved"].includes(patchState)) {
    if (patchState === "none") return "none";
    if (patchState === "skipped") return "skipped";
    return "pending";
  }
  const commit = currentCommit(r);
  if (commit.status === "running" || (r.queue && r.queue.phase === "commit")) return "running";
  if (commit.status === "generated" && commit.commit_message) return "generated";
  if (commit.status === "failed") return "failed";
  if (commit.status === "skipped") return "skipped";
  return "pending";
}

function patchReason(reason) {
  if (!reason) return "";
  const [code, ...rest] = String(reason).split(":");
  const text = PATCH_REASON[code.trim()];
  return text ? `${text}${rest.length ? `（${rest.join(":").trim()}）` : ""}` : reason;
}

/* Patch texts shown on the page, so the copy/download buttons get the exact bytes (not the HTML). */
const PATCH_TEXTS = new Map();
const COMMIT_TEXTS = new Map();

function commitEvidenceNote(commit) {
  const evidence = commit.test_evidence || {};
  if (evidence.official_swebench_harness === "resolved") return "已有 SWE-bench 官方測試通過紀錄，因此提交訊息包含驗證資訊。";
  if (evidence.official_swebench_harness === "unresolved") return "SWE-bench 官方測試未通過，因此提交訊息只描述程式變更。";
  return "尚未執行官方測試，因此提交訊息只描述程式變更。";
}

function commitRegenerate(r, source, label = "重新產生提交訊息") {
  return `<button type="button" class="btn btn-quiet btn-small" data-action="commit-message" data-source="${esc(source)}" ${r.busy ? "disabled" : ""}>${label}</button>`;
}

function commitMessageBox(r, commit, source) {
  if (!commit || commit.status === "skipped") return "";
  if (commit.status !== "generated" || !commit.commit_message) {
    return `<div class="commit-box"><div class="commit-head"><b>提交訊息</b>${commitRegenerate(r, source)}</div><p class="err">${esc(publicStageMessage("commit", commit.error, "提交訊息產生失敗，請重新產生。"))}</p></div>`;
  }
  const text = String(commit.full_message || commit.commit_message);
  const id = `c${COMMIT_TEXTS.size + 1}`;
  COMMIT_TEXTS.set(id, text);
  return `<div class="commit-box"><div class="commit-head"><b>提交訊息</b>
      <span class="patch-tools"><button type="button" class="btn btn-primary btn-small" data-copy-commit="${id}">複製提交訊息</button>${commitRegenerate(r, source)}</span></div>
    <pre class="commit-msg">${esc(text)}</pre>
    <p class="hint">${esc(commitEvidenceNote(commit))} 此內容依 Ticket 與補丁自動產生，提交前請人工審查；工作台不會自動提交變更。</p></div>`;
}

function renderCommitStage(r, n) {
  const patch = (r.stages.patch || {}).output || {};
  const commit = currentCommit(r);
  const state = commitTabState(r);
  const chipCls = { generated: "chip-done", failed: "chip-failed", skipped: "chip-fallback", none: "chip-fallback" }[state] || "";
  let content;
  if (state === "generated") {
    content = commitMessageBox(r, commit, "current");
  } else if (state === "running") {
    content = `<p class="stage-pending">${r.queue ? esc(queueText(r.queue)) : "正在產生提交訊息…"}</p>`;
  } else if (state === "failed") {
    content = `<p class="err">${esc(publicStageMessage("commit", commit.error, "提交訊息產生失敗，請重新產生。"))}</p><p class="hint">補丁仍然保留。</p><div class="loc-controls">${commitRegenerate(r, "current")}</div>`;
  } else if (state === "skipped" && stageStatus(r, "patch") === "skipped") {
    content = `<p class="stage-pending">補丁生成被略過，所以沒有提交訊息。</p>`;
  } else if (state === "skipped") {
    content = `<p class="stage-pending">提交訊息功能目前未啟用，請聯絡系統管理員。</p>`;
  } else if (state === "none") {
    content = `<p class="stage-pending">沒有通過檢查的補丁，因此不會生成提交訊息。</p>`;
  } else {
    content = `<p class="stage-pending">先完成補丁生成；選出補丁後會自動產生提交訊息草稿。</p>`;
  }
  return `<section class="stage" id="stage-commit">
    <div class="stage-head"><h3><span class="num">${n}</span>提交訊息</h3><span class="chip ${chipCls}">${esc(COMMIT_TAB_TEXT[state] || state)}</span></div>
    <div class="stage-body">${content}</div>
  </section>`;
}

function patchTools(patch, filename, source) {
  const id = `p${PATCH_TEXTS.size + 1}`;
  PATCH_TEXTS.set(id, { patch: String(patch || ""), filename, source });
  return `<div class="patch-tools">
      <button type="button" class="btn btn-primary btn-small" data-copy-code="${id}" title="套用補丁後的完整函式，可以直接貼上">複製修改後的程式碼</button>
      <button type="button" class="btn btn-quiet btn-small" data-copy-patch="${id}" title="複製可套用的程式差異">複製補丁</button>
      <button type="button" class="btn btn-quiet btn-small" data-download-patch="${id}">下載 .diff</button>
    </div>`;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  }
}

function downloadText(text, filename) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/x-diff;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function diffFileName(r, suffix = "") {
  return `${String(r.ticket_id || r.key).replace(/[^\w.-]+/g, "_")}${suffix}.diff`;
}

function commitBox(r, source) {
  const draft = (r.commit_messages || {})[source];
  const queued = r.queue && r.queue.phase === "commit";
  if (!draft && !queued) {
    return `<div class="commit-box"><div class="commit-head"><b>提交訊息</b>${commitRegenerate(r, source, "產生提交訊息")}</div>
      <p class="hint">系統會根據 Ticket 與補丁內容產生一行提交訊息草稿，通常只需幾秒鐘。</p></div>`;
  }
  if (queued || draft.status === "running") {
    return `<div class="commit-box"><div class="commit-head"><b>提交訊息</b></div><p class="stage-pending">${r.queue ? esc(queueText(r.queue)) : "正在產生提交訊息…"}</p></div>`;
  }
  return commitMessageBox(r, draft.status === "done" ? { ...draft, status: "generated" } : draft, source);
}

function diffHtml(patch) {
  const lines = String(patch || "").replace(/\n$/, "").split("\n").map((line) => {
    const cls = line.startsWith("+++") || line.startsWith("---") ? "d-file" : line.startsWith("@@") ? "d-hunk" : line.startsWith("+") ? "d-add" : line.startsWith("-") ? "d-del" : "";
    return `<span class="${cls}">${esc(line) || " "}</span>`;
  }).join("");
  return `<pre class="diff">${lines}</pre>`;
}

function officialBox(official, { mine = true } = {}) {
  if (!official) {
    return mine ? `<div class="verdict verdict-none"><b>尚未執行官方測試</b><span>這個補丁沒有官方測試紀錄（與先前送測過的補丁內容不完全相同）。目前只完成程式結構與補丁套用檢查，合併前仍需執行功能與回歸測試。</span></div>` : "";
  }
  const ok = official.official_resolved === true;
  const sha = String(official.patch_sha256 || "").slice(0, 12);
  const when = official.evaluated_at ? `${esc(official.evaluated_at)} ` : "";
  const run = official.run_url
    ? `<a href="${esc(official.run_url)}" target="_blank" rel="noopener">${esc(official.run_id || "查看測試紀錄")}</a>`
    : esc(official.run_id || "");
  const source = `<span class="hint">判定來源：${esc(official.evaluator || "SWE-bench 官方 harness")}；這個補丁與 ${when}送測的補丁完全相同（sha256 <code>${esc(sha)}</code>），沿用該次判定${run ? `：${run}` : ""}。</span>`;
  return `<div class="verdict ${ok ? "verdict-ok" : "verdict-ng"}"><b>SWE-bench 官方測試：${ok ? "通過" : "未通過"}</b>
    <span>${ok ? "問題重現測試（FAIL_TO_PASS）全部通過，既有功能測試（PASS_TO_PASS）沒有退步。" : "官方測試沒有全部通過，請勿直接合併。"}</span>${source}</div>`;
}

function renderPatchStage(r, n) {
  const stage = r.stages.patch || {};
  const s = stageStatus(r, "patch");
  const tabState = patchTabState(r);
  const locDone = stageStatus(r, "localization") === "done";
  const loc = (r.stages.localization || {}).output || {};
  const chipCls = { resolved: "chip-done", generated: "chip-done", none: "chip-fallback", failed: "chip-failed", running: "chip-running" }[tabState] || "";
  const level = { high: "高", medium: "中", low: "低" }[loc.confidence_level] || loc.confidence_level || "—";
  const method = `<p class="patch-method">系統會依錯誤定位結果產生多個修正方案，完成程式結構與套用檢查後，選擇改動範圍最小的方案。</p>`;
  const button = (again) => `<button class="btn ${again ? "btn-quiet" : "btn-primary"} btn-small" data-action="patch" ${again ? 'data-again="1"' : ""} ${r.busy || !locDone ? "disabled" : ""}>${again ? "重新產生" : "產生補丁"}</button>`;
  let content;
  if (s === "running" || (r.queue && r.queue.phase === "patch")) {
    content = `${method}<p class="stage-pending">${r.queue ? esc(queueText(r.queue)) : "正在產生補丁，完成後會自動更新。"}</p>`;
  } else if (s === "pending") {
    const lowNote = locDone && loc.confidence_level !== "high"
      ? `<p class="loc-note">這張 Ticket 的定位信心是「${esc(level)}」，產生的補丁需要人工審查。</p>` : "";
    content = locDone
      ? `${method}${lowNote}<div class="loc-controls">${button(false)}<span class="hint">通常需要幾分鐘，完成後會自動更新。</span></div>`
      : `${method}<p class="stage-pending">要先完成錯誤定位，才能產生補丁。</p>`;
  } else if (s === "failed") {
    content = `<p class="err">${esc(publicStageMessage("patch", stage.error))}</p>${method}<div class="loc-controls">${button(false)}</div>`;
  } else if (s === "skipped") {
    const why = (stage.warnings || []).map((w) => `<li>${esc(publicStageMessage("patch", w, "補丁未產生，請確認前一步結果後重試。"))}</li>`).join("");
    content = `<ul class="warn-list">${why}</ul>${method}${locDone ? `<div class="loc-controls">${button(false)}</div>` : ""}`;
  } else {
    const o = stage.output || {};
    const patchWarnings = (stage.warnings || []).filter((w) => !/\bAST\b|git apply|沒有跑過測試|不等於「修好」|尚未執行完整測試|SWE-bench|harness|官方測試/i.test(String(w)));
    const warnings = patchWarnings.length ? `<ul class="warn-list">${patchWarnings.map((w) => `<li>${esc(publicStageMessage("patch", w, "部分檢查尚未完成，請在合併前人工審查。"))}</li>`).join("")}</ul>` : "";
    const target = o.bug_location || {};
    const generated = (o.candidates || []).filter((c) => c.status === "generated").length;
    const candRows = (o.candidates || []).map((c, index) => `<tr class="${c.candidate_id === o.selected_candidate_id ? "is-selected" : ""}">
        <td>${index + 1}</td>
        <td>${c.status === "generated" ? (c.candidate_id === o.selected_candidate_id ? "<b>已選用</b>" : "基本檢查通過") : "未通過檢查"}</td>
        <td>${esc(patchReason(c.reason)) || (c.modified_files || []).map(esc).join("、")}</td></tr>`).join("");
    const summary = `<div class="loc-summary">
        <span>結果 <span class="chip ${chipCls}">${esc(PATCH_TAB_TEXT[tabState] || tabState)}</span></span>
        <span>改寫目標 <b class="mono">${esc(target.file || "—")}</b> <b>${esc(target.function || "")}</b>${o.target_rank ? `（定位第 ${esc(o.target_rank)} 名${o.target_fallback_used ? "，前面的候選不能改寫" : ""}）` : ""}</span>
        <span>${generated} / ${(o.candidates || []).length} 個修正方案通過基本檢查</span>
        <span>定位信心 <b>${esc(level)}</b></span>
      </div>`;
    const diff = o.patch_status === "generated"
      ? `${officialBox(o.official)}<div class="patch-head"><h4 class="sub-head">補丁 <span class="hint">修改 ${(o.modified_files || []).map(esc).join("、")}</span></h4>${patchTools(o.patch, diffFileName(r), "current")}</div>${diffHtml(o.patch)}<p class="hint">提交訊息草稿在第 7 步「提交訊息」。</p>`
      : `<p class="loc-note">${esc(o.explanation || "沒有候選通過檢查，所以沒有補丁。")}</p>`;
    const others = (o.candidates || []).filter((c) => c.status === "generated" && c.patch && c.candidate_id !== o.selected_candidate_id)
      .map((c, index) => `<details class="raw"><summary>其他可用方案 ${index + 1}（改動範圍較大）</summary>${patchTools(c.patch, diffFileName(r, `-candidate${c.candidate_id}`), `candidate-${c.candidate_id}`)}${diffHtml(c.patch)}</details>`).join("");
    content = `${warnings}${summary}${diff}
      <h4 class="sub-head">修正方案檢查結果</h4>
      <div class="table-wrap"><table class="cand-table"><thead><tr><th>#</th><th>結果</th><th>原因／修改的檔案</th></tr></thead><tbody>${candRows}</tbody></table></div>${others}
      <div class="loc-controls">${button(true)}<a class="btn btn-quiet btn-small" href="/api/tickets/${encodeURIComponent(r.key)}/artifact/patch_result.json">下載完整結果 JSON</a></div>`;
  }
  const foot = s !== "running" ? `<div class="stage-foot"><span>${esc(STAGE_PURPOSE.patch)}</span></div>` : "";
  return `<section class="stage" id="stage-patch">
    <div class="stage-head"><h3><span class="num">${n}</span>補丁生成</h3><span class="chip ${chipCls}">${esc(PATCH_TAB_TEXT[tabState] || STATUS_TEXT[tabState] || tabState)}</span></div>
    <div class="stage-body">${content}</div>${foot}</section>`;
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
  const commitBtn = event.target.closest("[data-copy-commit]");
  if (commitBtn) {
    const message = COMMIT_TEXTS.get(commitBtn.dataset.copyCommit);
    if (message) toast(await copyText(message) ? "已複製提交訊息" : "複製失敗");
    return;
  }
  const codeBtn = event.target.closest("[data-copy-code]");
  if (codeBtn && state.detail) {
    const item = PATCH_TEXTS.get(codeBtn.dataset.copyCode);
    if (!item) return;
    try {
      codeBtn.disabled = true;
      const data = await api(`/api/tickets/${encodeURIComponent(state.detail.key)}/patched-code?source=${encodeURIComponent(item.source)}`);
      const parts = data.files.flatMap((f) => f.functions.map((fn) => ({ ...fn, path: f.path })));
      const text = parts.length === 1
        ? parts[0].code
        : parts.map((fn) => `# ===== ${fn.path}  ${fn.name}（第 ${fn.start_line}–${fn.end_line} 行）=====\n${fn.code}`).join("\n");
      const where = parts.map((fn) => `${fn.name}（${fn.path} 第 ${fn.start_line}–${fn.end_line} 行）`).join("、");
      toast(await copyText(text) ? `已複製修改後的 ${where}` : "複製失敗");
    } catch (err) {
      toast(err.message);
    } finally {
      codeBtn.disabled = false;
    }
    return;
  }
  const copyBtn = event.target.closest("[data-copy-patch]");
  if (copyBtn) {
    const item = PATCH_TEXTS.get(copyBtn.dataset.copyPatch);
    if (item) toast(await copyText(item.patch) ? `已複製${item.label || "補丁"}` : "複製失敗");
    return;
  }
  const downloadBtn = event.target.closest("[data-download-patch]");
  if (downloadBtn) {
    const item = PATCH_TEXTS.get(downloadBtn.dataset.downloadPatch);
    if (item) { downloadText(item.patch, item.filename); toast(`已下載 ${item.filename}`); }
    return;
  }
  const btn = event.target.closest("[data-action]");
  if (!btn || !state.detail) return;
  const key = state.detail.key;
  const path = `/api/tickets/${encodeURIComponent(key)}`;
  if (btn.dataset.action === "pin") { await togglePin(key); return; }
  try {
    btn.disabled = true;
    if (["rerun", "not-duplicate", "is-duplicate", "localize", "localize-other"].includes(btn.dataset.action)) state.tabPinned = false;
    if (btn.dataset.action === "patch") state.tab = "patch";
    switch (btn.dataset.action) {
      case "rerun":
        await post(`${path}/run`, runPreferences());
        toast(state.automationMode === "automatic" ? "已開始全自動處理，將接續生成補丁與提交訊息" : "已開始處理");
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
      case "commit-message":
        await post(`${path}/commit-message`, { source: btn.dataset.source });
        toast("已開始產生提交訊息草稿");
        break;
      case "patch":
        if (btn.dataset.again && !confirm("重新產生可能得到不同結果，且會取代目前的補丁。確定嗎？")) { btn.disabled = false; return; }
        await post(`${path}/patch`);
        toast("已開始產生補丁，完成後會自動更新");
        break;
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
      case "stop": {
        const result = await post(`${path}/stop`, {});
        toast(result.state === "stopping" ? "目前這一步結束後就會停止" : "已停止處理");
        break;
      }
      case "delete": {
        const running = state.detail.queue && state.detail.queue.state === "running";
        if (!confirm(`刪除 ${state.detail.ticket_id}？\n\n這張 Ticket 的處理結果會一起刪除，無法復原。${running ? "\n它正在執行一個步驟，會在這一步結束後刪除。" : ""}`)) { btn.disabled = false; return; }
        const result = await api(path, { method: "DELETE" });
        toast(result.deferred ? `${state.detail.ticket_id} 會在目前這一步結束後刪除` : `已刪除 ${state.detail.ticket_id}`);
        goHome(projectKey(state.detail));
        break;
      }
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
  const options = state.projects.map((p) => [p.project, p.project]);
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
  const base = history ? "這批歷史 Ticket 屬於哪個專案（只會和這個專案的 Ticket 比對）" : "這批 Ticket 屬於哪個專案";
  $("#importProjectLabel").textContent = named.length && unnamed
    ? `其餘 ${unnamed} 張沒寫專案的${history ? "歷史 Ticket 屬於哪個專案" : " Ticket 屬於哪個專案"}`
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
  const body = { project: $("#importProject").value, ...runPreferences() };
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
      toast(`已匯入 ${result.created.length} 張 Ticket，開始${state.automationMode === "automatic" ? "全自動" : "半自動"}處理`);
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
  $("#statusBody").innerHTML = `<div class="status-grid">${STATUS_STAGES.map(([key]) => {
    const s = report.stages[key];
    const title = (STAGES.find(([stageKey]) => stageKey === key) || [key, key])[1];
    const chip = s.ready ? "可用" : "需要設定";
    return `<div class="status-card"><h4>${esc(title)}<span class="chip ${s.ready ? "chip-done" : "chip-fallback"}">${chip}</span></h4>
      <p class="hint">${s.ready ? "此步驟已準備完成。" : "此步驟缺少必要資源，請由系統管理員檢查。"}</p>
      <details class="raw"><summary>查看管理資訊</summary><ul>
        ${s.checks.map((c) => `<li><span class="${c.ok ? "ok" : c.required ? "ng" : "opt"}">${c.ok ? "✓" : c.required ? "✗" : "○"}</span><span>${esc(c.label)}</span>${c.detail ? `<span class="d">${esc(c.detail)}</span>` : ""}</li>`).join("")}
      </ul></details></div>`;
  }).join("")}</div>`;
  renderProjects(projects.projects);
}

function renderProjects(projects) {
  $("#projectBody").innerHTML = projects.length ? projects.map((p) => {
    const job = p.index_job || {};
    const jobText = job.status === "running" ? "建立中…" : job.status === "failed" ? "建立失敗，請聯絡系統管理員。" : job.status === "done" ? `已建立（${job.files} 個檔案，${(job.duration_ms / 1000).toFixed(1)} 秒）` : "";
    return `<div class="project-row"><div><b>${esc(p.project)}</b><br>
      <span class="hint">${p.repo_exists ? "專案程式碼已連結。" : "尚未連結專案程式碼。"} ${p.index_exists ? `搜尋資料更新於 ${esc(p.index_updated_at)}` : "第一次分析程式碼位置時會自動準備搜尋資料。"} ${esc(jobText)}</span></div>
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
  try { applyAutomationMode(localStorage.getItem(MODE_KEY) || "semi"); } catch { applyAutomationMode("semi"); }
  try { applyPreferences(JSON.parse(localStorage.getItem(PREFERENCES_KEY) || "{}")); } catch { applyPreferences(); }
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
    else if (e.target.closest("[data-pick-stop]")) stopPicked();
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
  $("#settingsButton").addEventListener("click", openSettings);
  $("#settingsForm").addEventListener("submit", saveSettings);
  $("#testNotificationButton").addEventListener("click", sendTestNotification);
  $$('#settingsForm input[name="automationMode"]').forEach((input) => input.addEventListener("change", renderModeNotice));
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
