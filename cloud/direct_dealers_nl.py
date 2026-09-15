from __future__ import annotations

import hashlib
import html as html_lib
import re
import ssl
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, HTTPSHandler, Request, build_opener

from marktplaats import detect_features, extract_first_image, strip_tags


@dataclass(frozen=True)
class Dealer:
    key: str
    source: str
    name: str
    base: str
    city: str


GOEDHART = Dealer(
    key="goedhart",
    source="Goedhart Motoren NL",
    name="Goedhart Motoren B.V.",
    base="https://www.goedhartmotoren.nl",
    city="Bodegraven",
)

# Keep a normal browser-like session. This cuts request volume and preserves any
# cookies Goedhart sets while we inspect its public occasion catalogue.
_COOKIE_JAR = CookieJar()
_OPENER = build_opener(
    HTTPCookieProcessor(_COOKIE_JAR),
    HTTPSHandler(context=ssl.create_default_context()),
)
_PAGE_CACHE: dict[str, str] = {}
_DETAIL_CACHE: dict[str, str] = {}


class LinkCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
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
            self.links.append((self._href, " ".join(" ".join(self._text).split())))
            self._href = None
            self._text = []


def fetch(url: str, referer: str | None = None, retries: int = 1) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/151.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.7",
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache",
        "Connection": "close",
    }
    if referer:
        headers["Referer"] = referer

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = Request(url, headers=headers)
            with _OPENER.open(req, timeout=35) as response:
                raw = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, errors="replace")
        except HTTPError as exc:
            last_error = exc
            if exc.code not in (403, 408, 429, 500, 502, 503, 504) or attempt >= retries:
                raise
            time.sleep(1.0 + attempt)
        except URLError as exc:
            last_error = exc
            if attempt >= retries:
                raise
            time.sleep(1.0 + attempt)
    assert last_error is not None
    raise last_error


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
        "honda-hornet-750": lambda x: "hornet" in x and ("750" in x or "cb750" in x),
    }
    if bid in rules:
        return bool(rules[bid](t))
    model = _norm(str(bike.get("model", "")))
    return bool(model and model in t)


def _brand_slug(make: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (make or "").strip().lower()).strip("-")


def _index_url(bike: dict, page_no: int = 1) -> str:
    brand = _brand_slug(str(bike.get("make", "")))
    base = f"{GOEDHART.base}/motoren/{brand}-motoren?condition=10070"
    return base if page_no <= 1 else base + f"&p={page_no}"


def _card_fragment(page: str, href: str) -> str:
    # Inventory pages usually use relative hrefs while _extract_links normalizes
    # them to absolute URLs. Try both forms when locating the surrounding card.
    needles = [href]
    parsed = urlparse(href)
    if parsed.path:
        rel = parsed.path + (("?" + parsed.query) if parsed.query else "")
        needles += [rel, html_lib.escape(rel, quote=True)]
    pos = -1
    for needle in needles:
        pos = page.find(needle)
        if pos >= 0:
            break
    if pos < 0:
        return ""
    starts: list[tuple[int, str]] = []
    for token in ("<li", "<article", '<div class="product-item-info', "<div class='product-item-info"):
        p = page.rfind(token, max(0, pos - 15000), pos)
        if p >= 0:
            starts.append((p, token))
    if starts:
        start, token = max(starts)
        if token.startswith("<li"):
            close = "</li>"
        elif token.startswith("<article"):
            close = "</article>"
        else:
            close = "</div>"
        end = page.find(close, pos, min(len(page), pos + 20000))
        if end >= 0:
            return page[start : end + len(close)]
    return page[max(0, pos - 5000) : min(len(page), pos + 9000)]


def _extract_links(page: str) -> list[tuple[str, str]]:
    parser = LinkCollector()
    try:
        parser.feed(page)
    except Exception:
        pass
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    host = urlparse(GOEDHART.base).netloc.lower()
    for href, anchor in parser.links:
        raw = html_lib.unescape(str(href)).replace("\\/", "/").strip()
        if not raw or raw.startswith(("javascript:", "mailto:", "tel:")):
            continue
        url = urljoin(GOEDHART.base + "/", raw).split("#", 1)[0]
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != host:
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append((url, anchor))
    return out


def _parse_price(text: str) -> int | None:
    values: list[int] = []
    for m in re.finditer(r"(?:€|eur)\s*([0-9]{1,3}(?:[.\s][0-9]{3})+|[0-9]{3,5})(?:[,.][0-9]{2}|,-)?", text, flags=re.I):
        n = _num(m.group(1))
        if n is not None and 500 <= n <= 100000:
            values.append(n)
    return min(values) if values else None


