/* Trace Console —— 只读地把 common/trace.py 落盘的 Span 树画成瀑布图。
 *
 * 三个区：左列表（GET /api/v1/traces）→ 中瀑布图 + Span 树 → 右 Span 详情。
 *
 * 两条纪律：
 * 1. **一律用 DOM API 拼装，不用 innerHTML**。轨迹 payload 里是原始用户提问和
 *    模型输出，直接当 HTML 插进去就是一个自伤的 XSS 面。
 * 2. **树要能容忍残缺**：进程被 kill 时根 span 不会落盘，父 span 也可能不在本
 *    文件里（跨 Worker 重启的重试会写成第二个文件）。多根、孤儿、未收口都要画。
 */

const REFRESH_MS = 3000;
const KINDS = ["engine", "turn", "llm", "tool", "retrieval", "plan", "compact", "request"];

const el = {
  daySelect: document.getElementById("daySelect"),
  engineSelect: document.getElementById("engineSelect"),
  statusSelect: document.getElementById("statusSelect"),
  levelSelect: document.getElementById("levelSelect"),
  autoRefresh: document.getElementById("autoRefresh"),
  searchInput: document.getElementById("searchInput"),
  traceList: document.getElementById("traceList"),
  listMeta: document.getElementById("listMeta"),
  rollup: document.getElementById("rollup"),
  waterfall: document.getElementById("waterfall"),
  emptyState: document.getElementById("emptyState"),
  spanDetail: document.getElementById("spanDetail"),
  scopeHint: document.getElementById("scopeHint"),
};

const state = {
  selectedTraceId: null,
  selectedSpanId: null,
  trace: null,
  timer: null,
  daysLoaded: false,
};

/* ---------- 小工具 ---------- */

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

function fmtMs(value) {
  if (value === null || value === undefined) return "—";
  if (value < 1000) return `${Math.round(value)}ms`;
  return `${(value / 1000).toFixed(2)}s`;
}

function fmtClock(epochSeconds) {
  if (!epochSeconds) return "—";
  const d = new Date(epochSeconds * 1000);
  return d.toLocaleTimeString("zh-CN", { hour12: false });
}

function kindClass(kind) {
  return KINDS.includes(kind) ? `kind-${kind}` : "kind-other";
}

async function fetchJson(url) {
  const response = await fetch(url);
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(`${response.status} ${detail.slice(0, 200)}`);
  }
  return response.json();
}

/* ---------- 轨迹列表 ---------- */

function listQuery() {
  const params = new URLSearchParams({ limit: "60" });
  if (el.daySelect.value) params.set("day", el.daySelect.value);
  if (el.engineSelect.value) params.set("engine", el.engineSelect.value);
  if (el.statusSelect.value) params.set("status", el.statusSelect.value);
  if (el.searchInput.value.trim()) params.set("q", el.searchInput.value.trim());
  return params.toString();
}

async function loadList() {
  let data;
  try {
    data = await fetchJson(`/api/v1/traces?${listQuery()}`);
  } catch (error) {
    el.listMeta.textContent = `列表加载失败：${error.message}`;
    return;
  }

  if (!state.daysLoaded) {
    for (const day of data.days) {
      el.daySelect.append(new Option(day, day));
    }
    el.daySelect.prepend(new Option("全部", ""));
    el.daySelect.value = "";
    state.daysLoaded = true;
  }

  el.listMeta.textContent = data.truncated
    ? `${data.total} 条（已达扫描上限，缩小日期范围可看到更早的）`
    : `${data.total} 条`;
  renderList(data.traces);
}

