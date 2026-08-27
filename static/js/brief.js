// static/js/brief.js
//
// Morning Brief — the self-model loop's daily surface inside Odysseus.
//
// The nightly agents (even-odysseus ADR-0013/0014) write a report, inferred
// insights, and ≤5 morning questions. This window is where the operator
// actually MEETS them: a glanceable masthead, questions answered in place,
// insights judged Confirm / Dismiss ("judge, don't obey" — nothing inferred
// acts without confirmation), yesterday's narrative, and a link into Today
// on the board. Everything is proxied via /api/brief/* — the brain token
// never reaches the page.
//
// No timers: fetches happen on open and after actions only, so these paths
// need no interactive_gate passive entry. Once per day, on the first app
// load with an unseen brief, the window auto-opens (localStorage toggle in
// the header turns that off) and the sidebar dot marks it until seen.
//
// Stock floating-window shell: same recipe as board.js / bridge.js
// (modalManager + windowDrag + the mobile-sheet recovery re-route).

import { showToast } from './ui.js';
import * as Modals from './modalManager.js';
import { makeWindowDraggable } from './windowDrag.js';

const API = window.location.origin;
const MODAL_ID = 'brief-modal';
const AUTO_KEY = 'brief-auto-open';         // 'off' disables the morning auto-open
const SEEN_KEY = 'brief-seen-day';          // last date the brief was opened

const SUN_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>';

let _open = false;
let _data = null;

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

/* ── helpers ── */

function _esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function _today() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function _mastheadDate() {
  return new Date().toLocaleDateString(undefined,
    { weekday: 'long', month: 'long', day: 'numeric' });
}

function _markSeen() {
  try { localStorage.setItem(SEEN_KEY, _today()); } catch { /* noop */ }
  document.getElementById('brief-notif-dot')?.style.setProperty('display', 'none');
}

/* ── sections ── */

function _budgetMeter(pending, budget) {
  let ticks = '';
  for (let i = 0; i < budget; i++) {
    ticks += `<span class="brief-tick${i < pending ? ' on' : ''}"></span>`;
  }
  return `<span class="brief-meter" title="${pending} of ${budget} morning questions pending">${ticks}<span class="brief-meter-n">${pending}/${budget}</span></span>`;
}

function _renderQuestions(el) {
  const qs = _data.questions || [];
  const head = `<div class="brief-sec-head"><span>Questions</span>${_budgetMeter(qs.length, _data.question_budget || 5)}</div>`;
  if (!qs.length) {
    el.innerHTML = `${head}<div class="brief-empty">Nothing to answer — the budget refills tonight.</div>`;
    return;
  }
  el.innerHTML = head;
  for (const q of qs) {
    const card = document.createElement('div');
    card.className = 'brief-q';
    card.innerHTML = `
      <div class="brief-q-thread">${_esc(q.thread_title || q.thread_topic || '')}${q.kind === 'promotion' ? ' <span class="brief-chip">promotion</span>' : ''}</div>
      <div class="brief-q-text">${_esc(q.text)}</div>
      <div class="brief-q-answer">
        <textarea rows="1" placeholder="Answer…" aria-label="Answer"></textarea>
        <div class="brief-q-actions">
          <button type="button" class="brief-btn brief-btn-primary" data-act="answer">Answer</button>
          <button type="button" class="brief-btn" data-act="dontknow">I don't know yet</button>
          <button type="button" class="brief-btn brief-btn-quiet" data-act="skip">Skip</button>
        </div>
      </div>`;
    const ta = card.querySelector('textarea');
    ta.addEventListener('input', () => {
      ta.style.height = 'auto';
      ta.style.height = `${ta.scrollHeight}px`;
    });
    ta.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        card.querySelector('[data-act="answer"]').click();
      }
    });
    card.querySelectorAll('.brief-btn').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const act = btn.dataset.act;
        let body;
        if (act === 'answer') {
          const text = ta.value.trim();
          if (!text) { ta.focus(); return; }
          body = { id: q.id, answer: text };
        } else if (act === 'dontknow') {
          body = { id: q.id, answer: "I don't know yet" };
        } else {
          body = { id: q.id, disposition: 'dismiss' };
        }
        card.querySelectorAll('.brief-btn').forEach(b => (b.disabled = true));
        try {
          const res = await _api('POST', '/api/brief/answer', body);
          card.classList.add('brief-done');
          if (res.promoted) showToast('Promotion staged for review on the board');
          if (res.insight_confirmed) showToast('Linked insight confirmed');
          setTimeout(() => _refresh(), 350);
        } catch (e) {
          card.querySelectorAll('.brief-btn').forEach(b => (b.disabled = false));
          showToast(`brief: ${e.message}`);
        }
      });
    });
    el.appendChild(card);
  }
}

