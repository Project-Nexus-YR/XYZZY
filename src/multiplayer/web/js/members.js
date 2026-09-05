import { api } from './api.js';
import { loadProvenance } from './meta.js';
import { canGovernOntology } from './ontology.js';
import { changeMemberRole, inviteMember, removeMember } from './rooms.js';
import { closeModal, openCenterView, openContext, openFieldDialog, openModal } from './shell.js';
import { loadState } from './socket.js';
import { errorMessage, escHtml, reconcileList, renderMarkdown, toast } from './util.js';
import { state } from './state.js';

export function renderAgents(agents) {
  const panel = document.getElementById('agents-panel');
  if (!agents.length) { panel.innerHTML = '<div class="card"><div class="detail">No agents</div></div>'; return; }
  const canRemove = state.currentRoomRole === 'admin';
  reconcileList(panel, agents, a => a.agent_id, a => `
    <div class="card" data-agent-id="${escHtml(a.agent_id)}">
      <div class="title">${escHtml(a.name)} <span class="badge ${escHtml(a.status.toLowerCase())}">${escHtml(a.status)}</span></div>
      <div class="detail">${escHtml(a.role)}${a.handle ? ` · <span class="handle">@${escHtml(a.handle)}</span>` : ''}</div>
      ${canRemove ? `<button class="btn-sm" data-agent-id="${escHtml(a.agent_id)}" data-agent-name="${escHtml(a.name)}" data-action="confirmRemoveAgent">Remove</button>` : ''}
    </div>
  `);
}

export function confirmRemoveAgent(agentId, agentName) {
  openModal(`
    <h3>Remove ${escHtml(agentName)}?</h3>
    <p>This takes the agent out of the channel and settles anything it had in flight.</p>
    <div class="field-error hidden" id="remove-agent-error"></div>
    <div class="modal-actions">
      <button type="button" class="btn-sm" data-action="closeModal">Cancel</button>
      <button type="button" class="btn-primary" data-action="submitRemoveAgent" data-agent-id="${escHtml(agentId)}">Remove</button>
    </div>
  `);
}

export async function submitRemoveAgent(agentId) {
  try {
    await api('DELETE', `/rooms/${state.roomId}/agents/${encodeURIComponent(agentId)}`);
    closeModal();
    toast('Agent removed.');
    await loadState();
  } catch (err) {
    const errorEl = document.getElementById('remove-agent-error');
    errorEl.textContent = errorMessage(err);
    errorEl.classList.remove('hidden');
  }
}

export function renderSidebarAgents(agents) {
  const el = document.getElementById('agents-sidebar');
  el.innerHTML = agents.map(a => `
    <div class="item">
      <span class="dot ${a.status === 'WORKING' ? 'working' : a.status === 'IDLE' ? 'idle' : 'online'}"></span>
      ${escHtml(a.name)}${a.handle ? ` <span class="handle">@${escHtml(a.handle)}</span>` : ''} <span class="badge badge-push ${escHtml(a.status.toLowerCase())}">${escHtml(a.status)}</span>
    </div>
  `).join('');
}

export function renderTasks(tasks) {
  const panel = document.getElementById('tasks-panel');
  if (!tasks.length) { panel.innerHTML = '<div class="card"><div class="detail">No tasks</div></div>'; return; }
  reconcileList(panel, tasks, t => t.task_id, t => `
    <div class="card" data-task-id="${escHtml(t.task_id)}">
      <div class="title">${escHtml(t.title)} <span class="badge ${escHtml(t.status.toLowerCase())}">${escHtml(t.status)}</span></div>
      <div class="detail">${t.assigned_agent_id ? 'Assigned: ' + escHtml(t.assigned_agent_id.slice(0,12)) : 'Unassigned'} | ${escHtml(t.priority)}</div>
    </div>
  `);
}

export function renderApprovals(approvals) {
  const panel = document.getElementById('approvals-panel');
  if (!approvals.length) { panel.innerHTML = '<div class="card"><div class="detail">No pending approvals</div></div>'; return; }
  panel.innerHTML = approvals.map(a => `
    <div class="approval-card">
      <div class="action">${escHtml(a.action)}</div>
      <div class="reason">${escHtml(a.reason || 'awaiting a human')}</div>
      <div class="btns">
        <button class="btn-approve" data-action="approveAction" data-approval-id="${escHtml(a.approval_id)}">Approve</button>
        <button class="btn-reject" data-action="rejectAction" data-approval-id="${escHtml(a.approval_id)}">Reject</button>
      </div>
    </div>
  `).join('');
}

