// Runs the real dashboard page and script in node, against a stand-in API, and checks what
// it shows and what it sends. A small DOM of its own, enough for this page: node has no
// browser, and the package needs nothing from npm. Usage: node dashboard_test.js PAGE SCRIPT
"use strict";
const fs = require("fs");
const vm = require("vm");

const [pagePath, scriptPath] = process.argv.slice(2);
const PAGE = fs.readFileSync(pagePath, "utf8");
const SCRIPT = fs.readFileSync(scriptPath, "utf8");

// --- a small DOM

class Node {
  constructor() { this.childNodes = []; this.parentNode = null; }
  get firstChild() { return this.childNodes[0] || null; }
  appendChild(child) {
    if (child.parentNode) { child.parentNode.removeChild(child); }
    child.parentNode = this;
    this.childNodes.push(child);
    return child;
  }
  removeChild(child) {
    this.childNodes = this.childNodes.filter((c) => c !== child);
    child.parentNode = null;
    return child;
  }
  remove() { if (this.parentNode) { this.parentNode.removeChild(this); } }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(""); }
  set textContent(text) {
    this.childNodes.forEach((c) => { c.parentNode = null; });
    this.childNodes = [];
    if (text !== "") { this.appendChild(new Text(String(text))); }
  }
}

class Text extends Node {
  constructor(data) { super(); this.data = data; }
  get textContent() { return this.data; }
  set textContent(text) { this.data = String(text); }
}

const PROPS = { className: "class", id: "id", type: "type", placeholder: "placeholder" };

