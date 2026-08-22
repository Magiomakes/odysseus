// static/js/board.js
//
// My Tasks board — Sunsama-style personal task planner inside Odysseus.
//
// Layout, left to right: AGENTS column (the far-left "teammate" column —
// drop a card on its target to hand off; working/review cards live there),
// backlog rail grouped by Sunsama-style horizons, then a rolling week of
// day columns (today + 6, plus a calm "Earlier" column when overdue
// planned cards exist).
//
// Cards carry #channels (free-form, colored deterministically), time
// estimates (day columns show a planned-total footer), and flow: todo →
// handed_off (agent working) → in_review (result attached, human gate) →
// done → archived. Agent results are reconciled server-side on every
// board read. Fully self-contained: injects its own stylesheet, pane DOM,
// and sidebar entry — index.html's only hook is this script tag.

import { showToast } from './ui.js';
import * as Modals from './modalManager.js';
import { makeWindowDraggable } from './windowDrag.js';

const API = window.location.origin;
const MODAL_ID = 'board-modal';

const BOARD_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="5" height="18" rx="1"/><rect x="10" y="3" width="5" height="12" rx="1"/><rect x="17" y="3" width="5" height="8" rx="1"/></svg>';
const HOME_KEY = 'board-home-view';
const COLLAPSE_KEY = 'board-horizons-collapsed';
const SHOW_AGENTS_KEY = 'board-show-agents';
const SHOW_BACKLOG_KEY = 'board-show-backlog';

function _railOn(key) { return (localStorage.getItem(key) || 'on') === 'on'; }
function _railToggle(key) {
  localStorage.setItem(key, _railOn(key) ? 'off' : 'on');
  _render();
  // Rails change the strip's geometry — re-anchor rather than stranding
  // the user on whatever days the preserved scroll now points at.
  const pane = document.getElementById('board-pane');
  const body = pane?.querySelector('.board-body');
  const target = pane?.querySelector('.board-col.today') ||
    pane?.querySelector('.board-agents, .board-backlog');
  if (body && target) body.scrollLeft = Math.max(0, target.offsetLeft - 12);
  const cols = pane?.querySelector('.board-cols');
  if (cols && cols.scrollWidth > cols.clientWidth) cols.scrollLeft = 0;
}

const HORIZONS = [
  { key: 'week', label: 'This week or two' },
  { key: 'month', label: 'Next month' },
  { key: 'quarter', label: 'Next quarter' },
  { key: 'year', label: 'Next year' },
  { key: 'someday', label: 'Someday' },
  { key: 'never', label: 'Never' },
];

let _tasks = [];
let _models = [];
let _channelFilter = null;
let _open = false;
let _dragId = null;
let _pollTimer = null;
let _detailCardId = null;

// Capture Inbox (ADR-0015): pending capture decisions moved here from the
// old Captures pane. Fed by the bridge mod's /api/bridge/review proxies —
// soft dependency: bridge absent/unconfigured → the section never renders.
let _inbox = [];
let _bridgeOk = null;   // null = not probed yet, then true/false
let _classifyInFlight = false;

const INBOX_BUCKETS = [
  { name: 'research', label: 'research' },
  { name: 'email-draft', label: 'email draft' },
  { name: 'manual', label: 'my list' },
];
const INBOX_REASONS = {
  uncertain_owner: 'whose task?',
  low_confidence: 'unsure',
  owned_unconfirmed: 'heard you own this',
  verifier_ungrounded: 'not grounded',
  verifier_unavailable: 'unverified',
};

/* ── date + format helpers (all local-time) ── */

function _fmt(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}
function _today() { return _fmt(new Date()); }
function _addDays(iso, n) {
  const [y, m, d] = iso.split('-').map(Number);
  return _fmt(new Date(y, m - 1, d + n));
}
const _DAY_LABELS = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'];
function _dayLabel(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  return _DAY_LABELS[new Date(y, m - 1, d).getDay()];
}
function _dayNum(iso) { return String(Number(iso.split('-')[2])); }

// 90 → "1:30", 30 → "0:30"
function _fmtEst(min) {
  if (!min || min <= 0) return '';
  return `${Math.floor(min / 60)}:${String(min % 60).padStart(2, '0')}`;
}
// "1:30" | "90" | "90m" | "1.5h" → minutes (null on nonsense)
function _parseEst(s) {
  s = (s || '').trim().toLowerCase();
  if (!s) return 0;
  let m;
  if ((m = s.match(/^(\d+):([0-5]?\d)$/))) return Number(m[1]) * 60 + Number(m[2]);
  if ((m = s.match(/^(\d*\.?\d+)\s*h$/))) return Math.round(Number(m[1]) * 60);
  if ((m = s.match(/^(\d+)\s*m?$/))) return Number(m[1]);
  return null;
}

// Deterministic channel color: name → HSL hue. Colored text, no backgrounds
// (the accent budget stays reserved for today/drop/add/review).
function _channelColor(name) {
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.codePointAt(0)) % 360;
  return `hsl(${h}, 55%, 62%)`;
}

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

async function _load() {
  const data = await _api('GET', '/api/board/tasks');
  _tasks = data.tasks || [];
}

async function _loadInbox() {
  if (_bridgeOk === null) {
    try {
      const s = await _api('GET', '/api/bridge/status');
      _bridgeOk = !!(s.configured && s.ok);
    } catch { _bridgeOk = false; }
  }
  if (!_bridgeOk) { _inbox = []; return; }
  try {
    const data = await _api('GET', '/api/bridge/review');
    _inbox = data.review || [];
  } catch { _inbox = []; }
  // Backfill missing bucket proposals (slow, model-backed) after paint.
  if (_inbox.some(l => !l.bucket) && !_classifyInFlight) {
    _classifyInFlight = true;
    _api('POST', '/api/bridge/review/classify', {}).then(data => {
      if (data && data.review) { _inbox = data.review; _render(); }
    }).catch(() => { /* chips stay unclassified */ })
      .finally(() => { _classifyInFlight = false; });
  }
}

