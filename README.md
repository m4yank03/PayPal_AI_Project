# TrustPay AI

A **PayPal Sandbox only** freelance milestone demo using FastAPI, SQLite, Gemini AI, and AG Grid. AI proposes milestones and reviews evidence; client approval and verified PayPal capture remain separate actions. The demo PIN is not production authentication.

## Run locally

Requires Python 3.10 or newer.

```sh
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn backend.main:app --reload
```

Create a local, untracked `.env` with your own `GEMINI_API_KEY`, `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, and `CLIENT_APPROVAL_PIN` (at least eight characters). Use **Sandbox** PayPal application credentials. `GEMINI_MODEL` remains optional. No credentials are included in this repository.

Open `http://127.0.0.1:8000`. `TRUSTPAY_BASE_URL` defaults to that origin; set it to the exact dashboard origin when using another port or host (HTTPS required except localhost). Use the same origin throughout checkout. `PAYPAL_BASE_URL`, if set, must be `https://api-m.sandbox.paypal.com`; live PayPal is rejected.

SQLite continues to use `backend/trustpay.db`. Startup only creates missing tables; it does not clear or migrate existing records. `TRUSTPAY_DB_PATH` optionally selects a different database. Back up your database before switching deployments. Historical activity entries are preserved, including any incorrect entries created by older code.

## Sandbox payment flow

1. Create a project, generate milestones with Gemini, and submit work for AI review.
2. Select **Approve Milestone** and enter the Client Demo PIN.
3. Select **Create PayPal Checkout**, enter the PIN, and continue in the same tab to `www.sandbox.paypal.com` using a Sandbox buyer account.
4. PayPal's return page offers **Return to dashboard**, restoring the project and milestone. Cancellation offers the same dashboard path without changing payment status.
5. Select **Verify & Capture Payment** and enter the PIN. Only a completed capture matching the saved order, USD currency, and expected amount marks the milestone paid. The capture ID remains visible in the confirmation.

The PIN travels in `X-Client-Pin` on approve, checkout, and capture. It is never put in URLs or browser storage. Return URLs restore context only; they never prove payment or automatically capture it. Older saved orders are located by PayPal's `token` when returning.

Activity entries are committed together with the corresponding successful local change. Reusing an approval, checkout, or capture does not duplicate the timeline. SQLite write transactions do not span PayPal network calls, and state is rechecked before committing concurrent responses. PayPal retries retain stable `PayPal-Request-Id` values ([PayPal idempotency documentation](https://developer.paypal.com/api/rest/reference/idempotency/)). PayPal and SQLite cannot share a distributed transaction: if PayPal succeeds but local saving fails, retry the **same** action/order promptly. PayPal retains idempotency keys for a limited time; a persistent mismatch needs Sandbox order reconciliation, not another payment. This demo does not include webhook reconciliation or expired-order replacement.

## Automated tests

```sh
pip install -r requirements-dev.txt
python -m playwright install chromium
python -m pytest -q
```

To use installed Google Chrome instead: `python -m pytest -q --browser-channel chrome`.

Backend-only: `python -m pytest -q tests/test_payments.py tests/test_paypal_client.py`.

Tests cover PIN handling, successful and rejected state transitions, PayPal errors and malformed responses, capture verification, rollback, repeated and concurrent actions, data preservation, Gemini endpoint persistence, Sandbox URL restrictions, and browser navigation/return/error handling. Every test uses a temporary SQLite database. PayPal and Gemini are simulated; unexpected provider calls fail the test. Browser tests execute the real dashboard and FastAPI routes in Chromium, with an offline AG Grid adapter to avoid a CDN dependency. They do not test AG Grid internals or real PayPal/Gemini services. The existing manual `ai/test_gemini.py` is excluded from test discovery because it calls Gemini.

GitHub Actions runs the same suite with Chromium. `.env` variants, databases and SQLite sidecar files, virtual environments, and test artifacts are ignored.