function renderList(traces) {
  el.traceList.replaceChildren();
  if (!traces.length) {
    el.traceList.append(node("p", "empty-hint", "没有匹配的轨迹。"));
    return;
  }
  for (const item of traces) {
    const row = node("button", "trace-row");
    row.type = "button";
    if (item.trace_id === state.selectedTraceId) row.classList.add("is-selected");

    const statusName = item.in_flight ? "running" : item.status;
    row.append(node("span", `status-dot status-${statusName}`));

    const main = node("div", "trace-row-main");
    main.append(node("div", "trace-row-id", item.trace_id));
    const meta = node("div", "trace-row-meta");
    meta.append(node("span", "", fmtClock(item.started_at)));
    meta.append(node("span", "", item.engine || item.process || "—"));
    meta.append(node("span", "", `${item.span_count} spans`));
    meta.append(node("span", "", item.in_flight ? "进行中" : fmtMs(item.duration_ms)));
    main.append(meta);
    row.append(main);

    if (item.attempts > 1) row.append(node("span", "badge", `×${item.attempts}`));
    if (item.error_count) row.append(node("span", "badge badge-danger", String(item.error_count)));

    row.addEventListener("click", () => selectTrace(item.trace_id));
    el.traceList.append(row);
  }
}

/* ---------- 轨迹详情 ---------- */

async function selectTrace(traceId) {
  state.selectedTraceId = traceId;
  state.selectedSpanId = null;
  el.spanDetail.replaceChildren(
    node("p", "empty-hint", "点击左侧时间条查看单个 Span 的属性、输入输出与事件。"),
  );
  const url = new URL(window.location.href);
  url.searchParams.set("trace_id", traceId);
  window.history.replaceState(null, "", url);
  await loadTrace();
  await loadList();   // 重画列表以更新选中态
}

async function loadTrace() {
  if (!state.selectedTraceId) return;
  try {
    state.trace = await fetchJson(
      `/api/v1/traces/${encodeURIComponent(state.selectedTraceId)}` +
      `?level=${encodeURIComponent(el.levelSelect.value)}`,
    );
  } catch (error) {
    el.emptyState.hidden = false;
    el.waterfall.hidden = true;
    el.rollup.hidden = true;
    el.emptyState.replaceChildren(node("p", "", `轨迹加载失败：${error.message}`));
    return;
  }
  renderTrace(state.trace);
}

function renderTrace(trace) {
  el.emptyState.hidden = true;
  el.waterfall.hidden = false;
  renderRollup(trace);
  renderWaterfall(trace);
  if (state.selectedSpanId) {
    const span = trace.spans.find((s) => s.span_id === state.selectedSpanId);
    if (span) renderSpanDetail(span, timeWindow(trace.spans).t0);
  }
}

function statCell(label, value) {
  const cell = node("div", "stat");
  cell.append(node("span", "stat-label", label));
  cell.append(node("span", "stat-value", value));
  return cell;
}

function renderRollup(trace) {
  const spans = trace.spans;
  const { t0, t1 } = timeWindow(spans);
  const engineSpans = spans.filter((s) => s.kind === "engine");
  const root = engineSpans[engineSpans.length - 1];
  const attributes = (root && root.attributes) || {};
  const llms = spans.filter((s) => s.kind === "llm");
  const tokens = llms.reduce((sum, s) => sum + (Number((s.attributes || {}).total_tokens) || 0), 0);
  const errors = spans.filter((s) => s.status === "error").length;

  el.rollup.replaceChildren();
  el.rollup.hidden = false;

  const head = node("div", "rollup-head");
  head.append(node("h2", "", trace.trace_id));
  const tags = node("div", "rollup-tags");
  if (attributes.engine) tags.append(node("span", "tag", attributes.engine));
  if (attributes.run_id) tags.append(node("span", "tag tag-mono", attributes.run_id));
  if (engineSpans.length > 1) tags.append(node("span", "tag tag-warn", `${engineSpans.length} 次 attempt`));
  if (!engineSpans.length) tags.append(node("span", "tag tag-warn", "无根 span · 进行中或进程中断"));
  tags.append(node("span", "tag", `level ${trace.level}`));
  head.append(tags);
  el.rollup.append(head);

  const stats = node("div", "rollup-stats");
  stats.append(statCell("总时长", fmtMs((t1 - t0) * 1000)));
  stats.append(statCell("Span", String(spans.length)));
  stats.append(statCell("模型调用", String(llms.length)));
  stats.append(statCell("Token", tokens ? String(tokens) : "—"));
  stats.append(statCell("工具", String(spans.filter((s) => s.kind === "tool").length)));
  stats.append(statCell("首字", fmtMs(attributes.ttft_ms)));
  stats.append(statCell("结束原因", attributes.finish_reason || "—"));
  stats.append(statCell("错误", errors ? String(errors) : "0"));
  el.rollup.append(stats);

  if (trace.trace_files && trace.trace_files.length) {
    const files = node("div", "rollup-files");
    files.append(node("span", "stat-label", "落盘"));
    for (const path of trace.trace_files) files.append(node("code", "", path));
    el.rollup.append(files);
  }
}

