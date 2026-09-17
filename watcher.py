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
from html.parser import HTMLParser
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
LIVING_STONE_API_URL = "https://living-stone.be/api/estates"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36 ApartmentWatcher/3.0"
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
    "prendre un rendez-vous",
    "planifier une visite",
    "planifier votre visite",
    "planifiez votre visite",
    "programmer une visite",
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
        if self.rent is None or self.charges is None:
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
    patterns = [
        r"Charges\s+locataire\s*\n?\s*€?\s*([0-9][0-9.\s]*)\s*(?:p/m|par mois|/mois)?",
        r"Charges(?:\s+communes)?(?:\s+forfaitaires?)?\s*[:\-]?\s*€?\s*([0-9][0-9.\s]*)\s*(?:€|EUR)?\s*(?:/\s*mois|p/m|par mois)",
        r"Charges(?:\s+communes)?\s*[:\-]?\s*€?\s*([0-9][0-9.\s]*)\s*(?:€|EUR)\b",
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
    for line in text.splitlines()[:120]:
        line = normalize_text(line)
        if not line or len(line) > 140:
            continue
        if re.search(r"\b\d{4}\s+[A-Za-zÀ-ÿ'’\- ]{2,}\b", line) and re.search(r"\d", line):
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




# ------------------------------ Structura list HTML ---------------------------

class StructuraListParser(HTMLParser):
    """Parse Structura's public rental-results HTML without a browser.

    The search page already contains the useful metadata for each card: URL,
    city, type, rent, bedrooms, surface, EPC label, description/charges and the
    first gallery photos.  We only open detail pages later for listings that
    match the user's criteria.
    """

    _VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[dict] = []
        self.current: dict | None = None
        self.card_div_depth = 0
        self.capture_key: str | None = None
        self.capture_tag: str | None = None
        self.max_page = 1

    @staticmethod
    def _attrs(attrs) -> dict[str, str]:
        return {str(k): str(v or "") for k, v in attrs}

    @staticmethod
    def _classes(attr_map: dict[str, str]) -> set[str]:
        return {x for x in attr_map.get("class", "").split() if x}

    def _append(self, key: str, value: str) -> None:
        if self.current is None or not value:
            return
        self.current.setdefault(key, []).append(value)

    def _begin_capture(self, tag: str, key: str) -> None:
        self.capture_tag = tag
        self.capture_key = key

    def handle_starttag(self, tag: str, attrs) -> None:
        a = self._attrs(attrs)
        classes = self._classes(a)

        if tag == "button" and a.get("data-page", "").isdigit():
            self.max_page = max(self.max_page, int(a["data-page"]))

        if self.current is None:
            if tag == "div" and "property_card" in classes:
                self.current = {
                    "sold": "property_card--sold" in classes,
                    "images": [],
                    "sticker_classes": [],
                    "all_text": [],
                }
                self.card_div_depth = 1
            return

        if tag == "div":
            self.card_div_depth += 1

        if tag == "a" and "property_card__url" in classes and a.get("href"):
            self.current["url"] = a["href"]

        if tag == "img":
            raw = a.get("src") or ""
            if raw:
                self.current["images"].append(raw)
            srcset = a.get("srcset") or ""
            if srcset:
                for part in srcset.split(","):
                    candidate = part.strip().split()[0] if part.strip() else ""
                    if candidate:
                        self.current["images"].append(candidate)

        if "property_card__city" in classes:
            self._begin_capture(tag, "city")
        elif "property_card__type" in classes:
            self._begin_capture(tag, "type")
        elif "property_card__price" in classes:
            self._begin_capture(tag, "price")
        elif "property_card__desc" in classes:
            self._begin_capture(tag, "desc")
        elif "icon_rooms" in classes:
            self._begin_capture(tag, "rooms")
        elif "icon_area" in classes:
            self._begin_capture(tag, "area")
        elif "sticker" in classes:
            self.current["sticker_classes"].extend(sorted(classes))
            self._begin_capture(tag, "sticker")
        elif "epc_label" in classes:
            self.current["epc_classes"] = sorted(classes)
            self._begin_capture(tag, "epc_text")

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self.current is None:
            return
        text = data.strip()
        if not text:
            return
        self.current["all_text"].append(text)
        if self.capture_key:
            self._append(self.capture_key, text)

    def handle_endtag(self, tag: str) -> None:
        if self.current is None:
            return

        if self.capture_tag == tag:
            self.capture_key = None
            self.capture_tag = None

        if tag == "div":
            self.card_div_depth -= 1
            if self.card_div_depth == 0:
                self.cards.append(self.current)
                self.current = None


def _joined(parts) -> str:
    if not parts:
        return ""
    if isinstance(parts, str):
        return normalize_text(parts)
    return normalize_text(" ".join(str(x) for x in parts if x))


def _structura_epc(card: dict) -> str | None:
    visible = _joined(card.get("epc_text"))
    if visible and re.fullmatch(r"[A-G](?:\+|\-)?(?:\s*\d{1,3})?", visible, flags=re.I):
        return visible.upper()

    for cls in card.get("epc_classes") or []:
        if not cls.startswith("class_"):
            continue
        code = cls[len("class_"):].upper()
        code = code.replace("_PLUS", "+").replace("_MINUS", "-")
        if re.fullmatch(r"[A-G](?:\+|\-)?", code):
            return code
    return None


def _structura_listing_from_card(card: dict) -> Listing | None:
    raw_url = str(card.get("url") or "").strip()
    if not raw_url:
        return None
    url = clean_url(urljoin(STRUCTURA_LIST_URL, raw_url))

    city = _joined(card.get("city")) or city_from_url(url, "structura")
    property_type = _joined(card.get("type")) or "Appartement"
    price_text = _joined(card.get("price"))
    desc = _joined(card.get("desc"))
    sticker = _joined(card.get("sticker"))
    all_text = _joined(card.get("all_text"))

    # The /a-louer/appartements page can contain subtypes such as Loft.  The
    # page itself is already the apartment category, so do not throw those away.
    if property_type.lower() in {"garage-cave", "bureau", "commerce-bureaux-rapport", "rez-de-chaussée commercial"}:
        return None

    bedrooms = parse_money(_joined(card.get("rooms")))
    area_text = _joined(card.get("area"))
    surface_match = re.search(r"([0-9]+(?:[.,][0-9]+)?)", area_text)
    surface = parse_float(surface_match.group(1)) if surface_match else None

    reference_match = re.search(r"/(\d+)$", urlsplit(url).path.rstrip("/"))
    reference = reference_match.group(1) if reference_match else None

    status_text = " ".join((sticker, all_text))
    available = not bool(card.get("sold")) and is_available(status_text)

    image_urls = _dedupe_image_urls(card.get("images") or [], url)
    title = f"{property_type} à louer"
    if city:
        title += f" à {city}"

    return Listing(
        source="structura",
        key=make_key("structura", url, reference),
        url=url,
        title=title,
        city=city,
        postal_code=None,
        address=None,
        rent=extract_price(price_text) or extract_price(all_text),
        charges=extract_charges(desc),
        bedrooms=bedrooms,
        surface=surface,
        floor=extract_floor(desc),
        epc=_structura_epc(card) or extract_epc(desc),
        image_url=image_urls[0] if image_urls else None,
        booking_url=None,
        reference=reference,
        available=available,
        image_urls=image_urls,
    )


def _parse_structura_list_html(markup: str) -> tuple[list[Listing], int]:
    parser = StructuraListParser()
    parser.feed(markup)

    listings: list[Listing] = []
    seen: set[str] = set()
    for card in parser.cards:
        item = _structura_listing_from_card(card)
        if item and item.key not in seen:
            seen.add(item.key)
            listings.append(item)

    return listings, parser.max_page


def _fetch_structura_html_sync() -> tuple[list[Listing], int]:
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "fr-BE,fr;q=0.9,en;q=0.7",
            "User-Agent": USER_AGENT,
            "Referer": "https://www.structura.be/fr/",
        }
    )
    response = session.get(STRUCTURA_LIST_URL, timeout=25)
    response.raise_for_status()
    return _parse_structura_list_html(response.text)


