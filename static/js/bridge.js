// static/js/bridge.js
//
// Captures — the even-odysseus recorded-sessions browser inside Odysseus.
//
// Recordings ONLY (operator direction 2026-08-21 / ADR-0015): the capture-
// review inbox that used to share this pane now lives on the My Tasks board
// (board.js consumes this mod's /api/bridge/review* proxies for it). This
// window is the archive: session list → detail with record + transcript +
// audio, plus a "tasks from this session" cross-link into the board.
//
// Stock floating-window shell: a `.modal` registered with modalManager
// (minimize-to-dock, z-order) and made draggable/resizable via
// makeWindowDraggable — the same recipe as every built-in tool window.
//
// All data is proxied live from the brain service via /api/bridge/* —
// nothing is copied into Odysseus. Fully self-contained: injects its own
// stylesheet and sidebar entry — index.html's only hook is this script tag.
// If /api/bridge/status reports unconfigured, nothing is injected at all.

import { showToast } from './ui.js';
import * as Modals from './modalManager.js';
import { makeWindowDraggable } from './windowDrag.js';

const API = window.location.origin;
const MODAL_ID = 'bridge-modal';

const MIC_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a4 4 0 0 0-4 4v6a4 4 0 0 0 8 0V6a4 4 0 0 0-4-4z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="22"/></svg>';

let _open = false;
let _sessions = [];
let _detailId = null;

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

/* ── sessions ── */

function _renderSessions() {
  const pane = document.getElementById('bridge-pane');
  if (!pane) return;
  const head = pane.querySelector('.bridge-col-head');
  const body = pane.querySelector('.bridge-sessions-body');
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
    <div class="bridge-session-tasks"></div>
    <div class="bridge-record">${_md(d.record) || '<p class="bridge-empty">No record.</p>'}</div>
    <details class="bridge-transcript"><summary>Transcript</summary><pre>${_esc(d.transcript || '')}</pre></details>`;
  wrap.querySelector('.bridge-back').addEventListener('click', () => {
    _detailId = null;
    _renderSessions();
  });
  body.innerHTML = '';
  body.appendChild(wrap);
  _renderSessionTasks(wrap.querySelector('.bridge-session-tasks'), d.id);
}

// Reverse link (ADR-0015 provenance both ways): tasks the pipeline created
// from THIS recording, read best-effort from the board mod. Absent board
// mod / board error → the block just stays empty.
async function _renderSessionTasks(el, sessionId) {
  if (!el) return;
  let tasks = [];
  try {
    const data = await _api('GET', '/api/board/tasks');
    tasks = (data.tasks || []).filter(t => t.session_id === sessionId);
  } catch { return; }
  if (!tasks.length) return;
  el.innerHTML = `<div class="bridge-stasks-label">Tasks from this session</div>`;
  for (const t of tasks) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'bridge-stask';
    row.innerHTML = `
      <span class="bridge-stask-title">${_esc(t.title)}</span>
      <span class="bridge-stask-status">${_esc((t.status || '').replace('_', ' '))}</span>`;
    row.addEventListener('click', () => {
      window.odysseusBoard?.open?.();
    });
    el.appendChild(row);
  }
}

async function _refresh() {
  try {
    await _loadSessions();
  } catch (e) {
    showToast(`captures: ${e.message}`);
  }
  _renderSessions();
}

/* ── open / close (stock floating-window lifecycle) ── */

export async function openCaptures() {
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
    <div class="modal-content bridge-modal-content">
      <div class="modal-header">
        <h4>${MIC_ICON}<span style="margin-left:6px">Captures</span>
          <span class="bridge-sub" style="margin-left:10px;text-transform:none;letter-spacing:0">recorded sessions</span></h4>
        <span style="flex:1"></span>
        <button class="close-btn" title="Close">✖</button>
      </div>
      <div class="modal-body bridge-modal-body">
        <div id="bridge-pane" role="region" aria-label="Captures">
          <div class="bridge-col-head"><span>Sessions</span></div>
          <div class="bridge-sessions-body"></div>
        </div>
      </div>
    </div>`;
  document.body.appendChild(modal);

  Modals.register(MODAL_ID, {
    sidebarBtnId: 'tool-bridge-btn',
    label: 'Captures',
    icon: MIC_ICON,
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
  document.getElementById('tool-bridge-btn')?.classList.add('active');
  await _refresh();
}

function _teardown() {
  _open = false;
  _detailId = null;
  document.getElementById(MODAL_ID)?.remove();
  document.getElementById('tool-bridge-btn')?.classList.remove('active');
}

export function closeCaptures() {
  if (Modals.isRegistered(MODAL_ID)) Modals.close(MODAL_ID);
  else _teardown();
}

// Deep link used by the board's "source recording ↗": open the window
// directly onto one session's detail.
export async function openSession(sessionId) {
  await openCaptures();
  _detailId = sessionId;
  _renderSessions();
}

/* ── bootstrap ── */

function _injectDom() {
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = '/static/bridge.css';
  document.head.appendChild(link);

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
      <span class="grow">Captures</span>`;
    // Stock sidebar semantics: closed → open, minimized → restore, open → minimize.
    item.addEventListener('click', () => {
      const modal = document.getElementById(MODAL_ID);
      if (!modal) { openCaptures(); return; }
      if (Modals.isMinimized(MODAL_ID) || modal.classList.contains('hidden')) {
        openCaptures();  // restores, or recovers a gesture-hidden sheet
      } else {
        Modals.minimize(MODAL_ID);
      }
    });
    anchor.parentNode.insertBefore(item, anchor);
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
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _boot);
} else {
  _boot();
}

window.odysseusCaptures = { open: openCaptures, close: closeCaptures, openSession };
