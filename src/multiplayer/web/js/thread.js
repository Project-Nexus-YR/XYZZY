import { api } from './api.js';
import { selectBranch } from './branch.js';
import { attribution, renderMentions, reportUnrecognizedMentions } from './messages.js';
import { refreshRooms, switchRoom } from './rooms.js';
import { openContext } from './shell.js';
import { loadState } from './socket.js';
import { errorMessage, escHtml, formatTime, highlightExcerpt, idempotencyKey, memberName, shortId, toast } from './util.js';
import { state } from './state.js';

export async function openThread(messageId) {
  state.openThreadRootId = messageId;
  state.threadReplyTargetId = messageId;
  openContext('thread');
  await refreshThread();
}

export function setThreadTarget(messageId) {
  state.threadReplyTargetId = messageId;
  const input = document.getElementById('thread-reply-input');
  input.placeholder = `Reply to ${shortId(messageId)}`;
  input.focus();
}

export async function refreshThread() {
  if (!state.openThreadRootId) return;
  const list = document.getElementById('thread-list');
  const form = document.getElementById('thread-reply-form');
  let thread;
  try {
    thread = await api('GET', `/messages/${state.openThreadRootId}/thread`);
  } catch (err) {
    list.innerHTML = `<div class="panel-copy panel-copy-tight">${escHtml(errorMessage(err))}</div>`;
    form.hidden = true;
    return;
  }
  if (thread.length) {
    state.openThreadRootId = thread[0].message_id;
    if (!thread.some(entry => entry.message_id === state.threadReplyTargetId)) state.threadReplyTargetId = state.openThreadRootId;
  }
  const replies = Math.max(thread.length - 1, 0);
  document.getElementById('thread-title').textContent = `Thread · ${replies} ${replies === 1 ? 'reply' : 'replies'}`;
  document.getElementById('thread-copy').textContent = 'Replies stay attached to the message they answer.';
  list.innerHTML = thread.map(entry => `
    <div class="thread-item thread-depth-${Math.min(entry.thread_depth, 4)}">
      ${attribution(entry,
        `<span class="time" title="${escHtml(entry.created_at || '')}">${formatTime(entry.created_at)}</span>` +
        (entry.reply_count ? `<span class="thread-counts">${escHtml(entry.reply_count)} ${entry.reply_count === 1 ? 'reply' : 'replies'}</span>` : ''))}
      <div class="thread-body">${renderMentions(entry.content)}</div>
      <div class="msg-actions"><button class="action" data-message-id="${escHtml(entry.message_id)}" data-action="setThreadTarget">Reply to this</button></div>
    </div>`).join('');
  form.hidden = !['admin', 'editor', 'member'].includes(state.currentRoomRole);
}

export async function submitThreadReply(event) {
  event.preventDefault();
  const input = document.getElementById('thread-reply-input');
  const content = input.value.trim();
  const target = state.threadReplyTargetId || state.openThreadRootId;
  if (!content || !target) return;
  const invoke = document.getElementById('thread-invoke-agents').checked;
  input.value = '';
  try {
    const sent = await api('POST', `/messages/${target}/replies`,
      {content, invoke_mentioned_agents: invoke},
      {idempotencyKey: idempotencyKey()});
    reportUnrecognizedMentions(sent);
    await loadState();
  } catch (err) {
    input.value = content;
    toast(`Reply was not sent: ${errorMessage(err)}`, 'error');
  }
}

export async function runSearch(event) {
  event.preventDefault();
  const query = document.getElementById('search-input').value.trim();
  const scoped = document.getElementById('search-this-room').checked;
  const results = document.getElementById('search-results');
  if (!query) return;
  results.innerHTML = '<div class="panel-copy panel-copy-tight">Searching…</div>';
  try {
    const scope = scoped ? `&room_id=${encodeURIComponent(state.roomId)}` : '';
    const hits = await api('GET', `/search?q=${encodeURIComponent(query)}${scope}`);
    if (!hits.length) {
      results.innerHTML = '<div class="panel-copy panel-copy-tight">No results you are allowed to read.</div>';
      return;
    }
    results.innerHTML = hits.map(hit => `
      <div class="search-hit" tabindex="0" role="button" data-object-kind="${escHtml(hit.object_kind)}" data-object-id="${escHtml(hit.object_id)}" data-container-id="${escHtml(hit.container_id || '')}" data-room-id="${escHtml(hit.room_id)}" data-action="openSearchHit" data-enter-action="openSearchHit">
        <div class="hit-meta">#${escHtml(hit.room_name || hit.room_id)} · ${escHtml(hit.object_kind)} · ${escHtml(memberName(hit.author_id))} · ${new Date(hit.created_at).toLocaleString()}</div>
        <div>${highlightExcerpt(hit.excerpt)}</div>
      </div>`).join('');
  } catch (err) {
    results.innerHTML = `<div class="panel-copy panel-copy-tight">${escHtml(errorMessage(err))}</div>`;
  }
}

// Where each searchable kind is read. object_id addresses the object; container_id
// is the extra id its family needs, which is the whole reason the server sends it.
// Routing every kind through openThread is what painted "message not found" over a
// task, a decision or an artifact version.
export const SEARCH_HIT_TARGETS = {
  TASK: data => ({view: 'members', selector: `[data-task-id="${data.objectId}"]`}),
  DECISION: data => ({view: 'members', selector: `[data-decision-id="${data.objectId}"]`}),
  ARTIFACT_VERSION: data => ({view: 'artifacts', selector: `[data-artifact-id="${data.containerId}"]`}),
  AGENT_OUTPUT: data => ({view: 'branch', selector: `[data-output-id="${data.objectId}"]`}),
};

// A hit in another channel opens that channel first. Opening its thread while the
// main pane still showed the old one is what made the result unreadable.
export async function openSearchHit(data) {
  const hitRoomId = data.roomId;
  if (hitRoomId && hitRoomId !== state.roomId) {
    if (!state.myRooms.some(room => room.room_id === hitRoomId)) await refreshRooms();
    if (!state.myRooms.some(room => room.room_id === hitRoomId)) {
      toast('That result is in a channel this view cannot open.', 'error');
      return;
    }
    await switchRoom(hitRoomId);
  }
  if (data.objectKind === 'MESSAGE') { await openThread(data.objectId); return; }
  const target = SEARCH_HIT_TARGETS[data.objectKind];
  if (!target) { toast(`This view cannot open a ${data.objectKind} result.`, 'error'); return; }
  // An output lives in one branch, and the panel shows one branch at a time.
  if (data.objectKind === 'AGENT_OUTPUT') {
    const output = state.allRoomOutputs.find(o => o.output_id === data.objectId);
    if (output && output.branch_id && output.branch_id !== state.currentBranchId) {
      await selectBranch(output.branch_id);
    }
  }
  const {view, selector} = target(data);
  openContext(view);
  const element = document.querySelector(selector);
  if (!element) { toast('That result is not in this channel snapshot.', 'error'); return; }
  // Tasks and decisions live inside the collapsed Workspace records disclosure.
  const disclosure = element.closest('details');
  if (disclosure) disclosure.open = true;
  element.scrollIntoView({block: 'center'});
  element.classList.add('search-focus');
  setTimeout(() => element.classList.remove('search-focus'), 2400);
}

// The server brackets each match; escape first, then turn the brackets into marks.
