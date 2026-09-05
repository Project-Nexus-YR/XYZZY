import { API, COOKIE_401_GUARD_KEY, SESSION_STORAGE_KEY, api, clearStoredSession, persistSession, readStoredSession, rememberRoomId, storedRoomId } from './api.js';
import { loadTemplates } from './branch.js';
import { connectWS, loadState } from './socket.js';
import { errorMessage, toast } from './util.js';
import { state } from './state.js';

export async function signOut() {
  if (state.sessionMode === 'cookie') {
    try { await api('POST', '/auth/logout', null, {skipAuthRedirect: true}); }
    catch (_) { /* best effort — clearing local state still ends the session here */ }
  }
  state.sessionMode = '';
  state.accessToken = '';
  clearStoredSession();
  rememberRoomId('');
  location.reload();
}

// A wrong token used to surface only as a corner toast, gone in seconds and
// easy to miss. This puts the failure on the field itself: an error border,
// plain words, and focus — and it clears the moment the person types again.
export function showTokenError(message) {
  const input = document.getElementById('setup-token');
  const err = document.getElementById('setup-token-error');
  input.classList.add('error');
  err.textContent = message;
  err.classList.remove('hidden');
  input.focus();
}
export function clearTokenError() {
  document.getElementById('setup-token').classList.remove('error');
  document.getElementById('setup-token-error').classList.add('hidden');
}

export async function setup() {
  state.userName = document.getElementById('setup-name').value.trim();
  state.accessToken = document.getElementById('setup-token').value;
  const roomName = document.getElementById('setup-room').value.trim();
  clearTokenError();
  if (!state.accessToken) { showTokenError('Enter your access token.'); return; }
  state.sessionMode = 'bearer';
  // A fresh sign-in with a new token undoes whatever the last one's revocation
  // set: without this, a second 4401 (or 401) on this token never runs
  // handleBearerUnauthorized again, since the guard from the previous
  // session's teardown is still latched true.
  state.bearerSessionEnding = false;
  const setupButton = document.getElementById('setup-button');
  setupButton.disabled = true;
  setupButton.textContent = 'Opening…';

  try {
    // Discovery is authenticated and membership-scoped. A reload, stale browser
    // storage, or a second browser can therefore resume without replaying setup
    // writes. Only the non-secret active room ID is stored in the browser.
    const context = await api('GET', '/me/context');
    state.userId = context.user_id;
    state.myRooms = context.rooms;
    const savedRoomId = storedRoomId();
    const roomsNamedForDecision = context.rooms.filter(room => room.name === roomName);
    // Returning members land in a channel they belong to; the channel field only
    // matters on the first sign-in, where it names the channel being created.
    const room = context.rooms.find(item => item.room_id === savedRoomId)
      || roomsNamedForDecision[0]
      || context.rooms[0];

    // No room means this token has nothing to resume into: only then does the
    // first-run pair (display name + channel) actually matter.
    if (!room && (!state.userName || !roomName)) {
      toast('First sign-in creates your channel — give it a name and yourself a display name.', 'error');
      setupButton.disabled = false;
      setupButton.textContent = 'Enter workspace';
      const firstRun = document.getElementById('setup-first-run');
      firstRun.open = true;
      document.getElementById(state.userName ? 'setup-room' : 'setup-name').focus();
      return;
    }
    if (room) {
      state.roomId = room.room_id;
      state.workspaceId = room.workspace_id;
      const existingWorkspace = context.workspaces.find(
        item => item.workspace_id === state.workspaceId
      );
      state.orgId = existingWorkspace ? existingWorkspace.org_id : '';
    } else {
      // The server owns the cold-start idempotency boundary. Two tabs can both
      // observe an empty context and still receive this same atomic hierarchy.
      const bootstrap = await api('POST', '/me/bootstrap', {
        display_name: state.userName, room_name: roomName
      });
      state.orgId = bootstrap.organization.org_id;
      state.workspaceId = bootstrap.workspace.workspace_id;
      state.roomId = bootstrap.room.room_id;
    }

    rememberRoomId(state.roomId);
    persistSession();
    showWorkspace();
    // connectWS()'s onopen already rehydrates state once the socket is live; a second,
    // explicit loadState() fired here raced it for the same room snapshot and the
    // dev server 401'd whichever request lost the race, quietly dropping the session.
    await loadTemplates();
  } catch (err) {
    state.sessionMode = '';
    state.accessToken = '';
    document.getElementById('setup-token').value = '';
    if (err.status === 401) {
      showTokenError("That access token wasn't accepted.");
    } else {
      toast(`Could not open workspace: ${errorMessage(err)}`, 'error');
    }
  } finally {
    setupButton.disabled = false;
    setupButton.textContent = 'Enter workspace';
  }
}

