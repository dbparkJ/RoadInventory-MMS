"""Explicit offline run archive and state backup/restore; see P1_PRESERVATION.md."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mms_shp_detection.infrastructure.preservation import (
    OfflineRequired, PreservationError, archive_run, backup_state,
    restore_preservation, verify_preservation,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("archive-run", "backup-state"):
        command = commands.add_parser(name)
        command.add_argument("--source", type=Path, required=True)
        command.add_argument("--destination", type=Path, required=True)
        command.add_argument("--source-alias", required=True)
        command.add_argument("--config", action="append", required=True, help="relative configuration snapshot; repeatable")
        command.add_argument("--offline", action="store_true", help="confirm ALL web/CLI/external writers were stopped beforehand")
        command.add_argument("--offline-evidence", type=Path, required=True)
        if name == "archive-run":
            command.add_argument("--artifact", action="append", default=[], help="explicit generated output relative path")
    for name in ("restore", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--source", type=Path, required=True)
        if name == "restore":
            command.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command in {"archive-run", "backup-state"}:
            evidence = json.loads(args.offline_evidence.read_text(encoding="utf-8-sig"))
            options = dict(config_paths=args.config, offline=args.offline, evidence=evidence, source_alias=args.source_alias)
            if args.command == "archive-run":
                result = archive_run(args.source, args.destination, artifact_paths=args.artifact, **options)
            else:
                result = backup_state(args.source, args.destination, **options)
        elif args.command == "restore":
            result = restore_preservation(args.source, args.destination)
        else:
            result = verify_preservation(args.source)
    except (OfflineRequired, FileNotFoundError, PermissionError, sqlite3.OperationalError) as exc:
        print(json.dumps({"status": "BLOCKED_VALIDATION", "cause_type": type(exc).__name__,
                          "message": str(exc) if isinstance(exc, OfflineRequired) else "required offline file/lock evidence unavailable"}))
        return 2
    except (PreservationError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        print(json.dumps({"status": "FAIL", "cause_type": type(exc).__name__,
                          "message": str(exc) if isinstance(exc, PreservationError) else "preservation failed; source data retained"}))
        return 1
    print(json.dumps({"status": "PASS", "kind": result["kind"], "files": len(result["files"]),
                      "validation": result.get("validation"), "run": {key: result["run"][key] for key in ("job_id", "attempt")} if "run" in result else None},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
