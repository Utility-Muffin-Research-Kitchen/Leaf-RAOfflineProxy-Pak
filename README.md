# Leaf-RAOfflineProxy-Pak

Leaf managed-service pak packaging [misantronic/RAOfflineProxy](https://github.com/misantronic/RAOfflineProxy)
for the Miniloong Pocket 1 (MLP1), per
`umrk-workspace/plans/RAOfflineProxy/README.md`.

**Status:** 0.1.0 (real) and 0.0.1 (inert floor) are published in the Pak Rat
catalog, both immutable. The source now carries the 0.1.1 candidate: the
refreshed upstream pin plus the patch data bundled standalone Flycast loads
offline, gated to Leaf/Jawaka `0.12.0`. The published 0.1.0 row keeps its
`0.10.0` minimum, so older Leaf releases keep being offered 0.1.0. A version is
published only by a deliberate tag and catalog change, never from this README.

## Layout

- `release-lock.json` — pak-level pin: versions, minimum Leaf/Jawaka floor
  (`0.12.0`), toolchain image digest, sibling commits. `pak.json` and
  `floor/requirements/` carry the same pair; `make test-version-metadata`
  fails when any copy disagrees.
- `locks/upstream.lock.json` — upstream tag/commit/archive hash plus the
  production import-graph exclusions.
- `locks/runtime.lock.json` — CPython / liblzma / CA bundle pins and the
  system-library ABI boundary.
- `patches/app/*.patch` — the focused Leaf patch over the upstream tag
  (`-p1` at the extracted tarball root):
  - `storage.py.patch` — SQLite `DELETE`/`FULL` enforced by read-back,
    startup `integrity_check`, quarantine-and-refuse on corruption, JSON
    fallback disabled; learned logins looked up per user, or the most
    recent sign-in, never whichever row the table returns first.
  - `config.py.patch` — strips upstream's distro detection and its
    RetroArch/PPSSPP/Dolphin/batocera/ROCKNIX config readers; the state
    directory comes from `RAOFFLINEPROXY_CONFIG_DIR` and the port is fixed.
    `assemble-app.sh` fails if any shipped module names such a reader or
    path again.
  - `network.py.patch` — 500 ms reachability probe bound; failed probes are
    cached, not repeated before every request.
  - `flusher.py.patch` — flushes use only tokens learned from proxied
    traffic (no RetroArch cfg reads); single-account guard: pending awards
    flush with their owner's own token or wait.
  - `proxy_service.py.patch` — 4-worker bounded gate with non-daemon
    threads and `block_on_close`; fixed `/leaf/health` before normal
    dispatch; sparse pending-award connectivity sweeps (no idle polling);
    upstream PID/inode/self-update machinery removed; per-account queue
    guard; no image-download scheduling after pass-through.
- `src/raofflineproxy/leaf_service.py` — supervised foreground entry
  (`-m raofflineproxy.leaf_service`): lease-fd validation, required-env
  checks, fixed config, SIGTERM/SIGINT drain. Overlaid at assembly.
- `scripts/fetch-sources.sh` — lock-verified source downloads into
  `workdir/sources/` (fails on mismatch; never fetches unpinned refs).
- `scripts/build-runtime-cpython.sh` — private CPython runtime built in the
  pinned `mlp1-toolchain` container, pruned/flattened/stripped, with a
  PYTHONHOME relocation proof.
- `scripts/assemble-app.sh` — re-derives `build/mlp1/app/raofflineproxy`
  from the lock: extract, allow-list prune, patch, overlay, byte-compile,
  and import-graph qualification.
- `pak.json`, `launch.sh`, `src/service-run` — real service manifest, inert
  foreground UI launcher, supervised Python entry, and dual Leaf/Jawaka gate.
- `floor/`, `scripts/package_floor.py` — stable-id inert compatibility floor.
- `scripts/package_mlp1.py` — deterministic real pak assembly, license
  inventory, symlink/case-collision rejection, and ZIP output.
- `scripts/package_check.py` — manifest, layout, excluded-module, host-path,
  and ZIP regular-file checks.
- `ports/mlp1/Makefile`, `src/ui_main.c` — minimal Catastrophe information and
  floor screens; `res/icon.png` is the bundled Catastrophe icon with its MIT
  license recorded in the package.
- `scripts/device/r0-offline-spike.sh` — on-device R0 evidence script
  (offline simulation, queue/durability checks).
- `docs/r0-evidence.md` — device evidence and measurements for R0.

`workdir/` and `build/` are ignored build products; everything re-derives
from the locks.

## Locked sibling bootstrap

The build reads sibling checkouts next to this one, and each is pinned by
commit in `release-lock.json`. Nothing tracks a branch: a sibling at any other
commit is a different input, and the build refuses it.

| Sibling | Lock key | Used for |
| --- | --- | --- |
| `Catastrophe` | `catastrophe_commit` (tree `catastrophe_tree`) | the pak's UI and floor screens; a build input |
| `Jawaka` | `jawaka_commit` | the real Pak Rat client `make catalog-selection-smoke` drives; test only |
| `Leaf` | `leaf_commit` | the local-feed generator for the same smoke; test only |

A fresh bootstrap, from an empty directory:

```sh
git clone https://github.com/Utility-Muffin-Research-Kitchen/Leaf-RAOfflineProxy-Pak.git
lock() { python3 -c 'import json,sys; print(json.load(open("Leaf-RAOfflineProxy-Pak/release-lock.json"))[sys.argv[1]])' "$1"; }
for repo in Catastrophe Jawaka Leaf; do
  key="$(echo "$repo" | tr '[:upper:]' '[:lower:]')_commit"
  git clone "https://github.com/Utility-Muffin-Research-Kitchen/$repo.git"
  git -C "$repo" checkout --detach "$(lock "$key")"
done
cd Leaf-RAOfflineProxy-Pak
make test-package          # needs Docker and the pinned mlp1-toolchain image
```

`make ui-mlp1` (and so every package build) runs
`scripts/verify-catastrophe.sh`: a Catastrophe checkout must be at
`catastrophe_commit` with no local changes, and a plain directory must hash to
`catastrophe_tree`. Point `CATASTROPHE_DIR` at a worktree of the pinned commit
if your usual Catastrophe checkout is elsewhere. CI checks out the same three
commits.

## Corresponding source

`make dist-source` writes `build/dist/raofflineproxy-<version>-source.tar.gz`:
this repository at `HEAD`, every archive the locks name (upstream
RAOfflineProxy, CPython, xz, the CA bundle, rcheevos, libchdr) and the locked
Catastrophe tree, with a `corresponding-source.json` listing each input's hash.
It refuses a working tree with uncommitted changes. The package rebuilds from
the extracted archive alone:

```sh
tar -xzf raofflineproxy-<version>-source.tar.gz
cd raofflineproxy-<version>-source/Leaf-RAOfflineProxy-Pak
make package-mlp1 package-floor-mlp1
```

The toolchain is the `mlp1-toolchain` image named by digest in
`release-lock.json`. `make test-dist-source` extracts the archive with
downloads disabled and checks the inputs, the Catastrophe tree and the
assembled app against this checkout; with `DIST_SOURCE_REBUILD=1`, as CI runs
it, it rebuilds both packages from the archive and requires identical ZIPs.

## Commands

```sh
scripts/fetch-sources.sh                    # verify/download all pinned sources
scripts/build-runtime-cpython.sh            # build runtime -> build/mlp1/runtime
scripts/assemble-app.sh                     # patched app -> build/mlp1/app
make package-platform PLATFORM=mlp1         # real pak assembly
make package-floor-mlp1                     # inert floor assembly
make test-package test-version-gate         # structural and gate checks
make test-version-metadata                  # one version pair across the source
make test-network-fixtures test-account-guard test-precache-fixtures
make test-chd-reader                        # CHD track layout on synthetic discs
make test-state-compat                      # 0.1.0 state: upgrade and rollback
make catalog-selection-smoke                # real Pak Rat client, version ladder
make dist-source test-dist-source           # corresponding source and rebuild
```

## Runtime contract (implemented in the patch set)

- Listener `127.0.0.1:8080`, origin `https://retroachievements.org`, neither
  configurable.
- Durable state only under the dir named by `RAOFFLINEPROXY_CONFIG_DIR`
  (the pak maps it to `$USERDATA_PATH/RAOfflineProxy`): SQLite cache,
  pending awards, cached tokens, award-signing secret, logs.
- `/leaf/health` is answered before normal dispatch, never touches SQLite,
  and only after the listener serves and requests can complete.
- The service never reads or writes RetroArch configuration and never
  resolves credentials from config files.
- SQLite runs `journal_mode=DELETE`, `synchronous=FULL` (verified by
  read-back), startup `integrity_check`, quarantine-and-refuse on
  corruption — pending awards are never silently reset.
- Offline leaderboard submissions are refused explicitly, never queued: a
  leaderboard entry has no achievement id, so queueing one would insert an
  `achievementId=0` row and collide with that column's `UNIQUE` constraint.
- Only rows still awaiting a flush constrain the single-account rule, on both
  the queue side and the flush side.

## Version gating

`min_leaf_version` is the enforced gate, in the Pak Rat catalog and again at
runtime via `lib/leaf-version-gate.sh` (sourced by both `launch.sh` and
`bin/service-run`). It is enforced twice on purpose: the catalog gate stops a
fetch, but does nothing about a pak already on the card when Leaf is rolled
back or the card is moved to another device.

`min_jawaka_version` is declared in `pak.json` and recorded by Jawaka's
discovery, but is **not** a hard runtime gate. Nothing on the device publishes
an installed Jawaka version — `release.json` carries `schema`, `product`,
`platform`, `version`, `release_id`, `installed_at` and `source`, and no
component exports `UMRK_JAWAKA_VERSION` — so requiring one would fail closed on
every real install. Jawaka ships inside the Leaf release payload, so the Leaf
version already pins it. If a future Leaf publishes `jawaka_version`, the gate
picks it up and enforces it automatically.

## License

GPL-3.0-only, copyright Utility Muffin Research Kitchen. See [LICENSE](LICENSE)
for the license text and [NOTICE](NOTICE) for the third-party inventory.

The patch series in `patches/` and the Leaf-owned modules in `src/` modify
misantronic's GPL-3.0-only RAOfflineProxy, so the combined work is GPL-3.0-only
rather than MIT like the other UMRK paks.

`Catastrophe-LICENSE.txt` is a verbatim copy of Catastrophe's own MIT
license and keeps that project's copyright line unchanged — it is a
third-party license reproduction, not this repo's attribution.