// A page reload used to drop straight back to the sign-in screen even though the
// token was still good for this tab. sessionStorage is per-tab, so it keeps the
// same promise the setup card makes ("your token stays in this tab").
export async function resumeStoredSession() {
  const saved = readStoredSession();
  if (!saved || !saved.token) return;
  // The setup form stays visible until this settles; block it so an impatient
  // click cannot start a second, colliding sign-in while this one is in flight.
  const setupButton = document.getElementById('setup-button');
  setupButton.disabled = true;
  state.accessToken = saved.token;
  state.userName = saved.name || '';
  state.sessionMode = 'bearer';
  state.bearerSessionEnding = false;
  try {
    const context = await api('GET', '/me/context');
    state.userId = context.user_id;
    state.myRooms = context.rooms;
    const room = context.rooms.find(item => item.room_id === storedRoomId()) || context.rooms[0];
    if (!room) throw new Error('No channels for this session.');
    state.roomId = room.room_id;
    state.workspaceId = room.workspace_id;
    const existingWorkspace = context.workspaces.find(item => item.workspace_id === state.workspaceId);
    state.orgId = existingWorkspace ? existingWorkspace.org_id : '';
    rememberRoomId(state.roomId);
    showWorkspace();
    // connectWS()'s onopen already rehydrates state once the socket is live; a second,
    // explicit loadState() fired here raced it for the same room snapshot and the
    // dev server 401'd whichever request lost the race, quietly dropping the session.
    await loadTemplates();
  } catch (_) {
    state.sessionMode = '';
    state.accessToken = '';
    state.userName = '';
    clearStoredSession();
  } finally {
    setupButton.disabled = false;
  }
}

export async function showWorkspace() {
  document.getElementById('setup-screen').style.display = 'none';
  document.getElementById('app-header').style.display = 'flex';
  document.getElementById('app-main').style.display = 'grid';
  // Quiet and persistent rather than a banner: a demo session looks like any
  // other workspace except for this one line, so nothing about the product
  // being explored feels like a sandboxed toy.
  document.getElementById('workspace-sub').textContent =
    state.ssoConfig.demo ? 'Demo workspace' : 'Shared decision space';
  // The snapshot has to settle, and state.lastSequence has to reach its real
  // value from it, before the socket subscribes: the same order switchRoom
  // (rooms.js) already uses for a room switch. A socket opened first sends
  // last_sequence=0, so the server replays the room's entire log instead of
  // just what this snapshot did not already carry.
  try { await loadState(); }
  catch (_) { /* connectWS's own onopen retries the snapshot once it is live */ }
  connectWS();
}

