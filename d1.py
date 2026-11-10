"""Minimálny klient pre Cloudflare D1 (REST API)."""
from __future__ import annotations

import math
import os
import time
from pathlib import Path

import requests

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
INSERT_CHUNK = 40

OFFER_COLUMNS = (
    "flyer_id, store, valid_from, valid_to, page, name, details, quantity, quantity_unit, "
    "price, old_price, discount_pct, unit_price, unit_price_unit, flags"
)


def lit(value) -> str:
    """SQL literál. Hodnoty pochádzajú z verejných PDF, preto sa dôsledne escapujú."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value) if math.isfinite(value) else "NULL"
    return "'" + str(value).replace("\x00", "").replace("'", "''") + "'"


class D1:
    def __init__(self, account_id: str, database_id: str, token: str):
        self.url = (
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
            f"/d1/database/{database_id}/query"
        )
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    @classmethod
    def from_env(cls) -> "D1":
        missing = [k for k in ("CF_ACCOUNT_ID", "CF_D1_DATABASE_ID", "CF_API_TOKEN") if not os.environ.get(k)]
        if missing:
            raise SystemExit(f"Chýbajú premenné prostredia: {', '.join(missing)}")
        return cls(os.environ["CF_ACCOUNT_ID"], os.environ["CF_D1_DATABASE_ID"], os.environ["CF_API_TOKEN"])

    def _post(self, body: dict) -> list[dict]:
        for attempt in range(4):
            resp = self.session.post(self.url, json=body, timeout=120)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt * 2)
                continue
            break
        try:
            data = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise
        if not resp.ok or not data.get("success"):
            raise RuntimeError(f"D1 chyba {resp.status_code}: {data.get('errors')}")
        return data["result"]

    def query(self, sql: str) -> list[dict]:
        return self._post({"sql": sql})[0].get("results", [])

    def batch(self, statements: list[str]) -> None:
        """Vykoná príkazy v jednej transakcii (všetko alebo nič)."""
        self._post({"batch": [{"sql": s} for s in statements]})

    def ensure_schema(self) -> None:
        statements = [s.strip() for s in SCHEMA_PATH.read_text(encoding="utf-8").split(";") if s.strip()]
        self.batch(statements)

    def flyer_pdf_url(self, flyer_id: str) -> str | None:
        rows = self.query(f"SELECT pdf_url FROM flyers WHERE flyer_id = {lit(flyer_id)}")
        return rows[0]["pdf_url"] if rows else None

    def replace_flyer(self, flyer, offers) -> None:
        """Atomicky nahradí ponuky letáku novými."""
        fid = lit(flyer.flyer_id)
        statements = [f"DELETE FROM offers WHERE flyer_id = {fid}"]
        for i in range(0, len(offers), INSERT_CHUNK):
            rows = []
            for o in offers[i:i + INSERT_CHUNK]:
                values = (
                    flyer.flyer_id, flyer.store, flyer.valid_from, flyer.valid_to, o.page, o.name,
                    o.details, o.quantity, o.quantity_unit, o.price, o.old_price, o.discount_pct,
                    o.unit_price, o.unit_price_unit, o.flags,
                )
                rows.append("(" + ", ".join(lit(v) for v in values) + ")")
            statements.append(f"INSERT INTO offers ({OFFER_COLUMNS}) VALUES " + ", ".join(rows))
        statements.append(
            "INSERT OR REPLACE INTO flyers (flyer_id, store, region, title, valid_from, valid_to, "
            "pdf_url, n_offers, fetched_at) VALUES ("
            + ", ".join(lit(v) for v in (
                flyer.flyer_id, flyer.store, flyer.region, flyer.title, flyer.valid_from,
                flyer.valid_to, flyer.pdf_url, len(offers),
            ))
            + ", datetime('now'))"
        )
        self.batch(statements)
