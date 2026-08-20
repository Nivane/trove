/* Trove chat UI — zero-build vanilla JS (precision tool design).
   Consumes the SSE event stream from POST /v1/chat (wire format in
   trove/api/sse.py: `event: <type>\ndata: <json>\n\n`), renders step
   cards + the final markdown answer, and manages sessions via
   /v1/sessions. */

"use strict";

/* ── i18n (UI chrome labels; step-card labels use the backend `lang`) ── */

const I18N = {
  zh: {
    title: "Trove 数据问答", newSession: "新建会话", placeholder: "输入问题…",
    send: "发送", stop: "停止", delete: "删除", noSessions: "暂无会话",
    processing: "思考中", copy: "复制", copied: "已复制",
    retry: (n) => `重试#${n}`, round: (n) => `第 ${n} 轮`,
    confirmDelete: "删除该会话？", datasource: "数据源",
    meta: (n) => `${n} 条消息`, aborted: "已停止",
    sessionNotFound: "会话不存在，已新建会话", error: "错误",
    hitlApprove: "批准执行", hitlReject: "否决",
    hitlApproveOne: "确认", hitlApproveAll: "确认，继续全部任务", hitlStop: "不继续",
    hitlChosen: (d) => `已选择：${d}`,
    taskPanel: "任务进度",
    sessionsLabel: "会话", collapseTitle: "折叠侧栏",
    themeLight: "主题：浅色", themeDark: "主题：深色",
    stepsSummary: (n, ms, tokens) => `${n} 步 · ${ms}ms${tokens ? ` · ${tokens}` : ""}`,
    welcomeTitle: "Trove 数据问答",
    welcomeSubtitle: "用自然语言提问，自动生成 SQL、执行并验证答案",
    disclaimer: "内容由 AI 生成，可能出错，重要信息请核对",
    langBtn: "EN",
    noSlashMatch: "没有匹配的命令",
    noSession: "未选择会话",
    sessionCleared: "会话已清空",
    sessionCompacted: (n) => `会话已压缩，保留最近 ${n} 条消息`,
    suggestions: [
      "哪个地区的平均贷款金额最高？",
      "列出各地区的账户数量",
      "贷款状态分布如何？",
      "这个数据源有几张表？",
    ],
  },
  en: {
    title: "Trove Chat", newSession: "New chat", placeholder: "Ask a question…",
    send: "Send", stop: "Stop", delete: "Delete", noSessions: "No sessions yet",
    processing: "Thinking", copy: "Copy", copied: "Copied",
    retry: (n) => `retry #${n}`, round: (n) => `Round ${n}`,
    confirmDelete: "Delete this session?", datasource: "Datasource",
    meta: (n) => `${n} messages`, aborted: "Stopped",
    sessionNotFound: "Session missing — created a new one", error: "Error",
    hitlApprove: "Approve", hitlReject: "Reject",
    hitlApproveOne: "Confirm", hitlApproveAll: "Confirm, continue all", hitlStop: "Stop",
    hitlChosen: (d) => `Chosen: ${d}`,
    taskPanel: "Tasks",
    sessionsLabel: "Sessions", collapseTitle: "Toggle sidebar",
    themeLight: "Theme: light", themeDark: "Theme: dark",
    stepsSummary: (n, ms, tokens) => `${n} steps · ${ms}ms${tokens ? ` · ${tokens}` : ""}`,
    welcomeTitle: "Trove Chat",
    welcomeSubtitle: "Ask in natural language — SQL is generated, executed and verified",
    disclaimer: "AI-generated — verify important information",
    langBtn: "中文",
    noSlashMatch: "No matching command",
    noSession: "No active session",
    sessionCleared: "Session cleared",
    sessionCompacted: (n) => `Session compacted — ${n} messages remain`,
    suggestions: [
      "Which region has the highest average loan amount?",
      "List account counts by region",
      "What is the distribution of loan statuses?",
      "How many tables does this datasource have?",
    ],
  },
};

/* Step-card labels per node — texts mirror the REPL's _step_summary
   (trove/cli/app.py); parse_date/answer_metadata/metadata_check/clarify
   are web-UI additions. */

const NODE_LABELS = {
  route_intent: {
    zh: (d) => `意图：${d.intent ?? "query"}`,
    en: (d) => `intent: ${d.intent ?? "query"}`,
  },
  schema_linking: {
    zh: (d) => `匹配 ${(d.matched_tables ?? []).length} 表${d.kb_terms ? `，${d.kb_terms} 术语` : ""}`,
    en: (d) => `matched ${(d.matched_tables ?? []).length} tables${d.kb_terms ? `, ${d.kb_terms} terms` : ""}`,
  },
  planner: { zh: () => "生成查询计划", en: () => "drafting query plan" },
  gen_sql: {
    zh: (d) => `生成 SQL（校验 ${d.attempts ?? 1} 次）`,
    en: (d) => `generating SQL (${d.attempts ?? 1} validation passes)`,
  },
  execute_sql: {
    zh: (d) => `${d.row_count ?? -1} 行`,
    en: (d) => `${d.row_count ?? -1} rows`,
  },
  select: {
    zh: (d) => (d.consensus ? "候选一致" : "候选不一致（低置信）"),
    en: (d) => (d.consensus ? "candidates agree" : "candidates disagree (low confidence)"),
  },
  validate: {
    zh: (d) => (d.reason ? "规则失败" : "通过"),
    en: (d) => (d.reason ? "rule failed" : "passed"),
  },
  analyze_error: {
    zh: (d) => `诊断${d.rollback ? ` · 回退 → ${d.rollback}` : ""}`,
    en: (d) => `diagnosis${d.rollback ? ` · rollback → ${d.rollback}` : ""}`,
  },
  reflect: {
    zh: (d) => `裁决 ${d.verdict ?? ""}${d.verdict && d.verdict !== "OK" ? `：${d.reason ?? ""}` : ""}`,
    en: (d) => `verdict ${d.verdict ?? ""}${d.verdict && d.verdict !== "OK" ? `: ${d.reason ?? ""}` : ""}`,
  },
  output: { zh: () => "生成答案", en: () => "composing answer" },
  parse_date: { zh: () => "解析日期", en: () => "parsing date" },
  answer_metadata: { zh: () => "回答元数据", en: () => "answering metadata" },
  metadata_check: { zh: () => "元数据核查", en: () => "checking metadata" },
  clarify: { zh: () => "追问澄清", en: () => "asking clarification" },
};

/* Step-card body field labels (keyed by the backend `lang` of the step). */

const BODY_K = {
  zh: {
    intent: "意图", evidence: "依据", tables: "匹配表", terms: "术语",
    attempts: "校验次数", reason: "原因", error: "错误", analysis: "分析",
    rollback: "回退", rows: "行数", time: "耗时", verdict: "裁决",
    consensus: "候选一致",
  },
  en: {
    intent: "intent", evidence: "evidence", tables: "matched tables", terms: "terms",
    attempts: "validation passes", reason: "reason", error: "error", analysis: "analysis",
    rollback: "rollback", rows: "rows", time: "time", verdict: "verdict",
    consensus: "consensus",
  },
};

/* Inline SVG icons (lucide-style, stroke = currentColor). */
const ICONS = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  trash: '<path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
  chevronDown: '<path d="m6 9 6 6 6-6"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>',
  arrowUp: '<path d="M12 19V5M5 12l7-7 7 7"/>',
  stop: '<rect x="7" y="7" width="10" height="10" rx="2"/>',
  arrowUpRight: '<path d="M7 17 17 7M8 7h9v9"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
  moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
  monitor: '<rect x="2" y="4" width="20" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>',
  panelLeft: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  dot: '<circle cx="12" cy="12" r="4"/>',
};