async function _loadModels() {
  try {
    const data = await _api('GET', '/api/models');
    const raw = Array.isArray(data) ? data : (data.models || data.data || []);
    _models = raw
      .map(m => (typeof m === 'string' ? m : (m.id || m.name || m.model || '')))
      .filter(Boolean);
  } catch { _models = []; }
}

/* ── selection helpers ── */

function _visible(t) {
  return !_channelFilter || t.channel === _channelFilter;
}

// Day/backlog columns hold cards that are NOT parked in the agents column.
function _inAgentsCol(t) { return t.status === 'handed_off' || t.status === 'in_review'; }

function _cardsFor(date) {
  const rows = _tasks.filter(t => _visible(t) && !_inAgentsCol(t) &&
    (date === null ? !t.planned_date : t.planned_date === date));
  rows.sort((a, b) =>
    (a.status === 'done') - (b.status === 'done') ||
    (a.position || 0) - (b.position || 0));
  return rows;
}

function _overdueCards() {
  const today = _today();
  return _tasks
    .filter(t => _visible(t) && !_inAgentsCol(t) && t.planned_date &&
      t.planned_date < today && t.status !== 'done')
    .sort((a, b) => (a.planned_date < b.planned_date ? -1 : 1));
}

function _esc(s) {
  const div = document.createElement('div');
  div.textContent = s ?? '';
  return div.innerHTML;
}

/* ── card element ── */

function _cardEl(t) {
  const el = document.createElement('div');
  const fromCapture = t.source && t.source !== 'manual';
  el.className = `board-card status-${t.status}${fromCapture ? ' capture' : ''}`;
  el.draggable = t.status !== 'handed_off';
  el.tabIndex = 0;
  el.dataset.id = t.id;
  const overdue = t.due && t.due < _today() && t.status !== 'done';
  const meta = [];
  if (fromCapture) meta.push('<span class="board-card-capture">capture</span>');
  if (t.channel) {
    meta.push(`<span class="board-card-channel" style="color:${_channelColor(t.channel)}">#${_esc(t.channel)}</span>`);
  }
  if (t.due) meta.push(`<span class="board-card-due${overdue ? ' overdue' : ''}">due ${_esc(t.due)}</span>`);
  if (t.status === 'handed_off') meta.push('<span>working…</span>');
  if (t.status === 'in_review') meta.push('<span>ready</span>');
  const est = _fmtEst(t.estimate_minutes);
  el.innerHTML = `
    <div class="board-card-row">
      <span class="board-card-glyph"></span>
      <span class="board-card-title">${_esc(t.title)}</span>
      ${est ? `<span class="board-card-est">${est}</span>` : ''}
    </div>
    ${meta.length ? `<div class="board-card-meta">${meta.join('')}</div>` : ''}`;
  // Auto-landed capture todos are dismissable in one hover-click (delete =
  // dismiss, ADR-0015) — the decision the old review inbox used to gate.
  if (fromCapture && t.status === 'todo') {
    const x = document.createElement('button');
    x.type = 'button';
    x.className = 'board-card-dismiss';
    x.title = 'Dismiss (delete this captured task)';
    x.textContent = '✕';
    x.addEventListener('click', async (e) => {
      e.stopPropagation();
      try {
        await _api('DELETE', `/api/board/tasks/${t.id}`);
        await _refresh();
      } catch (err) { showToast(`Dismiss failed: ${err.message}`); }
    });
    el.appendChild(x);
  }
  el.addEventListener('click', () => _openDetail(t.id));
  el.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); _openDetail(t.id); }
  });
  el.addEventListener('dragstart', e => {
    _dragId = t.id;
    el.classList.add('dragging');
    document.getElementById('board-pane').classList.add('drag-active');
    e.dataTransfer.effectAllowed = 'move';
    try { e.dataTransfer.setData('text/plain', t.id); } catch { /* IE-ism */ }
  });
  el.addEventListener('dragend', () => {
    _dragId = null;
    el.classList.remove('dragging');
    document.getElementById('board-pane').classList.remove('drag-active');
    document.querySelectorAll('.drop-target').forEach(n => n.classList.remove('drop-target'));
  });
  _wireTouchDrag(el, t);
  return el;
}

/* ── touch drag: long-press to lift, ghost follows the finger ──
   HTML5 DnD never fires from touch, so phones get their own path.
   A 320ms still-press lifts the card (moving early = a scroll, and
   cancels the lift); while dragging, a non-passive touchmove listener
   pins the page, elementFromPoint tracks the [data-drop] zone under
   the finger, and holding near a screen edge auto-scrolls the column
   strip so every column is reachable on a phone. */

let _touchDrag = null; // { id, card, ghost, x, y, raf, suppressClick }

function _wireTouchDrag(card, t) {
  card.addEventListener('pointerdown', e => {
    if (e.pointerType === 'mouse') return;          // desktop keeps HTML5 DnD
    if (t.status === 'handed_off' || _touchDrag) return;
    const startX = e.clientX, startY = e.clientY;
    const timer = setTimeout(() => {
      cleanup();
      _beginTouchDrag(card, t.id, startX, startY);
    }, 320);
    const onMove = ev => {
      if (Math.hypot(ev.clientX - startX, ev.clientY - startY) > 10) cleanup();
    };
    const cleanup = () => {
      clearTimeout(timer);
      card.removeEventListener('pointermove', onMove);
      card.removeEventListener('pointerup', cleanup);
      card.removeEventListener('pointercancel', cleanup);
    };
    card.addEventListener('pointermove', onMove);
    card.addEventListener('pointerup', cleanup);
    card.addEventListener('pointercancel', cleanup);
  });
  // A drag's trailing click must not open the detail overlay.
  card.addEventListener('click', e => {
    if (_touchDrag?.suppressClick && _touchDrag.card === card) {
      e.stopImmediatePropagation();
      e.preventDefault();
      _touchDrag = null;
    }
  }, true);
  card.addEventListener('contextmenu', e => {
    if (_touchDrag) e.preventDefault();  // long-press menu vs drag
  });
}

