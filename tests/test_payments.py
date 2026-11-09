from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import sqlite3
import threading

import pytest

from backend import main
from paypal.client import PayPalError
from conftest import APPROVAL_URL, HEADERS, ORDER_ID, PIN


def post(client, milestone, action, **kwargs):
    return client.post(f"/api/milestones/{milestone}/{action}", headers=HEADERS, **kwargs)


def events(client, project):
    return [event["event_type"] for event in client.get(f"/api/projects/{project}/activity").json()]


def approve_checkout(client, milestone):
    assert post(client, milestone, "approve").status_code == 200
    assert post(client, milestone, "checkout").status_code == 200


@pytest.mark.parametrize("action", ["approve", "checkout", "capture"])
@pytest.mark.parametrize("pin", [None, "wrong"])
def test_pin_required_without_side_effects(client, seeded, paypal, action, pin):
    project, milestone = seeded
    headers = {} if pin is None else {"X-Client-Pin": pin}
    assert client.post(f"/api/milestones/{milestone}/{action}", headers=headers).status_code == 403
    assert events(client, project) == []
    for mock in paypal:
        mock.assert_not_called()


@pytest.mark.parametrize("action", ["approve", "checkout", "capture"])
def test_unconfigured_pin(client, seeded, monkeypatch, action):
    monkeypatch.delenv("CLIENT_APPROVAL_PIN")
    assert post(client, seeded[1], action).status_code == 503
    assert events(client, seeded[0]) == []


def test_success_and_retries_log_each_transition_once(client, seeded, paypal):
    project, milestone = seeded
    for action in ("approve", "checkout", "capture"):
        first = post(client, milestone, action)
        second = post(client, milestone, action)
        assert first.status_code == second.status_code == 200
    assert events(client, project) == ["payment_captured", "checkout_created", "approved"]
    assert second.json()["already_paid"] is True
    assert client.get(f"/api/projects/{project}/milestones").json()[0]["status"] == "paid"
    paypal[0].assert_called_once_with(milestone, 10.0)
    paypal[1].assert_called_once_with(ORDER_ID)
    assert post(client, milestone, "approve").status_code == 409
    assert post(client, milestone, "checkout").status_code == 409
    assert len(events(client, project)) == 3


def test_failed_preconditions_never_log(client, seeded):
    project, milestone = seeded
    for action in ("approve", "checkout", "capture"):
        assert post(client, 99999, action).status_code == 404
    assert post(client, milestone, "checkout").status_code == 409
    assert post(client, milestone, "capture").status_code == 404
    with main.database() as db:
        db.execute("DELETE FROM reviews")  # Only this test's temporary database.
    assert post(client, milestone, "approve").status_code == 409
    assert events(client, project) == []


@pytest.mark.parametrize("action", ["checkout", "capture"])
def test_provider_failure_never_logs_success(client, seeded, paypal, action):
    project, milestone = seeded
    assert post(client, milestone, "approve").status_code == 200
    if action == "capture":
        assert post(client, milestone, "checkout").status_code == 200
    before = events(client, project)
    paypal[action == "capture"].side_effect = PayPalError("Sandbox action failed. Please retry.")
    assert post(client, milestone, action).status_code == 502
    assert events(client, project) == before
    assert client.get(f"/api/projects/{project}/milestones").json()[0]["status"] == "approved"


@pytest.mark.parametrize("bad", [
    {}, {"id": ORDER_ID}, {"id": ORDER_ID, "links": None},
    {"id": ORDER_ID, "links": [{"rel": "approve", "href": "https://www.paypal.com/checkoutnow"}]},
])
def test_invalid_checkout_response_does_not_save(client, seeded, paypal, bad):
    project, milestone = seeded
    post(client, milestone, "approve")
    paypal[0].return_value = bad
    assert post(client, milestone, "checkout").status_code == 502
    assert events(client, project) == ["approved"]
    with main.database() as db:
        assert db.execute("SELECT COUNT(*) FROM paypal_orders").fetchone()[0] == 0


@pytest.mark.parametrize("field,value", [
    ("order_id", "wrong-order"), ("order_status", "APPROVED"), ("capture_status", "PENDING"),
    ("capture_id", ""), ("currency", "EUR"), ("amount", "9.00"), ("units", []), ("units", None),
    ("captures", []), ("captures", None), ("capture_amount", None),
])
def test_capture_verification_rejects_unconfirmed_payments(client, seeded, paypal, field, value):
    project, milestone = seeded
    approve_checkout(client, milestone)
    result = deepcopy(paypal[1].return_value)
    capture = result["purchase_units"][0]["payments"]["captures"][0]
    if field == "order_id": result["id"] = value
    elif field == "order_status": result["status"] = value
    elif field == "capture_status": capture["status"] = value
    elif field == "capture_id": capture["id"] = value
    elif field == "currency": capture["amount"]["currency_code"] = value
    elif field == "amount": capture["amount"]["value"] = value
    elif field == "capture_amount": capture["amount"] = value
    elif field == "units": result["purchase_units"] = value
    elif field == "captures": result["purchase_units"][0]["payments"]["captures"] = value
    paypal[1].return_value = result
    assert post(client, milestone, "capture").status_code == 502
    assert events(client, project) == ["checkout_created", "approved"]
    with main.database() as db:
        row = db.execute("SELECT * FROM paypal_orders").fetchone()
        assert row["status"] == "created" and row["capture_id"] is None


