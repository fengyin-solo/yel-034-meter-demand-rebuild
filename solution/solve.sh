#!/bin/bash
set -euo pipefail
cd /app
install -m 0755 /solution/reference.py air_settlement.py
runuser -u nobody -- python3 air_settlement.py --root /data
