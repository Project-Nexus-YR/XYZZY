import { api } from './api.js';
import { closeModal, currentCenterView, openCenterView, openContext, openModal, toggleAITray } from './shell.js';
import { loadState } from './socket.js';
import { errorMessage, escHtml, htmlToElement, idempotencyKey, memberName, morphElement, reconcileList, renderMarkdown, shortId, toast } from './util.js';
import { state } from './state.js';

export async function loadTemplates() {
  try {
    state.agentTemplates = await api('GET', '/agent-templates');
    renderTemplates();
  } catch (err) {
    document.getElementById('template-grid').innerHTML =
      `<div class="empty-outputs">Could not load specialist templates: ${escHtml(errorMessage(err))}</div>`;
    setLaunchNote('Specialist launch is unavailable.', 'error');
  }
}

export function renderTemplates() {
  const preferred = new Set(['Architect', 'Security Reviewer', 'Researcher']);
  const grid = document.getElementById('template-grid');
  grid.innerHTML = state.agentTemplates.map(t => `
    <label class="template-option">
      <input type="checkbox" value="${escHtml(t.template_id)}"
             ${preferred.has(t.name) ? 'checked' : ''} data-change-action="toggleTemplateSelection">
      <span class="template-body">
        <span class="template-check" aria-hidden="true"><svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m3 8.4 3.1 3.1L13 4.6"/></svg></span>
        <span class="template-copy"><span class="template-name">${escHtml(t.name)}</span>${t.role && t.role !== t.name ? `<span class="template-role">${escHtml(t.role)}</span>` : ''}</span>
        <span class="template-state">${preferred.has(t.name) ? 'Selected' : 'Select'}</span>
      </span>
    </label>
  `).join('');
  updateTemplateSelection();
}

export function toggleTemplateSelection(input) {
  const turnLocked = document.getElementById('turn-locked-mode').checked;
  const checked = [...document.querySelectorAll('#template-grid input:checked')];
  const limit = turnLocked ? 1 : 3;
  if (checked.length > limit) {
    if (turnLocked) {
      checked.filter(item => item !== input).forEach(item => { item.checked = false; });
    } else {
      input.checked = false;
      toast('Parallel branches support up to three specialists.', 'error');
    }
  }
  document.querySelectorAll('.template-option').forEach(option => {
    option.querySelector('.template-state').textContent = option.querySelector('input').checked ? 'Selected' : 'Select';
  });
  updateTemplateSelection();
}

export function selectedTemplates() {
  const ids = new Set([...document.querySelectorAll('#template-grid input:checked')].map(i => i.value));
  return state.agentTemplates.filter(t => ids.has(t.template_id));
}

export function updateTemplateSelection() {
  const count = selectedTemplates().length;
  const turnLocked = document.getElementById('turn-locked-mode').checked;
  document.getElementById('template-selection-count').textContent =
    count ? `${count} selected` : (turnLocked ? 'Choose 1' : 'Choose 2 to 3');
  document.getElementById('launch-button').disabled = turnLocked
    ? count !== 1
    : count < 2 || count > 3;
  document.getElementById('launch-button').textContent = turnLocked
    ? 'Start turn-locked run'
    : 'Run selected in parallel';
}

export function branchTitle(branch) {
  const prompt = (branch.initiating_prompt || branch.prompt || 'AI branch').trim();
  return prompt.length > 46 ? `${prompt.slice(0, 46)}…` : prompt;
}

export function branchStatus(branch, runs) {
  if (branch.status) return branch.status.toLowerCase();
  const related = runs.filter(run => run.branch_id === branch.branch_id);
  if (related.some(run => ['PENDING','RUNNING'].includes(run.status))) return 'running';
  if (related.some(run => run.status === 'FAILED') && related.some(run => run.status === 'COMPLETED')) return 'partial';
  if (related.length && related.every(run => run.status === 'COMPLETED')) return 'completed';
  if (related.some(run => run.status === 'FAILED')) return 'failed';
  return 'queued';
}

