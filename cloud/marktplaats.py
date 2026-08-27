from __future__ import annotations

import html as html_lib
import re
import ssl
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote
from urllib.request import Request, urlopen

BASE = "https://www.marktplaats.nl"


@dataclass
class Link:
    href: str
    text: str


class LinkCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[Link] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self._href = href
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href is not None:
            text = " ".join(" ".join(self._text).split())
            self.links.append(Link(self._href, text))
            self._href = None
            self._text = []


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def query_slug(text: str) -> str:
    return "%2B".join(quote(part.lower(), safe="") for part in text.split())


def fetch(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/151.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache",
    }
    req = Request(url, headers=headers)
    context = ssl.create_default_context()
    with urlopen(req, timeout=30, context=context) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")


def strip_tags(fragment: str) -> str:
    fragment = re.sub(r"<script\b[^>]*>.*?</script>", " ", fragment, flags=re.I | re.S)
    fragment = re.sub(r"<style\b[^>]*>.*?</style>", " ", fragment, flags=re.I | re.S)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    return " ".join(html_lib.unescape(fragment).split())


def card_fragment(page: str, href: str) -> str:
    pos = page.find(href)
    if pos < 0:
        return ""
    starts = []
    for token in ("<article", "<li"):
        p = page.rfind(token, max(0, pos - 12000), pos)
        if p >= 0:
            starts.append((p, token))
    if starts:
        start, token = max(starts)
        close = "</article>" if token == "<article" else "</li>"
        end = page.find(close, pos, min(len(page), pos + 18000))
        if end >= 0:
            return page[start : end + len(close)]
    return page[max(0, pos - 4500) : min(len(page), pos + 7000)]


def parse_int(value: str) -> int:
    return int(re.sub(r"\D", "", value))


def parse_price(text: str) -> int | None:
    for pattern in (r"€\s*([0-9][0-9.\s]{1,10})(?:,-)?", r"EUR\s*([0-9][0-9.\s]{1,10})(?:,-)?"):
        m = re.search(pattern, text, flags=re.I)
        if m:
            try:
                value = parse_int(m.group(1))
                if 100 <= value <= 100000:
                    return value
            except ValueError:
                pass
    return None


def extract_years(text: str) -> list[int]:
    return [int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", text)]


def parse_year(title: str, text: str) -> int | None:
    """Return the listing's own year, not merely the first year allowed by our filter.

    Marktplaats cards can contain unrelated years in surrounding metadata. If the ad title
    explicitly says a year (for example "CBR600RR 2003"), that year wins even when it is
    outside BikeHunter's configured range. The normal validation layer can then reject it.
    """
    title_years = [y for y in extract_years(title) if 1980 <= y <= 2027]
    if title_years:
        return title_years[0]

    body_years = [y for y in extract_years(text) if 1980 <= y <= 2027]
    return body_years[0] if body_years else None


def parse_km(text: str) -> int | None:
    m = re.search(r"\b([0-9]{1,3}(?:[.\s][0-9]{3})+|[0-9]{3,6})\s*km\b", text, flags=re.I)
    if not m:
        return None
    try:
        value = parse_int(m.group(1))
        return value if 0 <= value <= 500000 else None
    except ValueError:
        return None


def listing_id(href: str) -> str:
    m = re.search(r"/(m\d+)[^/]*$", href.split("?", 1)[0])
    if m:
        return m.group(1)
    m = re.search(r"\bm\d+\b", href)
    return m.group(0) if m else href


def title_from_href(href: str) -> str:
    tail = unquote(href.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1])
    tail = re.sub(r"^m\d+-", "", tail)
    tail = tail.replace("-", " ")
    return " ".join(tail.split()).strip()


def clean_anchor_title(text: str) -> str:
    t = " ".join(text.split())
    t = re.sub(r"^Bewaren in Mijn Favorieten\s*", "", t, flags=re.I)
    t = re.sub(r"\s*[-–—]?\s*Details\b.*$", "", t, flags=re.I)
    return t.strip(" -–—")


