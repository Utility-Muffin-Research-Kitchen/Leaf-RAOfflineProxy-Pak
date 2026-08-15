/* RAOfflineProxy pak UI.
 *
 * Two jobs: explain what the pak is, and drive offline pre-caching. The
 * caching itself deliberately lives in the service, not here -- the service
 * owns the cache, the HTTP stack and the learned credentials, and a run there
 * survives this process exiting. So this is a thin client over the service's
 * loopback control endpoints, and closing the UI mid-run is harmless.
 *
 * The per-game action lives here rather than in Jawaka's launcher on purpose:
 * product-specific RAOfflineProxy UI inside Jawaka is exactly what the plan's
 * locked boundaries forbid. The launch bridge is allowed there because it is
 * bounded and generic-free; a "prepare this game" button would not be.
 *
 * The floor build (-DRAOFFLINEPROXY_FLOOR) is the inert compatibility screen
 * shown when Leaf is too old. It promises to perform no network activity, so
 * the control client is compiled out of it entirely rather than merely left
 * uncalled -- a promise the binary keeps, not just the code path.
 */
#define CAT_IMPLEMENTATION
#include "catastrophe.h"
#define CAT_WIDGETS_IMPLEMENTATION
#include "catastrophe_widgets.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef RAOFFLINEPROXY_FLOOR
#include "cJSON.h"
#include "http_local.h"

/* The Leaf service adapter binds this port and nothing configures it away;
 * it is a single constant there and a single constant here. */
#define RAOP_PORT 8080

/* Refuse to build a picker list from an implausible response rather than
 * allocating whatever a bug on the other end claims. The qualification device
 * holds 1,968 games. */
#define RAOP_MAX_GAMES 20000

typedef struct {
    int id;
    char *name;
    char *system;
} raop_game;

static void raop_games_free(raop_game *games, int count) {
    if (!games) {
        return;
    }
    for (int i = 0; i < count; i++) {
        free(games[i].name);
        free(games[i].system);
    }
    free(games);
}

static char *raop_strdup(const char *value) {
    if (!value) {
        value = "";
    }
    size_t len = strlen(value);
    char *copy = malloc(len + 1);
    if (copy) {
        memcpy(copy, value, len + 1);
    }
    return copy;
}

/* -- service calls ------------------------------------------------------- */

static bool raop_service_ready(void) {
    http_local_response response;
    if (!http_local_request(RAOP_PORT, "GET", "/leaf/health", NULL, &response)) {
        return false;
    }
    bool ready = response.status == 200 && response.body &&
                 strstr(response.body, "\"ready\":true") != NULL;
    http_local_response_free(&response);
    return ready;
}

/* Pull an "error" string out of a control response so the UI can show the
 * service's own words. Refusals here are meaningful -- "no cached token",
 * "already running", "schema is version 7" -- and paraphrasing them in the UI
 * would mean maintaining two descriptions of the same condition. */
static void raop_copy_error(const char *body, char *out, size_t out_size) {
    snprintf(out, out_size, "The service rejected the request.");
    if (!body) {
        return;
    }
    cJSON *root = cJSON_Parse(body);
    if (!root) {
        return;
    }
    const cJSON *error = cJSON_GetObjectItemCaseSensitive(root, "error");
    if (cJSON_IsString(error) && error->valuestring) {
        snprintf(out, out_size, "%s", error->valuestring);
    }
    cJSON_Delete(root);
}

