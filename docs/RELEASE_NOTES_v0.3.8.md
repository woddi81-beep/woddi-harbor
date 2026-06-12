# Woddi Harbor v0.3.8

This release makes local MCP diagnostics resilient and adds managed OpenStack
token authentication.

## Upgrade

```bash
git fetch --tags
git checkout v0.3.8
.venv/bin/pip install -e .
./harbor.sh restart
```

## NetBox diagnosis

`module diagnose` no longer exits with a traceback when a local MCP worker
refuses the connection. It reports status, health, discovery errors, recent logs
and the matching start command.

```bash
./harbor.sh cli module start netbox
./harbor.sh cli module diagnose netbox
```

## OpenStack

Open `/admin`, select **Module**, then **OpenStack einbinden**. Enter the project
name, token and Identity/Auth URL. The token is stored under
`data/secrets/modules/openstack/` with file mode `0600`; it is never returned by
the API.

The host also needs the `openstack` executable. After saving:

```bash
./harbor.sh cli module start openstack
./harbor.sh cli module discover openstack
./harbor.sh cli module test openstack
./harbor.sh cli module call openstack list_servers '{}'
```
