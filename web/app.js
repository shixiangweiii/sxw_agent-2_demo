const PDFJS_URL = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.10.38/pdf.min.mjs";
const PDFJS_WORKER_URL = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.10.38/pdf.worker.min.mjs";
const MAMMOTH_URL = "https://unpkg.com/mammoth@1.8.0/mammoth.browser.min.js";
const MAX_DOCUMENTS = 5;
const MAX_TEXT_CHARS = 200000;

const el = {
  healthStatus: document.querySelector("#healthStatus"),
  agentUuid: document.querySelector("#agentUuid"),
  userId: document.querySelector("#userId"),
  sessionId: document.querySelector("#sessionId"),
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
};

const state = {
  documents: [],
  imageFile: null,
  imagePreviewUrl: "",
  abortController: null,
};

let pdfjsPromise = null;
let mammothPromise = null;

function newSessionId() {
  return `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function setStatus(text, kind = "") {
  el.composerStatus.textContent = text;
  el.composerStatus.className = `composer-status ${kind}`.trim();
}

function setBusy(isBusy) {
  el.sendBtn.disabled = isBusy;
  el.stopBtn.disabled = !isBusy;
  el.queryInput.disabled = isBusy;
  el.documentInput.disabled = isBusy;
  el.imageInput.disabled = isBusy;
}

function scrollToBottom() {
  el.messages.scrollTop = el.messages.scrollHeight;
}

function escapeText(value) {
  return String(value ?? "");
}

function shortCallId(value) {
  const callId = String(value ?? "");
  if (!callId) {
    return "";
  }
  return callId.length <= 8 ? callId : callId.slice(-8);
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

function addProcessPanel(messageNode) {
  let panel = messageNode.querySelector(".process-panel");
  if (panel) {
    return panel.querySelector(".process-list");
  }
  panel = document.createElement("details");
  panel.className = "process-panel";

  const summary = document.createElement("summary");
  summary.textContent = "过程";
  const list = document.createElement("div");
  list.className = "process-list";

  panel.append(summary, list);
  messageNode.append(panel);
  return list;
}

function addProcessItem(messageNode, label, payload) {
  const list = addProcessPanel(messageNode);
  const item = document.createElement("div");
  item.className = "process-item";

  const name = document.createElement("div");
  name.className = "process-label";
  name.textContent = label;
  item.append(name);

  const pre = document.createElement("pre");
  pre.textContent = typeof payload === "string" ? payload : JSON.stringify(payload, null, 2);
  item.append(pre);

  list.append(item);
  scrollToBottom();
}

function addCitations(messageNode, refs) {
  if (!Array.isArray(refs) || refs.length === 0) {
    return;
  }
  const section = document.createElement("section");
  section.className = "citations";

  const title = document.createElement("h3");
  title.textContent = "引用";
  const list = document.createElement("ul");
  list.className = "citation-list";

  for (const ref of refs) {
    const item = document.createElement("li");
    item.className = "citation-item";
    item.textContent = `[${ref.n}] ${ref.title || ref.doc_id || "document"}`;
    list.append(item);
  }

  section.append(title, list);
  messageNode.append(section);
  scrollToBottom();
}

function renderAttachments() {
  el.attachmentList.replaceChildren();

  for (const doc of state.documents) {
    const item = document.createElement("div");
    item.className = "attachment-item";
    item.dataset.name = doc.file.name;

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
  if (doc) {
    doc.status = status;
  }
  renderAttachments();
}

function inferFileType(file) {
  const name = file.name.toLowerCase();
  if (file.type) {
    return file.type;
  }
  if (name.endsWith(".md") || name.endsWith(".markdown")) {
    return "text/markdown";
  }
  if (name.endsWith(".pdf")) {
    return "application/pdf";
  }
  if (name.endsWith(".docx")) {
    return "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
  }
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
  if (window.mammoth) {
    return window.mammoth;
  }
  if (!mammothPromise) {
    mammothPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = MAMMOTH_URL;
      script.async = true;
      script.onload = () => resolve(window.mammoth);
      script.onerror = () => reject(new Error("failed to load mammoth"));
      document.head.append(script);
    });
  }
  return mammothPromise;
}

async function parsePdf(file) {
  const pdfjs = await loadPdfjs();
  const data = new Uint8Array(await file.arrayBuffer());
  const pdf = await pdfjs.getDocument({ data }).promise;
  const pages = [];
  for (let pageNo = 1; pageNo <= pdf.numPages; pageNo += 1) {
    const page = await pdf.getPage(pageNo);
    const content = await page.getTextContent();
    const text = content.items.map((item) => item.str || "").join(" ").trim();
    if (text) {
      pages.push(text);
    }
  }
  return pages.join("\n\n");
}

async function parseDocx(file) {
  const mammoth = await loadMammoth();
  const result = await mammoth.extractRawText({ arrayBuffer: await file.arrayBuffer() });
  return result.value || "";
}

async function extractDocumentText(file) {
  const type = inferFileType(file);
  const name = file.name.toLowerCase();
  let text = "";

  if (type === "application/pdf" || name.endsWith(".pdf")) {
    text = await parsePdf(file);
  } else if (type.includes("wordprocessingml") || name.endsWith(".docx")) {
    text = await parseDocx(file);
  } else {
    text = await file.text();
  }

  text = text.trim();
  if (!text) {
    throw new Error("未提取到文本");
  }
  if (text.length > MAX_TEXT_CHARS) {
    throw new Error(`文本超过 ${MAX_TEXT_CHARS} 字符，请拆分后上传`);
  }
  return text;
}

function buildDocId(sessionId, index) {
  const safeSession = sessionId.replace(/[^a-zA-Z0-9_-]/g, "-").slice(0, 48) || "session";
  return `web-${safeSession}-${Date.now()}-${index}`;
}

async function indexSelectedDocuments() {
  if (state.documents.length === 0) {
    return null;
  }
  if (state.documents.length > MAX_DOCUMENTS) {
    throw new Error(`每次最多上传 ${MAX_DOCUMENTS} 个文档`);
  }

  const userId = el.userId.value.trim() || "web-user";
  const sessionId = el.sessionId.value.trim() || newSessionId();
  el.sessionId.value = sessionId;

  const documents = [];
  for (const [index, entry] of state.documents.entries()) {
    updateDocumentStatus(entry.file.name, "解析中...");
    const content = await extractDocumentText(entry.file);
    updateDocumentStatus(entry.file.name, "等待入库");
    documents.push({
      doc_id: buildDocId(sessionId, index),
      title: entry.file.name,
      content,
      metadata: {
        source: "web-ui",
        file_name: entry.file.name,
        file_type: inferFileType(entry.file),
        user_id: userId,
        session_id: sessionId,
      },
    });
  }

  setStatus("文档入库中...");
  const resp = await fetch("/api/v1/documents/index", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ documents }),
  });
  if (!resp.ok) {
    const errorBody = await resp.json().catch(() => ({}));
    throw new Error(errorBody.detail || "文档入库失败，检查 arag 服务");
  }
  const data = await resp.json();
  for (const entry of state.documents) {
    updateDocumentStatus(entry.file.name, "已入库");
  }
  return data;
}

function appendUserMessage(query) {
  const parts = [];
  if (query) {
    parts.push(query);
  }
  if (state.documents.length) {
    parts.push(`文档：${state.documents.map((entry) => entry.file.name).join(", ")}`);
  }
  if (state.imageFile) {
    parts.push(`图片：${state.imageFile.name}`);
  }
  appendMessage("user", parts.join("\n"));
}

function parseSseBlock(block) {
  const event = { type: "message", data: "" };
  const lines = block.split(/\r?\n/);
  const data = [];
  for (const line of lines) {
    if (line.startsWith("event:")) {
      event.type = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      data.push(line.slice(5).trimStart());
    }
  }
  event.data = data.join("\n");
  return event;
}

async function readSseResponse(resp, assistant) {
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    buffer = buffer.replace(/\r\n/g, "\n");
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      handleSseEvent(parseSseBlock(block), assistant);
      boundary = buffer.indexOf("\n\n");
    }
  }
  buffer += decoder.decode();
  const tail = buffer.trim();
  if (tail) {
    handleSseEvent(parseSseBlock(tail), assistant);
  }
}

function handleSseEvent(event, assistant) {
  let payload = {};
  try {
    payload = event.data ? JSON.parse(event.data) : {};
  } catch {
    payload = { raw: event.data };
  }

  if (event.type === "text") {
    assistant.body.textContent += escapeText(payload.delta);
    scrollToBottom();
    return;
  }
  if (event.type === "citation") {
    addCitations(assistant.node, payload.refs || []);
    return;
  }
  if (event.type === "tool_call") {
    const callId = shortCallId(payload.id);
    const callLabel = callId ? ` · #${callId}` : "";
    addProcessItem(
      assistant.node,
      `tool_call · ${payload.name || ""}${callLabel}`,
      payload,
    );
    return;
  }
  if (event.type === "tool_result") {
    const callId = shortCallId(payload.id || payload.response?.skillCallId);
    const callLabel = callId ? ` · #${callId}` : "";
    addProcessItem(
      assistant.node,
      `tool_result · ${payload.name || ""}${callLabel}`,
      payload,
    );
    return;
  }
  if (event.type === "plan_step") {
    addProcessItem(assistant.node, "plan_step", payload);
    return;
  }
  if (event.type === "skill_event") {
    const eventName = payload.dataType === "CARD" ? "skill_card" : "skill_event";
    const callId = shortCallId(payload.skillCallId);
    const callLabel = callId ? ` · #${callId}` : "";
    const label = `${eventName} · ${payload.skill || ""}${callLabel}`;
    addProcessItem(assistant.node, label, payload.data ?? payload);
    return;
  }
  if (event.type === "error") {
    assistant.body.textContent += `\n${payload.message || "请求失败"}`;
    assistant.node.classList.add("error");
    return;
  }
  if (event.type !== "done") {
    addProcessItem(assistant.node, event.type, payload);
  }
}

