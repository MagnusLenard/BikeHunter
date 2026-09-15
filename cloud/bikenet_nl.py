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

CURRENT_BASE = "https://www.bikenet.nl"
LEGACY_BASE = "https://www2.bikenet.nl"

# BikeNet's indexed search pages use /occasions/?merk=...&pagina=...&type=...
# The type filter is a broad BikeNet bucket, not always the exact advertised model name.
# We try a few known aliases where BikeNet's naming varies.
MODEL_TYPES = {
    "honda-cbr600f": ["cbr 600"],
    "honda-cbr600rr": ["cbr 600"],
    "honda-cbr650r": ["cbr 650"],
    "honda-cbr500r": ["cbr 500"],
    "yamaha-r3": ["yzf r3", "yzf-r3"],
    "yamaha-r6": ["yzf r6", "yzf-r6", "r6"],
    "yamaha-r7": ["yzf r7", "yzf-r7", "r7"],
    "honda-rebel500": ["cmx 500", "rebel 500"],
    "triumph-street-triple-675": ["street triple", "street triple 675"],
    "triumph-daytona-675": ["daytona 675"],
    "suzuki-gsxr600": ["gsx r 600", "gsx-r 600"],
    "aprilia-rs660": ["rs 660", "rs660"],
    "honda-hornet-750": ["cb 750", "hornet 750"],
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
    # www2 currently presents an expired certificate. This exception is deliberately
    # restricted to public BikeNet GET pages. No cookies, tokens or credentials are sent.
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


def search_types_for(bike: dict) -> list[str]:
    bid = str(bike.get("id", ""))
    if MODEL_TYPES.get(bid):
        return MODEL_TYPES[bid]
    model = str(bike.get("model") or "").replace("-", " ").replace("/", " ")
    model = " ".join(model.split()).lower()
    return [model] if model else []


def _search_urls(bike: dict, page: int = 1) -> list[str]:
    brand = str(bike.get("make") or "").strip().lower()
    page = max(1, int(page))
    urls: list[str] = []
    for type_name in search_types_for(bike):
        params = urlencode({"merk": brand, "pagina": page, "type": type_name})
        # First choice: this is the actual BikeNet filter URL shape indexed by search engines.
        urls.append(f"{LEGACY_BASE}/occasions/?{params}")
        # Fallbacks for BikeNet's newer frontend, which has changed routing a few times.
        urls.append(f"{CURRENT_BASE}/occasions/?{params}")
        urls.append(f"{CURRENT_BASE}/?{params}")
    return urls


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
    # Prefer current valid-TLS detail host. A legacy fallback is used if it 404s.
    return CURRENT_BASE + suffix


def listing_links(page: str, bike: dict | None = None) -> list[str]:
    parser = HrefCollector()
    parser.feed(page)
    out: list[str] = []
    seen: set[str] = set()
    for href in parser.hrefs:
        url = _canonical_detail_url(href)
        if not url:
            continue
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


def _matching_title(page: str, text: str, bike: dict) -> str | None:
    # The current BikeNet detail frontend often places generic H1s such as
    # "Proefrit aanvragen" before the actual vehicle heading.  Taking the first H1
    # therefore silently discarded every valid listing.  Inspect all headings and
    # keep the first one that actually matches the watched motorcycle.
    heading_candidates: list[str] = []
    for raw in re.findall(r"<h[1-3]\b[^>]*>(.*?)</h[1-3]>", page, flags=re.I | re.S):
        candidate = strip_tags(raw).strip()
        if candidate and candidate not in heading_candidates:
            heading_candidates.append(candidate)

    # Some BikeNet layouts split make and model into separate headings.  A model-only
    # heading such as "YZF-R7" or "CBR 650 R ABS" is enough for strict_model_match.
    for candidate in heading_candidates:
        if strict_model_match(candidate, bike):
            return candidate

    # Structured labels are more stable than page-title boilerplate and also work when
    # the visual heading is split across multiple tags.
    make_value = _label_value(text, "Merk", ("Type", "Bouwjaar", "Kilometerstand", "Garantie"))
    type_value = _label_value(text, "Type", ("Bouwjaar", "Kilometerstand", "Garantie", "Kleur"))
    if type_value:
        combined = " ".join(x for x in (make_value, type_value) if x).strip()
        if combined and strict_model_match(combined, bike):
            return combined
        if strict_model_match(type_value, bike):
            return type_value

    # Last resort: only accept the HTML title when it really identifies the bike.
    m = re.search(r"<title\b[^>]*>(.*?)</title>", page, flags=re.I | re.S)
    if m:
        candidate = strip_tags(m.group(1)).strip()
        if candidate and strict_model_match(candidate, bike):
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
        "source": "BikeNet Dealers NL",
        "country": "NL",
        "url": url,
        "image_url": extract_first_image(page),
        "conditional_price": False,
        "features": detect_features(text),
        "raw_text": text[:1400],
    }


def _collect_links(bike: dict) -> tuple[list[str], str]:
    first_urls = _search_urls(bike, 1)
    first_url = first_urls[0] if first_urls else CURRENT_BASE
    seen: set[str] = set()
    links: list[str] = []
    any_200 = False
    last_404: HTTPError | None = None

    # Filtered model searches should be small. Three pages is enough for the watched bikes.
    for page_no in range(1, 4):
        page_added = 0
        page_had_200 = False
        for url in _search_urls(bike, page_no):
            try:
                page = fetch(url)
            except HTTPError as exc:
                if exc.code == 404:
                    last_404 = exc
                    continue
                raise
            page_had_200 = True
            any_200 = True
            if "bikenet" not in page.lower() or len(page) < 900:
                continue
            found = listing_links(page, bike)
            for detail_url in found:
                if detail_url not in seen:
                    seen.add(detail_url)
                    links.append(detail_url)
                    page_added += 1
            # As soon as one route variant yields matching listings, do not request the
            # alternate frontend copies for this page.
            if found:
                break
        if page_no > 1 and page_added == 0:
            break
        if page_no == 1 and not page_had_200 and not any_200:
            continue

    if not any_200 and last_404 is not None:
        # All route variants were 404: surface one source-level failure rather than silently
        # pretending BikeNet has no stock.
        raise last_404
    return links, first_url


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
    links, first_url = _collect_links(bike)
    print(f"    BikeNet: {len(links)} matching detail links for {bike.get('make')} {bike.get('model')}")
    listings: list[dict] = []
    completed = 0

    def load_one(detail_url: str):
        detail = _fetch_detail(detail_url)
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
    parsed = list(unique.values())
    print(f"    BikeNet: {len(parsed)} parsed listings for {bike.get('make')} {bike.get('model')}")
    return parsed, first_url


__all__ = ["HTTPError", "URLError", "search_bike"]
