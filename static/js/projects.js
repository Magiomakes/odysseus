// static/js/projects.js
//
// Projects — the even-odysseus world-model container view inside the
// Workspace (DESIGN-projects-surface / DESIGN-workspace step 2).
//
// Not a window: board.js owns the Workspace shell (the board modal) and
// calls `window.odysseusProjects.mount(container, opts)` every time the
// Projects tab comes front, passing the `#projects-pane` element.
// Idempotency is ours: build the DOM once, refresh data on every mount.
// `opts.project` (entity id) deep-links straight onto that project's page
// — the hook the board's project chip uses via
// `window.odysseusBoard.switchWorkspaceView('projects', {project: id})`.
//
// Drill-in, list → page (the Captures window's recipe): the list of
// project entities from the brain's world model, a page per project with
// its honest state (blank facets render as visible gaps — that is the
// point, not a polished brochure) and its document folder as a drop
// target. Drops upload + RAG-index in-process via /api/projects/*, so a
// dropped file is searchable in chat immediately. Read-only otherwise —
// no facet writes from here (v1 decision).

import { showToast } from './ui.js';

const API = window.location.origin;

// Statuses that land a project in the collapsed Archived group.
const ARCHIVED_STATUSES = new Set(['archived', 'done', 'completed', 'closed', 'retired']);
const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;   // mirrors the server's per-file cap

let _root = null;        // the #projects-pane element we mounted into
let _projects = [];
let _detailId = null;    // entity id while a project page is front
let _detail = null;      // last-loaded detail payload (files re-render w/o refetch)
let _uploading = false;
let _keysWired = false;

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
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

/* ── format helpers ── */

function _esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function _date(iso) {
  return iso ? String(iso).slice(0, 10) : '';
}

// Relative age from epoch seconds — file rows read "2h ago", not timestamps.
function _rel(epoch) {
  if (!epoch) return '';
  const s = Math.max(0, Date.now() / 1000 - epoch);
  if (s < 90) return 'just now';
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.round(h / 24);
  if (d < 14) return `${d}d ago`;
  return new Date(epoch * 1000).toISOString().slice(0, 10);
}

function _size(bytes) {
  if (bytes == null) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function _isArchived(p) {
  return ARCHIVED_STATUSES.has(String(p.status || '').toLowerCase());
}

/* ── mount contract ── */

function mount(container, opts = {}) {
  if (container && (_root !== container || !container.querySelector('.prj-body'))) {
    _root = container;
    _root.classList.add('projects-live');   // lifts board.css's stub centering
    _root.innerHTML = `<div class="prj-body"></div>`;
  }
  if (!_root) return;
  if (opts.project != null) {
    const id = Number(opts.project);
    if (Number.isFinite(id)) { _detailId = id; _detail = null; }
  }
  _wireKeys();
  _refresh();
}

// Escape backs out of a project page instead of letting board.js's own
// bubble-phase handler close the whole Workspace window. Capture phase on
// document runs first; stopPropagation there also suppresses the bubble
// listeners. Only intercepts while the Projects view is front AND a page
// is open — every other Escape falls through to the board untouched.
function _wireKeys() {
  if (_keysWired) return;
  _keysWired = true;
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (!_root || !_root.isConnected || _root.classList.contains('board-view-off')) return;
    if (_detailId == null) return;
    e.stopPropagation();
    _detailId = null;
    _detail = null;
    _refresh();
  }, true);
}

/* ── refresh / error states ── */

async function _refresh() {
  const body = _root?.querySelector('.prj-body');
  if (!body) return;
  if (_detailId != null) { await _renderDetail(body); return; }
  try {
    const data = await _api('GET', '/api/projects');
    _projects = data.projects || [];
  } catch (e) {
    _renderProblem(body, e);
    return;
  }
  _renderList(body);
}

// 503 = bridge unconfigured (quiet, no retry — config won't appear by
// itself); 502/504/network = brain unreachable (quiet, retryable).
function _renderProblem(body, err) {
  const unconfigured = err.status === 503;
  body.innerHTML = `
    <div class="prj-quiet">
      <div>${unconfigured ? 'Projects is not configured.'
        : 'The brain service isn’t reachable right now.'}</div>
      ${unconfigured ? '' : '<button type="button" class="prj-retry">Retry</button>'}
    </div>`;
  body.querySelector('.prj-retry')?.addEventListener('click', () => _refresh());
}

