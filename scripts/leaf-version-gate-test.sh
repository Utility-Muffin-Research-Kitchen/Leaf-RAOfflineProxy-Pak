#!/bin/sh
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/raop-gate.XXXXXX")
trap 'rm -rf "$TMP"' EXIT

cat >"$TMP/manifest.json" <<'JSON'
{"min_leaf_version":"0.11.0","min_jawaka_version":"0.11.0"}
JSON

run_gate() {
    set +e
    (
        . "$ROOT/lib/leaf-version-gate.sh"
        leaf_version_gate_manifest "$TMP/manifest.json" "$TMP/release.json"
    )
    actual=$?
    set -e
}

# Hypothetical future release.json that also publishes jawaka_version.
check() {
    expected=$1
    leaf=$2
    jawaka=$3
    cat >"$TMP/release.json" <<JSON
{"version":"$leaf","jawaka_version":"$jawaka"}
JSON
    run_gate
    [ "$actual" -eq "$expected" ] || {
        echo "gate expected $expected, got $actual for Leaf=$leaf Jawaka=$jawaka" >&2
        exit 1
    }
}

# The shape the managed installer actually writes today: no jawaka_version.
# This is the case every real device hits, so it is the one that must not
# regress into a refusal.
check_real_release_json() {
    expected=$1
    leaf=$2
    cat >"$TMP/release.json" <<JSON
{
  "schema": 1,
  "product": "leaf",
  "platform": "mlp1",
  "version": "$leaf",
  "release_id": "leaf-mlp1-sd-2026-08-14-gabcdef0",
  "installed_at": "2026-08-14T00:00:00Z",
  "source": "managed-install"
}
JSON
    run_gate
    [ "$actual" -eq "$expected" ] || {
        echo "gate expected $expected, got $actual for real release.json Leaf=$leaf" >&2
        exit 1
    }
}

check 0 0.11.0 0.11.0
check 67 0.10.0-beta.3 0.11.0
check 67 0.11.0 0.10.0
# Jawaka version absent/blank is advisory: Leaf alone decides.
check 0 0.11.0 ""
check 0 0.12.0 "not-a-version"

check_real_release_json 0 0.11.0
check_real_release_json 0 0.12.3
check_real_release_json 67 0.10.9

# A missing release.json is still unknown-installed-Leaf and refuses.
rm -f "$TMP/release.json"
run_gate
[ "$actual" -eq 66 ] || {
    echo "gate expected 66 for a missing release.json, got $actual" >&2
    exit 1
}

echo "leaf-version-gate-test: ok"
