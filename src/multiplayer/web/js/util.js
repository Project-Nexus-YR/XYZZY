import { state } from './state.js';

export const THEME_STORAGE_KEY = 'xyzzy.theme';
export function currentTheme() {
  return document.documentElement.dataset.theme
    || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
}
export function toggleTheme() {
  const next = currentTheme() === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem(THEME_STORAGE_KEY, next); }
  catch (_) { /* Private browsing can disable storage; the preference still holds for this tab. */ }
}
try {
  const storedTheme = localStorage.getItem(THEME_STORAGE_KEY);
  if (storedTheme === 'dark' || storedTheme === 'light') document.documentElement.dataset.theme = storedTheme;
} catch (_) { /* nothing stored is the same as never having chosen */ }

export function memberName(id) {
  if (!id) return 'Unknown';
  // The member row is server truth for everyone, self included — the typed
  // sign-in name is only a last-resort fallback before the first snapshot lands.
  const member = state.roomMembers.find(item => item.user_id === id);
  if (member && member.display_name) return member.display_name;
  if (id === state.userId) return state.userName || id;
  const agent = state.roomAgents.find(item => item.agent_id === id);
  if (agent) return agent.name;
  return id;
}

export function formatTime(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
}

// The one renderer for anything an agent or the room wrote in markdown-ish prose.
// Escape first, then transform a small fixed set of markers — never raw HTML, no
// link auto-linking. Human chat messages never pass through this.
export function renderMarkdown(text) {
  const lines = escHtml(text || '').split('\n');
  const inline = s => s.replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  let html = '', inList = false, inCode = false, para = [];
  const flushPara = () => { if (para.length) { html += `<p>${para.join(' ')}</p>`; para = []; } };
  const closeList = () => { if (inList) { html += '</ul>'; inList = false; } };
  lines.forEach(line => {
    if (line.startsWith('```')) {
      if (inCode) { html += '</pre>'; inCode = false; } else { flushPara(); closeList(); html += '<pre>'; inCode = true; }
      return;
    }
    if (inCode) { html += line + '\n'; return; }
    const heading = line.match(/^(#{1,3})\s+(.*)/);
    if (heading) { flushPara(); closeList(); const l = heading[1].length + 2; html += `<h${l}>${inline(heading[2])}</h${l}>`; return; }
    if (/^-\s+/.test(line)) {
      flushPara();
      if (!inList) { html += '<ul>'; inList = true; }
      html += `<li>${inline(line.replace(/^-\s+/, ''))}</li>`;
      return;
    }
    closeList();
    if (line.trim() === '') { flushPara(); return; }
    para.push(inline(line));
  });
  flushPara(); closeList();
  if (inCode) html += '</pre>';
  return html;
}

export function toast(message, tone = '') {
  const region = document.getElementById('toast-region');
  const item = document.createElement('div');
  item.className = `toast ${tone}`;
  item.textContent = message;
  region.appendChild(item);
  setTimeout(() => item.remove(), 4200);
}

// index.html gives #ws-status role="status" (an implicit polite live region),
// so its own text content is what a screen reader announces on every change —
// a separate aria-label duplicating that same text would only be one more
// place for the two to drift apart. A narrow room header can visually clip
// this element without touching the DOM text itself, so the accessible
// announcement stays intact even when the label is not fully visible.
export function setWsStatus(text, connected) {
  const el = document.getElementById('ws-status');
  el.textContent = text;
  el.classList.toggle('connected', Boolean(connected));
}

export function escHtml(s) {
  if (s === undefined || s === null) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// FastAPI's own validation failures carry `detail` as a list of {loc, msg, type}
// objects, not a string — anything downstream that reads `parsed.detail` as
// prose (a toast, a field-error <div>) used to print '[object Object]' for
// that one class of refusal, the one carrying the most precise reason of them
// all. A string detail (every other error handler in this app) still passes
// straight through.
export function errorMessage(err) {
  const raw = err && err.message ? err.message : String(err || 'Unknown error');
  try {
    const parsed = JSON.parse(raw);
    const detail = parsed.detail;
    if (Array.isArray(detail)) return detail.map(entry => entry.msg || JSON.stringify(entry)).join('; ');
    if (typeof detail === 'string') return detail || raw;
    return raw;
  } catch (_) {
    return raw;
  }
}

export function shortId(value) {
  if (!value) return 'unknown';
  return value.length > 18 ? `${value.slice(0, 8)}…${value.slice(-6)}` : value;
}

// SCREAMING_SNAKE machine tokens read as debug output; people get the words.
export function humanizeToken(value) {
  return String(value || '').replace(/_/g, ' ').toLowerCase();
}

export function bytesToBase64Url(bytes) {
  let binary = '';
  bytes.forEach(byte => { binary += String.fromCharCode(byte); });
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function idempotencyKey() {
  return crypto.randomUUID();
}

export function logEvent(msg) {
  const log = document.getElementById('events-log');
  log.querySelector('.events-empty')?.remove();
  const div = document.createElement('div');
  div.textContent = `[${msg.sequence}] ${msg.event_type} by ${msg.actor_id || '?'}`;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  updateActivityLogSummary();
}

// Collapsed, the "Nothing yet." line inside is invisible — the section header
// is the only thing anyone sees, so it has to say there is nothing rather
// than read like every other populated section.
export function updateActivityLogSummary() {
  const log = document.getElementById('events-log');
  const summary = document.getElementById('activity-log-summary');
  if (!log || !summary) return;
  const isEmpty = log.children.length === 0 || Boolean(log.querySelector('.events-empty'));
  summary.textContent = isEmpty ? 'Activity log · quiet' : 'Activity log';
}

export function highlightExcerpt(excerpt) {
  return escHtml(excerpt).replace(/\[([^\]]*)\]/g, '<mark>$1</mark>');
}

// A snapshot re-render must not blow away and recreate every row on every
// refresh: a node the user is mid-interaction with (a pending click, a hover
// reveal in flight) has to survive a refresh that does not actually change
// it, or the click lands on nothing. reconcileList keys each row by
// keyFn(item), reuses the element already in the container for a key it has
// already seen (rewriting its content only when renderFn's output for that
// key actually changed since last time), creates a fresh element for a new
// key, and removes elements whose key is gone. A container whose full set of
// keys and content is unchanged since the last call is never written to.
export function reconcileList(container, items, keyFn, renderFn) {
  const existing = new Map();
  Array.from(container.children).forEach(el => {
    if (el.dataset.reconcileKey) existing.set(el.dataset.reconcileKey, el);
  });
  const seen = new Set();
  let cursor = null;
  items.forEach(item => {
    const key = String(keyFn(item));
    const html = renderFn(item).trim();
    let el = existing.get(key);
    if (el) {
      if (el.dataset.reconcileFp !== html) morphElement(el, html);
    } else {
      el = htmlToElement(html);
      el.dataset.reconcileKey = key;
      el.dataset.reconcileFp = html;
    }
    seen.add(key);
    const wantsNext = cursor ? cursor.nextSibling : container.firstChild;
    if (wantsNext !== el) container.insertBefore(el, wantsNext);
    cursor = el;
  });
  Array.from(container.children).forEach(el => {
    if (!seen.has(el.dataset.reconcileKey)) el.remove();
  });
}

// Parses one HTML string into a single detached element, with morph
// ownership bookkeeping (see stampOwnership) applied recursively so the
// result is ready to be handed to morphChildren/morphElement right away.
export function htmlToElement(html) {
  const template = document.createElement('template');
  template.innerHTML = html;
  const el = template.content.firstElementChild;
  stampOwnership(el);
  return el;
}

// Every element morphChildren/morphElement create or patch gets a record of
// which attribute names came from OUR OWN last render, stored as a plain JS
// property (not a DOM attribute, so it never round-trips through
// serialization or cloning on its own) rather than a fixed exclusion list.
// That is what lets a foreign attribute survive a real content change on the
// very same node: an attribute nobody here ever authored (a test probe, a
// class toggled by unrelated code, an aria-* another handler owns) is never
// in this set, so the "remove what we no longer want" pass below can never
// touch it, no matter what the freshly rendered template does or does not
// contain.
function stampOwnership(node) {
  if (node.nodeType !== Node.ELEMENT_NODE) return;
  node.__morphOwnedAttrs = new Set(Array.from(node.attributes).map(attr => attr.name));
  Array.from(node.children).forEach(stampOwnership);
}

// Writes newNode's attributes onto oldNode, guarded per attribute, removing
// only the ones oldNode's own last render put there and the new one no
// longer wants -- see stampOwnership for why that is a set we track rather
// than infer.
function syncElementAttributes(oldNode, newNode) {
  const owned = oldNode.__morphOwnedAttrs || new Set();
  const nextNames = new Set(Array.from(newNode.attributes).map(attr => attr.name));
  owned.forEach(name => {
    if (!nextNames.has(name)) oldNode.removeAttribute(name);
  });
  nextNames.forEach(name => {
    const value = newNode.getAttribute(name);
    if (oldNode.getAttribute(name) !== value) oldNode.setAttribute(name, value);
  });
  oldNode.__morphOwnedAttrs = nextNames;
}

// The default identity for a child node during a keyed morph: an element
// carrying a reaction's own emoji is that reaction's chip, wherever it sits
// in the list; the reply/thread-open button is one conceptual slot regardless
// of which of its two variants (a bare "Reply" action, or the standing
// "Reply in thread" one messageActions swaps it for once a thread exists)
// is currently rendered -- both carry the same data-action, which is what
// keeps that slot's node identity stable across the swap instead of falling
// out of position sync with every reaction chip after it, the way a purely
// positional match does the moment that slot's presence changes. Anything
// else has no stable identity of its own here and falls back to position,
// among only the other position-matched siblings (see morphChildren).
function defaultKeyOf(node) {
  if (node.nodeType !== Node.ELEMENT_NODE) return null;
  const data = node.dataset || {};
  if (data.emoji) return `emoji:${data.emoji}`;
  if (data.action === 'openThread') return 'thread-action';
  if (data.key) return `key:${data.key}`;
  return null;
}

// Patches oldEl's children in place to match newEl's children, preserving
// node identity for anything the key (or, failing that, position among
// other unkeyed siblings) says is the same slot -- instead of the blunt
// `el.innerHTML = ...` this replaced, and instead of pure positional
// matching, which mis-pairs everything once a keyed sibling's presence
// changes: inserting the thread-open button in front of the reactions
// row shifts every reaction chip's index by one, so positional-only
// matching would compare each chip against whatever used to sit one slot
// over -- a structural mismatch -- and replace it outright, losing every
// chip's identity over one unrelated button appearing. A genuine
// structural change (a different tag, text where there was an element)
// still lands as a replace: there is nothing meaningful to preserve across
// an actual shape change.
export function morphChildren(oldEl, newEl, keyOf = defaultKeyOf) {
  const oldNodes = Array.from(oldEl.childNodes);
  const newNodes = Array.from(newEl.childNodes);

  // A node stamped __foreign (a plain JS property, so it is never copied by
  // cloneNode and never round-trips through any template) was appended by
  // code outside this render pipeline entirely -- a "Full output" record,
  // say. This function did not create it and has no rendered counterpart
  // for it in newEl, so it is excluded up front: never a candidate to be
  // matched against, reused as an unkeyed slot filler, or (see the removal
  // pass below) torn down just because nothing in the new render claims it.
  const oldByKey = new Map();
  const oldUnkeyed = [];
  oldNodes.forEach(node => {
    if (node.__foreign) return;
    const key = keyOf(node);
    if (key !== null) oldByKey.set(key, node);
    else oldUnkeyed.push(node);
  });

  const usedOld = new Set();
  let unkeyedCursor = 0;
  // For each new node, the old node to reuse (patched in place), or null --
  // meaning a fresh clone is what belongs at this position.
  const resolved = newNodes.map(newNode => {
    const key = keyOf(newNode);
    if (key !== null) {
      const match = oldByKey.get(key);
      if (match && !usedOld.has(match) && match.nodeType === newNode.nodeType) {
        usedOld.add(match);
        return match;
      }
      return null;
    }
    // Unkeyed: paired positionally, but only among the other not-yet-used
    // unkeyed old nodes, and only when the node type/tag actually agrees --
    // a keyed sibling's insertion or removal shifts these indices, but
    // never changes which unkeyed nodes exist or their relative order.
    while (unkeyedCursor < oldUnkeyed.length) {
      const candidate = oldUnkeyed[unkeyedCursor];
      unkeyedCursor += 1;
      if (usedOld.has(candidate)) continue;
      if (candidate.nodeType === newNode.nodeType && candidate.nodeName === newNode.nodeName) {
        usedOld.add(candidate);
        return candidate;
      }
      // A structural mismatch among the unkeyed siblings themselves: this
      // candidate has no home in the new render and is removed below.
    }
    return null;
  });

  // Stale nodes are removed BEFORE anything is placed. A same-parent move
  // (insertBefore of a node that is already this parent's child) drops a
  // pending pointer event and blurs focus exactly like a real relocation
  // does, even though nothing about layout changes once it settles -- so a
  // survivor already sitting in its correct final position relative to the
  // other survivors must never be moved just because a to-be-removed
  // sibling still happens to sit between them at the moment the placement
  // loop below checks. Removing first means that never happens: by the time
  // placement runs, only survivors are left to check position against.
  oldNodes.forEach(node => {
    if (node.__foreign) return;
    if (!usedOld.has(node) && node.parentNode === oldEl) oldEl.removeChild(node);
  });

  let cursor = null;
  newNodes.forEach((newNode, i) => {
    let el = resolved[i];
    if (el) {
      patchNode(el, newNode, keyOf);
    } else {
      el = newNode.cloneNode(true);
      stampOwnership(el);
    }
    const wantsNext = cursor ? cursor.nextSibling : oldEl.firstChild;
    if (wantsNext !== el) oldEl.insertBefore(el, wantsNext);
    cursor = el;
  });
}

function patchNode(oldNode, newNode, keyOf) {
  if (oldNode.nodeType === Node.TEXT_NODE || oldNode.nodeType === Node.COMMENT_NODE) {
    if (oldNode.textContent !== newNode.textContent) oldNode.textContent = newNode.textContent;
    return;
  }
  if (oldNode.nodeType !== Node.ELEMENT_NODE) return;
  syncElementAttributes(oldNode, newNode);
  if (newNode.children.length === 0 && oldNode.children.length === 0) {
    // A leaf element (a <span> or <button> holding only text): comparing
    // and setting textContent directly is what avoids the same
    // markup-vs-serialization trap this function exists to fix.
    if (oldNode.textContent !== newNode.textContent) oldNode.textContent = newNode.textContent;
  } else {
    morphChildren(oldNode, newNode, keyOf);
  }
}

// Morphs `el` into `html`'s shape in place -- same node identity, so a click
// in flight against it survives -- by replacing its attributes and children
// with the freshly rendered version. Every write is guarded: setAttribute
// (dataset included, since data-* is a real attribute) queues a mutation
// record even when the value does not change, so a caller that reaches this
// function only because *something* about the item changed must still
// perform zero writes for whichever individual attributes and children did
// not.
export function morphElement(el, html) {
  const next = htmlToElement(html);
  syncElementAttributes(el, next);
  morphChildren(el, next);
  if (el.dataset.reconcileFp !== html) el.dataset.reconcileFp = html;
}
