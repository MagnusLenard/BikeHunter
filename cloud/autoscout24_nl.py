from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from marktplaats import detect_features, extract_first_image, relevant, strip_tags

BASE = "https://www.autoscout24.nl"


def slug(text: str) -> str:
    value = text.lower().strip().replace("+", " ")
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


KNOWN_MODEL_SLUGS = {
    "honda-cbr600f": "cbr-600",
    "honda-cbr600rr": "cbr-600",
    "honda-cbr650r": "cbr-650",
    "honda-cbr500r": "cbr-500-r",
    "yamaha-r3": "yzf-r3",
    "yamaha-r6": "yzf-r6",
    "yamaha-r7": "yzf-r7",
    "honda-rebel500": "cmx-500",
    "triumph-street-triple-675": "street-triple-675",
    "triumph-daytona-675": "daytona-675",
    "suzuki-gsxr600": "gsx-r-600",
}

KNOWN_PATH_SUFFIXES = {
    "honda-cbr600rr": "ve_rr",
}


def search_url(bike: dict) -> str:
    make = slug(str(bike.get("make", "")))
    explicit = str(bike.get("autoscout_model_slug") or "").strip()
    model = explicit or KNOWN_MODEL_SLUGS.get(str(bike.get("id")), "") or slug(str(bike.get("model", "")))
    params = {
        "cy": "NL",
        "fregfrom": int(bike.get("min_year", 2011)),
        "fregto": int(bike.get("max_year", 2026)),
        "priceto": int(bike.get("max_price_eur") or 6000),
        "sort": "standard",
        "desc": 0,
    }
    suffix = str(bike.get("autoscout_path_suffix") or KNOWN_PATH_SUFFIXES.get(str(bike.get("id")), "")).strip("/")
    path = f"{BASE}/lst-moto/{quote(make)}/{quote(model)}"
    if suffix:
        path += f"/{quote(suffix)}"
    return f"{path}?{urlencode(params)}"


def fetch(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.7,en;q=0.6",
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
    }
    req = Request(url, headers=headers)
    with urlopen(req, timeout=35, context=ssl.create_default_context()) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")


def _text(fragment: str) -> str:
    return strip_tags(fragment)


def _parse_int(text: str) -> int | None:
    digits = re.sub(r"\D", "", text or "")
    return int(digits) if digits else None


def conditional_price(fragment: str) -> bool:
    text = _text(fragment).lower()
    markers = (
        "prijs bij financiering", "alleen bij financiering", "financieringskorting",
        "financieringsbonus", "vanaf per maand", "leaseprijs",
    )
    return any(marker in text for marker in markers)


def parse_price(fragment: str) -> int | None:
    text = _text(fragment)
    values: list[int] = []
    for m in re.finditer(r"(?:€|EUR|&euro;)\s*([0-9]{1,3}(?:[.\s][0-9]{3})+|[0-9]{3,6})\b", text, flags=re.I):
        number = _parse_int(m.group(1))
        if number is not None and 1000 <= number <= 100000:
            values.append(number)
    return min(values) if values else None


def parse_year(fragment: str) -> int | None:
    m = re.search(r"\b(?:0?[1-9]|1[0-2])\s*/\s*((?:19|20)\d{2})\b", _text(fragment))
    return int(m.group(1)) if m else None


def parse_km(fragment: str) -> int | None:
    m = re.search(r"\b([0-9]{1,3}(?:[.\s][0-9]{3})+|[0-9]{1,6})\s*km\b", _text(fragment), flags=re.I)
    if not m:
        return None
    value = _parse_int(m.group(1))
    return value if value is not None and value <= 500000 else None


def parse_title(fragment: str) -> str:
    m = re.search(r"<h2\b[^>]*>(.*?)</h2>", fragment, flags=re.I | re.S)
    if m:
        title = strip_tags(m.group(1)).strip()
        if title:
            return title
    m = re.search(r"<h[1-4]\b[^>]*>(.*?)</h[1-4]>", fragment, flags=re.I | re.S)
    return strip_tags(m.group(1)).strip() if m else "AutoScout24 listing"


