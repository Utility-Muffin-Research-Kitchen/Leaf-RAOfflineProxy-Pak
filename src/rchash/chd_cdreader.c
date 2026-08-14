/* CHD backing for rcheevos' pluggable CD reader.
 *
 * rcheevos has no built-in CHD support: rc_hash exposes an rc_hash_cdreader_t
 * and expects the host to supply one. Without it the four CD systems on MLP1
 * (Sega CD, PC Engine CD, PlayStation, Dreamcast) cannot be hashed at all,
 * because their images are .chd.
 *
 * The contract is rcheevos' own default reader (src/rhash/cdreader.c):
 *
 *   - `sector` is ABSOLUTE, not track-relative; the reader subtracts the
 *     track's first sector itself.
 *   - read_sector returns USER DATA, with the sync/header skipped for raw
 *     modes, and spans consecutive sectors when more is requested than one
 *     sector holds.
 *   - first_track_sector reports where the opened track begins, so callers can
 *     turn an absolute sector back into an offset.
 *
 * CHD stores a CD as fixed 2448-byte frames (2352 data + 96 subcode), eight to
 * a hunk, with each track's start padded to a 4-frame boundary. Track layout
 * comes from CHT2/CHTR metadata rather than from the file, which is why this
 * needs a parser at all.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

#include "libchdr/chd.h"
#include "libchdr/cdrom.h"
#include "rc_hash.h"

#define RAPROXY_MAX_TRACKS 99
#define RAPROXY_CHD_TRACK_PADDING 4

typedef struct {
    uint32_t number;
    uint32_t frames;      /* data frames, excluding pregap */
    uint32_t pregap;
    uint32_t chd_start;   /* first frame of this track within the CHD */
    uint32_t abs_start;   /* first absolute sector, as rc_hash counts them */
    uint32_t sector_size; /* bytes of the 2352 frame this track type uses */
    uint32_t data_size;   /* user-data bytes per sector */
    uint32_t header_size; /* bytes skipped before user data */
    int is_data;
} raproxy_chd_track;

typedef struct {
    chd_file *chd;
    uint8_t *hunk;
    uint32_t hunk_bytes;
    uint32_t frames_per_hunk;
    int32_t cached_hunk;
    raproxy_chd_track track;
} raproxy_chd_handle;

/* Map a CHD track TYPE string to the sector geometry rc_hash expects.
 *
 * Straight from rcheevos' own table (src/rhash/cdreader.c): every one of these
 * modes carries a 2048-byte payload, and only the header length differs.
 *
 *   MODE1/2048  no header                       payload 2048
 *   MODE1/2352  16-byte header, 288-byte footer payload 2048
 *   MODE2/2336   8-byte header, 280-byte footer payload 2048
 *   MODE2/2352  24-byte header, 280-byte footer payload 2048
 *
 * Getting MODE2 wrong is silent: the read succeeds and returns plausible bytes
 * from the wrong offset, so the hash is confidently incorrect rather than
 * absent. */
static void raproxy_track_geometry(const char *type, raproxy_chd_track *track) {
    track->sector_size = CD_MAX_SECTOR_DATA;
    track->header_size = 0;
    track->data_size = CD_MAX_SECTOR_DATA;
    track->is_data = 1;

    if (strcmp(type, "MODE1") == 0) {
        /* cooked 2048 */
        track->data_size = 2048;
    } else if (strcmp(type, "MODE1_RAW") == 0) {
        /* MODE1/2352: 16-byte header, 288-byte footer */
        track->header_size = 16;
        track->data_size = 2048;
    } else if (strcmp(type, "MODE2") == 0 || strcmp(type, "MODE2_FORM_MIX") == 0) {
        /* MODE2/2336: 8-byte subheader, 280-byte footer */
        track->header_size = 8;
        track->data_size = 2048;
    } else if (strcmp(type, "MODE2_FORM1") == 0) {
        /* cooked 2048 */
        track->data_size = 2048;
    } else if (strcmp(type, "MODE2_FORM2") == 0) {
        track->data_size = 2324;
    } else if (strcmp(type, "MODE2_RAW") == 0) {
        /* MODE2/2352: 24-byte header (16 sync+header + 8 subheader), 280-byte
         * footer. This is the PlayStation layout. */
        track->header_size = 24;
        track->data_size = 2048;
    } else if (strcmp(type, "AUDIO") == 0) {
        track->is_data = 0;
        track->data_size = CD_MAX_SECTOR_DATA;
    }
}