// SSO primary vs. today's token form, decided once from /auth/config. sso:false
// renders exactly as before — the token fields stay open and primary, no button.
// The left intro's step 1 is driven by this same config so it never describes
// a sign-in path the card itself isn't offering.
export function renderSetupScreen() {
  const card = document.querySelector('.setup-card');
  const ssoButton = document.getElementById('setup-sso-button');
  const demoButton = document.getElementById('setup-demo-button');
  const details = document.getElementById('setup-token-details');
  const summary = document.getElementById('setup-token-summary');
  const intro = document.getElementById('setup-card-intro');
  const step1 = document.getElementById('setup-step-1-text');
  demoButton.style.display = state.ssoConfig.demo ? 'block' : 'none';
  if (state.ssoConfig.sso) {
    card.classList.add('sso-mode');
    ssoButton.textContent = `Continue with ${state.ssoConfig.provider_label}`;
    ssoButton.style.display = 'block';
    summary.style.display = 'block';
    summary.textContent = 'Use an access token instead';
    details.open = false;
    intro.textContent = `Sign in with ${state.ssoConfig.provider_label}. One click — your team's sign-in service does the rest.`;
    step1.textContent = `Sign in with your team's ${state.ssoConfig.provider_label}`;
  } else {
    card.classList.remove('sso-mode');
    ssoButton.style.display = 'none';
    summary.style.display = 'none';
    details.open = true;
    step1.textContent = 'Sign in with your access token';
  }
  // Demo mode is refused server-side alongside SSO or a real token list, so
  // this always wins last: nothing above describes a path the card also
  // offers a one-click shortcut around.
  if (state.ssoConfig.demo) {
    intro.textContent = 'One click, no account, no API key — explore a workspace already mid-decision.';
    step1.textContent = 'Explore the demo workspace';
  }
  // Config has now answered which variant this card shows; reveal it in one
  // shot instead of letting the pre-JS default (the token form, open) flash
  // before a resolved SSO config swaps it out from under the person reading it.
  document.getElementById('setup-card-variant').classList.remove('hidden');
}

export function startSsoLogin() {
  location.href = `${API}/auth/login`;
}

// The demo token is fixed server-side ("demo") and the workspace it opens is
// seeded once at startup — no token field, no channel field, because there is
// exactly one demo workspace to enter.
export async function enterDemoWorkspace() {
  const button = document.getElementById('setup-demo-button');
  button.disabled = true;
  button.textContent = 'Opening…';
  state.accessToken = 'demo';
  state.userName = 'Yasser';
  state.sessionMode = 'bearer';
  state.bearerSessionEnding = false;
  try {
    const context = await api('GET', '/me/context');
    state.userId = context.user_id;
    state.myRooms = context.rooms;
    const room = context.rooms.find(item => item.room_id === storedRoomId()) || context.rooms[0];
    if (!room) throw new Error('The demo workspace has no channel yet.');
    state.roomId = room.room_id;
    state.workspaceId = room.workspace_id;
    const existingWorkspace = context.workspaces.find(item => item.workspace_id === state.workspaceId);
    state.orgId = existingWorkspace ? existingWorkspace.org_id : '';
    rememberRoomId(state.roomId);
    persistSession();
    showWorkspace();
    await loadTemplates();
  } catch (err) {
    state.sessionMode = '';
    state.accessToken = '';
    toast(`Could not open the demo workspace: ${errorMessage(err)}`, 'error');
  } finally {
    button.disabled = false;
    button.textContent = 'Explore the demo workspace';
  }
}

// <summary> toggles its <details> on click already, but the critic found it
// wasn't reliably keyboard-focusable — tabindex + role="button" fix that, and
// this keeps Enter/Space toggling behavior explicit rather than assumed.
export function handleSummaryKey(event) {
  if (event.key !== 'Enter' && event.key !== ' ') return;
  event.preventDefault();
  document.getElementById('setup-token-details').open = !document.getElementById('setup-token-details').open;
}

// One listener keeps aria-expanded true whenever the details is actually open,
// regardless of whether it got there by mouse click, the keydown handler
// above, or renderSetupScreen setting .open directly.
document.getElementById('setup-token-details').addEventListener('toggle', () => {
  document.getElementById('setup-token-summary')
    .setAttribute('aria-expanded', String(document.getElementById('setup-token-details').open));
});

