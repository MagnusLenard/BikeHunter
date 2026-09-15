from __future__ import annotations

import html as html_lib
import re
import ssl
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, unquote_plus, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

from marktplaats import detect_features, extract_first_image, relevant, strip_tags

# BikeNet currently has two public front ends:
# - www.bikenet.nl: current site, valid TLS, good for detail pages
# - www2.bikenet.nl: legacy SEO inventory pages, still live but its TLS cert is expired
#
# The legacy inventory pages are the only ones that expose stable brand/type indexes such as
# /occasions/honda/cb-750/. We therefore use www2 ONLY to read public index pages, with TLS
# verification disabled for that host only, and immediately rewrite every detail URL to the
# current www.bikenet.nl host where normal TLS verification remains enabled.
BASE = "https://www.bikenet.nl"
INDEX_BASE = "https://www2.bikenet.nl"
SEARCH_PATH = "/occasions/"

# BikeNet groups variants under broad type buckets. Strict title matching later separates
# CBR600F vs CBR600RR, etc.
MODEL_PATHS = {
    "honda-cbr600f": ["honda/cbr-600"],
    "honda-cbr600rr": ["honda/cbr-600"],
    "honda-cbr650r": ["honda/cbr-650"],
    "honda-cbr500r": ["honda/cbr-500"],
    "yamaha-r3": ["yamaha/yzf-r3"],
    "yamaha-r6": ["yamaha/yzf-r6"],
    "yamaha-r7": ["yamaha/yzf-r7"],
    "honda-rebel500": ["honda/cmx-500"],
    "triumph-street-triple-675": ["triumph/street-triple"],
    "triumph-daytona-675": ["triumph/daytona-675"],
    "suzuki-gsxr600": ["suzuki/gsx-r-600"],
    "aprilia-rs660": ["aprilia/rs-660"],
    "honda-hornet-750": ["honda/cb-750", "honda/hornet-750"],
}

MODEL_TYPES = {
    "honda-cbr600f": "cbr 600",
    "honda-cbr600rr": "cbr 600",
    "honda-cbr650r": "cbr 650",
    "honda-cbr500r": "cbr 500",
    "yamaha-r3": "yzf r3",
    "yamaha-r6": "yzf r6",
    "yamaha-r7": "yzf r7",
    "honda-rebel500": "cmx 500",
    "triumph-street-triple-675": "street triple",
    "triumph-daytona-675": "daytona 675",
    "suzuki-gsxr600": "gsx r 600",
    "aprilia-rs660": "rs 660",
    "honda-hornet-750": "cb 750",
}


class HrefCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.hrefs.append(href)


def _request(url: str) -> Request:
    return Request(url, headers={
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/151.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.7",
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache",
    })


def fetch(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    # Narrow exception for BikeNet's legacy *public index* host. No credentials, cookies,
    # tokens, POST bodies or private data are ever sent here.
    context = ssl._create_unverified_context() if host == "www2.bikenet.nl" else ssl.create_default_context()
    with urlopen(_request(url), timeout=35, context=context) as response:
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
        "aprilia-rs660": lambda x: "rs660" in x,
        "honda-hornet-750": lambda x: "cb750" in x and "hornet" in x,
    }
    if bid in rules:
        return bool(rules[bid](t))
    model = _norm(str(bike.get("model", "")))
    return model in t if model else relevant(title, bike)


def search_type_for(bike: dict) -> str:
    bid = str(bike.get("id", ""))
    if MODEL_TYPES.get(bid):
        return MODEL_TYPES[bid]
    model = str(bike.get("model") or "").replace("-", " ").replace("/", " ")
    return " ".join(model.split()).lower()


def _index_urls(bike: dict, page: int = 1) -> list[str]:
    bid = str(bike.get("id", ""))
    paths = MODEL_PATHS.get(bid) or []
    page = max(1, int(page))
    urls: list[str] = []
    for path in paths:
        base = f"{INDEX_BASE}{SEARCH_PATH}{path.strip('/')}/"
        urls.append(base if page == 1 else base + "?" + urlencode({"pagina": page}))
    return urls


