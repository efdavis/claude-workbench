#!/usr/bin/env python3
"""Claim (or roll back) a Jira issue for dispatch.

A detached dispatch lane runs headless, so the claim runs against the Jira REST API
using token auth (JIRA_BASE_URL + JIRA_EMAIL + JIRA_API_TOKEN). Self-contained: no
shared board helper. _call() below tolerates the 204 No Content returned by the
assignee PUT and the transition POST (an unconditional json.load would crash on it).

Claim is optimistic: Jira has no atomic compare-and-set on assignee, so we capture the
prior state, assign + transition, then re-read to confirm we still own it. A lost race
leaves the winner's assignee untouched. Crucially, the mutation is bracketed: a failure
AFTER the assignee write is committed reports OUTCOME=claim-uncertain with a distinct
exit code (8) so the caller knows the issue may be mutated and MUST roll it back - it is
not a clean no-op. Rollback RESTORES the captured prior assignee + status (it never
blind-unassigns), and if the status-restore transition is unavailable it says so loudly
(exit 7) rather than leaving the issue inconsistent.

Auth (required, no defaults):
    JIRA_BASE_URL   your Jira site, e.g. https://you.atlassian.net
    JIRA_EMAIL      the Atlassian account email the token belongs to
    JIRA_API_TOKEN  a personal API token, mint at
                    https://id.atlassian.com/manage-profile/security/api-tokens

Usage:
    python3 jira_claim.py --ticket PROJ-75 --action claim
    python3 jira_claim.py --ticket PROJ-75 --action rollback \
        --prior-assignee <accountId|NONE> --prior-status "To Do"

On --action claim, stdout carries machine-readable lines the shell parses:
    OUTCOME=<claimed|already-mine|assign-only|refused-other|lost-race|no-transition|claim-uncertain|error>
    PRIOR_ASSIGNEE=<accountId|NONE>
    PRIOR_STATUS=<status name>

Exit codes:
    0  owned (claimed / already-mine / assign-only) or rollback fully restored
    2  usage / environment error (missing auth env, bad issue key, bad args)
    3  refused - issue assigned to someone else
    4  no matching "In Progress" transition (fail closed, nothing mutated)
    5  lost race - another dispatch won the assignee between assign and confirm
    6  Jira HTTP / transport error BEFORE any mutation (nothing to roll back)
    7  rollback could only partially restore (assignee restored, status could not be)
    8  claim-uncertain - the assignee write committed but a later step failed; the
       issue MAY be mutated and MUST be rolled back (the caller drives that)
"""

import argparse
import base64
import os
import re
import signal
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request

# Self-contained token auth (no shared board helper).
SITE = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
TIMEOUT = int(os.environ.get("JIRA_TIMEOUT", "30"))

TARGET_STATUS = "In Progress"
NONE_SENTINEL = "NONE"  # CLI/stdout marker for "no assignee"
TICKET_RE = re.compile(r"^[A-Z][A-Z0-9]+-[0-9]+$")

# exit codes
EX_OK = 0
EX_USAGE = 2
EX_REFUSED_OTHER = 3
EX_NO_TRANSITION = 4
EX_LOST_RACE = 5
EX_HTTP = 6
EX_PARTIAL_ROLLBACK = 7
EX_MUTATED = 8


def auth_header() -> str:
    email = os.environ.get("JIRA_EMAIL", "")
    token = os.environ.get("JIRA_API_TOKEN", "")
    raw = f"{email}:{token}".encode()
    return "Basic " + base64.b64encode(raw).decode()


class ClaimError(Exception):
    """A fail-closed error carrying the exit code the CLI should return."""

    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


class _Interrupted(Exception):
    """Raised by the SIGINT/SIGTERM handler so a signal in the mutation window is
    caught by the same `except Exception` that turns any post-assign failure into
    OUTCOME=claim-uncertain (exit 8) - never a silent orphaned claim."""


# Set True immediately before the assignee write, so an interrupt/exception can tell
# a pre-mutation abort (no rollback needed) from a post-mutation one (rollback needed).
_MUTATION_STARTED = False


def _install_signal_handlers() -> None:
    def _raise(signum, _frame):
        raise _Interrupted(f"signal {signum}")

    signal.signal(signal.SIGINT, _raise)
    signal.signal(signal.SIGTERM, _raise)


def _parse_body(raw: bytes):
    """204-tolerant JSON parse: empty body -> None, else json.loads.

    Factored out as a pure function so it is unit-testable without a network call
    (this is the exact seam that crashes when a helper does an unconditional
    json.load on a 204 No Content).
    """
    import json

    if not raw:
        return None
    return json.loads(raw)