// Runs can be gone (archived) while the branch's authored outputs persist — a run
// count of 0 next to two visible output cards was reading as a contradiction. Count
// whichever roster is actually populated: distinct specialists, not stale executions.
export function branchAgentCount(branch, related) {
  if (related.length) return related.length;
  const distinct = new Set(state.allRoomOutputs.filter(o => o.branch_id === branch.branch_id).map(o => o.agent_id));
  return distinct.size;
}
// A branch always has a real initiator server-side; when the field is missing from
// this payload, say nothing rather than print a placeholder that reads as a name.
export function branchStarterClause(branch) {
  return branch.initiating_user_id ? `Started by ${memberName(branch.initiating_user_id)} · ` : '';
}

export function renderBranches(branches, runs) {
  const list = document.getElementById('branches-list');
  if (!branches.length) {
    list.innerHTML = '<div class="nav-item"><span class="nav-icon"><svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 2.6v4.6a3 3 0 0 0 3 3h5.4"/><circle cx="4" cy="13" r="1.5"/><circle cx="12.8" cy="10.2" r="1.5"/></svg></span><span class="nav-name">No branches yet</span></div>';
    document.getElementById('branch-panel-title').textContent = 'No AI branch selected';
    document.getElementById('branch-panel-title').classList.remove('hidden');
    document.getElementById('branch-panel-copy').textContent = 'Start AI work from the composer. Runs and outputs remain together here.';
    document.getElementById('branch-runs').innerHTML = '';
    return;
  }
  reconcileList(list, branches.slice().reverse(), branch => branch.branch_id, branch => {
    const related = runs.filter(run => run.branch_id === branch.branch_id);
    const status = branchStatus(branch, runs);
    const count = branchAgentCount(branch, related);
    return `<button class="nav-item branch-nav ${branch.branch_id === state.currentBranchId && currentCenterView() === 'branch' ? 'active' : ''}" data-branch-id="${escHtml(branch.branch_id)}" data-action="selectBranch" aria-label="${escHtml(branchTitle(branch))} — ${escHtml(status)}, ${count} ${count === 1 ? 'agent' : 'agents'}"><span class="nav-icon"><svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 2.6v4.6a3 3 0 0 0 3 3h5.4"/><circle cx="4" cy="13" r="1.5"/><circle cx="12.8" cy="10.2" r="1.5"/></svg></span><span class="nav-name"><span>${escHtml(branchTitle(branch))}</span><span class="branch-summary">${escHtml(status)} · ${count} ${count === 1 ? 'agent' : 'agents'}</span></span></button>`;
  });
  const branch = branches.find(item => item.branch_id === state.currentBranchId) || branches[branches.length - 1];
  const related = runs.filter(run => run.branch_id === branch.branch_id);
  const status = branchStatus(branch, runs);
  const count = branchAgentCount(branch, related);
  // The room header already carries this branch's title (⑂ + truncated
  // prompt) whenever this view is open — repeating it in the card beneath
  // said the same thing twice. The card now carries only what the header
  // doesn't: mode, status, and how many agents.
  document.getElementById('branch-panel-title').textContent = '';
  document.getElementById('branch-panel-title').classList.add('hidden');
  document.getElementById('branch-panel-copy').textContent = `${branch.mode === 'TURN_LOCKED_SINGLE' ? 'Single agent' : 'Parallel'} · ${status} · ${count} ${count === 1 ? 'agent' : 'agents'}`;
  document.getElementById('branch-runs').innerHTML = related.map(run => {
    const agent = state.roomAgents.find(item => item.agent_id === run.agent_id);
    // Mirrors the same active-run test cancelCurrentTurn() uses, so Interrupt/Redirect
    // appear exactly when a Cancel for this run would also make sense.
    const isActive = ['PENDING', 'RUNNING'].includes(run.status);
    const controls = isActive && run.agent_id
      ? `<div class="btns"><button class="btn-sm" data-action="interruptAgent" data-agent-id="${escHtml(run.agent_id)}">Interrupt</button><button class="btn-sm" data-action="openRedirectModal" data-agent-id="${escHtml(run.agent_id)}">Redirect</button></div>`
      : '';
    return `<div class="card"><div class="title"><span class="status-text ${String(run.status).toLowerCase()}">${escHtml(agent ? agent.name : 'Specialist')} · ${escHtml(run.status)}</span></div><div class="detail">${escHtml(agent ? agent.role : 'Independent analysis')}</div>${controls}</div>`;
  }).join('');
  renderBranchActivity(branches, runs);
}

