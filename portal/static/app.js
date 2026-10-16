/* HARVEST — command center terminal */

const $ = (id) => document.getElementById(id);
let actionsSeries = [], errorsSeries = [], procSeries = [];
let networks = [];
let stoppingRequested = false;
// HTTP-опрос при обрыве /ws (live-кадры идут каждые WS_PUSH_S=3с с бэка).
// Таблицы (история, кошельки) тяжёлые — опрашиваем в 3 раза реже.
const refreshMs = 4000;
const tablesRefreshMs = refreshMs * 3;

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
}
function fmt(n) {
  n = Number(n);
  if (!isFinite(n)) return "—";
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(Math.round(n * 100) / 100);
}

async function j(url, opts) {
  const r = await fetch(url, opts);
  const body = await r.json().catch(() => ({}));
  if (r.status === 401) { location.href = "/login"; throw new Error("unauth"); }
  if (!r.ok) throw new Error(body.error || "HTTP " + r.status);
  return body;
}

/* ── MOUSE GLOW ─────────────────────────────────────────────── */
const glowEl = $("mouseGlow");
document.addEventListener("mousemove", e => {
  if (glowEl) {
    glowEl.style.left = e.clientX + "px";
    glowEl.style.top = e.clientY + "px";
  }
});

/* ── NAV ────────────────────────────────────────────────────── */
function nav(name) {
  document.querySelectorAll(".nav-item[data-nav]").forEach(el =>
    el.classList.toggle("active", el.getAttribute("data-nav") === name));
  document.querySelectorAll(".section").forEach(s =>
    s.classList.toggle("active", s.id === "sec-" + name));
  if (name === "scheduler") loadScheduler();
  if (name === "groups") loadGroups();
  if (name === "rpc") loadRpcHealth();
  if (name === "alerts") loadBalanceAlerts();
}

/* ── NETWORKS ───────────────────────────────────────────────── */
function netMeta(n) {
  return `chain ${n.chain_id} · ${esc(n.currency)}` +
    (n.faucet ? ` · <a href="${esc(n.faucet)}" target="_blank" rel="noopener">кран</a>` : "");
}

function netCard(n) {
  const tag = n.running
    ? '<span class="badge badge-running">фармит</span>'
    : n.current ? '<span class="badge badge-current">текущая</span>' : "";
  const btn = n.running
    ? `<button class="btn btn-danger btn-sm" data-net-stop="${n.slug}"><i class="ti ti-player-stop"></i> Стоп</button>`
    : `<button class="btn btn-primary btn-sm" data-net-start="${n.slug}"><i class="ti ti-player-play"></i> Фармить</button>`;
  const acts = (n.activities || []).map(a =>
    `<li><i class="ti ti-check"></i>${esc(a)}</li>`).join("");
  return `<div class="net-card">
    <div class="net-card-header">
      <div>
        <div class="net-card-name">${esc(n.name)}</div>
        <div class="net-card-meta">${netMeta(n)}</div>
      </div>
      ${tag}
    </div>
    <div class="net-card-tagline">${esc(n.tagline || "")}</div>
    <div class="net-card-desc">${esc(n.description || "")}</div>
    <div class="net-card-rewards">
      <div class="label">Награды</div>
      ${esc(n.rewards || "—")}
    </div>
    ${acts ? `<ul class="net-card-activities">${acts}</ul>` : ""}
    <div class="net-card-footer">${btn}</div>
  </div>`;
}

async function loadNetworks() {
  try {
    const d = await j("/api/networks");
    networks = d.networks || [];
    const sel = $("netSelect");
    sel.innerHTML = networks.map(n =>
      `<option value="${n.slug}" ${n.current ? "selected" : ""}>${esc(n.name)}</option>`).join("");
    $("netCards").innerHTML = networks.map(netCard).join("");
    document.querySelectorAll("[data-net-start]").forEach(b =>
      b.onclick = () => netAction(b.dataset.netStart, "start"));
    document.querySelectorAll("[data-net-stop]").forEach(b =>
      b.onclick = () => netAction(b.dataset.netStop, "stop"));
    renderNetMeta();
  } catch (e) { if (e.message !== "unauth") toast("Сети: " + e.message); }
}

