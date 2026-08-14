from __future__ import annotations

import asyncio
import logging
import math
import os
import weakref
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from .datasets import public_dataset, utc_now
from .datasets import router as datasets_router
from .detections import router as detections_router
from .media import POINT_PREVIEW_MAX_BUDGET, VWORLD_DEVELOPMENT_KEY
from .media import router as media_router
from .optimizer import router as optimizer_router
from .overlays import router as overlays_router
from .runs import RunManager, public_run
from .runs import router as runs_router
from .security import UnsafePath, normalize_relative_path, opaque_id, resolve_under_root
from .store import WebStore
from .surveys import router as surveys_router
from .uploads import router as uploads_router

API_VERSION = "1"
SERVER_VERSION = "0.1.0"


def _panorama_alignment_defaults(config_path: Path) -> tuple[float, float]:
    """Read the validated pipeline's residual image-space angular offsets."""

    try:
        document = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) or {}
        section = document.get("panorama_alignment", {})
        yaw = float(section.get("panorama_yaw_offset_deg", 0.0))
        pitch = float(section.get("panorama_pitch_offset_deg", 0.0))
        if not (math.isfinite(yaw) and math.isfinite(pitch)):
            raise ValueError("non-finite panorama offset")
        if not (-180.0 <= yaw <= 180.0 and -45.0 <= pitch <= 45.0):
            raise ValueError("panorama offset outside web preview limits")
        return yaw, pitch
    except (AttributeError, OSError, TypeError, ValueError, yaml.YAMLError):
        return 0.0, 0.0


@dataclass(frozen=True)
class StorageRoot:
    id: str
    label: str
    path: Path
    writable: bool = True

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.label,
            "label": self.label,
            "writable": self.writable,
        }


def _default_project_root() -> Path:
    return Path(__file__).resolve().parents[2]


@dataclass
class WebAppConfig:
    project_root: Path = field(default_factory=_default_project_root)
    state_dir: Path | None = None
    allowed_roots: Sequence[Path | str] | Mapping[str, Path | str] | None = None
    pipeline_config_path: Path | None = None
    server_name: str = "MMS 작업 서버"
    upload_relative_dir: str = "_web_uploads"
    max_upload_files: int = 100_000
    max_upload_file_bytes: int = 500 * 1024**3
    max_upload_total_bytes: int = 5 * 1024**4
    max_upload_chunk_bytes: int = 512 * 1024**2
    max_tree_entries: int = 1_000
    max_result_files: int = 2_000
    max_result_shapefiles: int = 256
    max_result_shapefile_files: int = 2_048
    max_result_priority_entries: int = 10_000
    max_route_points: int = 10_000
    max_panorama_previews: int = 2
    max_point_previews: int = 1
    max_overlay_upload_files: int = 32
    max_overlay_file_bytes: int = 512 * 1024**2
    max_overlay_total_bytes: int = 1024**3
    max_overlay_features: int = 500_000
    max_overlay_response_features: int = 10_000
    enable_run_worker: bool = True
    static_dir: Path | None = None

    def __post_init__(self) -> None:
        self.project_root = Path(self.project_root).expanduser().resolve(strict=True)
        self.state_dir = (
            Path(self.state_dir).expanduser().resolve(strict=False)
            if self.state_dir is not None
            else self.project_root / ".cache" / "webapp"
        )
        self.pipeline_config_path = (
            Path(self.pipeline_config_path).expanduser().resolve(strict=False)
            if self.pipeline_config_path is not None
            else self.project_root / "config.yaml"
        )
        self.static_dir = (
            Path(self.static_dir).expanduser().resolve(strict=False)
            if self.static_dir is not None
            else self.project_root / "webui" / "dist"
        )
        if self.allowed_roots is None:
            configured = os.environ.get("MMS_WEB_STORAGE_ROOTS", "").strip()
            if configured:
                self.allowed_roots = tuple(
                    Path(item) for item in configured.split(os.pathsep) if item.strip()
                )
            else:
                self.allowed_roots = (self.project_root / "data",)
        normalize_relative_path(self.upload_relative_dir, allow_empty=False)
        if (
            min(
                self.max_upload_files,
                self.max_upload_file_bytes,
                self.max_upload_total_bytes,
                self.max_upload_chunk_bytes,
                self.max_tree_entries,
                self.max_result_files,
                self.max_result_shapefiles,
                self.max_result_shapefile_files,
                self.max_result_priority_entries,
                self.max_route_points,
                self.max_panorama_previews,
                self.max_point_previews,
                self.max_overlay_upload_files,
                self.max_overlay_file_bytes,
                self.max_overlay_total_bytes,
                self.max_overlay_features,
                self.max_overlay_response_features,
            )
            <= 0
        ):
            raise ValueError("Web server limits must be positive.")
        if self.max_result_shapefile_files < self.max_result_shapefiles:
            raise ValueError(
                "max_result_shapefile_files must allow at least one file per SHP bundle."
            )


