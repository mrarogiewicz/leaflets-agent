"""Týždenný job: nájde letáky Lidl a Kaufland, extrahuje produkty a uloží ich do Cloudflare D1.

Použitie:
  python extract.py                      # zápis do D1 (premenné CF_ACCOUNT_ID, CF_D1_DATABASE_ID, CF_API_TOKEN)
  python extract.py --dry-run            # bez D1, výsledok uloží do data/out/*.csv
  python extract.py --stores lidl --force
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from dataclasses import asdict
from pathlib import Path

try:  # na firemných počítačoch používa certifikáty z Windows
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

from d1 import D1
from flyer_parser import parse_pdf
from sources import Flyer, find_flyers, make_session

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent / "data"))
MIN_OFFERS = 50  # menej znamená, že sa zmenil formát PDF, a staré dáta sa neprepisujú


def download_pdf(session, flyer: Flyer) -> Path:
    target = DATA_DIR / "pdfs" / f"{flyer.store}_{flyer.flyer_id}.pdf"
    if target.exists() and target.stat().st_size > 0:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".part")
    with session.get(flyer.pdf_url, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
    tmp.replace(target)
    return target


def write_csv(flyer: Flyer, offers) -> Path:
    out = DATA_DIR / "out" / f"{flyer.store}_{flyer.valid_from}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(asdict(offers[0])))
        writer.writeheader()
        writer.writerows(asdict(o) for o in offers)
    return out


def process(session, db: D1 | None, flyer: Flyer, force: bool) -> None:
    label = f"{flyer.store} {flyer.valid_from}..{flyer.valid_to} ({flyer.flyer_id[:8]})"
    if db and not force and db.flyer_pdf_url(flyer.flyer_id) == flyer.pdf_url:
        print(f"[skip] {label}: už je v databáze")
        return
    print(f"[download] {label}: {flyer.pdf_url}")
    pdf = download_pdf(session, flyer)
    offers, stats = parse_pdf(str(pdf))
    print(f"[parse] {label}: {stats}")
    if len(offers) < MIN_OFFERS:
        raise RuntimeError(f"{label}: nájdených len {len(offers)} produktov, formát PDF sa asi zmenil")
    if db:
        db.replace_flyer(flyer, offers)
        print(f"[d1] {label}: uložených {len(offers)} produktov")
    else:
        print(f"[csv] {write_csv(flyer, offers)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stores", default="lidl,kaufland")
    ap.add_argument("--dry-run", action="store_true", help="nezapisovať do D1, len CSV")
    ap.add_argument("--force", action="store_true", help="spracovať aj už uložené letáky")
    ap.add_argument("--kaufland-region", default=os.environ.get("KAUFLAND_REGION", "1220"))
    args = ap.parse_args()

    session = make_session()
    db = None
    if not args.dry_run:
        db = D1.from_env()
        db.ensure_schema()

    failures = 0
    for store in (s.strip() for s in args.stores.split(",") if s.strip()):
        try:
            flyers = find_flyers(session, store, args.kaufland_region)
            print(f"[find] {store}: {len(flyers)} letákov")
            if not flyers:
                raise RuntimeError(f"{store}: nenašiel sa žiadny leták")
        except Exception as exc:
            print(f"[ERROR] {store}: {exc}", file=sys.stderr)
            failures += 1
            continue
        for flyer in flyers:
            try:
                process(session, db, flyer, args.force)
            except Exception as exc:
                print(f"[ERROR] {store} {flyer.valid_from}: {exc}", file=sys.stderr)
                failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
