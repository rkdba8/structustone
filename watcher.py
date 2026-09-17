from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qsl, quote_plus, urlencode, urljoin, urlsplit, urlunsplit
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
    image_urls: list[str] = field(default_factory=list)

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


def _normalize_image_url(raw: str, base_url: str) -> str | None:
    raw = (raw or "").strip().strip('"\'')
    if not raw or raw.startswith(("data:", "blob:", "javascript:")):
        return None
    # srcset candidates can contain a trailing width/density descriptor.
    raw = re.sub(r"\s+(?:\d+w|\d+(?:\.\d+)?x)$", "", raw).strip()
    url = urljoin(base_url, raw)
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        return None

    lowered = url.lower()
    banned = (
        "favicon", "logo", "sprite", "icon", "marker", "placeholder",
        "avatar", "facebook", "instagram", "linkedin", "youtube", "tiktok",
        "/flags/", "cookie", "consent", "tracking", "pixel",
    )
    if any(word in lowered for word in banned):
        return None
    if re.search(r"\.(?:svg|gif)(?:$|[?#])", lowered):
        return None
    return url


_IMAGE_TRANSFORM_QUERY_KEYS = {
    "w", "width", "h", "height", "q", "quality", "fit", "crop", "fm",
    "format", "auto", "dpr", "rect", "sharp", "blur", "gravity", "g",
}


