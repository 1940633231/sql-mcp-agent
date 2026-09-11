"""Authentication primitives for MCP requests.

Authentication establishes a Principal. Authorization decisions are handled by
``mcp_server.authorization`` and never trust roles supplied in tool arguments.
"""