function _renderInsights(el) {
  const ins = _data.insights || [];
  const head = `<div class="brief-sec-head"><span>Insights</span><span class="brief-sub">judge, don't obey</span></div>`;
  if (!ins.length) {
    el.innerHTML = `${head}<div class="brief-empty">No unjudged insights.</div>`;
    return;
  }
  el.innerHTML = head;
  for (const i of ins) {
    const card = document.createElement('div');
    card.className = 'brief-insight';
    const conf = typeof i.confidence === 'number' && i.confidence > 0
      ? `<span class="brief-conf">${Math.round(i.confidence * 100)}%</span>` : '';
    card.innerHTML = `
      <div class="brief-i-text">${_esc(i.text)}${conf}</div>
      <div class="brief-i-actions">
        <button type="button" class="brief-btn brief-btn-primary" data-d="confirm">Confirm</button>
        <button type="button" class="brief-btn brief-btn-quiet" data-d="dismiss">Dismiss</button>
      </div>`;
    card.querySelectorAll('.brief-btn').forEach((btn) => {
      btn.addEventListener('click', async () => {
        card.querySelectorAll('.brief-btn').forEach(b => (b.disabled = true));
        try {
          await _api('POST', '/api/brief/insight',
            { id: i.id, disposition: btn.dataset.d });
          card.classList.add('brief-done');
          setTimeout(() => _refresh(), 350);
        } catch (e) {
          card.querySelectorAll('.brief-btn').forEach(b => (b.disabled = false));
          showToast(`brief: ${e.message}`);
        }
      });
    });
    el.appendChild(card);
  }
}

function _renderYesterday(el) {
  const rep = _data.report;
  const head = `<div class="brief-sec-head"><span>Yesterday</span><span class="brief-sub">${_esc(_data.day || '')}</span></div>`;
  const narrative = rep && rep.narrative ? rep.narrative.trim() : '';
  if (!narrative) {
    el.innerHTML = `${head}<div class="brief-empty">No report for ${_esc(_data.day || 'that day')} yet — the nightly run writes it at 02:30.</div>`;
    return;
  }
  const paras = narrative.split(/\n{2,}|\n/).filter(Boolean)
    .map(p => `<p>${_esc(p)}</p>`).join('');
  el.innerHTML = `${head}<div class="brief-narrative">${paras}</div>`;
}

// Today: soft dependency on the board mod (absent/erroring board → hidden).
async function _renderToday(el) {
  let tasks = [];
  try {
    const data = await _api('GET', '/api/board/tasks');
    tasks = data.tasks || [];
  } catch { el.innerHTML = ''; return; }
  const today = _today();
  const todays = tasks.filter(t => t.planned_date === today &&
    t.status !== 'done' && t.status !== 'dismissed');
  const reviewN = _data.review_pending || 0;
  el.innerHTML = `
    <div class="brief-sec-head"><span>Today</span>
      <span class="brief-sub">${todays.length} planned${reviewN ? ` · ${reviewN} captures to review` : ''}</span></div>
    ${todays.slice(0, 8).map(t => `
      <div class="brief-t-row">
        <span class="brief-t-title">${_esc(t.title)}</span>
        ${t.channel ? `<span class="brief-t-chan">#${_esc(t.channel)}</span>` : ''}
      </div>`).join('') || '<div class="brief-empty">Nothing planned yet.</div>'}
    <button type="button" class="brief-btn brief-open-board">Open the board ›</button>`;
  el.querySelector('.brief-open-board')?.addEventListener('click', () => {
    window.odysseusBoard?.open?.();
  });
}

/* ── refresh / render ── */

async function _refresh() {
  const pane = document.getElementById('brief-pane');
  if (!pane) return;
  try {
    _data = await _api('GET', '/api/brief/brief');
  } catch (e) {
    pane.innerHTML = `<div class="brief-empty">Brief unavailable — ${_esc(e.message)}</div>`;
    return;
  }
  const firstPaint = !pane.querySelector('.brief-masthead');
  pane.innerHTML = `
    <div class="brief-masthead">
      <div class="brief-date">${_esc(_mastheadDate())}</div>
      <div class="brief-counts">
        <span>${(_data.questions || []).length} questions</span>
        <span>${(_data.insights || []).length} insights</span>
        <span>${_data.review_pending || 0} to review</span>
      </div>
    </div>
    <div class="brief-sec" id="brief-sec-questions"></div>
    <div class="brief-sec" id="brief-sec-insights"></div>
    <div class="brief-sec" id="brief-sec-yesterday"></div>
    <div class="brief-sec" id="brief-sec-today"></div>`;
  _renderQuestions(pane.querySelector('#brief-sec-questions'));
  _renderInsights(pane.querySelector('#brief-sec-insights'));
  _renderYesterday(pane.querySelector('#brief-sec-yesterday'));
  _renderToday(pane.querySelector('#brief-sec-today'));
  if (firstPaint) {
    // The one orchestrated moment: masthead + sections stagger in once per
    // open. Interaction feedback everywhere else is immediate.
    pane.classList.add('brief-enter');
  }
}

/* ── open / close (stock floating-window lifecycle) ── */

