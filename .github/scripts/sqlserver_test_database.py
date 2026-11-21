"""CI only (#70): wait for the throwaway SQL Server, create an empty test database, and print
its SQLAlchemy URL. The sa password is generated in the job and never committed."""

import os
import sys
import time
from urllib.parse import quote_plus

import pyodbc

DRIVER = "ODBC Driver 18 for SQL Server"
DATABASE = "pdops_test"


def odbc_braced(value: str) -> str:
    """Escape a value for {braces} in an ODBC connection string."""
    return value.replace("}", "}}")


def main() -> int:
    password = os.environ["MSSQL_SA_PASSWORD"]
    server = "127.0.0.1,1433"
    deadline = time.monotonic() + 120
    while True:
        try:
            connection = pyodbc.connect(
                f"DRIVER={{{DRIVER}}};SERVER={server};UID=sa;PWD={{{odbc_braced(password)}}};"
                "TrustServerCertificate=yes", autocommit=True, timeout=5)
            break
        except pyodbc.Error:
            if time.monotonic() > deadline:
                print("SQL Server didn't start in time.", file=sys.stderr)
                return 1
            time.sleep(2)
    connection.execute(f"IF DB_ID('{DATABASE}') IS NULL CREATE DATABASE {DATABASE}")
    print(f"mssql+pyodbc://sa:{quote_plus(password)}@127.0.0.1:1433/{DATABASE}"
          f"?driver={quote_plus(DRIVER)}&TrustServerCertificate=yes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