// Keyed by branch id, in the same #messages container reconcileMessages
// owns — see that function's own comment on why it explicitly ignores
// '.branch-activity' elements rather than sweeping them up as orphans.
// Wholesale remove-then-reinsert on every call (the old behaviour) meant a
// press on a card's own selectBranch button was lost exactly like a message
// click was, whenever a snapshot landed mid-press; this reuses each card by
// identity instead, writing to one only when its own rendered markup changed.
export function renderBranchActivity(branches, runs) {
  const container = document.getElementById('messages');
  const existing = new Map();
  Array.from(container.children).forEach(el => {
    if (el.classList.contains('branch-activity') && el.dataset.reconcileKey) {
      existing.set(el.dataset.reconcileKey, el);
    }
  });
  const seen = new Set();
  // Cards always sit after every chat message and the unread rule, in
  // `branches` order — the last non-card child is where the first one goes.
  let cursor = null;
  Array.from(container.children).forEach(el => {
    if (!el.classList.contains('branch-activity')) cursor = el;
  });
  branches.forEach(branch => {
    const related = runs.filter(run => run.branch_id === branch.branch_id);
    const status = branchStatus(branch, runs);
    // One vocabulary everywhere selections are summarized: included/excluded/
    // unreviewed, never a bare "reviewed" that says nothing about the outcome.
    const branchOutputs = state.roomOutputs.filter(output => output.branch_id === branch.branch_id);
    const included = branchOutputs.filter(output => state.outputSelections.get(output.output_id) === 'included').length;
    const excluded = branchOutputs.filter(output => state.outputSelections.get(output.output_id) === 'excluded').length;
    const count = branchAgentCount(branch, related);
    const key = `branch-activity:${branch.branch_id}`;
    const html = `<article class="branch-activity" data-reconcile-key="${escHtml(key)}"><button data-action="selectBranch" data-branch-id="${branch.branch_id}" aria-label="${escHtml(branchTitle(branch))} — ${escHtml(status)}, ${included} included, ${excluded} excluded"><span class="branch-symbol"><svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 2.6v4.6a3 3 0 0 0 3 3h5.4"/><circle cx="4" cy="13" r="1.5"/><circle cx="12.8" cy="10.2" r="1.5"/></svg></span><span><span class="branch-title">${escHtml(branchTitle(branch))}</span><span class="branch-detail">${escHtml(branchStarterClause(branch))}${count} ${count === 1 ? 'agent' : 'agents'}</span></span><span class="branch-progress"><span class="status-text ${status}">${escHtml(status)}</span><br>${included} included · ${excluded} excluded</span></button></article>`;
    let el = existing.get(key);
    if (el) {
      if (el.dataset.reconcileFp !== html) morphElement(el, html);
    } else {
      el = htmlToElement(html);
      el.dataset.reconcileFp = html;
    }
    seen.add(key);
    const wantsNext = cursor ? cursor.nextSibling : container.firstChild;
    if (wantsNext !== el) container.insertBefore(el, wantsNext);
    cursor = el;
  });
  Array.from(container.children).forEach(el => {
    if (!el.classList.contains('branch-activity')) return;
    if (!seen.has(el.dataset.reconcileKey)) el.remove();
  });
}

// A run's own record carries no branch_id — only a branch's execution_ids list
// says which runs are its. This is the one place that maps back the other way.
export function branchIdForExecution(executionId) {
  const branch = state.roomBranches.find(b => (b.execution_ids || []).includes(executionId));
  return branch ? branch.branch_id : '';
}