async def scrape_structura_html() -> tuple[list[Listing], int]:
    listings, max_page = await asyncio.to_thread(_fetch_structura_html_sync)
    print(
        f"structura HTML: {len(listings)} fiche(s) détectée(s) "
        f"sur la page appartements"
    )
    if listings:
        photo_counts = [len(item.image_urls) for item in listings]
        print(
            f"structura HTML: {sum(photo_counts)} photo(s) de prévisualisation "
            f"({sum(photo_counts) / len(photo_counts):.1f}/annonce en moyenne)"
        )
    return listings, max_page


async def scrape_structura_full_list_with_browser(browser: Browser) -> list[Listing]:
    """Fallback only when Structura exposes a 'load more' button."""
    context = await browser.new_context(user_agent=USER_AGENT, locale="fr-BE")
    page = await context.new_page()
    try:
        await page.goto(STRUCTURA_LIST_URL, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(800)

        for _ in range(10):
            button = page.locator("#search_body button[data-page]").first
            try:
                if await button.count() == 0 or not await button.is_visible():
                    break
                before = await page.locator("#search_results .property_card").count()
                await button.click()
                try:
                    await page.wait_for_function(
                        "before => document.querySelectorAll('#search_results .property_card').length > before",
                        arg=before,
                        timeout=8_000,
                    )
                except Exception:
                    await page.wait_for_timeout(1500)
                after = await page.locator("#search_results .property_card").count()
                if after <= before:
                    break
            except Exception:
                break

        markup = await page.content()
        listings, _ = _parse_structura_list_html(markup)
        print(f"structura navigateur: {len(listings)} fiche(s) après chargement complet")
        return listings
    finally:
        await context.close()


async def enrich_structura_listing(page: Page, item: Listing) -> None:
    """Open only an eligible Structura detail page for exact metadata/gallery."""
    try:
        await page.goto(item.url, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(600)
        text = await page.locator("body").inner_text(timeout=15_000)
    except Exception as exc:
        print(f"WARN: enrichissement Structura impossible {item.url}: {exc}", file=sys.stderr)
        return

    try:
        title = normalize_text(await page.locator("h1").first.inner_text(timeout=2_000))
        if title:
            item.title = title
    except Exception:
        pass

    address = await extract_jsonld_address(page) or extract_address_from_text(text)
    if address:
        item.address = address
        item.postal_code = extract_postal_code(address) or item.postal_code
    elif not item.postal_code:
        item.postal_code = extract_postal_code(text[:3000])

    item.rent = extract_price(text) or item.rent
    detail_charges = extract_charges(text)
    if detail_charges is not None:
        item.charges = detail_charges
    item.bedrooms = extract_bedrooms(text) or item.bedrooms
    item.surface = extract_surface(text) or item.surface
    item.floor = extract_floor(text) or item.floor
    item.epc = extract_epc(text) or item.epc
    item.booking_url = await extract_booking_url(page, item.url)
    item.available = is_available(text)

    og_image = await get_meta_content(
        page,
        [
            'meta[property="og:image"]',
            'meta[name="twitter:image"]',
            'meta[property="twitter:image"]',
        ],
    )
    if og_image:
        og_image = urljoin(item.url, og_image)

    page_images = await extract_image_urls(page, item.url, og_image)
    merged = list(item.image_urls)
    if og_image:
        merged.append(og_image)
    merged.extend(page_images)
    item.image_urls = _dedupe_image_urls(merged, item.url)
    if item.image_urls:
        item.image_url = item.image_urls[0]

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
        for match in re.findall(
            r'href=["\']([^"\']*/fr/appartement/a-louer/[^"\'#?]+)',
            markup,
            re.I,
        ):
            urls.add(clean_url(urljoin(list_url, match)))
    elif source == "structura":
        for match in re.findall(
            r'href=["\']([^"\']*/fr/appartement-a-louer-a-[^"\'#?]+/\d+)',
            markup,
            re.I,
        ):
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

    raw = re.sub(r"\s+(?:\d+w|\d+(?:\.\d+)?x)$", "", raw).strip()
    url = urljoin(base_url, raw)
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        return None

    lowered = url.lower()
    banned = (
        "favicon",
        "logo",
        "sprite",
        "icon",
        "marker",
        "placeholder",
        "avatar",
        "facebook",
        "instagram",
        "linkedin",
        "youtube",
        "tiktok",
        "/flags/",
        "cookie",
        "consent",
        "tracking",
        "pixel",
    )
    if any(word in lowered for word in banned):
        return None
    if re.search(r"\.(?:svg|gif)(?:$|[?#])", lowered):
        return None
    return url


_IMAGE_TRANSFORM_QUERY_KEYS = {
    "w",
    "width",
    "h",
    "height",
    "q",
    "quality",
    "fit",
    "crop",
    "fm",
    "format",
    "auto",
    "dpr",
    "rect",
    "sharp",
    "blur",
    "gravity",
    "g",
}


def _image_identity(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path
    # Living Stone CDN variants place resized files in a /conversions/ folder.
    # Treat those as the same underlying photo as the original asset.
    path = path.replace("/conversions/", "/")

    path = re.sub(
        r"(?i)(?:[-_](?:thumb|thumbnail|small|medium|large|xl|xxl|original)|[-_]\d{2,4}x\d{2,4})(?=\.(?:jpe?g|png|webp|avif)$)",
        "",
        path,
    )
    path = "/".join(
        segment
        for segment in path.split("/")
        if segment.lower()
        not in {"thumb", "thumbnail", "small", "medium", "large", "xl", "xxl", "original"}
    )
    path = re.sub(r"(?i)\.(?:jpe?g|png|webp|avif)$", "", path)

    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in _IMAGE_TRANSFORM_QUERY_KEYS:
            continue
        query.append((key, value))

    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), "")
    )


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
    """Collecte les photos probables de l'annonce.

    La photo de couverture est toujours placée en premier. Le filtrage des
    doublons élimine autant que possible les variantes responsive d'une même
    image.
    """
    candidates: list[str] = []
    if cover_url:
        candidates.append(cover_url)

    candidates.extend(await extract_jsonld_images(page, base_url))

    try:
        hrefs = await page.locator("a").evaluate_all(
            r"""els => els.flatMap(a => {
                const href = a.href || a.getAttribute('href') || '';
                if (!href) return [];
                const low = href.toLowerCase();
                if (/\.(jpg|jpeg|png|webp|avif)(\?|#|$)/.test(low)) return [href];
                if (/(image|photo|media|gallery|cdn)/.test(low) && a.querySelector('img')) return [href];
                return [];
            })"""
        )
        candidates.extend(hrefs)
    except Exception:
        pass

    try:
        images = await page.locator("img").evaluate_all(
            r"""els => els.flatMap(img => {
                const vals = [];
                const push = v => { if (v) vals.push(v); };

                push(img.currentSrc);
                push(img.src);

                for (const name of [
                    'data-src', 'data-lazy-src', 'data-original',
                    'data-image', 'data-large', 'data-full'
                ]) {
                    push(img.getAttribute(name));
                }

                for (const name of ['srcset', 'data-srcset']) {
                    const set = img.getAttribute(name) || '';
                    set.split(',').forEach(part => push(part.trim().split(/\s+/)[0]));
                }

                const w = img.naturalWidth || 0;
                const h = img.naturalHeight || 0;
                const text = (
                    (img.alt || '') + ' ' +
                    (typeof img.className === 'string' ? img.className : '') + ' ' +
                    (img.id || '')
                ).toLowerCase();

                const likelyGallery = /(gallery|photo|image|property|estate|slider|carousel|swiper)/.test(text);
                if ((w >= 280 && h >= 180) || likelyGallery) return vals;
                return [];
            })"""
        )
        candidates.extend(images)
    except Exception:
        pass

    urls = _dedupe_image_urls(candidates, base_url)
    cover_clean = _normalize_image_url(cover_url or "", base_url) if cover_url else None

    filtered: list[str] = []
    for url in urls:
        low = url.lower()

        if cover_clean and url == cover_clean:
            filtered.append(url)
            continue

        if any(
            token in low
            for token in (
                "/images/",
                "/image/",
                "/photos/",
                "/photo/",
                "/media/",
                "cloudinary",
                "cdn",
                "imgix",
                "property",
                "pand",
                "estate",
            )
        ):
            filtered.append(url)
            continue

        if re.search(r"\.(?:jpe?g|png|webp|avif)(?:$|[?])", low):
            filtered.append(url)

    return filtered


