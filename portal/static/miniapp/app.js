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
    expired: { ru: "Сессия устарела — закрой и открой из бота.", en: "Session expired — reopen from the bot." },
    retry: { ru: "Повторить", en: "Retry" },
    stopped: { ru: "Ферма остановлена. Поле ждёт запуска.", en: "Farm stopped. Field awaits launch." },
  };

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
    showGate("<p>" + esc(T.expired[lang]) + "</p>");
  }

  /* ---------- назад: TG BackButton или ряд в интерфейсе ---------- */
  function backRow() {
    var canTG = tg && tg.BackButton && atLeast("6.1");
    if (canTG) return "";
    return '<div class="tma-backrow"><button class="tma-btn" data-go="field">←</button></div>';
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
  function setMain(btn) {
    try {
      if (!tg || !tg.MainButton) return;
      if (!btn) { tg.MainButton.hide(); return; }
      tg.MainButton.setText(btn.text);
      tg.MainButton.show();
      tg.MainButton.onClick(btn.onClick);
    } catch (e) { /* молча: обычная кнопка в интерфейсе вместо MainButton */ }
  }

  /* ---------- поле ---------- */
  var prevCells = "";
  function gridHtml(cells, total) {
    cells = (cells || "").slice(0, 400);
    var out = "";
    for (var i = 0; i < cells.length; i++) {
      var c = cells[i];
      var flash = prevCells && prevCells[i] && prevCells[i] !== c ? " flash" : "";
      out += '<span class="c c-' + c + flash + '" data-tip="#' + (i + 1) + " · " + c + '">' +
        (GLYPH[c] || "·") + "</span>";
    }
    if (!out) out = '<span class="c c-i">·</span>';
    if ((total || 0) > cells.length) out += "<p>+N: +" + ((total || 0) - cells.length) + "</p>";
    prevCells = cells;
    return '<div class="tma-grid">' + out + "</div>";
  }
  function mark(s) {
    if (s.running && s.paused) return "[PAUSE]";
    return s.running ? "[RUN]" : "[STOP]";
  }
  function renderField(s) {
    lang = s.lang || lang;
    $("tmaSub").textContent = mark(s) + " " + (s.network || "—") + " · " + (s.chain_id || "—");
    var h = '<div class="tma-card"><div class="tma-kv"><span>кошельки</span><b>' + esc(s.wallet_count) +
      '</b></div><div class="tma-kv"><span>цикл</span><b>' + esc(s.cycle) + " / ∞</b></div>" +
      '<div class="tma-kv"><span>ошибки</span><b>' + esc(s.errors) + "</b></div>" +
      '<div class="tma-kv"><span>health</span><b>' + Number(s.health_factor || 0).toFixed(2) + "</b></div>";
    if (s.mode === "pro") {
      h += '<div class="tma-kv"><span>workers</span><b>' + esc(s.workers) + "</b></div>";
    } else {
      h += "<p>кошельки — ваши адреса · health — живость сети</p>";
    }
    h += "</div>" + gridHtml(s.cells, s.cells_total) +
      '<p>обновлено ' + esc(s.updated_at) + "</p>";
    if (!s.running) h += '<div class="tma-card">' + esc(T.stopped[lang]) + "</div>";
    h += '<div class="tma-row"><button class="tma-btn" data-act="refresh">Обновить</button></div>';
    screen.innerHTML = h;
    if (s.running) setMain({ text: "ПАУЗА", onClick: function () { farm("pause"); } });
    else setMain({ text: "ЗАПУСК", onClick: function () { farm("start"); } });
  }
  async function farm(action) {
    if (action === "stop") {
      var ok = false;
      try {
        if (tg && tg.showConfirm) ok = await new Promise(function (res) {
          tg.showConfirm("Остановить фарм?", res);
        });
        else ok = window.confirm("Остановить фарм?");
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
    var h = backRow() + '<div class="tma-card"><p>Шаг ' + wiz.step + "/3</p>";
    if (wiz.step === 1) {
      h += nets.map(function (n) {
        return '<button class="tma-btn" data-net="' + esc(n.slug) + '">' + esc(n.name) + "</button>";
      }).join("");
    } else if (wiz.step === 2) {
      h += "<p>Кошельки</p>" + presets.wallets.map(function (w) {
        return '<button class="tma-btn" data-w="' + w + '">' + w + "</button>";
      }).join("") + "<p>Циклы</p>" + presets.cycles.map(function (c) {
        return '<button class="tma-btn" data-c="' + c + '">' + (c === 0 ? "∞" : c) + "</button>";
      }).join("");
    } else {
      h += "<p>Профиль (быстрый = --fast: меньше задержек, заметнее)</p>" + presets.profiles.map(function (p) {
        return '<button class="tma-btn" data-p="' + p + '">' + p + "</button>";
      }).join("") +
        '<div class="tma-row"><button class="tma-btn" data-f="1">С краном</button>' +
        '<button class="tma-btn" data-f="0">Без крана</button></div>';
    }
    screen.innerHTML = h + "</div>";
    setMain(wiz.step < 3
      ? { text: "ДАЛЕЕ", onClick: function () { wiz.step = Math.min(3, wiz.step + 1); renderNets(); } }
      : null);
  }
  async function buildCmd(sendYaml) {
    setProgress(true);
    try {
      var body = await jpost("/api/miniapp/command", {
        kind: sendYaml ? "cfg_send" : "cmd", net: wiz.net, wallets: wiz.wallets,
        cycles: wiz.cycles, profile: wiz.profile, faucet: wiz.faucet });
      if (sendYaml) { toast("YAML отправлен в чат с ботом"); haptic("ok"); return; }
      screen.innerHTML = backRow() + '<div class="tma-card"><pre id="cmdPre">' +
        esc(body.cmd) + '</pre><div class="tma-row"><button class="tma-btn" data-act="copy">' +
        "Скопировать</button>" + '<button class="tma-btn" data-act="yamlsend">YAML файлом</button>' +
        '<button class="tma-btn" data-act="verify">Проверить</button></div><div id="verifyOut"></div></div>';
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
      screen.innerHTML = backRow() + '<div class="tma-card"><p>Ошибка сети</p>' +
        '<button class="tma-btn" data-go="history">' + esc(T.retry[lang]) + "</button></div>";
    }
    setMain(null);
  }
  function histCsv() {
    if (!histRows.length) { toast("empty"); return; }
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
      sched = '<div class="tma-kv"><span>расписание</span><b>' +
        esc((sc.tasks || []).length) + " задач</b></div>";
    } catch (e) { sched = ""; }
    screen.innerHTML = backRow() +
      '<div class="tma-card"><p>Файл содержит приватные ключи. Только тестнет.</p>' +
      '<div class="tma-row"><button class="tma-btn" data-exp="csv">CSV</button>' +
      '<button class="tma-btn" data-exp="json">JSON</button></div></div>' + sched +
      '<div class="tma-card"><div class="tma-row"><button class="tma-btn" data-mode="1">Режим: ' +
      esc(s.mode || "novice") + '</button><button class="tma-btn" data-lang="1">Язык: ' +
      esc((s.lang || "ru").toUpperCase()) + "</button></div></div>";
    setMain(null);
  }
  async function doExport(fmt) {
    var confirmed = false;
    try {
      if (tg && tg.showConfirm) confirmed = await new Promise(function (res) {
        tg.showConfirm("Файл содержит приватные ключи. Продолжить?", res);
      });
      else confirmed = window.confirm("Файл содержит приватные ключи. Продолжить?");
    } catch (e) { confirmed = false; }
    if (!confirmed) return;
    setProgress(true);
    try {
      var tok = await jpost("/api/miniapp/export/token", { format: fmt });
      await jpost("/api/miniapp/export/request",
        { format: fmt, confirm_token: tok.confirm_token });
      haptic("ok");
      toast("Файл отправлен в чат с ботом");
    } catch (e) { haptic("err"); toast(String(e.message || e)); }
    setProgress(false);
  }

  /* ---------- навигация ---------- */
  function go(name) {
    tab = name;
    var btns = tabs.querySelectorAll("button");
    for (var i = 0; i < btns.length; i++) {
      btns[i].classList.toggle("on", btns[i].getAttribute("data-tab") === name);
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
        screen.innerHTML = '<div class="tma-card"><p>Ошибка сети</p>' +
          '<button class="tma-btn" data-act="refresh">' + esc(T.retry[lang]) + "</button></div>";
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
    var act = el.getAttribute("data-act");
    if (act === "refresh") { loadState(true); return; }
    if (act === "histcsv") { histCsv(); return; }
    if (act === "copy") {
      var pre = $("cmdPre");
      var txt = pre ? pre.textContent : "";
      if (navigator.clipboard) navigator.clipboard.writeText(txt).then(function () { toast("ok"); });
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
      setMain({ text: "ГОТОВО", onClick: function () { buildCmd(false); } });
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
    if (!tg || !tg.initData) {
      showGate("<p>" + esc(T.openFromBot[lang]) + "</p>");
      return;
    }
    try {
      tg.ready();
      tg.expand();
      try {
        if (tg.setHeaderColor) tg.setHeaderColor("bg_color");
        if (tg.setBackgroundColor) tg.setBackgroundColor("#08080e");
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
