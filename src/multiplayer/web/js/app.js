// Entry point: wires every inline handler the markup used to carry (onclick,
// onkeydown, onsubmit, onchange, oninput) to delegated, data-attribute-keyed
// listeners, then starts the app. Everything the delegated actions call is a
// plain export from one of the sibling modules, unchanged in behavior.
import { toggleTheme } from './util.js';
import { enterDemoWorkspace, startSsoLogin, setup, signOut, clearTokenError, handleSummaryKey, initAuth } from './auth.js';
import {
  openContext, closeContext, openCenterView, toggleSidebar, toggleAITray, setStrategy,
  handleComposerKey, toggleChannelMenu, closeChannelMenu, openModal, closeModal,
} from './shell.js';
import {
  openCreateChannelModal, openBrowseChannels, switchRoom, submitCreateChannel,
  openLeaveChannelConfirm, submitLeaveChannel, openInvitePeople, inviteMember,
  changeMemberRole, removeMember, refreshRooms,
} from './rooms.js';
import {
  openNotifications, openNotification, openAgentOutput, toggleReaction,
  markRoomRead, sendMessage,
} from './messages.js';
import { submitThreadReply, runSearch, openSearchHit, openThread, setThreadTarget } from './thread.js';
import {
  confirmRemoveAgent, submitRemoveAgent, selectArtifact, openCreateDecisionDialog,
  setDecisionStatus, declarePosture, approveAction, rejectAction,
} from './members.js';
import { reviewOntologyEntity, reviewOntologyRelationship } from './ontology.js';
import {
  toggleTemplateSelection, updateTemplateSelection, selectBranch, resumePendingRuns,
  cancelCurrentTurn, interruptAgent, openRedirectModal, submitRedirect,
  launchParallelAnalyses, setOutputSelection, updateSelectionSummary, publishSynthesis,
} from './branch.js';
import { askMeta, askMetaKind, loadProvenance } from './meta.js';
import { loadState } from './socket.js';
import { state } from './state.js';

// The pre-module e2e suite (tests/e2e/test_web_client.py) predates this split
// and still reaches into a few internals directly through page.evaluate() —
// the reconnect cursor, the in-flight-load buffer, the current room — to
// assert on state a click alone cannot observe. `state` and every helper it
// used to reach as a bare global lived on `window` before round 2 moved them
// into modules; this bridge is exactly that list, so that white-box coverage
// keeps working unchanged rather than losing it to the refactor.
Object.defineProperties(window, {
  roomId: {get: () => state.roomId},
  workspaceId: {get: () => state.workspaceId},
  lastSequence: {get: () => state.lastSequence},
  pendingEventsDuringLoad: {get: () => state.pendingEventsDuringLoad},
});
window.loadState = loadState;
window.refreshRooms = refreshRooms;
window.markRoomRead = markRoomRead;
window.closeChannelMenu = closeChannelMenu;
window.switchRoom = switchRoom;

function openAiTray() {
  toggleAITray(true);
}

function closeAiTray() {
  toggleAITray(false);
}

function closeSidebar() {
  toggleSidebar(false);
}

function invitePeopleFromMenu() {
  closeChannelMenu();
  openInvitePeople();
}

function channelDetailsFromMenu() {
  closeChannelMenu();
  openContext('members');
}

function leaveChannelFromMenu() {
  closeChannelMenu();
  openLeaveChannelConfirm();
}

function openBrowsedRoom(el) {
  closeContext();
  switchRoom(el.dataset.roomId);
}

function clickSetupButton() {
  document.getElementById('setup-button').click();
}