static bool raop_fetch_games(const char *scope, raop_game **out_games,
                             int *out_count, char *error, size_t error_size) {
    *out_games = NULL;
    *out_count = 0;

    char path[128];
    snprintf(path, sizeof(path), "/leaf/precache/games?scope=%s", scope);

    http_local_response response;
    if (!http_local_request(RAOP_PORT, "GET", path, NULL, &response)) {
        snprintf(error, error_size,
                 "Could not reach the RAOfflineProxy service.\n\n"
                 "Enable it under Settings > Services, then try again.");
        return false;
    }
    if (response.status != 200) {
        raop_copy_error(response.body, error, error_size);
        http_local_response_free(&response);
        return false;
    }

    cJSON *root = cJSON_Parse(response.body);
    http_local_response_free(&response);
    if (!root) {
        snprintf(error, error_size, "The service sent a malformed game list.");
        return false;
    }

    const cJSON *array = cJSON_GetObjectItemCaseSensitive(root, "games");
    int count = cJSON_IsArray(array) ? cJSON_GetArraySize(array) : 0;
    if (count <= 0) {
        cJSON_Delete(root);
        *out_count = 0;
        return true;
    }
    if (count > RAOP_MAX_GAMES) {
        count = RAOP_MAX_GAMES;
    }

    raop_game *games = calloc((size_t)count, sizeof(*games));
    if (!games) {
        cJSON_Delete(root);
        snprintf(error, error_size, "Out of memory building the game list.");
        return false;
    }

    int filled = 0;
    for (int i = 0; i < count; i++) {
        const cJSON *entry = cJSON_GetArrayItem(array, i);
        const cJSON *id = cJSON_GetObjectItemCaseSensitive(entry, "id");
        const cJSON *name = cJSON_GetObjectItemCaseSensitive(entry, "name");
        const cJSON *system = cJSON_GetObjectItemCaseSensitive(entry, "system");
        if (!cJSON_IsNumber(id) || !cJSON_IsString(name)) {
            continue;
        }
        games[filled].id = id->valueint;
        games[filled].name = raop_strdup(name->valuestring);
        games[filled].system =
            raop_strdup(cJSON_IsString(system) ? system->valuestring : "");
        if (!games[filled].name || !games[filled].system) {
            raop_games_free(games, filled + 1);
            cJSON_Delete(root);
            snprintf(error, error_size, "Out of memory building the game list.");
            return false;
        }
        filled++;
    }
    cJSON_Delete(root);

    *out_games = games;
    *out_count = filled;
    return true;
}

static bool raop_start(const char *json_body, char *message, size_t message_size) {
    http_local_response response;
    if (!http_local_request(RAOP_PORT, "POST", "/leaf/precache/start", json_body,
                            &response)) {
        snprintf(message, message_size,
                 "Could not reach the RAOfflineProxy service.");
        return false;
    }
    bool ok = response.status == 200;
    if (!ok) {
        raop_copy_error(response.body, message, message_size);
    }
    http_local_response_free(&response);
    return ok;
}

static void raop_cancel(void) {
    http_local_response response;
    if (http_local_request(RAOP_PORT, "POST", "/leaf/precache/cancel", "{}",
                           &response)) {
        http_local_response_free(&response);
    }
}

typedef struct {
    char state[32];
    char current[128];
    int total;
    int processed;
    int cached;
    int skipped;
    int unsupported;
    int failed;
} raop_status;

static bool raop_fetch_status(raop_status *status) {
    memset(status, 0, sizeof(*status));
    snprintf(status->state, sizeof(status->state), "unknown");

    http_local_response response;
    if (!http_local_request(RAOP_PORT, "GET", "/leaf/precache/status", NULL,
                            &response)) {
        return false;
    }
    if (response.status != 200 || !response.body) {
        http_local_response_free(&response);
        return false;
    }
    cJSON *root = cJSON_Parse(response.body);
    http_local_response_free(&response);
    if (!root) {
        return false;
    }

    const cJSON *field = cJSON_GetObjectItemCaseSensitive(root, "state");
    if (cJSON_IsString(field) && field->valuestring) {
        snprintf(status->state, sizeof(status->state), "%s", field->valuestring);
    }
    field = cJSON_GetObjectItemCaseSensitive(root, "current");
    if (cJSON_IsString(field) && field->valuestring) {
        snprintf(status->current, sizeof(status->current), "%s", field->valuestring);
    }
    struct {
        const char *key;
        int *slot;
    } numbers[] = {
        { "total", &status->total },
        { "processed", &status->processed },
        { "cached", &status->cached },
        { "skipped", &status->skipped },
        { "unsupported", &status->unsupported },
        { "failed", &status->failed },
    };
    for (size_t i = 0; i < sizeof(numbers) / sizeof(numbers[0]); i++) {
        field = cJSON_GetObjectItemCaseSensitive(root, numbers[i].key);
        if (cJSON_IsNumber(field)) {
            *numbers[i].slot = field->valueint;
        }
    }
    cJSON_Delete(root);
    return true;
}

/* -- screens ------------------------------------------------------------- */

static void raop_message(const char *text) {
    cat_footer_item footer[] = {
        { .button = CAT_BTN_A, .label = "OK", .is_confirm = true },
    };
    cat_message_opts options = {
        .message = text,
        .footer = footer,
        .footer_count = 1,
    };
    cat_confirm_result result;
    (void)cat_confirmation(&options, &result);
}

