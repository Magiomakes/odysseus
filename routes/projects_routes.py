# routes/projects_routes.py
"""Projects view — even-odysseus world-model proxy + per-project doc folders.

The brain service (even-odysseus, default http://127.0.0.1:8765) owns the
world model (ADR-0018): project entities, their facets, edges and open
loops. This module makes those projects browsable and drag-and-drop
addable INSIDE Odysseus (DESIGN-projects-surface, 2026-08-27) without
creating a second project store: list/detail are server-side proxies of
the brain's `/api/self/world` read API, merged with the on-disk file
state of each project's document folder under
`data/personal_docs/projects/<name>/`.

Uploads reuse the /api/personal/upload conventions (realpath confinement
under PERSONAL_DIR, secure filenames, size cap) and the SAME in-process
RAG indexing path, so a dropped file is retrievable by chat immediately —
no watcher exists over personal_docs, which is exactly why in-app drop
beats file-system sync. Deletes reuse the personal delete-file path
(delete_by_source + exclude_file).

docs_dir resolution (legacy-data wrinkle): the canonical stored form on a
project entity is a RELATIVE path `projects/<name>` (resolved against the
fork's personal_docs root). Legacy entities carry an absolute
`~/Documents/EvenOdysseus/projects/<name>` (a Finder symlink to the same
folders) — we recover the trailing `projects/<name>` segment from it; if
that fails we derive `projects/<entity name>` (spaces preserved, matching
the existing folders). After a successful upload the canonical relative
value is re-stamped to the brain via `POST /api/self/world/{id}/docs-dir`
(non-fatal on failure — the upload already succeeded).

Modularity contract (LOCAL-MODS.md): self-contained in this file; app.py's
only hook is one include_router line. The brain client is deliberately
duplicated from routes/bridge_routes.py rather than imported — mods are
independent branches and feat/bridge-review may not be composed in.

Config (.env; unset = routes answer 503 and the view hides itself):
    BRIDGE_BASE_URL   brain service origin (default http://127.0.0.1:8765)
    BRIDGE_TOKEN      the brain's INGEST_TOKEN (server-to-server; the
                      browser never sees it — that is the point of proxying).

Auth: require_user + require_admin on every route (personal_routes.py
idiom — these routes read/write the personal-docs store). Bearer ody_
tokens are rejected outright by require_user, so no token scope-gate is
needed here (stricter than the bridge's _require_token_access).
"""

import json
import logging
import os
import re
import uuid
from typing import List, Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile

from core.constants import PERSONAL_DIR
from core.middleware import require_admin
from src.auth_helpers import require_user
from src.rag_singleton import get_rag_manager
from src.upload_handler import secure_filename
from src.upload_limits import PERSONAL_UPLOAD_MAX_BYTES

logger = logging.getLogger(__name__)

_PROJECT_KINDS = ("project-template", "project-instance")
_PROJECTS_SEGMENT = "projects"
_DOCS_DIR_MAX_LEN = 200  # mirrors the brain's validator

_T_READ = 15.0
_T_STAMP = 15.0


# --------------------------------------------------------------------------
# Brain client (bridge_routes idiom, self-contained — see module docstring)
# --------------------------------------------------------------------------

def _base() -> str:
    return (os.environ.get("BRIDGE_BASE_URL") or "http://127.0.0.1:8765").rstrip("/")


def _token() -> str:
    return os.environ.get("BRIDGE_TOKEN", "")


def _configured() -> bool:
    return bool(_token())


def _headers() -> dict:
    return {"Authorization": f"Bearer {_token()}"}


async def _brain(method: str, path: str, *, json_body=None,
                 timeout: float = _T_READ) -> dict:
    """One JSON round-trip to the brain service. Raises HTTPException with
    the brain's status on a non-2xx, 502/504 when the brain is down/slow.
    Seam for tests (monkeypatch this)."""
    if not _configured():
        raise HTTPException(503, "projects not configured (set BRIDGE_TOKEN)")
    url = _base() + path
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, url, json=json_body,
                                        headers=_headers())
    except httpx.TimeoutException:
        raise HTTPException(504, f"brain timed out on {path}")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"brain unreachable ({e.__class__.__name__})")
    if resp.status_code >= 400:
        detail = resp.text[:300]
        try:
            detail = resp.json().get("error", detail)
        except Exception:
            pass
        raise HTTPException(resp.status_code, detail)
    try:
        return resp.json()
    except Exception:
        raise HTTPException(502, f"brain returned non-JSON for {path}")


# --------------------------------------------------------------------------
# docs_dir resolution
# --------------------------------------------------------------------------

