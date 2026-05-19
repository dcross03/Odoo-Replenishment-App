"""Alert categorization.

Each Buy-route product is categorized into one of:
    - URGENT  — months_of_stock < lead_time_months + urgent_buffer
    - ADDON   — months_of_stock < lead_time_months + addon_buffer (not urgent)
    - NONE    — months_of_stock is fine relative to lead time

Independently, every product is flagged for the "low stock catchall" if
forecasted qty < low_stock_threshold (default 500). The catchall is
surfaced two ways in the email:
    - per-vendor: if a vendor already has URGENT items, include their
      under-500 items in the same email so they can be added to the PO
    - orphan: items whose vendor doesn't appear in the urgent list at all
      get listed in a single "Low Stock — Catchall" section in the master
      digest

We also support items with no vendor assigned — they go into an
"Unassigned Vendor" bucket in the master digest.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from app.odoo.queries import Product
from app.services.velocity import Velocity

logger = logging.getLogger(__name__)


class Tier(str, Enum):
    URGENT = "urgent"
    ADDON = "addon"
    NONE = "none"


@dataclass
class AlertRow:
    """One product's full alert profile."""
    product: Product
    velocity: Velocity
    months_of_stock: Optional[float]   # None if velocity is 0
    lead_time_months: float
    tier: Tier
    is_low_stock: bool                 # virtual_available < threshold
    is_critical: bool                  # virtual_available < 0 (already over-sold)
    suggested_order_qty: float         # primary supplierinfo.min_qty
    suggested_total_price: float       # qty * supplierinfo.price

    @property
    def has_vendor(self) -> bool:
        return self.product.primary_supplierinfo is not None

    @property
    def vendor_name(self) -> str:
        si = self.product.primary_supplierinfo
        return si.vendor_name if si else "Unassigned Vendor"

    @property
    def vendor_id(self) -> Optional[int]:
        si = self.product.primary_supplierinfo
        return si.vendor_id if si else None


INTERNAL_PRODUCTION_VENDOR_NAMES = {
    "sun company, inc.",
    "sun company, inc",
    "sun company",
    "big discoveries",
}


def _is_internal_production(row_vendor_name: str) -> bool:
    """Return True if this vendor is actually internal production (not a
    real purchase vendor). We exclude these from alerts because they
    should be on the Manufacture route, not Buy.
    """
    return (row_vendor_name or "").strip().lower() in INTERNAL_PRODUCTION_VENDOR_NAMES


def categorize(
    products: list[Product],
    velocities: dict[int, Velocity],
    urgent_buffer_months: float,
    addon_buffer_months: float,
    low_stock_threshold: float,
) -> tuple[list[AlertRow], list[Product]]:
    """Return (rows, internal_production_products).

    Rows contain the categorized buy-route items. Internal-production
    products are excluded from rows and returned separately so the run
    script can log them as a "needs route cleanup" warning.
    """
    rows: list[AlertRow] = []
    internal: list[Product] = []
    for p in products:
        v = velocities.get(p.id) or Velocity()

        si = p.primary_supplierinfo

        # Skip items whose "vendor" is actually internal production
        if si and _is_internal_production(si.vendor_name):
            internal.append(p)
            continue

        # Lead time in months. If no vendor assigned, treat as 0 (still
        # triggers on extreme low stock, vendor-less products get bucketed
        # into the Unassigned section anyway).
        lead_time_months = (si.lead_time_days / 30.0) if si else 0.0

        if v.monthly_total > 0:
            mos = p.virtual_available / v.monthly_total
        else:
            mos = None

        # Determine tier
        urgent_thresh = lead_time_months + urgent_buffer_months
        addon_thresh = lead_time_months + addon_buffer_months
        if mos is None:
            tier = Tier.NONE
        elif mos < urgent_thresh:
            tier = Tier.URGENT
        elif mos < addon_thresh:
            tier = Tier.ADDON
        else:
            tier = Tier.NONE

        is_low_stock = p.virtual_available < low_stock_threshold
        is_critical = p.virtual_available < 0

        order_qty = si.min_qty if si else 0.0
        rows.append(AlertRow(
            product=p,
            velocity=v,
            months_of_stock=mos,
            lead_time_months=lead_time_months,
            tier=tier,
            is_low_stock=is_low_stock,
            is_critical=is_critical,
            suggested_order_qty=order_qty,
            suggested_total_price=(order_qty * (si.price if si else 0.0)),
        ))
    return rows, internal


@dataclass
class VendorDigest:
    """All AlertRows for a single vendor."""
    vendor_id: int
    vendor_name: str
    urgent: list[AlertRow]
    addon: list[AlertRow]
    low_stock_extras: list[AlertRow]  # vendor's low-stock items not already in urgent/addon

    @property
    def has_actionable_items(self) -> bool:
        return bool(self.urgent)

    @property
    def all_po_candidates(self) -> list[AlertRow]:
        """Items to include on the draft PO: urgent + addon (NOT catchall extras)."""
        return self.urgent + self.addon


def group_by_vendor(rows: list[AlertRow]) -> tuple[list[VendorDigest], list[AlertRow], list[AlertRow]]:
    """Split rows into:
        1. List of VendorDigests for vendors with URGENT items
        2. Orphan low-stock items (vendor has no urgent, item is low-stock)
        3. Unassigned-vendor items (no vendor at all, any tier or low-stock)
    """
    rows_by_vendor: dict[int, list[AlertRow]] = {}
    unassigned: list[AlertRow] = []

    for r in rows:
        if not r.has_vendor:
            # Only surface unassigned items that are urgent or low-stock
            if r.tier == Tier.URGENT or r.is_low_stock:
                unassigned.append(r)
            continue
        rows_by_vendor.setdefault(r.vendor_id, []).append(r)

    # Vendors with at least one urgent item -> VendorDigest
    digests: list[VendorDigest] = []
    orphan_low_stock: list[AlertRow] = []
    for vendor_id, vendor_rows in rows_by_vendor.items():
        urgent = [r for r in vendor_rows if r.tier == Tier.URGENT]
        addon = [r for r in vendor_rows if r.tier == Tier.ADDON]
        low_stock_extras = [
            r for r in vendor_rows
            if r.is_low_stock and r.tier == Tier.NONE
        ]
        if urgent:
            vendor_name = vendor_rows[0].vendor_name
            digests.append(VendorDigest(
                vendor_id=vendor_id,
                vendor_name=vendor_name,
                urgent=urgent,
                addon=addon,
                low_stock_extras=low_stock_extras,
            ))
        else:
            # No urgent for this vendor: any low-stock items become orphan
            orphan_low_stock.extend(
                r for r in vendor_rows if r.is_low_stock
            )

    # Sort digests by vendor name, items within each by months-of-stock
    digests.sort(key=lambda d: d.vendor_name.lower())
    for d in digests:
        d.urgent.sort(key=lambda r: (r.months_of_stock or 0.0))
        d.addon.sort(key=lambda r: (r.months_of_stock or 999.0))
        d.low_stock_extras.sort(key=lambda r: r.product.virtual_available)
    # Orphan low-stock: vendor name first (alphabetical), then SKU A-Z
    orphan_low_stock.sort(key=lambda r: (r.vendor_name.lower(), r.product.default_code))
    unassigned.sort(key=lambda r: r.product.virtual_available)

    return digests, orphan_low_stock, unassigned
