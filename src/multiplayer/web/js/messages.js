import { api } from './api.js';
import { refreshRooms, switchRoom } from './rooms.js';
import { QUICK_REACTIONS, closeContext, currentCenterView, openContext } from './shell.js';
import { loadState } from './socket.js';
import { openThread } from './thread.js';
import { errorMessage, escHtml, formatTime, htmlToElement, idempotencyKey, memberName, morphChildren, renderMarkdown, shortId, toast } from './util.js';
import { state } from './state.js';

export const NOTIF_SEEN_KEY = 'xyzzy.notificationsSeenAt';
export async function refreshNotificationDot() {
  try {
    state.lastNotifications = await api('GET', '/notifications');
    const seenAt = Number(localStorage.getItem(NOTIF_SEEN_KEY) || 0);
    const unseen = state.lastNotifications.some(n => Date.parse(n.created_at) > seenAt);
    document.getElementById('notif-dot').classList.toggle('hidden', !unseen);
  } catch (_) { /* the bell just stays quiet until the next successful refresh */ }
}

export async function openNotifications() {
  openContext('notifications');
  const list = document.getElementById('notifications-list');
  try {
    state.lastNotifications = await api('GET', '/notifications');
  } catch (err) {
    list.innerHTML = `<div class="panel-section"><div class="panel-copy">${escHtml(errorMessage(err))}</div></div>`;
    return;
  }
  try { localStorage.setItem(NOTIF_SEEN_KEY, String(Date.now())); } catch (_) { /* private browsing */ }
  document.getElementById('notif-dot').classList.add('hidden');
  if (!state.lastNotifications.length) {
    list.innerHTML = '<div class="panel-section"><div class="panel-copy">Nothing yet — mentions and invitations land here.</div></div>';
    return;
  }
  const sorted = [...state.lastNotifications].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
  list.innerHTML = sorted.map(n => `
    <div class="notif-row" data-room-id="${escHtml(n.room_id || '')}" data-action="openNotification">
      <div class="title">${escHtml(n.title)}</div>
      <div class="body">${escHtml(n.body)}</div>
      <div class="time">${formatTime(n.created_at)}</div>
    </div>
  `).join('');
}

export async function openNotification(targetRoomId) {
  if (!targetRoomId) return;
  closeContext();
  if (!state.myRooms.some(item => item.room_id === targetRoomId)) await refreshRooms();
  if (state.myRooms.some(item => item.room_id === targetRoomId)) await switchRoom(targetRoomId);
}

// A second message from the same person inside five minutes continues the
// first: one name, one avatar, one block. The timestamp stays reachable on
// hover rather than repeating down the column. `previousEl` is whatever DOM
// element the new message will land right after — read from its dataset
// rather than from a second copy of the message model, so the same check
// serves both a single live append (previousEl is the current last child)
// and a full snapshot reconcile (previousEl is the element just placed for
// the prior item in the snapshot).
function isGroupedWithPrevious(m, previousEl) {
  // A broadcast thread reply is answering a different, older message — grouping
  // it under whatever happens to be last in the channel (or grouping the next
  // channel message under it) would borrow its neighbor's identity. Either side
  // being a thread reply forces a full header.
  if (!previousEl || !previousEl.classList.contains('msg') || previousEl.classList.contains('system')) return false;
  const role = (m.role || 'system').toLowerCase();
  if (role === 'system' || !m.sender_id || previousEl.dataset.senderId !== m.sender_id) return false;
  if (m.is_thread_reply || previousEl.dataset.isThreadReply === 'true') return false;
  const stamp = m.created_at ? Date.parse(m.created_at) : NaN;
  const previousStamp = Number(previousEl.dataset.stamp);
  return Number.isFinite(stamp) && Number.isFinite(previousStamp) && stamp - previousStamp < 300000;
}

// Whether the unread rule and unread class should show for this message.
// Deliberately kept OUT of computeMessageMarkup's fingerprint: state.readCursor
// changes independently of any message's own data (markRoomRead advances it
// straight away, without a snapshot round trip), so a fingerprint that baked
// unread in would go stale the moment readCursor moved — the fingerprint
// would say "unread" long after the class was already correct, forcing a
// full, action-button-destroying rewrite on the next reconcile for a message
// nothing about actually changed.
function isUnread(m) {
  return Boolean(m.sequence && m.sequence > state.readCursor);
}

