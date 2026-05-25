#!/usr/bin/env bash
set -euo pipefail

sudo zypper refresh
sudo zypper install -y \
  python3 \
  python3-pip \
  python3-virtualenv \
  git \
  curl \
  ca-certificates

echo "SLES bootstrap complete."
