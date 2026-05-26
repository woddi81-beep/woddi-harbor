#!/bin/bash
cd "$(dirname "$0")"

LOGDIR="$HOME/.harbor/logs"
mkdir -p "$LOGDIR"
LOGFILE="$LOGDIR/harbor.log"
exec .venv/bin/uvicorn app.control:create_app --factory --host 127.0.0.1 --port 9680 --access-log 2>> "$LOGFILE"