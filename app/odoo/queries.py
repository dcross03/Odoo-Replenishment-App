"""All Odoo read operations for the replenishment app.

Centralizes every query so domain knowledge about field names, route IDs,
and state filters lives in one place. The downstream services (velocity,
alerts, email) consume the typed objects this module returns and don't
touch XML-RPC directly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from app.odoo.client import OdooClient

logger = logging.getLogger(__name__)

# The "Buy" route ID. Confirmed by inspecting stock.route in production.
BUY_ROUTE_ID = 6


@dataclass
class Vendor:
    """A vendor (res.partner) summary."""
    id: int
    name: str
    email: Optional[str] = None


@dataclass
class SupplierInfo:
    """A product.supplierinfo record — one product's vendor offering.

    A product can have multiple supplierinfo records (multiple vendors,
    or quantity-based pricing tiers). For the replenishment alert we use
    the first record by sequence (Odoo's "primary vendor" convention).
    """
    id: int
    product_template_id: int       # the template this offering is on
    product_variant_id: Optional[int]  # None if applies to all variants
    vendor_id: int
    vendor_name: str
    min_qty: float                 # what we treat as the default order qty
    price: float
    lead_time_days: int            # Odoo's `delay` field
    sequence: int


@dataclass
class Product:
    """A buy-route product, with everything we need to compute alerts."""
    id: int                        # product.product (variant) id
    template_id: int               # product.template id
    default_code: str              # SKU
    name: str
    virtual_available: float       # forecasted = on hand + incoming - outgoing
    qty_available: float           # on hand
    incoming_qty: float
    outgoing_qty: float
    has_buy_route: bool
    primary_supplierinfo: Optional[SupplierInfo] = None


@dataclass
class BomLine:
    """A single component line in a BOM."""
    component_product_id: int      # the variant referenced by the line
    qty_per_parent: float          # qty consumed per 1 unit of the parent BOM


@dataclass
class Bom:
    """A bill of materials, including its lines."""
    id: int
    parent_template_id: int        # the template this BOM produces
    parent_qty: float              # qty produced per BOM run (usually 1)
    bom_type: str                  # 'normal', 'phantom' (kit), 'subcontract'
    lines: list[BomLine] = field(default_factory=list)


@dataclass
class SaleLine:
    """A single sold line (filtered to confirmed orders only)."""
    product_id: int                # product.product (variant) id
    qty: float
    date_order: datetime


def fetch_buy_route_products(client: OdooClient) -> list[Product]:
    """Pull every product on the Buy route, with inventory + primary vendor.

    Filters:
      - Active products only (no archived)
      - Storable products only (is_storable=True) — skips Service items, fees,
        tooling line items, and any consumable that doesn't track inventory
      - Excludes any product tagged "Discontinued" (matched by tag name,
        case-insensitive, also catches the misspelled "Discountinued")
    """
    logger.info("Fetching Buy-route products from Odoo...")

    # First, look up tag IDs for any discontinued-style tag names so we can
    # exclude them. Matching by name (not id) so we're robust to the typo
    # currently in production ("Discountinued").
    discontinued_tag_ids = _find_discontinued_tag_ids(client)
    if discontinued_tag_ids:
        logger.info("  Excluding products with discontinued tag id(s): %s",
                    discontinued_tag_ids)

    domain = [
        ("active", "=", True),
        ("route_ids", "in", [BUY_ROUTE_ID]),
        ("default_code", "!=", False),
        ("is_storable", "=", True),
    ]
    if discontinued_tag_ids:
        domain.append(("product_tag_ids", "not in", discontinued_tag_ids))

    raw = client.search_read(
        "product.product",
        domain=domain,
        fields=[
            "id", "default_code", "name", "product_tmpl_id",
            "virtual_available", "qty_available", "incoming_qty", "outgoing_qty",
            "route_ids", "seller_ids",
        ],
    )
    logger.info("  %d buy-route storable products found", len(raw))

    # Collect supplierinfo ids across all products and batch-fetch
    all_seller_ids: set[int] = set()
    for p in raw:
        for sid in p.get("seller_ids") or []:
            all_seller_ids.add(sid)
    suppliers_by_id = _fetch_supplierinfo(client, list(all_seller_ids))

    products: list[Product] = []
    for p in raw:
        tmpl_id = p["product_tmpl_id"][0] if p.get("product_tmpl_id") else None
        if tmpl_id is None:
            continue

        primary_si = _pick_primary_supplierinfo(
            p.get("seller_ids") or [], suppliers_by_id, variant_id=p["id"]
        )

        products.append(Product(
            id=p["id"],
            template_id=tmpl_id,
            default_code=(p.get("default_code") or "").strip(),
            name=p.get("name") or "",
            virtual_available=p.get("virtual_available") or 0.0,
            qty_available=p.get("qty_available") or 0.0,
            incoming_qty=p.get("incoming_qty") or 0.0,
            outgoing_qty=p.get("outgoing_qty") or 0.0,
            has_buy_route=True,
            primary_supplierinfo=primary_si,
        ))
    return products


def _find_discontinued_tag_ids(client: OdooClient) -> list[int]:
    """Find any product.tag whose name looks like 'Discontinued' (handles
    the current misspelling 'Discountinued' too). Matching ilike '%isconti%'
    catches both 'Discontinued' and 'Discountinued' but nothing unrelated.
    """
    try:
        raw = client.search_read(
            "product.tag",
            domain=[("name", "ilike", "isconti")],
            fields=["id", "name"],
        )
        return [r["id"] for r in raw]
    except Exception as e:
        logger.warning("Could not look up discontinued tag(s): %s", e)
        return []


def _fetch_supplierinfo(
    client: OdooClient, ids: list[int]
) -> dict[int, SupplierInfo]:
    """Batch-fetch supplierinfo records by id."""
    if not ids:
        return {}
    raw = client.search_read(
        "product.supplierinfo",
        domain=[("id", "in", ids)],
        fields=[
            "id", "partner_id", "product_tmpl_id", "product_id",
            "min_qty", "price", "delay", "sequence",
        ],
    )
    out: dict[int, SupplierInfo] = {}
    for s in raw:
        if not s.get("partner_id"):
            continue
        out[s["id"]] = SupplierInfo(
            id=s["id"],
            product_template_id=s["product_tmpl_id"][0] if s.get("product_tmpl_id") else 0,
            product_variant_id=s["product_id"][0] if s.get("product_id") else None,
            vendor_id=s["partner_id"][0],
            vendor_name=s["partner_id"][1],
            min_qty=s.get("min_qty") or 0.0,
            price=s.get("price") or 0.0,
            lead_time_days=int(s.get("delay") or 0),
            sequence=int(s.get("sequence") or 0),
        )
    return out


def _pick_primary_supplierinfo(
    seller_ids: list[int],
    pool: dict[int, SupplierInfo],
    variant_id: int,
) -> Optional[SupplierInfo]:
    """Pick the lowest-sequence supplierinfo, preferring variant-specific over
    template-wide. Returns None if no vendor is assigned.
    """
    candidates = [pool[sid] for sid in seller_ids if sid in pool]
    if not candidates:
        return None
    # Prefer records that match this exact variant (vs. template-wide)
    variant_specific = [c for c in candidates if c.product_variant_id == variant_id]
    pool_to_use = variant_specific or candidates
    return sorted(pool_to_use, key=lambda c: c.sequence)[0]


def fetch_all_boms(client: OdooClient) -> list[Bom]:
    """Pull every BOM and its lines.

    Used for component demand roll-up: if a Buy-route component appears in
    a parent BOM, the parent's sales velocity translates into component
    demand.
    """
    logger.info("Fetching BOMs from Odoo...")
    bom_raw = client.search_read(
        "mrp.bom",
        domain=[],
        fields=["id", "product_tmpl_id", "product_id", "product_qty",
                "type", "bom_line_ids"],
    )

    # Batch-fetch all bom.line records
    all_line_ids: list[int] = []
    for b in bom_raw:
        all_line_ids.extend(b.get("bom_line_ids") or [])

    line_raw = client.search_read(
        "mrp.bom.line",
        domain=[("id", "in", all_line_ids)],
        fields=["id", "product_id", "product_qty", "bom_id"],
    ) if all_line_ids else []

    lines_by_bom: dict[int, list[BomLine]] = {}
    for l in line_raw:
        bom_id = l["bom_id"][0] if l.get("bom_id") else None
        if bom_id is None or not l.get("product_id"):
            continue
        lines_by_bom.setdefault(bom_id, []).append(BomLine(
            component_product_id=l["product_id"][0],
            qty_per_parent=l.get("product_qty") or 0.0,
        ))

    boms: list[Bom] = []
    for b in bom_raw:
        tmpl_id = b["product_tmpl_id"][0] if b.get("product_tmpl_id") else None
        if tmpl_id is None:
            continue
        boms.append(Bom(
            id=b["id"],
            parent_template_id=tmpl_id,
            parent_qty=b.get("product_qty") or 1.0,
            bom_type=b.get("type") or "normal",
            lines=lines_by_bom.get(b["id"], []),
        ))
    logger.info("  %d BOMs found, %d total lines", len(boms), len(line_raw))
    return boms


def fetch_sales_since(
    client: OdooClient, since: date
) -> list[SaleLine]:
    """Pull all confirmed sale order lines on/after `since`.

    Filters:
      - state = 'sale' (Odoo's confirmed-order state; 'done' was deprecated
        but kept here in case any old data exists)
      - excludes display-only lines (section headers, notes)
      - excludes lines with zero or negative qty
      - excludes orders whose state is 'cancel'
    """
    logger.info("Fetching sale order lines since %s...", since.isoformat())
    raw = client.search_read(
        "sale.order.line",
        domain=[
            ("state", "in", ["sale", "done"]),
            ("order_id.date_order", ">=", since.isoformat()),
            ("product_uom_qty", ">", 0),
            ("display_type", "in", [False, ""]),
        ],
        fields=["product_id", "product_uom_qty", "order_id"],
    )

    # Order date isn't on the line; pull a map of order_id -> date_order
    order_ids = sorted({r["order_id"][0] for r in raw if r.get("order_id")})
    orders_by_id: dict[int, datetime] = {}
    if order_ids:
        order_raw = client.search_read(
            "sale.order",
            domain=[("id", "in", order_ids)],
            fields=["id", "date_order"],
        )
        for o in order_raw:
            if o.get("date_order"):
                # Odoo returns ISO string; parse to datetime
                orders_by_id[o["id"]] = datetime.fromisoformat(o["date_order"])

    lines: list[SaleLine] = []
    for r in raw:
        if not r.get("product_id") or not r.get("order_id"):
            continue
        order_id = r["order_id"][0]
        date_order = orders_by_id.get(order_id)
        if date_order is None:
            continue
        lines.append(SaleLine(
            product_id=r["product_id"][0],
            qty=r.get("product_uom_qty") or 0.0,
            date_order=date_order,
        ))
    logger.info("  %d confirmed sale lines since cutover", len(lines))
    return lines


def fetch_vendors(client: OdooClient, vendor_ids: list[int]) -> dict[int, Vendor]:
    """Batch-fetch vendor (res.partner) details by id."""
    if not vendor_ids:
        return {}
    raw = client.search_read(
        "res.partner",
        domain=[("id", "in", vendor_ids)],
        fields=["id", "name", "email"],
    )
    return {
        r["id"]: Vendor(id=r["id"], name=r["name"], email=r.get("email") or None)
        for r in raw
    }
