/* vstream capability test. Plain ES5 so older TV browsers can run it. */
(function () {
  "use strict";

  var PLAY_TIMEOUT = 12000;
  var MEDIA = "/caps/media/";
  var S = {
    session: String(Date.now()) + "-" + String(Math.random()).slice(2, 8),
    stage: "",
    env: {},
    support: [],
    playback: [],
    subtitles: null,
    audio: null,
    speed: null,
    keys: []
  };
  var manifest = null;
  var busy = false;

  function $(id) { return document.getElementById(id); }
  function setStage(t) { $("stage").textContent = t; }

  function send(stage) {
    S.stage = stage;
    try {
      var x = new XMLHttpRequest();
      x.open("POST", "/caps/report", true);
      x.setRequestHeader("Content-Type", "application/json");
      x.send(JSON.stringify(S));
    } catch (e) { /* the page still shows results */ }
  }

  function getJSON(url, cb) {
    var x = new XMLHttpRequest();
    x.open("GET", url, true);
    x.onload = function () {
      try { cb(null, JSON.parse(x.responseText)); } catch (e) { cb(e); }
    };
    x.onerror = function () { cb(new Error("network error")); };
    x.send();
  }

  function addRow(table, name, result, detail) {
    var tr = document.createElement("tr");
    var a = document.createElement("td"); a.textContent = name;
    var b = document.createElement("td"); b.textContent = result; b.className = result;
    var c = document.createElement("td"); c.textContent = detail || "";
    tr.appendChild(a); tr.appendChild(b); tr.appendChild(c);
    $(table).getElementsByTagName("tbody")[0].appendChild(tr);
  }

  function record(name, result, detail) {
    S.playback.push({ name: name, result: result, detail: detail || "" });
    addRow("results", name, result, detail);
  }

  /* ---------- focus navigation for TV remotes ---------- */

  function buttons() {
    var all = document.getElementsByTagName("button"), out = [];
    for (var i = 0; i < all.length; i++) {
      if (!all[i].disabled && all[i].offsetParent !== null) out.push(all[i]);
    }
    return out;
  }

  function moveFocus(step) {
    var list = buttons();
    if (!list.length) return;
    var i = list.indexOf(document.activeElement);
    i = i < 0 ? 0 : (i + step + list.length) % list.length;
    list[i].focus();
  }

  function keyName(e) {
    return (e.key || "?") + "/" + (e.keyCode || e.which || 0);
  }

  document.addEventListener("keydown", function (e) {
    var k = keyName(e);
    if (S.keys.indexOf(k) < 0) S.keys.push(k);
    $("keys").textContent = "Remote keys seen: " + S.keys.join("  ");
    var code = e.keyCode;
    if (code === 37 || code === 38) { moveFocus(-1); e.preventDefault(); }
    if (code === 39 || code === 40) { moveFocus(1); e.preventDefault(); }
  });

  // Keep Back on this page and record it as a key.
  if (window.history && history.pushState) {
    history.pushState({ caps: 1 }, "");
    window.addEventListener("popstate", function () {
      if (S.keys.indexOf("Back(popstate)") < 0) S.keys.push("Back(popstate)");
      $("keys").textContent = "Remote keys seen: " + S.keys.join("  ");
      history.pushState({ caps: 1 }, "");
    });
  }

  /* ---------- browser environment and codec support ---------- */

  function envInfo() {
    var v = document.createElement("video");
    var MS = window.MediaSource || window.ManagedMediaSource || window.WebKitMediaSource;
    var env = {
      userAgent: navigator.userAgent,
      screen: screen.width + "x" + screen.height + " @" + (window.devicePixelRatio || 1),
      viewport: window.innerWidth + "x" + window.innerHeight,
      colorDepth: screen.colorDepth,
      hdrDisplay: !!(window.matchMedia && matchMedia("(dynamic-range: high)").matches),
      mediaSource: !!window.MediaSource,
      managedMediaSource: !!window.ManagedMediaSource,
      hlsJsSupported: !!(window.Hls && Hls.isSupported()),
      hlsJsVersion: window.Hls ? Hls.version : "",
      nativeHls: v.canPlayType("application/vnd.apple.mpegurl") || "no",
      audioTracksApi: "audioTracks" in v,
      textTracksApi: "textTracks" in v,
      playbackQualityApi: typeof v.getVideoPlaybackQuality === "function",
      cookies: navigator.cookieEnabled,
      fetchApi: typeof window.fetch === "function",
      promise: typeof window.Promise === "function"
    };
    try { new Function("var f = (a) => a; class A {}"); env.es2015 = true; }
    catch (e) { env.es2015 = false; }
    S.env = env;
    for (var k in env) {
      if (Object.prototype.hasOwnProperty.call(env, k)) addRow("env", k, "", String(env[k]));
    }
    return MS;
  }

  function supportInfo(MS) {
    var v = document.createElement("video");
    var types = [
      ["H.264 High 8-bit", 'video/mp4; codecs="avc1.640028"'],
      ["H.264 High10 10-bit", 'video/mp4; codecs="avc1.6e0028"'],
      ["HEVC Main (hvc1)", 'video/mp4; codecs="hvc1.1.6.L120.90"'],
      ["HEVC Main (hev1)", 'video/mp4; codecs="hev1.1.6.L120.90"'],
      ["HEVC Main10", 'video/mp4; codecs="hvc1.2.4.L120.90"'],
      ["HEVC Main10 4K", 'video/mp4; codecs="hvc1.2.4.L153.90"'],
      ["AV1 8-bit", 'video/mp4; codecs="av01.0.08M.08"'],
      ["AV1 10-bit", 'video/mp4; codecs="av01.0.08M.10"'],
      ["AV1 10-bit 4K", 'video/mp4; codecs="av01.0.12M.10"'],
      ["VP9", 'video/webm; codecs="vp9"'],
      ["AAC", 'audio/mp4; codecs="mp4a.40.2"'],
      ["AC-3", 'audio/mp4; codecs="ac-3"'],
      ["E-AC-3", 'audio/mp4; codecs="ec-3"'],
      ["Opus", 'audio/mp4; codecs="opus"'],
      ["FLAC", 'audio/mp4; codecs="flac"']
    ];
    for (var i = 0; i < types.length; i++) {
      var native = v.canPlayType(types[i][1]) || "no";
      var mse = "no MSE";
      if (MS && MS.isTypeSupported) mse = MS.isTypeSupported(types[i][1]) ? "yes" : "no";
      S.support.push({ name: types[i][0], type: types[i][1], canPlayType: native, mse: mse });
      addRow("results", "Support: " + types[i][0], native !== "no" || mse === "yes" ? "pass" : "fail",
        "canPlayType=" + native + ", MSE=" + mse);
    }
  }

  /* ---------- playback tests ---------- */

  function freshVideo(muted) {
    var box = $("player");
    var old = box.getElementsByTagName("video");
    while (old.length) {
      try { old[0].pause(); old[0].removeAttribute("src"); old[0].load(); } catch (e) { }
      box.removeChild(old[0]);
    }
    var v = document.createElement("video");
    v.muted = !!muted;
    v.setAttribute("playsinline", "");
    v.preload = "auto";
    box.insertBefore(v, $("overlay"));
    return v;
  }

  function tryPlay(v, onBlocked) {
    var p;
    try { p = v.play(); } catch (e) { if (onBlocked) onBlocked(e); return; }
    if (p && p.then) p.then(null, function (e) { if (onBlocked) onBlocked(e); });
  }

  function decodedFrames(v) {
    if (typeof v.getVideoPlaybackQuality === "function") {
      return v.getVideoPlaybackQuality().totalVideoFrames;
    }
    if (typeof v.webkitDecodedFrameCount === "number") return v.webkitDecodedFrameCount;
    return -1;
  }

  /* setup(video, fail) attaches the source and returns a cleanup function. */
  function playTest(name, setup, done) {
    setStage("Playing: " + name);
    var v = freshVideo(true);
    var finished = false, timer = null, poll = null, cleanup = null;
    function finish(result, detail) {
      if (finished) return;
      finished = true;
      clearTimeout(timer); clearInterval(poll);
      try { if (cleanup) cleanup(); } catch (e) { }
      try { v.pause(); } catch (e) { }
      record(name, result, detail);
      setTimeout(done, 300);
    }
    v.addEventListener("error", function () {
      var code = v.error ? v.error.code : "?";
      finish("fail", "media error " + code + (v.error && v.error.message ? ": " + v.error.message : ""));
    });
    cleanup = setup(v, function (why) { finish("fail", why); });
    tryPlay(v, function (e) { /* muted play may still be refused; the timeout reports it */
      if (!finished) setStage("Playing: " + name + " (play() refused: " + (e && e.name) + ")");
    });
    poll = setInterval(function () {
      var frames = decodedFrames(v);
      if (v.currentTime >= 1.0 && v.videoWidth > 0 && frames !== 0) {
        finish("pass", v.videoWidth + "x" + v.videoHeight + (frames > 0 ? ", " + frames + " frames" : ""));
      }
    }, 250);
    timer = setTimeout(function () {
      finish("fail", "timeout: readyState=" + v.readyState + " networkState=" + v.networkState +
        " time=" + v.currentTime.toFixed(2) + " width=" + v.videoWidth + " frames=" + decodedFrames(v));
    }, PLAY_TIMEOUT);
  }

  function srcSetup(url) {
    return function (v) { v.src = url; return null; };
  }

  function hlsSetup(url) {
    return function (v, fail) {
      var hls = new Hls({ enableWorker: true });
      hls.on(Hls.Events.ERROR, function (ev, data) {
        if (data && data.fatal) fail("hls.js " + data.type + ": " + data.details);
      });
      hls.loadSource(url);
      hls.attachMedia(v);
      return function () { hls.destroy(); };
    };
  }

  function playbackQueue() {
    var q = [];
    var hlsOk = !!(window.Hls && Hls.isSupported());
    var nativeHls = !!document.createElement("video").canPlayType("application/vnd.apple.mpegurl");
    var clips = manifest.clips || [];
    for (var i = 0; i < clips.length; i++) {
      var c = clips[i];
      if (!c.mp4) { q.push({ skip: c.label, why: c.error || "not generated" }); continue; }
      q.push({ name: c.label + " MP4", setup: srcSetup(MEDIA + c.mp4) });
      if (c.hls && hlsOk) q.push({ name: c.label + " HLS hls.js", setup: hlsSetup(MEDIA + c.hls) });
      if (c.hls && nativeHls) q.push({ name: c.label + " HLS native", setup: srcSetup(MEDIA + c.hls) });
    }
    var ts = manifest.extras && manifest.extras.ts;
    if (ts && hlsOk) q.push({ name: "H.264 HLS (TS) hls.js", setup: hlsSetup(MEDIA + ts) });
    if (ts && nativeHls) q.push({ name: "H.264 HLS (TS) native", setup: srcSetup(MEDIA + ts) });
    if (!hlsOk && !nativeHls) q.push({ skip: "HLS", why: "no MSE for hls.js and no native HLS" });
    return q;
  }

  function runQueue(q, done) {
    if (!q.length) { done(); return; }
    var t = q.shift();
    if (t.skip) { record(t.skip, "skip", t.why); runQueue(q, done); return; }
    playTest(t.name, t.setup, function () { runQueue(q, done); });
  }

  /* ---------- questions answered with the remote ---------- */

  function ask(question, answers, cb) {
    var box = $("ask");
    box.innerHTML = "";
    var p = document.createElement("p");
    p.textContent = question;
    box.appendChild(p);
    var first = null;
    for (var i = 0; i < answers.length; i++) {
      (function (label) {
        var b = document.createElement("button");
        b.textContent = label;
        b.onclick = function () { box.innerHTML = ""; cb(label); };
        box.appendChild(b);
        if (!first) first = b;
      })(answers[i]);
    }
    if (first) first.focus();
  }

  function addButton(label, onclick) {
    var b = document.createElement("button");
    b.textContent = label;
    b.onclick = onclick;
    $("ask").appendChild(b);
    return b;
  }

  /* ---------- subtitles: native WebVTT track and our own overlay ---------- */

  function parseTime(t) {
    var p = t.trim().split(":"), s = 0;
    for (var i = 0; i < p.length; i++) s = s * 60 + parseFloat(p[i].replace(",", "."));
    return s;
  }

  function parseVtt(text) {
    var cues = [], lines = text.replace(/\r/g, "").split("\n");
    for (var i = 0; i < lines.length; i++) {
      if (lines[i].indexOf("-->") > 0) {
        var parts = lines[i].split("-->");
        var start = parseTime(parts[0]), end = parseTime(parts[1].trim().split(" ")[0]);
        var body = [];
        for (i++; i < lines.length && lines[i] !== ""; i++) body.push(lines[i]);
        cues.push({ start: start, end: end, text: body.join("\n") });
      }
    }
    return cues;
  }

  function subtitleTest(done) {
    var long = manifest.extras && manifest.extras.long;
    if (!long) { S.subtitles = { result: "skip", why: "no 20 s clip" }; done(); return; }
    setStage("Subtitles: a native track at the bottom, our overlay at the top");
    var x = new XMLHttpRequest();
    x.open("GET", MEDIA + manifest.vtt, true);
    x.onload = function () {
      var cues = parseVtt(x.responseText);
      var res = { parsedCues: cues.length, overlaySeen: [], nativeCues: null };
      var v = freshVideo(true);
      var tr = document.createElement("track");
      tr.kind = "subtitles"; tr.srclang = "en"; tr.label = "English"; tr["default"] = true;
      tr.src = MEDIA + manifest.vtt;
      v.appendChild(tr);
      v.src = MEDIA + long;
      if (v.textTracks && v.textTracks[0]) v.textTracks[0].mode = "showing";
      var ov = $("overlay");
      var timer = setInterval(function () {
        var t = v.currentTime, txt = "";
        for (var i = 0; i < cues.length; i++) {
          if (t >= cues[i].start && t < cues[i].end) { txt = cues[i].text; break; }
        }
        ov.innerHTML = "";
        if (txt) {
          var s = document.createElement("span"); s.textContent = txt; ov.appendChild(s);
          if (res.overlaySeen.indexOf(txt) < 0) res.overlaySeen.push(txt);
        }
      }, 150);
      tryPlay(v);
      setTimeout(function () {
        if (v.textTracks && v.textTracks[0] && v.textTracks[0].cues) {
          res.nativeCues = v.textTracks[0].cues.length;
        }
        ask("Do you see a yellow 'Subtitle line N' at the top (our overlay)?", ["Yes", "No"], function (a) {
          res.overlayVisible = a;
          ask("Do you see 'Subtitle line N' at the bottom (the browser's own subtitles)?", ["Yes", "No"], function (b) {
            res.nativeVisible = b;
            clearInterval(timer); ov.innerHTML = "";
            try { v.pause(); } catch (e) { }
            res.result = res.overlayVisible === "Yes" ? "pass" : "fail";
            S.subtitles = res;
            record("Subtitles overlay", res.result, "cues seen: " + res.overlaySeen.length);
            record("Subtitles native track", res.nativeVisible === "Yes" ? "pass" : "fail",
              "cues loaded: " + res.nativeCues);
            done();
          });
        });
      }, 4500);
    };
    x.onerror = function () { S.subtitles = { result: "fail", why: "could not load VTT" }; done(); };
    x.send();
  }

  /* ---------- audio track switching ---------- */

  function audioTest(done) {
    var url = manifest.extras && manifest.extras.multi_audio;
    if (!url) { S.audio = { result: "skip", why: "no multi-audio clip" }; done(); return; }
    url = MEDIA + url;
    var res = { method: "", tracks: [], switched: false, events: [] };
    var v = freshVideo(false);
    var hls = null, current = 0;
    function names() {
      if (hls) {
        var out = [];
        for (var i = 0; i < hls.audioTracks.length; i++) {
          out.push(hls.audioTracks[i].name + " (" + hls.audioTracks[i].lang + ")");
        }
        return out;
      }
      if (v.audioTracks) {
        var n = [];
        for (var j = 0; j < v.audioTracks.length; j++) n.push(v.audioTracks[j].label || v.audioTracks[j].language || ("track " + j));
        return n;
      }
      return [];
    }
    function select(i) {
      current = i;
      if (hls) { hls.audioTrack = i; return; }
      if (v.audioTracks) {
        for (var j = 0; j < v.audioTracks.length; j++) v.audioTracks[j].enabled = (j === i);
      }
    }
    if (window.Hls && Hls.isSupported()) {
      res.method = "hls.js";
      hls = new Hls();
      hls.on(Hls.Events.AUDIO_TRACK_SWITCHED, function (ev, d) {
        res.switched = true; res.events.push("switched to " + d.id);
        setStage("Audio: now playing track " + (d.id + 1) + " of " + hls.audioTracks.length);
      });
      hls.on(Hls.Events.ERROR, function (ev, d) { if (d && d.fatal) res.events.push("fatal " + d.details); });
      hls.loadSource(url);
      hls.attachMedia(v);
    } else if (v.canPlayType("application/vnd.apple.mpegurl")) {
      res.method = "native";
      v.src = url;
    } else {
      S.audio = { result: "skip", why: "no way to play HLS" };
      done(); return;
    }
    setStage("Audio: you should hear a low tone (440 Hz, English)");
    tryPlay(v, function (e) {
      res.events.push("play() refused: " + (e && e.name));
      setStage("Audio: press Play to start the sound");
      addButton("Play", function () { tryPlay(v); });
    });
    setTimeout(function () {
      res.tracks = names();
      setStage("Audio: switching to track 2 (higher tone, Japanese). Tracks: " + res.tracks.join(", "));
      select(1);
      setTimeout(function () {
        ask("Did the tone get higher? (Switch audio flips between the tracks.)", ["Yes", "No", "No sound at all"], function (a) {
          res.heardSwitch = a;
          if (hls) hls.destroy();
          try { v.pause(); } catch (e) { }
          res.result = a === "Yes" ? "pass" : "fail";
          S.audio = res;
          record("Audio track switch (" + res.method + ")", res.result,
            "tracks: " + res.tracks.length + ", event: " + res.switched + ", heard: " + a);
          done();
        });
        addButton("Switch audio", function () { select(current === 0 ? 1 : 0); });
      }, 3000);
    }, 4000);
  }

  /* ---------- download speed ---------- */

  function speedTest(done) {
    var sizes = [2, 8, 32, 64, 128], runs = [];
    setStage("Speed: downloading from the server…");
    function next() {
      if (!sizes.length) return finish();
      var mb = sizes.shift();
      var x = new XMLHttpRequest();
      var t0 = Date.now();
      x.open("GET", "/caps/speed?mb=" + mb + "&r=" + Math.random(), true);
      x.responseType = "arraybuffer";
      x.timeout = 120000;
      x.onload = function () {
        var secs = (Date.now() - t0) / 1000;
        var bytes = x.response ? x.response.byteLength : mb * 1048576;
        var mbps = bytes * 8 / secs / 1e6;
        runs.push({ mb: mb, seconds: secs, mbps: Math.round(mbps * 10) / 10 });
        setStage("Speed: " + mb + " MB in " + secs.toFixed(1) + " s = " + mbps.toFixed(1) + " Mbit/s");
        if (secs > 8) return finish();
        next();
      };
      x.onerror = x.ontimeout = function () { runs.push({ mb: mb, error: true }); finish(); };
      x.send();
    }
    function finish() {
      var best = 0;
      for (var i = 0; i < runs.length; i++) if (runs[i].mbps > best && runs[i].mb >= 8) best = runs[i].mbps;
      if (!best) for (var j = 0; j < runs.length; j++) if (runs[j].mbps > best) best = runs[j].mbps;
      S.speed = { best_mbps: best, runs: runs };
      record("Download speed", best ? "pass" : "fail", best + " Mbit/s (best run of 8 MB or more)");
      done();
    }
    next();
  }

  /* ---------- wiring ---------- */

  function lock(on) {
    busy = on;
    $("start").disabled = on;
    $("speedonly").disabled = on;
  }

  function runAll() {
    if (busy || !manifest) return;
    lock(true);
    S.playback = []; S.support = [];
    supportInfo(window.MediaSource || window.ManagedMediaSource || window.WebKitMediaSource);
    send("support");
    runQueue(playbackQueue(), function () {
      send("playback");
      subtitleTest(function () {
        send("subtitles");
        audioTest(function () {
          send("audio");
          speedTest(function () {
            freshVideo(true);
            send("done");
            setStage("Done. Results were sent to the server. You can keep pressing remote keys.");
            lock(false);
            $("start").focus();
          });
        });
      });
    });
  }

  $("start").onclick = runAll;
  $("speedonly").onclick = function () {
    if (busy) return;
    lock(true);
    speedTest(function () { send("speed"); setStage("Speed test done."); lock(false); });
  };

  envInfo();
  send("env");
  getJSON("/caps/clips.json", function (err, data) {
    if (err) { setStage("Could not load the clip list: " + err.message); return; }
    manifest = data;
    var missing = [];
    for (var i = 0; i < data.clips.length; i++) if (!data.clips[i].mp4) missing.push(data.clips[i].label);
    setStage("Ready. Press Start all tests." + (missing.length ? " Not generated: " + missing.join(", ") : ""));
    $("start").focus();
  });
})();
