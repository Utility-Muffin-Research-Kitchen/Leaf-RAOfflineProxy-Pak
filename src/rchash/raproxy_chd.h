/* CHD backing for rcheevos' pluggable CD reader.
 *
 * rcheevos ships no CHD support -- it exposes rc_hash_cdreader_t and expects
 * the host to supply one -- so without this the four CD systems on MLP1
 * (Sega CD, PC Engine CD, PlayStation, Dreamcast) cannot be hashed at all.
 *
 * This declaration exists so the glue's call is prototyped. It was previously
 * called with no declaration in scope, which compiles under an implicit-int
 * assumption and would silently accept a later signature change -- on a call
 * whose ORDERING is load-bearing: it must run before rc_hash_initialize_iterator.
 */
#ifndef RAPROXY_CHD_H
#define RAPROXY_CHD_H

/* Registers the CHD reader globally, delegating non-CHD paths to rcheevos'
 * default reader. Idempotent: free to call once per hash. */
void raproxy_chd_install_cdreader(void);

#endif /* RAPROXY_CHD_H */