async def extract_booking_url(page: Page, base_url: str) -> str | None:
    try:
        controls = await page.locator("a, button, input[type=button], input[type=submit]").evaluate_all(
            r"""els => els.map(el => ({
                tag: el.tagName.toLowerCase(),
                text: ((el.innerText || el.value || '') + '').trim(),
                href: el.getAttribute('href'),
                dataUrl: el.getAttribute('data-url'),
                dataHref: el.getAttribute('data-href'),
                formAction: el.getAttribute('formaction'),
                onclick: el.getAttribute('onclick'),
                form: el.form ? el.form.getAttribute('action') : null
            }))"""
        )
    except Exception:
        return None

    for control in controls:
        text = normalize_text(control.get("text") or "").lower()
        if not any(label in text for label in VISIT_LABELS):
            continue

        candidates = [
            control.get("href"),
            control.get("dataUrl"),
            control.get("dataHref"),
            control.get("formAction"),
            control.get("form"),
        ]
        onclick = str(control.get("onclick") or "")
        if onclick:
            match = re.search(
                r"(?:https?://[^'\"\s)]+|(?:location(?:\.href)?|window\.open)\s*[=(]\s*['\"]([^'\"]+))",
                onclick,
                flags=re.I,
            )
            if match:
                candidates.append(match.group(1) or match.group(0))

        for raw in candidates:
            raw = str(raw or "").strip()
            if not raw or raw.startswith(("javascript:", "#")):
                continue
            url = urljoin(base_url, raw)
            if urlsplit(url).scheme in {"http", "https"}:
                # Keep query strings: booking systems often encode an estate ID.
                return url

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

            for child in value.values():
                found = walk(child)
                if found:
                    return found

        elif isinstance(value, list):
            for child in value:
                found = walk(child)
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




