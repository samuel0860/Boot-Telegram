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
  var tabMenu = document.getElementById("tabbar-menu");
  if (tabMenu) tabMenu.addEventListener("click", function () { setMenu(!sidebar.classList.contains("open")); });

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
  var ios = /iphone|ipad|ipod/i.test(navigator.userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  var mobile = ios || /android/i.test(navigator.userAgent);

  var buttons = document.querySelectorAll("[data-install]");
  var help = document.getElementById("install-help");
  var deferred = null;
  function show(on) { buttons.forEach(function (b) { b.hidden = !on; }); }

  // No iPhone o Safari não tem botão de instalar: o nosso botão abre o passo a passo.
  // Em celulares Android sem o aviso automático (ex.: sem https), também mostra o passo a passo.
  if (!standalone && mobile) show(true);
  if (help) help.classList.add(ios ? "is-ios" : "is-other");

  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault();
    deferred = e;
    show(true);
  });
  window.addEventListener("appinstalled", function () { show(false); });

  buttons.forEach(function (b) {
    b.addEventListener("click", function () {
      if (deferred) {
        deferred.prompt();
        deferred.userChoice.finally(function () { deferred = null; });
        return;
      }
      if (help && typeof help.showModal === "function") help.showModal();
    });
  });
  if (help) {
    help.querySelectorAll("[data-close]").forEach(function (b) {
      b.addEventListener("click", function () { help.close(); });
    });
    help.addEventListener("click", function (e) { if (e.target === help) help.close(); });
  }
})();

// ---------------------------------------------------------------------------
// Tabelas no celular: cada linha vira um cartão com o nome da coluna ao lado do valor
// ---------------------------------------------------------------------------
(function () {
  "use strict";
  document.querySelectorAll(".content table").forEach(function (table) {
    var heads = Array.prototype.map.call(table.querySelectorAll("thead th"), function (th) {
      return th.textContent.trim();
    });
    if (!heads.length) return;
    table.classList.add("stack");
    var wrap = table.closest(".table-wrap");
    if (wrap) wrap.classList.add("stack-wrap");
    table.querySelectorAll("tbody tr").forEach(function (tr) {
      var i = 0;
      Array.prototype.forEach.call(tr.children, function (td) {
        if (!td.hasAttribute("data-label")) td.setAttribute("data-label", heads[i] || "");
        i += td.colSpan || 1;
      });
    });
  });
})();
