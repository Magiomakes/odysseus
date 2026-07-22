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
| `fix/task-owner-attribution` | Bearer `ody_` tokens attribute task CRUD + notifications to the real minting owner via `effective_user()`, not the sandboxed `"api"` pseudo-user (otherwise token-created tasks are invisible to the named-login UI). Also exposes `owner` in the task API representation so a bearer-token client can confirm attribution (it was correct in the DB but absent from the JSON, so clients read it as `owner=None`). Upstream PR #4822 — candidate to upstream then drop. | `routes/task_routes.py` |
| `fix/app-bind-host` | macOS app launcher honors `APP_BIND` from `.env` (e.g. `0.0.0.0` for LAN/Tailscale); defaults to `127.0.0.1`. | `build-macos-app.sh` |
| `fix/task-agent-wall-clock-timeout` | Hard wall-clock cap (`task_agent_timeout_seconds`, default 900s) on a scheduled task's agent loop. Task execution holds the single `Semaphore(1)` slot, so one wedged/slow stream would otherwise park it forever and stall the whole queue ("no task fired in weeks"). On expiry the stream is cancelled, partial output kept, slot released. Upstream PR #4827 — candidate to upstream then drop. | `src/task_scheduler.py`, `src/settings.py` |
| `feat/task-grounding-url-fetch` | Scheduled task agents can pull grounding from a URL named in the prompt (even-odysseus "pull, not push" / ADR-0007). Research tasks (no tool loop) pre-fetch any **allowlisted** URL and prepend it as grounding context; llm tasks get `web_fetch` promoted to always-available. Gated by env `WEB_FETCH_ALLOWLIST` (comma-separated hostnames; empty = off) so an autonomous task can't be steered into an SSRF fetch. *(Amended 2026-07-02: redirect hops re-pass the allowlist — `follow_redirects=False` + manual hop loop.)* **Requires `WEB_FETCH_ALLOWLIST=<even-odysseus host>` in `.env`.** | `src/task_scheduler.py`, `src/tool_index.py`, `tests/test_task_grounding_fetch.py` |
| `fix/web-fetch-private-ip-guard` | SSRF guard on the generic `web_fetch` tool: refuses targets that are, or resolve to, loopback/private/link-local space unless the hostname is on `WEB_FETCH_ALLOWLIST` (same env + semantics as the grounding pre-fetch, so the allowlist constrains everything its name implies). Task-agent prompts are untrusted input (voice transcripts, API-created tasks) — without this, an injected instruction could read any service on the machine/LAN. Added 2026-07-02 (even-odysseus VISION.md Phase 0). | `src/net_guard.py` (new), `src/agent_tools/web_tools.py`, `tests/test_web_fetch_guard.py` |
| `fix/task-result-delivery` | Task results reach the user even when the app is closed. (1) `output_target='notification'` results were queued only in RAM and wiped by any restart — now persisted to `DATA_DIR/task_notifications.json` and restored on startup. (2) The `email_results` column existed in the schema (and defaulted on) but no code ever read it — now honored for llm/research tasks via the existing SMTP delivery path (housekeeping actions excluded to avoid inbox spam; skipped when output target is already email). Added 2026-07-22. Candidate to upstream. | `src/task_scheduler.py` |
| `fix/memory-mcp-owner` | Built-in memory MCP server launches with `ODYSSEUS_MCP_MEMORY_OWNER` resolved (explicit env wins; single-user installs scope to the sole `auth.json` account). The MCP stdio client *replaces* the child env, so the owner never reached the server before — against an owner-scoped store every `manage_memory` call (agent saves, even-odysseus bridge sessions) silently failed with a scope error. Added 2026-07-22. Candidate to upstream. | `src/builtin_mcp.py` |
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

```bash
# 1. Pull the new upstream code
git fetch origin

# 2. Advance the pristine mirror (fast-forward only — must succeed cleanly)
git checkout main
git merge --ff-only origin/main

# 3. Re-seat each mod onto the new base, one at a time.
#    Each is a single commit, so a conflict (if any) is small and local.
for b in fix/task-owner-attribution fix/task-agent-wall-clock-timeout fix/app-bind-host feat/task-grounding-url-fetch fix/web-fetch-private-ip-guard fix/task-result-delivery fix/memory-mcp-owner meta/local-mods-guide; do
  echo "==> rebasing $b"
  git checkout "$b" && git rebase main || {
    echo "CONFLICT in $b — resolve the file, 'git add' it, then 'git rebase --continue'."
    echo "(or 'git rebase --abort' to back out and deal with it manually)"
    exit 1
  }
done

# 4. Rebuild the integration branch = main + all mods, and run from it.
git checkout -B integration main
git merge --no-edit fix/task-owner-attribution fix/task-agent-wall-clock-timeout fix/app-bind-host feat/task-grounding-url-fetch fix/web-fetch-private-ip-guard fix/task-result-delivery fix/memory-mcp-owner meta/local-mods-guide

# 5. (optional) push the re-seated branches to your fork
git push --force-with-lease fork \
  fix/task-owner-attribution fix/task-agent-wall-clock-timeout fix/app-bind-host feat/task-grounding-url-fetch fix/web-fetch-private-ip-guard fix/task-result-delivery fix/memory-mcp-owner meta/local-mods-guide
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
