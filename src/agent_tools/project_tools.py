# src/agent_tools/project_tools.py
"""save_to_project — file chat content into a world-model project's folder.

Part of the feat/projects-view mod (DESIGN-projects-surface): the sanctioned
way for the chat assistant (and agents) to file text into one of the
operator's projects. The tool writes a file into the project's document
folder under ``data/personal_docs/projects/<name>/`` AND RAG-indexes it
in-process, so the content immediately (a) appears in the Workspace's
Projects view file list and (b) is retrievable in any chat.

It deliberately reuses the module-level helpers in
``routes/projects_routes.py`` (lazy src->routes import — the codebase idiom,
cf. task_scheduler/builtin_actions importing email_routes helpers) so file
placement, realpath confinement, chunk metadata and the docs-dir stamp stay
byte-identical with the upload route. That identity is load-bearing: the
Projects view's DELETE route deindexes by the same ``source`` path, and chat
retrieval filters on the same ``owner`` metadata.

Content contract (create_document conventions):
  XML tags (preferred, what native function calls are converted to):
      <project>Williams Fellowship</project>
      <title>meeting-notes</title>          (optional)
      <format>md</format>                   (optional: md | txt)
      <content>...the text...</content>
  Line-based fallback: line 1 = project name or id, rest = content
  (title is then derived from the first content line).
"""

import logging
import os
import re

logger = logging.getLogger(__name__)

# Generous sanity cap (the upload route's 25MB is for binary files; pasted
# chat text beyond this is almost certainly a mistake).
MAX_CONTENT_BYTES = 2 * 1024 * 1024

_FORMAT_EXTS = {
    "md": ".md", "markdown": ".md",
    "txt": ".txt", "text": ".txt", "plain": ".txt",
}


def _rag():
    """RAG singleton accessor (module-level seam for tests)."""
    from src.rag_singleton import get_rag_manager
    return get_rag_manager()


def _docs_manager():
    """The app's PersonalDocsManager (set at startup via
    src.ai_interaction.init; None in bare unit tests)."""
    from src import ai_interaction
    return getattr(ai_interaction, "_personal_docs_manager", None)


def _parse_content(raw: str):
    """Return (project, title, fmt, body). XML tags first, then line-based."""
    project = title = fmt = body = None
    mp = re.search(r"<project>\s*(.*?)\s*</project>", raw, re.DOTALL | re.IGNORECASE)
    mt = re.search(r"<title>\s*(.*?)\s*</title>", raw, re.DOTALL | re.IGNORECASE)
    mf = re.search(r"<format>\s*(.*?)\s*</format>", raw, re.DOTALL | re.IGNORECASE)
    mc = re.search(r"<content>\s*(.*?)\s*</content>", raw, re.DOTALL | re.IGNORECASE)
    if mp or mc:
        project = mp.group(1).strip() if mp else None
        title = mt.group(1).strip() if mt else None
        fmt = mf.group(1).strip().lower() if mf else None
        body = mc.group(1) if mc else None
    if project is None or body is None:
        cleaned = re.sub(r"</?(?:project|title|format|content)>", "", raw)
        lines = cleaned.strip().split("\n")
        if project is None:
            project = lines[0].strip() if lines else ""
            lines = lines[1:]
        if body is None:
            body = "\n".join(lines)
    return project or "", title, fmt, body or ""


def _derive_title(body: str) -> str:
    """First non-empty content line, markdown-heading hashes stripped."""
    for line in body.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:60]
    return "note"


def _match_project(entities, wanted: str):
    """Match id exactly or name case-insensitively. Instances outrank
    templates and active outranks archived when a name is duplicated."""
    wanted = (wanted or "").strip()
    candidates = []
    for e in entities:
        if wanted.isdigit() and str(e.get("id")) == wanted:
            candidates.append(e)
        elif (str(e.get("name") or "").strip().lower() == wanted.lower()):
            candidates.append(e)
    candidates.sort(key=lambda e: (e.get("status") != "active",
                                   e.get("kind") != "project-instance"))
    return candidates[0] if candidates else None


class SaveToProjectTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        # Lazy import: routes module carries fastapi/httpx deps and the
        # shared helpers; importing here keeps agent_tools import light and
        # lets tests monkeypatch projects_routes seams (_brain, PERSONAL_DIR).
        import routes.projects_routes as _pr
        from fastapi import HTTPException

        owner = (ctx or {}).get("owner")
        project_ref, title, fmt, body = _parse_content(content or "")

        if not project_ref:
            return {"error": "save_to_project needs a <project> (name or id).",
                    "exit_code": 1}
        if not body.strip():
            return {"error": "save_to_project got empty <content> — nothing to save.",
                    "exit_code": 1}
        body_bytes = body.encode("utf-8")
        if len(body_bytes) > MAX_CONTENT_BYTES:
            return {"error": f"Content is too large to file "
                             f"({len(body_bytes)} bytes > {MAX_CONTENT_BYTES} cap). "
                             "Save it as a file upload in the Projects view instead.",
                    "exit_code": 1}

        rag = _rag()
        if not rag:
            return {"error": "RAG system is not available — cannot index the "
                             "content. Is the embedding service running?",
                    "exit_code": 1}

        try:
            world = await _pr._brain("GET", "/api/self/world")
        except HTTPException as e:
            if e.status_code == 503:
                return {"error": "Projects are not configured on this "
                                 "deployment (BRIDGE_TOKEN unset).", "exit_code": 1}
            return {"error": f"Could not reach the projects backend: {e.detail}",
                    "exit_code": 1}

        entities = [e for e in world.get("entities", [])
                    if e.get("kind") in _pr._PROJECT_KINDS]
        entity = _match_project(entities, project_ref)
        if entity is None:
            names = sorted({str(e.get("name") or "").strip()
                            for e in entities if e.get("name")})
            listing = "; ".join(names) if names else "(none)"
            return {"error": f"No project matches {project_ref!r}. "
                             f"Available projects: {listing}. "
                             "Use one of these names (or its id) exactly.",
                    "exit_code": 1}

        try:
            rel, needs_stamp = _pr._resolve_docs_rel(entity)
            abs_dir = _pr._docs_abs(rel)
            created = not os.path.isdir(abs_dir)
            os.makedirs(abs_dir, exist_ok=True)

            ext = _FORMAT_EXTS.get((fmt or "").lower(), ".md")
            stem = (title or _derive_title(body)).strip() or "note"
            file_path, stored_name = _pr._unique_target(abs_dir, f"{stem}{ext}")
            with open(file_path, "wb") as f:
                f.write(body_bytes)
        except HTTPException as e:
            return {"error": f"Could not file into project "
                             f"{entity.get('name')!r}: {e.detail}", "exit_code": 1}

        manager = _docs_manager()

        # Re-uploading a previously deleted name must clear the persisted
        # listing exclusion (same semantics as the upload route's _unexclude).
        if manager is not None:
            try:
                excluded = getattr(manager, "excluded_files", None)
                abs_path = os.path.abspath(file_path)
                if isinstance(excluded, set) and abs_path in excluded:
                    excluded.discard(abs_path)
                    manager._save_excluded()
            except Exception as e:
                logger.warning(f"Could not clear exclusion for {file_path}: {e}")

        # Index in-process with metadata IDENTICAL to the upload route's —
        # the DELETE route deindexes by source and chat retrieval is
        # owner-scoped, so any drift here breaks them.
        indexed_chunks = 0
        failed_chunks = 0
        try:
            chunks = rag._split_into_chunks(body, chunk_size=500)
        except Exception as e:
            logger.warning(f"Chunking failed for {stored_name}: {e}")
            chunks = []
        for i, chunk in enumerate(chunks):
            metadata = {
                "source": file_path,
                "filename": stored_name,
                "stored_filename": stored_name,
                "directory": abs_dir,
                "type": ext,
                "chunk_id": i,
            }
            if owner:
                metadata["owner"] = owner
            try:
                ok = rag.add_document(chunk, metadata)
            except Exception as e:
                logger.warning(f"RAG add failed for {stored_name} chunk {i}: {e}")
                ok = False
            if ok:
                indexed_chunks += 1
            else:
                failed_chunks += 1

        # Register the folder with the personal-docs manager (index=False —
        # chunks were just added in-process with owner metadata; a manager
        # re-index would create a second ownerless copy). Non-fatal.
        if manager is not None:
            try:
                tracked = manager.get_indexed_directories()
            except Exception:
                tracked = []
            if abs_dir not in tracked:
                try:
                    manager.add_directory(abs_dir, index=False)
                except Exception as e:
                    logger.warning(f"Could not register {abs_dir} with "
                                   f"personal-docs manager: {e}")

        # Canonical docs_dir re-stamp to the brain (upload-route semantics:
        # non-fatal — the file is already saved).
        stamped = False
        if needs_stamp or created:
            try:
                stamped = await _pr._stamp_docs_dir(entity.get("id"), rel)
            except Exception as e:
                logger.warning(f"docs-dir stamp failed for entity "
                               f"{entity.get('id')}: {e}")

        project_name = entity.get("name") or project_ref
        msg = (f"Filed to project {project_name!r} as {stored_name} "
               f"({rel}/{stored_name}), {indexed_chunks} chunk"
               f"{'s' if indexed_chunks != 1 else ''} indexed for retrieval.")
        if failed_chunks:
            msg += f" WARNING: {failed_chunks} chunk(s) failed to index."
        if indexed_chunks == 0 and not failed_chunks:
            msg += " (No indexable text was produced.)"
        msg += (" The file now appears in the Workspace Projects view and its"
                " content is retrievable in chats.")
        if stamped:
            msg += " Project docs folder registered with the brain."
        logger.info(f"save_to_project: {project_name!r} <- {stored_name} "
                    f"({indexed_chunks} chunks, owner={owner!r})")
        return {"output": msg, "exit_code": 0}
