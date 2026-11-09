"""Real Chromium, real dashboard/API, simulated PayPal and a minimal offline AG Grid adapter.

The adapter keeps payment tests independent of CDN availability; AG Grid itself
and Gemini model quality are outside this suite. No credentials or live API calls.
"""
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from conftest import APPROVAL_URL, HEADERS, ORDER_ID, PIN

ORIGIN = "http://trustpay.test"
GRID_ADAPTER = """
window.agGrid = {createGrid(target, options) {
  return {setGridOption(name, rows) {
    if (name !== 'rowData') return;
    target.replaceChildren();
    for (const row of rows) {
      const button = document.createElement('button');
      button.textContent = row.title + ' · ' + row.status;
      button.onclick = () => options.onRowClicked({data: row});
      target.appendChild(button);
    }
  }, exportDataAsCsv() {}};
}};
"""


@pytest.fixture
def dashboard(page, client, seeded, paypal):
    posts = []
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def route_request(route):
        request = route.request
        url = urlsplit(request.url)
        if request.url.startswith(ORIGIN):
            if request.method == "POST":
                posts.append(request)
            response = client.request(request.method, url.path + ("?" + url.query if url.query else ""),
                                      headers=request.headers, content=request.post_data)
            route.fulfill(status=response.status_code, body=response.content,
                          content_type=response.headers.get("content-type", "application/json"))
        elif url.hostname == "www.sandbox.paypal.com":
            route.fulfill(body=f'<a href="{ORIGIN}/api/paypal/return?token={ORDER_ID}">Approve in simulated Sandbox</a>', content_type="text/html")
        elif url.hostname == "cdn.jsdelivr.net":
            route.fulfill(body=GRID_ADAPTER, content_type="application/javascript")
        else:
            route.abort()

    page.route("**/*", route_request)
    page.goto(ORIGIN)
    expect(page.locator("#notice")).to_contain_text("connected successfully")
    yield posts
    assert errors == []


def dialogs(page, pin=PIN, cancel_confirm=False):
    def handle(dialog):
        if dialog.type == "confirm":
            dialog.dismiss() if cancel_confirm else dialog.accept()
        elif pin is None:
            dialog.dismiss()
        else:
            dialog.accept(pin)
    page.on("dialog", handle)


def ready_checkout(page, client, seeded):
    client.post(f"/api/milestones/{seeded[1]}/approve", headers=HEADERS)
    page.reload()
    expect(page.locator("#checkoutBtn")).to_be_enabled()


def test_complete_payment_flow_with_pin_and_return_context(page, dashboard, client, seeded):
    project, milestone = seeded
    dialogs(page)
    page.locator("#approveBtn").click()
    expect(page.locator("#notice")).to_contain_text("Milestone approved")
    page.locator("#checkoutBtn").click()
    expect(page).to_have_url(APPROVAL_URL)
    page.get_by_text("Approve in simulated Sandbox").click()
    expect(page.get_by_role("heading")).to_have_text("Returned from PayPal Sandbox")
    # A newer project would normally be selected first; the callback restores ours.
    client.post("/api/projects", json={"title": "Other project", "description": "Another active project", "budget_usd": 20})
    page.get_by_role("link", name="Return to dashboard").click()
    expect(page.locator("#notice")).to_contain_text("Buyer approval is not payment confirmation")
    expect(page.locator("#projectSelect")).to_have_value(str(project))
    expect(page.locator("#details")).to_contain_text("Demo milestone")
    assert not any(request.url.endswith("/capture") for request in dashboard)
    page.locator("#captureBtn").click()
    expect(page.locator("#notice")).to_contain_text("Capture ID: TEST-CAPTURE")
    expect(page.locator("#details")).to_contain_text("paid")
    expect(page.locator("#activityLog .review-card")).to_have_count(3)
    assert [r.url.rsplit("/", 1)[1] for r in dashboard] == ["approve", "checkout", "capture"]
    for request in dashboard:
        assert request.headers["x-client-pin"] == PIN
        assert PIN not in request.url and request.post_data is None
    assert page.evaluate("JSON.stringify({...localStorage}) + JSON.stringify({...sessionStorage})") == "{}{}"