// The class list and inner markup a message renders to — everything about it
// that is derived purely from the message's own data. Unread and grouped are
// both applied separately (see syncUnreadClass, syncGroupedClass) precisely
// because neither is: unread is a function of state.readCursor, and grouped
// is a function of whichever element happens to sit immediately before this
// one right now — markRoomRead deleting the "New" divider changes what that
// neighbor is without changing one byte of this message's own data, and
// baking either into this fingerprint would make that unrelated event read
// as "this message changed" on the next reconcile.
function computeMessageMarkup(m) {
  const role = (m.role || 'system').toLowerCase();
  const time = `<div class="time" title="${escHtml(m.created_at || '')}">${formatTime(m.created_at)}</div>`;
  // An open "Full output" record is render state keyed on this message's
  // own id (state.openOutputRecords), not a DOM node the pipeline has to
  // hope survives by accident: including it here means the record is a
  // normal, keyed part of this message's own template, so any reconcile
  // triggered by something else entirely (a reaction landing, say) re-renders
  // it from the same data and morphChildren reuses the existing node by its
  // 'output-record' key exactly like any other tracked child, rather than
  // tearing down a foreign one it never recognized in the first place.
  const outputRecord = renderOutputRecordHtml(state.openOutputRecords.get(m.message_id));
  const innerHTML = `${attribution(m)}<div class="bubble">${renderMentions(m.content)}</div>${time}${messageActions(m)}${outputRecord}`;
  return { className: `msg ${role}`, innerHTML };
}

// Applies (or clears) the 'unread' class on its own, independent of whatever
// reconcile pass this runs inside — guarded so a message already in the
// right read state is never written to just because the reconcile visited
// it. This is what keeps markRoomRead's direct classList.remove('unread')
// truthful forever after: reconcileFp does not encode unread, so nothing a
// later reconcile does can find that removal "stale" and rewrite over it.
function syncUnreadClass(el, m) {
  const shouldBeUnread = isUnread(m);
  if (el.classList.contains('unread') !== shouldBeUnread) el.classList.toggle('unread', shouldBeUnread);
}

// Same treatment as syncUnreadClass, for the same reason: grouped depends on
// a neighbor, not on this message's own data, so it is synced on every visit
// rather than folded into the content fingerprint.
function syncGroupedClass(el, m, previousEl) {
  const shouldBeGrouped = isGroupedWithPrevious(m, previousEl);
  if (el.classList.contains('grouped') !== shouldBeGrouped) el.classList.toggle('grouped', shouldBeGrouped);
}