/* Walk CHT2/CHTR metadata and fill the track table with both CHD frame
 * offsets and absolute sector numbers. */
static int raproxy_read_tracks(chd_file *chd, raproxy_chd_track *tracks,
                               int *out_count) {
    char metadata[512];
    uint32_t resultlen = 0;
    uint32_t chd_frame = 0;
    /* rcheevos addresses sectors in LBA where track 1 begins at 0, NOT the
     * 150-frame MSF offset: rc_hash_sega_cd reads sector 0 outright and
     * rc_cd_read_sector passes the number through unmodified. Starting this
     * counter at 150 made every such read land before the track and return
     * nothing, which surfaced as "Not a Sega CD" with the data sitting
     * correctly in the file the whole time. */
    uint32_t abs_sector = 0;
    int count = 0;

    for (int index = 0; index < RAPROXY_MAX_TRACKS; index++) {
        char type[32] = "", subtype[32] = "", pgtype[32] = "", pgsub[32] = "";
        int number = 0, frames = 0, pregap = 0, postgap = 0;
        chd_error err;

        err = chd_get_metadata(chd, CDROM_TRACK_METADATA2_TAG, index,
                               metadata, sizeof(metadata), &resultlen, NULL, NULL);
        if (err == CHDERR_NONE) {
            if (sscanf(metadata, CDROM_TRACK_METADATA2_FORMAT, &number, type,
                       subtype, &frames, &pregap, pgtype, pgsub, &postgap) != 8) {
                break;
            }
        } else {
            err = chd_get_metadata(chd, CDROM_TRACK_METADATA_TAG, index,
                                   metadata, sizeof(metadata), &resultlen, NULL, NULL);
            if (err != CHDERR_NONE) {
                break;
            }
            if (sscanf(metadata, CDROM_TRACK_METADATA_FORMAT, &number, type,
                       subtype, &frames) != 4) {
                break;
            }
            pregap = 0;
        }

        raproxy_chd_track *track = &tracks[count];
        memset(track, 0, sizeof(*track));
        track->number = (uint32_t)number;
        track->frames = (uint32_t)frames;
        track->pregap = (uint32_t)pregap;
        track->chd_start = chd_frame;
        raproxy_track_geometry(type, track);

        /* A pregap declared in metadata is not stored in the CHD, so it
         * advances the absolute sector counter without consuming frames. */
        track->abs_start = abs_sector + (uint32_t)pregap;
        abs_sector += (uint32_t)pregap + (uint32_t)frames;

        /* Each track's data is padded up to a 4-frame boundary in the CHD. */
        chd_frame += (uint32_t)frames;
        chd_frame = (chd_frame + RAPROXY_CHD_TRACK_PADDING - 1) /
                    RAPROXY_CHD_TRACK_PADDING * RAPROXY_CHD_TRACK_PADDING;
        count++;
    }

    *out_count = count;
    return count > 0;
}

static int raproxy_select_track(const raproxy_chd_track *tracks, int count,
                                uint32_t wanted) {
    if (wanted == RC_HASH_CDTRACK_FIRST_DATA) {
        for (int i = 0; i < count; i++) {
            if (tracks[i].is_data) return i;
        }
        return -1;
    }
    if (wanted == RC_HASH_CDTRACK_LAST) {
        return count - 1;
    }
    if (wanted == RC_HASH_CDTRACK_LARGEST) {
        int best = -1;
        for (int i = 0; i < count; i++) {
            if (tracks[i].is_data && (best < 0 || tracks[i].frames > tracks[best].frames)) {
                best = i;
            }
        }
        return best;
    }
    if (wanted == RC_HASH_CDTRACK_FIRST_OF_SECOND_SESSION) {
        /* Single-session images are the norm for these consoles; without
         * session metadata the honest answer is "no such track". */
        return -1;
    }
    for (int i = 0; i < count; i++) {
        if (tracks[i].number == wanted) return i;
    }
    return -1;
}