function timeWindow(spans) {
  const starts = spans.map((s) => s.start_ts).filter((v) => typeof v === "number");
  const ends = spans.map((s) => s.end_ts).filter((v) => typeof v === "number");
  const t0 = starts.length ? Math.min(...starts) : 0;
  const t1 = ends.length ? Math.max(...ends, t0) : t0;
  return { t0, t1: t1 > t0 ? t1 : t0 + 0.001 };
}

/** 按 parent_span_id 建树。容忍多根、孤儿（父不在本轨迹里）与环。 */
function buildTree(spans) {
  const byId = new Map(spans.map((s) => [s.span_id, s]));
  const children = new Map();
  const roots = [];
  for (const span of spans) {
    const parent = span.parent_span_id;
    if (parent && byId.has(parent) && parent !== span.span_id) {
      if (!children.has(parent)) children.set(parent, []);
      children.get(parent).push(span);
    } else {
      roots.push(span);
    }
  }
  const byStart = (a, b) => (a.start_ts || 0) - (b.start_ts || 0);
  const ordered = [];
  const seen = new Set();
  const walk = (span, depth) => {
    if (seen.has(span.span_id)) return;
    seen.add(span.span_id);
    ordered.push({ span, depth });
    (children.get(span.span_id) || []).sort(byStart).forEach((kid) => walk(kid, depth + 1));
  };
  roots.sort(byStart).forEach((root) => walk(root, 0));
  // 环导致的漏网之鱼兜底挂到顶层，绝不静默丢 span。
  for (const span of spans) if (!seen.has(span.span_id)) ordered.push({ span, depth: 0 });
  return ordered;
}

function renderWaterfall(trace) {
  const { t0, t1 } = timeWindow(trace.spans);
  const total = t1 - t0;
  el.waterfall.replaceChildren();

  for (const { span, depth } of buildTree(trace.spans)) {
    const row = node("div", "span-row");
    row.dataset.spanId = span.span_id;
    if (span.span_id === state.selectedSpanId) row.classList.add("is-selected");

    const label = node("div", "span-label");
    label.style.paddingLeft = `${depth * 14}px`;
    label.append(node("span", `kind-dot ${kindClass(span.kind)}`));
    label.append(node("span", "span-name", span.name));
    row.append(label);

    const track = node("div", "span-track");
    const open = typeof span.end_ts !== "number";
    const start = ((span.start_ts - t0) / total) * 100;
    const width = open
      ? Math.max(100 - start, 1)
      : Math.max(((span.end_ts - span.start_ts) / total) * 100, 0.6);
    const bar = node("div", `span-bar ${kindClass(span.kind)}`);
    bar.style.left = `${Math.max(0, Math.min(start, 99.4))}%`;
    bar.style.width = `${width}%`;
    if (span.status === "error") bar.classList.add("is-error");
    if (span.status === "cancelled") bar.classList.add("is-cancelled");
    if (open) bar.classList.add("is-open");
    bar.title = `${span.name} · ${open ? "未收口" : fmtMs(span.duration_ms)}`;
    track.append(bar);
    row.append(track);

    row.append(node("div", "span-duration", open ? "未收口" : fmtMs(span.duration_ms)));
    row.addEventListener("click", () => {
      state.selectedSpanId = span.span_id;
      el.waterfall.querySelectorAll(".span-row.is-selected")
        .forEach((n) => n.classList.remove("is-selected"));
      row.classList.add("is-selected");
      renderSpanDetail(span, t0);
    });
    el.waterfall.append(row);
  }
}

/* ---------- Span 详情 ---------- */

