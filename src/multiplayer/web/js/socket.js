import { api, handleBearerUnauthorized, persistSession } from './api.js';
import { emit } from './bus.js';
import { bytesToBase64Url, logEvent, setWsStatus, toast, updateActivityLogSummary } from './util.js';
import { state } from './state.js';

export const WS_MAX_DELAY = 30000;

// Which room the current ws is subscribed to, so connectWS can tell "already
// the live socket for this room" from "stale, close it first".

// The literal text appendSystemMessage renders for a room_event, shared
// between a live/replayed frame (handleRealtimeEvent below) and a
// snapshot's own events_since (loadStateImpl below): the snapshot already
// carries the same room_events a live socket would have delivered since its
// own watermark, so applying it renders the same lines a person watching
// the socket would have seen appear live, with no replay needed to produce
// them. Returns null for every event_type that never renders one.
function systemMessageText(eventType, payload) {
  switch (eventType) {
    case 'user.joined_room':
      return `${payload.user_id} joined the room`;
    case 'user.left_room':
      return `${payload.user_id} left the room`;
    case 'user.invited_room':
      return `${payload.user_id} was invited as ${payload.role}`;
    case 'user.role_changed':
      return `${payload.user_id} is now ${payload.role}`;
    case 'user.removed_room':
      return `${payload.user_id} was removed from the channel`;
    case 'agent.joined_room':
      return `Agent ${payload.name} (${payload.role}) joined`;
    case 'room.posture_declared':
      return `Channel posture is now ${String(payload.posture || '').toLowerCase()}`;
    case 'human.interrupted_agent':
      return 'Agent interrupted by human';
    case 'human.redirected_agent':
      return `Agent redirected: ${payload.instruction || ''}`;
    default:
      return null;
  }
}

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
    loadStateOrShowReconnecting();
  };
  socket.onclose = (event) => {
    // A socket replaced by a channel switch must neither reconnect nor repaint.
    if (socket !== state.ws) return;
    if (event.code === 4403) { emit('handleAccessRevoked'); return; }
    // 4401 is the server closing on a token it no longer accepts (a revoke, a
    // rotation) — see websocket.py:259 and tests/security/test_token_auth.py.
    // Reconnecting into the same rejected credential forever is not a
    // recoverable state, so bearer mode ends the session the same way a
    // cookie 401 does (handleCookieUnauthorized in api.js). A bare 1006 is
    // deliberately left alone here: it is indistinguishable from a plain
    // network drop, and treating every dropped connection as a revoked
    // credential would sign a person out over a flaky network.
    if (event.code === 4401 && state.sessionMode === 'bearer') {
      handleBearerUnauthorized('Your access token is no longer valid.');
      return;
    }
    if (event.code === 4408) {
      // The server closes with 4408 when the cursor this handshake carried
      // names a point its room log never reached (a wiped or re-seeded
      // room, a snapshot from somewhere else). state.lastSequence only
      // ever rises (see the Math.max calls in loadStateImpl and
      // handleRealtimeEvent below), so leaving it as is would reconnect
      // with the same poisoned cursor and get the same close forever.
      // Reconnecting on 0 fixes that but is its own trap on a large room:
      // it replays the entire history from scratch. The only honest cursor
      // is the room's real head, which only a fresh snapshot carries (as
      // `latest_sequence`, not the capped `events_since` watermark this
      // module already uses for state.lastSequence elsewhere), so this
      // resets to 0 and never opens a socket again until a snapshot has
      // actually landed. `loadState()` itself, not the
      // loadStateOrShowReconnecting() wrapper: that wrapper turns a
      // rejected fetch into a resolved one (just a status update), which
      // would send this straight into the same reconnect-on-0 trap on the
      // very first failed retry. A rejection here instead waits out the
      // same backoff a plain reconnect uses before trying the snapshot
      // again, so a flaky network or a rate limit slows the retries down
      // rather than reconnecting on 0 at the speed of the failing fetch.
      // `loadState()` dedupes onto the doomed socket's own onopen fetch
      // when one is already in flight (see loadState's
      // staleCallDuringLoad handling above), so this does not double it.
      setWsStatus('Reconnecting', false);
      state.lastSequence = 0;
      const awaitSnapshotThenReconnect = () => {
        loadState().then(connectWS, () => {
          state.wsReconnectAttempts++;
          const delay = Math.min(
            state.wsReconnectDelay * Math.pow(1.5, state.wsReconnectAttempts - 1), WS_MAX_DELAY);
          state.wsReconnectTimer = setTimeout(awaitSnapshotThenReconnect, delay);
        });
      };
      awaitSnapshotThenReconnect();
      return;
    }
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
  // Settled explicitly on both branches rather than through .finally():
  // .finally()'s own callback result is discarded when the promise it is
  // attached to rejects, so a failed fetch whose staleCallDuringLoad
  // follow-up then succeeds still rejected the whole chain (a caller
  // awaiting this, the 4408 handler's own retry loop among them, took the
  // failure branch and re-armed a redundant backoff even though a fresh
  // snapshot had just landed). Returning the follow-up's own promise from
  // a .then() rejection handler instead makes this adopt whatever that
  // follow-up actually settles as.
  const settleAndMaybeRetry = (wasRejected) => (outcome) => {
    state.loadStatePromise = null;
    state.loadStatePromiseRoom = null;
    if (state.staleCallDuringLoad) {
      state.staleCallDuringLoad = false;
      return loadState();
    }
    if (wasRejected) throw outcome;
    return outcome;
  };
  state.loadStatePromise = loadStateImpl().then(
    settleAndMaybeRetry(false),
    settleAndMaybeRetry(true),
  );
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
  emit('renderRoomsList');
  emit('updateRoomHeader');

  // Messages. A reply lives in its thread unless it was explicitly broadcast, and
  // the server applies that rule to the listing, so this renders what it sends.
  emit('applyReadCursor', snapshot.read_cursor);
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
  // events_since carries the same room_events a live socket would have
  // delivered since the snapshot's own watermark; the ones among them that
  // render a system line (systemMessageText above) become synthetic
  // messages here, keyed by their own event_id exactly like a real message
  // is keyed by its message_id, so reconcileMessages gives them the same
  // zero-mutation guarantee on an unchanged reload that it already gives
  // every real message, instead of a fresh remove-and-recreate on every
  // single call (a keyless node, the shape a live appendSystemMessage still
  // uses, is exactly what reconcileMessages' own sweep always removes).
  // Sorted in with snapshot.messages by the same `sequence` field both
  // carry, not appended after them: a fixed tail position would drift out
  // from under a system line the moment a newer real message lands (that
  // message's own live appendMessage anchors to whatever is physically last
  // right now), forcing the very next reconcile to move the system line
  // past it and, with it, blur focus a person had inside that new message.
  // That is exactly the class of bug lastMessageElement/appendMessage exists
  // to avoid. Sequence order is also what a real replay would have delivered:
  // every room_event, message.created included, used to land over the
  // socket in that same order.
  const systemEventMessages = snapshot.events_since
    .map(event => ({ event, text: systemMessageText(event.event_type, event.payload) }))
    .filter(({ text }) => text)
    .map(({ event, text }) => ({
      message_id: event.event_id,
      role: 'system',
      sender_id: 'system',
      content: text,
      sequence: event.sequence,
      created_at: event.timestamp,
    }));
  const messagesWithSystemLines = [...snapshot.messages, ...systemEventMessages]
    .sort((a, b) => (a.sequence || 0) - (b.sequence || 0));
  emit('reconcileMessages',
    messagesWithSystemLines,
    new Set(Array.from(liveDuringFetchMessageIds, id => `m:${id}`))
  );

  // Agents
  state.roomAgents = snapshot.agents || [];
  emit('renderAgents', snapshot.agents);
  emit('renderSidebarAgents', snapshot.agents);

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
  state.outputSelections.clear();
  (snapshot.output_selections || []).forEach(selection => {
    if (!state.currentBranchId || selection.branch_id === state.currentBranchId) {
      state.outputSelections.set(selection.output_id, selection.disposition.toLowerCase());
    }
  });
  emit('renderBranches', durableBranches, snapshot.runs || []);
  emit('renderOutputs', state.roomOutputs, snapshot.runs || []);
  emit('renderResumeRunsBanner', snapshot.runs || []);
  // renderBranches() just appended branch-activity cards below the last chat message,
  // after reconcileMessages()'s own auto-scroll already ran — so the newest item in
  // the transcript was landing half behind the composer until a manual scroll.
  // Re-settle once this frame's layout is final.
  emit('scrollMessagesToBottom');
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
    document.getElementById('turn-lock-title').textContent = `${lockBranch ? emit('branchTitle', lockBranch) : 'A specialist'} is working on this turn`;
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
  emit('applyPermissions');
  emit('renderPosture', snapshot.room.posture);
  state.roomOntology = snapshot.ontology || {entities: [], relationships: [], reviews: []};
  emit('renderOntology', state.roomOntology);

  // Tasks
  emit('renderTasks', snapshot.tasks);

  // Approvals
  emit('renderApprovals', snapshot.pending_approvals);

  // Decisions render first: the artifact reader below embeds the same cards,
  // so lastDecisions must already be current when renderArtifacts reads it.
  emit('renderDecisions', snapshot.decisions);

  // Artifacts
  emit('renderArtifacts', snapshot.artifacts);
  document.getElementById('artifact-nav-count').textContent = snapshot.artifacts.length;

  // Members
  emit('renderMembers', snapshot.members);

  // Rebuild the canonical audit trail rather than leaving the event panel
  // empty after a page reload. Repeated WS onopen hydration cannot duplicate it.
  const eventsLog = document.getElementById('events-log');
  eventsLog.innerHTML = snapshot.events_since.length ? '' : '<div class="events-empty">Nothing yet.</div>';
  snapshot.events_since.forEach(event => logEvent(event));
  updateActivityLogSummary();
  const snapshotSequence = snapshot.events_since.length > 0
    ? Math.max(...snapshot.events_since.map(event => event.sequence))
    : 0;
  // The room's real head, not the events_since page's own capped watermark:
  // a room past the state route's events_limit (500 by default) has a head
  // higher than any sequence events_since actually carries, and a 4408
  // reconnect (see onclose above) needs that real number, not one that
  // undercounts it and pulls the whole gap back down through the socket.
  state.lastSequence = Math.max(state.lastSequence, snapshotSequence, snapshot.latest_sequence ?? 0);
  // Anything buffered while this fetch was in flight and not already covered
  // by the snapshot itself gets replayed now, so it lands on the fresh state
  // instead of vanishing into the rebuild above. message.created is a real
  // room_event and does join events_since (see the 'message.created' case
  // below) -- but snapshotSequence still cannot be trusted to watermark one
  // reliably: get_room_state (audit.py) reads the room's head first and
  // bounds both messages and events_since by that same number, so a message
  // created in the gap between that read and the rest is excluded from both
  // consistently rather than landing in one but not the other, but
  // get_room_events' events_since read is still capped by the state route's
  // events_limit (500 by default, 1000 at most), so a busy-enough room can
  // cut it before every event since last_sequence is back. A buffered
  // message.created for a message reconcileMessages just rendered from
  // snapshot.messages above can still
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

  if (state.openThreadRootId) await emit('refreshThread');
  emit('updateRoomHeader');
  emit('refreshNotificationDot');
  state.roomUnreadCounts.delete(state.roomId);
  emit('refreshOtherRoomUnreads');
}