/* ── list view ── */

function _rowMeta(p) {
  const bits = [];
  if (p.deadline) bits.push(`due ${_date(p.deadline)}`);
  const n = p.file_count || 0;
  bits.push(n === 1 ? '1 file' : `${n} files`);
  return bits.join(' · ');
}

// One-line status: the status field, falling back to deadline / last-
// activity phrasing when the entity carries no status of its own.
function _statusLine(p) {
  if (p.status) return p.status;
  if (p.deadline) return `due ${_date(p.deadline)}`;
  if (p.updated_at) return `updated ${_date(p.updated_at)}`;
  return '';
}

function _projectRow(p) {
  const row = document.createElement('button');
  row.type = 'button';
  row.className = 'prj-row';
  const kind = p.kind === 'project-template'
    ? '<span class="prj-kind">template</span>' : '';
  row.innerHTML = `
    <span class="prj-rname">${_esc(p.name)}${kind}</span>
    <span class="prj-rstatus">${_esc(_statusLine(p))}</span>
    <span class="prj-rmeta">${_esc(_rowMeta(p))}</span>`;
  row.addEventListener('click', () => {
    _detailId = p.id;
    _detail = null;
    _refresh();
  });
  return row;
}

function _renderList(body) {
  const active = _projects.filter(p => !_isArchived(p));
  const archived = _projects.filter(_isArchived);
  body.innerHTML = `
    <div class="prj-col-head"><span>Projects</span>
      <span class="prj-sub">${_projects.length}</span></div>
    <div class="prj-list"></div>`;
  const list = body.querySelector('.prj-list');
  if (!_projects.length) {
    list.innerHTML = `<div class="prj-empty">No projects yet — the nightly
      brain creates them as it learns what you're working on.</div>`;
    return;
  }
  for (const p of active) list.appendChild(_projectRow(p));
  if (archived.length) {
    const det = document.createElement('details');
    det.className = 'prj-archived';
    det.innerHTML = `<summary>Archived
      <span class="prj-sub">${archived.length}</span></summary>`;
    for (const p of archived) det.appendChild(_projectRow(p));
    list.appendChild(det);
  }
}

/* ── project page — state block ── */

// The entity's honest state in plain speech. Known facts render as prose
// lines; every blank facet renders as a visible gap ("? budget — nothing
// known yet") — hiding the gaps would defeat the surface's purpose.
function _stateBlock(d) {
  const facts = [];
  const gaps = [];

  if (d.summary && d.summary.trim()) {
    facts.push(`<p class="prj-summary">${_esc(d.summary)}</p>`);
  } else {
    gaps.push(['summary', 'nothing captured yet']);
  }

  if (d.deadline) facts.push(_fact('deadline', _date(d.deadline)));
  else gaps.push(['deadline', 'nothing known yet']);
  if (d.starts) facts.push(_fact('starts', _date(d.starts)));
  if (d.importance && String(d.importance).trim()) {
    facts.push(_fact('importance', d.importance));
  } else {
    gaps.push(['importance', 'nothing known yet']);
  }

  for (const f of d.facets || []) {
    const val = (f.value || '').trim();
    const who = f.delegated_to_name;
    if (val) {
      facts.push(_fact(f.name, val + (who ? ` · with ${who}` : '')));
    } else if (who) {
      gaps.push([f.name, `with ${who}, but nothing recorded yet`]);
    } else {
      gaps.push([f.name, 'nothing known yet']);
    }
  }

  for (const e of d.edges || []) {
    if (e.kind === 'delegation' && e.person_name) {
      facts.push(`<p class="prj-fact">${_esc(e.person_name)} holds
        <strong>${_esc(e.label || 'a piece of this')}</strong>.</p>`);
    }
  }

  const playbook = d.meta?.playbook;
  if (Array.isArray(playbook) && playbook.length) {
    facts.push(_fact('playbook', playbook.join(', ')));
  }

  const loops = d.open_loops || d.loops;
  if (Array.isArray(loops) && loops.length) {
    const items = loops.map(l =>
      `<li>${_esc(l.description || l.text || l.name || JSON.stringify(l))}</li>`).join('');
    facts.push(`<div class="prj-loops"><div class="prj-label">Open loops</div>
      <ul>${items}</ul></div>`);
  }

  const gapHtml = gaps.map(([name, note]) => `
    <p class="prj-gap"><span class="prj-gap-q">?</span>
      <strong>${_esc(name)}</strong> — ${_esc(note)}</p>`).join('');

  return `<div class="prj-state">${facts.join('')}${gapHtml}</div>`;
}

