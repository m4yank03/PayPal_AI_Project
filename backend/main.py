from pathlib import Path
import sqlite3
from contextlib import contextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import json
from ai.milestone_generator import generate_milestones
from ai.work_reviewer import review_work
from paypal.client import (
    create_paypal_order,
    capture_paypal_order,
    PayPalError,
)
from fastapi.responses import HTMLResponse
from fastapi.responses import FileResponse

DB = Path(__file__).resolve().parent / 'trustpay.db'
app = FastAPI(title='TrustPay AI', version='0.1.0')

@contextmanager
def database():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

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

@app.post("/api/milestones/{milestone_id}/approve")
def approve_milestone(milestone_id: int):
    with database() as db:
        milestone = db.execute(
            "SELECT * FROM milestones WHERE id = ?",
            (milestone_id,)
        ).fetchone()

        if milestone is None:
            raise HTTPException(404, "Milestone not found")

        if milestone["status"] == "approved":
            return {
                "milestone_id": milestone_id,
                "status": "approved"
            }

        if milestone["status"] != "pending":
            raise HTTPException(
                409, "Milestone cannot be approved"
            )

        review_count = db.execute("""
            SELECT COUNT(*)
            FROM reviews
            JOIN submissions ON
                reviews.submission_id = submissions.id
            WHERE submissions.milestone_id = ?
        """, (milestone_id,)).fetchone()[0]

        if review_count == 0:
            raise HTTPException(
                409, "Milestone needs an AI review first"
            )

        db.execute(
            "UPDATE milestones SET status = ? WHERE id = ?",
            ("approved", milestone_id)
        )

    return {
        "milestone_id": milestone_id,
        "status": "approved"
    }


@app.post("/api/milestones/{milestone_id}/checkout")
def start_checkout(milestone_id: int):
    with database() as db:
        milestone = db.execute(
            "SELECT * FROM milestones WHERE id = ?",
            (milestone_id,)
        ).fetchone()

        if milestone is None:
            raise HTTPException(404, "Milestone not found")

        if milestone["status"] == "paid":
            raise HTTPException(409, "Already paid")

        existing = db.execute(
            "SELECT * FROM paypal_orders WHERE milestone_id = ?",
            (milestone_id,)
        ).fetchone()

    if existing:
        return {
            "order_id": existing["order_id"],
            "approval_url": existing["approval_url"],
            "amount_usd": existing["amount_usd"]
        }

    if milestone["status"] != "approved":
        raise HTTPException(
            409, "Client must approve milestone first"
        )

    try:
        order = create_paypal_order(
            milestone_id,
            milestone["amount_usd"]
        )
    except PayPalError as exc:
        print(f"PayPal order error: {exc}")
        raise HTTPException(
            502, "PayPal order creation failed"
        ) from exc

    order_id = order.get("id")

    approval_url = next(
        (
            link["href"]
            for link in order.get("links", [])
            if link.get("rel") in ("payer-action", "approve")
        ),
        None
    )

    if not order_id or not approval_url:
        raise HTTPException(
            502, "PayPal returned no approval link"
        )

    with database() as db:
        db.execute("""
            INSERT INTO paypal_orders
            (milestone_id, order_id, approval_url, amount_usd)
            VALUES (?, ?, ?, ?)
        """, (
            milestone_id,
            order_id,
            approval_url,
            milestone["amount_usd"]
        ))

    return {
        "order_id": order_id,
        "approval_url": approval_url,
        "amount_usd": milestone["amount_usd"],
        "status": "awaiting_buyer_approval"
    }


@app.post("/api/milestones/{milestone_id}/capture")
def capture_payment(milestone_id: int):
    with database() as db:
        payment = db.execute(
            "SELECT * FROM paypal_orders WHERE milestone_id = ?",
            (milestone_id,)
        ).fetchone()

    if payment is None:
        raise HTTPException(404, "Checkout not created")

    if payment["status"] == "paid":
        return {
            "status": "paid",
            "capture_id": payment["capture_id"],
            "already_paid": True
        }

    try:
        result = capture_paypal_order(payment["order_id"])
    except PayPalError as exc:
        print(f"PayPal capture error: {exc}")
        raise HTTPException(
            502, "Payment capture failed"
        ) from exc

    units = result.get("purchase_units", [])
    captures = (
        units[0].get("payments", {}).get("captures", [])
        if units else []
    )

    capture = next(
        (c for c in captures
         if c.get("status") == "COMPLETED"),
        None
    )

    expected_amount = f"{payment['amount_usd']:.2f}"

    if (
        result.get("id") != payment["order_id"]
        or result.get("status") != "COMPLETED"
        or capture is None
        or not capture.get("id")
        or capture.get("amount", {}).get("currency_code") != "USD"
        or capture.get("amount", {}).get("value") != expected_amount
    ):
        raise HTTPException(
            502, "Payment not confirmed as completed"
        )

    with database() as db:
        db.execute("""
            UPDATE paypal_orders
            SET status = 'paid', capture_id = ?
            WHERE milestone_id = ?
        """, (capture["id"], milestone_id))

        db.execute("""
            UPDATE milestones
            SET status = 'paid'
            WHERE id = ?
        """, (milestone_id,))

    return {
        "status": "paid",
        "milestone_id": milestone_id,
        "order_id": payment["order_id"],
        "capture_id": capture["id"],
        "amount_usd": payment["amount_usd"]
    }


@app.get("/api/paypal/return", response_class=HTMLResponse)
def paypal_return():
    return """
    <h2>PayPal Sandbox approval received</h2>
    <p>Return to the API docs and run the
    milestone capture endpoint to verify payment.</p>
    <a href="/docs">Open TrustPay API Docs</a>
    """


@app.get("/api/paypal/cancel", response_class=HTMLResponse)
def paypal_cancel():
    return """
    <h2>Sandbox checkout cancelled</h2>
    <p>No payment has been marked as paid.</p>
    <a href="/docs">Open TrustPay API Docs</a>
    """

@app.get("/", include_in_schema=False)
def dashboard():
    frontend = (
        Path(__file__).resolve().parents[1]
        / "frontend"
        / "index.html"
    )
    return FileResponse(frontend)