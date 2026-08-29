# Local Mods & How to Update Odysseus

This is a **fork** of upstream Odysseus (`origin` = `pewdiepie-archdaemon/odysseus`).
We carry a handful of local changes on top of upstream. They are kept **modular** so
that when upstream releases new code we can re-seat ("socket") each change onto the
new base independently, with minimal conflicts.

> **Read this from the `integration` branch** (your normal working/running branch).
> It deliberately does **not** live on `main` — see "The pristine-main invariant" below.

---

## The model

```
main  ── byte-identical mirror of origin/main (UPSTREAM). Never commit here.
│
├─ fix/task-owner-attribution        one atomic commit   (functional mod)
├─ fix/task-agent-wall-clock-timeout  one atomic commit  (functional mod)
├─ fix/app-bind-host                 one atomic commit   (functional mod)
├─ feat/task-grounding-url-fetch     one atomic commit   (functional mod)
└─ meta/local-mods-guide             one atomic commit   (this file)

integration ── main + every branch above, merged together.
               This is what you RUN. It is rebuilt on every update.
               Never commit original work here — it gets thrown away and remade.
```

**One mod = one branch = one atomic, self-documenting commit off pristine `main`.**
That is the whole trick. Each branch touches as few files as possible, so a rebase
onto new upstream is either clean or a tiny obvious conflict in one file.

### The mods we carry

