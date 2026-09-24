PYTHON ?= python3
PLATFORM ?= mlp1
WORKSPACE_ROOT ?= $(abspath ..)
CATASTROPHE_DIR ?= $(WORKSPACE_ROOT)/Catastrophe
MLP1_TOOLCHAIN_IMAGE ?= $(shell $(PYTHON) -c 'import json; print(json.load(open("release-lock.json"))["mlp1_toolchain_image"])')
MLP1_CONTAINER_REPO ?= /workspace/$(notdir $(CURDIR))
PAK_VERSION ?= $(shell $(PYTHON) -c 'import json; print(json.load(open("release-lock.json"))["pak_version"])')
FLOOR_PAK_VERSION ?= $(shell $(PYTHON) -c 'import json; print(json.load(open("release-lock.json"))["floor_version"])')
MIN_LEAF_VERSION ?= $(shell $(PYTHON) -c 'import json; print(json.load(open("release-lock.json"))["min_leaf_version"])')
MIN_JAWAKA_VERSION ?= $(shell $(PYTHON) -c 'import json; print(json.load(open("release-lock.json"))["min_jawaka_version"])')

.PHONY: fetch-sources runtime-mlp1 app-mlp1 ui-mlp1 package-platform package-mlp1 package-floor-mlp1 rchash-mlp1 catalog-fixture catalog-selection-smoke test-package test-version-gate test-version-metadata test-network-fixtures test-account-guard test-precache-fixtures clean

fetch-sources:
	./scripts/fetch-sources.sh

runtime-mlp1: fetch-sources
	./scripts/build-runtime-cpython.sh

app-mlp1: fetch-sources
	./scripts/assemble-app.sh

rchash-mlp1: fetch-sources
	./scripts/build-rchash.sh

ui-mlp1:
	@expected="$$( $(PYTHON) -c 'import json; print(json.load(open("release-lock.json"))["catastrophe_commit"])' )"; \
	actual="$$( git -C "$(CATASTROPHE_DIR)" rev-parse HEAD 2>/dev/null || echo missing )"; \
	if [ "$$actual" != "$$expected" ]; then \
		echo "Catastrophe sibling must be at the release-lock commit $$expected, got $$actual." >&2; \
		echo "An arbitrary sibling HEAD is not a reproducible input. Point CATASTROPHE_DIR" >&2; \
		echo "at a checkout of the pinned commit (e.g. a git worktree) and retry." >&2; \
		exit 1; \
	fi
	docker run --rm \
		--user "$$(id -u):$$(id -g)" \
		-e SOURCE_DATE_EPOCH="$$( $(PYTHON) -c 'import json; print(json.load(open("locks/runtime.lock.json"))["source_date_epoch"])' )" \
		-v "$(WORKSPACE_ROOT):/workspace" \
		-w "$(MLP1_CONTAINER_REPO)" \
		"$(MLP1_TOOLCHAIN_IMAGE)" \
		make -f ports/mlp1/Makefile BUILD_DIR=build/mlp1 CATASTROPHE_DIR=/workspace/$(notdir $(CATASTROPHE_DIR))

package-platform:
	@case "$(PLATFORM)" in \
		mlp1) $(MAKE) package-mlp1 ;; \
		*) echo "unsupported Leaf-RAOfflineProxy-Pak platform: $(PLATFORM)" >&2; exit 1 ;; \
	esac

package-mlp1: runtime-mlp1 app-mlp1 rchash-mlp1 ui-mlp1
	$(PYTHON) scripts/package_mlp1.py \
		--pak-version "$(PAK_VERSION)" \
		--min-leaf-version "$(MIN_LEAF_VERSION)" \
		--min-jawaka-version "$(MIN_JAWAKA_VERSION)"

package-floor-mlp1: ui-mlp1
	$(PYTHON) scripts/package_floor.py \
		--pak-version "$(FLOOR_PAK_VERSION)" \
		--min-leaf-version "$(MIN_LEAF_VERSION)" \
		--min-jawaka-version "$(MIN_JAWAKA_VERSION)"

catalog-fixture:
	$(PYTHON) scripts/build-catalog-fixture.py

catalog-selection-smoke:
	bash scripts/catalog-selection-smoke.sh

test-network-fixtures: app-mlp1
	$(PYTHON) scripts/network-fixture-test.py

test-account-guard: app-mlp1
	$(PYTHON) scripts/account-guard-test.py

test-precache-fixtures: app-mlp1
	$(PYTHON) scripts/precache-fixture-test.py

test-version-gate:
	bash scripts/leaf-version-gate-test.sh

test-version-metadata:
	$(PYTHON) scripts/version-metadata-test.py

test-package: package-mlp1 package-floor-mlp1
	$(PYTHON) scripts/package_check.py

clean:
	rm -rf build/catalog-fixture build/mlp1/rchash \
		build/mlp1/package build/mlp1/floor/package \
		build/mlp1/RAOfflineProxy.mlp1.pak.zip \
		build/mlp1/floor/RAOfflineProxy.mlp1.pak.zip \
		build/mlp1/bin/raofflineproxy-ui build/mlp1/bin/raofflineproxy-floor
