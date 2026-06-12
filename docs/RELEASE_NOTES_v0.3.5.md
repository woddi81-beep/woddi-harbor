# Woddi Harbor v0.3.5

Local Docs and Maildir modules no longer require a running HTTP worker.

The central module execution path now runs local search, statistics and reindex
operations directly. This applies consistently to CLI calls, chat context retrieval
and administration endpoints.

After upgrading, document search works while Harbor is stopped:

```bash
.venv/bin/woddi-harbor module call 10 search \
  --payload '{"query":"Installation","top_k":5}'
```
