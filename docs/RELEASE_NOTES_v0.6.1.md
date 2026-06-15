# Woddi Harbor v0.6.1

NetBox worker liveness is now independent of the upstream NetBox API.

The local `/health` endpoint returns as soon as the worker is ready. DNS,
schema discovery and API reachability are checked by MCP discovery,
`./harbor.sh module discover netbox` and `./harbor.sh module test netbox`.

This prevents a slow or temporarily unavailable NetBox instance from exceeding
the local worker startup timeout.