/* Per-node step-card icons. */
const NODE_ICONS = {
  route_intent: '<circle cx="12" cy="12" r="10"/><path d="m16.24 7.76-2.12 6.36-6.36 2.12 2.12-6.36z"/>',
  parse_date: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 11h18"/>',
  schema_linking: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M9 4v16"/>',
  planner: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
  gen_sql: '<path d="m16 18 6-6-6-6M8 6l-6 6 6 6"/>',
  execute_sql: '<path d="M6 4.5v15l13-7.5-13-7.5Z"/>',
  select: '<circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M6 9v3a6 6 0 0 0 6 6h3"/>',
  validate: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>',
  reflect: '<path d="M12 3v18M5 8h14M5 8a3 3 0 1 0 0-.01M19 8a3 3 0 1 0 0-.01"/>',
  analyze_error: '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
  output: '<path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>',
  answer_metadata: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
  metadata_check: '<path d="M3.85 8.62a4 4 0 0 1 4.78-4.77 4 4 0 0 1 6.74 0 4 4 0 0 1 4.78 4.78 4 4 0 0 1 0 6.74 4 4 0 0 1-4.77 4.78 4 4 0 0 1-6.75 0 4 4 0 0 1-4.78-4.77 4 4 0 0 1 0-6.76Z"/><path d="m9 12 2 2 4-4"/>',
  clarify: '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3M12 17h.01"/>',
};

