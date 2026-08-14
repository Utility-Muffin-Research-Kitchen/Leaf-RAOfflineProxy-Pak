#!/usr/bin/env bash
# Fetch verify-only sources for the RAOfflineProxy pak: upstream app archive and
# everything in locks/runtime.lock.json. Downloads are cached in workdir/sources
# and always verified against the lock (size + sha256). Builds must use only
# these files; fetching main/latest at package time is not allowed.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
RUNTIME_LOCK="${RUNTIME_LOCK:-$ROOT/locks/runtime.lock.json}"
UPSTREAM_LOCK="${UPSTREAM_LOCK:-$ROOT/locks/upstream.lock.json}"
SOURCES_DIR="${1:-$ROOT/workdir/sources}"
PYTHON="${PYTHON:-python3}"

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing required tool: $1" >&2
    exit 1
  }
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

size_of() {
  if stat -f %z "$1" >/dev/null 2>&1; then
    stat -f %z "$1"
  else
    stat -c %s "$1"
  fi
}

need "$PYTHON"
need curl
mkdir -p "$SOURCES_DIR"

fetch_one() {
  local filename="$1" expected_size="$2" expected_sha="$3" url="$4"
  local dest="$SOURCES_DIR/$filename"
  if [ -f "$dest" ] &&
     [ "$(size_of "$dest")" = "$expected_size" ] &&
     [ "$(sha256_of "$dest")" = "$expected_sha" ]; then
    echo "OK $filename"
    return 0
  fi
  local tmp="$SOURCES_DIR/.download-$(basename "$filename")"
  rm -f "$tmp"
  curl -fL --retry 3 --output "$tmp" "$url"
  local actual_size actual_sha
  actual_size="$(size_of "$tmp")"
  actual_sha="$(sha256_of "$tmp")"
  if [ "$actual_size" != "$expected_size" ]; then
    echo "size mismatch for $filename: expected $expected_size got $actual_size" >&2
    rm -f "$tmp"
    exit 1
  fi
  if [ "$actual_sha" != "$expected_sha" ]; then
    echo "sha256 mismatch for $filename: expected $expected_sha got $actual_sha" >&2
    rm -f "$tmp"
    exit 1
  fi
  mv -f "$tmp" "$dest"
  echo "OK $filename"
}

# runtime.lock.json source_inputs
while IFS=$'\t' read -r filename size sha url; do
  fetch_one "$filename" "$size" "$sha" "$url"
done < <("$PYTHON" - "$RUNTIME_LOCK" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fp:
    lock = json.load(fp)
for item in lock.get("source_inputs", []):
    print("\t".join([
        item["filename"],
        str(item["size"]),
        item["sha256"],
        item["url"],
    ]))
PY
)

# upstream.lock.json app archive
while IFS=$'\t' read -r filename size sha url; do
  fetch_one "$filename" "$size" "$sha" "$url"
done < <("$PYTHON" - "$UPSTREAM_LOCK" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fp:
    lock = json.load(fp)
archive = lock["archive"]
print("\t".join([
    archive["filename"],
    str(archive["size"]),
    archive["sha256"],
    archive["url"],
]))
PY
)
