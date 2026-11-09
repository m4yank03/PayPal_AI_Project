import pytest
from fastapi.testclient import TestClient

from backend import main

PIN = "test-only-pin"
HEADERS = {"X-Client-Pin": PIN}
ORDER_ID = "TEST-ORDER"
APPROVAL_URL = f"https://www.sandbox.paypal.com/checkoutnow?token={ORDER_ID}"


@pytest.fixture(autouse=True)
def isolated_app(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB", tmp_path / "trustpay-test.db")
    monkeypatch.setenv("CLIENT_APPROVAL_PIN", PIN)

    def unexpected(*args, **kwargs):
        pytest.fail("Tests must mock PayPal/Gemini; no external service calls are allowed")

    monkeypatch.setattr("paypal.client.httpx.post", unexpected)
    monkeypatch.setattr(main, "generate_milestones", unexpected)
    monkeypatch.setattr(main, "review_work", unexpected)
    main.initialize_database()


@pytest.fixture
def client():
    with TestClient(main.app) as client:
        yield client


@pytest.fixture
def seeded(client):
    project = client.post("/api/projects", json={
        "title": "Demo project", "description": "Build a portfolio website", "budget_usd": 10
    }).json()
    with main.database() as db:
        milestone = db.execute("""
            INSERT INTO milestones (project_id, title, description, acceptance_criteria, percentage, amount_usd)
            VALUES (?, 'Demo milestone', 'Implement the portfolio', '["Works correctly"]', 100, 10)
        """, (project["id"],)).lastrowid
        submission = db.execute(
            "INSERT INTO submissions (milestone_id, evidence_text) VALUES (?, 'Test evidence')", (milestone,)
        ).lastrowid
        db.execute("""
            INSERT INTO reviews (submission_id, summary, recommendation, criteria_results)
            VALUES (?, 'Reviewed', 'send_to_client', '[]')
        """, (submission,))
    return project["id"], milestone


@pytest.fixture
def paypal(monkeypatch):
    from unittest.mock import Mock
    create = Mock(return_value={"id": ORDER_ID, "links": [{"rel": "payer-action", "href": APPROVAL_URL}]})
    capture = Mock(return_value={
        "id": ORDER_ID, "status": "COMPLETED", "purchase_units": [{"payments": {"captures": [{
            "id": "TEST-CAPTURE", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "10.00"}
        }]}}]
    })
    monkeypatch.setattr(main, "create_paypal_order", create)
    monkeypatch.setattr(main, "capture_paypal_order", capture)
    return create, capture
