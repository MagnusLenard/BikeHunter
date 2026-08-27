from __future__ import annotations

import html
import json
import os
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from marktplaats import HTTPError as MpHTTPError, URLError as MpURLError, search_bike as search_marktplaats
from tweedehands import HTTPError as BeHTTPError, URLError as BeURLError, search_bike as search_2dehands
from autoscout24_de import HTTPError as DeHTTPError, URLError as DeURLError, search_bike as search_autoscout24

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clamp(value, low, high):
    return max(low, min(high, value))


def sb_request(method: str, path: str, payload=None, prefer: str | None = None):
    if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SECRET_KEY GitHub secret")
    url = f"{SUPABASE_URL}/rest/v1/{path.lstrip('/')}"
    headers = {
        "apikey": SUPABASE_SECRET_KEY,
        "Accept": "application/json",
        "User-Agent": "BikeHunter-Cloud/1.0",
    }
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if prefer:
        headers["Prefer"] = prefer
    req = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=45) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8", errors="replace"))
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        raise RuntimeError(f"Supabase HTTP {e.code}: {detail[:800]}") from e
    except URLError as e:
        raise RuntimeError(f"Supabase connection error: {getattr(e, 'reason', e)}") from e


def parse_jsonish(value, default):
    if isinstance(value, type(default)):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, type(default)) else default
        except json.JSONDecodeError:
            return default
    return default


def load_cloud_config():
    bike_rows = sb_request("GET", "bikes?select=*&enabled=eq.true&order=sort_order.asc") or []
    settings_rows = sb_request("GET", "app_settings?select=*&id=eq.1&limit=1") or []
    if not settings_rows:
        raise RuntimeError("Supabase app_settings row id=1 was not found")
    s = settings_rows[0]
    settings = {
        "default_max_price_eur": int(s.get("default_max_price_eur") or 6000),
        "scan_interval_hours": int(s.get("scan_interval_hours") or 6),
        "country_priority": parse_jsonish(s.get("country_priority"), ["NL", "BE", "DE"]),
        "countries": {
            "NL": bool(s.get("country_nl", True)),
            "BE": bool(s.get("country_be", True)),
            "DE": bool(s.get("country_de", True)),
        },
        "telegram": s.get("telegram_settings") if isinstance(s.get("telegram_settings"), dict) else parse_jsonish(s.get("telegram_settings"), {}),
        "dashboard_url": str(s.get("dashboard_url") or "https://bikehunter.magnuslenard.workers.dev"),
        "initial_digest_sent": bool(s.get("initial_digest_sent", False)),
    }

    bikes = []
    for r in bike_rows:
        slug = r.get("slug") or str(r.get("id"))
        bikes.append({
            "id": slug,
            "db_id": str(r["id"]),
            "make": r.get("brand") or "",
            "model": r.get("model") or "",
            "query": r.get("search_query") or f"{r.get('brand','')} {r.get('model','')}",
            "mobile_query": r.get("mobile_query") or r.get("search_query") or "",
            "match_terms": r.get("match_terms") or [],
            "min_year": int(r.get("year_min") or 2011),
            "max_year": int(r.get("year_max") or 2026),
            "max_price_eur": int(r.get("max_price") or settings["default_max_price_eur"]),
            "sweet_spots": parse_jsonish(r.get("sweet_spots"), []),
            "sweetspot_only": bool(r.get("sweet_only", False)),
            "notes": r.get("notes") or "",
            "autoscout_model_slug": r.get("autoscout_model_slug") or "",
            "autoscout_path_suffix": r.get("autoscout_path_suffix") or "",
        })
    return bikes, settings


def in_sweet_spot(year: int, bike: dict) -> bool:
    return any(int(r.get("min", 0)) <= year <= int(r.get("max", 0)) for r in bike.get("sweet_spots", []) if isinstance(r, dict))


def price_limit(bike: dict, settings: dict) -> int:
    return int(bike.get("max_price_eur") or settings.get("default_max_price_eur", 6000))


