import { api, persistSession } from './api.js';
import { applyPermissions, branchTitle, renderBranches, renderOutputs, renderResumeRunsBanner } from './branch.js';
import { renderAgents, renderApprovals, renderArtifacts, renderDecisions, renderMembers, renderPosture, renderSidebarAgents, renderTasks } from './members.js';
import { appendMessage, appendSystemMessage, applyReadCursor, reconcileMessages, refreshNotificationDot, refreshUnread, scrollMessagesToBottom } from './messages.js';
import { renderOntology } from './ontology.js';
import { handleAccessRevoked, refreshOtherRoomUnreads, refreshRooms, renderRoomsList } from './rooms.js';
import { outputSelections, updateRoomHeader } from './shell.js';
import { refreshThread } from './thread.js';
import { bytesToBase64Url, logEvent, setWsStatus, toast, updateActivityLogSummary } from './util.js';
import { state } from './state.js';

export const WS_MAX_DELAY = 30000;
// Which room the current ws is subscribed to, so connectWS can tell "already
// the live socket for this room" from "stale, close it first".

// Nulling the handlers before close is what stops a deliberately-replaced
// socket's own onclose from ever running: onclose used to still fire after
// this tab had already moved on, see itself as an unexpected drop, and
// schedule its own reconnect — which, once it eventually fired, opened a
// second live socket nothing had asked to close, leaked on every rapid
// re-switch back to a room whose earlier socket had not finished tearing
// down yet. The `socket !== ws` guards on the remaining handlers stay as a
// second line of defense, not the only one.
export function closeSocket(socket) {
  if (!socket) return;
  socket.onopen = null;
  socket.onclose = null;
  socket.onmessage = null;
  socket.onerror = null;
  socket.close();
}

export function connectWS() {
  // Already the one live socket for this exact room: reuse it rather than
  // opening a second one alongside it.
  if (state.ws && state.wsRoomId === state.roomId
      && (state.ws.readyState === WebSocket.CONNECTING || state.ws.readyState === WebSocket.OPEN)) {
    return;
  }
  closeSocket(state.ws);
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const encRoomId = encodeURIComponent(state.roomId);
  // The same last_sequence field the /state fetch already sends: whatever
  // this tab's own snapshot most recently reached. The server replays every
  // later room_event in order, deduped, before live delivery, so a subscribe
  // that names where its snapshot ended cannot miss whatever landed between
  // that snapshot and this socket going live.
  const wsQuery = `room_id=${encRoomId}&last_sequence=${state.lastSequence}`;
  clearTimeout(state.wsReconnectTimer);
  // Cookie mode rides the cookie with no token subprotocol at all — the server
  // Origin-checks the upgrade instead, since a WS handshake can't carry the
  // custom header the HTTP CSRF gate uses.
  // The server only ever echoes back a subprotocol it selected itself — cookie
  // auth selects none (the Origin header stands in for it), and a real browser
  // aborts the handshake with a 1006 if it offered a subprotocol list the server
  // didn't pick from. So cookie mode offers none at all, matching what the server
  // will (not) select.
  let socket;
  if (state.sessionMode === 'cookie') {
    socket = new WebSocket(`${proto}://${location.host}/ws?${wsQuery}`);
  } else {
    const encodedToken = bytesToBase64Url(new TextEncoder().encode(state.accessToken));
    socket = new WebSocket(`${proto}://${location.host}/ws?${wsQuery}`,
      ['xyzzy.v1', `bearer.${encodedToken}`]);
  }
  state.ws = socket;
  state.wsRoomId = state.roomId;
  socket.onopen = () => {
    if (socket !== state.ws) return;
    setWsStatus('Connected', true);
    state.wsReconnectDelay = 1000;
    state.wsReconnectAttempts = 0;
    // Rehydrate durable runs and outputs after every reconnect. Ordered events
    // remain the synchronization source; state restores anything missed offline.
    loadState();
  };
  socket.onclose = (event) => {
    // A socket replaced by a channel switch must neither reconnect nor repaint.
    if (socket !== state.ws) return;
    if (event.code === 4403) { handleAccessRevoked(); return; }
    setWsStatus('Reconnecting', false);
    state.wsReconnectAttempts++;
    const delay = Math.min(state.wsReconnectDelay * Math.pow(1.5, state.wsReconnectAttempts - 1), WS_MAX_DELAY);
    state.wsReconnectTimer = setTimeout(connectWS, delay);
  };
  socket.onerror = () => {};
  socket.onmessage = (e) => {
    if (socket !== state.ws) return;
    try {
      const msg = JSON.parse(e.data);
      handleRealtimeEvent(msg);
    } catch(err) {
      console.error('Failed to parse WS message:', err);
    }
  };
}

