import { api } from './api.js';
import { openFieldDialog } from './shell.js';
import { loadState } from './socket.js';
import { errorMessage, escHtml, shortId, toast } from './util.js';
import { state } from './state.js';

export function canGovernOntology() {
  return ['admin', 'editor', 'member'].includes(state.currentRoomRole);
}

export function ontologyBadge(record) {
  if (record.review_status === 'CONFIRMED') {
    return '<span class="ontology-badge confirmed">confirmed</span>';
  }
  if (record.review_status === 'CORRECTED') {
    return '<span class="ontology-badge corrected">corrected</span>';
  }
  if (record.derivation_kind === 'AI_DERIVED') {
    return '<span class="ontology-badge">AI-derived</span>';
  }
  return '<span class="ontology-badge system">system</span>';
}

export function ontologyEntityActions(entity) {
  if (!canGovernOntology()) return '';
  return `<div class="ontology-actions" data-governance="entity">
    <button data-action="reviewOntologyEntity" data-entity-id="${entity.entity_id}" data-review="CONFIRM">Confirm</button>
    <button data-action="reviewOntologyEntity" data-entity-id="${entity.entity_id}" data-review="CORRECT">Correct</button>
  </div>`;
}

export function ontologyRelationshipActions(relationship) {
  if (!canGovernOntology()) return '';
  return `<div class="ontology-actions" data-governance="relationship">
    <button data-action="reviewOntologyRelationship" data-relationship-id="${relationship.relationship_id}" data-review="CONFIRM">Confirm link</button>
    <button data-action="reviewOntologyRelationship" data-relationship-id="${relationship.relationship_id}" data-review="CORRECT">Correct link</button>
  </div>`;
}

export function identifierList(label, identifiers) {
  const values = identifiers || [];
  return `${label}: ${values.map(value => `<code>${escHtml(value)}</code>`).join(', ') || 'none'}`;
}