def _brand_url(bike: dict, page: int = 1) -> str:
    brand = re.sub(r"[^a-z0-9-]", "", str(bike.get("make") or "").strip().lower().replace(" ", "-"))
    page = max(1, int(page))
    base = f"{INDEX_BASE}{SEARCH_PATH}{brand}/"
    return base if page == 1 else base + "?" + urlencode({"pagina": page})


def _canonical_detail_url(href: str) -> str | None:
    h = html_lib.unescape(href).replace("\\/", "/").strip()
    low = h.lower()
    if "/occasion/" not in low or "/occasions/" in low:
        return None
    # Resolve against the legacy index, then keep only BikeNet-owned detail paths.
    parsed = urlparse(urljoin(INDEX_BASE, h).split("#", 1)[0])
    host = (parsed.hostname or "").lower()
    if not (host == "bikenet.nl" or host.endswith(".bikenet.nl")):
        return None
    if "/occasion/" not in parsed.path.lower():
        return None
    # Always fetch details from the current valid-TLS host.
    suffix = parsed.path
    if parsed.query:
        suffix += "?" + parsed.query
    return BASE + suffix


def listing_links(page: str, bike: dict | None = None) -> list[str]:
    parser = HrefCollector()
    parser.feed(page)
    out: list[str] = []
    seen: set[str] = set()
    for href in parser.hrefs:
        url = _canonical_detail_url(href)
        if not url:
            continue
        # BikeNet detail URLs contain the make/model slug. This cheap pre-filter is especially
        # useful when we have to fall back to a whole-brand page.
        if bike is not None:
            path_hint = unquote(urlparse(url).path).replace("-", " ")
            if not strict_model_match(path_hint, bike):
                continue
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _label_value(text: str, label: str, stop_labels: tuple[str, ...]) -> str | None:
    stops = "|".join(re.escape(x) for x in stop_labels)
    m = re.search(rf"\b{re.escape(label)}\s*:\s*(.*?)(?=\b(?:{stops})\s*:|$)", text, flags=re.I)
    return " ".join(m.group(1).split()) if m else None


def parse_detail(page: str, url: str, bike: dict) -> dict | None:
    text = strip_tags(page)

    m = re.search(r"<h1\b[^>]*>(.*?)</h1>", page, flags=re.I | re.S)
    title = strip_tags(m.group(1)).strip() if m else None
    if not title:
        m = re.search(r"<title\b[^>]*>(.*?)</title>", page, flags=re.I | re.S)
        title = strip_tags(m.group(1)).strip() if m else None
    if not title or not strict_model_match(title, bike):
        return None

    price = None
    price_block = _label_value(text, "Prijs", ("Grondslag", "Merk", "Type", "Bouwjaar"))
    if price_block:
        values = []
        for raw in re.findall(r"€\s*([0-9][0-9.\s]{1,12}(?:,[0-9]{2})?)", price_block, flags=re.I):
            value = _num(raw.split(",", 1)[0])
            if value is not None and 100 <= value <= 100000:
                values.append(value)
        if values:
            price = values[-1]
    if price is None:
        m = re.search(r"€\s*([0-9]{1,3}(?:[.\s][0-9]{3})+|[0-9]{3,6})(?:,[0-9]{2})?", text)
        if m:
            price = _num(m.group(1))

    year = None
    raw_year = _label_value(text, "Bouwjaar", ("Kilometerstand", "Garantie", "Kleur", "Modeljaar", "Categorie"))
    if raw_year:
        ym = re.search(r"\b((?:19|20)\d{2})\b", raw_year)
        if ym:
            year = int(ym.group(1))

    mileage = None
    raw_km = _label_value(text, "Kilometerstand", ("Garantie", "Kleur", "Modeljaar", "Categorie", "Gewicht"))
    if raw_km:
        mileage = _num(raw_km)
        if mileage is not None and mileage > 500000:
            mileage = None

    seller = "BikeNet dealer"
    m = re.search(r"[?&]naam=([^&\"'<>]+)", page, flags=re.I)
    if m:
        candidate = unquote_plus(html_lib.unescape(m.group(1))).strip()
        if candidate:
            seller = candidate[:120]
    if seller == "BikeNet dealer":
        # Current detail layout commonly places the dealer in an h2/h3 near its address.
        candidates = re.findall(r"<(?:h2|h3)\b[^>]*>(.*?)</(?:h2|h3)>", page, flags=re.I | re.S)
        for raw in reversed(candidates):
            candidate = strip_tags(raw).strip()
            if candidate and not candidate.lower().startswith(("andere interessante", "omschrijving", "voertuig")):
                if len(candidate) <= 120:
                    seller = candidate
                    break

    location = "Netherlands"
    m = re.search(r"\b(\d{4}\s?[A-Z]{2})\b", text)
    if m:
        location = f"NL-{m.group(1).replace(' ', '')}"

    path = urlparse(url).path.rstrip("/")
    sid = path.rsplit("/", 1)[-1] or re.sub(r"\W+", "-", url.lower()).strip("-")[-40:]
    return {
        "source_id": sid,
        "bike_id": bike["id"],
        "title": title,
        "price": price,
        "year": year,
        "km": mileage,
        "location": location,
        "seller": seller,
        "source": "BikeNet Dealers NL",
        "country": "NL",
        "url": url,
        "image_url": extract_first_image(page),
        "conditional_price": False,
        "features": detect_features(text),
        "raw_text": text[:1400],
    }


