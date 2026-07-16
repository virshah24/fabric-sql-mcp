import argparse
import asyncio
import csv
import json
import os
import re
import shutil
import struct
import subprocess
import time
import urllib.request
import uuid
from collections import defaultdict, deque
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyodbc
from azure.identity import AzureCliCredential, DefaultAzureCredential
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
import uvicorn


SQL_COPT_SS_ACCESS_TOKEN = 1256

DEFAULT_TENANT_ID = ""
DEFAULT_SERVER = ""
DEFAULT_DATABASE = ""
EXPORT_DIR = Path(os.getenv("FABRIC_SQL_EXPORT_DIR", "/tmp/fabric-sql-mcp-exports"))
DANGEROUS_SQL_PATTERN = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|deny|execute|exec|xp_|sp_)\b",
    re.IGNORECASE,
)
_rate_windows: dict[str, deque[float]] = defaultdict(deque)
_export_lock = asyncio.Semaphore(int(os.getenv("MCP_EXPORT_CONCURRENCY", "1")))


def _normalize_database_name(database: str | None) -> str | None:
    if not database:
        return database
    cleaned = database.strip()
    match = re.match(
        r"^(?P<name>.+)-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        cleaned,
        re.IGNORECASE,
    )
    return match.group("name") if match else cleaned


def _json_safe(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return value


class ApiKeyAndRateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, api_key: str = "", rate_limit_per_minute: int = 60) -> None:
        super().__init__(app)
        self.api_key = api_key
        self.rate_limit_per_minute = rate_limit_per_minute

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if request.method == "OPTIONS" or request.url.path in {"/health", "/demo"}:
            return await call_next(request)

        if self.api_key:
            auth = request.headers.get("authorization", "")
            api_key = request.headers.get("x-api-key", "")
            if auth != f"Bearer {self.api_key}" and api_key != self.api_key:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)

        client = request.client.host if request.client else "unknown"
        now = time.time()
        window = _rate_windows[client]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self.rate_limit_per_minute:
            return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
        window.append(now)

        request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response


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
            return AzureCliCredential(tenant_id=tenant_id or "").get_token(scope).token
        except Exception as exc:
            raise RuntimeError(
                "Could not acquire an Azure token. In Azure, enable managed identity; "
                "locally, sign in with Azure CLI."
            ) from exc
    try:
        return AzureCliCredential(tenant_id=tenant_id or "").get_token(scope).token
    except Exception:
        pass
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


def _validate_select_query(query: str, *, for_export: bool = False) -> str:
    stripped = query.strip()
    if not stripped:
        raise ValueError("Query is required.")
    if len(stripped) > int(os.getenv("FABRIC_SQL_MAX_QUERY_CHARS", "20000")):
        raise ValueError("Query is too large.")

    without_trailing_semicolon = stripped[:-1].strip() if stripped.endswith(";") else stripped
    if ";" in without_trailing_semicolon:
        raise ValueError("Multiple SQL statements are not allowed.")

    normalized = without_trailing_semicolon.lower()
    if not (normalized.startswith("select") or normalized.startswith("with")):
        raise ValueError("Only read-only SELECT queries are allowed.")
    if DANGEROUS_SQL_PATTERN.search(without_trailing_semicolon):
        raise ValueError("Query contains a disallowed SQL keyword.")
    if not for_export and "information_schema." not in normalized and not re.search(r"\b(top|offset|count(?:_big)?\s*\()", normalized):
        raise ValueError("Interactive queries must include TOP, OFFSET/FETCH, or COUNT. Use export for larger result sets.")
    return without_trailing_semicolon


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
    resolved_database = _normalize_database_name(database) or os.getenv("FABRIC_SQL_DATABASE", DEFAULT_DATABASE)
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
    return [
        {
            column: _json_safe(value)
            for column, value in zip(columns, row)
        }
        for row in rows
    ]


def _execute_sql(
    query: str,
    database: str | None = None,
    max_rows: int = 100,
    server: str | None = None,
    tenant_id: str | None = None,
    workspace_id: str | None = None,
    sql_endpoint_id: str | None = None,
) -> dict[str, Any]:
    query = _validate_select_query(query)
    max_rows = min(max_rows, int(os.getenv("FABRIC_SQL_MAX_QUERY_ROWS", "5000")))

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
            "database": _normalize_database_name(database) or os.getenv("FABRIC_SQL_DATABASE", DEFAULT_DATABASE),
            "server": server or os.getenv("FABRIC_SQL_SERVER", DEFAULT_SERVER),
            "rowCount": len(rows),
            "rows": rows,
        }


