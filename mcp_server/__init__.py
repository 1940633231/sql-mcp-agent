"""MySQL SQL MCP Server 包。

子模块：
    - config     数据库连接 + 传输参数
    - server     服务器入口（组装工具与资源）
    - tools      对外暴露的 MCP 工具（schema / query）
    - security   只读安全防线（parser / policy / validator / models）
    - database   连接与查询执行（connection / executor）
"""
