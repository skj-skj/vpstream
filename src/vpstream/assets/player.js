// The vpstream player for browser mode. A TV remote often works as a pointer that sends
// clicks and no keys, so every control is a large button; arrow keys and Enter work too.
// ES5, and text from the server goes in with textContent only.
(function () {
  "use strict";
  var S = JSON.parse(document.getElementById("settings").textContent);
  var HLS_TYPE = "application/vnd.apple.mpegurl";
  var HIDE_MS = 4000;
  var STEP = 10;

  var video = $("video");
  var hls = null;
  var current = "";  // the master playlist that is loaded
  var audio = S.start_audio;  // an index into S.audio; S.audio.length means Off
  var sub = S.start_sub;  // an index into S.subs; -1 means Off
  var cues = [];
  var subLoad = 0;  // counts subtitle loads, so a slow one that was replaced is dropped
  var hideTimer = null;

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

  // --- the controls: shown while paused, and for a while after any click or move

  function showControls() {
    document.body.className = "controls";
    if (hideTimer) { window.clearTimeout(hideTimer); }
    hideTimer = window.setTimeout(function () {
      if (!video.paused) { document.body.className = ""; }
    }, HIDE_MS);
  }

  function seekTo(t) {
    var end = video.duration || Infinity;
    video.currentTime = Math.max(0, Math.min(t, end - 0.5));
    draw();
  }

  function update() {
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

  on("play", function () { if (video.paused) { start(); } else { video.pause(); } });
  on("back", function () { seekTo(video.currentTime - STEP); });
  on("forward", function () { seekTo(video.currentTime + STEP); });
  on("audio", nextAudio);
  on("subs", nextSub);
  on("episodes", function () { window.location.href = S.index; });
  on("next", function () { if (S.next) { window.location.href = S.next; } });
  $("bar").addEventListener("click", function (event) {
    event.stopPropagation();
    var box = $("bar").getBoundingClientRect();
    if (video.duration && box.width) {
      seekTo(video.duration * (event.clientX - box.left) / box.width);
    }
    showControls();
  });

  document.addEventListener("click", showControls);
  document.addEventListener("mousemove", showControls);
  document.addEventListener("keydown", function (event) {
    var buttons = Array.prototype.filter.call(
      document.querySelectorAll("#buttons button"), function (b) { return !b.hidden; });
    var at = buttons.indexOf(document.activeElement);
    var key = event.key || "";
    showControls();
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
  video.addEventListener("ended", function () {
    showControls();
    if (S.next) { $("next").focus(); }
  });
  window.setInterval(draw, 250);

  $("title").textContent = (S.folder ? S.number + ". " : "") + S.title;
  $("episodes").hidden = !S.folder;
  $("next").hidden = !S.next;
  $("audio").hidden = !S.audio.length;
  $("subs").hidden = !S.subs.length;
  $("audio").textContent = audioLabel();
  $("subs").textContent = subLabel();
  var first = sub >= 0 && S.subs[sub].kind === "burn" ? S.subs[sub].url : S.master;
  load(first, 0, true);
  if (sub >= 0 && S.subs[sub].kind === "soft") { fetchCues(S.subs[sub].url, subLoad); }
  $("play").focus();
  showControls();
})();
