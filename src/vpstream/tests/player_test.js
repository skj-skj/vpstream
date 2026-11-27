// Runs the real player page and scripts (vtt.js, player.js) in node, with the small DOM of
// dom.js, a stand-in hls.js, a video element that does what it is told, requests that the
// checks answer themselves and a clock they move. Usage: node player_test.js PAGE VTT PLAYER
"use strict";
const fs = require("fs");
const vm = require("vm");
const { Node, parse, newDocument, settle, runner, eq, ok, text } = require("./dom.js");

const [pagePath, vttPath, playerPath] = process.argv.slice(2);
const PAGE = fs.readFileSync(pagePath, "utf8");
const VTT = fs.readFileSync(vttPath, "utf8");
const PLAYER = fs.readFileSync(playerPath, "utf8");

// --- what the daemon tells the page

function settings(over) {
  return Object.assign({
    title: "Film", number: 1, count: 1, folder: false, index: "/aaa", next: null,
    master: "/aaa/1/hls/base/master.m3u8",
    audio: [{ label: "English", language: "eng", number: 1 }, { label: "Japanese", language: "jpn", number: 2 }],
    start_audio: 0,
    subs: [
      { id: "1-soft", label: "English", number: 1, kind: "soft", url: "/aaa/1/hls/subs/1.vtt" },
      { id: "3-burn", label: "Signs", number: 3, kind: "burn", url: "/aaa/1/hls/burn/3/master.m3u8" },
    ],
    start_sub: -1, start: 0, ack: 0, report: "/aaa/1/client", assets: "/aaa/assets/",
  }, over || {});
}

// --- one page load

function load(over, options) {
  const S = settings(over);
  const html = PAGE.replace("{{SETTINGS}}", JSON.stringify(S)).replace("{{TITLE}}", "t").replace(/\{\{ASSETS\}\}/g, "");
  const root = parse(html);
  const document = newDocument(root);
  let now = 0;
  const timers = [];
  const addTimer = (fn, ms, every) => { timers.push({ fn, at: now + ms, every: every ? ms : 0, live: true }); return timers.length; };
  const window = {
    setInterval: (fn, ms) => addTimer(fn, ms, true),
    setTimeout: (fn, ms) => addTimer(fn, ms || 0, false),
    clearTimeout: (id) => { if (timers[id - 1]) { timers[id - 1].live = false; } },
    location: { href: "" },
    innerWidth: 1000, innerHeight: 400,  // a phone on its side: the video fills the height
    listeners: {},
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
  };
  // Moves the clock, running each timer that comes due, in time order.
  const tick = (ms) => {
    const end = now + ms;
    for (;;) {
      const due = timers.filter((t) => t.live && t.at <= end).sort((a, b) => a.at - b.at)[0];
      if (!due) { break; }
      now = due.at;
      if (due.every) { due.at += due.every; } else { due.live = false; }
      due.fn();
    }
    now = end;
  };

  // The video element, as far as the player uses it.
  const video = document.getElementById("video");
  Object.assign(video, {
    currentTime: 0, duration: NaN, paused: true, muted: false, readyState: 0, played: 0,
    audioTracks: null,
    play() { this.paused = false; this.played += 1; this.dispatch("play"); return Promise.resolve(); },
    pause() { this.paused = true; this.dispatch("pause"); },
    canPlayType: () => (options && options.native ? "maybe" : ""),
  });
  // The video has loaded its start and plays from `at`, as a browser would say it.
  video.ready = (duration, at) => {
    video.duration = duration;
    video.readyState = 1;
    video.dispatch("loadedmetadata");
    video.readyState = 4;
    if (at !== undefined) { video.currentTime = at; }
    video.dispatch("timeupdate");
  };
  // Full screen as a browser has it: the standard API, Safari's webkit one, or none.
  const fullApi = (options && options.full) || "standard";
  const fire = (name) => (document.listeners[name] || []).forEach((fn) => fn({}));
  if (fullApi !== "none") {
    const w = fullApi === "webkit";
    const enter = function () {
      if (options && options.refuse) { return Promise.reject(new Error("not allowed")); }
      document[w ? "webkitFullscreenElement" : "fullscreenElement"] = this;
      fire(w ? "webkitfullscreenchange" : "fullscreenchange");
      return w ? undefined : Promise.resolve();
    };
    const leave = () => {
      document[w ? "webkitFullscreenElement" : "fullscreenElement"] = null;
      fire(w ? "webkitfullscreenchange" : "fullscreenchange");
      return w ? undefined : Promise.resolve();
    };
    document[w ? "webkitFullscreenEnabled" : "fullscreenEnabled"] = true;
    document[w ? "webkitFullscreenElement" : "fullscreenElement"] = null;
    document[w ? "webkitExitFullscreen" : "exitFullscreen"] = leave;
    root[w ? "webkitRequestFullscreen" : "requestFullscreen"] = enter;
  }
  document.getElementById("bar").getBoundingClientRect = () => ({ left: 0, width: 1000 });

  const hlses = [];
  class Hls {
    constructor(config) { this.config = config; this.handlers = {}; this.audioTracks = [{}, {}]; this.audioTrack = 0; hlses.push(this); }
    static isSupported() { return !(options && options.native); }
    on(name, fn) { this.handlers[name] = fn; }
    emit(name, data) { this.handlers[name](name, data); }
    loadSource(url) { this.url = url; }
    attachMedia() {}
    destroy() { this.destroyed = true; }
    startLoad() {}
    recoverMediaError() {}
  }
  Hls.Events = { FRAG_LOADED: "frag", MANIFEST_PARSED: "manifest", ERROR: "error" };
  Hls.ErrorTypes = { NETWORK_ERROR: "net", MEDIA_ERROR: "media" };
  if (!(options && options.noHls)) { window.Hls = Hls; }

  const xhrs = [];
  class XMLHttpRequest {
    open(method, url) { this.method = method; this.url = url; this.headers = {}; }
    setRequestHeader(name, value) { this.headers[name] = value; }
    send() { this.sent = true; xhrs.push(this); if (options && options.onSend) { options.onSend(this); } }
    // The answer arrives.
    respond(status, body) { this.status = status; this.responseText = typeof body === "string" ? body : JSON.stringify(body); this.onload(); }
    fail() { this.onerror(); }
    get query() {
      const q = {};
      (this.url.split("?")[1] || "").split("&").forEach((p) => { const [k, v] = p.split("="); q[k] = decodeURIComponent(v); });
      return q;
    }
  }

  const Date = { now: () => 1e12 + now };  // the clock the checks move
  const context = { window, document, XMLHttpRequest, Node, console, JSON, Math, Object, Array, String,
    Number, Error, Promise, Date, isFinite, encodeURIComponent, decodeURIComponent, Infinity, NaN };
  window.window = window;
  vm.createContext(context);
  vm.runInContext(VTT, context);
  context.VpVtt = window.VpVtt;
  vm.runInContext(PLAYER, context);
  const reports = () => xhrs.filter((x) => x.url.indexOf(S.report + "?") === 0);
  const later = (fn, ms) => addTimer(fn, ms, false);
  return { S, root, video, hlses, xhrs, reports, tick, later, now: () => now, document, window, $: (id) => root.querySelector("#" + id),
    hls: () => hlses[hlses.length - 1] };
}

