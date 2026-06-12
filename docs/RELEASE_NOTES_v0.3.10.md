# Woddi Harbor v0.3.10

`module call` now accepts both supported payload forms:

```bash
./harbor.sh cli module call openstack list_servers '{}'
./harbor.sh cli module call openstack list_servers --payload '{}'
```

The first form restores compatibility with existing Harbor documentation.
