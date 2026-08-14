# Leaf-RAOfflineProxy-Pak

Leaf managed-service pak packaging [misantronic/RAOfflineProxy](https://github.com/misantronic/RAOfflineProxy)
for the Miniloong Pocket 1 (MLP1), per
`umrk-workspace/plans/RAOfflineProxy/README.md`.

**Status:** R2 package assembly complete; package/service gate validated over
SSH on the MLP1 test device. The real service remains below the declared
`0.11.0` Leaf/Jawaka floor on that device, so no compatible supervised install
has been claimed. This checkout is local-only; the remote repo, catalog entry,
tags, and releases are not authorized by the plan.

## Layout

- `release-lock.json` — pak-level pin: versions, minimum Leaf/Jawaka floor
  (provisional `0.11.0`, confirmed at R4), toolchain image digest.
- `locks/upstream.lock.json` — upstream tag/commit/archive hash plus the
  production import-graph exclusions.
- `locks/runtime.lock.json` — CPython / liblzma / CA bundle pins and the
  system-library ABI boundary.
- `patches/app/*.patch` — the focused Leaf patch over the upstream tag
  (`-p1` at the extracted tarball root):
  - `storage.py.patch` — SQLite `DELETE`/`FULL` enforced by read-back,
    startup `integrity_check`, quarantine-and-refuse on corruption, JSON
    fallback disabled.
  - `network.py.patch` — 500 ms reachability probe bound; failed probes are
    cached, not repeated before every request.
  - `flusher.py.patch` — flushes use only tokens learned from proxied
    traffic (no RetroArch cfg reads); single-account guard.
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

## Commands

```sh
scripts/fetch-sources.sh                    # verify/download all pinned sources
scripts/build-runtime-cpython.sh            # build runtime -> build/mlp1/runtime
scripts/assemble-app.sh                     # patched app -> build/mlp1/app
make package-platform PLATFORM=mlp1         # real pak assembly
make package-floor-mlp1                      # inert floor assembly
make test-package test-version-gate          # structural and gate checks
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

GPL-3.0-only. See [LICENSE](LICENSE).

This repository is owned by Utility Muffin Research Kitchen. The patch series
in `patches/` and the Leaf-owned modules in `src/` modify misantronic's
GPL-3.0-only RAOfflineProxy, so the combined work is GPL-3.0-only rather than
MIT like the other UMRK paks. `Catastrophe-LICENSE.txt` is a verbatim copy of
Catastrophe's own MIT license and keeps that project's copyright line
unchanged — it is a third-party license reproduction, not this repo's
attribution.