function _beginTouchDrag(card, id, x, y) {
  const pane = document.getElementById('board-pane');
  const rect = card.getBoundingClientRect();
  const ghost = card.cloneNode(true);
  ghost.className = card.className + ' board-ghost';
  ghost.style.width = `${rect.width}px`;
  ghost.style.left = `${rect.left}px`;
  ghost.style.top = `${rect.top}px`;
  document.body.appendChild(ghost);

  _touchDrag = { id, card, ghost, x, y, raf: null, suppressClick: false };
  _dragId = id;
  card.classList.add('dragging');
  pane.classList.add('drag-active');
  try { navigator.vibrate?.(12); } catch { /* no haptics */ }

  const dx = x - rect.left, dy = y - rect.top;
  let zone = null;

  const setGhost = () => {
    ghost.style.left = `${_touchDrag.x - dx}px`;
    ghost.style.top = `${_touchDrag.y - dy}px`;
  };
  setGhost();

  const onPointerMove = e => {
    if (!_touchDrag) return;
    _touchDrag.x = e.clientX;
    _touchDrag.y = e.clientY;
  };
  const onTouchMove = e => { if (_touchDrag) e.preventDefault(); };

  const tick = () => {
    if (!_touchDrag) return;
    setGhost();
    // Edge auto-scroll every horizontally scrollable strip (mobile:
    // .board-body; desktop: .board-cols).
    for (const c of [pane.querySelector('.board-body'), pane.querySelector('.board-cols')]) {
      if (!c || c.scrollWidth <= c.clientWidth + 4) continue;
      if (_touchDrag.x < 52) c.scrollLeft -= 16;
      else if (_touchDrag.x > window.innerWidth - 52) c.scrollLeft += 16;
    }
    // Vertical auto-scroll inside the card list under the finger.
    const under = document.elementFromPoint(_touchDrag.x, _touchDrag.y);
    const list = under?.closest?.('.board-col-cards, .board-agent-sections, .board-backlog-groups');
    if (list && list.scrollHeight > list.clientHeight + 4) {
      const r = list.getBoundingClientRect();
      if (_touchDrag.y < r.top + 44) list.scrollTop -= 12;
      else if (_touchDrag.y > r.bottom - 44) list.scrollTop += 12;
    }
    const z = under?.closest?.('[data-drop]') || null;
    if (z !== zone) {
      zone?.classList.remove('drop-target');
      zone = z;
      zone?.classList.add('drop-target');
    }
    _touchDrag.raf = requestAnimationFrame(tick);
  };
  _touchDrag.raf = requestAnimationFrame(tick);

  const finish = async (commit) => {
    if (!_touchDrag) return;
    cancelAnimationFrame(_touchDrag.raf);
    document.removeEventListener('pointermove', onPointerMove);
    document.removeEventListener('touchmove', onTouchMove);
    document.removeEventListener('pointerup', onUp);
    document.removeEventListener('pointercancel', onCancel);
    ghost.remove();
    card.classList.remove('dragging');
    pane.classList.remove('drag-active');
    zone?.classList.remove('drop-target');
    const spec = commit && zone ? zone.dataset.drop : null;
    _dragId = null;
    _touchDrag.suppressClick = true;   // swallow the trailing click
    setTimeout(() => { _touchDrag = null; }, 350);
    if (spec) await _performDrop(spec, id);
  };
  const onUp = () => finish(true);
  const onCancel = () => finish(false);

  document.addEventListener('pointermove', onPointerMove);
  document.addEventListener('touchmove', onTouchMove, { passive: false });
  document.addEventListener('pointerup', onUp);
  document.addEventListener('pointercancel', onCancel);
}

/* ── drop dispatch (shared by mouse HTML5 DnD and touch drag) ── */
// Zones carry data-drop specs: "day|2026-07-28", "horizon|month", "agents".

async function _performDrop(spec, id) {
  try {
    if (spec === 'agents') {
      _showHandoffPicker(id);
      return;
    }
    if (spec.startsWith('horizon|')) {
      await _api('PATCH', `/api/board/tasks/${id}`, {
        clear_planned_date: true, horizon: spec.slice(8),
      });
      await _refresh();
      return;
    }
    if (spec.startsWith('day|')) {
      const date = spec.slice(4) || null;
      const inCol = _cardsFor(date);
      const last = inCol.length ? Math.max(...inCol.map(c => c.position || 0)) : 0;
      await _api('PATCH', `/api/board/tasks/${id}`, date
        ? { planned_date: date, position: last + 1 }
        : { clear_planned_date: true, position: last + 1 });
      await _refresh();
    }
  } catch (err) { showToast(`${err.message}`); }
}

function _wireDrop(zone, spec) {
  zone.dataset.drop = spec;
  zone.addEventListener('dragover', e => {
    if (!_dragId) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    zone.classList.add('drop-target');
  });
  zone.addEventListener('dragleave', () => zone.classList.remove('drop-target'));
  zone.addEventListener('drop', async e => {
    e.preventDefault();
    zone.classList.remove('drop-target');
    if (!_dragId) return;
    await _performDrop(spec, _dragId);
  });
}

function _dropToDay(listEl, date) { _wireDrop(listEl, `day|${date || ''}`); }
function _dropToHorizon(zone, horizon) { _wireDrop(zone, `horizon|${horizon}`); }

/* ── quick add ── */

