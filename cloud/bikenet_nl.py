from __future__ import annotations

import html as html_lib
import re
import ssl
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, unquote_plus, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

from marktplaats import detect_features, extract_first_image, relevant, strip_tags

CURRENT_BASE = "https://www.bikenet.nl"
LEGACY_BASE = "https://www2.bikenet.nl"
SOURCE_NAME = "BikeNet Dealers NL"
MAX_BRAND_PAGES = 60

# The model-filter query on BikeNet returns HTTP 200 but does not render listing cards in the
# HTML seen by a simple HTTP client.  Brand pages *do* render the ads server-side.  We therefore
# crawl each relevant brand once per scan and cache its detail links for every watched model of
# that brand.  This is both more reliable and far cheaper than crawling the same brand per bike.
_BRAND_LINK_CACHE: dict[str, list[str]] = {}
_BRAND_PAGE_COUNT: dict[str, int] = {}


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
    # BikeNet's www2 public inventory host currently presents an expired certificate.  The
    # exception is restricted to anonymous GETs to that public host; no credentials/cookies are
    # sent.  Detail pages are preferred on the valid-TLS current host.
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
        "honda-hornet-750": lambda x: ("cb750" in x or "hornet750" in x) and "hornet" in x,
    }
    if bid in rules:
        return bool(rules[bid](t))
    model = _norm(str(bike.get("model", "")))
    return model in t if model else relevant(title, bike)


def _candidate_path_match(url: str, bike: dict) -> bool:
    """Cheap pre-filter before detail fetches. Final acceptance is stricter on the detail page."""
    hint = unquote(urlparse(url).path).replace("-", " ")
    if strict_model_match(hint, bike):
        return True
    n = _norm(hint)
    bid = str(bike.get("id", ""))
    # BikeNet sometimes calls current Hornets simply CB750A in the URL.  Fetch those candidates
    # and let the detail-page text/year decide whether they are really a Hornet.
    if bid == "honda-hornet-750" and "cb750" in n:
        return True
    return False


def _brand_name(bike: dict) -> str:
    return re.sub(r"[^a-z0-9-]", "", str(bike.get("make") or "").strip().lower().replace(" ", "-"))


def _brand_url(brand: str, page: int) -> str:
    return f"{LEGACY_BASE}/occasions/?" + urlencode({"merk": brand, "pagina": max(1, int(page))})


def _canonical_detail_url(href: str) -> str | None:
    h = html_lib.unescape(href).replace("\\/", "/").strip()
    low = h.lower()
    if "/occasion/" not in low or "/occasions/" in low:
        return None
    parsed = urlparse(urljoin(LEGACY_BASE, h).split("#", 1)[0])
    host = (parsed.hostname or "").lower()
    if not (host == "bikenet.nl" or host.endswith(".bikenet.nl")):
        return None
    if "/occasion/" not in parsed.path.lower():
        return None
    suffix = parsed.path
    if parsed.query:
        suffix += "?" + parsed.query
    return CURRENT_BASE + suffix


def listing_links(page: str) -> list[str]:
    parser = HrefCollector()
    parser.feed(page)
    out: list[str] = []
    seen: set[str] = set()
    for href in parser.hrefs:
        url = _canonical_detail_url(href)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _brand_links(bike: dict) -> tuple[list[str], str]:
    brand = _brand_name(bike)
    if not brand:
        return [], LEGACY_BASE
    first_url = _brand_url(brand, 1)
    if brand in _BRAND_LINK_CACHE:
        return _BRAND_LINK_CACHE[brand], first_url

    all_links: list[str] = []
    seen: set[str] = set()
    pages_fetched = 0
    consecutive_empty = 0

    # V6/V7 proved that BikeNet's query route is reachable from GitHub.  The important discovery
    # is that adding type=... gives a 200 page whose listing cards are client-side only, while the
    # brand-only route renders the actual dealer ads in HTML.  Crawl brand pages until the list
    # ends, then reuse this cache for every watched model of that brand.
    for page_no in range(1, MAX_BRAND_PAGES + 1):
        url = _brand_url(brand, page_no)
        try:
            page = fetch(url)
        except HTTPError as exc:
            if exc.code == 404 and page_no > 1:
                break
            raise
        pages_fetched += 1
        if "bikenet" not in page.lower() or len(page) < 900:
            consecutive_empty += 1
            if consecutive_empty >= 2:
                break
            continue

        page_links = listing_links(page)
        new_count = 0
        for detail_url in page_links:
            if detail_url not in seen:
                seen.add(detail_url)
                all_links.append(detail_url)
                new_count += 1

        if new_count == 0:
            consecutive_empty += 1
        else:
            consecutive_empty = 0
        if page_no > 1 and consecutive_empty >= 2:
            break

    _BRAND_LINK_CACHE[brand] = all_links
    _BRAND_PAGE_COUNT[brand] = pages_fetched
    print(f"    BikeNet: indexed {len(all_links)} dealer ads across {pages_fetched} {brand} pages")
    return all_links, first_url


