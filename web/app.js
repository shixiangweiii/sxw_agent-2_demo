const PDFJS_URL = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.10.38/pdf.min.mjs";
const PDFJS_WORKER_URL = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.10.38/pdf.worker.min.mjs";
const MAMMOTH_URL = "https://unpkg.com/mammoth@1.8.0/mammoth.browser.min.js";
const MAX_DOCUMENTS = 5;
const MAX_TEXT_CHARS = 200000;

const el = {
  healthStatus: document.querySelector("#healthStatus"),
  agentUuid: document.querySelector("#agentUuid"),
  engineSelect: document.querySelector("#engineSelect"),
  userId: document.querySelector("#userId"),
  conversationId: document.querySelector("#conversationId"),
  newSessionBtn: document.querySelector("#newSessionBtn"),
  documentInput: document.querySelector("#documentInput"),
  imageInput: document.querySelector("#imageInput"),
  attachmentList: document.querySelector("#attachmentList"),
  messages: document.querySelector("#messages"),
  chatForm: document.querySelector("#chatForm"),
  queryInput: document.querySelector("#queryInput"),
  composerStatus: document.querySelector("#composerStatus"),
  sendBtn: document.querySelector("#sendBtn"),
  stopBtn: document.querySelector("#stopBtn"),
  cancelBtn: document.querySelector("#cancelBtn"),
};

const state = {
  documents: [],
  imageFile: null,
  imagePreviewUrl: "",
  runId: localStorage.getItem("sxw.run_id") || "",
  traceId: "",
  lastSeq: Number(localStorage.getItem("sxw.last_seq") || "0"),
  watchController: null,
  watching: false,
  terminal: false,
  submitting: false,
};

let pdfjsPromise = null;
let mammothPromise = null;

function uuid() {
  return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
}

function setStatus(text, kind = "") {
  el.composerStatus.textContent = text;
  el.composerStatus.className = `composer-status ${kind}`.trim();
}

function refreshControls() {
  el.sendBtn.disabled = state.submitting || (state.runId && !state.terminal);
  el.stopBtn.disabled = !state.watching;
  el.cancelBtn.disabled = !state.runId || state.terminal;
  el.queryInput.disabled = state.submitting;
  el.documentInput.disabled = state.submitting;
  el.imageInput.disabled = state.submitting;
}

function scrollToBottom() {
  el.messages.scrollTop = el.messages.scrollHeight;
}

function appendMessage(role, content = "", extraClass = "") {
  const node = document.createElement("article");
  node.className = `message ${role} ${extraClass}`.trim();
  const label = document.createElement("div");
  label.className = "message-role";
  label.textContent = role === "user" ? "You" : role === "assistant" ? "Agent" : "System";
  const body = document.createElement("div");
  body.className = "message-content";
  body.textContent = content;
  node.append(label, body);
  el.messages.append(node);
  scrollToBottom();
  return { node, body };
}

function addProcessItem(messageNode, label, payload) {
  let panel = messageNode.querySelector(".process-panel");
  if (!panel) {
    panel = document.createElement("details");
    panel.className = "process-panel";
    const summary = document.createElement("summary");
    summary.textContent = "过程";
    const list = document.createElement("div");
    list.className = "process-list";
    panel.append(summary, list);
    messageNode.append(panel);
  }
  const item = document.createElement("div");
  item.className = "process-item";
  const name = document.createElement("div");
  name.className = "process-label";
  name.textContent = label;
  const pre = document.createElement("pre");
  pre.textContent = typeof payload === "string" ? payload : JSON.stringify(payload, null, 2);
  item.append(name, pre);
  panel.querySelector(".process-list").append(item);
  scrollToBottom();
}

function addTraceLink(messageNode, traceId) {
  if (!traceId || messageNode.querySelector(".trace-link")) return;
  const link = document.createElement("a");
  link.className = "trace-link";
  link.href = `/trace-ui/?trace_id=${encodeURIComponent(traceId)}`;
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = "查看轨迹";
  link.title = `诊断轨迹 ${traceId}`;
  messageNode.append(link);
}

