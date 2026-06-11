# Woddi Harbor v0.3.0

Woddi Harbor 0.3.0 turns the previous deployment snapshot into a cleaner,
local-first product: no runtime service is required, local configuration is separated
from repository defaults, and all managed components can be stopped or removed with
one command.

## Highlights

- `woddi-harbor console` is the single interactive entry point.
- `./harbor.sh stop` stops Harbor, modules, MCP processes and monitoring.
- `./harbor.sh uninstall-runtime` removes managed user services, TLS and monitoring
  while preserving documents, chats, configuration and backups.
- Real operation and customer documents can be imported reproducibly with manifests.
- The external Ollama/OpenAI-compatible LLM connection has health checks, retries,
  model validation and bounded timeouts.
- The included `harbor-ops-mcp` demonstrates install, create, start, discovery,
  tool calls, restart and stop/start recovery.
- Admin module/MCP workflows use forms instead of raw JSON prompts.
- Chat supports explicit module selection, readable code blocks, copying and session
  deletion.

## Runtime Model

Manual operation is the default:

```bash
scripts/install_production.sh manual
./harbor.sh start
./harbor.sh console
./harbor.sh stop
```

systemd, local TLS and Prometheus remain optional deployment features. They are not
installed by default and were removed from the verified development host after their
end-to-end validation.

## Verification

- 56 tests passed
- Ruff, Python compile and JavaScript syntax checks passed
- Security check reported no findings
- Dependency audit reported no known vulnerabilities
- Production check passed with real local sources
- Health load test: 10,000 requests, concurrency 64, zero errors, p95 88.04 ms
- Readiness load test: 5,000 requests, concurrency 32, zero errors, p95 38.36 ms
- LLM and MCP outage/recovery behavior verified

## Target Hardware

The required release benchmark on 128 GiB RAM and four CPU sockets has not been
executed because no accessible host matches that profile. The available host has
15.51 GiB RAM, one socket and 24 logical CPUs. Harbor now enforces this distinction
with `tools/hardware_gate.py`; substitute-host measurements cannot certify the target.
