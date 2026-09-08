from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mms_shp_detection.infrastructure.process_owner import (
    OwnedExecution, OwnershipUnknown, inspect_owner, new_intent, process_identity,
)

ROOT = Path(__file__).resolve().parents[1]
PRELUDE = "from mms_shp_detection.infrastructure.process_owner import owned_child_handshake,owned_child_finish; owned=owned_child_handshake(); "


def creation_options():
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}


class ProcessOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.children = []
        self.owners = []

    async def asyncTearDown(self) -> None:
        for owner, process in self.children:
            try:
                await owner.terminate(process)
            finally:
                owner.close()
        for owner in self.owners:
            owner.close()
        self.temporary.cleanup()

    async def spawn(self, body: str, *, authorize: bool = True):
        intent = new_intent("test-run", 1)
        owner = OwnedExecution(intent, self.directory)
        self.owners.append(owner)
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", PRELUDE + body,
            cwd=ROOT, env={**os.environ, **owner.environment(), "MMS_PIPELINE_JOB_ID": "test-run"},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **creation_options(),
        )
        self.children.append((owner, process))
        acknowledged = await owner.attach(process)
        if authorize:
            owner.authorize(process)
            await process.stdin.drain()
        return owner, process, acknowledged

    async def test_self_acknowledgement_matches_os_and_private_cancel_exits(self) -> None:
        owner, process, record = await self.spawn("import time; print('started',flush=True); time.sleep(300)")
        self.assertEqual((await process.stdout.readline()).strip(), b"started")
        self.assertEqual(record["identity"], process_identity(record["identity"]["pid"]))
        self.assertEqual(inspect_owner(record), "live")
        observed = owner.observe()
        self.assertEqual(observed["identity"], record["identity"])
        self.assertIn("last_observed_alive_at", observed)
        await owner.terminate(process)
        owner.close()
        self.assertEqual(inspect_owner(record), "exited")

    async def test_no_pipeline_work_before_explicit_parent_authorization(self) -> None:
        marker = self.directory / "model-started.txt"
        owner, process, record = await self.spawn(
            f"from pathlib import Path; Path({str(marker)!r}).write_text('unsafe');", authorize=False,
        )
        await asyncio.sleep(0.1)
        self.assertFalse(marker.exists())
        process.stdin.close()  # Crash before authorization: only child acknowledgement exists.
        await asyncio.wait_for(process.wait(), 5)
        owner.close()
        self.assertFalse(marker.exists())
        self.assertEqual(inspect_owner(record), "exited")

    @unittest.skipUnless(os.name == "nt", "Windows STILL_ACTIVE exit-code distinction")
    async def test_real_exit_259_is_not_mistaken_for_a_live_process(self) -> None:
        owner, process, record = await self.spawn("import sys,time; print('ready',flush=True); time.sleep(0.1); sys.exit(259)")
        self.assertEqual((await process.stdout.readline()).strip(), b"ready")
        self.assertEqual(await asyncio.wait_for(process.wait(), 5), 259)
        self.assertIsNone(process_identity(record["identity"]["pid"]))
        owner.close()
        self.assertEqual(inspect_owner(record), "exited")

    async def test_cancel_cleans_descendant_tree(self) -> None:
        body = (
            "import subprocess,sys,time,json; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(300)']); "
            "print(json.dumps({'child':child.pid}),flush=True); time.sleep(300)"
        )
        owner, process, record = await self.spawn(body)
        child_pid = json.loads(await process.stdout.readline())["child"]
        child_identity = process_identity(child_pid)
        self.assertIsNotNone(child_identity)
        await owner.terminate(process)
        owner.close()
        for _ in range(100):
            if process_identity(child_pid) != child_identity:
                break
            await asyncio.sleep(0.05)
        self.assertNotEqual(process_identity(child_pid), child_identity)
        self.assertEqual(inspect_owner(record), "exited")

    async def test_reused_or_unavailable_pid_never_authorizes_killing(self) -> None:
        owner, process, record = await self.spawn("import time; time.sleep(300)")
        reused = {**record, "identity": {**record["identity"], "birth": "different-start"}}
        self.assertEqual(inspect_owner(reused), "reused")
        self.assertEqual(inspect_owner(new_intent("unknown", 1)), "unknown")
        with mock.patch("mms_shp_detection.infrastructure.process_owner.process_identity", side_effect=OwnershipUnknown("denied")):
            self.assertEqual(inspect_owner(record), "unknown")
        self.assertEqual(process_identity(record["identity"]["pid"]), record["identity"])

    async def test_parent_exit_closes_owned_child_and_grandchild(self) -> None:
        report = self.directory / "report.json"
        parent_code = '''import asyncio,json,os,sys
from pathlib import Path
from mms_shp_detection.infrastructure.process_owner import OwnedExecution,new_intent
async def main():
    owner=OwnedExecution(new_intent("orphan-test",1),Path(sys.argv[1]))
    opts={"creationflags":0x200|0x08000000} if os.name=="nt" else {"start_new_session":True}
    code="from mms_shp_detection.infrastructure.process_owner import owned_child_handshake; owned_child_handshake(); import subprocess,sys,time; child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(300)']); print(child.pid,flush=True); time.sleep(300)"
    process=await asyncio.create_subprocess_exec(sys.executable,"-c",code,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,env={**os.environ,**owner.environment(),"MMS_PIPELINE_JOB_ID":"orphan-test"},**opts)
    record=await owner.attach(process)
    owner.authorize(process)
    child_pid=int(await process.stdout.readline())
    Path(sys.argv[2]).write_text(json.dumps({"record":record,"grandchild":child_pid}))
    os._exit(0)
asyncio.run(main())
'''
        parent = await asyncio.create_subprocess_exec(
            sys.executable, "-c", parent_code, str(self.directory), str(report),
            cwd=ROOT, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            **creation_options(),
        )
        await asyncio.wait_for(parent.wait(), 15)
        self.assertEqual(parent.returncode, 0, (await parent.stderr.read()).decode())
        report_value = json.loads(report.read_text())
        record = report_value["record"]
        for _ in range(100):
            if inspect_owner(record) == "exited":
                break
            await asyncio.sleep(0.05)
        self.assertEqual(inspect_owner(record), "exited")
        self.assertIsNone(process_identity(report_value["grandchild"]))


if __name__ == "__main__":
    unittest.main()
