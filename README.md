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
- The SQL tool rejects non-`SELECT` statements.
- For production, place the external MCP endpoint behind appropriate authentication or gateway controls.
