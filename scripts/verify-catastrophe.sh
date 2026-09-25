#!/usr/bin/env bash
# Prove a Catastrophe sibling is the release-lock input, in a git checkout or not.
#
# A checkout must be AT the locked commit. A plain directory -- the Catastrophe
# tree inside a dist-source archive -- must hash to the locked commit's tree id,
# computed with git's own object hashing in a throwaway repository, so the
# corresponding-source rebuild is held to the same input as a checkout.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
DIR="${1:?usage: verify-catastrophe.sh CATASTROPHE_DIR}"
PYTHON="${PYTHON:-python3}"

lock_value() {
    "$PYTHON" -c 'import json, sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' \
        "$ROOT/release-lock.json" "$1"
}
expected_commit="$(lock_value catastrophe_commit)"
expected_tree="$(lock_value catastrophe_tree)"

if [ ! -d "$DIR" ]; then
    echo "Catastrophe sibling missing: $DIR" >&2
    exit 1
fi

if [ "$(git -C "$DIR" rev-parse --show-toplevel 2>/dev/null || true)" = "$(CDPATH= cd -- "$DIR" && pwd -P)" ]; then
    actual="$(git -C "$DIR" rev-parse HEAD)"
    if [ "$actual" != "$expected_commit" ]; then
        echo "Catastrophe sibling must be at the release-lock commit $expected_commit, got $actual." >&2
        echo "An arbitrary sibling HEAD is not a reproducible input. Point CATASTROPHE_DIR" >&2
        echo "at a checkout of the pinned commit (e.g. a git worktree) and retry." >&2
        exit 1
    fi
    if [ -n "$(git -C "$DIR" status --porcelain --untracked-files=no)" ]; then
        echo "Catastrophe sibling at $expected_commit has local modifications." >&2
        exit 1
    fi
    echo "Catastrophe checkout at $expected_commit"
    exit 0
fi

scratch="$(mktemp -d "${TMPDIR:-/tmp}/raop-catastrophe.XXXXXX")"
trap 'rm -rf "$scratch"' EXIT
git init -q "$scratch/repo"
actual_tree="$(
    GIT_DIR="$scratch/repo/.git" GIT_WORK_TREE="$DIR" GIT_INDEX_FILE="$scratch/index" \
        git -c core.autocrlf=false -c core.fileMode=true add -A . >/dev/null &&
    GIT_DIR="$scratch/repo/.git" GIT_INDEX_FILE="$scratch/index" git write-tree
)"
if [ "$actual_tree" != "$expected_tree" ]; then
    echo "Catastrophe directory $DIR is tree $actual_tree, not the locked" >&2
    echo "$expected_commit (tree $expected_tree)." >&2
    exit 1
fi
echo "Catastrophe tree $expected_tree (commit $expected_commit)"