def valid_listing(x: dict, bike: dict, settings: dict) -> bool:
    if x.get("price") is None or x.get("year") is None:
        return False
    year = int(x["year"])
    price = int(x["price"])
    if not int(bike["min_year"]) <= year <= int(bike["max_year"]):
        return False
    if bike.get("sweetspot_only") and bike.get("sweet_spots") and not in_sweet_spot(year, bike):
        return False
    if price > price_limit(bike, settings) or price < 500:
        return False
    return True


def score_listing(x: dict, bike: dict, settings: dict) -> int:
    year = int(x["year"])
    price = int(x["price"])
    mileage = x.get("km")
    limit = max(price_limit(bike, settings), 1)

    price_score = 35 * clamp((limit - price) / max(limit * 0.40, 1), 0, 1)
    km_value = 90000 if mileage is None else int(mileage)
    mileage_score = 25 * clamp(1 - (km_value - 8000) / 72000, 0, 1)

    min_year = int(bike.get("min_year", 2011))
    max_year = max(int(bike.get("max_year", year)), min_year + 1)
    year_score = 8 + 10 * clamp((year - min_year) / (max_year - min_year), 0, 1)
    if in_sweet_spot(year, bike):
        year_score += 10
    year_score = min(year_score, 26)

    country_score = {"NL": 14, "BE": 8, "DE": 5}.get(x.get("country"), 0)
    features = set(x.get("features") or [])
    feature_score = 0
    if "ABS" in features or "C-ABS" in features:
        feature_score += 5
    if "TC" in features:
        feature_score += 3
    if "Quickshifter" in features:
        feature_score += 1
    if "Slipper clutch" in features:
        feature_score += 1

    conditional_penalty = 14 if x.get("conditional_price") else 0
    return int(round(clamp(price_score + mileage_score + year_score + country_score + feature_score - conditional_penalty, 0, 100)))


def tg_call(method: str, params: dict, timeout: int = 30):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Telegram GitHub secrets are missing")
    body = urlencode({k: v for k, v in params.items() if v is not None}).encode("utf-8")
    req = Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "BikeHunter-Cloud/1.0"},
        method="POST",
    )
    with urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode("utf-8", errors="replace"))
    if not result.get("ok"):
        raise RuntimeError(result.get("description") or "Telegram API error")
    return result


def euro(value) -> str:
    return "€" + f"{int(value):,}".replace(",", ".")


def km(value) -> str:
    return "km unknown" if value is None else f"{int(value):,}".replace(",", ".") + " km"


def flag(code: str) -> str:
    return {"NL": "🇳🇱", "BE": "🇧🇪", "DE": "🇩🇪"}.get(code, "🏳️")


def send_telegram(kind: str, x: dict, bike: dict, dashboard_url: str):
    name = f"{bike['make']} {bike['model']}".strip()
    top = "🆕 <b>NEWLY FOUND</b>" if kind == "new" else "💸 <b>PRICE DROP</b>"
    lines = [top, f"<b>{html.escape(name)}</b>"]
    if kind == "drop":
        drop = int(x.get("price_drop") or 0)
        old = int(x["price"]) + drop
        lines.append(f"<b>{euro(old)} → {euro(x['price'])}</b>  ↓ {euro(drop)}")
    else:
        lines.append(f"<b>{euro(x['price'])}</b>")
    location = html.escape(str(x.get("location") or x.get("country") or ""))
    lines.append(f"{x.get('year')} · {km(x.get('km'))} · {flag(str(x.get('country')))} {location}")
    details = [f"Score {int(x.get('score') or 0)}/100"]
    if x.get("sweet_spot"):
        details.append("⭐ Sweet spot")
    if x.get("conditional_price"):
        details.append("⚠️ Conditional price")
    lines.append(" · ".join(details))
    caption = "\n".join(lines)
    markup = json.dumps({"inline_keyboard": [
        [{"text": "Open listing ↗", "url": x["url"]}],
        [{"text": "🏍 Open BikeHunter", "url": dashboard_url}],
    ]}, ensure_ascii=False)
    params = {"chat_id": TELEGRAM_CHAT_ID, "parse_mode": "HTML", "reply_markup": markup}
    photo = x.get("image_url")
    if photo:
        try:
            tg_call("sendPhoto", {**params, "photo": photo, "caption": caption[:1024]}, timeout=40)
            return
        except Exception as e:
            print(f"Telegram photo fallback: {type(e).__name__}: {e}")
    tg_call("sendMessage", {**params, "text": caption, "disable_web_page_preview": "false"})



