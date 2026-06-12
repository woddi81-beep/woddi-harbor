# Woddi Harbor v0.3.9

OpenStack MCP workers now resolve the OpenStack CLI directly from Harbor's
virtual environment instead of depending on the parent process `PATH`.

## Upgrade

```bash
git pull --ff-only origin main
.venv/bin/python -m pip install -e . --no-deps --no-build-isolation
.venv/bin/python -m pip install python-openstackclient
./harbor.sh cli module stop openstack
./harbor.sh cli module start openstack
```

Verify the client and MCP call:

```bash
.venv/bin/openstack --version
./harbor.sh cli module diagnose openstack --lines 100
./harbor.sh cli module call openstack list_servers '{}'
```
