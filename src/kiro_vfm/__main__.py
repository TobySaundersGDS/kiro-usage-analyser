"""Console entrypoint: ``uv run kiro-vfm``.

Starts a uvicorn server bound to 127.0.0.1 by default. Supports a ``--check``
flag that constructs the app and exits, acting as a startup smoke test without
opening a socket.
"""

from __future__ import annotations

import argparse
import sys

from .app import create_app
from .config import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kiro-vfm")
    parser.add_argument("--host", default=None, help="bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="bind port (default 8000)")
    parser.add_argument(
        "--check",
        action="store_true",
        help="construct the app and exit (startup smoke test)",
    )
    args = parser.parse_args(argv)

    base = Settings.from_env()
    settings = Settings(
        host=args.host or base.host,
        port=args.port or base.port,
        session_ttl_seconds=base.session_ttl_seconds,
        limits=base.limits,
    )

    app = create_app(settings)

    if args.check:
        # Build succeeded; report and exit without binding a socket.
        print(f"kiro-vfm OK (would bind {settings.host}:{settings.port})")
        return 0

    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
