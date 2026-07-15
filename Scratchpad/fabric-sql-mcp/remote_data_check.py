import asyncio
import os
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    endpoint = os.environ.get("MCP_ENDPOINT", "http://localhost:8000/mcp")
    token = os.environ.get("MCP_AUTH_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else None
    async with streamablehttp_client(endpoint, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for q in [
                "SELECT COUNT(*) AS FactRows FROM dbo.factsales",
                "SELECT TOP (5) * FROM dbo.factsales",
                "SELECT COUNT(*) AS StoreRows FROM dbo.dimstore",
                "SELECT TOP (5) * FROM dbo.dimstore",
            ]:
                result = await session.call_tool("fabric_sql_query", {"query": q})
                print("---", q, "---")
                print(result.content[0].text)

asyncio.run(main())