const press = (page, id) => page.$(id).click();

// The next report goes out and is answered with these commands.
function exchange(page, commands, ms) {
  page.tick(ms === undefined ? 2000 : ms);
  const x = page.reports().filter((r) => r.status === undefined).pop();
  ok(x, "a report was sent");
  const q = x.query;
  x.respond(200, { commands: commands || [] });
  return q;
}

// --- watching together

let serverNow = 5000;  // the daemon's clock in its answers: each answer is newer
function groupState(over) {
  serverNow += 1;
  return Object.assign({ rev: 1, video: 1, position: 0, paused: true, audio: null, sub: null,
    duration: 0, window: 0, changed_by: "", changed_ago: null, now: serverNow }, over || {});
}

function loadGroup(state, over, options) {
  const together = { state: groupState(state), sync: "/aaa/1/sync", cooldown: 10, names: [] };
  return load(Object.assign({ together, start: (state && state.position) || 0 }, over || {}), options);
}

const waits = (page) => page.xhrs.filter((x) => x.url.indexOf("/aaa/1/sync?") === 0);
const open = (list) => list.filter((x) => x.status === undefined);

// Answer the report on its way with the group's state (and what became of its press).
function answer(page, state, extra) {
  const x = open(page.reports()).pop();
  ok(x, "a report is on its way");
  const q = x.query;
  x.respond(200, Object.assign({ together: groupState(state), cooldown: 0, outcome: q.act ? "applied" : null }, extra || {}));
  return q;
}

// Loaded, at the group's place, and told by the daemon that its cooldown is over.
function inGroup(page, state, at) {
  page.hls().emit("manifest");
  page.video.ready(600, at || 0);
  page.tick(250);  // it is at the group's place: it says so soon
  page.tick(300);
  const q = answer(page, state, { cooldown: 0 });
  eq(q.in, "1", "it says it is in");
  return q;
}

// --- watching together over the internet: a simulated daemon and network

