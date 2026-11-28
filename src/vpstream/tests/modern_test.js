// Runs the real page and scripts of the new player (modern.html, vtt.js, modern.js) in node,
// as player_test.js does the old one: the small DOM of dom.js, a stand-in hls.js, a video
// element that does what it is told, requests that the checks answer themselves and a clock
// they move. The harness is player_test.js's, with touches, a pointer, this browser's
// storage, an iPhone's full screen and a screen that turns. Usage:
// node modern_test.js PAGE VTT SYNC PLAYER
"use strict";
const fs = require("fs");
const vm = require("vm");
const { Node, parse, newDocument, settle, runner, eq, ok, text } = require("./dom.js");

const [pagePath, vttPath, syncPath, playerPath] = process.argv.slice(2);
const PAGE = fs.readFileSync(pagePath, "utf8");
const VTT = fs.readFileSync(vttPath, "utf8");
const SYNC = fs.readFileSync(syncPath, "utf8");
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
    location: { href: "", reloads: 0, reload() { this.reloads += 1; } },
    localStorage: options && options.storage,
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
    // A browser sends the pause event later, not from pause().
    pause() { this.paused = true; Promise.resolve().then(() => this.dispatch("pause")); },
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
  // An iPhone: no full screen of the page, only of the video, in the system's own player.
  if (fullApi === "iphone") {
    video.webkitEnterFullscreen = function () {
      if (this.readyState < 1) { throw new Error("not loaded"); }
      this.webkitDisplayingFullscreen = true;
      this.dispatch("webkitbeginfullscreen");
    };
    video.leaveFull = function () { this.webkitDisplayingFullscreen = false; this.dispatch("webkitendfullscreen"); };
  }
  const fire = (name) => (document.listeners[name] || []).forEach((fn) => fn({}));
  if (fullApi !== "none" && fullApi !== "iphone") {
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
  document.getElementById("bar").getBoundingClientRect = () => ({ left: 0, width: 1000, top: 0, height: 36 });

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
  if (options && options.pointer) { window.PointerEvent = function () {}; }
  if (options && options.touch) { window.ontouchstart = null; }
  if (options && options.screen) { window.screen = options.screen; }

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
  vm.runInContext(SYNC, context);
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
    audio: null, sub: null, duration: g.duration, window: 0, changed_by: "", changed_ago: null, now: serverNow + t / 1000 });
  const delay = (base) => Math.max(1, base + (rng() * 2 - 1) * (o.jitter || 0));
  let held = [];
  const early = [];  // sent while the page loads
  const reply = (x, body) => page.later(() => { if (x.status === undefined) { x.respond(200, body); } }, delay(o.down));
  const alone = !!o.alone;  // the only page: the daemon takes the group where it is
  const answerWait = (w) => reply(w.x, { together: state(clock()), cooldown: 0, alone, waited: (clock() - w.began) / 1000 });
  const arrive = (x) => {
    const q = x.query, t = clock();
    if (x.url.indexOf("/aaa/1/sync?") === 0) {
      if (Number(q.rev) !== g.rev) { reply(x, { together: state(t), cooldown: 0, alone, waited: 0 }); return; }
      const w = { x, began: t };
      held.push(w);
      page.later(() => { if (held.indexOf(w) >= 0) { held = held.filter((h) => h !== w); answerWait(w); } }, 20000);
      return;
    }
    // As together.Room.report: alone, joined, of this rev, the group playing.
    if (alone && q.o !== undefined && q.in === "1" && Number(q.rev) === g.rev && !g.paused && Math.abs(Number(q.o)) > 0.5) {
      g.position = live(t) + Number(q.o); g.anchor = t;
    }
    reply(x, { together: state(t), cooldown: 0, alone, outcome: null });
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
      sync: !page.$("sync").hidden, join: !page.$("join").hidden, controls: shown(page), seeks });
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
// --- the new player's own helpers

// A tap on the video at x (and y), as a finger or a click gives it.
const tapAt = (page, x, y) => page.$("taps").dispatch("click", { clientX: x, clientY: y === undefined ? 200 : y });
const shown = (page) => page.root.querySelector("body").classes.includes("controls");
const bodyHas = (page, cls) => page.root.querySelector("body").classes.includes(cls);
const isOn = (page, id) => page.$(id).classes.includes("on");
function storage(data) {
  return { data: Object.assign({}, data || {}), getItem(k) { return k in this.data ? this.data[k] : null; },
    setItem(k, v) { this.data[k] = String(v); }, removeItem(k) { delete this.data[k]; } };
}
function orientationOf(refuse) {
  const o = { locks: [], unlocks: 0, lock(kind) { this.locks.push(kind); return refuse ? Promise.reject(new Error("NotSupportedError")) : Promise.resolve(); },
    unlock() { this.unlocks += 1; } };
  return { orientation: o };
}
const key = (page, k) => page.document.listeners.keydown.forEach((fn) => fn({ key: k, preventDefault() {}, target: null }));

