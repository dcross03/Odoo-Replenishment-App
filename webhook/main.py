"""FastAPI service for the 'Create Draft PO' email button.

Workflow:
    1. User clicks button in digest email -> hits /create-po?token=...
    2. We verify the signed token (issued by the run script)
    3. We call Odoo to create a draft PO with the urgent + addon items
       for that vendor
    4. We redirect the browser to the new PO in Odoo

Deploy to Render as a Web Service. Set env vars (see .env.template).
Render's free tier sleeps after 15 minutes of inactivity; first click after
a long idle has a ~30 second cold start, then it's instant. That's fine
for a weekly workflow.
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeSerializer

from odoo_po import OdooConfig, POCreateError, create_draft_po

# Load .env from project root if present (for local dev)
ROOT = Path(__file__).resolve().parent.parent
env_file = ROOT / ".env.production"
if env_file.exists():
    load_dotenv(env_file)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("webhook")

app = FastAPI(title="Odoo Replenishment Webhook")

# Tokens older than this are rejected. Long enough to handle clicking links
# from emails a few weeks old, short enough to prevent indefinite replay.
TOKEN_MAX_AGE_DAYS = 60


def _load_odoo_config() -> OdooConfig:
    missing = [k for k in
               ("ODOO_URL", "ODOO_DB", "ODOO_USERNAME", "ODOO_API_KEY")
               if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"Missing env vars: {missing}")
    return OdooConfig(
        url=os.environ["ODOO_URL"].rstrip("/"),
        database=os.environ["ODOO_DB"],
        username=os.environ["ODOO_USERNAME"],
        api_key=os.environ["ODOO_API_KEY"],
    )


def _verify_token(token: str) -> dict:
    secret = os.environ.get("WEBHOOK_SHARED_SECRET")
    if not secret:
        raise HTTPException(500, "Webhook misconfigured: missing shared secret")
    try:
        payload = URLSafeSerializer(secret, salt="create-po").loads(token)
    except BadSignature:
        raise HTTPException(400, "Invalid or tampered token")

    # Age check
    issued_str = payload.get("issued_at")
    if not issued_str:
        raise HTTPException(400, "Token missing issued_at")
    try:
        issued = date.fromisoformat(issued_str)
    except ValueError:
        raise HTTPException(400, "Token has malformed issued_at")
    if date.today() - issued > timedelta(days=TOKEN_MAX_AGE_DAYS):
        raise HTTPException(
            400,
            f"Token is older than {TOKEN_MAX_AGE_DAYS} days. "
            "Trigger a fresh digest run.",
        )

    vendor_id = payload.get("vendor_id")
    product_ids = payload.get("product_ids") or []
    if not vendor_id or not product_ids:
        raise HTTPException(400, "Token missing vendor_id or product_ids")
    return payload


def _error_page(title: str, detail: str) -> HTMLResponse:
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif;
        max-width: 560px; margin: 80px auto; padding: 24px;
        background: #f6f6f6; color: #1c1c1c; }}
.box {{ background: #fff; padding: 24px 28px; border-radius: 4px;
        border-left: 4px solid #b71c1c; }}
h1 {{ margin: 0 0 12px 0; font-size: 18px; color: #b71c1c; }}
p {{ margin: 8px 0; font-size: 14px; }}
code {{ background: #f0f0f0; padding: 2px 5px; border-radius: 2px;
        font-size: 12px; }}
</style></head>
<body>
<div class="box">
  <h1>{title}</h1>
  <p>{detail}</p>
  <p style="color:#666; font-size:12px; margin-top:18px;">
    If this keeps happening, check the Render service logs and re-run the
    replenishment script to generate a fresh email.
  </p>
</div>
</body></html>"""
    return HTMLResponse(html, status_code=400)


@app.get("/")
def health() -> dict:
    return {"status": "ok", "service": "odoo-replenishment-webhook"}


@app.get("/healthz")
def healthz() -> dict:
    """Render uses this for health checks."""
    return {"status": "ok"}


@app.get("/create-po")
def create_po(token: str = Query(..., description="Signed token from digest email")):
    """The endpoint hit by the email button."""
    try:
        payload = _verify_token(token)
    except HTTPException as e:
        return _error_page("Couldn't verify request", str(e.detail))

    vendor_id = payload["vendor_id"]
    product_ids = payload["product_ids"]

    try:
        odoo_cfg = _load_odoo_config()
    except RuntimeError as e:
        return _error_page("Webhook misconfigured", str(e))

    try:
        result = create_draft_po(odoo_cfg, vendor_id, product_ids)
    except POCreateError as e:
        logger.exception("PO creation failed")
        return _error_page("Couldn't create draft PO", str(e))

    logger.info("Redirecting to %s", result.web_url)
    return RedirectResponse(url=result.web_url, status_code=302)
