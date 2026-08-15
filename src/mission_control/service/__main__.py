"""Run the service locally: ``python -m mission_control.service``.

Auth model: the default bind is loopback (the v1 boundary). A deployment that binds
a routable interface (``MC_SERVICE_HOST=0.0.0.0`` on Fargate) MUST also require the
bearer token (set ``MC_API_TOKEN``) and stay inside a private network (no public
ingress). The AWS deployment does both: VPC-internal only, reachable from the
Homebase BFF over Cloud Map, with the token enforced on every mutation.

    python -m mission_control.service              # StubWorker (offline, deterministic $)
    MC_SERVICE_SDK=1 python -m mission_control.service   # real SdkWorker (real $)
"""

from __future__ import annotations

import os

import uvicorn

from . import build_default_manager, create_app

# Loopback by default (the v1 boundary); 0.0.0.0 on Fargate, where the port is only
# reachable inside the VPC and MC_API_TOKEN is required.
HOST = os.environ.get("MC_SERVICE_HOST", "127.0.0.1")
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
