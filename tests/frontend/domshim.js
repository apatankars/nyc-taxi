// Minimal DOM + browser shim, enough to run static/app.js under jsc.
// Not a browser: it builds a real element tree from index.html, supports the
// handful of selectors app.js uses, and records nothing else.

"use strict";

// ---------------------------------------------------------------- URLSearchParams
if (typeof URLSearchParams === "undefined") {
  globalThis.URLSearchParams = class {
    constructor(init) {
      this._pairs = [];
      if (init instanceof URLSearchParams) this._pairs = init._pairs.slice();
      else if (typeof init === "string" && init) {
        for (const part of init.replace(/^\?/, "").split("&")) {
          if (!part) continue;
          const i = part.indexOf("=");
          this._pairs.push(i < 0 ? [part, ""]
            : [decodeURIComponent(part.slice(0, i)),
               decodeURIComponent(part.slice(i + 1))]);
        }
      }
    }
    append(k, v) { this._pairs.push([String(k), String(v)]); }
    set(k, v) {
      this._pairs = this._pairs.filter(([key]) => key !== String(k));
      this.append(k, v);
    }
    get(k) { const hit = this._pairs.find(([key]) => key === String(k));
             return hit ? hit[1] : null; }
    getAll(k) { return this._pairs.filter(([key]) => key === String(k))
                                 .map(([, v]) => v); }
    toString() {
      return this._pairs.map(([k, v]) =>
        encodeURIComponent(k) + "=" + encodeURIComponent(v)).join("&");
    }
  };
}

// ---------------------------------------------------------------- elements
let nextId = 0;

class Node {
  constructor(tag) {
    this.tagName = String(tag || "").toUpperCase();
    this.localName = String(tag || "").toLowerCase();
    this.children = [];
    this.parentNode = null;
    this.attributes = {};
    this.dataset = {};
    this.listeners = {};
    this.style = {};
    this._text = null;      // text content, once assigned
    this.isText = false;    // true only for real text nodes
    this._uid = nextId++;
    this.hidden = false;
    this.disabled = false;
    this.tabIndex = -1;
    this.clientWidth = 900;
  }

  // --- tree
  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  get childNodes() { return this.children; }

