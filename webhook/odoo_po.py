"""Draft PO creation against Odoo from the webhook.

Given a vendor_id and a list of product.product (variant) IDs, fetch each
product's primary supplierinfo for THIS vendor, pull the default order qty
and unit price from the Purchase tab, and create a single draft
purchase.order with one line per product.
"""
from __future__ import annotations

import logging
import xmlrpc.client
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


class POCreateError(Exception):
    pass


@dataclass
class OdooConfig:
    url: str
    database: str
    username: str
    api_key: str


@dataclass
class POLineSpec:
    product_id: int
    qty: float
    price_unit: float
    description: str


@dataclass
class POCreateResult:
    po_id: int
    po_name: str
    line_count: int
    odoo_url: str  # ready-to-redirect URL

    @property
    def web_url(self) -> str:
        return self.odoo_url


def _connect(cfg: OdooConfig):
    common = xmlrpc.client.ServerProxy(
        f"{cfg.url}/xmlrpc/2/common", allow_none=True
    )
    uid = common.authenticate(cfg.database, cfg.username, cfg.api_key, {})
    if not uid:
        raise POCreateError("Odoo authentication failed")
    models = xmlrpc.client.ServerProxy(
        f"{cfg.url}/xmlrpc/2/object", allow_none=True
    )
    return uid, models


def _execute(cfg, uid, models, model: str, method: str, args, kwargs=None):
    try:
        return models.execute_kw(
            cfg.database, uid, cfg.api_key, model, method, args, kwargs or {}
        )
    except xmlrpc.client.Fault as e:
        raise POCreateError(f"Odoo error {model}.{method}: {e.faultString}") from e


def _fetch_line_specs(
    cfg: OdooConfig, uid: int, models, vendor_id: int, product_ids: list[int]
) -> list[POLineSpec]:
    """For each product, find the supplierinfo row matching this vendor and
    build a POLineSpec from min_qty + price. Falls back to default qty=1
    and price=0 if no matching supplierinfo is found (rare but handled).
    """
    if not product_ids:
        return []

    products = _execute(cfg, uid, models, "product.product", "search_read",
        [[("id", "in", product_ids)]],
        {"fields": ["id", "default_code", "name", "product_tmpl_id",
                    "seller_ids", "description_purchase"]},
    )

    # Batch-fetch all supplierinfo records referenced
    all_seller_ids: set[int] = set()
    for p in products:
        for sid in p.get("seller_ids") or []:
            all_seller_ids.add(sid)
    suppliers = _execute(cfg, uid, models, "product.supplierinfo", "search_read",
        [[("id", "in", list(all_seller_ids))]],
        {"fields": ["id", "partner_id", "product_id", "product_tmpl_id",
                    "min_qty", "price", "sequence"]},
    ) if all_seller_ids else []

    suppliers_by_id = {s["id"]: s for s in suppliers}

    specs: list[POLineSpec] = []
    for p in products:
        # Filter to supplierinfo rows where partner_id matches this vendor
        candidates = []
        for sid in p.get("seller_ids") or []:
            s = suppliers_by_id.get(sid)
            if s and s.get("partner_id") and s["partner_id"][0] == vendor_id:
                candidates.append(s)

        if not candidates:
            # Fall back to qty=1, price=0 so the line still gets created
            qty = 1.0
            price = 0.0
            logger.warning(
                "No supplierinfo for product %s and vendor %s, using qty=1 price=0",
                p.get("default_code"), vendor_id,
            )
        else:
            # Prefer variant-specific over template-wide
            variant_specific = [c for c in candidates if c.get("product_id") and c["product_id"][0] == p["id"]]
            picked = sorted(variant_specific or candidates, key=lambda c: c.get("sequence") or 0)[0]
            qty = picked.get("min_qty") or 1.0
            price = picked.get("price") or 0.0

        # Description matches Odoo's UI behavior: "[SKU] Name" on the first line,
        # then the Purchase Description (from the product's Purchase tab) on
        # subsequent lines if present.
        base_desc = f"[{p.get('default_code') or ''}] {p.get('name') or ''}".strip()
        purchase_desc = (p.get("description_purchase") or "").strip()
        full_desc = f"{base_desc}\n{purchase_desc}" if purchase_desc else base_desc

        specs.append(POLineSpec(
            product_id=p["id"],
            qty=qty,
            price_unit=price,
            description=full_desc,
        ))
    return specs


def create_draft_po(
    cfg: OdooConfig, vendor_id: int, product_ids: list[int]
) -> POCreateResult:
    """Create the draft PO and return enough info to redirect to it."""
    uid, models = _connect(cfg)

    line_specs = _fetch_line_specs(cfg, uid, models, vendor_id, product_ids)
    if not line_specs:
        raise POCreateError(
            "No valid product lines for this vendor (empty product_ids?)"
        )

    # Fetch vendor defaults that Odoo would normally onchange-populate from
    # partner_id but doesn't fire over XML-RPC create.
    vendor = _execute(cfg, uid, models, "res.partner", "read",
                       [[vendor_id]],
                       {"fields": ["property_supplier_payment_term_id"]})
    payment_term_id = None
    if vendor and vendor[0].get("property_supplier_payment_term_id"):
        payment_term_id = vendor[0]["property_supplier_payment_term_id"][0]

    # Build the create payload. Order lines go in via the (0, 0, vals) syntax
    # which tells Odoo to create new linked records.
    order_lines = [
        (0, 0, {
            "product_id": spec.product_id,
            "product_qty": spec.qty,
            "price_unit": spec.price_unit,
            "name": spec.description,
        })
        for spec in line_specs
    ]

    vals = {
        "partner_id": vendor_id,
        "order_line": order_lines,
        "origin": "Auto-generated: Replenishment Digest",
    }
    if payment_term_id:
        vals["payment_term_id"] = payment_term_id

    po_id = _execute(cfg, uid, models, "purchase.order", "create", [vals])

    # Read back the auto-generated name (e.g. "P00042")
    po = _execute(cfg, uid, models, "purchase.order", "read",
                   [[po_id]], {"fields": ["name"]})
    po_name = po[0]["name"] if po else f"PO #{po_id}"

    odoo_url = f"{cfg.url}/odoo/purchase/{po_id}"

    logger.info("Created draft PO %s (id=%d) with %d lines for vendor=%d",
                po_name, po_id, len(line_specs), vendor_id)

    return POCreateResult(
        po_id=po_id,
        po_name=po_name,
        line_count=len(line_specs),
        odoo_url=odoo_url,
    )
