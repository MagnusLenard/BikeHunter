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

BASE = "https://www.autoscout24.de"


def slug(text: str) -> str:
    value = text.lower().strip()
    value = value.replace("+", " ")
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

# AutoScout exposes some variants as a path filter rather than a separate model.
# Using the variant path avoids pulling unrelated CBR 600 F/Sport results for the RR.
KNOWN_PATH_SUFFIXES = {
    "honda-cbr600rr": "ve_rr",
}


def search_url(bike: dict) -> str:
    make = slug(str(bike.get("make", "")))
    explicit = str(bike.get("autoscout_model_slug") or "").strip()
    model = explicit or KNOWN_MODEL_SLUGS.get(str(bike.get("id")), "")
    if not model:
        # Best-effort fallback for bikes added later from Manage bikes.
        model = slug(str(bike.get("model", "")))

    params = {
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
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.7,en;q=0.6",
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
    """Flag AutoScout prices that only apply under a financing/other condition."""
    text = _text(fragment).lower()
    markers = (
        "bedingter preis",
        "nur bei finanzierung",
        "preis bei finanzierung",
        "finanzierungsbonus",
    )
    return any(marker in text for marker in markers)


def parse_price(fragment: str) -> int | None:
    """Parse the visible advertised card price.

    AutoScout can expose both an old/regular price and a reduced/current price in the
    same card. Prefer the lowest plausible *purchase* price, while ignoring tiny
    finance/monthly amounts. The detail page is verified later and remains authoritative.
    """
    text = _text(fragment)
    values: list[int] = []
    for m in re.finditer(r"(?:€|EUR|&euro;)\s*([0-9][0-9.\s]{1,10})", text, flags=re.I):
        number = _parse_int(m.group(1))
        if number is not None and 1000 <= number <= 100000:
            values.append(number)
    return min(values) if values else None


def parse_year(fragment: str) -> int | None:
    # AutoScout search cards display first registration as MM/YYYY.
    m = re.search(r"\b(?:0?[1-9]|1[0-2])\s*/\s*((?:19|20)\d{2})\b", _text(fragment))
    return int(m.group(1)) if m else None


def parse_km(fragment: str) -> int | None:
    text = _text(fragment)
    m = re.search(r"\b([0-9]{1,3}(?:[.\s][0-9]{3})+|[0-9]{1,6})\s*km\b", text, flags=re.I)
    if not m:
        return None
    value = _parse_int(m.group(1))
    if value is None or value > 500000:
        return None
    return value


def parse_title(fragment: str) -> str:
    m = re.search(r"<h2\b[^>]*>(.*?)</h2>", fragment, flags=re.I | re.S)
    if m:
        title = strip_tags(m.group(1)).strip()
        if title:
            return title
    # Fallback used if AutoScout changes h2 to another heading level.
    m = re.search(r"<h[1-4]\b[^>]*>(.*?)</h[1-4]>", fragment, flags=re.I | re.S)
    return strip_tags(m.group(1)).strip() if m else "AutoScout24 listing"


def parse_location(fragment: str) -> tuple[str, str]:
    text = _text(fragment)
    # Search cards currently contain strings such as "Privat DE-12351 Berlin, Stadt"
    # or "Dealer Name GmbH DE-72770 Reutlingen".
    m = re.search(r"\bDE-(\d{5})\s+([^|]{2,80}?)(?=(?:\s{2,}|$))", text, flags=re.I)
    if m:
        loc = f"DE-{m.group(1)} {m.group(2).strip()}"
    else:
        loc = "Germany"
    seller = "Private" if re.search(r"\bPrivat\b", text, flags=re.I) else "Dealer"
    return loc[:110], seller


def _unescape_url(value: str) -> str:
    value = html_lib.unescape(value or "")
    value = value.replace("\\u002F", "/").replace("\\/", "/")
    value = value.replace("&amp;", "&")
    return value.strip()


def extract_listing_url(fragment: str, page: str, title: str) -> str | None:
    """Return only a URL that is physically inside this exact result card.

    The old parser searched the whole page when a card link was missing. That could pair
    bike A's title/price with bike B's URL. For alerts, no link is better than a wrong link.
    """
    patterns = [
        r"href\s*=\s*[\"']([^\"']*(?:/angebote/|/offers/)[^\"']*)[\"']",
        r"[\"'](?:url|href)[\"']\s*:\s*[\"']([^\"']*(?:/angebote/|/offers/)[^\"']*)[\"']",
        r"(https?:\\?/\\?/(?:www\.)?autoscout24\.(?:de|com)[^\"'<>\s]*(?:angebote|offers)[^\"'<>\s]*)",
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
    """Conservative model check to stop broad AutoScout model pages crossing bikes."""
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
    if model:
        return model in t
    return relevant(title, bike)


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

    # Fallback to explicit price metadata / visible price labels.
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
    """Verify the exact linked advert before BikeHunter can alert on it.

    AutoScout is intentionally fail-closed here: if we cannot verify the detail page, the
    result is skipped for this scan rather than sending a potentially wrong bike/price.
    """
    try:
        page = fetch(url)
    except Exception:
        return None
    title, price = _detail_title_and_price(page)
    if not title or price is None:
        return None
    if not strict_model_match(title, bike):
        return None
    return title, price


def extract_image(fragment: str) -> str | None:
    # Reuse the generic extractor, then bias toward AutoScout's photo CDN if needed.
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
    # If markup changes and article boundaries disappear, carve chunks around the
    # dedicated AutoScout price test-id. This keeps the adapter diagnosable.
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
    return "as24-" + hashlib.sha1(raw).hexdigest()[:20]


def _visible_lines(page: str) -> list[str]:
    """Turn server-rendered AutoScout search HTML into readable text lines.

    AutoScout sometimes changes the card containers/classes while keeping the visible
    title -> price -> first registration -> mileage -> seller/location sequence intact.
    This fallback deliberately ignores CSS classes so small frontend redesigns do not
    break BikeHunter.
    """
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", page, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"</(?:h1|h2|h3|h4|div|p|li|article|section|span|button|a)\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    lines = []
    for raw in text.splitlines():
        line = " ".join(raw.split()).strip()
        if line:
            lines.append(line)
    return lines


def _fallback_text_listings(page: str, bike: dict, search_page_url: str) -> list[dict]:
    """Parse listing rows from visible text when AutoScout card markup is unfamiliar."""
    lines = _visible_lines(page)
    out: list[dict] = []
    price_re = re.compile(r"^(?:€|EUR)\s*([0-9][0-9.\s]{1,10})(?:\s|$)", flags=re.I)
    date_re = re.compile(r"\b(?:0?[1-9]|1[0-2])\s*/\s*((?:19|20)\d{2})\b")
    km_re = re.compile(r"\b([0-9]{1,3}(?:[.\s][0-9]{3})+|[0-9]{1,6})\s*km\b", flags=re.I)

    for i, line in enumerate(lines):
        pm = price_re.search(line)
        if not pm:
            continue
        price = _parse_int(pm.group(1))
        if price is None or not (100 <= price <= 100000):
            continue

        year = None
        mileage = None
        location = "Germany"
        seller = "Dealer"
        for later in lines[i + 1:i + 18]:
            if year is None:
                dm = date_re.search(later)
                if dm:
                    year = int(dm.group(1))
            if mileage is None:
                km_m = km_re.search(later)
                if km_m:
                    mileage = _parse_int(km_m.group(1))
            if "DE-" in later:
                loc_m = re.search(r"(?:Privat\s+)?(DE-\d{5}\s+.+)", later, flags=re.I)
                if loc_m:
                    location = loc_m.group(1).strip()[:110]
                seller = "Private" if re.search(r"\bPrivat\b", later, flags=re.I) else "Dealer"
                break

        if year is None or mileage is None:
            continue

        title = None
        for prev in reversed(lines[max(0, i - 14):i]):
            low = prev.lower()
            if low in {"merken", "neu"} or "angebote für" in low or "sortieren" in low or "filter" == low:
                continue
            if len(prev) < 6 or len(prev) > 180:
                continue
            if relevant(prev, bike):
                title = prev
                break
        if not title:
            continue

        context = ' '.join(lines[max(0, i-3):i+18])
        combined = f"{title} {context}"
        is_conditional = any(marker in context.lower() for marker in (
            "bedingter preis", "nur bei finanzierung", "preis bei finanzierung", "finanzierungsbonus"
        ))
        source_id = _fingerprint(title, price, year, mileage, location)
        out.append({
            "source_id": source_id,
            "bike_id": bike["id"],
            "title": title,
            "price": price,
            "year": year,
            "km": mileage,
            "location": location,
            "seller": seller,
            "source": "AutoScout24 DE",
            "country": "DE",
            "url": search_page_url,
            "image_url": None,
            "conditional_price": is_conditional,
            "features": detect_features(combined),
            "raw_text": combined[:1200],
        })

    unique = {}
    for x in out:
        unique[x["source_id"]] = x
    return list(unique.values())


def search_bike(bike: dict) -> tuple[list[dict], str]:
    url = search_url(bike)
    page = fetch(url)

    low = page.lower()
    if len(page) < 100000 or "autoscout24" not in low:
        raise ValueError("AutoScout24 returned an unexpectedly small/unrecognized page")
    if "access denied" in low or "verify you are human" in low:
        raise ValueError("AutoScout24 returned a block/challenge page")

    fragments = _article_fragments(page)
    if not fragments:
        fragments = _fallback_fragments(page)

    listings: list[dict] = []
    parsed_card_count = 0
    relevant_card_count = 0
    for fragment in fragments:
        title = parse_title(fragment)
        combined = f"{title} {_text(fragment)}"
        is_conditional = conditional_price(fragment)
        # Count cards that look structurally parseable even when they belong to a
        # different CBR 600 variant. This prevents false parser warnings.
        probe_price = parse_price(fragment)
        probe_year = parse_year(fragment)
        probe_km = parse_km(fragment)
        if probe_price is not None and (probe_year is not None or probe_km is not None):
            parsed_card_count += 1
        if not strict_model_match(title, bike):
            continue
        relevant_card_count += 1

        price = probe_price
        year = probe_year
        mileage = probe_km
        location, seller = parse_location(fragment)
        direct_url = extract_listing_url(fragment, page, title)
        if not direct_url:
            continue

        verified = verify_detail(direct_url, bike)
        if not verified:
            continue
        verified_title, verified_price = verified
        title = verified_title
        price = verified_price
        combined = f"{title} {_text(fragment)}"

        source_id = None
        uuid_match = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", direct_url, flags=re.I)
        if uuid_match:
            source_id = uuid_match.group(1).lower()
        if not source_id:
            source_id = _fingerprint(title, price, year, mileage, location)

        listings.append({
            "source_id": source_id,
            "bike_id": bike["id"],
            "title": title,
            "price": price,
            "year": year,
            "km": mileage,
            "location": location,
            "seller": seller,
            "source": "AutoScout24 DE",
            "country": "DE",
            "url": direct_url,
            "image_url": extract_image(fragment),
            "conditional_price": is_conditional,
            "features": detect_features(combined),
            "raw_text": _text(fragment)[:1200],
        })

    # Do not use the old text-only fallback for alerts: it cannot reliably pair a
    # visible result with its exact advert URL. Accuracy is more important than count.

    # A 200 response with lots of search-page data but zero parsed cards is a parser
    # problem, not a healthy empty result. Surface it as a warning for easy diagnosis.
    offer_count = None
    plain = _text(page)
    m = re.search(r"#?\s*([0-9]{1,4})\s+Angebote\s+für", plain, flags=re.I)
    if m:
        offer_count = int(m.group(1))
    if not listings and offer_count and offer_count > 0:
        # If AutoScout gave us real, parseable cards but none matched the exact bike
        # (for example CBR 600 F cards on a broad CBR 600 page), that is a healthy
        # zero-result search rather than a parser failure.
        if parsed_card_count == 0 and relevant_card_count == 0:
            raise ValueError(f"page says {offer_count} offers but BikeHunter could not parse the result cards")

    return listings, url


__all__ = ["HTTPError", "URLError", "search_bike", "search_url"]