function selectedNet() { return networks.find(n => n.slug === $("netSelect").value) || null; }

function renderNetMeta() {
  const n = selectedNet();
  if (!n) return;
  $("netMeta").innerHTML = netMeta(n);
  const d = $("netDesc");
  if (d) d.innerHTML = `<span style="color:var(--text-0);font-weight:600">${esc(n.name)}</span> — ${esc(n.tagline || "")}. ${esc(n.description || "")} <span style="color:var(--text-3)">(${esc(n.rewards || "")})</span>`;
}

async function netAction(slug, action) {
  try {
    await j(`/api/farm/network/${slug}/${action}`, { method: "POST" });
    await loadNetworks();
    await tick();
  } catch (e) { toast("Не удалось: " + e.message); }
}

/* ── STATE RENDER ───────────────────────────────────────────── */
function renderState(s) {
  const pool = s.pool || {};
  const total = (pool.actions ?? 0) + (pool.errors ?? 0);
  const rate = total > 0 ? (pool.actions / total * 100) : null;

  $("cActions").textContent = fmt(pool.actions);
  $("sActions").textContent = "за цикл " + fmt((pool.actions ?? 0) / Math.max(pool.cycles || 1, 1));

  $("sRate").textContent = rate === null ? "—" : rate.toFixed(1) + "%";
  $("sRateSub").textContent = rate !== null ? fmt(total) + " всего" : "—";

  $("cErrors").textContent = fmt(pool.errors);
  $("sErrors").textContent = "дроп " + fmt(pool.dropped ?? 0);

  $("cCycles").textContent = fmt(pool.cycles);
  $("sCycles").textContent = "история " + fmt((s.db || {}).cycles);

  $("cWallets").textContent = fmt(s.wallet_count);
  $("cProcessed").textContent = fmt(pool.processed);
  $("sProcessed").textContent = "из " + fmt(s.wallet_count);
  $("cWorkers").textContent = fmt(pool.dyn_workers ?? 0);
  $("sWorkers").textContent = "health " + (s.health_factor ?? 1).toFixed(2);

  const pct = rate === null ? 0 : rate;
  $("progressPct").textContent = pct.toFixed(1) + "%";
  $("successBar").style.width = pct + "%";

  const dot = $("dot"), st = $("statusText");
  if (!s.running) {
    stoppingRequested = false;
    dot.className = "status-dot off";
    st.textContent = "stopped";
  } else if (stoppingRequested) {
    dot.className = "status-dot pau";
    st.textContent = "stopping...";
  } else if (pool.paused) {
    dot.className = "status-dot pau";
    st.textContent = "paused";
  } else {
    dot.className = "status-dot on";
    st.textContent = "running";
  }

  $("healthText").textContent = (s.health_factor ?? 1).toFixed(2);
  $("updatedText").textContent = new Date().toLocaleTimeString("ru-RU");

  if (s.network) {
    $("netName").textContent = s.network;
    $("headerNet").textContent = s.network;
    if ($("netSelect")) $("netSelect").value = s.network;
  }
}

/* ── CHARTS ─────────────────────────────────────────────────── */
function drawCharts() {
  drawChart($("chartMain"), [actionsSeries, errorsSeries], ["#a855f7", "#ef4444"]);
  drawChart($("chartProc"), [procSeries], ["#22d3ee"]);
}

