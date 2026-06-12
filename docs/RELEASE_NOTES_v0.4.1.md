# Woddi Harbor v0.4.1

Harbor now handles Keystone tokens without a service catalog:

- one accessible project: Harbor scopes automatically;
- multiple accessible projects: the error lists project names and IDs;
- explicit project ID: Harbor scopes directly and does not require a domain.

## Upgrade

```bash
git fetch https://github.com/woddi81-beep/woddi-harbor.git refs/heads/main
git checkout --detach FETCH_HEAD
.venv/bin/python -m pip install -e . --no-build-isolation
./harbor.sh cli module stop openstack
./harbor.sh cli module start openstack
./harbor.sh cli module call openstack list_servers --payload '{}'
```

If Harbor lists multiple projects, enter the required project ID in the
OpenStack dialog and restart the module.
