/* Loopback-only HTTP for the pak UI. See http_local.c for why it is this small. */
#ifndef RAOFFLINEPROXY_HTTP_LOCAL_H
#define RAOFFLINEPROXY_HTTP_LOCAL_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

typedef struct {
    int status;        /* HTTP status code, 0 if it could not be parsed */
    char *body;        /* NUL-terminated; owned by the caller */
    size_t body_len;
    bool truncated;    /* response hit the size ceiling */
} http_local_response;

/* Returns false when the service could not be reached or did not answer in
 * time -- which the UI must report as "service not running", never as an
 * empty result. On true the caller owns response->body. */
bool http_local_request(uint16_t port, const char *method, const char *path,
                        const char *body, http_local_response *response);

void http_local_response_free(http_local_response *response);

#endif /* RAOFFLINEPROXY_HTTP_LOCAL_H */