// A small seeded random number generator, so a run is the same every time.
function seeded(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// One page of the group, for `seconds`, with the daemon on the other side of a network:
// each way takes `up` and `down` ms, give or take `jitter`. The daemon holds a wait as
// control does (until the group changes, 20 s at most, saying how long: `waited`). The
// video plays at its playbackRate, and a seek takes `seekMs` (half to one and a half of
// it) before it plays on, as it fetches from the new place. `changes`: [ms, fn(group)]
// the group's changes from elsewhere; `stalls`: [ms, for ms] when the video waits for data
// (a link slower than the video); `presses`: [ms, fn(page)] the viewer's; `seekMsFrom`:
// [ms, seekMs] a seek's time from then on. Returns how far
// the page was from the group (every 100 ms), its seeks, and its changes of rate.
async function netRun(o) {
  const rng = seeded(o.seed || 1);
  const g = { rev: 1, position: o.start || 100, anchor: 0, paused: !!o.paused, duration: 600 };
  let page = null;
  const clock = () => page.now();
  const live = (t) => g.paused ? g.position : g.position + (t - g.anchor) / 1000;
  const state = (t) => ({ rev: g.rev, video: 1, position: Math.round(live(t) * 1000) / 1000, paused: g.paused,
    audio: null, sub: null, duration: g.duration, window: 0, changed_by: "", changed_ago: null, now: 5000 + t / 1000 });
  const delay = (base) => Math.max(1, base + (rng() * 2 - 1) * (o.jitter || 0));
  let held = [];
  const early = [];  // sent while the page loads
  const reply = (x, body) => page.later(() => { if (x.status === undefined) { x.respond(200, body); } }, delay(o.down));
  const answerWait = (w) => reply(w.x, { together: state(clock()), cooldown: 0, waited: (clock() - w.began) / 1000 });
  const arrive = (x) => {
    const q = x.query, t = clock();
    if (x.url.indexOf("/aaa/1/sync?") === 0) {
      if (Number(q.rev) !== g.rev) { reply(x, { together: state(t), cooldown: 0, waited: 0 }); return; }
      const w = { x, began: t };
      held.push(w);
      page.later(() => { if (held.indexOf(w) >= 0) { held = held.filter((h) => h !== w); answerWait(w); } }, 20000);
      return;
    }
    reply(x, { together: state(t), cooldown: 0, outcome: null });
  };
  const change = (fn) => {
    const t = clock();
    g.position = live(t); g.anchor = t;
    fn(g);
    g.rev += 1;
    const woken = held; held = [];
    woken.forEach(answerWait);
  };
  page = loadGroup({ paused: g.paused, position: g.position, duration: 600 }, {},
    { onSend: (x) => { if (page) { page.later(() => arrive(x), delay(o.up)); } else { early.push(x); } } });
  early.forEach((x) => page.later(() => arrive(x), delay(o.up)));
  const v = page.video;
  v.playbackRate = 1;
  let pos = 0, seeking = false, seekUntil = 0, seeks = 0, rates = 0, lastRate = 1, playing = false;
  Object.defineProperty(v, "currentTime", { get: () => pos, set: (t) => {
    pos = t;
    const from = (o.seekMsFrom || []).filter(([at]) => clock() - base >= at).pop();
    const ms = from ? from[1] : (o.seekMs || 0);
    if (playing) { seeks += 1; seeking = true; seekUntil = clock() + ms * (0.5 + rng()); }
  } });
  Object.defineProperty(v, "seeking", { get: () => seeking });
  let frames = 0, base = Infinity;
  const frame = () => {
    if (seeking && clock() >= seekUntil) { seeking = false; v.dispatch("seeked"); }
    const t = clock() - base;  // as `changes` count
    const stalled = (o.stalls || []).some(([at, ms]) => t >= at && t < at + ms);
    if (!seeking && !stalled && !v.paused) { pos += 0.05 * v.playbackRate; }
    if (v.playbackRate !== lastRate) { rates += 1; lastRate = v.playbackRate; }
    frames += 1;
    if (frames % 5 === 0) { v.dispatch("timeupdate"); }
    page.later(frame, 50);
  };
  // The page loads the video a moment after it opens, at the place the page was told.
  page.tick(o.loadMs || 1500);
  page.hls().emit("manifest");
  v.ready(600, page.hls().config.startPosition);
  playing = true;
  frame();
  const changes = (o.changes || []).slice().sort((a, b) => a[0] - b[0]);
  const presses = (o.presses || []).slice().sort((a, b) => a[0] - b[0]);
  const off = [];
  base = clock();
  for (let t = 0; t < o.seconds * 1000; t += 100) {
    while (changes.length && changes[0][0] <= t) { change(changes.shift()[1]); }
    while (presses.length && presses[0][0] <= t) { presses.shift()[1](page); }
    page.tick(100);
    await settle();
    off.push({ t: t + 100, off: pos - live(clock()), rate: v.playbackRate, paused: v.paused, locked: page.$("play").disabled,
      sync: !page.$("sync").hidden, seeks });
  }
  const open = off.find((s) => !s.locked);
  return { off, seeks, rates, unlocked: open ? open.t / 1000 : Infinity };
}

// The worst distance from the group from `from` ms on, and how long after `from` it took
// to come within `near` s and stay there.
function worst(run, from, to) {
  return Math.max(...run.off.filter((s) => s.t >= from && (to === undefined || s.t < to)).map((s) => Math.abs(s.off)));
}
function settledBy(run, from, near) {
  const after = run.off.filter((s) => s.t >= from);
  let last = from;
  after.forEach((s) => { if (Math.abs(s.off) >= near) { last = s.t; } });
  return (last - from) / 1000;
}

// --- the checks

const { check, finish } = runner();

(async () => {
  await check("it starts where the daemon says, with the tracks it says", async () => {
    const page = load({ start: 754.5, start_audio: 1, start_sub: 0 });
    eq(page.hls().config.startPosition, 754.5);
    eq(page.hls().url, page.S.master);
    page.hls().emit("manifest");
    eq(page.hls().audioTrack, 1);
    eq(text(page.$("audio")), "Audio: Japanese"); eq(text(page.$("subs")), "Subtitles: English");
    ok(page.xhrs.some((x) => x.url === "/aaa/1/hls/subs/1.vtt"), "the soft subtitles are fetched");
    const burned = load({ start: 30, start_sub: 1 });
    eq(burned.hls().url, "/aaa/1/hls/burn/3/master.m3u8", "a burned-in start loads that version");
    eq(load({ start: 0 }).hls().config.startPosition, -1, "0 is hls.js's own start");
  });

  await check("it reports every 2 s: its position only once the video is really there", async () => {
    const page = load({ start: 600 });
    eq(page.reports().length, 0);
    let q = exchange(page);
    ok(typeof q.pg === "string" && /^[a-z0-9]{1,16}$/.test(q.pg), "a page id the daemon takes: " + q.pg);
    delete q.pg;
    eq(q, { p: "1", ack: "0", a: "1", s: "off", z: "m" }, "no position while loading, no duration yet");
    page.video.ready(5400, 600);
    page.video.paused = false;
    q = exchange(page);
    eq([q.t, q.d, q.p], ["600.0", "5400.0", "0"]);
    page.video.duration = Infinity;  // a browser may say so before it knows
    ok(!("d" in exchange(page)), "no duration that is not a number");
  });

  await check("one report at a time; a lost or failed one does not stop the next", async () => {
    const page = load();
    page.tick(2000);
    eq(page.reports().length, 1);
    page.tick(4000);
    eq(page.reports().length, 1, "none while one is on its way");
    page.reports()[0].fail();
    page.tick(2000);
    eq(page.reports().length, 2);
    page.reports()[1].respond(500, "Something went wrong.");
    page.tick(2000);
    eq(page.reports().length, 3);
    page.reports()[2].respond(200, "not json");  // nothing breaks
    page.tick(2000);
    eq(page.reports().length, 4);
    page.reports()[3].onabort();
    page.tick(2000);
    eq(page.reports().length, 5);
  });

  await check("it does what the owner asks: audio, Off, subtitles and seek, then says it saw them", async () => {
    const page = load();
    page.hls().emit("manifest");
    page.video.ready(5400, 10);
    exchange(page, [{ seq: 1, audio: 2 }, { seq: 3, seek: 610 }, { seq: 2, sub: "1-soft" }]);
    eq(page.hls().audioTrack, 1); eq(text(page.$("audio")), "Audio: Japanese");
    eq(page.video.currentTime, 610);
    eq(text(page.$("subs")), "Subtitles: English");
    ok(page.xhrs.some((x) => x.url === "/aaa/1/hls/subs/1.vtt"));
    const q = exchange(page, [], 300);  // reported soon after, not 2 s later
    eq([q.ack, q.a, q.s, q.t], ["3", "2", "1-soft", "610.0"]);
    exchange(page, [{ seq: 4, audio: 0 }]);
    ok(page.video.muted, "Off mutes"); eq(text(page.$("audio")), "Audio: Off");
    exchange(page, [{ seq: 5, sub: "3-burn" }]);
    eq(page.hls().url, "/aaa/1/hls/burn/3/master.m3u8"); eq(page.hls().config.startPosition, 610);
    exchange(page, [{ seq: 6, sub: "off" }]);
    eq(page.hls().url, page.S.master, "Off goes back to the base version");
  });

  await check("a command it has seen, or one for a track this video lacks, changes nothing", async () => {
    const page = load({ ack: 7 });
    page.hls().emit("manifest");
    page.video.ready(100, 5);
    exchange(page, [{ seq: 7, audio: 2 }, { seq: 6, seek: 50 }]);
    eq(page.hls().audioTrack, 0, "the page itself carried commands up to 7");
    eq(page.video.currentTime, 5);
    exchange(page, [{ seq: 8, audio: 9 }, { seq: 9, sub: "2-soft" }]);
    eq(text(page.$("audio")), "Audio: English"); eq(text(page.$("subs")), "Subtitles: Off");
    eq(exchange(page, [], 300).ack, "9", "seen all the same");
    exchange(page, [{ seq: 9, seek: 80 }]);  // the same reply again: not applied twice
    eq(page.video.currentTime, 5);
  });

  await check("a press made while a report is out wins over the command that crosses it", async () => {
    const page = load();
    page.hls().emit("manifest");
    page.video.ready(5400, 100);
    page.tick(2000);
    const out = page.reports().pop();
    press(page, "audio");  // English -> Japanese, after the report left
    press(page, "forward");  // 100 -> 110
    out.respond(200, { commands: [{ seq: 1, audio: 0 }, { seq: 2, seek: 3000 }, { seq: 3, sub: "1-soft" }] });
    eq(text(page.$("audio")), "Audio: Japanese", "the press stands");
    eq(page.video.currentTime, 110, "and the viewer's seek");
    eq(text(page.$("subs")), "Subtitles: English", "a command for what was not pressed applies");
    const q = exchange(page, [], 300);
    eq([q.a, q.t, q.s, q.ack], ["2", "110.0", "1-soft", "3"]);
    // A command sent after the press was reported is obeyed.
    exchange(page, [{ seq: 4, audio: 1 }]);
    eq(text(page.$("audio")), "Audio: English");
  });

  await check("the viewer's own buttons still work, and each change is reported soon", async () => {
    const page = load();
    page.hls().emit("manifest");
    page.video.ready(5400, 100);
    press(page, "audio"); press(page, "audio");
    eq(text(page.$("audio")), "Audio: Off"); ok(page.video.muted);
    press(page, "audio");
    eq(text(page.$("audio")), "Audio: English"); ok(!page.video.muted);
    press(page, "subs"); eq(text(page.$("subs")), "Subtitles: English");
    press(page, "subs"); eq(page.hls().url, "/aaa/1/hls/burn/3/master.m3u8");
    press(page, "subs"); eq(text(page.$("subs")), "Subtitles: Off");
    page.video.readyState = 4;
    press(page, "back"); eq(page.video.currentTime, 90);
    page.$("bar").dispatch("click", { clientX: 500 });
    eq(page.video.currentTime, 2700);
    page.tick(300);
    eq(page.reports().length, 1, "one report for the presses, soon after");
    eq(page.reports()[0].query.s, "off");
  });

  await check("a report says when each button was last pressed, until a report with it arrives", async () => {
    const page = load();
    page.hls().emit("manifest");
    page.video.ready(5400, 100);
    page.tick(1000);
    press(page, "audio");
    page.tick(300);  // reported soon after
    let x = page.reports().pop();
    eq([x.query.pa, x.query.ps, x.query.pk], ["0.3", undefined, undefined]);
    eq(x.headers["X-Vpstream-Player"], "1", "the header that tells a report from a cross-site request");
    x.fail();  // lost: the next report (the 2 s one, at 2000) says it again, later
    page.tick(700);
    x = page.reports().pop();
    eq(x.query.pa, "1.0");
    x.respond(200, { commands: [] });
    ok(!("pa" in exchange(page)), "said once it arrived");
    press(page, "forward"); press(page, "subs");
    const q = exchange(page, [], 300);
    eq([q.pk, q.ps, q.pa], ["0.3", "0.3", undefined]);
    const id = q.pg;
    ok(id, "it has one"); eq(exchange(page).pg, id, "the same page id on every report");
    const other = load();
    ok(exchange(other).pg !== id, "another page has another id");
  });

  await check("-10 s and +10 s before the video is loaded do nothing, and do not count as a press", async () => {
    const page = load({ start: 600 }, { native: true });
    press(page, "back"); press(page, "forward");
    page.video.duration = 5400; page.video.readyState = 1;
    page.video.dispatch("loadedmetadata");
    page.tick(0);
    eq(page.video.currentTime, 600, "the resume position stands");
    ok(!("pk" in exchange(page)));
  });

  await check("a seek asked for before the video can take it waits for it, and beats the start position", async () => {
    const page = load({ start: 600 }, { native: true });
    eq(page.video.src, page.S.master);
    exchange(page, [{ seq: 1, seek: 1200 }]);
    eq(page.video.currentTime, 0, "nothing loaded yet");
    page.video.duration = 5400;
    page.video.readyState = 1;
    page.video.dispatch("loadedmetadata");
    eq(page.video.currentTime, 600, "the start position first");
    page.tick(0);
    eq(page.video.currentTime, 1200, "then the seek");
  });

  await check("Hide puts the controls away, even paused, until a click or a key", async () => {
    const page = load();
    const shown = () => page.document.body.className === "controls";
    page.video.pause();
    ok(shown(), "paused: shown");
    press(page, "hide");
    ok(!shown(), "hidden at once, though paused");
    page.document.listeners.mousemove.forEach((fn) => fn({}));  // the remote's pointer moves
    ok(!shown(), "a move does not bring them back");
    page.video.pause();  // nor does anything the page does by itself
    page.tick(10000);
    ok(!shown());
    page.$("video").click();  // a click anywhere does
    ok(shown(), "back on a click");
    press(page, "hide");
    page.document.listeners.keydown.forEach((fn) => fn({ key: "ArrowRight", preventDefault() {} }));
    ok(shown(), "back on a key");
    // While playing, they hide by themselves after a while, as before, and come back on a move.
    page.video.paused = false;
    page.document.listeners.mousemove.forEach((fn) => fn({}));
    page.tick(4000);
    ok(!shown(), "hidden by the timer");
    page.document.listeners.mousemove.forEach((fn) => fn({}));
    ok(shown(), "and back on a move");
  });

  await check("full screen: the button and the f key enter and leave it, the whole page", async () => {
    for (const api of ["standard", "webkit"]) {
      const page = load({}, { full: api });
      const now = () => page.document.fullscreenElement || page.document.webkitFullscreenElement;
      eq(page.$("full").hidden, false, api + ": a button");
      eq(text(page.$("full")), "Full screen");
      press(page, "full");
      ok(now() === page.root, api + ": the page, so the controls and subtitles stay");
      eq(text(page.$("full")), "Exit full screen");
      press(page, "full");
      ok(!now()); eq(text(page.$("full")), "Full screen");
      const key = (k, more) => page.document.listeners.keydown.forEach((fn) => fn(Object.assign({ key: k, preventDefault() {} }, more)));
      key("f"); ok(now(), api + ": f enters");
      key("F"); ok(!now(), api + ": and leaves (Shift too)");
      key("f", { metaKey: true }); key("f", { ctrlKey: true }); key("f", { altKey: true });
      ok(!now(), "not with Cmd, Ctrl or Alt: those are the browser's");
      // Leaving with Esc is the browser's own; the label follows.
      key("f");
      page.document.exitFullscreen ? page.document.exitFullscreen() : page.document.webkitExitFullscreen();
      eq(text(page.$("full")), "Full screen");
    }
  });

  await check("full screen: no button where the browser has none, and a refusal is said", async () => {
    const none = load({}, { full: "none" });
    eq(none.$("full").hidden, true);
    none.document.listeners.keydown.forEach((fn) => fn({ key: "f", preventDefault() {} }));  // nothing breaks
    const refused = load({}, { refuse: true });
    press(refused, "full");
    await settle();
    eq(text(refused.$("message")), "Full screen is not allowed here.");
  });

  await check("without a place to report it only plays", async () => {
    const page = load({ report: null });
    page.tick(10000);
    eq(page.reports().length, 0);
    eq(page.xhrs.length, 0);
  });

  await check("a video without audio tracks reports none and cannot be told one", async () => {
    const page = load({ audio: [], start_audio: -1 });
    eq(page.$("audio").hidden, true);
    ok(!("a" in exchange(page, [{ seq: 1, audio: 0 }])));
    ok(!page.video.muted, "Off is not taken for a video with nothing to mute");
  });


  // Over the internet: trips of 100-300 ms each way and back, uneven and jittery, and seeks
  // that take a while. The checks run each case with three seeds.
  const INTERNET = [
    ["100 ms", { up: 50, down: 50, jitter: 5, seekMs: 400 }],
    ["300 ms, jittery", { up: 150, down: 150, jitter: 100, seekMs: 700 }],
    ["300 ms, uneven", { up: 250, down: 50, jitter: 100, seekMs: 700 }],
  ];
  const SEEDS = [1, 2, 3];

  await check("together over the internet: it stays with the group, by its rate, without swinging", async () => {
    for (const [name, o] of INTERNET) {
      for (const seed of SEEDS) {
        const run = await netRun(Object.assign({ seconds: 60, seed }, o));
        ok(worst(run, 10000) < 0.3, `${name}, seed ${seed}: within 0.3 s (${worst(run, 10000)})`);
        ok(run.seeks <= 2 && run.rates <= 6, `${name}, seed ${seed}: ${run.seeks} seeks, ${run.rates} changes of rate`);
      }
    }
  });

  await check("together over the internet: a page that loads late is put at the group's place by a seek", async () => {
    for (const [name, o] of INTERNET) {
      for (const seed of SEEDS) {
        // It loads 1.5 s after it was told where the group is: behind, but in tolerance.
        // At its place it says so, and its cooldown starts (here the daemon gives none);
        // what is left, under JOINED_OFF, its rate makes up before the cooldown ends.
        const run = await netRun(Object.assign({ seconds: 25, seed, loadMs: 1500 }, o));
        ok(run.unlocked < 4, `${name}, seed ${seed}: at the group's place after ${run.unlocked} s`);
        ok(worst(run, run.unlocked * 1000) < 0.5, `${name}, seed ${seed}: and near it from then on (${worst(run, run.unlocked * 1000)}, ${run.unlocked})`);
        const near = settledBy(run, 0, 0.3);
        ok(near < run.unlocked + 10, `${name}, seed ${seed}: within 0.3 s after ${near} s`);
      }
    }
  });

  await check("together over the internet: after the group's seek it lands where the group is by then", async () => {
    for (const [name, o] of INTERNET) {
      for (const seed of SEEDS) {
        const changes = [[10000, (g) => { g.position = 300; }], [30000, (g) => { g.position = 100; }]];
        const run = await netRun(Object.assign({ seconds: 50, seed, changes }, o));
        // The first seek tells it what a seek takes; the next aims ahead by that.
        ok(settledBy(run, 30000, 0.3) < 2, `${name}, seed ${seed}: near after ${settledBy(run, 30000, 0.3)} s`);
        ok(worst(run, 40000) < 0.3, `${name}, seed ${seed}: and stays (${worst(run, 40000)})`);
      }
    }
  });

  await check("together over the internet: a seek that takes longer than the tolerance is not made again and again", async () => {
    for (const seed of SEEDS) {
      const changes = [[10000, (g) => { g.position = 300; }], [30000, (g) => { g.position = 100; }]];
      const run = await netRun({ seconds: 60, seed, up: 150, down: 150, jitter: 100, seekMs: 2500, changes });
      ok(run.seeks <= 5, `seed ${seed}: ${run.seeks} seeks`);
      ok(worst(run, 55000) < 0.3, `seed ${seed}: with the group in the end (${worst(run, 55000)})`);
    }
  });

  await check("together over the internet: a pause and a play reach it on time", async () => {
    for (const [name, o] of INTERNET) {
      const changes = [[10000, (g) => { g.paused = true; }], [15000, (g) => { g.paused = false; }]];
      const run = await netRun(Object.assign({ seconds: 30, changes }, o));
      ok(run.off.filter((x) => x.t > 11000 && x.t < 15000).every((x) => x.paused), `${name}: paused`);
      ok(worst(run, 16000) < 0.3, `${name}: and on with the group (${worst(run, 16000)})`);
    }
  });

  await check("together over the internet: a video that waits for data plays on out of sync, unseeked, until Sync", async () => {
    for (const [name, o] of INTERNET) {
      for (const seed of SEEDS) {
        // A link slower than the video: it stops for 6 s at 20 s. Seeking then only threw
        // away what it had and stopped it again (the VPS hand test, 2026-11-26).
        const sync = (page) => press(page, "sync");
        const run = await netRun(Object.assign({ seconds: 50, seed, stalls: [[20000, 6000]], presses: [[35000, sync]] }, o));
        const at = (t) => run.off.find((x) => x.t >= t);
        ok(!at(19000).sync && at(30000).sync, `${name}, seed ${seed}: Sync shows once it is out of sync`);
        ok(Math.abs(at(30000).off) > 5, `${name}, seed ${seed}: left where it is (${at(30000).off})`);
        eq(at(34900).seeks, at(19000).seeks, `${name}, seed ${seed}: no seek of its own`);
        ok(!at(30000).paused, `${name}, seed ${seed}: it plays on`);
        // A seek takes half to one and a half of seekMs: where it lands, its rate does the rest.
        ok(settledBy(run, 35000, 1) < 3, `${name}, seed ${seed}: Sync puts it back (${settledBy(run, 35000, 1)} s)`);
        ok(worst(run, 45000) < 0.31 && !at(40000).sync, `${name}, seed ${seed}: and near (${worst(run, 45000)})`);
      }
    }
  });

  await check("together over the internet: a seek that lands early waits there for the group", async () => {
    for (const seed of SEEDS) {
      // Sync after a slow seek aims far ahead; this seek is quick, and lands early: it
      // was left ahead out of sync (the VPS hand test, 2026-11-26, 3.1 s ahead).
      const sync = (page) => press(page, "sync");
      const run = await netRun({ seconds: 70, seed, up: 100, down: 100, jitter: 20, seekMs: 4000,
        seekMsFrom: [[45000, 200]], stalls: [[10000, 8000], [35000, 8000]], presses: [[25000, sync], [50000, sync]] });
      ok(settledBy(run, 50000, 0.3) < 6, `seed ${seed}: with the group after ${settledBy(run, 50000, 0.3)} s`);
      ok(!run.off.find((x) => x.t >= 60000).sync, `seed ${seed}: in sync`);
    }
  });

  await check("together: it starts paused where the group is, and waits for it to change", async () => {
    const page = loadGroup({ position: 120, rev: 4 });
    eq(page.hls().config.startPosition, 120);
    page.hls().emit("manifest");
    eq(page.video.played, 0, "not played: the group is paused");
    const w = waits(page);
    eq(w.length, 1);
    eq([w[0].query.rev, w[0].headers["X-Vpstream-Player"]], ["4", "1"]);
    w[0].respond(200, { together: groupState({ rev: 5, paused: false, position: 121 }), waited: 3 });
    eq(waits(page).length, 1, "the next wait goes out at once (a timer of 0)");
    page.tick(0);
    eq(waits(page).length, 2);
    eq(waits(page)[1].query.rev, "5");
    waits(page)[1].respond(200, { together: groupState({ rev: 5, paused: false }), waited: 0 });
    page.tick(0);
    eq(waits(page).length, 2, "not held and nothing new: it waits a little first");
    page.tick(2000);
    eq(waits(page).length, 3);
    waits(page)[2].fail();
    page.tick(2000);
    eq(waits(page).length, 4, "and again after a failure");
  });

  await check("together: reports only say where it is; only a press carries an act", async () => {
    const page = loadGroup();
    inGroup(page);
    let q = exchange(page, [], 2000);
    ok(!("act" in q) && !("pk" in q) && !("pz" in q), "status only: " + JSON.stringify(q));
    eq([q.p, q.rev, q.in, q.t, q.a, q.s], ["1", "1", "1", "0.0", "1", "off"]);
    press(page, "play");
    q = answer(page, { rev: 2, paused: false });
    eq([q.act, q.id.indexOf(q.pg + "-")], ["play", 0]);
    await settle();
    ok(!page.video.paused, "it follows the group, which plays now");
    press(page, "play");
    eq(answer(page, { rev: 3, paused: true }).act, "pause", "Play reads Pause while the group plays");
    ok(page.video.paused);
  });

  await check("together: a report says how far the page is off the group, as it measures it", async () => {
    const page = loadGroup({ paused: false, position: 100 });
    inGroup(page, { paused: false, position: 100 }, 100);
    await settle();
    page.video.currentTime = 101.5;
    const w = open(waits(page)).pop();
    w.respond(200, { together: groupState({ paused: false, position: 100.6 }), waited: 1e6 });  // no trip
    const told = page.now();
    let x = null;
    while (!x) { page.tick(100); x = open(page.reports()).pop(); }
    // The group moved on from 100.6 since it was told; the page is where it was put.
    // (It left within the last 100 ms.)
    const off = 101.5 - (100.6 + (page.now() - told) / 1000);
    ok(x.query.o !== undefined && Math.abs(Number(x.query.o) - off) <= 0.1, `${x.query.o}, not ${off}`);
  });

  await check("together: a pause the browser makes by itself goes nowhere", async () => {
    const page = loadGroup({ paused: false, position: 10 });
    inGroup(page, { paused: false, position: 10 }, 10);
    await settle();
    ok(!page.video.paused, "it plays with the group");
    page.video.pause();  // the headphones came out
    for (let i = 0; i < 3; i++) { ok(!("act" in exchange(page, [], 2000)), "nothing for the group"); }
    eq(page.$("join").hidden, false, "this page waits for a tap");
    eq(page.video.played, 1, "and does not start again by itself");
    press(page, "join");
    await settle();
    ok(!page.video.paused);
    ok(!("act" in exchange(page, [], 2000)), "the tap sends nothing either");
    eq(page.$("join").hidden, true);
  });

  await check("together: a page that just joined cannot press anything but Full screen and Hide", async () => {
    const page = loadGroup();
    page.hls().emit("manifest");
    const space = () => page.document.listeners.keydown.forEach((fn) => fn({ key: " ", preventDefault() {} }));
    const media = () => page.document.listeners.keydown.forEach((fn) => fn({ key: "MediaPlayPause", preventDefault() {} }));
    const all = () => {
      ["play", "back", "forward", "audio", "subs"].forEach((id) => press(page, id));
      space(); media();
      page.$("bar").dispatch("click", { clientX: 500 });
    };
    page.tick(250);
    eq(text(page.$("note")), "Joining the group...");
    all();
    ok(page.$("play").disabled && page.$("bar") && !page.$("full").disabled && !page.$("hide").disabled);
    page.video.ready(600, 0);
    page.tick(550);
    answer(page, {}, { cooldown: 10 });
    eq(text(page.$("note")), "Watching together: the controls work in 10 s");
    all();
    for (let i = 0; i < 4; i++) { ok(!("act" in exchange(page, [], 2000)), "nothing goes out"); }
    ok(/^Watching together: the controls work in [23] s$/.test(text(page.$("note"))), "it counts down");
    press(page, "full");
    ok(page.document.fullscreenElement, "Full screen works");
    page.tick(3000);
    eq(text(page.$("note")), "");
    ok(!page.$("play").disabled);
    open(page.reports()).forEach((x) => x.respond(200, { together: groupState(), cooldown: 0 }));
    page.document.activeElement = null;  // Space on a button is the browser's own click
    space();
    eq(answer(page, { rev: 2, paused: false }).act, "play", "and now Space plays for everyone");
  });

  await check("together: a pause pressed here pauses this page at once, while the press is on its way", async () => {
    const page = loadGroup({ paused: false, position: 100 });
    inGroup(page, { paused: false, position: 100 }, 100);
    await settle();
    page.tick(1000);
    ok(!page.video.paused);
    press(page, "play");
    ok(page.video.paused, "paused here at once: its answer may be slow to come back");
    eq(text(page.$("play")), "Play");
    const x = open(page.reports()).pop();
    eq(x.query.act, "pause");
    page.tick(5000);
    ok(page.video.paused, "and it stays paused while the press is on its way");
    x.respond(200, { together: groupState({ rev: 2, paused: true, position: 101 }), cooldown: 0, outcome: "applied" });
    page.tick(1000);
    ok(page.video.paused, "the group paused too");
    // A press of pause that was not done: the group plays on, and so does this page.
    open(waits(page)).forEach((w) => w.respond(200, { together: groupState({ rev: 3, paused: false, position: 101 }), waited: 1e6 }));
    page.tick(1000);
    await settle();
    ok(!page.video.paused, "playing with the group again");
    press(page, "play");
    ok(page.video.paused);
    let q;
    do { q = answer(page, { rev: 3, paused: false, position: 102 }, { outcome: "refused" }); } while (!q.act);
    eq(q.act, "pause");
    page.tick(1000);
    await settle();
    ok(!page.video.paused, "refused: it plays on with the group");
  });

  await check("together: three quick -10 s taps are three steps the daemon stacks", async () => {
    const page = loadGroup({ position: 300 });
    inGroup(page, { position: 300 }, 300);
    press(page, "back");
    press(page, "back");  // while the first is out: they go one after another
    press(page, "back");
    const steps = [];
    for (let i = 0; i < 3; i++) {
      const q = answer(page, { position: 300 }, { outcome: i ? "queued" : "applied" });
      steps.push([q.act, q.v, q.id]);
    }
    eq(steps.map((s) => s.slice(0, 2)), [["skip", "-10"], ["skip", "-10"], ["skip", "-10"]]);
    eq(new Set(steps.map((s) => s[2])).size, 3, "each with an id of its own");
    eq(text(page.$("note")), "Seeking for everyone...", "a press that waits for the window says so");
    eq(page.video.currentTime, 300, "nothing moves here until the group does");
    page.tick(2000);
    const w = open(waits(page)).pop();
    w.respond(200, { together: groupState({ rev: 2, position: 270 }), waited: 1.5 });
    eq(page.video.currentTime, 270);
    eq(text(page.$("note")), "");
  });

  await check("together: a press that failed goes again with the same id", async () => {
    const page = loadGroup();
    inGroup(page);
    press(page, "forward");
    const first = open(page.reports()).pop();
    const id = first.query.id;
    first.fail();
    page.tick(2000);
    eq(answer(page).id, id);
    ok(!("act" in exchange(page, [], 2000)), "done once it was answered");
  });

  // The group is at `position` and this page at `current`, told by an answer that took no
  // time on the way (the daemon held it longer than it was out).
  const at = (page, position, current) => {
    page.video.currentTime = current;
    open(waits(page)).pop().respond(200, { together: groupState({ paused: false, position }), waited: 1e6 });
    page.tick(0);  // the next wait goes out
  };

  await check("together: it keeps close by its rate; far off by itself it is out of sync until Sync", async () => {
    const page = loadGroup({ paused: false, position: 100 });
    inGroup(page, { paused: false, position: 100 }, 100);
    await settle();
    page.tick(1000);
    eq(page.$("sync").hidden, true, "no Sync while it is with the group");
    at(page, 100, 103.5);
    eq([page.video.currentTime, page.video.playbackRate], [103.5, 1], "3.5 s ahead: no seek");
    eq(page.$("sync").hidden, false, "but Sync");
    press(page, "sync");
    eq(page.video.currentTime, 100, "Sync goes back to the group's place");
    eq(page.$("sync").hidden, true);
    ok(!("act" in exchange(page, [], 2000)), "nothing went to the group");
    page.tick(1000);
    at(page, 100, 101);
    eq([page.video.currentTime, page.video.playbackRate], [101, 0.95], "1 s ahead: a little slower");
    at(page, 100, 98.5);
    eq(page.video.playbackRate, 1.05, "behind: a little faster");
    at(page, 100, 100.25);
    eq(page.video.playbackRate, 1.05, "on until it is under 0.2 s");
    at(page, 100, 100.1);
    eq(page.video.playbackRate, 1, "and back to normal");
    at(page, 100, 100.25);
    eq(page.video.playbackRate, 1, "0.25 s off is near enough");
    page.tick(1000);
    at(page, 100, 98);
    eq([page.video.currentTime, page.$("sync").hidden], [98, true], "2 s behind is in tolerance");
    at(page, 100, 97.9);
    eq([page.video.currentTime, page.video.playbackRate, page.$("sync").hidden], [97.9, 1, false], "more is out of sync");
    press(page, "sync");
    eq(page.video.currentTime, 100, "Sync goes ahead to it too");
  });

  await check("together: a change of the group's still moves it, ahead or back, by a seek", async () => {
    const page = loadGroup({ paused: false, position: 100 });
    inGroup(page, { paused: false, position: 100 }, 100);
    await settle();
    page.tick(1000);
    const change = (rev, position) => {
      open(waits(page)).pop().respond(200, { together: groupState({ rev, paused: false, position }), waited: 1e6 });
      page.tick(0);
    };
    change(2, 300);
    eq(page.video.currentTime, 300, "someone's seek ahead");
    page.tick(1000);
    change(3, 50);
    eq(page.video.currentTime, 50, "someone's seek back");
    page.tick(1000);
    page.video.currentTime = 40;  // fell behind by itself, out of sync...
    open(waits(page)).pop().respond(200, { together: groupState({ rev: 3, paused: false, position: 50 }), waited: 1e6 });
    page.tick(250);
    eq([page.video.currentTime, page.$("sync").hidden], [40, false]);
    change(4, 50);  // ...and someone pressed something: the group's change puts it back
    eq([page.video.currentTime, page.$("sync").hidden], [50, true]);
  });

  await check("together: Sync works in the cooldown, and a press refused for it says so", async () => {
    const page = loadGroup({ paused: false, position: 100 });
    page.hls().emit("manifest");
    page.video.ready(600, 100);
    page.tick(550);
    answer(page, { paused: false, position: 100 }, { cooldown: 10 });
    ok(page.$("play").disabled, "in its cooldown");
    page.tick(1000);
    at(page, 100, 95);
    ok(!page.$("sync").hidden && !page.$("sync").disabled, "Sync shows and works");
    press(page, "sync");
    eq(page.video.currentTime, 100);
    page.tick(10000);
    open(page.reports()).forEach((x) => x.respond(200, { together: groupState({ paused: false, position: 110 }), cooldown: 0 }));
    ok(!page.$("play").disabled);
    // Away longer than together.PAGE_SECONDS, the page has joined again: the daemon says so.
    press(page, "play");
    answer(page, { paused: false, position: 110 }, { outcome: "cooldown", cooldown: 9.8 });
    eq(text(page.$("note")), "Not done: you joined again, the controls work in a few seconds.");
  });

  await check("together: half the trip of an answer is allowed for", async () => {
    const page = loadGroup({ paused: false, position: 100 });
    inGroup(page, { paused: false, position: 100 }, 100);
    await settle();
    at(page, 100, 100);
    page.tick(1000);  // the wait that went out is answered 1 s later, held 0.2 s of it
    page.video.currentTime = 102.4;
    open(waits(page)).pop().respond(200, { together: groupState({ paused: false, position: 102 }), waited: 0.2 });
    // The group was at 102 0.4 s ago: at 102.4 now, where this page is.
    eq(page.video.playbackRate, 1);
  });

  await check("together: where the rate cannot change it keeps up by seeks only", async () => {
    const page = loadGroup({ paused: false, position: 100 });
    Object.defineProperty(page.video, "playbackRate", { get: () => 1, set: () => {} });
    inGroup(page, { paused: false, position: 100 }, 100);
    await settle();
    page.tick(1000);
    at(page, 100, 100.6);
    eq(page.video.currentTime, 100.6, "0.6 s: no seek");
    page.tick(1000);
    at(page, 100, 101.5);
    eq(page.video.currentTime, 100, "1.5 s: a seek, where the rate would have done");
  });

  await check("together: a browser that will not play without a tap says so, and the tap works in the cooldown", async () => {
    const page = loadGroup({ paused: false, position: 50 });
    page.video.play = function () { return Promise.reject(new Error("autoplay")); };
    page.hls().emit("manifest");
    page.video.ready(600, 50);
    page.tick(250);
    await settle();
    page.tick(250);
    eq(page.$("join").hidden, false);
    eq(text(page.$("join")), "Tap to join playback");
    page.video.play = function () { this.paused = false; this.played += 1; this.dispatch("play"); return Promise.resolve(); };
    press(page, "join");
    await settle();
    ok(!page.video.paused, "this page plays");
    page.tick(250);
    eq(page.$("join").hidden, true);
    ok(!("act" in exchange(page, [], 2000)), "nothing went to the group");
  });

  await check("together: a new rev is followed: pause, audio, subtitles, and another video", async () => {
    const page = loadGroup({ paused: false, position: 10 });
    inGroup(page, { paused: false, position: 10 }, 10);
    await settle();
    const rev = (state) => {
      open(waits(page)).pop().respond(200, { together: groupState(state), waited: 1 });
      page.tick(0);
    };
    rev({ rev: 2, paused: true, position: 12 });
    ok(page.video.paused, "paused with the group");
    eq(page.video.currentTime, 12);
    eq(text(page.$("play")), "Play");
    rev({ rev: 3, paused: true, position: 12, audio: 2 });
    eq(text(page.$("audio")), "Audio: Japanese");
    rev({ rev: 4, paused: true, position: 12, audio: 0, sub: "1-soft" });
    ok(page.video.muted); eq(text(page.$("subs")), "Subtitles: English");
    rev({ rev: 5, paused: true, position: 12, audio: 0, sub: "3-burn" });
    eq(page.hls().url, "/aaa/1/hls/burn/3/master.m3u8", "burned in: the other version");
    page.video.ready(600, 12);
    page.tick(250);
    eq(page.video.currentTime, 12, "and at the group's place after");
    const old = groupState({ rev: 2, paused: false });
    old.now = 1;
    open(waits(page)).pop().respond(200, { together: old, waited: 1 });
    ok(page.video.paused, "an answer older than the last one is dropped");
    page.tick(0);
    rev({ rev: 6, video: 2 });
    eq(page.window.location.href, "/aaa/2", "another video: the page of it");
  });

  await check("together: Next and Episodes choose a video for everyone", async () => {
    const names = ["One", "Two", "Three"];
    const page = load({ folder: true, count: 3, next: "/aaa/2", together: { state: groupState(), sync: "/aaa/1/sync", cooldown: 10, names } });
    inGroup(page);
    press(page, "next");
    eq([answer(page).act, page.window.location.href], ["video", ""], "it asks; it does not go");
    eq(page.reports().pop().query.v, "2");
    press(page, "episodes");
    eq(page.$("list").hidden, false);
    const choices = page.$("list").querySelectorAll("button");
    eq(choices.length, 4);
    choices[2].click();
    eq(page.$("list").hidden, true);
    const q = answer(page, {}, { outcome: "queued" });
    eq([q.act, q.v], ["video", "3"]);
    eq(text(page.$("note")), "Changing the video for everyone...");
  });


  await check("together: a new rev is followed even when the daemon's clock was set back", async () => {
    const page = loadGroup({ rev: 3 });
    inGroup(page, { rev: 3 });
    const back = groupState({ rev: 4, paused: false, position: 0 });
    back.now -= 30;  // the clock was stepped back
    open(waits(page)).pop().respond(200, { together: back, waited: 1 });
    page.tick(250);
    eq([page.video.paused, text(page.$("play"))], [false, "Pause"], "it plays with the group");
    const same = groupState({ rev: 4, paused: false, position: 0 });
    same.now = back.now - 1;  // the same rev, told before: dropped
    same.paused = true;
    open(waits(page)).pop().respond(200, { together: same, waited: 1 });
    eq(page.video.paused, false, "an older answer of the same rev is dropped");
    const old = groupState({ rev: 3, paused: true });
    old.now += 1000;
    page.tick(0);
    open(waits(page)).pop().respond(200, { together: old, waited: 1 });
    eq(page.video.paused, false, "an older rev is dropped, whatever its clock");
  });

  await check("together: a press the daemon refuses says so", async () => {
    const page = load({ folder: true, count: 3, next: "/aaa/2", together: { state: groupState(), sync: "/aaa/1/sync", cooldown: 10, names: ["a", "b", "c"] } });
    inGroup(page);
    press(page, "next");
    answer(page, {}, { outcome: "refused" });
    eq(text(page.$("note")), "That cannot be done for everyone right now.");
    page.tick(4000);
    eq(text(page.$("note")), "", "and then it goes");
  });

  await check("subtitle size: a press goes to the next size, drawn by the video's height", async () => {
    const page = load({ start_sub: 0 });
    eq(page.$("size").hidden, false, "shown while soft subtitles are on");
    eq(text(page.$("size")), "Subtitle size: Medium");
    eq(page.$("overlay").style.fontSize, "18px", "4.5% of a 400 px high video");
    press(page, "size");
    eq(text(page.$("size")), "Subtitle size: Large");
    eq(page.$("overlay").style.fontSize, "22px");
    press(page, "size");
    press(page, "size");
    eq(text(page.$("size")), "Subtitle size: Extra small", "after the largest, the smallest");
    eq(page.$("overlay").style.fontSize, "12px", "3% of 400 px");
    const q = exchange(page, [], 300);
    eq(q.z, "xs");
    ok("pz" in q, "the press is reported");
    page.window.innerWidth = 1920;
    page.window.innerHeight = 1080;
    press(page, "size");
    page.window.listeners.resize.forEach((fn) => fn());
    eq(page.$("overlay").style.fontSize, "41px", "a TV: 3.75% of 1080 px");
    page.window.innerHeight = 2000;  // a page taller than the video: its black bars do not count
    page.window.listeners.resize.forEach((fn) => fn());
    eq(page.$("overlay").style.fontSize, "41px");
  });

  await check("subtitle size: a phone held upright counts half its height (at most its width), so each size differs", async () => {
    const page = load({ start_sub: 0 });
    page.window.innerWidth = 412;
    page.window.innerHeight = 915;  // the video is 232 px high: half the window, 458, is more; its width less
    page.window.listeners.resize.forEach((fn) => fn());
    const seen = [];
    for (let i = 0; i < 5; i++) {
      seen.push(page.$("overlay").style.fontSize);
      press(page, "size");
    }
    eq(seen, ["19px", "23px", "29px", "12px", "15px"], "medium, large, extra large, then the smallest");
    page.window.innerWidth = 400;
    page.window.innerHeight = 2000;  // very narrow: its width is the floor
    page.window.listeners.resize.forEach((fn) => fn());
    eq(page.$("overlay").style.fontSize, "18px", "medium again: 4.5% of 400 px");
  });

  await check("subtitle size: the button is there only while soft subtitles are on", async () => {
    const page = load();
    eq(page.$("size").hidden, true, "subtitles off");
    press(page, "subs");
    eq([text(page.$("subs")), page.$("size").hidden], ["Subtitles: English", false]);
    press(page, "subs");
    eq([text(page.$("subs")), page.$("size").hidden], ["Subtitles: Signs (burned in)", true], "burned in: drawn into the video");
    exchange(page, [{ seq: 1, sub: "1-soft" }]);
    eq(page.$("size").hidden, false, "and when the owner turns soft ones on");
    page.$("size").focus();
    exchange(page, [{ seq: 2, sub: "3-burn" }]);
    eq([page.$("size").hidden, page.document.activeElement === page.$("subs")], [true, true], "a remote on it goes to Subtitles");
    const burned = [{ id: "3-burn", label: "Signs", number: 3, kind: "burn", url: "/aaa/1/hls/burn/3/master.m3u8" }];
    eq(load({ subs: burned, start_sub: 0 }).$("size").hidden, true, "only burned-in subtitles");
    eq(load({ subs: [], start_sub: -1 }).$("size").hidden, true, "no subtitles");
  });

  await check("subtitle size: the page starts with this browser's, and the owner changes it", async () => {
    const page = load({ start_sub_size: "xl" });
    eq(text(page.$("size")), "Subtitle size: Extra large");
    exchange(page, [{ seq: 1, sub_size: "s" }]);
    eq(text(page.$("size")), "Subtitle size: Small");
    eq(load({ start_sub_size: "huge" }).$("overlay").style.fontSize, "18px", "one it does not know: medium");
    page.tick(2000);  // a report goes out...
    press(page, "size");  // ...the viewer presses: that wins over a command that crosses it
    open(page.reports()).pop().respond(200, { commands: [{ seq: 2, sub_size: "xs" }] });
    eq(text(page.$("size")), "Subtitle size: Medium");
  });

  await check("together: the subtitle size is this page's own, and works in the cooldown", async () => {
    const page = loadGroup();
    page.hls().emit("manifest");
    page.video.ready(600, 0);
    eq(page.$("audio").disabled, true, "in its cooldown");
    eq(page.$("size").disabled, false);
    press(page, "size");
    eq(text(page.$("size")), "Subtitle size: Large", "at once, here only");
    page.tick(600);  // it is at the group's place too: one report says both
    let q = answer(page, {}, { cooldown: 10 });
    ok(!("act" in q), "nothing for the group");
    eq([q.z, "pz" in q], ["l", true]);
    page.tick(2000);
    q = answer(page, {}, { commands: [{ seq: 4, sub_size: "xs" }] });
    eq(text(page.$("size")), "Subtitle size: Extra small", "the owner's command for this browser");
    page.tick(2000);
    eq(answer(page, {}).ack, "4", "it says it saw it");
  });

  await check("together: a wait that was aborted goes again", async () => {
    const page = loadGroup();
    eq(waits(page).length, 1);
    waits(page)[0].onabort();
    page.tick(2000);
    eq(waits(page).length, 2);
  });

  finish();
})();
