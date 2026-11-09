from pathlib import Path
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from html import escape
from urllib.parse import urlencode
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, Field
import json
from ai.milestone_generator import generate_milestones
from ai.work_reviewer import review_work
from paypal.client import (
    create_paypal_order,
    capture_paypal_order,
    PayPalError,
    validate_approval_url,
)
from fastapi.responses import HTMLResponse
from fastapi.responses import FileResponse
import os
import secrets

DB = Path(os.getenv("TRUSTPAY_DB_PATH", Path(__file__).resolve().parent / "trustpay.db"))


@asynccontextmanager
async def lifespan(app):
    initialize_database()
    yield


app = FastAPI(title='TrustPay AI', version='0.1.0', lifespan=lifespan)

@contextmanager
def database():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

def initialize_database():
    with database() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            budget_usd REAL NOT NULL CHECK(budget_usd > 0),
            status TEXT NOT NULL DEFAULT 'draft'
        )''')
        db.execute('''
            CREATE TABLE IF NOT EXISTS milestones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                acceptance_criteria TEXT NOT NULL,
                percentage INTEGER NOT NULL,
                amount_usd REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                FOREIGN KEY (project_id) REFERENCES projects(id)
            )
        ''')

        db.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                milestone_id INTEGER NOT NULL,
                evidence_text TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (milestone_id)
                    REFERENCES milestones(id)
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                submission_id INTEGER NOT NULL UNIQUE,
                summary TEXT NOT NULL,
                recommendation TEXT NOT NULL,
                criteria_results TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (submission_id)
                    REFERENCES submissions(id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS paypal_orders (
                milestone_id INTEGER PRIMARY KEY,
                order_id TEXT NOT NULL UNIQUE,
                approval_url TEXT NOT NULL,
                amount_usd REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'created',
                capture_id TEXT UNIQUE,
                FOREIGN KEY (milestone_id)
                    REFERENCES milestones(id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                milestone_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (milestone_id) REFERENCES milestones(id)
            )
        """)

def record_activity(db, milestone_id, event_type, details):
    db.execute(
        """
        INSERT INTO activity_log
        (milestone_id, event_type, details)
        VALUES (?, ?, ?)
        """,
        (milestone_id, event_type, details)
    )


class ProjectCreate(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=10, max_length=3000)
    budget_usd: float = Field(gt=0, le=100000)

@app.get('/api/health')
def health():
    return {'status': 'ok', 'app': 'TrustPay AI', 'version': '0.1.0'}

@app.post('/api/projects', status_code=201)
def create_project(project: ProjectCreate):
    with database() as db:
        cursor = db.execute(
            'INSERT INTO projects (title, description, budget_usd) VALUES (?, ?, ?)',
            (project.title, project.description, project.budget_usd)
        )
        project_id = cursor.lastrowid
        row = db.execute('SELECT * FROM projects WHERE id = ?', (project_id,)).fetchone()
        return dict(row)

@app.get('/api/projects')
def list_projects():
    with database() as db:
        return [dict(row) for row in db.execute('SELECT * FROM projects ORDER BY id DESC')]

@app.get('/api/projects/{project_id}')
def get_project(project_id: int):
    with database() as db:
        row = db.execute('SELECT * FROM projects WHERE id = ?', (project_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail='Project not found')
        return dict(row)



@app.post("/api/projects/{project_id}/generate-milestones")
def generate_project_milestones(project_id: int):
    with database() as db:
        project = db.execute(
            "SELECT * FROM projects WHERE id = ?",
            (project_id,)
        ).fetchone()

    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    # Avoid duplicate milestones and unnecessary AI calls.
    with database() as db:
        existing = db.execute(
            "SELECT COUNT(*) FROM milestones WHERE project_id = ?",
            (project_id,)
        ).fetchone()[0]

    if existing:
        raise HTTPException(
            status_code=409,
            detail="Milestones already exist for this project"
        )

    if round(project["budget_usd"] * 100) < 3:
        raise HTTPException(
            status_code=422,
            detail="Budget must be at least $0.03"
        )

    try:
        milestones = generate_milestones(
            project["title"],
            project["description"],
            project["budget_usd"]
        )
    except Exception as exc:
        print(f"Milestone generation failed: {type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=502,
            detail="AI milestone generation failed. Check server logs."
        ) from exc

    with database() as db:
        # Atomic transaction prevents duplicate plans from
        # concurrent requests.
        db.execute("BEGIN IMMEDIATE")

        existing = db.execute(
            "SELECT COUNT(*) FROM milestones WHERE project_id = ?",
            (project_id,)
        ).fetchone()[0]

        if existing:
            raise HTTPException(
                status_code=409,
                detail="Milestones already exist for this project"
            )

        for milestone in milestones:
            db.execute(
                """
                INSERT INTO milestones
                (project_id, title, description,
                 acceptance_criteria, percentage, amount_usd)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    milestone["title"],
                    milestone["description"],
                    json.dumps(milestone["acceptance_criteria"]),
                    milestone["percentage"],
                    milestone["amount_usd"]
                )
            )

    return {
        "project_id": project_id,
        "milestones": milestones,
        "total_usd": round(
            sum(m["amount_usd"] for m in milestones), 2
        )
    }


@app.get("/api/projects/{project_id}/milestones")
def list_milestones(project_id: int):
    with database() as db:
        project = db.execute(
            "SELECT id FROM projects WHERE id = ?",
            (project_id,)
        ).fetchone()

        if project is None:
            raise HTTPException(
                status_code=404,
                detail="Project not found"
            )

        rows = db.execute(
            """
            SELECT * FROM milestones
            WHERE project_id = ?
            ORDER BY id
            """,
            (project_id,)
        ).fetchall()

    result = []

    for row in rows:
        milestone = dict(row)
        milestone["acceptance_criteria"] = json.loads(
            milestone["acceptance_criteria"]
        )
        result.append(milestone)

    return result

class WorkSubmission(BaseModel):
    evidence_text: str = Field(min_length=30, max_length=5000)


@app.post("/api/milestones/{milestone_id}/review", status_code=201)
def review_milestone(milestone_id: int, submission: WorkSubmission):

    with database() as db:
        milestone = db.execute(
            "SELECT * FROM milestones WHERE id = ?",
            (milestone_id,)
        ).fetchone()

    if milestone is None:
        raise HTTPException(
            status_code=404,
            detail="Milestone not found"
        )

    criteria = json.loads(milestone["acceptance_criteria"])

    try:
        result = review_work(
            title=milestone["title"],
            description=milestone["description"],
            criteria=criteria,
            evidence=submission.evidence_text
        )

    except Exception as exc:
        print(
            f"AI Work Review failed: "
            f"{type(exc).__name__}: {exc}"
        )
        raise HTTPException(
            status_code=502,
            detail="AI review failed. Check server logs."
        ) from exc

    with database() as db:
        cursor = db.execute(
            """
            INSERT INTO submissions
            (milestone_id, evidence_text)
            VALUES (?, ?)
            """,
            (milestone_id, submission.evidence_text)
        )

        submission_id = cursor.lastrowid

        review_cursor = db.execute(
            """
            INSERT INTO reviews
            (submission_id, summary, recommendation,
             criteria_results)
            VALUES (?, ?, ?, ?)
            """,
            (
                submission_id,
                result["summary"],
                result["recommendation"],
                json.dumps(result["criteria_results"])
            )
        )

        review_id = review_cursor.lastrowid

    return {
        "review_id": review_id,
        "submission_id": submission_id,
        "milestone_id": milestone_id,
        "review": result
    }


@app.get("/api/milestones/{milestone_id}/reviews")
def list_work_reviews(milestone_id: int):

    with database() as db:
        milestone = db.execute(
            "SELECT id FROM milestones WHERE id = ?",
            (milestone_id,)
        ).fetchone()

        if milestone is None:
            raise HTTPException(
                status_code=404,
                detail="Milestone not found"
            )

        rows = db.execute(
            """
            SELECT
                reviews.id AS review_id,
                submissions.id AS submission_id,
                reviews.summary,
                reviews.recommendation,
                reviews.criteria_results,
                reviews.created_at
            FROM reviews
            JOIN submissions
                ON reviews.submission_id = submissions.id
            WHERE submissions.milestone_id = ?
            ORDER BY reviews.id DESC
            """,
            (milestone_id,)
        ).fetchall()

    output = []

    for row in rows:
        item = dict(row)
        item["criteria_results"] = json.loads(
            item["criteria_results"]
        )
        output.append(item)

    return output


# DEMO ONLY: These routes do not yet authenticate
# the client. Never use for live payments.

def verify_client_pin(pin: str):
    expected = os.getenv("CLIENT_APPROVAL_PIN", "")

    if len(expected) < 8:
        raise HTTPException(
            status_code=503,
            detail="Client demo PIN not configured"
        )

    if not secrets.compare_digest(pin.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(
            status_code=403,
            detail="Invalid client demo PIN"
        )

@app.post("/api/milestones/{milestone_id}/approve")
def approve_milestone(milestone_id: int, x_client_pin: str = Header(default="")):
    verify_client_pin(x_client_pin)
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        milestone = db.execute(
            "SELECT * FROM milestones WHERE id = ?", (milestone_id,)
        ).fetchone()
        if milestone is None:
            raise HTTPException(404, "Milestone not found")
        if milestone["status"] == "approved":
            return {"milestone_id": milestone_id, "status": "approved"}
        if milestone["status"] != "pending":
            raise HTTPException(409, "Only a pending milestone can be approved.")
        review_count = db.execute("""
            SELECT COUNT(*) FROM reviews
            JOIN submissions ON reviews.submission_id = submissions.id
            WHERE submissions.milestone_id = ?
        """, (milestone_id,)).fetchone()[0]
        if review_count == 0:
            raise HTTPException(409, "Submit work for an AI review before approving this milestone.")
        db.execute("UPDATE milestones SET status = 'approved' WHERE id = ?", (milestone_id,))
        record_activity(db, milestone_id, "approved", "Client approved milestone for Sandbox checkout")
    return {"milestone_id": milestone_id, "status": "approved"}


def checkout_response(order):
    # Validate stored orders too; older rows may contain a non-Sandbox link.
    try:
        validate_approval_url(order["approval_url"])
    except PayPalError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {
        "order_id": order["order_id"],
        "approval_url": order["approval_url"],
        "amount_usd": order["amount_usd"],
        "status": "awaiting_buyer_approval",
    }


def require_checkout_milestone(db, milestone_id):
    milestone = db.execute("SELECT * FROM milestones WHERE id = ?", (milestone_id,)).fetchone()
    if milestone is None:
        raise HTTPException(404, "Milestone not found")
    if milestone["status"] == "paid":
        raise HTTPException(409, "This milestone is already paid.")
    if milestone["status"] != "approved":
        raise HTTPException(409, "Approve this milestone before opening PayPal Sandbox checkout.")
    return milestone


@app.post("/api/milestones/{milestone_id}/checkout")
def start_checkout(milestone_id: int, x_client_pin: str = Header(default="")):
    verify_client_pin(x_client_pin)
    with database() as db:
        milestone = require_checkout_milestone(db, milestone_id)
        existing = db.execute(
            "SELECT * FROM paypal_orders WHERE milestone_id = ?", (milestone_id,)
        ).fetchone()
    if existing:
        return checkout_response(existing)

    # Network calls run outside SQLite write transactions. The PayPal client
    # uses a stable request ID so retries refer to the same Sandbox order.
    try:
        order = create_paypal_order(milestone_id, milestone["amount_usd"])
    except PayPalError as exc:
        raise HTTPException(502, str(exc)) from exc
    order_id = order.get("id")
    links = order.get("links", [])
    if not isinstance(links, list):
        links = []
    approval_url = next(
        (link.get("href") for link in links
         if isinstance(link, dict) and link.get("rel") in ("payer-action", "approve")),
        None,
    )
    if not isinstance(order_id, str) or not order_id or not approval_url:
        raise HTTPException(502, "PayPal did not return a checkout link. Retry Sandbox checkout.")
    response = checkout_response({
        "order_id": order_id, "approval_url": approval_url, "amount_usd": milestone["amount_usd"]
    })
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        require_checkout_milestone(db, milestone_id)
        # Another request may have saved the order while PayPal was responding.
        existing = db.execute(
            "SELECT * FROM paypal_orders WHERE milestone_id = ?", (milestone_id,)
        ).fetchone()
        if existing:
            return checkout_response(existing)
        db.execute("""
            INSERT INTO paypal_orders (milestone_id, order_id, approval_url, amount_usd)
            VALUES (?, ?, ?, ?)
        """, (milestone_id, order_id, approval_url, milestone["amount_usd"]))
        record_activity(db, milestone_id, "checkout_created", "PayPal Sandbox checkout created")
    return response


def paid_response(payment, already_paid=False):
    return {
        "status": "paid", "milestone_id": payment["milestone_id"],
        "order_id": payment["order_id"], "capture_id": payment["capture_id"],
        "amount_usd": payment["amount_usd"], "already_paid": already_paid,
    }


def verified_capture(result, payment):
    units = result.get("purchase_units")
    if not isinstance(units, list) or len(units) != 1 or not isinstance(units[0], dict):
        return None
    payments = units[0].get("payments")
    captures = payments.get("captures") if isinstance(payments, dict) else None
    if not isinstance(captures, list) or len(captures) != 1 or not isinstance(captures[0], dict):
        return None
    capture = captures[0]
    amount = capture.get("amount")
    if (
        result.get("id") != payment["order_id"]
        or result.get("status") != "COMPLETED"
        or capture.get("status") != "COMPLETED"
        or not isinstance(capture.get("id"), str) or not capture["id"]
        or not isinstance(amount, dict)
        or amount.get("currency_code") != "USD"
        or amount.get("value") != f"{payment['amount_usd']:.2f}"
    ):
        return None
    return capture


@app.post("/api/milestones/{milestone_id}/capture")
def capture_payment(milestone_id: int, x_client_pin: str = Header(default="")):
    verify_client_pin(x_client_pin)
    with database() as db:
        payment = db.execute(
            "SELECT * FROM paypal_orders WHERE milestone_id = ?", (milestone_id,)
        ).fetchone()
        if payment is None:
            raise HTTPException(404, "Create a PayPal Sandbox checkout before verifying payment.")
        if payment["status"] == "paid":
            return paid_response(payment, already_paid=True)
        require_checkout_milestone(db, milestone_id)
    try:
        result = capture_paypal_order(payment["order_id"])
    except PayPalError as exc:
        raise HTTPException(502, str(exc)) from exc
    capture = verified_capture(result, payment)
    if capture is None:
        raise HTTPException(502, "PayPal has not confirmed the expected USD payment as completed. No local payment status was changed; verify the Sandbox order before retrying capture.")
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute(
            "SELECT * FROM paypal_orders WHERE milestone_id = ?", (milestone_id,)
        ).fetchone()
        if current is None or current["order_id"] != payment["order_id"]:
            raise HTTPException(409, "Checkout changed during verification. Refresh the dashboard.")
        if current["status"] == "paid":
            return paid_response(current, already_paid=True)
        require_checkout_milestone(db, milestone_id)
        db.execute("""
            UPDATE paypal_orders SET status = 'paid', capture_id = ? WHERE milestone_id = ?
        """, (capture["id"], milestone_id))
        db.execute("UPDATE milestones SET status = 'paid' WHERE id = ?", (milestone_id,))
        record_activity(db, milestone_id, "payment_captured", "PayPal Sandbox payment verified and captured")
    return paid_response({**dict(payment), "capture_id": capture["id"]})


def paypal_landing(cancelled, token, milestone_id):
    # The callback only restores dashboard context; it never confirms payment.
    with database() as db:
        if token:
            milestone = db.execute("""
                SELECT m.id, m.project_id FROM milestones m
                JOIN paypal_orders p ON p.milestone_id = m.id WHERE p.order_id = ?
            """, (token,)).fetchone()
        elif milestone_id is not None:
            milestone = db.execute("SELECT id, project_id FROM milestones WHERE id = ?", (milestone_id,)).fetchone()
        else:
            milestone = None
    params = {"paypal": "cancelled" if cancelled else "returned"}
    if milestone:
        params.update(project_id=milestone["project_id"], milestone_id=milestone["id"])
    dashboard_url = escape("/?" + urlencode(params) + "#payments", quote=True)
    title = "Sandbox checkout cancelled" if cancelled else "Returned from PayPal Sandbox"
    message = (
        "Checkout was cancelled. No payment status was changed. You can reopen checkout from the dashboard."
        if cancelled else
        "Buyer approval alone does not mark this milestone paid. Return to the dashboard, select Verify &amp; Capture Payment, and enter your Client Demo PIN."
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{title} — TrustPay AI</title>
    <style>body{{font:17px/1.6 system-ui;background:#f6f8fc;color:#18243a;margin:0;padding:8vh 24px}}
    main{{max-width:640px;margin:auto;padding:32px;background:white;border-radius:16px}}
    a{{display:inline-block;background:#476df5;color:white;padding:12px 20px;border-radius:8px;text-decoration:none}}</style>
    </head><body><main><p>TrustPay AI · Sandbox only</p><h1>{title}</h1><p>{message}</p>
    <a href="{dashboard_url}">Return to dashboard</a></main></body></html>"""


@app.get("/api/paypal/return", response_class=HTMLResponse)
def paypal_return(token: str | None = None, milestone_id: int | None = None):
    return paypal_landing(False, token, milestone_id)


@app.get("/api/paypal/cancel", response_class=HTMLResponse)
def paypal_cancel(token: str | None = None, milestone_id: int | None = None):
    return paypal_landing(True, token, milestone_id)


@app.get("/", include_in_schema=False)
def dashboard():
    frontend = (
        Path(__file__).resolve().parents[1]
        / "frontend"
        / "index.html"
    )
    return FileResponse(frontend)


@app.get("/api/projects/{project_id}/activity")
def get_project_activity(project_id: int):
    with database() as db:
        project = db.execute(
            "SELECT id FROM projects WHERE id = ?",
            (project_id,)
        ).fetchone()

        if project is None:
            raise HTTPException(404, "Project not found")

        rows = db.execute("""
            SELECT
                a.id,
                a.milestone_id,
                m.title AS milestone_title,
                a.event_type,
                a.details,
                a.created_at
            FROM activity_log a
            JOIN milestones m
                ON a.milestone_id = m.id
            WHERE m.project_id = ?
            ORDER BY a.id DESC
            LIMIT 50
        """, (project_id,)).fetchall()

    return [dict(row) for row in rows]
