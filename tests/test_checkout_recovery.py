from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
from unittest.mock import Mock

import httpx
import pytest

from backend import main
from paypal import client as provider
from conftest import HEADERS, ORDER_ID, PIN

NEW_ORDER = "TEST-REPLACEMENT"
NEW_URL = f"https://www.sandbox.paypal.com/checkoutnow?token={NEW_ORDER}"


@pytest.fixture
def unavailable(client, seeded, paypal, monkeypatch):
    project, milestone = seeded
    assert client.post(f"/api/milestones/{milestone}/approve", headers=HEADERS).status_code == 200
    assert client.post(f"/api/milestones/{milestone}/checkout", headers=HEADERS).status_code == 200
    paypal[0].reset_mock()
    paypal[0].return_value = {"id": NEW_ORDER, "links": [{"rel": "payer-action", "href": NEW_URL}]}
    lookup = Mock(side_effect=provider.PayPalOrderUnavailable("Saved order is unavailable"))
    monkeypatch.setattr(main, "get_paypal_order", lookup)
    return project, milestone, lookup


def checkout(client, milestone, confirmed=True, old_id=ORDER_ID):
    return client.post(f"/api/milestones/{milestone}/checkout", headers=HEADERS,
                       json={"replace_order_id": old_id} if confirmed else None)


def saved_order():
    with main.database() as db:
        return dict(db.execute("SELECT * FROM paypal_orders").fetchone())


def history():
    with main.database() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paypal_order_history'").fetchone()
        return [dict(r) for r in db.execute("SELECT * FROM paypal_order_history")] if exists else []


def test_unavailable_order_needs_explicit_confirmation(client, unavailable, paypal):
    _, milestone, _ = unavailable
    result = checkout(client, milestone, confirmed=False)
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "checkout_unavailable"
    assert result.json()["detail"]["order_id"] == ORDER_ID
    assert saved_order()["order_id"] == ORDER_ID
    assert history() == []
    paypal[0].assert_not_called()
    paypal[1].assert_not_called()


@pytest.mark.parametrize("reason", ["unavailable", "voided"])
def test_confirmed_recovery_preserves_old_order_and_logs_once(client, unavailable, paypal, reason):
    project, milestone, lookup = unavailable
    if reason == "voided":
        lookup.side_effect = None
        lookup.return_value = {"id": ORDER_ID, "status": "VOIDED"}
    old = saved_order()
    response = checkout(client, milestone)
    assert response.status_code == 200 and response.json()["order_id"] == NEW_ORDER
    assert saved_order()["order_id"] == NEW_ORDER
    assert saved_order()["status"] == "created" and saved_order()["capture_id"] is None
    archive = history()
    assert len(archive) == 1
    for key, value in old.items():
        assert archive[0][key] == value
    assert archive[0]["replacement_order_id"] == NEW_ORDER
    assert archive[0]["replacement_reason"] == reason
    events = client.get(f"/api/projects/{project}/activity").json()
    assert [e["event_type"] for e in events] == ["checkout_recreated", "checkout_created", "approved"]
    request_id = paypal[0].call_args.kwargs["request_id"]
    assert len(request_id) == 32 and request_id != f"trustpay-create-{milestone}"
    paypal[1].assert_not_called()
    lookup.side_effect = None
    lookup.return_value = {"id": NEW_ORDER, "status": "PAYER_ACTION_REQUIRED"}
    assert checkout(client, milestone).json()["order_id"] == NEW_ORDER
    assert paypal[0].call_count == 1
    assert len(history()) == 1


def test_stale_consent_does_not_replace_another_unavailable_order(client, unavailable, paypal):
    result = checkout(client, unavailable[1], old_id="DIFFERENT-ORDER")
    assert result.status_code == 409 and result.json()["detail"]["order_id"] == ORDER_ID
    paypal[0].assert_not_called()
    assert history() == []


@pytest.mark.parametrize("status", ["COMPLETED", "APPROVED", "UNKNOWN", None])
def test_provider_state_blocks_replacement(client, unavailable, paypal, status):
    _, milestone, lookup = unavailable
    lookup.side_effect = None
    lookup.return_value = {"id": ORDER_ID, "status": status}
    assert checkout(client, milestone).status_code in (409, 502)
    paypal[0].assert_not_called()
    paypal[1].assert_not_called()
    assert history() == []


@pytest.mark.parametrize("message", ["Authentication failed", "Connection timed out", "Unreadable response"])
def test_lookup_errors_never_offer_replacement(client, unavailable, paypal, message):
    _, milestone, lookup = unavailable
    lookup.side_effect = provider.PayPalError(message)
    result = checkout(client, milestone)
    assert result.status_code == 502 and isinstance(result.json()["detail"], str)
    paypal[0].assert_not_called()
    assert history() == []


def test_timeout_retry_uses_same_replacement_id(client, unavailable, paypal):
    _, milestone, _ = unavailable
    result = paypal[0].return_value
    paypal[0].side_effect = [provider.PayPalError("Connection timed out"), result]
    assert checkout(client, milestone).status_code == 502
    assert saved_order()["order_id"] == ORDER_ID and history() == []
    assert checkout(client, milestone).status_code == 200
    first, second = paypal[0].call_args_list
    assert first.kwargs["request_id"] == second.kwargs["request_id"]