function addCitations(messageNode, refs) {
  if (!Array.isArray(refs) || !refs.length) return;
  const section = document.createElement("section");
  section.className = "citations";
  const title = document.createElement("h3");
  title.textContent = "引用";
  const list = document.createElement("ul");
  list.className = "citation-list";
  refs.forEach((ref, index) => {
    const item = document.createElement("li");
    item.className = "citation-item";
    item.textContent = `[${ref.n || index + 1}] ${ref.title || ref.doc_id || ref.evidence_id || "document"}`;
    list.append(item);
  });
  section.append(title, list);
  messageNode.append(section);
}

function renderAttachments() {
  el.attachmentList.replaceChildren();
  for (const doc of state.documents) {
    const item = document.createElement("div");
    item.className = "attachment-item";
    const name = document.createElement("div");
    name.className = "attachment-name";
    name.textContent = doc.file.name;
    const meta = document.createElement("div");
    meta.className = "attachment-meta";
    meta.textContent = doc.status || `${Math.ceil(doc.file.size / 1024)} KB`;
    item.append(name, meta);
    el.attachmentList.append(item);
  }
  if (state.imageFile) {
    const item = document.createElement("div");
    item.className = "attachment-item";
    const img = document.createElement("img");
    img.className = "image-preview";
    img.alt = state.imageFile.name;
    img.src = state.imagePreviewUrl;
    const name = document.createElement("div");
    name.className = "attachment-name";
    name.textContent = state.imageFile.name;
    item.append(img, name);
    el.attachmentList.append(item);
  }
}

function updateDocumentStatus(fileName, status) {
  const doc = state.documents.find((entry) => entry.file.name === fileName);
  if (doc) doc.status = status;
  renderAttachments();
}

function inferFileType(file) {
  const name = file.name.toLowerCase();
  if (file.type) return file.type;
  if (name.endsWith(".md") || name.endsWith(".markdown")) return "text/markdown";
  if (name.endsWith(".pdf")) return "application/pdf";
  if (name.endsWith(".docx")) return "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
  return "text/plain";
}

async function loadPdfjs() {
  if (!pdfjsPromise) {
    pdfjsPromise = import(PDFJS_URL).then((pdfjs) => {
      pdfjs.GlobalWorkerOptions.workerSrc = PDFJS_WORKER_URL;
      return pdfjs;
    });
  }
  return pdfjsPromise;
}

async function loadMammoth() {
  if (window.mammoth) return window.mammoth;
  if (!mammothPromise) {
    mammothPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = MAMMOTH_URL;
      script.onload = () => resolve(window.mammoth);
      script.onerror = () => reject(new Error("failed to load mammoth"));
      document.head.append(script);
    });
  }
  return mammothPromise;
}

async function extractDocumentText(file) {
  const type = inferFileType(file);
  let text = "";
  if (type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf")) {
    const pdfjs = await loadPdfjs();
    const pdf = await pdfjs.getDocument({ data: new Uint8Array(await file.arrayBuffer()) }).promise;
    const pages = [];
    for (let pageNo = 1; pageNo <= pdf.numPages; pageNo += 1) {
      const page = await pdf.getPage(pageNo);
      const content = await page.getTextContent();
      pages.push(content.items.map((item) => item.str || "").join(" "));
    }
    text = pages.join("\n\n");
  } else if (type.includes("wordprocessingml")) {
    const mammoth = await loadMammoth();
    text = (await mammoth.extractRawText({ arrayBuffer: await file.arrayBuffer() })).value || "";
  } else {
    text = await file.text();
  }
  text = text.trim();
  if (!text) throw new Error("未提取到文本");
  if (text.length > MAX_TEXT_CHARS) throw new Error(`文本超过 ${MAX_TEXT_CHARS} 字符`);
  return text;
}

