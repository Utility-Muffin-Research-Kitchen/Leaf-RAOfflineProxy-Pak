SHELL := /bin/bash

CC ?= aarch64-buildroot-linux-gnu-gcc
STRIP ?= aarch64-buildroot-linux-gnu-strip
BUILD_DIR ?= build/mlp1
CATASTROPHE_DIR ?= /workspace/Catastrophe
MLP1_BUILD_PROFILE ?= release
PKG_CONFIG ?= pkg-config
MLP1_FLAGS_MK ?= $(firstword $(wildcard /opt/mlp1-toolchain/umrk/mlp1-build-flags.mk /workspace/mlp1-toolchain/flags/mlp1-build-flags.mk ../mlp1-toolchain/flags/mlp1-build-flags.mk))
ifneq ($(MLP1_FLAGS_MK),)
include $(MLP1_FLAGS_MK)
else
UMRK_MLP1_PROFILE_CFLAGS ?= -O2 -mcpu=cortex-a55 -mtune=cortex-a55 -ffunction-sections -fdata-sections -DNDEBUG
UMRK_MLP1_PROFILE_LDFLAGS ?= -Wl,--gc-sections
endif

COMMON_SRC := src/ui_main.c $(CATASTROPHE_DIR)/include/cjson/cJSON.c
# The floor screen states that it performs no network activity. It keeps that
# promise by not linking the control client at all, rather than by leaving it
# uncalled -- the binary is the claim, not the code path.
UI_SRC := $(COMMON_SRC) src/http_local.c
FLOOR_SRC := $(COMMON_SRC)
HDR := $(CATASTROPHE_DIR)/include/catastrophe.h $(CATASTROPHE_DIR)/include/catastrophe_widgets.h src/http_local.h
SDL_CFLAGS := $(shell $(PKG_CONFIG) --cflags sdl2 SDL2_ttf SDL2_image)
SDL_LIBS := $(shell $(PKG_CONFIG) --libs sdl2 SDL2_ttf SDL2_image)
CFLAGS := -std=gnu11 $(UMRK_MLP1_PROFILE_CFLAGS) -Wall -Wextra -Werror \
    -DPLATFORM_MLP1 -I$(CATASTROPHE_DIR)/include -I$(CATASTROPHE_DIR)/include/cjson $(SDL_CFLAGS)
LDFLAGS := $(UMRK_MLP1_PROFILE_LDFLAGS) $(SDL_LIBS) -lm

.PHONY: all clean

all: $(BUILD_DIR)/bin/raofflineproxy-ui $(BUILD_DIR)/bin/raofflineproxy-floor

$(BUILD_DIR)/bin/raofflineproxy-ui: $(UI_SRC) $(HDR)
	@mkdir -p "$(BUILD_DIR)/bin"
	$(CC) $(CFLAGS) -Isrc -o "$@" $(UI_SRC) $(LDFLAGS)
	$(STRIP) "$@"

$(BUILD_DIR)/bin/raofflineproxy-floor: $(FLOOR_SRC) $(HDR)
	@mkdir -p "$(BUILD_DIR)/bin"
	$(CC) $(CFLAGS) -Isrc -DRAOFFLINEPROXY_FLOOR -o "$@" $(FLOOR_SRC) $(LDFLAGS)
	$(STRIP) "$@"

clean:
	rm -rf "$(BUILD_DIR)/bin/raofflineproxy-ui" "$(BUILD_DIR)/bin/raofflineproxy-floor"
