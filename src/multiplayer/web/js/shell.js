import { branchTitle, launchParallelAnalyses, updateTemplateSelection } from './branch.js';
import { scheduleAutoRead, sendMessage } from './messages.js';
import { escHtml } from './util.js';
import { state } from './state.js';

export const QUICK_REACTIONS = ['👍', '🎯', '⚠️'];
// 'branch'/'artifacts'/'meta'/'ontology' are center-view calls; every existing call
// site that used to open the right panel for these keeps working unchanged. Evidence
// has no view of its own — it lives with the artifact it supports.
export const CENTER_VIEW_ROUTES = {branch: 'branch', artifacts: 'artifact', ontology: 'artifact', meta: 'meta'};
export function openContext(view = 'branch') {
  if (CENTER_VIEW_ROUTES[view]) { openCenterView(CENTER_VIEW_ROUTES[view]); return; }
  openRightPanel(view);
}

export function openRightPanel(view) {
  const panel = document.querySelector('.right-panel');
  panel.classList.add('open');
  document.querySelectorAll('.context-view').forEach(el => el.classList.toggle('active', el.dataset.contextView === view));
  const titles = {thread: 'Thread', search: 'Search', members: 'People', browse: 'Browse channels', notifications: 'Notifications'};
  document.getElementById('context-title').textContent = titles[view] || 'Context';
  // Opening search always reveals the form and puts the caret in it, even on
  // the very first open before any layout has settled.
  if (view === 'search') {
    requestAnimationFrame(() => document.getElementById('search-input')?.focus());
  }
}

export function closeContext() { document.querySelector('.right-panel').classList.remove('open'); }

export function currentCenterView() {
  return document.querySelector('.center-view.active')?.dataset.centerView || 'conversation';
}

export function openCenterView(view) {
  document.querySelectorAll('.center-view').forEach(el => el.classList.toggle('active', el.dataset.centerView === view));
  document.querySelectorAll('#branches-list .nav-item').forEach(el => el.classList.toggle('active', view === 'branch' && el.dataset.branchId === state.currentBranchId));
  document.getElementById('nav-meta')?.classList.toggle('active', view === 'meta');
  document.getElementById('nav-artifacts')?.classList.toggle('active', view === 'artifact');
  document.querySelectorAll('#rooms-list .nav-item').forEach(el => el.classList.toggle('active', view === 'conversation' && el.dataset.roomId === state.roomId));
  closeSidebarDrawer();
  updateRoomHeader();
  if (view === 'conversation') scheduleAutoRead();
}

export function updateRoomHeader() {
  const view = currentCenterView();
  const back = document.getElementById('header-back');
  const hashIcon = document.getElementById('room-hash-icon');
  const nameEl = document.getElementById('room-name');
  if (view === 'conversation') {
    back.classList.add('hidden');
    hashIcon.classList.remove('hidden');
    nameEl.textContent = state.currentRoomName || 'channel';
    return;
  }
  back.classList.remove('hidden');
  hashIcon.classList.add('hidden');
  if (view === 'branch') {
    const branch = state.roomBranches.find(b => b.branch_id === state.currentBranchId);
    nameEl.textContent = branch ? `⑂ ${branchTitle(branch)}` : '⑂ AI work';
  } else if (view === 'artifact') {
    nameEl.textContent = 'Artifacts';
  } else if (view === 'meta') {
    nameEl.textContent = 'Ask Meta';
  }
}