function drawChart(canvas, series, colors) {
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth * dpr;
  const h = canvas.clientHeight * dpr;
  // Display:none секции дают нулевые размеры — рисование всё равно стёрло
  // бы уже готовый кадр, поэтому пропускаем такой вызов.
  if (w < 2 || h < 2) return;
  if (canvas.width !== w) canvas.width = w;
  if (canvas.height !== h) canvas.height = h;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, w, h);

  const pad = 8 * dpr;
  const plotW = w - pad * 2;
  const plotH = h - pad * 2;

  ctx.strokeStyle = "rgba(139,92,246,0.05)";
  ctx.lineWidth = dpr;
  for (let i = 1; i < 4; i++) {
    const y = pad + (i / 4) * plotH;
    ctx.beginPath();
    ctx.moveTo(pad, y);
    ctx.lineTo(w - pad, y);
    ctx.stroke();
  }

  series.forEach((data, si) => {
    if (data.length < 2) return;
    const max = Math.max(...data, 1);
    const min = Math.min(...data, 0);
    const range = max - min || 1;
    const x = i => pad + (i / (data.length - 1)) * plotW;
    const y = v => h - pad - ((v - min) / range) * plotH;
    const col = colors[si % colors.length];

    ctx.beginPath();
    ctx.moveTo(x(0), y(data[0]));
    for (let i = 1; i < data.length; i++) {
      const prev = { x: x(i - 1), y: y(data[i - 1]) };
      const cur = { x: x(i), y: y(data[i]) };
      const cpx = (prev.x + cur.x) / 2;
      ctx.bezierCurveTo(cpx, prev.y, cpx, cur.y, cur.x, cur.y);
    }
    ctx.strokeStyle = col;
    ctx.lineWidth = (si === 0 ? 2 : 1.5) * dpr;
    ctx.stroke();

    if (si === 0) {
      ctx.lineTo(x(data.length - 1), h - pad);
      ctx.lineTo(x(0), h - pad);
      ctx.closePath();
      const g = ctx.createLinearGradient(0, pad, 0, h - pad);
      g.addColorStop(0, hexA(col, 0.12));
      g.addColorStop(1, hexA(col, 0));
      ctx.fillStyle = g;
      ctx.fill();
    }

    const lastX = x(data.length - 1);
    const lastY = y(data[data.length - 1]);
    ctx.beginPath();
    ctx.arc(lastX, lastY, 3 * dpr, 0, Math.PI * 2);
    ctx.fillStyle = col;
    ctx.fill();
    ctx.beginPath();
    ctx.arc(lastX, lastY, 5 * dpr, 0, Math.PI * 2);
    ctx.strokeStyle = hexA(col, 0.25);
    ctx.lineWidth = dpr;
    ctx.stroke();
  });
}

function hexA(hex, a) {
  try {
    hex = hex.replace("#", "");
    return `rgba(${parseInt(hex.slice(0, 2), 16)},${parseInt(hex.slice(2, 4), 16)},${parseInt(hex.slice(4, 6), 16)},${a})`;
  } catch { return `rgba(168,85,247,${a})`; }
}

/* ── TABLES ─────────────────────────────────────────────────── */
async function loadHistory() {
  try {
    const d = await j("/api/cycle-history");
    const rows = (d.history || []).map(c => {
      const e = c.errors || 0;
      const tag = e === 0
        ? '<span class="badge badge-ok">ok</span>'
        : `<span class="badge badge-warn">${esc(e)}</span>`;
      return `<tr>
        <td class="mono" style="color:var(--text-3)">${esc(c.id)}</td>
        <td>${tag}</td>
        <td class="num">${fmt(c.actions_ok)}</td>
        <td class="num">${esc(e)}</td>
        <td class="num">${fmt(c.duration_s)}</td>
        <td class="num">${esc(c.wallets)}</td>
        <td class="mono" style="color:var(--text-2)">${esc(String(c.started_at || "").slice(0, 16))}</td>
      </tr>`;
    }).join("");
    $("tHistory").innerHTML = rows || '<tr><td colspan="7" style="color:var(--text-3)">циклов пока нет</td></tr>';
  } catch {}
}