function isPlaceholder(text) {
  return /^\[(image|binary) /.test(text);
}

function renderValue(value) {
  if (typeof value === "string") {
    if (isPlaceholder(value)) return node("span", "chip", value);
    const box = node("pre", "value-block", value);
    if (value.includes("…[truncated,")) box.classList.add("is-truncated");
    return box;
  }
  return node("pre", "value-block", JSON.stringify(value, null, 2));
}

function renderSpanDetail(span, t0) {
  const panel = el.spanDetail;
  panel.replaceChildren();

  const head = node("div", "detail-head");
  head.append(node("span", `kind-dot ${kindClass(span.kind)}`));
  head.append(node("h3", "", span.name));
  panel.append(head);

  const meta = node("div", "detail-meta");
  meta.append(node("span", `pill pill-${span.status}`, span.status));
  meta.append(node("span", "", span.kind));
  meta.append(node("span", "", typeof span.end_ts === "number"
    ? fmtMs(span.duration_ms) : "未收口"));
  meta.append(node("span", "", `+${fmtMs((span.start_ts - t0) * 1000)}`));
  panel.append(meta);

  const attributes = span.attributes || {};
  if (Object.keys(attributes).length) {
    panel.append(node("h4", "detail-title", "属性"));
    const table = node("dl", "attr-list");
    for (const key of Object.keys(attributes).sort()) {
      table.append(node("dt", "", key));
      const value = attributes[key];
      table.append(node("dd", "", typeof value === "object"
        ? JSON.stringify(value) : String(value)));
    }
    panel.append(table);
  }

  const payloads = span.payloads || {};
  for (const key of Object.keys(payloads)) {
    const value = payloads[key];
    const box = node("details", "payload");
    const summary = node("summary", "", key);
    // summary 级别落盘的 payload 形状是 {chars, sha1, head}，直接把体量标出来，
    // 免得用户以为内容丢了。
    if (value && typeof value === "object" && typeof value.chars === "number") {
      summary.append(node("span", "payload-size", `${value.chars} 字符 · ${value.sha1}`));
      box.append(summary);
      box.append(renderValue(value.head || ""));
    } else {
      const text = typeof value === "string" ? value : JSON.stringify(value);
      summary.append(node("span", "payload-size", `${text.length} 字符`));
      box.append(summary);
      box.append(renderValue(value));
    }
    panel.append(box);
  }

  const events = span.events || [];
  if (events.length) {
    panel.append(node("h4", "detail-title", `事件 (${events.length})`));
    const list = node("ol", "event-list");
    for (const item of events) {
      const entry = node("li", "event-item");
      const head2 = node("div", "event-head");
      head2.append(node("span", "event-name", item.name));
      head2.append(node("span", "event-offset", `+${fmtMs((item.ts - t0) * 1000)}`));
      entry.append(head2);
      const fields = { ...item };
      delete fields.name;
      delete fields.ts;
      if (Object.keys(fields).length) entry.append(renderValue(fields));
      list.append(entry);
    }
    panel.append(list);
  }
  if (span.events_dropped) {
    panel.append(node("p", "warn-note",
      `另有 ${span.events_dropped} 条事件因超出单 span 上限被丢弃。`));
  }
}

/* ---------- 轮询与启动 ---------- */

async function refresh() {
  await loadList();
  if (state.selectedTraceId) await loadTrace();
}

function scheduleRefresh() {
  if (state.timer) clearInterval(state.timer);
  state.timer = null;
  if (!el.autoRefresh.checked) return;
  state.timer = setInterval(() => {
    // 后台标签页不必轮询，省得开着控制台过夜把磁盘读穿。
    if (document.visibilityState === "visible") refresh();
  }, REFRESH_MS);
}

function bindEvents() {
  el.daySelect.addEventListener("change", loadList);
  el.engineSelect.addEventListener("change", loadList);
  el.statusSelect.addEventListener("change", loadList);
  el.levelSelect.addEventListener("change", loadTrace);
  el.autoRefresh.addEventListener("change", scheduleRefresh);
  let searchTimer = null;
  el.searchInput.addEventListener("input", () => {
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(loadList, 200);
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && el.autoRefresh.checked) refresh();
  });
}

async function init() {
  bindEvents();
  const requested = new URLSearchParams(window.location.search).get("trace_id");
  await loadList();
  if (requested) {
    state.selectedTraceId = requested;
    await loadTrace();
    await loadList();
  }
  scheduleRefresh();
}

init();