// A signed-in person with no rooms is one input away from a workspace, not a
// stranger: the card swaps the sign-in choices for that single input.
export function renderCookieFirstRun() {
  const ssoButton = document.getElementById('setup-sso-button');
  const details = document.getElementById('setup-token-details');
  const intro = document.getElementById('setup-card-intro');
  ssoButton.style.display = 'none';
  details.open = true;
  document.getElementById('setup-token-summary').style.display = 'none';
  // The name and token fields belong to the credential flows; this person is
  // already signed in, so only the channel input remains.
  for (const selector of ['label[for="setup-name"]', '#setup-name', 'label[for="setup-token"]', '#setup-token']) {
    const el = document.querySelector(selector);
    if (el) el.style.display = 'none';
    if (el && el.nextElementSibling && el.nextElementSibling.classList.contains('field-help')) {
      el.nextElementSibling.style.display = 'none';
    }
  }
  intro.textContent = "You're signed in. Name your first channel to open the workspace.";
  const firstRun = document.getElementById('setup-first-run');
  firstRun.open = true;
  firstRun.querySelector('summary').style.display = 'none';
  const button = document.getElementById('setup-button');
  button.textContent = 'Create channel';
  button.onclick = createFirstChannelFromCookieSession;
  document.getElementById('setup-room').focus();
}

export async function createFirstChannelFromCookieSession() {
  const roomName = document.getElementById('setup-room').value.trim();
  if (!roomName) return toast('First sign-in creates your channel — give it a name.', 'error');
  const button = document.getElementById('setup-button');
  button.disabled = true;
  try {
    // The users row already holds the provider's name; bootstrap never
    // overwrites it, so the placeholder display name here can never surface.
    await api('POST', '/me/bootstrap', {display_name: state.userId, room_name: roomName});
    location.reload();
  } catch (err) {
    toast(`Could not create the channel: ${errorMessage(err)}`, 'error');
    button.disabled = false;
  }
}

// Cookie mode never puts a credential in JS: signed-in-ness is decided by asking
// the server, not by reading a token this tab was never given. A 401 here just
// means there is no cookie session — the setup screen (already rendered) stands.
export async function tryCookieSession() {
  try {
    const context = await api('GET', '/me/context', null, {skipAuthRedirect: true});
    state.sessionMode = 'cookie';
    state.userId = context.user_id;
    state.myRooms = context.rooms;
    const room = context.rooms.find(item => item.room_id === storedRoomId()) || context.rooms[0];
    if (!room) {
      // Signed in, but no room yet: the one thing left to do is name the first
      // channel. Sending this person back to the sign-in screen they just came
      // from would read as a failed login.
      renderCookieFirstRun();
      return;
    }
    state.roomId = room.room_id;
    state.workspaceId = room.workspace_id;
    const existingWorkspace = context.workspaces.find(item => item.workspace_id === state.workspaceId);
    state.orgId = existingWorkspace ? existingWorkspace.org_id : '';
    rememberRoomId(state.roomId);
    try { sessionStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify({mode: 'cookie'})); }
    catch (_) { /* Private browsing can disable storage; the tab still holds it in memory. */ }
    try { sessionStorage.removeItem(COOKIE_401_GUARD_KEY); } catch (_) { /* nothing to clear */ }
    showWorkspace();
    await loadTemplates();
  } catch (_) {
    state.sessionMode = '';
  }
}

export async function initAuth() {
  try {
    const r = await fetch(`${API}/auth/config`);
    if (r.ok) state.ssoConfig = await r.json();
  } catch (_) { /* default sso:false stands — today's token form renders */ }
  renderSetupScreen();

  const saved = readStoredSession();
  if (saved && saved.token) {
    await resumeStoredSession();
    return;
  }
  // No stored bearer session: a cookie session (fresh SSO callback, or a reload
  // after one) is only discoverable by asking — there is no token to read. But
  // /auth/config already answered that question for free, so a signed-out load
  // never has to throw a blind, console-visible 401 at /me/context to find out.
  if (state.ssoConfig.authenticated) await tryCookieSession();
}
