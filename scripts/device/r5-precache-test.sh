#!/bin/sh
# R5 acceptance: a game that has never been launched must play offline after
# being pre-cached.
#
# That is the whole feature in one sentence, and it is worth testing directly
# because every part of it can fail silently. The hash must equal the one
# rcheevos computes inside RetroArch, or the data is stored under a key nothing
# looks up; the stored shape must match what the offline responder reads; and
# "it returned Success" proves neither, because offline startsession is
# synthesized and answers Success on a total cache miss.
#
# Runs ON the MLP1. The pre-cache half needs the network and drives the live
# service; the verification half runs a second instance on a spare port against
# a COPY of the database with an unreachable upstream, so the live service and
# the real cache are never touched.
#
#   r5-precache-test.sh <pak-dir> <live-config-dir> <game-id> [port]
#
# <game-id> is a Jawaka library id -- one that has never been played, so the
# test proves pre-caching rather than re-reading what a launch already cached.
set -u

PAK="${1:?pak directory}"
LIVE="${2:?live config dir}"
GAME_ID="${3:?jawaka library game id (never played)}"
PORT="${4:-8097}"

RT="$PAK/runtime"
PROXY="http://127.0.0.1:8080"
fail=0

http() { wget -q -T 8 -O - "$1" 2>/dev/null; }
post() { wget -q -T 15 -O - --post-data "$2" --header="Content-Type: application/json" "$1" 2>/dev/null; }
form() { wget -q -T 8 -O - --post-data "$2" "$1" 2>/dev/null; }

expect() {
    case "$3" in
        *"$2"*) echo "PASS  $1" ;;
        *) echo "FAIL  $1 (no $2 in: $(echo "$3" | cut -c1-140))"; fail=1 ;;
    esac
}

# -- 1. pre-cache it through the live service ---------------------------------
case "$(http "$PROXY/leaf/health")" in
    *'"ready":true'*) ;;
    *) echo "FATAL: live service is not ready on 8080"; exit 1 ;;
esac

START=$(post "$PROXY/leaf/precache/start" "{\"scope\":\"games\",\"game_ids\":[$GAME_ID]}")
expect "pre-cache run accepted" '"started":true' "$START"

STATUS=""
i=0
while [ $i -lt 40 ]; do
    STATUS=$(http "$PROXY/leaf/precache/status")
    case "$STATUS" in
        *'"state":"done"'*|*'"state":"failed"'*|*'"state":"cancelled"'*) break ;;
    esac
    i=$((i + 1))
    sleep 2
done
echo "final status: $STATUS"
expect "run completed" '"state":"done"' "$STATUS"

# Pull the hash and RA id the run reported, so verification uses what was
# actually stored rather than values hardcoded here.
ROM_HASH=$(echo "$STATUS" | sed 's/.*"rom_hash":"\([0-9a-f]*\)".*/\1/')
RA_ID=$(echo "$STATUS" | sed 's/.*"ra_game_id":\([0-9]*\).*/\1/')
echo "rom_hash=$ROM_HASH ra_game_id=$RA_ID"

case "$STATUS" in
    *'"status":"cached"'*) ;;
    *'"status":"skipped"'*)
        echo "NOTE  game was already cached; pick an unplayed id to prove pre-caching"
        ;;
    *)
        echo "FAIL  game was not cached; nothing to verify offline"
        exit 1
        ;;
esac

# -- 2. verify it offline, against a copy -------------------------------------
W=/tmp/r5-precache-verify
rm -rf "$W"
mkdir -p "$W/config"
cp "$LIVE/proxy.sqlite3" "$W/config/" || exit 1

export PYTHONHOME="$RT"
export PYTHONPATH="$PAK/app"
export LD_LIBRARY_PATH="$RT/lib"
export PYTHONDONTWRITEBYTECODE=1
export RAOFFLINEPROXY_CONFIG_DIR="$W/config"
export RAOFFLINEPROXY_TEST_SKIP_LEASE=1

RAPROXY_PORT="$PORT" nohup "$RT/bin/python3" - > "$W/run.out" 2>&1 <<'PYEOF' &
import os
import signal
import threading

from raofflineproxy.proxy_service import run_proxy_service

config = {
    "proxy_host": "127.0.0.1",
    "proxy_port": int(os.environ["RAPROXY_PORT"]),
    "upstream_host": "https://192.0.2.1",  # TEST-NET-1: genuinely offline
    "cache_images": False,
}
stop = threading.Event()
signal.signal(signal.SIGTERM, lambda *a: stop.set())
run_proxy_service(config, stop)
PYEOF
SVC=$!
trap 'kill $SVC 2>/dev/null; rm -rf "$W"' EXIT HUP INT TERM

i=0
while [ $i -lt 40 ]; do
    case "$(http "http://127.0.0.1:$PORT/leaf/health")" in
        *'"ready":true'*) break ;;
    esac
    i=$((i + 1))
    sleep 0.25
done
echo "offline verifier up on $PORT"

B="http://127.0.0.1:$PORT/dorequest.php"

# Assert on cached CONTENT. "Success":true is worthless here: offline
# startsession synthesizes an empty session and reports success on a miss.
R=$(form "$B" "r=achievementsets&u=Helaas&t=tok&m=$ROM_HASH")
expect "achievementsets served offline" "\"GameId\":$RA_ID" "$R"

R=$(form "$B" "r=gameid&m=$ROM_HASH")
expect "gameid resolves offline (derived, cost no request)" "$RA_ID" "$R"

R=$(form "$B" "r=startsession&u=Helaas&t=tok&g=$RA_ID&h=0&m=$ROM_HASH&l=12.1")
expect "startsession served offline" '"Success":true' "$R"

# Control: a hash nothing ever cached must miss, or the assertions above prove
# nothing about the cache.
R=$(form "$B" "r=achievementsets&u=Helaas&t=tok&m=00000000000000000000000000000000")
case "$R" in
    *'"GameId"'*) echo "FAIL  control hash returned a game"; fail=1 ;;
    *) echo "PASS  control hash missed, as it must" ;;
esac

if [ $fail -eq 0 ]; then
    echo "r5-precache: ok"
else
    echo "r5-precache: FAILED"
fi
exit $fail
