(function () {
  "use strict";
  // Confirmação para formulários marcados com data-confirm (sem JS inline, compatível com CSP).
  document.addEventListener("submit", function (e) {
    var form = e.target;
    var msg = form.getAttribute && form.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) {
      e.preventDefault();
    }
  });
})();
