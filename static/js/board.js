// static/js/board.js
//
// My Tasks board — Sunsama-style personal task planner inside Odysseus.
// Backlog rail + rolling week of day columns (today + 6). Cards are the
// user's own tasks (manual, or ingested from the even-odysseus bridge via
// POST /api/board/ingest). Drag a card between days to plan it; drag it
// onto the handoff dock to hand it to an agent (creates a run-now
// scheduled task). Agent results are reconciled server-side on every
// board read and land on the card as 'in_review' — the human review gate.
//
// Fully self-contained: injects its own stylesheet, pane DOM, and sidebar
// entry, so the only index.html hook is this module's script tag.

import { showToast } from './ui.js';

const API = window.location.origin;
const HOME_KEY = 'board-home-view';

let _tasks = [];
let _open = false;
let _dragId = null;
let _pollTimer = null;
let _detailCardId = null;

/* ── date helpers (all local-time) ── */

function _fmt(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}
function _today() { return _fmt(new Date()); }
function _addDays(iso, n) {
  const [y, m, d] = iso.split('-').map(Number);
  const dt = new Date(y, m - 1, d + n);
  return _fmt(dt);
}
const _DAY_LABELS = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'];
function _dayLabel(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  return _DAY_LABELS[new Date(y, m - 1, d).getDay()];
}
function _dayNum(iso) { return String(Number(iso.split('-')[2])); }

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

/* ── rendering ── */

function _cardsFor(date) {
  // date === null → backlog
  const rows = _tasks.filter(t =>
    date === null ? !t.planned_date : t.planned_date === date);
  rows.sort((a, b) =>
    (a.status === 'done') - (b.status === 'done') ||
    (a.position || 0) - (b.position || 0));
  return rows;
}

function _overdueCards() {
  const today = _today();
  return _tasks
    .filter(t => t.planned_date && t.planned_date < today && t.status !== 'done')
    .sort((a, b) => (a.planned_date < b.planned_date ? -1 : 1));
}

function _esc(s) {
  const div = document.createElement('div');
  div.textContent = s ?? '';
  return div.innerHTML;
}

function _cardEl(t) {
  const el = document.createElement('div');
  el.className = `board-card status-${t.status}`;
  el.draggable = t.status !== 'handed_off';
  el.tabIndex = 0;
  el.dataset.id = t.id;
  const overdue = t.due && t.due < _today() && t.status !== 'done';
  const meta = [];
  if (t.due) meta.push(`<span class="board-card-due${overdue ? ' overdue' : ''}">due ${_esc(t.due)}</span>`);
  if (t.source && t.source !== 'manual') meta.push(`<span>${_esc(t.source)}</span>`);
  if (t.status === 'handed_off') meta.push('<span>agent working…</span>');
  if (t.status === 'in_review') meta.push('<span>ready for review</span>');
  el.innerHTML = `
    <div class="board-card-row">
      <span class="board-card-glyph"></span>
      <span class="board-card-title">${_esc(t.title)}</span>
    </div>
    ${meta.length ? `<div class="board-card-meta">${meta.join('')}</div>` : ''}`;
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
  return el;
}

function _columnEl(date, { label, extraClass = '', colIndex = 0 } = {}) {
  const col = document.createElement('div');
  col.className = `board-col ${extraClass}`.trim();
  col.style.setProperty('--col-i', colIndex);
  const cards = _cardsFor(date);
  const openCount = cards.filter(c => c.status !== 'done').length;
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
  list.dataset.date = date || '';
  cards.forEach(t => list.appendChild(_cardEl(t)));
  _wireDropzone(list, date);
  col.appendChild(list);

  col.appendChild(_quickAdd(date));
  return col;
}

