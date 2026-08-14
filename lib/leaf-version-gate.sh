#!/bin/sh

# Runtime compatibility gate for both the real and inert-floor
# RAOfflineProxy packages.
#
# Leaf's installed version is authoritative and always present: the managed
# installer writes {"version": ...} into .umrk/$PLATFORM/release.json.
#
# There is deliberately NO hard runtime gate on Jawaka. Nothing on the device
# publishes an installed Jawaka version -- release.json carries schema,
# product, platform, version, release_id, installed_at and source, and no
# component exports UMRK_JAWAKA_VERSION or JAWAKA_VERSION -- so requiring one
# would fail closed on every real install and the pak could never run. Jawaka
# ships inside the Leaf release payload, so the Leaf version already fixes the
# Jawaka version; min_jawaka_version in pak.json stays as the declaration
# Jawaka's discovery records. If a future Leaf publishes jawaka_version, this
# gate picks it up and enforces it; until then an absent value is "not
# determinable", never "incompatible".

leaf_version_json_string() {
    _rop_file=$1
    _rop_key=$2
    [ -f "$_rop_file" ] || return 1
    _rop_count=$(awk -v needle="\"$_rop_key\"" '
        {
            line = $0
            while ((position = index(line, needle)) != 0) {
                count++
                line = substr(line, position + length(needle))
            }
        }
        END { print count + 0 }
    ' "$_rop_file") || return 2
    [ "$_rop_count" -eq 1 ] || {
        [ "$_rop_count" -eq 0 ] && return 1
        return 2
    }
    _rop_values=$(sed -n \
        "s/^.*\"$_rop_key\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" \
        "$_rop_file") || return 2
    [ -n "$_rop_values" ] || return 2
    printf '%s\n' "$_rop_values"
}

leaf_version_strip_zeros() {
    _rop_number=$1
    while [ "${#_rop_number}" -gt 1 ] && [ "${_rop_number#0}" != "$_rop_number" ]; do
        _rop_number=${_rop_number#0}
    done
    printf '%s\n' "$_rop_number"
}

leaf_version_components() {
    _rop_value=$1
    _rop_allow_release=$2
    if [ "$_rop_allow_release" -eq 1 ]; then
        case "$_rop_value" in
            v*|V*) _rop_value=${_rop_value#?} ;;
        esac
    fi
    _rop_major=${_rop_value%%.*}
    _rop_rest=${_rop_value#*.}
    [ "$_rop_rest" != "$_rop_value" ] || return 1
    _rop_minor=${_rop_rest%%.*}
    _rop_patch_and_suffix=${_rop_rest#*.}
    [ "$_rop_patch_and_suffix" != "$_rop_rest" ] || return 1
    _rop_patch=${_rop_patch_and_suffix%%[!0-9]*}
    _rop_suffix=${_rop_patch_and_suffix#"$_rop_patch"}
    case "$_rop_major:$_rop_minor:$_rop_patch" in
        *[!0-9:]*|::*|*::|:*) return 1 ;;
    esac
    if [ "$_rop_allow_release" -eq 0 ] && [ -n "$_rop_suffix" ]; then
        return 1
    fi
    case "$_rop_suffix" in
        .*) return 1 ;;
    esac
    _rop_major=$(leaf_version_strip_zeros "$_rop_major")
    _rop_minor=$(leaf_version_strip_zeros "$_rop_minor")
    _rop_patch=$(leaf_version_strip_zeros "$_rop_patch")
    [ "$_rop_major" -le 9999 ] && [ "$_rop_minor" -le 9999 ] &&
        [ "$_rop_patch" -le 9999 ] || return 1
    printf '%s %s %s\n' "$_rop_major" "$_rop_minor" "$_rop_patch"
}

leaf_version_at_least() {
    _rop_installed=$(leaf_version_components "$1" 1) || return 2
    _rop_required=$(leaf_version_components "$2" 0) || return 3
    # shellcheck disable=SC2086
    set -- $_rop_installed
    _rop_imajor=$1; _rop_iminor=$2; _rop_ipatch=$3
    # shellcheck disable=SC2086
    set -- $_rop_required
    _rop_rmajor=$1; _rop_rminor=$2; _rop_rpatch=$3
    [ "$_rop_imajor" -gt "$_rop_rmajor" ] && return 0
    [ "$_rop_imajor" -lt "$_rop_rmajor" ] && return 1
    [ "$_rop_iminor" -gt "$_rop_rminor" ] && return 0
    [ "$_rop_iminor" -lt "$_rop_rminor" ] && return 1
    [ "$_rop_ipatch" -ge "$_rop_rpatch" ]
}

leaf_version_gate_manifest() {
    _rop_manifest=$1
    _rop_release_json=$2
    LEAF_REQUIRED_VERSION=
    LEAF_INSTALLED_VERSION=
    JAWAKA_REQUIRED_VERSION=
    JAWAKA_INSTALLED_VERSION=
    LEAF_VERSION_GATE_REASON=

    LEAF_REQUIRED_VERSION=$(leaf_version_json_string "$_rop_manifest" min_leaf_version)
    _rop_status=$?
    [ "$_rop_status" -eq 0 ] || {
        LEAF_VERSION_GATE_REASON=no-leaf-minimum
        return 65
    }
    JAWAKA_REQUIRED_VERSION=$(leaf_version_json_string "$_rop_manifest" min_jawaka_version)
    _rop_status=$?
    [ "$_rop_status" -eq 0 ] || {
        LEAF_VERSION_GATE_REASON=no-jawaka-minimum
        return 65
    }
    leaf_version_components "$LEAF_REQUIRED_VERSION" 0 >/dev/null 2>&1 || {
        LEAF_VERSION_GATE_REASON=invalid-leaf-minimum
        return 65
    }
    leaf_version_components "$JAWAKA_REQUIRED_VERSION" 0 >/dev/null 2>&1 || {
        LEAF_VERSION_GATE_REASON=invalid-jawaka-minimum
        return 65
    }

    LEAF_INSTALLED_VERSION=$(leaf_version_json_string "$_rop_release_json" version)
    _rop_status=$?
    [ "$_rop_status" -eq 0 ] || {
        LEAF_INSTALLED_VERSION=Unknown
        LEAF_VERSION_GATE_REASON=unknown-installed-leaf-version
        return 66
    }
    leaf_version_components "$LEAF_INSTALLED_VERSION" 1 >/dev/null 2>&1 || {
        LEAF_VERSION_GATE_REASON=invalid-installed-leaf-version
        return 66
    }

    if ! leaf_version_at_least "$LEAF_INSTALLED_VERSION" "$LEAF_REQUIRED_VERSION"; then
        LEAF_VERSION_GATE_REASON=below-leaf-minimum
        return 67
    fi

    # Advisory only -- see the header. An absent or unparseable value leaves
    # JAWAKA_INSTALLED_VERSION=Unknown and does not block startup.
    JAWAKA_INSTALLED_VERSION=${UMRK_JAWAKA_VERSION:-${JAWAKA_VERSION:-}}
    if [ -z "$JAWAKA_INSTALLED_VERSION" ]; then
        JAWAKA_INSTALLED_VERSION=$(leaf_version_json_string "$_rop_release_json" jawaka_version 2>/dev/null || true)
    fi
    if [ -z "$JAWAKA_INSTALLED_VERSION" ]; then
        JAWAKA_INSTALLED_VERSION=Unknown
    elif ! leaf_version_components "$JAWAKA_INSTALLED_VERSION" 1 >/dev/null 2>&1; then
        JAWAKA_INSTALLED_VERSION=Unknown
    elif ! leaf_version_at_least "$JAWAKA_INSTALLED_VERSION" "$JAWAKA_REQUIRED_VERSION"; then
        LEAF_VERSION_GATE_REASON=below-jawaka-minimum
        return 67
    fi

    LEAF_VERSION_GATE_REASON=compatible
    return 0
}