async function loadWallets() {
  try {
    const d = await j("/api/top-wallets");
    const rows = (d.wallets || []).map((w, i) =>
      `<tr>
        <td class="mono" style="color:var(--text-3)">${i + 1}</td>
        <td><span class="mono copyable" data-addr="${esc(w.address)}">${esc(w.address)}</span></td>
        <td class="num">${fmt(w.actions)}</td>
      </tr>`
    ).join("");
    $("tWallets").innerHTML = rows || '<tr><td colspan="3" style="color:var(--text-3)">кошельков нет — создайте кнопкой «Создать» ниже</td></tr>';
    document.querySelectorAll(".copyable").forEach(el =>
      el.addEventListener("click", () =>
        navigator.clipboard?.writeText(el.getAttribute("data-addr")).then(() => toast("Адрес скопирован"))));
  } catch {}
}

/* ── TICK ───────────────────────────────────────────────────── */
async function loadMetrics() {
  try {
    const d = await j("/api/metrics?limit=60");
    const metrics = d.metrics || [];
    actionsSeries = metrics.map(m => m.actions);
    errorsSeries = metrics.map(m => m.errors);
    procSeries = metrics.map(m => m.processed);
    drawCharts();
    $("chartLegend1").textContent = "окно " + actionsSeries.length;
    $("chartLegend2").textContent = "окно " + procSeries.length;
  } catch {}
}

function pushSample(pool) {
  actionsSeries.push(pool.actions ?? 0);
  errorsSeries.push(pool.errors ?? 0);
  procSeries.push(pool.processed ?? 0);
  if (actionsSeries.length > 60) {
    actionsSeries.shift(); errorsSeries.shift(); procSeries.shift();
  }
  drawCharts();
  $("chartLegend1").textContent = "окно " + actionsSeries.length;
  $("chartLegend2").textContent = "окно " + procSeries.length;
}

async function refreshTables() {
  await loadHistory();
  await loadWallets();
}

async function tick() {
  try {
    const s = await j("/api/stats");
    renderState(s);
    pushSample(s.pool || {});
    await refreshTables();
  } catch (e) { if (e.message !== "unauth") toast("Ошибка: " + e.message); }
}

/* ── LIVE FEED ──────────────────────────────────────────────── */
/* /ws сам пушит статус каждые 3 сек. Таблиц (история, кошельки) в этих
   кадрах нет — их опрашиваем своим таймером. При обрыве сокета возвращаемся
   на полный HTTP-опрос и переподключаемся с экспоненциальным шагом. */
let ws = null;
let pollTimer = 0;
let wsBackoff = 1000;

function setFeed(mode) {
  const dot = $("feedDot"), txt = $("feedText");
  if (!dot || !txt) return;
  const live = mode === "live";
  dot.className = "feed-dot " + (live ? "on" : "off");
  txt.textContent = live ? "live" : "poll";
  txt.title = live ? "Обновления по WebSocket" : "Обновления HTTP-опросом";
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(tick, refreshMs);
  setFeed("poll");
}

function stopPolling() {
  if (!pollTimer) return;
  clearInterval(pollTimer);
  pollTimer = 0;
}

function connectLive() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  try {
    ws = new WebSocket(proto + "//" + location.host + "/ws");
  } catch {
    startPolling();
    return;
  }
  ws.onopen = () => { wsBackoff = 1000; stopPolling(); setFeed("live"); };
  ws.onmessage = ev => {
    let m;
    try { m = JSON.parse(ev.data); } catch { return; }
    if (m.type === "status") { renderState(m); pushSample(m.pool || {}); }
    if (m.type === "farm" || m.type === "network") { loadNetworks(); refreshTables(); }
  };
  ws.onerror = () => { if (ws) ws.close(); };
  ws.onclose = () => {
    ws = null;
    startPolling();
    setTimeout(connectLive, wsBackoff);
    wsBackoff = Math.min(wsBackoff * 2, 30000);
  };
}

/* ── TOAST ──────────────────────────────────────────────────── */
function toast(msg) {
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = msg;
  Object.assign(t.style, {
    position: "fixed", bottom: "16px", left: "50%", transform: "translateX(-50%)",
    zIndex: "9999"
  });
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3500);
}