def _parse_meta(entity: dict) -> dict:
    """The brain serializes entity.meta as a JSON string; parse defensively."""
    raw = entity.get("meta")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _is_canonical_docs_dir(value: str) -> bool:
    """Mirror of the brain's validator: a relative path under `projects/`,
    no empty/'.'/'..' segments, no backslashes/control chars, bounded."""
    if not isinstance(value, str) or not value or len(value) > _DOCS_DIR_MAX_LEN:
        return False
    if "\\" in value or any(ord(c) < 32 for c in value):
        return False
    parts = value.split("/")
    if parts[0] != _PROJECTS_SEGMENT or len(parts) < 2:
        return False
    return all(p not in ("", ".", "..") for p in parts[1:])


def _sanitize_folder_name(name: str) -> str:
    """Sanitize an entity name into a folder segment the way the existing
    project folders are named: spaces PRESERVED ("Williams Fellowship"),
    path separators/control chars stripped, no leading dots."""
    cleaned = re.sub(r"[\\/:\x00-\x1f]", "", name or "").strip().lstrip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:100].strip()


def _resolve_docs_rel(entity: dict) -> Tuple[str, bool]:
    """Return (relative docs_dir under PERSONAL_DIR, needs_stamp).

    Canonical stored form passes through untouched. The legacy absolute
    form (`~/Documents/EvenOdysseus/projects/<name>` — a symlink to the
    same personal_docs folders) is recovered by extracting the trailing
    `projects/<name>` segment so the pointer keeps targeting the folder it
    always did (NB: entity "Innovation Sprint Fall 2026" legitimately
    points at folder "Innovation Sprint" — deriving from the entity name
    would break that link). Only a missing/unrecoverable value falls back
    to `projects/<entity name>`.
    """
    meta = _parse_meta(entity)
    stored = meta.get("docs_dir")
    if isinstance(stored, str) and _is_canonical_docs_dir(stored.strip()):
        return stored.strip(), False

    # Legacy absolute / ~ form: recover ".../projects/<tail>".
    if isinstance(stored, str) and stored.strip():
        normalized = stored.strip().replace("\\", "/")
        marker = f"/{_PROJECTS_SEGMENT}/"
        idx = normalized.rfind(marker)
        if idx != -1:
            tail = normalized[idx + len(marker):].strip("/")
            candidate = f"{_PROJECTS_SEGMENT}/{tail}"
            if _is_canonical_docs_dir(candidate):
                return candidate, True

    # Missing/invalid: derive from the entity name.
    folder = _sanitize_folder_name(str(entity.get("name") or ""))
    if not folder:
        raise HTTPException(500, "cannot derive a docs folder for this project")
    return f"{_PROJECTS_SEGMENT}/{folder}", True


def _docs_abs(rel: str) -> str:
    """Resolve the relative docs_dir against PERSONAL_DIR with realpath
    confinement (personal_routes idiom — a symlinked segment must not
    escape the personal-docs root)."""
    base_abs = os.path.realpath(PERSONAL_DIR)
    resolved = os.path.realpath(os.path.join(base_abs, rel))
    try:
        in_base = os.path.commonpath([resolved, base_abs]) == base_abs
    except ValueError:
        in_base = False
    if not in_base or resolved == base_abs:
        raise HTTPException(403, "project folder must be inside personal documents")
    return resolved


def _list_files(abs_dir: str) -> List[dict]:
    """Non-hidden regular files in the project folder: name, size, mtime."""
    if not os.path.isdir(abs_dir):
        return []
    out = []
    try:
        with os.scandir(abs_dir) as it:
            for entry in it:
                if entry.name.startswith("."):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                out.append({"name": entry.name, "size": st.st_size,
                            "mtime": int(st.st_mtime)})
    except OSError as e:
        logger.warning(f"Cannot list project folder {abs_dir}: {e}")
        return []
    out.sort(key=lambda f: (-f["mtime"], f["name"]))
    return out


async def _stamp_docs_dir(entity_id: int, rel: str) -> bool:
    """Re-stamp the canonical relative docs_dir onto the brain entity.
    Non-fatal by contract: the upload already succeeded, losing the stamp
    only delays the migration until the next upload."""
    try:
        res = await _brain("POST", f"/api/self/world/{entity_id}/docs-dir",
                           json_body={"docs_dir": rel}, timeout=_T_STAMP)
        if res.get("ok"):
            logger.info(f"Stamped docs_dir={rel!r} on brain entity {entity_id}")
            return True
        logger.warning(f"docs-dir stamp for entity {entity_id} returned {res!r}")
    except HTTPException as e:
        logger.warning(f"docs-dir stamp for entity {entity_id} failed: {e.detail}")
    except Exception as e:
        logger.warning(f"docs-dir stamp for entity {entity_id} failed: {e}")
    return False