def _image_identity(url: str) -> str:
    """Return a stable identity for responsive variants of the same photo.

    Real-estate sites commonly expose one photo several times (thumbnail, 800px,
    1600px, WebP, etc.). Telegram should receive the photo once, not every
    responsive rendition.
    """
    parts = urlsplit(url)
    path = parts.path

    # Common filename transforms: photo-1200x800.jpg, photo_640x480.webp,
    # photo-large.jpg, etc. Keep the actual URL for sending; normalize only the
    # identity used for deduplication.
    path = re.sub(
        r"(?i)(?:[-_](?:thumb|thumbnail|small|medium|large|xl|xxl|original)|[-_]\d{2,4}x\d{2,4})(?=\.(?:jpe?g|png|webp|avif)$)",
        "",
        path,
    )
    # Some CDNs put the rendition in its own path segment.
    path = "/".join(
        segment for segment in path.split("/")
        if segment.lower() not in {"thumb", "thumbnail", "small", "medium", "large", "xl", "xxl", "original"}
    )
    # JPG/WebP/AVIF versions of the same path are normally the same photo.
    path = re.sub(r"(?i)\.(?:jpe?g|png|webp|avif)$", "", path)

    # Strip standard image-resize query params while retaining meaningful IDs
    # or signatures used by the CDN.
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in _IMAGE_TRANSFORM_QUERY_KEYS:
            continue
        query.append((key, value))

    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def _dedupe_image_urls(urls: Iterable[str], base_url: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        url = _normalize_image_url(raw, base_url)
        if not url:
            continue
        key = _image_identity(url)
        if key in seen:
            continue
        seen.add(key)
        out.append(url)
    return out


async def extract_jsonld_images(page: Page, base_url: str) -> list[str]:
    try:
        scripts = await page.locator('script[type="application/ld+json"]').all_text_contents()
    except Exception:
        return []

    candidates: list[str] = []

    def walk(value):
        if isinstance(value, dict):
            image = value.get("image")
            if isinstance(image, str):
                candidates.append(image)
            elif isinstance(image, list):
                for entry in image:
                    if isinstance(entry, str):
                        candidates.append(entry)
                    elif isinstance(entry, dict):
                        for key in ("url", "contentUrl", "thumbnailUrl"):
                            if isinstance(entry.get(key), str):
                                candidates.append(entry[key])
            elif isinstance(image, dict):
                for key in ("url", "contentUrl", "thumbnailUrl"):
                    if isinstance(image.get(key), str):
                        candidates.append(image[key])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for raw in scripts:
        try:
            walk(json.loads(raw))
        except Exception:
            continue
    return _dedupe_image_urls(candidates, base_url)


async def extract_image_urls(page: Page, base_url: str, cover_url: str | None) -> list[str]:
    """Collect listing/gallery photos while filtering obvious site chrome.

    We combine JSON-LD, links to full-size images and rendered <img> elements.
    The cover image is deliberately kept first.
    """
    # Do not scroll through every detail page here. Most galleries expose their
    # URLs in the DOM/data attributes immediately; scrolling every listing made
    # scheduled runs several minutes slower and also loaded lots of duplicate
    # responsive image variants.
    candidates: list[str] = []
    if cover_url:
        candidates.append(cover_url)

    candidates.extend(await extract_jsonld_images(page, base_url))

    try:
        hrefs = await page.locator("a").evaluate_all(
            """els => els.flatMap(a => {
                const href = a.href || a.getAttribute('href') || '';
                if (!href) return [];
                const low = href.toLowerCase();
                if (/\\.(jpg|jpeg|png|webp|avif)(\\?|#|$)/.test(low)) return [href];
                if (/(image|photo|media|gallery|cdn)/.test(low) && a.querySelector('img')) return [href];
                return [];
            })"""
        )
        candidates.extend(hrefs)
    except Exception:
        pass

    try:
        images = await page.locator("img").evaluate_all(
            """els => els.flatMap(img => {
                const vals = [];
                const push = v => { if (v) vals.push(v); };
                push(img.currentSrc);
                push(img.src);
                for (const name of ['data-src','data-lazy-src','data-original','data-image','data-large','data-full']) {
                    push(img.getAttribute(name));
                }
                for (const name of ['srcset','data-srcset']) {
                    const set = img.getAttribute(name) || '';
                    set.split(',').forEach(part => push(part.trim().split(/\\s+/)[0]));
                }
                // Keep actual content-sized images and gallery/lightbox images.
                const meta = {
                    vals,
                    w: img.naturalWidth || 0,
                    h: img.naturalHeight || 0,
                    text: ((img.alt || '') + ' ' + (img.className || '') + ' ' + (img.id || '')).toLowerCase()
                };
                const likelyGallery = /(gallery|photo|image|property|estate|slider|carousel|swiper)/.test(meta.text);
                if ((meta.w >= 280 && meta.h >= 180) || likelyGallery) return meta.vals;
                return [];
            })"""
        )
        candidates.extend(images)
    except Exception:
        pass

    urls = _dedupe_image_urls(candidates, base_url)

    # Avoid obvious tiny UI assets missed by DOM dimensions by preferring common
    # photo/CDN paths; always preserve the cover image.
    cover_clean = _normalize_image_url(cover_url or "", base_url) if cover_url else None
    filtered: list[str] = []
    for url in urls:
        low = url.lower()
        if cover_clean and url == cover_clean:
            filtered.append(url)
            continue
        if any(token in low for token in ("/images/", "/image/", "/photos/", "/photo/", "/media/", "cloudinary", "cdn", "imgix", "property", "pand", "estate")):
            filtered.append(url)
            continue
        if re.search(r"\.(?:jpe?g|png|webp|avif)(?:$|[?])", low):
            filtered.append(url)

    # A website may expose the same underlying image through multiple resized URLs.
    # Keep exact URLs for now; Telegram will show all distinct gallery assets.
    return filtered


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
    image_urls = await extract_image_urls(page, url, image_url)
    if not image_url and image_urls:
        image_url = image_urls[0]

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
        image_urls=image_urls,
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
        if listings:
            photo_counts = [len(item.image_urls) for item in listings]
            print(
                f"{source}: {sum(photo_counts)} photo(s), "
                f"moyenne {sum(photo_counts) / len(photo_counts):.1f}/annonce "
                f"(min {min(photo_counts)}, max {max(photo_counts)})"
            )
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


def _telegram_api(token: str, method: str, payload: dict, timeout: int = 30) -> requests.Response:
    response = requests.post(
        f"https://api.telegram.org/bot{token}/{method}",
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    return response


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _send_single_photo(token: str, chat_id: str, photo_url: str, caption: str | None = None,
                       keyboard: dict | None = None) -> bool:
    payload: dict = {"chat_id": chat_id, "photo": photo_url}
    if caption:
        payload.update({"caption": caption, "parse_mode": "HTML"})
    if keyboard:
        payload["reply_markup"] = keyboard
    try:
        _telegram_api(token, "sendPhoto", payload, timeout=30)
        return True
    except requests.RequestException as exc:
        print(f"WARN: photo Telegram ignorée ({photo_url}): {exc}", file=sys.stderr)
        return False


def _send_album_batch(
    token: str,
    chat_id: str,
    photos: list[str],
    caption: str | None = None,
) -> bool:
    media = []

    for index, photo_url in enumerate(photos):
        item = {
            "type": "photo",
            "media": photo_url,
        }

        # Telegram affiche la légende sous l'album si elle est
        # attachée à la première photo.
        if index == 0 and caption:
            item["caption"] = caption
            item["parse_mode"] = "HTML"

        media.append(item)

    try:
        _telegram_api(
            token,
            "sendMediaGroup",
            {
                "chat_id": chat_id,
                "media": media,
            },
            timeout=45,
        )
        return True

    except requests.RequestException as exc:
        print(
            f"WARN: album Telegram échoué: {exc}",
            file=sys.stderr,
        )
        return False


def send_telegram(
    item: Listing,
    event: str = "new",
    max_photos: int = 10,
) -> None:
    token, chat_id = _telegram_credentials()

    caption = format_notification(item, event=event)
    keyboard = telegram_keyboard(item)

    photos = list(
        item.image_urls
        or ([] if not item.image_url else [item.image_url])
    )

    if item.image_url and item.image_url not in photos:
        photos.insert(0, item.image_url)

    photos = _dedupe_image_urls(photos, item.url)

    # Ici 10 = maximum 10 photos AU TOTAL dans l'album.
    if max_photos > 0:
        photos = photos[:max_photos]

    print(
        f"Telegram: {item.source} {item.key} "
        f"-> {len(photos)} photo(s) dans la galerie"
    )

    # ---------- Galerie + texte ----------
    if len(photos) >= 2:
        album_sent = _send_album_batch(
            token,
            chat_id,
            photos,
            caption=caption,
        )

        if not album_sent:
            # Fallback : première photo + texte
            _send_single_photo(
                token,
                chat_id,
                photos[0],
                caption=caption,
            )

    elif len(photos) == 1:
        _send_single_photo(
            token,
            chat_id,
            photos[0],
            caption=caption,
        )

    else:
        # Aucun média disponible
        _telegram_api(
            token,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": caption,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )

    # ---------- Message séparé avec boutons ----------
    _telegram_api(
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": "🔗 <b>Liens rapides</b>",
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": keyboard,
        },
        timeout=20,
    )


def current_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def update_state_and_notify(listings: Iterable[Listing], config: dict, state: dict) -> tuple[int, int]:
    max_rent = int(config["max_rent"])
    min_bedrooms = int(config["min_bedrooms"])
    max_photos = int(config.get("telegram_max_photos", 0) or 0)
    force_send_current = os.environ.get("FORCE_SEND_CURRENT", "").lower() in {"1", "true", "yes", "on"}
    try:
        force_send_limit = int(os.environ.get("FORCE_SEND_LIMIT", "1") or "1")
    except ValueError:
        force_send_limit = 1
    forced_sent = 0
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
        if (
            force_send_current
            and qualifies
            and (force_send_limit <= 0 or forced_sent < force_send_limit)
        ):
            event = "new"
            forced_sent += 1
        elif not first_run and qualifies and not already_notified:
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
            send_telegram(item, event=event, max_photos=max_photos)
            notification_count += 1
            already_notified = True

        # First run establishes a baseline without sending a burst of old listings.
        if first_run and qualifies:
            already_notified = True

        record = asdict(item)
        # Gallery URLs are transient notification data; storing dozens of them
        # per listing bloats seen.json and creates noisy commits every run.
        record.pop("image_urls", None)
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
