from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mms_shp_detection.webapp import WebAppConfig, create_app


def is_loopback_bind(host: str) -> bool:
    """Return whether *host* limits the listener to the local machine."""

    normalized = host.strip().rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the MMS browser, preview, upload, and processing API."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--ssl-certfile",
        type=Path,
        help="PEM certificate chain used to serve HTTPS directly.",
    )
    parser.add_argument(
        "--ssl-keyfile",
        type=Path,
        help="PEM private key used to serve HTTPS directly.",
    )
    parser.add_argument(
        "--storage-root",
        action="append",
        type=Path,
        dest="storage_roots",
        help="Allowed server folder. Repeat to expose more than one root.",
    )
    parser.add_argument(
        "--state-dir", type=Path, default=PROJECT_ROOT / ".cache" / "webapp"
    )
    parser.add_argument("--no-run-worker", action="store_true")
    parser.add_argument("--reload", action="store_true")
    parser.add_argument(
        "--allow-remote-bind",
        action="store_true",
        help=(
            "Acknowledge that a non-loopback listener is protected by a firewall "
            "and an authenticated TLS reverse proxy. The app has no built-in login."
        ),
    )
    parser.add_argument(
        "--auth-username",
        default=os.environ.get("MMS_WEB_USERNAME"),
        help="Require HTTP Basic authentication with this username.",
    )
    parser.add_argument(
        "--auth-password-env",
        default="MMS_WEB_PASSWORD",
        help="Environment variable containing the HTTP Basic password.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if (args.ssl_certfile is None) != (args.ssl_keyfile is None):
        parser.error("--ssl-certfile and --ssl-keyfile must be provided together")
    for option, path in (
        ("--ssl-certfile", args.ssl_certfile),
        ("--ssl-keyfile", args.ssl_keyfile),
    ):
        if path is not None and not path.is_file():
            parser.error(f"{option} does not exist or is not a file: {path}")
    auth_password = os.environ.get(args.auth_password_env)
    if (args.auth_username is None) != (auth_password is None):
        parser.error(
            "authentication requires --auth-username (or MMS_WEB_USERNAME) and "
            f"the {args.auth_password_env} environment variable"
        )
    if not is_loopback_bind(args.host) and not args.allow_remote_bind:
        parser.error(
            "refusing a non-loopback listener because the app has no built-in "
            "authentication; keep --host 127.0.0.1 behind an authenticated reverse "
            "proxy, or pass --allow-remote-bind only after network access is protected"
        )
    config = WebAppConfig(
        project_root=PROJECT_ROOT,
        state_dir=args.state_dir,
        # ``None`` lets WebAppConfig honor MMS_WEB_STORAGE_ROOTS before its
        # project/data fallback. Explicit --storage-root values still win.
        allowed_roots=args.storage_roots or None,
        enable_run_worker=not args.no_run_worker,
        auth_username=args.auth_username,
        auth_password=auth_password,
    )
    app = create_app(config)
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit(
            "uvicorn is required. Run scripts/setup_web.ps1 (Windows) or "
            "scripts/setup_web.sh (Linux) first."
        ) from exc
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=[str(PROJECT_ROOT)] if args.reload else None,
        ssl_certfile=str(args.ssl_certfile) if args.ssl_certfile else None,
        ssl_keyfile=str(args.ssl_keyfile) if args.ssl_keyfile else None,
    )


if __name__ == "__main__":
    main()
