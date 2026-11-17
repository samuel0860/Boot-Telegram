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
})();
