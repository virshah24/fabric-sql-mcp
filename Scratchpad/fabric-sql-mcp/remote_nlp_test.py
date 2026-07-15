import asyncio
import os
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    endpoint = os.environ.get("MCP_ENDPOINT", "http://localhost:8000/mcp")
    async with streamablehttp_client(endpoint) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("fabric_sql_nlp_query", {"question": "Which cities sold the most units?"})
            print(result.content[0].text)

asyncio.run(main())
