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

  // Snel invullen van een LLM-provider (alleen naam, base-URL en model; nooit een sleutel)
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-preset-base]");
    if (!b) return;
    var set = function (id, v) { var el = document.getElementById(id); if (el) el.value = v; };
    set("llm-name", b.getAttribute("data-preset-name"));
    set("llm-base", b.getAttribute("data-preset-base"));
    set("llm-model", b.getAttribute("data-preset-model"));
    var k = document.getElementById("llm-key");
    if (k) { k.placeholder = b.getAttribute("data-preset-prefix"); k.focus(); }
  });

  // Facecam aanwijzen: rechthoek slepen over het stilstaande beeld
  function initFacecamPicker(box) {
    var img = box.querySelector("img"), sel = box.querySelector(".facecam-sel");
    var start = null;
    function show(x, y, w, h) {
      sel.hidden = false;
      sel.style.left = x + "%"; sel.style.top = y + "%"; sel.style.width = w + "%"; sel.style.height = h + "%";
      var set = function (id, v) { var el = document.getElementById(id); if (el) el.value = v.toFixed(2); };
      set("fc-x", x); set("fc-y", y); set("fc-w", w); set("fc-h", h);
    }
    function pos(e) {
      var r = img.getBoundingClientRect();
      return { x: Math.min(100, Math.max(0, (e.clientX - r.left) / r.width * 100)),
               y: Math.min(100, Math.max(0, (e.clientY - r.top) / r.height * 100)) };
    }
    var initial = (box.getAttribute("data-box") || "").split(",").map(parseFloat);
    if (initial.length === 4 && initial.every(function (v) { return !isNaN(v); })) show.apply(null, initial);
    box.addEventListener("pointerdown", function (e) {
      e.preventDefault(); start = pos(e); box.setPointerCapture(e.pointerId);
    });
    box.addEventListener("pointermove", function (e) {
      if (!start) return;
      var p = pos(e);
      show(Math.min(start.x, p.x), Math.min(start.y, p.y), Math.abs(p.x - start.x), Math.abs(p.y - start.y));
    });
    box.addEventListener("pointerup", function () { start = null; });
  }

  document.addEventListener("DOMContentLoaded", function () {
    updateSelection();
    document.querySelectorAll(".facecam-picker").forEach(initFacecamPicker);
  });
})();
