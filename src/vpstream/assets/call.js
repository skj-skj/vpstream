// The video call of watching together, for the touch player (modern.js draws it). ES5.
//
// The daemon keeps who is in the call and passes the messages that set it up (see
// together.Call); the video and the sound go browser to browser, never through it. Each
// pair of pages in the call has one RTCPeerConnection: the page with the lower id makes the
// offer, the other answers. An offer or an answer goes once, with every address its browser
// found (no trickle), so a pair needs two messages. Addresses come from a public STUN
// server; there is no TURN, so a pair whose networks will not meet gets "Could not connect"
// on its tile, and the rest of the call goes on.
//
// The film comes first. The camera is small (CAMERA: a tile is small), each page gets at
// most MAX_BITRATE of it, and the call's packets go at the lowest priority. Only a page
// that shows this one's tile on screen gets the video at all (the tiles it `sees`: on its
// page of tiles, not in Zen, the tab in front). While the film is short of data (it waits,
// or has little ahead: modern.js says which), no video goes out and none comes in, only
// sound, until it has played a while again. Nothing happens until someone presses Call:
// no camera, no connection.
//
// A page joins with its camera on and its mic off. Leaving, the camera and the mic are let
// go. A page loaded again (another video for the group, or the other look) takes its old
// place in the call, as it was, without a ring. A tab that closes or loads again tells
// the daemon it went away, so the others stop showing it at once (together.Call.away).
//
// The page makes one with window.VpCall(S.together.call, page, look): `look` has changed()
// (draw the call again), name() the viewer's own name, said(text) a notice, and audioBox
// (where each other person's sound plays, in an <audio> of its own).
(function () {
  "use strict";
  var ICE = [{ urls: "stun:stun.cloudflare.com:3478" }, { urls: "stun:stun.l.google.com:19302" }];
  var FPS = 12;
  var MAX_BITRATE = 100000;  // bits a second, of video, to each page that shows this one
  var SOUND_BITRATE = 32000;  // ...and of sound, to each page
  var CAMERA = { width: { ideal: 240, max: 320 }, height: { ideal: 180, max: 240 },
    frameRate: { ideal: FPS, max: FPS } };
  var SOUND = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };
  var GATHER_MS = 3000;  // an offer or answer goes with the addresses found by then
  var CONNECT_MS = 20000;  // a pair not connected by then tries again
  var TRIES = 3;  // ...this many times, then its tile says it could not connect
  var RETRY_MS = 1000;
  var RESUME_MS = 3000;  // the film plays this long again before video goes out again
  var STATE_MS = 300;  // what this page has on is told the daemon this soon after a change
  var POST_TIMEOUT_MS = 10000;
  var SENDS = 3;  // a message whose post got no answer (or "too many") goes this many times
  // An offer or an answer is at most this long (together.MAX_MAIL_DATA): past it, the
  // addresses that matter least (on this machine, IPv6 first) are left out until it fits.
  var MAX_SDP = 12000;
  var KEY = "vpstream-call";  // this tab's place in the call, for the page loaded next
  var AGAIN_MS = 60000;  // ...if it loads this soon

  function nothing() {}

  // Whether this browser can be in a call: a secure page (HTTPS) with a camera API.
  function possible() {
    var nav = window.navigator;
    return !!(window.isSecureContext && window.RTCPeerConnection && window.MediaStream &&
      nav && nav.mediaDevices && nav.mediaDevices.getUserMedia);
  }

  window.VpCall = function (url, me, look) {
    var hooks = { changed: nothing, said: nothing, name: function () { return ""; }, audioBox: null };
    for (var k in look) { hooks[k] = look[k]; }
    var id = "";  // the call that runs, "" for none
    var boot = "";  // the daemon's, as it last said (see together.Call)
    var version = -1;
    var members = [];  // as the daemon last said, in the order they joined; not those away
    var everyone = [];  // ...and those too (see again)
    var by = "", starter = "";  // who started it: their name, and their page
    var ring = false;
    var after = 0;  // the newest message taken
    var inCall = false;
    var joining = false;
    var dismissed = "";  // the call whose ring this page put away (Not now)
    var peers = {};  // page -> its connection to this page
    var audio = null, video = null;  // this page's own tracks while in the call
    var preview = null;  // ...its video, for its own tile
    var mic = false, cam = true;
    var facing = "user";
    var sees = [];  // the pages whose tiles show here now
    var filmWaits = false, resumeTimer = null;
    var stateTimer = null;
    var cameras = 0;  // how many this browser has, once known

    function media() { return window.navigator.mediaDevices; }

    function member(page, list) {
      list = list || members;
      for (var i = 0; i < list.length; i++) { if (list[i].page === page) { return list[i]; } }
      return null;
    }

    // The tiles this page shows, as the daemon is told: none while the film is short of
    // data, so the others send it no video.
    function shown() { return filmWaits ? [] : sees; }

    // --- talking to the daemon

    // What a post carries: what it does, the page, and the messages it has.
    function message(body) {
      body.pg = me;
      body.cm = after;
      return JSON.stringify(body);
    }

    function post(body, done) {
      var x = new XMLHttpRequest();
      x.open("POST", url, true);
      x.setRequestHeader("X-Vpstream-Player", "1");  // see player.REPORT_HEADER
      x.setRequestHeader("Content-Type", "application/json");
      x.timeout = POST_TIMEOUT_MS;
      x.onload = function () {
        var answer = null;
        if (x.status === 200) {
          try { answer = JSON.parse(x.responseText); } catch (e) { answer = null; }
        }
        if (answer && answer.call) { update(answer.call); }
        if (done) { done(answer); }
      };
      x.onerror = x.ontimeout = function () { if (done) { done(null); } };
      x.send(message(body));
    }

    // A message for another page in the call. One that got no answer (lost, or "too
    // many": the daemon took none of it) goes again, while `still()` says it is wanted.
    function send(to, kind, data, still) {
      var tries = 0;
      (function go() {
        tries += 1;
        post({ "do": "send", to: to, kind: kind, data: data }, function (a) {
          if (!a && tries < SENDS && inCall && (!still || still())) { window.setTimeout(go, RETRY_MS); }
        });
      })();
    }

    // An offer or an answer short enough to go (see MAX_SDP): its host addresses are left
    // out, IPv6 first, then the others, from the last, until it fits. The public ones
    // (from STUN) are what connects two homes.
    function fit(sdp) {
      if (sdp.length <= MAX_SDP) { return sdp; }
      var lines = sdp.split("\r\n");
      function host(line, v6) {
        if (line.indexOf("a=candidate:") !== 0 || line.indexOf(" typ host") < 0) { return false; }
        var address = line.split(" ")[4] || "";
        return !v6 || address.indexOf(":") >= 0;
      }
      [true, false].forEach(function (v6) {
        for (var i = lines.length - 1; i >= 0 && lines.join("\r\n").length > MAX_SDP; i--) {
          if (host(lines[i], v6)) { lines.splice(i, 1); }
        }
      });
      return lines.join("\r\n");
    }

    // What this page has on, told the daemon a moment after it changes: the tiles shown
    // may change several times in a row (a turn of the phone).
    function tellSoon() {
      if (stateTimer) { window.clearTimeout(stateTimer); }
      stateTimer = window.setTimeout(function () {
        stateTimer = null;
        if (inCall) { post({ "do": "state", mic: mic, cam: cam, sees: shown() }); }
      }, STATE_MS);
    }

    // --- this page's camera and mic

    function wants(withVideo, withAudio) {
      var v = false;
      if (withVideo) {
        v = { facingMode: facing };
        for (var key in CAMERA) { v[key] = CAMERA[key]; }
      }
      return { video: v, audio: withAudio ? SOUND : false };
    }

    function first(stream, kind) {
      var list = stream && stream["get" + (kind === "audio" ? "Audio" : "Video") + "Tracks"]();
      return list && list.length ? list[0] : null;
    }

    function setVideo(track) {
      if (video && video !== track) { video.stop(); }
      video = track;
      preview = track ? new window.MediaStream([track]) : null;
    }

    // The camera and the mic, or what of them can be had: the camera alone, or the mic
    // alone. `done` runs either way.
    function getMedia(done) {
      var asks = [wants(cam, true), wants(false, true), wants(cam, false)];
      function next(i) {
        if (i >= asks.length || (!asks[i].video && !asks[i].audio)) {
          cam = false;
          mic = false;
          hooks.said("The camera and the mic cannot be used here: you see and hear the others only.");
          return done();
        }
        media().getUserMedia(asks[i]).then(function (stream) {
          audio = first(stream, "audio");
          setVideo(first(stream, "video"));
          if (!video) { cam = false; }
          if (!audio) { mic = false; }
          if (audio) { audio.enabled = mic; }
          countCameras();
          done();
        }, function () { next(i + 1); });
      }
      next(0);
    }

    function countCameras() {
      var m = media();
      if (!m.enumerateDevices) { return; }
      m.enumerateDevices().then(function (list) {
        cameras = list.filter(function (d) { return d.kind === "videoinput"; }).length;
        hooks.changed();
      }, nothing);
    }

    // The camera (facing as `facing` says) in place of this page's video, while in the
    // call: `then` after it is, `failed` when the browser gives none.
    function camera(then, failed) {
      media().getUserMedia(wants(true, false)).then(function (stream) {
        if (!inCall) { first(stream, "video").stop(); return; }
        setVideo(first(stream, "video"));
        then();
        steerAll();
        hooks.changed();
      }, failed);
    }

    // The mic asked for again, on a tap of Mic while this page has none: the one asked for
    // on joining may have been refused then, or been taken since by another app (iOS).
    // A refusal now says why (NotAllowedError, NotReadableError...).
    var askingMic = false;
    function micAgain() {
      if (askingMic) { return; }
      askingMic = true;
      media().getUserMedia(wants(false, true)).then(function (stream) {
        askingMic = false;
        var track = first(stream, "audio");
        if (!inCall || !track) {
          if (track) { track.stop(); }
          if (inCall) { hooks.said("No mic can be used here."); }
          return;
        }
        if (audio) { audio.stop(); }
        audio = track;
        mic = true;
        steerAll();
        tellSoon();
        hooks.changed();
      }, function (e) {
        askingMic = false;
        var why = e && e.name && e.name !== "Error" ? " (" + e.name + ")" : "";
        hooks.said("No mic can be used here" + why + ".");
      });
    }

    function stopMedia() {
      if (audio) { audio.stop(); }
      audio = null;
      setVideo(null);
    }

    // --- the connections

    function offerer(page) { return me < page; }

    function sendsVideo(page) {
      var m = member(page);
      return !!(cam && video && !filmWaits && m && m.sees && m.sees.indexOf(me) >= 0);
    }

    // At most MAX_BITRATE and FPS of video, SOUND_BITRATE of sound, both after anything
    // else of this browser (the film). A short link costs the video its sharpness first.
    function cap(sender, isVideo) {
      if (!sender || !sender.getParameters || !sender.setParameters) { return; }
      try {
        var p = sender.getParameters();
        if (!p.encodings || !p.encodings.length) { p.encodings = [{}]; }
        var e = p.encodings[0];
        e.maxBitrate = isVideo ? MAX_BITRATE : SOUND_BITRATE;
        e.priority = e.networkPriority = isVideo ? "very-low" : "low";
        if (isVideo) {
          e.maxFramerate = FPS;
          p.degradationPreference = "maintain-framerate";
        }
        var done = sender.setParameters(p);
        if (done && done["catch"]) { done["catch"](nothing); }
      } catch (e) { /* not yet negotiated: done once it connects */ }
    }

    function replace(sender, track) {
      if (!sender || sender.track === track) { return; }
      var done = sender.replaceTrack(track);
      if (done && done["catch"]) { done["catch"](nothing); }
    }

    // What goes to one page: the sound always (the mic's own switch mutes it), the video
    // only where it shows.
    function steer(peer) {
      replace(peer.audioSender, audio);
      replace(peer.videoSender, sendsVideo(peer.page) ? video : null);
    }

    function steerAll() { for (var p in peers) { steer(peers[p]); } }

    function newPeer(page) {
      var el = document.createElement("audio");
      el.setAttribute("autoplay", "");
      if (hooks.audioBox) { hooks.audioBox.appendChild(el); }
      peers[page] = { page: page, pc: null, sound: el, stream: null, state: "connecting", tries: 0, timer: null,
        audioSender: null, videoSender: null };
      return peers[page];
    }

    function closePc(peer) {
      if (peer.timer) { window.clearTimeout(peer.timer); peer.timer = null; }
      if (peer.pc) {
        var pc = peer.pc;
        peer.pc = null;
        try { pc.close(); } catch (e) { /* closed already */ }
      }
      peer.audioSender = peer.videoSender = null;
    }

    function drop(page) {
      var peer = peers[page];
      if (!peer) { return; }
      closePc(peer);
      peer.sound.srcObject = null;
      if (peer.sound.parentNode) { peer.sound.parentNode.removeChild(peer.sound); }
      delete peers[page];
    }

    // A new connection for this pair, in place of the one it had.
    function fresh(peer) {
      closePc(peer);
      peer.stream = null;
      peer.state = "connecting";
      var pc = new window.RTCPeerConnection({ iceServers: ICE });
      peer.pc = pc;
      pc.ontrack = function (event) {
        if (peer.pc !== pc) { return; }
        var one = new window.MediaStream([event.track]);
        if (event.track.kind === "audio") {
          peer.sound.srcObject = one;
          var playing = peer.sound.play && peer.sound.play();
          if (playing && playing["catch"]) { playing["catch"](nothing); }
        } else {
          peer.stream = one;
        }
        hooks.changed();
      };
      pc.onconnectionstatechange = function () {
        if (peer.pc !== pc) { return; }
        var s = pc.connectionState;
        if (s === "connected") {
          peer.state = "connected";
          peer.tries = 0;
          if (peer.timer) { window.clearTimeout(peer.timer); peer.timer = null; }
          cap(peer.audioSender, false);
          cap(peer.videoSender, true);
        } else if (s === "failed") {
          failed(peer, pc);
        }
        hooks.changed();
      };
      peer.timer = window.setTimeout(function () {
        if (peer.pc === pc && peer.state !== "connected") { failed(peer, pc); }
      }, CONNECT_MS);
      return pc;
    }

    // Whether `pc` is still the pair's connection: its offer or answer is still wanted.
    function mine(peer, pc) { return function () { return peer.pc === pc && peers[peer.page] === peer; }; }

    // An offer or an answer goes when its browser has found its addresses, or after
    // GATHER_MS with those it has.
    function gathered(pc) {
      return new Promise(function (resolve) {
        if (pc.iceGatheringState === "complete") { return resolve(); }
        var timer = window.setTimeout(resolve, GATHER_MS);
        pc.onicegatheringstatechange = function () {
          if (pc.iceGatheringState === "complete") { window.clearTimeout(timer); resolve(); }
        };
      });
    }

    function offer(peer) {
      var pc = fresh(peer);
      peer.audioSender = pc.addTransceiver("audio", { direction: "sendrecv" }).sender;
      peer.videoSender = pc.addTransceiver("video", { direction: "sendrecv" }).sender;
      steer(peer);
      pc.createOffer().then(function (d) { return pc.setLocalDescription(d); })
        .then(function () { return gathered(pc); })
        .then(function () { if (peer.pc === pc) { send(peer.page, "offer", fit(pc.localDescription.sdp), mine(peer, pc)); } })
        ["catch"](function () { failed(peer, pc); });
    }

    function answer(peer, sdp) {
      var pc = fresh(peer);
      pc.setRemoteDescription({ type: "offer", sdp: sdp }).then(function () {
        pc.getTransceivers().forEach(function (t) {
          var kind = t.receiver && t.receiver.track && t.receiver.track.kind;
          t.direction = "sendrecv";
          if (kind === "audio") { peer.audioSender = t.sender; }
          if (kind === "video") { peer.videoSender = t.sender; }
        });
        steer(peer);
        return pc.createAnswer();
      }).then(function (d) { return pc.setLocalDescription(d); })
        .then(function () { return gathered(pc); })
        .then(function () { if (peer.pc === pc) { send(peer.page, "answer", fit(pc.localDescription.sdp), mine(peer, pc)); } })
        ["catch"](function () { failed(peer, pc); });
    }

    // A pair that did not connect tries again, TRIES times: the one that offers offers
    // again; the other asks it to (bye).
    function failed(peer, pc) {
      if (peer.pc !== pc || peers[peer.page] !== peer) { return; }
      peer.tries += 1;
      closePc(peer);
      if (peer.tries >= TRIES) {
        peer.state = "failed";
        hooks.changed();
        return;
      }
      peer.state = "connecting";
      if (offerer(peer.page)) {
        window.setTimeout(function () { if (peers[peer.page] === peer && !peer.pc && inCall) { offer(peer); } }, RETRY_MS);
      } else {
        send(peer.page, "bye", "", function () { return peers[peer.page] === peer && !peer.pc; });
      }
      hooks.changed();
    }

    function read(m) {
      var peer = peers[m.from];
      if (!peer || typeof m.data !== "string") { return; }
      if (m.kind === "offer" && !offerer(m.from)) {
        answer(peer, m.data);
      } else if (m.kind === "answer" && peer.pc && peer.pc.signalingState === "have-local-offer") {
        var pc = peer.pc;
        pc.setRemoteDescription({ type: "answer", sdp: m.data })["catch"](function () { failed(peer, pc); });
      } else if (m.kind === "bye" && offerer(m.from) && peer.tries < TRIES) {
        peer.tries += 1;
        offer(peer);
      }
    }

    // --- the call as the daemon says it

    // The connections follow who is in the call: one to each other page, none to a page
    // that left.
    function follow() {
      var others = {};
      members.forEach(function (m) {
        if (m.page === me) { return; }
        others[m.page] = true;
        if (!peers[m.page]) {
          var peer = newPeer(m.page);
          if (offerer(m.page)) { offer(peer); }
        }
      });
      for (var p in peers) { if (!others[p]) { drop(p); } }
      steerAll();
    }

    // Out of the call, here: every connection closed, the camera and the mic let go.
    function leaveHere() {
      for (var p in peers) { drop(p); }
      stopMedia();
      inCall = false;
      forget();
    }

    // The place to take again if this tab loads the player again soon (see `again`).
    function remember() {
      try {
        if (window.sessionStorage) {
          window.sessionStorage.setItem(KEY, JSON.stringify({ id: id, page: me, mic: mic, cam: cam, at: Date.now() }));
        }
      } catch (e) { /* a private window: it is rung again */ }
    }

    function forget() {
      try { if (window.sessionStorage) { window.sessionStorage.removeItem(KEY); } } catch (e) { /* none kept */ }
    }

    function kept() {
      try {
        var place = JSON.parse((window.sessionStorage && window.sessionStorage.getItem(KEY)) || "null");
        return place && Date.now() - place.at < AGAIN_MS ? place : null;
      } catch (e) { return null; }
    }

    // A call state from the daemon (the answer to a wait, a report or a post). True when
    // it had news: a change, or a message.
    function update(s) {
      if (!s || typeof s.v !== "number") { return false; }
      // The daemon came back: its count of changes starts over (see together.Call).
      if (s.boot !== boot) {
        boot = s.boot;
        version = -1;
      }
      var news = false;
      if (s.id !== id) {
        if (inCall) {
          leaveHere();
          if (!s.id) { hooks.said("The call ended."); }
        }
        id = s.id;
        after = 0;
        version = -1;
      }
      if (s.v >= version) {
        news = s.v !== version;
        version = s.v;
        everyone = s.members || [];
        members = everyone.filter(function (m) { return !m.away || m.page === me; });
        by = s.by || "";
        starter = s.starter || "";
        ring = !!s.ring;
        var was = inCall;
        inCall = !!member(me);
        if (was && !inCall) { leaveHere(); }
        if (inCall) { follow(); }
      }
      (s.mail || []).forEach(function (m) {
        if (m.seq > after) {
          after = m.seq;
          news = true;
          if (inCall) { read(m); }
        }
      });
      if (!inCall && !joining && id) { again(); }
      hooks.changed();
      return news;
    }

    // This tab was in this call before it loaded again: back in, as it was.
    function again() {
      var place = kept();
      forget();
      if (!place || place.id !== id || !member(place.page, everyone)) { return; }
      mic = !!place.mic;
      cam = place.cam !== false;
      join(place.page, place.id);
    }

    // Into the call `expect` (the one rung, or the one this tab was in), or a new one
    // when "". The daemon says "ended" when that call is over by the time the browser has
    // the camera: no new call is made for a Join.
    function join(was, expect) {
      if (joining || inCall || (was && !id)) { return; }
      joining = true;
      hooks.changed();
      getMedia(function () {
        post({ "do": "start", was: was || "", call: expect || "", mic: mic, cam: cam, sees: shown() }, function (a) {
          joining = false;
          var ok = a && (a.outcome === "started" || a.outcome === "joined");
          if (!ok) {
            if (!inCall) { stopMedia(); }
            hooks.said(a && a.outcome === "full" ? "The call is full." :
              a && a.outcome === "ended" ? "The call ended." : "Could not join the call.");
          } else {
            dismissed = id;
            steerAll();
          }
          hooks.changed();
        });
      });
    }

    // The tab closes or loads again: the others stop showing it now, not when it has been
    // quiet a while. (keepalive: the post goes on after the page has gone.)
    function goodbye() {
      if (!window.fetch) { return; }
      try {
        var sent = window.fetch(url, { method: "POST", keepalive: true,
          headers: { "X-Vpstream-Player": "1", "Content-Type": "application/json" },
          body: message({ "do": "away" }) });
        if (sent && sent["catch"]) { sent["catch"](nothing); }
      } catch (e) { /* it leaves when quiet */ }
    }

    if (window.addEventListener) {
      window.addEventListener("pagehide", function () {
        if (inCall) { remember(); goodbye(); }
      });
      // The browser kept the page and shows it again (Back): it is back in the call, if
      // nothing took its place.
      window.addEventListener("pageshow", function (event) {
        if (event && event.persisted && inCall) { forget(); tellSoon(); }
      });
    }

    return {
      possible: possible(),
      update: update,
      // What a report or a wait says of the call it has seen.
      query: function () { return ["cv=" + Math.max(0, version), "cm=" + after]; },
      // Call, or Join: a new call when none runs. The camera starts on, the mic off.
      start: function () {
        if (inCall || joining) { return; }
        mic = false;
        cam = true;
        join("", id);
      },
      // Not now: the ring of this call goes away; Call joins it later.
      dismiss: function () { dismissed = id; hooks.changed(); },
      end: function () {
        if (!inCall) { return; }
        post({ "do": "end" });
        leaveHere();
        hooks.changed();
      },
      toggleMic: function () {
        if (!audio || audio.readyState === "ended") {
          if (inCall) { micAgain(); } else { hooks.said("No mic can be used here."); }
          return;
        }
        mic = !mic;
        audio.enabled = mic;
        tellSoon();
        hooks.changed();
      },
      toggleCam: function () {
        if (cam) {
          cam = false;
          setVideo(null);
          steerAll();
          tellSoon();
          hooks.changed();
          return;
        }
        camera(function () {
          cam = !!video;
          tellSoon();
        }, function () { hooks.said("The camera cannot be used here."); });
      },
      // The front camera or the back one.
      flip: function () {
        facing = facing === "user" ? "environment" : "user";
        if (!cam || !video) { hooks.changed(); return; }
        camera(nothing, function () {
          facing = facing === "user" ? "environment" : "user";
          hooks.said("The other camera cannot be used.");
        });
      },
      // The pages whose tiles show now; only they get this page's video.
      show: function (pages) {
        var next = (pages || []).filter(function (p) { return p !== me; });
        if (next.join(",") === sees.join(",")) { return; }
        sees = next;
        if (inCall) { tellSoon(); }
      },
      // The film is short of data: no video goes out or comes in until it has had enough
      // for a while again.
      film: function (waits) {
        if (waits) {
          if (resumeTimer) { window.clearTimeout(resumeTimer); resumeTimer = null; }
          if (!filmWaits) {
            filmWaits = true;
            steerAll();
            if (inCall) { tellSoon(); }
          }
        } else if (filmWaits && !resumeTimer) {
          resumeTimer = window.setTimeout(function () {
            resumeTimer = null;
            filmWaits = false;
            steerAll();
            if (inCall) { tellSoon(); }
          }, RESUME_MS);
        }
      },
      // The tiles: the others in the order they joined, then this page's own. `state` is
      // "connecting", "connected" or "failed"; `stream` the video to show, null for none.
      tiles: function () {
        if (!inCall) { return []; }
        var list = [];
        members.forEach(function (m) {
          if (m.page === me) { return; }
          var peer = peers[m.page];
          list.push({ page: m.page, name: m.name, mine: false, mic: !!m.mic, cam: !!m.cam,
            stream: peer && peer.stream, state: peer ? peer.state : "connecting" });
        });
        list.push({ page: me, name: hooks.name(), mine: true, mic: mic, cam: cam && !!video,
          stream: preview, state: "connected", front: facing === "user" });
        return list;
      },
      running: function () { return !!id; },
      inCall: function () { return inCall; },
      joining: function () { return joining; },
      // Rung: someone started a call (`by`) and this page has not answered.
      ringing: function () { return ring && !!id && !inCall && !joining && dismissed !== id; },
      by: function () { return by; },
      // The others in the call, by name, but who started it.
      others: function () {
        return members.filter(function (m) { return m.page !== starter; }).map(function (m) { return m.name; });
      },
      count: function () { return members.length; },
      mic: function () { return mic; },
      cam: function () { return cam && !!video; },
      canFlip: function () { return cameras > 1; }
    };
  };
})();
