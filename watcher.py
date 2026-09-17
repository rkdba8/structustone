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
from urllib.parse import quote_plus, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
from playwright.async_api import Browser, Page, async_playwright

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"
STATE_FILE = ROOT / "seen.json"

STRUCTURA_LIST_URL = "https://www.structura.be/fr/a-louer/appartements"
LIVING_STONE_LIST_URL = "https://living-stone.be/fr/a-louer"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36 ApartmentWatcher/2.0"
)

UNAVAILABLE_MARKERS = (
    "loué avec succès",
    "loue avec succes",
    "en option",
    "in optie",
    "visites complètes",
    "visites completes",
)

VISIT_LABELS = (
    "prendre rendez-vous",
    "planifier une visite",
    "prendre un rendez-vous",
    "réserver une visite",
    "reserver une visite",
)

BRUSSELS_TZ = ZoneInfo("Europe/Brussels")


@dataclass
class Listing:
    source: str
    key: str
    url: str
    title: str
    city: str | None
    postal_code: str | None
    address: str | None
    rent: int | None
    charges: int | None
    bedrooms: int | None
    surface: float | None
    floor: str | None
    epc: str | None
    image_url: str | None
    booking_url: str | None
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

    @property
    def total_monthly(self) -> int | None:
        if self.rent is None:
            return None
        if self.charges is None:
            return None
        return self.rent + self.charges


# ------------------------------ Parsing helpers ------------------------------

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


