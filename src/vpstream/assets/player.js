// The vpstream player for browser mode. A TV remote often works as a pointer that sends
// clicks and no keys, so every control is a large button; arrow keys and Enter work too.
// ES5, and text from the server goes in with textContent only.
//
// Every REPORT_MS it tells the daemon where it is and what it plays with, and gets back what
// the owner asked of it on the dashboard. The newer of a button press and a command wins: a
// report says how long ago each kind of button was pressed, so the daemon drops a command
// made before; a command that crosses a press on the way back is dropped here. Either way the
// daemon's record ends up as what this player does. Each page has an id of its own, so two
// tabs of one browser both get the owner's commands.
(function () {
  "use strict";
  var S = JSON.parse(document.getElementById("settings").textContent);
  var HLS_TYPE = "application/vnd.apple.mpegurl";
  var HIDE_MS = 4000;
  var STEP = 10;
  var REPORT_MS = 2000;
  var SOON_MS = 300;  // a change is reported this soon after it is made
  var REPORT_TIMEOUT_MS = 8000;

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
  var seen = S.ack || 0;  // the newest command seen; the page itself carries the older ones
  var pressed = { audio: 0, sub: 0, seek: 0 };  // counts the viewer's own presses
  var told = { audio: 0, sub: 0, seek: 0 };  // ...as far as a report that arrived said
  var pressedAt = { audio: 0, sub: 0, seek: 0 };  // when the last one was (Date.now)
  var KEYS = { audio: "pa", sub: "ps", seek: "pk" };  // see clients.PRESSED
  var page = Math.random().toString(36).slice(2, 10) || "p";
  var asking = null;  // the report on its way
  var soonTimer = null;

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

  function seekTo(t) {
    if (video.readyState < 1) { seekWhenReady = t; return; }
    var end = video.duration || Infinity;
    video.currentTime = Math.max(0, Math.min(t, end - 0.5));
    draw();
  }

  // --- reporting to the daemon, and doing what the owner asks

  function query() {
    var q = ["p=" + (video.paused ? 1 : 0), "ack=" + seen, "pg=" + page];
    for (var kind in KEYS) {
      if (pressed[kind] !== told[kind]) {
        q.push(KEYS[kind] + "=" + Math.max(0, (Date.now() - pressedAt[kind]) / 1000).toFixed(1));
      }
    }
    if (settled) { q.push("t=" + Math.max(0, video.currentTime).toFixed(1)); }
    if (isFinite(video.duration) && video.duration > 0) { q.push("d=" + video.duration.toFixed(1)); }
    if (S.audio.length) { q.push("a=" + audioValue()); }
    q.push("s=" + encodeURIComponent(subValue()));
    return q.join("&");
  }

  function report() {
    if (asking || !S.report) { return; }
    var before = { audio: pressed.audio, sub: pressed.sub, seek: pressed.seek };
    var x = new XMLHttpRequest();
    asking = x;
    x.open("GET", S.report + "?" + query(), true);
    x.setRequestHeader("X-Vpstream-Player", "1");  // see player.REPORT_HEADER
    x.timeout = REPORT_TIMEOUT_MS;
    x.onload = function () {
      asking = null;
      if (x.status !== 200) { return; }
      told = before;  // the daemon knows of these presses now
      var answer;
      try { answer = JSON.parse(x.responseText); } catch (e) { return; }
      obey(answer.commands || [], before);
    };
    x.onerror = x.ontimeout = x.onabort = function () { asking = null; };
    x.send();
  }

  function reportSoon() {
    if (soonTimer) { window.clearTimeout(soonTimer); }
    soonTimer = window.setTimeout(function () { soonTimer = null; report(); }, SOON_MS);
  }

  // `before`: the viewer's presses when the report went out.
  function obey(commands, before) {
    var newest = seen;
    var changed = false;
    for (var i = 0; i < commands.length; i++) {
      var c = commands[i];
      if (!(c.seq > seen)) { continue; }
      newest = Math.max(newest, c.seq);
      if ("audio" in c && pressed.audio === before.audio) { changed = setAudio(c.audio) || changed; }
      if ("sub" in c && pressed.sub === before.sub) { changed = setSub(c.sub) || changed; }
      if ("seek" in c && pressed.seek === before.seek) { seekTo(c.seek); changed = true; }
    }
    if (newest > seen) {
      seen = newest;
      if (changed) { showControls(); }
      reportSoon();  // says what was seen, and what it did
    }
  }

  // A press of the viewer's own: it wins over a command made before it, or crossing it.
  function mark(kind) {
    pressed[kind] += 1;
    pressedAt[kind] = Date.now();
    reportSoon();
  }

  function press(kind, action) {
    return function () { mark(kind); action(); };
  }

  // -10 s and +10 s step from where the video is, so only once it is somewhere.
  function step(by) {
    return function () {
      if (video.readyState < 1) { return; }
      mark("seek");
      seekTo(video.currentTime + by);
    };
  }

  function update() {
    if (video.readyState >= 2) { settled = true; }
    var d = video.duration || 0;
    $("fill").style.width = d ? (100 * video.currentTime / d) + "%" : "0";
    $("time").textContent = clock(video.currentTime) + (d ? " / " + clock(d) : "");
    $("play").textContent = video.paused ? "Play" : "Pause";
    draw();
  }

  function on(id, action) {
    $(id).addEventListener("click", function (event) {
      event.stopPropagation();
      action();
      showControls();
    });
  }

  on("play", function () { if (video.paused) { start(); } else { video.pause(); } reportSoon(); });
  on("back", step(-STEP));
  on("forward", step(STEP));
  on("audio", press("audio", nextAudio));
  on("subs", press("sub", nextSub));
  on("episodes", function () { window.location.href = S.index; });
  on("next", function () { if (S.next) { window.location.href = S.next; } });
  $("bar").addEventListener("click", function (event) {
    event.stopPropagation();
    var box = $("bar").getBoundingClientRect();
    if (video.duration && box.width) {
      mark("seek");
      seekTo(video.duration * (event.clientX - box.left) / box.width);
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
      $("play").click();
      event.preventDefault();
    }
  });

  ["timeupdate", "play", "pause", "seeked", "durationchange"].forEach(function (name) {
    video.addEventListener(name, update);
  });
  video.addEventListener("pause", showControls);
  video.addEventListener("loadedmetadata", function () {
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
  window.setInterval(report, REPORT_MS);

  $("title").textContent = (S.folder ? S.number + ". " : "") + S.title;
  $("episodes").hidden = !S.folder;
  $("next").hidden = !S.next;
  $("audio").hidden = !S.audio.length;
  $("subs").hidden = !S.subs.length;
  $("audio").textContent = audioLabel();
  $("subs").textContent = subLabel();
  var first = sub >= 0 && S.subs[sub].kind === "burn" ? S.subs[sub].url : S.master;
  load(first, S.start || 0, true);
  if (sub >= 0 && S.subs[sub].kind === "soft") { fetchCues(S.subs[sub].url, subLoad); }
  $("play").focus();
  showControls();
})();
