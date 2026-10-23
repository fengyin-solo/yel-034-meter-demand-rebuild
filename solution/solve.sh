#!/bin/bash
set -euo pipefail
cd /app
install -m 0755 /solution/reference.py rebuild_bills.py
runuser -u nobody -- python3 rebuild_bills.py --root /data