def _as_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _decode_living_stone_asset_name(raw_name: str | None) -> str:
    """Decode Living Stone's base64-ish media name when possible.

    The API's image `name` contains an encoded original path, which lets us
    identify promotional assets such as the online-appointment card and keep
    them out of the Telegram gallery.
    """
    if not raw_name:
        return ""

    import base64

    candidate = str(raw_name).strip()
    # The API sometimes appends a normal extension to the base64 payload.
    candidate = re.sub(r"\.(?:jpe?g|png|webp|avif)$", "", candidate, flags=re.I)
    candidate += "=" * (-len(candidate) % 4)
    try:
        return base64.b64decode(candidate).decode("utf-8", errors="ignore")
    except Exception:
        return ""



def _decode_living_stone_url_asset(url: str) -> str:
    import base64

    try:
        basename = Path(urlsplit(url).path).name
        basename = re.sub(
            r"-(?:thumb|small|medium|large|xlarge)\.(?:webp|jpe?g|png|avif)$",
            "",
            basename,
            flags=re.I,
        )
        basename = re.sub(r"\.(?:webp|jpe?g|png|avif)$", "", basename, flags=re.I)
        basename += "=" * (-len(basename) % 4)
        return base64.b64decode(basename).decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _living_stone_non_property_url(url: str) -> bool:
    decoded = _decode_living_stone_url_asset(url)
    haystack = f"{decoded} {url}".lower()
    markers = (
        "onlineafspraak",
        "online-afspraak",
        "online afspraak",
        "appointment",
        "afspraak",
        "/team/",
        "medewerker",
        "agent-photo",
    )
    return any(marker in haystack for marker in markers)


