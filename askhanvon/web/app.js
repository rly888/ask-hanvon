/* 问小汉 · 前端逻辑（原生 JS，无构建依赖） */
const API = "";
let TOKEN = localStorage.getItem("ah_token") || "";
let ME = JSON.parse(localStorage.getItem("ah_me") || "null");
let CURRENT_SESSION = "";
let CURRENT_BOOK = null;
let PENDING_ORDER = null;

/* ---------- 基础 ---------- */
async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (TOKEN) headers["Authorization"] = "Bearer " + TOKEN;
  const res = await fetch(API + path, { ...opts, headers });
  if (res.status === 401) { showLogin(); throw new Error("请先登录"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "请求失败");
  return data;
}
function track(eventType, props = {}, bookId = "") {
  api("/api/events", { method: "POST", body: JSON.stringify([
    { event_type: eventType, book_id: bookId, props }]) }).catch(() => {});
}
function el(id) { return document.getElementById(id); }
function esc(s) {
  return String(s || "").replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function showModal(id) { el(id).classList.add("show"); }
function closeModal(id) { el(id).classList.remove("show"); }
window.closeModal = closeModal;

/* ---------- 认证 ---------- */
function showLogin() { showModal("loginModal"); }
async function login() {
  try {
    const d = await api("/api/auth/login", { method: "POST",
      body: JSON.stringify({ username: el("loginUsername").value, password: el("loginPassword").value }) });
    TOKEN = d.token; ME = d;
    localStorage.setItem("ah_token", TOKEN);
    localStorage.setItem("ah_me", JSON.stringify(d));
    closeModal("loginModal"); renderUser(); loadSessions(); loadShelf();
  } catch (e) { el("loginAlert").innerHTML = '<div class="alert err">' + esc(e.message) + "</div>"; }
}
async function registerMe() {
  try {
    const d = await api("/api/auth/register", { method: "POST",
      body: JSON.stringify({ username: el("loginUsername").value, password: el("loginPassword").value }) });
    TOKEN = d.token; ME = { user_id: d.user_id, username: d.username, role: "user" };
    localStorage.setItem("ah_token", TOKEN);
    localStorage.setItem("ah_me", JSON.stringify(ME));
    closeModal("loginModal"); renderUser(); loadSessions(); loadShelf();
  } catch (e) { el("loginAlert").innerHTML = '<div class="alert err">' + esc(e.message) + "</div>"; }
}
function logout() {
  TOKEN = ""; ME = null; localStorage.removeItem("ah_token"); localStorage.removeItem("ah_me");
  renderUser(); el("sessions").innerHTML = "";
}
function renderUser() {
  const on = !!ME;
  el("loginBtn").style.display = on ? "none" : "";
  el("logoutBtn").style.display = on ? "" : "none";
  el("userBadge").style.display = on ? "" : "none";
  el("userBadge").textContent = on ? (ME.username + (ME.role === "admin" ? "（管理员）" : "")) : "";
  el("adminLink").style.display = on && ME.role === "admin" ? "" : "none";
}

/* ---------- 健康与模式徽标 ---------- */
async function loadHealth() {
  try {
    const h = await api("/api/health");
    el("modeBadge").textContent = h.llm_configured ? "· LLM 在线" : "· 离线兜底模式（未配置 LLM Key）";
  } catch (e) { /* ignore */ }
}

/* ---------- 会话 ---------- */
async function loadSessions() {
  if (!ME) return;
  try {
    const d = await api("/api/sessions");
    el("sessions").innerHTML = (d.sessions || []).map(s =>
      '<div class="session-item' + (s.id === CURRENT_SESSION ? " active" : "") +
      '" onclick="openSession(\'' + s.id + '\')"><span>' + esc(s.title || "会话") +
      "</span><span>" + esc((s.updated_at || "").slice(5, 16)) + "</span></div>").join("");
  } catch (e) { /* ignore */ }
}
async function openSession(sid) {
  CURRENT_SESSION = sid;
  el("chatBox").innerHTML = "";
  try {
    const d = await api("/api/sessions/" + sid + "/messages");
    (d.messages || []).forEach(m => appendMessage(m.role, m.content, m.meta || {}, false));
    scrollChat();
  } catch (e) { /* ignore */ }
  loadSessions();
}

/* ---------- 聊天（SSE 流式） ---------- */
async function sendMsg() {
  const text = el("chatInput").value.trim();
  if (!text) return;
  el("chatInput").value = "";
  el("sendBtn").disabled = true;
  appendMessage("user", text, {}, true);
  const bubble = addAssistantShell();
  let answer = "";
  let finalPayload = null;

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(TOKEN ? { Authorization: "Bearer " + TOKEN } : {}) },
      body: JSON.stringify({ message: text, session_id: CURRENT_SESSION, stream: true }),
    });
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const line = buf.slice(0, idx).trim(); buf = buf.slice(idx + 2);
        if (!line.startsWith("data:")) continue;
        let evt; try { evt = JSON.parse(line.slice(5)); } catch (e) { continue; }
        handleChatEvent(evt, bubble, t => { answer += t; }, p => { finalPayload = p; });
      }
    }
  } catch (e) {
    bubble.body.textContent = "我暂时查不到，请稍后再试。";
  }
  el("sendBtn").disabled = false;
  if (finalPayload && finalPayload.session_id && finalPayload.session_id !== CURRENT_SESSION) {
    CURRENT_SESSION = finalPayload.session_id;
    loadSessions();
  }
  scrollChat();
}

