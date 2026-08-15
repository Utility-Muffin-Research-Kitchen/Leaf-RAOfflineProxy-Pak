/* CTL-1 client for the pak UI's own service controls.
 *
 * The pak can start and stop its service, and set "Start with Leaf", without
 * sending the user to Settings > Services. Telling someone to leave the app to
 * make the app work is a poor first run, and this pak's first run is exactly
 * that situation: freshly installed, the service is deliberately disabled.
 *
 * Two controls, not one, because jawakad genuinely has two states and
 * conflating them is a bug this project already paid for. "Start with Leaf"
 * (CTL-1 enable/disable) is a persisted preference that sets desired_enabled
 * and starts nothing; run/stop acts on the process now. The launch bridge's
 * routing gate was wrong twice for assuming one implied the other.
 *
 * Framing is CTL-1's: a 4-byte network-order length, then the JSON body, in
 * both directions.
 */
#include "ctl1_local.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

#include "cJSON.h"

/* Generous for a control reply, small enough that a confused peer cannot make
 * the UI allocate without bound. */
#define RAOP_CTL1_MAX_FRAME (256u * 1024u)
#define RAOP_CTL1_TIMEOUT_MS 2000

static atomic_uint raop_ctl1_sequence = 1u;

static int raop__io_all(int fd, void *buffer, size_t length, bool writing) {
    unsigned char *cursor = buffer;
    while (length > 0) {
        ssize_t count = writing ? write(fd, cursor, length) : read(fd, cursor, length);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            return -1;
        }
        cursor += (size_t)count;
        length -= (size_t)count;
    }
    return 0;
}

static int raop__exchange(const char *socket_path, const char *request,
                          size_t request_len, char **response,
                          size_t *response_len) {
    struct sockaddr_un address;
    struct timeval timeout;
    uint32_t network_length;
    uint32_t received_length = 0;
    char *payload = NULL;
    int fd = -1;

    if (!socket_path || !request || request_len == 0 ||
        request_len > RAOP_CTL1_MAX_FRAME || !response || !response_len ||
        strlen(socket_path) >= sizeof(address.sun_path)) {
        return -1;
    }
    *response = NULL;
    *response_len = 0;

    fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) {
        return -1;
    }
    int flags = fcntl(fd, F_GETFD);
    if (flags >= 0) {
        (void)fcntl(fd, F_SETFD, flags | FD_CLOEXEC);
    }
    timeout.tv_sec = RAOP_CTL1_TIMEOUT_MS / 1000;
    timeout.tv_usec = (RAOP_CTL1_TIMEOUT_MS % 1000) * 1000;
    (void)setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    (void)setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));

    memset(&address, 0, sizeof(address));
    address.sun_family = AF_UNIX;
    memcpy(address.sun_path, socket_path, strlen(socket_path) + 1u);
    if (connect(fd, (struct sockaddr *)&address, sizeof(address)) != 0) {
        goto fail;
    }

    network_length = htonl((uint32_t)request_len);
    if (raop__io_all(fd, &network_length, sizeof(network_length), true) != 0 ||
        raop__io_all(fd, (void *)request, request_len, true) != 0 ||
        raop__io_all(fd, &received_length, sizeof(received_length), false) != 0) {
        goto fail;
    }
    received_length = ntohl(received_length);
    if (received_length == 0 || received_length > RAOP_CTL1_MAX_FRAME) {
        goto fail;
    }
    payload = malloc((size_t)received_length + 1u);
    if (!payload || raop__io_all(fd, payload, received_length, false) != 0 ||
        memchr(payload, '\0', received_length) != NULL) {
        goto fail;
    }
    payload[received_length] = '\0';
    close(fd);
    *response = payload;
    *response_len = received_length;
    return 0;

fail:
    free(payload);
    if (fd >= 0) {
        close(fd);
    }
    return -1;
}

void raop_ctl1_socket_path(char *out, size_t out_size) {
    const char *daemon = getenv("UMRK_DAEMON_SOCKET");
    if (!daemon || !daemon[0]) {
        daemon = getenv("JAWAKA_SOCKET_PATH");
    }
    if (daemon && daemon[0]) {
        snprintf(out, out_size, "%s", daemon);
        return;
    }
    const char *runtime = getenv("UMRK_RUNTIME_PATH");
    if (!runtime || !runtime[0]) {
        runtime = "/tmp/jawaka-runtime";
    }
    snprintf(out, out_size, "%s/jawakad.sock", runtime);
}

