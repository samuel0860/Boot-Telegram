// Aplica o tema salvo ANTES de a página aparecer (evita piscar). Padrão: claro.
(function () {
  try {
    if (localStorage.getItem("theme") === "dark") document.documentElement.setAttribute("data-theme", "dark");
  } catch (err) { /* sem armazenamento: fica no claro */ }
})();
