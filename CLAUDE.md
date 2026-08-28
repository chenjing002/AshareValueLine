# CLAUDE.md

Guidance for Claude Code when working in this repo.

## MCP tools — do NOT load the broken financial-market browser tools

The `xhcj-mcp-financial-market` server exposes 5 tools whose upstream `inputSchema`
uses `"type": "list"` — an invalid JSON Schema type (the array type is `"array"`).
Loading any of them via ToolSearch registers the bad schema in the live tool set, and
the Anthropic API then rejects **every** subsequent request with:

    400 tools.<n>.custom.input_schema: JSON schema is invalid (draft 2020-12)

This poisons the whole session; the only recovery is `/clear` or a new session.

**Never ToolSearch-load these tools:**
`stock-browser`, `bond-browser`, `fund-browser`, `usstock-browser`, `xhcj-mcp-stock-xssjj`

To get financial data without them:
- Use the direct-HTTP fetch scripts `fetch_data.py` / `fetch_hk_data.py` — they call
  `mcp.cnfic.com.cn` directly and never register a Claude tool, so the schema bug is irrelevant.
- The other server, `xhcj-mcp-quote-stock-real` (42 tools), has valid schemas and is safe to use.

A durable alternative (not yet implemented) is a local MCP proxy that rewrites
`"type":"list"` → `"type":"array"` in `tools/list` before the schema reaches the API.