// A run left PENDING by a navigation away mid-flight never auto-executes on
// reload — that would be a silent write nobody asked for. It only shows up here,
// where a click is the only thing that starts it.
export function renderResumeRunsBanner(runs) {
  const banner = document.getElementById('resume-runs-banner');
  const pending = runs.filter(run => run.status === 'PENDING');
  if (!pending.length) { banner.classList.add('hidden'); banner.innerHTML = ''; return; }
  banner.classList.remove('hidden');
  banner.innerHTML = `<div class="card"><div class="detail">${pending.length} run${pending.length === 1 ? '' : 's'} left pending by an earlier launch.</div>
    <div class="btns"><button class="btn-sm" data-action="resumePendingRuns">Resume ${pending.length} run${pending.length === 1 ? '' : 's'}</button></div></div>`;
}

export async function resumePendingRuns() {
  const pending = state.roomRuns.filter(run => run.status === 'PENDING');
  const results = await Promise.allSettled(pending.map(run => {
    const branchId = branchIdForExecution(run.execution_id);
    return branchId
      ? api('POST', `/branches/${branchId}/runs/${run.execution_id}/execute`)
      : Promise.reject(new Error('no branch for run'));
  }));
  const failed = results.filter(r => r.status === 'rejected').length;
  await loadState();
  toast(failed ? `${pending.length - failed} resumed; ${failed} failed.` : `${pending.length} run${pending.length === 1 ? '' : 's'} resumed.`, failed ? 'error' : 'success');
}

export async function selectBranch(branchId) {
  state.currentBranchId = branchId;
  openCenterView('branch');
  await loadState();
}

// A boolean or string DOM property still queues a mutation record when set
// to the value it already has (an attribute "change" happens regardless of
// whether old and new agree), so setting one on every call — as this used to
// — makes every message's reaction row (they live inside #messages) mutate
// on every single load, snapshot-unchanged or not. setIfChanged is the guard
// every write below goes through instead.
function setIfChanged(el, prop, value) {
  if (el[prop] !== value) el[prop] = value;
}

export function applyPermissions() {
  const canEdit = ['admin','editor','member'].includes(state.currentRoomRole);
  const readOnly = 'Your viewer role is read-only';
  // Re-enable on promotion, not only disable on demotion; the send button also
  // honours the turn lock, and synthesis keeps its own selection-based state.
  const aiTrigger = document.getElementById('ai-trigger');
  if (aiTrigger) { setIfChanged(aiTrigger, 'disabled', !canEdit); setIfChanged(aiTrigger, 'title', canEdit ? '' : readOnly); }
  const sendButton = document.getElementById('send-message-button');
  if (sendButton) {
    setIfChanged(sendButton, 'disabled', !canEdit || Boolean(state.currentTurnLock));
    setIfChanged(sendButton, 'title', canEdit ? '' : readOnly);
  }
  const synthesizeButton = document.getElementById('synthesize-button');
  if (synthesizeButton && !canEdit) { setIfChanged(synthesizeButton, 'disabled', true); setIfChanged(synthesizeButton, 'title', readOnly); }
  const synthesisType = document.getElementById('synthesis-type');
  if (synthesisType) setIfChanged(synthesisType, 'disabled', !canEdit);
  const synthesisTitle = document.getElementById('synthesis-title');
  if (synthesisTitle) setIfChanged(synthesisTitle, 'disabled', !canEdit);
  document.querySelectorAll('.review-actions button').forEach(button => {
    setIfChanged(button, 'disabled', !canEdit);
    setIfChanged(button, 'title', canEdit ? '' : readOnly);
  });
  // Reacting and replying are mutations; reading a thread or searching is not.
  // These buttons live inside #messages, right next to whatever a person may
  // be mid-click on, so an unguarded write here is exactly the kind of
  // snapshot-triggered noise reconcileMessages elsewhere in this app is built
  // to avoid. Editable, the title stays whatever messageActions already
  // authored ("React 👍", read back from aria-label rather than blanked to
  // '' and back on every call): only the read-only explanation is this
  // function's own to write, and only while it actually applies.
  document.querySelectorAll('.msg-actions button[data-emoji]').forEach(button => {
    setIfChanged(button, 'disabled', !canEdit);
    setIfChanged(button, 'title', canEdit ? button.getAttribute('aria-label') || '' : readOnly);
  });
  const invokeAgents = document.getElementById('invoke-agents');
  if (invokeAgents) setIfChanged(invokeAgents, 'disabled', !canEdit);
  const threadForm = document.getElementById('thread-reply-form');
  if (threadForm) setIfChanged(threadForm, 'hidden', !canEdit || !state.openThreadRootId);
}

