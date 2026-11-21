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
//
// Watching together (S.together), the daemon holds the group's state and this page follows
// it: whether it plays, the video, where, the audio and the subtitles. A report then only
// says where the page is; a press of a button goes to the group (act=) and comes back as
// the group's state, here and on every other page. Nothing the browser does by itself (a
// pause when the tab is put away or the headphones come out) goes to the group. The page
// waits for the group to change on S.together.sync, a request the daemon holds until it
// does. It keeps up with the group by its playback rate, or by a seek when it is far off.
// A page that just joined has a cooldown: its buttons do nothing for a while, but Full
// screen and Hide.
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

  // Watching together (see the top)
  var T = S.together || null;
  var SEEK_OFF = 2;  // seconds off the group: a seek
  var RATE_OFF = 0.3;  // ...a little faster or slower, until it is this close:
  var RATE_DONE = 0.2;
  var RATE_ONLY_SEEK = 1;  // where the rate cannot be changed, a seek from this far off
  var PAUSED_OFF = 0.25;  // paused, a seek from this far off
  var JOINED_OFF = 0.5;  // a page this close to the group is at its place
  var SEEK_GAP_MS = 1000;  // a seek to keep up is given this long to happen
  var WAIT_RETRY_MS = 2000;
  var WAIT_TIMEOUT_MS = 30000;
  var group = null;  // the group's state, as last told, with when (at) and the trip (lead)
  var joined = false;  // loaded and at the group's place; reports say so from then on
  var heard = false;  // the daemon answered a report that said so: `until` is its cooldown
  var until = Infinity;  // Date.now() when the cooldown ends
  var acts = [];  // presses for the group still to go out, oldest first
  var actCount = 0;
  var hint = "", hintUntil = 0;  // what this page's press that waits for a window does
  var held = false;  // not playing while the group plays: a tap starts this page
  var starting = false;  // a play() that has not answered yet
  var rated = false, rateWorks = true;
  var lastSeek = 0;

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
    if (T) { return startHere(); }
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

  // --- reporting to the daemon, and doing what the owner asks

  function query() {
    if (T) { return groupQuery(); }
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
    var sent = Date.now(), act = T ? acts[0] : null, said = joined;
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
      if (T) { return answered(answer, sent, act, said); }
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

  // --- watching together

  function groupQuery() {
    var q = ["p=" + (video.paused ? 1 : 0), "pg=" + page, "rev=" + (group ? group.rev : 0)];
    if (settled) { q.push("t=" + Math.max(0, video.currentTime).toFixed(1)); }
    if (isFinite(video.duration) && video.duration > 0) { q.push("d=" + video.duration.toFixed(1)); }
    if (S.audio.length) { q.push("a=" + audioValue()); }
    q.push("s=" + encodeURIComponent(subValue()));
    if (joined) { q.push("in=1"); }
    if (acts.length) {
      q.push("act=" + acts[0].act, "id=" + acts[0].id);
      if (acts[0].v !== undefined) { q.push("v=" + encodeURIComponent(String(acts[0].v))); }
    }
    return q.join("&");
  }

  // The answer to a report: the group's state, and what became of the press it carried.
  function answered(answer, sent, act, said) {
    if (act && acts[0] === act) {
      acts.shift();
      if (answer.outcome === "queued") { showHint(act.act); }
    }
    if (said && typeof answer.cooldown === "number") {
      heard = true;
      until = Date.now() + answer.cooldown * 1000;
    }
    adopt(answer, sent);
    if (acts.length) { report(); }
  }

  var HINTS = {
    play: "Playing for everyone...", pause: "Pausing for everyone...",
    seek: "Seeking for everyone...", skip: "Seeking for everyone...",
    audio: "Changing the audio for everyone...", sub: "Changing the subtitles for everyone...",
    video: "Changing the video for everyone..."
  };

  function showHint(act) {
    hint = HINTS[act] || "";
    hintUntil = Date.now() + ((group && group.window) || 2) * 1000 + 1000;
    note();
  }

  // Where the group is now, as far as this page can tell: what it was told, moved on by
  // the time since, and by half the trip of the answer.
  function groupAt() {
    if (!group) { return 0; }
    var at = group.position;
    if (!group.paused) { at += (Date.now() - group.at) / 1000 + group.lead; }
    return group.duration ? Math.min(at, group.duration) : at;
  }

  // Take the group's state from an answer that left at `sent`. An answer older than the
  // one taken last (by the daemon's clock) is dropped; a new rev is followed.
  function adopt(answer, sent) {
    var s = answer && answer.together;
    if (!s || typeof s.rev !== "number") { return; }
    if (group && s.now < group.now) { return; }
    var trip = Math.max(0, (Date.now() - sent) / 1000 - (answer.waited || 0));
    var fresh = !group || s.rev !== group.rev;
    group = {
      rev: s.rev, video: s.video, position: s.position, paused: s.paused, audio: s.audio,
      sub: s.sub, duration: s.duration || 0, window: s.window, now: s.now,
      at: Date.now(), lead: trip / 2
    };
    if (s.video !== S.number) {
      window.location.href = S.index + "/" + s.video;
      return;
    }
    if (fresh) {
      hint = "";
      if (s.audio !== null && s.audio !== undefined) { setAudio(s.audio); }
      if (s.sub !== null && s.sub !== undefined) { setSub(s.sub); }
      if (!s.paused) { held = false; }
      showControls();
    }
    keepUp();
    update();
    note();
  }

  function setRate(rate) {
    if (video.playbackRate === rate) { rated = rate !== 1; return; }
    video.playbackRate = rate;
    if (Math.abs((video.playbackRate || 1) - rate) > 0.001) { rateWorks = false; }
    rated = rate !== 1 && rateWorks;
  }

  function catchUp(at) {
    lastSeek = Date.now();
    seekTo(at);
  }

  // Keep this page with the group: play or pause as it does, and stay at its place.
  function keepUp() {
    if (!T || !group || !settled || video.readyState < 1 || video.seeking) { return; }
    // Where this video can be: a seek stops half a second before its end (see seekTo).
    var target = Math.min(groupAt(), video.duration ? video.duration - 0.5 : Infinity);
    var off = video.currentTime - target;
    var far = Math.abs(off);
    var free = Date.now() - lastSeek >= SEEK_GAP_MS;
    if (group.paused) {
      setRate(1);
      if (!video.paused) { video.pause(); }
      if (far > PAUSED_OFF && free) { catchUp(target); }
    } else {
      if (video.paused) {
        if (!held && !starting) { startHere(); }
      } else if ((far > SEEK_OFF || (!rateWorks && far > RATE_ONLY_SEEK)) && free) {
        setRate(1);
        catchUp(target);
      } else if (rated && far < RATE_DONE) {
        setRate(1);
      } else if (rateWorks && far > RATE_OFF) {
        setRate(off > 0 ? 0.95 : 1.05);
      }
    }
    if (!joined && Math.abs(video.currentTime - target) < JOINED_OFF &&
        (group.paused || !video.paused)) {
      joined = true;
      reportSoon();
    }
  }

  // Play here only. A browser that will not play without a tap shows the join button.
  function startHere() {
    var p = video.play();
    if (!(p && p.then)) { return; }
    starting = true;
    p.then(function () { starting = false; held = false; note(); }, function () {
      starting = false;
      held = true;
      note();
      showControls();
    });
  }

  function locked() {
    return !!T && (!joined || !heard || Date.now() < until);
  }

  // What the controls say while this page waits, and which of them work.
  function note() {
    if (!T) { return; }
    var text = "";
    if (hint && Date.now() < hintUntil) { text = hint; } else { hint = ""; }
    if (!text && locked()) {
      var left = Math.ceil((until - Date.now()) / 1000);
      text = !joined || !heard || !isFinite(left) ? "Joining the group..." :
        "Watching together: the controls work in " + left + " s";
    }
    if ($("note").textContent !== text) { $("note").textContent = text; }
    var off = locked();
    ["play", "back", "forward", "audio", "subs", "episodes", "next"].forEach(function (id) {
      if ($(id).disabled !== off) { $(id).disabled = off; }
    });
    var tap = held && group && !group.paused;
    if ($("join").hidden === !!tap) { $("join").hidden = !tap; }
  }

  // A press for the group: it goes out with the next report, which goes now.
  function propose(act, value) {
    if (locked()) { return; }
    actCount += 1;
    acts.push({ act: act, v: value, id: page + "-" + actCount });
    if (asking) { reportSoon(); } else { report(); }
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
        propose("video", i + 1);
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

  // Wait for the group to change: the daemon answers when it does, or after a while.
  function wait() {
    var x = new XMLHttpRequest();
    var sent = Date.now(), before = group ? group.rev : 0;
    x.open("GET", T.sync + "?pg=" + page + "&rev=" + before, true);
    x.setRequestHeader("X-Vpstream-Player", "1");
    x.timeout = WAIT_TIMEOUT_MS;
    x.onload = function () {
      var answer = null;
      if (x.status === 200) {
        try { answer = JSON.parse(x.responseText); } catch (e) { answer = null; }
      }
      if (answer) { adopt(answer, sent); }
      // At once again after a real wait or a change; else (the daemon did not hold it, or
      // it failed) a little later.
      var again = answer && ((answer.waited || 0) >= 1 || (group && group.rev !== before));
      window.setTimeout(wait, again ? 0 : WAIT_RETRY_MS);
    };
    x.onerror = x.ontimeout = function () { window.setTimeout(wait, WAIT_RETRY_MS); };
    x.send();
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
      if (T) { return propose("skip", by); }
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
    $("play").textContent = (T && group ? group.paused : video.paused) ? "Play" : "Pause";
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
    if (T) { return propose(group && group.paused ? "play" : "pause"); }
    if (video.paused) { start(); } else { video.pause(); }
    reportSoon();
  });
  on("back", step(-STEP));
  on("forward", step(STEP));
  on("audio", function () {
    if (T) { return S.audio.length && propose("audio", nextAudioValue()); }
    press("audio", nextAudio)();
  });
  on("subs", function () {
    if (T) { return S.subs.length && propose("sub", nextSubValue()); }
    press("sub", nextSub)();
  });
  on("episodes", function () {
    if (T) { return locked() || showList(); }
    window.location.href = S.index;
  });
  on("next", function () {
    if (T) { return S.next && propose("video", S.number + 1); }
    if (S.next) { window.location.href = S.next; }
  });
  on("full", toggleFull);
  on("join", function () { held = false; startHere(); note(); });
  $("bar").addEventListener("click", function (event) {
    event.stopPropagation();
    var box = $("bar").getBoundingClientRect();
    if (video.duration && box.width && !locked()) {
      var at = video.duration * (event.clientX - box.left) / box.width;
      if (T) { propose("seek", Math.max(0, at).toFixed(1)); } else { mark("seek"); seekTo(at); }
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
      if (!locked()) { $("play").click(); }
      event.preventDefault();
    } else if ((key === "f" || key === "F") && !event.ctrlKey && !event.metaKey && !event.altKey) {
      toggleFull();
      event.preventDefault();
    }
  });
  document.addEventListener("fullscreenchange", fullLabel);
  document.addEventListener("webkitfullscreenchange", fullLabel);

  ["timeupdate", "play", "pause", "seeked", "durationchange"].forEach(function (name) {
    video.addEventListener(name, update);
  });
  video.addEventListener("pause", showControls);
  // A pause the browser made by itself while the group plays (the tab put away, the
  // headphones out): this page waits for a tap, and the group goes on.
  video.addEventListener("pause", function () {
    if (T && group && !group.paused && settled && !video.ended) { held = true; note(); }
  });
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
  if (T) {
    window.setInterval(function () { keepUp(); note(); }, 250);
  }

  $("title").textContent = (S.folder ? S.number + ". " : "") + S.title;
  $("episodes").hidden = !S.folder;
  $("next").hidden = !S.next;
  $("audio").hidden = !S.audio.length;
  $("subs").hidden = !S.subs.length;
  $("full").hidden = !canFull();
  $("audio").textContent = audioLabel();
  $("subs").textContent = subLabel();
  var first = sub >= 0 && S.subs[sub].kind === "burn" ? S.subs[sub].url : S.master;
  if (T) {
    adopt({ together: T.state, waited: 0 }, Date.now());
    note();
    wait();
  }
  load(first, S.start || 0, !T);  // watching together, keepUp starts it
  if (sub >= 0 && S.subs[sub].kind === "soft") { fetchCues(S.subs[sub].url, subLoad); }
  $("play").focus();
  showControls();
})();
