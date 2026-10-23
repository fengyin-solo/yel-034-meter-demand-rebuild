#!/bin/bash
set -euo pipefail

LOG_DIR=/logs/verifier
REWARD_FILE="$LOG_DIR/reward.txt"
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
        write_status "INCOMPLETE: verifier exited before weighted scoring completed"
        write_reward 0.000
    fi
}

on_signal() {
    write_status "INCOMPLETE: verifier interrupted before weighted scoring completed"
    write_reward 0.000
    completed=1
    exit 0
}

write_reward 0.000
write_status "INCOMPLETE: pytest has not completed"
trap on_exit EXIT
trap on_signal INT TERM HUP

"$TRUSTED_PYTHON" -m pip install --no-cache-dir --no-index --find-links=/opt/wheels pytest==8.4.1 \
    > /tmp/pytest-install.log 2>&1 || {
    cat /tmp/pytest-install.log
    write_status "INCOMPLETE: pytest installation failed"
    write_reward 0.000
    completed=1
    exit 0
}

RESULT_XML=$(mktemp "$LOG_DIR/pytest-results.XXXXXX")
rm -f "$RESULT_XML"
SITE_PACKAGES=$(env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/tmp \
    "$TRUSTED_PYTHON" -I -S -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')

set +e
env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/tmp LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    PYTHONPATH="$SITE_PACKAGES:/tests" PYTHONNOUSERSITE=1 \
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    "$TRUSTED_TIMEOUT" --signal=TERM --kill-after=10s 300s \
    "$TRUSTED_PYTHON" -S -s -P -B -c 'import sys, pytest; raise SystemExit(pytest.main(sys.argv[1:]))' \
    -v -s -p no:cacheprovider \
    --junitxml="$RESULT_XML" /tests/test_outputs.py > "$TEST_LOG" 2>&1
rc=$?
set -e
cat "$TEST_LOG" 2>/dev/null || true

if [ "$rc" -ne 0 ] && [ "$rc" -ne 1 ]; then
    case "$rc" in
        124|125|126|127|137|143)
            write_status "INCOMPLETE: pytest did not complete; runner exited with code $rc"
            ;;
        *)
            write_status "INCOMPLETE: pytest exited before a scoreable report with code $rc"
            ;;
    esac
    write_reward 0.000
    completed=1
    exit 0
fi

if [ ! -s "$RESULT_XML" ]; then
    write_status "INCOMPLETE: pytest completion report missing"
    write_reward 0.000
    completed=1
    exit 0
fi

if ! env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/tmp \
    "$TRUSTED_PYTHON" -I -S -B /tests/score.py \
    --config /tests/scoring.json \
    --junit "$RESULT_XML" \
    --report-out "$LOG_DIR/scoring-report.json" \
    --reward-out "$REWARD_FILE"; then
    write_status "INCOMPLETE: weighted scoring rejected the completion report"
    write_reward 0.000
    completed=1
    rm -f "$RESULT_XML"
    exit 0
fi

REWARD=$(cat "$REWARD_FILE")
case "$REWARD" in
    0.[0-9][0-9][0-9]|1.000) ;;
    *)
        write_status "INCOMPLETE: weighted scorer produced an invalid reward"
        write_reward 0.000
        completed=1
        rm -f "$RESULT_XML"
        exit 0
        ;;
esac

chmod 0444 "$REWARD_FILE"
write_status "COMPLETED: weighted scoring finished with reward=$REWARD"
completed=1
rm -f "$RESULT_XML"
exit 0
