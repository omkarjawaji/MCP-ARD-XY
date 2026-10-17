"""CLI: `python -m mcp_gateway serve|init-clients`."""

from __future__ import annotations

import argparse
import sys

from starlette.middleware import Middleware as ASGIMiddleware

from .config import load_settings
from .gateway import build_server
from .security import AuthRateLimitMiddleware, load_clients, write_example_clients


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="mcp-gateway")
    sub = ap.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="run the gateway")
    serve.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    serve.add_argument("--discovery", choices=["flat", "meta"], help="tool exposure mode (env: GATEWAY_DISCOVERY)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--no-auth", action="store_true", help="HTTP without bearer auth (local testing only)")

    sub.add_parser("init-clients", help="write a clients.json with freshly generated API keys")

    args = ap.parse_args(argv)
    settings = load_settings(getattr(args, "discovery", None))

    if args.cmd == "init-clients":
        write_example_clients(settings.clients_file)
        print(f"Wrote {settings.clients_file} (keys are inside; the file is gitignored).")
        return

    mcp = build_server(settings)
    if args.transport == "stdio":
        mcp.run(transport="stdio", show_banner=False)
        return

    middleware: list[ASGIMiddleware] = []
    if args.no_auth:
        print("WARNING: running HTTP without authentication or rate limiting.", file=sys.stderr)
    elif not settings.clients_file.exists():
        sys.exit(f"{settings.clients_file} not found. Run `init-clients` first, or pass --no-auth.")
    else:
        middleware.append(ASGIMiddleware(AuthRateLimitMiddleware, clients=load_clients(settings.clients_file)))
    mcp.run(transport="http", host=args.host, port=args.port, middleware=middleware, show_banner=False)


if __name__ == "__main__":
    main()
