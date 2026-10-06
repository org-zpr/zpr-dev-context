#!/usr/bin/env python3
"""List a user's org-zpr "ref impl" (project #1) tasks in the CURRENT iteration.

Current iteration = the iteration in the Iteration field configuration whose
[startDate, startDate+duration) window contains today.

The list holds every current-iteration item assigned to the user, plus every
UNASSIGNED current-iteration issue that is not yet started (Status "Backlog",
"Ready" or no Status), since the user may claim those. An item assigned to someone else is
never listed. Each item is put in one category:

  pickable        open issue, Status "Ready", unassigned or assigned to the user.
                  Status "Ready" is the team's green-light: only these may be
                  started (see "Picking up a task" in ../SKILL.md).
  awaiting-ready  open issue in "Backlog" or with no Status. Filed into the
                  iteration but not green-lit yet; listed so a forgotten one is
                  noticed.
  mine            anything else assigned to the user (in progress, in review,
                  done, pull requests).

Pickable items sort first: Priority (P0 before P1 before P2 before none), then
issues explicitly assigned to the user before unassigned ones, then repo and
number.

Usage:
  python3 my-current-tasks.py            # human-readable, assignee = authenticated gh user
  python3 my-current-tasks.py --json     # machine-readable (for automation/diffing)
  python3 my-current-tasks.py --user X   # different assignee login
  python3 my-current-tasks.py --retry-marker
        Append a volatile "PENDING-TODO ... tick=<epoch>" line IFF at least one
        item is pickable. Intended for output-hash-based monitors that
        suppress a run when the output is unchanged: without the marker, a run
        that fails after the hash was recorded would leave the pickable item
        unstarted and never retried. With it, output keeps changing every tick
        while a pickable item is outstanding. Idle output stays byte-stable.
        Off by default so interactive runs stay clean.

Requires: gh authenticated with scopes read:org, read:project, repo.
"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ghretry import gh_login, run_gh  # noqa: E402

ORG = "org-zpr"
PROJECT_NUMBER = 1

QUERY = """
query($org:String!, $num:Int!, $cursor:String) {
  organization(login:$org) {
    projectV2(number:$num) {
      title
      items(first:100, after:$cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          fieldValues(first:20) {
            nodes {
              __typename
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2SingleSelectField { name } }
              }
              ... on ProjectV2ItemFieldIterationValue {
                title startDate duration
                field { ... on ProjectV2IterationField { name } }
              }
            }
          }
          content {
            __typename
            ... on Issue {
              number title url state repository { name }
              assignees(first:10) { nodes { login } }
            }
            ... on PullRequest {
              number title url state repository { name }
              assignees(first:10) { nodes { login } }
            }
            ... on DraftIssue { title }
          }
        }
      }
    }
  }
}
"""

ITER_CFG_QUERY = """
query($org:String!, $num:Int!) {
  organization(login:$org) {
    projectV2(number:$num) {
      field(name:"Iteration") {
        ... on ProjectV2IterationField {
          configuration { iterations { title startDate duration } }
        }
      }
    }
  }
}
"""


def gh_graphql(query, **variables):
    """GraphQL call with bounded retries on transient network faults."""
    cmd = ["api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        if v is None:
            continue
        flag = "-F" if isinstance(v, int) else "-f"
        cmd += [flag, f"{k}={v}"]
    return json.loads(run_gh(cmd))


def current_iteration_title(today=None):
    today = today or datetime.date.today()
    d = gh_graphql(ITER_CFG_QUERY, org=ORG, num=PROJECT_NUMBER)
    cfg = d["data"]["organization"]["projectV2"]["field"]["configuration"]
    for it in cfg["iterations"]:
        start = datetime.date.fromisoformat(it["startDate"])
        end = start + datetime.timedelta(days=it["duration"])
        if start <= today < end:
            return it["title"], str(start), str(end - datetime.timedelta(days=1))
    return None, None, None


def fetch_items():
    cursor, items = None, []
    while True:
        d = gh_graphql(QUERY, org=ORG, num=PROJECT_NUMBER, cursor=cursor)
        page = d["data"]["organization"]["projectV2"]["items"]
        items.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            return items
        cursor = page["pageInfo"]["endCursor"]


def field_values(node):
    """Return the (Status, Iteration, Priority) values of a project item; None if unset."""
    status, iteration, priority = None, None, None
    for fv in node.get("fieldValues", {}).get("nodes", []):
        fname = (fv.get("field") or {}).get("name")
        if fv.get("__typename") == "ProjectV2ItemFieldSingleSelectValue" and fname == "Status":
            status = fv.get("name")
        elif fv.get("__typename") == "ProjectV2ItemFieldSingleSelectValue" and fname == "Priority":
            priority = fv.get("name")
        elif fv.get("__typename") == "ProjectV2ItemFieldIterationValue" and fname == "Iteration":
            iteration = fv.get("title")
    return status, iteration, priority


# Not-started Status values. Only the exact string "Ready" is the green-light;
# renaming that project column silently stops all pickup. No Status counts as
# Backlog.
BACKLOG = "Backlog"
READY = "Ready"
NOT_STARTED = (None, BACKLOG, READY)
CATEGORY_ORDER = {"pickable": 0, "awaiting-ready": 1, "mine": 2}


def categorize(item):
    """Return the category of a selected item (see the module docstring)."""
    is_open_issue = item["type"] == "Issue" and item["state"] == "OPEN"
    if is_open_issue and item["status"] == READY:
        return "pickable"
    if is_open_issue and item["status"] in (None, BACKLOG):
        return "awaiting-ready"
    return "mine"


def sort_key(item):
    """Category, then Priority (unset last), then assigned-to-user first, then repo/number."""
    return (
        CATEGORY_ORDER[item["category"]],
        item["priority"] or "P~",  # "P~" sorts after every "P<digit>"
        not item["assigned_to_user"],
        item["repo"] or "",
        item["number"] or 0,
    )


def select(nodes, user, iteration):
    """Pick the current-iteration items listed for `user` from raw project items.

    Keeps items assigned to `user`, and unassigned issues that are not yet
    started. Items assigned to anyone else are dropped: they are someone else's
    work even when green-lit.
    """
    matches = []
    for node in nodes:
        content = node.get("content") or {}
        assignees = [a["login"] for a in (content.get("assignees") or {}).get("nodes", [])]
        status, item_iteration, priority = field_values(node)
        if item_iteration != iteration:
            continue
        unassigned_unstarted = (
            not assignees
            and content.get("__typename") == "Issue"
            and content.get("state") == "OPEN"
            and status in NOT_STARTED
        )
        if user not in assignees and not unassigned_unstarted:
            continue
        item = {
            "number": content.get("number"),
            "title": content.get("title"),
            "url": content.get("url"),
            "state": content.get("state"),
            "repo": (content.get("repository") or {}).get("name"),
            "status": status,
            "priority": priority,
            "iteration": item_iteration,
            "type": content.get("__typename"),
            "assigned_to_user": user in assignees,
        }
        item["category"] = categorize(item)
        matches.append(item)
    matches.sort(key=sort_key)
    return matches


def retry_marker(matches):
    """Volatile line emitted only while at least one item is pickable.

    Forces an output-hash monitor to differ on every tick so a tick that
    failed to start the work is retried on the next one. Disappears (restoring
    a stable hash) as soon as nothing is pickable.
    """
    pickable = [m for m in matches if m["category"] == "pickable"]
    if not pickable:
        return None
    ids = ",".join(f"{m['repo']}#{m['number']}" for m in pickable)
    tick = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    return (
        f"PENDING-TODO {len(pickable)} [{ids}] tick={tick}"
        "  (volatile line: forces re-check until these leave Ready; not a change signal)"
    )


def main():
    as_json = "--json" in sys.argv
    want_marker = "--retry-marker" in sys.argv
    if "--user" in sys.argv:
        user = sys.argv[sys.argv.index("--user") + 1]
    else:
        user = gh_login()

    cur, start, end = current_iteration_title()
    if cur is None:
        print("No current iteration matches today's date.", file=sys.stderr)
        sys.exit(2)

    matches = select(fetch_items(), user, cur)

    if as_json:
        payload = {
            "iteration": cur, "start": start, "end": end,
            "user": user, "count": len(matches), "items": matches,
        }
        if want_marker:
            payload["retry_marker"] = retry_marker(matches)
        print(json.dumps(payload, indent=2))
        return

    print(f"{cur} ({start} -> {end})  user={user}  items={len(matches)}")
    if not matches:
        print("  (nothing assigned or pickable)")
    for m in matches:
        owner = "assigned" if m["assigned_to_user"] else "unassigned"
        print(f"  {m['category']:<13} [{m['status'] or 'no status'}] [{m['priority'] or '-'}] "
              f"({owner}) {m['repo']}#{m['number']} {m['title']}")
        print(f"      {m['url']}")
    if want_marker:
        marker = retry_marker(matches)
        if marker:
            print(marker)


if __name__ == "__main__":
    main()
