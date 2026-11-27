// The vpstream player for browser mode. A TV remote often works as a pointer that sends
// clicks and no keys, so every control is a large button; arrow keys and Enter work too.
// ES5, and text from the server goes in with textContent only.
//
// How it talks to the daemon, the owner's commands and watching together are sync.js's, as
// for the touch look (modern.js). Watching together, a press of a button goes to the group;
// in the cooldown of a page that just joined, only Full screen, Hide and the subtitle size
// work. The subtitle size is each browser's own, also watching together.
(function () {
  "use strict";
  var S = JSON.parse(document.getElementById("settings").textContent);
  var HLS_TYPE = "application/vnd.apple.mpegurl";
  var HIDE_MS = 4000;
  var STEP = 10;
  var REPORT_MS = 2000;

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
  // Subtitle sizes (see clients.SUB_SIZES): the height of a letter as a share of the
  // video's height as shown, so a phone and a TV show the same, as a player app does. A
  // video much lower than the window (a phone held upright) counts as FLOOR of the
  // window's height, so its subtitles stay readable and each size still differs; never
  // more than the window's width, so a narrow window still fits a line.
  var SIZES = [
    { id: "xs", label: "Extra small", share: 0.03 },
    { id: "s", label: "Small", share: 0.0375 },
    { id: "m", label: "Medium", share: 0.045 },
    { id: "l", label: "Large", share: 0.055 },
    { id: "xl", label: "Extra large", share: 0.07 }
  ];
  var FLOOR = 0.5;
  var size = 2;  // an index into SIZES
  var T = S.together || null;  // watching together (see sync.js)
  // Reports, the owner's commands and watching together: as the touch look (see sync.js).
  var core = window.VpSync(S, video, S.ui || "old", {
    settled: function () { return settled; },
    audio: audioValue,
    sub: subValue,
    size: function () { return SIZES[size].id; },
    setAudio: setAudio,
    setSub: setSub,
    setSize: setSize,
    seekTo: seekTo,
    showControls: showControls,
    update: update,
    render: note
  });

  function $(id) { return document.getElementById(id); }

  function say(text) { $("message").textContent = text || ""; }

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
      hls = new window.Hls({ enableWorker: true, startPosition: at > 0 ? at : -1 });
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
      p["catch"](function () { say("Press Play to start."); showControls(); });
    }
  }

  // --- audio: each press goes to the next track, then Off (muted), then the first again

  function audioLabel() {
    if (!S.audio.length) { return "Audio: none"; }
    return "Audio: " + (audio >= S.audio.length ? "Off" : S.audio[audio].label);
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
    $("audio").textContent = audioLabel();
  }

  function nextAudio() {
    if (!S.audio.length) { return; }
    audio = (audio + 1) % (S.audio.length + 1);
    applyAudio();
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

  function audioValue() { return audio >= S.audio.length ? 0 : S.audio[audio].number; }

  // --- subtitles: Off, then each prepared one (soft, or a burned-in version), then Off

  function subLabel() {
    if (sub < 0) { return "Subtitles: Off"; }
    var s = S.subs[sub];
    return "Subtitles: " + s.label + (s.kind === "burn" ? " (burned in)" : "");
  }

  function applySub() {
    var choice = sub >= 0 ? S.subs[sub] : null;
    var master = choice && choice.kind === "burn" ? choice.url : S.master;
    cues = [];
    draw();
    subLoad += 1;
    if (master !== current) { load(master, video.currentTime, !video.paused); }
    if (choice && choice.kind === "soft") { fetchCues(choice.url, subLoad); }
    $("subs").textContent = subLabel();
    showSize();
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

  function nextSub() {
    if (!S.subs.length) { return; }
    sub = sub + 1 >= S.subs.length ? -1 : sub + 1;
    applySub();
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

  function subValue() { return sub < 0 ? "off" : S.subs[sub].id; }

  // --- subtitle size: each press goes to the next, from the largest back to the smallest

  // The height the sizes are a share of: the video as shown (the window, less the black
  // bars of its shape), but FLOOR of the window's height (at most its width) at least.
  function sizeHeight() {
    var w = window.innerWidth || 0, h = window.innerHeight || 0;
    if (!w || !h) { return 0; }
    var shape = video.videoWidth && video.videoHeight ? video.videoHeight / video.videoWidth : 9 / 16;
    return Math.max(Math.min(FLOOR * h, w), Math.min(h, w * shape));
  }

  function applySize() {
    var height = sizeHeight();
    if (height) { $("overlay").style.fontSize = Math.round(SIZES[size].share * height) + "px"; }
    $("size").textContent = "Subtitle size: " + SIZES[size].label;
  }

  // The size button is there while soft subtitles are on: only those does the page draw.
  // A remote that was on it goes to Subtitles, so the arrows still start from somewhere.
  function showSize() {
    var choice = sub >= 0 ? S.subs[sub] : null;
    var hide = !(choice && choice.kind === "soft");
    if (hide && document.activeElement === $("size")) { $("subs").focus(); }
    $("size").hidden = hide;
  }

  function nextSize() {
    size = (size + 1) % SIZES.length;
    applySize();
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

  // --- the controls: shown while paused, and for a while after any click or move. Hide
  // puts them away, paused or not, until the next click or key: a remote that works as a
  // pointer moves all the time, so a move does not bring them back then.

  var tucked = false;

  function showControls() {
    if (tucked) { return; }
    document.body.className = "controls";
    if (hideTimer) { window.clearTimeout(hideTimer); }
    hideTimer = window.setTimeout(function () {
      if (!video.paused) { document.body.className = ""; }
    }, HIDE_MS);
  }

  function hideControls() {
    tucked = true;
    if (hideTimer) { window.clearTimeout(hideTimer); hideTimer = null; }
    document.body.className = "";
  }

  function bringBack() {
    tucked = false;
    showControls();
  }

  // --- full screen: the whole page, so the controls and the subtitles stay on it. Safari
  // has the API under webkit names. A browser without it (some TVs) shows no button.

  var root = document.documentElement;

  function fullElement() {
    return document.fullscreenElement || document.webkitFullscreenElement || null;
  }

  function canFull() {
    return !!(document.fullscreenEnabled || document.webkitFullscreenEnabled) &&
      !!(root.requestFullscreen || root.webkitRequestFullscreen);
  }

  function toggleFull() {
    if (!canFull()) { return; }
    var asked;
    if (fullElement()) {
      asked = document.exitFullscreen ? document.exitFullscreen() : document.webkitExitFullscreen();
    } else {
      asked = root.requestFullscreen ? root.requestFullscreen() : root.webkitRequestFullscreen();
    }
    if (asked && asked["catch"]) { asked["catch"](function () { say("Full screen is not allowed here."); }); }
  }

  function fullLabel() {
    $("full").textContent = fullElement() ? "Exit full screen" : "Full screen";
  }

  function seekTo(t) {
    if (video.readyState < 1) { seekWhenReady = t; return; }
    var end = video.duration || Infinity;
    video.currentTime = Math.max(0, Math.min(t, end - 0.5));
    draw();
  }

  // --- watching together

  // What the controls say while this page waits, and which of them work.
  function note() {
    if (!T) { return; }
    var now = core.status();
    if ($("note").textContent !== now.text) { $("note").textContent = now.text; }
    ["play", "back", "forward", "audio", "subs", "episodes", "next"].forEach(function (id) {
      if ($(id).disabled !== now.locked) { $(id).disabled = now.locked; }
    });
    if ($("sync").hidden === now.behind) { $("sync").hidden = !now.behind; }
    if ($("join").hidden === now.join) { $("join").hidden = !now.join; }
  }

  function nextAudioValue() {
    var next = (audio + 1) % (S.audio.length + 1);
    return next >= S.audio.length ? 0 : S.audio[next].number;
  }

  function nextSubValue() {
    var next = sub + 1 >= S.subs.length ? -1 : sub + 1;
    return next < 0 ? "off" : S.subs[next].id;
  }

  // The group's videos, to choose one for everyone.
  function showList() {
    var box = $("list");
    while (box.firstChild) { box.removeChild(box.firstChild); }
    T.names.forEach(function (name, i) {
      var b = document.createElement("button");
      b.type = "button";
      b.textContent = (i + 1) + ". " + name;
      b.addEventListener("click", function (event) {
        event.stopPropagation();
        box.hidden = true;
        core.propose("video", i + 1);
      });
      box.appendChild(b);
    });
    var close = document.createElement("button");
    close.type = "button";
    close.textContent = "Back";
    close.addEventListener("click", function (event) { event.stopPropagation(); box.hidden = true; });
    box.appendChild(close);
    box.hidden = false;
  }

  function press(kind, action) {
    return function () { core.mark(kind); action(); };
  }

  // -10 s and +10 s step from where the video is, so only once it is somewhere.
  function step(by) {
    return function () {
      if (T) { return core.propose("skip", by); }
      if (video.readyState < 1) { return; }
      core.mark("seek");
      seekTo(video.currentTime + by);
    };
  }

  function update() {
    if (video.readyState >= 2) { settled = true; }
    var d = video.duration || 0;
    $("fill").style.width = d ? (100 * video.currentTime / d) + "%" : "0";
    $("time").textContent = clock(video.currentTime) + (d ? " / " + clock(d) : "");
    $("play").textContent = core.playing() ? "Pause" : "Play";
    draw();
  }

  function on(id, action) {
    $(id).addEventListener("click", function (event) {
      event.stopPropagation();
      action();
      showControls();
    });
  }

  on("play", function () {
    if (T) { return core.propose(core.groupPaused() ? "play" : "pause"); }
    if (video.paused) { start(); } else { video.pause(); }
    core.reportSoon();
  });
  on("back", step(-STEP));
  on("forward", step(STEP));
  on("audio", function () {
    if (T) { return S.audio.length && core.propose("audio", nextAudioValue()); }
    press("audio", nextAudio)();
  });
  on("subs", function () {
    if (T) { return S.subs.length && core.propose("sub", nextSubValue()); }
    press("sub", nextSub)();
  });
  on("size", press("size", nextSize));  // this page's own, also watching together
  on("episodes", function () {
    if (T) { return core.locked() || showList(); }
    window.location.href = S.index;
  });
  on("next", function () {
    if (T) { return S.next && core.propose("video", S.number + 1); }
    if (S.next) { window.location.href = S.next; }
  });
  on("full", toggleFull);
  on("sync", core.syncHere);
  on("join", core.tapped);
  $("bar").addEventListener("click", function (event) {
    event.stopPropagation();
    var box = $("bar").getBoundingClientRect();
    if (video.duration && box.width && !core.locked()) {
      var at = video.duration * (event.clientX - box.left) / box.width;
      if (T) { core.propose("seek", Math.max(0, at).toFixed(1)); } else { core.mark("seek"); seekTo(at); }
    }
    showControls();
  });

  $("hide").addEventListener("click", function (event) {
    event.stopPropagation();
    hideControls();
  });
  document.addEventListener("click", bringBack);
  document.addEventListener("mousemove", showControls);
  document.addEventListener("keydown", function (event) {
    var buttons = Array.prototype.filter.call(
      document.querySelectorAll("#buttons button"), function (b) { return !b.hidden; });
    var at = buttons.indexOf(document.activeElement);
    var key = event.key || "";
    bringBack();
    if (key === "ArrowRight" || key === "ArrowLeft") {
      var step = key === "ArrowRight" ? 1 : -1;
      buttons[(at + step + buttons.length) % buttons.length].focus();
      event.preventDefault();
    } else if ((key === " " || key === "MediaPlayPause") && at < 0) {
      if (!core.locked()) { $("play").click(); }
      event.preventDefault();
    } else if ((key === "f" || key === "F") && !event.ctrlKey && !event.metaKey && !event.altKey) {
      toggleFull();
      event.preventDefault();
    }
  });
  document.addEventListener("fullscreenchange", fullLabel);
  document.addEventListener("webkitfullscreenchange", fullLabel);
  if (window.addEventListener) { window.addEventListener("resize", applySize); }

  ["timeupdate", "play", "pause", "seeked", "durationchange"].forEach(function (name) {
    video.addEventListener(name, update);
  });
  video.addEventListener("pause", showControls);
  video.addEventListener("seeked", core.seekDone);
  // A pause the browser made by itself while the group plays (the tab put away, the
  // headphones out): this page waits for a tap, and the group goes on.
  video.addEventListener("pause", function () {
    if (core.browserPaused()) { core.hold(); }
  });
  video.addEventListener("loadedmetadata", function () {
    applySize();  // the video's shape is known now
    // After the start position of the load is set, so the seek asked for since wins.
    window.setTimeout(function () {
      if (seekWhenReady !== null) { var t = seekWhenReady; seekWhenReady = null; seekTo(t); }
    }, 0);
  });
  video.addEventListener("ended", function () {
    showControls();
    if (S.next) { $("next").focus(); }
  });
  window.setInterval(draw, 250);
  window.setInterval(core.report, REPORT_MS);
  if (T) {
    window.setInterval(function () { core.keepUp(); note(); }, 250);
  }

  $("title").textContent = (S.folder ? S.number + ". " : "") + S.title;
  $("episodes").hidden = !S.folder;
  $("next").hidden = !S.next;
  $("audio").hidden = !S.audio.length;
  $("subs").hidden = !S.subs.length;
  showSize();
  setSize(S.start_sub_size);
  applySize();
  $("full").hidden = !canFull();
  $("audio").textContent = audioLabel();
  $("subs").textContent = subLabel();
  var first = sub >= 0 && S.subs[sub].kind === "burn" ? S.subs[sub].url : S.master;
  if (T) {
    core.begin();
  }
  load(first, S.start || 0, !T);  // watching together, keepUp starts it
  if (sub >= 0 && S.subs[sub].kind === "soft") { fetchCues(S.subs[sub].url, subLoad); }
  $("play").focus();
  showControls();
})();
