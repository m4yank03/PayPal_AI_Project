import httpx
import pytest

from paypal import client as paypal


@pytest.mark.parametrize("url", [
    "https://www.paypal.com/checkoutnow", "http://www.sandbox.paypal.com/checkoutnow",
    "https://www.sandbox.paypal.com.evil.example/checkoutnow", "javascript:alert(1)",
    "https://user:pass@www.sandbox.paypal.com/checkoutnow", "https://www.sandbox.paypal.com:8443/checkoutnow",
    "https://www.sandbox.paypal.com:bad/checkoutnow", "//www.sandbox.paypal.com/checkoutnow", None,
])
def test_only_sandbox_approval_urls(url):
    with pytest.raises(paypal.PayPalError, match="Only PayPal Sandbox"):
        paypal.validate_approval_url(url)


def test_valid_sandbox_url():
    url = "https://www.sandbox.paypal.com/checkoutnow?token=TEST"
    assert paypal.validate_approval_url(url) == url


@pytest.mark.parametrize("body", ["not-json", "[]", "null"])
def test_malformed_provider_response(body):
    with pytest.raises(paypal.PayPalError, match="response"):
        paypal.paypal_response(httpx.Response(200, text=body))


def test_provider_error_redacts_raw_details():
    with pytest.raises(paypal.PayPalError) as error:
        paypal.paypal_response(httpx.Response(422, json={
            "message": "sensitive-provider-data", "details": [{"issue": "ORDER_NOT_APPROVED", "value": "private-buyer-data"}]
        }))
    assert "Approve the payment" in str(error.value)
    assert "sensitive-provider-data" not in str(error.value)
    assert "private-buyer-data" not in str(error.value)


@pytest.mark.parametrize("base", ["https://api-m.paypal.com", "https://example.com"])
def test_live_api_override_is_blocked(monkeypatch, base):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "test-client")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("PAYPAL_BASE_URL", base)
    with pytest.raises(paypal.PayPalError, match="Only PayPal Sandbox"):
        paypal.access_token()


@pytest.mark.parametrize("base", ["http://example.com", "https://user:pass@example.com", "https://example.com/path", "https://example.com?token=abc"])
def test_invalid_dashboard_origin(monkeypatch, base):
    monkeypatch.setenv("TRUSTPAY_BASE_URL", base)
    with pytest.raises(paypal.PayPalError, match="TRUSTPAY_BASE_URL"):
        paypal.app_base_url()


def test_requests_use_sandbox_context_and_stable_retry_ids(monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "test-client")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "test-secret")
    monkeypatch.delenv("PAYPAL_BASE_URL", raising=False)
    monkeypatch.setenv("TRUSTPAY_BASE_URL", "https://trustpay.example")
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json={"access_token": "test-token"} if url.endswith("/token") else {"id": "TEST"})

    monkeypatch.setattr(paypal.httpx, "post", fake_post)
    for _ in range(2):
        paypal.create_paypal_order(42, 10)
        paypal.capture_paypal_order("TEST")
    assert all(url.startswith("https://api-m.sandbox.paypal.com/") for url, _ in calls)
    orders = [kwargs for url, kwargs in calls if url.endswith("/orders")]
    captures = [kwargs for url, kwargs in calls if url.endswith("/capture")]
    assert all(call["headers"]["Prefer"] == "return=representation" for call in captures)
    assert orders[0]["headers"]["PayPal-Request-Id"] == orders[1]["headers"]["PayPal-Request-Id"]
    assert captures[0]["headers"]["PayPal-Request-Id"] == captures[1]["headers"]["PayPal-Request-Id"]
    context = orders[0]["json"]["payment_source"]["paypal"]["experience_context"]
    assert context["return_url"] == "https://trustpay.example/api/paypal/return?milestone_id=42"
    assert context["cancel_url"] == "https://trustpay.example/api/paypal/cancel?milestone_id=42"
    assert orders[0]["json"]["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "10.00"}


def test_missing_access_token_has_safe_error(monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "test-client")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "test-secret")
    monkeypatch.delenv("PAYPAL_BASE_URL", raising=False)
    monkeypatch.setattr(paypal.httpx, "post", lambda *args, **kwargs: httpx.Response(200, json={}))
    with pytest.raises(paypal.PayPalError, match="access token"):
        paypal.access_token()