// Writes the given markup onto `el` in place — same node identity in or out —
// and stamps a fingerprint so a later reconcile can tell whether this exact
// message needs writing to again. Every write below is guarded: an attribute
// (dataset included — each is a real data-* attribute) still queues a
// mutation record even when set to the value it already has, so a call that
// changes nothing about a message must still perform zero writes to satisfy
// that.
//
// The content itself goes through morphChildren rather than a direct
// `el.innerHTML = markup.innerHTML` (or, worse, a guard that compares the
// two): a live element's `el.innerHTML` is the browser's own serialization,
// which never matches a hand-built template string byte for byte (attribute
// order, quoting, entity encoding all differ), so that comparison reads
// "changed" on essentially every call, even when nothing about the message
// is different — which is exactly what forced a full rebuild, and lost a
// pending click on this message's own action buttons, on every reconcile
// that happened to visit an otherwise-untouched message. morphChildren
// compares a live, previously-rendered tree against a freshly parsed one
// (both browser-serialized, so the comparison is meaningful), leaves
// anything structurally unchanged alone, and only patches what actually
// differs — so the action buttons survive even a reconcile that does find a
// real change elsewhere in the same message (a new reaction, say).
function applyMessageMarkup(el, m, markup, previousEl) {
  // The full class list -- base plus the two ephemeral ones -- computed and
  // written in one guarded call, not base-only here with syncUnreadClass/
  // syncGroupedClass patching 'unread'/'grouped' back on right after: doing
  // it in three separate writes (assign base, toggle unread, toggle
  // grouped) means the first write wipes whichever ephemeral classes this
  // element already correctly had, forcing the next two calls to restore
  // them -- three attribute mutations for a class list that, in the common
  // case (a message re-rendering for a reason that has nothing to do with
  // its read or grouping state, a live arrival's best-effort timestamp
  // getting corrected, say), never actually needed to change at all.
  const fullClassName = [markup.className, isUnread(m) && 'unread', isGroupedWithPrevious(m, previousEl) && 'grouped']
    .filter(Boolean).join(' ');
  if (el.className !== fullClassName) el.className = fullClassName;
  const wrapper = htmlToElement(`<div>${markup.innerHTML}</div>`);
  morphChildren(el, wrapper);
  // The avatar letter is read back off the one renderer's output rather than
  // resolving the name a second time, so the two can never disagree.
  const initial = (el.querySelector('.sender')?.textContent || '?').trim().slice(0, 1);
  if (el.dataset.initial !== initial) el.dataset.initial = initial;
  if (m.message_id) {
    if (el.dataset.messageId !== m.message_id) el.dataset.messageId = m.message_id;
    const reconcileKey = `m:${m.message_id}`;
    if (el.dataset.reconcileKey !== reconcileKey) el.dataset.reconcileKey = reconcileKey;
  }
  if (m.sender_id && el.dataset.senderId !== m.sender_id) el.dataset.senderId = m.sender_id;
  const threadReplyFlag = m.is_thread_reply ? 'true' : 'false';
  if (el.dataset.isThreadReply !== threadReplyFlag) el.dataset.isThreadReply = threadReplyFlag;
  const stamp = m.created_at ? Date.parse(m.created_at) : NaN;
  if (Number.isFinite(stamp)) {
    const stampText = String(stamp);
    if (el.dataset.stamp !== stampText) el.dataset.stamp = stampText;
  }
  const fp = `${markup.className} ${markup.innerHTML}`;
  if (el.dataset.reconcileFp !== fp) el.dataset.reconcileFp = fp;
}

// The branch activity cards renderBranchActivity (branch.js) reconciles
// always sit after every chat message; a live message has to land before
// them, not at the container's own end, or its first reconcile has to move
// it there itself (insertBefore, on a node that may hold focus right now --
// a reply box someone is mid-keystroke in inside another message's thread,
// say -- and a move blurs whatever was focused inside it, same as a replace
// would).
function lastMessageElement(container) {
  let node = container.lastElementChild;
  while (node && node.classList.contains('branch-activity')) node = node.previousElementSibling;
  return node;
}

// A single message arriving live (a real-time event, a system notice):
// always a new node, inserted at the position a full reconcile would also
// place it at (see lastMessageElement), keyed the same way reconcileMessages
// keys its own elements so a later full reconcile recognizes and reuses it
// rather than creating a duplicate.
export function appendMessage(m) {
  const container = document.getElementById('messages');
  const previousEl = lastMessageElement(container);
  const el = document.createElement('div');
  applyMessageMarkup(el, m, computeMessageMarkup(m), previousEl);
  const firstBranchActivity = container.querySelector('.branch-activity');
  if (firstBranchActivity) container.insertBefore(el, firstBranchActivity);
  else container.appendChild(el);
  scrollMessagesToBottom();
}