function svgIcon(inner) {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ` +
    `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${inner}</svg>`;
}

function icon(name) {
  return svgIcon(ICONS[name] || ICONS.dot);
}

/* ── Safe DOM helpers (a stale cached page may lack newer elements —
   degrade gracefully instead of throwing mid-init) ── */

const $ = (id) => document.getElementById(id);

function setText(id, text) { const el = $(id); if (el) el.textContent = text; }
function setHtml(id, html) { const el = $(id); if (el) el.innerHTML = html; }
function setTitle(id, text) { const el = $(id); if (el) el.title = text; }
function on(id, evt, fn) { const el = $(id); if (el) el.addEventListener(evt, fn); }

/* Two-state theme: an explicit light/dark choice is persisted; before the
   first toggle the system preference wins (legacy "auto" values count as
   unset). */
function storedTheme() {
  const v = localStorage.getItem("trove_ui_theme");
  if (v === "light" || v === "dark") return v;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

const state = {
  lang: localStorage.getItem("trove_ui_lang") || "zh",
  theme: storedTheme(),
  sidebarCollapsed: localStorage.getItem("trove_ui_sidebar") === "1",
  sessionId: localStorage.getItem("trove_ui_session") || null,
  sessions: [],
  titles: {},        // session_id → derived title (first user question)
  currentTurn: null, // {el, stepsEl, statusEl, answerEl, toolbarEl, round, finished}
  controller: null,  // AbortController of the in-flight /v1/chat stream
  pendingHitl: null, // HITL: {sessionId, workflow, batch} — waiting for user decision
  batchRunning: false, // 批处理中:逐任务的 done 只追加答案,不结束整轮
};

function t(key, ...args) {
  const v = I18N[state.lang][key];
  return typeof v === "function" ? v(...args) : v;
}

/* ── XSS-safe helpers ────────────────────────────────── */

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/* ── Theme (auto / light / dark, persisted) ──────────── */

function applyTheme() {
  document.documentElement.dataset.theme = state.theme;
  const btn = $("theme-toggle");
  if (btn) {
    btn.innerHTML = icon(state.theme === "dark" ? "moon" : "sun");
    btn.title = t(state.theme === "dark" ? "themeDark" : "themeLight");
  }
}

function cycleTheme() {
  state.theme = state.theme === "dark" ? "light" : "dark";
  localStorage.setItem("trove_ui_theme", state.theme);
  applyTheme();
}

function applySidebar() {
  document.body.classList.toggle("sidebar-collapsed", state.sidebarCollapsed);
}

function closeDrawer() {
  document.body.classList.remove("sidebar-open");
}

/* ── SSE frame parser (POST → fetch stream) ──────────── */

function createSSEParser() {
  let buffer = "";
  return function feed(chunk) {
    buffer += chunk;
    const events = [];
    let idx;
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      let type = "message", data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) type = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim() + "\n";
      }
      try {
        events.push({ type, data: JSON.parse(data.trim()) });
      } catch {
        /* skip malformed frame */
      }
    }
    return events;
  };
}

/* ── Markdown mini-renderer (escape-first; covers the shapes emitted by
   trove/workflow/nodes/output.py + free-form metadata answers) ── */

const NUMERIC_RE = /^-?[\d,.\s]+(?:%|ms|[€$¥])?$/;

function inlineTransforms(text) {
  /* text is already HTML-escaped. Split on code spans so `*not italic*`
     inside backticks survives the emphasis transforms. */
  const parts = text.split(/`([^`]+)`/g);
  let out = "";
  for (let i = 0; i < parts.length; i++) {
    if (i % 2 === 1) {
      out += `<code>${parts[i]}</code>`;
    } else {
      out += parts[i]
        .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
        .replace(/\*([^*]+)\*/g, "<em>$1</em>");
    }
  }
  return out;
}

/* Code block with header bar (language label + icon-only copy button). */
function codeBlock(escapedCode, lang) {
  const label = (lang || "code").toUpperCase();
  return `<div class="code-block">` +
    `<div class="code-header"><span class="code-lang">${esc(label)}</span>` +
    `<button type="button" class="copy-btn" title="${esc(t("copy"))}" aria-label="${esc(t("copy"))}">${icon("copy")}</button></div>` +
    `<code>${escapedCode}</code></div>`;
}

function splitCells(line) {
  const cells = line.split("|");
  if (cells.length && cells[0].trim() === "") cells.shift();
  if (cells.length && cells[cells.length - 1].trim() === "") cells.pop();
  return cells.map((c) => c.trim());
}

function isSeparatorRow(line) {
  const cells = splitCells(line);
  return cells.length > 0 && cells.every((c) => /^:?-{2,}:?$/.test(c));
}

function renderMarkdown(md) {
  const lines = String(md ?? "").replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let para = [];

  const flushPara = () => {
    if (para.length) {
      out.push(`<p>${inlineTransforms(para.join("\n")).replace(/\n/g, "<br>")}</p>`);
      para.length = 0;
    }
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    // Fenced code block — no inline transforms inside.
    const fence = line.match(/^```([a-zA-Z]*)\s*$/);
    if (fence) {
      flushPara();
      const code = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) {
        code.push(lines[i]);
        i++;
      }
      out.push(codeBlock(esc(code.join("\n")), fence[1] || "code"));
      continue;
    }

    const h = line.match(/^(#{2,3})\s+(.+)$/);
    if (h) {
      flushPara();
      const tag = h[1] === "###" ? "h3" : "h2";
      out.push(`<${tag}>${inlineTransforms(h[2])}</${tag}>`);
      continue;
    }

    if (/^---+\s*$/.test(line)) {
      flushPara();
      out.push("<hr>");
      continue;
    }

    // Table: header row + `---` separator row. Rows whose cell count
    // mismatches the header (e.g. unescaped `|` in a data value) degrade
    // to a plain paragraph.
    if (line.startsWith("|") && i + 1 < lines.length && isSeparatorRow(lines[i + 1])) {
      flushPara();
      const headerCells = splitCells(line).map(inlineTransforms);
      const rows = [];
      i += 2;
      while (i < lines.length && lines[i].startsWith("|")) {
        const cells = splitCells(lines[i]);
        if (cells.length === headerCells.length) {
          rows.push(
            `<tr>${cells.map((c) =>
              `<td${NUMERIC_RE.test(c) ? ' class="num"' : ""}>${inlineTransforms(c)}</td>`).join("")}</tr>`,
          );
        } else {
          out.push(`<p>${inlineTransforms(lines[i])}</p>`);
        }
        i++;
      }
      i--;
      out.push(
        `<div class="table-wrap"><table><thead><tr>` +
        `${headerCells.map((c) => `<th>${c}</th>`).join("")}</tr></thead>` +
        `<tbody>${rows.join("")}</tbody></table></div>`,
      );
      continue;
    }

    // Lists: consecutive `- `/`* ` or `1. ` lines form one list.
    const ul = line.match(/^[-*]\s+(.+)$/);
    const ol = line.match(/^\d+[.)]\s+(.+)$/);
    if (ul || ol) {
      flushPara();
      const items = [];
      const tag = ul ? "ul" : "ol";
      const rx = ul ? /^[-*]\s+(.+)$/ : /^\d+[.)]\s+(.+)$/;
      while (i < lines.length && rx.test(lines[i])) {
        items.push(`<li>${inlineTransforms(lines[i].match(rx)[1])}</li>`);
        i++;
      }
      i--;
      out.push(`<${tag}>${items.join("")}</${tag}>`);
      continue;
    }

    if (line.trim() === "") {
      flushPara();
      continue;
    }

    para.push(line);
  }
  flushPara();
  return out.join("\n");
}

/* ── Bubbles & step cards ────────────────────────────── */

function updateWelcome() {
  const el = $("welcome");
  if (!el) return;
  const hasMessages = $("message-list").querySelector(".bubble, .turn") !== null;
  el.hidden = hasMessages;
}

function appendUserBubble(text) {
  const el = document.createElement("div");
  el.className = "bubble user";
  el.textContent = text;
  $("message-list").appendChild(el);
  updateWelcome();
  scrollToBottom();
  return el;
}

function beginAssistantTurn() {
  const el = document.createElement("div");
  el.className = "turn";
  el.innerHTML =
    `<div class="status"><span class="dots"><span></span><span></span><span></span></span>` +
    `<span class="status-text">${esc(t("processing"))}</span></div>
    <div class="steps-wrap"><div class="steps"></div></div>
    <button type="button" class="steps-summary" hidden></button>
    <div class="answer markdown"></div>
    <div class="answer-toolbar" hidden>
      <button type="button" class="copy-btn copy-answer-btn" title="${esc(t("copy"))}" aria-label="${esc(t("copy"))}">${icon("copy")}</button>
    </div>`;
  $("message-list").appendChild(el);
  scrollToBottom();
  state.currentTurn = {
    el,
    stepsEl: el.querySelector(".steps"),
    stepsWrap: el.querySelector(".steps-wrap"),
    summaryEl: el.querySelector(".steps-summary"),
    statusEl: el.querySelector(".status"),
    answerEl: el.querySelector(".answer"),
    toolbarEl: el.querySelector(".answer-toolbar"),
    steps: [],       // {label, elapsed_ms, icon} — for the summary trail
    round: 0,
    finished: false,
  };
  return state.currentTurn;
}

function scrollToBottom() {
  const list = $("message-list");
  list.scrollTop = list.scrollHeight;
}

function roundDivider(n, lang) {
  const label = lang === "zh" ? `第 ${n} 轮` : `Round ${n}`;
  return `<div class="round-divider">${esc(label)}</div>`;
}

function renderStep(ev, turn) {
  const { seq, node, elapsed_ms, lang, detail } = ev;
  const stepLang = lang === "zh" ? "zh" : "en";
  const k = BODY_K[stepLang];
  const labelFn = NODE_LABELS[node];
  const label = labelFn ? labelFn[stepLang](detail) : node;

  // A new reflection round starts on rollback (analyze_error). A non-OK
  // reflect verdict belongs to the round it judged — no divider there.
  if (node === "analyze_error") {
    turn.round += 1;
    turn.stepsEl.insertAdjacentHTML("beforeend", roundDivider(turn.round, stepLang));
  }

  const retry = detail.retry > 0
    ? `<span class="retry-badge">${esc(stepLang === "zh" ? `重试#${detail.retry}` : `retry #${detail.retry}`)}</span>`
    : "";

  const body = stepBody(node, detail, k, stepLang);
  const row = document.createElement("div");
  row.className = "step running";
  row.innerHTML =
    `<div class="step-header">` +
    `<span class="seq">${esc(String(seq).padStart(2, "0"))}</span>` +
    `<span class="step-icon">${svgIcon(NODE_ICONS[node] || ICONS.dot)}</span>` +
    `<span class="step-label">${esc(label)}</span>` +
    `${retry}` +
    `<span class="elapsed">${esc(`${elapsed_ms}ms`)}</span>` +
    (body ? `<span class="chevron">${icon("chevronDown")}</span>` : "") +
    `</div>` +
    (body ? `<div class="step-body">${body}</div>` : "");
  row.querySelector(".step-header").addEventListener("click", () => {
    row.classList.toggle("expanded");
  });
  // Accordion: only the newest step is expanded while streaming.
  turn.stepsEl.querySelectorAll(".step.expanded")
    .forEach((s) => s.classList.remove("expanded"));
  if (body) row.classList.add("expanded");
  turn.stepsEl.querySelectorAll(".step.running")
    .forEach((s) => s.classList.remove("running"));
  row.classList.add("running");
  turn.stepsEl.appendChild(row);

  turn.steps.push({ label, elapsed_ms, icon: NODE_ICONS[node] || ICONS.dot });
  const statusText = turn.statusEl.querySelector(".status-text");
  statusText.textContent = label;
  scrollToBottom();
}

function kvLine(k, v) {
  return v ? `<div class="kv"><span class="k">${esc(k)}</span>${esc(v)}</div>` : "";
}

function llmLine(llm) {
  if (!llm) return "";
  const model = typeof llm === "string" ? llm : llm.model;
  return kvLine("llm", model);
}

/* intent_evidence is a dict of routing signals — render the ones that
   fired instead of stringifying the object ("[object Object]"). */
function evidenceLine(ev, k) {
  if (!ev || typeof ev !== "object") return "";
  const signals = [
    "strong_match", "data_signal", "write_signal", "chitchat_signal",
    "correction_signal", "followup_signal", "weak_signal",
    "mentioned_table", "term_hit", "rewritten", "substituted",
  ].filter((s) => ev[s]);
  const parts = signals.slice();
  if (ev.llm_verdict) parts.push(`llm_verdict: ${ev.llm_verdict}`);
  if (ev.llm_error) parts.push(`llm_error: ${ev.llm_error}`);
  if (ev.history_present === false) parts.push("no_history");
  return parts.length ? kvLine(k.evidence, parts.join(" · ")) : "";
}

function stepBody(node, d, k, stepLang) {
  switch (node) {
    case "route_intent":
      return kvLine(k.intent, d.intent) + evidenceLine(d.intent_evidence, k) + llmLine(d.llm);
    case "schema_linking": {
      const chips = (d.matched_tables ?? [])
        .map((tb) => `<span class="chip">${esc(tb)}</span>`).join("");
      return `<div class="kv"><span class="k">${esc(k.tables)}</span></div>` +
        `<div class="chips">${chips}</div>`;
    }
    case "planner":
      return d.plan ? `<div class="plan">${esc(d.plan)}</div>` : "";
    case "gen_sql":
      return (d.sql ? codeBlock(esc(d.sql), "sql") : "") +
        kvLine(k.attempts, d.attempts) + kvLine(k.reason, d.reason) + llmLine(d.llm);
    case "execute_sql":
      return kvLine(k.rows, d.row_count) +
        kvLine(k.time, d.execution_time_ms != null ? `${d.execution_time_ms}ms` : "") +
        kvLine(k.reason, d.reason);
    case "analyze_error":
      return kvLine(k.error, d.error) + kvLine(k.analysis, d.analysis) + kvLine(k.rollback, d.rollback);
    case "select":
      return kvLine(k.consensus, d.consensus ? "true" : "false");
    case "validate":
      return kvLine(k.reason, d.reason);
    case "reflect":
      return kvLine(k.verdict, d.verdict) + kvLine(k.reason, d.reason);
    default:
      return "";
  }
}

/* ── SSE event → DOM ─────────────────────────────────── */

function handleEvent(ev, turn) {
  const { type, data } = ev;
  if (data && data.summary) turn.lastSummary = data.summary;
  switch (type) {
    case "session":
      state.sessionId = data.session_id;
      localStorage.setItem("trove_ui_session", state.sessionId);
      hideTaskPanel();
      refreshSessions();
      break;
    case "thought":
      turn.statusEl.querySelector(".status-text").textContent = t("processing");
      break;
    case "step":
      renderStep(data, turn);
      break;
    case "task":
      /* 任务清单快照:面板同步;批内任一任务进行中 → 后续逐任务
         done 只追加答案,直到收尾的 batched done 才结束整轮。 */
      renderTaskPanel(data.tasks);
      // 批未终结判定:只要还有 pending/in_progress 任务,批就仍在跑
      // (HITL 暂停处任务已 done、剩余 pending → 仍是批,中间 done 只追加;
      // 全部终结后到来的收尾 batched done 才结束本轮)。
      state.batchRunning = (data.tasks || [])
        .some((tsk) => tsk.status === "pending" || tsk.status === "in_progress");
      break;
    case "done":
      if (data.summary && data.summary.batched) {
        /* 批收尾:整批结束,结束本轮 */
        state.batchRunning = false;
        finishTurn(turn, data.summary);
        if (data.content) {
          turn.answerEl.innerHTML += renderMarkdown(data.content);
        }
      } else if (state.batchRunning) {
        /* 批内单个任务完成:追加答案块,继续批 */
        turn.answerEl.innerHTML += renderMarkdown(data.content || "");
      } else {
        finishTurn(turn, data.summary);
        turn.answerEl.innerHTML = renderMarkdown(data.content);
      }
      break;
    case "hitl":
      /* 执行前人工确认(HITL):展示 SQL+语义,提供批准/否决按钮。
         流在此关闭(图已暂停),按钮触发 /resume 继续同一线程。
         批内任务(batch)给三选项:1 确认 / 2 确认并继续全部 / 3 不继续。 */
      state.pendingHitl = {
        sessionId: state.sessionId,
        workflow: "reflection",
        batch: ((data.payload || {}).task_context || {}).total > 1,
        actionsEl: null,
      };
      finishTurn(turn, data.summary);
      turn.answerEl.innerHTML =
        renderMarkdown(data.content || "") +
        (state.pendingHitl.batch
          ? `<div class="hitl-actions hitl-3">
               <button class="hitl-yes" onclick="resumeHitl('yes')">${esc(t("hitlApproveOne"))}</button>
               <button class="hitl-all" onclick="resumeHitl('approve_all')">${esc(t("hitlApproveAll"))}</button>
               <button class="hitl-no" onclick="resumeHitl('no')">${esc(t("hitlStop"))}</button>
             </div>`
          : `<div class="hitl-actions">
               <button class="hitl-yes" onclick="resumeHitl('yes')">${esc(t("hitlApprove"))}</button>
               <button class="hitl-no" onclick="resumeHitl('no')">${esc(t("hitlReject"))}</button>
             </div>`);
      state.pendingHitl.actionsEl = turn.answerEl.querySelector(".hitl-actions");
      break;
    case "error":
      state.batchRunning = false;
      finishTurn(turn, data.summary);
      if (data.summary) {
        turn.answerEl.innerHTML =
          `<div class="error-box">${renderMarkdown(data.content)}</div>`;
      } else {
        turn.answerEl.innerHTML =
          `<div class="error-box">${esc(data.content || t("error"))}</div>`;
      }
      break;
    /* plan/verdict/correction/sql/result are legacy flat events fully
       covered by `step` cards — ignored (same as the REPL). */
  }
}

/* ── 任务面板(消息流顶部的跨轮 todo 状态) ─────────────── */

const TASK_MARKS = {
  pending: "○", in_progress: "◐", done: "✓", failed: "✗", skipped: "–",
};

function ensureTaskPanel() {
  let panel = $("task-panel");
  if (!panel) {
    panel = document.createElement("div");
    panel.id = "task-panel";
    panel.className = "task-panel hidden";
    const list = $("message-list");
    list.insertBefore(panel, list.firstChild);
  }
  return panel;
}

function hideTaskPanel() {
  const panel = $("task-panel");
  if (panel) {
    panel.classList.add("hidden");
    panel.innerHTML = "";
  }
  state.batchRunning = false;
}

function renderTaskPanel(tasks) {
  if (!tasks || !tasks.length) {
    hideTaskPanel();
    return;
  }
  const panel = ensureTaskPanel();
  const chips = tasks.map((tsk) => {
    const st = tsk.status || "pending";
    const mark = TASK_MARKS[st] || "·";
    const title = String(tsk.title || "").slice(0, 32);
    return `<span class="task-chip task-${esc(st)}" title="${esc(title)}">` +
      `<span class="task-mark">${esc(mark)}</span> ${esc((tsk.position ?? 0) + 1)}. ${esc(title)}</span>`;
  }).join("");
  panel.innerHTML =
    `<span class="task-panel-title">${esc(t("taskPanel"))}</span> ${chips}`;
  panel.classList.remove("hidden");
}

function turnStats(summary, steps) {
  /* Run-level cost stats from the done summary (server is authoritative);
     fall back to the step-accumulated total time for old servers that
     omit summary stats. */
  let total = summary && summary.total_elapsed_ms != null
    ? summary.total_elapsed_ms
    : steps.reduce((a, s) => a + s.elapsed_ms, 0);
  let tokens = "";
  if (summary && summary.token_usage) {
    const u = summary.token_usage;
    tokens = `tokens ${u.prompt ?? 0}+${u.completion ?? 0}=${u.total ?? 0}`;
  }
  return { total, tokens };
}

function finishTurn(turn, summary) {
  turn.finished = true;
  turn.stepsEl.querySelectorAll(".step.running")
    .forEach((s) => s.classList.replace("running", "done"));
  // Collapse the trace into a one-line summary (click to re-expand).
  const stats = turnStats(summary, turn.steps);
  const iconsHtml = turn.steps
    .map((s) => `<span class="s-icon">${svgIcon(s.icon)}</span>`).join("");
  turn.summaryEl.innerHTML =
    `${iconsHtml}<span class="s-text">${esc(t("stepsSummary", turn.steps.length, stats.total, stats.tokens))}</span>`;
  turn.summaryEl.hidden = false;
  turn.stepsWrap.classList.add("hidden");
  // 用 onclick 覆盖而非 addEventListener 累加:finishTurn 理论上只会被
  // 收尾事件调用一次,但双绑定会让一次点击 toggle 两次 → 无法展开。
  turn.summaryEl.onclick = () => turn.stepsWrap.classList.toggle("hidden");
  turn.statusEl.remove();
  turn.toolbarEl.hidden = false;
  state.controller = null;
  state.currentTurn = null;
  setStreaming(false);
  scrollToBottom();
}

/* Stop pressed / session switched mid-stream: neutral rail, "aborted"
   status, and the composer back to idle (the partial trace stays). */
function abortTurn(turn) {
  if (!turn || turn.finished) return;
  turn.finished = true;
  turn.el.classList.add("aborted");
  turn.statusEl.classList.add("aborted");
  const st = turn.statusEl.querySelector(".status-text");
  if (st) st.textContent = t("aborted");
  turn.stepsEl.querySelectorAll(".step.running")
    .forEach((s) => s.classList.replace("running", "done"));
  state.controller = null;
  state.currentTurn = null;
  setStreaming(false);
}

function abortCurrentTurn() {
  const turn = state.currentTurn;
  if (turn && !turn.finished) {
    if (state.controller) state.controller.abort();
    abortTurn(turn);
  }
}

function setStreaming(on) {
  const input = $("question-input");
  const send = $("send-btn");
  const stop = $("stop-btn");
  if (input) input.disabled = on;
  if (send) send.disabled = on || !(input && input.value.trim());
  if (stop) stop.hidden = !on;
}

/* ── Send flow ───────────────────────────────────────── */

async function sendQuestion(text, retried = false) {
  const question = text.trim();
  if (!question || (state.currentTurn && !state.currentTurn.finished)) return;
  $("question-input").value = "";

  // Slash commands: catalog queries render locally; /clear and /compact
  // call the session endpoints. /kb /model /init /exit /trace remain
  // REPL-only.
  if (question.startsWith("/")) {
    await runSlashCommand(question);
    return;
  }

  const bubbleEl = appendUserBubble(question);
  const turn = beginAssistantTurn();
  setStreaming(true);

  // First question of a session → derive its title (sidebar + topbar).
  if (!state.titles[state.sessionId]) {
    state.titles[state.sessionId] = question.slice(0, 40);
    setText("session-title", state.titles[state.sessionId]);
  }

  const controller = new AbortController();
  state.controller = controller;
  $("stop-btn").onclick = () => controller.abort();

  let res;
  try {
    res = await fetch("/v1/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: state.sessionId,
        question,
        workflow: "reflection",
      }),
      signal: controller.signal,
    });
  } catch (err) {
    if (err.name === "AbortError") {
      abortTurn(turn);
      return;
    }
    finishTurn(turn);
    turn.answerEl.innerHTML = `<div class="error-box">${esc(String(err))}</div>`;
    return;
  }

  if (!res.ok) {
    // Stale localStorage session (deleted server-side) → recreate once.
    // Drop the stub bubble/turn so the retry renders a single clean pair.
    if (res.status === 404 && state.sessionId && !retried) {
      state.sessionId = null;
      localStorage.removeItem("trove_ui_session");
      turn.finished = true;
      state.currentTurn = null;
      state.controller = null;
      bubbleEl.remove();
      turn.el.remove();
      const note = document.createElement("div");
      note.className = "sys-note";
      note.textContent = t("sessionNotFound");
      $("message-list").appendChild(note);
      setStreaming(false);
      await createSession();
      await sendQuestion(question, true);
      return;
    }
    finishTurn(turn);
    turn.answerEl.innerHTML = `<div class="error-box">${esc(`HTTP ${res.status}`)}</div>`;
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  const feed = createSSEParser();
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      for (const ev of feed(decoder.decode(value, { stream: true }))) {
        handleEvent(ev, turn);
      }
    }
  } catch (err) {
    if (err.name !== "AbortError") {
      finishTurn(turn);
      turn.answerEl.innerHTML = `<div class="error-box">${esc(String(err))}</div>`;
      return;
    }
    abortTurn(turn);
    return;
  }
  if (!turn.finished) {
    finishTurn(turn, turn.lastSummary);
    // Stream closed without a terminal event → surface an error.
    if (!turn.answerEl.textContent.trim()) {
      turn.answerEl.innerHTML = `<div class="error-box">${esc(t("error"))}</div>`;
    }
  }
}

/* ── HITL 确认 → /resume ─────────────────────────────── */

function hitlDecisionLabel(decision, batch) {
  if (batch) {
    return decision === "approve_all" ? t("hitlApproveAll")
      : decision === "yes" ? t("hitlApproveOne") : t("hitlStop");
  }
  return decision === "yes" ? t("hitlApprove") : t("hitlReject");
}

async function resumeHitl(decision) {
  const pending = state.pendingHitl;
  state.pendingHitl = null;
  if (!pending || !pending.sessionId) return;
  /* 点击即替换按钮为所选结果,避免旧气泡一直挂着失效按钮。 */
  if (pending.actionsEl && pending.actionsEl.isConnected) {
    pending.actionsEl.insertAdjacentHTML(
      "afterend",
      `<div class="hitl-chosen">${esc(t("hitlChosen", hitlDecisionLabel(decision, pending.batch)))}</div>`,
    );
    pending.actionsEl.remove();
  }
  const turn = beginAssistantTurn();
  setStreaming(true);
  $("question-input").value = "";

  let res;
  try {
    res = await fetch(`/v1/sessions/${encodeURIComponent(pending.sessionId)}/resume`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision, workflow: "reflection" }),
    });
  } catch (err) {
    finishTurn(turn);
    turn.answerEl.innerHTML = `<div class="error-box">${esc(String(err))}</div>`;
    return;
  }
  if (!res.ok) {
    finishTurn(turn);
    turn.answerEl.innerHTML = `<div class="error-box">${esc(`HTTP ${res.status}`)}</div>`;
    return;
  }

  /* SSE 流与 /v1/chat 同构:批内 approve_all 时逐任务 done + 收尾
     batched done;其余情形等价于原来的 JSON 终态。 */
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  const feed = createSSEParser();
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      for (const ev of feed(decoder.decode(value, { stream: true }))) {
        handleEvent(ev, turn);
      }
    }
  } catch (err) {
    finishTurn(turn);
    turn.answerEl.innerHTML = `<div class="error-box">${esc(String(err))}</div>`;
    return;
  }
  if (!turn.finished) {
    finishTurn(turn, turn.lastSummary);
    if (!turn.answerEl.textContent.trim()) {
      turn.answerEl.innerHTML = `<div class="error-box">${esc(t("error"))}</div>`;
    }
  }
}

/* ── Slash commands (display-only) ───────────────────── */

const CMD_LABELS = {
  zh: {
    helpTitle: "可用命令", cmdHead: "命令", descHead: "说明",
    unknown: (n) => `未知命令 /${n}`, hint: "输入 /help 查看可用命令",
    ds: "数据源", type: "类型", conn: "地址", def: "默认",
    attr: "属性", val: "值",
    table: "表", cols: "列数", rows: "约行数",
    col: "列名", colType: "类型", pk: "主键", nullable: "可空",
    usage: (u) => `用法：/${u}`, noDs: "未连接数据源",
    noTables: "当前数据源没有表", noSchemas: "没有 schema 信息",
    tableNotFound: (n) => `表不存在：${n}`,
  },
  en: {
    helpTitle: "Available commands", cmdHead: "Command", descHead: "Description",
    unknown: (n) => `Unknown command /${n}`, hint: "Type /help to list commands",
    ds: "Datasource", type: "Type", conn: "Address", def: "Default",
    attr: "Attribute", val: "Value",
    table: "Table", cols: "Columns", rows: "Approx rows",
    col: "Column", colType: "Type", pk: "PK", nullable: "Nullable",
    usage: (u) => `Usage: /${u}`, noDs: "No datasource connected",
    noTables: "No tables in the current datasource", noSchemas: "No schema information",
    tableNotFound: (n) => `Table not found: ${n}`,
  },
};
const cmdT = (k, ...a) => {
  const v = CMD_LABELS[state.lang][k];
  return typeof v === "function" ? v(...a) : v;
};

const SLASH_COMMANDS = [
  { name: "help", aliases: ["h", "?"], usage: "help",
    desc: { zh: "显示可用命令", en: "Show available commands" } },
  { name: "datasource", aliases: ["ds"], usage: "datasource",
    desc: { zh: "查看当前数据源连接信息", en: "Show current datasource info" } },
  { name: "databases", aliases: ["dbs"], usage: "databases",
    desc: { zh: "列出所有数据源", en: "List all datasources" } },
  { name: "tables", aliases: [], usage: "tables",
    desc: { zh: "列出所有表", en: "List all tables" } },
  { name: "table_schema", aliases: ["schema"], usage: "table_schema <表名>",
    desc: { zh: "查看表结构", en: "Show table schema" } },
  { name: "schemas", aliases: [], usage: "schemas",
    desc: { zh: "列出所有 schema", en: "List all schemas" } },
  { name: "clear", aliases: [], usage: "clear",
    desc: { zh: "清空当前会话历史", en: "Clear current session history" } },
  { name: "compact", aliases: [], usage: "compact",
    desc: { zh: "压缩会话历史，节省上下文空间", en: "Compact conversation history" } },
];

/* renderMarkdown expects pre-escaped text — sanitize dynamic values
   (names/types can contain pipes, brackets, or HTML-ish chars). The
   table renderer splits cells on "|" without honoring escapes, so a
   literal pipe becomes a full-width ｜ to keep the row shape intact. */
function mdCell(v) {
  return esc(String(v ?? "")).replace(/\|/g, "｜").replace(/\n/g, " ");
}

async function fetchJson(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function fetchDatasources() {
  const data = await fetchJson("/v1/catalog/datasources");
  return data.datasources || [];
}

function connAddr(conn) {
  if (!conn) return "";
  if (conn.path) return mdCell(conn.path);
  if (conn.host) {
    const base = `${conn.host}${conn.port ? ":" + conn.port : ""}`;
    return conn.database ? `${base}/${conn.database}` : base;
  }
  return Object.entries(conn).map(([k, v]) => `${mdCell(k)}=${mdCell(v)}`).join(", ");
}

function slashHelp() {
  const l = state.lang;
  const rows = SLASH_COMMANDS.map((c) =>
    `| \`/${mdCell(c.usage)}\` | ${c.desc[l]} |`).join("\n");
  return `**${cmdT("helpTitle")}**\n\n` +
    `| ${cmdT("cmdHead")} | ${cmdT("descHead")} |\n|---|---|\n${rows}`;
}

function renderDatasourceCard(d) {
  const rows = [`| ${cmdT("type")} | ${mdCell(d.type || "—")} |`];
  for (const [k, v] of Object.entries(d.connection || {})) {
    rows.push(`| ${mdCell(k)} | ${mdCell(v)} |`);
  }
  return `**${mdCell(d.name)}**${d.default ? ` · ${cmdT("def")}` : ""}\n\n` +
    `| ${cmdT("attr")} | ${cmdT("val")} |\n|---|---|\n${rows.join("\n")}`;
}

async function slashDatasource() {
  const ds = await fetchDatasources();
  if (!ds.length) return cmdT("noDs");
  const cur = ds.find((d) => d.default) || ds[0];
  return renderDatasourceCard(cur);
}

async function slashDatabases() {
  const ds = await fetchDatasources();
  if (!ds.length) return cmdT("noDs");
  const rows = ds.map((d) =>
    `| ${mdCell(d.name)}${d.default ? " ✓" : ""} | ${mdCell(d.type || "—")} | ${connAddr(d.connection)} |`);
  return `| ${cmdT("ds")} | ${cmdT("type")} | ${cmdT("conn")} |\n|---|---|---|\n${rows.join("\n")}`;
}

async function slashTables() {
  const data = await fetchJson("/v1/catalog/tables");
  const tables = data.tables || [];
  if (!tables.length) return cmdT("noTables");
  const rows = tables.map((t) => {
    const n = t.row_count == null ? "—" : mdCell(t.row_count);
    return `| ${mdCell(t.name)} | ${mdCell(t.columns)} | ${n} |`;
  });
  return `| ${cmdT("table")} | ${cmdT("cols")} | ${cmdT("rows")} |\n|---|---|---|\n${rows.join("\n")}`;
}

async function slashSchemas() {
  const data = await fetchJson("/v1/catalog/tables");
  const schemas = [...new Set((data.tables || []).map((t) => t.schema).filter(Boolean))].sort();
  if (!schemas.length) return cmdT("noSchemas");
  return schemas.map((s) => `- \`${mdCell(s)}\``).join("\n");
}

async function slashTableSchema(arg) {
  const name = arg.trim();
  if (!name) return cmdT("usage", "table_schema <表名>");
  let data;
  try {
    data = await fetchJson(`/v1/catalog/tables/${encodeURIComponent(name)}`);
  } catch {
    return cmdT("tableNotFound", name);
  }
  const lines = [
    `**${mdCell(data.name)}**${data.schema ? ` · \`${mdCell(data.schema)}\`` : ""}`,
    `${cmdT("rows")}：${data.row_count == null ? "—" : mdCell(data.row_count)}`,
    "",
    `| ${cmdT("col")} | ${cmdT("colType")} | ${cmdT("pk")} | ${cmdT("nullable")} |`,
    "|---|---|---|---|",
  ];
  for (const c of data.columns || []) {
    lines.push(`| ${mdCell(c.name)} | ${mdCell(c.type)} | ${c.primary_key ? "✓" : ""} | ${c.nullable ? "✓" : "—"} |`);
  }
  return lines.join("\n");
}

async function slashClear() {
  if (!state.sessionId) return `**${cmdT("noSession")}**`;
  const res = await fetch(`/v1/sessions/${encodeURIComponent(state.sessionId)}/clear`, { method: "POST" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const list = $("message-list");
  if (list) list.innerHTML = "";
  setText("session-title", "");
  const note = document.createElement("div");
  note.className = "sys-note";
  note.textContent = t("sessionCleared");
  if (list) list.appendChild(note);
  updateWelcome();
  await refreshSessions();
  scrollToBottom();
}

async function slashCompact() {
  if (!state.sessionId) return `**${cmdT("noSession")}**`;
  const res = await fetch(`/v1/sessions/${encodeURIComponent(state.sessionId)}/compact`, { method: "POST" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  // Reload from the server so the UI reflects the compaction (the
  // summary + recent turns), then surface a confirmation note.
  await selectSession(state.sessionId);
  const list = $("message-list");
  const note = document.createElement("div");
  note.className = "sys-note";
  note.textContent = t("sessionCompacted", data.message_count);
  if (list) list.appendChild(note);
  updateWelcome();
  scrollToBottom();
}

async function runSlashCommand(text) {
  // Session-mutating commands handle their own UI (they replace the
  // message list), so they bypass the standard bubble/turn rendering.
  const parts = text.slice(1).split(/\s+/).filter(Boolean);
  const name = (parts[0] || "").toLowerCase();
  const cmd = SLASH_COMMANDS.find((c) => c.name === name || c.aliases.includes(name));
  if (cmd && cmd.name === "clear") {
    await slashClear();
    return;
  }
  if (cmd && cmd.name === "compact") {
    await slashCompact();
    return;
  }

  appendUserBubble(text);
  const el = document.createElement("div");
  el.className = "turn";
  el.innerHTML =
    `<div class="answer markdown"></div>` +
    `<div class="answer-toolbar">` +
    `<button type="button" class="copy-btn copy-answer-btn" title="${esc(t("copy"))}" ` +
    `aria-label="${esc(t("copy"))}">${icon("copy")}</button>` +
    `</div>`;
  $("message-list").appendChild(el);
  const answerEl = el.querySelector(".answer");

  const arg = parts.slice(1).join(" ");

  try {
    let md;
    if (!cmd) {
      md = `**${cmdT("unknown", name)}** — ${cmdT("hint")}`;
    } else {
      const handlers = {
        help: slashHelp,
        datasource: slashDatasource,
        databases: slashDatabases,
        tables: slashTables,
        schemas: slashSchemas,
        table_schema: () => slashTableSchema(arg),
      };
      md = await handlers[cmd.name]();
    }
    answerEl.innerHTML = renderMarkdown(md);
  } catch (err) {
    answerEl.innerHTML = `<div class="error-box">${esc(String(err.message || err))}</div>`;
  }
  updateWelcome();
  scrollToBottom();
}

/* ── Sessions sidebar ────────────────────────────────── */

async function refreshSessions() {
  const res = await fetch("/v1/sessions");
  if (!res.ok) return;
  const data = await res.json();
  state.sessions = data.sessions || [];
  renderSessionList();
}

function renderSessionList() {
  const nav = $("session-list");
  nav.innerHTML = "";
  if (!state.sessions.length) {
    nav.innerHTML = `<div class="empty">${esc(t("noSessions"))}</div>`;
    return;
  }
  for (const s of state.sessions) {
    const item = document.createElement("div");
    item.className = `session-item${s.session_id === state.sessionId ? " active" : ""}`;
    item.dataset.id = s.session_id;
    const title = state.titles[s.session_id] ||
      `#${String(s.session_id).slice(0, 6)}`;
    item.innerHTML =
      `<button type="button" class="session-select">` +
      `<span class="session-title">${esc(title)}</span>` +
      `<span class="session-meta">${esc(t("meta", s.message_count ?? 0))}</span></button>` +
      `<button type="button" class="delete-btn" title="${esc(t("delete"))}">${icon("trash")}</button>`;
    item.querySelector(".session-select").addEventListener("click", () => selectSession(s.session_id));
    item.querySelector(".delete-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      deleteSession(s.session_id);
    });
    nav.appendChild(item);
  }
}

