# Woddi Harbor v0.3.1

This hotfix prevents partially copied or mixed installations from failing later with
errors such as `ModuleNotFoundError: No module named 'app.backup'`.

Both installation paths now verify:

- all required source modules exist in the checkout,
- the installed modules can be imported,
- Python resolves them from the current Woddi Harbor directory.

## Repair an existing installation

```bash
cd /opt/woddi-ai/woddi-harbor
git fetch --tags
git checkout v0.3.1
rm -rf .venv
scripts/install_production.sh manual
.venv/bin/python tools/verify_installation.py
.venv/bin/woddi-harbor production-check
```

Runtime data under `data/` and local configuration files ignored by Git are retained.