// Rebuilds #messages from a fresh snapshot without discarding an element the
// snapshot leaves unchanged. A wholesale innerHTML replace (the old
// behaviour) recreates every message on every socket-triggered reload — a
// membership change, a reconnect, another member's message — which drops
// whatever the DOM node a person is mid-click on to nothing, silently
// swallowing that click. Every message keeps its identity across calls,
// keyed by its own message_id: a message whose rendered class list and
// markup did not change since the last call is never written to at all,
// only genuinely new messages are created, and only messages no longer in
// the snapshot are removed.
export function reconcileMessages(messages, protectedKeys = new Set()) {
  const container = document.getElementById('messages');
  // Every key this snapshot will still want, decided BEFORE anything else
  // touches the DOM -- see the removal pass right below for why a decision
  // made only after placement (the previous shape of this function) is too
  // late. A live-arrived system notice (appendSystemMessage: "so-and-so was
  // invited", joined, left) carries no reconcileKey at all, so it can never
  // appear in this set; a message deleted server-side likewise falls out
  // simply by not being in `messages` any more.
  const wanted = new Set(
    messages.filter(m => m.message_id).map(m => `m:${m.message_id}`)
  );
  if (messages.some(isUnread)) wanted.add('unread-rule');

  // protectedKeys names a message this exact snapshot cannot speak to either
  // way: one appendMessage already rendered live, from an event that landed
  // after the fetch behind `messages` was sent but before its response came
  // back (see loadStateImpl in socket.js, the only caller that ever passes
  // this). `wanted` alone cannot tell that case apart from a message that
  // truly no longer exists -- both are simply "not in `messages`" -- so
  // without this, a message-in-progress would fail the removal check below,
  // get torn down here, and then get rebuilt from the replayed event as a
  // brand new node: one logical message, but its identity, focus, and
  // anything else pinned to that DOM node gone across a reconcile that (from
  // the user's own timeline) never had any reason to touch it.
  //
  // Removed before a single node is placed. A same-parent move
  // (insertBefore of a node that is already this parent's child) drops a
  // pending pointer event and blurs focus exactly like a real relocation
  // does, even when nothing about layout changes once it settles. Left
  // until the end (the previous shape of this function), a still-present
  // stale sibling -- an ephemeral notice, a message removed from the
  // snapshot -- sits between two real messages during placement; the later
  // one's own "am I already immediately after the one before me" check
  // then correctly says no, because the stale node is in the way, and moves
  // it for a reposition that would have been unnecessary the instant the
  // stale node was gone anyway. Branch activity cards are a different
  // reconcile's territory entirely (see renderBranchActivity in branch.js):
  // never swept, never adopted into this pass's own key namespace.
  Array.from(container.children).forEach(el => {
    if (el.classList.contains('branch-activity')) return;
    const key = el.dataset.reconcileKey;
    if (!key || (!wanted.has(key) && !protectedKeys.has(key))) el.remove();
  });

  const existing = new Map();
  Array.from(container.children).forEach(el => {
    if (el.classList.contains('branch-activity')) return;
    if (el.dataset.reconcileKey) existing.set(el.dataset.reconcileKey, el);
  });
  let cursor = null;
  let unreadRuleShown = false;
  const place = el => {
    const wantsNext = cursor ? cursor.nextSibling : container.firstChild;
    if (wantsNext !== el) container.insertBefore(el, wantsNext);
    cursor = el;
  };
  messages.forEach(m => {
    if (!unreadRuleShown && isUnread(m)) {
      unreadRuleShown = true;
      let rule = existing.get('unread-rule');
      if (!rule) {
        rule = document.createElement('div');
        rule.className = 'unread-rule';
        rule.textContent = 'New';
        rule.dataset.reconcileKey = 'unread-rule';
      }
      place(rule);
    }
    const key = m.message_id ? `m:${m.message_id}` : null;
    const markup = computeMessageMarkup(m);
    const fp = `${markup.className} ${markup.innerHTML}`;
    let el = key ? existing.get(key) : null;
    if (el) {
      if (el.dataset.reconcileFp !== fp) applyMessageMarkup(el, m, markup, cursor);
      else {
        // Fingerprint unchanged means applyMessageMarkup did not run, so
        // 'unread'/'grouped' still need their own guarded check here: both
        // can go stale for reasons entirely outside this message's own
        // data (markRoomRead advancing state.readCursor directly, a
        // neighbor's own content changing what "grouped with the previous
        // message" means) that a content fingerprint comparison can never
        // see by design.
        syncUnreadClass(el, m);
        syncGroupedClass(el, m, cursor);
      }
    } else {
      el = document.createElement('div');
      applyMessageMarkup(el, m, markup, cursor);
    }
    place(el);
  });
  scrollMessagesToBottom();
}

// Only meaningful once every sibling that can land below the transcript — branch
// activity cards included — is already in the DOM. A rAF lets this frame's layout
// settle first, so the newest item is never left half behind the composer.
export function scrollMessagesToBottom() {
  const div = document.getElementById('messages');
  div.scrollTop = div.scrollHeight;
  requestAnimationFrame(() => { div.scrollTop = div.scrollHeight; scheduleAutoRead(); });
}