const INTERNET = [
  ["100 ms", { up: 50, down: 50, jitter: 5, seekMs: 400 }],
  ["300 ms, jittery", { up: 150, down: 150, jitter: 100, seekMs: 700 }],
  ["300 ms, uneven", { up: 250, down: 50, jitter: 100, seekMs: 700 }],
];
const SEEDS = [1, 2, 3];

// --- the checks

const { check, finish } = runner();

(async () => {
  await check("it starts where the daemon says, with the tracks it says, and the title over the bar", async () => {
    const page = load({ start: 754.5, start_audio: 1, start_sub: 0, title: "Big Film" });
    eq(page.hls().config.startPosition, 754.5);
    page.hls().emit("manifest");
    eq(page.hls().audioTrack, 1);
    ok(isOn(page, "opt-audio-1") && !isOn(page, "opt-audio-0"), "the sheet has Japanese on");
    ok(isOn(page, "opt-sub-1-soft"), "and the English subtitles");
    ok(isOn(page, "cc"), "the subtitles button says they are on");
    ok(page.xhrs.some((x) => x.url === "/aaa/1/hls/subs/1.vtt"), "the soft subtitles are fetched");
    eq(text(page.$("title")), "Big Film");
    ok(shown(page), "the controls show at first");
    eq(load({ start: 30, start_sub: 1 }).hls().url, "/aaa/1/hls/burn/3/master.m3u8", "a burned-in start loads that version");
    eq(load({ folder: true, number: 2, title: "Two" }).$("title").textContent, "2. Two");
  });

  await check("a tap in the middle plays and pauses; one at the side shows and hides the controls", async () => {
    const page = load();
    page.hls().emit("manifest");
    page.video.ready(600, 0);
    await settle();
    ok(!page.video.paused, "it starts by itself");
    tapAt(page, 500);
    await settle();
    ok(page.video.paused, "paused by a tap in the middle");
    ok(bodyHas(page, "controls") && !bodyHas(page, "playing"));
    page.tick(400);
    tapAt(page, 500);
    await settle();
    ok(!page.video.paused, "and plays again");
    page.tick(400);
    tapAt(page, 100);
    ok(shown(page), "a tap at the side waits: it may be a double tap");
    page.tick(350);
    ok(!shown(page), "then hides the controls");
    tapAt(page, 900);
    page.tick(350);
    ok(shown(page), "and the next shows them");
    page.tick(4000);
    ok(!shown(page), "they go away by themselves while it plays");
    page.video.pause();
    await settle();
    page.tick(5000);
    ok(shown(page), "and stay while it is paused");
  });

  await check("a double tap at the side steps 10 s, and each tap after it 10 s more", async () => {
    const page = load();
    page.hls().emit("manifest");
    page.video.ready(600, 300);
    tapAt(page, 100); page.tick(100); tapAt(page, 100);
    eq(page.video.currentTime, 290);
    eq(text(page.$("rippleback")), "−10 s");
    ok(page.$("rippleback").classes.includes("show"));
    page.tick(300); tapAt(page, 100);
    page.tick(300); tapAt(page, 100);
    eq(page.video.currentTime, 270, "two more taps, two more steps");
    eq(text(page.$("rippleback")), "−30 s");
    page.tick(1000);
    ok(!page.$("rippleback").classes.includes("show"), "the sign goes");
    tapAt(page, 900); page.tick(100); tapAt(page, 900);
    eq(page.video.currentTime, 280, "on the right: on");
    eq(text(page.$("rippleforward")), "+10 s");
    const q = exchange(page, [], 300);
    ok("pk" in q, "a seek of the viewer's own, reported");
    page.tick(1000);
    tapAt(page, 100); page.tick(400); tapAt(page, 100);
    eq(page.video.currentTime, 280, "taps too far apart are two single taps");
    eq(page.video.paused, false, "and a tap in the middle is never part of one");
  });

  await check("the bar: a drag shows where it goes and seeks on letting go", async () => {
    const page = load({}, { pointer: true });
    page.hls().emit("manifest");
    page.video.ready(600, 0);
    const bar = page.$("bar");
    bar.dispatch("pointerdown", { clientX: 250 });
    eq(text(page.$("bubble")), "2:30");
    ok(bar.classes.includes("drag"));
    bar.dispatch("pointermove", { clientX: 500 });
    eq(text(page.$("bubble")), "5:00");
    eq(page.$("fill").style.width, "50%", "the bar follows the finger");
    eq(page.video.currentTime, 0, "but the video waits for the finger to let go");
    exchange(page); exchange(page);
    ok(shown(page), "the controls stay while it drags");
    bar.dispatch("pointerup", { clientX: 600 });
    eq(page.video.currentTime, 360);
    ok(!bar.classes.includes("drag"));
    ok("pk" in exchange(page, [], 300), "reported as the viewer's seek");
    bar.dispatch("pointerdown", { clientX: 100 });
    bar.dispatch("pointercancel", {});
    eq(page.video.currentTime, 360, "a drag the browser took away seeks nowhere");
    // Without pointer events: touches, and the mouse
    const old = load();
    old.hls().emit("manifest");
    old.video.ready(600, 0);
    old.$("bar").dispatch("touchstart", { touches: [{ clientX: 100 }] });
    old.$("bar").dispatch("touchmove", { touches: [{ clientX: 200 }] });
    old.$("bar").dispatch("touchend", { touches: [], changedTouches: [{ clientX: 200 }] });
    eq(old.video.currentTime, 120);
    const early = load({}, { pointer: true });
    early.$("bar").dispatch("pointerdown", { clientX: 500 });
    ok(!early.$("bar").classes.includes("drag"), "before the video is loaded, nothing to drag");
  });

  await check("the sheet: audio, subtitles and their size are chosen from lists", async () => {
    const page = load();
    page.hls().emit("manifest");
    page.video.ready(600, 10);
    eq(page.$("sheet").hidden, true);
    press(page, "cc");
    eq(page.$("sheet").hidden, false);
    eq(page.$("sheetbody").querySelectorAll(".group").length, 2, "audio and subtitles; no size while none are drawn");
    press(page, "opt-audio-1");
    eq(page.hls().audioTrack, 1);
    let q = exchange(page, [], 300);
    eq([q.a, "pa" in q], ["2", true]);
    press(page, "opt-audio-2");
    ok(page.video.muted, "Off mutes");
    eq(exchange(page, [], 300).a, "0");
    press(page, "opt-sub-1-soft");
    ok(page.xhrs.some((x) => x.url === "/aaa/1/hls/subs/1.vtt"));
    eq(page.$("sheetbody").querySelectorAll(".group").length, 3, "the size, now that they are drawn here");
    press(page, "opt-size-xl");
    eq(page.$("overlay").style.fontSize, "28px", "7% of the video's height (400 px)");
    q = exchange(page, [], 300);
    eq([q.s, q.z, "ps" in q, "pz" in q], ["1-soft", "xl", true, true]);
    press(page, "opt-sub-3-burn");
    eq(page.hls().url, "/aaa/1/hls/burn/3/master.m3u8", "burned in: the other version");
    ok(!page.$("opt-size-m"), "a burned-in one has no size here");
    press(page, "opt-sub-off");
    eq(page.hls().url, page.S.master);
    ok(!isOn(page, "cc"));
    press(page, "sheetclose");
    eq(page.$("sheet").hidden, true);
    press(page, "cc");
    page.$("sheet").dispatch("click", {});
    eq(page.$("sheet").hidden, true, "a tap on the shade around it closes it");
    const none = load({ audio: [], subs: [], start_audio: -1 });
    eq(text(none.$("sheetbody")), "This video has no other audio or subtitles.");
  });

  await check("it does what the owner asks, and a press crossing a command wins", async () => {
    const page = load({ start_sub: 0, start_sub_size: "s" });
    page.hls().emit("manifest");
    page.video.ready(600, 10);
    ok(isOn(page, "opt-size-s"), "this browser's size");
    exchange(page, [{ seq: 1, audio: 2 }, { seq: 2, sub_size: "l" }, { seq: 3, seek: 200 }]);
    eq([page.hls().audioTrack, page.video.currentTime], [1, 200]);
    ok(isOn(page, "opt-size-l"));
    eq(exchange(page, [], 300).ack, "3", "it says it saw them");
    page.tick(2000);
    press(page, "opt-size-xs");
    open(page.reports()).pop().respond(200, { commands: [{ seq: 4, sub_size: "xl" }] });
    ok(isOn(page, "opt-size-xs"), "the viewer's press wins over the command that crossed it");
  });

  await check("the owner switching the stream to the old player loads the page again", async () => {
    const page = load();
    exchange(page);
    eq(page.window.location.reloads, 0);
    page.tick(2000);
    open(page.reports()).pop().respond(200, { commands: [], ui: "new" });
    eq(page.window.location.reloads, 0, "the same look: nothing");
    page.tick(2000);
    open(page.reports()).pop().respond(200, { commands: [], ui: "old" });
    eq(page.window.location.reloads, 1);
    const group = loadGroup();
    group.tick(2000);
    answer(group, {}, { ui: "old" });
    eq(group.window.location.reloads, 1, "watching together too");
  });

  await check("full screen: the whole page, turned to the video's shape where the screen can be locked", async () => {
    const screen = orientationOf(false);
    const page = load({}, { screen });
    page.hls().emit("manifest");
    page.video.ready(600, 0);
    eq(page.$("full").hidden, false);
    press(page, "full");
    ok(page.document.fullscreenElement === page.root, "the whole page");
    ok(bodyHas(page, "full"));
    eq(screen.orientation.locks, ["landscape"], "a wide video: landscape");
    press(page, "full");
    ok(!page.document.fullscreenElement && !bodyHas(page, "full"));
    eq(screen.orientation.unlocks, 1, "and the screen is free again");
    page.video.videoWidth = 1080; page.video.videoHeight = 1920;
    key(page, "f");
    eq(screen.orientation.locks, ["landscape", "portrait"], "an upright video: portrait");
    const webkit = load({}, { full: "webkit" });
    press(webkit, "full");
    ok(webkit.document.webkitFullscreenElement, "Safari's names (an iPad)");
    eq(load({}, { full: "none" }).$("full").hidden, true, "no button where there is no full screen");
    const refused = load({}, { refuse: true });
    press(refused, "full");
    await settle();
    eq(text(refused.$("note")), "Full screen is not allowed here.");
  });

  await check("full screen on an iPhone: the video in the system's player, with the soft subtitles as a track", async () => {
    const page = load({ start_sub: 0 }, { full: "iphone" });
    eq(page.$("full").hidden, false, "the button is there (the old player had none on an iPhone)");
    press(page, "full");
    eq(text(page.$("note")), "Full screen works once the video has loaded.");
    page.hls().emit("manifest");
    page.video.ready(600, 0);
    press(page, "full");
    ok(page.video.webkitDisplayingFullscreen);
    const track = page.$("nativesubs");
    ok(track, "the subtitles go along");
    eq([track.getAttribute("kind"), track.getAttribute("src")], ["subtitles", "/aaa/1/hls/subs/1.vtt"]);
    page.video.leaveFull();
    ok(!page.$("nativesubs"), "and are taken out after");
    const plain = load({}, { full: "iphone" });
    plain.hls().emit("manifest");
    plain.video.ready(600, 0);
    press(plain, "full");
    ok(plain.video.webkitDisplayingFullscreen && !plain.$("nativesubs"), "without subtitles, no track");
  });

  await check("rotate: the screen the other way, or the page turned where it cannot be locked", async () => {
    eq(load().$("rotate").hidden, true, "not on a computer");
    const screen = orientationOf(false);
    const android = load({}, { touch: true, screen });
    eq(android.$("rotate").hidden, false);
    press(android, "rotate");
    ok(android.document.fullscreenElement, "it goes full screen to lock the screen");
    eq(screen.orientation.locks, ["portrait"], "a wide window turns upright");
    // An iPhone: no full screen of the page, no lock: the page is turned on the screen.
    const iphone = load({}, { touch: true, full: "iphone", screen: orientationOf(true) });
    iphone.hls().emit("manifest");
    iphone.video.ready(600, 300);
    press(iphone, "rotate");
    ok(bodyHas(iphone, "turned"));
    // Turned, the video's left is the top of the screen.
    iphone.tick(1000);
    tapAt(iphone, 500, 50); iphone.tick(100); tapAt(iphone, 500, 50);
    eq(iphone.video.currentTime, 290, "a double tap at the top steps back");
    iphone.window.innerWidth = 400; iphone.window.innerHeight = 1000;  // the phone was turned
    iphone.window.listeners.resize.forEach((fn) => fn());
    ok(!bodyHas(iphone, "turned"), "turning the phone undoes it");
    // A lock that is refused (a browser that has the call, but not the lock) turns the page.
    const refusing = load({}, { touch: true, screen: orientationOf(true) });
    press(refusing, "rotate");
    press(refusing, "rotate");
    await settle();
    ok(bodyHas(refusing, "turned"));
  });

  await check("rotate: a full screen that is refused keeps no lock for the next one", async () => {
    const screen = orientationOf(false);
    const page = load({}, { touch: true, screen, refuse: true });
    page.hls().emit("manifest");
    page.video.ready(600, 0);
    press(page, "rotate");  // a wide window: it would lock to portrait, in full screen
    await settle();
    eq(screen.orientation.locks, []);
    page.document.documentElement.requestFullscreen = function () {  // allowed now
      page.document.fullscreenElement = this;
      (page.document.listeners.fullscreenchange || []).forEach((fn) => fn({}));
      return Promise.resolve();
    };
    press(page, "full");
    eq(screen.orientation.locks, ["landscape"], "Full screen locks to the video's shape");
  });

  await check("keys: Space plays and pauses, the arrows step, f is full screen", async () => {
    const page = load();
    page.hls().emit("manifest");
    page.video.ready(600, 100);
    await settle();
    key(page, " ");
    ok(page.video.paused);
    key(page, "ArrowRight");
    eq(page.video.currentTime, 110);
    key(page, "ArrowLeft"); key(page, "ArrowLeft");
    eq(page.video.currentTime, 90);
    key(page, "f");
    ok(page.document.fullscreenElement);
    key(page, "c");
    eq(page.$("sheet").hidden, false);
    key(page, "Escape");
    eq(page.$("sheet").hidden, true);
  });

  await check("the viewer's name: watching together only, kept in this browser, sent with each report", async () => {
    eq(load().$("me").hidden, true, "on its own a name is for no one");
    const store = storage();
    const page = loadGroup({}, {}, { storage: store });
    eq(page.$("me").hidden, false);
    ok(page.$("initial").classes.includes("anon"), "a figure until it has one");
    press(page, "me");
    eq(page.$("namebox").hidden, false);
    page.$("name").value = "  asha\n  k ";
    page.$("nameform").dispatch("submit", {});
    eq(page.$("namebox").hidden, true);
    eq(store.data["vpstream-name"], "asha k", "one line");
    eq(text(page.$("initial")), "A");
    page.tick(300);
    eq(answer(page).n, "asha k");
    const again = loadGroup({}, {}, { storage: store });
    again.tick(2000);
    eq(answer(again).n, "asha k", "it is remembered");
    const none = loadGroup();
    none.tick(2000);
    ok(!("n" in answer(none)), "no name, none sent");
    const broken = loadGroup({}, {}, { storage: { getItem() { throw new Error("private"); }, setItem() { throw new Error("private"); } } });
    press(broken, "me");
    broken.$("name").value = "Sam";
    broken.$("nameform").dispatch("submit", {});
    eq(text(broken.$("initial")), "S", "a browser that keeps nothing still has it for the page");
    press(broken, "me");
    press(broken, "namecancel");
    eq(broken.$("namebox").hidden, true);
  });

  await check("the viewer's name: cut whole, never inside an emoji, and cleaned as the daemon cleans it", async () => {
    // 1 letter and 39 emoji: 40 characters as Safari's maxlength counts them, 79 code units.
    const store = storage({ "vpstream-name": "a" + "😀".repeat(39) });
    const page = loadGroup({}, {}, { storage: store });
    page.tick(2000);
    const n = answer(page).n;
    eq(n, "a" + "😀".repeat(19), "the half emoji at the cut is left out");
    page.tick(2000);
    ok(open(page.reports()).length === 1, "and the reports go on");
    const emoji = loadGroup({}, {}, { storage: storage({ "vpstream-name": "😀Asha" }) });
    eq(text(emoji.$("initial")), "😀", "the face has the whole first character");
    const odd = loadGroup({}, {}, { storage: storage({ "vpstream-name": "As\u0085ha\u200bK" }) });
    odd.tick(2000);
    eq(answer(odd).n, "As ha K", "what is not printable is a space, as in clients.name_value");
  });

  await check("the viewer's name: half an emoji anywhere in it is a space, and the reports go on", async () => {
    const page = loadGroup({}, {}, { storage: storage({ "vpstream-name": "As\ud83dha\ude00" }) });
    page.tick(2000);
    eq(answer(page).n, "As ha", "as clients.name_value has it");
    page.tick(2000);
    ok(open(page.reports()).length === 1, "the next report goes out");
  });

  await check("the fullest report together stays within what the daemon reads (public.MAX_VALUES)", async () => {
    const page = loadGroup({ paused: true, position: 10 }, { start_sub: 0 }, { storage: storage({ "vpstream-name": "Asha" }) });
    inGroup(page, { paused: true, position: 10 }, 10);
    page.tick(2000);
    answer(page, { paused: true, position: 10 });
    press(page, "cc");
    page.$("opt-size-l").click();  // a size press not yet told: pz=
    page.$("opt-audio-1").click();  // a press for the group: act=, id=, v=
    const keys = open(page.reports()).pop().url.split("?")[1].split("&").map((p) => p.split("=")[0]);
    eq(keys.length, 16, keys.join(" "));
  });

  await check("together: each page says who changed the group, unless it was this page", async () => {
    const page = loadGroup({ paused: false, position: 10 }, {}, { storage: storage({ "vpstream-name": "Me" }) });
    const pg = inGroup(page, { paused: false, position: 10 }, 10).pg;
    const rev = (state) => {
      open(waits(page)).pop().respond(200, { together: groupState(state), waited: 1 });
      page.tick(0);
    };
    eq(page.$("toast").hidden, true);
    rev({ rev: 2, paused: true, position: 12, changes: [{ what: "pause", by: "d1", page: "other", name: "Asha" }] });
    eq(page.$("toast").hidden, false);
    eq(text(page.$("toasttext")), "Asha paused");
    eq(text(page.$("toastface")), "A");
    page.tick(4500);
    eq(page.$("toast").hidden, true, "for a few seconds");
    rev({ rev: 3, paused: true, position: 125, changes: [{ what: "seek", by: "owner", page: "", name: "" }] });
    eq(text(page.$("toasttext")), "The host jumped to 2:05");
    rev({ rev: 4, paused: true, position: 125, audio: 2, sub: "1-soft",
      changes: [{ what: "audio", by: "d2", page: "p2", name: "" }, { what: "sub", by: "d2", page: "p2", name: "" }] });
    eq(text(page.$("toasttext")), "Someone switched the audio to Japanese and turned on the subtitles: English");
    rev({ rev: 5, paused: false, position: 125, audio: 2, sub: "off",
      changes: [{ what: "play", by: "d1", page: "other", name: "Asha" }, { what: "sub", by: "d2", page: "p2", name: "Sam" }] });
    eq(text(page.$("toasttext")), "Asha pressed play · Sam turned the subtitles off");
    page.tick(4500);
    rev({ rev: 6, paused: true, position: 125, audio: 2, sub: "off", changes: [{ what: "pause", by: "me", page: pg, name: "Me" }] });
    eq(page.$("toast").hidden, true, "this page's own press: no notice");
    rev({ rev: 7, paused: true, position: 125, audio: 2, sub: "off", changes: [] });
    eq(page.$("toast").hidden, true, "the group reaching the end: no one did it");
    // A page that comes to another video says who sent it there, if that was just now.
    const names = ["One", "Two"];
    const changes = [{ what: "video", by: "d1", page: "other", name: "Asha" }];
    const moved = load({ number: 2, folder: true, together: { state: groupState({ video: 2, changes, changed_ago: 1.5 }), sync: "/aaa/1/sync", cooldown: 10, names } });
    eq(text(moved.$("toasttext")), "Asha switched to 2. Two");
    const later = load({ number: 2, folder: true, together: { state: groupState({ video: 2, changes, changed_ago: 60 }), sync: "/aaa/1/sync", cooldown: 10, names } });
    eq(later.$("toast").hidden, true, "a change of long ago is not news");
  });

  await check("together: a page asks for a name once it is in, if it has none", async () => {
    const page = loadGroup();
    inGroup(page);
    eq(text(page.$("toasttext")), "Tap the face at the top to tell the others your name.");
    const store = storage();
    const first = loadGroup({}, {}, { storage: store });
    inGroup(first);
    eq(first.$("toast").hidden, false, "once in this browser");
    const second = loadGroup({}, {}, { storage: store });
    inGroup(second);
    eq(second.$("toast").hidden, true, "not on each video after");
    const named = loadGroup({}, {}, { storage: storage({ "vpstream-name": "Asha" }) });
    inGroup(named);
    eq(named.$("toast").hidden, true);
  });

  await check("together: in the cooldown only full screen, the subtitle size and the name work", async () => {
    const page = loadGroup({}, { start_sub: 0 });
    page.hls().emit("manifest");
    page.video.ready(600, 0);
    page.tick(600);
    answer(page, {}, { cooldown: 10 });
    eq(text(page.$("note")), "Watching together: the controls work in 10 s");
    ok(page.$("play").disabled && page.$("opt-audio-1").disabled && page.$("opt-sub-off").disabled);
    ok(!page.$("opt-size-l").disabled, "the size is this page's own");
    tapAt(page, 500);
    tapAt(page, 100); page.tick(100); tapAt(page, 100);
    press(page, "opt-audio-1");
    page.$("bar").dispatch("touchstart", { touches: [{ clientX: 500 }] });
    page.$("bar").dispatch("touchend", { changedTouches: [{ clientX: 500 }] });
    press(page, "opt-size-l");
    for (let i = 0; i < 3; i++) {
      const q = exchange(page, [], 2000);
      ok(!("act" in q), "nothing for the group");
    }
    ok(isOn(page, "opt-size-l"));
    press(page, "full");
    ok(page.document.fullscreenElement);
    for (let i = 0; i < 3; i++) { page.tick(2000); answer(page); }
    ok(!page.$("play").disabled && !page.$("opt-audio-1").disabled, "after it, all");
    press(page, "opt-audio-1");
    const q = answer(page);
    eq([q.act, q.v], ["audio", "2"], "audio goes to the group");
  });

  await check("together: a tap in the middle, a double tap and the bar go to the group", async () => {
    const page = loadGroup({ paused: false, position: 300 }, {}, { pointer: true });
    inGroup(page, { paused: false, position: 300 }, 300);
    await settle();
    page.tick(500);
    tapAt(page, 500);
    ok(page.video.paused, "paused here at once, while the press is on its way");
    eq(page.$("play").getAttribute("aria-label"), "Play");
    let q = answer(page, { rev: 2, paused: true, position: 300 });
    eq(q.act, "pause");
    page.tick(500);
    tapAt(page, 100); page.tick(100); tapAt(page, 100); page.tick(100); tapAt(page, 100);
    const steps = [];
    for (let i = 0; i < 2; i++) { q = answer(page, { rev: 2, paused: true, position: 300 }, { outcome: "queued" }); steps.push([q.act, q.v]); }
    eq(steps, [["skip", "-10"], ["skip", "-10"]], "two steps the daemon stacks");
    eq(text(page.$("note")), "Seeking for everyone...");
    for (let i = 0; i < 2; i++) { page.tick(2000); answer(page, { rev: 2, paused: true, position: 300 }); }
    page.$("bar").dispatch("pointerdown", { clientX: 100 });
    page.$("bar").dispatch("pointerup", { clientX: 100 });
    q = answer(page, { rev: 2, paused: true, position: 300 });
    eq([q.act, q.v], ["seek", "60.0"]);
  });

  await check("together: in an iPhone's own full screen, its buttons are presses for the group", async () => {
    const page = loadGroup({ paused: false, position: 10 }, {}, { full: "iphone" });
    inGroup(page, { paused: false, position: 10 }, 10);
    await settle();
    page.tick(500);
    press(page, "full");
    page.video.pause();  // its own pause button
    await settle();
    eq(answer(page).act, "pause");
    page.video.leaveFull();
    page.video.play();
    await settle();
    page.tick(500);
    ok(open(page.reports()).every((x) => !("act" in x.query)), "outside it, a play the page did itself is no press");
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
    rev({ rev: 3, paused: true, position: 12, audio: 2 });
    ok(isOn(page, "opt-audio-1"));
    rev({ rev: 4, paused: true, position: 12, audio: 0, sub: "1-soft" });
    ok(page.video.muted && isOn(page, "opt-sub-1-soft"));
    rev({ rev: 5, paused: true, position: 12, audio: 0, sub: "3-burn" });
    eq(page.hls().url, "/aaa/1/hls/burn/3/master.m3u8");
    rev({ rev: 6, video: 2 });
    eq(page.window.location.href, "/aaa/2", "another video: the page of it");
  });

  await check("together: Next and the list of videos choose a video for everyone", async () => {
    const names = ["One", "Two", "Three"];
    const page = load({ folder: true, count: 3, next: "/aaa/2", together: { state: groupState(), sync: "/aaa/1/sync", cooldown: 10, names } });
    inGroup(page);
    press(page, "next");
    eq([answer(page).act, page.window.location.href], ["video", ""], "it asks; it does not go");
    press(page, "episodes");
    const choices = page.$("list").querySelectorAll("button");
    eq(choices.length, 4);
    choices[2].click();
    eq(page.$("list").hidden, true);
    const q = answer(page, {}, { outcome: "queued" });
    eq([q.act, q.v], ["video", "3"]);
    eq(text(page.$("note")), "Changing the video for everyone...");
  });

  await check("together: a pause the browser makes by itself goes nowhere; a tap joins again", async () => {
    const page = loadGroup({ paused: false, position: 10 });
    inGroup(page, { paused: false, position: 10 }, 10);
    await settle();
    page.video.pause();
    await settle();
    for (let i = 0; i < 2; i++) { ok(!("act" in exchange(page, [], 2000))); }
    eq(page.$("join").hidden, false);
    press(page, "join");
    await settle();
    ok(!page.video.paused);
    eq(page.$("join").hidden, true);
  });

  // The watching-together runs of player_test.js, on this page: the same sync, with these
  // controls.
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
        const run = await netRun(Object.assign({ seconds: 25, seed, loadMs: 1500 }, o));
        ok(run.unlocked < 4, `${name}, seed ${seed}: at the group's place after ${run.unlocked} s`);
        ok(worst(run, run.unlocked * 1000) < 0.5, `${name}, seed ${seed}: and near it from then on`);
      }
    }
  });

  await check("together over the internet: after the group's seek it lands where the group is by then", async () => {
    for (const [name, o] of INTERNET) {
      for (const seed of SEEDS) {
        const changes = [[10000, (g) => { g.position = 300; }], [30000, (g) => { g.position = 100; }]];
        const run = await netRun(Object.assign({ seconds: 50, seed, changes }, o));
        ok(settledBy(run, 30000, 0.3) < 2, `${name}, seed ${seed}: near after ${settledBy(run, 30000, 0.3)} s`);
        ok(worst(run, 40000) < 0.3, `${name}, seed ${seed}: and stays (${worst(run, 40000)})`);
      }
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

  await check("together over the internet: a video that waits for data plays on out of sync until Sync", async () => {
    for (const [name, o] of INTERNET) {
      for (const seed of SEEDS) {
        const sync = (page) => press(page, "sync");
        const run = await netRun(Object.assign({ seconds: 50, seed, stalls: [[20000, 6000]], presses: [[35000, sync]] }, o));
        const at = (t) => run.off.find((x) => x.t >= t);
        ok(!at(19000).sync && at(30000).sync, `${name}, seed ${seed}: Sync shows once it is out of sync`);
        eq(at(34900).seeks, at(19000).seeks, `${name}, seed ${seed}: no seek of its own`);
        ok(settledBy(run, 35000, 1) < 3, `${name}, seed ${seed}: Sync puts it back`);
        // Up to 0.1 s more on the uneven link: a page cannot see that its answers come faster
        // than its reports go, and it stops its rate once it measures 0.2 s off.
        ok(worst(run, 45000) < 0.4 && !at(40000).sync, `${name}, seed ${seed}: and near (${worst(run, 45000)})`);
      }
    }
  });

  await check("together over the internet: a seek that lands early waits there for the group", async () => {
    for (const seed of SEEDS) {
      const sync = (page) => press(page, "sync");
      const run = await netRun({ seconds: 70, seed, up: 100, down: 100, jitter: 20, seekMs: 4000,
        seekMsFrom: [[45000, 200]], stalls: [[10000, 8000], [35000, 8000]], presses: [[25000, sync], [50000, sync]] });
      ok(settledBy(run, 50000, 0.3) < 6, `seed ${seed}: with the group after ${settledBy(run, 50000, 0.3)} s`);
      ok(!run.off.find((x) => x.t >= 60000).sync, `seed ${seed}: in sync`);
      // Its wait there is its own pause: no tap is asked for (browsers send the event late).
      ok(run.off.some((x) => x.t > 50000 && x.paused), `seed ${seed}: it waited`);
      ok(!run.off.some((x) => x.join), `seed ${seed}: and no join button`);
    }
  });

  await check("together over the internet: alone, a video that waits for data takes the group along, with no Sync", async () => {
    for (const [name, o] of INTERNET) {
      for (const seed of SEEDS) {
        // The VPS hand test, 2026-11-28: one viewer, and the controls came up now and then.
        const run = await netRun(Object.assign({ seconds: 50, seed, alone: true, stalls: [[20000, 6000]] }, o));
        const after = run.off.filter((x) => x.t >= 20000);
        ok(after.every((x) => !x.sync), `${name}, seed ${seed}: no Sync`);
        ok(after.every((x) => !x.controls), `${name}, seed ${seed}: the controls stay away`);
        eq(run.off[run.off.length - 1].seeks, run.off.find((x) => x.t >= 19000).seeks, `${name}, seed ${seed}: no seek`);
        ok(worst(run, 32000) < 0.6, `${name}, seed ${seed}: the group is where it is (${worst(run, 32000)})`);
      }
    }
  });

  await check("together over the internet: out of sync, Sync shows by itself, not the controls", async () => {
    const run = await netRun({ seconds: 40, seed: 1, up: 50, down: 50, jitter: 5, seekMs: 400, stalls: [[20000, 6000]] });
    const at = (t) => run.off.find((x) => x.t >= t);
    ok(at(30000).sync, "Sync shows");
    ok(run.off.filter((x) => x.t >= 20000).every((x) => !x.controls), "and nothing else comes up");
  });

  finish();
})();