def _call(method: str, path: str, body: dict | None = None, params: dict | None = None):
    """One authenticated Jira REST call, tolerant of an empty (204) response body."""
    import json

    url = f"{SITE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", auth_header())
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return _parse_body(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise ClaimError(
                "Jira rejected credentials (401): check JIRA_API_TOKEN / JIRA_EMAIL.", EX_USAGE
            )
        if e.code == 403:
            raise ClaimError(f"Jira forbade {method} {path} (403): insufficient permission.", EX_HTTP)
        if e.code == 404:
            raise ClaimError(f"Jira {method} {path} not found (404): check the issue key.", EX_HTTP)
        raise ClaimError(f"Jira {method} {path} failed ({e.code}).", EX_HTTP)
    except urllib.error.URLError as e:
        raise ClaimError(f"Jira {method} {path} transport error: {e.reason}.", EX_HTTP)


# --- pure decision function (unit-testable, no network) ---------------------

def decide(current_assignee: str | None, current_status: str, caller: str) -> str:
    """Given the issue's current assignee/status and the caller, pick the action.

    Returns one of: already-mine, refused-other, assign-only, claim.
    (no-transition and lost-race are runtime outcomes, not decidable up front.)
    """
    if current_assignee == caller and current_status == TARGET_STATUS:
        return "already-mine"
    if current_assignee is not None and current_assignee != caller:
        return "refused-other"
    if current_status == TARGET_STATUS:
        # unassigned (or ours-without-status) but already In Progress:
        # assign only, do not require an In-Progress -> In-Progress self-loop.
        return "assign-only"
    return "claim"


# --- Jira operations --------------------------------------------------------

def _myself() -> str:
    return _call("GET", "/rest/api/3/myself")["accountId"]


def _get_assignee_status(key: str) -> tuple[str | None, str]:
    issue = _call("GET", f"/rest/api/3/issue/{key}", params={"fields": "assignee,status"})
    f = issue["fields"]
    return (f.get("assignee") or {}).get("accountId"), f["status"]["name"]


def _find_transition(key: str, status_name: str) -> tuple[str | None, list[str]]:
    """Look up transitions once; return (matching id or None, all available names)."""
    data = _call("GET", f"/rest/api/3/issue/{key}/transitions")
    tid, names = None, []
    for t in data.get("transitions", []):
        names.append(t.get("name", "?"))
        if tid is None and (t.get("to", {}).get("name") == status_name or t.get("name") == status_name):
            tid = t["id"]
    return tid, names


def _assign(key: str, account_id: str | None) -> None:
    _call("PUT", f"/rest/api/3/issue/{key}/assignee", body={"accountId": account_id})  # null unassigns


def _transition(key: str, transition_id: str) -> None:
    _call("POST", f"/rest/api/3/issue/{key}/transitions", body={"transition": {"id": transition_id}})


# --- actions ----------------------------------------------------------------

def do_claim(key: str) -> int:
    global _MUTATION_STARTED
    caller = _myself()
    prior_assignee, prior_status = _get_assignee_status(key)
    # Emit the captured prior state BEFORE any mutation and FLUSH, so the shell has
    # it even if a signal kills this process mid-mutation.
    print(f"PRIOR_ASSIGNEE={prior_assignee or NONE_SENTINEL}", flush=True)
    print(f"PRIOR_STATUS={prior_status}", flush=True)

    action = decide(prior_assignee, prior_status, caller)

    if action == "already-mine":
        print("OUTCOME=already-mine")
        print(f"jira-claim: {key} already assigned to you and {TARGET_STATUS}; nothing to do.")
        return EX_OK
    if action == "refused-other":
        print("OUTCOME=refused-other")
        print(f"jira-claim: {key} is assigned to another user ({prior_assignee}); refusing to claim.")
        return EX_REFUSED_OTHER

    # Confirm the target transition EXISTS before touching the assignee, so a
    # no-transition refusal happens with zero mutation (nothing to roll back).
    if action == "claim":
        tid, names = _find_transition(key, TARGET_STATUS)
        if tid is None:
            print("OUTCOME=no-transition")
            avail = ", ".join(names) or "(none)"
            print(f"jira-claim: no '{TARGET_STATUS}' transition for {key}; available: {avail}. Refusing.")
            return EX_NO_TRANSITION
    else:  # assign-only: already in target status, no transition needed
        tid = None

    # --- mutation boundary ---------------------------------------------------
    # Everything from the assignee write onward is treated as "the issue MAY be
    # mutated". Any failure OR an interrupt (the signal handler raises _Interrupted,
    # an Exception) here returns EX_MUTATED=8 so the caller rolls back - never a
    # silent orphaned claim. Pre-assign GET failures above stay uncaught and reach
    # main() -> EX_HTTP=6, a genuine no-mutation path with nothing to roll back.
    try:
        _MUTATION_STARTED = True
        _assign(key, caller)
        if tid is not None:
            _transition(key, tid)
        confirm_assignee, _ = _get_assignee_status(key)
    except Exception as e:
        # A real programming bug (KeyError, JSONDecodeError, ...) is deliberately treated
        # as "may be mutated -> roll back" (fail-closed); dump its trace to stderr so the
        # safety behavior doesn't also swallow the diagnostic. A signal interrupt is
        # expected, not a bug, so skip the trace there.
        if not isinstance(e, _Interrupted):
            traceback.print_exc(file=sys.stderr)
        print("OUTCOME=claim-uncertain")
        print(f"jira-claim: {key} claim did not complete cleanly after the assignee write began "
              f"({e}); the issue may be mutated and MUST be rolled back.", flush=True)
        return EX_MUTATED

    if confirm_assignee != caller:
        print("OUTCOME=lost-race")
        print(f"jira-claim: lost the claim race on {key} (now {confirm_assignee}); "
              f"leaving the winner's assignee untouched.")
        return EX_LOST_RACE

    print("OUTCOME=assign-only" if action == "assign-only" else "OUTCOME=claimed")
    print(f"jira-claim: claimed {key} (assignee=you, status={TARGET_STATUS}).")
    return EX_OK


def do_rollback(key: str, prior_assignee: str, prior_status: str) -> int:
    """Restore the captured prior assignee + status. Loud partial-restore (exit 7)
    if the status-restore transition is unavailable."""
    target_assignee = None if prior_assignee in ("", NONE_SENTINEL) else prior_assignee
    _assign(key, target_assignee)

    if not prior_status:
        # We were not told the prior status, so we cannot verify or restore it. Report a
        # partial restore (7) rather than falsely claim a full one - the same "unknown vs
        # nothing-to-do" distinction the dispatch shell's PRIOR_CAPTURED guard preserves.
        print(f"jira-claim: PARTIAL ROLLBACK of {key}: assignee restored, but no prior status "
              f"was provided, so status was left as-is. Check the status by hand.")
        return EX_PARTIAL_ROLLBACK

    _, current_status = _get_assignee_status(key)
    if current_status != prior_status:
        tid, names = _find_transition(key, prior_status)
        if tid is None:
            avail = ", ".join(names) or "(none)"
            print(
                f"jira-claim: PARTIAL ROLLBACK of {key}: assignee restored, but no transition "
                f"back to '{prior_status}' (available: {avail}); issue left in '{current_status}'. "
                f"Fix the status by hand."
            )
            return EX_PARTIAL_ROLLBACK
        _transition(key, tid)
    print(f"jira-claim: rolled back {key} (assignee + status restored to prior).")
    return EX_OK


def main() -> int:
    ap = argparse.ArgumentParser(description="Claim or roll back a Jira issue for dispatch.")
    ap.add_argument("--ticket", required=True)
    ap.add_argument("--action", required=True, choices=["claim", "rollback"])
    ap.add_argument("--prior-assignee", default=NONE_SENTINEL)
    ap.add_argument("--prior-status", default="")
    args = ap.parse_args()

    # Validate the issue key before it reaches any REST path (defense in depth:
    # this helper is independently invocable, not only via the shell's own check).
    if not TICKET_RE.match(args.ticket):
        print(f"jira-claim: invalid issue key '{args.ticket}' (expected e.g. PROJ-75).")
        return EX_USAGE
    # Fail closed on missing auth env with THIS script's exit code + message.
    if not SITE:
        print("jira-claim: JIRA_BASE_URL is not set (e.g. https://you.atlassian.net). Refusing.")
        return EX_USAGE
    if not os.environ.get("JIRA_EMAIL"):
        print("jira-claim: JIRA_EMAIL is not set (the account the API token belongs to). Refusing.")
        return EX_USAGE
    if not os.environ.get("JIRA_API_TOKEN"):
        print("jira-claim: JIRA_API_TOKEN is not set; mint one at "
              "https://id.atlassian.com/manage-profile/security/api-tokens. Refusing.")
        return EX_USAGE

    _install_signal_handlers()
    try:
        if args.action == "claim":
            return do_claim(args.ticket)
        return do_rollback(args.ticket, args.prior_assignee, args.prior_status)
    except ClaimError as e:
        print(f"jira-claim: {e}")
        return e.code
    except _Interrupted:
        # Belt-and-suspenders: do_claim's own boundary catches a post-assign interrupt.
        # If one still reaches here, honor the mutation flag rather than exit silently.
        if _MUTATION_STARTED:
            print("OUTCOME=claim-uncertain")
            print(f"jira-claim: {args.ticket} interrupted after the assignee write began; MUST roll back.")
            return EX_MUTATED
        print(f"jira-claim: {args.ticket} interrupted before any mutation; nothing to roll back.")
        return EX_HTTP


if __name__ == "__main__":
    sys.exit(main())