def parse_location(fragment: str) -> tuple[str, str]:
    text = _text(fragment)
    # Typical cards: "Particulier NL-2324 Leiden" or "Dealer B.V. NL-1234 AB City".
    m = re.search(r"\bNL-([0-9]{4}\s?[A-Z]{0,2})\s+([^|]{2,80}?)(?=(?:\s{2,}|$))", text, flags=re.I)
    loc = f"NL-{m.group(1).strip()} {m.group(2).strip()}" if m else "Netherlands"
    seller = "Private" if re.search(r"\bParticulier\b", text, flags=re.I) else "Dealer"
    return loc[:110], seller


def _unescape_url(value: str) -> str:
    value = html_lib.unescape(value or "")
    value = value.replace("\\u002F", "/").replace("\\/", "/").replace("&amp;", "&")
    return value.strip()


def extract_listing_url(fragment: str) -> str | None:
    # NL detail URLs use /aanbod/. Never search the entire page: the URL must belong
    # to this exact card or we risk pairing bike A with bike B's advert.
    patterns = [
        r"href\s*=\s*[\"']([^\"']*(?:/aanbod/|/offers/)[^\"']*)[\"']",
        r"[\"'](?:url|href)[\"']\s*:\s*[\"']([^\"']*(?:/aanbod/|/offers/)[^\"']*)[\"']",
        r"(https?:\\?/\\?/(?:www\.)?autoscout24\.(?:nl|com)[^\"'<>\s]*(?:aanbod|offers)[^\"'<>\s]*)",
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, fragment, flags=re.I):
            value = _unescape_url(m.group(1))
            if value.startswith("//"):
                return "https:" + value
            if value.startswith("/"):
                return BASE + value
            if value.startswith("http"):
                return value
    return None


def _norm_model(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def strict_model_match(title: str, bike: dict) -> bool:
    t = _norm_model(title)
    model = _norm_model(str(bike.get("model", "")))
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
    }
    if bid in rules:
        return bool(rules[bid](t))
    return model in t if model else relevant(title, bike)


def _jsonld_objects(page: str):
    for m in re.finditer(r"<script\b[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", page, flags=re.I | re.S):
        raw = html_lib.unescape(m.group(1)).strip()
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        stack = obj if isinstance(obj, list) else [obj]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                yield cur
                for v in cur.values():
                    if isinstance(v, dict):
                        stack.append(v)
                    elif isinstance(v, list):
                        stack.extend(x for x in v if isinstance(x, (dict, list)))
            elif isinstance(cur, list):
                stack.extend(cur)


def _detail_title_and_price(page: str) -> tuple[str | None, int | None]:
    titles: list[str] = []
    prices: list[int] = []
    for obj in _jsonld_objects(page):
        typ = str(obj.get("@type", "")).lower()
        if typ in {"product", "vehicle", "motorcycle", "offer"} or "offer" in typ:
            name = obj.get("name") or obj.get("headline")
            if isinstance(name, str) and 3 <= len(name.strip()) <= 220:
                titles.append(name.strip())
            for key in ("price", "lowPrice", "highPrice"):
                val = obj.get(key)
                if val is not None:
                    n = _parse_int(str(val))
                    if n is not None and 1000 <= n <= 100000:
                        prices.append(n)
            offers = obj.get("offers")
            if isinstance(offers, dict):
                for key in ("price", "lowPrice", "highPrice"):
                    val = offers.get(key)
                    if val is not None:
                        n = _parse_int(str(val))
                        if n is not None and 1000 <= n <= 100000:
                            prices.append(n)

    if not titles:
        m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', page, flags=re.I)
        if m:
            titles.append(html_lib.unescape(m.group(1)).strip())
        else:
            m = re.search(r"<h1\b[^>]*>(.*?)</h1>", page, flags=re.I | re.S)
            if m:
                titles.append(strip_tags(m.group(1)).strip())

    if not prices:
        for pat in (
            r'["\']price["\']\s*[:=]\s*["\']?([0-9][0-9.\s]{2,10})',
            r'(?:€|EUR)\s*([0-9][0-9.\s]{2,10})',
        ):
            for m in re.finditer(pat, page, flags=re.I):
                n = _parse_int(m.group(1))
                if n is not None and 1000 <= n <= 100000:
                    prices.append(n)
    return (titles[0] if titles else None, min(prices) if prices else None)


