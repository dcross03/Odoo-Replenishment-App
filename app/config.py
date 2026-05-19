"""Configuration loader.

Reads from .env.production locally; reads from os.environ in GitHub Actions
(where the secrets are injected as env vars).
"""
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAGE_HISTORY_PATH = PROJECT_ROOT / "data" / "sage" / "sage_sales_history.xlsx"
SAGE_SKU_MAP_PATH = PROJECT_ROOT / "data" / "sage" / "sage_to_odoo_sku_map.xlsx"


def _load_env() -> None:
    """Load .env.production if present. Falls back to bare os.environ."""
    env_file = PROJECT_ROOT / ".env.production"
    if env_file.exists():
        load_dotenv(env_file)


@dataclass(frozen=True)
class Config:
    odoo_url: str
    odoo_db: str
    odoo_username: str
    odoo_api_key: str
    sendgrid_api_key: str
    email_from: str
    email_to: str
    webhook_base_url: str
    webhook_shared_secret: str
    odoo_cutover_date: date
    urgent_buffer_months: float
    addon_buffer_months: float
    low_stock_threshold: float

    @classmethod
    def load(cls) -> "Config":
        _load_env()

        def required(name: str) -> str:
            v = os.environ.get(name)
            if not v:
                raise RuntimeError(
                    f"Missing required env var: {name}. "
                    f"Set it in .env.production or as a GitHub Actions secret."
                )
            return v

        def optional_float(name: str, default: float) -> float:
            v = os.environ.get(name)
            return float(v) if v else default

        cutover_str = required("ODOO_CUTOVER_DATE")
        try:
            cutover = date.fromisoformat(cutover_str)
        except ValueError as e:
            raise RuntimeError(
                f"ODOO_CUTOVER_DATE must be YYYY-MM-DD format, got: {cutover_str}"
            ) from e

        return cls(
            odoo_url=required("ODOO_URL").rstrip("/"),
            odoo_db=required("ODOO_DB"),
            odoo_username=required("ODOO_USERNAME"),
            odoo_api_key=required("ODOO_API_KEY"),
            sendgrid_api_key=required("SENDGRID_API_KEY"),
            email_from=required("EMAIL_FROM"),
            email_to=required("EMAIL_TO"),
            webhook_base_url=required("WEBHOOK_BASE_URL").rstrip("/"),
            webhook_shared_secret=required("WEBHOOK_SHARED_SECRET"),
            odoo_cutover_date=cutover,
            urgent_buffer_months=optional_float("URGENT_BUFFER_MONTHS", 2.0),
            addon_buffer_months=optional_float("ADDON_BUFFER_MONTHS", 4.0),
            low_stock_threshold=optional_float("LOW_STOCK_THRESHOLD", 500.0),
        )