/* Live progress, on a hand-rolled loop rather than cat_options_list.
 *
 * History worth keeping, because the symptom was invisible from reading: this
 * screen originally used the widget's refresh_interval_ms and sat frozen while
 * a run advanced underneath it, updating only when a button was pressed. The
 * widget selected CAT_ACTION_REFRESH on time, but its final cat_present() had
 * no remaining redraw deadline and took the idle path, sleeping to the next
 * wall-clock minute before returning -- measured at 11.7 s and 40 s.
 *
 * Catastrophe fixed that upstream (PR #9: request an active frame when the
 * deadline fires), so refresh_interval_ms is now sound. This screen stays
 * hand-rolled anyway, for two reasons that outlive the bug:
 *
 *   - It is a read-only status display, not a menu. cat_options_list draws a
 *     selection pill on the focused row, which advertises an interaction that
 *     does not exist here.
 *   - Liveness then depends only on this loop, not on which Catastrophe the
 *     pak happened to be built against. The failure mode of getting that wrong
 *     is a silently stale screen, not a build error.
 *
 * Re-arming cat_request_frame_in every frame before cat_present is the same
 * shape the Syncthing pak's live screens use.
 *
 * The run belongs to the service, so leaving this screen only stops watching.
 * "Stop" is separate and explicit, because backing out of a screen is not a
 * request to discard the API calls already spent.
 */
static void raop_screen_progress(void) {
    static const char *labels[7] = {
        "State", "Progress", "Current", "Cached", "Already cached",
        "No RA data", "Failed",
    };
    char values[7][160];

    cat_footer_item footer[] = {
        { .button = CAT_BTN_B, .label = "Back" },
        { .button = CAT_BTN_X, .label = "Stop" },
    };

    uint32_t next_poll = 0;
    raop_status status;
    bool have_status = false;

    for (;;) {
        uint32_t now = SDL_GetTicks();
        if (now >= next_poll) {
            have_status = raop_fetch_status(&status);
            next_poll = now + 1000u;
        }

        cat_input_event event;
        while (cat_poll_input(&event)) {
            if (!event.pressed) continue;
            if (event.button == CAT_BTN_B) return;
            if (event.button == CAT_BTN_X) {
                raop_cancel();
                next_poll = 0; /* reflect the cancel immediately */
            }
        }

        if (!have_status) {
            snprintf(values[0], sizeof(values[0]), "service not reachable");
            for (int i = 1; i < 7; i++) values[i][0] = '\0';
        } else {
            snprintf(values[0], sizeof(values[0]), "%s", status.state);
            snprintf(values[1], sizeof(values[1]), "%d / %d", status.processed,
                     status.total);
            snprintf(values[2], sizeof(values[2]), "%s",
                     status.current[0] ? status.current : "-");
            snprintf(values[3], sizeof(values[3]), "%d", status.cached);
            snprintf(values[4], sizeof(values[4]), "%d", status.skipped);
            snprintf(values[5], sizeof(values[5]), "%d", status.unsupported);
            snprintf(values[6], sizeof(values[6]), "%d", status.failed);
        }

        cat_draw_background();
        cat_draw_screen_title("Preparing for offline", NULL);
        SDL_Rect content = cat_get_content_rect(true, true, false);
        TTF_Font *label_font = cat_get_font(CAT_FONT_LARGE);
        TTF_Font *value_font = cat_get_font(CAT_FONT_SMALL);
        cat_theme *theme = cat_get_theme();
        int row_h = CAT_DS(34);
        int y = content.y;
        for (int i = 0; i < 7; i++) {
            cat_draw_text(label_font, labels[i], content.x, y, theme->text);
            int value_w = cat_measure_text(value_font, values[i]);
            cat_draw_text(value_font, values[i],
                          content.x + content.w - value_w, y + CAT_DS(4),
                          theme->hint);
            y += row_h;
        }
        if (cat_hints_enabled_from_env()) cat_draw_footer(footer, 2);

        /* Re-arm every frame: a single arming at entry is consumed by the
         * first present, after which the idle sleep runs to the next minute. */
        cat_request_frame_in(1000);
        cat_present();
    }
}