void *raproxy_chd_open_track(const char *path, uint32_t track) {
    chd_file *chd = NULL;
    if (chd_open(path, CHD_OPEN_READ, NULL, &chd) != CHDERR_NONE) {
        return NULL;
    }

    raproxy_chd_track tracks[RAPROXY_MAX_TRACKS];
    int count = 0;
    if (!raproxy_read_tracks(chd, tracks, &count)) {
        chd_close(chd);
        return NULL;
    }

    int index = raproxy_select_track(tracks, count, track);
    if (index < 0) {
        chd_close(chd);
        return NULL;
    }

    const chd_header *header = chd_get_header(chd);
    if (!header || header->hunkbytes == 0) {
        chd_close(chd);
        return NULL;
    }

    raproxy_chd_handle *handle = (raproxy_chd_handle *)calloc(1, sizeof(*handle));
    if (!handle) {
        chd_close(chd);
        return NULL;
    }
    handle->chd = chd;
    handle->hunk_bytes = header->hunkbytes;
    handle->frames_per_hunk = header->hunkbytes / CD_FRAME_SIZE;
    handle->cached_hunk = -1;
    handle->track = tracks[index];
    handle->hunk = (uint8_t *)malloc(header->hunkbytes);
    if (!handle->hunk || handle->frames_per_hunk == 0) {
        free(handle->hunk);
        free(handle);
        chd_close(chd);
        return NULL;
    }
    return handle;
}

/* Copy one sector's user data out of the CHD, reading (and caching) whichever
 * hunk contains it. */
static size_t raproxy_copy_sector(raproxy_chd_handle *handle, uint32_t abs_sector,
                                  uint8_t *out, size_t want) {
    const raproxy_chd_track *track = &handle->track;
    if (abs_sector < track->abs_start) {
        return 0;
    }
    uint32_t offset_in_track = abs_sector - track->abs_start;
    if (offset_in_track >= track->frames) {
        return 0;
    }

    uint32_t frame = track->chd_start + offset_in_track;
    uint32_t hunk = frame / handle->frames_per_hunk;
    uint32_t frame_in_hunk = frame % handle->frames_per_hunk;

    if ((int32_t)hunk != handle->cached_hunk) {
        if (chd_read(handle->chd, hunk, handle->hunk) != CHDERR_NONE) {
            return 0;
        }
        handle->cached_hunk = (int32_t)hunk;
    }

    size_t available = track->data_size;
    if (want > available) {
        want = available;
    }
    memcpy(out, handle->hunk + (size_t)frame_in_hunk * CD_FRAME_SIZE +
                track->header_size, want);
    return want;
}

size_t raproxy_chd_read_sector(void *track_handle, uint32_t sector,
                                      void *buffer, size_t requested_bytes) {
    raproxy_chd_handle *handle = (raproxy_chd_handle *)track_handle;
    if (!handle || !buffer) {
        return 0;
    }

    uint8_t *out = (uint8_t *)buffer;
    size_t total = 0;
    /* Span consecutive sectors, exactly as the default reader does when asked
     * for more than one sector's worth. */
    while (requested_bytes > 0) {
        size_t chunk = raproxy_copy_sector(handle, sector, out, requested_bytes);
        if (chunk == 0) {
            break;
        }
        total += chunk;
        out += chunk;
        requested_bytes -= chunk;
        sector++;
    }
    return total;
}

void raproxy_chd_close_track(void *track_handle) {
    raproxy_chd_handle *handle = (raproxy_chd_handle *)track_handle;
    if (!handle) {
        return;
    }
    if (handle->chd) {
        chd_close(handle->chd);
    }
    free(handle->hunk);
    free(handle);
}

uint32_t raproxy_chd_first_track_sector(void *track_handle) {
    raproxy_chd_handle *handle = (raproxy_chd_handle *)track_handle;
    return handle ? handle->track.abs_start : 0;
}

int raproxy_path_is_chd(const char *path) {
    if (!path) {
        return 0;
    }
    size_t len = strlen(path);
    return len > 4 && strcasecmp(path + len - 4, ".chd") == 0;
}

