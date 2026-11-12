// Runs the real dashboard page and script in node, against a stand-in API, and checks what
// it shows and what it sends, with the small DOM of dom.js. Usage: node dashboard_test.js
// PAGE SCRIPT
"use strict";
const fs = require("fs");
const vm = require("vm");
const { Node, parse, newDocument, settle, runner, eq, ok, text } = require("./dom.js");

const [pagePath, scriptPath] = process.argv.slice(2);
const PAGE = fs.readFileSync(pagePath, "utf8");
const SCRIPT = fs.readFileSync(scriptPath, "utf8");

// --- one page load, with a stand-in API

function load(answers) {
  const root = parse(PAGE);
  const sent = [];
  const document = newDocument(root);
  const timers = [];
  const intervals = [];
  const fetch = (url, options) => {
    const route = url.replace("/api/v1/", "");
    const body = options && options.body ? JSON.parse(options.body) : undefined;
    sent.push({ method: options.method, route, body });
    let answer = answers[route];
    if (typeof answer === "function") { answer = answer(body); }
    if (answer === undefined) { return Promise.reject(new Error("no answer for " + route)); }
    const status = answer.status || 200;
    const data = answer.data || answer;
    return Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(data) });
  };
  const window = {
    localStorage: { getItem: () => null, setItem() {} },
    isSecureContext: false,
    setInterval: (fn) => { intervals.push(fn); return intervals.length; },
    setTimeout: (fn) => { timers.push(fn); return 0; },
    clearTimeout() {},
  };
  const context = { window, document, fetch, Node, navigator: {}, console, Date, JSON, Math,
    Object, Array, String, Number, Error, Promise };
  window.window = window;
  vm.createContext(context);
  vm.runInContext(SCRIPT, context);
  return { root, sent, $: (id) => root.querySelector("#" + id), posts: () => sent.filter((s) => s.method === "POST"),
    refresh: () => intervals.forEach((fn) => fn()) };
}


// --- what the daemon says

const NOW = 1000000;
function status(over) {
  return Object.assign({
    version: "9.9.9", now: NOW, stream: null, stream_port: 1337, stream_port_state: "closed",
    firewall_problem: null, prepare: [], prepare_run: "r",
  }, over || {});
}
function stream(over) {
  return Object.assign({
    kind: "dir", source: "/m/show", alias: "show", url: "http://h:1337/show", guessers: [],
    mode: "browser", policy: "approval", unlocked_policy: "approval", started: NOW - 600,
    expires: NOW + 600, seconds_left: 600,
    videos: [
      { number: 1, name: "E01 <b>bold</b>.mkv", url: "http://h:1337/show/1", ready: true, preparing: false, note: null, job: null },
      { number: 2, name: "E02.mkv", url: "http://h:1337/show/2", ready: false, preparing: false, note: null,
        job: { state: "running", progress: 0.5 } },
      { number: 3, name: "E03.mkv", url: "http://h:1337/show/3", ready: false, preparing: false, note: "slow seek", job: null },
    ],
    devices: [
      { id: "b-0a0a0a", kind: "browser", ip: "192.0.2.1", name: "", allowed: null, passes: false, wl: true, code: "4821", user_agent: "JioSphere/2 Chrome/136",
        requests: 3, blocked: 3, bytes: 0, last_seen: NOW - 2 },
      { id: "192.0.2.2", kind: "player", ip: "192.0.2.2", name: "", allowed: true, passes: true, wl: false, code: "", user_agent: "VLC/3.0.25",
        requests: 90, blocked: 0, bytes: 2e9, last_seen: NOW - 5 },
    ],
  }, over || {});
}
const job = (id, state, over) => Object.assign({ id, source: "/m/show/E02.mkv", name: "E02.mkv",
  task: "base version (copy)", kind: "base", sub: 0, state, progress: 0.5, seconds_left: 60, error: "" }, over || {});

// --- the checks

const { check, finish } = runner();

