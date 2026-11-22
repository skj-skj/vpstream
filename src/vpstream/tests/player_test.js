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
    send() { this.sent = true; xhrs.push(this); }
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
  return { S, root, video, hlses, xhrs, reports, tick, document, window, $: (id) => root.querySelector("#" + id),
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
    eq(q, { p: "1", ack: "0", a: "1", s: "off" }, "no position while loading, no duration yet");
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
    ok(!("act" in q) && !("ack" in q) && !("pk" in q), "status only: " + JSON.stringify(q));
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

  await check("together: it keeps up by a seek when far off, by its rate when near", async () => {
    const page = loadGroup({ paused: false, position: 100 });
    inGroup(page, { paused: false, position: 100 }, 100);
    await settle();
    page.tick(1000);
    at(page, 100, 103.5);
    eq([page.video.currentTime, page.video.playbackRate], [100, 1], "3.5 s ahead: a seek");
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
    eq(page.video.currentTime, 98, "2 s behind is not yet a seek");
    at(page, 100, 97.9);
    eq(page.video.currentTime, 100, "more is");
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

  await check("together: a wait that was aborted goes again", async () => {
    const page = loadGroup();
    eq(waits(page).length, 1);
    waits(page)[0].onabort();
    page.tick(2000);
    eq(waits(page).length, 2);
  });

  finish();
})();
