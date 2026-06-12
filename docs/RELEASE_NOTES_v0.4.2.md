# Woddi Harbor v0.4.2

OpenStack timeouts are now configurable and identify the failing phase.

After upgrading, open **OpenStack einbinden**, set the timeout to `120` seconds,
save, and restart the module:

```bash
./harbor.sh cli module stop openstack
./harbor.sh cli module start openstack
./harbor.sh cli module call openstack list_servers --payload '{}'
```

Timeout errors now distinguish Keystone authentication, project discovery and
the final OpenStack service operation.
