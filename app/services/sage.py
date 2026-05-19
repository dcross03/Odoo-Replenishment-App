"""Sage historical sales history loader.

The Sage data is a 12-month total per SKU (May 2025 — April 2026). It feeds
the gradual-blend velocity calculation that powers replenishment alerts
until Odoo accumulates a full 52 weeks of post-cutover sales.

If the Sage SKU naming diverges from Odoo's `default_code`, drop a two-column
mapping file (sage_to_odoo_sku_map.xlsx) alongside the history file with
columns: Sage_SKU, Odoo_SKU. As of the initial validation pass all 256
Sage SKUs match Odoo exactly, so the map is optional.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from app.config import SAGE_HISTORY_PATH, SAGE_SKU_MAP_PATH

logger = logging.getLogger(__name__)


def load_sage_weekly_velocity() -> dict[str, float]:
    """Returns a dict of Odoo SKU -> Sage weekly velocity (units/week).

    Computed as: Sage 12-month total / 52.

    Any SKU rename map at SAGE_SKU_MAP_PATH is applied here, so the returned
    dict is keyed by *Odoo* SKU even when the source row used a Sage SKU.
    """
    history_path = Path(SAGE_HISTORY_PATH)
    if not history_path.exists():
        logger.warning("No Sage history at %s; Sage velocity will be empty", history_path)
        return {}

    df = pd.read_excel(history_path)
    # Tolerate either the canonical column names or the older "QtySold" name
    cols = {c.lower().strip(): c for c in df.columns}
    sku_col = cols.get("sku") or list(df.columns)[0]
    qty_col_candidates = ["qtysold", "qty_sold", "total units sold (may 2025 – april 2026)",
                          "total units sold (may 2025 - april 2026)", "qty"]
    qty_col = None
    for cand in qty_col_candidates:
        if cand in cols:
            qty_col = cols[cand]
            break
    if qty_col is None:
        qty_col = list(df.columns)[1]

    df = df[[sku_col, qty_col]].rename(columns={sku_col: "SKU", qty_col: "QtySold"})
    df["SKU"] = df["SKU"].astype(str).str.strip()
    df["QtySold"] = pd.to_numeric(df["QtySold"], errors="coerce")
    df = df.dropna(subset=["QtySold"])

    # Apply optional rename map
    rename_map: dict[str, str] = {}
    if Path(SAGE_SKU_MAP_PATH).exists():
        m = pd.read_excel(SAGE_SKU_MAP_PATH)
        m.columns = [c.strip() for c in m.columns]
        for _, row in m.iterrows():
            sage_sku = str(row.get("Sage_SKU") or "").strip()
            odoo_sku = str(row.get("Odoo_SKU") or "").strip()
            if sage_sku and odoo_sku:
                rename_map[sage_sku] = odoo_sku
        logger.info("Applied %d SKU rename(s) from sage_to_odoo_sku_map.xlsx", len(rename_map))

    out: dict[str, float] = {}
    for _, row in df.iterrows():
        sku = rename_map.get(row["SKU"], row["SKU"])
        weekly = float(row["QtySold"]) / 52.0
        # If multiple Sage SKUs collapse to the same Odoo SKU, sum them
        out[sku] = out.get(sku, 0.0) + weekly

    logger.info("Loaded Sage weekly velocity for %d SKUs", len(out))
    return out