def _product_candidates(page: str, bike: dict) -> list[tuple[str, int]]:
    blocked = (
        "/motoren/", "/blog", "/technische-tekeningen", "/winkel", "/contact",
        "/werkplaats", "/verhuur", "/klantenservice", "/account", "/checkout",
        "/catalogsearch", "/media/", "/static/",
    )
    out: list[tuple[str, int]] = []
    seen: set[str] = set()
    limit = int(bike.get("max_price_eur") or 999999)

    for url, anchor in _extract_links(page):
        path = unquote(urlparse(url).path).lower()
        if path in ("", "/") or any(path.startswith(x) for x in blocked):
            continue
        fragment = _card_fragment(page, url)
        card_text = strip_tags(fragment)
        combined = f"{anchor} {card_text}"
        if not strict_model_match(combined, bike):
            continue
        price = _parse_price(card_text)
        # On Goedhart's occasion category pages, a real motorcycle card contains a
        # visible asking price. Requiring it filters navigation/parts links.
        if price is None:
            continue
        # Do not open detail pages for bikes that are already outside BikeHunter's
        # configured ceiling. This also avoids pointless requests and rate limits.
        if price > limit or price < 500:
            continue
        if url not in seen:
            seen.add(url)
            out.append((url, price))
    return out


def _field(text: str, label: str, pattern: str) -> str | None:
    m = re.search(rf"\b{re.escape(label)}\s*:?\s*({pattern})", text, flags=re.I)
    return m.group(1).strip() if m else None


def _extract_title(page: str, text: str, bike: dict) -> str | None:
    for tag in ("h1", "h2", "h3", "h4"):
        for m in re.finditer(rf"<{tag}\b[^>]*>(.*?)</{tag}>", page, flags=re.I | re.S):
            value = strip_tags(m.group(1)).strip()
            if value and strict_model_match(value, bike):
                return value
    make = _field(text, "Merk", r"[A-Za-z0-9À-ÿ .+/_-]{2,40}")
    model = _field(text, "Type", r"[A-Za-z0-9À-ÿ .+/_-]{2,80}")
    combined = " ".join(x for x in (make, model) if x).strip()
    return combined if combined and strict_model_match(combined, bike) else None


def _source_id(url: str) -> str:
    path = unquote(urlparse(url).path).rstrip("/")
    tail = path.rsplit("/", 1)[-1]
    if tail and re.fullmatch(r"[A-Za-z0-9_.-]{3,140}", tail):
        return "goedhart-" + tail.lower()
    return "goedhart-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:18]


def parse_detail(page: str, url: str, bike: dict, card_price: int | None = None) -> dict | None:
    text = strip_tags(page)
    title = _extract_title(page, text, bike)
    if not title:
        return None
    price = _parse_price(text) or card_price

    year = None
    for label in ("Bouwjaar", "Modeljaar"):
        value = _field(text, label, r"(?:19|20)\d{2}")
        if value:
            year = int(re.search(r"(?:19|20)\d{2}", value).group(0))
            break

    mileage = None
    value = _field(text, "Kilometerstand", r"[0-9][0-9.\s]{0,10}\s*(?:km)?")
    if value:
        mileage = _num(value)
        if mileage is not None and not 0 <= mileage <= 500000:
            mileage = None

    return {
        "source_id": _source_id(url),
        "bike_id": bike["id"],
        "title": title,
        "price": price,
        "year": year,
        "km": mileage,
        "location": GOEDHART.city,
        "seller": GOEDHART.name,
        "source": GOEDHART.source,
        "country": "NL",
        "url": url,
        "image_url": extract_first_image(page),
        "conditional_price": False,
        "features": detect_features(text),
        "raw_text": text[:1800],
    }


def search_goedhart(bike: dict) -> tuple[list[dict], str]:
    index_url = _index_url(bike, 1)
    if index_url not in _PAGE_CACHE:
        _PAGE_CACHE[index_url] = fetch(index_url, retries=1)
    page = _PAGE_CACHE[index_url]

    candidates = _product_candidates(page, bike)
    listings: list[dict] = []
    failed = 0
    for url, card_price in candidates[:30]:
        try:
            if url not in _DETAIL_CACHE:
                time.sleep(0.35)
                _DETAIL_CACHE[url] = fetch(url, referer=index_url, retries=1)
            item = parse_detail(_DETAIL_CACHE[url], url, bike, card_price)
        except (HTTPError, URLError) as exc:
            failed += 1
            code = getattr(exc, "code", "connection")
            print(f"  {GOEDHART.source}: detail skipped ({code}) {url}")
            continue
        if item:
            listings.append(item)

    unique = {x["source_id"]: x for x in listings}
    print(
        f"  {GOEDHART.source}: {len(candidates)} affordable matching card(s) -> "
        f"{len(unique)} parsed" + (f" ({failed} detail fetch failed)" if failed else "")
    )
    return list(unique.values()), index_url


__all__ = ["HTTPError", "URLError", "search_goedhart", "strict_model_match", "parse_detail"]
