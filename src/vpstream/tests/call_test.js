// Runs the call of watching together (call.js) in node, with a stand-in for each thing a
// browser has: RTCPeerConnection, the camera and mic (getUserMedia), MediaStream, and the
// daemon on the other side of its posts (as together.Call has it, much simplified: who is
// in, and the mailboxes). A clock the checks move, and promises they let run. Usage:
// node call_test.js CALL
"use strict";
const fs = require("fs");
const vm = require("vm");
const { settle, runner, eq, ok } = require("./dom.js");

const CALL = fs.readFileSync(process.argv[2], "utf8");

// --- what a browser has, as far as call.js uses it

class Track {
  constructor(kind, label) { this.kind = kind; this.label = label || kind; this.enabled = true; this.stopped = false; }
  stop() { this.stopped = true; }
}

class MediaStream {
  constructor(tracks) { this.tracks = tracks || []; }
  getAudioTracks() { return this.tracks.filter((t) => t.kind === "audio"); }
  getVideoTracks() { return this.tracks.filter((t) => t.kind === "video"); }
}

class Sender {
  constructor() { this.track = null; this.params = { encodings: [] }; this.set = null; }
  replaceTrack(t) { this.track = t; return Promise.resolve(); }
  getParameters() { return JSON.parse(JSON.stringify(this.params)); }
  setParameters(p) { this.set = p; this.params = p; return Promise.resolve(); }
}

// One browser's RTCPeerConnection. Addresses are found at once unless `slowIce`; the
// checks connect a pair (`link`) or make it fail.
function peerClass(world) {
  return class RTCPeerConnection {
    constructor(config) {
      this.config = config;
      this.transceivers = [];
      this.signalingState = "stable";
      this.connectionState = "new";
      this.iceGatheringState = "new";
      this.localDescription = null;
      this.remoteDescription = null;
      this.closed = false;
      world.pcs.push(this);
    }
    addTransceiver(kind, init) {
      const t = { kind, direction: init.direction, sender: new Sender(), receiver: { track: new Track(kind, "remote") } };
      this.transceivers.push(t);
      return t;
    }
    getTransceivers() { return this.transceivers; }
    createOffer() { return Promise.resolve({ type: "offer", sdp: "offer " + world.name }); }
    createAnswer() { return Promise.resolve({ type: "answer", sdp: "answer " + world.name }); }
    setLocalDescription(d) {
      this.localDescription = { type: d.type, sdp: d.sdp + " +ice" };
      this.signalingState = d.type === "offer" ? "have-local-offer" : "stable";
      this.iceGatheringState = world.slowIce ? "gathering" : "complete";
      return Promise.resolve();
    }
    setRemoteDescription(d) {
      this.remoteDescription = d;
      if (d.type === "offer") {
        ["audio", "video"].forEach((kind) => this.addTransceiver(kind, { direction: "recvonly" }));
        this.signalingState = "have-remote-offer";
      } else {
        this.signalingState = "stable";
      }
      return Promise.resolve();
    }
    close() { this.closed = true; this.connectionState = "closed"; }
    // The pair connects: each side gets the other's sound and video.
    connected() {
      this.connectionState = "connected";
      this.onconnectionstatechange();
      this.transceivers.forEach((t) => this.ontrack({ track: t.receiver.track }));
    }
    fails() { this.connectionState = "failed"; this.onconnectionstatechange(); }
    gathered() { this.iceGatheringState = "complete"; this.onicegatheringstatechange(); }
  };
}

// --- the daemon, much simplified (see together.Call)