// Click actions. Each receives the element data-action was found on (via
// closest()) and the triggering event, and reads whatever dataset it needs —
// the same values the removed onclick attribute used to read off `this`.
const clickActions = {
  enterDemoWorkspace: () => enterDemoWorkspace(),
  startSsoLogin: () => startSsoLogin(),
  setup: () => setup(),
  openContext: (el) => openContext(el.dataset.actionArg),
  openCreateChannelModal: () => openCreateChannelModal(),
  openBrowseChannels: () => openBrowseChannels(),
  toggleTheme: () => toggleTheme(),
  signOut: () => signOut(),
  closeSidebar: () => closeSidebar(),
  toggleSidebar: () => toggleSidebar(),
  openCenterView: (el) => openCenterView(el.dataset.actionArg),
  markRoomRead: () => markRoomRead(),
  openNotifications: () => openNotifications(),
  toggleChannelMenu: (el, event) => toggleChannelMenu(event),
  invitePeopleFromMenu: () => invitePeopleFromMenu(),
  channelDetailsFromMenu: () => channelDetailsFromMenu(),
  leaveChannelFromMenu: () => leaveChannelFromMenu(),
  cancelCurrentTurn: () => cancelCurrentTurn(),
  toggleAITray: () => toggleAITray(),
  openAiTray: () => openAiTray(),
  closeAiTray: () => closeAiTray(),
  sendMessage: () => sendMessage(),
  publishSynthesis: () => publishSynthesis(),
  askMetaKind: (el) => askMetaKind(el.dataset.actionArg),
  askMeta: () => askMeta(),
  setStrategy: (el) => setStrategy(el.dataset.actionArg),
  launchParallelAnalyses: () => launchParallelAnalyses(),
  closeContext: () => closeContext(),
  openCreateDecisionDialog: () => openCreateDecisionDialog(),
  closeModal: () => closeModal(),
  switchRoom: (el) => switchRoom(el.dataset.roomId),
  openBrowsedRoom: (el) => openBrowsedRoom(el),
  submitLeaveChannel: () => submitLeaveChannel(),
  openNotification: (el) => openNotification(el.dataset.roomId),
  openAgentOutput: (el) => openAgentOutput(el.dataset.outputId, el),
  toggleReaction: (el) => toggleReaction(el.dataset),
  openThread: (el) => openThread(el.dataset.messageId),
  setThreadTarget: (el) => setThreadTarget(el.dataset.messageId),
  openSearchHit: (el) => openSearchHit(el.dataset),
  confirmRemoveAgent: (el) => confirmRemoveAgent(el.dataset.agentId, el.dataset.agentName),
  submitRemoveAgent: (el) => submitRemoveAgent(el.dataset.agentId),
  approveAction: (el) => approveAction(el.dataset.approvalId),
  rejectAction: (el) => rejectAction(el.dataset.approvalId),
  selectArtifact: (el) => selectArtifact(el.dataset.artifactId),
  loadProvenance: (el) => loadProvenance(el.dataset.versionId),
  setDecisionStatus: (el) => setDecisionStatus(el.dataset.decisionId, el.dataset.status),
  reviewOntologyEntity: (el) => reviewOntologyEntity(el.dataset.entityId, el.dataset.review),
  reviewOntologyRelationship: (el) => reviewOntologyRelationship(el.dataset.relationshipId, el.dataset.review),
  removeMember: (el) => removeMember(el.dataset.userId),
  resumePendingRuns: () => resumePendingRuns(),
  selectBranch: (el) => selectBranch(el.dataset.branchId),
  interruptAgent: (el) => interruptAgent(el.dataset.agentId),
  openRedirectModal: (el) => openRedirectModal(el.dataset.agentId),
  setOutputSelection: (el) => setOutputSelection(el.dataset.outputId, el.dataset.selection),
  clickSetupButton: () => clickSetupButton(),
};

const changeActions = {
  updateSelectionSummary: () => updateSelectionSummary(),
  updateTemplateSelection: () => updateTemplateSelection(),
  declarePosture: (el) => declarePosture(el.value),
  changeMemberRole: (el) => changeMemberRole(el.dataset.userId, el.value),
  toggleTemplateSelection: (el) => toggleTemplateSelection(el),
};

const inputActions = {
  clearTokenError: () => clearTokenError(),
  updateSelectionSummary: () => updateSelectionSummary(),
};

const submitActions = {
  submitThreadReply: (el, event) => submitThreadReply(event),
  runSearch: (el, event) => runSearch(event),
  submitCreateChannel: (el, event) => submitCreateChannel(event),
  inviteMember: (el, event) => inviteMember(event),
  submitRedirect: (el, event) => submitRedirect(event, el.dataset.agentId),
};

const keydownActions = {
  handleSummaryKey: (el, event) => handleSummaryKey(event),
  handleComposerKey: (el, event) => handleComposerKey(event),
};

const enterActions = {
  clickSetupButton: () => clickSetupButton(),
  askMeta: () => askMeta(),
  openSearchHit: (el) => openSearchHit(el.dataset),
};

function dispatch(table, name, el, event) {
  const fn = table[name];
  if (fn) fn(el, event);
}

function initDelegatedEvents() {
  document.addEventListener('click', (event) => {
    const el = event.target.closest('[data-action]');
    if (el) dispatch(clickActions, el.dataset.action, el, event);
  });
  document.addEventListener('submit', (event) => {
    const el = event.target.closest('[data-submit-action]');
    if (el) dispatch(submitActions, el.dataset.submitAction, el, event);
  });
  document.addEventListener('change', (event) => {
    const el = event.target.closest('[data-change-action]');
    if (el) dispatch(changeActions, el.dataset.changeAction, el, event);
  });
  document.addEventListener('input', (event) => {
    const el = event.target.closest('[data-input-action]');
    if (el) dispatch(inputActions, el.dataset.inputAction, el, event);
  });
  document.addEventListener('keydown', (event) => {
    const keyEl = event.target.closest('[data-keydown-action]');
    if (keyEl) {
      dispatch(keydownActions, keyEl.dataset.keydownAction, keyEl, event);
      return;
    }
    const enterEl = event.target.closest('[data-enter-action]');
    if (enterEl && event.key === 'Enter') {
      event.preventDefault();
      dispatch(enterActions, enterEl.dataset.enterAction, enterEl, event);
    }
  });
  // The one handler that is not name-dispatched: it must compare the event's
  // target against this exact element, not against whatever data-action
  // element the click bubbled through.
  const backdrop = document.getElementById('modal-backdrop');
  if (backdrop) {
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) closeModal();
    });
  }
}

initDelegatedEvents();
initAuth();