export function renderArtifacts(arts) {
  state.lastArtifacts = arts;
  const panel = document.getElementById('artifacts-panel');
  const selectorEl = document.getElementById('artifact-selector');
  const surface = document.getElementById('artifact-surface');
  panel.innerHTML = arts.length
    ? `<div class="card"><div class="detail">${arts.length} published ${arts.length === 1 ? 'artifact' : 'artifacts'}. Open Artifacts to read them.</div></div>`
    : '<div class="card"><div class="detail">No artifacts</div></div>';
  if (!arts.length) {
    selectorEl.innerHTML = '';
    surface.innerHTML = `<div class="view-empty"><p>A published Decision Brief, Progress Report, or Synthesis reads here as a formatted document with the evidence behind every claim. Review outputs in an AI branch and publish one to see it.</p><button class="btn-primary" data-action="openCenterView" data-action-arg="branch">Open AI branch</button></div>`;
    return;
  }
  if (!arts.some(a => a.artifact_id === state.selectedArtifactId)) state.selectedArtifactId = arts[arts.length - 1].artifact_id;
  selectorEl.innerHTML = arts.length > 1 ? arts.map(a => `
    <button class="${a.artifact_id === state.selectedArtifactId ? 'active' : ''}" data-artifact-id="${escHtml(a.artifact_id)}" data-action="selectArtifact">${escHtml(a.name)} · v${a.version}</button>
  `).join('') : '';
  const selected = arts.find(a => a.artifact_id === state.selectedArtifactId);
  // The content itself is data — never rewritten — but when its own first line
  // is already an H1, the reader's own heading is a second, duplicate title
  // sitting right above it. The metadata line still says what it needs to say.
  const firstContentLine = (selected.content || '').split('\n').find(l => l.trim() !== '') || '';
  const contentHasOwnTitle = /^#\s+\S/.test(firstContentLine.trim());
  surface.innerHTML = `<article class="artifact-doc" data-artifact-id="${escHtml(selected.artifact_id)}">
      ${contentHasOwnTitle ? '' : `<h2>${escHtml(selected.name)}</h2>`}
      <div class="artifact-doc-meta">${escHtml(selected.type)} · version ${selected.version}</div>
      ${selected.content ? `<div class="artifact-doc-content">${renderMarkdown(selected.content)}</div>
        <button class="btn-sm" data-action="loadProvenance" data-version-id="${selected.version_id}">Inspect claim provenance</button>
        <div id="provenance-${selected.version_id}"></div>` : '<div class="artifact-doc-content">No content on this version.</div>'}
      ${state.lastDecisions.length ? `<h3>Decisions</h3>${state.lastDecisions.map(decisionCardHtml).join('')}` : ''}
    </article>`;
}

export function selectArtifact(artifactId) {
  state.selectedArtifactId = artifactId;
  renderArtifacts(state.lastArtifacts);
}

// One card renderer for both the People-panel records list and the artifact
// reader, so a status change updates the badge wherever it is read from a
// single loadState() refresh rather than two renderers drifting apart.
export function decisionCardHtml(d) {
  const canAct = canGovernOntology();
  let actions = '';
  if (canAct && d.status === 'PROPOSED') {
    actions = `<button class="btn-sm" data-action="setDecisionStatus" data-decision-id="${escHtml(d.decision_id)}" data-status="ACTIVE">Accept</button>`;
  } else if (canAct && d.status === 'ACTIVE') {
    actions = `<button class="btn-sm" data-action="setDecisionStatus" data-decision-id="${escHtml(d.decision_id)}" data-status="SUPERSEDED">Supersede</button>`;
  }
  return `
    <div class="card" data-decision-id="${escHtml(d.decision_id)}">
      <div class="title">${escHtml(d.title)} <span class="badge completed">${escHtml(d.status)}</span></div>
      <div class="detail">${escHtml(d.content || '').slice(0,100)}</div>
      ${actions}
    </div>`;
}

