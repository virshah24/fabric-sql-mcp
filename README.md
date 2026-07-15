# Fabric SQL MCP

Python MCP server for querying Microsoft Fabric SQL endpoints with Microsoft Entra authentication.

## Features

- Local stdio MCP transport for desktop clients.
- Streamable HTTP transport for Azure Container Apps.
- Read-only SQL execution (`SELECT` only).
- Simple retail NLP-to-SQL helper tools.
- Default endpoint mode and explicit endpoint mode by workspace/SQL endpoint IDs.

## Tools

- `fabric_sql_query`
- `fabric_sql_nlp_query`
- `fabric_sql_list_tables`
- `fabric_sql_query_endpoint`
- `fabric_sql_nlp_query_endpoint`
- `fabric_sql_list_tables_endpoint`
- `fabric_sql_export_csv`
- `fabric_sql_export_csv_endpoint`

## Local setup

```powershell
cd Scratchpad\fabric-sql-mcp
python -m pip install -r requirements.txt
copy .env.example .env
```

Set the environment variables in `.env` or your shell, then run:

```powershell
python .\server.py --test "Which cities sold the most units?"
```

## Azure deployment

This repository includes AZD + Bicep deployment for Azure Container Apps:

```powershell
azd env new fabric-sql-mcp
azd env set AZURE_SUBSCRIPTION_ID <subscription-id>
azd env set AZURE_LOCATION <region>
azd env config set infra.parameters.environmentName fabric-sql-mcp
azd env config set infra.parameters.fabricTenantId <tenant-id>
azd env config set infra.parameters.fabricSqlServer <fabric-sql-endpoint-host>
azd env config set infra.parameters.fabricSqlDatabase <database-or-catalog-name>
azd env config set infra.parameters.mcpAuthToken <strong-random-token>
azd env config set infra.parameters.mcpAllowedOrigins "https://your-chat-app.example.com"
azd provision
azd deploy
```

The deployed MCP endpoint is exposed at:

```text
https://<container-app-host>/mcp
```

## Security

- No SQL passwords are used.
- Azure Container Apps uses managed identity.
- Remote HTTP deployments can require a Bearer token or `x-api-key` via `MCP_AUTH_TOKEN`.
- Browser callers are restricted by `MCP_ALLOWED_ORIGINS` when configured.
- In-memory per-client rate limiting is controlled by `MCP_RATE_LIMIT_PER_MINUTE`.
- SQL tools reject non-`SELECT` statements, multiple statements, and dangerous SQL keywords.
- Interactive query tools cap rows with `FABRIC_SQL_MAX_QUERY_ROWS`.
- CSV export tools cap rows with `FABRIC_SQL_MAX_EXPORT_ROWS` and page with `FABRIC_SQL_MAX_EXPORT_PAGE_SIZE`.

## CSV exports

Use `fabric_sql_export_csv` for the default endpoint or `fabric_sql_export_csv_endpoint` for an explicit Fabric SQL endpoint.

The tool writes CSV files to `FABRIC_SQL_EXPORT_DIR` and returns:

- `rowCount`
- `fileName`
- `downloadPath`
- `downloadUrl` when `PUBLIC_BASE_URL` is configured

For production, prefer replacing local temporary export storage with Azure Blob Storage and short-lived download URLs.

## Demo chat UI

The deployed container also serves a lightweight demo page:

```text
https://<container-app-host>/demo
```

The page lets you:

- enter the MCP bearer/API token,
- run a sample NLP query,
- export up to 2,000 rows to CSV through the protected `/demo/export` route.

The demo uses the same server-side guardrails and authentication as the MCP endpoint.
