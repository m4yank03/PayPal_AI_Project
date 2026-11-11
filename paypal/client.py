
import os
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

BASE_URL = "https://api-m.sandbox.paypal.com"


class PayPalError(Exception):
    pass


class PayPalOrderUnavailable(PayPalError):
    """PayPal explicitly reports that the saved order cannot be found."""


def get_paypal_order(order_id):
    token = access_token()
    try:
        response = httpx.get(
            f"{BASE_URL}/v2/checkout/orders/{quote(order_id, safe='')}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=25,
        )
    except httpx.RequestError as exc:
        raise PayPalError(
            "Cannot verify the saved Sandbox checkout. Please retry; no replacement was created."
        ) from exc

    if response.status_code == 404:
        try:
            data = response.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("name") == "RESOURCE_NOT_FOUND":
            details = data.get("details")
            if isinstance(details, list) and any(
                isinstance(detail, dict) and detail.get("issue") == "INVALID_RESOURCE_ID"
                for detail in details
            ):
                raise PayPalOrderUnavailable(
                    "PayPal can no longer find this saved Sandbox checkout."
                )

    order = paypal_response(response)
    if order.get("id") != order_id:
        raise PayPalError("PayPal returned a different order. No replacement was created.")
    return order


def validate_approval_url(url):
    try:
        parsed = urlsplit(url) if isinstance(url, str) else None
        valid = (parsed is not None and parsed.scheme == "https"
                 and parsed.hostname == "www.sandbox.paypal.com"
                 and parsed.port in (None, 443)
                 and not parsed.username and not parsed.password)
    except ValueError:
        valid = False
    if not valid:
        raise PayPalError("PayPal returned an unexpected checkout URL. Only PayPal Sandbox checkout is allowed.")
    return url


def app_base_url():
    base = os.getenv("TRUSTPAY_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    try:
        parsed = urlsplit(base)
        valid = (bool(parsed.hostname) and parsed.port != 0
                 and (parsed.scheme == "https" or
                      (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")))
                 and not parsed.username and not parsed.password
                 and not parsed.query and not parsed.fragment and not parsed.path)
    except ValueError:
        valid = False
    if not valid:
        raise PayPalError("Configure TRUSTPAY_BASE_URL as your dashboard origin: HTTPS, or HTTP on localhost.")
    return base


def paypal_response(response):
    try:
        data = response.json()
    except ValueError as exc:
        raise PayPalError("PayPal returned an unreadable response. Please retry the Sandbox action.") from exc
    if not isinstance(data, dict):
        raise PayPalError("PayPal returned an unexpected response. Please retry the Sandbox action.")
    if response.is_error:
        # Never surface or log raw provider bodies: they can contain buyer data
        # or credentials. Only known issue codes select our own safe messages.
        details = data.get("details", [])
        if not isinstance(details, list):
            details = []
        issues = {
            detail["issue"] for detail in details
            if isinstance(detail, dict) and isinstance(detail.get("issue"), str)
        }
        if "ORDER_NOT_APPROVED" in issues:
            message = "Approve the payment in PayPal Sandbox before selecting Verify & Capture Payment."
        elif "INSTRUMENT_DECLINED" in issues:
            message = "The Sandbox funding source was declined. Return to PayPal and choose another Sandbox funding source."
        elif "ORDER_ALREADY_CAPTURED" in issues:
            message = "PayPal reports this order was already captured. Verify the Sandbox order before retrying; do not create another payment."
        elif response.status_code in (401, 403):
            message = "PayPal Sandbox authentication failed. Check the server's Sandbox credentials."
        else:
            message = "PayPal could not complete the Sandbox action. Please retry; if it persists, check the Sandbox order in PayPal."
        raise PayPalError(message)
    return data


def access_token():
    client_id = os.getenv("PAYPAL_CLIENT_ID")
    secret = os.getenv("PAYPAL_CLIENT_SECRET")

    if not client_id or not secret:
        raise PayPalError("Sandbox credentials missing from .env")

    if os.getenv("PAYPAL_BASE_URL", BASE_URL) != BASE_URL:
        raise PayPalError("Only PayPal Sandbox is allowed")

    try:
        response = httpx.post(
            f"{BASE_URL}/v1/oauth2/token",
            auth=(client_id, secret),
            data={"grant_type": "client_credentials"},
            timeout=25,
        )
        token = paypal_response(response).get("access_token")
        if not isinstance(token, str) or not token:
            raise PayPalError("PayPal did not return a Sandbox access token. Check the server's Sandbox credentials.")
        return token
    except httpx.RequestError as exc:
        raise PayPalError("Cannot connect to PayPal Sandbox. Please retry.") from exc


def create_paypal_order(milestone_id, amount_usd, request_id=None):
    base = app_base_url()
    token = access_token()

    payload = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": f"milestone-{milestone_id}",
            "custom_id": f"trustpay-{milestone_id}",
            "description": f"TrustPay Milestone #{milestone_id}",
            "amount": {
                "currency_code": "USD",
                "value": f"{amount_usd:.2f}",
            },
        }],
        "payment_source": {
            "paypal": {
                "experience_context": {
                    "return_url":
                        f"{base}/api/paypal/return?milestone_id={milestone_id}",
                    "cancel_url":
                        f"{base}/api/paypal/cancel?milestone_id={milestone_id}",
                    "shipping_preference": "NO_SHIPPING",
                    "user_action": "PAY_NOW",
                }
            }
        },
    }

    try:
        response = httpx.post(
            f"{BASE_URL}/v2/checkout/orders",
            headers={
                "Authorization": f"Bearer {token}",
                "PayPal-Request-Id":
                    request_id or f"trustpay-create-{milestone_id}",
            },
            json=payload,
            timeout=25,
        )
        return paypal_response(response)
    except httpx.RequestError as exc:
        raise PayPalError("PayPal Sandbox checkout timed out or could not connect. Retry checkout to recover the same order.") from exc


def capture_paypal_order(order_id):
    token = access_token()

    try:
        response = httpx.post(
            f"{BASE_URL}/v2/checkout/orders/{order_id}/capture",
            headers={
                "Authorization": f"Bearer {token}",
                # The default minimal response omits the capture details that
                # TrustPay must verify before saving a paid milestone.
                "Prefer": "return=representation",
                "PayPal-Request-Id":
                    f"trustpay-capture-{order_id}",
            },
            json={},
            timeout=25,
        )
        return paypal_response(response)
    except httpx.RequestError as exc:
        raise PayPalError("PayPal Sandbox capture timed out or could not connect. Retry Verify & Capture Payment for this same order to confirm its status.") from exc