async function waitForIndexJob(jobId) {
  for (let count = 0; count < 480; count += 1) {
    const response = await fetch(`/api/v1/documents/index/jobs/${encodeURIComponent(jobId)}`);
    if (!response.ok) throw new Error("索引任务查询失败");
    const job = await response.json();
    if (job.state === "ACTIVATED") return job;
    if (job.state === "FAILED") throw new Error(job.error?.message || "文档入库失败");
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error("文档入库超时");
}

async function indexSelectedDocuments() {
  if (!state.documents.length) return [];
  const documents = [];
  for (const entry of state.documents) {
    updateDocumentStatus(entry.file.name, "解析中...");
    documents.push({
      // A filename is the demo's stable logical document identity. Re-uploading an edited
      // file creates a new immutable version and atomically replaces the active pointer.
      doc_id: `web:${entry.file.name}`,
      dataset_id: "default",
      title: entry.file.name,
      content: await extractDocumentText(entry.file),
      metadata: { source: "web-ui", file_name: entry.file.name, file_type: inferFileType(entry.file) },
    });
    updateDocumentStatus(entry.file.name, "等待入库");
  }
  const response = await fetch("/api/v1/documents/index", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ documents }),
  });
  if (!response.ok) throw new Error("文档入库受理失败");
  if (response.status !== 202) throw new Error(`文档入库受理必须返回 HTTP 202，实际为 ${response.status}`);
  const accepted = await response.json();
  if (!Array.isArray(accepted.job_ids) || accepted.job_ids.length !== documents.length) {
    throw new Error("文档入库受理响应缺少完整 job_ids");
  }
  const jobs = await Promise.all(accepted.job_ids.map(waitForIndexJob));
  state.documents.forEach((entry) => updateDocumentStatus(entry.file.name, "已激活"));
  return jobs;
}

async function uploadImageArtifact() {
  if (!state.imageFile) return [];
  const form = new FormData();
  form.append("file", state.imageFile, state.imageFile.name);
  const response = await fetch("/api/v1/artifacts", { method: "POST", body: form });
  if (!response.ok) throw new Error("图片 Artifact 上传失败");
  return [(await response.json()).artifact_id];
}

function parseSseBlock(block) {
  const event = { type: "message", id: null, data: "" };
  const data = [];
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith("id:")) event.id = Number(line.slice(3).trim());
    else if (line.startsWith("event:")) event.type = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  event.data = data.join("\n");
  return event;
}

function handleSseEvent(event, assistant) {
  if (!event.data) return;
  let envelope;
  try { envelope = JSON.parse(event.data); } catch { return; }
  const payload = envelope.payload || {};
  if (event.id) {
    state.lastSeq = Math.max(state.lastSeq, event.id);
    localStorage.setItem("sxw.last_seq", String(state.lastSeq));
  }
  if (event.type === "text_start") {
    // A retry/recovery is a new generation of the same semantic message.
    // Reset only the answer body; tool/Skill/plan process cards remain intact.
    assistant.body.textContent = "";
    scrollToBottom();
  } else if (event.type === "text") {
    assistant.body.textContent += payload.delta || "";
    scrollToBottom();
  } else if (event.type === "assistant_message") {
    // The committed semantic message is authoritative.  In particular, a
    // fresh page may be rebuilding its UI from events after all streaming
    // deltas were already consumed by a previous page instance.
    assistant.body.textContent = payload.text || "";
    scrollToBottom();
  } else if (event.type === "citation") {
    addCitations(assistant.node, payload.citations || payload.refs || []);
  } else if (["tool_call", "tool_result", "plan_step", "skill_event", "run_status", "activity_status"].includes(event.type)) {
    addProcessItem(assistant.node, event.type, payload);
  } else if (event.type === "terminal") {
    state.terminal = true;
    state.watching = false;
    setStatus(`运行结束：${envelope.terminal_status}`, envelope.terminal_status === "SUCCEEDED" ? "ok" : "bad");
    // 轨迹的根 span 在引擎收口时才落盘，所以链接等到终态再挂。
    addTraceLink(assistant.node, state.traceId);
    refreshControls();
  }
}