export async function cancelCurrentTurn() {
  if (!state.currentTurnLock || !state.currentBranchId) return;
  const run = state.roomRuns.find(item => item.branch_id === state.currentBranchId && ['PENDING','RUNNING'].includes(item.status));
  if (!run) return toast('No active run is available to cancel.');
  try { await api('POST', `/executions/${run.execution_id}/cancel`, {}); toast('Cancellation requested.'); await loadState(); }
  catch (err) { toast(`Could not cancel the run: ${errorMessage(err)}`, 'error'); }
}

export async function interruptAgent(agentId) {
  try {
    await api('POST', `/agents/${agentId}/interrupt`, {reason: ''});
    toast('Agent interrupted.');
    await loadState();
  } catch (err) { toast(`Could not interrupt the agent: ${errorMessage(err)}`, 'error'); }
}

export function openRedirectModal(agentId) {
  openModal(`
    <h3>Redirect agent</h3>
    <form data-submit-action="submitRedirect" data-agent-id="${escHtml(agentId)}">
      <label for="redirect-instruction">New instruction</label>
      <textarea id="redirect-instruction" required placeholder="Tell the agent what to do instead"></textarea>
      <div class="field-error hidden" id="redirect-error"></div>
      <div class="modal-actions">
        <button type="button" class="btn-sm" data-action="closeModal">Cancel</button>
        <button type="submit" class="btn-primary">Send</button>
      </div>
    </form>
  `);
}

export async function submitRedirect(event, agentId) {
  event.preventDefault();
  const instruction = document.getElementById('redirect-instruction').value.trim();
  if (!instruction) return;
  try {
    await api('POST', `/agents/${agentId}/redirect`, {instruction});
    closeModal();
    toast('Agent redirected.');
    await loadState();
  } catch (err) {
    const errorEl = document.getElementById('redirect-error');
    errorEl.textContent = errorMessage(err);
    errorEl.classList.remove('hidden');
  }
}

