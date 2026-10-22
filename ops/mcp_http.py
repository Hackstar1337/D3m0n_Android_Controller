"""Start the upstream MCP tools over localhost-only Streamable HTTP."""
from gitd.mcp_server import mcp
mcp.run(transport='streamable-http')
