(function () {
  "use strict";

  // Confirmação para formulários marcados com data-confirm (sem JS inline, compatível com CSP).
  document.addEventListener("submit", function (e) {
    var form = e.target;
    var msg = form.getAttribute && form.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) {
      e.preventDefault();
      return;
    }
    // Feedback visual em ações demoradas
    var btn = form.querySelector("button[data-loading]");
    if (btn) {
      btn.disabled = true;
      btn.textContent = btn.getAttribute("data-loading");
    }
  });

  // Menu lateral no celular
  var sidebar = document.getElementById("sidebar");
  var backdrop = document.getElementById("sidebar-backdrop");
  var toggle = document.getElementById("menu-toggle");
  function setMenu(open) {
    if (!sidebar) return;
    sidebar.classList.toggle("open", open);
    if (backdrop) backdrop.classList.toggle("open", open);
  }
  if (toggle) toggle.addEventListener("click", function () { setMenu(!sidebar.classList.contains("open")); });
  if (backdrop) backdrop.addEventListener("click", function () { setMenu(false); });

  // Grupos do menu (dropdown): lembra quais o usuário deixou abertos.
  var KEY = "nav-open-groups";
  var saved = [];
  try { saved = JSON.parse(localStorage.getItem(KEY) || "[]"); } catch (err) { saved = []; }
  var groups = document.querySelectorAll(".nav-group[data-group]");
  Array.prototype.forEach.call(groups, function (g) {
    if (saved.indexOf(g.getAttribute("data-group")) !== -1) g.open = true;
    g.querySelector("summary").addEventListener("click", function () { g.classList.add("anim"); });
    g.addEventListener("toggle", function () {
      var open = [];
      Array.prototype.forEach.call(groups, function (x) { if (x.open) open.push(x.getAttribute("data-group")); });
      try { localStorage.setItem(KEY, JSON.stringify(open)); } catch (err) { /* sem storage */ }
    });
  });

  // Mostrar/ocultar senha
  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest("[data-reveal]");
    if (!btn) return;
    var input = document.getElementById(btn.getAttribute("data-reveal"));
    if (!input) return;
    var show = input.type === "password";
    input.type = show ? "text" : "password";
    btn.classList.toggle("on", show);
    btn.setAttribute("aria-label", show ? "Ocultar senha" : "Mostrar senha");
    btn.title = btn.getAttribute("aria-label");
    input.focus();
  });
})();

// <select class="auto-submit">: troca a página ao escolher (ex.: escolher o bot na tela Ofertas).
document.addEventListener("change", function (e) {
  var el = e.target;
  if (el && el.classList && el.classList.contains("auto-submit") && el.form) el.form.submit();
});

// ---------------------------------------------------------------------------
// App instalável (PWA)
// ---------------------------------------------------------------------------
(function () {
  "use strict";
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/admin/sw.js", { scope: "/admin/" }).catch(function () {});
    });
  }

  var standalone = window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
  if (standalone) document.documentElement.classList.add("is-app");

  // Android / Chrome / Edge: botão "Instalar app"
  var installBtn = document.getElementById("install-app");
  var deferred = null;
  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault();
    deferred = e;
    if (installBtn) installBtn.hidden = false;
  });
  if (installBtn) {
    installBtn.addEventListener("click", function () {
      if (!deferred) return;
      deferred.prompt();
      deferred.userChoice.finally(function () {
        deferred = null;
        installBtn.hidden = true;
      });
    });
  }
  window.addEventListener("appinstalled", function () {
    if (installBtn) installBtn.hidden = true;
  });

  // iPhone/iPad (Safari não tem botão de instalar): mostra a dica uma vez
  var ios = /iphone|ipad|ipod/i.test(navigator.userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  var tip = document.getElementById("ios-install");
  var seen = false;
  try { seen = localStorage.getItem("ios-install-tip") === "1"; } catch (err) { /* sem armazenamento */ }
  if (tip && ios && !standalone && !seen) {
    tip.hidden = false;
    var close = document.getElementById("ios-install-close");
    if (close) {
      close.addEventListener("click", function () {
        tip.hidden = true;
        try { localStorage.setItem("ios-install-tip", "1"); } catch (err) { /* ignora */ }
      });
    }
  }
})();