export function renderOntology(ontology) {
  const panel = document.getElementById('ontology-panel');
  const tree = document.getElementById('ontology-tree');
  const mode = document.getElementById('ontology-mode');
  const entities = ontology.entities || [];
  const relationships = ontology.relationships || [];
  const reviews = ontology.reviews || [];
  const entitiesById = new Map(entities.map(entity => [entity.entity_id, entity]));
  const decisions = entities.filter(entity => entity.kind === 'Decision');
  panel.dataset.roomRole = state.currentRoomRole;
  panel.dataset.governance = canGovernOntology() ? 'enabled' : 'read-only';
  mode.textContent = canGovernOntology()
    ? `${state.currentRoomRole} · review enabled`
    : `${state.currentRoomRole} · read only`;

  if (!decisions.length) {
    tree.innerHTML = '<div class="ontology-empty">Publish a Decision Brief to materialize its evidence tree.</div>';
  } else {
    tree.innerHTML = decisions.map(decision => {
      const supportLinks = relationships.filter(link =>
        link.kind === 'SUPPORTS' && link.to_entity_id === decision.entity_id
      );
      const claims = supportLinks.map(link => ({
        entity: entitiesById.get(link.from_entity_id), supportLink: link
      })).filter(item => item.entity);
      const claimHtml = claims.map(item => {
        const claim = item.entity;
        const sourceLink = relationships.find(link =>
          link.kind === 'DERIVED_FROM' && link.from_entity_id === claim.entity_id
          && entitiesById.get(link.to_entity_id)?.kind === 'AgentOutput'
        );
        const output = sourceLink ? entitiesById.get(sourceLink.to_entity_id) : null;
        const persistedOutput = output
          ? state.roomOutputs.find(candidate => candidate.output_id === output.source_object_id)
          : null;
        const provider = persistedOutput
          ? `${persistedOutput.provider_name || 'simulated'} / ${persistedOutput.provider_model || 'simulated'}`
          : 'provider snapshot unavailable';
        const outputHtml = output ? `<div class="ontology-node output" data-ontology-kind="AgentOutput">
          <div class="ontology-node-head"><div class="ontology-label">AgentOutput · ${escHtml(shortId(output.source_object_id))}</div>${ontologyBadge(output)}</div>
          <div class="ontology-meta">Confidence ${Number(output.confidence).toFixed(2)} · ${identifierList('Evidence', output.evidence_ids)}<br>
          Provider: ${escHtml(provider)} · response <code>${escHtml(persistedOutput?.provider_response_id || 'not issued')}</code>
          <details><summary>Exact provider/source evidence</summary>
            <div>${identifierList('Source IDs', output.source_ids)}</div>
            <div>Source prompt: ${escHtml(persistedOutput?.source_prompt || 'Unavailable')}</div>
            <div>Provider input: ${escHtml(persistedOutput?.provider_input || 'Unavailable')}</div>
            <div>Evidence: ${escHtml(persistedOutput?.provider_evidence || persistedOutput?.content || 'Unavailable')}</div>
          </details></div>
          ${ontologyEntityActions(output)}
          ${sourceLink ? `<div class="edge-review" data-ontology-edge="DERIVED_FROM">
            <div class="ontology-meta">DERIVED_FROM · ${ontologyBadge(sourceLink)} · confidence ${Number(sourceLink.confidence).toFixed(2)}<br>${identifierList('Exact evidence', sourceLink.evidence_ids)}</div>
            ${ontologyRelationshipActions(sourceLink)}
          </div>` : ''}
        </div>` : '<div class="ontology-empty">Exact AgentOutput link unavailable.</div>';
        return `<div class="ontology-node claim" data-ontology-kind="Claim">
          <div class="ontology-node-head"><div class="ontology-label">${escHtml(claim.label)}</div>${ontologyBadge(claim)}</div>
          <div class="ontology-meta">Claim · confidence ${Number(claim.confidence).toFixed(2)}<br>
          ${identifierList('Evidence', claim.evidence_ids)}<br>${identifierList('Sources', claim.source_ids)}</div>
          ${ontologyEntityActions(claim)}
          <div class="edge-review" data-ontology-edge="SUPPORTS">
            <div class="ontology-meta">SUPPORTS decision · ${ontologyBadge(item.supportLink)} · confidence ${Number(item.supportLink.confidence).toFixed(2)}<br>${identifierList('Exact evidence', item.supportLink.evidence_ids)}</div>
            ${ontologyRelationshipActions(item.supportLink)}
          </div>
          ${outputHtml}
        </div>`;
      }).join('');
      return `<article class="ontology-node decision" data-ontology-kind="Decision">
        <div class="ontology-node-head"><div class="ontology-label">${escHtml(decision.label)}</div>${ontologyBadge(decision)}</div>
        <div class="ontology-meta">Decision · confidence ${Number(decision.confidence).toFixed(2)}<br>
        ${identifierList('Evidence', decision.evidence_ids)}<br>${identifierList('Sources', decision.source_ids)}</div>
        ${ontologyEntityActions(decision)}
        ${claimHtml || '<div class="ontology-empty">No supporting claims linked.</div>'}
      </article>`;
    }).join('');
  }

  const history = document.getElementById('ontology-history');
  const historyList = document.getElementById('ontology-history-list');
  history.querySelector('summary').textContent = `Review history · ${reviews.length} ${reviews.length === 1 ? 'entry' : 'entries'}`;
  historyList.innerHTML = reviews.length ? reviews.map(review => {
    const target = review.target_type === 'ENTITY'
      ? entitiesById.get(review.target_id)?.label || review.target_id
      : relationships.find(link => link.relationship_id === review.target_id)?.kind || review.target_id;
    return `<div class="history-item" data-review-action="${escHtml(review.action)}">
      <strong>${escHtml(review.action)}</strong> ${escHtml(review.target_type.toLowerCase())} · ${escHtml(target)}<br>
      ${escHtml(review.reviewed_by)} · ${escHtml(new Date(review.created_at).toLocaleString())}<br>
      Reason: ${escHtml(review.reason || 'No reason supplied')}
      <div class="history-values">Before ${escHtml(JSON.stringify(review.before))}<br>After ${escHtml(JSON.stringify(review.after))}</div>
    </div>`;
  }).join('') : '<div class="ontology-empty">No confirmation or correction history yet.</div>';
}

