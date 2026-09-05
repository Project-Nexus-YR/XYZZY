import { api } from './api.js';
import { errorMessage, escHtml, humanizeToken, renderMarkdown, shortId } from './util.js';
import { state } from './state.js';

export function metaDerivationLabel(record) {
  if (record.review_status === 'CORRECTED') return 'human-corrected';
  if (record.review_status === 'CONFIRMED') return 'human-confirmed';
  return record.derivation_kind === 'AI_DERIVED' ? 'AI-derived' : 'system-materialized';
}

export async function askMeta(preset = '') {
  const input = document.getElementById('meta-question');
  if (preset) input.value = preset;
  const question = input.value.trim();
  if (!question) return;
  return renderMeta(`question=${encodeURIComponent(question)}`);
}

// Naming the kind is the interface: the eight buttons cover everything Meta answers,
// so no capability depends on typing a phrasing the server happens to recognize. A
// question typed alongside one rides along for the audit record and decides nothing.
export async function askMetaKind(kind) {
  const typed = document.getElementById('meta-question').value.trim();
  const audit = typed ? `&question=${encodeURIComponent(typed)}` : '';
  return renderMeta(`kind=${encodeURIComponent(kind)}${audit}`);
}

export async function renderMeta(query) {
  const answer = document.getElementById('meta-answer');
  const evidence = document.getElementById('meta-evidence');
  answer.className = 'meta-answer';
  answer.textContent = 'Retrieving bounded room evidence…';
  evidence.innerHTML = '';
  try {
    const result = await api('GET', `/rooms/${state.roomId}/meta?${query}&limit=10`);
    const scopeRoom = (state.myRooms.find(item => item.room_id === result.scope.room_id) || {}).name || 'this channel';
    if (!result.decision) {
      // The kinds answered from assertions rather than one decision brief. The
      // scope line answers "how fresh is this" in words; the machine values ride
      // in the title for anyone who needs the exact event head.
      const lag = result.freshness.drain_lag_events;
      document.getElementById('meta-scope').innerHTML = `Scope: #${escHtml(scopeRoom)} · ${escHtml(humanizeToken(result.query.kind))}` +
        `<span title="authorized head ${escHtml(String(result.freshness.authorized_head ?? '—'))}"> · evidence current to event ${escHtml(String(result.freshness.authorized_head ?? '—'))}</span>` +
        (lag ? ` · ${escHtml(String(lag))} events still syncing` : '');
      answer.innerHTML = `<span class="ontology-badge" title="${escHtml(result.status)}">${escHtml(humanizeToken(result.status))}</span> ${renderMarkdown(result.summary)}`;
      evidence.innerHTML = [...result.claims, ...result.unconfirmed].map(claim =>
        `<article class="meta-chain"><span class="ontology-badge ${escHtml(claim.review_status.toLowerCase())}" title="${escHtml(claim.assurance)}">${escHtml(humanizeToken(claim.assurance))}</span>
          ${escHtml(claim.text)}<br>Confidence ${Number(claim.confidence).toFixed(2)} · as of event ${escHtml(claim.asserted_at_sequence)}
          · ${claim.current ? 'current' : `${claim.invalidating_events} later events`}
          · source ${escHtml(humanizeToken(claim.source_object_kind))} <code title="${escHtml(claim.source_object_id)}">${escHtml(shortId(claim.source_object_id))}</code></article>`).join('');
      return;
    }
    const counts = result.retrieval_counts;
    const freshness = result.freshness;
    const decisionLag = freshness.drain_lag_events;
    document.getElementById('meta-scope').innerHTML = `Scope: #${escHtml(scopeRoom)} · Decision Brief v${result.scope.version_number}` +
      `<span title="authorized head ${escHtml(String(freshness.authorized_head))}"> · evidence current to event ${escHtml(String(freshness.authorized_head))}</span>` +
      (decisionLag ? ` · ${escHtml(String(decisionLag))} events still syncing` : '') +
      ` · ${counts.returned_claims} of ${counts.available_claims} claims`;
    answer.innerHTML = `<span class="ontology-badge ${escHtml(result.decision.review_status.toLowerCase())}">${escHtml(metaDerivationLabel(result.decision))}</span> ` +
      `${renderMarkdown(result.summary)}<span class="ontology-meta">Confidence ${Number(result.decision.confidence).toFixed(2)} · freshness ${escHtml(freshness.decision_updated_at)}</span>`;
    evidence.innerHTML = result.evidence_chains.map((chain, index) => {
      const claim = chain.claim;
      const source = chain.exact_source_evidence;
      const decisionLink = chain.relationships.claim_to_decision;
      const correction = claim.latest_review
        ? `<br>Latest governance: ${escHtml(claim.latest_review.action)} by ${escHtml(claim.latest_review.reviewed_by)} · ${escHtml(claim.latest_review.reason || 'no reason supplied')}`
        : '';
      return `<article class="meta-chain">
        <strong>Decision → Claim ${index + 1} → AgentOutput</strong><br>
        <span class="ontology-badge ${escHtml(claim.review_status.toLowerCase())}">${escHtml(metaDerivationLabel(claim))}</span>
        ${escHtml(claim.label)}<br>
        Confidence ${Number(claim.confidence).toFixed(2)} · output <code title="${escHtml(source.output_id)}">${escHtml(shortId(source.output_id))}</code>${correction}<br>
        Governed link: <span class="ontology-badge ${escHtml(decisionLink.review_status.toLowerCase())}" title="${escHtml(decisionLink.kind)}">${escHtml(humanizeToken(decisionLink.kind))}</span>
        confidence ${Number(decisionLink.confidence).toFixed(2)}
        <details><summary>Exact provider/source evidence</summary><pre>${escHtml(JSON.stringify({
          source_prompt: source.source_prompt,
          provider_input: source.provider_input,
          provider_name: source.provider_name,
          provider_model: source.provider_model,
          provider_response_id: source.provider_response_id,
          provider_interventions: source.provider_interventions,
          provider_evidence: source.provider_evidence,
          evidence: source.evidence
        }, null, 2))}</pre></details>
      </article>`;
    }).join('');
  } catch (err) {
    answer.className = 'meta-answer meta-error';
    answer.textContent = errorMessage(err);
  }
}

export async function loadProvenance(versionId) {
  const target = document.getElementById(`provenance-${versionId}`);
  try {
    const result = await api('GET', `/artifact-versions/${versionId}/provenance`);
    target.innerHTML = result.claims.map(claim => `
      <div class="detail"><strong>Claim ${claim.ordinal}</strong> · AI-derived<br>
      Source <code>${escHtml(claim.output_id)}</code><br>${escHtml(claim.evidence)}</div>`).join('');
  } catch (err) {
    target.textContent = errorMessage(err);
  }
}
