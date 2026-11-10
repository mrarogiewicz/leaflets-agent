"""Extrakcia produktov z PDF letákov (Lidl, Kaufland) podľa pozícií textu."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import pymupdf

PRICE_RE = re.compile(r"^\s*(\d{1,4}[.,]\d{2})\s*(?:€|eur)?\s*$", re.I)
DISC_RE = re.compile(r"^\s*[-−–]\s*(\d{1,2})\s*[%﹪]\s*(\d{1,4}[.,]\d{2})?\s*$")
UNIT_PRICE_RE = re.compile(
    r"\(\s*=?\s*(\d+)\s*(kg|g|l|ml)\s*=?\s*(?:KC:\s*)?(\d+[.,]\d{2})[^)]{0,30}\)?", re.I
)
JUNK_NAME_RE = re.compile(
    r"^(\*|x\.xx|strán s našou|supercena|sme líder|ceny v eur|lacné|bežná cena|kus len|nad \d)", re.I
)
MULTI_QTY_RE = re.compile(r"(?<![\d.,])(\d+)\s*x\s*(\d+(?:[.,]\d+)?)\s*(kg|g|ml|cl|l)\b", re.I)
QTY_RE = re.compile(r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s*(kg|g|ml|cl|l|ks|kus)\b", re.I)
PURE_QTY_RE = re.compile(r"^\s*\d+(?:[.,]\d+)?\s*(kg|g|ml|cl|l|ks|kus)\b", re.I)
NOISE_RE = re.compile(
    r"(MIMORIADNA|^PONUKA$|do vyčerpania|AKTIVÁCIA|KUPÓN|BODOV|Cena pre všetkých|"
    r"^OD\s|^PLATÍ|Lidl Plus|Kaufland Card|^\W*$)",
    re.I,
)

MIN_MAIN_PRICE_SIZE = 12.0
GROUP_RADIUS = 70.0
MATCH_RADIUS = 70.0
BELOW_PENALTY = 15.0

UNIT_FACTORS = {
    "kg": ("kg", 1.0), "g": ("kg", 0.001), "l": ("l", 1.0), "ml": ("l", 0.001),
    "cl": ("l", 0.01), "ks": ("ks", 1.0), "kus": ("ks", 1.0),
}


@dataclass
class Item:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    size: float
    value: float | None = None

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)


@dataclass
class Offer:
    page: int
    name: str
    details: str
    price: float
    old_price: float | None
    discount_pct: int | None
    quantity: float | None
    quantity_unit: str | None
    unit_price: float | None
    unit_price_unit: str | None
    flags: str = ""


@dataclass
class _Group:
    main: Item
    old: Item | None = None
    disc: Item | None = None

    def bbox(self) -> tuple[float, float, float, float]:
        parts = [self.main.bbox] + [i.bbox for i in (self.old, self.disc) if i]
        return (
            min(p[0] for p in parts),
            min(p[1] for p in parts),
            max(p[2] for p in parts),
            max(p[3] for p in parts),
        )


@dataclass
class _Block:
    lines: list[Item] = field(default_factory=list)

    def bbox(self) -> tuple[float, float, float, float]:
        return (
            min(l.x0 for l in self.lines),
            min(l.y0 for l in self.lines),
            max(l.x1 for l in self.lines),
            max(l.y1 for l in self.lines),
        )


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def _gap(a: tuple, b: tuple) -> float:
    dx = max(0.0, a[0] - b[2], b[0] - a[2])
    dy = max(0.0, a[1] - b[3], b[1] - a[3])
    return math.hypot(dx, dy)


def _page_items(page: pymupdf.Page) -> tuple[list[Item], list[Item], list[Item], list[Item]]:
    prices: list[Item] = []
    discs: list[Item] = []
    olds: list[Item] = []
    texts: list[Item] = []
    seen: set[tuple] = set()
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            text = "".join(s["text"] for s in spans).strip()
            x0, y0, x1, y1 = line["bbox"]
            key = (text, round(x0), round(y0))
            if key in seen:  # niektoré letáky duplikujú text kvôli "tučnému" písmu
                continue
            seen.add(key)
            size = spans[0]["size"]
            if m := PRICE_RE.match(text):
                prices.append(Item(text, x0, y0, x1, y1, size, _num(m.group(1))))
            elif m := DISC_RE.match(text):
                discs.append(Item(text, x0, y0, x1, y1, size, float(m.group(1))))
                if m.group(2):  # Lidl: "-13﹪1.49" je zľava aj pôvodná cena v jednom riadku
                    olds.append(Item(m.group(2), x0, y0, x1, y1, size, _num(m.group(2))))
            else:
                texts.append(Item(text, x0, y0, x1, y1, size))
    return prices, discs, olds, texts


def _build_groups(prices: list[Item], discs: list[Item], olds: list[Item]) -> list[_Group]:
    mains: list[Item] = []
    small: list[Item] = list(olds)
    for p in prices:
        bigger_near = any(
            q is not p and q.size > p.size * 1.25 and _gap(p.bbox, q.bbox) <= GROUP_RADIUS
            for q in prices
        )
        if bigger_near or p.size < MIN_MAIN_PRICE_SIZE:
            small.append(p)
        else:
            mains.append(p)
    groups = [_Group(m) for m in mains]
    if not groups:
        return groups

    def attach(item: Item, slot: str) -> None:
        best = min(groups, key=lambda g: _gap(item.bbox, g.main.bbox))
        dist = _gap(item.bbox, best.main.bbox)
        if dist > GROUP_RADIUS:
            return
        current = getattr(best, slot)
        if current is None or dist < _gap(current.bbox, best.main.bbox):
            setattr(best, slot, item)

    for s in small:
        near_cheaper = [g for g in groups if _gap(s.bbox, g.main.bbox) <= GROUP_RADIUS and s.value > g.main.value]
        if near_cheaper:
            attach(s, "old")
    for d in discs:
        attach(d, "disc")
    return groups


def _build_blocks(texts: list[Item]) -> list[_Block]:
    blocks: list[_Block] = []
    for line in sorted(texts, key=lambda t: (t.y0, t.x0)):
        if line.size > 14 or NOISE_RE.search(line.text):
            continue
        for b in blocks:
            last = b.lines[-1]
            if abs(line.x0 - b.lines[0].x0) <= 4 and -1 <= line.y0 - last.y1 <= 3.5:
                b.lines.append(line)
                break
        else:
            blocks.append(_Block([line]))
    return [
        b for b in blocks
        if not PURE_QTY_RE.match(b.lines[0].text) and re.search(r"[^\W\d_]", b.lines[0].text)
    ]


def _split_name_details(block: _Block) -> tuple[str, str]:
    top = max(round(l.size, 1) for l in block.lines)
    name_lines: list[str] = []
    i = 0
    while i < len(block.lines) and round(block.lines[i].size, 1) >= top - 0.6:
        name_lines.append(block.lines[i].text)
        i += 1
    details = " ".join(l.text for l in block.lines[i:])
    return " ".join(name_lines).strip(), details.strip()


def _parse_quantity(name: str, details: str):
    """Vráti (množstvo, jednotka kg/l/ks, €/kg alebo €/l z letáku, jednotka tej ceny)."""
    unit_price = unit_price_unit = None
    text = f"{name} {details}"
    if m := UNIT_PRICE_RE.search(text):
        n, u, price = int(m.group(1)), m.group(2).lower(), _num(m.group(3))
        base_unit, factor = UNIT_FACTORS[u]
        unit_price = round(price / (n * factor), 2)
        unit_price_unit = base_unit
        text = UNIT_PRICE_RE.sub(" ", text)

    if m := MULTI_QTY_RE.search(text):
        qty, u = int(m.group(1)) * _num(m.group(2)), m.group(3).lower()
    elif m := QTY_RE.search(text):
        qty, u = _num(m.group(1)), m.group(2).lower()
    else:
        return None, None, unit_price, unit_price_unit
    base_unit, factor = UNIT_FACTORS[u]
    return round(qty * factor, 4), base_unit, unit_price, unit_price_unit


def parse_page(page: pymupdf.Page, page_no: int) -> tuple[list[Offer], int]:
    prices, discs, olds, texts = _page_items(page)
    groups = _build_groups(prices, discs, olds)
    blocks = _build_blocks(texts)

    pairs: list[tuple[float, int, int]] = []
    for gi, g in enumerate(groups):
        gb = g.bbox()
        for bi, b in enumerate(blocks):
            bb = b.bbox()
            dist = _gap(gb, bb) + (BELOW_PENALTY if bb[1] >= gb[3] else 0.0)
            if dist <= MATCH_RADIUS:
                pairs.append((dist, gi, bi))
    pairs.sort()

    used_g: set[int] = set()
    used_b: set[int] = set()
    offers: list[Offer] = []
    for _, gi, bi in pairs:
        if gi in used_g or bi in used_b:
            continue
        used_g.add(gi)
        used_b.add(bi)
        g, b = groups[gi], blocks[bi]
        name, details = _split_name_details(b)
        if len(name) < 3 or JUNK_NAME_RE.match(name) or (name.isupper() and not details):
            continue
        qty, qunit, unit_price, unit_price_unit = _parse_quantity(name, details)
        price = g.main.value
        old = g.old.value if g.old else None
        disc = int(g.disc.value) if g.disc else None
        if unit_price is None and qty and qunit in ("kg", "l"):
            unit_price, unit_price_unit = round(price / qty, 2), qunit

        flags: list[str] = []
        if old is not None and old <= price:
            flags.append("old<=price")
        if old and disc is not None and abs(round((1 - price / old) * 100) - disc) > 2:
            flags.append("discount_mismatch")
        if name[0].islower():
            flags.append("name_truncated")
        elif name.isupper() and len(name) > 12:
            flags.append("name_caps")
        if "KC:" in details:
            flags.append("card_price")
        offers.append(Offer(page_no, name, details, price, old, disc, qty, qunit,
                            unit_price, unit_price_unit, ",".join(flags)))
    return offers, len(groups) - len(used_g)


def parse_pdf(path: str) -> tuple[list[Offer], dict]:
    offers: list[Offer] = []
    unmatched = 0
    with pymupdf.open(path) as doc:
        pages = len(doc)
        for i, page in enumerate(doc, start=1):
            page_offers, page_unmatched = parse_page(page, i)
            offers.extend(page_offers)
            unmatched += page_unmatched
    stats = {"pages": pages, "offers": len(offers), "unmatched_prices": unmatched,
             "flagged": sum(1 for o in offers if o.flags)}
    return offers, stats