function handleChatEvent(evt, bubble, onDelta, onFinal) {
  switch (evt.type) {
    case "intent":
      bubble.steps.push({ t: "意图", v: evt.intent + (evt.book_title ? "（" + evt.book_title + "）" : "") + " · " + evt.source });
      break;
    case "plan":
      (evt.steps || []).forEach(s => bubble.steps.push({ t: "工具", v: s.tool + " · " + s.reason }));
      break;
    case "retrieval":
      bubble.steps.push({ t: "检索", v: "召回 " + evt.n + " 段（剔除不可信 " + evt.untrusted + "）" });
      break;
    case "context":
      bubble.steps.push({ t: "上下文", v: "置信 " + evt.confidence + " · " + (evt.chunks || []).join(" / ") });
      break;
    case "delta":
      bubble.text = (bubble.text || "") + (evt.text || "");
      bubble.body.textContent = bubble.text;
      scrollChat();
      break;
    case "citations":
      bubble.pendingCitations = evt.citations || [];
      break;
    case "done": {
      const p = evt;
      onFinal(p);
      bubble.body.textContent = p.text || bubble.text || "（无内容）";
      renderBubbleExtras(bubble, p);
      if (p.items && p.items.length) bubble.body.insertAdjacentHTML("beforeend", itemsHtml(p.items, p.card_kind));
      if (p.type === "comparison") bubble.body.insertAdjacentHTML("beforeend", comparisonHtml(p.data));
      if (p.type === "order") showPayModal(p.data);
      scrollChat();
      break;
    }
    case "error":
      bubble.body.textContent += "（" + (evt.error || "出错了") + "）";
      break;
  }
  bubble.stepsEl.innerHTML = bubble.steps.map(s =>
    '<span class="tag">' + esc(s.t) + "</span>" + esc(s.v)).join("<br>");
}

function addAssistantShell() {
  const wrap = document.createElement("div");
  wrap.className = "msg assistant";
  wrap.innerHTML = '<div style="width:100%"><div class="steps"></div>' +
    '<div class="bubble" style="margin-top:8px"></div></div>';
  el("chatBox").appendChild(wrap);
  const shell = {
    steps: [],
    pendingCitations: [],
    text: "",
    stepsEl: wrap.querySelector(".steps"),
    body: wrap.querySelector(".bubble"),
  };
  scrollChat();
  return shell;
}

