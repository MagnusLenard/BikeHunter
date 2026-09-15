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
    # Use the plain brand catalogue as the primary route. Goedhart's public
    # brand pages are server-rendered and expose the real stock-unit links.
    # The ?condition=10070 route has proven inconsistent for this cloud runner,
    # so condition is verified on each product detail page instead.
    brand = _brand_slug(str(bike.get("make", "")))
    base = f"{GOEDHART.base}/motoren/{brand}-motoren"
    return base if page_no <= 1 else base + f"?p={page_no}"


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

    # Magento product cards commonly link the same product more than once:
    # first an image-only <a> (empty anchor text), then the actual product-name
    # <a>.  Older BikeHunter code kept the *first* URL and discarded the later
    # title link, so every real Goedhart product could silently fail matching.
    # Keep the best/non-empty anchor text seen for each URL instead.
    by_url: dict[str, str] = {}
    host = urlparse(GOEDHART.base).netloc.lower()
    for href, anchor in parser.links:
        raw = html_lib.unescape(str(href)).replace("\\/", "/").strip()
        if not raw or raw.startswith(("javascript:", "mailto:", "tel:")):
            continue
        url = urljoin(GOEDHART.base + "/", raw).split("#", 1)[0]
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != host:
            continue
        clean_anchor = " ".join((anchor or "").split())
        previous = by_url.get(url, "")
        if url not in by_url or (clean_anchor and len(clean_anchor) > len(previous)):
            by_url[url] = clean_anchor
    return list(by_url.items())


def _parse_price(text: str) -> int | None:
    values: list[int] = []
    for m in re.finditer(r"(?:€|eur)\s*([0-9]{1,3}(?:[.\s][0-9]{3})+|[0-9]{3,5})(?:[,.][0-9]{2}|,-)?", text, flags=re.I):
        # Dutch prices are usually rendered as € 9.690,00.  Treat dots/spaces
        # as thousands separators and ignore the cents instead of turning that
        # into 969000.
        raw = m.group(0)
        amount = re.sub(r"^(?:€|eur)\s*", "", raw, flags=re.I).strip()
        amount = re.sub(r"(?:,[0-9]{2}|,-)$", "", amount)
        amount = amount.replace(".", "").replace(" ", "")
        n = int(amount) if amount.isdigit() else None
        if n is not None and 500 <= n <= 100000:
            values.append(n)
    return min(values) if values else None


def _stock_urls_from_page(page: str) -> list[tuple[str, str]]:
    """Return Goedhart motorcycle stock URLs using DOM links and raw HTML."""
    by_url: dict[str, str] = {u: a for u, a in _extract_links(page)}
    host = urlparse(GOEDHART.base).netloc.lower()
    patterns = (
        r"href=[\"']([^\"']*?-rhc[0-9a-z-]+(?:[/?#][^\"']*)?)[\"']",
        r"https?://www\.goedhartmotoren\.nl/[^\s\"'<>]*?-rhc[0-9a-z-]+[^\s\"'<>]*",
    )
    for pat in patterns:
        for m in re.finditer(pat, page, flags=re.I):
            raw = html_lib.unescape(m.group(1) if m.lastindex else m.group(0)).replace('\\/', '/')
            url = urljoin(GOEDHART.base + '/', raw).split('#', 1)[0]
            parsed = urlparse(url)
            if parsed.scheme in ('http', 'https') and parsed.netloc.lower() == host:
                by_url.setdefault(url, '')
    return list(by_url.items())


def _product_candidates(page: str, bike: dict) -> list[tuple[str, int | None]]:
    # V3 required a price from a guessed card fragment, which silently dropped
    # real products. V4 identifies the actual stock-unit URL first and verifies
    # price/year on the detail page.
    blocked = (
        "/motoren/", "/blog", "/technische-tekeningen", "/winkel", "/contact",
        "/werkplaats", "/verhuur", "/klantenservice", "/account", "/checkout",
        "/catalogsearch", "/media/", "/static/",
    )
    out: list[tuple[str, int | None]] = []
    seen: set[str] = set()

    for url, anchor in _stock_urls_from_page(page):
        path = unquote(urlparse(url).path).lower().rstrip("/")
        if path in ("", "/") or any(path.startswith(x) for x in blocked):
            continue

        fragment = _card_fragment(page, url)
        card_text = strip_tags(fragment)
        # Goedhart's canonical stock URL contains the model, e.g.
        # /yamaha-yzf-r7-rhc00002463857. Match that first so image-only cards
        # or Magento markup changes cannot silently discard the product.
        if not (strict_model_match(url, bike) or strict_model_match(f"{anchor} {card_text}", bike)):
            continue
        tail = path.rsplit("/", 1)[-1]
        stock_like = bool(re.search(r"(?:^|-)rhc[0-9a-z-]+$", tail, flags=re.I))
        occasion_like = "occasion" in card_text.lower()
        if not stock_like and not occasion_like:
            continue

        price = _parse_price(card_text)
        if url not in seen:
            seen.add(url)
            out.append((url, price))
    return out