/* ── EXPORT ─────────────────────────────────────────────────── */
function exportWallets(fmt) { window.location.href = "/api/wallets/export?format=" + fmt; }

async function createWallets() {
  const input = $("inWalletCount");
  const n = Math.max(1, Math.min(50, parseInt((input && input.value) || "3", 10) || 3));
  try {
    const d = await j("/api/wallets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ count: n }),
    });
    toast(`Создано кошельков: ${d.created}`);
    await loadWallets();
    await tick();
  } catch (e) { if (e.message !== "unauth") toast("Не удалось создать: " + e.message); }
}

/* ── SCHEDULER ──────────────────────────────────────────────── */
async function loadScheduler() {
  try {
    const d = await j("/api/scheduler");
    const tasks = d.tasks || [];
    const rows = tasks.map(t => {
      const status = t.enabled
        ? '<span class="badge badge-ok">активна</span>'
        : '<span class="badge badge-warn">выкл</span>';
      return `<tr>
        <td class="mono" style="color:var(--text-3)">${esc(t.id)}</td>
        <td>${esc(t.name)}</td>
        <td class="mono">${esc(t.cron)}</td>
        <td class="num">${esc(t.cycles)}</td>
        <td class="mono" style="color:var(--text-2)">${esc(t.last_run || "—")}</td>
        <td class="mono" style="color:var(--text-2)">${esc(t.next_run || "—")}</td>
        <td>${status}</td>
        <td><button class="btn btn-ghost btn-sm" onclick="deleteSchedulerTask(${t.id})"><i class="ti ti-trash"></i></button></td>
      </tr>`;
    }).join("");
    $("tScheduler").innerHTML = rows || '<tr><td colspan="8" style="color:var(--text-3)">задач нет</td></tr>';
  } catch (e) { if (e.message !== "unauth") toast("Планировщик: " + e.message); }
}

async function addSchedulerTask() {
  const name = prompt("Имя задачи:");
  if (!name) return;
  const cron = prompt("Cron-выражение (например, 0 */6 * * *):", "0 */6 * * *");
  if (!cron) return;
  const cycles = parseInt(prompt("Количество циклов:", "10"), 10);
  if (!cycles || isNaN(cycles)) return;
  try {
    await j("/api/scheduler", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, cron, cycles, enabled: true })
    });
    toast("Задача добавлена");
    await loadScheduler();
  } catch (e) { toast("Ошибка: " + e.message); }
}

async function deleteSchedulerTask(id) {
  if (!confirm("Удалить задачу?")) return;
  try {
    await j(`/api/scheduler/${id}`, { method: "DELETE" });
    toast("Задача удалена");
    await loadScheduler();
  } catch (e) { toast("Ошибка: " + e.message); }
}

async function startScheduler() {
  try {
    await j("/api/scheduler/start", { method: "POST" });
    toast("Планировщик запущен");
  } catch (e) { toast("Ошибка: " + e.message); }
}

async function stopScheduler() {
  try {
    await j("/api/scheduler/stop", { method: "POST" });
    toast("Планировщик остановлен");
  } catch (e) { toast("Ошибка: " + e.message); }
}

/* ── GROUPS ─────────────────────────────────────────────────── */
async function loadGroups() {
  try {
    const d = await j("/api/groups");
    const groups = d.groups || [];
    const rows = groups.map(g => {
      const color = g.color || "#a855f7";
      return `<tr>
        <td><span style="display:inline-block;width:16px;height:16px;background:${esc(color)};border-radius:4px"></span></td>
        <td>${esc(g.name)}</td>
        <td class="num">${esc(g.wallet_count || 0)}</td>
        <td class="mono" style="color:var(--text-2)">${esc(String(g.created_at || "").slice(0, 10))}</td>
        <td><button class="btn btn-ghost btn-sm" onclick="deleteGroup(${g.id})"><i class="ti ti-trash"></i></button></td>
      </tr>`;
    }).join("");
    $("tGroups").innerHTML = rows || '<tr><td colspan="5" style="color:var(--text-3)">групп нет</td></tr>';
  } catch (e) { if (e.message !== "unauth") toast("Группы: " + e.message); }
}

