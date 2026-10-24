/* harvest Mini App — Поле внутри Telegram. Без фреймворков.
   Глифы сетки — те же iwoer, что в portal/wallet_cells.py. */
(function () {
  "use strict";
  var tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  var $ = function (id) { return document.getElementById(id); };
  var screen = $("screen"), tabs = $("tabs"), gate = $("gate"), toastBox = $("toast");
  var state = null, etag = null, pollTimer = 0, tab = "field", nets = [], presets = null;
  var histRows = [];
  var wiz = { step: 1, net: "vibevibe", wallets: 50, cycles: 5, profile: "normal", faucet: true };
  var GLYPH = { i: "·", w: "■", o: "▣", e: "x", r: "~" };
  var lang = "ru";
  var T = {
    openFromBot: { ru: "Открой из бота — здесь нет данных.", en: "Open from the bot — no data here." },
    cdnFail: { ru: "Не загрузился Telegram SDK (сеть/CDN). Проверь соединение и обнови.", en: "Telegram SDK failed to load (network/CDN). Check connection and retry." },
    expired: { ru: "Сессия устарела — закрой и открой из бота.", en: "Session expired — reopen from the bot." },
    retry: { ru: "Повторить", en: "Retry" },
    stopped: { ru: "Стенд остановлен. Поле ждёт запуска.", en: "Rig stopped. Field awaits launch." },
    wallets: { ru: "кошельки", en: "wallets" },
    cycle: { ru: "цикл", en: "cycle" },
    errors: { ru: "ошибки", en: "errors" },
    health: { ru: "health", en: "health" },
    workers: { ru: "workers", en: "workers" },
    updated: { ru: "обновлено", en: "updated" },
    refresh: { ru: "Обновить", en: "Refresh" },
    start: { ru: "ЗАПУСК", en: "START" },
    pause: { ru: "ПАУЗА", en: "PAUSE" },
    resume: { ru: "ПРОДОЛЖИТЬ", en: "RESUME" },
    stop: { ru: "СТОП", en: "STOP" },
    empty: { ru: "пусто", en: "empty" },
    netErr: { ru: "Ошибка сети", en: "Network error" },
    keysNote: { ru: "Файл содержит приватные ключи. Только тестнет.", en: "File holds private keys. Testnet only." },
    keysAsk: { ru: "Файл содержит приватные ключи. Продолжить?", en: "File holds private keys. Continue?" },
    encPass: { ru: "Пароль для .enc (мин. 4 символа):", en: "Password for .enc (min 4 chars):" },
    badPass: { ru: "слабый пароль", en: "bad password" },
    sentChat: { ru: "Файл отправлен в чат с ботом", en: "File sent to the bot chat" },
    step: { ru: "Шаг", en: "Step" },
    walletsN: { ru: "Кошельки", en: "Wallets" },
    cyclesN: { ru: "Циклы", en: "Cycles" },
    profNote: { ru: "Профиль (быстрый = --fast: меньше задержек, заметнее)", en: "Profile (fast = --fast: fewer delays, more visible)" },
    withTap: { ru: "С краном", en: "With faucet" },
    withoutTap: { ru: "Без крана", en: "Without faucet" },
    toField: { ru: "Запустить выбранную сеть", en: "Launch selected network" },
    toFieldNote: { ru: "Запуск: switch + start на сервере.", en: "Launch: switch + start on the server." },
    netsFail: { ru: "Сети не загрузились", en: "Networks failed to load" },
    hintNovice: { ru: "кошельки — ваши адреса · health — живость сети", en: "wallets are your addresses · health is net liveness" },
    tap: { ru: "кран", en: "faucet" },
    rpcShort: { ru: "rpc", en: "rpc" },
    alive: { ru: "жив", en: "alive" },
    dry: { ru: "сухой", en: "dry" },
    dripBtn: { ru: "Пополнить", en: "Top up" },
    dripAsk: { ru: "Долить все кошельки с крана? Квота тратится по-настоящему.", en: "Top up all wallets from the faucet? Quota is spent for real." },
    dripRun: { ru: "Долив запущен фоном", en: "Top-up started in background" },
    dripBusy: { ru: "Долив уже идёт", en: "Top-up already running" },
    stopAsk: { ru: "Остановить прогон?", en: "Stop the run?" },
    nextBtn: { ru: "ДАЛЕЕ", en: "NEXT" },
    doneBtn: { ru: "ГОТОВО", en: "DONE" },
    yamlSent: { ru: "YAML отправлен в чат с ботом", en: "YAML sent to the bot chat" },
    copyBtn: { ru: "Скопировать", en: "Copy" },
    yamlFile: { ru: "YAML файлом", en: "YAML as file" },
    verifyBtn: { ru: "Проверить", en: "Verify" },
    schedTitle: { ru: "Расписание", en: "Schedule" },
    tasksN: { ru: "задач", en: "tasks" },
    schedFail: { ru: "Расписание недоступно", en: "Schedule unavailable" },
    alertsTitle: { ru: "Алерты", en: "Alerts" },
    onOff: { ru: ["выкл", "вкл"], en: ["off", "on"] },
    thr: { ru: "порог", en: "threshold" },
    offBtn: { ru: "Выкл", en: "Off" },
    modeBtn: { ru: "Режим", en: "Mode" },
    langBtn: { ru: "Язык", en: "Lang" },
    backBtn: { ru: "Назад", en: "Back" },
    netSwitched: { ru: "Сеть переключена", en: "Network switched" },
    pnlTitle: { ru: "PnL прогона", en: "Run PnL" },
    pnlOk: { ru: "успешных", en: "ok" },
    pnlFail: { ru: "отказов", en: "failed" },
    pnlFarm: { ru: "Δ прогона", en: "run Δ" },
  };
  function tr(key, idx) {
    var e = T[key];
    if (!e) return key;
    var v = e[lang] || e.ru;
    if (Array.isArray(v)) return v[idx === undefined ? 0 : idx] ?? v[0];
    return v;
  }

  function toast(msg) {
    toastBox.textContent = msg;
    toastBox.hidden = false;
    setTimeout(function () { toastBox.hidden = true; }, 3500);
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function haptic(kind) {
    try {
      if (tg && tg.HapticFeedback) {
        if (kind === "ok") tg.HapticFeedback.notificationOccurred("success");
        else if (kind === "err") tg.HapticFeedback.notificationOccurred("error");
        else tg.HapticFeedback.impactOccurred("light");
      }
    } catch (e) { /* молчаливая деградация */ }
  }
  function atLeast(v) {
    try { return tg && tg.isVersionAtLeast ? tg.isVersionAtLeast(v) : false; }
    catch (e) { return false; }
  }

  async function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({ "Authorization": "tma " + tg.initData }, opts.headers || {});
    var r = await fetch(path, opts);
    if (r.status === 401) { showExpired(); throw new Error("expired"); }
    if (r.status === 304) return { _cached: true };
    var body = await r.json().catch(function () { return {}; });
    if (!r.ok) throw new Error(body.error || ("HTTP " + r.status));
    return body;
  }
  function jpost(path, data) {
    return api(path, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data) });
  }

  function showGate(html) { gate.innerHTML = html; gate.hidden = false; }
  function hideGate() { gate.hidden = true; gate.innerHTML = ""; }
  function showExpired() {
    stopPoll();
    screen.innerHTML = "";
    tabs.hidden = true;
    setMain(null);
    showGate("<p>" + esc(tr("expired")) + "</p>");
  }

  /* ---------- тема + вьюпорт из Telegram ---------- */
  function applyTheme() {
    try {
      var root = document.querySelector(".tma");
      if (!root) return;
      var scheme = "";
      try { scheme = (tg && tg.colorScheme) || ""; } catch (e) {}
      scheme = String(scheme).toLowerCase();
      if (scheme !== "light" && scheme !== "dark") {
        try { scheme = window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"; }
        catch (e) { scheme = "dark"; }
      }
      root.setAttribute("data-tma-theme", scheme);
      try {
        var bg = (tg && tg.themeParams && tg.themeParams.bg_color) || (scheme === "light" ? "#ffffff" : "#05060a");
        if (tg && tg.setBackgroundColor) tg.setBackgroundColor(bg);
      } catch (e) { /* молча */ }
    } catch (e) { /* молча: остаётся тёмная */ }
  }
  function syncViewport() {
    try {
      var h = 0;
      if (tg) h = tg.viewportHeight || tg.viewportStableHeight || 0;
      if (!h) h = window.innerHeight || 0;
      if (h > 0) document.documentElement.style.setProperty("--tma-vh", (h / 100) + "px");
    } catch (e) { /* молча */ }
  }
  /* ---------- назад: TG BackButton или ряд в интерфейсе ---------- */
  function backRow() {
    var canTG = tg && tg.BackButton && atLeast("6.1");
    if (canTG) return "";
    return '<div class="tma-backrow"><button class="tma-btn" data-go="field" aria-label="' + esc(tr("backBtn")) + '">←</button></div>';
  }
  function syncBack(show) {
    try {
      if (tg && tg.BackButton && atLeast("6.1")) {
        if (show) { tg.BackButton.show(); } else { tg.BackButton.hide(); }
      }
    } catch (e) { /* молча */ }
  }
  if (tg && tg.BackButton) {
    try { tg.BackButton.onClick(function () { go("field"); }); } catch (e) { /* молча */ }
  }

  /* ---------- MainButton ---------- */
  var _mainHandler = null;
  function setMain(btn) {
    try {
      if (!tg || !tg.MainButton) return;
      if (!btn) { tg.MainButton.hide(); return; }
      tg.MainButton.setText(btn.text);
      if (_mainHandler) { try { tg.MainButton.offClick(_mainHandler); } catch (e) {} }
      _mainHandler = btn.onClick;
      tg.MainButton.onClick(btn.onClick);
      tg.MainButton.show();
    } catch (e) { /* молча: обычная кнопка в интерфейсе вместо MainButton */ }
  }

  /* ---------- поле ---------- */
  var prevCells = "";
  function gridHtml(cells, total) {
    cells = (cells || "").slice(0, 400);
    var counts = { i: 0, w: 0, o: 0, e: 0, r: 0 };
    var out = "";
    for (var i = 0; i < cells.length; i++) {
      var c = cells[i];
      if (counts[c] !== undefined) counts[c]++;
      var flash = prevCells && prevCells[i] && prevCells[i] !== c ? " flash" : "";
      // Один таб-стоп на сетку (как в docs): клетки скрыты от SR,
      // сводка — в aria-label контейнера.
      out += '<span class="c c-' + c + flash + '" aria-hidden="true">' +
        (GLYPH[c] || "·") + "</span>";
    }
    if (!out) out = '<span class="c c-i" aria-hidden="true">·</span>';
    prevCells = cells;
    var summary = cells.length + " wallets" +
      " (ok " + counts.o + ", work " + counts.w + ", err " + counts.e + ")" +
      ((total || 0) > cells.length ? ", +" + ((total || 0) - cells.length) + " more" : "");
    var grid = '<div class="tma-grid" role="img" aria-label="' + esc(summary) + '" tabindex="0">' + out + "</div>";
    if ((total || 0) > cells.length) grid += "<p>+N: +" + ((total || 0) - cells.length) + "</p>";
    return grid;
  }
  function mark(s) {
    if (s.running && s.paused) return "[PAUSE]";
    return s.running ? "[RUN]" : "[STOP]";
  }
  function renderField(s) {
    lang = s.lang || lang;
    $("tmaSub").textContent = mark(s) + " " + (s.network || "—") + " · " + (s.chain_id || "—");
    var h = '<div class="tma-card"><div class="tma-kv"><span>' + esc(tr("wallets")) + '</span><b>' + esc(s.wallet_count) +
      '</b></div><div class="tma-kv"><span>' + esc(tr("cycle")) + '</span><b>' + esc(s.cycle) + " / ∞</b></div>" +
      '<div class="tma-kv"><span>' + esc(tr("errors")) + '</span><b>' + esc(s.errors) + "</b></div>" +
      '<div class="tma-kv"><span>' + esc(tr("health")) + '</span><b>' + Number(s.health_factor || 0).toFixed(2) + "</b></div>";
    if (s.mode === "pro") {
      h += '<div class="tma-kv"><span>workers</span><b>' + esc(s.workers) + "</b></div>";
    } else {
      h += "<p>" + esc(tr("hintNovice")) + "</p>";
    }
    h += '<div class="tma-kv"><span>' + esc(tr("tap")) + '</span><b id="tmaFaucet">…</b></div>' +
      '<div class="tma-kv"><span>' + esc(tr("rpcShort")) + '</span><b id="tmaRpc">…</b></div>';
    h += "</div>" + gridHtml(s.cells, s.cells_total) +
      '<p>' + esc(tr("updated")) + ' ' + esc(s.updated_at) + "</p>";
    // PnL последнего прогона (агрегаты, без адресов кошельков).
    if (s.pnl && typeof s.pnl === "object") {
      var farm = Number(s.pnl.farm_delta);
      var dFarm = isFinite(farm) ? (farm > 0 ? "+" : "") + farm.toFixed(6) : "—";
      h += '<div class="tma-card"><div class="tma-kv"><span>' + esc(tr("pnlTitle")) + "</span><b>" +
        esc(s.pnl.actions_ok || 0) + " " + esc(tr("pnlOk")) + " · " +
        esc(s.pnl.actions_fail || 0) + " " + esc(tr("pnlFail")) + "</b></div>" +
        '<div class="tma-kv"><span>' + esc(tr("pnlFarm")) + "</span><b>" + esc(dFarm) + "</b></div></div>";
    }
    if (!s.running) h += '<div class="tma-card">' + esc(tr("stopped")) + "</div>";
    h += '<div class="tma-row"><button class="tma-btn" data-act="refresh">' + esc(tr("refresh")) + "</button>" +
      '<button class="tma-btn" data-act="drip">' + esc(tr("dripBtn")) + "</button>" +
      (s.running && !s.paused
        ? '<button class="tma-btn" data-farm="pause">' + esc(tr("pause")) + '</button><button class="tma-btn" data-farm="stop">' + esc(tr("stop")) + "</button>"
        : s.running && s.paused
          ? '<button class="tma-btn" data-farm="resume">' + esc(tr("resume")) + '</button><button class="tma-btn" data-farm="stop">' + esc(tr("stop")) + "</button>"
          : '<button class="tma-btn" data-farm="start">' + esc(tr("start")) + "</button>") + "</div>";
    screen.innerHTML = h;
    if (s.running && !s.paused) setMain({ text: tr("pause"), onClick: function () { farm("pause"); } });
    else if (s.running && s.paused) setMain({ text: tr("resume"), onClick: function () { farm("resume"); } });
    else setMain({ text: tr("start"), onClick: function () { farm("start"); } });
    // Кран/RPC подтягиваем отдельно (не блокируем поле).
    api("/api/miniapp/faucet").then(function (b) {
      var el = $("tmaFaucet");
      if (el) el.textContent = (b.live > 0 ? tr("alive") + " (" + b.live + ")" : tr("dry") + " (0)");
    }).catch(function () { var el = $("tmaFaucet"); if (el) el.textContent = "—"; });
    api("/api/miniapp/rpc").then(function (b) {
      var el = $("tmaRpc");
      if (!el) return;
      var nodes = (b && b.nodes) || [];
      if (!nodes.length) { el.textContent = "—"; return; }
      var bad = nodes.filter(function (n) { return n.ok === false; }).length;
      el.textContent = bad ? bad + " err / " + nodes.length : "ok " + nodes.length;
    }).catch(function () { var el = $("tmaRpc"); if (el) el.textContent = "—"; });
  }
  async function farm(action) {
    if (action === "stop") {
      var ok = false;
      try {
        if (tg && tg.showConfirm) ok = await new Promise(function (res) {
          tg.showConfirm(tr("stopAsk"), res);
        });
        else ok = window.confirm(tr("stopAsk"));
      } catch (e) { ok = false; }
      if (!ok) return;
    }
    setProgress(true);
    try {
      await jpost("/api/miniapp/farm", { action: action });
      haptic("ok");
      await loadState(true);
    } catch (e) { haptic("err"); toast(String(e.message || e)); }
    setProgress(false);
  }
  function setProgress(on) {
    try {
      if (tg && tg.MainButton) { if (on) tg.MainButton.showProgress(); else tg.MainButton.hideProgress(); }
    } catch (e) { /* молча */ }
  }

  /* ---------- сеть и масштаб (3 шага) ---------- */
  function renderNets() {
    var h = backRow() + '<div class="tma-card"><p>' + esc(tr("step")) + " " + wiz.step + "/3</p>";
    if (wiz.step === 1) {
      h += nets.map(function (n) {
        return '<button class="tma-btn" data-net="' + esc(n.slug) + '">' + esc(n.name) + "</button>";
      }).join("") + (nets.length
        ? '<div class="tma-row"><button class="tma-btn" data-netgo="1">' + esc(tr("toField")) + "</button></div>" +
          "<p>" + esc(tr("toFieldNote")) + "</p>"
        : "<p>" + esc(tr("netsFail")) + ' — <button class="tma-btn" data-go="nets">' + esc(tr("retry")) + "</button></p>");
    } else if (wiz.step === 2) {
      h += "<p>" + esc(tr("walletsN")) + "</p>" + presets.wallets.map(function (w) {
        return '<button class="tma-btn" data-w="' + w + '">' + w + "</button>";
      }).join("") + "<p>" + esc(tr("cyclesN")) + "</p>" + presets.cycles.map(function (c) {
        return '<button class="tma-btn" data-c="' + c + '">' + (c === 0 ? "∞" : c) + "</button>";
      }).join("");
    } else {
      h += "<p>" + esc(tr("profNote")) + "</p>" + presets.profiles.map(function (p) {
        return '<button class="tma-btn" data-p="' + p + '">' + p + "</button>";
      }).join("") +
        '<div class="tma-row"><button class="tma-btn" data-f="1">' + esc(tr("withTap")) + "</button>" +
        '<button class="tma-btn" data-f="0">' + esc(tr("withoutTap")) + "</button></div>";
    }
    screen.innerHTML = h + "</div>";
    setMain(wiz.step < 3
      ? { text: tr("nextBtn"), onClick: function () { wiz.step = Math.min(3, wiz.step + 1); renderNets(); } }
      : null);
  }
  async function buildCmd(sendYaml) {
    setProgress(true);
    try {
      var body = await jpost("/api/miniapp/command", {
        kind: sendYaml ? "cfg_send" : "cmd", net: wiz.net, wallets: wiz.wallets,
        cycles: wiz.cycles, profile: wiz.profile, faucet: wiz.faucet });
      if (sendYaml) { toast(tr("yamlSent")); haptic("ok"); return; }
      screen.innerHTML = backRow() + '<div class="tma-card"><pre id="cmdPre">' +
        esc(body.cmd) + '</pre><div class="tma-row"><button class="tma-btn" data-act="copy">' +
        esc(tr("copyBtn")) + "</button>" + '<button class="tma-btn" data-act="yamlsend">' + esc(tr("yamlFile")) + "</button>" +
        '<button class="tma-btn" data-act="verify">' + esc(tr("verifyBtn")) + "</button></div><div id=\"verifyOut\"></div></div>";
      haptic("ok");
    } catch (e) { haptic("err"); toast(String(e.message || e)); }
    setProgress(false);
  }

  /* ---------- история ---------- */
  function spark(vals) {
    var bars = "▁▂▃▄▅▆▇█", lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    if (hi <= lo) return bars[0].repeat(vals.length);
    return vals.map(function (v) {
      return bars[Math.max(0, Math.min(7, Math.round((v - lo) / (hi - lo) * 7)))];
    }).join("");
  }
  async function renderHistory() {
    screen.innerHTML = backRow() + '<div class="tma-card"><div class="skel"></div></div>';
    try {
      var body = await api("/api/miniapp/history?limit=20");
      histRows = body.history || [];
      var rows = histRows.map(function (c) {
        return "<tr><td>#" + esc(c.id) + "</td><td>" + esc(c.actions_ok) + "</td><td>" +
          esc(c.errors) + "</td><td>" + esc(c.wallets) + "</td></tr>";
      }).join("") || '<tr><td colspan="4">—</td></tr>';
      var sp = spark(histRows.map(function (c) { return c.actions_ok || 0; }));
      screen.innerHTML = backRow() + '<div class="tma-card"><svg width="100%" height="36">' +
        "<text x='0' y='24' font-size='20' fill='currentColor'>" + esc(sp) + "</text></svg>" +
        '<table><tr><th>#</th><th>OK</th><th>err</th><th>wal</th></tr>' + rows + "</table>" +
        '<div class="tma-row"><button class="tma-btn" data-act="histcsv">CSV</button></div></div>';
    } catch (e) {
      screen.innerHTML = backRow() + '<div class="tma-card"><p>' + esc(tr("netErr")) + "</p>" +
        '<button class="tma-btn" data-go="history">' + esc(tr("retry")) + "</button></div>";
    }
    setMain(null);
  }
  function histCsv() {
    if (!histRows.length) { toast(tr("empty")); return; }
    var csv = "id,actions_ok,errors,wallets,duration_s,started_at\n" + histRows.map(function (c) {
      return [c.id, c.actions_ok, c.errors, c.wallets, c.duration_s, c.started_at].join(",");
    }).join("\n");
    var b = new Blob([csv], { type: "text/csv" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(b);
    a.download = "harvest_history.csv";
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 2000);
  }

  /* ---------- опасная зона ---------- */
  async function renderDanger() {
    var s = state || {};
    var sched = "";
    try {
      var sc = await api("/api/miniapp/scheduler");
      var tasks = sc.tasks || [];
      sched = '<div class="tma-card"><p>' + esc(tr("schedTitle")) + ": " + esc(tasks.length) + " " + esc(tr("tasksN")) + "</p>" +
        tasks.slice(0, 5).map(function (t) {
          return "<p><code>" + esc(t.task_id || t.id || "?") + "</code> " +
            esc(t.name || "") + " → " + esc(t.next_run || "—") + "</p>";
        }).join("") + "</div>";
    } catch (e) { sched = '<div class="tma-card"><p>' + esc(tr("schedFail")) + "</p></div>"; }
    var alerts = "";
    try {
      var al = await api("/api/miniapp/alerts");
      alerts = '<div class="tma-card"><p>' + esc(tr("alertsTitle")) + ": " + esc(tr("onOff", al.enabled ? 1 : 0)) +
        " · " + esc(tr("thr")) + " " + esc(al.threshold) + '</p><div class="tma-row">' +
        '<button class="tma-btn" data-alert="0.01">0.01</button>' +
        '<button class="tma-btn" data-alert="0.001">0.001</button>' +
        '<button class="tma-btn" data-alert="off">' + esc(tr("offBtn")) + "</button></div></div>";
    } catch (e) { alerts = ""; }
    screen.innerHTML = backRow() +
      '<div class="tma-card"><p>' + esc(tr("keysNote")) + '</p>' +
      '<div class="tma-row"><button class="tma-btn" data-exp="csv">CSV</button>' +
      '<button class="tma-btn" data-exp="json">JSON</button></div>' +
      '<div class="tma-row"><button class="tma-btn" data-exp="enc-csv">CSV 🔒</button>' +
      '<button class="tma-btn" data-exp="enc-json">JSON 🔒</button></div></div>' + sched + alerts +
      '<div class="tma-card"><div class="tma-row"><button class="tma-btn" data-mode="1">' + esc(tr("modeBtn")) + ": " +
      esc(s.mode || "novice") + '</button><button class="tma-btn" data-lang="1">' + esc(tr("langBtn")) + ": " +
      esc((s.lang || "ru").toUpperCase()) + "</button></div></div>";
    setMain(null);
  }
  async function doExport(fmt) {
    var confirmed = false;
    try {
      if (tg && tg.showConfirm) confirmed = await new Promise(function (res) {
        tg.showConfirm(tr("keysAsk"), res);
      });
      else confirmed = window.confirm(tr("keysAsk"));
    } catch (e) { confirmed = false; }
    if (!confirmed) return;
    var password = null;
    if (fmt.indexOf("enc-") === 0) {
      try {
        password = window.prompt(tr("encPass"), "");
      } catch (e) { password = null; }
      if (!password || password.length < 4) { toast(tr("badPass")); return; }
    }
    setProgress(true);
    try {
      var tok = await jpost("/api/miniapp/export/token", { format: fmt });
      var req = { format: fmt, confirm_token: tok.confirm_token };
      if (password !== null) req.password = password;
      password = null;
      await jpost("/api/miniapp/export/request", req);
      haptic("ok");
      toast(tr("sentChat"));
    } catch (e) { haptic("err"); toast(String(e.message || e)); }
    setProgress(false);
  }

  /* ---------- навигация ---------- */
  function go(name) {
    tab = name;
    var btns = tabs.querySelectorAll("button");
    for (var i = 0; i < btns.length; i++) {
      var on = btns[i].getAttribute("data-tab") === name;
      btns[i].classList.toggle("on", on);
      btns[i].setAttribute("aria-selected", on ? "true" : "false");
    }
    syncBack(name !== "field");
    haptic("tap");
    if (name === "field") loadState(true);
    else if (name === "nets") renderNets();
    else if (name === "history") renderHistory();
    else if (name === "danger") renderDanger();
  }

  /* ---------- состояние: опрос 5с + ETag ---------- */
  async function loadState(force) {
    try {
      var headers = { "Authorization": "tma " + tg.initData };
      if (!force && etag) headers["If-None-Match"] = etag;
      var r = await fetch("/api/miniapp/state", { headers: headers });
      if (r.status === 401) { showExpired(); return; }
      if (r.status === 304) return;
      var t = r.headers.get("ETag");
      if (t) etag = t;
      state = await r.json();
      lang = state.lang || lang;
      if (tab === "field" || force) renderField(state);
    } catch (e) {
      if (tab === "field") {
        screen.innerHTML = '<div class="tma-card"><p>' + esc(tr("netErr")) + "</p>" +
          '<button class="tma-btn" data-act="refresh">' + esc(tr("retry")) + "</button></div>";
      }
    }
  }
  function startPoll() {
    stopPoll();
    pollTimer = setInterval(function () {
      if (!document.hidden && tab === "field") loadState(false);
    }, 5000);
  }
  function stopPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = 0; } }
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) return;
    if (tab === "field") loadState(true);
  });

  /* ---------- клики (делегирование, без hover) ---------- */
  function copyText(txt, okMsg) {
    function done() { toast(okMsg || "ok"); }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(txt).then(done, function () { legacyCopy(txt) && done(); });
      return;
    }
    legacyCopy(txt) && done();
  }
  function legacyCopy(txt) {
    try {
      var ta = document.createElement("textarea");
      ta.value = txt;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      var ok = document.execCommand("copy");
      ta.remove();
      return ok;
    } catch (e) { return false; }
  }
  document.addEventListener("click", function (ev) {
    var el = ev.target.closest("button");
    if (!el) {
      var tip = ev.target.closest("[data-tip]");
      if (tip) toast(tip.getAttribute("data-tip"));
      return;
    }
    haptic("tap");
    if (el.getAttribute("data-tab")) { go(el.getAttribute("data-tab")); return; }
    if (el.getAttribute("data-go")) { go(el.getAttribute("data-go")); return; }
    if (el.getAttribute("data-farm")) { farm(el.getAttribute("data-farm")); return; }
    if (el.getAttribute("data-netgo")) {
      (async function () {
        setProgress(true);
        try {
          await jpost("/api/miniapp/network/switch", { slug: wiz.net });
          haptic("ok");
          toast(tr("netSwitched") + ": " + wiz.net);
          await loadState(true);
        } catch (e) { haptic("err"); toast(String(e.message || e)); }
        setProgress(false);
      })();
      return;
    }
    if (el.getAttribute("data-alert")) {
      (async function () {
        var v = el.getAttribute("data-alert");
        setProgress(true);
        try {
          if (v === "off") await jpost("/api/miniapp/alerts", { enabled: false, threshold: 0 });
          else await jpost("/api/miniapp/alerts", { enabled: true, threshold: parseFloat(v) });
          haptic("ok");
          renderDanger();
        } catch (e) { haptic("err"); toast(String(e.message || e)); }
        setProgress(false);
      })();
      return;
    }
    var act = el.getAttribute("data-act");
    if (act === "refresh") { loadState(true); return; }
    if (act === "drip") {
      (async function () {
        var ok = false;
        try {
          if (tg && tg.showConfirm) ok = await new Promise(function (res) {
            tg.showConfirm(tr("dripAsk"), res);
          });
          else ok = window.confirm(tr("dripAsk"));
        } catch (e) { ok = false; }
        if (!ok) return;
        setProgress(true);
        try {
          var r = await jpost("/api/miniapp/faucet/drip", {});
          haptic("ok");
          toast(r.started ? tr("dripRun") : tr("dripBusy"));
          loadState(true);
        } catch (e) { haptic("err"); toast(String(e.message || e)); }
        setProgress(false);
      })();
      return;
    }
    if (act === "histcsv") { histCsv(); return; }
    if (act === "copy") {
      var pre = $("cmdPre");
      var txt = pre ? pre.textContent : "";
      copyText(txt, "ok");
      return;
    }
    if (act === "yamlsend") { buildCmd(true); return; }
    if (act === "verify") {
      (async function () {
        try {
          var cfgBody = await jpost("/api/miniapp/command", {
            kind: "cfg", net: wiz.net, wallets: wiz.wallets });
          var chk = await jpost("/api/miniapp/command",
            { kind: "check", yaml: cfgBody.yaml });
          $("verifyOut").innerHTML = "<pre>" +
            esc((chk.lines || []).join("\n")) + "</pre>";
          haptic(chk.ok ? "ok" : "err");
        } catch (e) { toast(String(e.message || e)); }
      })();
      return;
    }
    if (el.getAttribute("data-net")) { wiz.net = el.getAttribute("data-net"); wiz.step = 2; renderNets(); return; }
    if (el.getAttribute("data-w")) { wiz.wallets = parseInt(el.getAttribute("data-w"), 10); return; }
    if (el.getAttribute("data-c")) { wiz.cycles = parseInt(el.getAttribute("data-c"), 10); return; }
    if (el.getAttribute("data-p")) {
      wiz.profile = el.getAttribute("data-p");
      setMain({ text: tr("doneBtn"), onClick: function () { buildCmd(false); } });
      return;
    }
    if (el.getAttribute("data-f")) {
      wiz.faucet = el.getAttribute("data-f") === "1";
      buildCmd(false);
      return;
    }
    if (el.getAttribute("data-exp")) { doExport(el.getAttribute("data-exp")); return; }
    if (el.getAttribute("data-mode")) {
      jpost("/api/miniapp/settings", { mode: (state && state.mode === "pro") ? "novice" : "pro" })
        .then(function () { loadState(true); renderDanger(); });
      return;
    }
    if (el.getAttribute("data-lang")) {
      jpost("/api/miniapp/settings", { lang: (state && state.lang === "en") ? "ru" : "en" })
        .then(function () { loadState(true); renderDanger(); });
      return;
    }
  });
  /* ---------- старт ---------- */
  (function init() {
    applyTheme();
    syncViewport();
    try {
      if (tg && tg.onEvent) {
        tg.onEvent("themeChanged", applyTheme);
        tg.onEvent("viewportChanged", syncViewport);
      }
    } catch (e) { /* молча */ }
    window.addEventListener("resize", syncViewport);
    // Табы стрелками — как в docs play-tabs.
    try {
      tabs.addEventListener("keydown", function (e) {
        var btns = Array.prototype.slice.call(tabs.querySelectorAll("button[data-tab]"));
        var i = btns.indexOf(document.activeElement);
        if (i < 0) return;
        var n = null;
        if (e.key === "ArrowRight") n = (i + 1) % btns.length;
        else if (e.key === "ArrowLeft") n = (i - 1 + btns.length) % btns.length;
        else if (e.key === "Home") n = 0;
        else if (e.key === "End") n = btns.length - 1;
        if (n !== null && n !== undefined) { e.preventDefault(); btns[n].focus(); btns[n].click(); }
      });
    } catch (e) { /* молча */ }
    if (!window.Telegram || !window.Telegram.WebApp) {
      showGate("<p>" + esc(tr("cdnFail")) + "</p>");
      return;
    }
    if (!tg || !tg.initData) {
      showGate("<p>" + esc(tr("openFromBot")) + "</p>");
      return;
    }
    try {
      tg.ready();
      tg.expand();
      try {
        if (tg.disableVerticalSwipes) tg.disableVerticalSwipes();
        applyTheme();
        var scheme = (tg.colorScheme || "dark").toLowerCase();
        var bg = (tg.themeParams && tg.themeParams.bg_color) || (scheme === "light" ? "#ffffff" : "#05060a");
        if (tg.setHeaderColor) tg.setHeaderColor("bg_color");
        if (tg.setBackgroundColor) tg.setBackgroundColor(bg);
      } catch (e) { /* молча */ }
    } catch (e) { /* молча */ }
    hideGate();
    tabs.hidden = false;
    syncBack(false);
    api("/api/miniapp/networks").then(function (b) {
      nets = b.networks || [];
      presets = b.presets || presets;
    }).catch(function () { /* молча */ });
    loadState(true);
    startPoll();
  })();
})();