export async function launchParallelAnalyses() {
  const question = document.getElementById('analysis-question').value.trim();
  const templates = selectedTemplates();
  const turnLocked = document.getElementById('turn-locked-mode').checked;
  if (!question) {
    setLaunchNote('Enter a technical decision first.', 'error');
    return;
  }
  if ((turnLocked && templates.length !== 1) || (!turnLocked && (templates.length < 2 || templates.length > 3))) {
    setLaunchNote(turnLocked ? 'Select exactly one specialist.' : 'Select two or three specialists.', 'error');
    return;
  }

  const button = document.getElementById('launch-button');
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  button.innerHTML = '<span class="spinner"></span> Launching branches';
  setLaunchNote(`Starting ${templates.length} independent runs…`);

  const spawned = await Promise.allSettled(templates.map(template =>
    api('POST', `/rooms/${state.roomId}/agents`, {
      template_id: template.template_id,
      name: template.name
    })
  ));
  const agents = spawned.filter(result => result.status === 'fulfilled').map(result => result.value);
  if (agents.length !== templates.length) {
    // A partial spawn is not a usable roster: unwind the agents this flow just
    // created before surfacing the error, so a retry does not pile up orphans.
    const unwound = await Promise.allSettled(agents.map(agent =>
      api('DELETE', `/rooms/${state.roomId}/agents/${agent.agent_id}`)
    ));
    const stillPresent = unwound.filter(result => result.status === 'rejected').length;
    button.removeAttribute('aria-busy');
    button.textContent = 'Run selected in parallel';
    updateTemplateSelection();
    setLaunchNote(
      stillPresent
        ? `Spawn failed, and ${stillPresent} agent${stillPresent === 1 ? '' : 's'} from this launch could not be removed and may still be in the room.`
        : 'Spawn failed; the room was left as it was.',
      'error'
    );
    return;
  }
  let branchLaunch;
  try {
    branchLaunch = await api('POST', `/rooms/${state.roomId}/branches`, {
      mode: turnLocked ? 'TURN_LOCKED_SINGLE' : 'PARALLEL',
      prompt: question,
      agent_ids: agents.map(agent => agent.agent_id)
    }, { idempotencyKey: idempotencyKey() });
    state.currentBranchId = branchLaunch.branch.branch_id;
  } catch (err) {
    button.removeAttribute('aria-busy');
    button.textContent = 'Run selected in parallel';
    updateTemplateSelection();
    setLaunchNote(`Branch was not started: ${errorMessage(err)}`, 'error');
    return;
  }
  const results = await Promise.allSettled(branchLaunch.runs.map(run =>
    api('POST', `/branches/${state.currentBranchId}/runs/${run.execution_id}/execute`)
  ));

  const succeeded = results.filter(r => r.status === 'fulfilled').length;
  const failed = results.length - succeeded;
  button.removeAttribute('aria-busy');
  updateTemplateSelection();
  await loadState();
  if (failed) {
    setLaunchNote(`${succeeded} completed; ${failed} failed. Successful outputs remain persisted.`, 'error');
  } else {
    setLaunchNote(`${succeeded} authored outputs completed and persisted.`, 'success');
    // A successful launch is done — leaving the tray open only invites a second,
    // duplicate launch of the same question. A validation failure above returns
    // before this point, so the tray stays open for the person to fix the input.
    toggleAITray(false);
  }
}

export function renderOutputs(outputs, runs) {
  const panel = document.getElementById('outputs-panel');
  document.getElementById('outputs-count').textContent =
    `${outputs.length} specialist ${outputs.length === 1 ? 'output' : 'outputs'}`;
  if (!outputs.length) {
    const active = runs.filter(r => r.status === 'PENDING' || r.status === 'RUNNING').length;
    panel.innerHTML = active
      ? `<div class="empty-outputs">${active} specialist ${active === 1 ? 'run is' : 'runs are'} in progress. Outputs will reappear here after reconnect.</div>`
      : `<div class="view-empty"><p>This is where specialist branches compare independent, attributed analyses side by side. Launch two or three specialists on a question to fill it.</p><button class="btn-primary" data-action="openAiTray">Start AI work</button></div>`;
    updateSelectionSummary();
    return;
  }

  panel.innerHTML = outputs.map(output => {
    const agent = state.roomAgents.find(a => a.agent_id === output.agent_id);
    const selection = state.outputSelections.get(output.output_id) || '';
    const cardClass = selection ? ` ${selection}` : '';
    return `
      <article class="output-card${cardClass}" data-output-id="${escHtml(output.output_id)}">
        <div class="output-card-head">
          <div><div class="output-author">${escHtml(agent ? agent.name : 'Specialist')}</div>
          ${agent && agent.role && agent.role !== agent.name ? `<div class="output-role">${escHtml(agent.role)}</div>` : ''}</div>
        </div>
        <div class="output-content">${renderMarkdown(output.content || 'No readable content returned.')}</div>
        <div class="output-provenance">
          <details class="prompt-detail"><summary>Provenance</summary>
            <div>Source output <code>${escHtml(shortId(output.output_id))}</code> · run <code>${escHtml(shortId(output.execution_id))}</code></div>
            <div>Exact source prompt: ${escHtml(output.source_prompt || 'Unavailable')}</div>
          </details>
        </div>
        <div class="review-actions">
          <button class="include ${selection === 'included' ? 'active' : ''}" aria-pressed="${selection === 'included'}" data-action="setOutputSelection" data-output-id="${output.output_id}" data-selection="included">Include</button>
          <button class="exclude ${selection === 'excluded' ? 'active' : ''}" aria-pressed="${selection === 'excluded'}" data-action="setOutputSelection" data-output-id="${output.output_id}" data-selection="excluded">Exclude</button>
        </div>
      </article>`;
  }).join('');
  updateSelectionSummary();
}

