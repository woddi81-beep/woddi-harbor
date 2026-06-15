# Woddi Harbor v0.5.1

The installed application version can now be checked without opening the
interactive console:

```bash
woddi-harbor version
woddi-harbor --version
woddi-harbor version --short
```

The semantic version is sourced once and shared by package metadata, CLI output,
the FastAPI schema, MCP client handshakes and `/api/health`.