async function createSession() {
  const res = await fetch("/v1/sessions", { method: "POST" });
  if (!res.ok) return null;
  const data = await res.json();
  state.sessionId = data.session_id;
  localStorage.setItem("trove_ui_session", state.sessionId);
  await refreshSessions();
  return state.sessionId;
}

async function selectSession(id) {
  abortCurrentTurn();
  closeDrawer();
  const res = await fetch(`/v1/sessions/${id}`);
  if (!res.ok) return;
  const data = await res.json();
  state.sessionId = id;
  localStorage.setItem("trove_ui_session", id);

  const list = $("message-list");
  list.innerHTML = "";
  for (const m of data.messages || []) {
    if (m.role === "user") {
      appendUserBubble(m.content);
    } else if (m.role === "assistant") {
      const el = document.createElement("div");
      el.className = "turn";
      el.innerHTML =
        `<div class="answer markdown">${renderMarkdown(m.content)}</div>` +
        `<div class="answer-toolbar">` +
        `<button type="button" class="copy-btn copy-answer-btn" title="${esc(t("copy"))}" aria-label="${esc(t("copy"))}">${icon("copy")}</button></div>`;
      list.appendChild(el);
    }
  }
  // Derived title: first user question, else short id.
  const firstUser = (data.messages || []).find((m) => m.role === "user");
  state.titles[id] = firstUser ? firstUser.content.slice(0, 40) : "";
  $("session-title").textContent = state.titles[id] || "";
  renderSessionList();
  updateWelcome();
  scrollToBottom();

  // 跨轮任务状态恢复:serve 重启后任务清单仍在(会话文件持久化)。
  try {
    const tres = await fetch(`/v1/sessions/${encodeURIComponent(id)}/tasks`);
    if (tres.ok) {
      const tdata = await tres.json();
      if ((tdata.tasks || []).length) renderTaskPanel(tdata.tasks);
    }
  } catch { /* 面板恢复失败不阻断会话加载 */ }
}

