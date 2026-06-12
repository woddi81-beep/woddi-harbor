# Woddi Harbor v0.3.7

Harbor can now be explicitly exposed inside a protected network:

```bash
.venv/bin/woddi-harbor server set --host 0.0.0.0 --port 9680
./harbor.sh start
```

The setting is persistent. `HARBOR_HOST` and `HARBOR_PORT` remain available as
temporary environment overrides. Production check reports an external bind as a
warning, not as a release-blocking error.
