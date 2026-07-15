import argparse
import asyncio
import json
import os
import shutil
import struct
import subprocess
import urllib.request
from typing import Any

import pyodbc
from azure.identity import AzureCliCredential, DefaultAzureCredential
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings


SQL_COPT_SS_ACCESS_TOKEN = 1256

DEFAULT_TENANT_ID = ""
DEFAULT_SERVER = ""
DEFAULT_DATABASE = ""


def _get_access_token(tenant_id: str, resource: str = "https://database.windows.net/") -> str:
    scope = resource.rstrip("/") + "/.default"
    use_azure_identity = os.getenv("FABRIC_USE_AZURE_IDENTITY", "true").lower() in {"1", "true", "yes"}
    has_managed_identity = any(
        os.getenv(name)
        for name in ("AZURE_CLIENT_ID", "IDENTITY_ENDPOINT", "MSI_ENDPOINT", "MSI_SECRET")
    )
    if use_azure_identity and has_managed_identity:
        try:
            credential = DefaultAzureCredential(
                exclude_interactive_browser_credential=True,
                exclude_visual_studio_code_credential=True,
                exclude_azure_cli_credential=True,
            )
            return credential.get_token(scope).token
        except Exception:
            # Local MCP usage can still rely on an already-authenticated Azure CLI.
            pass

    az = shutil.which("az") or shutil.which("az.cmd")
    if not az:
        try:
            return AzureCliCredential().get_token(scope).token
        except Exception as exc:
            raise RuntimeError(
                "Could not acquire an Azure token. In Azure, enable managed identity; "
                "locally, sign in with Azure CLI."
            ) from exc
    command = [
        az,
        "account",
        "get-access-token",
        "--resource",
        resource,
        "--query",
        "accessToken",
        "-o",
        "tsv",
    ]
    if tenant_id:
        command[5:5] = ["--tenant", tenant_id]
    result = subprocess.run(command, capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL)
    return result.stdout.strip()


def _resolve_sql_server(
    server: str | None = None,
    workspace_id: str | None = None,
    sql_endpoint_id: str | None = None,
    tenant_id: str | None = None,
) -> str:
    if server:
        return server
    if not workspace_id and not sql_endpoint_id:
        resolved_server = os.getenv("FABRIC_SQL_SERVER", DEFAULT_SERVER)
        if not resolved_server:
            raise ValueError("Provide server or set FABRIC_SQL_SERVER.")
        return resolved_server
    if not workspace_id or not sql_endpoint_id:
        raise ValueError("Provide both workspace_id and sql_endpoint_id, or provide server directly.")

    resolved_tenant = tenant_id or os.getenv("FABRIC_TENANT_ID", DEFAULT_TENANT_ID)
    token = _get_access_token(resolved_tenant, "https://api.fabric.microsoft.com")
    url = (
        f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}"
        f"/sqlEndpoints/{sql_endpoint_id}/connectionString"
    )
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload["connectionString"]


def _token_struct(access_token: str) -> bytes:
    token_bytes = access_token.encode("utf-16-le")
    return struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)


def _connect(
    database: str | None = None,
    server: str | None = None,
    tenant_id: str | None = None,
    workspace_id: str | None = None,
    sql_endpoint_id: str | None = None,
) -> pyodbc.Connection:
    resolved_tenant = tenant_id or os.getenv("FABRIC_TENANT_ID", DEFAULT_TENANT_ID)
    resolved_server = _resolve_sql_server(
        server=server,
        workspace_id=workspace_id,
        sql_endpoint_id=sql_endpoint_id,
        tenant_id=resolved_tenant,
    )
    resolved_database = database or os.getenv("FABRIC_SQL_DATABASE", DEFAULT_DATABASE)
    if not resolved_database:
        raise ValueError("Provide database or set FABRIC_SQL_DATABASE.")
    token = _get_access_token(resolved_tenant)

    connection_string = (
        "Driver={ODBC Driver 18 for SQL Server};"
        f"Server=tcp:{resolved_server},1433;"
        f"Database={resolved_database};"
        "Encrypt=yes;"
        "TrustServerCertificate=no;"
        "Connection Timeout=30;"
    )
    return pyodbc.connect(connection_string, attrs_before={SQL_COPT_SS_ACCESS_TOKEN: _token_struct(token)})


def _rows_to_dicts(cursor: pyodbc.Cursor, max_rows: int) -> list[dict[str, Any]]:
    columns = [column[0] for column in cursor.description or []]
    rows = cursor.fetchmany(max_rows)
    return [dict(zip(columns, row)) for row in rows]


def _execute_sql(
    query: str,
    database: str | None = None,
    max_rows: int = 100,
    server: str | None = None,
    tenant_id: str | None = None,
    workspace_id: str | None = None,
    sql_endpoint_id: str | None = None,
) -> dict[str, Any]:
    if not query.strip().lower().startswith("select"):
        raise ValueError("Only read-only SELECT queries are allowed.")

    with _connect(
        database=database,
        server=server,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        sql_endpoint_id=sql_endpoint_id,
    ) as connection:
        cursor = connection.cursor()
        cursor.execute(query)
        rows = _rows_to_dicts(cursor, max_rows)
        return {
            "database": database or os.getenv("FABRIC_SQL_DATABASE", DEFAULT_DATABASE),
            "server": server or os.getenv("FABRIC_SQL_SERVER", DEFAULT_SERVER),
            "rowCount": len(rows),
            "rows": rows,
        }


