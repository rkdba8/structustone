from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from playwright.async_api import Browser, Page, async_playwright

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"
STATE_FILE = ROOT / "seen.json"

STRUCTURA_LIST_URL = "https://www.structura.be/fr/a-louer/appartements"
LIVING_STONE_LIST_URL = "https://living-stone.be/fr/a-louer"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36 ApartmentWatcher/1.0"
)

UNAVAILABLE_MARKERS = (
    "loué avec succès",
    "loue avec succes",
    "en option",
    "in optie",
    "visites complètes",
    "visites completes",
)


@dataclass
class Listing:
    source: str
    key: str
    url: str
    title: str
    city: str | None
    rent: int | None
    bedrooms: int | None
    surface: float | None
    reference: str | None
    available: bool

    def qualifies(self, max_rent: int, min_bedrooms: int) -> bool:
        return (
            self.available
            and self.rent is not None
            and self.rent <= max_rent
            and self.bedrooms is not None
            and self.bedrooms >= min_bedrooms
        )


def clean_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def normalize_text(text: str) -> str:
    return re.sub(r"[ \t]+", " ", html.unescape(text).replace("\u00a0", " ")).strip()


def parse_money(raw: str) -> int | None:
    digits = re.sub(r"\D", "", raw)
    return int(digits) if digits else None


def parse_float(raw: str) -> float | None:
    raw = raw.replace(".", "").replace(",", ".") if "," in raw else raw
    try:
        return float(raw)
    except ValueError:
        return None