def verify_detail(url: str, bike: dict) -> tuple[str, int] | None:
    try:
        page = fetch(url)
    except Exception:
        return None
    title, price = _detail_title_and_price(page)
    if not title or price is None or not strict_model_match(title, bike):
        return None
    return title, price


def extract_image(fragment: str) -> str | None:
    value = extract_first_image(fragment)
    if value and "autoscout24" in value.lower():
        return value
    for pattern in (
        r"https?:\\?/\\?/prod\.pictures\.autoscout24\.net/[^\"'<>\s]+",
        r"https?://prod\.pictures\.autoscout24\.net/[^\"'<>\s]+",
    ):
        m = re.search(pattern, fragment, flags=re.I)
        if m:
            return _unescape_url(m.group(0))
    return value


def _article_fragments(page: str) -> list[str]:
    return re.findall(r"<article\b[^>]*>.*?</article>", page, flags=re.I | re.S)


def _fallback_fragments(page: str) -> list[str]:
    positions = [m.start() for m in re.finditer(r"data-testid=[\"']regular-price[\"']", page, flags=re.I)]
    out = []
    for pos in positions:
        start = max(0, page.rfind("<h2", max(0, pos - 10000), pos))
        if start == 0 and "<h2" not in page[:pos]:
            start = max(0, pos - 5000)
        end = page.find("<h2", pos + 1, min(len(page), pos + 18000))
        if end < 0:
            end = min(len(page), pos + 10000)
        out.append(page[start:end])
    return out


def _fingerprint(title: str, price: int | None, year: int | None, km: int | None, location: str) -> str:
    raw = f"{title}|{price}|{year}|{km}|{location}".encode("utf-8", errors="ignore")
    return "as24nl-" + hashlib.sha1(raw).hexdigest()[:20]


def search_bike(bike: dict) -> tuple[list[dict], str]:
    url = search_url(bike)
    page = fetch(url)
    low = page.lower()
    if len(page) < 50000 or "autoscout24" not in low:
        raise ValueError("AutoScout24 NL returned an unexpectedly small/unrecognized page")
    if "access denied" in low or "verify you are human" in low:
        raise ValueError("AutoScout24 NL returned a block/challenge page")

    fragments = _article_fragments(page) or _fallback_fragments(page)
    listings: list[dict] = []
    parsed_card_count = 0
    relevant_card_count = 0
    for fragment in fragments:
        title = parse_title(fragment)
        is_conditional = conditional_price(fragment)
        probe_price = parse_price(fragment)
        probe_year = parse_year(fragment)
        probe_km = parse_km(fragment)
        if probe_price is not None and (probe_year is not None or probe_km is not None):
            parsed_card_count += 1
        if not strict_model_match(title, bike):
            continue
        relevant_card_count += 1

        direct_url = extract_listing_url(fragment)
        if not direct_url:
            continue
        verified = verify_detail(direct_url, bike)
        if not verified:
            continue
        verified_title, verified_price = verified
        location, seller = parse_location(fragment)

        uuid_match = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", direct_url, flags=re.I)
        source_id = uuid_match.group(1).lower() if uuid_match else _fingerprint(verified_title, verified_price, probe_year, probe_km, location)
        combined = f"{verified_title} {_text(fragment)}"
        listings.append({
            "source_id": source_id,
            "bike_id": bike["id"],
            "title": verified_title,
            "price": verified_price,
            "year": probe_year,
            "km": probe_km,
            "location": location,
            "seller": seller,
            "source": "AutoScout24 NL",
            "country": "NL",
            "url": direct_url,
            "image_url": extract_image(fragment),
            "conditional_price": is_conditional,
            "features": detect_features(combined),
            "raw_text": _text(fragment)[:1200],
        })

    # A populated result page with no structurally parseable cards is probably a site
    # markup change. Raise a warning instead of silently deactivating old listings.
    plain = _text(page)
    offer_count = None
    m = re.search(r"#?\s*([0-9.]{1,6})\s+resultaten", plain, flags=re.I)
    if m:
        offer_count = _parse_int(m.group(1))
    if not listings and offer_count and offer_count > 0 and parsed_card_count == 0 and relevant_card_count == 0:
        raise ValueError(f"page says {offer_count} results but BikeHunter could not parse the result cards")

    return listings, url


__all__ = ["HTTPError", "URLError", "search_bike", "search_url"]