def _translate_nlp(question: str) -> str:
    normalized = question.lower()
    asks_city = "city" in normalized or "cities" in normalized
    if "unit" in normalized and asks_city:
        return (
            "SELECT TOP (10) s.City, SUM(f.Units) AS UnitsSold "
            "FROM dbo.factsales f "
            "JOIN dbo.dimstore s ON f.StoreId = s.StoreId "
            "GROUP BY s.City "
            "ORDER BY UnitsSold DESC"
        )
    if "unit" in normalized and ("product" in normalized or "sku" in normalized):
        return (
            "SELECT TOP (10) p.ProductName, SUM(f.Units) AS UnitsSold "
            "FROM dbo.factsales f "
            "JOIN dbo.dimproduct p ON f.ProductId = p.ProductId "
            "GROUP BY p.ProductName "
            "ORDER BY UnitsSold DESC"
        )
    if "revenue" in normalized and asks_city:
        return (
            "SELECT TOP (10) s.City, SUM(f.RevenueUSD) AS RevenueUSD "
            "FROM dbo.factsales f "
            "JOIN dbo.dimstore s ON f.StoreId = s.StoreId "
            "GROUP BY s.City "
            "ORDER BY RevenueUSD DESC"
        )
    raise ValueError(
        "I can translate city/product units and city revenue questions. "
        "Use fabric_sql_query for custom SQL."
    )


dns_rebinding_protection = os.getenv("MCP_DNS_REBINDING_PROTECTION", "true").lower() in {
    "1",
    "true",
    "yes",
}

mcp = FastMCP(
    "fabric-sql-python",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=dns_rebinding_protection
    ),
)


@mcp.tool()
async def fabric_sql_query(query: str, database: str | None = None, max_rows: int = 100) -> dict[str, Any]:
    """Run a read-only SQL query against the configured Fabric SQL endpoint."""
    return await asyncio.to_thread(_execute_sql, query=query, database=database, max_rows=max_rows)


@mcp.tool()
async def fabric_sql_nlp_query(question: str, database: str | None = None, max_rows: int = 100) -> dict[str, Any]:
    """Translate a simple retail NLP question to SQL and run it against Fabric SQL."""
    sql = _translate_nlp(question)
    result = await asyncio.to_thread(_execute_sql, query=sql, database=database, max_rows=max_rows)
    result["question"] = question
    result["translatedSql"] = sql
    return result


@mcp.tool()
async def fabric_sql_list_tables(database: str | None = None) -> dict[str, Any]:
    """List base tables visible in the configured Fabric SQL endpoint database."""
    return await asyncio.to_thread(
        _execute_sql,
        query=(
            "SELECT TABLE_SCHEMA, TABLE_NAME "
            "FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_TYPE = 'BASE TABLE' "
            "ORDER BY TABLE_SCHEMA, TABLE_NAME"
        ),
        database=database,
        max_rows=500,
    )


@mcp.tool()
async def fabric_sql_query_endpoint(
    query: str,
    workspace_id: str,
    sql_endpoint_id: str,
    database: str,
    tenant_id: str | None = None,
    max_rows: int = 100,
) -> dict[str, Any]:
    """Run read-only SQL against any Fabric SQL endpoint by workspace ID and SQL endpoint item ID."""
    return await asyncio.to_thread(
        _execute_sql,
        query=query,
        database=database,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        sql_endpoint_id=sql_endpoint_id,
        max_rows=max_rows,
    )


@mcp.tool()
async def fabric_sql_nlp_query_endpoint(
    question: str,
    workspace_id: str,
    sql_endpoint_id: str,
    database: str,
    tenant_id: str | None = None,
    max_rows: int = 100,
) -> dict[str, Any]:
    """Translate a simple retail NLP question and run it against any Fabric SQL endpoint."""
    sql = _translate_nlp(question)
    result = await asyncio.to_thread(
        _execute_sql,
        query=sql,
        database=database,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        sql_endpoint_id=sql_endpoint_id,
        max_rows=max_rows,
    )
    result["question"] = question
    result["translatedSql"] = sql
    return result


@mcp.tool()
async def fabric_sql_list_tables_endpoint(
    workspace_id: str,
    sql_endpoint_id: str,
    database: str,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """List base tables for any Fabric SQL endpoint by workspace ID and SQL endpoint item ID."""
    return await asyncio.to_thread(
        _execute_sql,
        query=(
            "SELECT TABLE_SCHEMA, TABLE_NAME "
            "FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_TYPE = 'BASE TABLE' "
            "ORDER BY TABLE_SCHEMA, TABLE_NAME"
        ),
        database=database,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        sql_endpoint_id=sql_endpoint_id,
        max_rows=500,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", help="Run one NLP test query instead of starting MCP stdio.")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default=os.getenv("MCP_TRANSPORT", "stdio"),
        help="MCP transport to use.",
    )
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    args = parser.parse_args()
    if args.test:
        print(json.dumps(asyncio.run(fabric_sql_nlp_query(args.test)), indent=2, default=str))
        return

    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