@pytest.mark.parametrize("button", ["approveBtn", "checkoutBtn", "captureBtn"])
@pytest.mark.parametrize("pin", [None, ""])
def test_cancelled_or_empty_pin_sends_no_request(page, dashboard, client, seeded, button, pin):
    if button != "approveBtn": ready_checkout(page, client, seeded)
    dialogs(page, pin)
    page.locator("#" + button).click()
    expect(page.locator("#notice")).to_contain_text("Action cancelled" if pin is None else "Enter your Client Demo PIN")
    assert dashboard == []


def test_confirmation_cancel_is_not_success(page, dashboard):
    dialogs(page, cancel_confirm=True)
    page.locator("#approveBtn").click()
    expect(page.locator("#notice")).to_contain_text("Action cancelled")
    assert dashboard == []


def test_wrong_pin_displays_actionable_error(page, dashboard):
    dialogs(page, "incorrect")
    page.locator("#approveBtn").click()
    expect(page.locator("#notice")).to_contain_text("403: Invalid client demo PIN")
    expect(page.locator("#approveBtn")).to_be_enabled()


@pytest.mark.parametrize("url", ["https://www.paypal.com/checkoutnow", "https://www.sandbox.paypal.com.evil.example/", "https://user:pass@www.sandbox.paypal.com/", "http://www.sandbox.paypal.com/"])
def test_browser_rejects_non_sandbox_approval(page, dashboard, client, seeded, url):
    ready_checkout(page, client, seeded)
    page.route("**/checkout", lambda route: route.fulfill(json={"approval_url": url}))
    dialogs(page)
    page.locator("#checkoutBtn").click()
    expect(page.locator("#notice")).to_contain_text("Only PayPal Sandbox")
    expect(page).to_have_url(ORIGIN + "/")
    expect(page.locator("#approvalLink")).to_be_hidden()


def test_cancellation_returns_to_dashboard_without_capture(page, dashboard, seeded, client):
    ready_checkout(page, client, seeded)
    client.post(f"/api/milestones/{seeded[1]}/checkout", headers=HEADERS)
    page.goto(f"{ORIGIN}/api/paypal/cancel?token={ORDER_ID}")
    page.get_by_role("link", name="Return to dashboard").click()
    expect(page.locator("#notice")).to_contain_text("checkout cancelled")
    expect(page.locator("#details")).to_contain_text("approved")
    assert dashboard == []


def test_saved_capture_survives_dashboard_refresh_error(page, dashboard, client, seeded):
    ready_checkout(page, client, seeded)
    client.post(f"/api/milestones/{seeded[1]}/checkout", headers=HEADERS)
    page.route("**/milestones", lambda route: route.fulfill(status=503, json={"detail": "Refresh unavailable"}))
    dialogs(page)
    page.locator("#captureBtn").click()
    expect(page.locator("#notice")).to_contain_text("Capture ID: TEST-CAPTURE")
    expect(page.locator("#notice")).to_contain_text("Dashboard refresh failed")


def test_connection_error_is_readable(page, dashboard):
    page.route("**/approve", lambda route: route.abort())
    dialogs(page)
    page.locator("#approveBtn").click()
    expect(page.locator("#notice")).to_contain_text("Cannot reach TrustPay")
    expect(page.locator("#approveBtn")).to_be_enabled()


def test_validation_errors_show_field_and_message(page, dashboard):
    page.route("**/approve", lambda route: route.fulfill(status=422, json={"detail": [
        {"loc": ["body", "budget_usd"], "msg": "Input should be greater than 0"}
    ]}))
    dialogs(page)
    page.locator("#approveBtn").click()
    expect(page.locator("#notice")).to_contain_text("budget_usd: Input should be greater than 0")
