// static/js/bridge.js
//
// Captures pane — the even-odysseus bridge surface inside Odysseus.
//
// Left column: the pending capture-review inbox (confirm / dismiss /
// re-bucket — the same single human gate the phone plugin drives; a
// confirmed `manual` capture lands on the My Tasks board, an automatable
// one fires an Odysseus agent-task). Right: the recorded-sessions browser
// (record + transcript + audio). All data is proxied live from the brain
// service via /api/bridge/* — nothing is copied into Odysseus.
//
// Fully self-contained: injects its own stylesheet, pane DOM, and sidebar
// entry — index.html's only hook is this script tag. If /api/bridge/status
// reports unconfigured, nothing is injected at all.

import { showToast } from './ui.js';

const API = window.location.origin;

const BUCKETS = [
  { name: 'research', label: 'research' },
  { name: 'email-draft', label: 'email draft' },
  { name: 'manual', label: 'my list' },
];

let _open = false;
let _review = [];
let _sessions = [];
let _detailId = null;
let _pollTimer = null;

/* ── api ── */

async function _api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(`${API}${path}`, opts);
  if (!res.ok) {
    let detail = `${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch { /* noop */ }
    throw new Error(detail);
  }
  return res.json();
}

async function _loadReview() {
  const data = await _api('GET', '/api/bridge/review');
  _review = data.review || [];
}

async function _loadSessions() {
  const data = await _api('GET', '/api/bridge/sessions');
  _sessions = data.sessions || [];
}

/* ── format helpers ── */

function _esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function _when(iso) {
  if (!iso) return '';
  return iso.slice(0, 16).replace('T', ' · ');
}

function _dur(s) {
  if (!s) return '';
  const m = Math.round(s / 60);
  return m >= 60 ? `${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}` : `${m}m`;
}

const REASONS = {
  uncertain_owner: 'whose task?',
  low_confidence: 'unsure',
  owned_unconfirmed: 'heard you own this',
  verifier_ungrounded: 'not grounded',
  verifier_unavailable: 'unverified',
};

// Minimal markdown for the session record (headings / bullets / bold). The
// record is our own pipeline's output, escaped first — not arbitrary input.
function _md(text) {
  const out = [];
  let list = false;
  for (const raw of String(text || '').split('\n')) {
    const line = _esc(raw);
    const h = line.match(/^(#{1,3})\s+(.*)$/);
    const b = line.match(/^[-*]\s+(.*)$/);
    if (b) {
      if (!list) { out.push('<ul>'); list = true; }
      out.push(`<li>${b[1]}</li>`);
      continue;
    }
    if (list) { out.push('</ul>'); list = false; }
    if (h) out.push(`<h${h[1].length + 1}>${h[2]}</h${h[1].length + 1}>`);
    else if (line.trim()) out.push(`<p>${line}</p>`);
  }
  if (list) out.push('</ul>');
  return out.join('\n')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
}

/* ── review inbox ── */

function _rcardEl(line) {
  const card = document.createElement('div');
  card.className = 'bridge-rcard';
  card.dataset.index = line.queue_index;
  const reason = REASONS[line.reason] || line.reason;
  const when = line.created_at ? line.created_at.slice(0, 10) : '';
  card.innerHTML = `
    <div class="bridge-rtext">${_esc(line.text)}</div>
    <div class="bridge-rmeta">${_esc(line.kind)} · ${_esc(reason)}${when ? ` · ${when}` : ''}</div>
    <div class="bridge-buckets"></div>
    <div class="bridge-ractions">
      <button class="bridge-confirm" type="button">confirm</button>
      <button class="bridge-dismiss" type="button">dismiss</button>
    </div>`;
  const bucketsEl = card.querySelector('.bridge-buckets');
  let picked = line.bucket;
  const paint = () => {
    bucketsEl.innerHTML = '';
    for (const b of BUCKETS) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'bridge-bucket' + (picked === b.name ? ' picked' : '');
      chip.textContent = picked === b.name ? `→ ${b.label}` : b.label;
      chip.addEventListener('click', async () => {
        try {
          await _api('POST', '/api/bridge/review/resolve',
            { index: line.queue_index, disposition: 'reclassify', bucket: b.name });
          picked = b.name;
          paint();
        } catch (e) { showToast(`reclassify failed: ${e.message}`); }
      });
      bucketsEl.appendChild(chip);
    }
  };
  paint();

  const act = async (disposition) => {
    card.querySelectorAll('button').forEach(b => (b.disabled = true));
    try {
      const body = { index: line.queue_index, disposition };
      if (disposition === 'confirm' && picked) body.bucket = picked;
      await _api('POST', '/api/bridge/review/resolve', body);
    } catch (e) {
      showToast(`${disposition} failed: ${e.message}`);
      card.querySelectorAll('button').forEach(b => (b.disabled = false));
      return;
    }
    card.classList.add('leaving');
    setTimeout(() => {
      _review = _review.filter(l => l.queue_index !== line.queue_index);
      _renderReview();
      _paintBadge();
    }, 200);
    if (disposition === 'confirm') {
      showToast(picked === 'manual' ? 'Confirmed → My Tasks'
        : `Confirmed → ${picked || 'research'} agent`);
    }
  };
  card.querySelector('.bridge-confirm').addEventListener('click', () => act('confirm'));
  card.querySelector('.bridge-dismiss').addEventListener('click', () => act('dismiss'));
  return card;
}

function _renderReview() {
  const pane = document.getElementById('bridge-pane');
  if (!pane) return;
  const head = pane.querySelector('.bridge-review .bridge-col-head');
  head.innerHTML = `<span>Review</span>` +
    (_review.length ? `<span class="bridge-count">${_review.length}</span>` : '');
  const list = pane.querySelector('.bridge-review-list');
  list.innerHTML = '';
  if (!_review.length) {
    list.innerHTML = `<div class="bridge-empty">Nothing to review — all caught up.</div>`;
    return;
  }
  _review.forEach(l => list.appendChild(_rcardEl(l)));
}

/* ── sessions ── */

function _renderSessions() {
  const pane = document.getElementById('bridge-pane');
  if (!pane) return;
  const wrap = pane.querySelector('.bridge-sessions');
  const head = wrap.querySelector('.bridge-col-head');
  const body = wrap.querySelector('.bridge-sessions-body');
  if (_detailId) { _renderDetail(body); head.innerHTML = '<span>Session</span>'; return; }
  head.innerHTML = `<span>Sessions</span><span class="bridge-sub">${_sessions.length}</span>`;
  body.innerHTML = '';
  const list = document.createElement('div');
  list.className = 'bridge-sessions-list';
  if (!_sessions.length) {
    list.innerHTML = `<div class="bridge-empty">No recorded sessions yet.</div>`;
  }
  for (const s of _sessions) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'bridge-srow';
    const c = s.counts || {};
    const bits = [];
    if (c.items) bits.push(`${c.items} items`);
    if (s.duration_s) bits.push(_dur(s.duration_s));
    row.innerHTML = `
      <span class="bridge-swhen">${_esc(_when(s.started) || s.id.slice(0, 15))}</span>
      <span class="bridge-stitle">${_esc(s.title || s.summary || s.id)}</span>
      <span class="bridge-scounts">${_esc(bits.join(' · '))}</span>`;
    row.addEventListener('click', () => { _detailId = s.id; _renderSessions(); });
    list.appendChild(row);
  }
  body.appendChild(list);
}

async function _renderDetail(body) {
  body.innerHTML = `<div class="bridge-empty">Loading…</div>`;
  let d;
  try {
    d = await _api('GET', `/api/bridge/sessions/${encodeURIComponent(_detailId)}`);
  } catch (e) {
    body.innerHTML = `<div class="bridge-empty">Couldn't load session (${_esc(e.message)})</div>`;
    return;
  }
  const meta = d.meta || {};
  const wrap = document.createElement('div');
  wrap.className = 'bridge-detail';
  wrap.innerHTML = `
    <button class="bridge-back" type="button">‹ sessions</button>
    <div class="bridge-dtitle">${_esc(meta.title || d.id)}</div>
    <div class="bridge-dmeta">${_esc(_when(meta.started))}${meta.duration_s ? ` · ${_dur(meta.duration_s)}` : ''}${meta.extract_error ? ' · ⚠ extraction failed — transcript only' : ''}</div>
    ${d.has_audio ? `<audio class="bridge-audio" controls preload="none" src="/api/bridge/sessions/${encodeURIComponent(d.id)}/audio"></audio>` : ''}
    <div class="bridge-record">${_md(d.record) || '<p class="bridge-empty">No record.</p>'}</div>
    <details class="bridge-transcript"><summary>Transcript</summary><pre>${_esc(d.transcript || '')}</pre></details>`;
  wrap.querySelector('.bridge-back').addEventListener('click', () => {
    _detailId = null;
    _renderSessions();
  });
  body.innerHTML = '';
  body.appendChild(wrap);
}

