#!/bin/sh
# Regression: offline play must keep working AFTER an award has been queued.
#
# Upstream v1.11.1-alpha1 fixed exactly this on Android -- every game started
# after the first offline unlock failed with "no cached response" until the
# proxy was restarted. That is the single most damaging shape this pak can
# have, because it looks like success right up until the user is offline with
# a queued award and a second game that will not start.
#
# The Android mechanism cannot occur here (thread-per-request rather than a
# fixed worker pool; a blocking RLock rather than a 3-second latch that falls
# through as a cache miss), but "cannot occur" is a claim about code, and this
# is the claim worth testing rather than asserting.
#
# Runs ON the MLP1 against a COPY of the live cache, on a spare port, with an
# unreachable TEST-NET upstream so the service is genuinely offline. The live
# service and the real database are never touched. Usage:
#   offline-after-award-test.sh <pak-dir> <live-config-dir> <work-dir> [port]
set -u

PAK="${1:?pak directory}"
LIVE="${2:?live config dir (source of the cache copy)}"
WORK="${3:?scratch directory}"
PORT="${4:-8099}"

RT="$PAK/runtime"
CONFIG_DIR="$WORK/config"

export PYTHONHOME="$RT"
export PYTHONPATH="$PAK/app"
export LD_LIBRARY_PATH="$RT/lib"
export PYTHONDONTWRITEBYTECODE=1
export RAOFFLINEPROXY_CONFIG_DIR="$CONFIG_DIR"
export RAOFFLINEPROXY_TEST_SKIP_LEASE=1
PY="$RT/bin/python3"

rm -rf "$WORK"
mkdir -p "$CONFIG_DIR"
cp "$LIVE/proxy.sqlite3" "$CONFIG_DIR/proxy.sqlite3" || exit 1
# Offline awards are signed with this key; without it the queue path differs.
[ -f "$LIVE/award_secret.key" ] && cp "$LIVE/award_secret.key" "$CONFIG_DIR/"

http() { wget -q -T 5 -O - "$1" 2>/dev/null; }
http_post() { wget -q -T 5 -O - --post-data "$2" "$1" 2>/dev/null; }

RAPROXY_PORT="$PORT" nohup "$PY" - > "$WORK/run.out" 2>&1 <<'PYEOF' &
import os
import signal
import threading

from raofflineproxy.proxy_service import run_proxy_service

config = {
    "proxy_host": "127.0.0.1",
    "proxy_port": int(os.environ["RAPROXY_PORT"]),
    # TEST-NET-1: routable nowhere, so probes fail the way a dark network does.
    "upstream_host": "https://192.0.2.1",
    "cache_images": False,
}
stop = threading.Event()
signal.signal(signal.SIGTERM, lambda *a: stop.set())
signal.signal(signal.SIGINT, lambda *a: stop.set())
run_proxy_service(config, stop)
PYEOF
SVCPID=$!
trap 'kill $SVCPID 2>/dev/null' EXIT HUP INT TERM

i=0
while [ $i -lt 80 ]; do
    case "$(http "http://127.0.0.1:$PORT/leaf/health")" in
        *'"ready":true'*) break ;;
    esac
    i=$((i + 1))
    sleep 0.25
done
if ! kill -0 $SVCPID 2>/dev/null; then
    echo "FATAL: service died on startup"
    cat "$WORK/run.out"
    exit 1
fi
echo "service ready on port $PORT (offline: upstream 192.0.2.1)"

USER=helaas
TOKEN=faketoken
BASE="http://127.0.0.1:$PORT/dorequest.php"

# game_id:rom_hash for every title in the live cache. GAME_A takes the award;
# all of them must still answer from cache afterwards.
GAME_A=1446
GAME_A_HASH=8e3630186e35d477231bf8fd50e54cdd
GAME_A_UNLOCKED=3159
GAMES="1446:8e3630186e35d477231bf8fd50e54cdd 11279:3e9eb765a7b2912e60c30f363f0a3574 15998:6a480004891ae5d942e62afdbbbd1b52 2:a6fc42bd5d19e36e5d8a2d8c4f4e9d23"
# Must be an achievement the account has NOT already unlocked: queue_award
# treats an already-unlocked award as a satisfied no-op and returns success
# without writing a row, so a previously earned id would read as a queue
# failure that never happened. Picked from patch:1446 minus its UserUnlocks.
AWARD_ID=27181

fail=0

# Assert on cached CONTENT, never on "Success":true. Offline startsession is
# synthesized -- build_offline_start_session_response returns Success with an
# empty session when the cache misses -- so a success flag is satisfied by a
# total cache failure. "GameId":<id> and a specific unlock id can only come
# from a real cache read. A mutant that makes get_cache return None while an
# award is pending passes the Success check and fails these.
expect() {
    label="$1"; needle="$2"; haystack="$3"
    case "$haystack" in
        *"$needle"*) echo "PASS  $label" ;;
        *) echo "FAIL  $label (no $needle in: $(echo "$haystack" | cut -c1-140))"; fail=1 ;;
    esac
}

check_game() {
    gid="$1"; ghash="$2"; when="$3"
    R=$(http_post "$BASE" "r=achievementsets&u=$USER&t=$TOKEN&m=$ghash")
    expect "game $gid achievementsets from cache $when" "\"GameId\":$gid" "$R"
}

# Baseline: the awarded game reads back its real unlock before anything queues.
R=$(http_post "$BASE" "r=startsession&u=$USER&t=$TOKEN&g=$GAME_A&h=0&m=$GAME_A_HASH&l=12.1")
expect "game $GAME_A startsession carries unlock $GAME_A_UNLOCKED (before award)" \
    "\"ID\":$GAME_A_UNLOCKED" "$R"
check_game "$GAME_A" "$GAME_A_HASH" "(before award)"

# Queue the offline award. This is the state that broke Android.
R=$(http_post "$BASE" "r=awardachievement&u=$USER&t=$TOKEN&a=$AWARD_ID&h=0&m=$GAME_A_HASH&v=deadbeef")
echo "award queued offline: $R"

QUEUED=$("$PY" - "$CONFIG_DIR/proxy.sqlite3" <<'PYEOF'
import sqlite3
import sys

db = sqlite3.connect(sys.argv[1])
print(db.execute(
    "SELECT count(*) FROM pending_awards WHERE status = 'pending'").fetchone()[0])
PYEOF
)
if [ "$QUEUED" -ge 1 ]; then
    echo "PASS  $QUEUED award(s) pending"
else
    echo "FAIL  award did not queue (pending=$QUEUED)"
    fail=1
fi

# The regression itself: every game must still read from cache afterwards.
for entry in $GAMES; do
    gid=${entry%%:*}
    ghash=${entry##*:}
    check_game "$gid" "$ghash" "AFTER the queued award"
done

# The awarded game must still replay, still see its old unlock, and now also
# see the pending one merged in.
R=$(http_post "$BASE" "r=startsession&u=$USER&t=$TOKEN&g=$GAME_A&h=0&m=$GAME_A_HASH&l=12.1")
expect "game $GAME_A still carries unlock $GAME_A_UNLOCKED after its own award" \
    "\"ID\":$GAME_A_UNLOCKED" "$R"
expect "game $GAME_A merges the pending award $AWARD_ID into its session" \
    "\"ID\":$AWARD_ID" "$R"

if [ $fail -eq 0 ]; then
    echo "offline-after-award: ok"
else
    echo "offline-after-award: FAILED"
fi
exit $fail