def _project_view(entity: dict) -> dict:
    """Common projection: entity passthrough with meta parsed to an object
    plus the resolved docs_dir + on-disk file state."""
    rel, needs_stamp = _resolve_docs_rel(entity)
    abs_dir = _docs_abs(rel)
    view = dict(entity)
    view["meta"] = _parse_meta(entity)
    view["docs_dir"] = rel
    view["docs_dir_exists"] = os.path.isdir(abs_dir)
    view["docs_dir_canonical"] = not needs_stamp
    return view


async def _project_entity(entity_id: int) -> dict:
    """Fetch one entity from the brain and 404 unless it is a project."""
    entity = await _brain("GET", f"/api/self/world/{entity_id}")
    if entity.get("kind") not in _PROJECT_KINDS:
        raise HTTPException(404, "no project with that id")
    return entity


def _unique_target(abs_dir: str, original_name: Optional[str]) -> Tuple[str, str]:
    """Sanitized, collision-safe target path inside the project folder.
    Keeps the plain filename when free (project folders are browsed by
    name); a collision gets a short uuid suffix (personal_routes idiom)."""
    safe = secure_filename(os.path.basename(original_name or "upload"))
    if not safe or safe.startswith("."):
        safe = "upload"
    stem, ext = os.path.splitext(safe)
    stem = (stem or "upload")[:80]
    candidate = os.path.abspath(os.path.join(abs_dir, f"{stem}{ext.lower()}"))
    if os.path.exists(candidate):
        candidate = os.path.abspath(
            os.path.join(abs_dir, f"{stem}-{uuid.uuid4().hex[:10]}{ext.lower()}"))
    base_abs = os.path.abspath(abs_dir)
    if os.path.commonpath([candidate, base_abs]) != base_abs:
        raise HTTPException(400, "unsafe upload filename")
    return candidate, os.path.basename(candidate)


def _extract_text(file_path: str, content_bytes: bytes, ext: str) -> str:
    """Text extraction matching the personal-docs indexer's behavior."""
    from src.personal_docs import extract_pdf_text
    from src.markitdown_runtime import MARKITDOWN_EXTS
    if ext == ".pdf":
        return extract_pdf_text(file_path)
    if ext in MARKITDOWN_EXTS:
        from src.personal_docs import extract_office_text
        return extract_office_text(file_path)
    return content_bytes.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