export async function openCreateDecisionDialog() {
  const values = await openFieldDialog({
    title: 'New decision',
    fields: [
      {id: 'title', label: 'Title', value: '', required: true},
      {id: 'content', label: 'Description (optional)', value: '', type: 'textarea'},
    ],
    submitLabel: 'Create',
  });
  if (!values) return;
  try {
    await api('POST', `/rooms/${state.roomId}/decisions`, {title: values.title, content: values.content || ''});
    toast('Decision created.');
    await loadState();
  } catch (err) {
    toast(`Decision was not created: ${errorMessage(err)}`, 'error');
  }
}

export async function setDecisionStatus(decisionId, status) {
  try {
    await api('POST', `/decisions/${decisionId}/status`, {status});
    toast('Decision updated.');
    await loadState();
  } catch (err) {
    toast(`Could not update the decision: ${errorMessage(err)}`, 'error');
  }
}

export function renderDecisions(decs) {
  state.lastDecisions = decs;
  const panel = document.getElementById('decisions-panel');
  if (!decs.length) { panel.innerHTML = '<div class="card"><div class="detail">No decisions</div></div>'; return; }
  panel.innerHTML = decs.map(decisionCardHtml).join('');
}

// Said in the words of the person it stops, because a rule that parks a colleague's
// tool call is not an administrator's private setting.
export const POSTURE_COPY = {
  GUARDED: 'Guarded · only the tools that always need a human pause here',
  STRICT: 'Strict · every tool call in this channel pauses for a human',
};

export function renderPosture(posture) {
  const current = POSTURE_COPY[posture] ? posture : 'GUARDED';
  // Everyone in the channel reads it; only an admin gets the control, which is the
  // same capability the route requires and the service re-checks as it writes.
  const control = state.currentRoomRole === 'admin'
    ? `<select id="posture-select" aria-label="Channel posture" data-change-action="declarePosture"><option value="GUARDED"${current === 'GUARDED' ? ' selected' : ''}>Guarded</option><option value="STRICT"${current === 'STRICT' ? ' selected' : ''}>Strict</option></select>`
    : '';
  document.getElementById('posture-panel').innerHTML =
    `<div class="card member-row" data-posture="${escHtml(current)}"><div><div class="title">Channel posture</div><div class="detail" id="posture-current">${escHtml(POSTURE_COPY[current])}</div></div>${control}</div>`;
}

export async function declarePosture(posture) {
  try {
    await api('PATCH', `/rooms/${state.roomId}/posture`, {posture});
    await loadState();
    toast(`Channel posture is now ${posture.toLowerCase()}.`);
  } catch (err) {
    // The select shows a posture nobody declared until the snapshot corrects it.
    await loadState();
    toast(`Posture was not changed: ${errorMessage(err)}`, 'error');
  }
}

