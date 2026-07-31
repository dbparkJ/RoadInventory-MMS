from __future__ import annotations

import asyncio
import errno
import os
import re
import shutil
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Coroutine, TypeVar
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .datasets import utc_now
from .security import (
    UnsafePath,
    normalize_relative_path,
    opaque_id,
    resolve_under_root,
    safe_upload_name,
)


router = APIRouter(prefix="/api", tags=["uploads"])
T = TypeVar("T")


async def _finish_upload_owner_after_request_cancel(
    work: Coroutine[Any, Any, T],
    *,
    owner_tasks: set[asyncio.Task[Any]],
    logger: Any,
    context: str,
) -> T:
    """Let an upload owner finish after its HTTP request is cancelled."""

    task = asyncio.create_task(work)
    request_cancelled = False
    owner_tasks.add(task)

    def release_owner(completed: asyncio.Task[T]) -> None:
        owner_tasks.discard(completed)
        if not request_cancelled:
            return
        try:
            completed.result()
        except (asyncio.CancelledError, HTTPException):
            pass
        except Exception:
            logger.exception("%s failed after its request was cancelled.", context)

    task.add_done_callback(release_owner)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        request_cancelled = True
        raise


async def _drain_worker_before_cancelling(
    work: Coroutine[Any, Any, T],
) -> T:
    """Delay cancellation cleanup until non-cancellable thread work finishes."""

    task = asyncio.create_task(work)
    cancellation_requested = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancellation_requested = True
        except BaseException as exc:
            if cancellation_requested:
                raise asyncio.CancelledError() from exc
            raise

    if task.cancelled():
        raise asyncio.CancelledError()
    error = task.exception()
    if cancellation_requested:
        raise asyncio.CancelledError() from error
    if error is not None:
        raise error
    return task.result()


class UploadCoordinator:
    """Allow parallel files while completion waits for every active chunk."""

    def __init__(self) -> None:
        self.condition = asyncio.Condition()
        self.finalize_lock = asyncio.Lock()
        self.active_chunks = 0
        self.finalizing = False

    async def begin_chunk(self) -> None:
        async with self.condition:
            if self.finalizing:
                raise HTTPException(
                    status_code=409,
                    detail="Upload finalization is already in progress.",
                )
            self.active_chunks += 1

    async def end_chunk(self) -> None:
        async with self.condition:
            self.active_chunks = max(0, self.active_chunks - 1)
            if self.active_chunks == 0:
                self.condition.notify_all()

    async def begin_finalizing(self) -> None:
        async with self.condition:
            self.finalizing = True
            while self.active_chunks:
                await self.condition.wait()

    async def end_finalizing(self) -> None:
        async with self.condition:
            self.finalizing = False
            self.condition.notify_all()


def _upload_coordinator(request: Request, upload_id: str) -> UploadCoordinator:
    coordinator = request.app.state.upload_coordinators.get(upload_id)
    if coordinator is None:
        coordinator = UploadCoordinator()
        request.app.state.upload_coordinators[upload_id] = coordinator
    return coordinator


class UploadFileManifest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str
    size: int = Field(ge=0)
    type: str | None = None
    last_modified: int | str | None = None

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        try:
            return normalize_relative_path(value, allow_empty=False)
        except UnsafePath as exc:
            raise ValueError(str(exc)) from exc


class UploadManifest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    files: list[UploadFileManifest]
    root_id: str | None = None


def _public_upload(
    upload: dict[str, Any],
    files: list[dict[str, Any]],
    *,
    chunk_size: int | None = None,
) -> dict[str, Any]:
    return {
        "upload_id": upload["id"],
        "id": upload["id"],
        "name": upload["name"],
        "status": upload["status"],
        "total_size": int(upload["total_size"]),
        "chunk_size": int(chunk_size or 64 * 1024**2),
        "uploaded_size": sum(int(item["offset"]) for item in files),
        "files": [
            {
                "id": item["id"],
                "path": item["relative_path"],
                "size": int(item["size"]),
                "offset": int(item["offset"]),
                "uploaded_bytes": int(item["offset"]),
                "url": f"/api/uploads/{upload['id']}/files/{item['id']}",
            }
            for item in files
        ],
    }