def _living_stone_non_property_asset(image: dict) -> bool:
    decoded = _decode_living_stone_asset_name(image.get("name"))
    haystack = " ".join(
        str(x or "")
        for x in (
            decoded,
            image.get("description"),
            image.get("original_url"),
        )
    ).lower()
    markers = (
        "onlineafspraak",
        "online-afspraak",
        "online afspraak",
        "appointment",
        "afspraak",
        "/team/",
        "medewerker",
        "agent-photo",
    )
    return any(marker in haystack for marker in markers)


def _living_stone_api_images(raw: dict) -> list[str]:
    images = list(raw.get("images") or [])
    images.sort(key=lambda image: _as_int(image.get("order")) or 0)

    urls: list[str] = []
    seen_uuids: set[str] = set()

    for image in images:
        if not isinstance(image, dict):
            continue
        uuid = str(image.get("uuid") or "")
        if uuid and uuid in seen_uuids:
            continue
        if uuid:
            seen_uuids.add(uuid)

        if _living_stone_non_property_asset(image):
            continue

        # large_url is plenty for Telegram and much lighter than original_url.
        url = (
            image.get("large_url")
            or image.get("xlarge_url")
            or image.get("original_url")
            or image.get("medium_url")
        )
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            urls.append(url)

    return urls


def _living_stone_address(raw: dict) -> str | None:
    # Respect Living Stone's own display_address flag. If they hide the exact
    # address on the public listing, Telegram only shows postal code + city.
    if raw.get("display_address") is not True:
        return None

    street = normalize_text(str(raw.get("street") or ""))
    number = normalize_text(str(raw.get("number") or ""))
    box = normalize_text(str(raw.get("box") or ""))
    postal = normalize_text(str(raw.get("zip") or ""))
    city = normalize_text(str(raw.get("city") or ""))

    first = " ".join(part for part in (street, number) if part)
    if box:
        first += f"/{box}"
    second = " ".join(part for part in (postal, city) if part)
    return ", ".join(part for part in (first, second) if part) or None


def _living_stone_listing_from_api(raw: dict) -> Listing | None:
    site_urls = raw.get("site_urls") or {}
    fr_path = site_urls.get("fr") if isinstance(site_urls, dict) else None
    if not fr_path:
        return None

    url = clean_url(urljoin("https://living-stone.be", str(fr_path)))
    reference = str(raw.get("external_id") or raw.get("id") or "").strip() or None
    image_urls = _living_stone_api_images(raw)

    status = str(raw.get("status") or "").upper()
    display_status = str(raw.get("display_status") or "").upper()
    purpose = str(raw.get("purpose_status") or raw.get("purpose") or "").upper()
    available = (
        status == "ACTIVE"
        and display_status == "ONLINE"
        and purpose == "FOR_RENT"
    )

    surface_value = _as_float(raw.get("size_livable_area"))

    return Listing(
        source="living_stone",
        key=f"living_stone:{reference}" if reference else make_key("living_stone", url),
        url=url,
        title=normalize_text(str(raw.get("name") or "Appartement à louer")),
        city=normalize_text(str(raw.get("city") or "")) or None,
        postal_code=normalize_text(str(raw.get("zip") or "")) or None,
        address=_living_stone_address(raw),
        rent=_as_int(raw.get("price")),
        charges=None,  # absent from the list API; enriched from the detail page
        bedrooms=_as_int(raw.get("bedroom_count")),
        surface=surface_value,
        floor=normalize_text(str(raw.get("floor") or "")) or None,
        epc=normalize_text(str(raw.get("epc_label") or "")) or None,
        image_url=image_urls[0] if image_urls else None,
        booking_url=None,
        reference=reference,
        available=available,
        image_urls=image_urls,
    )


