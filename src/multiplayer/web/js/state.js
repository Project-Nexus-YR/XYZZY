export const state = {
  userId: '',
  userName: '',
  accessToken: '',
  roomId: '',
  ws: null,
  lastSequence: 0,
  orgId: '',
  workspaceId: '',
  agentTemplates: [],
  roomAgents: [],
  // Every output the room holds, unfiltered by branch: a mention-run output
  // belongs to no branch, and its message still has to be able to open it.
  allRoomOutputs: [],
  roomOutputs: [],
  // output_id -> 'included' | 'excluded', for the branch currently open. Lives
  // here rather than in a UI module because loadState (socket.js) clears and
  // repopulates it on every snapshot and branch.js reads it for rendering —
  // neither side should have to import the other just to reach a Map.
  outputSelections: new Map(),
  currentBranchId: '',
  currentBranchMode: '',
  currentTurnLock: null,
  roomOntology: {entities: [], relationships: [], reviews: []},
  currentRoomRole: 'viewer',
  currentRoomName: '',
  myRooms: [],
  roomMembers: [],
  roomBranches: [],
  roomRuns: [],
  openThreadRootId: '',
  threadReplyTargetId: '',
  readCursor: 0,
  selectedArtifactId: '',
  sessionMode: '', // '' | 'bearer' | 'cookie'
  ssoConfig: {sso: false, provider_label: 'single sign-on', demo: false},
  cookieRedirecting: false,
  // Guards handleBearerUnauthorized (api.js) the same way cookieRedirecting
  // guards handleCookieUnauthorized: a 401 on a fetch and a 4401 socket close
  // can both fire for the same revoked token within the same tick, and only
  // the first should tear the session down.
  bearerSessionEnding: false,
  wasAboveMobileBreak: window.innerWidth > 860,
  modalOpenerElement: null,
  pendingDialogResolve: null,
  roomUnreadCounts: new Map(),
  wsReconnectDelay: 1000,
  wsReconnectAttempts: 0,
  wsReconnectTimer: null,
  wsRoomId: null,
  loadStatePromise: null,
  loadStatePromiseRoom: null,
  staleCallDuringLoad: false,
  pendingEventsDuringLoad: [],
  lastNotifications: [],
  autoReadTimer: null,
  refreshUnreadTimer: null,
  lastArtifacts: [],
  lastDecisions: [],
  synthesisTitleAuto: '',
  // messageId -> outputId: which message's "Full output" record is
  // currently open, so a message re-render (a reaction, any snapshot
  // reconcile) can re-include the same record from data instead of the
  // open panel depending on a DOM node the render pipeline knows nothing
  // about surviving by accident.
  openOutputRecords: new Map(),
};
