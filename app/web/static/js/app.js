// Kleine progressive enhancements. Alles werkt ook zonder JavaScript.
(function () {
  "use strict";

  // Bevestiging voor destructieve acties
  document.addEventListener("submit", function (e) {
    var msg = e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  // Kopieerknoppen
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-copy]");
    if (!btn) return;
    var el = document.getElementById(btn.getAttribute("data-copy"));
    if (!el) return;
    var text = el.value !== undefined ? el.value : el.textContent;
    var done = function () { var t = btn.textContent; btn.textContent = "Gekopieerd ✓"; setTimeout(function () { btn.textContent = t; }, 1500); };
    if (navigator.clipboard) navigator.clipboard.writeText(text).then(done, function () { el.select && el.select(); });
    else { el.select && el.select(); document.execCommand("copy"); done(); }
  });

  // Clipselectie en "Download selectie (zip)"
  function updateSelection() {
    var boxes = document.querySelectorAll(".clip-check");
    var ids = [];
    boxes.forEach(function (b) {
      var card = b.closest(".clip");
      if (card) card.classList.toggle("selected", b.checked);
      if (b.checked) ids.push(b.value);
    });
    var link = document.getElementById("download-selection");
    if (link) {
      var base = link.getAttribute("data-base");
      link.setAttribute("href", base + "?" + ids.map(function (i) { return "ids=" + i; }).join("&"));
      link.classList.toggle("btn-primary", ids.length > 0);
      link.setAttribute("aria-disabled", ids.length ? "false" : "true");
      var c = document.getElementById("selection-count");
      if (c) c.textContent = ids.length;
    }
  }
  document.addEventListener("change", function (e) {
    if (e.target.matches(".clip-check")) updateSelection();
    if (e.target.id === "select-all") {
      document.querySelectorAll(".clip-check").forEach(function (b) { b.checked = e.target.checked; });
      updateSelection();
    }
  });
  document.addEventListener("click", function (e) {
    var link = e.target.closest("#download-selection");
    if (link && link.getAttribute("aria-disabled") === "true") { e.preventDefault(); alert("Selecteer eerst een of meer clips."); }
  });

  // Handmatige clip: tijden overnemen uit de bronspeler en het transcript
  function fmt(t) { return (Math.round(t * 10) / 10).toFixed(1); }
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-take-time]");
    if (b) {
      var player = document.getElementById("source-player");
      var input = document.getElementById(b.getAttribute("data-take-time"));
      if (player && input) input.value = fmt(player.currentTime);
      return;
    }
    var seg = e.target.closest("[data-seg-start]");
    if (seg) {
      var p = document.getElementById("source-player");
      var s = parseFloat(seg.getAttribute("data-seg-start"));
      var en = parseFloat(seg.getAttribute("data-seg-end"));
      var si = document.getElementById("manual-start"), ei = document.getElementById("manual-end");
      // eerste klik = start, tweede klik = einde
      if (si && ei) {
        if (!si.value || ei.value) { si.value = fmt(s); ei.value = ""; }
        else { ei.value = fmt(Math.max(en, parseFloat(si.value) + 1)); }
      }
      if (p) { p.currentTime = s; }
    }
  });

  document.addEventListener("DOMContentLoaded", updateSelection);
})();