def _export_csv(
    query: str,
    database: str | None = None,
    max_rows: int = 100000,
    page_size: int = 1000,
    server: str | None = None,
    tenant_id: str | None = None,
    workspace_id: str | None = None,
    sql_endpoint_id: str | None = None,
) -> dict[str, Any]:
    query = _validate_select_query(query, for_export=True)
    max_rows = min(max_rows, int(os.getenv("FABRIC_SQL_MAX_EXPORT_ROWS", "100000")))
    page_size = max(1, min(page_size, int(os.getenv("FABRIC_SQL_MAX_EXPORT_PAGE_SIZE", "5000"))))
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    file_name = f"fabric-sql-export-{uuid.uuid4().hex}.csv"
    path = EXPORT_DIR / file_name

    row_count = 0
    with _connect(
        database=database,
        server=server,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        sql_endpoint_id=sql_endpoint_id,
    ) as connection:
        cursor = connection.cursor()
        cursor.execute(query)
        columns = [column[0] for column in cursor.description or []]
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            while row_count < max_rows:
                rows = cursor.fetchmany(min(page_size, max_rows - row_count))
                if not rows:
                    break
                writer.writerows(rows)
                row_count += len(rows)

    public_base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    return {
        "database": _normalize_database_name(database) or os.getenv("FABRIC_SQL_DATABASE", DEFAULT_DATABASE),
        "rowCount": row_count,
        "fileName": file_name,
        "downloadPath": f"/exports/{file_name}",
        "downloadUrl": f"{public_base_url}/exports/{file_name}" if public_base_url else None,
        "maxRowsApplied": max_rows,
    }


def _discover_schema(
    database: str | None = None,
    server: str | None = None,
    tenant_id: str | None = None,
    workspace_id: str | None = None,
    sql_endpoint_id: str | None = None,
) -> dict[str, Any]:
    result = _execute_sql(
        query=(
            "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE "
            "FROM INFORMATION_SCHEMA.COLUMNS "
            "ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION"
        ),
        database=database,
        server=server,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        sql_endpoint_id=sql_endpoint_id,
        max_rows=5000,
    )
    rows = result["rows"]
    tables = sorted({f"{str(row['TABLE_SCHEMA']).strip()}.{str(row['TABLE_NAME']).strip()}" for row in rows})
    table_set = {table.strip().lower() for table in tables}
    if {"saleslt.salesorderdetail", "saleslt.salesorderheader"}.issubset(table_set):
        schema_profile = "saleslt"
    elif "dbo.factsales" in table_set:
        schema_profile = "retail"
    elif "dbo.trip" in table_set or any(table.endswith(".trip") for table in table_set):
        schema_profile = "trip"
    else:
        schema_profile = "generic"
    return {
        "database": result["database"],
        "server": result["server"],
        "schemaProfile": schema_profile,
        "tableCount": len(tables),
        "columnCount": len(rows),
        "tables": tables,
        "columns": rows,
    }