function _fact(name, value) {
  return `<p class="prj-fact"><strong>${_esc(name)}</strong> — ${_esc(value)}</p>`;
}

/* ── project page — files section ── */

function _fileRow(d, f) {
  const row = document.createElement('div');
  row.className = 'prj-file';
  row.innerHTML = `
    <span class="prj-fname">${_esc(f.name)}</span>
    <span class="prj-fmeta">${_esc(_size(f.size))} · ${_esc(_rel(f.mtime))}</span>
    <button type="button" class="prj-fdel" title="Delete (also removes it from chat search)">✕</button>`;
  row.querySelector('.prj-fdel').addEventListener('click', () => _deleteFile(d, f.name));
  return row;
}

function _renderFiles(d) {
  const sec = _root?.querySelector('.prj-files');
  if (!sec) return;
  const files = d.files || [];
  const listEl = sec.querySelector('.prj-file-list');
  const zone = sec.querySelector('.prj-drop');
  listEl.innerHTML = '';
  for (const f of files) listEl.appendChild(_fileRow(d, f));
  sec.querySelector('.prj-files-count').textContent = files.length || '';
  // Empty folder → the drop zone IS the section: tall and inviting.
  zone.classList.toggle('prj-drop-empty', !files.length);
  zone.querySelector('.prj-drop-text').textContent = files.length
    ? 'Drop files here to add — searchable in chat immediately'
    : 'Drop files here — they’re searchable in chat immediately';
}

function _setUploadStatus(html) {
  const el = _root?.querySelector('.prj-upload-status');
  if (el) el.innerHTML = html || '';
}

async function _upload(d, fileList) {
  if (_uploading) { showToast('Still uploading — one moment'); return; }
  const files = [...fileList];
  if (!files.length) return;
  const oversize = files.filter(f => f.size > MAX_UPLOAD_BYTES);
  const sendable = files.filter(f => f.size <= MAX_UPLOAD_BYTES);
  const lines = oversize.map(f =>
    `<div class="prj-up-bad">${_esc(f.name)} — too large (over 25 MB)</div>`);
  if (!sendable.length) { _setUploadStatus(lines.join('')); return; }

  _uploading = true;
  _root?.querySelector('.prj-browse')?.setAttribute('disabled', '');
  _setUploadStatus(`<div class="prj-up-busy">Uploading ${sendable.length}
    file${sendable.length === 1 ? '' : 's'}…</div>`);
  try {
    const fd = new FormData();
    for (const f of sendable) fd.append('files', f);
    const res = await fetch(`${API}/api/projects/${d.id}/files`, {
      method: 'POST', body: fd,
    });
    if (!res.ok) {
      let detail = `${res.status}`;
      try { detail = (await res.json()).detail || detail; } catch { /* noop */ }
      throw new Error(detail);
    }
    const out = await res.json();
    // Server may have renamed on collision — trust its names + listing.
    for (const u of out.uploaded || []) {
      lines.push(u.indexed_chunks > 0
        ? `<div class="prj-up-ok">${_esc(u.name)} — indexed (${u.indexed_chunks} chunk${u.indexed_chunks === 1 ? '' : 's'})</div>`
        : `<div class="prj-up-warn">${_esc(u.name)} — saved, but nothing indexable</div>`);
    }
    const failed = out.failed_count || 0;
    if (failed) lines.push(`<div class="prj-up-bad">${failed} failed — see server log</div>`);
    d.files = out.files || d.files;
    d.file_count = (d.files || []).length;
    _renderFiles(d);
    showToast(out.uploaded?.length
      ? `Added ${out.uploaded.length} file${out.uploaded.length === 1 ? '' : 's'} to ${d.name}`
      : 'Nothing uploaded');
  } catch (e) {
    lines.push(`<div class="prj-up-bad">Upload failed (${_esc(e.message)})</div>`);
    showToast(`Upload failed: ${e.message}`);
  } finally {
    _uploading = false;
    _root?.querySelector('.prj-browse')?.removeAttribute('disabled');
    _setUploadStatus(lines.join(''));
  }
}