export function renderMembers(members) {
  const el = document.getElementById('team-list');
  reconcileList(el, members, m => m.user_id, m => `
    <button class="nav-item" data-user-id="${escHtml(m.user_id)}" data-action="openContext" data-action-arg="members" aria-label="${escHtml(m.display_name || m.user_id)}${m.handle ? ` @${escHtml(m.handle)}` : ` — ${escHtml(m.role)}`}"><span class="presence-dot"></span><span class="nav-name">${escHtml(m.display_name || m.user_id)}</span><span class="nav-meta handle">${m.handle ? `@${escHtml(m.handle)}` : escHtml(m.role)}</span></button>
  `);
  const isAdmin = state.currentRoomRole === 'admin';
  const canInvite = ['admin','editor','member'].includes(state.currentRoomRole);
  const panel = document.getElementById('members-panel');
  if (!document.getElementById('members-cards')) {
    panel.innerHTML = '<div id="members-cards"></div><div id="members-controls"></div>';
  }
  reconcileList(document.getElementById('members-cards'), members, m => m.user_id, m => {
    const you = m.user_id === state.userId ? ' · you' : '';
    const name = m.display_name || m.user_id;
    const handle = m.handle ? ` <span class="handle">@${escHtml(m.handle)}</span>` : '';
    const idLine = `<div class="detail">ID <code>${escHtml(m.user_id)}</code></div>`;
    if (!isAdmin) {
      return `<div class="card" data-user-id="${escHtml(m.user_id)}"><div class="title">${escHtml(name)}${handle}${you}</div><div class="detail">${escHtml(roleLabel(m.role))}</div>${idLine}</div>`;
    }
    const roleSelect = `<select aria-label="Access for ${escHtml(name)}" data-user-id="${escHtml(m.user_id)}" data-change-action="changeMemberRole"><option value="admin"${m.role === 'admin' ? ' selected' : ''}>Admin</option><option value="editor"${m.role === 'editor' ? ' selected' : ''}>Can contribute</option><option value="viewer"${m.role === 'viewer' ? ' selected' : ''}>Read only</option></select>`;
    if (m.role === 'admin') {
      // Admins stay non-editable except for the role select itself: it lets an
      // admin demote another admin, and the server 400s a demotion that would
      // leave the room without one — that refusal surfaces inline via the same
      // toast path changeMemberRole already uses for every other role change.
      return `<div class="card member-row" data-user-id="${escHtml(m.user_id)}"><div><div class="title">${escHtml(name)}${handle}${you}</div><div class="detail">${escHtml(roleLabel(m.role))}</div>${idLine}</div>${roleSelect}</div>`;
    }
    return `<div class="card member-row" data-user-id="${escHtml(m.user_id)}"><div><div class="title">${escHtml(name)}${handle}</div><div class="detail">${escHtml(roleLabel(m.role))}</div>${idLine}</div>${roleSelect}<button class="btn-sm" type="button" data-user-id="${escHtml(m.user_id)}" data-action="removeMember">Remove</button></div>`;
  });
  // The invite form is rebuilt only when the caller's own access changes, so a
  // half-typed user id survives the re-render every membership event triggers.
  const controls = document.getElementById('members-controls');
  const controlsKey = `${isAdmin}:${canInvite}`;
  if (controls.dataset.key === controlsKey) return;
  controls.dataset.key = controlsKey;
  if (!canInvite) {
    controls.innerHTML = '<div class="panel-copy">Editors and admins can invite people. A channel admin changes access or removes members.</div>';
    return;
  }
  const copy = isAdmin
    ? 'Invited people see this channel in their sidebar at once. Removing someone ends their live feed and access immediately.'
    : 'Invited people see this channel in their sidebar at once. A channel admin changes access or removes members.';
  controls.innerHTML = `<form class="invite-form" data-submit-action="inviteMember"><input id="invite-user-id" list="invite-candidates" placeholder="User ID" autocomplete="off" aria-label="User ID to invite" required><datalist id="invite-candidates"></datalist><select id="invite-role" aria-label="Access level"><option value="editor">Can contribute</option><option value="viewer">Read only</option></select><button class="btn-sm" type="submit">Invite</button></form><div class="panel-copy">Ask the person for their user ID — it shows on their own entry in this list. ${copy}</div>`;
  loadInviteCandidates(new Set(members.map(m => m.user_id)));
}

// Feeds the invite picker's datalist from the workspace member directory. That
// route is new server-side and may still 404/403 until it lands — free-text
// entry (already required on the input) is the fallback, so a failure here is
// silent rather than surfaced as an error.
export async function loadInviteCandidates(existingMemberIds) {
  const datalist = document.getElementById('invite-candidates');
  if (!datalist) return;
  try {
    const candidates = await api('GET', `/workspaces/${state.workspaceId}/members`);
    datalist.innerHTML = candidates
      .filter(c => !existingMemberIds.has(c.user_id))
      .map(c => `<option value="${escHtml(c.user_id)}">${escHtml(c.display_name || c.user_id)} (@${escHtml(c.user_id)})</option>`)
      .join('');
  } catch {
    // No workspace member directory yet — the free-text input still works.
  }
}

export function roleLabel(role) {
  return {admin: 'admin · manages people', editor: 'editor · can contribute', viewer: 'viewer · read only'}[role] || role;
}

export async function approveAction(approvalId) {
  try {
    await api('POST', `/approvals/${approvalId}/approve`, {comment: 'Approved'});
    await loadState();
  } catch (err) {
    toast(`Approval was not recorded: ${errorMessage(err)}`, 'error');
  }
}

export async function rejectAction(approvalId) {
  try {
    await api('POST', `/approvals/${approvalId}/reject`, {comment: 'Rejected'});
    await loadState();
  } catch (err) {
    toast(`Rejection was not recorded: ${errorMessage(err)}`, 'error');
  }
}
