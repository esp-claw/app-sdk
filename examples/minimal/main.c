/*
 * SPDX-FileCopyrightText: 2026 esp-claw contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */
/*
 * minimal: smallest useful app, built and checked by the SDK's CI.
 *   minimal_ping  returns "pong"
 */
#define CLAW_ARENA_SIZE 256
#include "claw.h"

CLAW_DEFINE_ARENA()

static int32_t s_ping = -1;

CLAW_EXPORT("app_init") void app_init(void)
{
    s_ping = CLAW_REGISTER("ping", "Returns \"pong\" (SDK example app).", "{\"type\":\"object\",\"properties\":{}}");
}

CLAW_EXPORT("tool_call") int32_t tool_call(int32_t tool, const char *args, uint32_t len)
{
    (void)args;
    (void)len;
    if (tool != s_ping) {
        return -1;
    }
    CLAW_RESULT("pong");
    return 0;
}
