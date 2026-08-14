#!/bin/sh
# R0 device spike for the patched RAOfflineProxy service.
# Runs ON the MLP1 under the pak-shaped environment, offline-simulated via an
# unreachable TEST-NET upstream (https://192.0.2.1) so probe timeouts exercise
# the Wi-Fi-associated-but-dark path. Usage:
#   r0-offline-spike.sh <runtime-prefix> <app-parent> <work-dir>
set -u

RUNTIME="${1:?runtime prefix (contains bin/python3)}"
APP_PARENT="${2:?directory containing the raofflineproxy package}"
WORK="${3:?scratch directory}"
CONFIG_DIR="$WORK/userdata/RAOfflineProxy"

export PYTHONHOME="$RUNTIME"
export PYTHONPATH="$APP_PARENT"
export LD_LIBRARY_PATH="$RUNTIME/lib"
export SSL_CERT_FILE="$RUNTIME/ca-certificates.crt"
export PYTHONDONTWRITEBYTECODE=1
export RAOFFLINEPROXY_CONFIG_DIR="$CONFIG_DIR"
export RAOFFLINEPROXY_TEST_SKIP_LEASE=1

PY="$RUNTIME/bin/python3"
mkdir -p "$CONFIG_DIR"
rm -f "$CONFIG_DIR/proxy.sqlite3"

http() { wget -q -T 3 -O - "$1" 2>/dev/null || curl -s -m 3 "$1"; }
http_post() { wget -q -T 3 -O - --post-data "$2" "$1" 2>/dev/null || curl -s -m 3 -X POST -d "$2" "$1"; }

# Offline-equivalent service: unreachable upstream. Fixed listener.
UPSTREAM_HOST_OVERRIDE=https://192.0.2.1 nohup "$PY" - "$CONFIG_DIR" > "$WORK/offline-run.out" 2>&1 <<'PYEOF' &
import os
import sys

import threading

from raofflineproxy.proxy_service import run_proxy_service

config = {
    "proxy_host": "127.0.0.1",
    "proxy_port": 8080,
    "upstream_host": os.environ.get("UPSTREAM_HOST_OVERRIDE", "https://192.0.2.1"),
    "cache_images": False,
}
stop = threading.Event()
_SIGNALS = __import__("signal")
_SIGNALS.signal(_SIGNALS.SIGTERM, lambda *a: stop.set())
_SIGNALS.signal(_SIGNALS.SIGINT, lambda *a: stop.set())
run_proxy_service(config, stop)
PYEOF
SVCPID=$!
# cold readiness: how long until health answers ready
i=0
HEALTH=""
while [ $i -lt 60 ]; do
    HEALTH=$(http http://127.0.0.1:8080/leaf/health)
    case "$HEALTH" in *'"ready":true'*) break ;; esac
    i=$((i+1)); sleep 0.25
done
echo "cold health ready after ~$((i*250))ms: $HEALTH"
if ! kill -0 $SVCPID 2>/dev/null; then echo "FATAL: service died"; cat "$WORK/offline-run.out"; exit 1; fi
awk '/VmRSS/{print "rss at ready: "$2" "$3}' /proc/$SVCPID/status

# first offline request pays at most the 500ms probe bound
START=$(date +%s%N)
R1=$(http "http://127.0.0.1:8080/dorequest.php?r=gameid&m=deadbeef")
T1=$(( ($(date +%s%N) - START) / 1000000 ))
echo "first offline request ${T1}ms: $R1"

# subsequent requests reuse the failed reachability result
START=$(date +%s%N)
R2=$(http "http://127.0.0.1:8080/dorequest.php?r=gameid&m=deadbeef")
T2=$(( ($(date +%s%N) - START) / 1000000 ))
echo "second offline request ${T2}ms: $R2"

# queue one casual award offline (user alpha)
QA=$(http_post "http://127.0.0.1:8080/dorequest.php" "r=awardachievement&a=424242&u=alpha&t=faketoken")
echo "offline award queue: $QA"

# second user must NOT queue while alpha's award is pending
QB=$(http_post "http://127.0.0.1:8080/dorequest.php" "r=awardachievement&a=424243&u=beta&t=faketoken2")
echo "cross-account award: $QB"

"$PY" - "$CONFIG_DIR/proxy.sqlite3" <<'PYEOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
print("journal:", c.execute("PRAGMA journal_mode;").fetchone()[0],
      "sync:", c.execute("PRAGMA synchronous;").fetchone()[0])
rows = c.execute(
    "SELECT achievementId,status,length(signature),prevHash,retryCount FROM pending_awards"
).fetchall()
print("pending_awards:", rows)
c.close()
PYEOF

# durability across restart
kill -TERM $SVCPID
sleep 2
if kill -0 $SVCPID 2>/dev/null; then echo "FATAL: did not stop"; kill -9 $SVCPID; exit 1; fi
echo "clean stop ok"
"$PY" - "$CONFIG_DIR/proxy.sqlite3" <<'PYEOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
rows = c.execute("SELECT achievementId,status FROM pending_awards").fetchall()
print("after restart pending rows intact:", rows)
assert rows and rows[0][0] == 424242 and rows[0][1] == "pending", "pending award lost"
print("r0-offline-spike OK")
PYEOF
