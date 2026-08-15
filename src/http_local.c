/* Minimal HTTP client for the pak UI's loopback control calls.
 *
 * The UI talks to its own service over 127.0.0.1, so this needs no TLS, no
 * redirects, no chunked decoding and no keep-alive: the service answers one
 * response per connection and shuts down the write side, which makes
 * read-to-EOF both the simplest and the most robust framing available.
 *
 * Everything here is bounded. A UI that hangs on a socket is worse than one
 * that reports the service as unreachable: the user cannot tell the difference
 * between "thinking" and "wedged", and there is no way out but the power
 * button. So the connect and the read both have timeouts, and the response has
 * a hard ceiling.
 */
#include "http_local.h"

#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

/* The full game list is ~1,968 entries; 4 MB is far above that and far below
 * anything that would trouble the device. A response past it is a bug, not a
 * big library, and truncating loudly beats growing without bound. */
#define HTTP_LOCAL_MAX_BODY (4u * 1024u * 1024u)
#define HTTP_LOCAL_CONNECT_TIMEOUT_S 2
#define HTTP_LOCAL_READ_TIMEOUT_S 10

static int http__set_timeout(int fd, int option, int seconds) {
    struct timeval tv;
    tv.tv_sec = seconds;
    tv.tv_usec = 0;
    return setsockopt(fd, SOL_SOCKET, option, &tv, sizeof(tv));
}

static int http__connect(uint16_t port) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        return -1;
    }

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);

    if (http__set_timeout(fd, SO_SNDTIMEO, HTTP_LOCAL_CONNECT_TIMEOUT_S) != 0 ||
        http__set_timeout(fd, SO_RCVTIMEO, HTTP_LOCAL_READ_TIMEOUT_S) != 0) {
        close(fd);
        return -1;
    }
    /* Control calls are small and latency-visible in the UI. */
    int one = 1;
    (void)setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));

    if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        close(fd);
        return -1;
    }
    return fd;
}

static bool http__write_all(int fd, const char *data, size_t len) {
    size_t sent = 0;
    while (sent < len) {
        ssize_t n = send(fd, data + sent, len - sent, MSG_NOSIGNAL);
        if (n <= 0) {
            if (n < 0 && errno == EINTR) {
                continue;
            }
            return false;
        }
        sent += (size_t)n;
    }
    return true;
}

void http_local_response_free(http_local_response *response) {
    if (!response) {
        return;
    }
    free(response->body);
    response->body = NULL;
    response->body_len = 0;
    response->status = 0;
}

bool http_local_request(uint16_t port, const char *method, const char *path,
                        const char *body, http_local_response *response) {
    if (!method || !path || !response) {
        return false;
    }
    memset(response, 0, sizeof(*response));

    int fd = http__connect(port);
    if (fd < 0) {
        return false;
    }

    size_t body_len = body ? strlen(body) : 0;
    char header[512];
    int header_len = snprintf(
        header, sizeof(header),
        "%s %s HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        "Connection: close\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: %zu\r\n"
        "\r\n",
        method, path, body_len);
    if (header_len <= 0 || (size_t)header_len >= sizeof(header)) {
        close(fd);
        return false;
    }

    if (!http__write_all(fd, header, (size_t)header_len) ||
        (body_len && !http__write_all(fd, body, body_len))) {
        close(fd);
        return false;
    }

    char *buffer = NULL;
    size_t len = 0;
    size_t capacity = 0;
    bool truncated = false;
    for (;;) {
        if (len == capacity) {
            size_t next = capacity ? capacity * 2 : 8192;
            if (next > HTTP_LOCAL_MAX_BODY) {
                next = HTTP_LOCAL_MAX_BODY;
            }
            if (next == capacity) {
                truncated = true;
                break;
            }
            char *grown = realloc(buffer, next + 1);
            if (!grown) {
                free(buffer);
                close(fd);
                return false;
            }
            buffer = grown;
            capacity = next;
        }
        ssize_t n = recv(fd, buffer + len, capacity - len, 0);
        if (n == 0) {
            break; /* peer closed: the whole response is in hand */
        }
        if (n < 0) {
            if (errno == EINTR) {
                continue;
            }
            /* Includes the receive timeout, which is a failure here: the
             * service always answers promptly or not at all. */
            free(buffer);
            close(fd);
            return false;
        }
        len += (size_t)n;
    }
    close(fd);

    if (!buffer) {
        return false;
    }
    buffer[len] = '\0';

    if (strncmp(buffer, "HTTP/1.", 7) != 0) {
        free(buffer);
        return false;
    }
    const char *status_field = strchr(buffer, ' ');
    response->status = status_field ? atoi(status_field + 1) : 0;

    const char *separator = strstr(buffer, "\r\n\r\n");
    size_t offset = separator ? (size_t)(separator - buffer) + 4 : len;
    size_t payload_len = len - offset;

    char *payload = malloc(payload_len + 1);
    if (!payload) {
        free(buffer);
        return false;
    }
    memcpy(payload, buffer + offset, payload_len);
    payload[payload_len] = '\0';
    free(buffer);

    response->body = payload;
    response->body_len = payload_len;
    response->truncated = truncated;
    return true;
}
