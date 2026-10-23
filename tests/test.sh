#!/bin/bash
set -euo pipefail

LOG_DIR=/logs/verifier
REWARD_FILE="/logs/verifier/reward.txt"
STATUS_FILE="$LOG_DIR/verification-status.txt"
TEST_LOG="$LOG_DIR/test-stdout.txt"
TRUSTED_PYTHON=/usr/local/bin/python3
TRUSTED_TIMEOUT=/usr/bin/timeout
completed=0
mkdir -p "$LOG_DIR"

write_reward() {
  chmod 0644 "$REWARD_FILE" 2>/dev/null || true
  printf '%s\n' "$1" > /logs/verifier/reward.txt
  chmod 0444 "$REWARD_FILE"
}

write_status() {
  chmod 0644 "$STATUS_FILE" 2>/dev/null || true
  printf '%s\n' "$1" > "$STATUS_FILE"
  chmod 0444 "$STATUS_FILE"
}

on_exit() {
  if [ "$completed" -ne 1 ]; then
    write_status "INCOMPLETE: verifier exited before pytest completion"
    write_reward 0.000
  fi
}

on_signal() {
  write_status "INCOMPLETE: verifier interrupted before pytest completion"
  write_reward 0.000
  completed=1
  exit 0
}

write_reward 0.000
write_status "INCOMPLETE: pytest has not completed"
trap on_exit EXIT
trap on_signal INT TERM HUP

if ! printf '%s  %s\n' \
  '9121e2c1fdb355232495be3194c8dfe87ccc2d5dee45947b78e68f499790d7a7' '/opt/wheels/iniconfig-2.3.1-py3-none-any.whl' \
  'd7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c' '/opt/wheels/packaging-26.3-py3-none-any.whl' \
  'e920276dd6813095e9377c0bc5566d94c932c33b27a3e3945d8389c374dd4746' '/opt/wheels/pluggy-1.6.0-py3-none-any.whl' \
  '86540386c03d588bb81d44bc3928634ff26449851e99741617ecb9037ee5ec0b' '/opt/wheels/pygments-2.19.2-py3-none-any.whl' \
  '539c70ba6fcead8e78eebbf1115e8b589e7565830d7d006a8723f19ac8a0afb7' '/opt/wheels/pytest-8.4.1-py3-none-any.whl' \
  | sha256sum -c - > /tmp/pytest-wheel-check.log 2>&1; then
  cat /tmp/pytest-wheel-check.log
  write_status "INCOMPLETE: pytest wheel integrity check failed"
  write_reward 0.000
  completed=1
  exit 0
fi

SITE_PACKAGES=$("$TRUSTED_PYTHON" -S -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
env -i \
  HOME=/tmp \
  LANG=C.UTF-8 \
  PATH=/usr/local/bin:/usr/bin:/bin \
  PYTHONNOUSERSITE=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="$SITE_PACKAGES:/tests" \
  "$TRUSTED_PYTHON" -S -P -B -m pip install --force-reinstall --no-cache-dir --no-index --find-links=/opt/wheels pytest==8.4.1 >/tmp/pytest-install.log 2>&1 || {
  cat /tmp/pytest-install.log
  write_status "INCOMPLETE: pytest dependency unavailable"
  write_reward 0.000
  completed=1
  exit 0
}

REPORT=$(mktemp "$LOG_DIR/pytest-results.XXXXXX")
rm -f "$REPORT"
set +e
env -i \
  HOME=/tmp \
  LANG=C.UTF-8 \
  PATH=/usr/local/bin:/usr/bin:/bin \
  PYTHONNOUSERSITE=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="$SITE_PACKAGES:/tests" \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  "$TRUSTED_TIMEOUT" --signal=TERM --kill-after=10s 300s \
  "$TRUSTED_PYTHON" -S -s -P -B -m pytest -v -p no:cacheprovider \
  --junitxml="$REPORT" /tests/test_outputs.py > "$TEST_LOG" 2>&1
RC=$?
set -e
cat "$TEST_LOG" 2>/dev/null || true

if [ "$RC" -ne 0 ] && [ "$RC" -ne 1 ]; then
  write_status "INCOMPLETE: pytest did not complete; runner exited with code $RC"
  write_reward 0.000
elif [ ! -s "$REPORT" ]; then
  write_status "INCOMPLETE: pytest completion report missing"
  write_reward 0.000
else
  if env -i \
    HOME=/tmp \
    LANG=C.UTF-8 \
    PATH=/usr/local/bin:/usr/bin:/bin \
    PYTHONNOUSERSITE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="$SITE_PACKAGES:/tests" \
    "$TRUSTED_PYTHON" -S -P -B /tests/score.py \
    --config /tests/scoring.json --junit "$REPORT" \
    --report-out "$LOG_DIR/scoring-report.json" --reward-out "$REWARD_FILE"; then
    REWARD=$(cat "$REWARD_FILE")
    case "$REWARD" in
      0.[0-9][0-9][0-9]|1.000) ;;
      *) write_reward 0.000 ;;
    esac
    chmod 0444 "$REWARD_FILE"
    write_status "COMPLETED: weighted scoring finished with reward=$(cat "$REWARD_FILE")"
  else
    write_status "INCOMPLETE: weighted scorer rejected the report"
    write_reward 0.000
  fi
fi
completed=1
rm -f "$REPORT"
exit 0