async function deleteSession(id) {
  if (!confirm(t("confirmDelete"))) return;
  abortCurrentTurn();
  const res = await fetch(`/v1/sessions/${id}`, { method: "DELETE" });
  if (!res.ok) return;
  if (state.sessionId === id) {
    state.sessionId = null;
    localStorage.removeItem("trove_ui_session");
    state.titles = {};
    $("message-list").innerHTML = "";
    $("session-title").textContent = "";
    updateWelcome();
  }
  await refreshSessions();
  if (!state.sessionId && state.sessions.length) {
    await selectSession(state.sessions[0].session_id);
  }
}

/* ── i18n application ────────────────────────────────── */

function renderSuggestionChips() {
  const wrap = $("suggestion-chips");
  if (!wrap) return;
  wrap.innerHTML = "";
  for (const q of I18N[state.lang].suggestions) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "suggestion-chip";
    b.innerHTML = `${icon("arrowUpRight")}<span>${esc(q)}</span>`;
    b.addEventListener("click", () => sendQuestion(q));
    wrap.appendChild(b);
  }
}

function applyLang() {
  document.documentElement.lang = state.lang;
  setText("app-title", "Trove");
  setTitle("app-title", t("title"));
  setHtml("new-session-btn",
    `<span class="btn-icon">${icon("plus")}</span><span class="btn-text">${esc(t("newSession"))}</span>`);
  const sectionLabel = document.querySelector(".sidebar-section-label");
  if (sectionLabel) sectionLabel.textContent = t("sessionsLabel");
  const input = $("question-input");
  if (input) input.placeholder = t("placeholder");
  setTitle("send-btn", t("send"));
  setHtml("send-btn", `<span class="btn-icon">${icon("arrowUp")}</span>`);
  setTitle("stop-btn", t("stop"));
  setHtml("stop-btn", `<span class="btn-icon">${icon("stop")}</span>`);
  setText("lang-toggle", t("langBtn"));
  setTitle("sidebar-toggle", t("collapseTitle"));
  setHtml("sidebar-toggle", `<span class="btn-icon">${icon("panelLeft")}</span>`);
  setText("welcome-title", t("welcomeTitle"));
  setText("welcome-subtitle", t("welcomeSubtitle"));
  setText("composer-hint", t("disclaimer"));
  const ds = $("datasource-label");
  if (ds && ds.dataset.name) {
    ds.textContent = `${t("datasource")}：${ds.dataset.name}`;
  }
  applyTheme();
  renderSuggestionChips();
  renderSessionList();
}