async function addGroup() {
  const name = prompt("Имя группы:");
  if (!name) return;
  const color = prompt("Цвет (hex, например #a855f7):", "#a855f7");
  if (!color) return;
  try {
    await j("/api/groups", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, color })
    });
    toast("Группа создана");
    await loadGroups();
  } catch (e) { toast("Ошибка: " + e.message); }
}

async function deleteGroup(id) {
  if (!confirm("Удалить группу?")) return;
  try {
    await j(`/api/groups/${id}`, { method: "DELETE" });
    toast("Группа удалена");
    await loadGroups();
  } catch (e) { toast("Ошибка: " + e.message); }
}

/* ── RPC HEALTH ─────────────────────────────────────────────── */
async function loadRpcHealth() {
  try {
    const d = await j("/api/rpc-health");
    const box = $("rpcHealth");
    if (d.error) {
      box.innerHTML = `<div style="color:var(--red)">Ошибка: ${esc(d.error)}</div>`;
      return;
    }
    // /api/rpc-health отдаёт nodes списком нод, active — строкой с URL,
    // а latency/avg_ms могут быть null, когда замеров ещё нет.
    const nodes = Array.isArray(d.nodes) ? d.nodes : [];
    const live = nodes.filter(n => !n.dead);
    const avg = typeof d.avg_ms === "number" ? d.avg_ms : (live.length
      ? live.reduce((s, n) => s + (Number(n.latency_ms) || 0), 0) / live.length : null);
    const lat = typeof d.latency === "number" ? d.latency : avg;
    const activeUrl = typeof d.active === "string" ? d.active : null;
    const cb = String(d.cb_state || "—");
    const cbBadge = cb === "CLOSED" ? "badge-ok" : cb === "OPEN" ? "badge-err" : "badge-warn";

    const nodeRows = nodes.length ? nodes.map(n => {
      const isActive = activeUrl && n.url === activeUrl;
      const dead = !!n.dead;
      const badge = dead
        ? '<span class="badge badge-err">offline</span>'
        : (n.errors || 0) > 0
          ? `<span class="badge badge-warn">деградация</span>`
          : '<span class="badge badge-ok">живая</span>';
      return `<tr${isActive ? ' class="row-active"' : ""}>
        <td class="mono">${esc(n.url)}${isActive ? ' <span class="badge badge-info">active</span>' : ""}</td>
        <td class="num">${n.latency_ms == null ? "—" : Math.round(n.latency_ms)}</td>
        <td class="num">${n.score == null ? "—" : Math.round(n.score)}</td>
        <td class="num">${esc(n.errors || 0)}</td>
        <td>${badge}</td>
      </tr>`;
    }).join("") : '<tr><td colspan="5" style="color:var(--text-3)">нод нет</td></tr>';

    box.innerHTML = `
      <div class="rpc-active">
        <span class="status-dot ${activeUrl ? "on" : "off"}"></span>
        <span class="rpc-active-label">Активная нода</span>
        <span class="mono">${esc(activeUrl || "нет активной")}</span>
      </div>
      <div class="metrics-grid">
        <div class="metric-card">
          <div class="metric-label">Задержка</div>
          <div class="metric-value">${lat == null ? "—" : Math.round(lat)}</div>
          <div class="metric-sub">${lat == null ? "нет замеров" : "мс"}</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">Ошибки</div>
          <div class="metric-value">${fmt(d.errors || 0)}</div>
          <div class="metric-sub">${fmt(d.calls || 0)} вызовов</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">Circuit breaker</div>
          <div class="metric-value"><span class="badge ${cbBadge}">${esc(cb)}</span></div>
          <div class="metric-sub">${fmt(d.cb_failures || 0)} сбоев подряд</div>
        </div>
      </div>
      <table>
        <thead><tr><th>Нода</th><th>Задержка, мс</th><th>Скор</th><th>Ошибки</th><th>Статус</th></tr></thead>
        <tbody>${nodeRows}</tbody>
      </table>
      <div class="rpc-meta">
        <span>chain <b class="mono">${esc(d.chain_id ?? "—")}</b></span>
        <span>RPC/мин <b class="mono">${d.rpc_rate != null ? Number(d.rpc_rate).toFixed(2) : "—"}</b></span>
        <span>живых нод <b class="mono">${live.length}/${nodes.length}</b></span>
      </div>`;
  } catch (e) { if (e.message !== "unauth") toast("RPC: " + e.message); }
}