/* CTL-1 counts fields strictly: exactly v/id/op plus service_id for scoped
 * operations. An extra key is rejected as an invalid payload. */
static cJSON *raop__request(const char *operation, char request_id[64]) {
    cJSON *request = cJSON_CreateObject();
    snprintf(request_id, 64, "raofflineproxy-ui-%u",
             atomic_fetch_add_explicit(&raop_ctl1_sequence, 1u, memory_order_relaxed));
    if (!request || !cJSON_AddNumberToObject(request, "v", 1) ||
        !cJSON_AddStringToObject(request, "id", request_id) ||
        !cJSON_AddStringToObject(request, "op", operation) ||
        !cJSON_AddStringToObject(request, "service_id", RAOP_SERVICE_ID)) {
        cJSON_Delete(request);
        return NULL;
    }
    return request;
}

static cJSON *raop__call(const char *operation, const char *request_id_out) {
    (void)request_id_out;
    char socket_path[1024];
    char request_id[64];
    raop_ctl1_socket_path(socket_path, sizeof(socket_path));

    cJSON *request = raop__request(operation, request_id);
    if (!request) {
        return NULL;
    }
    char *body = cJSON_PrintUnformatted(request);
    cJSON_Delete(request);
    if (!body) {
        return NULL;
    }

    char *payload = NULL;
    size_t payload_len = 0;
    int rc = raop__exchange(socket_path, body, strlen(body), &payload, &payload_len);
    cJSON_free(body);
    if (rc != 0) {
        return NULL;
    }

    /* Validate the parse against the buffer BEFORE freeing it: comparing a
     * pointer into freed memory is undefined even when only its value is
     * read. Trailing bytes after a valid object mean a confused peer, not a
     * reply worth acting on. */
    const char *parse_end = NULL;
    cJSON *response = cJSON_ParseWithLengthOpts(payload, payload_len, &parse_end, false);
    bool consumed_whole_frame = response && parse_end == payload + payload_len;
    free(payload);

    if (!consumed_whole_frame || !cJSON_IsObject(response)) {
        cJSON_Delete(response);
        return NULL;
    }
    return response;
}

bool raop_ctl1_status(raop_service_status *status) {
    if (!status) {
        return false;
    }
    memset(status, 0, sizeof(*status));

    cJSON *response = raop__call("status", NULL);
    if (!response) {
        return false;
    }
    const cJSON *state = cJSON_GetObjectItemCaseSensitive(response, "effective_state");
    const cJSON *enabled = cJSON_GetObjectItemCaseSensitive(response, "desired_enabled");
    if (!cJSON_IsString(state)) {
        /* An error reply carries "error" instead; the service is not
         * controllable right now, which the caller shows rather than guesses
         * about. */
        cJSON_Delete(response);
        return false;
    }
    snprintf(status->effective_state, sizeof(status->effective_state), "%s",
             state->valuestring);
    status->start_with_leaf = cJSON_IsTrue(enabled);
    status->found = true;
    cJSON_Delete(response);
    return true;
}

bool raop_ctl1_action(const char *operation, char *error, size_t error_size) {
    if (error && error_size) {
        error[0] = '\0';
    }
    cJSON *response = raop__call(operation, NULL);
    if (!response) {
        if (error && error_size) {
            snprintf(error, error_size, "Leaf service control is unavailable.");
        }
        return false;
    }
    bool ok = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(response, "ok"));
    if (!ok && error && error_size) {
        const cJSON *failure = cJSON_GetObjectItemCaseSensitive(response, "error");
        const cJSON *message = cJSON_GetObjectItemCaseSensitive(failure, "message");
        snprintf(error, error_size, "%s",
                 cJSON_IsString(message) ? message->valuestring
                                         : "The service request failed.");
    }
    cJSON_Delete(response);
    return ok;
}

const char *raop_service_state_label(const raop_service_status *status) {
    if (!status || !status->found) {
        return "unavailable";
    }
    const char *state = status->effective_state;
    if (strcmp(state, "disabled") == 0) return "not running";
    if (strcmp(state, "stopped") == 0) return "stopped";
    if (strcmp(state, "starting") == 0) return "starting";
    if (strcmp(state, "running") == 0) return "running";
    if (strcmp(state, "stopping") == 0) return "stopping";
    if (strcmp(state, "backoff") == 0) return "retrying";
    return state[0] ? state : "unknown";
}

bool raop_service_is_up(const raop_service_status *status) {
    if (!status || !status->found) {
        return false;
    }
    return strcmp(status->effective_state, "running") == 0 ||
           strcmp(status->effective_state, "starting") == 0;
}
