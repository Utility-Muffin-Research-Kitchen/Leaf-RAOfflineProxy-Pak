# R4 — Pak Rat compatibility and release handoff

**Date:** 2026-08-14
**Device:** Miniloong Pocket 1, Leaf `v0.10.0-beta.3`, aarch64 Buildroot 5.10.209
**Plan:** `umrk-workspace/plans/RAOfflineProxy/README.md`

Nothing here is a release. No tag, no catalog entry, no publication. The repo is
private and the production storefront does not carry this app id; the catalog
fixture refuses to run if it ever does.

## Locked pins

| Input | Version | SHA-256 (first 16) | License |
| --- | --- | --- | --- |
| RAOfflineProxy (upstream) | `v1.11.1-alpha1` @ `be6898e6dc26` | `104732eda35feb04…` | GPL-3.0-only |
| CPython | 3.13.15 | `1e66a7945a48390e…` | PSF-2.0 |
| XZ Utils / liblzma | 5.8.2 | `890966ec3f5d5cc1…` | liblzma 0BSD |
| Mozilla CA store (via curl) | 2026-08-13 | `f66dff1bdf8f9606…` | MPL-2.0 |

Everything builds inside the pinned `mlp1-toolchain` container from
lock-verified archives. System ABI boundary (from the stock MLP1 rootfs,
re-verified 2026-08-14): OpenSSL, SQLite, libffi, expat, zlib, glibc 2.38.
liblzma is bundled because stock MLP1 has none. No OpenSSL rpath and no
PortMaster prefix or artifacts.

## Artifacts

| Package | ZIP bytes | ZIP SHA-256 | Installed |
| --- | ---: | --- | ---: |
| Real `0.1.0` | 9,062,475 | `4d8e7dd7af89818d5a862645e7b7e8bdf0f6ed86b188ae061a2885dc115f94bc` | 28,680 KB |
| Floor `0.0.1` | 108,782 | `ceadef5db83dd57cd146ea3d4dba8bbe5893fc6fb586cd93ecbbe9e23a1f74a2` | 172 KB |

The floor carries no Python runtime, no `bin/service-run`, no `app/` tree and no
`service` object — only its Catastrophe notice binary, launcher, the shared
version gate, its manifest and the icon. Both share the stable install name
`RAOfflineProxy.pak`, so a single audit entry covers both.

## Minimum Leaf version