// Every loadState() call in this file from here down is triggered by the
// socket itself, not by a person's own click — there is no button to put a
// field-error or toast on, and no click handler waiting to show one. A
// dropped connection or a mid-flight 401 already reads as "Reconnecting" to
// a person watching #ws-status, so a refresh that this same socket
// triggered failing for the same kind of reason reads the same way, instead
// of vanishing as a rejection nothing on the page ever surfaces. A control-
// triggered fetch (approveAction, sendMessage, declarePosture, ...) is a
// different case entirely and keeps its own try/toast handling untouched.
function loadStateOrShowReconnecting(onSuccess) {
  return loadState().then(onSuccess, () => setWsStatus('Reconnecting', false));
}

export function handleRealtimeEvent(msg) {
  if (msg.type === 'ping' || msg.type === 'pong' || msg.type === 'connected') return;
  if (msg.type === 'room_removed') {
    // Removal from the open channel arrives as a 4403 close on its socket instead.
    if (msg.room_id === state.roomId) return;
    const removedRoom = state.myRooms.find(room => room.room_id === msg.room_id);
    state.myRooms = state.myRooms.filter(room => room.room_id !== msg.room_id);
    emit('renderRoomsList');
    toast(`You were removed from #${removedRoom ? removedRoom.name : msg.room_id}.`, 'error');
    return;
  }
  if (msg.type === 'room_invited') {
    emit('refreshRooms').then(() => toast(`You were invited to #${msg.room_name} as ${msg.role}.`));
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
      loadStateOrShowReconnecting().finally(() => { state.resyncRequested = false; });
    }
    state.lastSequence = Math.max(state.lastSequence, msg.sequence || 0);
    logEvent(msg);
    switch(msg.event_type) {
      case 'message.created':
        if (msg.payload.parent_message_id && !msg.payload.broadcast_to_room) {
          // A thread reply belongs to its thread, not to the channel log.
          loadStateOrShowReconnecting();
          break;
        }
        emit('appendMessage', {message_id: msg.payload.message_id, role: msg.payload.role,
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
        emit('scheduleRefreshUnread');
        break;
      case 'message.reaction_added': case 'message.reaction_removed':
        loadStateOrShowReconnecting();
        break;
      case 'user.joined_room':
        emit('appendSystemMessage', systemMessageText('user.joined_room', msg.payload));
        break;
      case 'user.left_room':
        emit('appendSystemMessage', systemMessageText('user.left_room', msg.payload));
        break;
      // A reload's own reconcile already renders this event as a keyed
      // synthetic line from events_since (systemMessageText/loadStateImpl
      // above). Appending a second, keyless copy here as well used to
      // double every one of these on screen until an unrelated reconcile
      // swept the keyless half away.
      case 'user.invited_room':
        loadStateOrShowReconnecting();
        break;
      case 'user.role_changed':
        loadStateOrShowReconnecting();
        break;
      case 'user.removed_room':
        loadStateOrShowReconnecting();
        break;
      case 'agent.joined_room':
        emit('appendSystemMessage', systemMessageText('agent.joined_room', msg.payload));
        loadStateOrShowReconnecting();
        break;
      case 'agent.status_changed':
        loadStateOrShowReconnecting();
        break;
      // An agent removed out of band (another tab, another admin) used to
      // have no handler at all: the Agents panel kept showing it until some
      // unrelated snapshot refresh happened to catch up. loadState() re-reads
      // the room's agent roster the same way every other membership event
      // here does.
      case 'agent.left_room':
        loadStateOrShowReconnecting();
        break;
      case 'agent.run.started': case 'agent.output.created': case 'agent.run.completed':
      case 'output.selection.updated': case 'artifact.decision_brief_synthesized':
      case 'artifact.synthesis_published':
      case 'ontology.materialized': case 'ontology.assertion_confirmed':
      case 'ontology.assertion_corrected':
        loadStateOrShowReconnecting();
        break;
      case 'task.created': case 'task.assigned': case 'task.completed': case 'task.cancelled':
        loadStateOrShowReconnecting();
        break;
      case 'approval.requested': case 'approval.granted': case 'approval.rejected':
        loadStateOrShowReconnecting();
        break;
      case 'room.posture_declared':
        loadStateOrShowReconnecting();
        break;
      case 'artifact.created': case 'artifact.version_created':
        loadStateOrShowReconnecting();
        break;
      case 'decision.created':
        loadStateOrShowReconnecting();
        break;
      case 'human.interrupted_agent':
        emit('appendSystemMessage', systemMessageText('human.interrupted_agent', msg.payload));
        break;
      case 'human.redirected_agent':
        emit('appendSystemMessage', systemMessageText('human.redirected_agent', msg.payload));
        break;
      default:
        break;
    }
  }
}
