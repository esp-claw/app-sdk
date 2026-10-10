/* SPDX-License-Identifier: Apache-2.0 */
#include <assert.h>
#include "claw_util.h"
int main(void) {
 const char t[]="{\"x\":"; const char e[]="{}";
 const char *end=t+sizeof(t)-1, *ee=e+sizeof(e)-1;
 assert(!cj_ws(0,end)); assert(!cj_skip(0,end));
 assert(!cj_member(0,end,"x")); assert(!cj_first(0,end));
 assert(!cj_next(0,end)); assert(!cj_member(t,end,"missing"));
 assert(!cj_member(cj_member(e,ee,"web"),ee,"results")); return 0;
}