def setup_projects_routes(personal_docs_manager) -> APIRouter:
    """Build the /api/projects router.

    Args:
        personal_docs_manager: PersonalDocsManager instance (directory
            registration + listing exclusions live there).
    """
    router = APIRouter(prefix="/api/projects", tags=["projects"])

    def _rag():
        return get_rag_manager()

    def _unexclude(file_path: str) -> None:
        """Re-uploading a previously deleted name must clear the persisted
        listing exclusion, or the file stays hidden from /api/personal and
        the keyword lane forever. No public un-exclude API exists (only
        add_directory's clear-on-first-track), so reach into the manager's
        exclusion set guardedly."""
        try:
            excluded = getattr(personal_docs_manager, "excluded_files", None)
            abs_path = os.path.abspath(file_path)
            if isinstance(excluded, set) and abs_path in excluded:
                excluded.discard(abs_path)
                personal_docs_manager._save_excluded()
        except Exception as e:
            logger.warning(f"Could not clear exclusion for {file_path}: {e}")

    @router.get("")
    async def list_projects(owner: str = Depends(require_user),
                            _admin: None = Depends(require_admin)):
        """Brain world entities filtered to projects, each merged with its
        folder's file count."""
        world = await _brain("GET", "/api/self/world")
        projects = []
        for entity in world.get("entities", []):
            if entity.get("kind") not in _PROJECT_KINDS:
                continue
            try:
                view = _project_view(entity)
            except HTTPException as e:
                logger.warning(f"Skipping project entity "
                               f"{entity.get('id')}: {e.detail}")
                continue
            view["file_count"] = len(_list_files(_docs_abs(view["docs_dir"])))
            projects.append(view)
        # Active first, then by kind (instances before templates), then name.
        projects.sort(key=lambda p: (p.get("status") != "active",
                                     p.get("kind") != "project-instance",
                                     (p.get("name") or "").lower()))
        return {"projects": projects}

    @router.get("/{project_id}")
    async def project_detail(project_id: int,
                             owner: str = Depends(require_user),
                             _admin: None = Depends(require_admin)):
        """Brain entity detail (facets/edges/loops passthrough) merged with
        the folder's file list."""
        entity = await _project_entity(project_id)
        view = _project_view(entity)
        files = _list_files(_docs_abs(view["docs_dir"]))
        view["files"] = files
        view["file_count"] = len(files)
        return view

    @router.post("/{project_id}/files")
    async def upload_project_files(project_id: int,
                                   files: List[UploadFile] = File(...),
                                   owner: str = Depends(require_user),
                                   _admin: None = Depends(require_admin)):
        """Multipart upload into the project's folder + immediate in-process
        RAG indexing (same path as /api/personal/upload). Lazily creates the
        folder, registers it with the personal-docs manager, and re-stamps
        the canonical relative docs_dir to the brain when needed."""
        rag = _rag()
        if not rag:
            raise HTTPException(503, "RAG system is not available — is the "
                                     "embedding service running?")
        entity = await _project_entity(project_id)
        rel, needs_stamp = _resolve_docs_rel(entity)
        abs_dir = _docs_abs(rel)

        created = not os.path.isdir(abs_dir)
        os.makedirs(abs_dir, exist_ok=True)

        uploaded = []
        total_indexed = 0
        total_failed = 0
        for upload in files:
            try:
                file_path, stored_name = _unique_target(abs_dir, upload.filename)
                content_bytes = await upload.read(PERSONAL_UPLOAD_MAX_BYTES + 1)
                if len(content_bytes) > PERSONAL_UPLOAD_MAX_BYTES:
                    logger.warning(f"Rejected oversized project upload: "
                                   f"{upload.filename!r}")
                    total_failed += 1
                    continue
                with open(file_path, "wb") as f:
                    f.write(content_bytes)
                _unexclude(file_path)

                ext = os.path.splitext(stored_name)[1].lower()
                try:
                    text = _extract_text(file_path, content_bytes, ext)
                except Exception as e:
                    logger.warning(f"Text extraction failed for {stored_name}: {e}")
                    text = ""

                indexed_chunks = 0
                if text and text.strip():
                    chunks = rag._split_into_chunks(text, chunk_size=500)
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
                        if rag.add_document(chunk, metadata):
                            indexed_chunks += 1
                        else:
                            total_failed += 1
                total_indexed += indexed_chunks
                uploaded.append({"name": stored_name,
                                 "indexed_chunks": indexed_chunks})
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to upload/index {upload.filename}: {e}")
                total_failed += 1

        # Register the folder with the personal-docs manager (index=False:
        # chunks were just added in-process with owner metadata; a manager
        # re-index would create a second ownerless copy).
        try:
            tracked = personal_docs_manager.get_indexed_directories()
        except Exception:
            tracked = []
        if abs_dir not in tracked:
            try:
                personal_docs_manager.add_directory(abs_dir, index=False)
            except Exception as e:
                logger.warning(f"Could not register {abs_dir} with "
                               f"personal-docs manager: {e}")

        # Canonical re-stamp (legacy/missing docs_dir, or brand-new folder).
        stamped = False
        if uploaded and (needs_stamp or created):
            stamped = await _stamp_docs_dir(project_id, rel)

        return {
            "ok": True,
            "uploaded": uploaded,
            "indexed_count": total_indexed,
            "failed_count": total_failed,
            "docs_dir": rel,
            "docs_dir_created": created,
            "docs_dir_stamped": stamped,
            "files": _list_files(abs_dir),
        }

    @router.delete("/{project_id}/files")
    async def delete_project_file(project_id: int,
                                  name: str = Query(...),
                                  owner: str = Depends(require_user),
                                  _admin: None = Depends(require_admin)):
        """Remove one file from the project folder and deindex it
        (personal_routes delete-file path: delete_by_source + exclude_file)."""
        entity = await _project_entity(project_id)
        rel, _needs_stamp = _resolve_docs_rel(entity)
        abs_dir = _docs_abs(rel)

        base = os.path.basename(name or "")
        if not base or base != name or base.startswith("."):
            raise HTTPException(400, "bad file name")
        target = os.path.realpath(os.path.join(abs_dir, base))
        try:
            in_dir = os.path.commonpath([target, abs_dir]) == abs_dir
        except ValueError:
            in_dir = False
        if not in_dir or target == abs_dir:
            raise HTTPException(400, "bad file name")
        if not os.path.isfile(target):
            raise HTTPException(404, "no such file in this project")

        removed = 0
        rag = _rag()
        if rag:
            try:
                removed = rag.delete_by_source(target)
            except Exception as e:
                logger.warning(f"RAG removal failed for {target}: {e}")

        deleted_from_disk = False
        try:
            os.remove(target)
            deleted_from_disk = True
        except FileNotFoundError:
            pass  # race with another delete
        except OSError as e:
            raise HTTPException(500, f"could not delete file: {e}")

        try:
            personal_docs_manager.exclude_file(target)
        except Exception as e:
            logger.warning(f"Could not exclude {target} from listing: {e}")

        return {
            "ok": True,
            "name": base,
            "removed_chunks": removed,
            "deleted_from_disk": deleted_from_disk,
            "files": _list_files(abs_dir),
        }

    return router