// Trailing "#channel" tokens set the card's channel (Sunsama-style).
function _splitChannel(raw) {
  let title = raw.trim();
  let channel = null;
  const m = title.match(/(?:^|\s)#([^#\s][^#]*?)\s*$/);
  if (m) {
    channel = m[1].trim();
    title = title.slice(0, m.index).trim();
  }
  return { title, channel };
}

function _quickAdd(opts) {
  // opts: {planned_date} or {horizon}
  const wrap = document.createElement('div');
  wrap.className = 'board-add';
  const btn = document.createElement('button');
  btn.className = 'board-add-btn';
  btn.textContent = '+ task';
  btn.addEventListener('click', () => _openQuickAdd(wrap, btn, opts));
  wrap.appendChild(btn);
  return wrap;
}

function _openQuickAdd(wrap, btn, opts) {
  const input = document.createElement('input');
  input.type = 'text';
  input.placeholder = 'Title  #channel — Enter to add';
  wrap.replaceChildren(input);
  input.focus();
  const done = () => wrap.replaceChildren(btn);
  input.addEventListener('keydown', async e => {
    if (e.key === 'Escape') return done();
    if (e.key !== 'Enter') return;
    const { title, channel } = _splitChannel(input.value);
    if (!title) return done();
    try {
      await _api('POST', '/api/board/tasks', { title, channel, ...opts });
      await _refresh();
    } catch (err) { showToast(`Could not add task: ${err.message}`); }
  });
  input.addEventListener('blur', done);
}

/* ── capture inbox (ADR-0015: the agent-execution confirm gate) ──
   Manual captures auto-land scheduled; what needs a human here is the
   agent buckets (research / email-draft) — confirming fires a real agent
   through the brain's router, which delivers back onto this board as a
   linked, auto-handed-off card. */

function _inboxCardEl(line) {
  const card = document.createElement('div');
  card.className = 'board-inbox-card';
  const reason = INBOX_REASONS[line.reason] || line.reason;
  const when = line.created_at ? line.created_at.slice(0, 10) : '';
  card.innerHTML = `
    <div class="board-inbox-text">${_esc(line.text)}</div>
    <div class="board-inbox-meta">${_esc(line.kind)} · ${_esc(reason)}${when ? ` · ${when}` : ''}</div>
    <div class="board-inbox-buckets"></div>
    <div class="board-inbox-actions">
      <button class="bi-confirm" type="button">confirm</button>
      <button class="bi-dismiss" type="button">dismiss</button>
    </div>`;
  const bucketsEl = card.querySelector('.board-inbox-buckets');
  let picked = line.bucket;
  const paint = () => {
    bucketsEl.innerHTML = '';
    for (const b of INBOX_BUCKETS) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'board-inbox-bucket' + (picked === b.name ? ' picked' : '');
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
    _inbox = _inbox.filter(l => l.queue_index !== line.queue_index);
    if (disposition === 'confirm') {
      showToast(picked === 'manual' ? 'Confirmed → scheduled on the board'
        : `Confirmed → ${picked || 'research'} agent`);
      // The brain routes agent buckets back through /api/board/ingest with
      // auto-handoff — refresh shortly so the new card shows in Agents.
      setTimeout(_refresh, 1200);
    } else {
      _render();
    }
  };
  card.querySelector('.bi-confirm').addEventListener('click', () => act('confirm'));
  card.querySelector('.bi-dismiss').addEventListener('click', () => act('dismiss'));
  return card;
}

function _inboxEl() {
  const col = document.createElement('div');
  col.className = 'board-inbox';
  col.style.setProperty('--col-i', 0);
  col.innerHTML = `
    <div class="board-col-head">
      <span class="board-col-label">Inbox</span>
      <span class="board-col-count">${_inbox.length}</span>
    </div>
    <div class="board-inbox-list"></div>`;
  const list = col.querySelector('.board-inbox-list');
  _inbox.forEach(l => list.appendChild(_inboxCardEl(l)));
  return col;
}

/* ── agents column (far left) ── */

function _agentsColEl() {
  const col = document.createElement('div');
  col.className = 'board-agents';
  col.style.setProperty('--col-i', 0);
  col.innerHTML = `
    <div class="board-col-head"><span class="board-col-label">Agents</span></div>
    <div class="board-agent-drop">→ hand off<small>drop a card here</small></div>
    <div class="board-agent-sections"></div>`;

  const drop = col.querySelector('.board-agent-drop');
  _wireDrop(drop, 'agents');

  const sections = col.querySelector('.board-agent-sections');
  const working = _tasks.filter(t => _visible(t) && t.status === 'handed_off');
  const review = _tasks.filter(t => _visible(t) && t.status === 'in_review');
  const section = (label, cards, cls) => {
    const s = document.createElement('div');
    s.className = `board-agent-section ${cls}`;
    s.innerHTML = `<div class="board-agent-section-label">${label}${cards.length ? ` · ${cards.length}` : ''}</div>`;
    const list = document.createElement('div');
    list.className = 'board-col-cards';
    cards.forEach(t => list.appendChild(_cardEl(t)));
    if (!cards.length) list.innerHTML = `<div class="board-empty-hint">${cls === 'working' ? 'no agents running' : 'nothing to review'}</div>`;
    s.appendChild(list);
    return s;
  };
  sections.appendChild(section('Working', working, 'working'));
  sections.appendChild(section('Review', review, 'review'));
  return col;
}

function _showHandoffPicker(cardId) {
  document.getElementById('board-picker-backdrop')?.remove();
  const t = _tasks.find(x => x.id === cardId);
  if (!t) return;
  const backdrop = document.createElement('div');
  backdrop.className = 'board-detail-backdrop';
  backdrop.id = 'board-picker-backdrop';
  backdrop.addEventListener('click', e => { if (e.target === backdrop) backdrop.remove(); });

  const modelOpts = ['<option value="">default model</option>']
    .concat(_models.map(m => `<option value="${_esc(m)}">${_esc(m)}</option>`)).join('');
  const box = document.createElement('div');
  box.className = 'board-detail board-picker';
  box.innerHTML = `
    <div class="board-picker-title">Hand off: ${_esc(t.title)}</div>
    <div class="board-detail-row">
      <label>model</label>
      <select class="bp-model">${modelOpts}</select>
    </div>
    <div class="board-picker-choices">
      <button class="bp-complete"><strong>complete</strong><small>draft it, write it, do it</small></button>
      <button class="bp-research"><strong>research</strong><small>gather context, report back</small></button>
    </div>
    <div class="board-detail-actions"><span class="spacer"></span><button class="bp-cancel">cancel</button></div>`;

  const go = async (taskType) => {
    const model = box.querySelector('.bp-model').value || null;
    backdrop.remove();
    try {
      await _api('POST', `/api/board/tasks/${cardId}/handoff`, { task_type: taskType, model });
      showToast('Handed off — the card sits with the agent until it reports back.');
      await _refresh();
    } catch (err) { showToast(`Handoff failed: ${err.message}`); }
  };
  box.querySelector('.bp-complete').addEventListener('click', () => go('llm'));
  box.querySelector('.bp-research').addEventListener('click', () => go('research'));
  box.querySelector('.bp-cancel').addEventListener('click', () => backdrop.remove());
  backdrop.appendChild(box);
  document.body.appendChild(backdrop);
}

/* ── backlog with horizons ── */

function _collapsedSet() {
  try { return new Set(JSON.parse(localStorage.getItem(COLLAPSE_KEY) || '[]')); }
  catch { return new Set(); }
}
function _toggleCollapsed(key) {
  const s = _collapsedSet();
  s.has(key) ? s.delete(key) : s.add(key);
  localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...s]));
}

