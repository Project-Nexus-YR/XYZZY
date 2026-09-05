import { api, rememberRoomId } from './api.js';
import { emit } from './bus.js';
import { closeModal, closeSidebarDrawer, currentCenterView, openCenterView, openContext, openModal } from './shell.js';
import { errorMessage, escHtml, setWsStatus, toast } from './util.js';
import { state } from './state.js';

export function renderRoomsList() {
  const list = document.getElementById('rooms-list');
  if (!state.myRooms.length) {
    list.innerHTML = '<div class="nav-item"><span class="nav-icon">#</span><span class="nav-name">No channels</span></div>';
    return;
  }
  const roomIsActiveView = currentCenterView() === 'conversation';
  list.innerHTML = state.myRooms.map(room => {
    const unread = room.room_id === state.roomId ? 0 : (state.roomUnreadCounts.get(room.room_id) || 0);
    return `<button class="nav-item${room.room_id === state.roomId && roomIsActiveView ? ' active' : ''}" data-room-id="${escHtml(room.room_id)}" data-action="switchRoom" aria-label="#${escHtml(room.name)}${unread ? `, ${unread} unread` : ''}"><span class="nav-icon">#</span><span class="nav-name">${escHtml(room.name)}</span>${unread ? `<span class="nav-meta">${unread}</span>` : ''}</button>`;
  }).join('');
}

export async function refreshRooms() {
  const context = await api('GET', '/me/context');
  state.myRooms = context.rooms;
  renderRoomsList();
  refreshOtherRoomUnreads();
}

// Quiet unread counts for the sidebar's other rooms. My rooms are few, so a
// read-cursor call plus an events-since-cursor call per other room (capped at
// 10) is cheap; there is no lighter existing route that carries both a room's
// read position and its latest sequence in one call.
export async function refreshOtherRoomUnreads() {
  const others = state.myRooms.filter(room => room.room_id !== state.roomId).slice(0, 10);
  await Promise.allSettled(others.map(async room => {
    try {
      const cursor = await api('GET', `/rooms/${room.room_id}/read-cursor`);
      const events = await api('GET', `/rooms/${room.room_id}/events?after=${cursor.last_read_sequence || 0}`);
      const unread = events.filter(e => e.event_type === 'message.created').length;
      if (unread > 0) state.roomUnreadCounts.set(room.room_id, unread);
      else state.roomUnreadCounts.delete(room.room_id);
    } catch {
      state.roomUnreadCounts.delete(room.room_id);
    }
  }));
  renderRoomsList();
}

export async function switchRoom(targetRoomId) {
  const room = state.myRooms.find(item => item.room_id === targetRoomId);
  if (!room) return;
  if (targetRoomId === state.roomId) {
    // Already in this room: the click still means "take me back to the channel".
    closeSidebarDrawer();
    openCenterView('conversation');
    renderRoomsList();
    return;
  }
  state.roomId = room.room_id;
  state.workspaceId = room.workspace_id;
  rememberRoomId(state.roomId);
  state.currentBranchId = '';
  state.lastSequence = 0;
  state.openThreadRootId = '';
  state.threadReplyTargetId = '';
  state.readCursor = 0;
  state.roomUnreadCounts.delete(state.roomId);
  closeSidebarDrawer();
  openCenterView('conversation');
  renderRoomsList();
  emit('closeSocket', state.ws);
  // The snapshot has to settle, and this tab's own lastSequence has to reach
  // its final value from that snapshot, before the socket subscribes: opening
  // both at once (the old order here) left a gap an event could land in and
  // reach neither the snapshot nor the not-yet-subscribed socket. Fetching
  // first means connectWS's own last_sequence cursor names exactly where this
  // snapshot ended, matching the field the /state fetch already sends.
  try { await emit('loadState'); }
  catch (err) { toast(`Could not open #${room.name}: ${errorMessage(err)}`, 'error'); }
  // A second switchRoom called before this one's own await settled moved
  // roomId on again; loadState() above resolved on this call's own (by then
  // abandoned) fetch, not on a snapshot for whatever room is current now.
  // The later call is the one whose own await will resolve on a real fetch
  // for that room, and connect once it does — this one connecting instead
  // would open the socket before that snapshot exists.
  if (state.roomId !== targetRoomId) return;
  emit('connectWS');
}

export function openCreateChannelModal() {
  openModal(`
    <h3>Create channel</h3>
    <form data-submit-action="submitCreateChannel">
      <label for="new-channel-name">Name</label>
      <input id="new-channel-name" required autocomplete="off" placeholder="e.g. incident-response">
      <label for="new-channel-description">Description</label>
      <input id="new-channel-description" autocomplete="off" placeholder="Optional">
      <div class="field-error hidden" id="new-channel-error"></div>
      <div class="modal-actions">
        <button type="button" class="btn-sm" data-action="closeModal">Cancel</button>
        <button type="submit" class="btn-primary" id="new-channel-submit">Create</button>
      </div>
    </form>
  `);
}

