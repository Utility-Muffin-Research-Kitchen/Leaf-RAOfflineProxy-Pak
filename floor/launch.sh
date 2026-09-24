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

# The required versions come from the floor's own requirements/ files, the
# same pair the real package's pak.json and release-lock.json carry, so there
# is no second copy of them here to drift.
required() {
    cat "$PAK_DIR/requirements/$1" 2>/dev/null || echo Unknown
}

exec "$PAK_DIR/bin/raofflineproxy-floor" \
    "${1:-$(required min-leaf-version)}" "${2:-Unknown}" \
    "${3:-$(required min-jawaka-version)}" "${4:-Unknown}"