async function sendChat(query) {
  const form = new FormData();
  form.append("query", query || (state.imageFile ? "请描述这张图片。" : ""));
  form.append("user_id", el.userId.value.trim() || "web-user");
  form.append("session_id", el.sessionId.value.trim() || newSessionId());
  if (state.imageFile) {
    form.append("image", state.imageFile, state.imageFile.name);
  }

  const agentUuid = encodeURIComponent(el.agentUuid.value.trim() || "demo");
  state.abortController = new AbortController();
  const resp = await fetch(`/api/v1/chat/${agentUuid}/stream`, {
    method: "POST",
    body: form,
    signal: state.abortController.signal,
  });
  if (!resp.ok || !resp.body) {
    throw new Error(`对话请求失败：${resp.status}`);
  }

  const assistant = appendMessage("assistant", "");
  await readSseResponse(resp, assistant);
}

function clearAttachmentsAfterSend() {
  state.documents = [];
  state.imageFile = null;
  if (state.imagePreviewUrl) {
    URL.revokeObjectURL(state.imagePreviewUrl);
  }
  state.imagePreviewUrl = "";
  el.documentInput.value = "";
  el.imageInput.value = "";
  renderAttachments();
}

async function handleSubmit(event) {
  event.preventDefault();
  const query = el.queryInput.value.trim();
  if (!query && state.documents.length === 0 && !state.imageFile) {
    setStatus("请输入内容或选择附件", "bad");
    return;
  }

  appendUserMessage(query);
  setBusy(true);
  setStatus("处理中...");

  try {
    const indexResult = await indexSelectedDocuments();
    if (indexResult) {
      appendMessage("system", `文档已入库：${indexResult.indexed_docs} 个文档，${indexResult.indexed_chunks} 个片段。`);
    }
    if (query || state.imageFile) {
      await sendChat(query);
    } else {
      setStatus("文档已入库，可以开始提问", "ok");
    }
    el.queryInput.value = "";
    clearAttachmentsAfterSend();
    setStatus("完成", "ok");
  } catch (err) {
    if (err.name === "AbortError") {
      appendMessage("system", "已停止生成。");
      setStatus("已停止");
    } else {
      appendMessage("error", err.message || String(err), "error");
      setStatus(err.message || "请求失败", "bad");
    }
  } finally {
    setBusy(false);
    state.abortController = null;
  }
}