export async function submitCreateChannel(event) {
  event.preventDefault();
  const name = document.getElementById('new-channel-name').value.trim();
  const description = document.getElementById('new-channel-description').value.trim();
  const errorEl = document.getElementById('new-channel-error');
  const submitButton = document.getElementById('new-channel-submit');
  if (!name) return;
  submitButton.disabled = true;
  try {
    // This is a room-creation write in an already-discovered workspace, not a setup-time
    // org/workspace bootstrap — the path is built separately so it reads as what it is.
    const createRoomPath = `/workspaces/${state.workspaceId}/rooms`;
    const room = await api('POST', createRoomPath, {name, description});
    closeModal();
    await refreshRooms();
    await switchRoom(room.room_id);
    toast(`#${room.name} created.`);
  } catch (err) {
    errorEl.textContent = errorMessage(err);
    errorEl.classList.remove('hidden');
    submitButton.disabled = false;
  }
}

export async function openBrowseChannels() {
  openContext('browse');
  const list = document.getElementById('browse-channels-list');
  list.innerHTML = '<div class="panel-section"><div class="panel-copy">Loading…</div></div>';
  try {
    const rooms = await api('GET', `/workspaces/${state.workspaceId}/rooms`);
    if (!rooms.length) { list.innerHTML = '<div class="panel-section"><div class="panel-copy">No channels in this workspace yet.</div></div>'; return; }
    list.innerHTML = rooms.map(room => {
      const mine = state.myRooms.some(item => item.room_id === room.room_id);
      const action = mine
        ? `<button class="btn-sm" data-action="openBrowsedRoom" data-room-id="${escHtml(room.room_id)}">Open</button>`
        : `<span class="detail">by invitation</span>`;
      return `<div class="browse-room-row"><div><div class="title">#${escHtml(room.name)}</div><div class="detail">${escHtml(room.description || 'No description')}</div></div>${action}</div>`;
    }).join('');
  } catch (err) {
    list.innerHTML = `<div class="panel-section"><div class="panel-copy">${escHtml(errorMessage(err))}</div></div>`;
  }
}

export function openLeaveChannelConfirm() {
  const name = state.currentRoomName || 'this channel';
  openModal(`
    <h3>Leave #${escHtml(name)}?</h3>
    <p>You will need a new invitation to rejoin.</p>
    <div class="field-error hidden" id="leave-channel-error"></div>
    <div class="modal-actions">
      <button type="button" class="btn-sm" data-action="closeModal">Cancel</button>
      <button type="button" class="btn-primary" data-action="submitLeaveChannel">Leave channel</button>
    </div>
  `);
}

export async function submitLeaveChannel() {
  const errorEl = document.getElementById('leave-channel-error');
  const leftRoomId = state.roomId;
  const leftName = state.currentRoomName;
  try {
    await api('POST', `/rooms/${leftRoomId}/leave`);
    closeModal();
    state.myRooms = state.myRooms.filter(item => item.room_id !== leftRoomId);
    toast(`Left #${leftName}.`);
    if (state.myRooms.length) {
      await switchRoom(state.myRooms[0].room_id);
    } else {
      state.roomId = '';
      location.reload();
    }
  } catch (err) {
    errorEl.textContent = errorMessage(err);
    errorEl.classList.remove('hidden');
  }
}

export function openInvitePeople() {
  openContext('members');
  requestAnimationFrame(() => document.getElementById('invite-user-id')?.focus());
}

export function handleAccessRevoked() {
  const removedRoom = state.myRooms.find(item => item.room_id === state.roomId);
  const removedName = removedRoom ? removedRoom.name : 'this channel';
  state.myRooms = state.myRooms.filter(item => item.room_id !== state.roomId);
  toast(`You were removed from #${removedName}.`, 'error');
  setWsStatus('Removed', false);
  if (state.myRooms.length) {
    state.roomId = '';
    switchRoom(state.myRooms[0].room_id);
    return;
  }
  state.ws = null;
  rememberRoomId('');
  renderRoomsList();
  document.getElementById('messages').innerHTML = '';
  emit('appendSystemMessage', `You no longer have access to #${removedName}. Ask an admin for a new invitation.`);
  document.getElementById('msg-input').disabled = true;
  document.getElementById('send-message-button').disabled = true;
}

export async function inviteMember(event) {
  event.preventDefault();
  const input = document.getElementById('invite-user-id');
  const invitedUserId = input.value.trim();
  const role = document.getElementById('invite-role').value;
  if (!invitedUserId) return;
  try {
    await api('POST', `/rooms/${state.roomId}/members/invitations`, {user_id: invitedUserId, role});
    input.value = '';
    toast(`Invited ${invitedUserId} as ${role}.`);
    await emit('loadState');
  } catch (err) { toast(`Could not invite ${invitedUserId}: ${errorMessage(err)}`, 'error'); }
}

export async function changeMemberRole(memberUserId, role) {
  try {
    await api('PATCH', `/rooms/${state.roomId}/members/${encodeURIComponent(memberUserId)}`, {role});
    toast(`${memberUserId} is now ${role}.`);
  } catch (err) { toast(`Could not change access for ${memberUserId}: ${errorMessage(err)}`, 'error'); }
  await emit('loadState');
}

export async function removeMember(memberUserId) {
  try {
    await api('DELETE', `/rooms/${state.roomId}/members/${encodeURIComponent(memberUserId)}`);
    toast(`Removed ${memberUserId} from the channel.`);
    await emit('loadState');
  } catch (err) { toast(`Could not remove ${memberUserId}: ${errorMessage(err)}`, 'error'); }
}