export function closeSidebarDrawer() {
  const sidebar = document.getElementById('sidebar');
  const wasOpenDrawer = sidebar.classList.contains('open') && sidebar.getAttribute('aria-modal') === 'true';
  sidebar.classList.remove('open');
  document.getElementById('sidebar-backdrop').classList.remove('open');
  sidebar.removeAttribute('role');
  sidebar.removeAttribute('aria-modal');
  document.getElementById('sidebar-toggle')?.setAttribute('aria-expanded', 'false');
  // Only when this actually closed an open mobile drawer: every other caller
  // (switching rooms, opening a center view, a desktop resize) calls this
  // defensively whether or not the drawer was ever open, and would otherwise
  // steal focus onto a toggle button the person never interacted with.
  if (wasOpenDrawer) document.getElementById('sidebar-toggle')?.focus();
}
export function toggleSidebar(force) {
  const sidebar = document.getElementById('sidebar');
  const backdrop = document.getElementById('sidebar-backdrop');
  const open = force === undefined ? !sidebar.classList.contains('open') : force;
  sidebar.classList.toggle('open', open);
  backdrop.classList.toggle('open', open);
  // Only a mobile drawer is modal — on desktop the sidebar is permanent chrome,
  // never opened through this toggle, so these attributes only ever appear
  // alongside the drawer state that makes them true.
  if (open) {
    sidebar.setAttribute('role', 'dialog');
    sidebar.setAttribute('aria-modal', 'true');
    // Matches the modal's own Tab trap below (see the keydown listener) and
    // the same claim aria-modal is already making: the page behind it stops
    // being reachable, starting with where focus lands.
    sidebar.querySelector('.nav-item, a[href], button:not([disabled])')?.focus();
  }
  else { sidebar.removeAttribute('role'); sidebar.removeAttribute('aria-modal'); }
  document.getElementById('sidebar-toggle')?.setAttribute('aria-expanded', String(open));
}
export function toggleAITray(force) {
  const tray = document.getElementById('ai-tray');
  const open = force === undefined ? !tray.classList.contains('open') : force;
  tray.classList.toggle('open', open);
  document.getElementById('ai-trigger').setAttribute('aria-expanded', String(open));
  if (open) document.getElementById('analysis-question').focus();
}
export function setStrategy(strategy) {
  const single = strategy === 'single';
  document.getElementById('turn-locked-mode').checked = single;
  document.getElementById('strategy-single').classList.toggle('active', single);
  document.getElementById('strategy-parallel').classList.toggle('active', !single);
  document.getElementById('strategy-single').setAttribute('aria-pressed', String(single));
  document.getElementById('strategy-parallel').setAttribute('aria-pressed', String(!single));
  if (single) {
    document.querySelectorAll('#template-grid input:checked').forEach((input, index) => { if (index) input.checked = false; });
  }
  document.querySelectorAll('.template-option').forEach(option => {
    option.querySelector('.template-state').textContent = option.querySelector('input').checked ? 'Selected' : 'Select';
  });
  updateTemplateSelection();
}
export function handleComposerKey(event) {
  if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); sendMessage(); }
}

document.addEventListener('keydown', event => {
  if (event.key === 'Escape') {
    // Frontmost-first: the modal floats above the menu, above the tray, above the panel, above the drawer.
    if (!document.getElementById('modal-backdrop').classList.contains('hidden')) closeModal();
    else if (!document.getElementById('channel-menu').classList.contains('hidden')) closeChannelMenu();
    else if (document.getElementById('ai-tray').classList.contains('open')) toggleAITray(false);
    else if (document.querySelector('.right-panel').classList.contains('open')) closeContext();
    else if (document.getElementById('sidebar').classList.contains('open')) closeSidebarDrawer();
  }
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); document.getElementById('msg-input').focus(); }
  if ((event.metaKey || event.ctrlKey) && event.key === 'Enter' && document.getElementById('ai-tray').classList.contains('open')) {
    event.preventDefault();
    if (!document.getElementById('launch-button').disabled) launchParallelAnalyses();
  }
  // Trap Tab inside whichever of the modal or the (mobile) sidebar drawer is
  // currently the aria-modal surface — each claims the rest of the page is
  // unreachable, so Tab has to actually honour that rather than walking out
  // the back of it into whatever sits behind.
  if (event.key === 'Tab') {
    const modalOpen = !document.getElementById('modal-backdrop').classList.contains('hidden');
    const sidebar = document.getElementById('sidebar');
    const drawerOpen = !modalOpen && sidebar.getAttribute('aria-modal') === 'true';
    if (modalOpen || drawerOpen) {
      const scope = modalOpen ? document.getElementById('modal-card') : sidebar;
      const focusable = [...scope.querySelectorAll('button,input,textarea,select,a[href]')]
        .filter(el => !el.disabled && el.offsetParent !== null);
      if (focusable.length) {
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    }
  }
});
document.getElementById('messages').addEventListener('scroll', scheduleAutoRead);
window.addEventListener('focus', scheduleAutoRead);

