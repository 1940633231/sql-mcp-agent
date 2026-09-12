"""MCP 请求认证基础模块。

认证负责建立 Principal；授权由 ``mcp_server.authorization`` 处理，
不信任工具参数中传入的角色。
"""
