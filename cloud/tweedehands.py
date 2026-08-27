from __future__ import annotations

import re
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from marktplaats import (
    LinkCollector, card_fragment, choose_title, detect_features, extract_first_image,
    listing_id, norm, parse_km, parse_price, parse_year, query_slug, relevant, slug,
    strip_tags,
)

BASE = "https://www.2dehands.be"


def fetch(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "nl-BE,nl;q=0.9,fr-BE;q=0.8,en;q=0.7",
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache",
    }
    req = Request(url, headers=headers)
    with urlopen(req, timeout=30, context=ssl.create_default_context()) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")


def search_bike(bike: dict) -> tuple[list[dict], str]:
    make_slug = slug(str(bike["make"]))
    query = str(bike.get("query") or f"{bike['make']} {bike['model']}")
    url = f"{BASE}/l/motoren/motoren-{make_slug}/q/{query_slug(query)}/"
    page = fetch(url)

    parser = LinkCollector(); parser.feed(page)
    expected_path = f"/v/motoren/motoren-{make_slug}/"
    all_links = [x for x in parser.links if expected_path in x.href and re.search(r"\bm\d+\b", x.href)]

    grouped: dict[str, list] = {}
    href_by_id: dict[str, str] = {}
    for link in all_links:
        lid = listing_id(link.href)
        grouped.setdefault(lid, []).append(link)
        href_by_id.setdefault(lid, link.href)

    listings = []
    for lid, links in grouped.items():
        href = href_by_id[lid]
        fragment = card_fragment(page, href)
        text = strip_tags(fragment)
        title = choose_title(links, href)
        combined = f"{title} {text}"
        if not relevant(combined, bike):
            continue
        full_url = href if href.startswith("http") else BASE + href
        # 2dehands cards can contain secondary euro amounts (finance/monthly/accessory
        # amounts) before the actual asking price. The listing title often includes the
        # asking price, so prefer a price explicitly present in the title.
        title_price = parse_price(title)
        body_price = parse_price(text)
        asking_price = title_price if title_price is not None else body_price

        listings.append({
            "source_id": lid,
            "bike_id": bike["id"],
            "title": title,
            "price": asking_price,
            "year": parse_year(title, text),
            "km": parse_km(text),
            "location": "Belgium",
            "seller": "2dehands",
            "source": "2dehands BE",
            "country": "BE",
            "url": full_url,
            "image_url": extract_first_image(fragment),
            "features": detect_features(combined),
            "raw_text": text[:800],
        })
    return listings, url


__all__ = ["HTTPError", "URLError", "search_bike"]
