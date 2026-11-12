// Zet het gekozen thema vóór het tekenen van de pagina (geen witte flits).
(function () {
  try {
    var t = localStorage.getItem("thema");
    if (t === "dark" || t === "light") document.documentElement.setAttribute("data-theme", t);
  } catch (e) { /* geen opslag beschikbaar: systeeminstelling volgen */ }
})();
