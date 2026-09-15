from __future__ import annotations

import hashlib
import html as html_lib
import re
import ssl
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import Request, urlopen

from marktplaats import detect_features, extract_first_image, relevant, strip_tags


@dataclass(frozen=True)
class Dealer:
    key: str
    source: str
    name: str
    base: str
    city: str
    max_pages: int = 40


DEALERS = {
    "arie": Dealer(
        key="arie",
        source="Arie Molenaar Motors NL",
        name="Arie Molenaar Motors B.V.",
        base="https://occasions.ariemolenaarmotors.nl",
        city="IJsselstein",
        max_pages=20,
    ),
    "goedhart": Dealer(
        key="goedhart",
        source="Goedhart Motoren NL",
        name="Goedhart Motoren B.V.",
        base="https://occasions.goedhartmotoren.nl",
        city="Bodegraven",
        max_pages=40,
    ),
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


_INDEX_CACHE: dict[str, list[str]] = {}
_DETAIL_CACHE: dict[str, str] = {}


def fetch(url: str) -> str:
    req = Request(url, headers={
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
    with urlopen(req, timeout=35, context=ssl.create_default_context()) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _num(value: str) -> int | None:
    digits = re.sub(r"\D", "", value or "")
    return int(digits) if digits else None


def strict_model_match(text: str, bike: dict) -> bool:
    t = _norm(text)
    bid = str(bike.get("id", ""))
    rules = {
        "honda-cbr600rr": lambda x: "cbr600rr" in x,
        "honda-cbr600f": lambda x: "cbr600f" in x and "cbr600rr" not in x,
        "honda-cbr650r": lambda x: "cbr650r" in x and "cb650r" not in x,
        "honda-cbr500r": lambda x: "cbr500r" in x and "cb500" not in x,
        "yamaha-r3": lambda x: "yzfr3" in x or "yamahar3" in x,
        "yamaha-r6": lambda x: "yzfr6" in x or "yamahar6" in x,
        "yamaha-r7": lambda x: "yzfr7" in x or "yamahar7" in x,
        "honda-rebel500": lambda x: "cmx500" in x or "rebel500" in x,
        "triumph-street-triple-675": lambda x: "streettriple675" in x or ("streettriple" in x and "675" in x),
        "triumph-daytona-675": lambda x: "daytona675" in x,
        "suzuki-gsxr600": lambda x: "gsxr600" in x,
        "aprilia-rs660": lambda x: "rs660" in x,
        "honda-hornet-750": lambda x: ("cb750" in x and "hornet" in x) or "hornet750" in x,
    }
    if bid in rules:
        return bool(rules[bid](t))
    model = _norm(str(bike.get("model", "")))
    return model in t if model else relevant(text, bike)


_URL_ALIASES = {
    "honda-cbr600f": ("cbr600f", "cbr600fa"),
    "honda-cbr600rr": ("cbr600rr",),
    "honda-cbr650r": ("cbr650r",),
    "honda-cbr500r": ("cbr500r",),
    "yamaha-r3": ("yzfr3",),
    "yamaha-r6": ("yzfr6",),
    "yamaha-r7": ("yzfr7",),
    "honda-rebel500": ("cmx500", "rebel500"),
    "triumph-street-triple-675": ("streettriple675",),
    "triumph-daytona-675": ("daytona675",),
    "suzuki-gsxr600": ("gsxr600",),
    "aprilia-rs660": ("rs660",),
    "honda-hornet-750": ("cb750", "hornet750"),
}


def _candidate_url(url: str, bike: dict) -> bool:
    # BikeNet-powered dealer URLs include the actual model in /unit/<brand>/<model>/<id>.
    # Decode %2B/+/hyphens away before comparing so the pre-filter is strict but cheap.
    path = unquote(urlparse(url).path).replace("+", " ")
    n = _norm(path)
    aliases = _URL_ALIASES.get(str(bike.get("id", "")))
    if aliases:
        return any(alias in n for alias in aliases)
    model = _norm(str(bike.get("model", "")))
    return bool(model and model in n)


def _extract_unit_links(page: str, dealer: Dealer) -> list[str]:
    parser = HrefCollector()
    try:
        parser.feed(page)
    except Exception:
        pass

    candidates = list(parser.hrefs)
    # Fallbacks for sites that render the URL inside JSON/JS instead of a normal anchor.
    candidates += re.findall(r"(?:https?://[^\"'<>\\]+)?(/unit/[^\"'<>\\?#]+)", page, flags=re.I)
    candidates += [m.replace("\\/", "/") for m in re.findall(r"(?:https?:\\/\\/[^\"'<>]+)?(?:\\/unit\\/[^\"'<>?#]+)", page, flags=re.I)]

    out: list[str] = []
    seen: set[str] = set()
    dealer_host = urlparse(dealer.base).netloc.lower()
    for href in candidates:
        h = html_lib.unescape(str(href)).replace("\\/", "/").strip()
        if "/unit/" not in h.lower():
            continue
        url = urljoin(dealer.base + "/", h).split("#", 1)[0]
        parsed = urlparse(url)
        # Keep the dealer-owned host. This avoids accidentally following image/CDN links.
        if parsed.netloc.lower() != dealer_host:
            continue
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _index_url(dealer: Dealer, page_no: int) -> str:
    if page_no <= 1:
        return dealer.base + "/index.php"
    return dealer.base + f"/index.php?pagina={page_no}"


def _crawl_inventory(dealer: Dealer) -> list[str]:
    cached = _INDEX_CACHE.get(dealer.key)
    if cached is not None:
        return cached

    all_links: list[str] = []
    seen: set[str] = set()
    successful_pages = 0
    for page_no in range(1, dealer.max_pages + 1):
        url = _index_url(dealer, page_no)
        try:
            page = fetch(url)
        except HTTPError as exc:
            if page_no > 1 and getattr(exc, "code", None) in (404, 410):
                break
            raise

        links = _extract_unit_links(page, dealer)
        if page_no == 1 and not links:
            raise ValueError(f"{dealer.name} inventory page returned no /unit/ listing links")
        if not links:
            break

        successful_pages += 1
        new_links = [u for u in links if u not in seen]
        if not new_links:
            break
        for u in new_links:
            seen.add(u)
            all_links.append(u)

        # BikeNet dealer indexes normally show roughly a page of listings. If the final
        # page contains fewer than a full page, the next request is unnecessary; repeat
        # detection above is still the authoritative stop condition.
        if page_no > 1 and len(links) < 5:
            break

    if successful_pages == 0:
        raise ValueError(f"{dealer.name} inventory could not be read")
    _INDEX_CACHE[dealer.key] = all_links
    print(f"  {dealer.source}: indexed {len(all_links)} dealer listings across {successful_pages} page(s)")
    return all_links


def _detail_page(url: str) -> str:
    if url not in _DETAIL_CACHE:
        _DETAIL_CACHE[url] = fetch(url)
    return _DETAIL_CACHE[url]


def _field(text: str, label: str, pattern: str) -> str | None:
    # BikeNet detail pages render fields as e.g. "Bouwjaar:  |  2026". strip_tags()
    # turns this into normal text, so tolerate punctuation/separators between label/value.
    m = re.search(rf"\b{re.escape(label)}\s*:?\s*(?:\|\s*)?({pattern})", text, flags=re.I)
    return m.group(1).strip() if m else None


def _extract_title(page: str, text: str, bike: dict) -> str | None:
    headings = []
    for tag in ("h1", "h2", "h3"):
        for m in re.finditer(rf"<{tag}\b[^>]*>(.*?)</{tag}>", page, flags=re.I | re.S):
            value = strip_tags(m.group(1)).strip()
            if value:
                headings.append(value)
    for value in headings:
        if strict_model_match(value, bike):
            return value

    make = _field(text, "Merk", r"[A-Za-z0-9À-ÿ .+/_-]{2,40}")
    model = _field(text, "Type", r"[A-Za-z0-9À-ÿ .+/_-]{2,80}")
    combined = " ".join(x for x in (make, model) if x).strip()
    if combined and strict_model_match(combined, bike):
        return combined
    return None


def _parse_price(text: str) -> int | None:
    # Stay near the top of the detail page so financing examples lower on the page cannot
    # masquerade as the asking price. When a crossed-out old price and a current price are
    # both present, the lower amount is the actual sale price.
    head = text[:2600]
    values: list[int] = []
    for m in re.finditer(r"€\s*([0-9]{1,3}(?:[.\s][0-9]{3})+|[0-9]{3,5})(?:,-)?", head, flags=re.I):
        n = _num(m.group(1))
        if n is not None and 500 <= n <= 100000:
            values.append(n)
    return min(values) if values else None


def _source_id(url: str, dealer: Dealer) -> str:
    tail = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    if tail and re.fullmatch(r"[A-Za-z0-9_-]{3,60}", tail):
        return tail
    return dealer.key + "-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:18]


def parse_detail(page: str, url: str, bike: dict, dealer: Dealer) -> dict | None:
    text = strip_tags(page)
    title = _extract_title(page, text, bike)
    if not title:
        return None

    price = _parse_price(text)

    year = None
    value = _field(text, "Bouwjaar", r"(?:19|20)\d{2}")
    if not value:
        value = _field(text, "Modeljaar", r"(?:19|20)\d{2}")
    if value:
        m = re.search(r"(?:19|20)\d{2}", value)
        if m:
            year = int(m.group(0))

    mileage = None
    value = _field(text, "Kilometerstand", r"[0-9][0-9.\s]{0,10}\s*km")
    if value:
        mileage = _num(value)
        if mileage is not None and not 0 <= mileage <= 500000:
            mileage = None

    return {
        "source_id": _source_id(url, dealer),
        "bike_id": bike["id"],
        "title": title,
        "price": price,
        "year": year,
        "km": mileage,
        "location": dealer.city,
        "seller": dealer.name,
        "source": dealer.source,
        "country": "NL",
        "url": url,
        "image_url": extract_first_image(page),
        "conditional_price": False,
        "features": detect_features(text),
        "raw_text": text[:1800],
    }


def _search(dealer: Dealer, bike: dict) -> tuple[list[dict], str]:
    links = _crawl_inventory(dealer)
    candidates = [u for u in links if _candidate_url(u, bike)]
    listings: list[dict] = []
    fetch_failures = 0
    for url in candidates[:80]:
        try:
            page = _detail_page(url)
            item = parse_detail(page, url, bike, dealer)
        except (HTTPError, URLError):
            fetch_failures += 1
            continue
        if item:
            listings.append(item)

    # If every pre-filtered candidate failed to load, surface it as a source warning rather
    # than silently reporting zero stock. Zero candidates is normal and not an error.
    if candidates and fetch_failures == len(candidates):
        raise ValueError(f"{dealer.name} matching detail pages could not be fetched")

    unique = {x["source_id"]: x for x in listings}
    print(f"  {dealer.source}: {len(candidates)} matching detail link(s) -> {len(unique)} parsed")
    return list(unique.values()), dealer.base + "/index.php"


def search_arie_molenaar(bike: dict) -> tuple[list[dict], str]:
    return _search(DEALERS["arie"], bike)


def search_goedhart(bike: dict) -> tuple[list[dict], str]:
    return _search(DEALERS["goedhart"], bike)


__all__ = [
    "HTTPError",
    "URLError",
    "search_arie_molenaar",
    "search_goedhart",
    "strict_model_match",
    "parse_detail",
]