// The socket's onopen rehydrates on every reconnect, and a room switch or a fresh
// sign-in also asks for the snapshot explicitly. Two such calls landing at once used
// to race for the same room state, and the dev server 401'd whichever lost — this
// collapses concurrent callers onto the one in-flight request instead of firing two.
// Which room loadStatePromise's fetch was actually issued for. A switchRoom
// call resets roomId synchronously and immediately asks for a snapshot, so a
// caller arriving while a DIFFERENT room's fetch is still in flight must not
// dedupe onto it (that fetch can only ever answer for the room it was sent
// for) — it has to wait that fetch out and then ask again for its own room.
// A caller that arrives while a fetch for its OWN room is already in flight
// used to just await that fetch's promise, silently accepting data that
// predates whatever asked for the refresh (a task created mid-fetch, say).
// staleCallDuringLoad marks that a caller was shortchanged this way, and
// once the in-flight fetch settles, one more real loadState() runs before
// anyone else's await resolves, so the caller that lost the race still ends
// up with fresh data.
// Events that land while a snapshot fetch is in flight would otherwise be lost:
// the switch below either handles them directly (fine) or calls loadState(),
// which the staleCallDuringLoad follow-up above now covers. Buffer them here
// and replay after the snapshot lands, so a direct handler (appendMessage,
// appendSystemMessage) that ran against state the fetch in flight does not yet
// reflect gets to run again once the fresh snapshot is in. A keyed chat message
// this buffer is holding is also passed to reconcileMessages below as a
// protected key, so the snapshot's own reconcile -- fetched before this
// message existed, and so silent on it -- never reads "not in this
// snapshot" as "no longer exists" and tears down a node the replay would
// otherwise have to rebuild from scratch; a keyless system notice has no
// such node to protect (it carries no id the next snapshot could reconcile
// it against), which is exactly why it needs the replay to reappear.
export function loadState() {
  if (state.loadStatePromise) {
    if (state.loadStatePromiseRoom === state.roomId) {
      state.staleCallDuringLoad = true;
      return state.loadStatePromise;
    }
    // In flight for a room this tab has already left. Let it finish (its own
    // forRoom check in loadStateImpl discards the response), then ask again
    // for whichever room is current by then, rather than firing a second
    // fetch concurrently with the first.
    return state.loadStatePromise.catch(() => {}).then(loadState);
  }
  state.loadStatePromiseRoom = state.roomId;
  // The .finally() callback below has to hand its own follow-up loadState()
  // call back to whoever is chaining onto this promise: returning it from
  // finally is what makes the returned promise here wait for that follow-up
  // too, instead of settling right after the first fetch and leaving a
  // caller's await resolved on data that was already known stale.
  state.loadStatePromise = loadStateImpl().finally(() => {
    state.loadStatePromise = null;
    state.loadStatePromiseRoom = null;
    if (state.staleCallDuringLoad) {
      state.staleCallDuringLoad = false;
      return loadState();
    }
  });
  return state.loadStatePromise;
}
export async function loadStateImpl() {
  // Frozen at call time: this fetch answers for whichever room asked for it,
  // even if switchRoom moves the tab elsewhere before the response arrives.
  const forRoom = state.roomId;
  // Named `snapshot`, not `state`: the shared app state object above is
  // already called `state`, and this is the room snapshot the server just
  // returned — a same-named local here would shadow the import for the rest
  // of this function (and throw on first use, ahead of its own assignment).
  const snapshot = await api('GET', `/rooms/${forRoom}/state?last_sequence=0`);
  if (forRoom !== state.roomId) {
    // The room changed while this was in flight. Applying this response now
    // would overwrite the room the tab is actually looking at (messages,
    // lastSequence, the read cursor it drives) with a different room's data.
    // Whatever buffered mid-flight belonged to that abandoned room's socket
    // too, so it goes with this response rather than getting replayed later
    // against the room that is actually current now.
    state.pendingEventsDuringLoad = [];
    return;
  }
  // Members are read early: message attribution below needs the name lookup, not
  // just the room-header title.
  state.roomMembers = snapshot.members || [];
  state.currentRoomName = snapshot.room.name;
  document.getElementById('room-meta').textContent = snapshot.room.description || 'No description';
  const listedRoom = state.myRooms.find(room => room.room_id === state.roomId);
  if (listedRoom) listedRoom.name = snapshot.room.name;
  else state.myRooms.push({room_id: state.roomId, workspace_id: state.workspaceId, name: snapshot.room.name});
  renderRoomsList();
  updateRoomHeader();

  // Messages. A reply lives in its thread unless it was explicitly broadcast, and
  // the server applies that rule to the listing, so this renders what it sends.
  applyReadCursor(snapshot.read_cursor);
  // A message.created that arrived live while this exact fetch was in
  // flight is already in pendingEventsDuringLoad (pushed synchronously by
  // the socket handler below, well before this await resolves) and already
  // on screen (appendMessage ran in that same synchronous turn) -- but it
  // postdates the snapshot this fetch carries, so `snapshot.messages` has
  // never heard of it and reconcileMessages' own removal pass would read
  // that as "no longer exists" and tear it down, only for the replay step
  // further below to rebuild it from scratch: one logical message, a torn-
  // down-and-rebuilt DOM node, identity and focus lost for no reason visible
  // anywhere in the user's own timeline. Named here, before the reconcile
  // runs, so it is protected rather than discovered broken afterward.
  const liveDuringFetchMessageIds = new Set(
    state.pendingEventsDuringLoad
      .filter(event => event.type === 'room_event' && event.event_type === 'message.created')
      .map(event => event.payload?.message_id)
      .filter(Boolean)
  );
  reconcileMessages(
    snapshot.messages,
    new Set(Array.from(liveDuringFetchMessageIds, id => `m:${id}`))
  );

  // Agents
  state.roomAgents = snapshot.agents || [];
  renderAgents(snapshot.agents);
  renderSidebarAgents(snapshot.agents);

  // Durable specialist runs and authored outputs
  const durableBranches = (snapshot.branches || []).filter(branch => branch.lifecycle_managed !== false);
  state.roomBranches = durableBranches;
  state.roomRuns = snapshot.runs || [];
  if (!durableBranches.some(branch => branch.branch_id === state.currentBranchId)) {
    state.currentBranchId = durableBranches.length
      ? durableBranches[durableBranches.length - 1].branch_id
      : '';
  }
  const currentBranch = durableBranches.find(branch => branch.branch_id === state.currentBranchId);
  state.currentBranchMode = currentBranch ? currentBranch.mode : '';
  state.allRoomOutputs = snapshot.outputs || [];
  state.roomOutputs = (snapshot.outputs || []).filter(output =>
    !state.currentBranchId || output.branch_id === state.currentBranchId
  );
  outputSelections.clear();
  (snapshot.output_selections || []).forEach(selection => {
    if (!state.currentBranchId || selection.branch_id === state.currentBranchId) {
      outputSelections.set(selection.output_id, selection.disposition.toLowerCase());
    }
  });
  renderBranches(durableBranches, snapshot.runs || []);
  renderOutputs(state.roomOutputs, snapshot.runs || []);
  renderResumeRunsBanner(snapshot.runs || []);
  // renderBranches() just appended branch-activity cards below the last chat message,
  // after reconcileMessages()'s own auto-scroll already ran — so the newest item in
  // the transcript was landing half behind the composer until a manual scroll.
  // Re-settle once this frame's layout is final.
  scrollMessagesToBottom();
  state.currentTurnLock = snapshot.turn_lock || null;
  const messageInput = document.getElementById('msg-input');
  const messageButton = document.getElementById('send-message-button');
  const locked = Boolean(state.currentTurnLock);
  messageInput.disabled = locked;
  messageButton.disabled = locked;
  messageInput.placeholder = locked
    ? 'AI is working on this turn'
    : `Message #${snapshot.room.name}`;
  const lockBanner = document.getElementById('turn-lock-banner');
  lockBanner.classList.toggle('visible', locked);
  if (locked) {
    const lockBranch = durableBranches.find(branch => branch.branch_id === state.currentTurnLock.branch_id);
    document.getElementById('turn-lock-title').textContent = `${lockBranch ? branchTitle(lockBranch) : 'A specialist'} is working on this turn`;
  }

  // Evidence graph and governance state are part of the reconnect snapshot.
  const currentMember = (snapshot.members || []).find(member => member.user_id === state.userId);
  state.currentRoomRole = currentMember ? currentMember.role : 'viewer';
  // The typed name at setup only matters for first-time bootstrap. Once the server
  // has a member row, its display_name is the identity everyone else sees too —
  // showing anything else here would disagree with the People panel for no reason.
  if (currentMember && currentMember.display_name && currentMember.display_name !== state.userName) {
    // Say it once when the typed name loses to the workspace profile, so the
    // discard is visible instead of silent.
    if (state.userName && !sessionStorage.getItem('xyzzy.nameNotice')) {
      toast(`Signed in as ${currentMember.display_name} — your workspace profile name applies.`);
      try { sessionStorage.setItem('xyzzy.nameNotice', '1'); } catch (_) { /* fine untracked */ }
    }
    state.userName = currentMember.display_name;
    if (state.accessToken) persistSession();
  }
  document.getElementById('identity-name').textContent = state.userName || state.userId;
  document.getElementById('identity-role').textContent = state.currentRoomRole;
  document.getElementById('identity-avatar').textContent = (state.userName || state.userId || '?').slice(0,1).toUpperCase();
  applyPermissions();
  renderPosture(snapshot.room.posture);
  state.roomOntology = snapshot.ontology || {entities: [], relationships: [], reviews: []};
  renderOntology(state.roomOntology);

  // Tasks
  renderTasks(snapshot.tasks);

  // Approvals
  renderApprovals(snapshot.pending_approvals);

  // Decisions render first: the artifact reader below embeds the same cards,
  // so lastDecisions must already be current when renderArtifacts reads it.
  renderDecisions(snapshot.decisions);

  // Artifacts
  renderArtifacts(snapshot.artifacts);
  document.getElementById('artifact-nav-count').textContent = snapshot.artifacts.length;

  // Members
  renderMembers(snapshot.members);

  // Rebuild the canonical audit trail rather than leaving the event panel
  // empty after a page reload. Repeated WS onopen hydration cannot duplicate it.
  const eventsLog = document.getElementById('events-log');
  eventsLog.innerHTML = snapshot.events_since.length ? '' : '<div class="events-empty">Nothing yet.</div>';
  snapshot.events_since.forEach(event => logEvent(event));
  updateActivityLogSummary();
  const snapshotSequence = snapshot.events_since.length > 0
    ? Math.max(...snapshot.events_since.map(event => event.sequence))
    : 0;
  state.lastSequence = Math.max(state.lastSequence, snapshotSequence);
  // Anything buffered while this fetch was in flight and not already covered
  // by the snapshot itself gets replayed now, so it lands on the fresh state
  // instead of vanishing into the rebuild above. message.created is a real
  // room_event and does join events_since (see the 'message.created' case
  // below) -- but snapshotSequence still cannot be trusted to watermark one
  // reliably: get_room_state (service.py) reads events before it reads
  // messages, so a message created in the gap between those two reads can
  // land in snapshot.messages while its own event is a beat too late for
  // the events_since read moments earlier; and get_room_events' events_since
  // read is capped by the state route's events_limit (500 by default, 1000 at
  // most), so a busy-enough room can cut it before every event since
  // last_sequence is back. Either way, a buffered message.created for a message
  // reconcileMessages just rendered from snapshot.messages above can still
  // clear the watermark and replay anyway, calling appendMessage a second
  // time for a message_id already on screen: two ".msg" elements sharing
  // one reconcileKey, the second (empty) one winning every later reconcile's
  // `existing` lookup and silently orphaning whatever the first held open
  // (a "Full output" record, a focused control) mid-DOM, connected but
  // unreachable. Excluded here by message_id rather than left to that
  // downstream symptom. liveDuringFetchMessageIds joins the set too: those
  // messages are equally already on screen (reconcileMessages just above
  // was told to leave them alone), just not through snapshot.messages.
  const renderedMessageIds = new Set([
    ...snapshot.messages.map(m => m.message_id).filter(Boolean),
    ...liveDuringFetchMessageIds,
  ]);
  const replayEvents = state.pendingEventsDuringLoad.filter(event => {
    if (event.type === 'room_event' && event.event_type === 'message.created'
        && renderedMessageIds.has(event.payload?.message_id)) return false;
    return (event.sequence || 0) > snapshotSequence;
  });
  state.pendingEventsDuringLoad = [];
  replayEvents.forEach(event => handleRealtimeEvent(event));

  if (state.openThreadRootId) await refreshThread();
  updateRoomHeader();
  refreshNotificationDot();
  state.roomUnreadCounts.delete(state.roomId);
  refreshOtherRoomUnreads();
}

