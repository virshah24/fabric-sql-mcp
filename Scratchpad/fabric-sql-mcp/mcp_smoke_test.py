import asyncio
import os
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    env = os.environ.copy()
    params = StdioServerParameters(
        command="python",
        args=[str(Path(__file__).with_name("server.py"))],
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("TOOLS=" + ",".join(t.name for t in tools.tools))
            result = await asyncio.wait_for(
                session.call_tool("fabric_sql_nlp_query", {"question": "Which cities sold the most units?"}),
                timeout=90,
            )
            for item in result.content:
                print(getattr(item, "text", item))

asyncio.run(main())
