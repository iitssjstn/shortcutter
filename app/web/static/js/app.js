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

  // Clip-editor (zoals bij Twitch): kaders verslepen/schalen op de bewegende video, live staand voorbeeld,
  // schuifbalk tussen facecam en gameplay, tijdlijn met afspelen/geluid.
  function initClipEditor(form) {
    var stage = document.getElementById("ed-stage"), video = document.getElementById("ed-video");
    var canvas = document.getElementById("ed-preview"), ctx = canvas.getContext("2d");
    var out = document.getElementById("layout-json"), modeIn = document.getElementById("layout-mode");
    var seek = document.getElementById("ed-seek"), timeEl = document.getElementById("ed-time");
    var divider = document.getElementById("ed-divider"), wrap = document.getElementById("ed-preview-wrap");
    var clamp = function (v, a, b) { return Math.min(b, Math.max(a, v)); };
    var SW = 1920, SH = 1080, T0 = 0, T1 = 0;
    if (stage) {
      SW = parseFloat(stage.getAttribute("data-src-w")) || 1920; SH = parseFloat(stage.getAttribute("data-src-h")) || 1080;
      T0 = parseFloat(stage.getAttribute("data-start")) || 0; T1 = parseFloat(stage.getAttribute("data-end")) || T0 + 30;
    }
    var st = { mode: modeIn.value || "auto", top: 0.35, cam: null, game: null, rect: null };
    var auto = null;
    try {
      auto = JSON.parse(out.value || "null");
      if (auto && auto.type === "split") { st.cam = auto.cam; st.game = auto.game; st.top = auto.top || 0.35; }
      if (auto && auto.type === "crop") { st.rect = auto.rect; }
    } catch (e) { auto = null; }

    function A(k) {  // gewenste verhouding (breedte/hoogte, pixels) van het doelvak
      if (k === "cam") return 1080 / (1920 * st.top);
      if (k === "game") return 1080 / (1920 * (1 - st.top));
      return 1080 / 1920;
    }
    function hFor(w, k) { return w * SW / (A(k) * SH); }
    function fitAround(r, k) {  // zelfde midden en breedte, hoogte volgens verhouding; binnen het beeld
      var cx = r[0] + r[2] / 2, cy = r[1] + r[3] / 2, w = r[2], h = hFor(w, k);
      if (h > 1) { h = 1; w = h * SH * A(k) / SW; }
      if (w > 1) { w = 1; h = hFor(w, k); }
      return [clamp(cx - w / 2, 0, 1 - w), clamp(cy - h / 2, 0, 1 - h), w, h];
    }
    function centered(k) { return fitAround([0, 0, 1, 1], k); }
    var fc = null;
    try { fc = stage ? JSON.parse(stage.getAttribute("data-facecam") || "null") : null; } catch (e) { fc = null; }
    st.cam = fitAround(st.cam || fc || [0.7, 0.65, 0.28, 0.3], "cam");
    st.game = fitAround(st.game || centered("game"), "game");
    st.rect = fitAround(st.rect || centered("rect"), "rect");

    // ---- kaders
    var boxes = stage ? { cam: stage.querySelector(".cam"), game: stage.querySelector(".game"), rect: stage.querySelector(".crop") } : {};
    function placeBoxes() {
      if (!stage) return;
      Object.keys(boxes).forEach(function (k) {
        var el = boxes[k], r = st[k];
        var show = (st.mode === "split" && (k === "cam" || k === "game")) || (st.mode === "crop" && k === "rect");
        el.hidden = !show;
        el.style.left = r[0] * 100 + "%"; el.style.top = r[1] * 100 + "%";
        el.style.width = r[2] * 100 + "%"; el.style.height = r[3] * 100 + "%";
      });
      stage.classList.toggle("is-auto", st.mode === "auto");
    }
    function pos(e) {
      var b = stage.getBoundingClientRect();
      return { x: clamp((e.clientX - b.left) / b.width, 0, 1), y: clamp((e.clientY - b.top) / b.height, 0, 1) };
    }
    var drag = null;
    if (stage) stage.addEventListener("pointerdown", function (e) {
      var box = e.target.closest(".ed-box");
      if (!box || box.hidden) return;
      e.preventDefault();
      var k = box.getAttribute("data-k"), r = st[k], p = pos(e), h = e.target.closest(".h");
      Object.keys(boxes).forEach(function (o) { boxes[o].classList.toggle("front", o === k); });
      if (h) {  // schalen vanaf een hoek; de tegenoverliggende hoek blijft staan
        var west = h.classList.contains("nw") || h.classList.contains("sw");
        var north = h.classList.contains("nw") || h.classList.contains("ne");
        drag = { k: k, resize: true, ax: west ? r[0] + r[2] : r[0], ay: north ? r[1] + r[3] : r[1], west: west, north: north };
      } else {
        drag = { k: k, resize: false, dx: p.x - r[0], dy: p.y - r[1] };
      }
      stage.setPointerCapture(e.pointerId);
    });
    if (stage) stage.addEventListener("pointermove", function (e) {
      if (!drag) return;
      var p = pos(e), k = drag.k, r = st[k];
      if (drag.resize) {
        var w = Math.max(0.05, Math.abs(p.x - drag.ax));
        var maxW = drag.west ? drag.ax : 1 - drag.ax;
        var maxH = drag.north ? drag.ay : 1 - drag.ay;
        w = Math.min(w, maxW);
        var h = hFor(w, k);
        if (h > maxH) { h = maxH; w = h * SH * A(k) / SW; }
        st[k] = [drag.west ? drag.ax - w : drag.ax, drag.north ? drag.ay - h : drag.ay, w, h];
      } else {
        st[k] = [clamp(p.x - drag.dx, 0, 1 - r[2]), clamp(p.y - drag.dy, 0, 1 - r[3]), r[2], r[3]];
      }
      placeBoxes(); sync();
    });
    if (stage) stage.addEventListener("pointerup", function () { drag = null; });

    // ---- schuifbalk tussen facecam en gameplay (in het voorbeeld)
    var ddrag = false;
    divider.addEventListener("pointerdown", function (e) { if (st.mode !== "split") return; e.preventDefault(); ddrag = true; divider.setPointerCapture(e.pointerId); });
    divider.addEventListener("pointermove", function (e) {
      if (!ddrag) return;
      var b = wrap.getBoundingClientRect();
      st.top = clamp(Math.round(((e.clientY - b.top) / b.height) * 100) / 100, 0.2, 0.6);
      st.cam = fitAround(st.cam, "cam"); st.game = fitAround(st.game, "game");
      placeBoxes(); sync();
    });
    divider.addEventListener("pointerup", function () { ddrag = false; });

    // ---- modus
    function setMode(m) {
      st.mode = m; modeIn.value = m;
      form.querySelectorAll(".ed-mode").forEach(function (b) { b.classList.toggle("active", b.getAttribute("data-mode") === m); });
      placeBoxes(); sync();
    }
    form.querySelectorAll(".ed-mode").forEach(function (b) {
      b.addEventListener("click", function () { if (!stage && b.getAttribute("data-mode") !== "auto") return; setMode(b.getAttribute("data-mode")); });
    });
    function sync() {
      if (st.mode === "split") out.value = JSON.stringify({ type: "split", cam: st.cam, game: st.game, top: st.top });
      else if (st.mode === "crop") out.value = JSON.stringify({ type: "crop", rect: st.rect });
      else out.value = "";
      divider.hidden = st.mode !== "split";
      divider.style.top = st.top * 100 + "%";
    }

    // ---- video en tijdlijn
    function fmt(t) { t = Math.max(0, t); var m = Math.floor(t / 60), s = Math.floor(t % 60); return m + ":" + (s < 10 ? "0" : "") + s; }
    var dur = Math.max(0.1, T1 - T0);
    if (video) {
      video.addEventListener("loadedmetadata", function () { video.currentTime = T0; });
      video.addEventListener("timeupdate", function () {
        if (video.currentTime >= T1 || video.currentTime < T0 - 0.5) video.currentTime = T0;
        var rel = clamp(video.currentTime - T0, 0, dur);
        if (!seeking) seek.value = rel.toFixed(2);
        timeEl.textContent = fmt(rel) + " / " + fmt(dur);
      });
      var seeking = false;
      seek.addEventListener("input", function () { seeking = true; video.currentTime = T0 + parseFloat(seek.value); });
      seek.addEventListener("change", function () { seeking = false; });
      var play = document.getElementById("ed-play"), mute = document.getElementById("ed-mute");
      play.addEventListener("click", function () { if (video.paused) video.play(); else video.pause(); });
      video.addEventListener("play", function () { play.textContent = "❚❚"; play.setAttribute("aria-label", "Pauzeren"); });
      video.addEventListener("pause", function () { play.textContent = "▶"; play.setAttribute("aria-label", "Afspelen"); });
      mute.addEventListener("click", function () { video.muted = !video.muted; mute.textContent = video.muted ? "🔇" : "🔊"; });
      form.addEventListener("click", function (e) {
        var j = e.target.closest("[data-jump]");
        if (j) { video.currentTime = T0 + parseFloat(j.getAttribute("data-jump")); }
      });
    } else {
      seek.disabled = true;
    }

    // ---- ondertitel-opties
    form.addEventListener("click", function (e) {
      var b = e.target.closest("[data-cap-pos]");
      if (!b) return;
      var v = b.getAttribute("data-cap-pos");
      if (v === "35" && st.mode === "split") v = String(Math.round(st.top * 100));
      document.getElementById("cap-pos").value = v;
    });
    function currentCaption() {
      var rel = video ? video.currentTime - T0 : 0, rows = form.querySelectorAll(".cap-row"), pick = null;
      rows.forEach(function (r) {
        var a = parseFloat(r.getAttribute("data-start")), b = parseFloat(r.getAttribute("data-end"));
        if (rel >= a - 0.05 && rel <= b + 0.3) pick = r;
      });
      if (!pick && !video) pick = rows[0];
      return pick ? pick.querySelector("input").value : "";
    }

    // ---- live voorbeeld
    function blit(r, dx, dy, dw, dh) {
      if (!video || video.readyState < 2) return;
      var vw = video.videoWidth, vh = video.videoHeight;
      ctx.drawImage(video, r[0] * vw, r[1] * vh, r[2] * vw, r[3] * vh, dx, dy, dw, dh);
    }
    function frame() {
      var W = canvas.width, H = canvas.height;
      ctx.fillStyle = "#000"; ctx.fillRect(0, 0, W, H);
      if (st.mode === "split") {
        var th = Math.round(H * st.top);
        blit(st.cam, 0, 0, W, th); blit(st.game, 0, th, W, H - th);
      } else if (st.mode === "crop") {
        blit(st.rect, 0, 0, W, H);
      } else if (auto && auto.type === "split") {
        var t2 = Math.round(H * (auto.top || 0.35));
        blit(auto.cam, 0, 0, W, t2); blit(auto.game, 0, t2, W, H - t2);
      } else if (auto && auto.type === "crop") {
        blit(auto.rect, 0, 0, W, H);
      } else {
        ctx.fillStyle = "#9aa4b2"; ctx.font = "16px system-ui"; ctx.textAlign = "center";
        ctx.fillText("Automatische indeling", W / 2, H / 2);
      }
      var on = form.querySelector('input[name="cap_enabled"]');
      var text = on && on.checked ? currentCaption() : "";
      if (text) {
        if (document.getElementById("cap-upper").checked) text = text.toUpperCase();
        var size = { S: 68, M: 86, L: 108 }[document.getElementById("cap-size").value] * W / 1080;
        var y = H * parseInt(document.getElementById("cap-pos").value, 10) / 100;
        var hi = { geel: "#FFE500", groen: "#5AE047", wit: "#FFFFFF" }[document.getElementById("cap-color").value] || "#FFE500";
        ctx.font = "bold " + size + "px 'DejaVu Sans', sans-serif"; ctx.textBaseline = "middle";
        ctx.lineJoin = "round"; ctx.lineWidth = Math.max(2, size / 7); ctx.strokeStyle = "#000";
        var words = text.split(/\s+/).slice(0, 4), widths = words.map(function (w) { return ctx.measureText(w + " ").width; });
        var total = widths.reduce(function (a, b) { return a + b; }, 0), x = W / 2 - total / 2;
        ctx.textAlign = "left";
        words.forEach(function (w, i) { ctx.strokeText(w, x, y); ctx.fillStyle = i === 0 ? hi : "#FFFFFF"; ctx.fillText(w, x, y); x += widths[i]; });
      }
      requestAnimationFrame(frame);
    }
    setMode(st.mode);
    requestAnimationFrame(frame);
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
