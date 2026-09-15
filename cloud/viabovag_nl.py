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

BASE = "https://www.viabovag.nl"

MODEL_PATHS = {
    "honda-cbr600f": "/motoren/merk-honda/model-cbr-600-f",
    "honda-cbr600rr": "/motoren/merk-honda/model-cbr-600-rr",
    "honda-cbr650r": "/motoren/merk-honda/model-cbr-650-r",
    "honda-cbr500r": "/motoren/merk-honda/model-cbr-500-r",
    "yamaha-r3": "/motoren/merk-yamaha/model-yzf-r3",
    "yamaha-r6": "/motoren/merk-yamaha/model-r6",
    "yamaha-r7": "/motoren/merk-yamaha/model-yzf-r7",
    "honda-rebel500": "/motoren/merk-honda/model-cmx-500-rebel",
    "triumph-street-triple-675": "/motoren/merk-triumph/model-street-triple",
    "triumph-daytona-675": "/motoren/merk-triumph/model-daytona-675",
    "suzuki-gsxr600": "/motoren/merk-suzuki/model-gsx-r-600",
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
        "yamaha-r6": lambda x: "yzfr6" in x or "yamahar6" in x or ("r6" in x and "r60" not in x),
        "yamaha-r7": lambda x: "yzfr7" in x or "yamahar7" in x,
        "honda-rebel500": lambda x: "cmx500" in x or "rebel500" in x,
        "triumph-street-triple-675": lambda x: "streettriple" in x and ("675" in x or "765" not in x),
        "triumph-daytona-675": lambda x: "daytona675" in x,
        "suzuki-gsxr600": lambda x: "gsxr600" in x,
    }
    if bid in rules:
        return bool(rules[bid](t))
    model = _norm(str(bike.get("model", "")))
    return model in t if model else relevant(title, bike)


def listing_links(page: str) -> list[str]:
    parser = HrefCollector(); parser.feed(page)
    out: list[str] = []
    seen = set()
    for href in parser.hrefs:
        h = html_lib.unescape(href).replace("\\/", "/")
        if "/motor/aanbod/" not in h.lower():
            continue
        url = urljoin(BASE, h).split("?", 1)[0].split("#", 1)[0]
        # Normalize any site/profile/admin host back to public canonical host.
        url = re.sub(r"^https?://[^/]+", BASE, url)
        if url not in seen:
            seen.add(url); out.append(url)
    # Next/JSON fallback.
    for m in re.finditer(r"(?:https?://[^\"'<>]+)?(/motor/aanbod/[a-z0-9-]+)", page, flags=re.I):
        url = BASE + m.group(1)
        if url not in seen:
            seen.add(url); out.append(url)
    return out


def _field(text: str, label: str, value_pattern: str) -> str | None:
    m = re.search(rf"\b{re.escape(label)}\s+({value_pattern})", text, flags=re.I)
    return m.group(1).strip() if m else None


def parse_detail(page: str, url: str, bike: dict) -> dict | None:
    text = strip_tags(page)

    # The canonical H1 is the model (e.g. "Honda CBR 600 RR").
    m = re.search(r"<h1\b[^>]*>(.*?)</h1>", page, flags=re.I | re.S)
    title = strip_tags(m.group(1)).strip() if m else None
    if not title:
        m = re.search(r"<title\b[^>]*>(.*?)</title>", page, flags=re.I | re.S)
        title = strip_tags(m.group(1)).strip() if m else None
    if not title or not strict_model_match(title, bike):
        return None

    price = None
    m = re.search(r"\bPrijs\s+€\s*([0-9][0-9.\s]{1,10})", text, flags=re.I)
    if m:
        price = _num(m.group(1))
    if price is None:
        # Search-result header often exposes "7.499,-" without a euro sign.
        m = re.search(r"\b([0-9]{1,3}(?:\.[0-9]{3})+),-\b", text)
        if m:
            price = _num(m.group(1))

    mileage = None
    value = _field(text, "Kilometerstand", r"[0-9][0-9.\s]{0,10}\s*km")
    if value:
        mileage = _num(value)
        if mileage is not None and mileage > 500000:
            mileage = None

    year = None
    value = _field(text, "Bouwjaar", r"(?:0?[1-9]|1[0-2])?[-/]?(?:19|20)\d{2}|(?:19|20)\d{2}")
    if value:
        ym = re.search(r"((?:19|20)\d{2})", value)
        if ym:
            year = int(ym.group(1))
    if year is None:
        # Some ads omit Bouwjaar but do expose Datum eerste toelating.
        value = _field(text, "Datum eerste toelating", r"\d{1,2}-\d{1,2}-(?:19|20)\d{2}")
        if value:
            ym = re.search(r"((?:19|20)\d{2})", value)
            if ym:
                year = int(ym.group(1))

    seller = "BOVAG dealer"
    m = re.search(r"Deze\s+[A-Za-zÀ-ÿ]+\s+staat\s+bij:\s*([^0-9]{2,100}?)(?=\s+[A-Za-zÀ-ÿ]+straat|\s+[A-Za-zÀ-ÿ]+weg|\s+\d{4}\s?[A-Z]{2}|\s+\d[,\.]\d|$)", text, flags=re.I)
    if m:
        seller = " ".join(m.group(1).split())[:100]

    location = "Netherlands"
    # Prefer Dutch postcode + town/address context near the seller block.
    m = re.search(r"\b(\d{4}\s?[A-Z]{2})\b", text)
    if m:
        location = f"NL-{m.group(1).replace(' ', '')}"

    tail = url.rstrip("/").rsplit("/", 1)[-1]
    sid = tail or "bovag-" + hashlib.sha1(url.encode()).hexdigest()[:18]
    return {
        "source_id": sid,
        "bike_id": bike["id"],
        "title": title,
        "price": price,
        "year": year,
        "km": mileage,
        "location": location,
        "seller": seller,
        "source": "viaBOVAG NL",
        "country": "NL",
        "url": url,
        "image_url": extract_first_image(page),
        "conditional_price": False,
        "features": detect_features(text),
        "raw_text": text[:1400],
    }


def search_bike(bike: dict) -> tuple[list[dict], str]:
    path = MODEL_PATHS.get(str(bike.get("id", "")))
    if not path:
        return [], BASE + "/motoren"
    url = BASE + path
    page = fetch(url)
    low = page.lower()
    if "viabovag" not in low or len(page) < 5000:
        raise ValueError("viaBOVAG returned an unrecognized page")

    links = listing_links(page)[:50]
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
                completed += 1
                continue
            if item:
                listings.append(item)

    if links and completed == 0:
        raise ValueError("viaBOVAG detail pages could not be fetched")
    unique = {x["source_id"]: x for x in listings}
    return list(unique.values()), url


__all__ = ["HTTPError", "URLError", "search_bike"]