/* Fetch the system list (system, count) for the picker's first screen. */
static bool raop_fetch_systems(char names[][32], int counts[], int max,
                               int *out_count, char *error, size_t error_size) {
    *out_count = 0;
    http_local_response response;
    if (!http_local_request(RAOP_PORT, "GET", "/leaf/precache/games?scope=systems",
                            NULL, &response)) {
        snprintf(error, error_size,
                 "Could not reach the RAOfflineProxy service.\n\n"
                 "Enable it under Settings > Services, then try again.");
        return false;
    }
    if (response.status != 200) {
        raop_copy_error(response.body, error, error_size);
        http_local_response_free(&response);
        return false;
    }
    cJSON *root = cJSON_Parse(response.body);
    http_local_response_free(&response);
    if (!root) {
        snprintf(error, error_size, "The service sent a malformed system list.");
        return false;
    }
    const cJSON *array = cJSON_GetObjectItemCaseSensitive(root, "systems");
    int n = cJSON_IsArray(array) ? cJSON_GetArraySize(array) : 0;
    int filled = 0;
    for (int i = 0; i < n && filled < max; i++) {
        const cJSON *entry = cJSON_GetArrayItem(array, i);
        const cJSON *name = cJSON_GetObjectItemCaseSensitive(entry, "system");
        const cJSON *count = cJSON_GetObjectItemCaseSensitive(entry, "count");
        if (!cJSON_IsString(name)) continue;
        snprintf(names[filled], 32, "%s", name->valuestring);
        counts[filled] = cJSON_IsNumber(count) ? count->valueint : 0;
        filled++;
    }
    cJSON_Delete(root);
    *out_count = filled;
    return true;
}

/* Show a game list already narrowed by system or search, and prepare the
 * chosen title. Narrowing happens service-side so the list that arrives is the
 * list being shown: scrolling all 1,968 rows to reach one game is not a usable
 * way to choose, and the largest single system here holds 375. */
static void raop_screen_game_list(const char *title, const char *system,
                                  const char *search) {
    char path[256];
    char query[128];
    /* Percent-encode the few characters a title search can plausibly contain
     * that would otherwise terminate or split the query string. */
    size_t qi = 0;
    for (const char *c = search ? search : ""; *c && qi + 4 < sizeof(query); c++) {
        if ((*c >= 'a' && *c <= 'z') || (*c >= 'A' && *c <= 'Z') ||
            (*c >= '0' && *c <= '9') || *c == '-' || *c == '.' || *c == '_') {
            query[qi++] = *c;
        } else {
            qi += (size_t)snprintf(query + qi, sizeof(query) - qi, "%%%02X",
                                   (unsigned char)*c);
        }
    }
    query[qi] = '\0';
    snprintf(path, sizeof(path), "/leaf/precache/games?scope=all&system=%s&q=%s",
             system ? system : "", query);

    char error[256];
    raop_game *games = NULL;
    int count = 0;
    {
        http_local_response response;
        if (!http_local_request(RAOP_PORT, "GET", path, NULL, &response)) {
            raop_message("Could not reach the RAOfflineProxy service.");
            return;
        }
        if (response.status != 200) {
            raop_copy_error(response.body, error, sizeof(error));
            http_local_response_free(&response);
            raop_message(error);
            return;
        }
        cJSON *root = cJSON_Parse(response.body);
        http_local_response_free(&response);
        if (!root) {
            raop_message("The service sent a malformed game list.");
            return;
        }
        const cJSON *array = cJSON_GetObjectItemCaseSensitive(root, "games");
        int n = cJSON_IsArray(array) ? cJSON_GetArraySize(array) : 0;
        if (n > RAOP_MAX_GAMES) n = RAOP_MAX_GAMES;
        if (n > 0) {
            games = calloc((size_t)n, sizeof(*games));
            if (!games) {
                cJSON_Delete(root);
                raop_message("Out of memory building the game list.");
                return;
            }
            for (int i = 0; i < n; i++) {
                const cJSON *entry = cJSON_GetArrayItem(array, i);
                const cJSON *id = cJSON_GetObjectItemCaseSensitive(entry, "id");
                const cJSON *name = cJSON_GetObjectItemCaseSensitive(entry, "name");
                const cJSON *sys = cJSON_GetObjectItemCaseSensitive(entry, "system");
                if (!cJSON_IsNumber(id) || !cJSON_IsString(name)) continue;
                games[count].id = id->valueint;
                games[count].name = raop_strdup(name->valuestring);
                games[count].system =
                    raop_strdup(cJSON_IsString(sys) ? sys->valuestring : "");
                count++;
            }
        }
        cJSON_Delete(root);
    }

    if (count == 0) {
        raop_games_free(games, count);
        raop_message("No matching games.");
        return;
    }

    cat_list_item *items = calloc((size_t)count, sizeof(*items));
    if (!items) {
        raop_games_free(games, count);
        raop_message("Out of memory building the game list.");
        return;
    }
    for (int i = 0; i < count; i++) {
        items[i].label = games[i].name;
        items[i].trailing_text = games[i].system;
    }

    cat_footer_item footer[] = {
        { .button = CAT_BTN_B, .label = "Back" },
        { .button = CAT_BTN_A, .label = "Prepare", .is_confirm = true },
    };

    int cursor = 0;
    int scroll = 0;
    for (;;) {
        cat_list_opts opts = cat_list_default_opts(title, items, count);
        opts.footer = footer;
        opts.footer_count = 2;
        opts.initial_index = cursor;
        opts.visible_start_index = scroll;
        cat_list_result result;
        int rc = cat_list(&opts, &result);
        cursor = result.selected_index >= 0 ? result.selected_index : cursor;
        scroll = result.visible_start_index;

        if (rc == CAT_CANCELLED || result.action == CAT_ACTION_BACK) break;
        if (result.action != CAT_ACTION_SELECTED || cursor < 0 || cursor >= count) {
            continue;
        }

        char body[64];
        snprintf(body, sizeof(body), "{\"scope\":\"games\",\"game_ids\":[%d]}",
                 games[cursor].id);
        char message[256];
        if (!raop_start(body, message, sizeof(message))) {
            raop_message(message);
            continue;
        }
        raop_screen_progress();
    }

    free(items);
    raop_games_free(games, count);
}

