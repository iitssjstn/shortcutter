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
      var card = b.closest(".clip-row") || b.closest(".clip");
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
    if (e.target.id === "select-all-twitch") {
      document.querySelectorAll(".twitch-check:not(:disabled)").forEach(function (b) { b.checked = e.target.checked; });
    }
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

  // Clip-editor: indeling tekenen/verslepen met vaste verhouding, live voorbeeld met ondertitel
  function initClipEditor(form) {
    var stage = document.getElementById("le-stage"), img = document.getElementById("le-img");
    var canvas = document.getElementById("le-preview"), ctx = canvas.getContext("2d");
    var modeSel = document.getElementById("layout-mode"), topIn = document.getElementById("le-top");
    var out = document.getElementById("layout-json");
    var SW = parseFloat(stage.getAttribute("data-src-w")) || 1920, SH = parseFloat(stage.getAttribute("data-src-h")) || 1080;
    var clamp = function (v, a, b) { return Math.min(b, Math.max(a, v)); };
    var st = { cam: null, game: null, rect: null, top: 0.35 };
    try {
      var init = JSON.parse(out.value || "null");
      if (init && init.type === "split") { st.cam = init.cam; st.game = init.game; st.top = init.top || 0.35; }
      if (init && init.type === "crop") { st.rect = init.rect; }
    } catch (e) { /* geen beginwaarde */ }

    function aspect(k) {  // gewenste verhouding (breedte/hoogte in pixels) per vak
      if (k === "cam") return 1080 / (1920 * st.top);
      if (k === "game") return 1080 / (1920 * (1 - st.top));
      return 1080 / 1920;
    }
    function fit(r, k) {  // houd breedte, pas hoogte aan; krimp als het niet past
      var A = aspect(k), w = r[2], h = w * SW / A / SH;
      if (h > 1) { h = 1; w = h * SH * A / SW; }
      if (w > 1) { w = 1; h = w * SW / A / SH; }
      return [clamp(r[0], 0, 1 - w), clamp(r[1], 0, 1 - h), w, h];
    }
    function centered(k, wFrac) {
      var A = aspect(k), h = 1, w = h * SH * A / SW;
      if (wFrac && w > wFrac) { w = wFrac; h = w * SW / A / SH; }
      if (w > 1) { w = 1; h = w * SW / A / SH; }
      return [(1 - w) / 2, (1 - h) / 2, w, h];
    }
    var fc = null;
    try { fc = JSON.parse(stage.getAttribute("data-facecam") || "null"); } catch (e) { fc = null; }
    if (!st.cam) st.cam = fc || [0.02, 0.02, 0.3, 0.3];
    if (!st.game) st.game = centered("game");
    if (!st.rect) st.rect = centered("rect");
    topIn.value = Math.round(st.top * 100);
    document.getElementById("le-top-val").textContent = topIn.value + "%";

    var targetKey = function () {
      if (modeSel.value === "crop") return "rect";
      var r = form.querySelector('input[name="le-target"]:checked');
      return r ? r.value : "cam";
    };
    function pos(e) {
      var b = stage.getBoundingClientRect();
      return { x: clamp((e.clientX - b.left) / b.width, 0, 1), y: clamp((e.clientY - b.top) / b.height, 0, 1) };
    }
    var drag = null;
    stage.addEventListener("pointerdown", function (e) {
      if (modeSel.value === "auto") return;
      e.preventDefault();
      var k = targetKey(), p = pos(e), r = st[k];
      // klik op een ander vak in gesplitste modus: dat vak selecteren
      if (modeSel.value === "split") {
        ["cam", "game"].forEach(function (o) {
          var q = st[o];
          if (o !== k && p.x >= q[0] && p.x <= q[0] + q[2] && p.y >= q[1] && p.y <= q[1] + q[3] &&
              !(p.x >= r[0] && p.x <= r[0] + r[2] && p.y >= r[1] && p.y <= r[1] + r[3])) {
            k = o; r = st[o];
            var radio = form.querySelector('input[name="le-target"][value="' + o + '"]');
            if (radio) radio.checked = true;
          }
        });
      }
      var inside = p.x >= r[0] && p.x <= r[0] + r[2] && p.y >= r[1] && p.y <= r[1] + r[3];
      drag = inside ? { k: k, move: true, dx: p.x - r[0], dy: p.y - r[1] } : { k: k, move: false, sx: p.x, sy: p.y };
      stage.setPointerCapture(e.pointerId);
    });
    stage.addEventListener("pointermove", function (e) {
      if (!drag) return;
      var p = pos(e), r = st[drag.k];
      if (drag.move) {
        st[drag.k] = [clamp(p.x - drag.dx, 0, 1 - r[2]), clamp(p.y - drag.dy, 0, 1 - r[3]), r[2], r[3]];
      } else {
        var x = Math.min(p.x, drag.sx), y = Math.min(p.y, drag.sy);
        var w = Math.max(0.03, Math.abs(p.x - drag.sx)), h = Math.max(0.03, Math.abs(p.y - drag.sy));
        st[drag.k] = [clamp(x, 0, 1 - w), clamp(y, 0, 1 - h), w, h];
        if (drag.k === "cam") matchTop();
      }
      draw();
    });
    stage.addEventListener("pointerup", function () { drag = null; });

    // facecam-hoogte zo kiezen dat de getekende facecam precies past (binnen 20-60%)
    function matchTop() {
      var c = st.cam, A = (c[2] * SW) / (c[3] * SH);
      st.top = clamp(Math.round(1080 / (A * 1920) * 100) / 100, 0.2, 0.6);
      topIn.value = Math.round(st.top * 100);
      document.getElementById("le-top-val").textContent = topIn.value + "%";
    }
    // deel van een vak dat echt gebruikt wordt (midden, in de verhouding van het doelvak) - zoals de server
    function used(r, k) {
      var A = aspect(k), pw = r[2] * SW, ph = r[3] * SH, x = r[0], y = r[1], w = r[2], h = r[3];
      if (pw / ph > A) { var nw = ph * A / SW; x += (w - nw) / 2; w = nw; }
      else { var nh = pw / A / SH; y += (h - nh) / 2; h = nh; }
      return [x, y, w, h];
    }

    topIn.addEventListener("input", function () {
      st.top = parseInt(topIn.value, 10) / 100;
      document.getElementById("le-top-val").textContent = topIn.value + "%";
      draw();
    });
    modeSel.addEventListener("change", draw);
    ["cap-pos", "cap-size", "cap-color", "cap-upper"].forEach(function (id) {
      var el = document.getElementById(id); if (el) el.addEventListener("input", draw);
      if (el) el.addEventListener("change", draw);
    });
    form.addEventListener("click", function (e) {
      var b = e.target.closest("[data-cap-pos]");
      if (!b) return;
      var v = b.getAttribute("data-cap-pos");
      if (v === "35" && modeSel.value === "split") v = String(Math.round(st.top * 100));
      document.getElementById("cap-pos").value = v; draw();
    });
    form.addEventListener("input", function (e) { if (e.target.name === "cap_text") draw(); });

    function place(el, r, k) {
      el.style.left = r[0] * 100 + "%"; el.style.top = r[1] * 100 + "%";
      el.style.width = r[2] * 100 + "%"; el.style.height = r[3] * 100 + "%";
      var inner = el.querySelector(".le-used");
      if (!inner) { inner = document.createElement("i"); inner.className = "le-used"; el.appendChild(inner); }
      var u = used(r, k);
      inner.style.left = ((u[0] - r[0]) / r[2]) * 100 + "%"; inner.style.top = ((u[1] - r[1]) / r[3]) * 100 + "%";
      inner.style.width = (u[2] / r[2]) * 100 + "%"; inner.style.height = (u[3] / r[3]) * 100 + "%";
    }
    function blit(r, dx, dy, dw, dh) {
      if (!img || !img.naturalWidth) return;
      var iw = img.naturalWidth, ih = img.naturalHeight;
      ctx.drawImage(img, r[0] * iw, r[1] * ih, r[2] * iw, r[3] * ih, dx, dy, dw, dh);
    }
    function draw() {
      var m = modeSel.value;
      form.querySelectorAll(".le-tools").forEach(function (t) { t.style.display = m === "split" ? "" : "none"; });
      form.querySelector(".le-help").style.display = m === "auto" ? "none" : "";
      stage.querySelector(".cam").hidden = m !== "split";
      stage.querySelector(".game").hidden = m !== "split";
      stage.querySelector(".crop").hidden = m !== "crop";
      stage.classList.toggle("is-auto", m === "auto");
      place(stage.querySelector(".cam"), st.cam, "cam"); place(stage.querySelector(".game"), st.game, "game");
      place(stage.querySelector(".crop"), st.rect, "rect");
      var W = canvas.width, H = canvas.height;
      ctx.fillStyle = "#000"; ctx.fillRect(0, 0, W, H);
      if (m === "split") {
        var th = Math.round(H * st.top);
        blit(used(st.cam, "cam"), 0, 0, W, th); blit(used(st.game, "game"), 0, th, W, H - th);
        out.value = JSON.stringify({ type: "split", cam: st.cam, game: st.game, top: st.top });
      } else if (m === "crop") {
        blit(used(st.rect, "rect"), 0, 0, W, H);
        out.value = JSON.stringify({ type: "crop", rect: st.rect });
      } else {
        ctx.fillStyle = "#9aa4b2"; ctx.font = "14px system-ui"; ctx.textAlign = "center";
        ctx.fillText("Automatische indeling", W / 2, H / 2 - 30);
        out.value = "";
      }
      // ondertitel-voorbeeld
      var first = form.querySelector('input[name="cap_text"]');
      var text = first ? first.value.split(/\s+/).slice(0, 3).join(" ") : "voorbeeld ondertitel";
      if (document.getElementById("cap-upper").checked) text = text.toUpperCase();
      var size = { S: 68, M: 86, L: 108 }[document.getElementById("cap-size").value] * W / 1080;
      var y = H * parseInt(document.getElementById("cap-pos").value, 10) / 100;
      var hi = { geel: "#FFE500", groen: "#5AE047", wit: "#FFFFFF" }[document.getElementById("cap-color").value] || "#FFE500";
      ctx.font = "bold " + size + "px 'DejaVu Sans', sans-serif"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.lineJoin = "round"; ctx.lineWidth = Math.max(2, size / 7); ctx.strokeStyle = "#000";
      var words = text.split(" "), widths = words.map(function (w) { return ctx.measureText(w + " ").width; });
      var total = widths.reduce(function (a, b) { return a + b; }, 0), x = W / 2 - total / 2;
      ctx.textAlign = "left";
      words.forEach(function (w, i) {
        ctx.strokeText(w, x, y); ctx.fillStyle = i === 0 ? hi : "#FFFFFF"; ctx.fillText(w, x, y); x += widths[i];
      });
    }
    if (img) { if (img.complete) draw(); else img.addEventListener("load", draw); }
    draw();
  }

  // Clips: uitklappen (soepel, duwt de rest omlaag); de clip uit de URL (#clip-ID) staat open
  function setOpen(row, open) {
    row.classList.toggle("open", open);
    var btn = row.querySelector(".clip-toggle");
    if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
    if (!open) row.querySelectorAll("video").forEach(function (v) { v.pause(); });
  }
  document.addEventListener("click", function (e) {
    var t = e.target.closest(".clip-toggle");
    if (!t) return;
    var row = t.closest(".clip-row");
    setOpen(row, !row.classList.contains("open"));
  });

  document.addEventListener("DOMContentLoaded", function () {
    if (location.hash && /^#clip-\d+$/.test(location.hash)) {
      var row = document.querySelector(location.hash);
      if (row && row.classList.contains("clip-row")) {
        row.style.transition = "none";
        setOpen(row, true);
        setTimeout(function () { row.scrollIntoView({ block: "start" }); }, 50);
      }
    }
    updateSelection();
    document.querySelectorAll(".facecam-picker").forEach(initFacecamPicker);
    var ed = document.getElementById("clip-editor");
    if (ed) initClipEditor(ed);
  });
})();
