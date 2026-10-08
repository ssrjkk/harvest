/* HARVEST PORTAL — страница входа.
 * Внешний файл: inline-скрипты запрещены политикой CSP (script-src 'self').
 */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const err = $("err");
  const THEMES = ["dark", "light"];

  function applyTheme(t) {
    if (t !== "light") t = "dark";
    document.documentElement.setAttribute("data-bs-theme", t);
    localStorage.setItem("harvest_theme", t);
    if ($("thDark")) $("thDark").classList.toggle("active", t === "dark");
    if ($("thLight")) $("thLight").classList.toggle("active", t === "light");
  }
  const saved = localStorage.getItem("harvest_theme") || "dark";
  applyTheme(saved);
  THEMES.forEach((th) => {
    const el = $("th" + th[0].toUpperCase() + th.slice(1));
    if (el) el.addEventListener("click", () => applyTheme(th));
  });

  function showErr(msg) {
    err.style.display = "block";
    err.textContent = msg;
  }

  function toast(msg) {
    const t = document.createElement("div");
    t.className = "toast";
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 4000);
  }

  (async function () {
    const me = await fetch("/api/me").then(r => r.json());
    if (me.authed) { location.href = "/"; return; }

    const tg = window.Telegram?.WebApp;
    if (tg) {
      tg.ready();
      tg.expand();
      const res = await fetch("/api/tg/init", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ init_data: tg.initData })
      });
      const body = await res.json().catch(() => ({}));
      if (body.ok) { location.href = "/"; return; }
      toast("Mini App: нет доступа (" + (body.error || "?") + ")");
      tg.close();
      return;
    }

    const cfg = await fetch("/api/me").then(r => r.json());
    const googleOn = !!cfg.google, passwordOn = !!cfg.password;
    if (!googleOn && !passwordOn) {
      showErr("Вход не настроен: задайте FARMER_MASTER_KEY или Google credentials.");
      return;
    }
    $("btnGoogle").style.display = googleOn ? "block" : "none";
    $("sep").style.display = (googleOn && passwordOn) ? "flex" : "none";
    if (passwordOn) {
      $("pwForm").style.display = "block";
      if (!window.isSecureContext) {
        // http://localhost считается secure context; удалённый HTTP — нет.
        // Сервер всё равно откажет — не пускаем мастер-ключ по открытому HTTP.
        $("pw").disabled = true;
        $("pwSubmit").disabled = true;
        $("pwSubmit").textContent = "Вход по паролю доступен только по HTTPS";
        showErr("Страница открыта по незащищённому соединению — мастер-ключ не отправляется.");
      }
    }
  })();

  $("btnGoogle").addEventListener("click", () => { location.href = "/auth/google"; });
  $("pwForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!window.isSecureContext) return;
    const res = await fetch("/api/login/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: $("pw").value })
    });
    const body = await res.json().catch(() => ({}));
    if (res.ok) { location.href = "/"; }
    else { showErr(body.error || "Ошибка"); }
  });
})();