def test_storage_failure_preserves_original_and_rolls_back_archive(client, unavailable, paypal, monkeypatch):
    _, milestone, _ = unavailable
    original = saved_order()
    real_record = main.record_activity

    def fail_after_write(*args):
        real_record(*args)
        raise sqlite3.OperationalError("simulated storage failure")

    monkeypatch.setattr(main, "record_activity", fail_after_write)
    with pytest.raises(sqlite3.OperationalError):
        checkout(client, milestone)
    assert saved_order() == original and history() == []
    monkeypatch.setattr(main, "record_activity", real_record)
    assert checkout(client, milestone).status_code == 200
    assert paypal[0].call_args_list[0].kwargs == paypal[0].call_args_list[1].kwargs


def test_concurrent_recovery_archives_and_logs_once(client, unavailable, paypal):
    project, milestone, _ = unavailable
    barrier = threading.Barrier(2)
    result = paypal[0].return_value

    def overlap(*args, **kwargs):
        with main.database() as db:
            db.execute("BEGIN IMMEDIATE")
        barrier.wait(timeout=5)
        return result

    paypal[0].side_effect = overlap
    request = main.CheckoutRequest(replace_order_id=ORDER_ID)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: main.start_checkout(milestone, PIN, request), range(2)))
    assert all(item["order_id"] == NEW_ORDER for item in results)
    assert len(history()) == 1
    assert paypal[0].call_args_list[0].kwargs == paypal[0].call_args_list[1].kwargs
    events = client.get(f"/api/projects/{project}/activity").json()
    assert sum(e["event_type"] == "checkout_recreated" for e in events) == 1


def test_paid_during_provider_call_is_not_replaced(client, unavailable, paypal):
    _, milestone, _ = unavailable
    result = paypal[0].return_value

    def payment_finished(*args, **kwargs):
        with main.database() as db:
            db.execute("UPDATE milestones SET status='paid' WHERE id=?", (milestone,))
        return result

    paypal[0].side_effect = payment_finished
    assert checkout(client, milestone).status_code == 409
    assert saved_order()["order_id"] == ORDER_ID and history() == []


@pytest.mark.parametrize("bad", [
    {"id": ORDER_ID, "links": [{"rel": "approve", "href": NEW_URL}]},
    {"id": NEW_ORDER, "links": [{"rel": "approve", "href": "https://www.paypal.com/checkoutnow"}]},
    {"id": NEW_ORDER, "links": []},
])
def test_invalid_replacement_does_not_modify_saved_order(client, unavailable, paypal, bad):
    paypal[0].return_value = bad
    assert checkout(client, unavailable[1]).status_code == 502
    assert saved_order()["order_id"] == ORDER_ID and history() == []


@pytest.mark.parametrize("status,body,is_unavailable", [
    (404, {"name": "RESOURCE_NOT_FOUND", "details": [{"issue": "INVALID_RESOURCE_ID"}]}, True),
    (404, {"name": "RESOURCE_NOT_FOUND", "details": []}, False),
    (404, {"name": "OTHER", "details": [{"issue": "INVALID_RESOURCE_ID"}]}, False),
    (401, {"name": "AUTHENTICATION_FAILURE"}, False),
    (500, {"name": "RESOURCE_NOT_FOUND", "details": [{"issue": "INVALID_RESOURCE_ID"}]}, False),
])
def test_lookup_only_classifies_explicit_missing_orders(monkeypatch, status, body, is_unavailable):
    monkeypatch.setattr(provider, "access_token", lambda: "test-token")
    monkeypatch.setattr(provider.httpx, "get", lambda *args, **kwargs: httpx.Response(status, json=body))
    with pytest.raises(provider.PayPalError) as caught:
        provider.get_paypal_order(ORDER_ID)
    assert isinstance(caught.value, provider.PayPalOrderUnavailable) == is_unavailable


def test_lookup_is_sandbox_only_and_checks_order_identity(monkeypatch):
    monkeypatch.setattr(provider, "access_token", lambda: "test-token")
    get = Mock(return_value=httpx.Response(200, json={"id": ORDER_ID, "status": "CREATED"}))
    monkeypatch.setattr(provider.httpx, "get", get)
    assert provider.get_paypal_order(ORDER_ID)["id"] == ORDER_ID
    assert get.call_args.args[0] == provider.BASE_URL + "/v2/checkout/orders/" + ORDER_ID
    get.return_value = httpx.Response(200, json={"id": "wrong"})
    with pytest.raises(provider.PayPalError, match="different order"):
        provider.get_paypal_order(ORDER_ID)


def test_create_accepts_replacement_request_key(monkeypatch):
    monkeypatch.setattr(provider, "access_token", lambda: "test-token")
    post = Mock(return_value=httpx.Response(201, json={"id": NEW_ORDER}))
    monkeypatch.setattr(provider.httpx, "post", post)
    provider.create_paypal_order(1, 10, request_id="test-replacement-request")
    assert post.call_args.kwargs["headers"]["PayPal-Request-Id"] == "test-replacement-request"