function daemon() {
  const d = { id: "", boot: "b1", calls: 0, v: 1, members: [], ring: new Set(), left: new Set(), mail: {}, seq: 0, posts: [], names: {} };
  d.state = (page) => ({
    id: d.id, boot: d.boot, v: d.v, by: d.members.length ? d.names[d.starter] || "" : "", starter: d.starter || "",
    members: d.members.map((m) => Object.assign({ name: d.names[m.page] || "" }, m)),
    ring: !!d.id && !d.members.some((m) => m.page === page) && !d.left.has(page),
    mail: (d.mail[page] || []).filter((m) => m.seq > (d.after[page] || 0)),
  });
  d.after = {};
  d.handle = (body) => {
    d.posts.push(body);
    d.after[body.pg] = body.cm;
    let outcome = "refused";
    const at = d.members.findIndex((m) => m.page === body.pg);
    if (body.do === "start" && body.call && body.call !== d.id) {
      outcome = "ended";  // the call it meant to join is over
    } else if (body.do === "start") {
      const was = d.members.findIndex((m) => m.page === body.was);
      if (was >= 0) {
        d.members[was] = Object.assign({}, d.members[was], { page: body.pg, away: false });
        outcome = "joined";
      } else if (!d.id) {
        d.calls += 1; d.id = String(d.calls); d.starter = body.pg; d.left = new Set();
        d.members = [{ page: body.pg, mic: false, cam: true, sees: [] }];
        outcome = "started";
      } else if (at < 0) {
        d.members.push({ page: body.pg, mic: false, cam: true, sees: [] });
        outcome = "joined";
      } else {
        outcome = "joined";
      }
      const m = d.members.find((x) => x.page === body.pg);
      Object.assign(m, { mic: body.mic, cam: body.cam, sees: body.sees });
      d.v += 1;
    } else if (body.do === "end" && at >= 0) {
      if (d.members.length <= 2) { d.id = ""; d.members = []; d.mail = {}; outcome = "ended"; }
      else { d.members.splice(at, 1); d.left.add(body.pg); outcome = "left"; }
      d.v += 1;
    } else if (body.do === "state" && at >= 0) {
      Object.assign(d.members[at], { mic: body.mic, cam: body.cam, sees: body.sees });
      d.v += 1;
      outcome = "applied";
    } else if (body.do === "away" && at >= 0) {
      d.members[at].away = true;
      d.v += 1;
      outcome = "applied";
    } else if (body.do === "send") {
      d.seq += 1;
      (d.mail[body.to] = d.mail[body.to] || []).push({ seq: d.seq, from: body.pg, kind: body.kind, data: body.data });
      outcome = "sent";
    }
    return { call: d.state(body.pg), outcome };
  };
  return d;
}

// --- one page in its own browser

function browser(name, d, options) {
  const o = options || {};
  let now = 0;
  const timers = [];
  const said = [];
  const world = { name, pcs: [], slowIce: !!o.slowIce };
  const asked = [];  // what getUserMedia was asked for
  const tracks = [];  // ...and what it gave
  const session = o.session || {};
  const fetches = [];
  const window = {
    isSecureContext: o.secure !== false,
    RTCPeerConnection: o.noRtc ? undefined : peerClass(world),
    MediaStream,
    setTimeout: (fn, ms) => { timers.push({ fn, at: now + (ms || 0), live: true }); return timers.length; },
    clearTimeout: (id) => { if (timers[id - 1]) { timers[id - 1].live = false; } },
    sessionStorage: {
      getItem: (k) => (k in session ? session[k] : null),
      setItem: (k, v) => { session[k] = String(v); },
      removeItem: (k) => { delete session[k]; },
    },
    listeners: {},
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    // A post that goes on after the page has gone (keepalive).
    fetch: (url, init) => {
      fetches.push(Object.assign({ url }, init));
      d.handle(JSON.parse(init.body));
      return Promise.resolve({});
    },
    navigator: {
      mediaDevices: {
        getUserMedia(c) {
          asked.push(c);
          const refuse = o.refuse || [];
          if ((c.video && refuse.includes("video")) || (c.audio && refuse.includes("audio"))) {
            const e = new Error("refused");
            e.name = "NotAllowedError";
            return Promise.reject(e);
          }
          const got = [];
          if (c.audio) { got.push(new Track("audio", "mic")); }
          if (c.video) { got.push(new Track("video", "camera " + c.video.facingMode)); }
          tracks.push(...got);
          return Promise.resolve(new MediaStream(got));
        },
        enumerateDevices: () => Promise.resolve(o.cameras === 2
          ? [{ kind: "videoinput" }, { kind: "videoinput" }, { kind: "audioinput" }]
          : [{ kind: "videoinput" }, { kind: "audioinput" }]),
      },
    },
  };
  const tick = (ms) => {
    const end = now + ms;
    for (;;) {
      const due = timers.filter((t) => t.live && t.at <= end).sort((a, b) => a.at - b.at)[0];
      if (!due) { break; }
      now = due.at;
      due.live = false;
      due.fn();
    }
    now = end;
  };
  const posts = [];
  class XMLHttpRequest {
    open(method, url) { this.method = method; this.url = url; this.headers = {}; }
    setRequestHeader(k, v) { this.headers[k] = v; }
    send(body) {
      this.body = JSON.parse(body);
      posts.push(this);
      if (o.hold) { return; }  // answered by the check
      Promise.resolve().then(() => this.respond());
    }
    fail() { this.onerror(); }  // lost on the way, or "too many": no answer
    respond(answer) {
      this.status = 200;
      this.responseText = JSON.stringify(answer || d.handle(this.body));
      this.onload();
    }
  }
  const audioBox = { kids: [], appendChild(el) { this.kids.push(el); el.parentNode = this; }, removeChild(el) { this.kids = this.kids.filter((k) => k !== el); } };
  const document = { createElement: (tag) => ({ tag, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, play: () => Promise.resolve() }) };
  const context = { window, document, XMLHttpRequest, JSON, Math, Object, Array, String, Number, Error, Promise,
    Date: { now: () => 1e12 + now }, isFinite, Infinity, NaN, console };
  window.window = window;
  vm.createContext(context);
  vm.runInContext(CALL, context);
  const call = window.VpCall("/aaa/1/call", name, { said: (t) => said.push(t), name: () => d.names[name] || "", audioBox });
  // Whatever the daemon says of the call now, as the next wait or report would bring it.
  const poll = () => call.update(d.state(name));
  return { call, world, asked, tracks, posts, fetches, said, tick, poll, audioBox, window, session, pc: () => world.pcs[world.pcs.length - 1] };
}

