from __future__ import annotations

import uvicorn


def run_api() -> None:
    uvicorn.run(
        "researchops_service.composition:create_app",
        factory=True,
        host="0.0.0.0",
        port=8080,
        proxy_headers=False,
        server_header=False,
    )


if __name__ == "__main__":
    run_api()
