import { toast } from './util.js';
import { state } from './state.js';

export const API = '/api/v1';
export const ACTIVE_ROOM_STORAGE_KEY = 'xyzzy.activeRoomId';
// Every output the room holds, unfiltered by branch: a mention-run output belongs
// to no branch, and its message still has to be able to open it.
export const SESSION_STORAGE_KEY = 'xyzzy.session';
export const COOKIE_401_GUARD_KEY = 'xyzzy.cookie401At';

// A member row's display_name is what a person is called; the server falls back to
// the raw id only when a payload has not yet grown the field. Never show the id.
export function persistSession() {
  try { sessionStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify({token: state.accessToken, name: state.userName})); }
  catch (_) { /* Private browsing can disable storage; the tab still holds it in memory. */ }
}
export function readStoredSession() {
  try { const raw = sessionStorage.getItem(SESSION_STORAGE_KEY); return raw ? JSON.parse(raw) : null; }
  catch (_) { return null; }
}
export function clearStoredSession() {
  try { sessionStorage.removeItem(SESSION_STORAGE_KEY); } catch (_) { /* nothing to clear */ }
}
export async function api(method, path, body, options = {}) {
  const url = `${API}${path}`;
  const headers = {
    'Content-Type': 'application/json',
    // The header's presence, not its value, is the CSRF gate for cookie auth — a
    // cross-origin page cannot add it without a CORS preflight our origins refuse.
    // Sending it in bearer mode too is harmless.
    'X-XYZZY-Client': 'web'
  };
  if (state.accessToken) headers['Authorization'] = `Bearer ${state.accessToken}`;
  if (options.idempotencyKey) headers['Idempotency-Key'] = options.idempotencyKey;
  const opts = { method, headers };
  if (body) opts.body = JSON.stringify(body);
  let r;
  try {
    r = await fetch(url, opts);
  } catch (networkError) {
    // A keyed write is safe to retry once: the server replays the original
    // result instead of appending a second ordered event.
    if (!options.idempotencyKey) throw networkError;
    r = await fetch(url, opts);
  }
  if (!r.ok) {
    if (r.status === 401 && state.sessionMode === 'cookie' && !options.skipAuthRedirect) {
      handleCookieUnauthorized();
    }
    const e = await r.text();
    const error = new Error(e);
    error.status = r.status;
    throw error;
  }
  return r.json();
}

// A mid-session cookie 401 (the server-minted access token expired) is recovered
// with one full-page bounce through the IdP's own session, which makes it silent.
// The guard below stops a misconfigured IdP from redirect-looping the tab: a
// second 401 within a minute drops back to the setup screen with a visible error
// instead of bouncing forever.
export function handleCookieUnauthorized() {
  if (state.cookieRedirecting) return;
  const last = Number(sessionStorage.getItem(COOKIE_401_GUARD_KEY) || 0);
  const now = Date.now();
  if (now - last < 60000) {
    state.cookieRedirecting = true;
    try { sessionStorage.removeItem(COOKIE_401_GUARD_KEY); } catch (_) { /* nothing to clear */ }
    state.sessionMode = '';
    clearStoredSession();
    rememberRoomId('');
    if (state.ws) { state.ws.close(); state.ws = null; }
    document.getElementById('setup-screen').style.display = '';
    document.getElementById('app-header').style.display = 'none';
    document.getElementById('app-main').style.display = 'none';
    toast('Your sign-in expired. Please sign in again.', 'error');
    return;
  }
  state.cookieRedirecting = true;
  try { sessionStorage.setItem(COOKIE_401_GUARD_KEY, String(now)); } catch (_) { /* best effort */ }
  location.href = `${API}/auth/login`;
}

export function storedRoomId() {
  try { return localStorage.getItem(ACTIVE_ROOM_STORAGE_KEY) || ''; }
  catch (_) { return ''; }
}

export function rememberRoomId(value) {
  // Clearing removes the key outright: on a shared machine the next person to
  // sign in should inherit no trace of whose workspace was open before.
  try {
    if (value) localStorage.setItem(ACTIVE_ROOM_STORAGE_KEY, value);
    else localStorage.removeItem(ACTIVE_ROOM_STORAGE_KEY);
  }
  catch (_) { /* Private browsing can disable storage; server discovery still reconnects. */ }
}