def _make_storage_roots(config: WebAppConfig) -> list[StorageRoot]:
    raw_roots = config.allowed_roots
    if isinstance(raw_roots, Mapping):
        items = [(str(label), Path(path)) for label, path in raw_roots.items()]
    else:
        items = [
            (Path(path).name or f"Storage {index + 1}", Path(path))
            for index, path in enumerate(raw_roots or ())
        ]
    roots: list[StorageRoot] = []
    seen: set[str] = set()
    for label, path in items:
        resolved = path.expanduser().resolve(strict=True)
        if not resolved.is_dir():
            raise NotADirectoryError(
                f"Configured storage root is not a directory: {label}"
            )
        identity = str(resolved).casefold()
        if identity in seen:
            continue
        seen.add(identity)
        roots.append(
            StorageRoot(
                id=opaque_id("root", identity, length=16),
                label=label.strip() or resolved.name or f"Storage {len(roots) + 1}",
                path=resolved,
                writable=os.access(resolved, os.W_OK),
            )
        )
    if not roots:
        raise ValueError("At least one existing storage root is required.")
    return roots


def _setup_logger(state_dir: Path) -> logging.Logger:
    log_path = state_dir / "logs" / "webapp.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"mms_webapp.{hash(str(state_dir))}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handler = logging.FileHandler(log_path, encoding="utf-8", delay=True)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    return logger


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        if request.url.path.startswith("/assets/"):
            response.headers.setdefault(
                "Cache-Control", "public, max-age=31536000, immutable"
            )
        elif (
            request.url.path == "/"
            or request.url.path.endswith("/index.html")
            or request.url.path.endswith("/vworld-map.html")
            or request.url.path.endswith("/vworld-2d-map.html")
        ):
            response.headers["Cache-Control"] = "no-cache"
        return response


