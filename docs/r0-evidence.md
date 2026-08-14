# R0 device evidence — 2026-08-14

Device: MLP1 (RK3566, aarch64 Buildroot / LoongOS), glibc 2.38, kernel
5.10.209, reached over SSH (not adb). Primary card `/mnt/sdcard`
(FAT32-family mount, 58G), Leaf `0.10.0-beta.3` on card
(`.umrk/mlp1/release.json`).

## Locked pins

- Upstream `misantronic/RAOfflineProxy` `v1.11.0-alpha1`
  (`d2c22a7b86b1569efb283d1493224e368ba102be`), tag tarball SHA-256
  `a0a36a2e…eabc3` (see `locks/upstream.lock.json`).
- CPython 3.13.15 (bugfix series), source tarball verified.
- XZ/liblzma 5.8.2 (same archive as the proven PortMaster ui-runtime lock).
- curl/Mozilla CA bundle dated 2026-08-13.
- Toolchain image `mlp1-toolchain@sha256:66aac16fb8b0…`

## R0.2 — runtime build

- `scripts/build-runtime-cpython.sh` parameterizes the Python version from
  `locks/runtime.lock.json` (the PortMaster recipe hardcodes `3.10` in ~20
  places) and builds under the RAOfflineProxy prefix `/raofflineproxy` with
  **no** OpenSSL rpath.
- Container relocation proof passed: `3.13.15 (GCC 12.3.0)`,
  `OpenSSL 3.0.13`, `sqlite 3.45.1`, `installed-imports-ok`.
- Artifact: 8.8 MB zip; installed tree 29 MB local (ext4-style).

## R0.3 — on-device validation

- DT_NEEDED across the interpreter and every lib-dynload extension resolves
  to: `ld-linux-aarch64.so.1`, `libc.so.6`, `libm.so.6` (device glibc
  2.38); `libssl.so.3`, `libcrypto.so.3`; `libsqlite3.so.0`; `libffi.so.8`;
  `libexpat.so.1`; `libz.so.1` (all present in stock `/usr/lib`, verified);
  `liblzma.so.5` (bundled — stock MLP1 has none); own `libpython3.13.so.1.0`.
  No symbol beyond glibc 2.38 required.
- On device with relocated `PYTHONHOME`: imports pass; SSL binds the device
  `libssl.so.3` (`OpenSSL 3.0.8`); `HEAD https://retroachievements.org` →
  200 in 0.74 s with the bundled CA file.
- SQLite on the SD card: `journal_mode=DELETE` + `synchronous=FULL`
  read-back OK, commit/reopen OK.
- **Key finding:** `PRAGMA journal_mode=WAL` *succeeds* on the card, so
  upstream's WAL-try/DELETE-fallback would silently choose WAL. The Leaf
  patch forces DELETE explicitly and refuses otherwise. The pak's on-FAT
  install footprint is ~61 MB (cluster-size inflation of many small files).

## R0.4 — production-shaped service on device

Patched tree built by `scripts/assemble-app.sh` (byte-compile + import-graph
qualification: no excluded modules/imports/symbols survive).

- `leaf_service` **refuses to start without a valid
  `UMRK_SERVICE_LEASE_FD`** (rc 2, explanatory stderr) — proven on device.
- Cold start: `/leaf/health` answers the fixed
  `{"service":…,"protocol":"leaf-health-1","ready":true}` ~0.75–3 s after
  spawn (first-spawn interpreter load dominates); ready is set only after
  storage init + bind + serve thread.
- Listener loopback-only on 127.0.0.1:8080 (health + request flow proven;
  health never touches SQLite).
- Local readiness precedes any network probing (ordering in
  `run_proxy_service`); health is immediate even with the offline
  simulation below active.
- Clean SIGTERM stop on device with no leftover process, including after
  the offline queue test.

## R0.5 — SQLite durability

- `journal_mode=DELETE`, `synchronous=FULL` read-back enforced; refusal on
  mismatch. Verified on FAT card.
- Commit/reopen verified; pending award row (with signature chain)
  survives SIGTERM + restart.
- Corruption: mid-file overwrite → next start raises refusal rc 1, the
  database is quarantined (`proxy.sqlite3.corrupt-<millis>`), an incident
  is recorded (`storage_corruption.json`), and no fresh DB replaces it.
  Verified on device. Pending awards are never silently reset.
- Not yet done: bounded kill-9 mid-write interruption test (deferred to R2
  service qualification; DELETE+FULL atomicity is the mechanism the patch
  enforces).

## R0.6 — offline behavior (upstream simulated as unroutable
`https://192.0.2.1`)

- First offline request: ~795 ms total (probe bound 500 ms + request
  handling); second offline request: **56 ms** — the failed reachability
  result is reused instead of re-probing.
- Offline `gameid` miss returns upstream's explicit "Game not cached…"
  response.
- Offline `awardachievement` queues: `queued_offline` JSON + pending row
  (`signature` present, `prevHash=genesis`).
- Cross-account: a second user's award while one is pending →
  `409 account_mismatch` naming the pending owner; the first user's second
  award queues normally.
- Real captive/dead-Wi-Fi test not run — associating Wi-Fi changes would
  sever the SSH-only management path. The 192.0.2.1 simulation covers the
  same timeout path (connect stall at the 500 ms bound).

## R0.7 — measurements

- Runtime zip 8.8 MB; installed 29 MB (ext4) / ~61 MB apparent on the FAT
  card; upstream app tree ~0.4 MB python sources. Pak Rat update headroom
  should budget ≥ 2× installed pak, i.e. ≥ ~125 MB on-card.
- Idle RSS at ready: **25.4 MB**; idle CPU not separately profiled (no
  background network activity when no awards are pending: connectivity
  sweeps are pending-award-gated).
- Cold readiness: see R0.4 (~0.75–3 s to ready).
- Representative automatic-cache bytes: requires live game traffic (R3);
  provisional working caps for R2: **128 games / 32 MiB** of `api_cache`
  rows, eviction excluding `login*` keys, `user_agent`, pending awards, and
  the signing key. Revisit with R3 traffic.

## R0 stop conditions — none hit

Runtime needs no newer glibc (2.38 OK), all required stdlib modules
present, headless service becomes locally ready without network, and
acknowledged pending-award state survives restart and refuses corruption
silently.
