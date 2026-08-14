#!/bin/sh
set -eu

PAK_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
PLATFORM=${PLATFORM:-mlp1}

if [ -n "${UMRK_ENV_FILE:-}" ] && [ -f "$UMRK_ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$UMRK_ENV_FILE"
elif [ -n "${SDCARD_PATH:-}" ] &&
     [ -f "$SDCARD_PATH/.system/leaf/platforms/$PLATFORM/launcher/env.sh" ]; then
    # shellcheck disable=SC1090
    . "$SDCARD_PATH/.system/leaf/platforms/$PLATFORM/launcher/env.sh"
fi

SDCARD_PATH=${SDCARD_PATH:-/mnt/sdcard}
. "$PAK_DIR/lib/leaf-version-gate.sh"
gate_status=0
leaf_version_gate_manifest "$PAK_DIR/pak.json" \
    "$SDCARD_PATH/.umrk/$PLATFORM/release.json" || gate_status=$?
case "$gate_status" in
    0) ;;
    *)
        exec "$PAK_DIR/bin/raofflineproxy-floor" \
            "${LEAF_REQUIRED_VERSION:-Invalid}" \
            "${LEAF_INSTALLED_VERSION:-Unknown}" \
            "${JAWAKA_REQUIRED_VERSION:-Invalid}" \
            "${JAWAKA_INSTALLED_VERSION:-Unknown}"
        ;;
esac

exec "$PAK_DIR/bin/raofflineproxy-ui"
