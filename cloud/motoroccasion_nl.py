from __future__ import annotations

import hashlib
import html as html_lib
import re
import ssl
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from marktplaats import detect_features, extract_first_image, relevant, strip_tags

BASE = "https://www.motoroccasion.nl"

# Stable model pages used by Motoroccasion. These cover the BikeHunter watchlist.
MODEL_PAGES = {
    "honda-cbr600f": "/motoren/honda-cbr-600-f-b18-tg26.html",
    "honda-cbr600rr": "/motoren/honda-cbr-600-rr-b18-tg780.html",
    "honda-cbr650r": "/motoren/honda-cbr-650-r-b18-t6138.html",
    "honda-cbr500r": "/motoren/honda-cbr-500-r-b18-tg814.html",
    "yamaha-r3": "/motoren/yamaha-yzf-r3-b40-t6907.html",
    "yamaha-r6": "/motoren/yamaha-yzf-r6-b40-tg761.html",
    "yamaha-r7": "/motoren/yamaha-yzf-r7-b40-tg1425.html",
    "honda-rebel500": "/motoren/honda-cmx-500-b18-tg1321.html",
    "triumph-street-triple-675": "/motoren/triumph-street-triple-675-b37-t3852.html",
    "triumph-daytona-675": "/motoren/triumph-daytona-675-b37-t3355.html",
    "suzuki-gsxr600": "/motoren/suzuki-gsx-r-600-b35-tg206.html",
    "aprilia-rs660": "/motoren/aprilia-rs-660-b1-tg1427.html",
    "honda-hornet-750": "/motoren/honda-cb-750-hornet-b18-t9347.html",
}


class HrefCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


def fetch(url: str) -> str:
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.7",
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache",
    })
    with urlopen(req, timeout=35, context=ssl.create_default_context()) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")


def _num(value: str) -> int | None:
    digits = re.sub(r"\D", "", value or "")
    return int(digits) if digits else None


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def strict_model_match(title: str, bike: dict) -> bool:
    t = _norm(title)
    bid = str(bike.get("id", ""))
    rules = {
        "honda-cbr600rr": lambda x: "cbr600rr" in x,
        "honda-cbr600f": lambda x: "cbr600f" in x and "cbr600rr" not in x,
        "honda-cbr650r": lambda x: "cbr650r" in x,
        "honda-cbr500r": lambda x: "cbr500r" in x,
        "yamaha-r3": lambda x: "yzfr3" in x or "yamahar3" in x,
        "yamaha-r6": lambda x: "yzfr6" in x or "yamahar6" in x,
        "yamaha-r7": lambda x: "yzfr7" in x or "yamahar7" in x,
        "honda-rebel500": lambda x: "cmx500" in x or "rebel500" in x,
        "triumph-street-triple-675": lambda x: "streettriple675" in x,
        "triumph-daytona-675": lambda x: "daytona675" in x,
        "suzuki-gsxr600": lambda x: "gsxr600" in x,
        "aprilia-rs660": lambda x: "rs660" in x,
        "honda-hornet-750": lambda x: "cb750" in x and "hornet" in x,
    }
    if bid in rules:
        return bool(rules[bid](t))
    model = _norm(str(bike.get("model", "")))
    return model in t if model else relevant(title, bike)


def listing_links(page: str) -> list[str]:
    parser = HrefCollector()
    parser.feed(page)
    out: list[str] = []
    seen = set()
    # Detail pages use /motoren/<slug>-m1234567.html
    pat = re.compile(r"/motoren/[a-z0-9-]+-m\d+\.html(?:[?#][^\"']*)?$", flags=re.I)
    for href in parser.hrefs:
        clean = html_lib.unescape(href).replace("\\/", "/")
        if clean.startswith("http"):
            path = re.sub(r"^https?://[^/]+", "", clean)
        else:
            path = clean
        if not pat.search(path):
            continue
        url = urljoin(BASE, clean)
        url = url.split("#", 1)[0]
        if url not in seen:
            seen.add(url); out.append(url)
    # JSON-ish fallback used by some server-rendered pages.
    for m in re.finditer(r"(?:https?://www\.motoroccasion\.nl)?(/motoren/[a-z0-9-]+-m\d+\.html)", page, flags=re.I):
        url = BASE + m.group(1)
        if url not in seen:
            seen.add(url); out.append(url)
    return out