export async function openBrief() {
  const existing = document.getElementById(MODAL_ID);
  if (existing) {
    if (Modals.isMinimized(MODAL_ID)) { Modals.restore(MODAL_ID); return; }
    if (existing.classList.contains('hidden')) {
      Modals.minimize(MODAL_ID);
      Modals.restore(MODAL_ID);
    }
    return;
  }
  const autoOff = localStorage.getItem(AUTO_KEY) === 'off';
  const modal = document.createElement('div');
  modal.className = 'modal';
  modal.id = MODAL_ID;
  modal.innerHTML = `
    <div class="modal-content brief-modal-content">
      <div class="modal-header">
        <h4>${SUN_ICON}<span style="margin-left:6px">Morning Brief</span></h4>
        <span style="flex:1"></span>
        <button class="brief-auto-toggle" type="button"
          title="Open automatically on the first visit each morning"
          aria-pressed="${!autoOff}">auto-open ${autoOff ? 'off' : 'on'}</button>
        <button class="close-btn" title="Close">✖</button>
      </div>
      <div class="modal-body brief-modal-body">
        <div id="brief-pane" role="region" aria-label="Morning Brief"></div>
      </div>
    </div>`;
  document.body.appendChild(modal);

  Modals.register(MODAL_ID, {
    sidebarBtnId: 'tool-brief-btn',
    label: 'Morning Brief',
    icon: SUN_ICON,
    restoreFn: () => { _refresh(); },
    closeFn: _teardown,
  });
  Modals.injectMinimizeButton(modal, MODAL_ID);
  modal.querySelector('.close-btn').addEventListener('click', (e) => {
    e.stopPropagation();
    Modals.close(MODAL_ID);
  });
  modal.querySelector('.brief-auto-toggle').addEventListener('click', (e) => {
    const off = localStorage.getItem(AUTO_KEY) === 'off';
    try { localStorage.setItem(AUTO_KEY, off ? 'on' : 'off'); } catch { /* noop */ }
    e.target.textContent = `auto-open ${off ? 'on' : 'off'}`;
    e.target.setAttribute('aria-pressed', String(off));
  });
  {
    const content = modal.querySelector('.modal-content');
    const header = modal.querySelector('.modal-header');
    if (content && header) makeWindowDraggable(modal, { content, header });
  }

  _open = true;
  document.getElementById('tool-brief-btn')?.classList.add('active');
  _markSeen();
  await _refresh();
}

function _teardown() {
  _open = false;
  _data = null;
  document.getElementById(MODAL_ID)?.remove();
  document.getElementById('tool-brief-btn')?.classList.remove('active');
}

export function closeBrief() {
  if (Modals.isRegistered(MODAL_ID)) Modals.close(MODAL_ID);
  else _teardown();
}

/* ── bootstrap ── */

function _injectDom() {
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = '/static/brief.css';
  document.head.appendChild(link);

  const anchor = document.getElementById('tool-bridge-btn') ||
    document.getElementById('tool-board-btn') ||
    document.getElementById('tool-notes-btn');
  if (anchor && !document.getElementById('tool-brief-btn')) {
    const item = document.createElement('div');
    item.className = 'list-item';
    item.id = 'tool-brief-btn';
    item.innerHTML = `
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
        style="flex-shrink:0;opacity:0.5;">
        <circle cx="12" cy="12" r="4"/>
        <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>
      </svg>
      <span class="grow">Morning Brief</span>
      <span id="brief-notif-dot" class="sidebar-notif-dot" style="display:none"></span>`;
    item.addEventListener('click', () => {
      const modal = document.getElementById(MODAL_ID);
      if (!modal) { openBrief(); return; }
      if (Modals.isMinimized(MODAL_ID) || modal.classList.contains('hidden')) {
        openBrief();
      } else {
        Modals.minimize(MODAL_ID);
      }
    });
    anchor.parentNode.insertBefore(item, anchor);
  }

  window.addEventListener('modal-dismissed', (e) => {
    if (e.detail?.id !== MODAL_ID) return;
    if (!document.getElementById(MODAL_ID)) return;
    Modals.minimize(MODAL_ID);
  });

  document.addEventListener('keydown', (e) => {
    if (!_open || Modals.isMinimized(MODAL_ID)) return;
    if (e.key === 'Escape') closeBrief();
  });
}

async function _boot() {
  let status;
  try {
    status = await _api('GET', '/api/brief/status');
  } catch { return; }
  if (!status.configured) return;
  _injectDom();

  const seenToday = localStorage.getItem(SEEN_KEY) === _today();
  if (!seenToday) {
    document.getElementById('brief-notif-dot')?.style.setProperty('display', '');
    if (localStorage.getItem(AUTO_KEY) !== 'off' && status.ok) {
      // The morning moment: first visit of the day opens the brief once.
      setTimeout(() => { if (!document.getElementById(MODAL_ID)) openBrief(); }, 900);
    }
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _boot);
} else {
  _boot();
}

window.odysseusBrief = { open: openBrief, close: closeBrief };
