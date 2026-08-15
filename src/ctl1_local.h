/* CTL-1 service control for the pak UI. See ctl1_local.c for why there are
 * two independent controls rather than one. */
#ifndef RAOFFLINEPROXY_CTL1_LOCAL_H
#define RAOFFLINEPROXY_CTL1_LOCAL_H

#include <stdbool.h>
#include <stddef.h>

#define RAOP_SERVICE_ID "org.umrk.raofflineproxy"

typedef struct {
    bool found;                  /* jawakad answered with a real status */
    char effective_state[32];    /* running | starting | stopped | disabled | ... */
    bool start_with_leaf;        /* desired_enabled: the persisted autostart preference */
} raop_service_status;

/* Resolves the jawakad control socket the same way the Syncthing pak does:
 * UMRK_DAEMON_SOCKET, then JAWAKA_SOCKET_PATH, then
 * $UMRK_RUNTIME_PATH/jawakad.sock. */
void raop_ctl1_socket_path(char *out, size_t out_size);

/* False when jawakad is unreachable or answered an error -- which the UI must
 * report as "control unavailable" rather than as "stopped". */
bool raop_ctl1_status(raop_service_status *status);

/* operation: "run" | "stop" | "enable" | "disable" | "restart".
 * enable/disable set the persisted Start with Leaf preference and start
 * nothing; run/stop act on the process now. */
bool raop_ctl1_action(const char *operation, char *error, size_t error_size);

const char *raop_service_state_label(const raop_service_status *status);
bool raop_service_is_up(const raop_service_status *status);

#endif /* RAOFFLINEPROXY_CTL1_LOCAL_H */
