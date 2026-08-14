#!/usr/bin/env bash
# R4 step 2: prove the two-version catalog selects the right package for each
# class of client, against a disposable local feed served over loopback.
#
# Three cases, all driven through Jawaka's real Pak Rat client rather than a
# reimplementation of the selection rule:
#
#   below-minimum  Leaf 0.9.0      -> ungated floor, and the real version is
#                                     reported as gated with its requirement
#   exactly-minimum Leaf 99.99.99  -> real package
#   above-minimum  Leaf 100.0.0    -> real package
#
# A pre-gating client is covered by construction: it reads only the legacy
# fields, which build-catalog-fixture.py asserts are the floor byte-for-byte.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE="$(cd "$ROOT_DIR/.." && pwd)"
JAWAKA_DIR="$WORKSPACE/Jawaka"
LEAF_DIR="$WORKSPACE/Leaf"
APP_ID="org.umrk.raofflineproxy"
FLOOR_VERSION="0.0.1"
REAL_VERSION="0.1.0"
DISPOSABLE_MIN="99.99.99"

PORT="$(python3 - <<'PY'
import socket
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)"
BASE_URL="http://127.0.0.1:$PORT/pakrat/v1/"
FEED_ROOT="$LEAF_DIR/build/pakrat-local/raop"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/raop-catalog-smoke.XXXXXX")"
SERVER_PID=

cleanup() {
    [ -n "$SERVER_PID" ] && { kill "$SERVER_PID" 2>/dev/null || true; wait "$SERVER_PID" 2>/dev/null || true; }
    rm -rf "$TMP_ROOT"
}
trap cleanup EXIT HUP INT TERM

python3 "$ROOT_DIR/scripts/build-catalog-fixture.py" \
    --base-url "$BASE_URL" --min-leaf-version "$DISPOSABLE_MIN" >/dev/null

python3 -m http.server "$PORT" --bind 127.0.0.1 --directory "$FEED_ROOT" \
    >"$TMP_ROOT/http.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 50); do
    curl -fsS "$BASE_URL/storefront.json" >/dev/null 2>&1 && break
    kill -0 "$SERVER_PID" 2>/dev/null || { cat "$TMP_ROOT/http.log" >&2; exit 1; }
    sleep 0.1
done
curl -fsS "$BASE_URL/storefront.json" >/dev/null

BUILD_DIR="build/raop-catalog-smoke"
make -C "$JAWAKA_DIR" BUILD="$BUILD_DIR" jawaka-pakrat-smoke >/dev/null
SMOKE="$JAWAKA_DIR/$BUILD_DIR/bin/jawaka-pakrat-smoke"

SD_ROOT="$TMP_ROOT/sd"
STATE_DIR="$SD_ROOT/.umrk/mlp1"
PLATFORM_ROOT="$SD_ROOT/.system/leaf/platforms/mlp1"
mkdir -p "$STATE_DIR" "$PLATFORM_ROOT"
printf '%s\n' '{"managed_apps":[]}' >"$PLATFORM_ROOT/manifest.json"

write_release() {
    printf '{"schema":1,"product":"leaf","platform":"mlp1","version":"%s","release_id":"%s"}\n' \
        "$1" "$1" >"$STATE_DIR/release.json"
}

run_smoke() {
    PAKRAT_CATALOG_BASE_URL="$BASE_URL" "$SMOKE" \
        --platform mlp1 --sdcard-root "$SD_ROOT" "$@"
}

# ---- Case 1: below minimum -> the inert floor ------------------------------
write_release v0.9.0
below="$TMP_ROOT/below.tsv"
run_smoke list >"$below"
grep -F $'available\t'"$APP_ID"$'\t'"$FLOOR_VERSION"$'\t' "$below" >/dev/null
grep -F $'gated='"$REAL_VERSION"$'\tmin_leaf='"$DISPOSABLE_MIN" "$below" >/dev/null

run_smoke install "$APP_ID" >/dev/null
python3 - "$SD_ROOT/Apps/mlp1/RAOfflineProxy.pak/pak.json" "$FLOOR_VERSION" <<'PY'
import json, sys
manifest = json.load(open(sys.argv[1], encoding="utf-8"))
assert manifest["pak_version"] == sys.argv[2], manifest["pak_version"]
assert "min_leaf_version" not in manifest, "floor must be ungated"
assert "service" not in manifest, "floor must declare no service"
assert manifest.get("icon") == "res/icon.png", "floor lost its icon"
PY
# The floor is inert: no Python runtime, no service entry point, no app tree.
test ! -e "$SD_ROOT/Apps/mlp1/RAOfflineProxy.pak/runtime"
test ! -e "$SD_ROOT/Apps/mlp1/RAOfflineProxy.pak/bin/service-run"
test ! -e "$SD_ROOT/Apps/mlp1/RAOfflineProxy.pak/app"
test -e "$SD_ROOT/Apps/mlp1/RAOfflineProxy.pak/res/icon.png"

# Install-time recheck must refuse the real version on a below-minimum Leaf,
# even when it is named explicitly.
if run_smoke install-target "$APP_ID" "$REAL_VERSION" >/dev/null 2>&1; then
    echo "below-minimum install-time recheck accepted the real pak" >&2
    exit 1
fi
echo "case1 below-minimum -> floor $FLOOR_VERSION ok"

# ---- Case 1b: the runtime gate refuses independently of the catalog --------
# Catalog gating stops a fetch; this is the second, independent refusal that
# protects a pak already sitting on the card when Leaf is rolled back.
set +e
SDCARD_PATH="$SD_ROOT" PLATFORM=mlp1 \
    "$ROOT_DIR/build/mlp1/package/RAOfflineProxy.pak/bin/service-run" \
    >"$TMP_ROOT/runtime-gate.log" 2>&1
runtime_status=$?
set -e
[ "$runtime_status" -eq 64 ] || {
    cat "$TMP_ROOT/runtime-gate.log" >&2
    echo "runtime gate returned $runtime_status instead of 64" >&2
    exit 1
}
grep -F 'refusing to start: below-leaf-minimum' "$TMP_ROOT/runtime-gate.log" >/dev/null
echo "case1b runtime gate refuses below-minimum ok"

# ---- Case 2: exactly the minimum -> the real package -----------------------
write_release v"$DISPOSABLE_MIN"
exact="$TMP_ROOT/exact.tsv"
run_smoke list >"$exact"
grep -F "$APP_ID"$'\t'"$REAL_VERSION"$'\tinstalled='"$FLOOR_VERSION" "$exact" >/dev/null
echo "case2 exactly-minimum -> real $REAL_VERSION ok"

# ---- Case 3: above the minimum -> the real package -------------------------
write_release v100.0.0
above="$TMP_ROOT/above.tsv"
run_smoke list >"$above"
grep -F "$APP_ID"$'\t'"$REAL_VERSION"$'\tinstalled='"$FLOOR_VERSION" "$above" >/dev/null
echo "case3 above-minimum -> real $REAL_VERSION ok"

echo "PASS raop-catalog-selection-smoke"