/* ── Init ────────────────────────────────────────────── */

let slashActive = -1;

/* Slash-command suggestion menu: typing "/" at the start of the
   composer shows the matching commands; the menu is display-only
   (selection fills in the command text, then Enter submits). */
function showSlashMenu() {
  const menu = $("slash-menu");
  if (menu) menu.hidden = false;
}

function hideSlashMenu() {
  const menu = $("slash-menu");
  if (menu) menu.hidden = true;
  slashActive = -1;
}

function renderSlashMenu(filter) {
  const menu = $("slash-menu");
  if (!menu) return;
  const f = filter.toLowerCase();
  const matches = SLASH_COMMANDS.filter((c) =>
    c.name.toLowerCase().startsWith(f) ||
    c.aliases.some((a) => a.toLowerCase().startsWith(f)));
  if (!matches.length) {
    menu.innerHTML = `<div class="slash-empty">${esc(t("noSlashMatch"))}</div>`;
    showSlashMenu();
    slashActive = -1;
    return;
  }
  menu.innerHTML = matches.map((c, i) =>
    `<div class="slash-item" data-i="${i}">` +
      `<span class="slash-name">/${esc(c.name)}</span>` +
      `<span class="slash-desc">${esc(c.desc[state.lang])}</span>` +
    `</div>`).join("");
  slashActive = 0;
  slashMark(menu);
  showSlashMenu();
}