class Element extends Node {
  constructor(tag, attrs) {
    super();
    this.tagName = tag.toUpperCase();
    this.nodeName = this.tagName;
    this.attrs = Object.assign({}, attrs || {});
    this.listeners = {};
    this.style = {};
    this.onclick = null;
    this._value = this.attrs.value || "";
  }
  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  get hidden() { return "hidden" in this.attrs; }
  set hidden(on) { if (on) { this.attrs.hidden = ""; } else { delete this.attrs.hidden; } }
  get disabled() { return "disabled" in this.attrs; }
  set disabled(on) { if (on) { this.attrs.disabled = ""; } else { delete this.attrs.disabled; } }
  get value() { return this._value; }
  set value(v) { this._value = String(v); }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  dispatch(type, init) {
    let stopped = false;
    const event = Object.assign({ type, target: this, preventDefault() {}, stopPropagation() { stopped = true; } }, init);
    for (let node = this; node && !stopped; node = node.parentNode) {
      (node.listeners && node.listeners[type] || []).forEach((fn) => fn(event));
      if (type === "click" && node.onclick && node === this) { node.onclick(event); }
    }
    return event;
  }
  click() { this.dispatch("click"); }
  focus() { document.activeElement = this; }
  scrollIntoView() {}
  matches(selector) { return matches(this, selector); }
  closest(selector) {
    for (let node = this; node instanceof Element; node = node.parentNode) {
      if (matches(node, selector)) { return node; }
    }
    return null;
  }
  querySelectorAll(selector) { return all(this).filter((n) => n !== this && matches(n, selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  get classes() { return (this.attrs.class || "").split(/\s+/).filter(Boolean); }
}
Object.entries(PROPS).forEach(([prop, attr]) => {
  Object.defineProperty(Element.prototype, prop, {
    get() { return this.attrs[attr] || ""; },
    set(v) { this.attrs[attr] = String(v); },
  });
});

function all(root) {
  const out = [];
  (function walk(node) {
    node.childNodes.forEach((c) => { if (c instanceof Element) { out.push(c); walk(c); } });
  })(root);
  return out;
}

// Selectors: compounds of tag, #id, .class and [attr] or [attr="value"], joined by spaces.
function compound(node, part) {
  const re = /^([a-z]+)?((?:#[\w-]+|\.[\w-]+|\[[\w-]+(?:="[^"]*")?\])*)$/i;
  const m = re.exec(part);
  if (!m) { throw new Error("selector not supported: " + part); }
  if (m[1] && node.tagName !== m[1].toUpperCase()) { return false; }
  const bits = m[2].match(/#[\w-]+|\.[\w-]+|\[[\w-]+(?:="[^"]*")?\]/g) || [];
  return bits.every((bit) => {
    if (bit[0] === "#") { return node.attrs.id === bit.slice(1); }
    if (bit[0] === ".") { return node.classes.includes(bit.slice(1)); }
    const a = /^\[([\w-]+)(?:="([^"]*)")?\]$/.exec(bit);
    return a[2] === undefined ? a[1] in node.attrs : node.attrs[a[1]] === a[2];
  });
}

function matches(node, selector) {
  const parts = selector.trim().split(/\s+/);
  if (!compound(node, parts[parts.length - 1])) { return false; }
  let at = node.parentNode;
  for (let i = parts.length - 2; i >= 0; i--) {
    while (at instanceof Element && !compound(at, parts[i])) { at = at.parentNode; }
    if (!(at instanceof Element)) { return false; }
    at = at.parentNode;
  }
  return true;
}

// The page's own HTML: tags, attributes, text. <style> and <script> bodies are skipped.
const VOID = new Set(["meta", "input", "br", "img", "link"]);
function parse(html) {
  const root = new Element("html");
  let at = root;
  const token = /<!--[\s\S]*?-->|<!doctype[^>]*>|<\/([a-z0-9]+)\s*>|<([a-z0-9]+)((?:\s+[\w-]+(?:="[^"]*")?)*)\s*\/?>|([^<]+)/gi;
  let m;
  while ((m = token.exec(html))) {
    if (m[1]) { if (at.parentNode) { at = at.parentNode; } continue; }
    if (m[2]) {
      const tag = m[2].toLowerCase();
      const attrs = {};
      (m[3].match(/[\w-]+(?:="[^"]*")?/g) || []).forEach((a) => {
        const [k, v] = a.split(/=(.*)/s);
        attrs[k] = v === undefined ? "" : v.slice(1, -1);
      });
      const el = new Element(tag, attrs);
      at.appendChild(el);
      if (tag === "style" || tag === "script") {
        const end = html.indexOf("</" + tag, token.lastIndex);
        token.lastIndex = html.indexOf(">", end) + 1;
      } else if (!VOID.has(tag)) {
        at = el;
      }
      continue;
    }
    if (m[4] && m[4].trim()) { at.appendChild(new Text(m[4].replace(/&#8722;/g, "-").replace(/&amp;/g, "&"))); }
  }
  return root;
}

// --- one page load, with a stand-in API

let document;
function load(answers) {
  const root = parse(PAGE);
  const sent = [];
  document = {
    hidden: false,
    activeElement: null,
    getElementById: (id) => root.querySelector("#" + id),
    querySelector: (s) => root.querySelector(s),
    querySelectorAll: (s) => root.querySelectorAll(s),
    createElement: (tag) => new Element(tag),
    createTextNode: (text) => new Text(text),
  };
  const timers = [];
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
    setInterval: () => 0,
    setTimeout: (fn) => { timers.push(fn); return 0; },
    clearTimeout() {},
  };
  const context = { window, document, fetch, Node, navigator: {}, console, Date, JSON, Math,
    Object, Array, String, Number, Error, Promise };
  window.window = window;
  vm.createContext(context);
  vm.runInContext(SCRIPT, context);
  return { root, sent, $: (id) => root.querySelector("#" + id), posts: () => sent.filter((s) => s.method === "POST") };
}

const settle = () => new Promise((r) => setImmediate(r)).then(() => new Promise((r) => setImmediate(r)));

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
      { ip: "192.0.2.1", allowed: null, passes: false, wl: true, code: "4821", user_agent: "JioSphere/2 Chrome/136",
        requests: 3, blocked: 3, bytes: 0, last_seen: NOW - 2 },
      { ip: "192.0.2.2", allowed: true, passes: true, wl: false, code: "", user_agent: "VLC/3.0.25",
        requests: 90, blocked: 0, bytes: 2e9, last_seen: NOW - 5 },
    ],
  }, over || {});
}
const job = (id, state, over) => Object.assign({ id, source: "/m/show/E02.mkv", name: "E02.mkv",
  task: "base version (copy)", kind: "base", sub: 0, state, progress: 0.5, seconds_left: 60, error: "" }, over || {});

// --- the checks

const results = [];
async function check(name, fn) {
  try { await fn(); results.push(["pass", name]); } catch (e) { results.push(["FAIL", name + ": " + (e.stack || e)]); }
}
function eq(a, b, what) {
  const sa = JSON.stringify(a), sb = JSON.stringify(b);
  if (sa !== sb) { throw new Error((what || "") + " " + sa + " != " + sb); }
}
function ok(v, what) { if (!v) { throw new Error(what || "expected true"); } }
const text = (node) => node.textContent.replace(/\s+/g, " ").trim();

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
    waiter.querySelectorAll("button").find((b) => text(b) === "Let in").click();
    await settle();
    eq(page.posts(), [{ method: "POST", route: "devices", body: { ip: "192.0.2.1", allowed: true } }]);
    const other = page.$("devices").querySelectorAll(".item");
    eq(other.length, 1, "the waiting one is not listed twice");
    ok(text(other[0]).includes("2.0 GB sent") && text(other[0]).includes("watching now"));
    other[0].querySelectorAll("button").find((b) => text(b) === "Deny").click();
    await settle();
    eq(page.posts()[1].body, { ip: "192.0.2.2", allowed: false });
  });

  await check("a waiting device can be denied", async () => {
    const page = load({ status: status({ stream: stream() }), devices: status({ stream: stream() }) });
    await settle();
    page.$("waiters").querySelectorAll("button").find((b) => text(b) === "Deny").click();
    await settle();
    eq(page.posts(), [{ method: "POST", route: "devices", body: { ip: "192.0.2.1", allowed: false } }]);
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
    eq(page.root.querySelectorAll("label.browser").every((l) => l.hidden), true);
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
    const more = page.$("jobs").querySelectorAll("button").find((b) => text(b) === "Show 4 more");
    ok(more, "a button to show the rest");
    more.click();
    eq(page.$("jobs").querySelectorAll(".item").length, 14);
    page.$("jobs").querySelectorAll("button").find((b) => text(b) === "Show fewer").click();
    eq(page.$("jobs").querySelectorAll(".item").length, 10);
    items = page.$("jobs").querySelectorAll(".item");
    items[0].querySelectorAll("button").find((b) => text(b) === "Cancel").click();
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

  await check("with nothing running, the other tabs say so", async () => {
    const page = load({ status: status() });
    await settle();
    ok(text(page.$("devices")).includes("No stream") && text(page.$("policytext")).includes("No stream"));
    ok(text(page.$("jobs")).includes("Nothing to do"));
  });

  results.forEach(([state, name]) => console.log(state + " " + name));
  process.exit(results.some(([state]) => state === "FAIL") ? 1 : 0);
})();