async function _deleteFile(d, name) {
  if (!window.confirm(`Delete "${name}" from this project?\nIt is also removed from chat search.`)) return;
  try {
    const out = await _api('DELETE',
      `/api/projects/${d.id}/files?name=${encodeURIComponent(name)}`);
    d.files = out.files || (d.files || []).filter(f => f.name !== name);
    d.file_count = (d.files || []).length;
    _renderFiles(d);
    _setUploadStatus('');
    showToast(`Deleted ${name} (${out.removed_chunks || 0} chunks deindexed)`);
  } catch (e) {
    showToast(`Delete failed: ${e.message}`);
  }
}

// Drag-drop: wired ONLY on the files section inside this pane, and only
// reacting to external file drags (dataTransfer.types includes 'Files') —
// the board's own card drags carry text types and pass through untouched.
// Depth counter because dragenter/leave fire per child element.
function _wireDrop(sec, d) {
  const hasFiles = (e) =>
    e.dataTransfer && [...(e.dataTransfer.types || [])].includes('Files');
  let depth = 0;
  sec.addEventListener('dragenter', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth++;
    sec.classList.add('drop-target');
  });
  sec.addEventListener('dragover', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  });
  sec.addEventListener('dragleave', (e) => {
    if (!hasFiles(e)) return;
    if (--depth <= 0) { depth = 0; sec.classList.remove('drop-target'); }
  });
  sec.addEventListener('drop', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth = 0;
    sec.classList.remove('drop-target');
    if (e.dataTransfer.files?.length) _upload(d, e.dataTransfer.files);
  });
}

/* ── project page ── */

async function _renderDetail(body) {
  if (!_detail || _detail.id !== _detailId) {
    body.innerHTML = `<div class="prj-empty">Loading…</div>`;
    try {
      _detail = await _api('GET', `/api/projects/${_detailId}`);
    } catch (e) {
      if (e.status === 404) {
        showToast('No project with that id');
        _detailId = null;
        _refresh();
        return;
      }
      _renderProblem(body, e);
      return;
    }
  }
  const d = _detail;
  const kindBits = [];
  if (d.kind === 'project-template') kindBits.push('template');
  if (d.status) kindBits.push(d.status);
  if (d.last_activity) kindBits.push(`last activity ${_date(d.last_activity)}`);
  body.innerHTML = `
    <div class="prj-page">
      <button type="button" class="prj-back">‹ projects</button>
      <div class="prj-title">${_esc(d.name)}</div>
      <div class="prj-meta">${_esc(kindBits.join(' · '))}</div>
      ${_stateBlock(d)}
      <div class="prj-files">
        <div class="prj-label">Files <span class="prj-sub prj-files-count"></span>
          <span class="prj-label-spacer"></span>
          <button type="button" class="prj-browse">browse…</button>
          <input type="file" class="prj-file-input" multiple hidden>
        </div>
        <div class="prj-file-list"></div>
        <div class="prj-drop"><span class="prj-drop-text"></span></div>
        <div class="prj-upload-status"></div>
      </div>
    </div>`;
  body.querySelector('.prj-back').addEventListener('click', () => {
    _detailId = null;
    _detail = null;
    _refresh();
  });
  const input = body.querySelector('.prj-file-input');
  body.querySelector('.prj-browse').addEventListener('click', () => input.click());
  input.addEventListener('change', () => {
    if (input.files?.length) _upload(d, input.files);
    input.value = '';
  });
  _wireDrop(body.querySelector('.prj-files'), d);
  _renderFiles(d);
}

/* ── bootstrap ── */

// Stylesheet rides in from this module (established mod practice: index
// .html's only hook is the script tag). The mount contract is registered
// unconditionally — board.js probes `window.odysseusProjects?.mount`.
(function _boot() {
  if (!document.getElementById('projects-css')) {
    const link = document.createElement('link');
    link.id = 'projects-css';
    link.rel = 'stylesheet';
    link.href = '/static/projects.css';
    document.head.appendChild(link);
  }
})();

window.odysseusProjects = { mount };