async function consumeSse(response, assistant) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      handleSseEvent(parseSseBlock(buffer.slice(0, boundary)), assistant);
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");
    }
  }
}

async function watchRun(assistant) {
  state.watching = true;
  refreshControls();
  while (state.watching && !state.terminal) {
    state.watchController = new AbortController();
    try {
      const response = await fetch(
        `/api/v1/runs/${encodeURIComponent(state.runId)}/events?after_seq=${state.lastSeq}`,
        { signal: state.watchController.signal },
      );
      if (!response.ok || !response.body) throw new Error(`SSE 订阅失败：${response.status}`);
      await consumeSse(response, assistant);
      if (!state.terminal && state.watching) await new Promise((resolve) => setTimeout(resolve, 500));
    } catch (error) {
      if (error.name === "AbortError") break;
      setStatus("订阅断开，正在按 cursor 重连...");
      await new Promise((resolve) => setTimeout(resolve, 750));
    }
  }
  state.watchController = null;
  refreshControls();
}

async function createRun(query, attachmentRefs) {
  const response = await fetch("/api/v1/runs", {
    method: "POST",
    headers: { "content-type": "application/json", "Idempotency-Key": uuid() },
    body: JSON.stringify({
      client_request_id: uuid(),
      conversation_id: el.conversationId.value.trim() || null,
      principal_id: el.userId.value.trim() || "web-user",
      agent_id: el.agentUuid.value.trim() || "demo-agent",
      engine: el.engineSelect.value,
      input: { text: query || "请描述上传的附件。", attachment_refs: attachmentRefs },
    }),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error?.message || `Run 创建失败：${response.status}`);
  state.runId = body.run_id;
  // 服务端把本次请求的诊断轨迹键回显在响应头上（没带 x-trace-id 时由中间件生成）。
  // 它随 Run 落库，Worker 执行时接回来，因此可以直接拿去 Trace Console 查。
  state.traceId = response.headers.get("x-trace-id") || "";
  state.lastSeq = 0;
  state.terminal = false;
  el.conversationId.value = body.conversation_id;
  localStorage.setItem("sxw.run_id", state.runId);
  localStorage.setItem("sxw.last_seq", "0");
  localStorage.setItem("sxw.conversation_id", body.conversation_id);
  return body;
}

function clearAttachments() {
  state.documents = [];
  state.imageFile = null;
  if (state.imagePreviewUrl) URL.revokeObjectURL(state.imagePreviewUrl);
  state.imagePreviewUrl = "";
  el.documentInput.value = "";
  el.imageInput.value = "";
  renderAttachments();
}

async function handleSubmit(event) {
  event.preventDefault();
  const query = el.queryInput.value.trim();
  if (!query && !state.documents.length && !state.imageFile) {
    setStatus("请输入内容或选择附件", "bad");
    return;
  }
  appendMessage("user", [query, state.imageFile ? `图片：${state.imageFile.name}` : ""].filter(Boolean).join("\n"));
  state.submitting = true;
  refreshControls();
  try {
    const jobs = await indexSelectedDocuments();
    if (jobs.length) appendMessage("system", `${jobs.length} 个文档版本已 ACTIVATED。`);
    if (!query && !state.imageFile) {
      setStatus("文档入库完成", "ok");
      return;
    }
    const refs = await uploadImageArtifact();
    const created = await createRun(query, refs);
    const assistant = appendMessage("assistant", "");
    addProcessItem(assistant.node, "run_accepted", created);
    clearAttachments();
    el.queryInput.value = "";
    setStatus(`Run ${created.run_id} 执行中...`);
    await watchRun(assistant);
  } catch (error) {
    appendMessage("error", error.message || String(error), "error");
    setStatus(error.message || "请求失败", "bad");
  } finally {
    state.submitting = false;
    refreshControls();
  }
}

async function cancelRun() {
  if (!state.runId || state.terminal) return;
  const response = await fetch(`/api/v1/runs/${encodeURIComponent(state.runId)}/cancel`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ command_id: uuid(), reason: "cancelled from Web UI" }),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    setStatus(body.error?.message || "取消失败", "bad");
    return;
  }
  setStatus("取消指令已提交，等待安全边界...");
}