// Desktop reveals actions on hover; touch has no hover, so a tap on the message
// body (not on a button, link, or already-interactive control inside it) toggles
// them instead. Only one message's actions stay open at a time.
document.getElementById('messages').addEventListener('click', event => {
  if (event.target.closest('button, a, input, select, textarea, details, summary')) return;
  const msg = event.target.closest('.msg');
  if (!msg) return;
  const wasOpen = msg.classList.contains('actions-open');
  document.querySelectorAll('.msg.actions-open').forEach(el => el.classList.remove('actions-open'));
  if (!wasOpen) msg.classList.add('actions-open');
});
// Same tap-to-reveal for the thread pane's per-reply "Reply to this" — it
// shares the .msg-actions reveal rule, so it needs the same touch fallback.
document.getElementById('thread-list').addEventListener('click', event => {
  if (event.target.closest('button, a, input, select, textarea, details, summary')) return;
  const item = event.target.closest('.thread-item');
  if (!item) return;
  const wasOpen = item.classList.contains('actions-open');
  document.querySelectorAll('.thread-item.actions-open').forEach(el => el.classList.remove('actions-open'));
  if (!wasOpen) item.classList.add('actions-open');
});

// The sidebar drawer and the fixed-overlay right panel are desktop-safe "open"
// states that become full-screen sheets under 860px. A resize that crosses that
// line while one was left open must not let it survive into the drawer layout.
window.addEventListener('resize', () => {
  const isAboveNow = window.innerWidth > 860;
  if (state.wasAboveMobileBreak && !isAboveNow) {
    closeSidebarDrawer();
    closeContext();
  }
  state.wasAboveMobileBreak = isAboveNow;
});
document.addEventListener('click', event => {
  const menu = document.getElementById('channel-menu');
  if (!menu.classList.contains('hidden') && !menu.contains(event.target) && event.target.id !== 'channel-menu-button' && !event.target.closest('#channel-menu-button')) {
    closeChannelMenu();
  }
});

export function toggleChannelMenu(event) {
  event.stopPropagation();
  const menu = document.getElementById('channel-menu');
  if (!menu.classList.contains('hidden')) { closeChannelMenu(); return; }
  const btn = document.getElementById('channel-menu-button');
  const rect = btn.getBoundingClientRect();
  menu.style.top = `${rect.bottom + 4}px`;
  // getBoundingClientRect is always physical (left/right from the viewport's
  // own left edge), so the inline-end offset it feeds has to be picked per
  // direction: under rtl the inline-end edge is the physical left edge, so
  // the distance is rect.left itself, not window.innerWidth minus rect.right.
  const rtl = getComputedStyle(document.documentElement).direction === 'rtl';
  const insetInlineEnd = rtl ? rect.left : (window.innerWidth - rect.right);
  menu.style.insetInlineEnd = `${Math.max(8, insetInlineEnd)}px`;
  menu.classList.remove('hidden');
  // role="menu" promises arrow-key navigation between its menuitems; opening
  // it with focus left on the trigger button broke that promise entirely.
  menu.querySelector('[role="menuitem"]')?.focus();
}
export function closeChannelMenu() {
  const menu = document.getElementById('channel-menu');
  const wasOpen = !menu.classList.contains('hidden');
  menu.classList.add('hidden');
  if (wasOpen && menu.contains(document.activeElement)) {
    document.getElementById('channel-menu-button')?.focus();
  }
}
// ArrowUp/ArrowDown/Home/End move focus among the menu's own items, and
// Escape/outside-click closing it is already handled elsewhere — this only
// ever runs while the menu is open, since it is unreachable otherwise.
document.getElementById('channel-menu').addEventListener('keydown', event => {
  const items = [...document.querySelectorAll('#channel-menu [role="menuitem"]')];
  const index = items.indexOf(document.activeElement);
  if (event.key === 'ArrowDown') { event.preventDefault(); items[(index + 1) % items.length]?.focus(); }
  else if (event.key === 'ArrowUp') { event.preventDefault(); items[(index - 1 + items.length) % items.length]?.focus(); }
  else if (event.key === 'Home') { event.preventDefault(); items[0]?.focus(); }
  else if (event.key === 'End') { event.preventDefault(); items[items.length - 1]?.focus(); }
});

