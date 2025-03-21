import anyio
import click
import httpx
import mcp.types as types
from mcp.server.lowlevel import Server


async def fetch_website(
    url: str,
) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    headers = {
        "User-Agent": "MCP Test Server (github.com/modelcontextprotocol/python-sdk)"
    }
    async with httpx.AsyncClient(follow_redirects=True, headers=headers) as client:
        response = await client.get(url)
        response.raise_for_status()
        return [types.TextContent(type="text", text=response.text)]


@click.command()
@click.option("--port", default=8000, help="Port to listen on for SSE")
@click.option(
    "--transport",
    type=click.Choice(["stdio", "sse"]),
    default="stdio",
    help="Transport type",
)
def main(port: int, transport: str) -> int:
    app = Server("mcp-website-fetcher")

    @app.call_tool()
    async def fetch_tool(
        name: str, arguments: dict
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        if name != "fetch":
            raise ValueError(f"Unknown tool: {name}")
        if "url" not in arguments:
            raise ValueError("Missing required argument 'url'")
        return await fetch_website(arguments["url"])

    @app.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="fetch",
                description="Fetches a website and returns its content",
                inputSchema={
                    "type": "object",
                    "required": ["url"],
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "URL to fetch",
                        }
                    },
                },
            )
        ]

    if transport == "sse":
        from mcp.server.sse import SseServerTransport
        from starlette.applications import Starlette
        from starlette.routing import Mount, Route

        sse = SseServerTransport("/messages/")
        
        print("服务器已启动（SSE模式）")
        print("使用以下命令进行测试：")
        print(f"1. 建立 SSE 连接：curl -N http://localhost:{port}")
        print(f"2. 初始化会话：curl -X POST 'http://localhost:{port}/messages/?session_id=<session_id>' \\")
        print("     -H 'Content-Type: application/json' \\")
        print("     -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"0.1.0\",\"capabilities\":{},\"clientInfo\":{\"name\":\"Curl Client\",\"version\":\"1.0.0\"}}}'")
        print(f"3. 发送初始化完成通知：curl -X POST 'http://localhost:{port}/messages/?session_id=<session_id>' \\")
        print("     -H 'Content-Type: application/json' \\")
        print("     -d '{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}'")
        print(f"4. 获取工具列表：curl -X POST 'http://localhost:{port}/messages/?session_id=<session_id>' \\")
        print("     -H 'Content-Type: application/json' \\")
        print("     -d '{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/list\"}'")
        print(f"5. 调用 fetch 工具：curl -X POST 'http://localhost:{port}/messages/?session_id=<session_id>' \\")
        print("     -H 'Content-Type: application/json' \\")
        print("     -d '{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",\"params\":{\"name\":\"fetch\",\"arguments\":{\"url\":\"https://example.com\"}}}'")
        print("注意：<session_id> 需要替换为第一步 SSE 连接返回的 session_id")
   

        async def handle_sse(request):
            async with sse.connect_sse(
                request.scope, request.receive, request._send
            ) as streams:
                await app.run(
                    streams[0], streams[1], app.create_initialization_options()
                )

        starlette_app = Starlette(
            debug=True,
            routes=[
                Route("/", endpoint=handle_sse),
                Mount("/messages/", app=sse.handle_post_message),
            ],
        )

        import uvicorn

        uvicorn.run(starlette_app, host="0.0.0.0", port=port)
    else:
        from mcp.server.stdio import stdio_server
        
        print("服务器已启动（stdio模式）")
        print("等待输入中... 按 Ctrl+C 退出")
        
        async def arun():
            async with stdio_server() as streams:
                await app.run(
                    streams[0], streams[1], app.create_initialization_options()
                )

        anyio.run(arun)

    return 0

if __name__ == "__main__":
    main()