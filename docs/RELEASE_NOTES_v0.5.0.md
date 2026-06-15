# Woddi Harbor v0.5.0

This release tightens production behavior for the dedicated infrastructure
integrations.

- NetBox is anonymous and strictly read-only.
- OpenStack accepts only a project-scoped user token. The project context comes
  from Keystone token metadata; Harbor never guesses or re-scopes it.
- Identical concurrent upstream requests share one load and all in-memory caches
  are bounded.
- `/api/ready` returns HTTP 503 when the configured LLM is unavailable.
- `production-check` verifies live LLM and enabled integration health.
- Managed MCP instances restore their persisted desired state on Harbor startup.
- New installations bind to `127.0.0.1` unless an external address is explicitly
  configured.

After upgrading, save the NetBox and OpenStack integrations once in the admin
portal to remove obsolete token and credential secrets. OpenStack requires a new
project-scoped user token if the existing token is unscoped.
