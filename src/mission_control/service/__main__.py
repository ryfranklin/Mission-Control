"""Run the service locally: ``python -m mission_control.service``.

v1 auth model = **bind to localhost, no auth**. Do not expose this port beyond
the loopback interface without adding authentication first.

    python -m mission_control.service              # StubWorker (offline, deterministic $)
    MC_SERVICE_SDK=1 python -m mission_control.service   # real SdkWorker (real $)
"""

from __future__ import annotations

import os

import uvicorn

from . import build_default_manager, create_app

HOST = "127.0.0.1"  # loopback only — the v1 security boundary
PORT = int(os.environ.get("MC_SERVICE_PORT", "8000"))


def main() -> None:
    manager, plan_manager, builder, pool = build_default_manager(
        use_sdk=os.environ.get("MC_SERVICE_SDK") == "1"
    )
    app = create_app(manager, plan_manager, builder)
    try:
        uvicorn.run(app, host=HOST, port=PORT)
    finally:
        pool.close()


if __name__ == "__main__":
    main()
