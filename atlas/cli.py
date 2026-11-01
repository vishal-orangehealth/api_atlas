"""Command line entry point: `atlas`, `atlas demo`."""
from __future__ import annotations

import argparse
import threading
import webbrowser


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="atlas", description="API Atlas: explore an app, get API tests.")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="start API Atlas (default)")
    serve.add_argument("--port", type=int, default=7801)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--no-open", action="store_true", help="don't open the browser")

    demo = sub.add_parser("demo", help="start the Atlas Shop demo app to try it on")
    demo.add_argument("--port", type=int, default=8765)

    args = parser.parse_args(argv)
    import uvicorn

    if args.command == "demo":
        print(f"Atlas Shop demo running at http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
        from .demo.app import app as demo_app
        uvicorn.run(demo_app, host="127.0.0.1", port=args.port, log_level="warning")
        return

    port = getattr(args, "port", 7801)
    host = getattr(args, "host", "127.0.0.1")
    url = f"http://{host}:{port}"
    print(f"API Atlas running at {url}  (Ctrl+C to stop)")
    if not getattr(args, "no_open", False):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    from .server import create_app
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