def _fetch_living_stone_api_sync() -> list[Listing]:
    """Fetch all currently available Living Stone rental flats via JSON API."""
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Referer": LIVING_STONE_LIST_URL,
        }
    )

    page = 1
    last_page = 1
    listings: list[Listing] = []
    seen_keys: set[str] = set()

    while page <= last_page:
        response = session.get(
            LIVING_STONE_API_URL,
            params={
                "page": page,
                "per_page": 100,
                "sort": "published_at-desc",
                "category": "",
                "purpose": "FOR_RENT",
                "investment_property": "false",
                "available_only": "true",
            },
            timeout=25,
        )
        response.raise_for_status()
        payload = response.json()

        if not isinstance(payload, dict):
            raise RuntimeError("Réponse API Living Stone inattendue")

        last_page = max(1, _as_int(payload.get("last_page")) or 1)
        rows = payload.get("data") or []

        for raw in rows:
            if not isinstance(raw, dict):
                continue
            # We only want apartments/flats, including penthouses/duplexes/etc.
            if str(raw.get("category") or "").upper() != "FLAT":
                continue

            item = _living_stone_listing_from_api(raw)
            if item and item.key not in seen_keys:
                seen_keys.add(item.key)
                listings.append(item)

        page += 1

    return listings


async def scrape_living_stone_api() -> list[Listing]:
    listings = await asyncio.to_thread(_fetch_living_stone_api_sync)
    print(f"living_stone API: {len(listings)} appartement(s) disponible(s)")
    if listings:
        api_photo_counts = [len(item.image_urls) for item in listings]
        print(
            f"living_stone API: {sum(api_photo_counts)} image(s) de prévisualisation "
            f"({sum(api_photo_counts) / len(api_photo_counts):.1f}/annonce en moyenne)"
        )
    return listings


