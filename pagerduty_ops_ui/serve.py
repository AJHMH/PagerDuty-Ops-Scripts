"""`pd-ui`: run the service on localhost behind IIS (ADR 0008).

IIS terminates TLS and proxies to this process, which listens only on 127.0.0.1 and runs as
a Windows Service under a least-privilege account (packaging is #71).
"""

import os

import uvicorn

from .app import create_app
from .settings import Settings


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(
        create_app(settings),
        host="127.0.0.1",
        port=int(os.environ.get("PD_UI_PORT", "8080")),
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )


if __name__ == "__main__":
    main()