function slashMark(menu) {
  (menu.querySelectorAll(".slash-item") || []).forEach((el, i) => {
    el.classList.toggle("active", i === slashActive);
  });
}

function slashCommit(input) {
  const menu = $("slash-menu");
  const el = menu && menu.querySelector(`.slash-item[data-i="${slashActive}"]`);
  if (!el) return;
  const name = el.querySelector(".slash-name").textContent.replace(/^\//, "");
  const cmd = SLASH_COMMANDS.find((c) => c.name === name);
  if (cmd) {
    input.value = `/${cmd.name} `;
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 200)}px`;
    input.focus();
  }
  hideSlashMenu();
}

function bindComposer() {
  const input = $("question-input");
  const composer = $("composer");
  if (!input || !composer) return;
  const updateSend = () => { $("send-btn").disabled = !input.value.trim(); };
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 200)}px`;
    updateSend();
    const v = input.value;
    if (v.startsWith("/") && !v.includes(" ") && !v.includes("\n")) {
      renderSlashMenu(v.slice(1));
    } else {
      hideSlashMenu();
    }
  });
  composer.addEventListener("submit", (e) => {
    e.preventDefault();
    if (!$("slash-menu").hidden) hideSlashMenu();
    sendQuestion(input.value);
    input.style.height = "auto";
  });
  input.addEventListener("keydown", (e) => {
    const menu = $("slash-menu");
    const open = menu && !menu.hidden;
    // Enter sends — except during IME composition, where Enter commits
    // the current candidate (e.isComposing). With the slash menu open,
    // Enter commits the highlighted command first, then submits.
    if (open && e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      slashCommit(input);
      composer.requestSubmit();
      return;
    }
    if (open && e.key === "Tab") {
      e.preventDefault();
      slashCommit(input);
      return;
    }
    if (open && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      e.preventDefault();
      const items = menu.querySelectorAll(".slash-item");
      if (!items.length) return;
      slashActive = e.key === "ArrowDown"
        ? (slashActive + 1) % items.length
        : (slashActive - 1 + items.length) % items.length;
      slashMark(menu);
      return;
    }
    if (open && e.key === "Escape") {
      e.preventDefault();
      hideSlashMenu();
      return;
    }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      composer.requestSubmit();
    }
  });
  // Mouse selection: delegate clicks on the menu items.
  composer.addEventListener("mousedown", (e) => {
    const item = e.target.closest(".slash-item");
    if (!item) return;
    e.preventDefault();
    slashActive = Number(item.dataset.i);
    slashCommit(input);
  });
  composer.addEventListener("blur", (e) => {
    if (e.relatedTarget && e.relatedTarget.closest && e.relatedTarget.closest("#slash-menu")) return;
    hideSlashMenu();
  }, true);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
}