/* rcheevos resets iterator->callbacks.cdreader from a global before hashing a
 * disc (hash_disc.c: rc_hash_reset_iterator_disc), so installing a reader on
 * the iterator is silently discarded. The supported hook is the global
 * rc_hash_init_custom_cdreader, which means our reader also sees cue/bin/iso
 * -- hence the wrapper: CHD paths go to libchdr, everything else is delegated
 * back to rcheevos' own default reader. */
static rc_hash_cdreader_t g_default_cdreader;
static int g_default_captured = 0;

typedef struct {
    uint32_t magic;
    int is_chd;
    void *inner;
} raproxy_track_wrapper;

#define RAPROXY_WRAPPER_MAGIC 0x52414f50u  /* 'RAOP' */

static void *raproxy_wrap(void *inner, int is_chd) {
    if (!inner) {
        return NULL;
    }
    raproxy_track_wrapper *wrapper =
        (raproxy_track_wrapper *)calloc(1, sizeof(*wrapper));
    if (!wrapper) {
        return NULL;
    }
    wrapper->magic = RAPROXY_WRAPPER_MAGIC;
    wrapper->is_chd = is_chd;
    wrapper->inner = inner;
    return wrapper;
}

static raproxy_track_wrapper *raproxy_unwrap(void *handle) {
    raproxy_track_wrapper *wrapper = (raproxy_track_wrapper *)handle;
    return (wrapper && wrapper->magic == RAPROXY_WRAPPER_MAGIC) ? wrapper : NULL;
}

static void *raproxy_cdreader_open_track_iterator(
        const char *path, uint32_t track, const rc_hash_iterator_t *iterator) {
    if (raproxy_path_is_chd(path)) {
        return raproxy_wrap(raproxy_chd_open_track(path, track), 1);
    }
    if (g_default_cdreader.open_track_iterator) {
        return raproxy_wrap(
            g_default_cdreader.open_track_iterator(path, track, iterator), 0);
    }
    if (g_default_cdreader.open_track) {
        return raproxy_wrap(g_default_cdreader.open_track(path, track), 0);
    }
    return NULL;
}

static void *raproxy_cdreader_open_track(const char *path, uint32_t track) {
    return raproxy_cdreader_open_track_iterator(path, track, NULL);
}

static size_t raproxy_cdreader_read_sector(void *track_handle, uint32_t sector,
                                           void *buffer, size_t requested_bytes) {
    raproxy_track_wrapper *wrapper = raproxy_unwrap(track_handle);
    if (!wrapper) {
        return 0;
    }
    if (wrapper->is_chd) {
        return raproxy_chd_read_sector(wrapper->inner, sector, buffer, requested_bytes);
    }
    return g_default_cdreader.read_sector
        ? g_default_cdreader.read_sector(wrapper->inner, sector, buffer, requested_bytes)
        : 0;
}

static void raproxy_cdreader_close_track(void *track_handle) {
    raproxy_track_wrapper *wrapper = raproxy_unwrap(track_handle);
    if (!wrapper) {
        return;
    }
    if (wrapper->is_chd) {
        raproxy_chd_close_track(wrapper->inner);
    } else if (g_default_cdreader.close_track) {
        g_default_cdreader.close_track(wrapper->inner);
    }
    wrapper->magic = 0;
    free(wrapper);
}

static uint32_t raproxy_cdreader_first_track_sector(void *track_handle) {
    raproxy_track_wrapper *wrapper = raproxy_unwrap(track_handle);
    if (!wrapper) {
        return 0;
    }
    if (wrapper->is_chd) {
        return raproxy_chd_first_track_sector(wrapper->inner);
    }
    return g_default_cdreader.first_track_sector
        ? g_default_cdreader.first_track_sector(wrapper->inner)
        : 0;
}

void raproxy_chd_install_cdreader(void) {
    if (g_default_captured) {
        return;
    }
    rc_hash_get_default_cdreader(&g_default_cdreader);
    g_default_captured = 1;

    rc_hash_cdreader_t reader;
    memset(&reader, 0, sizeof(reader));
    reader.open_track = raproxy_cdreader_open_track;
    reader.open_track_iterator = raproxy_cdreader_open_track_iterator;
    reader.read_sector = raproxy_cdreader_read_sector;
    reader.close_track = raproxy_cdreader_close_track;
    reader.first_track_sector = raproxy_cdreader_first_track_sector;
    rc_hash_init_custom_cdreader(&reader);
}