def _translate_nlp(question: str, database: str | None = None, schema_profile: str | None = None) -> str:
    normalized = question.lower()
    normalized_database = (_normalize_database_name(database) or os.getenv("FABRIC_SQL_DATABASE", "")).lower()
    normalized_profile = (schema_profile or os.getenv("FABRIC_SQL_SCHEMA_PROFILE", "")).lower()
    top_match = re.search(r"\b(?:top|last|latest)\s+(\d{1,5})\b", normalized)
    top_n = min(int(top_match.group(1)), 5000) if top_match else 10

    if normalized_profile == "saleslt" or normalized_database == "fabsqldb1":
        if any(term in normalized for term in ("transaction", "transactions", "sales rows", "sales table", "order detail", "sales")) and any(
            term in normalized for term in ("last", "latest", "recent", "show")
        ):
            return (
                f"SELECT TOP ({top_n}) h.SalesOrderID, d.SalesOrderDetailID, h.OrderDate, h.CustomerID, "
                "d.ProductID, p.Name AS ProductName, d.OrderQty, d.UnitPrice, d.UnitPriceDiscount, "
                "CAST(d.OrderQty * d.UnitPrice * (1 - d.UnitPriceDiscount) AS decimal(18,2)) AS LineSalesAmount "
                "FROM SalesLT.SalesOrderDetail d "
                "JOIN SalesLT.SalesOrderHeader h ON d.SalesOrderID = h.SalesOrderID "
                "LEFT JOIN SalesLT.Product p ON d.ProductID = p.ProductID "
                "ORDER BY h.OrderDate DESC, h.SalesOrderID DESC, d.SalesOrderDetailID DESC"
            )
        if "product" in normalized or "goods" in normalized or "sku" in normalized:
            metric = "SUM(d.OrderQty)" if any(term in normalized for term in ("unit", "quantity", "sold")) else "SUM(d.OrderQty * d.UnitPrice * (1 - d.UnitPriceDiscount))"
            alias = "UnitsSold" if "SUM(d.OrderQty)" in metric else "SalesAmount"
            direction = "ASC" if any(term in normalized for term in ("bottom", "lowest", "least", "worst")) else "DESC"
            return (
                f"SELECT TOP ({top_n}) p.Name AS ProductName, {metric} AS {alias} "
                "FROM SalesLT.SalesOrderDetail d "
                "LEFT JOIN SalesLT.Product p ON d.ProductID = p.ProductID "
                "GROUP BY p.Name "
                f"ORDER BY {alias} {direction}"
            )
        if "summary" in normalized or "total" in normalized or "overall" in normalized:
            return (
                "SELECT COUNT_BIG(*) AS TransactionLineCount, SUM(d.OrderQty) AS UnitsSold, "
                "CAST(SUM(d.OrderQty * d.UnitPrice * (1 - d.UnitPriceDiscount)) AS decimal(18,2)) AS SalesAmount, "
                "CAST(AVG(d.UnitPrice) AS decimal(18,2)) AS AvgUnitPrice "
                "FROM SalesLT.SalesOrderDetail d"
            )

    if normalized_profile == "trip":
        distance_filter = ""
        distance_match = re.search(
            r"\b(?:more than|over|greater than|above|at least|>=|>)\s*(\d+(?:\.\d+)?)\s*(?:mile|miles|mi)\b",
            normalized,
        )
        if distance_match:
            operator = ">=" if any(term in normalized for term in ("at least", ">=")) else ">"
            distance_filter = f"WHERE t.TripDistanceMiles {operator} {float(distance_match.group(1))} "
        else:
            distance_match = re.search(
                r"\b(?:less than|under|below|at most|<=|<)\s*(\d+(?:\.\d+)?)\s*(?:mile|miles|mi)\b",
                normalized,
            )
            if distance_match:
                operator = "<=" if any(term in normalized for term in ("at most", "<=")) else "<"
                distance_filter = f"WHERE t.TripDistanceMiles {operator} {float(distance_match.group(1))} "

        if any(term in normalized for term in ("transaction", "transactions", "sales rows", "sales table", "trip", "trips", "ride", "rides")) and any(
            term in normalized for term in ("last", "latest", "recent", "show")
        ):
            return (
                f"SELECT TOP ({top_n}) t.* "
                "FROM dbo.Trip t "
                f"{distance_filter}"
                "ORDER BY t.DateID DESC"
            )
        if "summary" in normalized or "total" in normalized or "overall" in normalized:
            return (
                "SELECT COUNT_BIG(*) AS TripCount, "
                "CAST(SUM(t.FareAmount) AS decimal(18,2)) AS FareAmount, "
                "CAST(SUM(t.TotalAmount) AS decimal(18,2)) AS TotalAmount, "
                "CAST(AVG(t.TotalAmount) AS decimal(18,2)) AS AvgTotalAmount "
                "FROM dbo.Trip t "
                f"{distance_filter}".rstrip()
            )
        if "date" in normalized or "day" in normalized or "daily" in normalized:
            return (
                f"SELECT TOP ({top_n}) t.DateID, COUNT_BIG(*) AS TripCount, "
                "CAST(SUM(t.TotalAmount) AS decimal(18,2)) AS TotalAmount "
                "FROM dbo.Trip t "
                f"{distance_filter}"
                "GROUP BY t.DateID "
                "ORDER BY t.DateID DESC"
            )
        return (
            f"SELECT TOP ({top_n}) t.* "
            "FROM dbo.Trip t "
            f"{distance_filter}"
            "ORDER BY t.DateID DESC"
        )

    if any(term in normalized for term in ("transaction", "transactions", "sales rows", "sales table")) and any(
        term in normalized for term in ("last", "latest", "recent", "show")
    ):
        return (
            f"SELECT TOP ({top_n}) f.SaleId, f.[Date], f.StoreId, s.StoreName, s.City, s.Region, "
            "f.ProductId, p.ProductName, p.Category, p.Brand, f.Units, f.RevenueUSD "
            "FROM dbo.factsales f "
            "LEFT JOIN dbo.dimstore s ON f.StoreId = s.StoreId "
            "LEFT JOIN dbo.dimproduct p ON f.ProductId = p.ProductId "
            "ORDER BY f.[Date] DESC, f.SaleId DESC"
        )

    dimension: tuple[str, str, str] | None = None
    if "city" in normalized or "cities" in normalized:
        dimension = ("s.City", "City", "dbo.dimstore s ON f.StoreId = s.StoreId")
    elif "region" in normalized or "country" in normalized:
        dimension = ("s.Region", "Region", "dbo.dimstore s ON f.StoreId = s.StoreId")
    elif "store" in normalized:
        dimension = ("s.StoreName", "StoreName", "dbo.dimstore s ON f.StoreId = s.StoreId")
    elif "category" in normalized or "categories" in normalized:
        dimension = ("p.Category", "Category", "dbo.dimproduct p ON f.ProductId = p.ProductId")
    elif "brand" in normalized:
        dimension = ("p.Brand", "Brand", "dbo.dimproduct p ON f.ProductId = p.ProductId")
    elif "product" in normalized or "products" in normalized or "goods" in normalized or "sku" in normalized:
        dimension = ("p.ProductName", "ProductName", "dbo.dimproduct p ON f.ProductId = p.ProductId")
    elif "date" in normalized or "day" in normalized or "daily" in normalized:
        dimension = ("f.[Date]", "Date", "")

    if "average" in normalized or "avg" in normalized:
        if "unit" in normalized:
            metric = "AVG(CAST(f.Units AS float))"
            alias = "AvgUnits"
        else:
            metric = "SUM(f.RevenueUSD) / NULLIF(SUM(f.Units), 0)"
            alias = "AvgSellingPrice"
    elif "transaction" in normalized or "count" in normalized or "number of sales" in normalized:
        metric = "COUNT_BIG(*)"
        alias = "TransactionCount"
    elif "unit" in normalized or "quantity" in normalized or "sold" in normalized:
        metric = "SUM(f.Units)"
        alias = "UnitsSold"
    else:
        metric = "SUM(f.RevenueUSD)"
        alias = "RevenueUSD"

    order_direction = "ASC" if any(term in normalized for term in ("bottom", "lowest", "least", "worst")) else "DESC"

    if dimension:
        field, alias_dimension, join_clause = dimension
        join_sql = f"JOIN {join_clause} " if join_clause else ""
        return (
            f"SELECT TOP ({top_n}) {field} AS {alias_dimension}, {metric} AS {alias} "
            "FROM dbo.factsales f "
            f"{join_sql}"
            f"GROUP BY {field} "
            f"ORDER BY {alias} {order_direction}"
        )

    if "total" in normalized or "summary" in normalized or "overall" in normalized:
        return (
            "SELECT COUNT_BIG(*) AS TransactionCount, SUM(f.Units) AS UnitsSold, "
            "SUM(f.RevenueUSD) AS RevenueUSD, SUM(f.RevenueUSD) / NULLIF(SUM(f.Units), 0) AS AvgSellingPrice "
            "FROM dbo.factsales f"
        )

    raise ValueError(
        "I can translate retail sales questions about transactions, revenue, units, average selling price, "
        "and breakdowns by city, region, store, product, category, brand, or date. Use fabric_sql_query for custom SQL."
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


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> Response:
    return JSONResponse({"status": "ok", "version": "schema-discovery-trip-v2"})


@mcp.custom_route("/demo", methods=["GET"])
async def demo(_: Request) -> Response:
    return HTMLResponse(
        """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Fabric SQL MCP Demo</title>
  <style>
    body { font-family: Segoe UI, Arial, sans-serif; margin: 0; background: #f6f7f9; color: #242424; }
    main { max-width: 960px; margin: 32px auto; padding: 0 20px; }
    section { background: white; border: 1px solid #ddd; border-radius: 12px; padding: 20px; margin-bottom: 16px; }
    input, textarea, button { font: inherit; }
    input, textarea { width: 100%; box-sizing: border-box; border: 1px solid #aaa; border-radius: 8px; padding: 10px; margin: 6px 0 12px; }
    textarea { min-height: 84px; }
    button { border: 0; border-radius: 8px; padding: 10px 14px; background: #0f6cbd; color: white; cursor: pointer; margin-right: 8px; }
    pre { background: #1e1e1e; color: #f5f5f5; padding: 16px; border-radius: 8px; overflow: auto; }
    .muted { color: #666; }
  </style>
</head>
<body>
<main>
  <h1>Fabric SQL MCP Demo</h1>
  <p class="muted">Demo NLP query and CSV export against the deployed MCP-backed Fabric SQL service.</p>
  <section>
    <label>Bearer token / API key</label>
    <input id="token" type="password" placeholder="Paste MCP_AUTH_TOKEN" />
    <label>Fabric SQL endpoint host</label>
    <input id="server" placeholder="Use server configured on deployment" />
    <label>Database / catalog</label>
    <input id="database" placeholder="Use database configured on deployment" />
    <label>NLP question</label>
    <textarea id="question">Show me last 1500 trips that traveled more than 5 miles?</textarea>
    <button onclick="discoverSchema()">Discover Schema</button>
    <button onclick="runNlp()">Run NLP Query</button>
    <button onclick="exportCsv()">Export Current Question CSV</button>
  </section>
  <section>
    <h2>Result</h2>
    <pre id="output">Ready.</pre>
  </section>
</main>
<script>
  let lastTranslatedSql = null;
  let lastQuestion = null;
  let schemaProfile = null;

  function authHeaders() {
    const token = document.getElementById('token').value.trim();
    return { 'content-type': 'application/json', 'authorization': `Bearer ${token}` };
  }
  function connectionPayload() {
    const server = document.getElementById('server').value.trim();
    const database = document.getElementById('database').value.trim();
    return {
      server: server || null,
      database: database || null,
      schemaProfile: schemaProfile || null
    };
  }
  async function discoverSchema() {
    const output = document.getElementById('output');
    output.textContent = 'Discovering schema...';
    const response = await fetch('/demo/discover', {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify(connectionPayload())
    });
    const payload = await response.json();
    if (response.ok) {
      schemaProfile = payload.schemaProfile;
      lastTranslatedSql = null;
      lastQuestion = null;
    }
    output.textContent = JSON.stringify(payload, null, 2);
  }
  async function runNlp() {
    const output = document.getElementById('output');
    output.textContent = 'Running...';
    const response = await fetch('/demo/query', {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({
        question: document.getElementById('question').value,
        ...connectionPayload()
      })
    });
    const payload = await response.json();
    if (response.ok && payload.translatedSql) {
      lastTranslatedSql = payload.translatedSql;
      lastQuestion = document.getElementById('question').value;
    }
    output.textContent = JSON.stringify(payload, null, 2);
  }
  async function exportCsv() {
    const output = document.getElementById('output');
    const question = document.getElementById('question').value;
    output.textContent = 'Exporting...';
    const requestBody = {
      question,
      max_rows: 2000,
      ...connectionPayload()
    };
    if (lastTranslatedSql && lastQuestion === question) {
      requestBody.query = lastTranslatedSql;
    }
    const response = await fetch('/demo/export', {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify(requestBody)
    });
    const payload = await response.json();
    output.textContent = JSON.stringify(payload, null, 2);
    if (!response.ok) return;
    const file = await fetch(payload.downloadPath, { headers: authHeaders() });
    const blob = await file.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = payload.fileName;
    a.click();
    URL.revokeObjectURL(url);
  }
</script>
</body>
</html>
        """,
        headers={
            "cache-control": "no-store, no-cache, must-revalidate, max-age=0",
            "pragma": "no-cache",
        },
    )


@mcp.custom_route("/demo/discover", methods=["POST"])
async def demo_discover(request: Request) -> Response:
    try:
        payload = await request.json()
        result = await asyncio.to_thread(
            _discover_schema,
            database=payload.get("database"),
            server=payload.get("server"),
            tenant_id=payload.get("tenant_id"),
        )
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@mcp.custom_route("/demo/query", methods=["POST"])
async def demo_query(request: Request) -> Response:
    try:
        payload = await request.json()
        question = str(payload.get("question", "")).strip()
        if not question:
            return JSONResponse({"error": "question is required"}, status_code=400)
        sql = _translate_nlp(question, database=payload.get("database"), schema_profile=payload.get("schemaProfile"))
        result = await asyncio.to_thread(
            _execute_sql,
            query=sql,
            database=payload.get("database"),
            server=payload.get("server"),
            tenant_id=payload.get("tenant_id"),
            max_rows=100,
        )
        result["question"] = question
        result["translatedSql"] = sql
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@mcp.custom_route("/demo/export", methods=["POST"])
async def demo_export(request: Request) -> Response:
    try:
        payload = await request.json()
        max_rows = int(payload.get("max_rows", 2000))
        source = "default"
        if payload.get("query"):
            query = str(payload["query"])
            source = "provided_query"
        elif payload.get("question"):
            query = _translate_nlp(
                str(payload["question"]),
                database=payload.get("database"),
                schema_profile=payload.get("schemaProfile"),
            )
            source = "translated_question"
        else:
            query = "SELECT TOP (2000) * FROM dbo.factsales ORDER BY [Date] DESC, SaleId DESC"
        result = await asyncio.to_thread(
            _export_csv,
            query=query,
            database=payload.get("database"),
            server=payload.get("server"),
            tenant_id=payload.get("tenant_id"),
            max_rows=max_rows,
            page_size=1000,
        )
        result["translatedSql"] = query
        result["exportSource"] = source
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@mcp.custom_route("/exports/{file_name}", methods=["GET"])
async def download_export(request: Request) -> Response:
    file_name = request.path_params["file_name"]
    if not re.fullmatch(r"fabric-sql-export-[0-9a-f]+\.csv", file_name):
        return JSONResponse({"error": "Invalid file name"}, status_code=400)
    path = EXPORT_DIR / file_name
    if not path.exists():
        return JSONResponse({"error": "Export not found"}, status_code=404)
    return FileResponse(path, media_type="text/csv", filename=file_name)


@mcp.tool()
async def fabric_sql_query(query: str, database: str | None = None, max_rows: int = 100) -> dict[str, Any]:
    """Run a read-only SQL query against the configured Fabric SQL endpoint."""
    return await asyncio.to_thread(_execute_sql, query=query, database=database, max_rows=max_rows)


@mcp.tool()
async def fabric_sql_nlp_query(question: str, database: str | None = None, max_rows: int = 100) -> dict[str, Any]:
    """Translate a simple retail NLP question to SQL and run it against Fabric SQL."""
    sql = _translate_nlp(question, database=database)
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
async def fabric_sql_export_csv(query: str, database: str | None = None, max_rows: int = 100000, page_size: int = 1000) -> dict[str, Any]:
    """Export a read-only SQL query to CSV and return export metadata plus a download path."""
    async with _export_lock:
        return await asyncio.to_thread(
            _export_csv,
            query=query,
            database=database,
            max_rows=max_rows,
            page_size=page_size,
        )


@mcp.tool()
async def fabric_sql_export_csv_endpoint(
    query: str,
    workspace_id: str,
    sql_endpoint_id: str,
    database: str,
    tenant_id: str | None = None,
    max_rows: int = 100000,
    page_size: int = 1000,
) -> dict[str, Any]:
    """Export a query to CSV from any Fabric SQL endpoint by workspace ID and SQL endpoint item ID."""
    async with _export_lock:
        return await asyncio.to_thread(
            _export_csv,
            query=query,
            database=database,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            sql_endpoint_id=sql_endpoint_id,
            max_rows=max_rows,
            page_size=page_size,
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
    sql = _translate_nlp(question, database=database)
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
    if args.transport == "streamable-http":
        app = mcp.streamable_http_app()
        app.add_middleware(
            ApiKeyAndRateLimitMiddleware,
            api_key=os.getenv("MCP_AUTH_TOKEN", ""),
            rate_limit_per_minute=int(os.getenv("MCP_RATE_LIMIT_PER_MINUTE", "60")),
        )
        allowed_origins = [
            origin.strip()
            for origin in os.getenv("MCP_ALLOWED_ORIGINS", "").split(",")
            if origin.strip()
        ]
        if allowed_origins:
            app.add_middleware(
                CORSMiddleware,
                allow_origins=allowed_origins,
                allow_credentials=True,
                allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
                allow_headers=["authorization", "content-type", "mcp-session-id", "x-api-key", "x-request-id"],
            )
        uvicorn.run(app, host=args.host, port=args.port)
    else:
        mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
