"""Run the SentinelBank web app.

    python run_web.py                 # http://127.0.0.1:5000
    python run_web.py --port 8080

One process serves the customer portal, the analyst console and the ingestion endpoint,
so there is a single thing to start on stage. `api/main.py` (FastAPI) still exists and
still works -- it is the separated-service story -- but the demo does not need it running.
"""

from __future__ import annotations

import argparse

from web import create_app

app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the SentinelBank web app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--debug", action="store_true",
                        help="Reloader on. Leave it off for the demo -- a reload mid-beat "
                             "drops the session and the chat history with it.")
    args = parser.parse_args()

    # ASCII only. The default Windows console is cp1252 and a stray arrow in this banner
    # crashes the launch before Flask ever binds -- which is exactly the machine this runs
    # on during the demo.
    print(f"\n  SentinelBank -> http://{args.host}:{args.port}\n"
          f"  sign in with  customer / analyst / admin   (password: demo)\n")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
