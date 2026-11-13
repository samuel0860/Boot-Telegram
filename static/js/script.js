(function () {
  "use strict";

  var check = document.getElementById("check");
  var status = document.getElementById("status");
  var cta = document.getElementById("continue");
  if (!check || !cta) return;

  var done = false;

  // Etapa de confirmação VISUAL: não é um CAPTCHA nem uma proteção contra bots.
  function confirm() {
    if (done) return;
    done = true;
    check.setAttribute("aria-disabled", "true");
    check.classList.add("loading");

    setTimeout(function () {
      check.classList.remove("loading");
      check.classList.add("done");
      check.setAttribute("aria-checked", "true");
      status.textContent = "Confirmado! Toque em continuar.";
      status.classList.add("show");

      setTimeout(function () {
        cta.hidden = false;
        cta.removeAttribute("aria-hidden");
        cta.removeAttribute("tabindex");
        cta.classList.add("reveal");
        cta.focus({ preventScroll: true });
      }, 350);
    }, 900);
  }

  check.addEventListener("click", confirm);
  check.addEventListener("keydown", function (e) {
    if (e.key === " " || e.key === "Enter") {
      e.preventDefault();
      confirm();
    }
  });

  cta.addEventListener("click", function (e) {
    var href = cta.getAttribute("data-href");
    if (!done || !href || href === "#") {
      e.preventDefault();
      return;
    }
    // Repassa parâmetros de campanha (ex.: ?start=tiktok_video1) se presentes na URL da página.
    try {
      var params = new URLSearchParams(window.location.search);
      var start = params.get("start");
      if (start && /^[A-Za-z0-9_-]{1,64}$/.test(start)) {
        var url = new URL(href);
        url.searchParams.set("start", start);
        e.preventDefault();
        window.location.href = url.toString();
      }
    } catch (err) {
      /* usa o link padrão */
    }
  });
})();
