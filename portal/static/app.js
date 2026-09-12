/* HARVEST PORTAL frontend: статистика, старт/стоп, история, ссылки. */

const $ = (id) => document.getElementById(id);
let actionsSeries = [];
let stoppingRequested = false;

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[c]);
}

async function j(url, opts) {
  const r = await fetch(url, opts);
  const body = await r.json().catch(() => ({}));
  if (r.status === 401) { location.href = "/login"; throw new Error("unauth"); }
  if (!r.ok) throw new Error(body.error || "HTTP " + r.status);
  return body;
}

async function init() {
  const me = await j("/api/me");
  $("who").textContent = me.name || " —";

  const tg = window.Telegram?.WebApp;
  if (tg) { tg.ready(); tg.expand(); document.body.style.background = tg.colorScheme === "dark" ? "" : "#0d1322"; }

  $("btnStart").onclick = () => doAction("start");
  $("btnStop").onclick = () => doAction("stop");
  $("btnPause").onclick = () => doAction("pause");
  $("btnResume").onclick = () => doAction("resume");
  $("btnOut").onclick = () => j("/api/logout", { method: "POST" }).then(() => location.href = "/login");

  $("linksTitle").textContent = "Ссылки";
  const links = await j("/api/links");
  $("linksTitle").textContent = links.title || "Наши ссылки";
  const list = $("links");
  list.textContent = "";
  const items = links.items || [];
  if (!items.length) {
    list.append(Object.assign(document.createElement("span"), { className: "muted", textContent: "Ссылки появятся позже" }));
  } else {
    for (const l of items) {
      const url = String(l.url || "");
      if (!/^https?:\/\//i.test(url)) continue; // http/https только
      const a = document.createElement("a");
      a.href = url; a.target = "_blank"; a.rel = "noopener";
      a.className = "mt";
      a.textContent = String(l.label || url);
      list.append(a);
    }
  }

  setInterval(tick, 4000);
  tick();
}

async function doAction(action) {
  const btns = ["btnStart", "btnStop", "btnPause", "btnResume"];
  btns.forEach((id) => { $(id).disabled = true; });
  try {
    const r = await j("/api/farm/" + action, { method: "POST" });
    if (r.stopping) {
      stoppingRequested = true;
      $("statusText").textContent = "Останавливаемся…";
    }
    await tick();
  } catch (e) { toast("Не удалось: " + e.message); }
  finally {
    btns.forEach((id) => { $(id).disabled = false; });
  }
}

async function tick() {
  const s = await j("/api/stats");
  const pool = s.pool || {};
  $("cActions").textContent = pool.actions ?? 0;
  $("cErrors").textContent = pool.errors ?? 0;
  $("cProcessed").textContent = pool.processed ?? 0;
  $("cCycles").textContent = pool.cycles ?? 0;
  $("cWorkers").textContent = pool.dyn_workers != null ? `${pool.dyn_workers}` : "—";

  const dot = $("dot"), st = $("statusText");
  if (!s.running) {
    stoppingRequested = false;
    dot.className = "dot off"; st.textContent = "Ферма остановлена";
  }
  else if (stoppingRequested) { dot.className = "dot pau"; st.textContent = "Останавливаемся…"; }
  else if (pool.paused) { dot.className = "dot pau"; st.textContent = "Пауза"; }
  else { dot.className = "dot on"; st.textContent = "Работает"; }
  $("healthText").textContent = "· health " + (s.health_factor ?? 1).toFixed(2);

  actionsSeries.push(pool.actions ?? 0);
  if (actionsSeries.length > 60) actionsSeries.shift();
  drawChart(actionsSeries);

  await loadHistory();
  await loadWallets();
}

async function loadHistory() {
  try {
    const d = await j("/api/cycle-history");
    const rows = (d.history || []).map(c => {
      return `<tr><td>${esc(c.id)}</td><td>${esc(c.actions_ok)}</td><td>${esc(c.errors)}</td><td>${esc(c.duration_s)}</td><td>${esc(c.wallets)}</td></tr>`;
    }).join("");
    $("tHistory").innerHTML = rows || '<tr><td colspan="5" class="muted">циклов пока нет</td></tr>';
  } catch (e) { /* не критично */ }
}

async function loadWallets() {
  try {
    const d = await j("/api/top-wallets");
    const rows = (d.wallets || []).map((w, i) =>
      `<tr><td>${i + 1}</td><td>${esc(w.address)}</td><td>${esc(w.actions)}</td></tr>`).join("");
    $("tWallets").innerHTML = rows || '<tr><td colspan="3" class="muted">кошельков нет — создайте через CLI</td></tr>';
  } catch (e) { /* не критично */ }
}

function drawChart(series) {
  const cv = $("chart"), dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth * dpr, h = cv.clientHeight * dpr;
  if (cv.width !== w) cv.width = w;
  if (cv.height !== h) cv.height = h;
  const ctx = cv.getContext("2d");
  ctx.clearRect(0, 0, w, h);
  if (series.length < 2) return;
  const max = Math.max(...series, 1), min = Math.min(...series, 0);
  const pad = 4;
  const x = (i) => pad + (i / (series.length - 1)) * (w - pad * 2);
  const y = (v) => h - pad - ((v - min) / Math.max(max - min, 1e-9)) * (h - pad * 2);

  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, "rgba(25,227,255,.35)");
  grad.addColorStop(1, "rgba(25,227,255,0)");
  ctx.beginPath();
  series.forEach((v, i) => i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v)));
  ctx.strokeStyle = "#19e3ff"; ctx.lineWidth = 2; ctx.stroke();
  ctx.lineTo(x(series.length - 1), h); ctx.lineTo(x(0), h); ctx.closePath();
  ctx.fillStyle = grad; ctx.fill();
}

function toast(msg) {
  const t = document.createElement("div");
  t.className = "toast"; t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 4000);
}

init().catch(e => console.error(e));