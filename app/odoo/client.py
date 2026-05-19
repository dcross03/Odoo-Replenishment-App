"""Odoo XML-RPC client.

Mirrors the pattern used in amazon-settlement-app/app/odoo/client.py.

Usage:
    client = OdooClient.from_config(config)
    products = client.search_read(
        'product.product',
        [('default_code', '=', '6004')],
        ['id', 'name'],
    )
"""
from __future__ import annotations

import logging
import xmlrpc.client
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


class OdooError(Exception):
    """Raised when Odoo returns an error."""


@dataclass
class OdooClient:
    url: str
    database: str
    username: str
    api_key: str
    _uid: Optional[int] = None
    _common: Optional[Any] = None
    _models: Optional[Any] = None

    @classmethod
    def from_config(cls, config) -> "OdooClient":
        return cls(
            url=config.odoo_url,
            database=config.odoo_db,
            username=config.odoo_username,
            api_key=config.odoo_api_key,
        )

    def authenticate(self) -> int:
        """Authenticate and cache the user ID."""
        if self._uid is not None:
            return self._uid

        self._common = xmlrpc.client.ServerProxy(
            f"{self.url}/xmlrpc/2/common", allow_none=True
        )

        try:
            uid = self._common.authenticate(
                self.database, self.username, self.api_key, {}
            )
        except Exception as e:
            raise OdooError(f"Authentication failed: {e}") from e

        if not uid:
            raise OdooError(
                "Authentication returned no UID. Check ODOO_DB, ODOO_USERNAME, ODOO_API_KEY."
            )

        self._uid = uid
        self._models = xmlrpc.client.ServerProxy(
            f"{self.url}/xmlrpc/2/object", allow_none=True
        )
        logger.info("Authenticated to Odoo as uid=%d", uid)
        return uid

    def execute(
        self,
        model: str,
        method: str,
        args: list,
        kwargs: Optional[dict] = None,
    ) -> Any:
        """Execute a method on a model via execute_kw."""
        self.authenticate()
        try:
            return self._models.execute_kw(
                self.database,
                self._uid,
                self.api_key,
                model,
                method,
                args,
                kwargs or {},
            )
        except xmlrpc.client.Fault as e:
            raise OdooError(
                f"Odoo error calling {model}.{method}: {e.faultString}"
            ) from e

    def search_read(
        self,
        model: str,
        domain: list,
        fields: list,
        limit: Optional[int] = None,
        order: Optional[str] = None,
        include_archived: bool = False,
    ) -> list:
        """Convenience wrapper for the common search_read pattern."""
        kwargs: dict = {"fields": fields}
        if limit is not None:
            kwargs["limit"] = limit
        if order is not None:
            kwargs["order"] = order
        if include_archived:
            kwargs["context"] = {"active_test": False}
        return self.execute(model, "search_read", [domain], kwargs)

    def search_count(self, model: str, domain: list) -> int:
        return self.execute(model, "search_count", [domain])

    def create(self, model: str, vals: dict) -> int:
        return self.execute(model, "create", [vals])

    def write(self, model: str, ids: list, vals: dict) -> bool:
        return self.execute(model, "write", [ids, vals])
