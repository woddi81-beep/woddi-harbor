# Woddi Harbor v0.4.0

The OpenStack MCP backend has been replaced completely. Harbor now uses the
official `openstacksdk` Python API and no longer invokes an `openstack`
executable.

## Upgrade

Do not use `--no-deps` for this upgrade:

```bash
git fetch https://github.com/woddi81-beep/woddi-harbor.git refs/heads/main
git checkout --detach FETCH_HEAD
.venv/bin/python -m pip install -e . --no-build-isolation
```

Verify and restart:

```bash
.venv/bin/python -c 'import importlib.metadata; print(importlib.metadata.version("openstacksdk"))'
./harbor.sh cli module stop openstack
./harbor.sh cli module start openstack
./harbor.sh cli module diagnose openstack --lines 100
./harbor.sh cli module call openstack list_servers --payload '{}'
```

Existing OpenStack configuration and stored tokens remain valid.