// Whatever had focus before the modal opened, so closing it can put focus back
// rather than dropping a keyboard user at the top of the page.
export function openModal(html) {
  state.modalOpenerElement = document.activeElement;
  const card = document.getElementById('modal-card');
  card.innerHTML = html;
  // Every modal body here opens with an <h3> title; naming the dialog by it
  // gives the dialog role an accessible name instead of an empty one.
  const heading = card.querySelector('h3');
  if (heading) {
    if (!heading.id) heading.id = 'modal-title';
    card.setAttribute('aria-labelledby', heading.id);
  } else {
    card.removeAttribute('aria-labelledby');
  }
  document.getElementById('modal-backdrop').classList.remove('hidden');
  // Focus goes to the first field the moment it exists in the DOM, not on the
  // next animation frame - a caller (openFieldDialog, the create-channel and
  // redirect-agent dialogs) still wires up its own onsubmit/onclick handlers
  // right after this call returns, and a deferred rAF focus has, on some
  // paths, lost that race to a later synchronous focus/blur in the same tick.
  // The two button-only confirm dialogs (leave-channel, remove-agent) have no
  // input or textarea at all, so without a fallback focus stayed on whatever
  // was behind the backdrop — the modal claimed to be the only reachable
  // thing on the page and left a keyboard user tabbing through the page it
  // covers instead. Cancel is first in both of those dialogs' markup, so this
  // fallback lands on it rather than on the destructive action.
  (card.querySelector('input,textarea') || card.querySelector('button,select,a[href]'))?.focus();
}
// A modal closed any way (Cancel, Escape, backdrop click) resolves the field
// dialog's promise with null — one path, not three that each have to remember.
export function closeModal() {
  document.getElementById('modal-backdrop').classList.add('hidden');
  document.getElementById('modal-card').innerHTML = '';
  if (state.modalOpenerElement && state.modalOpenerElement.isConnected) state.modalOpenerElement.focus();
  state.modalOpenerElement = null;
  if (state.pendingDialogResolve) {
    const resolve = state.pendingDialogResolve;
    state.pendingDialogResolve = null;
    resolve(null);
  }
}

// The one in-app replacement for window.prompt()/confirm() in this file. Renders
// labelled inputs in the existing modal-card, resolves with {id: value, ...} on
// submit or null on Cancel/Escape/backdrop click — same shape prompt() gave callers.
export function openFieldDialog({title, description, fields, submitLabel = 'Submit'}) {
  return new Promise(resolve => {
    state.pendingDialogResolve = resolve;
    const fieldsHtml = fields.map(f => `
      <label for="dlg-${escHtml(f.id)}">${escHtml(f.label)}</label>
      ${f.type === 'textarea'
        ? `<textarea id="dlg-${escHtml(f.id)}" ${f.required ? 'required' : ''}>${escHtml(f.value || '')}</textarea>`
        : `<input id="dlg-${escHtml(f.id)}" type="text" value="${escHtml(f.value || '')}" ${f.required ? 'required' : ''} autocomplete="off">`}
    `).join('');
    openModal(`
      <h3>${escHtml(title)}</h3>
      ${description ? `<p>${escHtml(description)}</p>` : ''}
      <form id="field-dialog-form">
        ${fieldsHtml}
        <div class="field-error hidden" id="field-dialog-error"></div>
        <div class="modal-actions">
          <button type="button" class="btn-sm" id="field-dialog-cancel">Cancel</button>
          <button type="submit" class="btn-primary">${escHtml(submitLabel)}</button>
        </div>
      </form>
    `);
    document.getElementById('field-dialog-cancel').onclick = () => closeModal();
    document.getElementById('field-dialog-form').onsubmit = event => {
      event.preventDefault();
      const values = {};
      for (const f of fields) values[f.id] = document.getElementById(`dlg-${f.id}`).value;
      state.pendingDialogResolve = null;
      resolve(values);
      closeModal();
    };
  });
}