@pytest.mark.parametrize("action", ["approve", "checkout", "capture"])
def test_database_failure_rolls_back_state_and_log(client, seeded, paypal, monkeypatch, action):
    project, milestone = seeded
    if action != "approve": post(client, milestone, "approve")
    if action == "capture": post(client, milestone, "checkout")
    before = events(client, project)
    original = main.record_activity

    def broken(*args):
        original(*args)
        raise sqlite3.OperationalError("simulated commit failure")

    monkeypatch.setattr(main, "record_activity", broken)
    with pytest.raises(sqlite3.OperationalError):
        post(client, milestone, action)
    assert events(client, project) == before
    with main.database() as db:
        status = db.execute("SELECT status FROM milestones WHERE id = ?", (milestone,)).fetchone()[0]
        order = db.execute("SELECT * FROM paypal_orders").fetchone()
    assert status == ("pending" if action == "approve" else "approved")
    if action == "checkout": assert order is None
    if action == "capture": assert order["status"] == "created" and order["capture_id"] is None
    monkeypatch.setattr(main, "record_activity", original)
    assert post(client, milestone, action).status_code == 200
    assert len(events(client, project)) == len(before) + 1


@pytest.mark.parametrize("action", ["approve", "checkout", "capture"])
def test_concurrent_requests_log_once(client, seeded, paypal, action):
    project, milestone = seeded
    if action != "approve": post(client, milestone, "approve")
    if action == "capture": post(client, milestone, "checkout")
    if action != "approve":
        barrier = threading.Barrier(2)
        mock = paypal[action == "capture"]
        result = mock.return_value

        def overlapping(*args):
            # A second SQLite writer must remain possible during provider I/O.
            with main.database() as db:
                db.execute("BEGIN IMMEDIATE")
            barrier.wait(timeout=5)
            return result

        mock.side_effect = overlapping
    fn = {"approve": main.approve_milestone, "checkout": main.start_checkout, "capture": main.capture_payment}[action]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: fn(milestone, PIN), range(2)))
    assert len(results) == 2
    event = {"approve": "approved", "checkout": "checkout_created", "capture": "payment_captured"}[action]
    assert events(client, project).count(event) == 1


@pytest.mark.parametrize("route", ["return", "cancel"])
def test_callback_restores_context_without_payment_mutation(client, seeded, paypal, route):
    project, milestone = seeded
    approve_checkout(client, milestone)
    response = client.get(f"/api/paypal/{route}?token={ORDER_ID}")
    assert response.status_code == 200
    assert "Return to dashboard" in response.text and "/docs" not in response.text
    assert f"project_id={project}" in response.text and f"milestone_id={milestone}" in response.text
    assert events(client, project) == ["checkout_created", "approved"]
    paypal[1].assert_not_called()
    assert client.get(f"/api/paypal/{route}?token=unknown&milestone_id={milestone}").status_code == 200
    fallback = client.get(f"/api/paypal/{route}?milestone_id={milestone}")
    assert f"project_id={project}" in fallback.text
    assert "<script>" not in client.get(f"/api/paypal/{route}", params={"token": "<script>alert(1)</script>"}).text


def test_startup_preserves_existing_data(client, seeded, paypal):
    project, milestone = seeded
    approve_checkout(client, milestone)
    before = client.get(f"/api/projects/{project}/milestones").json()
    main.initialize_database()
    assert client.get(f"/api/projects/{project}/milestones").json() == before
    assert events(client, project) == ["checkout_created", "approved"]


def test_gemini_routes_still_save_results(client, monkeypatch):
    project = client.post("/api/projects", json={"title": "AI demo", "description": "Build a useful website", "budget_usd": 30}).json()
    monkeypatch.setattr(main, "generate_milestones", lambda *args: [
        {"title": f"Stage {i}", "description": "Deliver useful work", "acceptance_criteria": ["Works"], "percentage": pct, "amount_usd": amount}
        for i, pct, amount in [(1, 30, 9), (2, 30, 9), (3, 40, 12)]
    ])
    assert client.post(f"/api/projects/{project['id']}/generate-milestones").status_code == 200
    assert client.post(f"/api/projects/{project['id']}/generate-milestones").status_code == 409
    milestone = client.get(f"/api/projects/{project['id']}/milestones").json()[0]
    review = {"summary": "Reviewed", "recommendation": "send_to_client", "criteria_results": []}
    monkeypatch.setattr(main, "review_work", lambda **kwargs: review)
    assert client.post(f"/api/milestones/{milestone['id']}/review", json={"evidence_text": "A complete demonstration of the delivered website."}).status_code == 201
    assert client.get(f"/api/milestones/{milestone['id']}/reviews").json()[0]["summary"] == "Reviewed"


def test_commit_error_rolls_back_and_closes_connection(client, seeded, monkeypatch):
    real_connect = sqlite3.connect
    calls = []

    class FailingCommitConnection:
        row_factory = None

        def __init__(self):
            self.connection = real_connect(main.DB)

        def execute(self, *args):
            return self.connection.execute(*args)

        def commit(self):
            raise sqlite3.OperationalError("simulated disk failure")

        def rollback(self):
            calls.append("rollback")
            self.connection.rollback()

        def close(self):
            calls.append("close")
            self.connection.close()

    with monkeypatch.context() as patch:
        patch.setattr(main.sqlite3, "connect", lambda *args: FailingCommitConnection())
        with pytest.raises(sqlite3.OperationalError, match="disk failure"):
            with main.database() as db:
                db.execute("UPDATE milestones SET status = 'approved' WHERE id = ?", (seeded[1],))
                main.record_activity(db, seeded[1], "approved", "Test approval")
    assert calls == ["rollback", "close"]
    assert events(client, seeded[0]) == []
    with main.database() as db:
        assert db.execute("SELECT status FROM milestones").fetchone()[0] == "pending"