def _collect_model_links(bike: dict) -> tuple[list[str], str]:
    first_urls = _index_urls(bike, 1)
    first_url = first_urls[0] if first_urls else _brand_url(bike, 1)
    seen: set[str] = set()
    links: list[str] = []
    successful_index_fetch = False

    # Preferred: BikeNet's type-specific legacy index. Usually only one page is needed, but
    # allow three for busy buckets such as CBR 600.
    for page_no in range(1, 4):
        page_added = 0
        urls = _index_urls(bike, page_no)
        if not urls:
            break
        for url in urls:
            try:
                page = fetch(url)
            except HTTPError as exc:
                if exc.code == 404:
                    continue
                raise
            successful_index_fetch = True
            if "bikenet" not in page.lower() or len(page) < 1200:
                continue
            for detail_url in listing_links(page, bike):
                if detail_url not in seen:
                    seen.add(detail_url)
                    links.append(detail_url)
                    page_added += 1
        if page_no > 1 and page_added == 0:
            break

    if links:
        return links, first_url

    # Fallback: whole-brand legacy index. This handles an occasional missing/renamed type route.
    # URLs themselves contain model slugs, so unrelated listings are discarded before detail fetches.
    for page_no in range(1, 13):
        url = _brand_url(bike, page_no)
        try:
            page = fetch(url)
        except HTTPError as exc:
            if exc.code == 404 and page_no == 1:
                if successful_index_fetch:
                    break
                raise
            if exc.code == 404:
                break
            raise
        if "bikenet" not in page.lower() or len(page) < 1200:
            break
        page_links = listing_links(page, bike)
        new_count = 0
        for detail_url in page_links:
            if detail_url not in seen:
                seen.add(detail_url)
                links.append(detail_url)
                new_count += 1
        if page_no > 1 and new_count == 0:
            break

    return links, first_url


def search_bike(bike: dict) -> tuple[list[dict], str]:
    links, first_url = _collect_model_links(bike)
    listings: list[dict] = []
    completed = 0

    def load_one(detail_url: str):
        detail = fetch(detail_url)
        return parse_detail(detail, detail_url, bike)

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(load_one, detail_url): detail_url for detail_url in links[:90]}
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
        raise ValueError("BikeNet detail pages could not be fetched")

    unique = {x["source_id"]: x for x in listings}
    return list(unique.values()), first_url


__all__ = ["HTTPError", "URLError", "search_bike"]