/* Console first, then titles -- the same shape Leaf's scraper uses, and the
 * only way a 1,968-game library is navigable with a d-pad. Search is the
 * escape hatch for "I know the name". */
static void raop_screen_pick_game(void) {
    enum { MAX_SYSTEMS = 48 };
    char names[MAX_SYSTEMS][32];
    int counts[MAX_SYSTEMS];
    int system_count = 0;
    char error[256];

    if (!raop_fetch_systems(names, counts, MAX_SYSTEMS, &system_count, error,
                            sizeof(error))) {
        raop_message(error);
        return;
    }
    if (system_count == 0) {
        raop_message("No games in the library yet.\n\n"
                     "Let Leaf finish scanning, then try again.");
        return;
    }

    char totals[MAX_SYSTEMS][16];
    cat_list_item items[MAX_SYSTEMS];
    for (int i = 0; i < system_count; i++) {
        snprintf(totals[i], sizeof(totals[i]), "%d", counts[i]);
        items[i].label = names[i];
        items[i].metadata = NULL;
        items[i].image = NULL;
        items[i].selected = false;
        items[i].background_image = NULL;
        items[i].trailing_text = totals[i];
        items[i].disabled = false;
    }

    cat_footer_item footer[] = {
        { .button = CAT_BTN_B, .label = "Back" },
        { .button = CAT_BTN_X, .label = "Search" },
        { .button = CAT_BTN_A, .label = "Open", .is_confirm = true },
    };

    int cursor = 0;
    int scroll = 0;
    for (;;) {
        cat_list_opts opts = cat_list_default_opts("Choose a console", items,
                                                   system_count);
        opts.footer = footer;
        opts.footer_count = 3;
        opts.initial_index = cursor;
        opts.visible_start_index = scroll;
        opts.action_button = CAT_BTN_X;
        cat_list_result result;
        int rc = cat_list(&opts, &result);
        cursor = result.selected_index >= 0 ? result.selected_index : cursor;
        scroll = result.visible_start_index;

        if (rc == CAT_CANCELLED || result.action == CAT_ACTION_BACK) break;

        if (result.action == CAT_ACTION_TRIGGERED) {
            cat_keyboard_result typed;
            if (cat_keyboard("", "Search game titles", CAT_KB_GENERAL, &typed) == CAT_OK
                && typed.text[0]) {
                char title[96];
                /* The keyboard returns up to 1 KB; a title is a heading,
                 * not a transcript. */
                snprintf(title, sizeof(title), "Search: %.80s", typed.text);
                raop_screen_game_list(title, "", typed.text);
            }
            continue;
        }
        if (result.action == CAT_ACTION_SELECTED && cursor >= 0 &&
            cursor < system_count) {
            raop_screen_game_list(names[cursor], names[cursor], "");
        }
    }
}