/* ── badge + open/close ── */

function _paintBadge() {
  const el = document.querySelector('#tool-bridge-btn .bridge-badge');
  if (el) el.textContent = _review.length ? String(_review.length) : '';
}

async function _refresh() {
  try {
    await Promise.all([_loadReview(), _loadSessions()]);
  } catch (e) {
    showToast(`captures: ${e.message}`);
  }
  _renderReview();
  _renderSessions();
  _paintBadge();
  // Fill in missing bucket proposals (slow, model-backed) after first paint.
  if (_review.some(l => !l.bucket)) {
    _api('POST', '/api/bridge/review/classify', {}).then(data => {
      if (data && data.review) { _review = data.review; _renderReview(); }
    }).catch(() => { /* chips stay unclassified */ });
  }
}

export async function openCaptures() {
  const pane = document.getElementById('bridge-pane');
  if (!pane) return;
  _open = true;
  pane.classList.add('open');
  document.getElementById('tool-bridge-btn')?.classList.add('active');
  await _refresh();
}

export function closeCaptures() {
  _open = false;
  _detailId = null;
  document.getElementById('bridge-pane')?.classList.remove('open');
  document.getElementById('tool-bridge-btn')?.classList.remove('active');
}

/* ── bootstrap ── */

function _injectDom() {
  if (document.getElementById('bridge-pane')) return;

  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = '/static/bridge.css';
  document.head.appendChild(link);

  const pane = document.createElement('div');
  pane.id = 'bridge-pane';
  pane.setAttribute('role', 'region');
  pane.setAttribute('aria-label', 'Captures');
  pane.innerHTML = `
    <div class="bridge-header">
      <span class="bridge-title">Captures</span>
      <span class="bridge-sub">glasses → review → tasks</span>
      <span class="bridge-header-spacer"></span>
      <button type="button" class="bridge-close">esc close</button>
    </div>
    <div class="bridge-body">
      <div class="bridge-review">
        <div class="bridge-col-head"><span>Review</span></div>
        <div class="bridge-review-list"></div>
      </div>
      <div class="bridge-sessions">
        <div class="bridge-col-head"><span>Sessions</span></div>
        <div class="bridge-sessions-body" style="flex:1;display:flex;flex-direction:column;min-height:0;"></div>
      </div>
    </div>`;
  pane.querySelector('.bridge-close').addEventListener('click', closeCaptures);
  document.body.appendChild(pane);

  const anchor = document.getElementById('tool-board-btn') ||
    document.getElementById('tool-notes-btn');
  if (anchor && !document.getElementById('tool-bridge-btn')) {
    const item = document.createElement('div');
    item.className = 'list-item';
    item.id = 'tool-bridge-btn';
    item.innerHTML = `
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
        style="flex-shrink:0;opacity:0.5;">
        <path d="M12 2a4 4 0 0 0-4 4v6a4 4 0 0 0 8 0V6a4 4 0 0 0-4-4z"/>
        <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
        <line x1="12" y1="19" x2="12" y2="22"/>
      </svg>
      <span class="grow">Captures</span>
      <span class="bridge-badge" style="color:var(--accent, var(--color-accent));font-size:11px;"></span>`;
    item.addEventListener('click', () => (_open ? closeCaptures() : openCaptures()));
    anchor.parentNode.insertBefore(item, anchor);
  }

  document.addEventListener('keydown', e => {
    if (!_open) return;
    if (e.key === 'Escape') {
      if (_detailId) { _detailId = null; _renderSessions(); return; }
      closeCaptures();
    }
  });
}

async function _boot() {
  let status;
  try {
    status = await _api('GET', '/api/bridge/status');
  } catch { return; }
  if (!status.configured) return;
  _injectDom();
  // Keep the sidebar badge honest even while the pane is closed.
  const tick = async () => {
    if (!_open) {
      try { await _loadReview(); _paintBadge(); } catch { /* offline */ }
    }
    _pollTimer = setTimeout(tick, 90_000);
  };
  try { await _loadReview(); _paintBadge(); } catch { /* offline */ }
  _pollTimer = setTimeout(tick, 90_000);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _boot);
} else {
  _boot();
}

window.odysseusCaptures = { open: openCaptures, close: closeCaptures };
