/* Leaf's ctypes ABI over rcheevos' rc_hash.
 *
 * The pak needs a RetroAchievements ROM hash for a game it has NOT launched,
 * so it can pre-cache achievement data before the user goes offline. rcheevos
 * is the only correct source for that hash: it is per-console (NES skips the
 * iNES header, Mega Drive is a plain file MD5, Arcade hashes a name), and a
 * hash that disagrees with the one RetroArch computes is worse than none --
 * the cache is written under a key nothing ever looks up.
 *
 * Two entry points, both writing NUL-terminated 32-char hex into out:
 *
 *   int raproxy_hash_file(const char *path, uint32_t console_id, char out[33]);
 *   int raproxy_hash_buffer(const uint8_t *data, size_t len,
 *                           uint32_t console_id, const char *name,
 *                           char out[33]);
 *
 * The buffer form exists because archives must be decompressed before hashing.
 * Handed a .zip, rc_hash's untargeted iterator returns the ARCADE hash, and
 * Arcade hashes the filename rather than the content -- so a zipped Mega Drive
 * ROM hashes to something RetroArch never asks for. The caller extracts (the
 * Python stdlib's zipfile is enough) and passes the bytes here with the
 * console id its Leaf system implies.
 *
 * Both return 1 on success and 0 on failure.
 */
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "rc_hash.h"
#include "rc_version.h"

#include "raproxy_chd.h"

#define RAPROXY_HASH_LEN 33

int raproxy_hash_file(const char *path, uint32_t console_id, char *out) {
    if (!path || !out) {
        return 0;
    }
    /* Registers globally and delegates non-CHD paths to rcheevos' default
       reader; idempotent, so calling it per hash is free after the first. */
    raproxy_chd_install_cdreader();
    memset(out, 0, RAPROXY_HASH_LEN);

    rc_hash_iterator_t iterator;
    rc_hash_initialize_iterator(&iterator, path, NULL, 0);
    char hash[RAPROXY_HASH_LEN];
    int ok = rc_hash_generate(hash, console_id, &iterator);
    rc_hash_destroy_iterator(&iterator);
    if (!ok) {
        return 0;
    }
    memcpy(out, hash, RAPROXY_HASH_LEN);
    return 1;
}

int raproxy_hash_buffer(const uint8_t *data, size_t len, uint32_t console_id,
                        const char *name, char *out) {
    if (!data || !out) {
        return 0;
    }
    memset(out, 0, RAPROXY_HASH_LEN);

    /* name is the ROM's filename inside its archive. rc_hash uses it only to
     * pick an extension-dependent path for some consoles; the bytes decide the
     * hash. Passing "" is valid where the console does not care. */
    rc_hash_iterator_t iterator;
    rc_hash_initialize_iterator(&iterator, name ? name : "", data, len);
    char hash[RAPROXY_HASH_LEN];
    int ok = rc_hash_generate(hash, console_id, &iterator);
    rc_hash_destroy_iterator(&iterator);
    if (!ok) {
        return 0;
    }
    memcpy(out, hash, RAPROXY_HASH_LEN);
    return 1;
}

/* Version of the rcheevos this was built against, so the pak can record which
 * hashing rules produced a cached entry and invalidate if the pin moves. */
const char *raproxy_rchash_version(void) {
    return RCHEEVOS_VERSION_STRING;
}
