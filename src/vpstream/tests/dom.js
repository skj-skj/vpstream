// A small DOM for running vpstream's own pages in node: enough for the dashboard and the
// player, nothing more. Node has no browser, and the package needs nothing from npm. Used by
// dashboard_test.js and player_test.js.
"use strict";

let document = null;  // the page loaded last, for focus()

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
  removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn); }
  dispatch(type, init) {
    let stopped = false;
    const event = Object.assign({ type, target: this, preventDefault() {}, stopPropagation() { stopped = true; } }, init);
    for (let node = this; node && !stopped; node = node.parentNode) {
      (node.listeners && node.listeners[type] || []).forEach((fn) => fn(event));
      if (type === "click" && node.onclick && node === this) { node.onclick(event); }
    }
    // Then the document's own listeners, as in a browser.
    if (!stopped && document && document.body && all(document.root).includes(this)) {
      (document.listeners[type] || []).forEach((fn) => fn(event));
    }
    return event;
  }
  click() { this.dispatch("click"); }
  focus() { document.activeElement = this; }
  blur() { if (document.activeElement === this) { document.activeElement = null; } }
  scrollIntoView() {}
  matches(selector) { return matches(this, selector); }
  closest(selector) {
    for (let node = this; node instanceof Element; node = node.parentNode) {
      if (matches(node, selector)) { return node; }
    }
    return null;
  }
  querySelectorAll(selector) { return new NodeList(all(this).filter((n) => n !== this && matches(n, selector))); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  get classes() { return (this.attrs.class || "").split(/\s+/).filter(Boolean); }
}
Object.entries(PROPS).forEach(([prop, attr]) => {
  Object.defineProperty(Element.prototype, prop, {
    get() { return this.attrs[attr] || ""; },
    set(v) { this.attrs[attr] = String(v); },
  });
});

// What querySelectorAll gives in a browser: indexes, length, item, forEach and nothing more,
// so a page script that calls .some or .map on it fails here as it would there.
class NodeList {
  constructor(nodes) { nodes.forEach((n, i) => { this[i] = n; }); this.length = nodes.length; }
  item(i) { return this[i] || null; }
  forEach(fn) { for (let i = 0; i < this.length; i++) { fn(this[i], i, this); } }
  [Symbol.iterator]() { return Array.prototype.values.call(this); }
}

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
        if (attrs.type === "application/json") { el.appendChild(new Text(html.slice(token.lastIndex, end))); }
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


// The document object of a parsed page.
function newDocument(root) {
  document = {
    root,
    hidden: false,
    activeElement: null,
    body: root.querySelector("body"),
    getElementById: (id) => root.querySelector("#" + id),
    querySelector: (s) => root.querySelector(s),
    querySelectorAll: (s) => root.querySelectorAll(s),
    createElement: (tag) => new Element(tag),
    createTextNode: (text) => new Text(text),
    listeners: {},
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
  };
  return document;
}

// Lets the promises and callbacks that are due run.
const settle = () => new Promise((r) => setImmediate(r)).then(() => new Promise((r) => setImmediate(r)));

// --- the checks

function runner() {
  const results = [];
  return {
    async check(name, fn) {
      try { await fn(); results.push(["pass", name]); } catch (e) { results.push(["FAIL", name + ": " + (e.stack || e)]); }
    },
    finish() {
      results.forEach(([state, name]) => console.log(state + " " + name));
      process.exit(results.some(([state]) => state === "FAIL") ? 1 : 0);
    },
  };
}
function eq(a, b, what) {
  const sa = JSON.stringify(a), sb = JSON.stringify(b);
  if (sa !== sb) { throw new Error((what || "") + " " + sa + " != " + sb); }
}
function ok(v, what) { if (!v) { throw new Error(what || "expected true"); } }
const text = (node) => node.textContent.replace(/\s+/g, " ").trim();

module.exports = { Node, Text, Element, parse, newDocument, settle, runner, eq, ok, text };