def _label_value(text: str, label: str, stop_labels: tuple[str, ...]) -> str | None:
    stops = "|".join(re.escape(x) for x in stop_labels)
    m = re.search(rf"\b{re.escape(label)}\s*:\s*(.*?)(?=\b(?:{stops})\s*:|$)", text, flags=re.I)
    return " ".join(m.group(1).split()) if m else None


def _matching_title(page: str, text: str, bike: dict) -> str | None:
    heading_candidates: list[str] = []
    for raw in re.findall(r"<h[1-3]\b[^>]*>(.*?)</h[1-3]>", page, flags=re.I | re.S):
        candidate = strip_tags(raw).strip()
        if candidate and candidate not in heading_candidates:
            heading_candidates.append(candidate)

    for candidate in heading_candidates:
        if strict_model_match(candidate, bike):
            return candidate

    make_value = _label_value(text, "Merk", ("Type", "Bouwjaar", "Kilometerstand", "Garantie"))
    type_value = _label_value(text, "Type", ("Bouwjaar", "Kilometerstand", "Garantie", "Kleur"))
    if type_value:
        combined = " ".join(x for x in (make_value, type_value) if x).strip()
        if combined and strict_model_match(combined, bike):
            return combined
        if strict_model_match(type_value, bike):
            return type_value

    m = re.search(r"<title\b[^>]*>(.*?)</title>", page, flags=re.I | re.S)
    if m:
        candidate = strip_tags(m.group(1)).strip()
        if candidate and strict_model_match(candidate, bike):
            return candidate

    # CB750A is also used by some BikeNet dealers for a modern CB750 Hornet.  Only accept that
    # broad label if the page itself explicitly says Hornet; this avoids old classic CB750 ads.
    if str(bike.get("id", "")) == "honda-hornet-750":
        page_norm = _norm(text)
        for candidate in heading_candidates:
            if "cb750" in _norm(candidate) and "hornet" in page_norm:
                return candidate
    return None


def parse_detail(page: str, url: str, bike: dict) -> dict | None:
    text = strip_tags(page)
    title = _matching_title(page, text, bike)
    if not title:
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
        "source": SOURCE_NAME,
        "country": "NL",
        "url": url,
        "image_url": extract_first_image(page),
        "conditional_price": False,
        "features": detect_features(text),
        "raw_text": text[:1400],
    }


def _fetch_detail(url: str) -> str:
    try:
        return fetch(url)
    except HTTPError as exc:
        if exc.code != 404:
            raise
        parsed = urlparse(url)
        suffix = parsed.path + (("?" + parsed.query) if parsed.query else "")
        return fetch(LEGACY_BASE + suffix)


def search_bike(bike: dict) -> tuple[list[dict], str]:
    brand_links, first_url = _brand_links(bike)
    links = [u for u in brand_links if _candidate_path_match(u, bike)]
    print(f"    BikeNet: {len(links)} model candidates for {bike.get('make')} {bike.get('model')}")

    listings: list[dict] = []
    fetched = 0

    def load_one(detail_url: str):
        detail = _fetch_detail(detail_url)
        return parse_detail(detail, detail_url, bike)

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(load_one, detail_url): detail_url for detail_url in links[:150]}
        for future in as_completed(futures):
            try:
                item = future.result()
                fetched += 1
            except (HTTPError, URLError):
                continue
            except Exception:
                fetched += 1
                continue
            if item:
                listings.append(item)

    if links and fetched == 0:
        raise ValueError("BikeNet detail pages could not be fetched")

    unique = {x["source_id"]: x for x in listings}
    parsed = list(unique.values())
    print(f"    BikeNet: {len(parsed)} parsed listings for {bike.get('make')} {bike.get('model')}")
    return parsed, first_url


__all__ = ["HTTPError", "URLError", "search_bike"]