export function handleRealtimeEvent(msg) {
  if (msg.type === 'ping' || msg.type === 'pong' || msg.type === 'connected') return;
  if (msg.type === 'room_removed') {
    // Removal from the open channel arrives as a 4403 close on its socket instead.
    if (msg.room_id === state.roomId) return;
    const removedRoom = state.myRooms.find(room => room.room_id === msg.room_id);
    state.myRooms = state.myRooms.filter(room => room.room_id !== msg.room_id);
    renderRoomsList();
    toast(`You were removed from #${removedRoom ? removedRoom.name : msg.room_id}.`, 'error');
    return;
  }
  if (msg.type === 'room_invited') {
    refreshRooms().then(() => toast(`You were invited to #${msg.room_name} as ${msg.role}.`));
    return;
  }
  if (msg.type === 'room_event') {
    if (state.loadStatePromise) state.pendingEventsDuringLoad.push(msg);
    // Sequence continuity: the server stamps every room_event with its
    // room's own sequence, so a delivered sequence that is not the last
    // one seen plus one is a gap this socket cannot fill on its own — the
    // cross-process fan-out is best-effort by contract (see
    // realtime/fanout.py). One resync per gap (guarded by
    // resyncRequested, cleared once the fresh snapshot lands) asks the
    // server to note it was detected, then reloads state from the room
    // event log, the single source of truth, rather than leaving a hole
    // nothing heals until an unrelated reconnect.
    if (msg.room_id === state.roomId && state.lastSequence && msg.sequence > state.lastSequence + 1
        && !state.resyncRequested) {
      state.resyncRequested = true;
      if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        state.ws.send(JSON.stringify({type: 'resync_request', room_id: state.roomId,
                                       expected: state.lastSequence + 1, got: msg.sequence}));
      }
      loadState().finally(() => { state.resyncRequested = false; });
    }
    state.lastSequence = Math.max(state.lastSequence, msg.sequence || 0);
    logEvent(msg);
    switch(msg.event_type) {
      case 'message.created':
        if (msg.payload.parent_message_id && !msg.payload.broadcast_to_room) {
          // A thread reply belongs to its thread, not to the channel log.
          loadState();
          break;
        }
        appendMessage({message_id: msg.payload.message_id, role: msg.payload.role,
                       sender_id: msg.payload.sender_id, content: msg.payload.content,
                       // The event says why an agent spoke, so a message that
                       // arrives live is as readable as one that arrives on reload.
                       metadata: {output_id: msg.payload.output_id,
                                  execution_id: msg.payload.execution_id,
                                  triggered_by: msg.payload.triggered_by,
                                  requested_by: msg.payload.requested_by},
                       is_thread_reply: Boolean(msg.payload.root_message_id),
                       sequence: msg.sequence,
                       // msg.payload carries no created_at of its own (the
                       // MESSAGE_CREATED event's payload dict never sets one —
                       // see MultiplayerService.send_message's
                       // message_event construction, outside this track's
                       // owned files). msg.timestamp is the RoomEvent's own
                       // timestamp, a separate `utcnow()` call made after the
                       // message row's, so it is close to but not exactly the
                       // message's real created_at — meaning the very next
                       // full reconcile finds this message's fingerprint
                       // "changed" against the snapshot's true value and
                       // rewrites it once. msg.payload.created_at is read
                       // first so this self-corrects for free the moment the
                       // server payload gains that field, with no further
                       // client change needed.
                       created_at: msg.payload.created_at || msg.timestamp});
        refreshUnread();
        break;
      case 'message.reaction_added': case 'message.reaction_removed':
        loadState();
        break;
      case 'user.joined_room':
        appendSystemMessage(`${msg.payload.user_id} joined the room`);
        break;
      case 'user.left_room':
        appendSystemMessage(`${msg.payload.user_id} left the room`);
        break;
      case 'user.invited_room':
        loadState().then(() => appendSystemMessage(`${msg.payload.user_id} was invited as ${msg.payload.role}`));
        break;
      case 'user.role_changed':
        loadState().then(() => appendSystemMessage(`${msg.payload.user_id} is now ${msg.payload.role}`));
        break;
      case 'user.removed_room':
        loadState().then(() => appendSystemMessage(`${msg.payload.user_id} was removed from the channel`));
        break;
      case 'agent.joined_room':
        appendSystemMessage(`Agent ${msg.payload.name} (${msg.payload.role}) joined`);
        loadState();
        break;
      case 'agent.status_changed':
        loadState();
        break;
      case 'agent.run.started': case 'agent.output.created': case 'agent.run.completed':
      case 'output.selection.updated': case 'artifact.decision_brief_synthesized':
      case 'artifact.synthesis_published':
      case 'ontology.materialized': case 'ontology.assertion_confirmed':
      case 'ontology.assertion_corrected':
        loadState();
        break;
      case 'task.created': case 'task.assigned': case 'task.completed': case 'task.cancelled':
        loadState();
        break;
      case 'approval.requested': case 'approval.granted': case 'approval.rejected':
        loadState();
        break;
      case 'room.posture_declared':
        loadState().then(() => appendSystemMessage(`Channel posture is now ${String(msg.payload.posture || '').toLowerCase()}`));
        break;
      case 'artifact.created': case 'artifact.version_created':
        loadState();
        break;
      case 'decision.created':
        loadState();
        break;
      case 'human.interrupted_agent':
        appendSystemMessage(`Agent interrupted by human`);
        break;
      case 'human.redirected_agent':
        appendSystemMessage(`Agent redirected: ${msg.payload.instruction || ''}`);
        break;
      default:
        break;
    }
  }
}
