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

.PHONY: fetch-sources runtime-mlp1 app-mlp1 ui-mlp1 package-platform package-mlp1 package-floor-mlp1 rchash-mlp1 catalog-fixture catalog-selection-smoke test-package test-version-gate clean

fetch-sources:
	./scripts/fetch-sources.sh

runtime-mlp1: fetch-sources
	./scripts/build-runtime-cpython.sh

app-mlp1: fetch-sources
	./scripts/assemble-app.sh

rchash-mlp1: fetch-sources
	./scripts/build-rchash.sh

ui-mlp1:
	docker run --rm \
		--user "$$(id -u):$$(id -g)" \
		-v "$(WORKSPACE_ROOT):/workspace" \
		-w "$(MLP1_CONTAINER_REPO)" \
		"$(MLP1_TOOLCHAIN_IMAGE)" \
		make -f ports/mlp1/Makefile BUILD_DIR=build/mlp1 CATASTROPHE_DIR=/workspace/Catastrophe

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

test-version-gate:
	bash scripts/leaf-version-gate-test.sh

test-package: package-mlp1 package-floor-mlp1
	$(PYTHON) scripts/package_check.py

clean:
	rm -rf build/catalog-fixture build/mlp1/rchash \
		build/mlp1/package build/mlp1/floor/package \
		build/mlp1/RAOfflineProxy.mlp1.pak.zip \
		build/mlp1/floor/RAOfflineProxy.mlp1.pak.zip \
		build/mlp1/bin/raofflineproxy-ui build/mlp1/bin/raofflineproxy-floor