function bindCopyButtons() {
  // Delegated: the markdown renderer injects .copy-btn into code headers,
  // and each answer carries a .copy-answer-btn in its toolbar. innerHTML
  // is saved/restored so the icon survives the "copied" swap.
  const list = $("message-list");
  if (!list) return;
  list.addEventListener("click", async (e) => {
    const flashCopied = (btn) => {
      const prev = btn.innerHTML;
      btn.title = t("copied");
      btn.innerHTML = icon("check");
      setTimeout(() => { btn.title = t("copy"); btn.innerHTML = prev; }, 1500);
    };
    const codeBtn = e.target.closest(".copy-btn:not(.copy-answer-btn)");
    if (codeBtn) {
      const code = codeBtn.closest(".code-block")?.querySelector("code");
      if (!code) return;
      await copyText(code.textContent);
      flashCopied(codeBtn);
      return;
    }
    const answerBtn = e.target.closest(".copy-answer-btn");
    if (answerBtn) {
      const turnEl = answerBtn.closest(".turn");
      const answer = turnEl?.querySelector(".answer");
      if (!answer || !answer.textContent.trim()) return;
      await copyText(answer.innerText);
      flashCopied(answerBtn);
    }
  });
}

async function loadDatasourceLabel() {
  try {
    const res = await fetch("/v1/catalog/datasources");
    if (!res.ok) return;
    const data = await res.json();
    const ds = (data.datasources || []).find((d) => d.default) || (data.datasources || [])[0];
    const label = $("datasource-label");
    if (ds && label) {
      label.dataset.name = ds.name;
      label.textContent = `${t("datasource")}：${ds.name}`;
      label.hidden = false;
    }
  } catch {
    /* datasource label is cosmetic */
  }
}

async function init() {
  applyTheme();
  applySidebar();
  applyLang();

  on("lang-toggle", "click", () => {
    state.lang = state.lang === "zh" ? "en" : "zh";
    localStorage.setItem("trove_ui_lang", state.lang);
    applyLang();
  });
  on("theme-toggle", "click", cycleTheme);
  // Desktop: collapse the sidebar. Mobile: overlay drawer + backdrop.
  const mobileMq = window.matchMedia("(max-width: 768px)");
  on("sidebar-toggle", "click", () => {
    if (mobileMq.matches) {
      document.body.classList.toggle("sidebar-open");
    } else {
      state.sidebarCollapsed = !state.sidebarCollapsed;
      localStorage.setItem("trove_ui_sidebar", state.sidebarCollapsed ? "1" : "0");
      applySidebar();
    }
  });
  mobileMq.addEventListener("change", (e) => { if (!e.matches) closeDrawer(); });
  const backdrop = $("backdrop");
  if (backdrop) backdrop.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeDrawer();
  });
  on("new-session-btn", "click", async () => {
    abortCurrentTurn();
    if (await createSession()) {
      closeDrawer();
      const list = $("message-list");
      if (list) list.innerHTML = "";
      setText("session-title", "");
      updateWelcome();
    }
  });
  bindComposer();
  bindCopyButtons();
  loadDatasourceLabel();
  await refreshSessions();
  if (state.sessionId) {
    await selectSession(state.sessionId);
  }
  updateWelcome();
}

document.addEventListener("DOMContentLoaded", init);
