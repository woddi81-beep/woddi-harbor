# Woddi Harbor v0.3.14

`./harbor.sh start` now starts Uvicorn explicitly on `0.0.0.0:9680`. It no
longer depends on a potentially stale stored loopback address.

An intentional override remains possible:

```bash
HARBOR_HOST=127.0.0.1 HARBOR_PORT=9680 ./harbor.sh start
```
