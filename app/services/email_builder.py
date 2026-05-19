"""Email builder.

Single consolidated digest email with all vendor sections + catchall + unassigned.
Each vendor section has its own signed "Create Draft PO" button.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Optional

from itsdangerous import URLSafeSerializer
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import Config
from app.services.alerts import AlertRow, VendorDigest

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "email_templates"


def _format_mos(mos: Optional[float]) -> str:
    if mos is None:
        return "—"
    return f"{mos:.1f}"


def _tags_html_for(r: AlertRow) -> str:
    """Status tags shown in the rightmost column of each row."""
    parts: list[str] = []
    if r.is_critical:
        parts.append('<span class="tag tag-critical">CRITICAL: BACKORDERED</span>')
    if r.tier.value == "urgent":
        parts.append('<span class="tag tag-urgent">URGENT</span>')
    elif r.tier.value == "addon":
        parts.append('<span class="tag tag-addon">ADD-ON</span>')
    if r.is_low_stock and r.tier.value == "none":
        parts.append('<span class="tag tag-lowstock">LOW STOCK</span>')
    return "".join(parts)


def _make_jinja_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.globals["fmt_mos"] = _format_mos
    env.globals["tags_for"] = _tags_html_for
    return env


def build_po_token(
    config: Config, vendor_id: int, product_ids: list[int]
) -> str:
    """Make a signed token for the 'Create Draft PO' link.

    The webhook validates the signature, then creates the PO from these
    product ids. Including the product list in the token means the webhook
    is stateless.
    """
    serializer = URLSafeSerializer(config.webhook_shared_secret, salt="create-po")
    payload = {
        "vendor_id": vendor_id,
        "product_ids": product_ids,
        "issued_at": date.today().isoformat(),
    }
    return serializer.dumps(payload)


def build_create_po_url(
    config: Config, vendor_id: int, product_ids: list[int]
) -> str:
    token = build_po_token(config, vendor_id, product_ids)
    return f"{config.webhook_base_url}/create-po?token={token}"


def render_digest(
    config: Config,
    run_date: date,
    n_weeks: int,
    digests: list[VendorDigest],
    orphan_low_stock: list[AlertRow],
    unassigned: list[AlertRow],
) -> str:
    """Render the consolidated digest email.

    Decorates each VendorDigest with `create_po_url` and `po_line_count`
    so the template can use them directly.
    """
    for d in digests:
        po_candidates = d.urgent + d.addon
        d.create_po_url = build_create_po_url(  # type: ignore[attr-defined]
            config, d.vendor_id, [r.product.id for r in po_candidates]
        )
        d.po_line_count = len(po_candidates)  # type: ignore[attr-defined]

    env = _make_jinja_env()
    tmpl = env.get_template("digest.html")
    return tmpl.render(
        run_date=run_date.strftime("%A, %B %d, %Y"),
        cutover_date=config.odoo_cutover_date.isoformat(),
        n_weeks=n_weeks,
        digests=digests,
        orphan_low_stock=orphan_low_stock,
        unassigned=unassigned,
        low_stock_threshold=config.low_stock_threshold,
    )


def send_email(config: Config, subject: str, html: str) -> bool:
    """Send via SendGrid. Returns True on 2xx."""
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail

    msg = Mail(
        from_email=config.email_from,
        to_emails=config.email_to,
        subject=subject,
        html_content=html,
    )
    sg = SendGridAPIClient(config.sendgrid_api_key)
    resp = sg.send(msg)
    ok = 200 <= resp.status_code < 300
    if ok:
        logger.info("Sent: %s (HTTP %d)", subject, resp.status_code)
    else:
        logger.error("SendGrid failed: HTTP %d for %s", resp.status_code, subject)
    return ok
