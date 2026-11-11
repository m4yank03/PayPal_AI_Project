# TrustPay AI

An AI-assisted freelance milestone workflow with PayPal Sandbox payments.

TrustPay AI helps clients and freelancers define milestones, review submitted work, approve deliverables, and verify payments through a single dashboard.

> **Sandbox only:** This project uses PayPal test accounts and simulated money. It is a demonstration application, not a production payment or escrow service.

## Features

- **AI milestone planning:** Generate three milestones with acceptance criteria and budget allocations using Google Gemini.
- **AI work review:** Evaluate submitted evidence against milestone acceptance criteria.
- **Client approval:** Require a Client Demo PIN before approving milestones, creating checkouts, or capturing payments.
- **PayPal Sandbox checkout:** Redirect buyers to PayPal and provide a return-to-dashboard flow.
- **Verified payment capture:** Validate the order, capture status, USD currency, and expected amount before marking a milestone paid.
- **Checkout recovery:** Offer a fresh checkout when PayPal reports a saved order unavailable, while preserving the previous order in history.
- **Payment activity timeline:** Record successful approvals, checkout creation, replacement checkouts, and verified captures.
- **Interactive dashboard:** Browse, filter, sort, and export milestone data using AG Grid.
- **Persistent storage:** Store projects, milestones, reviews, and payment records in SQLite.

## Tech Stack

| Component | Technology |
|---|---|
| Backend | Python, FastAPI |
| Frontend | HTML, CSS, JavaScript |
| Data grid | AG Grid Community |
| AI | Google Gemini |
| Database | SQLite |
| Payments | PayPal Sandbox Orders API |
| Testing | pytest, FastAPI TestClient, Playwright |

## How It Works

1. Create a project with a description and USD budget.
2. Generate milestones using Gemini.
3. Submit work evidence for an AI review.
4. Review the recommendation and approve the milestone using the Client Demo PIN.
5. Create a PayPal Sandbox checkout.
6. Sign in with a Sandbox buyer account and approve the payment.
7. Return to the dashboard and select **Verify & Capture Payment**.
8. After successful verification, the milestone becomes **paid** and the dashboard displays the Capture ID.

AI provides recommendations. The client makes the approval decision, and the backend verifies the payment.

## Local Setup

### Requirements

- Python 3.10 or newer
- A Google Gemini API key
- PayPal Sandbox application credentials
- Separate PayPal Sandbox buyer and seller accounts

### 1. Download the project

```bash
git clone https://github.com/m4yank03/PayPal_AI_Project.git
cd PayPal_AI_Project
```

### 2. Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
python -m pip install -r requirements.txt
```

### 4. Configure local environment variables

Create a `.env` file in the project root. Replace the placeholders below with your own values:

```dotenv
GEMINI_API_KEY=your_gemini_api_key

PAYPAL_CLIENT_ID=your_sandbox_client_id
PAYPAL_CLIENT_SECRET=your_sandbox_client_secret
PAYPAL_BASE_URL=https://api-m.sandbox.paypal.com

CLIENT_APPROVAL_PIN=choose_a_private_pin_at_least_8_characters

TRUSTPAY_BASE_URL=http://127.0.0.1:8000
```

Optional settings:

- `GEMINI_MODEL`: Override the Gemini model used by the application.
- `TRUSTPAY_DB_PATH`: Override the SQLite database location.

The default database is `backend/trustpay.db`. Existing records are preserved when the application restarts.

Keep `.env`, API credentials, and database files out of Git.

### 5. Start the application

```bash
python -m uvicorn backend.main:app --reload
```

Open:

- Dashboard: [http://127.0.0.1:8000](http://127.0.0.1:8000)
- API documentation: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

If you use another address or port, update `TRUSTPAY_BASE_URL` to match the dashboard origin.

## PayPal Sandbox Setup

Use two separate Sandbox accounts:

| Account | Purpose |
|---|---|
| Business / seller | Receives the Sandbox payment and owns the application's API credentials |
| Personal / buyer | Signs in at checkout and approves the Sandbox payment |

### Configuration verified during testing

The successful manual test used:

- Checkout currency: **USD**
- Buyer account: **PayPal Sandbox Personal**
- Buyer account country: **United States**

In this project's testing, changing to a US Sandbox Personal buyer account resolved the checkout error encountered with the earlier account setup. This records the configuration that worked; it is not a claim that all other account countries are unsupported.

Use Sandbox credentials throughout the flow.

## Payment Verification and Recovery

Returning from PayPal does not automatically mark a milestone paid.

The backend verifies:

- The returned order ID matches the saved order.
- The order and capture are completed.
- A capture ID is present.
- The captured currency is USD.
- The captured amount matches the saved milestone payment.

For an unavailable saved checkout, the dashboard asks before creating a replacement. The previous order is preserved in `paypal_order_history`.

If you have already completed a payment, cancel the replacement prompt and verify the existing order before attempting another payment.

Authentication failures, connection errors, and unknown order states do not automatically create replacement orders.

## Activity Logging and Data Integrity

Successful payment actions and their activity entries are saved in the same SQLite transaction.

- Failed actions do not add success entries.
- Repeated completed actions do not duplicate activity entries.
- Concurrent responses are checked against the latest saved state.
- Failed local transactions are rolled back.
- PayPal retries use stable request IDs.
- Previous checkout records are preserved during replacement.

PayPal and SQLite cannot commit as one transaction. If PayPal succeeds but local saving fails, retry the same action for the same order. Persistent inconsistencies require checking the Sandbox order before creating another payment.

## Testing

### Automated tests

Install the development dependencies and browser:

```bash
python -m pip install -r requirements-dev.txt
python -m playwright install chromium
```

Run the suite:

```bash
python -m pytest -q
```

To use an installed Google Chrome browser:

```bash
python -m pytest -q --browser-channel chrome
```

The regression suite covers:

- PIN enforcement and frontend header handling
- Successful and rejected payment actions
- Capture verification
- Database rollback
- Repeated and concurrent requests
- Checkout replacement and order-history preservation
- Sandbox URL restrictions
- Dashboard return and cancellation flows
- Error handling
- Persistence of Gemini-generated milestones and reviews

Automated tests use temporary databases and simulated PayPal/Gemini responses. Browser tests use an offline AG Grid adapter; they do not validate the AG Grid package itself.

### Manual Sandbox validation

The successful payment flow was also tested manually:

- Sandbox checkout opened.
- The buyer approved the payment.
- The dashboard return flow worked.
- Payment capture completed.
- The milestone displayed **paid** with a **Capture ID**.

## Project Structure

```text
PayPal_AI_Project/
├── ai/
│   ├── milestone_generator.py
│   └── work_reviewer.py
├── backend/
│   └── main.py
├── frontend/
│   └── index.html
├── paypal/
│   └── client.py
├── tests/
├── requirements.txt
├── requirements-dev.txt
├── pytest.ini
└── README.md
```

The local `.env` file and SQLite database are not included in version control.

## Limitations

- This application is restricted to PayPal Sandbox.
- The shared Client Demo PIN is intended for demonstration, not production authentication.
- AI reviews assess the evidence supplied as text; they do not independently inspect or verify external deliverables.
- Production features such as user-level authorization, webhook reconciliation, and dispute handling are outside the current scope.

## License

See the repository's `LICENSE` file.