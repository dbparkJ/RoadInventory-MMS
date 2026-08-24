from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

import mms_shp_detection.webapp.uploads as uploads_module
from mms_shp_detection.webapp import create_app


class WebAppUploadTests(unittest.TestCase):
    def test_upload_can_target_a_configured_external_storage_root(self) -> None:
        with (
            tempfile.TemporaryDirectory() as default_text,
            tempfile.TemporaryDirectory() as external_text,
            tempfile.TemporaryDirectory() as state_text,
        ):
            default_root = Path(default_text)
            external_root = Path(external_text)
            app = create_app(
                allowed_roots=[default_root, external_root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            external_root_id = app.state.storage_roots[1].id
            with TestClient(app) as client:
                created = client.post(
                    "/api/uploads",
                    json={
                        "name": "external-delivery",
                        "root_id": external_root_id,
                        "files": [{"path": "delivery/empty.txt", "size": 0}],
                    },
                )
                self.assertEqual(created.status_code, 201, created.text)
                completed = client.post(
                    f"/api/uploads/{created.json()['id']}/complete"
                )
                self.assertEqual(completed.status_code, 200, completed.text)
            relative_path = completed.json()["relative_path"]
            self.assertEqual(completed.json()["root_id"], external_root_id)
            self.assertTrue(
                (external_root / relative_path / "delivery/empty.txt").is_file()
            )
            self.assertFalse((default_root / relative_path).exists())

    def test_cancelled_chunk_waits_for_fsync_before_releasing_owner(self) -> None:
        with tempfile.TemporaryDirectory() as root_text, tempfile.TemporaryDirectory() as state_text:
            root = Path(root_text)
            state = Path(state_text)
            app = create_app(
                allowed_roots=[root],
                state_dir=state,
                start_runner=False,
            )
            with TestClient(app) as client:
                created = client.post(
                    "/api/uploads",
                    json={
                        "name": "cancel-fsync",
                        "files": [{"path": "a.bin", "size": 1}],
                    },
                ).json()

            upload_id = created["id"]
            file_id = created["files"][0]["id"]
            started = threading.Event()
            release = threading.Event()
            descriptor_valid_after_cancel = False
            actual_fsync = uploads_module.os.fsync

            def blocking_fsync(descriptor: int) -> None:
                nonlocal descriptor_valid_after_cancel
                started.set()
                if not release.wait(timeout=2):
                    raise TimeoutError("Test did not release fsync.")
                uploads_module.os.fstat(descriptor)
                descriptor_valid_after_cancel = True
                actual_fsync(descriptor)

            async def exercise() -> None:
                sent = False

                async def receive() -> dict[str, object]:
                    nonlocal sent
                    if sent:
                        return {"type": "http.disconnect"}
                    sent = True
                    return {
                        "type": "http.request",
                        "body": b"x",
                        "more_body": False,
                    }

                request = Request(
                    {
                        "type": "http",
                        "method": "PUT",
                        "path": "/",
                        "headers": [
                            (b"upload-offset", b"0"),
                            (b"content-range", b"bytes 0-0/1"),
                            (b"content-length", b"1"),
                        ],
                        "app": app,
                    },
                    receive,
                )
                lock_key = f"upload:{upload_id}:file:{file_id}"
                task = asyncio.create_task(
                    uploads_module.upload_chunk(upload_id, file_id, request)
                )
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                coordinator = app.state.upload_coordinators[upload_id]
                file_lock = app.state.upload_locks[lock_key]

                task.cancel()
                await asyncio.sleep(0.05)
                self.assertFalse(task.done())
                self.assertEqual(coordinator.active_chunks, 1)
                self.assertTrue(file_lock.locked())

                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=2)
                self.assertEqual(coordinator.active_chunks, 0)
                self.assertFalse(file_lock.locked())

            with mock.patch.object(
                uploads_module.os,
                "fsync",
                side_effect=blocking_fsync,
            ):
                try:
                    asyncio.run(exercise())
                finally:
                    release.set()

            staged = state / "upload-staging" / upload_id / "files" / "a.bin"
            self.assertTrue(descriptor_valid_after_cancel)
            self.assertEqual(staged.stat().st_size, 0)
            stored = app.state.store.get_upload_file(upload_id, file_id)
            self.assertEqual(int(stored["offset"]), 0)

    def test_cancelled_completion_finishes_move_and_database_commit(self) -> None:
        with tempfile.TemporaryDirectory() as root_text, tempfile.TemporaryDirectory() as state_text:
            root = Path(root_text)
            state = Path(state_text)
            app = create_app(
                allowed_roots=[root],
                state_dir=state,
                start_runner=False,
            )
            with TestClient(app) as client:
                created = client.post(
                    "/api/uploads",
                    json={
                        "name": "cancel-complete",
                        "files": [{"path": "a.bin", "size": 1}],
                    },
                ).json()
                upload_id = created["id"]
                file_id = created["files"][0]["id"]
                uploaded = client.put(
                    f"/api/uploads/{upload_id}/files/{file_id}",
                    content=b"x",
                    headers={
                        "Upload-Offset": "0",
                        "Content-Range": "bytes 0-0/1",
                    },
                )
                self.assertEqual(uploaded.status_code, 204, uploaded.text)

            started = threading.Event()
            release = threading.Event()
            actual_move = uploads_module._move_completed_upload
            actual_replace = uploads_module.os.replace
            staging_files = state / "upload-staging" / upload_id / "files"

            def blocking_move(source: Path, destination: Path) -> None:
                started.set()
                if not release.wait(timeout=2):
                    raise TimeoutError("Test did not release upload completion.")
                actual_move(source, destination)

            def cross_volume_replace(source: Path, destination: Path) -> None:
                if Path(source) == staging_files:
                    raise OSError(
                        uploads_module.errno.EXDEV,
                        "Simulated cross-volume upload move.",
                    )
                actual_replace(source, destination)

            async def exercise() -> None:
                request = Request(
                    {
                        "type": "http",
                        "method": "POST",
                        "path": "/",
                        "headers": [],
                        "app": app,
                    }
                )
                task = asyncio.create_task(
                    uploads_module.complete_upload(upload_id, request)
                )
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                coordinator = app.state.upload_coordinators[upload_id]

                task.cancel()
                try:
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertTrue(coordinator.finalizing)
                    self.assertTrue(coordinator.finalize_lock.locked())
                    self.assertEqual(len(app.state.upload_owner_tasks), 1)
                    with self.assertRaises(HTTPException) as rejected:
                        await coordinator.begin_chunk()
                    self.assertEqual(rejected.exception.status_code, 409)
                finally:
                    release.set()

                deadline = asyncio.get_running_loop().time() + 2
                while (
                    (
                        app.state.store.get_upload(upload_id)["status"] != "complete"
                        or app.state.upload_owner_tasks
                    )
                    and asyncio.get_running_loop().time() < deadline
                ):
                    await asyncio.sleep(0.01)
                stored = app.state.store.get_upload(upload_id)
                self.assertEqual(stored["status"], "complete")
                self.assertFalse(coordinator.finalizing)
                self.assertFalse(coordinator.finalize_lock.locked())
                self.assertIsNone(app.state.upload_coordinators.get(upload_id))
                self.assertFalse(app.state.upload_owner_tasks)

            with (
                mock.patch.object(
                    uploads_module,
                    "_move_completed_upload",
                    side_effect=blocking_move,
                ),
                mock.patch.object(
                    uploads_module.os,
                    "replace",
                    side_effect=cross_volume_replace,
                ),
            ):
                try:
                    asyncio.run(exercise())
                finally:
                    release.set()

            stored = app.state.store.get_upload(upload_id)
            destination = root / stored["destination_relative_path"]
            staging = state / "upload-staging" / upload_id
            self.assertEqual((destination / "a.bin").read_bytes(), b"x")
            self.assertFalse(
                destination.with_name(f".{destination.name}.partial").exists()
            )
            self.assertFalse(staging.exists())

    def test_completion_retry_recovers_moved_tree_after_database_failure(self) -> None:
        with tempfile.TemporaryDirectory() as root_text, tempfile.TemporaryDirectory() as state_text:
            root = Path(root_text)
            state = Path(state_text)
            app = create_app(
                allowed_roots=[root],
                state_dir=state,
                start_runner=False,
            )
            with TestClient(app) as client:
                created = client.post(
                    "/api/uploads",
                    json={
                        "name": "recover-complete",
                        "files": [{"path": "track/a.bin", "size": 1}],
                    },
                ).json()
                upload_id = created["id"]
                file_id = created["files"][0]["id"]
                uploaded = client.put(
                    f"/api/uploads/{upload_id}/files/{file_id}",
                    content=b"x",
                    headers={
                        "Upload-Offset": "0",
                        "Content-Range": "bytes 0-0/1",
                    },
                )
                self.assertEqual(uploaded.status_code, 204, uploaded.text)

                with (
                    mock.patch.object(
                        app.state.store,
                        "complete_upload",
                        side_effect=RuntimeError("Simulated DB update failure."),
                    ),
                    self.assertRaisesRegex(RuntimeError, "DB update failure"),
                ):
                    client.post(f"/api/uploads/{upload_id}/complete")

                stored = app.state.store.get_upload(upload_id)
                destination_relative = (
                    f"{app.state.config.upload_relative_dir}/"
                    f"{stored['safe_name']}-{upload_id[-8:]}"
                )
                destination = root / destination_relative
                staging = state / "upload-staging" / upload_id
                self.assertEqual(stored["status"], "uploading")
                self.assertEqual((destination / "track" / "a.bin").read_bytes(), b"x")
                self.assertFalse((staging / "files").exists())

                unexpected = destination / "unexpected.bin"
                unexpected.write_bytes(b"not in manifest")
                rejected = client.post(f"/api/uploads/{upload_id}/complete")
                self.assertEqual(rejected.status_code, 409, rejected.text)
                self.assertEqual(
                    app.state.store.get_upload(upload_id)["status"],
                    "uploading",
                )
                unexpected.unlink()

                recovered = client.post(f"/api/uploads/{upload_id}/complete")
                self.assertEqual(recovered.status_code, 200, recovered.text)
                self.assertEqual(
                    recovered.json()["relative_path"],
                    destination_relative,
                )
                self.assertEqual(
                    app.state.store.get_upload(upload_id)["status"],
                    "complete",
                )
                self.assertEqual(
                    (destination / "track" / "a.bin").read_bytes(),
                    b"x",
                )
                self.assertFalse(staging.exists())

    def test_different_files_upload_in_parallel_and_completion_waits(self) -> None:
        with tempfile.TemporaryDirectory() as root_text, tempfile.TemporaryDirectory() as state_text:
            root = Path(root_text)
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            active = 0
            max_active = 0
            both_started = threading.Event()

            async def delayed_write(
                _request,
                target: Path,
                *,
                start_offset: int,
                maximum_bytes: int,
            ) -> int:
                nonlocal active, max_active
                active += 1
                max_active = max(max_active, active)
                if active == 2:
                    both_started.set()
                try:
                    await asyncio.sleep(0.15)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(b"x" * maximum_bytes)
                    return maximum_bytes
                finally:
                    active -= 1

            with TestClient(app) as client:
                created = client.post(
                    "/api/uploads",
                    json={
                        "name": "parallel",
                        "files": [
                            {"path": "a.bin", "size": 1},
                            {"path": "b.bin", "size": 1},
                        ],
                    },
                ).json()
                upload_id = created["id"]

                def put_file(item: dict[str, object]):
                    return client.put(
                        f"/api/uploads/{upload_id}/files/{item['id']}",
                        content=b"x",
                        headers={
                            "Upload-Offset": "0",
                            "Content-Range": "bytes 0-0/1",
                        },
                    )

                with (
                    mock.patch(
                        "mms_shp_detection.webapp.uploads._write_upload_stream",
                        side_effect=delayed_write,
                    ),
                    ThreadPoolExecutor(max_workers=2) as executor,
                ):
                    pending = [
                        executor.submit(put_file, item)
                        for item in created["files"]
                    ]
                    self.assertTrue(both_started.wait(timeout=2))
                    completed = client.post(f"/api/uploads/{upload_id}/complete")
                    responses = [future.result(timeout=2) for future in pending]

                self.assertEqual([response.status_code for response in responses], [204, 204])
                self.assertEqual(max_active, 2)
                self.assertEqual(completed.status_code, 200, completed.text)
                destination = root / completed.json()["relative_path"]
                self.assertEqual((destination / "a.bin").read_bytes(), b"x")
                self.assertEqual((destination / "b.bin").read_bytes(), b"x")

    def test_resumable_offsets_and_safe_completion(self) -> None:
        with tempfile.TemporaryDirectory() as root_text, tempfile.TemporaryDirectory() as state_text:
            root = Path(root_text)
            app = create_app(
                allowed_roots=[root],
                state_dir=Path(state_text),
                start_runner=False,
            )
            with TestClient(app) as client:
                created = client.post(
                    "/api/uploads",
                    json={
                        "name": "survey",
                        "files": [
                            {
                                "path": "track/file.bin",
                                "size": 6,
                                "type": "application/octet-stream",
                                "last_modified": 1,
                            },
                            {
                                "path": "track/empty.txt",
                                "size": 0,
                                "type": "text/plain",
                                "last_modified": 1,
                            },
                        ],
                    },
                )
                self.assertEqual(created.status_code, 201, created.text)
                session = created.json()
                self.assertGreater(session["chunk_size"], 0)
                upload_id = session["id"]
                file_id = session["files"][0]["id"]
                url = f"/api/uploads/{upload_id}/files/{file_id}"
                self.assertEqual(client.head(url).headers["upload-offset"], "0")

                conflict = client.put(
                    url,
                    content=b"x",
                    headers={
                        "Upload-Offset": "1",
                        "Content-Range": "bytes 1-1/6",
                    },
                )
                self.assertEqual(conflict.status_code, 409)
                self.assertEqual(conflict.headers["upload-offset"], "0")

                first = client.put(
                    url,
                    content=b"abc",
                    headers={
                        "Upload-Offset": "0",
                        "Content-Range": "bytes 0-2/6",
                        "X-Relative-Path": "track%2Ffile.bin",
                    },
                )
                self.assertEqual(first.status_code, 204, first.text)
                self.assertEqual(first.headers["upload-offset"], "3")
                self.assertEqual(client.head(url).headers["upload-offset"], "3")

                second = client.put(
                    url,
                    content=b"def",
                    headers={
                        "Upload-Offset": "3",
                        "Content-Range": "bytes 3-5/6",
                        "X-Relative-Path": "track%2Ffile.bin",
                    },
                )
                self.assertEqual(second.status_code, 204, second.text)
                completed = client.post(f"/api/uploads/{upload_id}/complete")
                self.assertEqual(completed.status_code, 200, completed.text)
                relative = completed.json()["relative_path"]
                self.assertEqual((root / relative / "track" / "file.bin").read_bytes(), b"abcdef")
                self.assertEqual((root / relative / "track" / "empty.txt").read_bytes(), b"")

    def test_upload_manifest_rejects_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as root_text, tempfile.TemporaryDirectory() as state_text:
            app = create_app(
                allowed_roots=[Path(root_text)],
                state_dir=Path(state_text),
                start_runner=False,
            )
            with TestClient(app) as client:
                response = client.post(
                    "/api/uploads",
                    json={
                        "name": "unsafe",
                        "files": [{"path": "../escape.bin", "size": 1}],
                    },
                )
                self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
