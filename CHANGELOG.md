# Changelog

## 0.3.14 - 2026-06-12

### Fixed

- `harbor.sh start` always passes an explicit listen address to Uvicorn
- Manual startup no longer falls back to stale loopback configuration
- `HARBOR_HOST` and `HARBOR_PORT` remain explicit runtime overrides

## 0.3.13 - 2026-06-12

### Changed

- Harbor now listens on `0.0.0.0` by default
- Legacy implicit `127.0.0.1` configurations migrate once to the external bind
- Explicit binds configured through `server set` remain unchanged

## 0.3.12 - 2026-06-12

### Changed

- OpenStack project and project domain are optional for project-scoped tokens
- Harbor only applies Keystone project scoping when a project name is configured
- Existing project-scoped tokens use `token` authentication without project variables

## 0.3.11 - 2026-06-12

### Fixed

- OpenStack token authentication uses the Keystone v3 token plugin
- OpenStack tokens can be scoped with project name and project domain
- The admin portal exposes project domain with the conventional `Default` value

## 0.3.10 - 2026-06-12

### Fixed

- `module call` accepts JSON both as a third positional argument and through `--payload`
- Existing NetBox and OpenStack command examples work as documented

## 0.3.9 - 2026-06-12

### Fixed

- OpenStack MCP workers locate `openstack` next to the active virtualenv Python
- OpenStack CLI errors include the exact installation command and expected path
- `OPENSTACK_CLI` can explicitly select a client binary

## 0.3.8 - 2026-06-12

### Added

- OpenStack token authentication with project, Identity URL and region configuration in the admin portal
- Private per-module secret storage with directory mode `0700` and file mode `0600`
- OpenStack token status without exposing the stored token to browsers or module status output

### Fixed

- Module diagnostics now report connection failures as structured results instead of raising `Errno 111`
- Local NetBox, OpenStack and SAP MCP modules can be inspected with `module discover`

## 0.3.7 - 2026-06-12

### Added

- Persistent `server set` and `server show` CLI commands
- Explicit external binding support for protected networks

### Changed

- `harbor.sh start` uses persisted server settings unless environment overrides are set

## 0.3.6 - 2026-06-12

### Fixed

- Workerless Docs and Maildir modules no longer require a network port
- Workerless search modules are excluded from local port-conflict validation
- Document source setup resets stale transport and endpoint fields

## 0.3.5 - 2026-06-12

### Fixed

- Local Docs and Maildir calls execute directly without an HTTP worker
- CLI document search works while Harbor and module processes are stopped
- Chat and administration use the same workerless local search path

## 0.3.4 - 2026-06-12

### Fixed

- Source synchronization reindexes local document modules without an HTTP worker
- Manual operation no longer fails source sync with connection refused
- Sync output reports whether reindexing used direct or transported execution

## 0.3.3 - 2026-06-12

### Added

- HTML documentation is normalized to visible text before indexing
- PNG documentation assets are preserved alongside Markdown and HTML
- Source quality reports distinguish searchable text from binary assets

## 0.3.2 - 2026-06-12

### Fixed

- Removed all hard-coded ASV documentation references from the active product
- Added host-local document source configuration in `config/sources.local.json`
- Added one-command setup for the production operation and customer Markdown repositories
- Document synchronization now excludes non-Markdown repository artifacts

## 0.3.1 - 2026-06-12

### Fixed

- Installation now rejects incomplete source checkouts before starting Harbor
- Post-install verification detects missing modules and foreign top-level `app` packages
- Production and shell installers verify the effective Python import origin

## 0.3.0 - 2026-06-11

### Added

- Real reference-document importer with source manifests and quality verification
- Resilient Ollama/OpenAI-compatible LLM health checks, retries and timeout controls
- Canonical interactive console through `woddi-harbor console`
- Idempotent `runtime stop-all` and `runtime uninstall` commands
- Own process-based MCP example with full discovery and tool-call flow
- Hardware release gate for the 128 GiB / four-socket production target
- Optional local TLS and authenticated Prometheus installers
- Structured admin forms and improved chat module selection and session handling

### Changed

- systemd, TLS and monitoring are explicitly optional runtime components
- Local module configuration is stored in ignored `config/modules.local.json`
- The versioned module default is empty and contains no deployment-specific endpoints
- Configuration locks are stored under `data/runtime/locks`
- Obsolete embedded web applications were removed in favor of packaged web assets

### Fixed

- Read-only systemd workers no longer fail while reading configuration
- Production installer keeps the source checkout editable and finds deployment assets
- CLI module failures no longer print full Python tracebacks
- Tests no longer leave document indexes in the production runtime directory

### Verification

- 56 automated tests, Ruff, compile checks, JavaScript syntax checks and dependency audit
- 10,000 health requests at concurrency 64: zero errors, p95 88.04 ms
- 5,000 readiness requests at concurrency 32: zero errors, p95 38.36 ms
- Controlled LLM and MCP failure/recovery tests

### Known Limitation

- The required 128 GiB / four-socket benchmark remains blocked until that host is
  available. The available host has 15.51 GiB RAM and one CPU socket; its results do
  not count as target-hardware certification.

## 0.2.0 - 2026-06-10

### Added

- Separate streaming chat and administration web applications
- Persistent chat sessions, audit events and durable SQLite job queue
- User, role, module and tool permission administration
- Managed local and Git document sources with quality gates
- MCP package and instance lifecycle for HTTP, process, systemd and container drivers
- MCP install, start, stop, restart, upgrade and rollback workflows
- Backup/restore, backup timer and production readiness gate
- Prometheus metrics, Grafana dashboard and load-test thresholds
- Nginx and Caddy TLS reverse-proxy templates
- CI for Python 3.10 and 3.12, release packaging, Ruff, Mypy and dependency audit
- Product, SLO, privacy, security, installation, upgrade and operations documentation

### Changed

- API defaults to four workers for the 128 GB / multi-socket target host
- Background work moved from process-local threads to a dedicated persistent worker
- Web assets are packaged into the Python wheel
- SQLite connections are explicitly closed and schema migration version 2 is applied

### Security

- Fail-closed authentication when no active user exists
- PBKDF2 password hashes and role-based authorization
- Per-user module and tool allowlists
- Internal bearer authentication between Harbor and local workers
- Secret redaction, same-origin write protection and hardened systemd units

### Known Limitations

- The checked-in operation and customer document sources are placeholders and do not
  pass the production source-quality gate.
- Harbor 0.2.0 is designed for one Harbor host with multiple API workers. Multi-host
  high availability requires an external state database in a later release.

## 0.1.0

- Initial Harbor control plane, local search workers and MCP integration