def extract_price(text: str) -> int | None:
    patterns = [
        r"€\s*([0-9][0-9.\s]*)\s*(?:p/m|par mois|/\s*Maand|/\s*mois)",
        r"(?:Prix|Huurprijs)\s*\n?\s*€?\s*([0-9][0-9.\s]*)\s*(?:par mois|p/m|EUR/maand|/maand)?",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = parse_money(match.group(1))
            if value and 100 <= value <= 10000:
                return value
    return None


def extract_bedrooms(text: str) -> int | None:
    patterns = [
        r"(?:Chambres|Aantal slaapkamers)\s*\n?\s*(\d+)",
        r"(\d+)\s+chambres?\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def extract_surface(text: str) -> float | None:
    patterns = [
        r"(?:Surface habitable|Bewoonbare oppervlakte)\s*\n?\s*([0-9]+(?:[.,][0-9]+)?)\s*m²",
        r"\b([0-9]+(?:[.,][0-9]+)?)\s*m²\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return parse_float(match.group(1))
    return None


def extract_reference(text: str) -> str | None:
    match = re.search(r"(?:Réf\.|Ref)\s*:\s*#?\s*(\d+)", text, flags=re.IGNORECASE)
    return match.group(1) if match else None


def is_available(text: str) -> bool:
    # Status badges are near the top of the property page. Limiting the scan avoids
    # a false negative if a related-property card lower on the page is "en option".
    lowered = normalize_text(text).lower()[:2500]
    return not any(marker in lowered for marker in UNAVAILABLE_MARKERS)


def city_from_url(url: str, source: str) -> str | None:
    path = urlsplit(url).path
    if source == "living_stone":
        match = re.search(r"/a-louer/([^/]+?)(?:-\d{4})?(?:/|$)", path)
        if match:
            return match.group(1).replace("-", " ").title()
    if source == "structura":
        match = re.search(r"appartement-a-louer-a-([^/]+)/", path)
        if match:
            return match.group(1).replace("-", " ").title()
    return None


def make_key(source: str, url: str, reference: str | None = None) -> str:
    if reference:
        return f"{source}:{reference}"
    if source == "structura":
        match = re.search(r"/(\d+)$", urlsplit(url).path.rstrip("/"))
        if match:
            return f"{source}:{match.group(1)}"
    digest = hashlib.sha256(clean_url(url).encode()).hexdigest()[:16]
    return f"{source}:{digest}"


async def auto_scroll(page: Page, rounds: int = 8) -> None:
    previous_height = 0
    for _ in range(rounds):
        height = await page.evaluate("document.body.scrollHeight")
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(900)
        if height == previous_height:
            break
        previous_height = height


async def collect_links(page: Page, list_url: str, source: str) -> list[str]:
    await page.goto(list_url, wait_until="domcontentloaded", timeout=60_000)
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass
    await page.wait_for_timeout(2500)
    await auto_scroll(page)

    hrefs: list[str] = await page.locator("a").evaluate_all(
        "els => els.map(a => a.getAttribute('href')).filter(Boolean)"
    )
    urls: set[str] = set()
    for href in hrefs:
        absolute = clean_url(urljoin(list_url, href))
        path = urlsplit(absolute).path.lower()
        if source == "structura":
            if re.search(r"/fr/appartement-a-louer-a-[^/]+/\d+$", path):
                urls.add(absolute)
        elif source == "living_stone":
            if "/fr/appartement/a-louer/" in path:
                urls.add(absolute)

    # Some modern sites keep URLs in rendered HTML before turning them into anchors.
    markup = await page.content()
    if source == "living_stone":
        for match in re.findall(r'href=["\']([^"\']*/fr/appartement/a-louer/[^"\'#?]+)', markup, re.I):
            urls.add(clean_url(urljoin(list_url, match)))
    elif source == "structura":
        for match in re.findall(r'href=["\']([^"\']*/fr/appartement-a-louer-a-[^"\'#?]+/\d+)', markup, re.I):
            urls.add(clean_url(urljoin(list_url, match)))

    return sorted(urls)


async def parse_listing(page: Page, source: str, url: str) -> Listing | None:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(700)
        text = await page.locator("body").inner_text(timeout=15_000)
    except Exception as exc:
        print(f"WARN: impossible de lire {url}: {exc}", file=sys.stderr)
        return None

    try:
        title = normalize_text(await page.locator("h1").first.inner_text(timeout=3_000))
    except Exception:
        title = "Appartement à louer"

    reference = extract_reference(text) if source == "living_stone" else None
    return Listing(
        source=source,
        key=make_key(source, url, reference),
        url=url,
        title=title,
        city=city_from_url(url, source),
        rent=extract_price(text),
        bedrooms=extract_bedrooms(text),
        surface=extract_surface(text),
        reference=reference,
        available=is_available(text),
    )


async def scrape_source(browser: Browser, source: str, list_url: str) -> list[Listing]:
    context = await browser.new_context(user_agent=USER_AGENT, locale="fr-BE")
    page = await context.new_page()
    detail_page = await context.new_page()
    try:
        links = await collect_links(page, list_url, source)
        print(f"{source}: {len(links)} fiche(s) détectée(s)")
        listings: list[Listing] = []
        for url in links:
            item = await parse_listing(detail_page, source, url)
            if item:
                listings.append(item)
        return listings
    finally:
        await context.close()


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def telegram_escape(value: str) -> str:
    # HTML parse mode: these three characters must be escaped.
    return html.escape(value, quote=False)


def format_notification(item: Listing) -> str:
    source_name = "Structura" if item.source == "structura" else "Living Stone"
    lines = [f"🏠 <b>NOUVEL APPARTEMENT — {source_name}</b>"]
    if item.city:
        lines.append(f"📍 {telegram_escape(item.city)}")
    if item.rent is not None:
        lines.append(f"💶 {item.rent:,} €/mois".replace(",", " "))
    if item.bedrooms is not None:
        lines.append(f"🛏 {item.bedrooms} chambres")
    if item.surface is not None:
        surface = int(item.surface) if item.surface.is_integer() else item.surface
        lines.append(f"📐 {surface} m²")
    if item.reference:
        lines.append(f"🔖 Réf. #{telegram_escape(item.reference)}")
    lines.extend(["", f'<a href="{html.escape(item.url, quote=True)}">Voir l’annonce</a>'])
    return "\n".join(lines)


def send_telegram(message: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID doivent être configurés.")

    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=20,
    )
    response.raise_for_status()


def current_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def update_state_and_notify(listings: Iterable[Listing], config: dict, state: dict) -> tuple[int, int]:
    max_rent = int(config["max_rent"])
    min_bedrooms = int(config["min_bedrooms"])
    first_run = not bool(state.get("initialized", False))
    records = state.setdefault("listings", {})
    now = current_iso()
    notification_count = 0
    qualifying_count = 0

    for item in listings:
        qualifies = item.qualifies(max_rent=max_rent, min_bedrooms=min_bedrooms)
        if qualifies:
            qualifying_count += 1

        old = records.get(item.key, {})
        already_notified = bool(old.get("notified", False))

        # First run creates the baseline without sending existing offers.
        should_notify = (not first_run) and qualifies and not already_notified
        if should_notify:
            send_telegram(format_notification(item))
            notification_count += 1
            already_notified = True

        # On first run, qualifying current listings are intentionally suppressed forever;
        # non-qualifying ones stay unnotified so a later price drop can trigger an alert.
        if first_run and qualifies:
            already_notified = True

        records[item.key] = {
            "source": item.source,
            "url": item.url,
            "title": item.title,
            "city": item.city,
            "rent": item.rent,
            "bedrooms": item.bedrooms,
            "surface": item.surface,
            "reference": item.reference,
            "available": item.available,
            "qualifies": qualifies,
            "notified": already_notified,
            "last_seen": now,
        }

    state["initialized"] = True
    state["last_run"] = now
    return qualifying_count, notification_count


async def async_main() -> int:
    config = load_json(CONFIG_FILE, {})
    state = load_json(STATE_FILE, {"initialized": False, "listings": {}})

    required = {"max_rent", "min_bedrooms", "sources"}
    missing = required - set(config)
    if missing:
        raise RuntimeError(f"Configuration incomplète: {', '.join(sorted(missing))}")

    sources = []
    if config["sources"].get("structura", True):
        sources.append(("structura", STRUCTURA_LIST_URL))
    if config["sources"].get("living_stone", True):
        sources.append(("living_stone", LIVING_STONE_LIST_URL))

    all_listings: list[Listing] = []
    errors: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            for source, url in sources:
                try:
                    items = await scrape_source(browser, source, url)
                    if not items:
                        errors.append(f"{source}: aucune fiche trouvée")
                    all_listings.extend(items)
                except Exception as exc:
                    errors.append(f"{source}: {exc}")
                    print(f"ERROR {source}: {exc}", file=sys.stderr)
        finally:
            await browser.close()

    # Never overwrite the baseline when every source failed: that makes failures visible.
    if not all_listings:
        raise RuntimeError("Aucune annonce récupérée. " + " | ".join(errors))

    qualifying, notifications = update_state_and_notify(all_listings, config, state)
    save_state(state)

    print(
        f"Terminé: {len(all_listings)} annonce(s), {qualifying} correspondant aux critères, "
        f"{notifications} notification(s) envoyée(s)."
    )
    if errors:
        print("Avertissements: " + " | ".join(errors), file=sys.stderr)

    return 0


def main() -> None:
    try:
        raise SystemExit(asyncio.run(async_main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
