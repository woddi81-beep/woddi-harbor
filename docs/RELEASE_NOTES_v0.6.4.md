# Woddi Harbor v0.6.4

This release focuses on NetBox/OpenStack diagnostics and field visibility.

Harbor now keeps a local field catalog for NetBox and OpenStack modules in
`data/runtime/field_cache`. The admin module view exposes a `Felder` action for
NetBox/OpenStack modules. The dialog shows cached resources, known field paths,
filters, errors and raw JSON, and can refresh the catalog from the upstream
service.

OpenStack diagnostics are now structured. Token-only operation remains the
supported mode for users who can only create a token: Harbor does not pass a
separate project to the SDK. If Keystone returns an unscoped token or a token
without a service catalog, diagnostics report the token scope and concrete
hints instead of hiding the failure behind a generic server error.

NetBox upstream HTTP failures are now raised immediately and surfaced as
actionable diagnostics. Anonymous/read-only mode is still enforced, but HTTP
401/403 responses now clearly state that the upstream NetBox API does not allow
anonymous access to that resource.

Harbor startup now performs a best-effort `git pull --ff-only` before serving
traffic when it runs from a clean Git checkout. This applies to `harbor.sh
start` and the generated systemd unit. Set `HARBOR_AUTO_UPDATE=0` to disable
the startup update or `HARBOR_AUTO_UPDATE_STRICT=1` to fail startup when the
pull fails.

Useful checks after upgrading:

```bash
./harbor.sh module diagnose netbox
./harbor.sh module fields netbox --refresh
./harbor.sh module diagnose openstack
OS_TOKEN=<project-scoped-token> ./harbor.sh module fields openstack --refresh
```