function _backlogEl() {
  const rail = document.createElement('div');
  rail.className = 'board-backlog';
  rail.style.setProperty('--col-i', 1);
  const backlog = _cardsFor(null);
  const openCount = backlog.filter(c => c.status !== 'done').length;
  rail.innerHTML = `<div class="board-col-head">
    <span class="board-col-label">Backlog</span>
    <span class="board-col-count">${openCount || ''}</span></div>`;

  const groupsWrap = document.createElement('div');
  groupsWrap.className = 'board-backlog-groups';
  const collapsed = _collapsedSet();

  for (const h of HORIZONS) {
    const cards = backlog.filter(t => (t.horizon || 'week') === h.key);
    const group = document.createElement('div');
    group.className = 'board-horizon';
    const head = document.createElement('div');
    head.className = 'board-horizon-head';
    const isCollapsed = collapsed.has(h.key);
    head.innerHTML = `
      <span class="board-horizon-caret">${isCollapsed ? '▸' : '▾'}</span>
      <span class="board-horizon-label">${h.label}</span>
      <span class="board-col-count">${cards.filter(c => c.status !== 'done').length || ''}</span>`;
    head.addEventListener('click', () => { _toggleCollapsed(h.key); _render(); });
    group.appendChild(head);
    _dropToHorizon(head, h.key);

    if (!isCollapsed) {
      const list = document.createElement('div');
      list.className = 'board-col-cards horizon-cards';
      cards.forEach(t => list.appendChild(_cardEl(t)));
      _dropToHorizon(list, h.key);
      group.appendChild(list);
      group.appendChild(_quickAdd({ horizon: h.key }));
    }
    groupsWrap.appendChild(group);
  }
  rail.appendChild(groupsWrap);
  return rail;
}

/* ── header ── */

function _headerEl() {
  const today = _today();
  const head = document.createElement('div');
  head.className = 'board-header';
  const channels = [...new Set(_tasks.filter(t => t.channel && t.status !== 'archived').map(t => t.channel))]
    .sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
  const opts = ['<option value="">#all</option>']
    .concat(channels.map(c => `<option value="${_esc(c)}"${c === _channelFilter ? ' selected' : ''}>#${_esc(c)}</option>`))
    .join('');
  const agentsOn = _railOn(SHOW_AGENTS_KEY);
  const backlogOn = _railOn(SHOW_BACKLOG_KEY);
  const workingN = _tasks.filter(t => _inAgentsCol(t)).length;
  head.innerHTML = `
    <span class="board-range">${today} → ${_addDays(today, 6)}</span>
    <select class="board-channel-filter" title="Filter by channel">${opts}</select>
    <span class="board-header-spacer"></span>
    <button class="board-rail-toggle${agentsOn ? '' : ' rail-off'}" data-rail="agents"
      title="Show/hide the agents column">agents${!agentsOn && workingN ? ` (${workingN})` : ''}</button>
    <button class="board-rail-toggle${backlogOn ? '' : ' rail-off'}" data-rail="backlog"
      title="Show/hide the backlog">backlog</button>
    <button id="board-refresh-btn" title="Refresh">refresh</button>`;
  head.querySelector('.board-channel-filter').addEventListener('change', e => {
    _channelFilter = e.target.value || null;
    _render();
  });
  head.querySelector('[data-rail="agents"]').addEventListener('click', () => _railToggle(SHOW_AGENTS_KEY));
  head.querySelector('[data-rail="backlog"]').addEventListener('click', () => _railToggle(SHOW_BACKLOG_KEY));
  head.querySelector('#board-refresh-btn').addEventListener('click', _refresh);
  return head;
}

/* ── day columns ── */

