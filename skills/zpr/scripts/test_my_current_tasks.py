"""Tests for the task selection in my-current-tasks.py (no network).

Run with `python3 test_my_current_tasks.py` or `pytest test_my_current_tasks.py`.
"""
import importlib.util
import os

_spec = importlib.util.spec_from_file_location(
    "my_current_tasks",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "my-current-tasks.py"),
)
mct = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mct)

ME = "ZprBot1"
CUR = "Iteration 124"


def node(number, status=None, iteration=CUR, assignees=(), priority=None,
         kind="Issue", state="OPEN", repo="zpr-core", sub_issues=0, blockers=(),
         parent=None):
    """Build a raw project item shaped like the GraphQL response.

    `blockers` is a list of (number, state); `parent` is the dict made by
    umbrella().
    """
    fvs = []
    if status is not None:
        fvs.append({"__typename": "ProjectV2ItemFieldSingleSelectValue",
                    "name": status, "field": {"name": "Status"}})
    if priority is not None:
        fvs.append({"__typename": "ProjectV2ItemFieldSingleSelectValue",
                    "name": priority, "field": {"name": "Priority"}})
    if iteration is not None:
        fvs.append({"__typename": "ProjectV2ItemFieldIterationValue",
                    "title": iteration, "field": {"name": "Iteration"}})
    return {
        "fieldValues": {"nodes": fvs},
        "content": {
            "__typename": kind, "number": number, "title": f"t{number}",
            "url": f"u{number}", "state": state, "repository": {"name": repo},
            "assignees": {"nodes": [{"login": a} for a in assignees]},
            "subIssuesSummary": {"total": sub_issues},
            "blockedBy": {"nodes": [{"number": n, "state": st, "repository": {"name": repo}}
                                    for n, st in blockers]},
            "parent": parent,
        },
    }


def umbrella(number, created, children, repo="zpr-core"):
    """Build the `parent` node of an umbrella whose sub-issues are `children`, in order."""
    return {
        "number": number, "createdAt": created, "repository": {"name": repo},
        "subIssues": {"nodes": [{"number": c, "repository": {"name": repo}} for c in children]},
    }


def picked(nodes):
    """Return {number: category} for the selection made for ME."""
    return {m["number"]: m["category"] for m in mct.select(nodes, ME, CUR)}


def test_ready_unassigned_is_pickable():
    assert picked([node(1, "Ready")]) == {1: "pickable"}


def test_ready_assigned_to_me_is_pickable():
    assert picked([node(1, "Ready", assignees=[ME])]) == {1: "pickable"}


def test_ready_assigned_to_someone_else_is_not_listed():
    assert picked([node(1, "Ready", assignees=["alice"])]) == {}


def test_ready_assigned_to_me_and_someone_else_is_pickable():
    assert picked([node(1, "Ready", assignees=["alice", ME])]) == {1: "pickable"}


def test_backlog_or_no_status_is_awaiting_not_pickable():
    assert picked([node(1), node(2, assignees=[ME]), node(3, "Backlog")]) == {
        1: "awaiting-ready", 2: "awaiting-ready", 3: "awaiting-ready"}


def test_legacy_todo_status_is_not_pickable():
    assert picked([node(1, "Todo", assignees=[ME])]) == {1: "mine"}
    assert picked([node(2, "Todo")]) == {}


def test_other_iteration_or_none_is_not_listed():
    assert picked([node(1, "Ready", iteration="Iteration 125"),
                   node(2, "Ready", iteration=None)]) == {}


def test_closed_ready_is_not_pickable():
    assert picked([node(1, "Ready", assignees=[ME], state="CLOSED")]) == {1: "mine"}
    assert picked([node(2, "Ready", state="CLOSED")]) == {}


def test_pull_request_is_never_pickable():
    assert picked([node(1, "Ready", kind="PullRequest", assignees=[ME])]) == {1: "mine"}
    assert picked([node(2, "Ready", kind="PullRequest")]) == {}


def test_unassigned_started_work_is_not_listed():
    assert picked([node(1, "In Progress"), node(2, "Done")]) == {}


def test_my_started_work_is_listed_as_mine():
    assert picked([node(1, "In Progress", assignees=[ME])]) == {1: "mine"}


def test_order_priority_then_assigned_then_number():
    nodes = [
        node(5, "Ready"),
        node(4, "Ready", assignees=[ME]),
        node(3, "Ready", priority="P2"),
        node(2, "Ready", priority="P0"),
        node(1),
        node(6, "Ready", priority="P0", assignees=[ME]),
    ]
    assert [m["number"] for m in mct.select(nodes, ME, CUR)] == [6, 2, 3, 4, 5, 1]


def test_umbrella_is_never_pickable():
    assert picked([node(1, "Ready", sub_issues=3),
                   node(2, "Ready", sub_issues=1, assignees=[ME])]) == {1: "umbrella", 2: "umbrella"}


def test_ready_with_open_blocker_is_pre_authorized():
    assert picked([node(1, "Ready", blockers=[(9, "OPEN"), (8, "CLOSED")])]) == {1: "pre-authorized"}


def test_ready_with_only_closed_blockers_is_pickable():
    assert picked([node(1, "Ready", blockers=[(9, "CLOSED")])]) == {1: "pickable"}


def test_open_blockers_are_reported():
    [m] = mct.select([node(1, "Ready", blockers=[(9, "OPEN"), (8, "CLOSED")])], ME, CUR)
    assert m["blocked_by"] == ["zpr-core#9"]


def test_umbrella_order_oldest_umbrella_then_sub_issue_position():
    old = umbrella(100, "2026-01-01T00:00:00Z", [13, 11, 12])
    new = umbrella(50, "2026-06-01T00:00:00Z", [10])
    nodes = [
        node(10, "Ready", parent=new),
        node(1, "Ready"),
        node(12, "Ready", parent=old),
        node(11, "Ready", parent=old),
    ]
    assert [m["number"] for m in mct.select(nodes, ME, CUR)] == [11, 12, 10, 1]


def test_priority_beats_umbrella_order():
    old = umbrella(100, "2026-01-01T00:00:00Z", [11])
    nodes = [node(11, "Ready", parent=old), node(1, "Ready", priority="P0")]
    assert [m["number"] for m in mct.select(nodes, ME, CUR)] == [1, 11]


def test_retry_marker_only_when_pickable():
    assert mct.retry_marker(mct.select([node(2, "Ready", blockers=[(9, "OPEN")])], ME, CUR)) is None
    assert mct.retry_marker(mct.select([node(1)], ME, CUR)) is None
    assert "PENDING-TODO 1 [zpr-core#1]" in mct.retry_marker(mct.select([node(1, "Ready")], ME, CUR))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