function _quickAdd(date) {
  const wrap = document.createElement('div');
  wrap.className = 'board-add';
  const btn = document.createElement('button');
  btn.className = 'board-add-btn';
  btn.textContent = '+ task';
  btn.addEventListener('click', () => {
    const input = document.createElement('input');
    input.type = 'text';
    input.placeholder = 'Task title — Enter to add';
    wrap.replaceChildren(input);
    input.focus();
    const done = () => wrap.replaceChildren(btn);
    input.addEventListener('keydown', async e => {
      if (e.key === 'Escape') return done();
      if (e.key !== 'Enter') return;
      const title = input.value.trim();
      if (!title) return done();
      try {
        await _api('POST', '/api/board/tasks', { title, planned_date: date || null });
        await _refresh();
      } catch (err) { showToast(`Could not add task: ${err.message}`); }
    });
    input.addEventListener('blur', done);
  });
  wrap.appendChild(btn);
  return wrap;
}

function _wireDropzone(listEl, date) {
  listEl.addEventListener('dragover', e => {
    if (!_dragId) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    listEl.classList.add('drop-target');
  });
  listEl.addEventListener('dragleave', () => listEl.classList.remove('drop-target'));
  listEl.addEventListener('drop', async e => {
    e.preventDefault();
    listEl.classList.remove('drop-target');
    if (!_dragId) return;
    const id = _dragId;
    // Position: append after the column's last card.
    const inCol = _cardsFor(date);
    const last = inCol.length ? Math.max(...inCol.map(c => c.position || 0)) : 0;
    try {
      await _api('PATCH', `/api/board/tasks/${id}`, date
        ? { planned_date: date, position: last + 1 }
        : { clear_planned_date: true, position: last + 1 });
      await _refresh();
    } catch (err) { showToast(`Move failed: ${err.message}`); }
  });
}

function _dockEl() {
  const dock = document.createElement('div');
  dock.className = 'board-dock';
  const targets = [
    { type: 'llm', title: '→ agent: complete', hint: 'draft it, write it, do it' },
    { type: 'research', title: '→ agent: research', hint: 'gather context and report back' },
  ];
  for (const t of targets) {
    const zone = document.createElement('div');
    zone.className = 'board-dock-target';
    zone.innerHTML = `${_esc(t.title)}<small>${_esc(t.hint)}</small>`;
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
      const id = _dragId;
      try {
        await _api('POST', `/api/board/tasks/${id}/handoff`, { task_type: t.type });
        showToast('Handed off — the card will flip to review when the agent finishes.');
        await _refresh();
      } catch (err) { showToast(`Handoff failed: ${err.message}`); }
    });
    dock.appendChild(zone);
  }
  return dock;
}

function _render() {
  const pane = document.getElementById('board-pane');
  if (!pane) return;
  const today = _today();
  const range = `${today} → ${_addDays(today, 6)}`;
  pane.innerHTML = `
    <div class="board-header">
      <span class="board-title">My Tasks</span>
      <span class="board-range">${range}</span>
      <span class="board-header-spacer"></span>
      <button id="board-refresh-btn" title="Refresh">refresh</button>
      <button id="board-close-btn" title="Close (Esc)">close</button>
    </div>
    <div class="board-body">
      <div class="board-backlog" style="--col-i:0"></div>
      <div class="board-cols"></div>
    </div>`;

  const backlog = pane.querySelector('.board-backlog');
  const backlogCards = _cardsFor(null);
  const head = document.createElement('div');
  head.className = 'board-col-head';
  head.innerHTML = `<span class="board-col-label">Backlog</span>
    <span class="board-col-count">${backlogCards.filter(c => c.status !== 'done').length || ''}</span>`;
  backlog.appendChild(head);
  const list = document.createElement('div');
  list.className = 'board-col-cards';
  backlogCards.forEach(t => list.appendChild(_cardEl(t)));
  if (!backlogCards.length) {
    list.innerHTML = '<div class="board-empty-hint">Captures from your glasses and manual tasks land here.</div>';
  }
  _wireDropzone(list, null);
  backlog.appendChild(list);
  backlog.appendChild(_quickAdd(null));

  const cols = pane.querySelector('.board-cols');
  let colIndex = 1;
  const overdue = _overdueCards();
  if (overdue.length) {
    const oc = _columnEl(null, { label: 'Earlier', extraClass: 'overdue-col', colIndex });
    // Earlier column shows its own card set — replace the backlog cards
    // the generic builder put in.
    const ocList = oc.querySelector('.board-col-cards');
    ocList.replaceChildren(...overdue.map(t => _cardEl(t)));
    oc.querySelector('.board-col-count').textContent = overdue.length;
    oc.querySelector('.board-add')?.remove();
    cols.appendChild(oc);
    colIndex += 1;
  }
  for (let i = 0; i < 7; i++) {
    const date = _addDays(today, i);
    const col = _columnEl(date, {
      extraClass: date === today ? 'today' : '',
      label: date === today ? 'TODAY' : undefined,
      colIndex,
    });
    cols.appendChild(col);
    colIndex += 1;
  }

  pane.appendChild(_dockEl());
  pane.querySelector('#board-close-btn').addEventListener('click', closeBoard);
  pane.querySelector('#board-refresh-btn').addEventListener('click', _refresh);
}

