# Woddi Harbor v0.3.2

This release removes the historical ASV documentation coupling.

Production documentation is configured from the existing Markdown repositories:

```bash
.venv/bin/woddi-harbor source configure-docs \
  --operations-path /opt/woddi-ai/doku/documentation-operation-main \
  --customer-path /opt/woddi-ai/doku/documentation-customer-main
.venv/bin/woddi-harbor source sync operation-docs
.venv/bin/woddi-harbor source sync customer-docs
```

Host-specific paths are stored in ignored `config/sources.local.json`.