function _columnEl(date, { label, extraClass = '', colIndex = 0, cards = null } = {}) {
  const col = document.createElement('div');
  col.className = `board-col ${extraClass}`.trim();
  col.style.setProperty('--col-i', colIndex);
  const rows = cards ?? _cardsFor(date);
  const openCount = rows.filter(c => c.status !== 'done').length;
  const estTotal = rows.filter(c => c.status !== 'done')
    .reduce((sum, c) => sum + (c.estimate_minutes || 0), 0);
  const head = document.createElement('div');
  head.className = 'board-col-head';
  head.innerHTML = date
    ? `<span class="board-col-day">${_dayNum(date)}</span>
       <span class="board-col-label">${label || _dayLabel(date)}</span>
       <span class="board-col-count">${openCount || ''}</span>`
    : `<span class="board-col-label">${label}</span>
       <span class="board-col-count">${openCount || ''}</span>`;
  col.appendChild(head);

  const list = document.createElement('div');
  list.className = 'board-col-cards';
  rows.forEach(t => list.appendChild(_cardEl(t)));
  if (cards === null) {
    _dropToDay(list, date);
    // Add-task sits right beneath the card stack (Sunsama-style), inside
    // the scroll area — not detached at the column's bottom edge.
    list.appendChild(_quickAdd({ planned_date: date }));
  }
  col.appendChild(list);

  const foot = document.createElement('div');
  foot.className = 'board-col-foot';
  foot.textContent = estTotal ? `est ${_fmtEst(estTotal)}` : '';
  col.appendChild(foot);
  return col;
}

function _render() {
  const pane = document.getElementById('board-pane');
  if (!pane) return;
  // Re-renders must not yank the strip back to the left edge.
  const prevBody = pane.querySelector('.board-body');
  const prevScroll = prevBody
    ? { body: prevBody.scrollLeft, cols: pane.querySelector('.board-cols')?.scrollLeft || 0 }
    : null;
  pane.replaceChildren(_headerEl());

  const body = document.createElement('div');
  body.className = 'board-body';
  if (_inbox.length) body.appendChild(_inboxEl());
  if (_railOn(SHOW_AGENTS_KEY)) body.appendChild(_agentsColEl());
  if (_railOn(SHOW_BACKLOG_KEY)) body.appendChild(_backlogEl());

  const cols = document.createElement('div');
  cols.className = 'board-cols';
  let colIndex = 2;
  const overdue = _overdueCards();
  if (overdue.length) {
    cols.appendChild(_columnEl(null, {
      label: 'Earlier', extraClass: 'overdue-col', colIndex, cards: overdue,
    }));
    colIndex += 1;
  }
  const today = _today();
  for (let i = 0; i < 7; i++) {
    const date = _addDays(today, i);
    cols.appendChild(_columnEl(date, {
      extraClass: date === today ? 'today' : '',
      label: date === today ? 'TODAY' : undefined,
      colIndex,
    }));
    colIndex += 1;
  }
  body.appendChild(cols);
  pane.appendChild(body);

  if (prevScroll) {
    body.scrollLeft = prevScroll.body;
    cols.scrollLeft = prevScroll.cols;
  } else if (window.innerWidth <= 700) {
    // First open on a phone: land on Today, not the agents rail.
    requestAnimationFrame(() => {
      const todayCol = pane.querySelector('.board-col.today');
      if (todayCol) body.scrollLeft = todayCol.offsetLeft - 12;
    });
  }
}

async function _refresh() {
  try {
    await Promise.all([_load(), _loadInbox()]);
  } catch (err) {
    showToast(`Board load failed: ${err.message}`);
    return;
  }
  _render();
  _schedulePoll();
  if (_detailCardId) {
    const t = _tasks.find(x => x.id === _detailCardId);
    if (t) _renderDetail(t);
  }
}

function _schedulePoll() {
  if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
  if (!_open) return;
  const waiting = _tasks.some(t => t.status === 'handed_off');
  _pollTimer = setTimeout(_refresh, waiting ? 15000 : 90000);
}

/* ── detail / review overlay ── */

function _openDetail(id) {
  _detailCardId = id;
  const t = _tasks.find(x => x.id === id);
  if (t) _renderDetail(t);
}

function _closeDetail() {
  _detailCardId = null;
  document.getElementById('board-detail-backdrop')?.remove();
}

