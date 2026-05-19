"""Cross-reference Sage SKUs against Odoo's product catalog.

Run any time to verify all Sage SKUs in data/sage/sage_sales_history.xlsx
have matching Odoo products. Prints a categorized report and writes a
detailed xlsx to output/.

Usage:
    python scripts/validate_skus.py
"""
from __future__ import annotations

import logging
import sys
from difflib import get_close_matches
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from app.config import SAGE_HISTORY_PATH, Config
from app.odoo.client import OdooClient


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    log = logging.getLogger("validate_skus")

    config = Config.load()
    client = OdooClient.from_config(config)

    sage_path = Path(SAGE_HISTORY_PATH)
    if not sage_path.exists():
        log.error("Sage history not found at %s", sage_path)
        return 1

    df_sage = pd.read_excel(sage_path)
    df_sage.columns = ["SKU", "QtySold"] + list(df_sage.columns[2:])
    df_sage["SKU"] = df_sage["SKU"].astype(str).str.strip()
    df_sage["QtySold"] = pd.to_numeric(df_sage["QtySold"], errors="coerce")
    df_sage = df_sage.dropna(subset=["QtySold"])

    log.info("Loaded %d Sage SKUs (%d total units)",
             len(df_sage), int(df_sage["QtySold"].sum()))

    log.info("Fetching Odoo products (all, incl. archived)...")
    odoo_raw = client.search_read(
        "product.product",
        domain=[("default_code", "!=", False)],
        fields=["id", "default_code", "name", "active"],
        include_archived=True,
    )
    log.info("  %d Odoo products with SKU", len(odoo_raw))

    odoo_skus_to_name = {
        (r.get("default_code") or "").strip(): (r.get("name") or "")
        for r in odoo_raw
    }
    odoo_skus = set(odoo_skus_to_name.keys())
    sage_skus = set(df_sage["SKU"])
    sage_qty = dict(zip(df_sage["SKU"], df_sage["QtySold"]))

    matched = sage_skus & odoo_skus
    missing = sorted(sage_skus - odoo_skus, key=lambda s: -sage_qty.get(s, 0))

    print(f"\nExact matches:        {len(matched)} / {len(sage_skus)}")
    print(f"Missing in Odoo:      {len(missing)}")

    if missing:
        print("\n=== Missing SKUs (sorted by qty desc) ===")
        rows = []
        for sku in missing:
            qty = int(sage_qty.get(sku, 0))
            fuzzy = get_close_matches(sku, odoo_skus, n=3, cutoff=0.6)
            prefix = [s for s in odoo_skus if s.startswith(sku) or sku.startswith(s)]
            sub = [s for s in odoo_skus if sku in s or (len(sku) > 3 and s in sku)]
            sugs = list(dict.fromkeys(fuzzy + prefix + sub))[:3]
            print(f"  {sku:25s} qty={qty:6d}  suggestions: "
                  f"{[f'{s} ({odoo_skus_to_name.get(s,'')[:30]})' for s in sugs]}")
            rows.append({
                "Sage_SKU": sku,
                "Qty_Sold": qty,
                "Suggestion_1": sugs[0] if sugs else "",
                "Suggestion_1_Name": odoo_skus_to_name.get(sugs[0], "") if sugs else "",
                "Suggestion_2": sugs[1] if len(sugs) > 1 else "",
                "Suggestion_3": sugs[2] if len(sugs) > 2 else "",
            })

        out_dir = Path(__file__).resolve().parent.parent / "output"
        out_dir.mkdir(exist_ok=True)
        out_path = out_dir / "missing_skus.xlsx"
        pd.DataFrame(rows).to_excel(out_path, index=False)
        print(f"\nWrote detailed report to {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
