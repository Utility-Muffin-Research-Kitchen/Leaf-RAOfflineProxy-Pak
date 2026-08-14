#define CAT_IMPLEMENTATION
#include "catastrophe.h"
#define CAT_WIDGETS_IMPLEMENTATION
#include "catastrophe_widgets.h"

#include <stdio.h>

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
    const char *message =
        "RAOfflineProxy needs a newer Leaf release.\n\n"
        "Update Leaf and Jawaka before trying this pak again.\n\n"
        "This compatibility screen performs no network activity and writes "
        "no service state.";
#else
    const char *message =
        "Experimental, casual-only offline RetroAchievements.\n\n"
        "Sign in under Leaf Settings > Accounts, launch each game online once, "
        "then enable RAOfflineProxy in Settings > Services.\n\n"
        "The service stays disabled until you enable Start with Leaf. Cache and "
        "pending casual awards are retained under RAOfflineProxy userdata. "
        "Hardcore play always launches directly.";
#endif

    cat_footer_item footer[] = {
        { .button = CAT_BTN_B, .label = "Exit" },
        { .button = CAT_BTN_A, .label = "OK", .is_confirm = true },
    };
    cat_message_opts options = {
        .message = message,
        .image_path = NULL,
        .footer = footer,
        .footer_count = 2,
    };
    cat_confirm_result result;
    (void)cat_confirmation(&options, &result);
    cat_quit();
    return 0;
}