/* ── BALANCE ALERTS ─────────────────────────────────────────── */
async function loadBalanceAlerts() {
  try {
    const d = await j("/api/balance-alerts");
    $("chkBalanceAlerts").checked = d.enabled || false;
    $("inpBalanceThreshold").value = d.threshold || 0.001;
  } catch (e) { if (e.message !== "unauth") toast("Алерты: " + e.message); }
}

async function saveBalanceAlerts() {
  const enabled = $("chkBalanceAlerts").checked;
  const threshold = parseFloat($("inpBalanceThreshold").value);
  if (isNaN(threshold) || threshold < 0) {
    toast("Неверный порог");
    return;
  }
  try {
    await j("/api/balance-alerts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled, threshold })
    });
    toast("Настройки сохранены");
  } catch (e) { toast("Ошибка: " + e.message); }
}

/* ── INIT ───────────────────────────────────────────────────── */
async function init() {
  const me = await j("/api/me");
  if ($("who")) $("who").textContent = me.name || "—";

  const tg = window.Telegram?.WebApp;
  if (tg) { tg.ready(); tg.expand(); }

  document.querySelectorAll(".nav-item[data-nav]").forEach(el =>
    el.addEventListener("click", () => nav(el.getAttribute("data-nav"))));

  // Canvas'ы размерами в device pixels: без перерисовки после resize линии
  // растягиваются. Дебаунс, чтобы не рисовать на каждый пиксель перетаскивания.
  let resizeTimer = 0;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(drawCharts, 150);
  });

  $("navLogout").onclick = () => j("/api/logout", { method: "POST" }).then(() => location.href = "/login");
  $("btnNetStart").onclick = () => { const n = selectedNet(); if (n) netAction(n.slug, "start"); };
  $("btnNetStop").onclick = () => { const n = selectedNet(); if (n) netAction(n.slug, "stop"); };
  $("netSelect").onchange = renderNetMeta;
  $("btnExportCsv").onclick = () => exportWallets("csv");
  $("btnExportJson").onclick = () => exportWallets("json");
  if ($("btnWalletCreate")) $("btnWalletCreate").onclick = createWallets;

  if ($("btnSchedStart")) $("btnSchedStart").onclick = startScheduler;
  if ($("btnSchedStop")) $("btnSchedStop").onclick = stopScheduler;
  if ($("btnSchedAdd")) $("btnSchedAdd").onclick = addSchedulerTask;

  if ($("btnGroupAdd")) $("btnGroupAdd").onclick = addGroup;

  if ($("btnRpcRefresh")) $("btnRpcRefresh").onclick = loadRpcHealth;

  if ($("btnSaveAlerts")) $("btnSaveAlerts").onclick = saveBalanceAlerts;

  try {
    const links = await j("/api/links");
    $("linksTitle").textContent = links.title || "Ссылки";
    const list = $("links");
    const items = links.items || [];
    if (!items.length) {
      list.innerHTML = '<span style="color:var(--text-3)">Ссылки появятся позже</span>';
    } else {
      list.innerHTML = items
        .filter(l => /^https?:\/\//i.test(String(l.url || "")))
        .map(l => `<a class="link-item" href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.label || l.url)}</a>`)
        .join("");
    }
  } catch {}

  await loadNetworks();
  await loadMetrics();
  await tick();
  connectLive();
  setInterval(refreshTables, tablesRefreshMs);
}

init().catch(e => console.error(e));
