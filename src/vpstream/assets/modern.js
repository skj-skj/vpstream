// The vpstream player for browser mode, with touch controls: a phone, a tablet or a laptop.
// The stream's other look (player.js) is for a TV's remote; the owner picks one (--ui, or
// on the dashboard), and a page that is told the stream now uses the other loads again.
// ES5, and text from the server goes in with textContent only.
//
// What it does is what player.js does. How it talks to the daemon, the owner's commands and
// watching together are sync.js's, for both looks: watching together, a press goes to the
// group, and the page keeps close to the group by its playback rate.
//
// The controls: a tap in the middle plays or pauses, a double tap on the left or the right
// steps 10 s back or on (and each tap after it 10 s more), a tap elsewhere shows or hides
// the controls. The bar is dragged to seek. One sheet has the audio, the subtitles and their
// size. Full screen is the whole page where the browser can (Android, iPad, a computer); an
// iPhone can only show the video itself full screen, in its own player, where the soft
// subtitles go along as a track. Full screen turns the screen to the video's shape where the
// browser lets a page lock it; the rotate button turns it the other way, or, where it
// cannot be locked (an iPhone), turns the page on the screen.
//
// Watching together, full screen, the rotation and the subtitle size are this page's own;
// the rest is the group's. The viewer can give themselves a name (the face at the top): it
// goes with every report, and each page says who made a change of the group's ("Asha
// paused"), so a pause is never taken for a fault.
//
// Watching together over HTTPS, the viewers can call each other (call.js): Call starts a
// call, and every other page shows "Asha started a call" in the middle, with Join and Not
// now. In the call: End, Mic, Camera (and the other camera, where there is one), Zen (no
// tiles here; the others still see and hear this viewer) and, with more than 4 in the
// call, the pages of tiles. The tiles go in the black bars around the video (VpTiles), the
// others in the order they joined, then the viewer's own. A page that is not secure (plain
// HTTP) has none of it: the browser gives no camera there.
(function () {
  "use strict";
  // Where the tiles of a call go (see `place`), in pixels of the stage.
  var TILES = {
    perPage: 4,
    min: 72,  // a tile narrower than this does not fit in a bar
    pad: 6,  // from the bar's edges
    gap: 8,  // between two tiles
    top: 60,  // the top of the stage, under the top bar's buttons
    bottom: 104,  // ...and the bottom, under the bar and the buttons
    over: 0.18  // with no bar, each tile is this share of the stage's width, over the video
  };

  // Where the `n` tiles of a page go on a stage of w × h that shows a video of vw × vh
  // (16:9 until it is known), drawn whole (object-fit: contain): a list of {x, y, w, h,
  // bar}. A video narrower than the stage leaves bars at the sides: the right one holds
  // tiles 1 and 2, one over the other, between the top and the bottom controls, at 3:4;
  // the left one 3 and 4. A video wider than the stage (a phone held upright) leaves bars
  // above and below: the bottom one holds 1 and 2 side by side, the top one 3 and 4. When
  // the bars do not fit them all, the tiles go over the right of the video, one under the
  // other.
  function place(o) {
    var w = o.w, h = o.h, n = o.n;
    if (!n || !w || !h) { return []; }
    var vw = o.vw || 16, vh = o.vh || 9;
    var scale = Math.min(w / vw, h / vh);
    var side = (w - vw * scale) / 2, band = (h - vh * scale) / 2;
    var slots = [];
    var tw, th, x, y;
    if (side >= band) {
      var room = h - TILES.top - TILES.bottom;
      tw = side - 2 * TILES.pad;
      th = Math.min(tw * 4 / 3, (room - TILES.gap) / 2);
      tw = Math.min(tw, th * 3 / 4);
      if (tw >= TILES.min) {
        y = TILES.top + (room - 2 * th - TILES.gap) / 2;
        [w - side + (side - tw) / 2, (side - tw) / 2].forEach(function (left, i) {
          var bar = i ? "left" : "right";
          slots.push({ x: left, y: y, w: tw, h: th, bar: bar });
          slots.push({ x: left, y: y + th + TILES.gap, w: tw, h: th, bar: bar });
        });
      }
    } else {
      // The bottom bar first: above the controls; then the top one, under its buttons.
      [["bottom", h - band, band - TILES.bottom], ["top", TILES.top, band - TILES.top]].forEach(function (b) {
        th = b[2] - 2 * TILES.pad;
        tw = Math.min(th * 3 / 4, (w - 2 * TILES.pad - TILES.gap) / 2);
        th = Math.min(th, tw * 4 / 3);
        if (tw < TILES.min) { return; }
        x = (w - 2 * tw - TILES.gap) / 2;
        y = b[1] + (b[2] - th) / 2;
        slots.push({ x: x, y: y, w: tw, h: th, bar: b[0] });
        slots.push({ x: x + tw + TILES.gap, y: y, w: tw, h: th, bar: b[0] });
      });
    }
    if (slots.length >= n) { return slots.slice(0, n); }
    var space = h - TILES.top - TILES.bottom;
    tw = Math.max(TILES.min, w * TILES.over);
    th = Math.min(tw * 3 / 4, (space - (n - 1) * TILES.gap) / n);
    tw = th * 4 / 3;
    y = TILES.top + (space - n * th - (n - 1) * TILES.gap) / 2;
    var out = [];
    for (var i = 0; i < n; i++) {
      out.push({ x: w - TILES.pad - tw, y: y + i * (th + TILES.gap), w: tw, h: th, bar: "over" });
    }
    return out;
  }

  // The tiles in pages of TILES.perPage: how many pages, and those on page `at` (from 0).
  function paged(tiles, at) {
    var count = Math.max(1, Math.ceil(tiles.length / TILES.perPage));
    var page = Math.max(0, Math.min(at, count - 1));
    return { count: count, page: page, tiles: tiles.slice(page * TILES.perPage, (page + 1) * TILES.perPage) };
  }

  window.VpTiles = { place: place, paged: paged, sizes: TILES };

  var S = JSON.parse(document.getElementById("settings").textContent);
  var UI = "new";
  var HLS_TYPE = "application/vnd.apple.mpegurl";
  var HIDE_MS = 3500;
  var STEP = 10;
  // hls.js loads this far ahead, while the video plays and while it is paused (its own
  // default is 30 s): a slow link builds a cushion in a pause. At most BUFFER_BYTES, under
  // what a browser keeps for one video; and BEHIND seconds already played are kept, for a
  // step back, the rest let go.
  var BUFFER_SECONDS = 120;
  var BUFFER_BYTES = 150 * 1000 * 1000;
  var BEHIND_SECONDS = 90;
  var DOUBLE_MS = 300;  // two taps this close are a double tap
  var RUN_MS = 800;  // after a double tap, each tap on that side this soon is one step more
  var TOAST_MS = 4000;
  var REPORT_MS = 2000;
  var NAME_KEY = "vpstream-name";
  var MAX_NAME = 40;  // as clients.MAX_NAME
  // What is not printable, as Python's str.isprintable has it: a space in a name (see clean).
  var UNPRINTABLE = /[\u0000-\u001f\u007f-\u009f\u00ad\u061c\u180e\u200b-\u200f\u2028-\u202e\u2060-\u2064\ufeff]/g;

  var video = $("video");
  var hls = null;
  var current = "";  // the master playlist that is loaded
  var audio = S.start_audio;  // an index into S.audio; S.audio.length means Off
  var sub = S.start_sub;  // an index into S.subs; -1 means Off
  var cues = [];
  var subLoad = 0;  // counts subtitle loads, so a slow one that was replaced is dropped
  var hideTimer = null;
  var settled = false;  // the loaded version is at its start position: its time is real
  var seekWhenReady = null;  // a seek asked for before the video could take it
  // Subtitle sizes (see clients.SUB_SIZES and player.js): a share of the video's height.
  var SIZES = [
    { id: "xs", label: "Extra small", share: 0.03 },
    { id: "s", label: "Small", share: 0.0375 },
    { id: "m", label: "Medium", share: 0.045 },
    { id: "l", label: "Large", share: 0.055 },
    { id: "xl", label: "Extra large", share: 0.07 }
  ];
  var FLOOR = 0.5;
  var size = 2;  // an index into SIZES
  var name = readName();  // the viewer's own name, "" for none
  var T = S.together || null;  // watching together (see sync.js)
  var core = window.VpSync(S, video, UI, {
    settled: function () { return settled; },
    audio: function () { return audioNumber(audio); },
    sub: function () { return subId(sub); },
    size: function () { return SIZES[size].id; },
    name: function () { return name; },
    setAudio: setAudio,
    setSub: setSub,
    setSize: setSize,
    seekTo: seekTo,
    showControls: showControls,
    update: update,
    render: note,
    outOfSync: function () {},  // Sync shows by itself, over the video (see note)
    changed: announce,
    joined: function () {
      if (!name && !asked()) { toast("Tap the face at the top to tell the others your name.", ""); }
    },
    call: function (state) { return call ? call.update(state) : false; },
    callQuery: function () { return call ? call.query() : []; }
  });
  // The call of watching together, where this page can have one (see call.js).
  var call = T && T.call && window.VpCall ? window.VpCall(T.call, core.page, {
    changed: drawCall,
    said: function (text) { if (text) { hintHere(text); } },
    name: function () { return name; },
    audioBox: $("callsound")
  }) : null;
  if (call && !call.possible) { call = null; }
  var zen = false;
  var tilePage = 0;
  var tileEls = {};  // page -> its tile element
  var TRAY = ["hangup", "mic", "camera", "flip", "zen", "prevpage", "pageno", "nextpage"];
  var ICON_W = 46, PAGENO_W = 40;
  var AHEAD = 6;  // seconds of film loaded ahead, below which the call has no video  // what a button and the page number take in the row
  var squeezed = false, trayOpen = false;  // the call's buttons in the tray; the tray open
  var ownSeekAt = -Infinity;  // when this page last seeked the video itself

  function $(id) { return document.getElementById(id); }

  function say(text) { $("message").textContent = text || ""; }

  // A class of an element on or off. (className, for the browsers that have no classList.)
  function flag(el, cls, on) {
    var list = (el.className || "").split(/\s+/).filter(function (c) { return c && c !== cls; });
    if (on) { list.push(cls); }
    var next = list.join(" ");
    if (el.className !== next) { el.className = next; }
  }

  function has(el, cls) { return (" " + (el.className || "") + " ").indexOf(" " + cls + " ") >= 0; }

  function clock(seconds) {
    var s = Math.max(0, Math.floor(seconds || 0));
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    var mm = (h && m < 10 ? "0" : "") + m, ss = (r < 10 ? "0" : "") + r;
    return (h ? h + ":" : "") + mm + ":" + ss;
  }

  // --- loading a version: the base, or one with subtitles burned in

  function load(url, at, play) {
    if (hls) { hls.destroy(); hls = null; }
    current = url;
    settled = false;
    say("");
    if (window.Hls && window.Hls.isSupported()) {
      var recovered = false;
      var retries = 0;
      hls = new window.Hls({ enableWorker: true, startPosition: at > 0 ? at : -1,
        maxBufferLength: BUFFER_SECONDS, maxMaxBufferLength: BUFFER_SECONDS,
        maxBufferSize: BUFFER_BYTES, backBufferLength: BEHIND_SECONDS });
      hls.on(window.Hls.Events.FRAG_LOADED, function () { retries = 0; say(""); });
      hls.on(window.Hls.Events.MANIFEST_PARSED, function () {
        applyAudio();
        if (play) { start(); }
      });
      hls.on(window.Hls.Events.ERROR, function (event, data) {
        if (!data || !data.fatal) { return; }
        if (data.type === window.Hls.ErrorTypes.NETWORK_ERROR && retries < 3) {
          retries += 1;
          say("The connection dropped; trying again...");
          hls.startLoad();
        } else if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR && !recovered) {
          recovered = true;
          hls.recoverMediaError();
        } else {
          say("This video cannot be played here (" + data.details + ").");
        }
      });
      hls.loadSource(url);
      hls.attachMedia(video);
    } else if (video.canPlayType(HLS_TYPE)) {
      video.src = url;
      video.addEventListener("loadedmetadata", function once() {
        video.removeEventListener("loadedmetadata", once);
        if (at > 0) { video.currentTime = at; }
        applyAudio();
        if (play) { start(); }
      });
    } else {
      say("This browser cannot play HLS video.");
    }
  }

  function start() {
    if (T) { return core.startHere(); }
    var p = video.play();
    if (p && p["catch"]) {
      p["catch"](function () { showControls(); });  // a tap on Play starts it
    }
  }

  // --- audio

  function audioName(index) {
    return index >= S.audio.length ? "Off" : S.audio[index].label;
  }

  function applyAudio() {
    var off = audio >= S.audio.length;
    video.muted = off;
    if (!off && audio >= 0) {
      if (hls) {
        if (hls.audioTracks && hls.audioTracks.length > audio) { hls.audioTrack = audio; }
      } else if (video.audioTracks) {
        for (var i = 0; i < video.audioTracks.length; i++) {
          video.audioTracks[i].enabled = i === audio;
        }
      }
    }
    renderSheet();
  }

  // An audio track by its number, 0 for Off. False when this video has no such track.
  function setAudio(number) {
    var at = number === 0 ? S.audio.length : -1;
    for (var i = 0; i < S.audio.length && at < 0; i++) {
      if (S.audio[i].number === number) { at = i; }
    }
    if (at < 0 || !S.audio.length) { return false; }
    if (at !== audio) { audio = at; applyAudio(); }
    return true;
  }

  function audioNumber(index) { return index >= S.audio.length ? 0 : S.audio[index].number; }

  // --- subtitles: Off, or one that is prepared (soft, or a burned-in version)

  function subName(index) {
    if (index < 0) { return "Off"; }
    var s = S.subs[index];
    return s.label + (s.kind === "burn" ? " (burned in)" : "");
  }

  function softChoice() {
    var choice = sub >= 0 ? S.subs[sub] : null;
    return choice && choice.kind === "soft" ? choice : null;
  }

  function applySub() {
    var choice = sub >= 0 ? S.subs[sub] : null;
    var master = choice && choice.kind === "burn" ? choice.url : S.master;
    cues = [];
    draw();
    subLoad += 1;
    if (master !== current) { load(master, video.currentTime, !video.paused); }
    if (softChoice()) { fetchCues(choice.url, subLoad); }
    nativeSubs(nativeFull());
    renderSheet();
  }

  function fetchCues(url, ticket) {
    var x = new XMLHttpRequest();
    x.open("GET", url, true);
    x.onload = function () {
      if (ticket !== subLoad) { return; }
      if (x.status === 200) { cues = window.VpVtt.parse(x.responseText); draw(); }
      else { say("The subtitles could not be loaded."); }
    };
    x.onerror = function () {
      if (ticket === subLoad) { say("The subtitles could not be loaded."); }
    };
    x.send();
  }

  // Subtitles by their id ("2-soft", "3-burn") or "off". False when this video has none such.
  function setSub(id) {
    var at = id === "off" ? -1 : -2;
    for (var i = 0; i < S.subs.length && at === -2; i++) {
      if (S.subs[i].id === id) { at = i; }
    }
    if (at === -2) { return false; }
    if (at !== sub) { sub = at; applySub(); }
    return true;
  }

  function subId(index) { return index < 0 ? "off" : S.subs[index].id; }

  // --- subtitle size (see player.js)

  function sizeHeight() {
    var w = window.innerWidth || 0, h = window.innerHeight || 0;
    if (turned) { var t = w; w = h; h = t; }
    if (!w || !h) { return 0; }
    var shape = video.videoWidth && video.videoHeight ? video.videoHeight / video.videoWidth : 9 / 16;
    return Math.max(Math.min(FLOOR * h, w), Math.min(h, w * shape));
  }

  function applySize() {
    var height = sizeHeight();
    if (height) { $("overlay").style.fontSize = Math.round(SIZES[size].share * height) + "px"; }
    renderSheet();
    drawCall();  // the tiles go where the bars are now
  }

  // A size by its id. False for one that this page does not know.
  function setSize(id) {
    for (var i = 0; i < SIZES.length; i++) {
      if (SIZES[i].id === id) {
        if (i !== size) { size = i; applySize(); }
        return true;
      }
    }
    return false;
  }

  function draw() {
    var text = cues.length ? window.VpVtt.at(cues, video.currentTime) : "";
    var box = $("overlay");
    if (box.getAttribute("data-text") === text) { return; }
    box.setAttribute("data-text", text);
    while (box.firstChild) { box.removeChild(box.firstChild); }
    if (text) {
      var span = document.createElement("span");
      span.textContent = text;
      box.appendChild(span);
    }
  }

  // --- the sheet: audio, subtitles and their size, each a list to choose from

  var sheetLocked = null;  // whether the sheet was drawn locked (watching together)

  function option(id, label, on, disabled, action) {
    var b = document.createElement("button");
    b.type = "button";
    b.id = id;
    b.className = "opt" + (on ? " on" : "");
    b.textContent = label;
    b.disabled = !!disabled;
    b.addEventListener("click", function (event) {
      event.stopPropagation();
      if (!b.disabled) { action(); }
    });
    return b;
  }

  function section(title, buttons) {
    var box = document.createElement("div");
    box.className = "group";
    var h = document.createElement("h3");
    h.textContent = title;
    box.appendChild(h);
    var list = document.createElement("div");
    list.className = "opts";
    buttons.forEach(function (b) { list.appendChild(b); });
    box.appendChild(list);
    return box;
  }

  function renderSheet() {
    var body = $("sheetbody");
    if (!body) { return; }
    var off = core.locked();
    sheetLocked = off;
    while (body.firstChild) { body.removeChild(body.firstChild); }
    if (S.audio.length) {
      var a = [];
      for (var i = 0; i <= S.audio.length; i++) {
        a.push(option("opt-audio-" + i, audioName(i), i === audio, off, chooseAudio(i)));
      }
      body.appendChild(section("Audio", a));
    }
    if (S.subs.length) {
      var s = [option("opt-sub-off", "Off", sub < 0, off, chooseSub(-1))];
      for (var j = 0; j < S.subs.length; j++) {
        s.push(option("opt-sub-" + S.subs[j].id, subName(j), j === sub, off, chooseSub(j)));
      }
      body.appendChild(section("Subtitles", s));
    }
    // Only the soft subtitles are drawn here, so only they have a size.
    if (softChoice()) {
      var z = SIZES.map(function (one, k) {
        return option("opt-size-" + one.id, one.label, k === size, false, chooseSize(k));
      });
      body.appendChild(section("Subtitle size", z));
    }
    if (!body.firstChild) {
      var none = document.createElement("p");
      none.className = "none";
      none.textContent = "This video has no other audio or subtitles.";
      body.appendChild(none);
    }
    var cc = $("cc");
    if (cc) { flag(cc, "on", sub >= 0); }
  }

  function chooseAudio(index) {
    return function () {
      if (T) { return core.propose("audio", audioNumber(index)); }
      core.mark("audio");
      audio = index;
      applyAudio();
    };
  }

  function chooseSub(index) {
    return function () {
      if (T) { return core.propose("sub", subId(index)); }
      core.mark("sub");
      if (index !== sub) { sub = index; applySub(); }
    };
  }

  function chooseSize(index) {
    return function () {  // this page's own, also watching together
      core.mark("size");
      if (index !== size) { size = index; applySize(); }
    };
  }

  function openSheet(on) {
    $("sheet").hidden = !on;
    if (on) { renderSheet(); }
    showControls();
  }

  // --- the controls: shown while paused, a sheet is open or for a while after a tap

  function controlsShown() { return has(document.body, "controls"); }

  function busy() { return trayOpen || !$("sheet").hidden || !$("namebox").hidden || !$("list").hidden || dragging !== null; }

  function showControls() {
    flag(document.body, "controls", true);
    if (hideTimer) { window.clearTimeout(hideTimer); }
    hideTimer = window.setTimeout(function () {
      hideTimer = null;
      if (!video.paused && !busy()) { flag(document.body, "controls", false); }
    }, HIDE_MS);
  }

  function hideControls() {
    if (hideTimer) { window.clearTimeout(hideTimer); hideTimer = null; }
    if (!busy()) { flag(document.body, "controls", false); }
  }

  // A big play or pause sign in the middle, for a moment: what a tap there did.
  var flashTimer = null;
  function flash(kind) {
    var box = $("flash");
    box.setAttribute("data-kind", kind || "play");
    flag(box, "show", true);
    if (flashTimer) { window.clearTimeout(flashTimer); }
    flashTimer = window.setTimeout(function () { flag(box, "show", false); }, 600);
  }

  // --- taps on the video

  var lastTap = 0, lastZone = null, tapTimer = null;
  var runZone = 0, runAt = 0, runBy = 0, rippleTimer = null;

  // Which third of the video a tap is in: -1 the left, 0 the middle, 1 the right. Turned
  // (see rotate), the video's left is the top of the screen.
  function zone(event) {
    var along = turned ? event.clientY : event.clientX;
    var span = turned ? window.innerHeight : window.innerWidth;
    if (!span || typeof along !== "number") { return 0; }
    var at = along / span;
    return at < 0.35 ? -1 : at > 0.65 ? 1 : 0;
  }

  function tap(event) {
    if (!$("join").hidden) { return; }
    if (trayOpen) {
      openTray(false);
      showControls();
      return;
    }
    var where = zone(event);
    var now = Date.now();
    if (where !== 0 && ((where === lastZone && now - lastTap < DOUBLE_MS) ||
        (where === runZone && now - runAt < RUN_MS))) {
      if (tapTimer) { window.clearTimeout(tapTimer); tapTimer = null; }
      lastTap = 0;
      doubleStep(where, now);
      return;
    }
    lastTap = now;
    lastZone = where;
    if (where === 0) {
      togglePlay();
      return;
    }
    // A tap at the side waits a moment: it may be the first of a double tap.
    if (tapTimer) { window.clearTimeout(tapTimer); }
    tapTimer = window.setTimeout(function () {
      tapTimer = null;
      if (controlsShown()) { hideControls(); } else { showControls(); }
    }, DOUBLE_MS);
  }

  function doubleStep(where, now) {
    if (runZone !== where || now - runAt >= RUN_MS) { runBy = 0; }
    runZone = where;
    runAt = now;
    if (core.locked()) { showControls(); return; }
    runBy += STEP;
    step(where * STEP)();
    var ripple = $(where < 0 ? "rippleback" : "rippleforward");
    var other = $(where < 0 ? "rippleforward" : "rippleback");
    flag(other, "show", false);
    ripple.querySelector("b").textContent = (where < 0 ? "−" : "+") + runBy + " s";
    flag(ripple, "show", true);
    if (rippleTimer) { window.clearTimeout(rippleTimer); }
    rippleTimer = window.setTimeout(function () {
      flag($("rippleback"), "show", false);
      flag($("rippleforward"), "show", false);
    }, RUN_MS);
  }

  // --- the bar: a tap or a drag along it seeks, on letting go

  var dragging = null;  // the share of the video the drag is at, while one goes on

  function share(event) {
    var p = event.touches && event.touches[0] || event.changedTouches && event.changedTouches[0] || event;
    var box = $("bar").getBoundingClientRect();
    var at = turned ? (p.clientY - box.top) / (box.height || 1) : (p.clientX - box.left) / (box.width || 1);
    return Math.max(0, Math.min(1, at));
  }

  function dragTo(at) {
    dragging = at;
    var d = video.duration || 0;
    $("bubble").textContent = clock(at * d);
    $("bubble").style.left = (100 * at) + "%";
    flag($("bar"), "drag", true);
    paintBar();
  }

  function grab(event) {
    if (!video.duration || !isFinite(video.duration) || core.locked()) { showControls(); return; }
    if (event.preventDefault) { event.preventDefault(); }
    if (event.stopPropagation) { event.stopPropagation(); }
    dragTo(share(event));
    showControls();
  }

  function slide(event) {
    if (dragging === null) { return; }
    if (event.preventDefault) { event.preventDefault(); }
    dragTo(share(event));
  }

  function release(event) {
    if (dragging === null) { return; }
    if (event && event.stopPropagation) { event.stopPropagation(); }
    var cancelled = event && (event.type === "pointercancel" || event.type === "touchcancel");
    var placed = event && (typeof event.clientX === "number" || event.changedTouches);
    var at = cancelled ? null : placed ? share(event) : dragging;
    dragging = null;
    flag($("bar"), "drag", false);
    if (at !== null && video.duration) {
      var to = video.duration * at;
      if (T) { core.propose("seek", Math.max(0, to).toFixed(1)); } else { core.mark("seek"); seekTo(to); }
    }
    update();
    showControls();
  }

  // --- full screen and the screen's rotation

  var root = document.documentElement;
  var turned = false;  // the page is turned on the screen (see rotate)
  var turnedWide = false;  // ...from a window that was wide (so a turn of the phone undoes it)
  var lockAfterFull = null;  // the rotate button entered full screen, to lock to this

  function fullElement() {
    return document.fullscreenElement || document.webkitFullscreenElement || null;
  }

  // The whole page full screen: everywhere but an iPhone.
  function pageFull() {
    return !!(document.fullscreenEnabled || document.webkitFullscreenEnabled) &&
      !!(root.requestFullscreen || root.webkitRequestFullscreen);
  }

  // An iPhone: only the video itself, in the system's player.
  function videoFull() { return typeof video.webkitEnterFullscreen === "function"; }

  function nativeFull() { return !!video.webkitDisplayingFullscreen; }

  function canFull() { return pageFull() || videoFull(); }

  function toggleFull() {
    if (pageFull()) {
      var asked;
      if (fullElement()) {
        asked = document.exitFullscreen ? document.exitFullscreen() : document.webkitExitFullscreen();
      } else {
        asked = root.requestFullscreen ? root.requestFullscreen() : root.webkitRequestFullscreen();
      }
      if (asked && asked["catch"]) {
        asked["catch"](function () {
          lockAfterFull = null;  // the rotate button's lock is for that full screen only
          hintHere("Full screen is not allowed here.");
        });
      }
    } else if (videoFull()) {
      if (video.readyState < 1) { hintHere("Full screen works once the video has loaded."); return; }
      nativeSubs(true);
      try { video.webkitEnterFullscreen(); } catch (e) { nativeSubs(false); hintHere("Full screen is not allowed here."); }
    }
  }

  function wideScreen() { return (window.innerWidth || 0) > (window.innerHeight || 0); }

  function wideVideo() { return !video.videoWidth || !video.videoHeight || video.videoWidth >= video.videoHeight; }

  function orientation() { return window.screen && window.screen.orientation; }

  // Lock the screen to "landscape" or "portrait"; `failed` runs where it cannot be locked.
  function lockTo(kind, failed) {
    var o = orientation();
    if (!o || typeof o.lock !== "function") { if (failed) { failed(); } return; }
    try {
      var p = o.lock(kind);
      if (p && p["catch"]) { p["catch"](function () { if (failed) { failed(); } }); }
    } catch (e) {
      if (failed) { failed(); }
    }
  }

  function unlock() {
    var o = orientation();
    if (o && typeof o.unlock === "function") { try { o.unlock(); } catch (e) { /* none to undo */ } }
  }

  function fullChanged() {
    var full = !!fullElement();
    flag(document.body, "full", full);
    if (full) {
      turn(false);
      var kind = lockAfterFull || (wideVideo() ? "landscape" : "portrait");
      lockAfterFull = null;
      lockTo(kind);
    } else {
      unlock();
    }
    applySize();
    showControls();
  }

  // The rotate button: the screen the other way. Where the browser locks the screen only in
  // full screen, it goes full screen first; where it cannot lock it at all, the page is
  // turned a quarter on the screen.
  function rotate() {
    var want = wideScreen() ? "portrait" : "landscape";
    var o = orientation();
    var locks = !!o && typeof o.lock === "function" && pageFull();
    if (!locks) { turn(!turned); return; }
    if (!fullElement()) {
      lockAfterFull = want;
      toggleFull();
      return;
    }
    lockTo(want, function () { turn(!turned); });
  }

  function turn(on) {
    if (turned === on) { return; }
    turned = on;
    turnedWide = wideScreen();
    flag(document.body, "turned", on);
    applySize();
  }

  // An iPhone's own full screen shows no part of the page: the soft subtitles go into it as
  // a track of the video, and out again after.
  function nativeSubs(on) {
    var old = $("nativesubs");
    if (old) { old.parentNode.removeChild(old); }
    var choice = softChoice();
    if (!on || !choice || !videoFull()) { return; }
    var t = document.createElement("track");
    t.id = "nativesubs";
    t.setAttribute("kind", "subtitles");
    t.setAttribute("src", choice.url);
    t.setAttribute("label", choice.label);
    t.setAttribute("default", "");
    video.appendChild(t);
    if (t.track) { try { t.track.mode = "showing"; } catch (e) { /* the default shows it */ } }
  }

  function seekTo(t) {
    if (video.readyState < 1) { seekWhenReady = t; return; }
    var end = video.duration || Infinity;
    ownSeekAt = Date.now();
    video.currentTime = Math.max(0, Math.min(t, end - 0.5));
    draw();
  }

  // --- the viewer's name (watching together): kept in this browser

  function readName() {
    try { return clean((window.localStorage && window.localStorage.getItem(NAME_KEY)) || ""); }
    catch (e) { return ""; }
  }

  // One line, at most MAX_NAME characters, as the daemon keeps it (clients.name_value:
  // what is not printable is a space). Never cut inside a character that takes two code
  // units (an emoji): half of one cannot go in a report (encodeURIComponent throws).
  function clean(text) {
    return whole(String(text || "")).replace(UNPRINTABLE, " ").replace(/\s+/g, " ")
      .replace(/^ /, "").slice(0, MAX_NAME).replace(/[\ud800-\udbff]$/, "").replace(/ $/, "");
  }

  // Half of a character that takes two code units, on its own, is a space.
  function whole(text) {
    var out = "";
    for (var i = 0; i < text.length; i++) {
      var c = text.charCodeAt(i);
      if (c >= 0xd800 && c <= 0xdbff && i + 1 < text.length &&
          text.charCodeAt(i + 1) >= 0xdc00 && text.charCodeAt(i + 1) <= 0xdfff) {
        out += text.charAt(i) + text.charAt(i + 1);
        i += 1;
      } else {
        out += c >= 0xd800 && c <= 0xdfff ? " " : text.charAt(i);
      }
    }
    return out;
  }

  function saveName(text) {
    name = clean(text);
    try {
      if (window.localStorage) {
        if (name) { window.localStorage.setItem(NAME_KEY, name); } else { window.localStorage.removeItem(NAME_KEY); }
      }
    } catch (e) { /* a private window: the name lasts as long as the page */ }
    showName();
    core.reportSoon();
  }

  // Whether this browser was asked for a name already: it is asked once, not on each video.
  function asked() {
    try {
      if (!window.localStorage) { return false; }
      if (window.localStorage.getItem(NAME_KEY + "-asked")) { return true; }
      window.localStorage.setItem(NAME_KEY + "-asked", "1");
    } catch (e) { /* a private window: asked on each page */ }
    return false;
  }

  // The first character, whole: an emoji is two code units.
  function initial(text) {
    var first = /^[\ud800-\udbff][\udc00-\udfff]/.test(text || "") ? text.slice(0, 2) : (text || "").charAt(0);
    return first.toUpperCase();
  }

  // A colour of its own for each name, so the faces in a notice tell people apart.
  function hue(text) {
    var h = 0;
    for (var i = 0; i < text.length; i++) { h = (h * 31 + text.charCodeAt(i)) % 360; }
    return h;
  }

  function face(el, who) {
    el.textContent = initial(who);
    el.style.background = who ? "hsl(" + hue(who) + ", 55%, 42%)" : "";
    flag(el, "anon", !who);
  }

  function showName() {
    face($("initial"), name);
    $("me").setAttribute("aria-label", name ? "Your name: " + name : "Set your name");
    $("me").title = name ? name : "Set your name";
  }

  function openName(on) {
    $("namebox").hidden = !on;
    if (on) {
      $("name").value = name;
      if ($("name").focus) { $("name").focus(); }
    }
    showControls();
  }

  // --- watching together

  // A notice of this page's own (not the group's), on its own or watching together.
  var hereUntil = 0;
  function hintHere(text) {
    if (T) { core.hint(text); return; }
    hereUntil = Date.now() + 4000;
    $("note").textContent = text;
    window.setTimeout(function () {
      if (Date.now() >= hereUntil && $("note").textContent === text) { $("note").textContent = ""; }
    }, 4000);
  }

  // --- who changed the group: "Asha paused", "The host jumped to 12:30"

  function who(c) {
    if (c.name) { return c.name; }
    return c.by === "owner" ? "The host" : "Someone";
  }

  function did(c, s) {
    if (c.what === "pause") { return "paused"; }
    if (c.what === "play") { return "pressed play"; }
    if (c.what === "seek") { return "jumped to " + clock(s.position); }
    if (c.what === "audio") {
      if (s.audio === 0) { return "turned the audio off"; }
      for (var i = 0; i < S.audio.length; i++) {
        if (S.audio[i].number === s.audio) { return "switched the audio to " + S.audio[i].label; }
      }
      return "switched the audio";
    }
    if (c.what === "sub") {
      if (s.sub === "off") { return "turned the subtitles off"; }
      for (var j = 0; j < S.subs.length; j++) {
        if (S.subs[j].id === s.sub) { return "turned on the subtitles: " + subName(j); }
      }
      return "switched the subtitles";
    }
    if (c.what === "video") {
      var names = (T && T.names) || [];
      return "switched to " + (names[s.video - 1] ? s.video + ". " + names[s.video - 1] : "video " + s.video);
    }
    return "";
  }

  // Say who made the group's change, unless it was this page. A page that just loaded says
  // only a change made a moment ago: the other video it came to.
  function announce(s, first) {
    var list = (s.changes || []).filter(function (c) { return c && c.page !== core.page && did(c, s); });
    if (!list.length) { return; }
    if (first && !(typeof s.changed_ago === "number" && s.changed_ago <= 5)) { return; }
    var people = [], words = {};
    list.forEach(function (c) {
      var key = who(c);
      if (!(key in words)) { people.push(key); words[key] = []; }
      words[key].push(did(c, s));
    });
    var text = people.map(function (key) { return key + " " + words[key].join(" and "); }).join(" · ");
    toast(text, people.length === 1 && list[0].name ? list[0].name : "");
  }

  var toastTimer = null;
  function toast(text, person) {
    $("toasttext").textContent = text;
    face($("toastface"), person || "");
    $("toastface").hidden = !person;
    $("toast").hidden = false;
    if (toastTimer) { window.clearTimeout(toastTimer); }
    toastTimer = window.setTimeout(function () { $("toast").hidden = true; }, TOAST_MS);
  }

  // --- the call (call.js): its buttons, the ring, and the tiles

  var HEADPHONES_KEY = "vpstream-call-hint";
  var SVG = "http://www.w3.org/2000/svg";

  // The stage's size: turned (see rotate), its width is the window's height.
  function stageSize() {
    var w = window.innerWidth || 0, h = window.innerHeight || 0;
    return turned ? { w: h, h: w } : { w: w, h: h };
  }

  // Once in this browser: the film's sound through the speakers reaches the mic.
  function headphones() {
    try {
      if (!window.localStorage || window.localStorage.getItem(HEADPHONES_KEY)) { return; }
      window.localStorage.setItem(HEADPHONES_KEY, "1");
    } catch (e) { /* a private window: said each time */ }
    toast("Use headphones: through the speakers, the film's sound reaches the others by your mic.", "");
  }

  // Call, or Join: the browser asks for the camera and the mic first.
  function joinCall() {
    call.start();
    headphones();
  }

  function drawCall() {
    if (!call) { return; }
    var inside = call.inCall();
    var live = call.running() && !inside;
    $("call").hidden = inside;
    $("call").disabled = call.joining();
    flag($("call"), "live", live);
    $("call").setAttribute("aria-label", live ? "Join the call" : "Call");
    ["hangup", "mic", "camera", "zen"].forEach(function (id) { $(id).hidden = !inside; });
    $("flip").hidden = !inside || !call.cam() || !call.canFlip();
    flag($("mic"), "off", !call.mic());
    $("mic").setAttribute("aria-label", call.mic() ? "Mute" : "Unmute");
    flag($("camera"), "off", !call.cam());
    $("camera").setAttribute("aria-label", call.cam() ? "Turn the camera off" : "Turn the camera on");
    flag($("zen"), "on", zen);
    $("zen").setAttribute("aria-label", zen ? "Show the tiles" : "Hide the tiles");
    fitRow();
    drawRing();
    drawTiles();
  }

  // The call's buttons go in the row while it has room for them; on a narrower screen (a
  // phone held upright) they go in a tray above it, behind the three dots, so Audio and
  // subtitles, Rotate and Full screen always show.
  function fitRow() {
    var row = $("row"), tray = $("tray");
    var used = $("clock").offsetWidth || 0, extra = 0;
    for (var i = 0; i < row.childNodes.length; i++) {
      var el = row.childNodes[i];
      if (el.nodeName === "BUTTON" && !el.hidden && el.id !== "more" && TRAY.indexOf(el.id) < 0) { used += ICON_W; }
    }
    TRAY.forEach(function (id) { if (!$(id).hidden) { extra += id === "pageno" ? PAGENO_W : ICON_W; } });
    var room = row.clientWidth || 0;  // 0: not laid out yet
    var squeeze = call.inCall() && room > 0 && used + extra > room;
    if (squeeze !== squeezed) {
      squeezed = squeeze;
      TRAY.forEach(function (id) {
        if (squeeze) { tray.appendChild($(id)); } else { row.insertBefore($(id), $("next")); }
      });
    }
    if (!squeeze) { trayOpen = false; }
    $("more").hidden = !squeeze;
    tray.hidden = !trayOpen;
    flag($("more"), "on", trayOpen);
    $("more").setAttribute("aria-expanded", trayOpen ? "true" : "false");
  }

  function openTray(on) {
    trayOpen = on;
    fitRow();
  }

  // "Asha started a call. Ravi is in it too. The video plays on."
  function drawRing() {
    var on = call.ringing();
    if ($("ring").hidden === on) { $("ring").hidden = !on; }
    if (!on) { return; }
    var by = call.by();
    face($("ringface"), by);
    $("ringtitle").textContent = (by || "Someone") + " started a call";
    var others = call.others();
    var named = others.filter(function (n) { return n; });
    var also = "";
    if (others.length && named.length === others.length && others.length <= 2) {
      also = named.join(" and ") + (others.length === 1 ? " is" : " are") + " in it too. ";
    } else if (others.length) {
      also = others.length + (others.length === 1 ? " other is" : " others are") + " in it too. ";
    }
    $("ringtext").textContent = also + "The video plays on.";
  }

  function makeTile(page) {
    var el = document.createElement("div");
    el.className = "tile";
    el.setAttribute("data-page", page);
    var v = document.createElement("video");
    v.setAttribute("autoplay", "");
    v.setAttribute("playsinline", "");
    v.muted = true;  // the sound plays apart, in call.js's own audio
    var initial = document.createElement("span");
    initial.className = "face";
    var label = document.createElement("span");
    label.className = "tname";
    var state = document.createElement("span");
    state.className = "tstate";
    // The mic-off sign, at the bottom right of a muted tile.
    var mute = document.createElement("span");
    mute.className = "tmute";
    var svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("class", "ic");
    svg.setAttribute("viewBox", "0 0 24 24");
    ["M12 3a3 3 0 0 1 3 3v5a3 3 0 0 1-6 0V6a3 3 0 0 1 3-3zM5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21", "M4 4l16 16"].forEach(function (d) {
      var path = document.createElementNS(SVG, "path");
      path.setAttribute("d", d);
      svg.appendChild(path);
    });
    mute.appendChild(svg);
    [v, initial, label, state, mute].forEach(function (child) { el.appendChild(child); });
    // A tap on a tile is not a tap on the video: it neither plays, pauses nor steps.
    el.addEventListener("click", function (event) { event.stopPropagation(); showControls(); });
    tileEls[page] = el;
    return el;
  }

  function fillTile(el, t) {
    var v = el.childNodes[0], initial = el.childNodes[1], label = el.childNodes[2];
    var state = el.childNodes[3], mute = el.childNodes[4];
    var stream = t.cam && t.stream ? t.stream : null;
    if (v.srcObject !== stream) {
      v.srcObject = stream;
      var playing = stream && v.play && v.play();
      if (playing && playing["catch"]) { playing["catch"](function () {}); }
    }
    v.hidden = !stream;
    initial.hidden = !!stream;  // the camera is off, or its video has not come yet
    face(initial, t.name);
    label.textContent = t.mine ? (t.name ? t.name + " (you)" : "You") : (t.name || "Someone");
    state.textContent = t.mine ? "" : t.state === "failed" ? "Could not connect" :
      t.state === "connecting" ? "Connecting..." : "";
    mute.hidden = t.mic;
    flag(el, "mirror", !!(t.mine && t.front));
  }

  function dropTile(page) {
    var el = tileEls[page];
    delete tileEls[page];
    el.childNodes[0].srcObject = null;
    if (el.parentNode) { el.parentNode.removeChild(el); }
  }

  // The tiles of the page of tiles shown, where they go; the others are told which of
  // them show here (only those send their video). In Zen, in an iPhone's own full screen
  // or with the tab put away, none.
  function drawTiles() {
    var all = call.inCall() ? call.tiles() : [];
    var at = paged(all, tilePage);
    tilePage = at.page;
    var many = all.length > TILES.perPage;
    ["prevpage", "pageno", "nextpage"].forEach(function (id) { $(id).hidden = !many; });
    $("prevpage").disabled = at.page === 0;
    $("nextpage").disabled = at.page >= at.count - 1;
    $("pageno").textContent = (at.page + 1) + "/" + at.count;
    var shown = zen || nativeFull() || !all.length ? [] : at.tiles;
    var size = stageSize();
    var spots = place({ w: size.w, h: size.h, vw: video.videoWidth, vh: video.videoHeight, n: shown.length });
    var keep = {};
    shown.forEach(function (t, i) {
      var el = tileEls[t.page] || makeTile(t.page);
      keep[t.page] = true;
      fillTile(el, t);
      el.style.left = Math.round(spots[i].x) + "px";
      el.style.top = Math.round(spots[i].y) + "px";
      el.style.width = Math.round(spots[i].w) + "px";
      el.style.height = Math.round(spots[i].h) + "px";
      el.setAttribute("data-bar", spots[i].bar);
      if (el.parentNode !== $("tiles")) { $("tiles").appendChild(el); }
    });
    for (var page in tileEls) { if (!keep[page]) { dropTile(page); } }
    var seen = document.hidden ? [] : shown.filter(function (t) { return !t.mine; });
    call.show(seen.map(function (t) { return t.page; }));
  }

  // Whether the film plays and is short of data: it waits, or has less than AHEAD seconds
  // loaded ahead of it, short of the end. A paused film is not (the group talks).
  function short() {
    if (video.paused || video.ended) { return false; }
    if (video.readyState < 3) { return true; }
    var at = video.currentTime, b = video.buffered;
    for (var i = 0; b && i < b.length; i++) {
      if (b.start(i) <= at + 0.5 && at < b.end(i)) {
        return b.end(i) - at < AHEAD && b.end(i) < (video.duration || Infinity) - 0.5;
      }
    }
    return true;
  }

  function turnTiles(by) {
    tilePage += by;
    drawCall();
  }

  // What the controls say while this page waits, and which of them work.
  function note() {
    if (!T) { return; }
    var now = core.status();
    if ($("note").textContent !== now.text) { $("note").textContent = now.text; }
    ["play", "back", "forward", "episodes", "next"].forEach(function (id) {
      if ($(id).disabled !== now.locked) { $(id).disabled = now.locked; }
    });
    flag($("bar"), "locked", now.locked);
    if (sheetLocked !== now.locked) { renderSheet(); }
    if ($("sync").hidden === now.behind) { $("sync").hidden = !now.behind; }
    if ($("join").hidden === now.join) { $("join").hidden = !now.join; }
  }

  function showList() {
    var box = $("list");
    while (box.firstChild) { box.removeChild(box.firstChild); }
    var head = document.createElement("h3");
    head.textContent = "Choose a video for everyone";
    box.appendChild(head);
    T.names.forEach(function (title, i) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "opt" + (i + 1 === S.number ? " on" : "");
      b.textContent = (i + 1) + ". " + title;
      b.addEventListener("click", function (event) {
        event.stopPropagation();
        box.hidden = true;
        core.propose("video", i + 1);
      });
      box.appendChild(b);
    });
    var close = document.createElement("button");
    close.type = "button";
    close.className = "opt back";
    close.textContent = "Back";
    close.addEventListener("click", function (event) { event.stopPropagation(); box.hidden = true; });
    box.appendChild(close);
    box.hidden = false;
  }

  function step(by) {
    return function () {
      if (T) { return core.propose("skip", by); }
      if (video.readyState < 1) { return; }
      core.mark("seek");
      seekTo(video.currentTime + by);
    };
  }

  function togglePlay() {
    if (T) {
      if (core.locked()) { showControls(); return; }
      var play = core.groupPaused();
      flash(play ? "play" : "pause");
      core.propose(play ? "play" : "pause");
    } else {
      flash(video.paused ? "play" : "pause");
      if (video.paused) { start(); } else { video.pause(); }
      core.reportSoon();
    }
    showControls();
  }

  // Up to where the video is loaded, from where it plays.
  function bufferedEnd() {
    var b = video.buffered;
    if (!b || !b.length) { return 0; }
    for (var i = 0; i < b.length; i++) {
      if (b.start(i) <= video.currentTime + 0.5 && b.end(i) >= video.currentTime) { return b.end(i); }
    }
    return 0;
  }

  function paintBar() {
    var d = video.duration || 0;
    var at = dragging !== null ? dragging : d ? video.currentTime / d : 0;
    var pct = (100 * Math.max(0, Math.min(1, at || 0))) + "%";
    $("fill").style.width = pct;
    $("thumb").style.left = pct;
    $("buffered").style.width = d ? (100 * Math.min(1, bufferedEnd() / d)) + "%" : "0";
  }

  function update() {
    if (video.readyState >= 2) { settled = true; }
    var d = video.duration || 0;
    paintBar();
    $("time").textContent = clock(dragging !== null ? dragging * d : video.currentTime);
    $("duration").textContent = d && isFinite(d) ? clock(d) : "";
    var on = core.playing();
    flag(document.body, "playing", on);
    $("play").setAttribute("aria-label", on ? "Pause" : "Play");
    draw();
  }

  function on(id, action) {
    $(id).addEventListener("click", function (event) {
      event.stopPropagation();
      if ($(id).disabled) { return; }
      action();
      showControls();
    });
  }

  // Taps on the bars themselves do not reach the video: they only keep the controls up.
  ["top", "bottom", "sheet", "namebox", "list", "toast", "ring"].forEach(function (id) {
    $(id).addEventListener("click", function (event) { event.stopPropagation(); showControls(); });
  });

  on("play", togglePlay);
  on("back", step(-STEP));
  on("forward", step(STEP));
  on("cc", function () { openSheet($("sheet").hidden); });
  on("sheetclose", function () { openSheet(false); });
  $("sheet").addEventListener("click", function (event) {
    if (event.target === $("sheet")) { openSheet(false); }  // the shade around the panel
  });
  on("episodes", function () {
    if (T) { return core.locked() || showList(); }
    window.location.href = S.index;
  });
  on("next", function () {
    if (T) { return S.next && core.propose("video", S.number + 1); }
    if (S.next) { window.location.href = S.next; }
  });
  if (call) {
    on("call", joinCall);
    on("more", function () { openTray(!trayOpen); });
    on("hangup", call.end);
    on("mic", call.toggleMic);
    on("camera", call.toggleCam);
    on("flip", call.flip);
    on("zen", function () { zen = !zen; drawCall(); });
    on("prevpage", function () { turnTiles(-1); });
    on("nextpage", function () { turnTiles(1); });
    on("ringjoin", joinCall);
    on("ringlater", call.dismiss);
    // The film comes first: while it is short of data, the call has no video.
    ["waiting", "playing", "pause", "progress"].forEach(function (kind) {
      video.addEventListener(kind, function () { call.film(short()); });
    });
    window.setInterval(function () { call.film(short()); }, 1000);
    document.addEventListener("visibilitychange", drawCall);
  }
  on("full", toggleFull);
  on("rotate", rotate);
  on("sync", core.syncHere);
  on("me", function () { openName($("namebox").hidden); });
  on("namecancel", function () { openName(false); });
  $("nameform").addEventListener("submit", function (event) {
    if (event.preventDefault) { event.preventDefault(); }
    saveName($("name").value);
    openName(false);
  });
  $("join").addEventListener("click", function (event) {
    event.stopPropagation();
    core.tapped();
  });
  $("taps").addEventListener("click", tap);

  // The bar: pointer events where the browser has them, else touches and the mouse.
  var bar = $("bar");
  if (window.PointerEvent) {
    bar.addEventListener("pointerdown", grab);
    document.addEventListener("pointermove", slide);
    document.addEventListener("pointerup", release);
    document.addEventListener("pointercancel", release);
  } else {
    bar.addEventListener("touchstart", grab);
    document.addEventListener("touchmove", slide);
    document.addEventListener("touchend", release);
    document.addEventListener("touchcancel", release);
    bar.addEventListener("mousedown", grab);
    document.addEventListener("mousemove", slide);
    document.addEventListener("mouseup", release);
  }
  bar.addEventListener("click", function (event) { event.stopPropagation(); });

  // A mouse that moves shows the controls; a finger's tap is told by the taps above (a
  // phone also sends a mouse move with each tap, which must not undo a tap that hides).
  if (window.PointerEvent) {
    document.addEventListener("pointermove", function (event) {
      if (event.pointerType === "mouse" && dragging === null) { showControls(); }
    });
  }

  document.addEventListener("keydown", function (event) {
    var key = event.key || "";
    if (!$("namebox").hidden) {
      if (key === "Escape") { openName(false); }
      return;  // the name is being typed
    }
    if (event.ctrlKey || event.metaKey || event.altKey) { return; }
    showControls();
    var target = event.target && event.target.tagName;
    var onButton = target === "BUTTON" || target === "INPUT" || target === "SELECT";
    if (key === "Escape") {
      openSheet(false);
      $("list").hidden = true;
    } else if ((key === " " || key === "k" || key === "MediaPlayPause") && !onButton) {
      togglePlay();
      event.preventDefault();
    } else if (key === "ArrowLeft" || key === "ArrowRight") {
      if (!core.locked()) { step(key === "ArrowLeft" ? -STEP : STEP)(); }
      event.preventDefault();
    } else if (key === "f" || key === "F") {
      toggleFull();
      event.preventDefault();
    } else if (key === "c" || key === "C") {
      openSheet($("sheet").hidden);
    }
  });
  document.addEventListener("fullscreenchange", fullChanged);
  document.addEventListener("webkitfullscreenchange", fullChanged);
  if (window.addEventListener) {
    window.addEventListener("resize", function () {
      // The phone was turned while the page was turned on it: the page goes back.
      if (turned && wideScreen() !== turnedWide) { turn(false); }
      applySize();
    });
  }

  ["timeupdate", "play", "pause", "seeked", "durationchange", "progress"].forEach(function (kind) {
    video.addEventListener(kind, update);
  });
  video.addEventListener("seeked", core.seekDone);
  video.addEventListener("waiting", function () { flag(document.body, "waiting", true); });
  ["playing", "canplay", "seeked", "pause"].forEach(function (kind) {
    video.addEventListener(kind, function () { flag(document.body, "waiting", false); });
  });
  // A pause shows the controls, but not this page's own wait for the group. One the browser
  // made by itself while the group plays (the tab put away, the headphones out): this page
  // waits for a tap, and the group goes on. In an iPhone's own full screen, its buttons are
  // the viewer's: a pause there is a press for the group.
  video.addEventListener("pause", function () {
    var kind = core.pauseKind();
    if (kind === "own") { return; }
    showControls();
    if (kind !== "browser") { return; }
    if (nativeFull() && !core.locked()) { core.propose("pause"); return; }
    core.hold();
  });
  video.addEventListener("play", function () {
    // Only a press in an iPhone's own full screen starts it while the group is paused.
    if (T && core.group() && core.groupPaused() && nativeFull()) {
      if (core.locked()) { core.pauseHere(); } else { core.propose("play"); }
    }
  });
  video.addEventListener("seeking", function () {
    if (!T || !core.group() || !nativeFull() || Date.now() - ownSeekAt < 1000) { return; }
    if (!core.locked() && Math.abs(video.currentTime - core.groupAt()) > core.tolerance) {
      core.propose("seek", Math.max(0, video.currentTime).toFixed(1));
    }
  });
  video.addEventListener("webkitbeginfullscreen", function () { nativeSubs(true); drawCall(); });
  video.addEventListener("webkitendfullscreen", function () { nativeSubs(false); showControls(); drawCall(); });
  video.addEventListener("loadedmetadata", function () {
    applySize();
    $("full").hidden = !canFull();  // an iPhone's video full screen is known by now
    window.setTimeout(function () {
      if (seekWhenReady !== null) { var t = seekWhenReady; seekWhenReady = null; seekTo(t); }
    }, 0);
  });
  video.addEventListener("ended", function () {
    showControls();
    if (S.next) { flag($("next"), "glow", true); }
  });
  window.setInterval(draw, 250);
  window.setInterval(core.report, REPORT_MS);
  if (T) {
    window.setInterval(function () { core.keepUp(); note(); }, 250);
  }

  $("title").textContent = (S.folder ? S.number + ". " : "") + S.title;
  $("episodes").hidden = !S.folder;
  $("next").hidden = !S.next;
  $("me").hidden = !T;  // a name is for watching together: others see who did what
  showName();
  setSize(S.start_sub_size);
  applySize();
  $("full").hidden = !canFull();
  var touch = (typeof navigator !== "undefined" && navigator.maxTouchPoints > 0) || "ontouchstart" in window;
  $("rotate").hidden = !touch;
  renderSheet();
  var first = sub >= 0 && S.subs[sub].kind === "burn" ? S.subs[sub].url : S.master;
  drawCall();
  if (T) {
    core.begin();
  }
  load(first, S.start || 0, !T);
  if (softChoice()) { fetchCues(S.subs[sub].url, subLoad); }
  update();
  showControls();
})();