def choose_title(links: list[Link], href: str) -> str:
    candidates = []
    for link in links:
        t = clean_anchor_title(link.text)
        if 8 <= len(t) <= 140 and "bewaren in mijn favorieten" not in t.lower():
            candidates.append(t)
    if candidates:
        # Search cards usually contain one concise title anchor and one giant card anchor.
        return min(candidates, key=len)
    return title_from_href(href) or "Marktplaats listing"


def match_terms_for(bike: dict) -> list[str]:
    configured = [norm(x) for x in bike.get("match_terms", []) if str(x).strip()]
    if configured:
        return configured
    model = norm(str(bike.get("model", "")))
    query = norm(str(bike.get("query", "")))
    return [x for x in (model, query) if len(x) >= 2]


def relevant(text: str, bike: dict) -> bool:
    t = norm(text)
    terms = match_terms_for(bike)
    return any(term in t for term in terms)



def extract_first_image(fragment: str) -> str | None:
    """Best-effort extraction of the first listing photo URL from a search-card fragment."""
    if not fragment:
        return None

    candidates: list[str] = []

    # Normal image attributes used by server-rendered/lazy-loaded cards.
    for attr in ("src", "data-src", "data-original"):
        pattern = r'<img\b[^>]*\b' + re.escape(attr) + r'=["\']([^"\']+)["\']'
        for m in re.finditer(pattern, fragment, flags=re.I):
            candidates.append(m.group(1))

    # Responsive images: take the first URL in each srcset.
    for m in re.finditer(r'<img\b[^>]*\bsrcset=["\']([^"\']+)["\']', fragment, flags=re.I):
        first = m.group(1).split(",", 1)[0].strip().split(" ", 1)[0]
        if first:
            candidates.append(first)

    # Fallback for image URLs embedded in JSON-ish card data.
    for m in re.finditer(r'https?:\\?/\\?/[^"\'<>\s]+', fragment, flags=re.I):
        url = m.group(0).replace("\\/", "/")
        low = url.lower()
        if any(token in low for token in ("image", "images", ".jpg", ".jpeg", ".png", ".webp")):
            candidates.append(url)

    for raw in candidates:
        url = html_lib.unescape(raw).replace("\\/", "/").strip()
        if url.startswith("//"):
            url = "https:" + url
        if not url.startswith(("http://", "https://")):
            continue
        low = url.lower()
        if any(x in low for x in ("sprite", "logo", "icon", "avatar")):
            continue
        return url
    return None


def detect_features(text: str) -> list[str]:
    low = text.lower()
    out = []
    patterns = [
        (r"\bc[- ]?abs\b", "C-ABS"),
        (r"\babs\b", "ABS"),
        (r"\b(hstc|traction control|tractiecontrole|krtc)\b", "TC"),
        (r"\bquick\s*shifter|quickshifter|kqs\b", "Quickshifter"),
        (r"\bslipper\s*clutch|slipperkoppeling\b", "Slipper clutch"),
    ]
    for pattern, label in patterns:
        if re.search(pattern, low, flags=re.I) and label not in out:
            out.append(label)
    return out


def search_bike(bike: dict) -> tuple[list[dict], str]:
    make_slug = slug(str(bike["make"]))
    query = str(bike.get("query") or f"{bike['make']} {bike['model']}")
    min_year = int(bike.get("min_year", 2011))
    max_year = int(bike.get("max_year", 2026))
    url = f"{BASE}/l/motoren/motoren-{make_slug}/q/{query_slug(query)}/"
    page = fetch(url)

    parser = LinkCollector()
    parser.feed(page)
    expected_path = f"/v/motoren/motoren-{make_slug}/"
    all_links = [x for x in parser.links if expected_path in x.href and re.search(r"\bm\d+\b", x.href)]

    grouped: dict[str, list[Link]] = {}
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
        listings.append({
            "source_id": lid,
            "bike_id": bike["id"],
            "title": title,
            "price": parse_price(text),
            "year": parse_year(title, text),
            "km": parse_km(text),
            "location": "Netherlands",
            "seller": "Marktplaats",
            "source": "Marktplaats NL",
            "country": "NL",
            "url": full_url,
            "image_url": extract_first_image(fragment),
            "features": detect_features(combined),
            "raw_text": text[:800],
        })
    return listings, url


__all__ = ["HTTPError", "URLError", "search_bike"]