`min_leaf_version: 0.10.0`, and that is the shippable value, not a lowering for
convenience. 0.10.0 is still in beta and the transient launch bridge
(Jawaka#64) lands before it ships, so 0.10.0 names the first release that can
actually route to this pak.

Residual gap, accepted: the gate treats a pre-release as satisfying its own
release floor (the same rule Syncthing uses), so a `0.10.0-beta.N` predating the
bridge also clears it. Acceptable because betas are dev builds and no catalog
entry exists before 0.10.0 ships.

`min_jawaka_version` is declared metadata that Jawaka's discovery records. It is
deliberately **not** a runtime gate: nothing on the device publishes an
installed Jawaka version, and Jawaka ships inside the Leaf payload, so the Leaf
version already pins it.

## Catalog qualification (steps 1–2)

`pakrat.json.in` renders one stable app id twice — ungated for the floor, gated
for the real build — merged by Leaf's own generator into a storefront whose
legacy fields point at the floor and whose descending `versions[]` is
`[real, floor]`.

`make catalog-selection-smoke` drives **Jawaka's real Pak Rat client**, not a
reimplementation of the selection rule:

| Client | Selects |
| --- | --- |
| Pre-gating (legacy fields only) | floor, by construction — the fixture asserts the legacy artifact's digest equals the floor's and differs from the real one |
| Leaf 0.9.0 (below minimum) | floor `0.0.1`; real reported gated with its requirement; explicit install of the real version refused by the install-time recheck |
| Leaf exactly the minimum | real |
| Leaf above the minimum | real |

The runtime gate refuses independently with exit 64 and reason
`below-leaf-minimum`, which is the second, separate refusal protecting a pak
already on the card when Leaf is rolled back.

## Device lifecycle (step 3)

Hand-staged copies were removed first so Pak Rat genuinely owned the install.

| Stage | Result |
| --- | --- |
| Catalog selection on device | Leaf 0.10.0-beta.3 → real `0.1.0`, nothing gated |
| First install | 6.2 s; service **disabled**; no userdata until first run; port 8080 idle |
| Update a **running** service | pid 29560 → 30230; exactly one process; healthy again |
| TXN-1 | `package mutation: begin` / `end` bracketing each mutation in the daemon log |
| State across update | 11 cache rows + award-signing key intact; `desired_enabled` preserved |
| Uninstall while running and enabled | `uninstalled (userdata preserved)`; process stopped; port released |
| Next launch needs no config repair | shared config still `cheevos_custom_host = ""`; zero occurrences of the proxy host |
| Reinstall | returns **disabled**; 11 rows, award key and `integrity_check ok` all retained |

Retention was proven against real state — the cache and award-signing key from
the session where achievement `3159` was earned offline and flushed on
reconnect — rather than synthetic rows.

## Low-headroom update (step 4)

| | Constrained | After removing the constraint |
| --- | --- | --- |
| Outcome | `artifact extraction/validation failed` | `installed 0.1.2` |
| Installed pak | `0.1.1` intact, full tree, 49,600 KB | `0.1.2` |
| Partial / move-aside dirs | none | — |
| Service | **still running, pid 628 unchanged**, healthy | restarted, healthy |
| Install record | `installed=0.1.1`, `update_available 0.1.2` | `installed=0.1.2` |
| Userdata | untouched | retained |

Pak Rat failed at extraction, *before* opening the mutation window, so the
running proxy never noticed and the install record continued to advertise the
update. Retrying after removing the constraint succeeded.

**How the constraint was applied, and what it does not cover.** Neither obvious
route was safe. The card writes at a measured 7.3 MB/s, so filling 57 GB of
free space would take about 2.2 hours and wear it heavily; filling a tmpfs
would consume most of the 486 MB free RAM and risk OOM-killing the launcher,
whose runtime lives in `/tmp`. A host test against a real FAT32 image was tried
and abandoned: installing a *service* pak requires a live supervisor, which a
mock SD root has none of.

What was done instead: a 12 MB tmpfs mounted over the staging directory only —
real device, real supervisor, real `ENOSPC`, no card writes, no memory
pressure — then unmounted.

This constrains the staging area rather than overall free space. It genuinely
exercises ENOSPC during extraction and proves the old package and its running
service survive, but it does **not** exercise a failure landing later, inside
the move-aside/promote window. That deeper case belongs to the fault-injection
points (`before-promote`, `after-promote`), which simulate power loss rather
than disk exhaustion, and remains unqualified.

## Release-policy audit (step 5)

`app-package-policy-smoke.sh` passes, and `RAOfflineProxy.pak` is in
`leaf_pakrat_owned_package_names()` — the single list that feeds both
`make-sd-release-zip.sh`'s audit call and the smoke, so the negative ownership
audit needed no separate registration.

The guard was verified to **reject**, not merely to pass on a clean tree:

| Leak vector | Audit result |
| --- | --- |
| `managed-apps.txt` | `error: managed-apps.txt claims Pak Rat-owned RAOfflineProxy.pak` |
| Platform manifest `managed_apps` | `error: platform manifest claims Pak Rat-owned RAOfflineProxy.pak` |
| Staged under `Apps/mlp1/` | `error: release stages Pak Rat-owned RAOfflineProxy.pak` |

Against the current tree, not a fixture: zero hits in `stage/common.mk` repo
lists and zero in `STAGE_APPS` definitions. `make leaf-release-policy-test`
passes (10 tests).

## GPL source offer

The pak distributes upstream's GPL-3.0-only work together with the Leaf patch
series in `patches/` and the modules in `src/`, which modify it. The combined
work is therefore GPL-3.0-only, unlike the MIT UMRK paks. `LICENSE` carries the
verbatim GPL text and `NOTICE` the copyright and third-party inventory; both
ship inside the pak under `licenses/`.

**Cleared 2026-08-15:** the repository is public, so the GPL-3.0 obligation to
convey source to anyone receiving the binary is satisfied by the URL `pak.json`
already advertises.

## The alpha decision (2026-08-15)

Shipping on `v1.11.1-alpha1`, deliberately, with the reasoning recorded because
"it was alpha" is the obvious later objection.

The framing that made this decidable: **there is no stable upstream to wait
for, and there never has been.** Of 30 upstream releases, 29 carry an `-alpha`
tag, and upstream's own Linux documentation states the target "is currently in
alpha" for every distro it supports. Releases land every 3-10 days, so any pin
goes stale quickly regardless. "Wait for stable" is not a deferral; it is a
decision never to ship.

What makes that acceptable here rather than reckless:

- The pak is qualified end to end on hardware, through the real Pak Rat install
  path, including offline play and flush-on-reconnect.
- **Hardcore never routes through the proxy.** A durable Hardcore setting
  launches direct, and the gate fails closed when the shared config cannot be
  read, so the worst case for a hardcore player is an unproxied launch.
- The pak describes itself as experimental and casual-only in its own UI, and
  the catalog description says so before install.
- The award queue is signed and hash-chained, and flush is exactly-once
  against a real account.

What a user is actually accepting: a proxy whose upstream may change behaviour
between releases, on a feature that only affects *casual* achievement
unlocking. The bounded blast radius is what makes an alpha dependency
tolerable, not the version number.

Re-qualification on each upstream bump remains mandatory; the assembly
qualifier enforces the mechanical half (no pruned module, forbidden import or
banned symbol), and the device checks the rest.

## Upstream alpha caveats

Upstream's Linux target is explicitly alpha and is moving quickly; the pin is
evidence of what was current, not a judgement that it is stable. Re-pin
deliberately before release rather than inheriting the current pin. The Leaf
patch series removes upstream's daemon launcher, PID files, autostart,
self-update, self-uninstall, log upload and RetroArch config access, and the
assembly qualifier fails the build if any pruned module, forbidden import or
banned symbol reappears — so an upstream bump is a re-qualification, not a
version-string change.

### v1.11.0-alpha1 → v1.11.1-alpha1 (2026-08-15)

Eight upstream commits. **The only functional change reaching shipped code is
`APP_VERSION`**, which is the `RAOfflineProxy/Linux/<version>` User-Agent tag.
Every other change lands in code the pak prunes or replaces:

| Upstream change | Why it does not reach us |
| --- | --- |
| Offline play breaking after the first queued award | Android only (`ProxyServer.kt`): an accept loop occupying a worker from a fixed pool, plus 3-second DB latch waits that fall through as cache misses. The Python service is thread-per-request over one sqlite connection guarded by an `RLock`, which blocks rather than timing out, and `flush_lock` is never taken by a lookup — so there is no path that turns a slow DB read into a false "no cached response". |
| Boot prebind of the proxy port (`boot.py`) | Guards a boot-to-game race Leaf does not have: `jawakad` launches only a title the user picked, and the bridge proxies only when the service is already healthy. `boot.py` is pruned; the socket-adoption branch is patched out of `ProxyRuntimeServer.__init__`. |
| OnionOS version block | Distro integration, pruned. |
| `launch.log` collection | `log_uploader.py`, pruned. |
| Serve-before-migrate reorder in `run_proxy_service` | Not adopted. Leaf's order is deliberate: storage migration completes before the listener opens, so the port answering implies migrated storage — the invariant the launch bridge's health gate depends on. |

**Patch series:** two of thirteen hunks in `proxy_service.py.patch` rejected,
both in regions the patch rewrites wholesale (the import block and the
`ConnectivityMonitor`/`run_proxy_service` tail). Resolved against the new base
and the patch regenerated; the reviewed tail was carried over verbatim rather
than retyped.

**Device evidence (MLP1, 2026-08-15).** Installed over the R4 install with a
move-aside/promote, then restarted through CTL-1:

| | |
|---|---|
| Version actually loaded | `1.11.1-alpha1`, UA tag `RAOfflineProxy/Linux/1.11.1-alpha1` |
| Import graph | `proxy_service` imports clean — the service starting at all is proof the `.boot` import is gone, since it would raise `ImportError` |
| `boot.py` in the pak | absent |
| Restart | clean stop (`status: 0`), new pgid, `restart_count: 1`, **14 ms** to `ready` |
| Health | `{"ready":true}` on loopback |
| User data across the update | `integrity_check ok`, 18 cached rows and `award_secret.key` unchanged — the key offline signing depends on |
| Offline after a queued award | 10/10 assertions (see below) |
| `libraproxy_rchash.so` | md5-identical to the installed build, so the hash lane needs no re-verification |

`managed_apps` on the device excludes RAOfflineProxy, which is the Leaf#42
bootstrap/ownership invariant holding on real hardware rather than in a fixture.

**Launch bridge re-exercised on the new build** (Balloon Kid, gambatte, game
2184):

| | |
|---|---|
| Routing | `service healthy; proxied launch` → `transient cheevos host + forced casual override` |
| Per-launch config | `cheevos_custom_host = "127.0.0.1:8080"` — **1 occurrence**; hardcore `"false"` — 1 occurrence; `cheevos_token` absent |
| First-wins in the wild | the shared config still carries `cheevos_custom_host = ""` at line 3324, so an appended override would have lost to that empty string silently — the case the strip-and-write-once fix exists for |
| Traffic | `login2`, `achievementsets`, `startsession`, image, and a real `awardachievement` forwarded upstream; `h=0` throughout |
| Award routing | went upstream rather than queueing — `pending_awards: 0`, cache grew 18 → 22 |
| Exit restore | per-launch config unlinked; shared config unchanged at 3,325 lines with both keys at their original values; **0** occurrences of the proxy address |
| Service across the session | same pgid, `restart_count` unchanged — no crash or restart |

One caveat stated rather than implied: the install was a raw promote, not a
TXN-1 transaction, so jawakad's `installed_package` still reports the R4
catalog fixture's `0.1.2`. The install path itself was qualified at R4 and is
unchanged by this bump.

**Qualifier gap this found.** `proxy_service.py` began importing a module
upstream invented between the two tags (`.boot`). It was neither allow-listed
nor pruned, so it was silently dropped from the package, and because
byte-compilation does not resolve imports the pak assembled clean — it would
have died with `ImportError` on first start. The qualifier now requires every
relative import to name a shipped module. Verified by mutation in both import
forms; pruned modules still report as forbidden rather than merely unshipped.

## R5 device qualification (2026-08-15)

Pre-caching and the pak UI were qualified on hardware through the real Pak Rat
install path, not a hand-promoted tree.

| | |
|---|---|
| Install | Pak Rat from a local catalog fixture; `.pakrat-commit` marker and `pakrat_installs` record match on version, artifact sha256 and commit token |
| Pre-cache | `ActRaiser 2` (never launched) cached to RA game 3408, then served offline against an unreachable upstream |
| Console names | all 19 systems resolve through Jawaka's own three tiers |
| Service controls | Run / Stop and Start with Leaf driven from the pak over CTL-1 |
| Launch bridge | routing, exactly-once injection, forwarded traffic and exit restore |

**Do not hand-promote over a Pak Rat-managed install.** Replacing the tree with
`mv` leaves no `.pakrat-commit`, and jawakad's startup recovery then reports
`inconsistent committed tree reason=commit-marker-unreadable` every 500 ms
forever. It cost a debugging session here. Rebuild the artifact, regenerate the
catalog fixture, and use **Reinstall** from the store; that is what the fixture
is for, and it exercises the path users will take.

Three UI defects came only from someone holding the device, and none would have
surfaced in a headless check:

1. The progress screen never refreshed itself — `cat_options_list`'s timed
   refresh slept to the next wall-clock minute on exit (fixed upstream in
   Catastrophe#9; this pak hand-rolls the screen regardless).
2. A flat 1,968-game picker is not navigable with a d-pad.
3. Starting the service left the menu reading "unavailable": CTL-1 `run`
   returns at 24 ms while the control endpoints answer at ~1.8 s, and `cat_list`
   blocks until input, so the stale value survived until the next keypress.

## Not qualified

- Move-aside/promote-window failure under disk exhaustion (see step 4).
- Suspend/resume and crash-restart cycles for the service.

The pak UI and R5 offline pre-caching were listed here until 2026-08-15; both
are now qualified on hardware. See the R5 section above.

## Still required before a release can be cut

Ordered by dependency, because each step needs the one before it.

1. **The repo has no `.github/workflows/`.** There is no CI gate and, more to
   the point, no release workflow, so pushing a tag today publishes nothing.
   Every comparable pak has `ci.yml` and `release.yml`; `VideoFromHell` is the
   closest model, since it also builds inside the pinned `mlp1-toolchain`
   image read from `release-lock.json` rather than on the runner.
2. ~~Re-pin upstream deliberately.~~ **Decided 2026-08-15: ship on
   `v1.11.1-alpha1`.** See "The alpha decision" below.
3. **Cut the release**: tag both the real version and the floor, and let the
   workflow attach `RAOfflineProxy.mlp1.pak.zip` for each.
4. **Publish the catalog entry** in `leaf-docs/public/pakrat/v1/storefront.json`
   with the real GitHub release URLs, sizes and SHA-256s, then run
   `scripts/validate-pakrat-catalog.mjs --remote`. Pages deploys on push, which
   is what serves `https://leaf.game/pakrat/v1/`.
5. **Write the user-facing page** at
   `leaf-docs/src/content/docs/app-store/raofflineproxy.md`, alongside the
   seven existing app-store pages.

The two-version shape (gated real build over an ungated inert floor) is already
qualified against Jawaka's own client; step 4 is transcribing a proven shape to
production URLs, not designing one.
