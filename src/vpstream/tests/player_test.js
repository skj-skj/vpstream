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
  return { S, root, video, hlses, xhrs, reports, tick, document, $: (id) => root.querySelector("#" + id),
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

  finish();
})();
