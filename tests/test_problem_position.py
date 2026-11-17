"""Prev/next navigation endpoint: no 100-problem cap, contest-aware ordering."""

from tests.test_contest_api import contest_payload
from tests.test_problem_api import create_published_problem


def test_position_in_practice_mode(client, admin_headers):
    first = create_published_problem(client, admin_headers)
    second = create_published_problem(client, admin_headers)

    pos = client.get(f"/api/v1/problems/{first}/position")
    assert pos.status_code == 200, pos.text
    body = pos.json()
    assert body["total"] == 2
    assert body["index"] == 0
    assert body["prevId"] is None
    assert body["nextId"] == second

    pos2 = client.get(f"/api/v1/problems/{second}/position").json()
    assert pos2["index"] == 1
    assert pos2["prevId"] == first
    assert pos2["nextId"] is None


def test_position_unknown_problem_is_404(client):
    assert client.get("/api/v1/problems/999999/position").status_code == 404


def test_position_in_contest_mode_follows_contest_order(client, admin_headers, user_headers):
    first = create_published_problem(client, admin_headers)
    second = create_published_problem(client, admin_headers)
    payload = contest_payload(first)
    payload["problems"].append({"problemId": second, "label": "B", "position": 2})
    created = client.post("/api/v1/admin/contests", json=payload, headers=admin_headers)
    assert created.status_code == 201, created.text
    contest_id = created.json()["id"]
    assert client.post(f"/api/v1/admin/contests/{contest_id}/publish", headers=admin_headers).status_code == 200

    # Upcoming contest: problems are locked for regular users.
    locked = client.get(f"/api/v1/problems/{second}/position", params={"contest_id": contest_id}, headers=user_headers)
    assert locked.status_code == 403

    # Admins can inspect contest ordering even before start.
    admin_pos = client.get(
        f"/api/v1/problems/{second}/position", params={"contest_id": contest_id}, headers=admin_headers
    )
    assert admin_pos.status_code == 200, admin_pos.text
    body = admin_pos.json()
    assert body["total"] == 2
    assert body["index"] == 1
    assert body["prevId"] == first