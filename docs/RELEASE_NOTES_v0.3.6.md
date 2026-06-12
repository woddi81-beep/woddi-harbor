# Woddi Harbor v0.3.6

Workerless local Docs and Maildir modules no longer require a TCP port.

This resolves production-check failures such as:

```text
2 Module, 2 ungueltig
Port ungueltig: 0
```

After upgrading, run `source configure-docs` once to normalize modules `10` and `11`,
then synchronize both sources and rerun `production-check`.
