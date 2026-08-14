# R2 package evidence — 2026-08-14

## Build

- `make package-mlp1` completed with the pinned MLP1 toolchain image.
- Real package: `build/mlp1/package/RAOfflineProxy.pak`, 28,025,905
  installed bytes before on-card cluster allocation; deterministic ZIP at
  `build/mlp1/RAOfflineProxy.mlp1.pak.zip`.
- Floor package: `build/mlp1/floor/package/RAOfflineProxy.pak`, 87,760
  installed bytes; stable package id is identical to the real package and the
  floor has no service manifest, runtime, app code, or network entry point.
- `scripts/package_check.py` passed: regular files only, no symlinks, no
  case-folding collisions, no host paths, required manifest/layout fields,
  excluded upstream modules absent, and ZIP entries FAT32-safe.
- The package inventory includes the upstream GPL license, CPython PSF
  license, XZ 0BSD license, CA bundle, and all three lock records.

## Gate

- `make test-version-gate` passed compatible, below-Leaf, below-Jawaka, and
  unknown-Jawaka fixtures.
- The real service wrapper was copied to the SSH test device scratch root
  (not USB adb). With a Leaf `0.10.0-beta.3` release marker it refused before
  Python startup with status 64 and `below-leaf-minimum`.
- With a scratch release marker containing Leaf `0.11.0` and Jawaka `0.11.0`,
  plus a valid descriptor 3, the service reached `Proxy ready on
  127.0.0.1:8080`; the bundled relocated Python client received HTTP 200 with
  the fixed `/leaf/health` JSON, and the process stopped cleanly on SIGTERM.
- The actual device remains on the below-minimum Leaf/Jawaka pair, so this is
  not evidence of a compatible production supervised install.

## Leaf isolation

- `Leaf/scripts/app-package-policy.sh` contains one explicit
  `Leaf-RAOfflineProxy-Pak` policy case and one `RAOfflineProxy.pak`
  Pak-Rat-owned name.
- `app-package-policy-smoke.sh` passes with RAOfflineProxy included in the
  ownership audit and absent from default `STAGE_APPS` patterns.
- No `Leaf/stage/common.mk` required/bootstrap repo list was changed.
- The package was briefly copied to `/mnt/sdcard/Apps/mlp1/RAOfflineProxy.pak`
  over SSH for discovery-path inspection and removed again. No USB adb staging
  or testing was used.

## Not yet claimed

- `make stage-app APP=Leaf-RAOfflineProxy-Pak DEVICE=mlp1` was intentionally
  not run because the requested test policy excludes USB adb.
- Pak Rat install/update/uninstall, compatible-version Services control,
  suspend/resume, secondary-card removal, and UI rendering remain R2 device
  qualification work once a compatible Leaf/Jawaka test payload is available.
