/* 问小汉 · 管理后台逻辑 */
let TOKEN = localStorage.getItem("ah_token") || "";
let ME = JSON.parse(localStorage.getItem("ah_me") || "null");

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (TOKEN) headers["Authorization"] = "Bearer " + TOKEN;
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401 || res.status === 403) {
    document.body.insertAdjacentHTML("afterbegin",
      '<div class="alert err" style="margin:10px 20px">需要管理员登录（请先在前台登录管理员账号）</div>');
    throw new Error("forbidden");
  }
  const d = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(d.detail || "请求失败");
  return d;
}
function el(id) { return document.getElementById(id); }
function esc(s) {
  return String(s || "").replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function showTab(name) {
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".panel").forEach(p => p.classList.remove("show"));
  el("panel-" + name).classList.add("show");
}
window.showTab = showTab;

/* 总览 */
async function loadOverview() {
  try {
    const d = await api("/api/admin/overview");
    const k = [
      ["图书", d.books], ["知识块", d.chunks], ["用户", d.users],
      ["LLM", d.llm_configured ? "已配置" : "离线兜底"],
    ];
    el("kpiGrid").innerHTML = k.map(([l, v]) =>
      '<div class="kpi"><div class="num">' + esc(v) + '</div><div class="label">' + esc(l) + "</div></div>").join("");
    el("overviewEvals").innerHTML = (d.last_eval_runs || []).map(r =>
      '<div style="margin:6px 0">' + esc(r.ts) + " · " + esc(r.suite) +
      ' · <span class="' + (r.gate_passed ? "gate-pass" : "gate-fail") + '">' +
      (r.gate_passed ? "通过" : "未通过") + "</span></div>").join("");
  } catch (e) { /* ignore */ }
}

/* 策略 */
async function loadStrategies() {
  const d = await api("/api/admin/strategies");
  el("strategyTable").innerHTML = "<tr><th>Key</th><th>当前值</th></tr>" +
    Object.entries(d.strategies).map(([k, v]) =>
      "<tr><td>" + esc(k) + "</td><td><code>" + esc(JSON.stringify(v)) + "</code></td></tr>").join("");
}
async function saveStrategy() {
  try {
    await api("/api/admin/strategies/" + el("stKey").value.trim(), { method: "PUT",
      body: JSON.stringify({ value: JSON.parse(el("stValue").value) }) });
    loadStrategies();
  } catch (e) { alert(e.message); }
}
window.saveStrategy = saveStrategy;

/* A/B */
async function loadExps() {
  const d = await api("/api/admin/experiments");
  el("expList").innerHTML = "<table><tr><th>ID</th><th>名称</th><th>状态</th><th>赢家</th><th>指标</th><th>操作</th></tr>" +
    (d.experiments || []).map(x => "<tr><td>" + x.id + "</td><td>" + esc(x.name) + "</td><td>" +
      esc(x.status) + "</td><td>" + esc(x.winner || "-") + '</td><td><button class="small" onclick="expMetrics(\'' +
      esc(x.name) + '\')">查看</button></td><td>' +
      '<button class="small ghost" onclick="promoteExp(\'' + esc(x.name) + '\')">晋升 B</button></td></tr>').join("") +
    "</table><pre class=\"report\" id=\"expMetricsBox\" style=\"margin-top:10px\"></pre>";
}
async function createExp() {
  try {
    await api("/api/admin/experiments", { method: "POST", body: JSON.stringify({
      name: el("expName").value, description: el("expDesc").value,
      variants: JSON.parse(el("expVariants").value) }) });
    loadExps();
  } catch (e) { alert(e.message); }
}
async function expMetrics(name) {
  const d = await api("/api/admin/experiments/" + name + "/metrics");
  el("expMetricsBox").textContent = JSON.stringify(d, null, 2);
}
async function promoteExp(name) {
  await api("/api/admin/experiments/" + name + "/promote", { method: "POST",
    body: JSON.stringify({ winner: "B" }) });
  loadExps();
}
window.createExp = createExp; window.expMetrics = expMetrics; window.promoteExp = promoteExp;