function _renderDetail(t) {
  document.getElementById('board-detail-backdrop')?.remove();
  const backdrop = document.createElement('div');
  backdrop.className = 'board-detail-backdrop';
  backdrop.id = 'board-detail-backdrop';
  backdrop.addEventListener('click', e => { if (e.target === backdrop) _closeDetail(); });

  const horizonOpts = HORIZONS.map(h =>
    `<option value="${h.key}"${(t.horizon || 'week') === h.key ? ' selected' : ''}>${h.label}</option>`).join('');
  const box = document.createElement('div');
  box.className = 'board-detail';
  const canEditDraft = !!(t.result && t.scheduled_task_id);
  const resultBlock = t.result ? `
    <span class="board-detail-result-label">Agent result${t.run_status === 'error' ? ' — <span class="run-error">run failed</span>' : ''}${canEditDraft ? ' <button type="button" class="bd-edit-draft">edit draft</button>' : ''}</span>
    <div class="board-detail-result">${_esc(t.result)}</div>` : '';
  const sourceLink = t.session_id
    ? `<button type="button" class="bd-session-link" title="Open the recording this task came from">source recording ↗</button>`
    : '';
  box.innerHTML = `
    <input class="board-detail-title" value="${_esc(t.title)}" />
    <textarea class="board-detail-notes" placeholder="notes / context for you or the agent">${_esc(t.notes || '')}</textarea>
    <div class="board-detail-row">
      <label>plan</label><input type="date" class="bd-plan" value="${t.planned_date || ''}">
      <label>due</label><input type="date" class="bd-due" value="${t.due || ''}">
    </div>
    <div class="board-detail-row">
      <label>#</label><input type="text" class="bd-channel" placeholder="channel" value="${_esc(t.channel || '')}" style="width:110px">
      <label>est</label><input type="text" class="bd-est" placeholder="1:30" value="${_fmtEst(t.estimate_minutes)}" style="width:52px">
      <label>horizon</label><select class="bd-horizon">${horizonOpts}</select>
      ${sourceLink}
      <span style="margin-left:auto">${_esc(t.status.replace('_', ' '))}${t.source !== 'manual' ? ` · ${_esc(t.source)}` : ''}${t.bucket ? ` · ${_esc(t.bucket)}` : ''}${t.draft_saved ? ' · in Drafts' : ''}</span>
    </div>
    ${resultBlock}
    <div class="board-detail-actions"></div>`;

  // Provenance jump: open the Captures window on this card's recording
  // (graceful no-op if the bridge mod is absent).
  box.querySelector('.bd-session-link')?.addEventListener('click', () => {
    if (window.odysseusCaptures?.openSession) {
      _closeDetail();
      window.odysseusCaptures.openSession(t.session_id);
    } else {
      showToast('Captures pane not available');
    }
  });

  // Edit-draft (ADR-0015 learn-from-edit): PATCH saves the edit (the server
  // preserves the pristine original once); then the edit is forwarded
  // fire-and-forget to the brain so it can learn contact facts from the diff.
  box.querySelector('.bd-edit-draft')?.addEventListener('click', () => {
    const resultEl = box.querySelector('.board-detail-result');
    if (!resultEl || box.querySelector('.bd-draft-editor')) return;
    const editor = document.createElement('div');
    editor.className = 'bd-draft-editor';
    const ta = document.createElement('textarea');
    ta.className = 'bd-draft-textarea';
    ta.value = t.result || '';
    const row = document.createElement('div');
    row.className = 'board-detail-actions';
    const save = document.createElement('button');
    save.className = 'primary';
    save.textContent = 'save edit';
    const cancel = document.createElement('button');
    cancel.textContent = 'cancel';
    row.append(save, cancel);
    editor.append(ta, row);
    resultEl.replaceWith(editor);
    ta.focus();
    cancel.addEventListener('click', () => _renderDetail(t));
    save.addEventListener('click', async () => {
      const edited = ta.value;
      save.disabled = cancel.disabled = true;
      try {
        await _api('PATCH', `/api/board/tasks/${t.id}`, { result: edited });
      } catch (err) {
        showToast(`Save failed: ${err.message}`);
        save.disabled = cancel.disabled = false;
        return;
      }
      // Learning is best-effort — the edit is already saved.
      _api('POST', '/api/bridge/draft-feedback', {
        card_id: t.id,
        title: t.title,
        original: t.result_original || t.result || '',
        edited,
        session_id: t.session_id || '',
      }).then(r => {
        const n = (r && (r.applied || 0) + (r.created || 0)) || 0;
        if (n) showToast(`Learned ${n} contact fact${n === 1 ? '' : 's'} from your edit`);
      }).catch(() => { /* learning skipped; edit kept */ });
      await _refresh();
    });
  });

  const actions = box.querySelector('.board-detail-actions');
  const btn = (label, cls, fn) => {
    const b = document.createElement('button');
    if (cls) b.className = cls;
    b.textContent = label;
    b.addEventListener('click', fn);
    actions.appendChild(b);
    return b;
  };

  if (t.status === 'in_review' || t.status === 'todo') {
    btn('mark done', 'primary', async () => {
      await _api('PATCH', `/api/board/tasks/${t.id}`, { status: 'done' });
      _closeDetail(); _refresh();
    });
  }
  if (t.status === 'in_review') {
    btn('back to todo', '', async () => {
      await _api('PATCH', `/api/board/tasks/${t.id}`, { status: 'todo' });
      _closeDetail(); _refresh();
    });
  }
  if (t.status === 'done') {
    btn('reopen', '', async () => {
      await _api('PATCH', `/api/board/tasks/${t.id}`, { status: 'todo' });
      _closeDetail(); _refresh();
    });
    btn('archive', '', async () => {
      await _api('PATCH', `/api/board/tasks/${t.id}`, { status: 'archived' });
      _closeDetail(); _refresh();
    });
  }
  if (t.status === 'todo') {
    btn('hand off', '', () => { _closeDetail(); _showHandoffPicker(t.id); });
  }
  const spacer = document.createElement('span');
  spacer.className = 'spacer';
  actions.appendChild(spacer);
  btn('delete', 'danger', async () => {
    await _api('DELETE', `/api/board/tasks/${t.id}`);
    _closeDetail(); _refresh();
  });

  const saveField = async (patch) => {
    try { await _api('PATCH', `/api/board/tasks/${t.id}`, patch); await _refresh(); }
    catch (err) { showToast(`Save failed: ${err.message}`); }
  };
  box.querySelector('.board-detail-title').addEventListener('change', e => {
    const v = e.target.value.trim();
    if (v && v !== t.title) saveField({ title: v });
  });
  box.querySelector('.board-detail-notes').addEventListener('change', e => {
    if (e.target.value !== (t.notes || '')) saveField({ notes: e.target.value });
  });
  box.querySelector('.bd-plan').addEventListener('change', e => {
    saveField(e.target.value ? { planned_date: e.target.value } : { clear_planned_date: true });
  });
  box.querySelector('.bd-due').addEventListener('change', e => {
    saveField(e.target.value ? { due: e.target.value } : { clear_due: true });
  });
  box.querySelector('.bd-channel').addEventListener('change', e => {
    saveField({ channel: e.target.value });
  });
  box.querySelector('.bd-est').addEventListener('change', e => {
    const min = _parseEst(e.target.value);
    if (min === null) { showToast('Estimate like 1:30, 90m, or 1.5h'); return; }
    saveField({ estimate_minutes: min });
  });
  box.querySelector('.bd-horizon').addEventListener('change', e => {
    saveField({ horizon: e.target.value });
  });

  backdrop.appendChild(box);
  document.body.appendChild(backdrop);
}