def extract_charges(text: str) -> int | None:
    # Prefer explicit charge labels and ignore deposits/guarantees/parking.
    patterns = [
        r"Charges\s+locataire\s*\n?\s*€?\s*([0-9][0-9.\s]*)\s*(?:p/m|par mois|/mois)?",
        r"Charges(?:\s+communes)?\s*[:\-]?\s*€?\s*([0-9][0-9.\s]*)\s*€?\s*(?:/\s*mois|p/m|par mois)",
        r"(?:frais|kosten)\s+(?:mensuels|communs|communes)[^\n€]{0,80}?([0-9][0-9.\s]*)\s*€",
        r"([0-9][0-9.\s]*)\s*€\s+de\s+charges(?:\s+communes)?",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = parse_money(match.group(1))
            if value is not None and 0 <= value <= 1500:
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


def extract_floor(text: str) -> str | None:
    patterns = [
        r"(?:^|\n)\s*[ÉEée]tage\s*\n?\s*([^\n]{1,20})",
        r"\b(?:au|situé au|situe au)\s+(\d{1,2}(?:er|e|ème|eme)?)\s+étage\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            value = normalize_text(match.group(1))
            if value and len(value) <= 20:
                return value
    return None


def extract_epc(text: str) -> str | None:
    # Belgian listings may show a letter/score (A67, B, C...) or only kWh/m².
    patterns = [
        r"\bPEB\s*[:\-]?\s*([A-G](?:\+|\-)?\s*\d{0,3})\b",
        r"\bEPC\s*[:\-]?\s*([A-G](?:\+|\-)?\s*\d{0,3})\b",
        r"(?:^|\n)\s*PEB\s*\n\s*([0-9]{1,4}\s*kWh/m²)",
        r"(?:^|\n)\s*EPC\s*\n\s*([0-9]{1,4}\s*kWh/m²)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            return normalize_text(match.group(1)).upper().replace("KWH/M²", "kWh/m²")
    return None


def extract_reference(text: str) -> str | None:
    match = re.search(r"(?:Réf\.|Ref)\s*:\s*#?\s*(\d+)", text, flags=re.IGNORECASE)
    return match.group(1) if match else None


def extract_postal_code(text: str) -> str | None:
    match = re.search(r"\b([1-9]\d{3})\b", text)
    return match.group(1) if match else None


def extract_address_from_text(text: str) -> str | None:
    # Typical Belgian address: street + number[, ] + 4-digit postcode + city.
    for line in text.splitlines()[:120]:
        line = normalize_text(line)
        if not line or len(line) > 140:
            continue
        if re.search(r"\b\d{4}\s+[A-Za-zÀ-ÿ'’\- ]{2,}\b", line) and re.search(r"\d", line):
            # Avoid price/energy lines that also contain a 4-digit number.
            if "€" not in line and "kwh" not in line.lower() and "peb" not in line.lower():
                return line
    return None


def is_available(text: str) -> bool:
    lowered = normalize_text(text).lower()[:3000]
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


# ------------------------------ Browser scraping -----------------------------

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

    markup = await page.content()
    if source == "living_stone":
        for match in re.findall(r'href=["\']([^"\']*/fr/appartement/a-louer/[^"\'#?]+)', markup, re.I):
            urls.add(clean_url(urljoin(list_url, match)))
    elif source == "structura":
        for match in re.findall(r'href=["\']([^"\']*/fr/appartement-a-louer-a-[^"\'#?]+/\d+)', markup, re.I):
            urls.add(clean_url(urljoin(list_url, match)))

    return sorted(urls)


async def get_meta_content(page: Page, selectors: list[str]) -> str | None:
    for selector in selectors:
        try:
            value = await page.locator(selector).first.get_attribute("content", timeout=1200)
            if value:
                return value.strip()
        except Exception:
            pass
    return None


async def extract_booking_url(page: Page, base_url: str) -> str | None:
    try:
        anchors = await page.locator("a").evaluate_all(
            "els => els.map(a => ({text:(a.innerText||'').trim(), href:a.getAttribute('href')}))"
        )
    except Exception:
        return None

    for anchor in anchors:
        text = normalize_text(anchor.get("text") or "").lower()
        href = anchor.get("href")
        if href and any(label in text for label in VISIT_LABELS):
            return clean_url(urljoin(base_url, href))
    return None


async def extract_jsonld_address(page: Page) -> str | None:
    try:
        scripts = await page.locator('script[type="application/ld+json"]').all_text_contents()
    except Exception:
        return None

    def walk(value):
        if isinstance(value, dict):
            addr = value.get("address")
            if isinstance(addr, dict):
                parts = [
                    addr.get("streetAddress"),
                    addr.get("postalCode"),
                    addr.get("addressLocality"),
                ]
                compact = ", ".join(str(x).strip() for x in parts if x)
                if compact:
                    return compact
            for v in value.values():
                found = walk(v)
                if found:
                    return found
        elif isinstance(value, list):
            for v in value:
                found = walk(v)
                if found:
                    return found
        return None

    for raw in scripts:
        try:
            data = json.loads(raw)
        except Exception:
            continue
        found = walk(data)
        if found:
            return found
    return None


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

    image_url = await get_meta_content(
        page,
        ['meta[property="og:image"]', 'meta[name="twitter:image"]', 'meta[property="twitter:image"]'],
    )
    if image_url:
        image_url = urljoin(url, image_url)

    address = await extract_jsonld_address(page) or extract_address_from_text(text)
    postal_code = extract_postal_code(address or "") or extract_postal_code(text[:2500])
    booking_url = await extract_booking_url(page, url)

    return Listing(
        source=source,
        key=make_key(source, url, reference),
        url=url,
        title=title,
        city=city_from_url(url, source),
        postal_code=postal_code,
        address=address,
        rent=extract_price(text),
        charges=extract_charges(text),
        bedrooms=extract_bedrooms(text),
        surface=extract_surface(text),
        floor=extract_floor(text),
        epc=extract_epc(text),
        image_url=image_url,
        booking_url=booking_url,
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
        for index, url in enumerate(links, start=1):
            item = await parse_listing(detail_page, source, url)
            if item:
                listings.append(item)
            if index % 5 == 0 or index == len(links):
                print(f"{source}: {index}/{len(links)} fiche(s) analysée(s)")
        return listings
    finally:
        await context.close()


# ------------------------------ State + Telegram -----------------------------

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def telegram_escape(value: str) -> str:
    return html.escape(value, quote=False)


def format_euros(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " €"


def detected_time() -> str:
    return datetime.now(BRUSSELS_TZ).strftime("%H:%M")


def format_notification(item: Listing, event: str = "new") -> str:
    source_name = "Structura" if item.source == "structura" else "Living Stone"
    headline = "🆕 <b>NOUVEL APPARTEMENT</b>" if event == "new" else "📉 <b>BAISSE DE PRIX</b>"
    lines = [headline]

    location = None
    if item.postal_code and item.city:
        location = f"{item.postal_code} {item.city}"
    elif item.city:
        location = item.city
    elif item.address:
        location = item.address
    if location:
        lines.extend(["", f"📍 <b>{telegram_escape(location)}</b>"])

    if item.rent is not None:
        if item.charges is not None:
            lines.append(f"💶 <b>{format_euros(item.rent)}</b> + {format_euros(item.charges)} charges")
            lines.append(f"💰 Total loyer + charges : <b>{format_euros(item.total_monthly or item.rent)}/mois</b>")
        else:
            lines.append(f"💶 <b>{format_euros(item.rent)}/mois</b>")
            lines.append("💳 Charges : non précisées")

    details: list[str] = []
    if item.bedrooms is not None:
        details.append(f"🛏 {item.bedrooms} ch.")
    if item.surface is not None:
        surface = int(item.surface) if item.surface.is_integer() else item.surface
        details.append(f"📐 {surface} m²")
    if item.floor:
        details.append(f"🏢 étage {telegram_escape(item.floor)}")
    if details:
        lines.extend(["", " · ".join(details)])

    if item.epc:
        lines.append(f"🌱 PEB/EPC : <b>{telegram_escape(item.epc)}</b>")
    if item.reference:
        lines.append(f"🔖 Réf. #{telegram_escape(item.reference)}")

    lines.extend(["", f"🏷 {source_name}", f"🕐 Détecté à {detected_time()}"])
    return "\n".join(lines)


def telegram_keyboard(item: Listing) -> dict:
    buttons = [{"text": "🏠 Voir l’annonce", "url": item.url}]
    if item.address:
        maps_url = "https://www.google.com/maps/search/?api=1&query=" + quote_plus(item.address)
        buttons.append({"text": "📍 Maps", "url": maps_url})

    rows = [buttons]
    if item.booking_url:
        rows.append([{"text": "📅 Réserver une visite", "url": item.booking_url}])
    return {"inline_keyboard": rows}


def _telegram_credentials() -> tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID doivent être configurés.")
    return token, chat_id


def send_telegram(item: Listing, event: str = "new") -> None:
    token, chat_id = _telegram_credentials()
    caption = format_notification(item, event=event)
    keyboard = telegram_keyboard(item)

    # Prefer a native Telegram photo so the cover image is displayed large.
    if item.image_url:
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                json={
                    "chat_id": chat_id,
                    "photo": item.image_url,
                    "caption": caption,
                    "parse_mode": "HTML",
                    "reply_markup": keyboard,
                },
                timeout=25,
            )
            response.raise_for_status()
            return
        except requests.RequestException as exc:
            print(f"WARN: sendPhoto a échoué, fallback texte: {exc}", file=sys.stderr)

    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": caption,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
            "reply_markup": keyboard,
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
        old_rent = old.get("rent")
        old_qualifies = bool(old.get("qualifies", False))

        event: str | None = None
        if not first_run and qualifies and not already_notified:
            event = "new"
        elif (
            not first_run
            and qualifies
            and already_notified
            and isinstance(old_rent, int)
            and item.rent is not None
            and item.rent < old_rent
        ):
            event = "price_drop"
        elif not first_run and qualifies and not old_qualifies:
            event = "new"

        if event:
            send_telegram(item, event=event)
            notification_count += 1
            already_notified = True

        # First run establishes a baseline without sending a burst of old listings.
        if first_run and qualifies:
            already_notified = True

        record = asdict(item)
        record.update(
            {
                "qualifies": qualifies,
                "notified": already_notified,
                "last_seen": now,
            }
        )
        records[item.key] = record

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