const posted = (b, what) => b.posts.map((x) => x.body).filter((body) => body.do === what);

// Pages a and b (a has the lower id, so it offers), both in the call and connected.
async function pair(options) {
  const d = daemon();
  d.names = { a: "Asha", b: "Ravi" };
  const a = browser("a", d, options), b = browser("b", d, options);
  a.call.start();
  await settle();
  b.poll();
  b.call.start();
  await settle();
  a.poll(); b.poll();
  await settle();
  b.poll();  // the offer
  await settle();
  a.poll();  // the answer
  await settle();
  a.pc().connected(); b.pc().connected();
  return { d, a, b };
}

const sender = (b, kind) => b.pc().transceivers.find((t) => t.kind === kind).sender;

// --- the checks

const { check, finish } = runner();

(async () => {
  await check("a call needs a secure page, RTCPeerConnection and a camera API", async () => {
    const d = daemon();
    ok(browser("a", d).call.possible);
    ok(!browser("a", d, { secure: false }).call.possible, "plain HTTP");
    ok(!browser("a", d, { noRtc: true }).call.possible, "no WebRTC");
  });

  await check("Call: the camera starts on, small, and the mic off; the call rings the others", async () => {
    const d = daemon();
    const a = browser("a", d), b = browser("b", d);
    a.call.start();
    eq(a.call.joining(), true);
    await settle();
    const ask = a.asked[0];
    eq([ask.video.width.ideal, ask.video.height.ideal, ask.video.frameRate.max, ask.video.facingMode], [240, 180, 12, "user"]);
    ok(ask.audio.echoCancellation && ask.audio.noiseSuppression, "the film's echo is taken out");
    const mic = a.tracks.find((t) => t.kind === "audio");
    eq(mic.enabled, false, "muted");
    const start = posted(a, "start")[0];
    eq([start.mic, start.cam, start.pg], [false, true, "a"]);
    eq(a.posts[0].headers["X-Vpstream-Player"], "1");
    eq(a.posts[0].headers["Content-Type"], "application/json");
    ok(a.call.inCall() && !a.call.ringing());
    eq(a.world.pcs.length, 0, "nobody to connect to yet");
    b.poll();
    ok(b.call.ringing(), "b is rung");
    eq(b.call.by(), "", "a gave no name");
    eq(b.asked.length, 0, "no camera before b answers");
    b.call.dismiss();
    ok(!b.call.ringing(), "Not now");
    b.poll();
    ok(!b.call.ringing(), "and it stays away for this call");
  });

  await check("the page with the lower id offers; one connection per pair; the offer goes with its addresses", async () => {
    const { a, b } = await pair();
    eq(a.world.pcs.length, 1);
    eq(b.world.pcs.length, 1);
    eq(posted(a, "send").map((s) => [s.to, s.kind, s.data]), [["b", "offer", "offer a +ice"]]);
    eq(posted(b, "send").map((s) => [s.to, s.kind, s.data]), [["a", "answer", "answer b +ice"]]);
    eq(a.pc().remoteDescription, { type: "answer", sdp: "answer b +ice" });
    ok(a.pc().config.iceServers.some((s) => s.urls === "stun:stun.cloudflare.com:3478"), "a public STUN server");
    ok(!a.pc().config.iceServers.some((s) => /^turns?:/.test(s.urls)), "no TURN");
    eq(b.pc().transceivers.map((t) => t.direction), ["sendrecv", "sendrecv"], "b sends too");
    const tiles = a.call.tiles();
    eq(tiles.map((t) => [t.page, t.name, t.mine]), [["b", "Ravi", false], ["a", "Asha", true]], "the others first, then you");
    eq(tiles[0].state, "connected");
    ok(tiles[0].stream && tiles[0].stream.getVideoTracks().length === 1, "b's video");
    eq(a.audioBox.kids.length, 1, "b's sound plays in an audio element of its own");
    ok(a.audioBox.kids[0].srcObject.getAudioTracks().length === 1);
  });

  await check("an offer waits for the addresses, at most 3 s", async () => {
    const d = daemon();
    const a = browser("a", d, { slowIce: true }), b = browser("b", d, { slowIce: true });
    a.call.start(); await settle();
    b.poll(); b.call.start(); await settle();
    a.poll(); await settle();
    eq(posted(a, "send").length, 0, "still finding its addresses");
    a.tick(2999); await settle();
    eq(posted(a, "send").length, 0);
    a.tick(1); await settle();
    eq(posted(a, "send").length, 1, "goes with what it has");
    b.poll(); await settle();
    b.pc().gathered(); await settle();
    eq(posted(b, "send").length, 1, "the answer goes once they are found");
  });

  await check("video and sound are capped, at the lowest priority; video goes only to a page that shows this one's tile", async () => {
    const { d, a, b } = await pair();
    const set = sender(a, "video").set, e = set.encodings[0];
    eq([e.maxBitrate, e.maxFramerate, set.degradationPreference], [100000, 12, "maintain-framerate"]);
    eq([e.priority, e.networkPriority], ["very-low", "very-low"], "after the film");
    const sound = sender(a, "audio").set.encodings[0];
    eq([sound.maxBitrate, sound.priority, sound.maxFramerate], [32000, "low", undefined]);
    ok(sender(a, "audio").track && sender(a, "audio").track.kind === "audio", "the sound goes");
    eq(sender(a, "video").track, null, "b shows no tile of a yet");
    b.call.show(["a"]);
    b.tick(300); await settle();
    eq(posted(b, "state").pop().sees, ["a"]);
    a.poll();
    eq(sender(a, "video").track.kind, "video", "b shows a: a sends");
    b.call.show([]);  // Zen, or another page of tiles
    b.tick(300); await settle();
    a.poll();
    eq(sender(a, "video").track, null);
    eq(d.members.length, 2);
  });

  await check("while the film is short of data no video comes in, until it has had enough for 3 s", async () => {
    const { a, b } = await pair();
    b.call.show(["a"]); b.tick(300); await settle(); a.poll();
    eq(sender(a, "video").track.kind, "video");
    b.call.film(true);
    b.tick(300); await settle();
    eq(posted(b, "state").pop().sees, [], "b says it shows no tile");
    a.poll();
    eq(sender(a, "video").track, null, "so a sends b no video");
    ok(sender(a, "audio").track, "the sound goes on");
    b.call.show(["a"]);  // drawn again meanwhile
    b.call.film(false);
    b.tick(3000); await settle();
    eq(posted(b, "state").pop().sees, [], "not yet");
    b.tick(300); await settle();
    eq(posted(b, "state").pop().sees, ["a"]);
    a.poll();
    eq(sender(a, "video").track.kind, "video");
  });

  await check("while the film waits for data no video goes out, until it has played 3 s", async () => {
    const { a, b } = await pair();
    b.call.show(["a"]); b.tick(300); await settle(); a.poll();
    a.call.film(true);
    eq(sender(a, "video").track, null);
    ok(sender(a, "audio").track, "the sound goes on");
    a.call.film(false);
    a.tick(2000);
    a.call.film(true);  // waits again: it starts over
    a.call.film(false);
    a.tick(2999);
    eq(sender(a, "video").track, null);
    a.tick(1);
    eq(sender(a, "video").track.kind, "video");
  });

  await check("Mic and Camera: this page's own, told the others through the daemon", async () => {
    const { d, a, b } = await pair();
    b.call.show(["a"]); b.tick(300); await settle(); a.poll();
    a.call.toggleMic();
    eq(a.tracks.find((t) => t.kind === "audio").enabled, true);
    a.tick(300); await settle();
    eq(d.members[0].mic, true);
    b.poll();
    eq(b.call.tiles()[0].mic, true, "b's tile of a says so");
    const camera = a.tracks.find((t) => t.kind === "video");
    a.call.toggleCam();
    ok(camera.stopped, "the camera is let go");
    eq(sender(a, "video").track, null);
    eq(a.call.tiles().pop().cam, false);
    a.tick(300); await settle();
    b.poll();
    eq(b.call.tiles()[0].cam, false, "b shows a's initial");
    a.call.toggleCam();
    await settle();
    const again = a.tracks.filter((t) => t.kind === "video").pop();
    ok(again !== camera && !again.stopped);
    eq(sender(a, "video").track, again, "sent again, with no new offer");
    eq(a.world.pcs.length, 1);
  });

  await check("the front camera or the back one", async () => {
    const { a } = await pair({ cameras: 2 });
    ok(a.call.canFlip());
    const front = a.tracks.find((t) => t.kind === "video");
    a.call.flip();
    await settle();
    eq(a.asked.pop().video.facingMode, "environment");
    ok(front.stopped);
    eq(a.call.tiles().pop().front, false);
    ok(!browser("z", daemon()).call.canFlip(), "one camera: nothing to flip to");
  });

  await check("End, with two: both are out; the camera and mic are let go and the connections closed", async () => {
    const { d, a, b } = await pair();
    a.call.end();
    ok(a.tracks.every((t) => t.stopped), "a's camera and mic");
    ok(a.world.pcs.every((pc) => pc.closed));
    eq(a.audioBox.kids.length, 0);
    ok(!a.call.inCall());
    await settle();
    eq(d.id, "");
    b.poll();
    ok(!b.call.inCall() && b.tracks.every((t) => t.stopped) && b.world.pcs.every((pc) => pc.closed));
    eq(b.said.pop(), "The call ended.");
    eq(b.call.tiles(), []);
  });

  await check("with three, End takes out only the one who pressed it", async () => {
    const d = daemon();
    const [a, b, c] = ["a", "b", "c"].map((n) => browser(n, d));
    for (const x of [a, b, c]) { x.poll(); x.call.start(); await settle(); }
    for (let i = 0; i < 3; i++) { a.poll(); b.poll(); c.poll(); await settle(); }
    eq(a.world.pcs.length, 2, "a offers to b and c");
    eq(b.world.pcs.length, 2, "b answers a and offers to c");
    eq(c.call.tiles().map((t) => t.page), ["a", "b", "c"]);
    c.call.end();
    await settle();
    a.poll(); b.poll();
    ok(a.call.inCall() && b.call.inCall());
    eq(a.call.tiles().map((t) => t.page), ["b", "a"]);
    ok(a.world.pcs.find((pc) => pc.localDescription.sdp === "offer a +ice" && pc.closed), "a's connection to c closed");
    ok(!c.call.ringing(), "who left is not rung");
  });

  await check("a pair that does not connect tries again, then says Could not connect", async () => {
    const { a, b } = await pair();
    a.pc().fails();
    eq(a.call.tiles()[0].state, "connecting");
    a.tick(1000); await settle();
    eq(a.world.pcs.length, 2, "a offers again");
    b.poll(); await settle();
    eq(b.world.pcs.length, 2, "b answers it");
    b.pc().fails();
    await settle();
    eq(posted(b, "send").pop().kind, "bye", "b asks a to try again");
    a.poll(); await settle();
    eq(a.world.pcs.length, 3);
    a.tick(20000); await settle();  // never connects
    eq(a.call.tiles()[0].state, "failed");
    ok(a.call.inCall(), "the rest of the call goes on");
  });

  await check("every new call rings again, and a page loaded again takes its place without one", async () => {
    const { d, a, b } = await pair();
    b.call.end(); await settle();
    a.poll();
    a.call.start(); await settle();
    b.poll();
    ok(b.call.ringing(), "a new call: b is rung, though it ended the last");
    b.call.start(); await settle();
    // b's tab loads the next video: the page that comes takes b's place. Until then the
    // others do not show b.
    b.window.listeners.pagehide.forEach((fn) => fn());
    eq(b.fetches.map((f) => [f.method, f.keepalive, JSON.parse(f.body).do, f.headers["X-Vpstream-Player"]]),
      [["POST", true, "away", "1"]]);
    a.poll();
    eq(a.call.tiles().map((t) => t.page), ["a"], "b is away");
    eq(a.call.count(), 1);
    ok(a.world.pcs.every((pc) => pc.closed), "a's connection to b closed");
    const again = browser("b2", d, { session: b.session });
    again.poll();
    ok(!again.call.ringing(), "no ring: it is coming back");
    await settle();
    const start = posted(again, "start")[0];
    eq([start.was, start.cam, start.mic], ["b", true, false]);
    eq(d.members.map((m) => m.page), ["a", "b2"]);
    ok(again.call.inCall());
    a.poll();
    eq(a.call.tiles().map((t) => [t.page, t.state]), [["b2", "connecting"], ["a", "connected"]], "never three");
    const late = browser("c", d);
    late.poll();
    ok(late.call.ringing(), "who opens the stream while it runs is rung");
  });

  await check("no camera allowed: in all the same, seeing and hearing the others", async () => {
    const d = daemon();
    const a = browser("a", d, { refuse: ["video", "audio"] });
    a.call.start(); await settle();
    ok(a.call.inCall());
    eq(posted(a, "start")[0].cam, false);
    ok(/cannot be used/.test(a.said.pop()));
    const mic = browser("m", d, { refuse: ["video"] });
    mic.call.start(); await settle();
    eq([mic.call.cam(), posted(mic, "start")[0].cam], [false, false], "the mic alone");
    ok(mic.tracks.some((t) => t.kind === "audio"));
  });

  await check("Mic with no mic yet asks again: refused, it says why; allowed, it goes to the others", async () => {
    const options = { refuse: ["audio"] };
    const { d, a } = await pair(options);
    ok(!a.tracks.some((t) => t.kind === "audio"), "joined with the camera alone");
    const asks = a.asked.length;
    a.call.toggleMic(); await settle();
    eq(a.asked.length, asks + 1, "asked once more, on the tap");
    eq(a.asked[a.asked.length - 1].video, false, "the mic alone");
    eq(a.said.pop(), "No mic can be used here (NotAllowedError).");
    eq(a.call.mic(), false);
    options.refuse = [];  // allowed in the settings, or let go by the other app
    a.call.toggleMic(); await settle();
    const mic = a.tracks.find((t) => t.kind === "audio");
    ok(mic && mic.enabled, "a mic, on");
    eq(a.call.mic(), true);
    eq(sender(a, "audio").track, mic, "sent to the other page");
    a.tick(300); await settle();
    eq(d.members[0].mic, true, "told the daemon");
    a.call.toggleMic();
    eq([a.call.mic(), mic.enabled], [false, false], "a tap now mutes it, as ever");
    eq(a.asked.length, asks + 2);
  });

  await check("a mic taken while on: Mic mutes it first; an answer after End is let go", async () => {
    const { d, a } = await pair();
    a.call.toggleMic();
    const taken = a.tracks.find((t) => t.kind === "audio");
    taken.readyState = "ended";  // another app has it now
    const asks = a.asked.length;
    a.call.toggleMic(); await settle();
    eq([a.call.mic(), a.asked.length], [false, asks], "muted, nothing asked");
    eq(sender(a, "audio").track, null);
    a.tick(300); await settle();
    eq(d.members[0].mic, false, "told the daemon");
    // Asked again, the answer comes only after End and a new Call: it is let go.
    let answer;
    const m = a.window.navigator.mediaDevices, real = m.getUserMedia;
    m.getUserMedia = () => new Promise((r) => { answer = r; });
    a.call.toggleMic();
    m.getUserMedia = real;
    a.call.end(); await settle();
    a.call.start(); await settle();
    const late = new Track("audio", "late mic");
    answer(new MediaStream([late]));
    await settle();
    ok(late.stopped, "let go");
    eq(a.call.mic(), false, "the new call's mic stays off");
  });

  await check("a daemon that comes back behind the page's version is followed, and never in a loop", async () => {
    const d = daemon();
    const a = browser("a", d);
    d.v = 10;
    a.poll();
    eq(a.call.query(), ["cv=10", "cm=0"]);
    // An answer that came late (older than one already taken) is no news.
    eq(a.call.update(Object.assign(d.state("a"), { v: 9 })), false, "late: no news, no wait at once");
    // A crash lost the last changes: the daemon is back with fewer, and a boot of its own.
    d.v = 6; d.boot = "b2";
    eq(a.poll(), true, "the new daemon's call is news once");
    eq(a.call.query(), ["cv=6", "cm=0"], "its version from now on: the waits are held again");
    eq(a.poll(), false);
  });

  await check("Join on a call that ended while the browser asked for the camera makes no new call", async () => {
    const d = daemon();
    const a = browser("a", d), b = browser("b", d);
    a.call.start(); await settle();
    b.poll();
    ok(b.call.ringing());
    d.id = ""; d.members = []; d.v += 1;  // a ended it, before b's answer to the prompt came
    b.call.start(); await settle();
    eq(posted(b, "start")[0].call, "1", "b meant call 1");
    eq([b.call.inCall(), b.call.running(), d.id], [false, false, ""], "no call started by a Join");
    eq(b.said.pop(), "The call ended.");
    ok(b.tracks.every((t) => t.stopped), "the camera let go");
    const c = browser("c", d);
    c.call.start(); await settle();
    eq([posted(c, "start")[0].call, d.id], ["", "2"], "Call with none running: a new one");
  });

  await check("an offer whose post got no answer goes again, while it is still wanted", async () => {
    const d = daemon();
    const b = browser("b", d);
    b.call.start(); await settle();
    const a = browser("a", d, { hold: true });
    a.poll();
    a.call.start(); await settle();
    a.posts[0].respond(); await settle();  // a is in; it offers to b
    const sends = () => a.posts.filter((x) => x.body.do === "send");
    eq(sends().length, 1);
    sends()[0].fail();
    a.tick(1000); await settle();
    eq(sends().length, 2, "sent again");
    eq(sends()[1].body.data, sends()[0].body.data);
    sends()[1].fail(); a.tick(1000); await settle();
    sends()[2].fail(); a.tick(1000); await settle();
    eq(sends().length, 3, "three times at most");
    sends()[2].respond(); await settle();
  });

  await check("an offer too long for the daemon leaves out host addresses, IPv6 first", async () => {
    const d = daemon();
    const b = browser("b", d);
    b.call.start(); await settle();
    const a = browser("a", d);
    const lines = ["v=0", "a=x-pad:",
      "a=candidate:1 1 udp 2122260223 192.168.1.5 50000 typ host",
      "a=candidate:2 1 udp 2122262783 2001:db8::1 50001 typ host",
      "a=candidate:3 1 udp 2122262783 2001:db8::2 50002 typ host",
      "a=candidate:4 1 udp 1686052607 203.0.113.7 50000 typ srflx raddr 192.168.1.5 rport 50000", ""];
    lines[1] += "p".repeat(12050 - lines.join("\r\n").length);  // 50 past the limit
    const long = lines.join("\r\n");
    a.world.pcs.push = function (pc) {
      Array.prototype.push.call(this, pc);
      pc.setLocalDescription = function (desc) {
        this.localDescription = { type: desc.type, sdp: long };
        this.signalingState = "have-local-offer";
        this.iceGatheringState = "complete";
        return Promise.resolve();
      };
    };
    a.poll();
    a.call.start(); await settle();
    const sent = posted(a, "send")[0].data;
    eq(long.length, 12050);
    ok(sent.length <= 12000, "it fits: " + sent.length);
    ok(!/2001:db8::2/.test(sent) && /2001:db8::1/.test(sent), "an IPv6 host address went, the last: one was enough");
    ok(/192\.168\.1\.5 50000 typ host/.test(sent) && /typ srflx/.test(sent), "the rest stay");
    eq(sent.slice(-2), "\r\n");
  });

  finish();
})();