/* Campaign */
async function loadCampaigns() {
  const d = await api("/api/admin/campaigns");
  el("cpList").innerHTML = "<table><tr><th>ID</th><th>名称</th><th>槽位</th><th>书目</th><th>启用</th><th></th></tr>" +
    (d.campaigns || []).map(c => "<tr><td>" + c.id + "</td><td>" + esc(c.name) + "</td><td>" + esc(c.slot) +
      "</td><td>" + esc(JSON.stringify(c.book_ids)) + "</td><td>" + (c.enabled ? "是" : "否") +
      '</td><td><button class="small ghost" onclick="delCampaign(' + c.id + ')">删除</button></td></tr>').join("") +
    "</table>";
  el("priorityList").innerHTML = "<table><tr><th>槽位</th><th>Book</th><th>权重</th><th>理由</th></tr>" +
    (d.priority || []).map(p => "<tr><td>" + esc(p.slot) + "</td><td>" + esc(p.book_id) + "</td><td>" +
      p.weight + "</td><td>" + esc(p.reason) + "</td></tr>").join("") + "</table>";
}
async function createCampaign() {
  try {
    await api("/api/admin/campaigns", { method: "POST", body: JSON.stringify({
      name: el("cpName").value, book_ids: JSON.parse(el("cpBooks").value || "[]") }) });
    loadCampaigns();
  } catch (e) { alert(e.message); }
}
async function setPriority() {
  try {
    await api("/api/admin/priority", { method: "POST", body: JSON.stringify({
      book_id: el("prBook").value.trim(), reason: el("prReason").value }) });
    loadCampaigns();
  } catch (e) { alert(e.message); }
}
async function delCampaign(id) { await api("/api/admin/campaigns/" + id, { method: "DELETE" }); loadCampaigns(); }
window.createCampaign = createCampaign; window.setPriority = setPriority; window.delCampaign = delCampaign;

/* 评测 */
async function runEval(suite) {
  el("evalReport").textContent = "评测执行中…（RAG 全量约需 1-3 分钟，取决于是否启用 LLM）";
  try {
    const d = await api("/api/admin/evals/run", { method: "POST",
      body: JSON.stringify({ suite }) });
    el("evalReport").textContent = d.report || JSON.stringify(d, null, 2);
    loadEvalRuns();
  } catch (e) { el("evalReport").textContent = "失败: " + e.message; }
}
async function loadEvalRuns() {
  const d = await api("/api/admin/evals/runs");
  el("evalRuns").innerHTML = "<tr><th>时间</th><th>套件</th><th>总量</th><th>通过项</th><th>门禁</th></tr>" +
    (d.runs || []).map(r => "<tr><td>" + esc(r.ts) + "</td><td>" + esc(r.suite) + "</td><td>" +
      r.total + "</td><td>" + r.passed + '</td><td class="' + (r.gate_passed ? "gate-pass" : "gate-fail") +
      '">' + (r.gate_passed ? "通过" : "未通过") + "</td></tr>").join("");
}
window.runEval = runEval;

/* 审计 */
async function loadAudit() {
  const a = await api("/api/admin/audit?limit=50");
  el("auditTable").innerHTML = "<tr><th>时间</th><th>用户</th><th>工具</th><th>决定</th><th>理由</th></tr>" +
    (a.logs || []).map(l => "<tr><td>" + esc(l.ts) + "</td><td>" + esc(l.user_id || "-") + "</td><td>" +
      esc(l.tool) + '</td><td class="' + (l.decision === "allow" ? "gate-pass" : "gate-fail") + '">' +
      esc(l.decision) + "</td><td>" + esc(l.reason) + "</td></tr>").join("");
  const i = await api("/api/admin/injections");
  el("injTable").innerHTML = "<tr><th>时间</th><th>来源</th><th>片段</th><th>命中</th><th>分</th><th>拦截</th></tr>" +
    (i.hits || []).map(h => "<tr><td>" + esc(h.ts) + "</td><td>" + esc(h.source) + "</td><td>" +
      esc(h.snippet) + "</td><td>" + esc(h.patterns) + "</td><td>" + h.score + "</td><td>" +
      (h.blocked ? "是" : "否") + "</td></tr>").join("");
}