def parse_detail(page: str, url: str, bike: dict) -> dict | None:
    text = strip_tags(page)
    title = None
    m = re.search(r"<h1\b[^>]*>(.*?)</h1>", page, flags=re.I | re.S)
    if m:
        title = strip_tags(m.group(1)).strip()
    if not title:
        m = re.search(r"<title\b[^>]*>(.*?)</title>", page, flags=re.I | re.S)
        title = strip_tags(m.group(1)).strip() if m else None
    if not title or not strict_model_match(title, bike):
        return None

    price = None
    m = re.search(r"\bPrijs\s*:\s*€\s*([0-9][0-9.\s]{1,10})", text, flags=re.I)
    if m:
        price = _num(m.group(1))
    if price is None:
        m = re.search(r"€\s*([0-9][0-9.\s]{1,10})(?:,-)?", text)
        price = _num(m.group(1)) if m else None

    year = None
    for pat in (r"\bBouwjaar\s*:\s*((?:19|20)\d{2})\b", r"\bBouwjaar\s*((?:19|20)\d{2})\b"):
        m = re.search(pat, text, flags=re.I)
        if m:
            year = int(m.group(1)); break
    if year is None:
        m = re.search(r"[-–]\s*((?:19|20)\d{2})\b", title)
        if m:
            year = int(m.group(1))

    mileage = None
    for pat in (r"\bTeller(?:stand)?\s*:\s*([0-9][0-9.\s]{0,10})\s*Km\b", r"\bTeller\s*([0-9][0-9.\s]{0,10})\s*Km\b"):
        m = re.search(pat, text, flags=re.I)
        if m:
            mileage = _num(m.group(1)); break
    if mileage is not None and mileage > 500000:
        mileage = None

    # The dealer name varies in markup; capture a useful best-effort label if exposed.
    seller = "Motoroccasion dealer"
    m = re.search(r"\bBedrijf\s+([A-ZÀ-ÿ0-9][^|]{2,80}?)(?=\s+(?:Adres|Telefoon|Website|Top|Omschrijving)\b)", text, flags=re.I)
    if m:
        seller = m.group(1).strip()[:100]

    image = extract_first_image(page)
    sid_m = re.search(r"-m(\d+)\.html", url, flags=re.I)
    sid = f"m{sid_m.group(1)}" if sid_m else "mo-" + hashlib.sha1(url.encode()).hexdigest()[:18]
    return {
        "source_id": sid,
        "bike_id": bike["id"],
        "title": title,
        "price": price,
        "year": year,
        "km": mileage,
        "location": "Netherlands",
        "seller": seller,
        "source": "Motoroccasion NL",
        "country": "NL",
        "url": url,
        "image_url": image,
        "conditional_price": False,
        "features": detect_features(text),
        "raw_text": text[:1400],
    }


def search_bike(bike: dict) -> tuple[list[dict], str]:
    path = MODEL_PAGES.get(str(bike.get("id", "")))
    if not path:
        # For bikes later added in the dashboard, do not guess a Motoroccasion type ID.
        # Other NL sources still scan it normally.
        return [], BASE
    url = BASE + path
    page = fetch(url)
    low = page.lower()
    if "motoroccasion" not in low or len(page) < 5000:
        raise ValueError("Motoroccasion returned an unrecognized page")

    links = listing_links(page)[:60]
    listings: list[dict] = []
    completed = 0

    def load_one(detail_url: str):
        detail = fetch(detail_url)
        return parse_detail(detail, detail_url, bike)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(load_one, detail_url): detail_url for detail_url in links}
        for future in as_completed(futures):
            try:
                item = future.result()
                completed += 1
            except (HTTPError, URLError):
                continue
            except Exception:
                # One malformed advert should not kill the whole source.
                completed += 1
                continue
            if item:
                listings.append(item)

    if links and completed == 0:
        raise ValueError("Motoroccasion detail pages could not be fetched")
    unique = {x["source_id"]: x for x in listings}
    return list(unique.values()), url


__all__ = ["HTTPError", "URLError", "search_bike"]
