"""Vyhľadanie aktuálnych letákov (Lidl, Kaufland) cez leaflets.schwarz API."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_URL = "https://endpoints.leaflets.schwarz/v4/flyer"
LIDL_LIST_URL = "https://www.lidl.sk/letaky"
KAUFLAND_LIST_URL = "https://predajne.kaufland.sk/aktualna-ponuka/letak.html"
TIMEOUT = 30


@dataclass
class Flyer:
    store: str
    flyer_id: str
    region: str
    title: str
    valid_from: str | None
    valid_to: str | None
    pdf_url: str


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
    retry = Retry(total=4, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _flyer_from_api(session: requests.Session, store: str, identifier: str, region: str) -> Flyer:
    resp = session.get(API_URL, params={"flyer_identifier": identifier}, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"API nenašlo leták {identifier}: {data.get('message')}")
    f = data["flyer"]
    return Flyer(
        store=store,
        flyer_id=f["id"],
        region=region,
        title=f.get("title") or identifier,
        valid_from=f.get("offerStartDate") or f.get("startDate"),
        valid_to=f.get("offerEndDate") or f.get("endDate"),
        pdf_url=f["pdfUrl"],
    )


def find_lidl(session: requests.Session) -> list[Flyer]:
    page = session.get(LIDL_LIST_URL, timeout=TIMEOUT)
    page.raise_for_status()
    slugs: list[str] = []
    for m in re.finditer(r'href="https://www\.lidl\.sk/l/sk/letak/([^/"]+)/ar/0"[^>]*?data-track-name="([^"]*)"', page.text, re.S):
        slug, name = m.group(1), html.unescape(m.group(2))
        # len týždenný potravinový leták, nie magazíny a brožúry
        if slug.startswith("online-letak") and "let" in name.lower() and slug not in slugs:
            slugs.append(slug)
    return [_flyer_from_api(session, "lidl", slug, "0") for slug in slugs]


def find_kaufland(session: requests.Session, region: str) -> list[Flyer]:
    page = session.get(KAUFLAND_LIST_URL, timeout=TIMEOUT)
    page.raise_for_status()
    # stránka ukazuje letáky pre predvolený región, identifikátor má tvar SK_sk_KDZ_<región>_SK45-LFT
    weeks = list(dict.fromkeys(re.findall(r"SK_sk_KDZ_\d{4}_(SK\d+-LFT)", page.text)))
    return [_flyer_from_api(session, "kaufland", f"SK_sk_KDZ_{region}_{week}", region) for week in weeks]


def find_flyers(session: requests.Session, store: str, kaufland_region: str) -> list[Flyer]:
    if store == "lidl":
        return find_lidl(session)
    if store == "kaufland":
        return find_kaufland(session, kaufland_region)
    raise ValueError(f"Neznámy obchod: {store}")