/* 模型调用 */
async function loadModelCalls() {
  const d = await api("/api/admin/model_calls");
  el("mhTable").innerHTML = "<tr><th>服务</th><th>模型</th><th>调用</th><th>tokens</th><th>成本</th><th>平均延迟</th></tr>" +
    (d.by_model || []).map(r => "<tr><td>" + esc(r.service) + "</td><td>" + esc(r.model) + "</td><td>" +
      r.calls + "</td><td>" + ((r.pt || 0) + (r.ct || 0)) + "</td><td>¥" + (r.cost || 0).toFixed(4) +
      "</td><td>" + ((r.avg_ms || 0).toFixed ? r.avg_ms.toFixed(0) + "ms" : r.avg_ms + "ms") + "</td></tr>").join("");
  el("mhRecent").innerHTML = "<tr><th>时间</th><th>服务</th><th>模型</th><th>状态</th><th>延迟</th></tr>" +
    (d.recent || []).slice(0, 30).map(r => "<tr><td>" + esc(r.ts) + "</td><td>" + esc(r.service) +
      "</td><td>" + esc(r.model) + '</td><td class="' + (r.status === "ok" ? "gate-pass" : "gate-fail") +
      '">' + esc(r.status) + "</td><td>" + r.latency_ms + "ms</td></tr>").join("");
}

/* 内容 / 离线 */
async function loadAdminBooks() {
  const d = await api("/api/admin/books");
  el("adminBooks").innerHTML = "<tr><th>书</th><th>分类</th><th>版本</th><th>块数</th><th>来源</th></tr>" +
    (d.books || []).map(b => "<tr><td>《" + esc(b.title) + "》" + esc(b.author) + "</td><td>" + esc(b.category) +
      "</td><td>v" + b.version + "</td><td>" + b.n_chunks + "</td><td>" + esc(b.source_file) + "</td></tr>").join("");
}
async function uploadBook() {
  const f = el("uploadFile").files[0];
  if (!f) { alert("请选择文件"); return; }
  const fd = new FormData(); fd.append("file", f);
  const res = await fetch("/api/admin/books/upload", {
    method: "POST", headers: { Authorization: "Bearer " + TOKEN }, body: fd });
  const d = await res.json();
  el("contentAlert").innerHTML = '<div class="' + (res.ok ? "alert ok" : "alert err") + '">' +
    esc(JSON.stringify(d.report || d.detail || d)) + "</div>";
  loadAdminBooks();
}
async function reindex() {
  const d = await api("/api/admin/books/reindex", { method: "POST", body: JSON.stringify({ reindex: true }) });
  el("contentAlert").innerHTML = '<div class="alert ok">' + esc(JSON.stringify(d.reports)) + "</div>";
  loadAdminBooks();
}
async function offlineTask(path) {
  el("offlineReport").textContent = "执行中…";
  try {
    const d = await api("/api/admin/" + path, { method: "POST", body: "{}" });
    el("offlineReport").textContent = JSON.stringify(d, null, 2);
  } catch (e) { el("offlineReport").textContent = "失败: " + e.message; }
}
window.uploadBook = uploadBook; window.reindex = reindex; window.offlineTask = offlineTask;

/* 启动 */
if (ME) el("who").textContent = ME.username + (ME.role === "admin" ? "（管理员）" : "（非管理员）");
loadOverview(); loadStrategies(); loadExps(); loadCampaigns(); loadEvalRuns();
loadAudit(); loadModelCalls(); loadAdminBooks();