def _staging_upload_dir(app: Any, upload_id: str) -> Path:
    return app.state.config.state_dir / "upload-staging" / upload_id


def _parse_content_range(value: str | None) -> tuple[int, int, int] | None:
    if not value:
        return None
    match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+)", value.strip(), re.IGNORECASE)
    if match is None:
        raise ValueError("Content-Range must use bytes START-END/TOTAL.")
    start, end, total = (int(match.group(index)) for index in range(1, 4))
    if end < start:
        raise ValueError("Content-Range end precedes its start.")
    return start, end, total


@router.post("/uploads", status_code=status.HTTP_201_CREATED)
async def create_upload(payload: UploadManifest, request: Request) -> dict[str, Any]:
    config = request.app.state.config
    if not payload.files:
        raise HTTPException(status_code=422, detail="Upload manifest has no files.")
    if len(payload.files) > config.max_upload_files:
        raise HTTPException(
            status_code=413,
            detail=f"Upload manifest exceeds {config.max_upload_files} files.",
        )
    try:
        safe_name = safe_upload_name(payload.name)
    except UnsafePath as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    relative_paths = [item.path for item in payload.files]
    if len(set(path.casefold() for path in relative_paths)) != len(relative_paths):
        raise HTTPException(status_code=422, detail="Upload manifest contains duplicate paths.")
    total_size = sum(item.size for item in payload.files)
    if any(item.size > config.max_upload_file_bytes for item in payload.files):
        raise HTTPException(status_code=413, detail="An upload file exceeds the server limit.")
    if total_size > config.max_upload_total_bytes:
        raise HTTPException(status_code=413, detail="Upload exceeds the server total-size limit.")

    upload_id = f"u_{uuid.uuid4().hex}"
    root = (
        request.app.state.storage_roots_by_id.get(payload.root_id)
        if payload.root_id
        else request.app.state.upload_root
    )
    if root is None or not root.writable:
        raise HTTPException(status_code=422, detail="Selected upload storage is unavailable.")
    now = utc_now()
    upload = {
        "id": upload_id,
        "name": payload.name.strip(),
        "safe_name": safe_name,
        "root_id": root.id,
        "total_size": total_size,
        "created_at": now,
        "updated_at": now,
    }
    files = [
        {
            "id": opaque_id("uf", upload_id, item.path.casefold()),
            "relative_path": item.path,
            "size": item.size,
            "last_modified": (
                int(item.last_modified)
                if isinstance(item.last_modified, (int, float))
                else None
            ),
            "offset": 0,
        }
        for item in payload.files
    ]
    staging = _staging_upload_dir(request.app, upload_id)
    staging.mkdir(parents=True, exist_ok=False)
    (staging / "files").mkdir()
    try:
        request.app.state.store.create_upload(upload, files)
        for item in files:
            if int(item["size"]) != 0:
                continue
            empty_path = resolve_under_root(
                staging / "files",
                item["relative_path"],
                must_exist=False,
                expect_directory=False,
                reject_symlinks=True,
            )
            empty_path.parent.mkdir(parents=True, exist_ok=True)
            empty_path.touch(exist_ok=False)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    stored = request.app.state.store.get_upload(upload_id)
    return _public_upload(
        stored,
        files,
        chunk_size=min(
            request.app.state.config.max_upload_chunk_bytes,
            16 * 1024**2,
        ),
    )  # type: ignore[arg-type]