// The manual checkmark stays, but a reload used to bring the NEW rule back every
// time even though the reader had plainly already seen the bottom of the channel.
// Advance the cursor on their behalf once it is genuinely true: this is the
// conversation view, the tab has focus, and the scroller is sitting at the bottom
// — debounced so a fast scroll-past does not count as having read it.
export function scheduleAutoRead() {
  clearTimeout(state.autoReadTimer);
  if (currentCenterView() !== 'conversation' || !document.hasFocus()) return;
  if (state.readCursor >= state.lastSequence) return;
  const div = document.getElementById('messages');
  if (!div || div.scrollHeight - div.scrollTop - div.clientHeight > 24) return;
  state.autoReadTimer = setTimeout(markRoomRead, 1500);
}

// Both panes attribute a message through this one function, because a pane with a
// template of its own is a pane that drifts: the thread pane had one, and an agent
// answer read there was a bare id with no trigger, no invoker and no output. It is
// also the pane a mention-invoked answer is read in, since such an answer inherits
// the mention's broadcast and a mention inside a thread does not broadcast.
export function attribution(m, trailing = '') {
  return `<div class="sender">${escHtml(displayNameFor(m))}${trailing}</div>${agentProvenance(m)}`;
}

// An id is not a name. memberName knows what a member or agent is called, and
// falls back to the raw id only when the sender is genuinely not in this room.
export function displayNameFor(m) {
  return m.sender_id ? memberName(m.sender_id) : 'System';
}

// Why this agent spoke: what triggered the turn, who asked for it, and a way
// through to the AgentOutput this message is the conversational surface of.
export function agentProvenance(m) {
  if ((m.role || '').toUpperCase() !== 'AGENT') return '';
  const meta = m.metadata || {};
  if (!meta.output_id && !meta.triggered_by) return '';
  const trigger = String(meta.triggered_by || 'DIRECT').toLowerCase();
  const invoker = meta.requested_by
    ? ` · asked by ${escHtml(memberName(meta.requested_by))}`
    : '';
  const record = meta.output_id
    ? ` · <button class="output-link" data-output-id="${escHtml(meta.output_id)}" data-action="openAgentOutput">Full output ${escHtml(shortId(meta.output_id))}</button>`
    : '';
  const excerpted = meta.output_excerpted ? ' · excerpt' : '';
  return `<div class="agent-provenance">Answered on ${escHtml(trigger)}${invoker}${excerpted}${record}</div>`;
}

// The record content itself, shared by the two places it can appear: the
// direct DOM edit openAgentOutput makes for instant feedback on click, and
// computeMessageMarkup's own inclusion of it on every later reconcile. Both
// carry the same 'output-record' key, which is what lets morphChildren treat
// whichever one already exists as the thing the other reuses rather than a
// stranger to tear down or duplicate. Returns '' when outputId is undefined
// (nothing open) or the output has gone missing from this room's snapshot.
function renderOutputRecordHtml(outputId) {
  if (outputId === undefined) return '';
  const output = state.allRoomOutputs.find(o => o.output_id === outputId);
  if (!output) return '';
  const agent = state.roomAgents.find(a => a.agent_id === output.agent_id);
  return `<div class="output-record" data-key="output-record">
    <div class="output-record-head">${escHtml(agent ? agent.name : output.agent_id)} · output <code>${escHtml(shortId(output.output_id))}</code> · run <code>${escHtml(shortId(output.execution_id))}</code></div>
    <div class="output-record-body">${renderMarkdown(output.content || 'No readable content returned.')}</div>
    <details class="prompt-detail"><summary>Exact source prompt</summary><div>${escHtml(output.source_prompt || 'Unavailable')}</div></details>
  </div>`;
}