| Branch | What it does | Files touched |
|---|---|---|
| `fix/task-owner-attribution` | Bearer `ody_` tokens attribute task CRUD + notifications to the real minting owner via `effective_user()`, not the sandboxed `"api"` pseudo-user (otherwise token-created tasks are invisible to the named-login UI). Also exposes `owner` in the task API representation so a bearer-token client can confirm attribution (it was correct in the DB but absent from the JSON, so clients read it as `owner=None`). Upstream PR #4822 — candidate to upstream then drop. *(Amended 2026-08-27b:)* bearer tokens now need the `chat` scope AND a minting owner (403 otherwise, codex_routes idiom), and a token is never admin for the shell-executing actions (`run_local`/`run_script`/`ssh_command`) — owner attribution had silently re-opened them to admin-minted tokens. | `routes/task_routes.py`, `tests/test_task_token_gate.py` (new) |
| `fix/app-bind-host` | macOS app launcher honors `APP_BIND` from `.env` (e.g. `0.0.0.0` for LAN/Tailscale); defaults to `127.0.0.1`. | `build-macos-app.sh` |
| `fix/task-agent-wall-clock-timeout` | Hard wall-clock cap (`task_agent_timeout_seconds`, default 900s) on a scheduled task's agent loop. Task execution holds the single `Semaphore(1)` slot, so one wedged/slow stream would otherwise park it forever and stall the whole queue ("no task fired in weeks"). On expiry the stream is cancelled, partial output kept, slot released. Upstream PR #4827 — candidate to upstream then drop. | `src/task_scheduler.py`, `src/settings.py` |
| `feat/task-grounding-url-fetch` | Scheduled task agents can pull grounding from a URL named in the prompt (even-odysseus "pull, not push" / ADR-0007). Research tasks (no tool loop) pre-fetch any **allowlisted** URL and prepend it as grounding context; llm tasks get `web_fetch` promoted to always-available. Gated by env `WEB_FETCH_ALLOWLIST` (comma-separated hostnames; empty = off) so an autonomous task can't be steered into an SSRF fetch. *(Amended 2026-07-02: redirect hops re-pass the allowlist — `follow_redirects=False` + manual hop loop.)* **Requires `WEB_FETCH_ALLOWLIST=<even-odysseus host>` in `.env`.** | `src/task_scheduler.py`, `src/tool_index.py`, `tests/test_task_grounding_fetch.py` |
| `fix/web-fetch-private-ip-guard` | SSRF guard on the generic `web_fetch` tool: refuses targets that are, or resolve to, loopback/private/link-local space unless the hostname is on `WEB_FETCH_ALLOWLIST` (same env + semantics as the grounding pre-fetch, so the allowlist constrains everything its name implies). Task-agent prompts are untrusted input (voice transcripts, API-created tasks) — without this, an injected instruction could read any service on the machine/LAN. Added 2026-07-02 (even-odysseus VISION.md Phase 0). *(Amended 2026-08-27b:)* also blocks RFC 6598 100.64.0.0/10 (CGNAT — the Tailscale range; CPython's is_private misses it), so tailnet peers aren't fetchable; allowlist still exempts. | `src/net_guard.py` (new), `src/agent_tools/web_tools.py`, `tests/test_web_fetch_guard.py` |
| `fix/task-result-delivery` | Task results reach the user even when the app is closed. (1) `output_target='notification'` results were queued only in RAM and wiped by any restart — now persisted to `DATA_DIR/task_notifications.json` and restored on startup. (2) The `email_results` column existed in the schema (and defaulted on) but no code ever read it — now honored for llm/research tasks via the existing SMTP delivery path (housekeeping actions excluded to avoid inbox spam; skipped when output target is already email). Added 2026-07-22. Candidate to upstream. | `src/task_scheduler.py` |
| `fix/memory-mcp-owner` | Built-in memory MCP server launches with `ODYSSEUS_MCP_MEMORY_OWNER` resolved (explicit env wins; single-user installs scope to the sole `auth.json` account). The MCP stdio client *replaces* the child env, so the owner never reached the server before — against an owner-scoped store every `manage_memory` call (agent saves, even-odysseus bridge sessions) silently failed with a scope error. Added 2026-07-22. Candidate to upstream. | `src/builtin_mcp.py` |
| `feat/task-board` | **My Tasks board** — Sunsama-style personal task planner (far-left AGENTS teammate column with drop-to-handoff picker, backlog grouped by Sunsama horizons, #channels + estimates + day totals, rolling week day columns, board-as-home). *(Amended 2026-08-22, even-odysseus ADR-0015:)* stock floating-window shell (modalManager + windowDrag, not a full-screen pane); ingest v2 (optional planned_date/horizon pre-scheduled landing, session_id/context_url provenance, bucket, prompt+task_type → auto-handoff on a linked card, idempotency guards card AND handoff); PATCH result w/ result_original preserved once; capture chip + hover-dismiss; Inbox section (capture confirm gate, fed by feat/bridge-review's /api/bridge/review* — hidden when absent, soft dependency); edit-draft UI firing /api/bridge/draft-feedback; completed email-draft results IMAP-appended once to the stock Drafts folder (X-Odysseus-Kind/Ref). New `user_tasks` table + `/api/board/*` (self-contained in `routes/board_routes.py`): manual cards, webhook ingest for the even-odysseus Task Manager sink (idempotent), drag-to-agent handoff creating linked run-now scheduled tasks, pull-style result reconciliation flipping cards to `in_review`. UI injects itself (`static/js/board.js` + `static/board.css`); index.html hook is one script tag. *(Amended 2026-08-25:)* poll-flicker fix (fingerprinted repaint); capacity-based start gate for board handoffs (`wait_for_capacity`: hard block on even-odysseus ingest `/health busy`, soft budget on load/mem/chat-stream; board tasks dispatch despite UI presence, exempt from the foreground cancel + watchdog; housekeeping quiet gate bounded via `background_task_gate_timeout_seconds`); completed results discoverable (board research results → library Documents via `src/document_actions.create_result_document`, notifications carry session_id/document_id, tasks.js polls from app load with click-through toasts/browser notifications — NOTE soft dependency: notification persistence helpers live in `fix/task-result-delivery`). *(Amended 2026-08-25b:)* the five `[Board] `-literal gate sites generalized to a settings-driven predicate `_is_priority_task` (`user_priority_task_prefixes`, default board handoffs + the even-odysseus "Nightly insight agent"/"Nightly question agent") — the nightly self-model agents had kept housekeeping semantics and timed out at 2400s every night for a month. Added 2026-07-22. *(Amended 2026-08-27b, post-v1.0.3 sync:)* research result docs get a `Research: ` title prefix + run-id footer and scheduler document-results a date footer (upstream's `run_document_tidy` HARD-deletes junk-titled and fingerprint-duplicate docs); `[Board] ` notifications click through to the board; Completed-tab refresh batched + minimized-aware; model picker reads `/api/models` `items[]` (was always empty); `task_capacity_max_load_per_core` float-clamped; capacity-gate brain probe failure logs a rate-limited warning instead of silently failing open. *(Amended 2026-08-27c, world-model link:)* + nullable indexed `project` column on `user_tasks` (`IngestItem.project` optional, additive `_ensure_columns` migration, copied on ingest, returned by the card serializer; no UI chip) — the even-odysseus world-model board link, ADR-0018 brain-side. *(Amended 2026-08-27d, Workspace shell:)* the board window becomes the **Workspace** (even-odysseus `DESIGN-workspace.md` step 1) — modal header gains a Today/Projects view switcher (mod-local `.board-view-tab` echo of the stock `.lib-tab` strip); Today = the board unchanged, Projects = a stub `#projects-pane` awaiting `feat/projects-view` (`window.odysseusProjects.mount(container, opts)` mount contract; switch hook exported as `switchWorkspaceView(name, opts)` + on `window.odysseusBoard`). No new window, no new rail button, no stock-file changes. *(Amended 2026-08-27f, board chip + filter — DESIGN-projects-surface board amendment:)* cards whose task carries `project` render a small clickable chip (meta-row pill, the capture chip's solid sibling) that jumps the Workspace to that project's page via `switchWorkspaceView('projects', {project: <entity id>})` — names resolve to ids against `/api/projects` (case-insensitive, cached per board open); an unresolvable name passes a sentinel id so projects.js's own unknown-id fallback (toast + back to list) handles the miss. Header gains a project `<select>` beside the channel filter: distinct project names on current cards + "all projects", composes with the channel filter, persisted in localStorage `board-project-filter` (a stale persisted value is dropped; with no project on any card the control does not render). Chip CSS declares height/margin explicitly to beat the stock mobile `button{height:32px}` rules. | `routes/board_routes.py` (new), `static/js/board.js` (new), `static/board.css` (new), `tests/test_board_routes.py` (new), `tests/test_capacity_gate.py` (new), `tests/test_result_document.py` (new), `src/document_actions.py`, `app.py`, `static/index.html`, `static/js/tasks.js`, `src/task_scheduler.py`, `src/settings.py`, `routes/auth_routes.py`, `requirements.txt`, `src/interactive_gate.py` (board paths passive — board polling must not cancel the agent runs it watches; capacity-gate helpers) |
| `feat/bridge-review` | **Captures window** — even-odysseus bridge surface: `/api/bridge/*` proxies the brain service's review queue + sessions API server-side (config: `BRIDGE_BASE_URL` + `BRIDGE_TOKEN` in `.env`; the token never reaches the page). *(Amended 2026-08-22, ADR-0015:)* the UI is now a stock floating window showing RECORDINGS ONLY (sessions browser + audio + a best-effort 'tasks from this session' list; exposes `odysseusCaptures.openSession(id)`); the review-inbox UI moved to the board's Inbox section but the `/review*` proxies remain (the board consumes them). New `POST /api/bridge/draft-feedback` forwards an operator's email-draft edit to the brain's learn-from-edit endpoint (validated, length-bounded). Hidden entirely when unconfigured. *(Amended 2026-08-27, PLAN-captures-context Phase 3:)* “Ask AI about this session” button in the session detail — gallery discuss-photo flow (createDirectChat + record-sans-transcript attached as `<id>-record.md` + prefilled composer naming the session id so the captures MCP tools can pull more; never auto-sends). Added 2026-08-21. *(Amended 2026-08-27b:)* bearer tokens need `chat` scope + owner on every proxy but `/status` — mutating verbs ride the full-privilege `BRIDGE_TOKEN` (confused-deputy). | `routes/bridge_routes.py` (new), `static/js/bridge.js` (new), `static/bridge.css` (new), `tests/test_bridge_routes.py` (new), `app.py`, `static/index.html` |
| `feat/contact-notes` | **Contact context notes + bearer-reachable contacts surface** (`/api/contact-notes/*`) — the even-odysseus CRM loop's storage (ADR-0015). CardDAV mode edits the vCard's standard NOTE property SURGICALLY on the raw card (upstream's rebuild-the-card `_update_contact` would destroy ORG/PHOTO/foreign props — never called); no CardDAV → `DATA_DIR/contact_notes.json` sidecar. search (notes attached) / add / get-append-replace note. Auth via the `effective_user` pattern (bearer `ody_` acts as its minting owner; upstream contact routes' `require_admin` 403s tokens). Added 2026-08-22. *(Amended 2026-08-27b:)* append does ONE CardDAV fetch (was two — doubled round-trips, wider RMW race). | `routes/contacts/contact_notes_routes.py` (new), `tests/test_contact_notes_routes.py` (new), `app.py` |
| `feat/task-lifecycle` | Finished one-off tasks stop piling up in the Tasks tab: an hourly scheduler sweep archives completed `schedule='once'` tasks older than `task_archive_completed_days` (default 3; -1 disables; status flip only — runs history kept, still deletable). `GET /api/tasks` excludes archived by default (`?status=archived` to view); `POST /api/tasks/archive-finished` backs the "clear finished (N)" chip; `archived` chip in the Tasks tab. Added 2026-08-25. | `src/task_scheduler.py`, `routes/task_routes.py`, `routes/auth_routes.py`, `src/settings.py`, `static/js/tasks.js`, `tests/test_task_archive_sweep.py` (new) |
| `feat/morning-brief` | **Morning Brief window** — the self-model loop's daily surface (even-odysseus ADR-0013/0014/0016). Stock floating window: masthead (date + counts + question-budget ticks), the ≤5 morning questions answered INLINE (answer / "I don't know yet" / skip → brain `/api/self/questions/answer`), inferred insights judged Confirm/Dismiss (→ brain `/api/self/insights/resolve`, the ADR-0009 provenance gate), yesterday's report narrative, Today preview via the board mod (soft dependency, hidden when absent). `/api/brief/*` proxies the brain server-side (shares `BRIDGE_BASE_URL`/`BRIDGE_TOKEN` with feat/bridge-review; hidden entirely when unconfigured). No polling timers — no interactive_gate entry needed. First visit of a day auto-opens once (localStorage `brief-auto-open` toggle) + sidebar unseen dot. Added 2026-08-25. *(Amended 2026-08-27b:)* same token gate as feat/bridge-review. | `routes/brief_routes.py` (new), `static/js/brief.js` (new), `static/brief.css` (new), `tests/test_brief_routes.py` (new), `app.py` (STT anchor), `static/index.html` |
| `feat/captures-mcp` | **Captures MCP server** — read-only stdio MCP server (`search_captures`, `get_capture`, `list_recent_captures`, `get_day_digest`, `get_operator_profile`) over the even-odysseus brain's read API (its ADR-0017 surface), so the chat AI can search/read recorded sessions and the operator profile. Carries ONLY the scoped `BRIDGE_READ_TOKEN` (never `BRIDGE_TOKEN` — prompt-injection blast radius, even-odysseus ADR-0013); GET-only; size caps enforced server-side here. Registered via the admin MCP UI (DB row, stdio, `venv/bin/python mcp_servers/captures_server.py`), NOT `_BUILTIN_SERVERS` — zero app-code change, and non-builtin tools auto-flow to the chat agent. **Requires `BRIDGE_READ_TOKEN=<even-odysseus INGEST_READ_TOKEN>` in `.env`.** Added 2026-08-27. | `mcp_servers/captures_server.py` (new), `tests/test_captures_mcp.py` (new) |
| `feat/memory-projection` | **Memory projection route** — `PUT /api/memory/projection`: declarative, token-reachable reconcile of even-odysseus self-model content (operator-CONFIRMED insights + answered morning Q&A) into native memory, so ChatProcessor's ambient injection surfaces the facts with zero tool calls (PLAN-captures-context Phase 4; ADR-0009 §4 — Confirm licenses projection). Stock memory REST refuses bearer tokens (`require_user` 403s them; PUT/DELETE resolve to the `api` pseudo-user), so this uses the `effective_user` pattern like feat/contact-notes. Upsert by `projection_key` + delete-on-omission, scoped to the caller's own rows under the request prefix — manual memories structurally unreachable. Added 2026-08-27. *(Amended 2026-08-27b:)* ownerless tokens 403 (matches contact-notes); reconcile fires `memory_added`; projection rows excluded from upstream's fuzzy `action_consolidate_memory` (would churn: consolidation deletes → next sync re-adds). | `routes/memory/memory_projection_routes.py` (new), `tests/test_memory_projection_routes.py` (new), `app.py`, `src/builtin_actions.py` |
| `feat/projects-view` | **Projects view backend** — even-odysseus world-model surface (DESIGN-projects-surface, ADR-0018): `/api/projects` list/detail proxy the brain's `/api/self/world` read API (`BRIDGE_BASE_URL`/`BRIDGE_TOKEN`, bridge idiom; brain client deliberately duplicated — mods are independent branches), filtered to project entities and merged with per-project file state under `data/personal_docs/projects/<name>/`. Multipart upload reuses the `/api/personal/upload` conventions (realpath confinement, secure unique names, size cap) + the SAME in-process RAG indexing path so drops are chat-retrievable immediately; folder lazily created + registered with the personal-docs manager; delete = `delete_by_source` + `exclude_file` (upload clears a stale exclusion). Legacy absolute `meta.docs_dir` is recovered to the canonical relative `projects/<name>` and re-stamped to the brain via `POST /api/self/world/{id}/docs-dir` after a successful upload (non-fatal). Auth `require_user`+`require_admin` (personal_routes idiom). Added 2026-08-27. *(Amended 2026-08-27e, the view itself:)* `static/js/projects.js` fulfils the Workspace shell's mount contract (`window.odysseusProjects.mount(container, opts)`, idempotent per tab switch; `opts.project` deep-links onto a project page — the hook the board chip uses via `switchWorkspaceView('projects', {project})`). Drill-in list → page (bridge.js recipe): list rows name/status/deadline/file-count with template kind tagged and archived statuses in a collapsed group; page renders the entity's honest state — **every blank facet is a visible `? name — nothing known yet` gap line by design** — plus the doc folder as a drop target (external-file drags only, `types` includes `'Files'`, so board card drags pass through) with browse fallback, per-file indexed-chunk results, and confirm-gated delete (deindex warning). 503 → quiet "not configured", 502/504 → quiet retry. `static/projects.css` = board/bridge terminal idiom on host tokens (accent: drop highlight, browse action, focus rings only); neutralizes stock global `details`/`summary` chrome and the mobile `button{height:32px}` rule inside the pane. NB: stock ui.js's capture-phase Escape arbiter closes the hovered/topmost window before mod listeners see the key — Esc-to-back only fires when the arbiter defers (focus in a text input, pointer off the window); the ‹ back button is the primary path. index.html hook is one script tag. *(Amended 2026-08-28, save_to_project chat tool:)* the assistant can now file pasted chat content into a project — "save this to the Williams Fellowship project as meeting-notes" writes `projects/<name>/<title>.md` and RAG-indexes it in-process with metadata identical to the upload route (so the Projects view lists it immediately, chat retrieval finds it, and the DELETE route deindexes it). Handler `src/agent_tools/project_tools.py` lazily reuses the routes module's helpers (src→routes idiom); registration is one line each in TOOL_HANDLERS/TOOL_TAGS, FUNCTION_TOOL_SCHEMAS + function-call→XML-block elif, a ctx-passing dispatch elif in tool_execution (BEFORE the ownerless dynamic_handlers catch-all — owner must reach chunk metadata), tool_index description + `project(s)` keyword force-include, TOOL_SECTIONS prompt entry, NON_ADMIN_BLOCKED_TOOLS + _PLAN_MODE_KNOWN_MUTATORS, _COMMON_TOOL_NAMES. Wrong project name → error listing the real names; 2MB cap; identical re-call is idempotent ("Already filed") — local models re-issue calls, and RAG's content-hash dedupe would make a suffixed twin unindexable noise. *(Amended 2026-08-28b, world-model browse:)* the one Projects tab now browses the WHOLE world model (operator-decided: sections, not new tabs/windows) — collapsed People(N)/Areas(N) `<details>` groups after the active project rows (Archived idiom generalized to `.prj-group`; graveyard stays last + dimmed) with drill-in person/area pages on the same back-button pattern. Backend on the same router: `GET /api/projects/world` (people+areas lists with server-computed plain-speech lines) + `GET /api/projects/world/{id}` (person/area detail — sibling route; `/{project_id}` stays project-only byte-stable, and `/world` MUST register before it or the wildcard 422s the literal). Obligation state is plain arithmetic, no LLM (never recorded / due now via cadence overdue or season month±1 unless done ≤62d / next-due date / no schedule known; cadence phrased humanly, 180→"every 6 months"); ADR-0018 hard rule: operator-life nouns only on screen. Person page: edges→"Reports to Ray"/"Holds recruitment for Williams Fellowship", open-loop lines ("waiting on their reply about X since date"), facet gaps; area page: obligation lines + honest "fills in as the morning questions ask" footer; world fetch failure degrades to projects-only list. | `routes/projects_routes.py` (new), `tests/test_projects_world_routes.py` (new), `tests/test_projects_routes.py` (new), `static/js/projects.js` (new), `static/projects.css` (new), `static/index.html`, `app.py`, `src/agent_tools/project_tools.py` (new), `tests/test_save_to_project_tool.py` (new), `src/agent_tools/__init__.py`, `src/tool_schemas.py`, `src/tool_execution.py`, `src/tool_index.py`, `src/tool_security.py`, `src/tool_policy.py`, `src/agent_loop.py` |
| `fix/agent-loop-any-import` | One-line fix for upstream 451900fc (v1.0.3 hotfix prep): `_resolved_tool_event_name` uses a `dict[str, Any]` annotation but `Any` was never imported, so `src.agent_loop` raises NameError **on import** — the whole chat agent loop is dead and 21 test files fail at collection (plus ~37 downstream test failures). Candidate to upstream then drop. Added 2026-08-27. | `src/agent_loop.py` |
| `meta/local-mods-guide` | This document. | `LOCAL-MODS.md` |

### Dropped because upstream absorbed them

| Branch | When | Why dropped |
|---|---|---|
| `fix/task-endpoint-url-normalization` | 2026-06-26 sync | Merged upstream as PR #4619 (2026-06-24); the new base carries it. |
| `feat/mcp-streamable-http` | 2026-06-26 sync | Upstream `main` now ships a native `http` = "Streamable HTTP" MCP transport **with OAuth/browser-authorization** (`_connect_http` → `streamablehttp_client(url, auth=provider)`) and the Add-MCP-Server UI already exposes the `http` transport option. Upstream's version is strictly more capable than this mod (which had a preflight probe but no OAuth), so re-seating it would regress auth. Branch retained locally for reference; not composed into `integration`. |

---

## The pristine-main invariant

`main` must stay **byte-identical to `origin/main`** at all times. Verify with:

```bash
git rev-parse main origin/main | uniq | wc -l   # must print 1
```

Why it matters: the update step fast-forwards `main` to the new upstream
(`git merge --ff-only`). If `main` carries even one local commit, the fast-forward
fails and the clean model falls apart. So **nothing local goes on `main`** — not even
this guide. Local stuff lives on its own branch and is composed into `integration`.

---

## How to update to a new upstream Odysseus

Run these from the repo root. You can start from `integration` (where this file is).

> **History-rewrite note (2026-08-27):** upstream force-pushed a rewritten history
> (an email scrub re-hashed every commit after ~2026-06-01), so the ff-only merge in
> step 2 could not apply. A one-time substitution was performed: `git reset --hard
> origin/main` on `main` (safe — zero local commits), then each mod re-seated with
> `git rebase --onto main <old-main-sha> <branch>` instead of `git rebase main`
> (the plain form would replay ~1900 upstream commits because the merge-base
> predates the rewrite). Pre-rewrite refs are preserved under the
> `backup/resync-0827/*` tags. If upstream ever rewrites history again, repeat that
> substitution; otherwise the normal flow below applies.

```bash
# 1. Pull the new upstream code
git fetch origin

# 2. Advance the pristine mirror (fast-forward only — must succeed cleanly)
git checkout main
git merge --ff-only origin/main

# 3. Re-seat each mod onto the new base, one at a time.
#    Each is a single commit, so a conflict (if any) is small and local.
for b in fix/task-owner-attribution fix/task-agent-wall-clock-timeout fix/app-bind-host feat/task-grounding-url-fetch fix/web-fetch-private-ip-guard fix/task-result-delivery fix/memory-mcp-owner feat/task-board feat/task-lifecycle feat/bridge-review feat/contact-notes feat/morning-brief feat/captures-mcp feat/memory-projection feat/projects-view fix/agent-loop-any-import meta/local-mods-guide; do
  echo "==> rebasing $b"
  git checkout "$b" && git rebase main || {
    echo "CONFLICT in $b — resolve the file, 'git add' it, then 'git rebase --continue'."
    echo "(or 'git rebase --abort' to back out and deal with it manually)"
    exit 1
  }
done

# 4. Rebuild the integration branch = main + all mods, and run from it.
git checkout -B integration main
git merge --no-edit fix/task-owner-attribution fix/task-agent-wall-clock-timeout fix/app-bind-host feat/task-grounding-url-fetch fix/web-fetch-private-ip-guard fix/task-result-delivery fix/memory-mcp-owner feat/task-board feat/task-lifecycle feat/bridge-review feat/contact-notes feat/morning-brief feat/captures-mcp feat/memory-projection feat/projects-view fix/agent-loop-any-import meta/local-mods-guide

# 5. (optional) push the re-seated branches to your fork
git push --force-with-lease fork \
  fix/task-owner-attribution fix/task-agent-wall-clock-timeout fix/app-bind-host feat/task-grounding-url-fetch fix/web-fetch-private-ip-guard fix/task-result-delivery fix/memory-mcp-owner feat/task-board feat/task-lifecycle feat/bridge-review feat/contact-notes feat/morning-brief feat/captures-mcp feat/memory-projection feat/projects-view fix/agent-loop-any-import meta/local-mods-guide
```

### If a rebase hits a conflict
Upstream changed the same lines a mod touches. Open the conflicted file, keep the
intent described in the table above, `git add` it, then `git rebase --continue`.
Because each mod is one tightly-scoped commit, this is usually a one-file, few-line fix.

### If upstream absorbs a mod
If a new upstream release already does what one of our mods does (e.g. they fix
`_owner` themselves), the rebase of that branch will come up empty / no longer apply.
Just **delete that branch** and drop it from steps 3–5 and the table above. The other
mods are unaffected — that independence is the point.

---

## Adding a NEW local mod

Never edit files directly on `main` or `integration`. Instead:

```bash
git checkout main
git checkout -b fix/my-new-thing      # branch off pristine main
# ...make the change, keep it surgical, touch as few files as possible...
git commit -am "fix(scope): what and WHY (so a future rebase conflict is easy to resolve)"
```

Then:
1. Add a row to the table above (on this `meta/local-mods-guide` branch).
2. Add the branch name to the `for` loop and the `merge` line in the update steps above.
3. Rebuild `integration` (step 4) so the new mod goes live.

Keep each commit message explanatory — the message is what makes a future conflict
trivial to resolve correctly.

---

## Running

Run the service from `integration` (it has all mods composed in). Persistence on the
Mac mini is via `./install-service.sh` (bound to the Tailscale IP). Day-to-day:
`git checkout integration` and you have everything.
