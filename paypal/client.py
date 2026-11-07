
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

BASE_URL = "https://api-m.sandbox.paypal.com"


class PayPalError(Exception):
    pass


def paypal_response(response):
    if response.is_error:
        raise PayPalError(
            f"PayPal HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )
    return response.json()


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
        return paypal_response(response)["access_token"]
    except httpx.RequestError as exc:
        raise PayPalError("PayPal connection failed") from exc


def create_paypal_order(milestone_id, amount_usd):
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
                        "http://127.0.0.1:8000/api/paypal/return",
                    "cancel_url":
                        "http://127.0.0.1:8000/api/paypal/cancel",
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
                    f"trustpay-create-{milestone_id}",
            },
            json=payload,
            timeout=25,
        )
        return paypal_response(response)
    except httpx.RequestError as exc:
        raise PayPalError("Order creation connection failed") from exc


def capture_paypal_order(order_id):
    token = access_token()

    try:
        response = httpx.post(
            f"{BASE_URL}/v2/checkout/orders/{order_id}/capture",
            headers={
                "Authorization": f"Bearer {token}",
                "PayPal-Request-Id":
                    f"trustpay-capture-{order_id}",
            },
            json={},
            timeout=25,
        )
        return paypal_response(response)
    except httpx.RequestError as exc:
        raise PayPalError("Payment capture connection failed") from exc