// The message is the excerpt; this is the record it points at. Nothing is fetched
// again — the room snapshot already carries every output the reader may see. The
// clicked button carries its own row, so the record opens beside whichever pane
// was read from and one output shown twice does not open in the wrong one.
export function openAgentOutput(outputId, trigger) {
  const holder = trigger && trigger.closest('.msg, .thread-item');
  if (!holder) return;
  const messageId = holder.dataset.messageId;

  // A channel message's own render (computeMessageMarkup) is what keeps this
  // record's identity through anything else that reconciles the message —
  // toggling the state here and syncing the DOM directly is what gives the
  // click itself instant feedback, without waiting for a network round trip
  // or an unrelated reconcile to notice the state changed.
  if (holder.classList.contains('msg') && messageId) {
    if (state.openOutputRecords.get(messageId) === outputId) {
      state.openOutputRecords.delete(messageId);
    } else {
      const output = state.allRoomOutputs.find(o => o.output_id === outputId);
      if (!output) { toast('That output is not in this room snapshot.', 'error'); return; }
      state.openOutputRecords.set(messageId, outputId);
    }
    const html = renderOutputRecordHtml(state.openOutputRecords.get(messageId));
    const existing = holder.querySelector('.output-record');
    if (!html) {
      existing?.remove();
    } else if (existing) {
      const next = htmlToElement(html);
      existing.replaceWith(next);
    } else {
      holder.appendChild(htmlToElement(html));
    }
    return;
  }

  // Thread items are rebuilt wholesale on every refreshThread() call (see
  // thread.js) rather than reconciled incrementally, so there is no morph
  // for a manually-appended child to be caught by here — __foreign is a
  // defensive marker in case that ever changes, not load-bearing today.
  if (holder.dataset.outputOpen === outputId) {
    holder.querySelector('.output-record')?.remove();
    delete holder.dataset.outputOpen;
    return;
  }
  const output = state.allRoomOutputs.find(o => o.output_id === outputId);
  if (!output) { toast('That output is not in this room snapshot.', 'error'); return; }
  const record = htmlToElement(renderOutputRecordHtml(outputId));
  record.__foreign = true;
  holder.appendChild(record);
  holder.dataset.outputOpen = outputId;
}

export function appendSystemMessage(text) {
  appendMessage({role: 'system', sender_id: 'system', content: text});
}

// A mention is whatever the server derived from the text; this only marks it up.
export function renderMentions(text) {
  return escHtml(text).replace(/(^|[^\w@])@([A-Za-z0-9][A-Za-z0-9_.\-]*)/g,
    (all, lead, handle) => `${lead}<span class="mention">@${handle}</span>`);
}

export function messageActions(m) {
  if (!m.message_id || (m.role || '').toUpperCase() === 'SYSTEM') return '';
  const id = escHtml(m.message_id);
  const groups = new Map(QUICK_REACTIONS.map(emoji => [emoji, {count: 0, mine: false}]));
  (m.reactions || []).forEach(reaction => {
    const entry = groups.get(reaction.emoji) || {count: 0, mine: false};
    entry.count += 1;
    if (reaction.actor_id === state.userId) entry.mine = true;
    groups.set(reaction.emoji, entry);
  });
  const chips = [...groups.entries()].map(([emoji, entry]) =>
    `<button class="${entry.mine ? 'reacted' : ''}" data-message-id="${id}" data-emoji="${escHtml(emoji)}" data-mine="${entry.mine}" data-action="toggleReaction" aria-label="React ${escHtml(emoji)}" title="React ${escHtml(emoji)}">${escHtml(emoji)}${entry.count ? ` ${entry.count}` : ''}</button>`).join('');
  // Every number here was counted from the reply rows on this read: the whole
  // thread, who is in it, and when it last moved. Escaped like every other
  // server-carried value even though both are ordinarily plain integers --
  // CSP stops an injected <script> from running, not the injection itself.
  const count = m.reply_count || 0;
  const people = m.participant_count || 1;
  const thread = count
    ? `${escHtml(count)} ${count === 1 ? 'reply' : 'replies'} · ${escHtml(people)} ${people === 1 ? 'person' : 'people'}`
    : '';
  // A broadcast reply is already an answer in a thread. Offering it as somewhere to
  // start one said the opposite of what it is.
  const standingLabel = m.is_thread_reply
    ? (thread ? `Reply in thread · ${thread}` : 'Reply in thread')
    : thread;
  const lastReply = m.last_reply_at ? ` title="Last reply ${escHtml(new Date(m.last_reply_at).toLocaleString())}"` : '';
  // One button, always rendered, whether or not a thread exists yet: starting
  // a thread and reading one already open are the same conceptual slot, and
  // a person can be mid-press on it at the exact moment a first reply lands
  // and turns "Reply" into "Reply in thread". The two used to be genuinely
  // different DOM nodes -- a hover-revealed .action living inside
  // .msg-actions, and a standing .thread-open sibling of it -- which a keyed
  // morph can key identically but can never actually reuse across a change
  // of PARENT: key matching only ever happens among siblings, and these
  // were never siblings of each other. One always-present node, whose
  // "standing" look is a class toggle rather than a swap of which element
  // exists at all, is what lets the node -- and whatever focus or pending
  // click it is carrying -- survive that exact moment.
  const standing = Boolean(standingLabel);
  const label = standingLabel || 'Reply';
  const threadAction = `<button class="thread-action${standing ? ' standing' : ''}" data-message-id="${id}"${lastReply} data-action="openThread">${label}</button>`;
  return `${threadAction}<div class="msg-actions">${chips}</div>`;
}