/* ── open / close / bootstrap ──
   Stock floating-window model (operator direction 2026-08-21): the board is a
   draggable/resizable/minimizable `.modal` window like every built-in tool —
   built on open, torn down on close, chip-docked on minimize — NOT a
   full-screen fixed pane. The board's own render machinery is untouched: it
   still targets #board-pane, which now lives inside the modal body. */

export async function openBoard() {
  const existing = document.getElementById(MODAL_ID);
  if (existing) {
    if (Modals.isMinimized(MODAL_ID)) { Modals.restore(MODAL_ID); return; }
    // Stock mobile gestures (swipe-down sheet dismiss, backdrop tap) hide
    // the modal directly in ui.js without telling modalManager. For a
    // teardown-on-close window that left a hidden-but-registered zombie
    // that open() refused to reopen. Normalize to a real minimize→restore.
    if (existing.classList.contains('hidden')) {
      Modals.minimize(MODAL_ID);
      Modals.restore(MODAL_ID);
    }
    return;
  }
  const modal = document.createElement('div');
  modal.className = 'modal';
  modal.id = MODAL_ID;
  modal.innerHTML = `
    <div class="modal-content board-modal-content">
      <div class="modal-header">
        <h4>${BOARD_ICON}<span style="margin-left:6px">My Tasks</span></h4>
        <span style="flex:1"></span>
        <button class="close-btn" title="Close">✖</button>
      </div>
      <div class="modal-body board-modal-body">
        <div id="board-pane" role="region" aria-label="My Tasks board"></div>
      </div>
    </div>`;
  document.body.appendChild(modal);

  Modals.register(MODAL_ID, {
    sidebarBtnId: 'tool-board-btn',
    label: 'My Tasks',
    icon: BOARD_ICON,
    restoreFn: () => { _refresh(); },
    closeFn: _teardown,
  });
  Modals.injectMinimizeButton(modal, MODAL_ID);
  modal.querySelector('.close-btn').addEventListener('click', (e) => {
    e.stopPropagation();
    Modals.close(MODAL_ID);
  });
  {
    const content = modal.querySelector('.modal-content');
    const header = modal.querySelector('.modal-header');
    if (content && header) makeWindowDraggable(modal, { content, header });
  }

  _open = true;
  document.getElementById('tool-board-btn')?.classList.add('active');
  _loadModels();  // fire-and-forget; picker falls back to "default model"
  await _refresh();
}

// Full teardown — invoked by Modals.close (✖ button, closeBoard, Esc).
function _teardown() {
  _open = false;
  _closeDetail();
  document.getElementById('board-picker-backdrop')?.remove();
  if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
  document.getElementById(MODAL_ID)?.remove();
  document.getElementById('tool-board-btn')?.classList.remove('active');
}

export function closeBoard() {
  if (Modals.isRegistered(MODAL_ID)) Modals.close(MODAL_ID);
  else _teardown();
}

function _injectDom() {
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = '/static/board.css';
  document.head.appendChild(link);

  const notesBtn = document.getElementById('tool-notes-btn');
  if (notesBtn && !document.getElementById('tool-board-btn')) {
    const item = document.createElement('div');
    item.className = 'list-item';
    item.id = 'tool-board-btn';
    item.innerHTML = `
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
        style="flex-shrink:0;opacity:0.5;">
        <rect x="3" y="3" width="5" height="18" rx="1"/>
        <rect x="10" y="3" width="5" height="12" rx="1"/>
        <rect x="17" y="3" width="5" height="8" rx="1"/>
      </svg>
      <span class="grow">My Tasks</span>`;
    // Stock sidebar semantics: closed → open, minimized → restore, open → minimize.
    item.addEventListener('click', () => {
      const modal = document.getElementById(MODAL_ID);
      if (!modal) { openBoard(); return; }
      if (Modals.isMinimized(MODAL_ID) || modal.classList.contains('hidden')) {
        openBoard();  // restores, or recovers a gesture-hidden sheet
      } else {
        Modals.minimize(MODAL_ID);
      }
    });
    notesBtn.parentNode.insertBefore(item, notesBtn);
  }

  // Mobile swipe-down fires `modal-dismissed` AFTER ui.js has already hidden
  // the modal. Stock big tools (cookbook/calendar/email) re-route that to
  // minimize via modalManager's allowlist; this window isn't on it, so do the
  // same re-route mod-locally — otherwise the sheet is hidden with `_open`
  // still true and no dock chip, and it can never be reopened.
  window.addEventListener('modal-dismissed', (e) => {
    if (e.detail?.id !== MODAL_ID) return;
    if (!document.getElementById(MODAL_ID)) return;
    Modals.minimize(MODAL_ID);
  });

  document.addEventListener('keydown', e => {
    if (!_open || Modals.isMinimized(MODAL_ID)) return;
    if (e.key === 'Escape') {
      if (document.getElementById('board-picker-backdrop')) {
        document.getElementById('board-picker-backdrop').remove();
        return;
      }
      if (_detailCardId) { _closeDetail(); return; }
      closeBoard();
      return;
    }
    // Sunsama's "add a task with A" — quick-add into Today.
    if ((e.key === 'a' || e.key === 'A') && !e.metaKey && !e.ctrlKey && !e.altKey) {
      const tag = (document.activeElement?.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select' ||
          document.activeElement?.isContentEditable) return;
      const todayCol = document.querySelector('.board-col.today .board-add');
      const btn = todayCol?.querySelector('.board-add-btn');
      if (btn) { e.preventDefault(); btn.click(); }
    }
  });
}

function _boot() {
  _injectDom();
  if ((localStorage.getItem(HOME_KEY) || 'on') === 'on') {
    openBoard();
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _boot);
} else {
  _boot();
}

// Console escape hatch: localStorage.setItem('board-home-view','off')
window.odysseusBoard = { open: openBoard, close: closeBoard };