async function loadHealth() {
  try {
    const resp = await fetch("/healthz");
    const data = await resp.json();
    el.healthStatus.textContent = `${data.engine || "agent"} · ${data.model || ""}`;
  } catch {
    el.healthStatus.textContent = "未连接";
  }
}

function bindEvents() {
  el.chatForm.addEventListener("submit", handleSubmit);
  el.stopBtn.addEventListener("click", () => {
    if (state.abortController) {
      state.abortController.abort();
    }
  });
  el.newSessionBtn.addEventListener("click", () => {
    el.sessionId.value = newSessionId();
    appendMessage("system", "新会话已创建。");
  });
  el.documentInput.addEventListener("change", () => {
    state.documents = Array.from(el.documentInput.files || []).slice(0, MAX_DOCUMENTS).map((file) => ({
      file,
      status: `${Math.ceil(file.size / 1024)} KB`,
    }));
    if ((el.documentInput.files || []).length > MAX_DOCUMENTS) {
      setStatus(`每次最多上传 ${MAX_DOCUMENTS} 个文档`, "bad");
    } else {
      setStatus("");
    }
    renderAttachments();
  });
  el.imageInput.addEventListener("change", () => {
    if (state.imagePreviewUrl) {
      URL.revokeObjectURL(state.imagePreviewUrl);
    }
    state.imageFile = (el.imageInput.files || [])[0] || null;
    state.imagePreviewUrl = state.imageFile ? URL.createObjectURL(state.imageFile) : "";
    renderAttachments();
  });
}

function init() {
  el.sessionId.value = newSessionId();
  bindEvents();
  loadHealth();
  appendMessage("system", "已就绪。");
}

init();