  // --- attributes
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === "class") this.className = String(value);
    if (name.startsWith("data-")) {
      const key = name.slice(5).replace(/-([a-z])/g,
                                       (_, c) => c.toUpperCase());
      this.dataset[key] = String(value);
    }
  }
  getAttribute(name) {
    return name in this.attributes ? this.attributes[name] : null;
  }
  addEventListener(type, fn) {
    (this.listeners[type] = this.listeners[type] || []).push(fn);
  }
  dispatch(type, event = {}) {
    for (const fn of this.listeners[type] || []) fn({ preventDefault() {},
                                                     ...event });
  }

  // --- content
  set textContent(value) {
    this.children = [];
    this._text = String(value);
  }
  get textContent() {
    if (this._text !== null) return this._text;
    return this.children.map((c) => c.textContent).join("");
  }
  set innerHTML(value) {
    if (value !== "") throw new Error("shim: innerHTML only supports \"\"");
    this.children = [];
    this._text = null;
  }
  get innerHTML() { return this.textContent; }

  // --- layout stubs
  getBoundingClientRect() { return { width: 120, height: 40, top: 0, left: 0 }; }

  // --- form controls
  get selectedOptions() {
    return this.children.filter((c) => c.localName === "option" && c.selected);
  }
  get options() {
    return this.children.filter((c) => c.localName === "option");
  }
  set value(v) {
    this._value = String(v);
    for (const option of this.options) {
      option.selected = option.getAttribute("value") === String(v);
    }
  }
  get value() {
    if (this.localName === "select") {
      const chosen = this.options.find((o) => o.selected);
      if (chosen) return chosen.getAttribute("value") || "";
      return this.options.length ? (this.options[0].getAttribute("value") || "")
                                 : (this._value || "");
    }
    return this._value === undefined ? (this.getAttribute("value") || "")
                                     : this._value;
  }

  // --- selectors
  descendants(out = []) {
    for (const child of this.children) {
      if (!child.isText) { out.push(child); child.descendants(out); }
    }
    return out;
  }
  matches(sel) {
    sel = sel.trim();
    if (sel.startsWith("#")) return this.attributes.id === sel.slice(1);
    if (sel.startsWith(".")) {
      return (this.attributes.class || "").split(/\s+/).includes(sel.slice(1));
    }
    if (sel.startsWith("[")) {
      const [, name, value] = sel.match(/^\[([\w-]+)(?:=([^\]]+))?\]$/) || [];
      if (!name) return false;
      if (value === undefined) return name in this.attributes;
      return this.attributes[name] === value.replace(/['"]/g, "");
    }
    // tag with optional [attr=value] suffix, e.g. button[role=tab]
    const [, tag, rest] = sel.match(/^([a-zA-Z0-9]+)(\[.*\])?$/) || [];
    if (!tag) return false;
    if (this.localName !== tag.toLowerCase()) return false;
    return rest ? this.matches(rest) : true;
  }
  querySelectorAll(selector) {
    // Only what app.js uses: a single selector, optionally "ancestor descendant".
    const parts = selector.trim().split(/\s+(?![^\[]*\])/);
    let scope = [this];
    for (const part of parts) {
      const next = [];
      for (const node of scope) {
        for (const cand of node.descendants()) {
          if (cand.matches(part) && !next.includes(cand)) next.push(cand);
        }
      }
      scope = next;
    }
    return scope;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

// ---------------------------------------------------------------- html parsing
const VOID = new Set(["meta", "link", "br", "hr", "img", "input"]);

function parseHTML(html) {
  const root = new Node("html");
  const stack = [root];
  const re = /<!--[\s\S]*?-->|<!DOCTYPE[^>]*>|<(\/?)([a-zA-Z0-9]+)((?:\s+[^\s=>]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*(\/?)>|([^<]+)/g;
  let match;
  while ((match = re.exec(html))) {
    const [whole, closing, tag, attrs, selfClose, text] = match;
    if (whole.startsWith("<!")) continue;
    if (text !== undefined) {
      if (text.trim()) {
        const node = new Node("#text");
        node._text = text;
        node.isText = true;
        stack[stack.length - 1].appendChild(node);
      }
      continue;
    }
    if (closing) {
      for (let i = stack.length - 1; i > 0; i -= 1) {
        if (stack[i].localName === tag.toLowerCase()) {
          stack.length = i;
          break;
        }
      }
      continue;
    }
    const node = new Node(tag);
    const attrRe = /([^\s=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
    let attr;
    while ((attr = attrRe.exec(attrs || ""))) {
      const name = attr[1];
      if (!name) continue;
      const value = attr[2] !== undefined ? attr[2]
                  : attr[3] !== undefined ? attr[3]
                  : attr[4] !== undefined ? attr[4] : "";
      node.setAttribute(name, value);
    }
    if (node.attributes.hidden !== undefined) node.hidden = true;
    stack[stack.length - 1].appendChild(node);
    if (!VOID.has(tag.toLowerCase()) && !selfClose) stack.push(node);
  }
  return root;
}

// ---------------------------------------------------------------- globals
function install(html, tokens = {}) {
  const root = parseHTML(html);
  const documentElement = root;

  globalThis.document = {
    documentElement,
    body: root.querySelector("body") || root,
    createElement: (tag) => new Node(tag),
    createElementNS: (_ns, tag) => new Node(tag),
    createTextNode: (text) => { const n = new Node("#text");
                                n._text = String(text); n.isText = true;
                                return n; },
    querySelector: (s) => root.querySelector(s),
    querySelectorAll: (s) => root.querySelectorAll(s),
    addEventListener() {},
  };

  globalThis.getComputedStyle = () => ({
    getPropertyValue: (name) => tokens[name] || "#2a78d6",
  });

  globalThis.window = {
    innerWidth: 1400, innerHeight: 900,
    addEventListener() {},
  };
  globalThis.history = { replaceState() {} };
  globalThis.location = { hash: "" };
  globalThis.setTimeout = (fn) => { globalThis.__deferred.push(fn); return 0; };
  globalThis.clearTimeout = () => {};
  globalThis.__deferred = [];
  globalThis.requestAnimationFrame = (fn) => { fn(0); return 0; };

  return { root, document: globalThis.document };
}

globalThis.__shim = { install, Node, parseHTML };