function renderBubbleExtras(bubble, p) {
  if (p.citations && p.citations.length) {
    const html = p.citations.map(c =>
      '<span class="cite-chip" onclick="openBookById(\'' + c.book_id + '\')">[' + c.idx + "] " +
      esc("《" + c.book_title + "》第" + c.chapter_no + "章 " + c.chapter_title + " · " + c.pages) +
      "</span>").join("");
    bubble.body.insertAdjacentHTML("beforeend", '<div class="citations">' + html + "</div>");
  }
  const m = [];
  if (p.model) m.push("模型: " + p.model);
  if (p.degraded) m.push("离线兜底");
  if (p.latency_ms) m.push("耗时 " + p.latency_ms + "ms");
  if (p.usage && (p.usage.prompt_tokens || p.usage.completion_tokens)) {
    m.push("tokens " + (p.usage.prompt_tokens || 0) + "/" + (p.usage.completion_tokens || 0)
      + " · ¥" + Number(p.usage.cost || 0).toFixed(4));
  }
  if (p.retried) m.push("已按改写查询重检一次");
  if (m.length) bubble.body.insertAdjacentHTML("beforeend",
    '<div class="meta">' + esc(m.join(" · ")) + "</div>");
  // 追问建议 chips（P2-4）
  if (p.suggestions && p.suggestions.length) {
    bubble.body.insertAdjacentHTML("beforeend",
      '<div class="suggestions">' + p.suggestions.map(s =>
        '<button class="ghost small" onclick="askSuggestion(this)">' + esc(s) + "</button>").join("") +
      "</div>");
  }
}
function askSuggestion(btn) {
  el("chatInput").value = btn.textContent.trim();
  sendMsg();
}
window.askSuggestion = askSuggestion;

function appendMessage(role, content, meta, isNow) {
  const wrap = document.createElement("div");
  wrap.className = "msg " + role;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = content || "";
  wrap.appendChild(bubble);
  if (role === "assistant" && meta) {
    if (meta.citations && meta.citations.length) {
      bubble.insertAdjacentHTML("beforeend", '<div class="citations">' +
        meta.citations.map(c => '<span class="cite-chip" onclick="openBookById(\'' + c.book_id +
          '\')">《' + esc(c.book_title) + "》第" + esc(c.chapter_no) + "章</span>").join("") + "</div>");
    }
  }
  el("chatBox").appendChild(wrap);
  scrollChat();
}
function scrollChat() { const b = el("chatBox"); b.scrollTop = b.scrollHeight; }

/* ---------- 推荐书架 ---------- */
async function loadShelf() {
  try {
    const d = await api("/api/recommend?scene=homepage&top_k=6");
    el("shelf").innerHTML = (d.items || []).map(itemHtml).join("") ||
      '<div style="color:var(--muted)">书库为空，请先导入样书。</div>';
  } catch (e) {
    el("shelf").innerHTML = '<div class="alert err">' + esc(e.message) + "</div>";
  }
}
function itemHtml(i) {
  return '<div class="book-card" onclick="openBookById(\'' + i.book_id + '\')">' +
    '<div class="book-cover">' + esc(i.cover_emoji || "📘") + "</div>" +
    '<div class="info"><div class="title">' + i.position + ". " + esc(i.title) + "</div>" +
    '<div class="author">' + esc(i.author) + " · " + esc(i.category) + ' · 评分 ' + esc(i.score) + "</div>" +
    '<div class="reasons">' + (i.reasons || []).map(r => '<span class="reason-chip">' + esc(r) + "</span>").join("") + "</div>" +
    '<div class="breakdown">' + esc("通道: " + (i.channels || []).join("+") +
      (i.breakdown && i.breakdown.rules_applied && i.breakdown.rules_applied.length
        ? " · 规则: " + i.breakdown.rules_applied.join("+") : "")) + "</div>" +
    "</div></div>";
}
function itemsHtml(items, kind) {
  return '<div style="margin-top:10px;display:flex;flex-direction:column;gap:8px">' +
    items.map(i => itemHtml(Object.assign({ position: "" }, i))).join("") + "</div>";
}
function comparisonHtml(data) {
  const cols = (data && data.columns) || [];
  return "<table><tr><th>书</th><th>作者</th><th>分类</th><th>要点</th></tr>" +
    cols.map(c => c.found ? "<tr><td>" + esc(c.title) + "</td><td>" + esc(c.author) +
      "</td><td>" + esc(c.category) + "</td><td>" + esc((c.key_points || []).join("；")) +
      "</td></tr>" : "<tr><td>" + esc(c.title) + '</td><td colspan="3">未收录</td></tr>').join("") +
    "</table>";
}