def send_top_deal(x: dict, bike: dict, dashboard_url: str, rank: int):
    name = f"{bike.get('make','')} {bike.get('model','')}".strip() or str(x.get("title") or "Bike")
    lines = [
        f"🏆 <b>TOP DEAL #{rank}</b>",
        f"<b>{html.escape(name)}</b>",
        f"<b>{euro(x['price'])}</b>",
        f"{x.get('year')} · {km(x.get('km'))} · {flag(str(x.get('country')))} {html.escape(str(x.get('location') or x.get('country') or ''))}",
    ]
    details = [f"Score {int(x.get('score') or 0)}/100"]
    if x.get("sweet_spot"):
        details.append("⭐ Sweet spot")
    if x.get("conditional_price"):
        details.append("⚠️ Conditional price")
    lines.append(" · ".join(details))
    caption = "\n".join(lines)
    markup = json.dumps({"inline_keyboard": [
        [{"text": "Open listing ↗", "url": x["url"]}],
        [{"text": "🏍 Open BikeHunter", "url": dashboard_url}],
    ]}, ensure_ascii=False)
    params = {"chat_id": TELEGRAM_CHAT_ID, "parse_mode": "HTML", "reply_markup": markup}
    photo = x.get("image_url")
    if photo:
        try:
            tg_call("sendPhoto", {**params, "photo": photo, "caption": caption[:1024]}, timeout=40)
            return
        except Exception as e:
            print(f"Telegram top-deal photo fallback: {type(e).__name__}: {e}")
    tg_call("sendMessage", {**params, "text": caption, "disable_web_page_preview": "false"})


def send_initial_digest(current: list[dict], bikes_by_slug: dict, dashboard_url: str):
    top5 = sorted(current, key=lambda x: int(x.get("score") or 0), reverse=True)[:5]
    lines = ["🏆 <b>BikeHunter is live — Top 5 deals</b>", ""]
    for i, x in enumerate(top5, 1):
        bike = bikes_by_slug.get(x.get("bike_id"), {})
        name = f"{bike.get('make','')} {bike.get('model','')}".strip() or str(x.get("title") or "Bike")
        lines.append(
            f"<b>{i}. {html.escape(name)}</b> — {euro(x['price'])} · {x.get('year')} · {km(x.get('km'))} · {flag(str(x.get('country')))} · {int(x.get('score') or 0)}/100"
        )
    lines += ["", "Below are the 5 deal cards with direct listing links.", "After this, I’ll only alert you for newly found listings and price drops."]
    markup = json.dumps({"inline_keyboard": [[{"text": "🏍 Open all deals in BikeHunter", "url": dashboard_url}]]}, ensure_ascii=False)
    tg_call("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID,
        "parse_mode": "HTML",
        "text": "\n".join(lines),
        "reply_markup": markup,
        "disable_web_page_preview": "true",
    })
    for i, x in enumerate(top5, 1):
        send_top_deal(x, bikes_by_slug.get(x.get("bike_id"), {}), dashboard_url, i)