export async function toggleReaction(data) {
  const emoji = data.emoji;
  const messageId = data.messageId;
  try {
    if (data.mine === 'true') {
      await api('DELETE', `/messages/${messageId}/reactions/${encodeURIComponent(emoji)}`);
    } else {
      await api('POST', `/messages/${messageId}/reactions`, {emoji});
    }
    await loadState();
  } catch (err) {
    toast(`Reaction was not saved: ${errorMessage(err)}`, 'error');
  }
}

export function applyReadCursor(cursor) {
  if (!cursor) return;
  state.readCursor = cursor.last_read_sequence || 0;
  const unread = cursor.unread_messages || 0;
  const pill = document.getElementById('unread-pill');
  const shouldHide = unread === 0;
  if (pill.hidden !== shouldHide) pill.hidden = shouldHide;
  if (pill.textContent !== String(unread)) pill.textContent = unread;
}

export async function refreshUnread() {
  try { applyReadCursor(await api('GET', `/rooms/${state.roomId}/read-cursor`)); }
  catch (err) { console.error('Failed to read the read cursor:', err); }
}

export async function markRoomRead() {
  try {
    applyReadCursor(await api('PUT', `/rooms/${state.roomId}/read-cursor`, {last_read_sequence: state.lastSequence}));
    // Safe to mutate these classes directly, ahead of any reconcile: a
    // message's reconcileFp never encodes unread (see isUnread/
    // syncUnreadClass above), so this removal cannot go stale relative to a
    // fingerprint the next snapshot compares against, and cannot itself
    // trigger a full, action-button-destroying rewrite of the message.
    document.querySelectorAll('.msg.unread').forEach(el => el.classList.remove('unread'));
    document.querySelectorAll('.unread-rule').forEach(el => el.remove());
  } catch (err) {
    toast(`Read position was not saved: ${errorMessage(err)}`, 'error');
  }
}

export async function sendMessage() {
  const input = document.getElementById('msg-input');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';

  const invokeAgents = document.getElementById('invoke-agents');
  try {
    const sent = await api('POST', `/rooms/${state.roomId}/messages`, {
      content: text, role: 'HUMAN',
      invoke_mentioned_agents: Boolean(invokeAgents && invokeAgents.checked)
    }, { idempotencyKey: idempotencyKey() });
    reportUnrecognizedMentions(sent);
  } catch(err) {
    // A refusal the console swallowed is a refusal the author never saw: their
    // text came back and nothing said why. The thread composer beside this one
    // has always said, and both say it the same way.
    input.value = text;
    toast(`Message was not sent: ${errorMessage(err)}`, 'error');
  }
}

// The message was sent either way; this says who it did not reach, because an
// unaddressed @handle used to be indistinguishable from no mention at all.
export function reportUnrecognizedMentions(sent) {
  const missed = (sent && sent.unrecognized_mentions) || [];
  if (!missed.length) return;
  toast(`Nobody here answers to ${missed.map(h => `@${h}`).join(', ')}. Check the handle in People or Agents.`, 'error');
}
