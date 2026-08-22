from __future__ import annotations

import uvicorn


def run_api() -> None:
    uvicorn.run(
        "pilot_staging.composition:create_app",
        factory=True,
        host="0.0.0.0",
        port=8090,
        proxy_headers=False,
        server_header=False,
        access_log=False,
    )


if __name__ == "__main__":
    run_api()