async function resumeStoredRun() {
  if (!state.runId) return;
  try {
    const response = await fetch(`/api/v1/runs/${encodeURIComponent(state.runId)}`);
    if (!response.ok) return;
    const run = await response.json();
    el.conversationId.value = run.conversation_id;
    // 刷新页面后响应头没了，改从 Run 上取持久化的诊断关联键。
    state.traceId = run.trace_id || "";
    // localStorage owns only the transport cursor, not a durable rendering of
    // the answer/process DOM.  On a fresh page the old cursor therefore cannot
    // be used as a projection cursor: replay every committed public event to
    // rebuild the UI, even when the Run already reached terminal while closed.
    const previousCursor = state.lastSeq;
    state.lastSeq = 0;
    state.terminal = false;
    localStorage.setItem("sxw.last_seq", "0");
    const assistant = appendMessage("assistant", "");
    addProcessItem(assistant.node, "resume", {
      run_id: state.runId,
      after_seq: 0,
      previous_transport_cursor: previousCursor,
    });
    setStatus("从 committed events 重建上次 Run...");
    await watchRun(assistant);
  } catch { /* a stale local cursor is harmless */ }
}

async function loadHealth() {
  try {
    const response = await fetch("/healthz");
    const health = await response.json();
    el.healthStatus.textContent = `${health.service} · ${Object.keys(health.active_releases || {}).length}/3 releases`;
  } catch { el.healthStatus.textContent = "未连接"; }
}

function bindEvents() {
  el.chatForm.addEventListener("submit", handleSubmit);
  el.stopBtn.addEventListener("click", () => {
    state.watching = false;
    if (state.watchController) state.watchController.abort();
    setStatus("已停止观看；Run 仍在 Worker 中执行。");
    refreshControls();
  });
  el.cancelBtn.addEventListener("click", cancelRun);
  el.newSessionBtn.addEventListener("click", () => {
    state.watching = false;
    if (state.watchController) state.watchController.abort();
    state.runId = "";
    state.lastSeq = 0;
    state.terminal = false;
    el.conversationId.value = "";
    localStorage.removeItem("sxw.run_id");
    localStorage.removeItem("sxw.last_seq");
    localStorage.removeItem("sxw.conversation_id");
    appendMessage("system", "将在下次发送时创建新 Conversation。");
    refreshControls();
  });
  el.documentInput.addEventListener("change", () => {
    state.documents = Array.from(el.documentInput.files || []).slice(0, MAX_DOCUMENTS)
      .map((file) => ({ file, status: `${Math.ceil(file.size / 1024)} KB` }));
    renderAttachments();
  });
  el.imageInput.addEventListener("change", () => {
    if (state.imagePreviewUrl) URL.revokeObjectURL(state.imagePreviewUrl);
    state.imageFile = (el.imageInput.files || [])[0] || null;
    state.imagePreviewUrl = state.imageFile ? URL.createObjectURL(state.imageFile) : "";
    renderAttachments();
  });
}

function init() {
  el.conversationId.value = localStorage.getItem("sxw.conversation_id") || "";
  bindEvents();
  refreshControls();
  loadHealth();
  appendMessage("system", "已就绪。停止观看不会取消 Run。");
  resumeStoredRun();
}

init();