export async function reviewOntologyEntity(entityId, action) {
  const entity = (state.roomOntology.entities || []).find(item => item.entity_id === entityId);
  if (!entity || !canGovernOntology()) return;
  const body = {action};
  if (action === 'CONFIRM') {
    const values = await openFieldDialog({
      title: 'Confirm assertion',
      fields: [{id: 'reason', label: 'Confirmation reason (optional)', value: 'Checked against exact evidence.'}],
      submitLabel: 'Confirm',
    });
    if (!values) return;
    body.reason = values.reason;
  } else {
    const values = await openFieldDialog({
      title: 'Correct assertion',
      fields: [
        {id: 'label', label: 'Corrected assertion', value: entity.label},
        {id: 'confidence', label: 'Corrected confidence (0–1)', value: String(entity.confidence)},
        {id: 'reason', label: 'Correction reason (required)', value: '', required: true},
      ],
      submitLabel: 'Save correction',
    });
    if (!values) return;
    const confidence = Number(values.confidence);
    if (values.label !== entity.label) body.corrected_label = values.label;
    if (confidence !== Number(entity.confidence)) body.corrected_confidence = confidence;
    body.reason = values.reason;
    if (body.corrected_label === undefined && body.corrected_confidence === undefined) {
      toast('Change the assertion or confidence before saving a correction.', 'error');
      return;
    }
  }
  try {
    await api('POST', `/rooms/${state.roomId}/ontology/entities/${entityId}/reviews`, body);
    await loadState();
  } catch (err) {
    toast(`Ontology review was not saved: ${errorMessage(err)}`, 'error');
  }
}

export async function reviewOntologyRelationship(relationshipId, action) {
  const relationship = (state.roomOntology.relationships || []).find(
    item => item.relationship_id === relationshipId
  );
  if (!relationship || !canGovernOntology()) return;
  const body = {action};
  if (action === 'CONFIRM') {
    const values = await openFieldDialog({
      title: 'Confirm link',
      fields: [{id: 'reason', label: 'Link confirmation reason (optional)', value: 'Checked against exact evidence.'}],
      submitLabel: 'Confirm',
    });
    if (!values) return;
    body.reason = values.reason;
  } else {
    const values = await openFieldDialog({
      title: 'Correct link',
      description: 'Valid relationships: OWNS, BLOCKS, DEPENDS_ON, SUPPORTS, CONTRADICTS, REFERENCES, DERIVED_FROM.',
      fields: [
        {id: 'kind', label: 'Corrected relationship', value: relationship.kind},
        {id: 'confidence', label: 'Corrected confidence (0–1)', value: String(relationship.confidence)},
        {id: 'reason', label: 'Correction reason (required)', value: '', required: true},
      ],
      submitLabel: 'Save correction',
    });
    if (!values) return;
    const confidence = Number(values.confidence);
    if (values.kind !== relationship.kind) body.corrected_kind = values.kind;
    if (confidence !== Number(relationship.confidence)) body.corrected_confidence = confidence;
    body.reason = values.reason;
    if (body.corrected_kind === undefined && body.corrected_confidence === undefined) {
      toast('Change the relationship or confidence before saving a correction.', 'error');
      return;
    }
  }
  try {
    await api('POST', `/rooms/${state.roomId}/ontology/relationships/${relationshipId}/reviews`, body);
    await loadState();
  } catch (err) {
    toast(`Ontology relationship review was not saved: ${errorMessage(err)}`, 'error');
  }
}

// Said in the words of the person it stops, because a rule that parks a colleague's
// tool call is not an administrator's private setting.