async function _refresh() {
  try {
    await _load();
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

  const box = document.createElement('div');
  box.className = 'board-detail';
  const resultBlock = t.result ? `
    <span class="board-detail-result-label">Agent result${t.run_status === 'error' ? ' — <span class="run-error">run failed</span>' : ''}</span>
    <div class="board-detail-result">${_esc(t.result)}</div>` : '';
  box.innerHTML = `
    <input class="board-detail-title" value="${_esc(t.title)}" />
    <textarea class="board-detail-notes" placeholder="notes / context for you or the agent">${_esc(t.notes || '')}</textarea>
    <div class="board-detail-row">
      <label>plan</label><input type="date" class="bd-plan" value="${t.planned_date || ''}">
      <label>due</label><input type="date" class="bd-due" value="${t.due || ''}">
      <span style="margin-left:auto">${_esc(t.status.replace('_', ' '))}${t.source !== 'manual' ? ` · ${_esc(t.source)}` : ''}</span>
    </div>
    ${resultBlock}
    <div class="board-detail-actions"></div>`;

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
    btn('hand off', '', async () => {
      try {
        await _api('POST', `/api/board/tasks/${t.id}/handoff`, { task_type: 'llm' });
        showToast('Handed off to an agent.');
      } catch (err) { showToast(`Handoff failed: ${err.message}`); }
      _closeDetail(); _refresh();
    });
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

  backdrop.appendChild(box);
  document.body.appendChild(backdrop);
}

/* ── open / close / bootstrap ── */

export async function openBoard() {
  const pane = document.getElementById('board-pane');
  if (!pane) return;
  _open = true;
  pane.classList.add('open');
  document.getElementById('tool-board-btn')?.classList.add('active');
  await _refresh();
}

export function closeBoard() {
  _open = false;
  _closeDetail();
  if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
  const pane = document.getElementById('board-pane');
  pane?.classList.remove('open');
  document.getElementById('tool-board-btn')?.classList.remove('active');
}

function _injectDom() {
  if (document.getElementById('board-pane')) return;

  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = '/static/board.css';
  document.head.appendChild(link);

  const pane = document.createElement('div');
  pane.id = 'board-pane';
  pane.setAttribute('role', 'region');
  pane.setAttribute('aria-label', 'My Tasks board');
  document.body.appendChild(pane);

  // Sidebar entry, injected above the Notes tool so index.html stays
  // untouched beyond this module's script tag.
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
    item.addEventListener('click', () => (_open ? closeBoard() : openBoard()));
    notesBtn.parentNode.insertBefore(item, notesBtn);
  }

  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape' || !_open) return;
    if (_detailCardId) { _closeDetail(); return; }
    closeBoard();
  });
}

function _boot() {
  _injectDom();
  // Board-as-home: open on load unless the user turned it off.
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
