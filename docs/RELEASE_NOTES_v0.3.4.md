# Woddi Harbor v0.3.4

This hotfix removes the worker startup dependency from local document synchronization.

Commands such as:

```bash
.venv/bin/woddi-harbor source sync operation-docs
.venv/bin/woddi-harbor source sync customer-docs
```

now rebuild local document indexes directly in the CLI process. Harbor and its module
workers may remain stopped during import. Successful output reports
`"reindex_mode": "direct"`.
