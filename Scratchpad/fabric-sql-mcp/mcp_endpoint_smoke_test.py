import asyncio
import os
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    env = os.environ.copy()
    workspace_id = os.environ["FABRIC_WORKSPACE_ID"]
    sql_endpoint_id = os.environ["FABRIC_SQL_ENDPOINT_ID"]
    database = os.environ["FABRIC_SQL_DATABASE"]
    tenant_id = os.environ.get("FABRIC_TENANT_ID")
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
            result = await session.call_tool("fabric_sql_nlp_query_endpoint", {
                "question": "Which cities sold the most units?",
                "workspace_id": workspace_id,
                "sql_endpoint_id": sql_endpoint_id,
                "database": database,
                "tenant_id": tenant_id,
            })
            print(result.content[0].text)

asyncio.run(main())
