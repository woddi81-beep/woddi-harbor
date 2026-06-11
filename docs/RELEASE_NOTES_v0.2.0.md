# Woddi Harbor v0.2.0

Woddi Harbor 0.2.0 is the first product-shaped release of the self-hosted AI control
plane. It connects an external LLM with controlled document sources, infrastructure
modules and managed MCP services.

## Highlights

- Streaming browser chat with persistent sessions
- Administrative portal for modules, sources, users, MCPs, jobs, audit, backups and
  services
- Durable SQLite job queue with a dedicated worker
- Managed local and Git sources with content quality checks
- MCP package installation and lifecycle management, including rollback
- Role-based access and per-user module/tool allowlists
- Prometheus metrics, Grafana dashboard, backup timer and production preflight
- CI-tested Python 3.10/3.12 package with wheel and source distribution

## Architecture

The release separates the API, background job worker, local module workers and MCP
instances. The LLM remains on an external server. Harbor binds to loopback and is
published through a TLS reverse proxy.

The default deployment uses four API workers and one durable job worker. This is a
conservative starting point for the target host with 128 GB RAM and four CPU sockets;
further worker tuning must be based on NUMA-aware load measurements.

## Security

- Harbor fails closed until an active administrator exists.
- Passwords use PBKDF2 hashes.
- Browser writes are same-origin protected.
- Local workers require an internal bearer token.
- Module and tool permissions are enforced server-side.
- MCP package manifests and process paths are validated.
- Secrets and runtime state are excluded from version control.

## Operations

Templates are included for:

- systemd API, job worker, module and backup services
- Nginx or Caddy TLS termination
- logrotate
- Prometheus scraping
- Grafana dashboards

Operational procedures are documented in:

- `docs/INSTALL.md`
- `docs/HOWTO.md`
- `docs/OPERATIONS.md`
- `docs/RUNBOOK.md`
- `docs/UPGRADE.md`

## Verification

The release gate includes:

- 43 unit and API structure tests
- Ruff linting
- focused Mypy checks
- secret scanning
- dependency vulnerability audit
- Python 3.10 and 3.12 CI
- wheel and source distribution builds

## Known Production Blocker

The repository currently contains only tiny placeholder files for the operation and
customer document sources. `woddi-harbor production-check` intentionally fails until
real source content is imported and both sources pass the quality gate.

## Upgrade

Create a pre-upgrade backup, install the new package, run `production-check`, then
start the API, job worker and module services. Schema migration version 2 is applied
automatically. See `docs/UPGRADE.md` for the full procedure.