def _require_upload_file(
    request: Request, upload_id: str, file_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    upload = request.app.state.store.get_upload(upload_id)
    if upload is None:
        raise HTTPException(status_code=404, detail="Upload not found.")
    file_item = request.app.state.store.get_upload_file(upload_id, file_id)
    if file_item is None:
        raise HTTPException(status_code=404, detail="Upload file not found.")
    return upload, file_item


@router.head("/uploads/{upload_id}/files/{file_id}")
async def upload_head(upload_id: str, file_id: str, request: Request) -> Response:
    upload, item = _require_upload_file(request, upload_id, file_id)
    return Response(
        status_code=204,
        headers={
            "Upload-Offset": str(item["offset"]),
            "Upload-Length": str(item["size"]),
            "Upload-Status": upload["status"],
            "Cache-Control": "no-store",
        },
    )


async def _write_upload_stream(
    request: Request,
    target: Path,
    *,
    start_offset: int,
    maximum_bytes: int,
) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.is_symlink():
        raise UnsafePath("Symbolic links are not allowed in upload staging.")
    mode = "r+b" if target.exists() else "w+b"
    written = 0
    with target.open(mode) as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() != start_offset:
            raise RuntimeError("Staged file length does not match its saved upload offset.")
        handle.seek(start_offset)
        try:
            async for chunk in request.stream():
                if not chunk:
                    continue
                if written + len(chunk) > maximum_bytes:
                    raise OverflowError("Chunk exceeds its declared or permitted length.")
                handle.write(chunk)
                written += len(chunk)
            handle.flush()
            await _drain_worker_before_cancelling(
                asyncio.to_thread(os.fsync, handle.fileno())
            )
        except BaseException:
            handle.truncate(start_offset)
            raise
    return written


@router.put("/uploads/{upload_id}/files/{file_id}")
async def upload_chunk(
    upload_id: str,
    file_id: str,
    request: Request,
) -> Response:
    coordinator = _upload_coordinator(request, upload_id)
    await coordinator.begin_chunk()
    lock_key = f"upload:{upload_id}:file:{file_id}"
    lock = request.app.state.upload_locks.setdefault(lock_key, asyncio.Lock())
    try:
        async with lock:
            upload, item = _require_upload_file(request, upload_id, file_id)
            if upload["status"] != "uploading":
                raise HTTPException(status_code=409, detail="Upload is not accepting chunks.")
            try:
                header_offset = request.headers.get("upload-offset")
                content_range = _parse_content_range(request.headers.get("content-range"))
                if header_offset is not None:
                    requested_offset = int(header_offset)
                elif content_range is not None:
                    requested_offset = content_range[0]
                else:
                    raise ValueError("Upload-Offset or Content-Range is required.")
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            current_offset = int(item["offset"])
            if requested_offset != current_offset:
                return Response(
                    status_code=409,
                    headers={
                        "Upload-Offset": str(current_offset),
                        "Cache-Control": "no-store",
                    },
                )
            if content_range is not None:
                start, end, total = content_range
                if start != current_offset or total != int(item["size"]):
                    raise HTTPException(
                        status_code=409,
                        detail="Content-Range does not match manifest.",
                    )
                declared_bytes = end - start + 1
            else:
                content_length = request.headers.get("content-length")
                try:
                    declared_bytes = (
                        int(content_length)
                        if content_length is not None
                        else min(
                            request.app.state.config.max_upload_chunk_bytes,
                            int(item["size"]) - current_offset,
                        )
                    )
                except ValueError as exc:
                    raise HTTPException(
                        status_code=400, detail="Invalid Content-Length."
                    ) from exc
            relative_header = request.headers.get("x-relative-path")
            if relative_header is not None:
                try:
                    supplied_relative = normalize_relative_path(
                        unquote(relative_header), allow_empty=False
                    )
                except UnsafePath as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
                if supplied_relative != item["relative_path"]:
                    raise HTTPException(
                        status_code=409,
                        detail="X-Relative-Path does not match the upload manifest.",
                    )
            remaining = int(item["size"]) - current_offset
            if declared_bytes < 0 or declared_bytes > remaining:
                raise HTTPException(
                    status_code=413, detail="Chunk exceeds the manifest file size."
                )
            if declared_bytes > request.app.state.config.max_upload_chunk_bytes:
                raise HTTPException(
                    status_code=413, detail="Chunk exceeds the server chunk limit."
                )

            staging_files = _staging_upload_dir(request.app, upload_id) / "files"
            try:
                target = resolve_under_root(
                    staging_files,
                    item["relative_path"],
                    must_exist=False,
                    expect_directory=False,
                    reject_symlinks=True,
                )
                written = await _write_upload_stream(
                    request,
                    target,
                    start_offset=current_offset,
                    maximum_bytes=declared_bytes,
                )
            except OverflowError as exc:
                raise HTTPException(status_code=413, detail=str(exc)) from exc
            except (UnsafePath, OSError, RuntimeError) as exc:
                request.app.state.logger.warning(
                    "Rejected upload chunk for %s/%s: %s", upload_id, file_id, exc
                )
                raise HTTPException(
                    status_code=409,
                    detail="Upload staging state is inconsistent.",
                ) from exc
            if content_range is not None and written != declared_bytes:
                with target.open("r+b") as handle:
                    handle.truncate(current_offset)
                raise HTTPException(
                    status_code=400,
                    detail="Chunk length does not match Content-Range.",
                )
            new_offset = current_offset + written
            updated = request.app.state.store.update_upload_offset(
                upload_id, file_id, current_offset, new_offset, utc_now()
            )
            if not updated:
                with target.open("r+b") as handle:
                    handle.truncate(current_offset)
                raise HTTPException(
                    status_code=409, detail="Upload offset changed concurrently."
                )
            return Response(
                status_code=204,
                headers={
                    "Upload-Offset": str(new_offset),
                    "Upload-Length": str(item["size"]),
                    "Cache-Control": "no-store",
                },
            )
    finally:
        await coordinator.end_chunk()


def _validate_upload_tree(upload_tree: Path, files: list[dict[str, Any]]) -> None:
    if upload_tree.is_symlink() or bool(
        getattr(upload_tree, "is_junction", lambda: False)()
    ):
        raise UnsafePath("An upload tree cannot be a symbolic link or junction.")

    expected_files = {str(item["relative_path"]) for item in files}
    expected_directories: set[str] = set()
    for relative_path in expected_files:
        parts = PurePosixPath(relative_path).parts
        expected_directories.update(
            PurePosixPath(*parts[:index]).as_posix()
            for index in range(1, len(parts))
        )

    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    for path in upload_tree.rglob("*"):
        if path.is_symlink() or bool(
            getattr(path, "is_junction", lambda: False)()
        ):
            raise UnsafePath("Symbolic links and junctions are not allowed in uploads.")
        relative_path = path.relative_to(upload_tree).as_posix()
        if path.is_dir():
            actual_directories.add(relative_path)
        elif path.is_file():
            actual_files.add(relative_path)
        else:
            raise UnsafePath("Uploads may contain only regular files and directories.")
    if (
        actual_files != expected_files
        or actual_directories != expected_directories
    ):
        raise ValueError("An upload tree does not match its manifest.")

    for item in files:
        path = resolve_under_root(
            upload_tree,
            item["relative_path"],
            must_exist=True,
            expect_directory=False,
            reject_symlinks=True,
        )
        if path.stat().st_size != int(item["size"]):
            raise ValueError("An upload file size does not match its manifest.")


def _move_completed_upload(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, destination)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        # Cross-volume server layouts need a copy followed by removal.  copytree
        # preserves no symlinks because staging validation rejects all of them.
        temporary = destination.with_name(f".{destination.name}.partial")
        if temporary.exists():
            raise FileExistsError("A partial upload destination already exists.")
        try:
            shutil.copytree(source, temporary, symlinks=False)
            os.replace(temporary, destination)
            shutil.rmtree(source)
        except BaseException:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
            raise


async def _complete_upload_owned(
    upload_id: str,
    request: Request,
    coordinator: UploadCoordinator,
) -> dict[str, Any]:
    async with coordinator.finalize_lock:
        upload = request.app.state.store.get_upload(upload_id)
        if upload is None:
            raise HTTPException(status_code=404, detail="Upload not found.")
        if upload["status"] == "complete":
            return {
                "upload_id": upload_id,
                "root_id": upload["root_id"],
                "relative_path": upload["destination_relative_path"],
            }
        if upload["status"] != "uploading":
            raise HTTPException(status_code=409, detail="Upload cannot be completed.")
        await coordinator.begin_finalizing()
        completed = False
        try:
            # Re-read after active chunk writers have drained.
            upload = request.app.state.store.get_upload(upload_id)
            files = request.app.state.store.list_upload_files(upload_id)
            incomplete = [
                item for item in files if int(item["offset"]) != int(item["size"])
            ]
            if incomplete:
                raise HTTPException(
                    status_code=409,
                    detail=f"{len(incomplete)} upload file(s) are incomplete.",
                )
            staging = _staging_upload_dir(request.app, upload_id)
            staging_files = staging / "files"
            destination_relative = (
                f"{request.app.state.config.upload_relative_dir}/"
                f"{upload['safe_name']}-{upload_id[-8:]}"
            )
            try:
                destination_root = request.app.state.storage_roots_by_id.get(
                    upload["root_id"]
                )
                if destination_root is None or not destination_root.writable:
                    raise UnsafePath("Upload storage is no longer available.")
                destination = resolve_under_root(
                    destination_root.path,
                    destination_relative,
                    must_exist=False,
                    expect_directory=True,
                    reject_symlinks=True,
                )
                if destination.exists():
                    staging_source_exists = staging_files.exists() or (
                        staging_files.is_symlink()
                        or bool(
                            getattr(staging_files, "is_junction", lambda: False)()
                        )
                    )
                    if staging_source_exists:
                        raise FileExistsError(
                            "Upload staging and destination both exist."
                        )
                    # A same-volume rename or a completed cross-volume copy can
                    # survive a process crash immediately before the DB commit.
                    # The opaque destination is deterministic, so an exact tree
                    # revalidation makes that narrow retry safe and idempotent.
                    await asyncio.to_thread(
                        _validate_upload_tree,
                        destination,
                        files,
                    )
                else:
                    await asyncio.to_thread(
                        _validate_upload_tree,
                        staging_files,
                        files,
                    )
                    await asyncio.to_thread(
                        _move_completed_upload, staging_files, destination
                    )
                # The now-empty generated staging folder contains no user data.
                if staging.exists():
                    staging.rmdir()
            except (UnsafePath, OSError, ValueError) as exc:
                request.app.state.logger.warning(
                    "Upload completion failed for %s: %s", upload_id, exc
                )
                raise HTTPException(
                    status_code=409,
                    detail="Upload could not be finalized safely.",
                ) from exc
            request.app.state.store.complete_upload(
                upload_id, destination_relative, utc_now()
            )
            completed = True
        finally:
            await coordinator.end_finalizing()
        if completed:
            request.app.state.upload_coordinators.pop(upload_id, None)
        return {
            "upload_id": upload_id,
            "root_id": upload["root_id"],
            "relative_path": destination_relative,
        }


@router.post("/uploads/{upload_id}/complete")
async def complete_upload(upload_id: str, request: Request) -> dict[str, Any]:
    coordinator = _upload_coordinator(request, upload_id)
    owner_tasks = getattr(request.app.state, "upload_owner_tasks", None)
    if owner_tasks is None:
        owner_tasks = set()
        request.app.state.upload_owner_tasks = owner_tasks
    return await _finish_upload_owner_after_request_cancel(
        _complete_upload_owned(upload_id, request, coordinator),
        owner_tasks=owner_tasks,
        logger=request.app.state.logger,
        context=f"Upload completion {upload_id}",
    )