def chunked(items, size=100):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def main() -> int:
    started = now_iso()
    scan_row = sb_request("POST", "scan_runs", {"started_at": started, "status": "running"}, "return=representation")
    scan_id = scan_row[0]["id"] if scan_row else None

    try:
        bikes, settings = load_cloud_config()
        if not bikes:
            raise RuntimeError("No enabled bikes in Supabase")

        prev_runs = sb_request("GET", "scan_runs?select=id,status&status=eq.finished&order=id.desc&limit=1") or []
        first_baseline = len(prev_runs) == 0
        existing_rows = sb_request("GET", "listings?select=source,external_id,price,bike_id,first_seen&limit=1000") or []
        existing = {(r["source"], str(r["external_id"])): r for r in existing_rows}

        countries = settings["countries"]
        priority = settings["country_priority"] or ["NL", "BE", "DE"]
        adapters = {
            "NL": ("Marktplaats NL", search_marktplaats, MpHTTPError, MpURLError),
            "BE": ("2dehands BE", search_2dehands, BeHTTPError, BeURLError),
            "DE": ("AutoScout24 DE", search_autoscout24, DeHTTPError, DeURLError),
        }

        source_counts = {v[0]: 0 for v in adapters.values()}
        source_attempts = {v[0]: 0 for v in adapters.values()}
        source_errors_count = {v[0]: 0 for v in adapters.values()}
        warnings: list[str] = []
        current: list[dict] = []
        bikes_by_slug = {b["id"]: b for b in bikes}

        print(f"BikeHunter cloud scan | {len(bikes)} bikes | {' > '.join(priority)}")
        for i, bike in enumerate(bikes, 1):
            print(f"[{i:02d}/{len(bikes):02d}] {bike['make']} {bike['model']}")
            for code in priority:
                if not countries.get(code, True) or code not in adapters:
                    continue
                label, func, http_err, url_err = adapters[code]
                source_attempts[label] += 1
                try:
                    raw, _ = func(bike)
                except http_err as e:
                    source_errors_count[label] += 1
                    msg = f"{bike['make']} {bike['model']} / {label}: HTTP {getattr(e, 'code', '?')}"
                    warnings.append(msg); print("  WARN", msg); continue
                except url_err as e:
                    source_errors_count[label] += 1
                    msg = f"{bike['make']} {bike['model']} / {label}: connection error {getattr(e, 'reason', e)}"
                    warnings.append(msg); print("  WARN", msg); continue
                except Exception as e:
                    source_errors_count[label] += 1
                    msg = f"{bike['make']} {bike['model']} / {label}: {type(e).__name__}: {e}"
                    warnings.append(msg); print("  WARN", msg); continue

                usable = [x for x in raw if valid_listing(x, bike, settings)]
                unique = {(x.get("source"), str(x.get("source_id"))): x for x in usable}
                usable = list(unique.values())
                for x in usable:
                    key = (str(x["source"]), str(x["source_id"]))
                    old = existing.get(key)
                    x["new"] = bool(old is None and not first_baseline)
                    x["price_drop"] = 0
                    if old and old.get("price") is not None and int(old["price"]) > int(x["price"]):
                        x["price_drop"] = int(old["price"]) - int(x["price"])
                    x["sweet_spot"] = in_sweet_spot(int(x["year"]), bike)
                    x["score"] = score_listing(x, bike, settings)
                    x["db_bike_id"] = bike["db_id"]
                    x.pop("raw_text", None)
                    current.append(x)
                source_counts[label] += len(usable)
                print(f"  {code}: {len(raw)} candidates -> {len(usable)} inside rules")

        current_map = {(x["source"], str(x["source_id"])): x for x in current}
        current = list(current_map.values())
        current.sort(key=lambda x: (-int(x.get("score", 0)), x.get("bike_id", ""), int(x.get("price", 999999))))

        # Only deactivate a source when every configured search for that source succeeded.
        # If a source is partially down, its older listings stay visible until a clean scan.
        for label in source_counts:
            attempts = source_attempts[label]
            errors = source_errors_count[label]
            if attempts > 0 and errors == 0:
                sb_request("PATCH", f"listings?source=eq.{quote(label, safe='')}", {"active": False}, "return=minimal")

        stamp = now_iso()
        rows = []
        for x in current:
            old = existing.get((x["source"], str(x["source_id"])))
            row = {
                "bike_id": x["db_bike_id"],
                "source": x["source"],
                "country": x["country"],
                "external_id": str(x["source_id"]),
                "url": x["url"],
                "title": x.get("title"),
                "price": int(x["price"]),
                "previous_price": int(old["price"]) if old and old.get("price") is not None and int(old["price"]) != int(x["price"]) else None,
                "year": int(x["year"]),
                "mileage_km": int(x["km"]) if x.get("km") is not None else None,
                "location": x.get("location"),
                "seller": x.get("seller"),
                "image_url": x.get("image_url"),
                "score": int(x.get("score") or 0),
                "sweet_spot": bool(x.get("sweet_spot")),
                "conditional_price": bool(x.get("conditional_price")),
                "features": x.get("features") or [],
                "last_seen": stamp,
                "active": True,
            }
            rows.append(row)

        for batch in chunked(rows, 100):
            sb_request(
                "POST",
                "listings?on_conflict=source,external_id",
                batch,
                "resolution=merge-duplicates,return=minimal",
            )

        tg_cfg = settings.get("telegram") or {}
        dashboard_url = settings.get("dashboard_url") or "https://bikehunter.magnuslenard.workers.dev"
        telegram_ready = tg_cfg.get("enabled", True) and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID

        # One-time welcome digest: summary + exactly 5 deal cards + dashboard link.
        initial_digest_ran = False
        if telegram_ready and not settings.get("initial_digest_sent", False):
            try:
                send_initial_digest(current, bikes_by_slug, dashboard_url)
                sb_request(
                    "PATCH",
                    "app_settings?id=eq.1",
                    {"initial_digest_sent": True, "updated_at": now_iso()},
                    "return=minimal",
                )
                initial_digest_ran = True
            except Exception as e:
                warnings.append(f"Telegram initial digest: {type(e).__name__}: {e}")

        alert_queue: list[tuple[str, dict]] = []
        # Do not mix normal NEW/PRICE DROP alerts into the same run as the one-time Top 5 welcome digest.
        if not first_baseline and telegram_ready and not initial_digest_ran:
            if tg_cfg.get("notify_new", True):
                alert_queue += [("new", x) for x in current if x.get("new")]
            if tg_cfg.get("notify_price_drop", True):
                alert_queue += [("drop", x) for x in current if int(x.get("price_drop") or 0) > 0]
            alert_queue.sort(key=lambda z: int(z[1].get("score") or 0), reverse=True)
            max_alerts = int(tg_cfg.get("max_alerts_per_scan", 20) or 20)
            for kind, x in alert_queue[:max_alerts]:
                try:
                    send_telegram(kind, x, bikes_by_slug[x["bike_id"]], dashboard_url)
                except Exception as e:
                    warnings.append(f"Telegram {x.get('title')}: {type(e).__name__}: {e}")

        new_count = sum(1 for x in current if x.get("new"))
        drop_count = sum(1 for x in current if int(x.get("price_drop") or 0) > 0)
        finished = now_iso()
        if scan_id is not None:
            sb_request(
                "PATCH",
                f"scan_runs?id=eq.{scan_id}",
                {
                    "finished_at": finished,
                    "status": "finished",
                    "total_listings": len(current),
                    "new_listings": new_count,
                    "price_drops": drop_count,
                    "source_counts": source_counts,
                    "warnings": warnings,
                },
                "return=minimal",
            )
        print(f"DONE | {len(current)} listings | {new_count} new | {drop_count} price drops | {len(warnings)} warnings")
        if first_baseline:
            print("First cloud scan = baseline. Existing ads were not sent as NEW alerts.")
        return 0

    except Exception as e:
        print(f"FATAL: {type(e).__name__}: {e}", file=sys.stderr)
        if scan_id is not None:
            try:
                sb_request(
                    "PATCH",
                    f"scan_runs?id=eq.{scan_id}",
                    {"finished_at": now_iso(), "status": "failed", "warnings": [f"{type(e).__name__}: {e}"]},
                    "return=minimal",
                )
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