async def enrich_living_stone_listing(page: Page, item: Listing) -> None:
    """Enrich an API listing with charges, booking link and full gallery.

    The list API gives excellent structured metadata but, in the payload we saw,
    only a few preview images and no monthly charges. We therefore open only the
    listings that already match the user's price/bedroom criteria.
    """
    try:
        await page.goto(item.url, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(700)
        text = await page.locator("body").inner_text(timeout=15_000)
    except Exception as exc:
        print(f"WARN: enrichissement Living Stone impossible {item.url}: {exc}", file=sys.stderr)
        return

    item.charges = extract_charges(text)
    item.booking_url = await extract_booking_url(page, item.url)

    try:
        title = normalize_text(await page.locator("h1").first.inner_text(timeout=2_000))
        if title:
            item.title = title
    except Exception:
        pass

    og_image = await get_meta_content(
        page,
        [
            'meta[property="og:image"]',
            'meta[name="twitter:image"]',
            'meta[property="twitter:image"]',
        ],
    )
    if og_image:
        og_image = urljoin(item.url, og_image)

    page_images = await extract_image_urls(page, item.url, og_image)
    merged = list(item.image_urls)
    if og_image:
        merged.append(og_image)
    merged.extend(page_images)
    merged = [url for url in merged if not _living_stone_non_property_url(url)]

    # _image_identity knows that /conversions/...-large.webp and the original
    # DigitalOcean image are the same asset, so the first image no longer doubles.
    item.image_urls = _dedupe_image_urls(merged, item.url)
    if item.image_urls:
        item.image_url = item.image_urls[0]

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
        [
            'meta[property="og:image"]',
            'meta[name="twitter:image"]',
            'meta[property="twitter:image"]',
        ],
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
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def telegram_escape(value: str) -> str:
    return html.escape(value, quote=False)


def format_euros(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " €"


def detected_time() -> str:
    return datetime.now(BRUSSELS_TZ).strftime("%H:%M")


def format_notification(item: Listing, event: str = "new") -> str:
    source_name = "Structura" if item.source == "structura" else "Living Stone"
    headline = (
        "🆕 <b>NOUVEL APPARTEMENT</b>"
        if event == "new"
        else "📉 <b>BAISSE DE PRIX</b>"
    )
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
            lines.append(
                f"💶 <b>{format_euros(item.rent)}</b> + "
                f"{format_euros(item.charges)} charges"
            )
            lines.append(
                f"💰 Total loyer + charges : "
                f"<b>{format_euros(item.total_monthly or item.rent)}/mois</b>"
            )
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
        maps_url = (
            "https://www.google.com/maps/search/?api=1&query="
            + quote_plus(item.address)
        )
        buttons.append({"text": "📍 Maps", "url": maps_url})

    rows = [buttons]

    if item.booking_url:
        rows.append(
            [{"text": "📅 Réserver une visite", "url": item.booking_url}]
        )

    return {"inline_keyboard": rows}


def _telegram_credentials() -> tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID doivent être configurés."
        )

    return token, chat_id


def _telegram_api(
    token: str,
    method: str,
    payload: dict,
    timeout: int = 30,
) -> requests.Response:
    response = requests.post(
        f"https://api.telegram.org/bot{token}/{method}",
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    return response


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _send_single_photo(
    token: str,
    chat_id: str,
    photo_url: str,
    caption: str | None = None,
    keyboard: dict | None = None,
) -> bool:
    payload: dict = {"chat_id": chat_id, "photo": photo_url}

    if caption:
        payload.update({"caption": caption, "parse_mode": "HTML"})
    if keyboard:
        payload["reply_markup"] = keyboard

    try:
        _telegram_api(token, "sendPhoto", payload, timeout=30)
        return True
    except requests.RequestException as exc:
        print(
            f"WARN: photo Telegram ignorée ({photo_url}): {exc}",
            file=sys.stderr,
        )
        return False


def _send_album_batch(
    token: str,
    chat_id: str,
    photos: list[str],
    caption: str | None = None,
) -> bool:
    media: list[dict] = []
    for index, photo_url in enumerate(photos):
        entry: dict = {"type": "photo", "media": photo_url}
        if index == 0 and caption:
            entry["caption"] = caption
            entry["parse_mode"] = "HTML"
        media.append(entry)

    try:
        _telegram_api(
            token,
            "sendMediaGroup",
            {"chat_id": chat_id, "media": media},
            timeout=45,
        )
        return True
    except requests.RequestException as exc:
        print(
            f"WARN: album Telegram échoué: {exc}",
            file=sys.stderr,
        )
        return False


def send_telegram(item: Listing, event: str = "new", max_photos: int = 10) -> None:
    """Send one compact apartment block to Telegram.

    Preferred layout:
      1) one Telegram gallery containing up to `max_photos` photos, with the
         apartment details as the album caption;
      2) one small "Liens rapides" message directly below with the buttons.

    Telegram media groups cannot carry inline keyboards, hence the separate
    button message.
    """
    token, chat_id = _telegram_credentials()
    caption = format_notification(item, event=event)
    keyboard = telegram_keyboard(item)

    photos = list(item.image_urls or ([] if not item.image_url else [item.image_url]))
    if item.image_url and item.image_url not in photos:
        photos.insert(0, item.image_url)
    photos = _dedupe_image_urls(photos, item.url)

    if max_photos > 0:
        photos = photos[:max_photos]

    print(
        f"Telegram: {item.source} {item.key} -> "
        f"{len(photos)} photo(s) dans la galerie"
    )

    content_sent = False

    if len(photos) >= 2:
        content_sent = _send_album_batch(
            token,
            chat_id,
            photos,
            caption=caption,
        )

    elif len(photos) == 1:
        content_sent = _send_single_photo(
            token,
            chat_id,
            photos[0],
            caption=caption,
        )

    if not content_sent:
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

    # Buttons live in their own compact message because sendMediaGroup does not
    # accept reply_markup / inline keyboards.
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


def update_state_and_notify(
    listings: Iterable[Listing],
    config: dict,
    state: dict,
) -> tuple[int, int]:
    max_rent = int(config["max_rent"])
    min_bedrooms = int(config["min_bedrooms"])

    # Nombre maximum de photos AU TOTAL dans la galerie Telegram.
    max_photos = int(config.get("telegram_max_photos", 0) or 0)

    force_send_current = os.environ.get("FORCE_SEND_CURRENT", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

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
        qualifies = item.qualifies(
            max_rent=max_rent,
            min_bedrooms=min_bedrooms,
        )

        if qualifies:
            qualifying_count += 1

        old = records.get(item.key)

        # Key migration safety: the Living Stone API gives us a cleaner stable
        # reference than HTML scraping. Reuse an existing state record when the
        # URL is the same so switching scraper does not resend old apartments.
        if old is None:
            for previous_key, previous in list(records.items()):
                previous_url = previous.get("url") if isinstance(previous, dict) else None
                if (
                    previous_url
                    and previous.get("source") == item.source
                    and clean_url(str(previous_url)) == clean_url(item.url)
                ):
                    old = previous
                    if previous_key != item.key:
                        records.pop(previous_key, None)
                    break

        old = old or {}
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
            send_telegram(
                item,
                event=event,
                max_photos=max_photos,
            )
            notification_count += 1
            already_notified = True

        # Premier lancement : on crée une baseline sans spammer Telegram.
        if first_run and qualifies:
            already_notified = True

        record = asdict(item)

        # On ne stocke pas toutes les URLs de galerie dans seen.json.
        # Elles sont utiles uniquement au moment d'envoyer la notification.
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
    state = load_json(
        STATE_FILE,
        {"initialized": False, "listings": {}},
    )

    required = {"max_rent", "min_bedrooms", "sources"}
    missing = required - set(config)
    if missing:
        raise RuntimeError(
            f"Configuration incomplète: {', '.join(sorted(missing))}"
        )

    max_rent = int(config["max_rent"])
    min_bedrooms = int(config["min_bedrooms"])

    all_listings: list[Listing] = []
    errors: list[str] = []
    living_stone_items: list[Listing] = []
    structura_items: list[Listing] = []
    structura_max_page = 1

    # Both sources now use a lightweight structured discovery step first.
    # Living Stone exposes JSON; Structura already renders the listing metadata
    # directly in the HTML returned by a normal GET request.
    if config["sources"].get("living_stone", True):
        try:
            living_stone_items = await scrape_living_stone_api()
            if not living_stone_items:
                errors.append("living_stone: aucune fiche trouvée via l'API")
        except Exception as exc:
            errors.append(f"living_stone API: {exc}")
            print(f"ERROR living_stone API: {exc}", file=sys.stderr)

    if config["sources"].get("structura", True):
        try:
            structura_items, structura_max_page = await scrape_structura_html()
            if not structura_items:
                errors.append("structura: aucune fiche trouvée dans le HTML")
        except Exception as exc:
            errors.append(f"structura HTML: {exc}")
            print(f"ERROR structura HTML: {exc}", file=sys.stderr)

    living_needs_browser = any(
        item.qualifies(max_rent=max_rent, min_bedrooms=min_bedrooms)
        for item in living_stone_items
    )
    structura_needs_browser = bool(structura_items) and (
        structura_max_page > 1
        or any(
            item.qualifies(max_rent=max_rent, min_bedrooms=min_bedrooms)
            for item in structura_items
        )
    )

    # A browser is now used only for:
    #   - Structura's occasional "load more" pagination;
    #   - detail pages that already pass the user's coarse filters, so we can
    #     recover the exact address, booking link and a fuller photo gallery.
    if living_needs_browser or structura_needs_browser:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                if structura_items and structura_max_page > 1:
                    try:
                        fully_loaded = await scrape_structura_full_list_with_browser(browser)
                        if fully_loaded:
                            structura_items = fully_loaded
                    except Exception as exc:
                        errors.append(f"structura pagination: {exc}")
                        print(f"ERROR structura pagination: {exc}", file=sys.stderr)

                if structura_items:
                    eligible_structura = [
                        item
                        for item in structura_items
                        if item.qualifies(
                            max_rent=max_rent,
                            min_bedrooms=min_bedrooms,
                        )
                    ]
                    if eligible_structura:
                        context = await browser.new_context(
                            user_agent=USER_AGENT,
                            locale="fr-BE",
                        )
                        detail_page = await context.new_page()
                        try:
                            print(
                                f"structura: enrichissement de "
                                f"{len(eligible_structura)} annonce(s) éligible(s)"
                            )
                            for index, item in enumerate(eligible_structura, start=1):
                                await enrich_structura_listing(detail_page, item)
                                if index % 5 == 0 or index == len(eligible_structura):
                                    print(
                                        f"structura: {index}/{len(eligible_structura)} "
                                        f"fiche(s) enrichie(s)"
                                    )
                        finally:
                            await context.close()

                if living_stone_items and living_needs_browser:
                    context = await browser.new_context(
                        user_agent=USER_AGENT,
                        locale="fr-BE",
                    )
                    detail_page = await context.new_page()
                    try:
                        eligible_living = [
                            item
                            for item in living_stone_items
                            if item.qualifies(
                                max_rent=max_rent,
                                min_bedrooms=min_bedrooms,
                            )
                        ]
                        print(
                            f"living_stone: enrichissement de "
                            f"{len(eligible_living)} annonce(s) éligible(s)"
                        )
                        for index, item in enumerate(eligible_living, start=1):
                            await enrich_living_stone_listing(detail_page, item)
                            if index % 5 == 0 or index == len(eligible_living):
                                print(
                                    f"living_stone: {index}/{len(eligible_living)} "
                                    f"fiche(s) enrichie(s)"
                                )
                    finally:
                        await context.close()
            finally:
                await browser.close()

    all_listings.extend(structura_items)
    all_listings.extend(living_stone_items)

    if not all_listings:
        raise RuntimeError(
            "Aucune annonce récupérée. " + " | ".join(errors)
        )

    qualifying, notifications = update_state_and_notify(
        all_listings,
        config,
        state,
    )
    save_state(state)

    print(
        f"Terminé: {len(all_listings)} annonce(s), "
        f"{qualifying} correspondant aux critères, "
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