export async function setOutputSelection(outputId, selection) {
  try {
    await api('PUT', `/branches/${state.currentBranchId}/output-selections/${outputId}`, {
      disposition: selection.toUpperCase()
    });
    await loadState();
  } catch (err) {
    toast(`Selection was not saved: ${errorMessage(err)}`, 'error');
  }
}

// Derives a sensible starting title from a branch prompt's first clause, so the
// title field never starts blank without ever lying about what was asked.
export function firstClause(prompt) {
  const trimmed = (prompt || '').trim();
  const clause = trimmed.split(/[.!?\n]/)[0].trim();
  return clause || trimmed;
}

// The title input tracks the branch's derived title until the person edits it;
// once edited, their wording wins even if the branch selection changes under it.
export function updateSelectionSummary() {
  const available = new Set(state.roomOutputs.map(o => o.output_id));
  const included = [...state.outputSelections].filter(([id, disposition]) => available.has(id) && disposition === 'included').length;
  const excluded = [...state.outputSelections].filter(([id, disposition]) => available.has(id) && disposition === 'excluded').length;
  const reviewed = included + excluded;
  document.getElementById('selection-summary').textContent = reviewed
    ? `${included} included · ${excluded} excluded · ${state.roomOutputs.length - reviewed} unreviewed`
    : 'No outputs selected';
  const minimumIncluded = state.currentBranchMode === 'TURN_LOCKED_SINGLE' ? 1 : 2;
  const titleInput = document.getElementById('synthesis-title');
  const branch = state.roomBranches.find(b => b.branch_id === state.currentBranchId);
  const derived = firstClause(branch ? (branch.initiating_prompt || branch.prompt || '') : '');
  if (!titleInput.value.trim() || titleInput.value === state.synthesisTitleAuto) {
    titleInput.value = derived;
    state.synthesisTitleAuto = derived;
  }
  const title = titleInput.value.trim();
  const valid = state.roomOutputs.length >= minimumIncluded
    && reviewed === state.roomOutputs.length
    && included >= minimumIncluded
    && Boolean(title);
  const synthesize = document.getElementById('synthesize-button');
  const chosen = SYNTHESIS_TYPES[document.getElementById('synthesis-type').value];
  synthesize.disabled = !valid;
  synthesize.textContent = included
    ? `Publish ${chosen.name} from ${included}`
    : `Publish ${chosen.name}`;
  synthesize.title = valid ? '' : `Review every output, include at least ${minimumIncluded}, and give the synthesis a title`;
}

export const SYNTHESIS_TYPES = {
  GENERAL_SYNTHESIS: { name: 'General Synthesis' },
  DECISION_BRIEF: { name: 'Decision Brief' },
  PROGRESS_REPORT: { name: 'Progress Report' }
};

export async function publishSynthesis() {
  const button = document.getElementById('synthesize-button');
  const type = document.getElementById('synthesis-type').value;
  const chosen = SYNTHESIS_TYPES[type];
  const title = document.getElementById('synthesis-title').value.trim();
  if (!title) return;
  button.disabled = true;
  try {
    await api('POST', `/branches/${state.currentBranchId}/syntheses`, {
      title, synthesis_type: type
    }, { idempotencyKey: idempotencyKey() });
    await loadState();
    openContext('artifacts');
    toast(`${chosen.name} published.`);
  } catch (err) {
    toast(`${chosen.name} was not published: ${errorMessage(err)}`, 'error');
    updateSelectionSummary();
  }
}

export function setLaunchNote(text, tone = '') {
  const note = document.getElementById('launch-note');
  note.textContent = text;
  note.className = `launch-note${tone ? ` ${tone}` : ''}`;
}
