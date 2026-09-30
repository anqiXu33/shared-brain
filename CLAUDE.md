本项目在共享记忆（shared-brain MCP）中的 project 名是 shared-brain。

这是共享记忆 MCP server 本身的代码：Python + FastMCP，数据库是 Supabase（pgvector），部署在 Render。
改动 server.py 后需要 git push，Render 会自动重新部署。
数据库改动写成 migrations/ 下的 SQL 文件，在 Supabase SQL Editor 手动运行。
