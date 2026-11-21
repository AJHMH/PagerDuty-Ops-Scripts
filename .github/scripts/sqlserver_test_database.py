"""CI only (#70): wait for the throwaway SQL Server and create an empty test database.

The sa password is generated in the job and never committed. This script never prints it:
the workflow builds the test database URL itself."""

import os
import sys
import time

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
    print(f"Created {DATABASE}.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