/* ---------- 图书详情 / 阅读 / 购买 ---------- */
async function openBookById(bookId) { 
  const d = await api("/api/books/" + bookId);
  CURRENT_BOOK = d.book;
  el("bookTitle").textContent = "《" + d.book.title + "》";
  el("bookInfo").innerHTML = esc(d.book.author + " · " + d.book.category + (d.book.tags ? " · " + d.book.tags : "")) +
    "<br>" + esc(d.book.description || "");
  el("chapterList").innerHTML = (d.chapters || []).map(c =>
    '<div class="chapter-item" onclick="readChapter(\'' + bookId + "'," + c.no + ')">' +
    esc((c.vol ? c.vol + " · " : "") + "第" + c.no + "回 " + c.title) + "</div>").join("");
  showModal("bookModal");
  track("click", { source: "detail" }, bookId);
}
async function readChapter(bookId, chapterNo) {
  const d = await api("/api/books/" + bookId + "/content?chapter_no=" + chapterNo);
  el("readerTitle").textContent = "《" + d.book.title + "》第" + d.chapter_no + "回 " + d.chapter_title;
  el("readerMeta").textContent = "电子版页 " + d.page_start + "-" + d.page_end;
  el("readerBody").innerHTML = (d.paragraphs || []).map(p => "<p>" + esc(p) + "</p>").join("");
  closeModal("bookModal"); showModal("readerModal");
  track("read_duration", { seconds: 30 }, bookId);
}
async function collectBook() {
  if (!ME) { showLogin(); return; }
  const r = await api("/api/tools/my_library", { method: "POST",
    body: JSON.stringify({ arguments: { action: "collect", book_title: CURRENT_BOOK.title } }) });
  alert(r.data && r.data.message || "已收藏");
}
async function buyBook() {
  if (!ME) { showLogin(); return; }
  try {
    const r = await api("/api/tools/purchase_init", { method: "POST",
      body: JSON.stringify({ arguments: { book_title: CURRENT_BOOK.title, qty: 1 } }) });
    if (!r.ok) { alert(r.error); return; }
    showPayModal(r.data);
  } catch (e) { alert(e.message); }
}
function showPayModal(order) {
  PENDING_ORDER = order;
  el("payInfo").innerHTML = '<div class="alert ok">《' + esc(order.book_title) + "》× " + order.qty +
    " · 合计 ¥" + esc(order.price) + " · 订单号 " + esc(order.order_id) +
    "<br>确认令牌：<b>" + esc(order.confirm_token) + "</b>（" + order.expires_in + "s 内有效）</div>";
  el("payToken").value = order.confirm_token;
  showModal("payModal");
}
async function confirmPay() {
  try {
    const r = await api("/api/tools/purchase_confirm", { method: "POST",
      body: JSON.stringify({ arguments: { order_id: PENDING_ORDER.order_id,
        confirm_token: el("payToken").value } }) });
    if (r.ok) { alert(r.data.message || "支付成功"); closeModal("payModal"); }
    else alert(r.error);
  } catch (e) { alert(e.message); }
}

/* ---------- 搜索 ---------- */
async function doSearch() {
  const q = el("globalSearch").value.trim();
  if (!q) return;
  appendMessage("user", "搜索: " + q, {}, true);
  const shell = addAssistantShell();
  const r = await api("/api/search?q=" + encodeURIComponent(q));
  shell.body.textContent = r.ok ? ("找到 " + r.data.total + " 本相关图书：") : (r.error || "未找到");
  if (r.ok && r.data.results.length) {
    shell.body.insertAdjacentHTML("beforeend", itemsHtml(r.data.results));
  }
  track("search", { query: q });
}
window.doSearch = doSearch;
window.showLogin = showLogin; window.login = login; window.registerMe = registerMe;
window.logout = logout; window.sendMsg = sendMsg; window.openBookById = openBookById;
window.readChapter = readChapter; window.collectBook = collectBook; window.buyBook = buyBook;
window.confirmPay = confirmPay;

/* ---------- 启动 ---------- */
renderUser(); loadHealth(); loadShelf();
if (ME) loadSessions();