class WorkerProcessLock:
    """Hold one OS-released lock for the state directory's GPU worker."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError(
                "Another MMS web worker already owns this state directory. "
                "Run exactly one ASGI worker per state directory."
            ) from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()
            self._handle = None


class SPAStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Any) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code != 404:
            return response
        # API routes are registered before this mount; retaining their 404s is
        # important for clients and prevents index.html being returned as JSON.
        if path.startswith("api/"):
            return response
        return await super().get_response("index.html", scope)


def _public_root_entry(root: StorageRoot, relative_path: str) -> dict[str, Any]:
    return {
        "root_id": root.id,
        "path": relative_path,
        "relative_path": relative_path,
        "name": Path(relative_path).name if relative_path else root.label,
    }


def _tree_payload(
    root: StorageRoot, relative_path: str, max_entries: int
) -> dict[str, Any]:
    target = resolve_under_root(
        root.path,
        relative_path,
        must_exist=True,
        expect_directory=True,
        reject_symlinks=True,
    )
    entries: list[dict[str, Any]] = []
    truncated = False
    try:
        children = sorted(
            target.iterdir(),
            key=lambda path: (not path.is_dir(), path.name.casefold(), path.name),
        )
    except OSError as exc:
        raise PermissionError("The selected folder cannot be read.") from exc
    for child in children:
        if len(entries) >= max_entries:
            truncated = True
            break
        try:
            is_link = child.is_symlink()
            kind = "directory" if child.is_dir() else "file"
            child_relative = child.relative_to(root.path).as_posix()
            stat = child.stat(follow_symlinks=False)
        except OSError:
            continue
        entries.append(
            {
                "name": child.name,
                "path": child_relative,
                "relative_path": child_relative,
                "type": kind,
                "kind": kind,
                "size": int(stat.st_size) if kind == "file" else None,
                "size_bytes": int(stat.st_size) if kind == "file" else None,
                "modified_at": datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).isoformat(),
                "selectable": kind == "directory" and not is_link,
                "symlink": is_link,
            }
        )
    parent = None
    if relative_path:
        parent = Path(relative_path).parent.as_posix()
        if parent == ".":
            parent = ""
    return {
        **_public_root_entry(root, relative_path),
        "parent": parent,
        "entries": entries,
        "items": entries,
        "truncated": truncated,
    }


def create_app(
    config: WebAppConfig | None = None,
    *,
    allowed_roots: Sequence[Path | str] | Mapping[str, Path | str] | None = None,
    storage_roots: Sequence[Path | str] | Mapping[str, Path | str] | None = None,
    state_dir: Path | str | None = None,
    project_root: Path | str | None = None,
    start_runner: bool | None = None,
) -> FastAPI:
    """Create a testable ASGI app with explicit filesystem authority.

    Keyword overrides are provided for concise tests.  Production callers should
    generally construct ``WebAppConfig`` and pass it as the sole argument.
    """

    if config is not None and any(
        value is not None
        for value in (
            allowed_roots,
            storage_roots,
            state_dir,
            project_root,
            start_runner,
        )
    ):
        raise ValueError(
            "Pass either WebAppConfig or create_app keyword overrides, not both."
        )
    if allowed_roots is not None and storage_roots is not None:
        raise ValueError("allowed_roots and storage_roots are aliases; pass only one.")
    if config is None:
        config = WebAppConfig(
            project_root=Path(project_root)
            if project_root is not None
            else _default_project_root(),
            state_dir=Path(state_dir) if state_dir is not None else None,
            allowed_roots=allowed_roots if allowed_roots is not None else storage_roots,
            enable_run_worker=True if start_runner is None else bool(start_runner),
        )

    config.state_dir.mkdir(parents=True, exist_ok=True)
    roots = _make_storage_roots(config)
    writable_roots = [root for root in roots if root.writable]
    if not writable_roots:
        raise ValueError(
            "At least one storage root must be writable for server uploads."
        )
    store = WebStore(config.state_dir / "registry.sqlite3")
    logger = _setup_logger(config.state_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker_lock_acquired = False
        try:
            if config.enable_run_worker:
                app.state.worker_process_lock.acquire()
                worker_lock_acquired = True
                recovered = app.state.run_manager.recover_after_restart(utc_now())
                if recovered:
                    logger.warning(
                        "Recovered %d interrupted run(s) after server restart.",
                        recovered,
                    )
                app.state.run_manager.start()
            yield
        finally:
            try:
                await app.state.run_manager.stop()
                # A cancelled HTTP completion request leaves an owner task that
                # must finish its atomic move and registry commit.  Drain those
                # owners before shutdown closes resources or the event loop.
                upload_owners = list(app.state.upload_owner_tasks)
                if upload_owners:
                    await asyncio.gather(*upload_owners, return_exceptions=True)
                # Preview worker threads cannot be force-cancelled safely.  Let
                # active resize/read/atomic-write owners finish before closing
                # the shared point reader and log handlers.
                media_owners = list(app.state.media_owner_tasks)
                if media_owners:
                    await asyncio.gather(*media_owners, return_exceptions=True)
                tasks = [
                    *app.state.scan_tasks.values(),
                    *app.state.catalog_tasks.values(),
                    *app.state.address_inflight.values(),
                ]
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                if app.state.point_reader is not None:
                    app.state.point_reader.close()
                for handler in list(app.state.logger.handlers):
                    app.state.logger.removeHandler(handler)
                    handler.close()
            finally:
                if worker_lock_acquired:
                    app.state.worker_process_lock.release()

    app = FastAPI(
        title="MMS Web Workspace API",
        version=SERVER_VERSION,
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.storage_roots = roots
    app.state.storage_roots_by_id = {root.id: root for root in roots}
    app.state.upload_root = writable_roots[0]
    app.state.store = store
    app.state.logger = logger
    app.state.scan_tasks = {}
    app.state.catalog_tasks = {}
    app.state.catalogs = {}
    # Fingerprint/file locks disappear once no request references them, so a
    # long-running server does not retain one lock per preview or upload chunk.
    app.state.media_locks = weakref.WeakValueDictionary()
    app.state.overlay_locks = weakref.WeakValueDictionary()
    app.state.upload_locks = weakref.WeakValueDictionary()
    app.state.upload_coordinators = weakref.WeakValueDictionary()
    app.state.upload_owner_tasks = set()
    app.state.media_owner_tasks = set()
    app.state.panorama_semaphore = asyncio.Semaphore(config.max_panorama_previews)
    app.state.point_preview_semaphore = asyncio.Semaphore(config.max_point_previews)
    app.state.run_archive_semaphore = asyncio.Semaphore(2)
    app.state.address_semaphore = asyncio.Semaphore(2)
    app.state.address_failure_cache = {}
    app.state.address_inflight = {}
    app.state.vworld_api_key = os.environ.get(
        "MMS_VWORLD_API_KEY", VWORLD_DEVELOPMENT_KEY
    ).strip()
    (
        app.state.panorama_yaw_offset_deg,
        app.state.panorama_pitch_offset_deg,
    ) = _panorama_alignment_defaults(config.pipeline_config_path)
    app.state.worker_process_lock = WorkerProcessLock(config.state_dir / "worker.lock")
    try:
        from mms_shp_detection.pointcloud import PointCloudReaderCache

        app.state.point_reader = PointCloudReaderCache()
        app.state.point_preview_available = True
    except ImportError:
        app.state.point_reader = None
        app.state.point_preview_available = False
    app.state.run_manager = RunManager(app)

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
    app.include_router(datasets_router)
    app.include_router(detections_router)
    app.include_router(media_router)
    app.include_router(overlays_router)
    app.include_router(optimizer_router)
    app.include_router(uploads_router)
    app.include_router(runs_router)
    app.include_router(surveys_router)

    @app.get("/api/health", tags=["system"])
    async def health() -> dict[str, Any]:
        return {
            "status": "ok" if store.ping() else "error",
            "api_version": API_VERSION,
            "version": SERVER_VERSION,
            "queue": {
                "active_run_id": app.state.run_manager._active_run_id,
                "worker_enabled": config.enable_run_worker,
            },
        }

    @app.get("/api/storage", tags=["storage"])
    async def storage() -> dict[str, Any]:
        public = [root.public() for root in roots]
        return {"items": public, "roots": public, "storage_roots": public}

    @app.get("/api/storage/{root_id}/tree", tags=["storage"])
    async def storage_tree(
        root_id: str,
        path: str = Query(""),
    ) -> dict[str, Any]:
        root = app.state.storage_roots_by_id.get(root_id)
        if root is None:
            raise HTTPException(status_code=404, detail="Storage root not found.")
        try:
            normalized = normalize_relative_path(path)
            return await asyncio.to_thread(
                _tree_payload, root, normalized, config.max_tree_entries
            )
        except (UnsafePath, FileNotFoundError, NotADirectoryError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.get("/api/bootstrap", tags=["system"])
    async def bootstrap() -> dict[str, Any]:
        datasets = [public_dataset(item) for item in store.list_datasets(limit=100)]
        recent_runs = [
            public_run(app, item, include_log=False)
            for item in store.list_runs(limit=20)
        ]
        public_roots = [root.public() for root in roots]
        return {
            "api_version": API_VERSION,
            "server_name": config.server_name,
            "map": {
                "provider": "vworld",
                "engine": "webgl",
                "version": "3.0",
            },
            "preview_defaults": {
                "panorama_point_yaw_offset_deg": app.state.panorama_yaw_offset_deg,
                "panorama_point_pitch_offset_deg": app.state.panorama_pitch_offset_deg,
                "panorama_point_budget": 30_000,
                "panorama_point_radius_m": 30.0,
                "panorama_point_cell_size_px": 3,
            },
            "storage_roots": public_roots,
            "datasets": datasets,
            "recent_runs": recent_runs,
            "runs": recent_runs,
            "capabilities": {
                "upload": True,
                "panorama": True,
                "point_cloud": app.state.point_preview_available,
                "auto_optimize": True,
                "folder_browser": True,
                "resumable_uploads": True,
                "panorama_preview": True,
                "point_preview": app.state.point_preview_available,
                "panorama_point_overlay": app.state.point_preview_available,
                "shp_overlays": True,
                "shp_feature_editing": True,
                "shp_result_download": True,
                "automatic_parameters": True,
                "run_sse": True,
                "single_gpu_queue": True,
                "crs_required": False,
                "crs_auto_detection": True,
                "max_panorama_width": 8192,
                "max_point_budget": POINT_PREVIEW_MAX_BUDGET,
                "upload_chunk_bytes": config.max_upload_chunk_bytes,
                "max_overlay_upload_bytes": config.max_overlay_total_bytes,
                "max_overlay_features": config.max_overlay_features,
            },
        }

    if config.static_dir.is_dir() and (config.static_dir / "index.html").is_file():
        app.mount(
            "/",
            SPAStaticFiles(directory=str(config.static_dir), html=True),
            name="web-ui",
        )

    return app
