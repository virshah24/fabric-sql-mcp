import asyncio
import os
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    endpoint = os.environ.get("MCP_ENDPOINT", "http://localhost:8000/mcp")
    async with streamablehttp_client(endpoint) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("TOOLS=" + ",".join(t.name for t in tools.tools))
            try:
                result = await session.call_tool("fabric_sql_list_tables", {})
                print(result.content[0].text)
            except Exception as exc:
                print("TOOL_CALL_ERROR=" + repr(exc))

asyncio.run(main())
