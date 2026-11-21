"""`pd-ui`: run the service on localhost behind IIS (ADR 0008).

IIS terminates TLS and proxies to this process, which listens only on 127.0.0.1 and runs as
a Windows Service under a least-privilege account (packaging is #71).
"""

import os

import uvicorn

from .app import create_app
from .migrate import SchemaOutOfDate
from .settings import Settings


def main() -> None:
    settings = Settings.from_env()
    try:
        app = create_app(settings)
    except SchemaOutOfDate as e:  # pd-ui-migrate hasn't been run for this version
        raise SystemExit(str(e)) from None
    # One process only: its single writer thread is what keeps applies apart (ADR 0012).
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ.get("PD_UI_PORT", "8080")),
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )


if __name__ == "__main__":
    main()
