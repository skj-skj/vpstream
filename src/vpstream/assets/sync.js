// What both looks of the player share (player.js for a TV's remote, modern.js for touch):
// the reports to the daemon and the owner's commands, and watching together. A look makes
// one with window.VpSync(S, video, ui, look), where `look` gives what is the page's own
// (see `hooks` below), and draws what `status()` says. ES5.
//
// Every REPORT_MS the page tells the daemon where it is and what it plays with, and gets back
// what the owner asked of it on the dashboard. The newer of a press and a command wins: a
// report says how long ago each kind of control was pressed, so the daemon drops a command
// made before; a command that crosses a press on the way back is dropped here. Either way the
// daemon's record ends up as what this player does. Each page has an id of its own, so two
// tabs of one browser both get the owner's commands. An answer that says the stream now uses
// the other look loads the page again, with it.
//
// Watching together (S.together), the daemon holds the group's state and this page follows
// it: whether it plays, the video, where, the audio and the subtitles. A report then only
// says where the page is; a press goes to the group (act=) and comes back as the group's
// state, here and on every other page. Nothing the browser does by itself (a pause when the
// tab is put away or the headphones come out) goes to the group. The page waits for the
// group to change on S.together.sync, a request the daemon holds until it does. It keeps
// close to the group by its playback rate, and a change of the group's (a press anywhere,
// its join) puts it at the group's place by a seek. A page that drifts more than
// T.tolerance off by itself (its video waits for data on a slow link) is out of sync: it is
// not seeked, which would throw away what it has fetched; it plays on where it is, and Sync
// puts it at the group's place, behind or ahead. A page that watches alone is never out of
// sync: the daemon takes the group where it is (see together.Room.report). A page that just
// joined has a cooldown: its controls for the group do nothing for a while.
//
// The subtitle size is each browser's own, also watching together: a press changes it on
// this page only, and the owner's command for it comes with the answer to a report.
(function () {
  "use strict";
  var SOON_MS = 300;  // a change is reported this soon after it is made
  var REPORT_TIMEOUT_MS = 8000;
  var KEYS = { audio: "pa", sub: "ps", seek: "pk", size: "pz" };  // see clients.PRESSED
  var RATE_OFF = 0.3;  // off the group: a little faster or slower, until it is this close:
  var RATE_DONE = 0.2;
  var RATE_ONLY_SEEK = 1;  // where the rate cannot be changed, a seek from this far off
  var PAUSED_OFF = 0.25;  // paused, a seek from this far off
  var JOINED_OFF = 0.5;  // a page this close to the group is at its place; until then, a seek
  var MAX_SEEK_COST = 5;  // seconds: a seek to keep up aims at most this far ahead
  var JOIN_SEEKS = 2;  // seeks to get to the group's place; then by its rate, as any page
  var SEEK_GAP_MS = 1000;  // a seek to keep up is given this long to happen
  var WAIT_RETRY_MS = 2000;
  var WAIT_TIMEOUT_MS = 30000;
  var HINTS = {
    play: "Playing for everyone...", pause: "Pausing for everyone...",
    seek: "Seeking for everyone...", skip: "Seeking for everyone...",
    audio: "Changing the audio for everyone...", sub: "Changing the subtitles for everyone...",
    video: "Changing the video for everyone..."
  };
  var REFUSED = "That cannot be done for everyone right now.";
  // A page away long enough has joined again (together.PAGE_SECONDS), before it heard so.
  var COOLING = "Not done: you joined again, the controls work in a few seconds.";

  function counts(from) {
    var out = {};
    for (var kind in KEYS) { out[kind] = from ? from[kind] : 0; }
    return out;
  }

  function nothing() {}

  // `look`: settled() whether the loaded version is at its start position (its time is
  // real); audio(), sub(), size() what it plays with, as a report says them; name() the
  // viewer's own name, "" for none; setAudio(number), setSub(id), setSize(id) false where
  // this video has no such; seekTo(t); showControls(); update() the time and the play
  // button; render() what status() says. Optional: changed(state, first) after a new state
  // of the group, joined() once this page is at the group's place, outOfSync() when Sync
  // comes up (by default the controls show, with it).
  window.VpSync = function (S, video, ui, look) {
    var hooks = { changed: nothing, joined: nothing, name: function () { return ""; } };
    for (var key in look) { hooks[key] = look[key]; }
    hooks.outOfSync = hooks.outOfSync || hooks.showControls;
    var T = S.together || null;
    var SYNC_OFF = (T && T.tolerance) || 2;  // seconds off the group: out of sync
    var page = Math.random().toString(36).slice(2, 10) || "p";
    var seen = S.ack || 0;  // the newest command seen; the page itself carries the older ones
    var pressed = counts();  // counts the viewer's own presses
    var told = counts();  // ...as far as a report that arrived said
    var pressedAt = counts();  // when the last one was (Date.now)
    var asking = null;  // the report on its way
    var soonTimer = null;
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
    // What a seek to keep up costs: from asking for it until the video plays there again,
    // the group moves on. One aims ahead by the last one's cost (seekCost).
    var seekCost = 0, seekBegan = 0;
    var joinSeeks = 0;
    var follow = false;  // the group changed: a seek may put this page at its new place
    var landed = false;  // a seek aimed ahead is done: if it was quicker, it waits there
    var early = false;  // paused ahead of the group, for it to come
    var behind = false;  // out of sync: Sync shows
    var alone = false;  // no other page watches: the group goes where this page is
    var pausingHere = false;  // a pause of this page's own, till its pause event

    // --- reporting to the daemon, and doing what the owner asks

    // Where it is and what it plays with, as both kinds of report say it.
    function where(q) {
      if (hooks.settled()) { q.push("t=" + Math.max(0, video.currentTime).toFixed(1)); }
      return q;
    }

    function plays(q) {
      if (isFinite(video.duration) && video.duration > 0) { q.push("d=" + video.duration.toFixed(1)); }
      if (S.audio.length) { q.push("a=" + hooks.audio()); }
      q.push("s=" + encodeURIComponent(hooks.sub()), "z=" + hooks.size());
      return q;
    }

    function pressedAgo(q, kind) {
      if (pressed[kind] !== told[kind]) {
        q.push(KEYS[kind] + "=" + Math.max(0, (Date.now() - pressedAt[kind]) / 1000).toFixed(1));
      }
    }

    function query() {
      if (T) { return groupQuery(); }
      var q = ["p=" + (video.paused ? 1 : 0), "ack=" + seen, "pg=" + page];
      for (var kind in KEYS) { pressedAgo(q, kind); }
      return plays(where(q)).join("&");
    }

    function report() {
      if (asking || !S.report) { return; }
      var before = counts(pressed);
      var sent = Date.now(), act = T ? acts[0] : null, said = joined;
      var url = S.report + "?" + query();  // before `asking`: a query that throws holds up none
      var x = new XMLHttpRequest();
      asking = x;
      x.open("GET", url, true);
      x.setRequestHeader("X-Vpstream-Player", "1");  // see player.REPORT_HEADER
      x.timeout = REPORT_TIMEOUT_MS;
      x.onload = function () {
        asking = null;
        if (x.status !== 200) { return; }
        told = before;  // the daemon knows of these presses now
        var answer;
        try { answer = JSON.parse(x.responseText); } catch (e) { return; }
        // The owner switched the stream to the other look: load the page again, with it.
        if (answer.ui && answer.ui !== ui && window.location.reload) {
          window.location.reload();
          return;
        }
        if (T) { return answered(answer, sent, act, said, before); }
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
        if ("audio" in c && pressed.audio === before.audio) { changed = hooks.setAudio(c.audio) || changed; }
        if ("sub" in c && pressed.sub === before.sub) { changed = hooks.setSub(c.sub) || changed; }
        if ("seek" in c && pressed.seek === before.seek) { hooks.seekTo(c.seek); changed = true; }
        if ("sub_size" in c && pressed.size === before.size) { changed = hooks.setSize(c.sub_size) || changed; }
      }
      if (newest > seen) {
        seen = newest;
        if (changed) { hooks.showControls(); }
        reportSoon();  // says what was seen, and what it did
      }
    }

    // A press of the viewer's own: it wins over a command made before it, or crossing it.
    function mark(kind) {
      pressed[kind] += 1;
      pressedAt[kind] = Date.now();
      reportSoon();
    }

    // --- watching together

    function groupQuery() {
      var q = ["p=" + (video.paused ? 1 : 0), "pg=" + page, "rev=" + (group ? group.rev : 0), "ack=" + seen];
      pressedAgo(q, "size");  // only the subtitle size is pressed here: the rest goes to the group
      where(q);
      // How far it is off the group, as it measures it: the daemon cannot tell how long the
      // report took to get there (see together.Room.report).
      // Not while its own pause is on its way: it stopped, the group has not yet.
      if (hooks.settled() && group && group.video === S.number && groupPaused() === group.paused) {
        q.push("o=" + (video.currentTime - groupAt()).toFixed(2));
      }
      plays(q);
      var name = hooks.name();
      if (name) { q.push("n=" + encodeURIComponent(name)); }
      if (joined) { q.push("in=1"); }
      if (acts.length) {
        q.push("act=" + acts[0].act, "id=" + acts[0].id);
        if (acts[0].v !== undefined) { q.push("v=" + encodeURIComponent(String(acts[0].v))); }
      }
      return q.join("&");
    }

    // The answer to a report: the group's state, what became of the press it carried, and
    // the owner's commands for this browser (its subtitle size).
    function answered(answer, sent, act, said, before) {
      obey(answer.commands || [], before);
      if (act && acts[0] === act) {
        acts.shift();
        if (answer.outcome === "queued") { showHint(HINTS[act.act]); }
        if (answer.outcome === "refused") { showHint(REFUSED); }
        if (answer.outcome === "cooldown") { showHint(COOLING); }
      }
      if (said && typeof answer.cooldown === "number") {
        heard = true;
        until = Date.now() + answer.cooldown * 1000;
      }
      adopt(answer, sent);
      if (acts.length) { report(); }
    }

    // A notice of this page's own, under the group's while a window is open.
    function showHint(text) {
      hint = text || "";
      hintUntil = Date.now() + ((group && group.window) || 2) * 1000 + 1000;
      hooks.render();
    }

    // Where the group is now, as far as this page can tell: what it was told, moved on by
    // the time since, and by half the trip of the answer.
    function groupAt() {
      if (!group) { return 0; }
      var at = group.position;
      if (!group.paused) { at += (Date.now() - group.at) / 1000 + group.lead; }
      return group.duration ? Math.min(at, group.duration) : at;
    }

    // ...where this video can be: a seek stops half a second before its end (see seekTo).
    function target() {
      return Math.min(groupAt(), video.duration ? video.duration - 0.5 : Infinity);
    }

    // Take the group's state from an answer that left at `sent`. An answer older than the
    // one taken last is dropped: one of an older rev, or of the same rev by the daemon's
    // clock (a rev only goes up, the clock may be set back); a new rev is followed.
    function adopt(answer, sent) {
      var s = answer && answer.together;
      if (!s || typeof s.rev !== "number") { return; }
      if (group && (s.rev < group.rev || (s.rev === group.rev && s.now < group.now))) { return; }
      alone = answer.alone === true;
      var trip = Math.max(0, (Date.now() - sent) / 1000 - (answer.waited || 0));
      var fresh = !group || s.rev !== group.rev;
      var first = !group;
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
        follow = true;
        early = false;
        hint = "";
        if (s.audio !== null && s.audio !== undefined) { hooks.setAudio(s.audio); }
        if (s.sub !== null && s.sub !== undefined) { hooks.setSub(s.sub); }
        if (!s.paused) { held = false; }
        hooks.showControls();
        hooks.changed(s, first);
      }
      keepUp();
      hooks.update();
      hooks.render();
    }

    function setRate(rate) {
      if (video.playbackRate === rate) { rated = rate !== 1; return; }
      video.playbackRate = rate;
      if (Math.abs((video.playbackRate || 1) - rate) > 0.001) { rateWorks = false; }
      rated = rate !== 1 && rateWorks;
    }

    function catchUp(at, ahead) {
      lastSeek = Date.now();
      seekBegan = ahead ? lastSeek : 0;
      hooks.seekTo(at + (ahead ? seekCost : 0));
    }

    // How long a seek to keep up took, until the video was there again.
    function seekDone() {
      if (!seekBegan) { return; }
      seekCost = Math.min(MAX_SEEK_COST, (Date.now() - seekBegan) / 1000);
      seekBegan = 0;
      landed = true;
    }

    // Keep this page with the group: play or pause as it does, and stay at its place.
    function keepUp() {
      if (!T || !group || !hooks.settled() || video.readyState < 1 || video.seeking) { return; }
      var to = target();
      var off = video.currentTime - to;
      var far = Math.abs(off);
      var free = Date.now() - lastSeek >= SEEK_GAP_MS;
      var out = false;
      var stays = false;  // alone, it stays where it is
      var ahead = landed && off > RATE_OFF && off <= MAX_SEEK_COST + 1;
      landed = false;
      if (groupPaused()) {
        setRate(1);
        early = false;
        if (!video.paused) { pauseHere(); }
        // Not while its own pause is on its way: where the group stopped is not known yet.
        if (far > PAUSED_OFF && free && group.paused) { catchUp(to); }
        follow = false;
      } else if (early) {
        // Ahead of the group after a seek that was quicker than the last: the group comes.
        if (off <= 0.05) { early = false; startHere(); }
      } else if (ahead && !video.paused) {
        early = true;
        setRate(1);
        pauseHere();
      } else if (video.paused) {
        if (!held && !starting) { startHere(); }
      } else if (!joined && joinSeeks < JOIN_SEEKS && far > JOINED_OFF) {
        // A page that has just come is put at the group's place by a seek, not by its rate:
        // its controls wait for it to get there. Seeks that take very uneven times never land
        // near it: after a few it is in, by its rate when near, out of sync when not.
        if (free) {
          joinSeeks += 1;
          follow = false;
          setRate(1);
          catchUp(to, true);
        }
      } else if (alone && !follow) {
        // Nobody else watches: where this page is, the group goes. No seek, no Sync.
        stays = true;
        setRate(1);
      } else if (far > SYNC_OFF) {
        setRate(1);
        if (!follow) {
          out = true;
        } else if (free) {
          follow = false;
          catchUp(to, true);
        }
      } else {
        if (free) { follow = false; }
        if (!rateWorks && far > RATE_ONLY_SEEK) {
          if (free) { catchUp(to, true); }
        } else if (rated && far < RATE_DONE) {
          setRate(1);
        } else if (rateWorks && far > RATE_OFF) {
          setRate(off > 0 ? 0.95 : 1.05);
        }
      }
      // Not while a seek runs: the video's time is already the seek's, but it is not there.
      // Out of sync after its seeks to join, or alone, it is in all the same: its cooldown starts.
      if (!joined && !video.seeking && (groupPaused() || !video.paused) &&
          (Math.abs(video.currentTime - to) < JOINED_OFF || ((out || stays) && free))) {
        joined = true;
        reportSoon();
        hooks.joined();
      }
      if (out !== behind) {
        behind = out;
        if (behind) { hooks.outOfSync(); }
        hooks.render();
      }
    }

    // Sync: this page only, to the group's place, aimed ahead by what a seek takes. It works
    // in the cooldown too: the group is not changed.
    function syncHere() {
      if (!group || group.paused) { return; }
      behind = false;
      follow = false;
      early = false;
      setRate(1);
      catchUp(target(), true);
      hooks.render();
    }

    // A pause of this page's own: not one the browser made (see pauseKind). Browsers send
    // the pause event later, not from pause(): the mark waits for it.
    function pauseHere() {
      if (video.paused) { return; }
      pausingHere = true;
      video.pause();
    }

    // Whether the group is paused, as far as this page goes: a press of pause here pauses it
    // at once, while the press is on its way (on a slow link its answer may take a while).
    // The last press of play or pause still to go out, or on its way, decides.
    function groupPaused() {
      for (var i = acts.length - 1; i >= 0; i--) {
        if (acts[i].act === "pause") { return true; }
        if (acts[i].act === "play") { break; }
      }
      return !!group && group.paused;
    }

    // Play here only. A browser that will not play without a tap shows the join button.
    function startHere() {
      var p = video.play();
      if (!(p && p.then)) { return; }
      starting = true;
      p.then(function () { starting = false; held = false; hooks.render(); }, function () {
        starting = false;
        held = true;
        hooks.render();
        hooks.showControls();
      });
    }

    function locked() {
      return !!T && (!joined || !heard || Date.now() < until);
    }

    // A press for the group: it goes out with the next report, which goes now.
    function propose(act, value) {
      if (locked()) { return; }
      actCount += 1;
      acts.push({ act: act, v: value, id: page + "-" + actCount });
      if (asking) { reportSoon(); } else { report(); }
      if (act === "pause") { keepUp(); hooks.update(); }
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
      x.onerror = x.ontimeout = x.onabort = function () { window.setTimeout(wait, WAIT_RETRY_MS); };
      x.send();
    }

    return {
      page: page,
      together: T,
      tolerance: SYNC_OFF,
      report: report,
      reportSoon: reportSoon,
      mark: mark,
      propose: propose,
      locked: locked,
      group: function () { return group; },
      groupAt: groupAt,
      groupPaused: groupPaused,
      // Whether it plays, as its play button shows: watching together, as the group does.
      playing: function () { return !(T && group ? groupPaused() : video.paused); },
      keepUp: keepUp,
      syncHere: syncHere,
      pauseHere: pauseHere,
      startHere: startHere,
      seekDone: seekDone,
      hint: showHint,
      // Once for each pause event, while the group plays: "own" for this page's own pause,
      // a moment's wait for the group; "browser" for one the browser made by itself (the
      // tab put away, the headphones out); "" for any other, and at the end.
      pauseKind: function () {
        var own = pausingHere;
        pausingHere = false;
        if (!T || !group || groupPaused() || video.ended) { return ""; }
        if (own) { return "own"; }
        return hooks.settled() ? "browser" : "";
      },
      hold: function () { held = true; hooks.render(); },
      // The join button: a tap starts this page where the browser would not by itself.
      tapped: function () { held = false; startHere(); hooks.render(); },
      // What the controls say while this page waits, which of them work, and whether Sync
      // and the join button show.
      status: function () {
        var text = "";
        if (hint && Date.now() < hintUntil) { text = hint; } else { hint = ""; }
        var off = locked();
        if (!text && off) {
          var left = Math.ceil((until - Date.now()) / 1000);
          text = !joined || !heard || !isFinite(left) ? "Joining the group..." :
            "Watching together: the controls work in " + left + " s";
        }
        return { text: text, locked: off, behind: behind, join: !!(held && group && !groupPaused()) };
      },
      // The group's state the page was made with, and the waits for it to change.
      begin: function () {
        if (!T) { return; }
        adopt({ together: T.state, waited: 0 }, Date.now());
        hooks.render();
        wait();
      }
    };
  };
})();