static void raop_screen_main(void) {
    for (;;) {
        char error[256];
        raop_game *recents = NULL;
        int recent_count = 0;
        bool have_recents =
            raop_fetch_games("recents", &recents, &recent_count, error, sizeof(error));

        char recents_value[64];
        if (have_recents) {
            snprintf(recents_value, sizeof(recents_value), "%d game%s", recent_count,
                     recent_count == 1 ? "" : "s");
        } else {
            snprintf(recents_value, sizeof(recents_value), "unavailable");
        }

        raop_status status;
        bool have_status = raop_fetch_status(&status);
        char status_value[96];
        if (!have_status) {
            snprintf(status_value, sizeof(status_value), "service not running");
        } else if (strcmp(status.state, "running") == 0) {
            snprintf(status_value, sizeof(status_value), "running %d / %d",
                     status.processed, status.total);
        } else {
            snprintf(status_value, sizeof(status_value), "%s", status.state);
        }

        cat_list_item items[] = {
            { .label = "Prepare Recents and Favourites",
              .trailing_text = recents_value },
            { .label = "Prepare one game", .trailing_text = "browse library" },
            { .label = "Run status", .trailing_text = status_value },
            { .label = "About this pak", .trailing_text = "" },
        };
        cat_footer_item footer[] = {
            { .button = CAT_BTN_B, .label = "Exit" },
            { .button = CAT_BTN_A, .label = "Select", .is_confirm = true },
        };

        cat_list_opts opts = cat_list_default_opts("RAOfflineProxy", items, 4);
        opts.footer = footer;
        opts.footer_count = 2;
        cat_list_result result;
        int rc = cat_list(&opts, &result);
        int choice = result.selected_index;

        if (rc == CAT_CANCELLED || result.action == CAT_ACTION_BACK) {
            raop_games_free(recents, recent_count);
            return;
        }
        if (result.action != CAT_ACTION_SELECTED) {
            raop_games_free(recents, recent_count);
            continue;
        }

        if (choice == 0) {
            if (!have_recents) {
                raop_message(error);
            } else if (recent_count == 0) {
                raop_message(
                    "Nothing in Recents or Favourites yet.\n\n"
                    "Play or favourite a few games first, or use Prepare one "
                    "game to choose a title directly.");
            } else {
                char message[256];
                if (!raop_start("{\"scope\":\"recents\"}", message, sizeof(message))) {
                    raop_message(message);
                } else {
                    raop_screen_progress();
                }
            }
        } else if (choice == 1) {
            raop_games_free(recents, recent_count);
            recents = NULL;
            recent_count = 0;
            raop_screen_pick_game();
        } else if (choice == 2) {
            if (!have_status) {
                raop_message(
                    "The RAOfflineProxy service is not running.\n\n"
                    "Enable it under Settings > Services. Pre-caching needs the "
                    "service, because the service owns the cache.");
            } else {
                raop_screen_progress();
            }
        } else {
            raop_message(
                "Experimental, casual-only offline RetroAchievements.\n\n"
                "Sign in under Leaf Settings > Accounts, launch one game online "
                "so the pak learns your token, then enable RAOfflineProxy in "
                "Settings > Services.\n\n"
                "Preparing a game downloads its achievement data now so it can "
                "be played offline later. Hardcore play always launches "
                "directly, unproxied.");
        }

        raop_games_free(recents, recent_count);
    }
}
#endif /* !RAOFFLINEPROXY_FLOOR */

int main(int argc, char **argv) {
    (void)argc;
    (void)argv;

    cat_config config = {
        .window_title = "RAOfflineProxy",
        .log_path = cat_resolve_log_path("raofflineproxy-ui"),
    };
    if (cat_init(&config) != CAT_OK) {
        fprintf(stderr, "RAOfflineProxy: Catastrophe initialization failed\n");
        return 1;
    }

#ifdef RAOFFLINEPROXY_FLOOR
    cat_footer_item footer[] = {
        { .button = CAT_BTN_B, .label = "Exit" },
        { .button = CAT_BTN_A, .label = "OK", .is_confirm = true },
    };
    cat_message_opts options = {
        .message =
            "RAOfflineProxy needs a newer Leaf release.\n\n"
            "Update Leaf and Jawaka before trying this pak again.\n\n"
            "This compatibility screen performs no network activity and writes "
            "no service state.",
        .image_path = NULL,
        .footer = footer,
        .footer_count = 2,
    };
    cat_confirm_result result;
    (void)cat_confirmation(&options, &result);
#else
    if (!raop_service_ready()) {
        raop_message(
            "RAOfflineProxy is installed but its service is not running.\n\n"
            "Enable it under Settings > Services to proxy achievements and to "
            "prepare games for offline play.");
    }
    raop_screen_main();
#endif

    cat_quit();
    return 0;
}