def _max_index_pages(page: str) -> int:
    nums = [int(x) for x in re.findall(r"(?:[?&]|&amp;)p=(\d+)", page, flags=re.I)]
    return max(1, min(max(nums, default=1), 10))


def _structured_price(page: str) -> int | None:
    patterns = (
        r"itemprop=[\"']price[\"'][^>]*content=[\"']([0-9]+(?:[.,][0-9]+)?)[\"']",
        r"content=[\"']([0-9]+(?:[.,][0-9]+)?)[\"'][^>]*itemprop=[\"']price[\"']",
        r"data-price-amount=[\"']([0-9]+(?:[.,][0-9]+)?)[\"']",
        r"[\"']price[\"']\s*:\s*[\"']?([0-9]+(?:[.,][0-9]+)?)",
    )
    for pat in patterns:
        m = re.search(pat, page, flags=re.I)
        if not m:
            continue
        raw = m.group(1).replace(",", ".")
        try:
            value = int(round(float(raw)))
        except ValueError:
            continue
        if 500 <= value <= 100000:
            return value
    return None

def _near_label_text(page: str, label: str, window: int = 700) -> str:
    m = re.search(re.escape(label), page, flags=re.I)
    if not m:
        return ""
    return strip_tags(page[m.start(): m.start() + window])


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
    condition_context = _near_label_text(page, "Conditie", 350)
    condition_match = re.search(r"\b(Occasion|Gebruikt|Nieuw)\b", condition_context, flags=re.I)
    if condition_match and condition_match.group(1).lower() == "nieuw":
        return None

    price = card_price or _structured_price(page) or _parse_price(text)

    year = None
    for label in ("Bouwjaar", "Modeljaar"):
        value = _field(text, label, r"(?:19|20)\d{2}")
        if value:
            year = int(re.search(r"(?:19|20)\d{2}", value).group(0))
            break
        ctx = _near_label_text(page, label, 350)
        ym = re.search(r"(?:19|20)\d{2}", ctx)
        if ym:
            year = int(ym.group(0))
            break

    mileage = None
    value = _field(text, "Kilometerstand", r"[0-9][0-9.\s]{0,10}\s*(?:km)?")
    if value:
        mileage = _num(value)
    if mileage is None:
        ctx = _near_label_text(page, "Kilometerstand", 350)
        mm = re.search(r"([0-9][0-9.\s]{0,10})\s*Km\b", ctx, flags=re.I)
        if mm:
            mileage = _num(mm.group(1))
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
    first_url = _index_url(bike, 1)
    if first_url not in _PAGE_CACHE:
        _PAGE_CACHE[first_url] = fetch(first_url, retries=1)
    first_page = _PAGE_CACHE[first_url]

    page_count = _max_index_pages(first_page)
    candidate_map: dict[str, int | None] = {}
    discovered_stock_urls: set[str] = set()

    for page_no in range(1, page_count + 1):
        index_url = _index_url(bike, page_no)
        if index_url not in _PAGE_CACHE:
            time.sleep(0.20)
            _PAGE_CACHE[index_url] = fetch(index_url, retries=1)
        page = _PAGE_CACHE[index_url]
        discovered_stock_urls.update(
            u for u, _ in _stock_urls_from_page(page)
            if "-rhc" in urlparse(u).path.lower()
        )
        for url, card_price in _product_candidates(page, bike):
            if url not in candidate_map or candidate_map[url] is None:
                candidate_map[url] = card_price

    listings: list[dict] = []
    failed = 0
    for url, card_price in list(candidate_map.items())[:40]:
        try:
            if url not in _DETAIL_CACHE:
                time.sleep(0.30)
                _DETAIL_CACHE[url] = fetch(url, referer=first_url, retries=1)
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
        f"  {GOEDHART.source} / {bike.get('make','')} {bike.get('model','')}: "
        f"scanned {page_count} catalogue page(s), {len(discovered_stock_urls)} stock URL(s), "
        f"{len(candidate_map)} model match(es) -> {len(unique)} parsed"
        + (f" ({failed} detail fetch failed)" if failed else "")
    )
    return list(unique.values()), first_url


__all__ = ["HTTPError", "URLError", "search_goedhart", "strict_model_match", "parse_detail"]