(async () => {
  await check("a live stream: alias, address, time left, videos and their states", async () => {
    const page = load({ status: status({ stream: stream(), stream_port_state: "open" }) });
    await settle();
    eq(page.$("idle").hidden, true); eq(page.$("live").hidden, false);
    eq(text(page.$("aliasname")), "/show"); eq(text(page.$("url")), "http://h:1337/show");
    eq(text(page.$("left")), "10m 00s"); eq(text(page.$("port")), "1337 open");
    eq(text(page.$("viewers")), "1", "only the device that gets in and asked lately");
    const items = page.$("videos").querySelectorAll(".item");
    eq(items.length, 3);
    ok(text(items[0]).includes("Ready") && text(items[1]).includes("Preparing 50%"), text(items[1]));
    ok(text(items[2]).includes("Not ready") && text(items[2]).includes("slow seek"));
    ok(items[1].querySelector(".progress"), "a progress bar for the video being prepared");
    // A file name is text, never markup.
    ok(text(items[0]).includes("E01 <b>bold</b>.mkv") && !items[0].querySelector("b"));
  });

  await check("a waiting device shows its code and is let in with one press", async () => {
    const page = load({ status: status({ stream: stream() }), devices: status({ stream: stream() }) });
    await settle();
    eq(page.$("waitcard").hidden, false); eq(text(page.$("waitbadge")), "1");
    const waiter = page.$("waiters").querySelector(".item");
    ok(text(waiter).includes("4821") && text(waiter).includes("JioSphere"));
    Array.from(waiter.querySelectorAll("button")).find((b) => text(b) === "Let in").click();
    await settle();
    eq(page.posts(), [{ method: "POST", route: "devices", body: { device: "b-0a0a0a", allowed: true } }]);
    const other = page.$("devices").querySelectorAll(".item");
    eq(other.length, 1, "the waiting one is not listed twice");
    ok(text(other[0]).includes("2.0 GB sent") && text(other[0]).includes("watching now"));
    Array.from(other[0].querySelectorAll("button")).find((b) => text(b) === "Deny").click();
    await settle();
    eq(page.posts()[1].body, { device: "192.0.2.2", allowed: false });
  });

  await check("a waiting device can be denied", async () => {
    const page = load({ status: status({ stream: stream() }), devices: status({ stream: stream() }) });
    await settle();
    Array.from(page.$("waiters").querySelectorAll("button")).find((b) => text(b) === "Deny").click();
    await settle();
    eq(page.posts(), [{ method: "POST", route: "devices", body: { device: "b-0a0a0a", allowed: false } }]);
  });

  await check("policies: switch, lock and unlock", async () => {
    let s = stream({ policy: "open" });
    const page = load({ status: () => status({ stream: s }), "stream/policy": (b) => { s = stream({ policy: b.policy }); return status({ stream: s }); },
      "stream/unlock": () => { s = stream({ policy: "open" }); return status({ stream: s }); } });
    await settle();
    page.$("policies").querySelector('[data-policy="locked"]').click();
    await settle();
    eq(page.posts()[0], { method: "POST", route: "stream/policy", body: { policy: "locked" } });
    eq(text(page.$("policies").querySelector('[data-policy="locked"]')), "Unlock");
    page.$("policies").querySelector('[data-policy="locked"]').click();
    await settle();
    eq(page.posts()[1].route, "stream/unlock");
    page.$("policies").querySelector('[data-policy="open"]').click();  // already open: nothing sent
    await settle();
    eq(page.posts().length, 2);
  });

  await check("start: the form sends what was chosen, and an empty time limit means the default", async () => {
    const page = load({ status: status(), "stream/start": status({ stream: stream() }) });
    await settle();
    eq(page.$("idle").hidden, false);
    page.$("kind").querySelector('[data-value="dir"]').click();
    page.$("mode").querySelector('[data-value="browser"]').click();
    page.$("path").value = " /m/show ";
    page.$("ttl").value = "";
    page.$("audio").value = "jpn";
    page.$("sub").value = "1";
    page.$("start").dispatch("submit");
    await settle();
    eq(page.posts()[0].body, { ttl: null, mode: "browser", dir: "/m/show", audio: "jpn", sub: "1" });
    eq(page.$("live").hidden, false);
  });

  await check("start in direct mode sends no audio or subtitles, and hides those fields", async () => {
    const page = load({ status: status(), "stream/start": status({ stream: stream() }) });
    await settle();
    page.$("audio").value = "jpn";
    page.$("path").value = "/m/film.mkv";
    page.$("ttl").value = "60";
    page.$("alias").value = "film";
    eq(Array.from(page.root.querySelectorAll("label.browser")).every((l) => l.hidden), true);
    page.$("start").dispatch("submit");
    await settle();
    eq(page.posts()[0].body, { ttl: 60, mode: "direct", file: "/m/film.mkv", alias: "film" });
  });

  await check("a transcode is asked about, and sent again confirmed only on yes", async () => {
    let asked = 0;
    const page = load({ status: status(), "stream/start": (b) => {
      asked += 1;
      return b.confirmed ? status({ stream: stream() }) : { status: 400, data: { error: "must be transcoded",
        code: "confirm", plans: [{ name: "f.mkv", seconds: 3600, duration: 6000, tasks: [{ task: "t" }] }] } };
    } });
    await settle();
    page.$("path").value = "/m/f.mkv";
    page.$("start").dispatch("submit");
    await settle();
    eq(page.$("modal").hidden, false);
    ok(text(page.$("modaltext")).includes("must be transcoded") && text(page.$("modaltext")).includes("f.mkv: about 1h 00m"));
    page.$("modalno").click();
    eq(asked, 1); eq(page.$("modal").hidden, true);
    page.$("start").dispatch("submit");
    await settle();
    page.$("modalyes").click();
    await settle();
    eq(asked, 3); eq(page.posts()[2].body.confirmed, true);
  });

  await check("a refusal is shown, and the daemon not answering is shown", async () => {
    const page = load({ status: status({ stream: stream() }), "stream/alias": { status: 400, data: { error: "the alias 'caps' is reserved" } } });
    await settle();
    page.$("newalias").value = "caps";
    page.$("setalias").click();
    await settle();
    ok(text(page.$("toasts")).includes("reserved"), text(page.$("toasts")));
    page.$("newalias").value = "";
    page.$("setalias").click();  // an empty alias is not sent
    await settle();
    eq(page.posts().length, 1);
    const down = load({});
    await settle();
    eq(text(down.$("livetext")), "the daemon does not answer"); eq(down.$("dot").className, "dot off");
  });

  await check("stop asks first, then stops", async () => {
    const page = load({ status: status({ stream: stream() }), "stream/stop": status() });
    await settle();
    page.$("stop").click();
    eq(page.posts().length, 0); eq(page.$("modal").hidden, false);
    page.$("modalyes").click();
    await settle();
    eq(page.posts()[0].route, "stream/stop"); eq(page.$("idle").hidden, false);
  });

  await check("a prepare shows its plan first, then queues it", async () => {
    const plan = { source: "/m/f.mkv", name: "f.mkv", duration: 6000, seconds: 4000, longer_than_film: false, unknown: false,
      tasks: [{ task: "subtitle 2 (burned in)", seconds: 4000 }], notes: ["subtitle 9: not in this file"],
      tracks: [{ kind: "subtitle", number: 2, label: "Full", treatment: "burned in" }] };
    const page = load({ status: status(), prepared: { prepared: [] },
      prepare: (b) => Object.assign(status(), { plans: [plan], jobs: b.dry_run ? [] : [5] }) });
    await settle();
    page.$("pkind").querySelector('[data-value="file"]').click();
    page.$("ppath").value = "/m/f.mkv";
    page.$("psub").value = "2,9";
    page.$("prepareform").dispatch("submit");
    await settle();
    eq(page.posts()[0].body, { dry_run: true, file: "/m/f.mkv", sub: "2,9" });
    eq(page.$("plan").hidden, false);
    const shown = text(page.$("plans"));
    ok(shown.includes("subtitle 2 (burned in)") && shown.includes("subtitle 9: not in this file") && shown.includes("S2 Full · burned in"), shown);
    page.$("queue").click();
    await settle();
    eq(page.posts()[1].body, { file: "/m/f.mkv", sub: "2,9", confirmed: true });
    eq(page.$("plan").hidden, true);
  });

  await check("a plan with nothing to make cannot be queued; the quick picks fill the field", async () => {
    const plan = { name: "f.mkv", duration: 60, seconds: 0, longer_than_film: false, unknown: false, tasks: [], notes: [], tracks: [] };
    const page = load({ status: status(), prepared: { prepared: [] }, prepare: Object.assign(status(), { plans: [plan], jobs: [] }) });
    await settle();
    page.root.querySelector('#prepareform .chips').querySelector('[data-sub="none"]').click();
    eq(page.$("psub").value, "none");
    page.$("ppath").value = "/m/f.mkv";
    page.$("prepareform").dispatch("submit");
    await settle();
    eq(page.$("queue").disabled, true);
    ok(text(page.$("plans")).includes("Nothing to make"));
  });

  await check("prepares: progress, cancel, and every finished one on request", async () => {
    const finished = Array.from({ length: 12 }, (_, i) => job(10 + i, i === 0 ? "failed" : "done", { error: i === 0 ? "no room" : "" }));
    const page = load({ status: status({ prepare: [job(1, "running"), job(2, "queued")].concat(finished) }),
      "prepare/cancel": status() });
    await settle();
    eq(text(page.$("jobbadge")), "2");
    let items = page.$("jobs").querySelectorAll(".item");
    eq(items.length, 2 + 8);
    ok(text(items[0]).includes("50%, about 1m 00s left") && items[0].querySelector(".progress"));
    ok(text(items[2]).includes("failed: no room"));
    const more = Array.from(page.$("jobs").querySelectorAll("button")).find((b) => text(b) === "Show 4 more");
    ok(more, "a button to show the rest");
    more.click();
    eq(page.$("jobs").querySelectorAll(".item").length, 14);
    Array.from(page.$("jobs").querySelectorAll("button")).find((b) => text(b) === "Show fewer").click();
    eq(page.$("jobs").querySelectorAll(".item").length, 10);
    items = page.$("jobs").querySelectorAll(".item");
    Array.from(items[0].querySelectorAll("button")).find((b) => text(b) === "Cancel").click();
    await settle();
    eq(page.posts()[0], { method: "POST", route: "prepare/cancel", body: { path: "/m/show/E02.mkv" } });
  });

  await check("the cache: what is prepared, and remove asks first; one in use cannot be removed", async () => {
    const rows = [
      { key: "a", source: "/m/a.mkv", stale: false, base: "copy", soft: [1], burn: [3], bytes: 2e9, in_use: true, busy: false },
      { key: "b", source: "/m/b.mkv", stale: true, base: "", soft: [], burn: [], bytes: 1e6, in_use: false, busy: false },
      { key: "c", source: "/m/c.mkv", stale: false, unchecked: true, base: "copy", soft: [], burn: [], bytes: 1e6, in_use: false, busy: false },
    ];
    const page = load({ status: status(), prepared: { prepared: rows }, "prepared/rm": Object.assign(status(), { removed: 1, kept: [] }) });
    await settle();
    page.$("tabs").querySelector('[data-tab="prepare"]').click();
    await settle();
    eq(page.root.querySelector('[data-panel="prepare"]').hidden, false);
    eq(page.root.querySelector('[data-panel="stream"]').hidden, true);
    const items = page.$("cache").querySelectorAll(".item");
    ok(text(items[0]).includes("base copy") && text(items[0]).includes("soft 1") && text(items[0]).includes("burned in 3") && text(items[0]).includes("in use"));
    ok(!items[0].querySelectorAll("button").length, "no Remove for one in use");
    ok(text(items[1]).includes("stale") && text(items[1]).includes("no base"));
    ok(text(items[2]).includes("cannot check") && !text(items[2]).includes("stale"), text(items[2]));
    eq(text(page.$("cachetotal")), "2.0 GB");
    items[1].querySelector("button").click();
    eq(page.posts().length, 0);
    page.$("modalyes").click();
    await settle();
    eq(page.posts()[0].body, { path: "/m/b.mkv" });
    page.$("rmstale").click();
    await settle();
    eq(page.posts()[1].body, { stale: true });
  });

  await check("firewall problems and guessers are shown", async () => {
    const page = load({ status: status({ firewall_problem: "ufw failed", stream: stream({ guessers: ["198.51.100.9"] }) }) });
    await settle();
    const notes = text(page.$("notes"));
    ok(notes.includes("Firewall: ufw failed") && notes.includes("198.51.100.9"), notes);
  });

  // --- viewers

  const MENU = { audio: [{ value: "1", label: "English" }, { value: "2", label: "Japanese" }],
    subs: [{ value: "1-soft", label: "English" }, { value: "3-burn", label: "Signs (burned in)" }] };
  const MENU2 = { audio: [{ value: "1", label: "English" }], subs: [{ value: "2-soft", label: "Français" }] };
  function record(over) {
    return Object.assign({ video: 1, position: 610, duration: 5400, paused: false, audio: 2, sub: "off",
      reported: NOW - 1, positions: {}, start_at: null, pending: {} }, over || {});
  }
  function client(over) {
    const c = record(over);
    c.active = NOW - c.reported <= 10;  // as the daemon says it
    return c;
  }
  function viewer(id, name, over, client_) {
    return Object.assign({ id, kind: "browser", ip: "192.0.2.30", name, allowed: true, passes: true, wl: true,
      code: "1111", user_agent: "JioSphere/2", requests: 9, blocked: 1, bytes: 1e6, last_seen: NOW - 1,
      client: client_ === undefined ? client() : client_ }, over || {});
  }
  function watched(devices) {
    const s = stream({ devices });
    s.videos[0].menu = MENU; s.videos[1].menu = MENU2; s.videos[2].menu = { audio: [], subs: [] };
    return s;
  }

  await check("viewers: who plays what, where, with which tracks, and what waits for them", async () => {
    const devices = [
      viewer("b-000001", "Living-room TV", {}, client({ pending: { seek: 3723, audio: 0 }, start_at: [2, 65] })),
      viewer("b-000002", "", { user_agent: "Mozilla/5.0 Chrome/120" }, client({ video: 2, paused: true, audio: 1, sub: "2-soft", position: 5, duration: 0 })),
      viewer("b-000003", "Gone", {}, client({ reported: NOW - 3600 })),
      viewer("b-000004", "New", {}, null),
      viewer("b-000005", "Waiting", { allowed: null, passes: false }),
      { id: "192.0.2.2", kind: "player", ip: "192.0.2.2", name: "", allowed: true, passes: true, wl: false, code: "",
        user_agent: "VLC/3", requests: 1, blocked: 0, bytes: 0, last_seen: NOW, client: null },
    ];
    const page = load({ status: status({ stream: watched(devices) }) });
    await settle();
    const items = page.$("clients").querySelectorAll(".item");
    eq(items.length, 4, "browsers that get in only: not the waiting one, not VLC");
    eq(text(page.$("viewerbadge")), "2", "the two that report now");
    const tv = text(items[0]);
    ok(tv.includes("Living-room TV") && tv.includes("Playing") && tv.includes("1. E01 <b>bold</b>.mkv"), tv);
    ok(tv.includes("10:10 / 1:30:00") && tv.includes("Audio: Japanese") && tv.includes("Subtitles: Off"), tv);
    ok(tv.includes("asked: seek to 1:02:03") && tv.includes("asked: audio Off") && tv.includes("next start: video 2 at 1:05"), tv);
    ok(items[0].querySelector(".progress"), "how far it got");
    const phone = text(items[1]);
    ok(phone.includes("Chrome/120") && phone.includes("Paused") && phone.includes("Audio: English") && phone.includes("Subtitles: Français"), phone);
    ok(!items[1].querySelector(".progress"), "no bar without a duration");
    ok(text(items[2]).includes("Left 1h 00m ago"), text(items[2]));
    ok(text(items[3]).includes("Not playing yet"), text(items[3]));
    eq(page.$("clientform").hidden, true, "nothing to change until one is ticked");
  });

  await check("viewers: tick some, then every change is sent for those ticked", async () => {
    const devices = [viewer("b-000001", "TV"), viewer("b-000002", "Phone", {}, client({ video: 2, audio: 1 })), viewer("b-000003", "Other")];
    const answer = () => status({ stream: watched(devices) });
    // As the daemon does: what was asked for waits in each record, as the player will take it.
    const asked = (body) => {
      devices.filter((d) => body.devices.includes(d.id)).forEach((d) => {
        if ("audio" in body) { d.client.pending.audio = body.audio === "off" ? 0 : Number(body.audio); }
        if ("sub" in body) { d.client.pending.sub = body.sub; }
      });
      return answer();
    };
    const page = load({ status: answer, clients: asked });
    await settle();
    page.$("tabs").querySelector('[data-tab="viewers"]').click();
    let items = page.$("clients").querySelectorAll(".item");
    items[0].click();
    items = page.$("clients").querySelectorAll(".item");
    items[1].click();
    eq(page.$("clientform").hidden, false); eq(text(page.$("tickedcount")), "2 ticked");
    // The lists hold the tracks of both videos, each once, and Off.
    const options = (id) => Array.from(page.$(id).querySelectorAll("option")).map((o) => o.getAttribute("value") || o.value);
    eq(options("caudio"), ["", "1", "2", "off"]);
    eq(options("csub"), ["", "1-soft", "3-burn", "2-soft", "off"]);
    // Each list shows what the ticked viewers play with: Choose... where they differ.
    eq(page.$("caudio").value, "", "Japanese on the TV, English on the phone");
    eq(page.$("csub").value, "off", "both without subtitles");
    page.$("caudio").focus();
    page.$("caudio").value = "2";
    page.$("caudio").dispatch("change");
    await settle();
    eq(page.posts()[0], { method: "POST", route: "clients", body: { audio: "2", devices: ["b-000001", "b-000002"] } });
    eq(page.$("caudio").value, "2", "what was chosen stays shown");
    page.$("csub").value = "3-burn";
    page.$("csub").dispatch("change");
    await settle();
    eq(page.posts()[1].body, { sub: "3-burn", devices: ["b-000001", "b-000002"] });
    page.refresh();
    await settle();
    eq([page.$("caudio").value, page.$("csub").value], ["2", "3-burn"], "and through the next status");
    // Ticking only the TV shows what the TV plays with.
    Array.from(page.$("clients").querySelectorAll(".item"))[1].click();
    eq(text(page.$("tickedcount")), "1 ticked");
    eq(page.$("caudio").value, "2");
    // A list that is being used is left as the viewer set it.
    page.$("csub").focus();
    page.$("csub").value = "off";
    page.refresh();
    await settle();
    eq(page.$("csub").value, "off");
    page.$("csub").blur();
    Array.from(page.$("clients").querySelectorAll(".item"))[1].click();  // both again
    page.$("seeknow").click();
    eq(page.posts().length, 2, "no position, nothing sent");
    eq(page.$("cat").getAttribute("inputmode"), null, "a keyboard that has a colon (Android's number pad has none)");
    eq(page.$("cat").getAttribute("type"), "text");
    page.$("cat").value = " 10:10 ";
    page.$("seeknow").click();
    await settle();
    eq(page.posts()[2].body, { seek: "10:10", devices: ["b-000001", "b-000002"] });
    page.$("startat").click();
    await settle();
    eq(page.posts()[3].body, { start_at: "10:10", devices: ["b-000001", "b-000002"] });
    page.$("cname").value = "Kids";
    page.$("rename").click();
    await settle();
    eq(page.posts()[4].body, { name: "Kids", devices: ["b-000001", "b-000002"] });
    // The ticks stay through the next status.
    eq(page.$("clients").querySelectorAll(".item.ticked").length, 2);
  });

  await check("viewers: the lists start on what the ticked viewers play with", async () => {
    const devices = [viewer("b-000001", "TV", {}, client({ audio: 1, sub: "1-soft" })),
      viewer("b-000002", "Phone", {}, client({ audio: 1, sub: "off", pending: { sub: "1-soft", audio: 0 } }))];
    const page = load({ status: status({ stream: watched(devices) }) });
    await settle();
    page.$("tickall").click();
    eq(page.$("csub").value, "1-soft", "what waits counts: the phone was asked for English");
    eq(page.$("caudio").value, "", "the phone was asked for Off, the TV plays English");
    Array.from(page.$("clients").querySelectorAll(".item"))[1].click();
    eq(page.$("caudio").value, "1", "the TV alone");
  });

  await check("viewers: tick all, untick all; a viewer that is gone loses its tick", async () => {
    let devices = [viewer("b-000001", "TV"), viewer("b-000002", "Phone")];
    const page = load({ status: () => status({ stream: watched(devices) }), clients: () => status({ stream: watched(devices) }) });
    await settle();
    page.$("tickall").click();
    eq(page.$("clients").querySelectorAll(".item.ticked").length, 2); eq(text(page.$("tickall")), "Untick all");
    devices = [viewer("b-000002", "Phone")];
    page.$("cname").value = "x";
    page.$("rename").click();  // its answer has one viewer left
    await settle();
    eq(page.posts()[0].body.devices, ["b-000001", "b-000002"]);
    eq(text(page.$("tickedcount")), "1 ticked");
    page.$("tickall").click();
    eq(page.$("clients").querySelectorAll(".item.ticked").length, 0, "the one left was all of them: untick");
    eq(text(page.$("tickall")), "Tick all"); eq(page.$("clientform").hidden, true);
    page.$("tickall").click();
    eq(page.$("clients").querySelectorAll(".item.ticked").length, 1);
  });

  await check("viewers: the lists keep what is chosen while the status refreshes", async () => {
    const devices = [viewer("b-000001", "TV")];
    const page = load({ status: () => status({ stream: watched(devices) }) });
    await settle();
    page.$("clients").querySelector(".item").click();
    const select = page.$("caudio");
    const before = select.querySelectorAll("option")[1];
    // A refresh with the same tracks leaves the list as it is (a phone's open picker stays).
    page.refresh();
    await settle();
    ok(select.querySelectorAll("option")[1] === before, "not rebuilt");
  });

  await check("viewers: a refused change is shown", async () => {
    const devices = [viewer("b-000001", "TV")];
    const page = load({ status: status({ stream: watched(devices) }), clients: { status: 400, data: { error: "not a time: 'x'" } } });
    await settle();
    page.$("clients").querySelector(".item").click();
    page.$("cat").value = "x";
    page.$("seeknow").click();
    await settle();
    ok(text(page.$("toasts")).includes("not a time"), text(page.$("toasts")));
  });

  await check("viewers: in direct mode and with no stream, the tab says why it is empty", async () => {
    const direct = load({ status: status({ stream: stream({ mode: "direct" }) }) });
    await settle();
    ok(text(direct.$("clients")).includes("Direct mode"));
    const idle = load({ status: status() });
    await settle();
    ok(text(idle.$("clients")).includes("No stream") && idle.$("viewerbadge").hidden);
  });

  await check("devices: a browser shows its name, its id, and is decided by id", async () => {
    const devices = [viewer("b-000001", "Living-room TV", { allowed: true })];
    const page = load({ status: status({ stream: watched(devices) }), devices: status({ stream: watched(devices) }) });
    await settle();
    const item = page.$("devices").querySelector(".item");
    ok(text(item).includes("Living-room TV") && text(item).includes("browser b-000001") && text(item).includes("192.0.2.30"), text(item));
    Array.from(item.querySelectorAll("button")).find((b) => text(b) === "Deny").click();
    await settle();
    eq(page.posts()[0].body, { device: "b-000001", allowed: false });
    ok(text(page.$("toasts")).includes("Shut out Living-room TV"));
  });

  await check("with nothing running, the other tabs say so", async () => {
    const page = load({ status: status() });
    await settle();
    ok(text(page.$("devices")).includes("No stream") && text(page.$("policytext")).includes("No stream"));
    ok(text(page.$("jobs")).includes("Nothing to do"));
  });

  finish();
})();
