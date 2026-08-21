#!/usr/bin/env python3
"""
Live Cam Playlist Generator — chococams.com (Optimized)
- Single persistent Playwright browser shared across all scrapes
- Parallel API calls + parallel page fetches (ThreadPoolExecutor)
- Scrapes ALL live models from listing (paginated)
- Indian girls page → Favourites group
- Named favourites from FAVOURITE_MODELS list → Favourites group
- Browser used only as last resort
"""

import re
import json
import time
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

BASE_URL       = "https://chococams.com"
MODEL_LIST_URL = f"{BASE_URL}/model/"

# These pages' models are ALL treated as Favourites
FAVOURITE_PAGES = [
    f"{BASE_URL}/female/ethnicity/indian",  # Indian girls → Favourites
]

# Named favourites (in addition to any scraped from FAVOURITE_PAGES)
FAVOURITE_MODELS = [
    "redpointx_", "tommy_and_sophie", "denobluora", "shannelpink_",
    "stupid_little_kitten", "lilkimchii", "_tenderpassion_", "gimbobar",
    "beawolf1887", "dolls_wallen", "miamax88", "twogirls2boys", "hakxram",
    "evaandtommi", "calehot98", "bonieandclyde1", "lovers_clover_x",
    "drake_and_zara", "lailaasher", "sc_andre", "beibi_sin", "loyliksi",
    "cumplaycouple", "bellapazzia13", "laurenxcros", "dexandlily",
    "cutefacebigass", "selinabentzzz", "playwithmil", "julesxdann",
    "alissa_and_alann", "dreamspussy", "notfallenangel", "lola_linss",
    "_sweetandsinner_", "butterfly_on_dick", "ashleyandzamir", "sam_y_sen",
    "gamebelka", "juliyajam", "amateur2friendswithbenefits", "jessdant_luv",
    "limiyan", "marilyn_mike", "lost_wanderers", "keutypie", "luis7777hui",
    "sweet_sugar87", "ebangelion", "sophywhisper", "elisabethwillian",
    "jackandjill", "alpugh", "homeofsex_", "ali_and_louie1", "jasson_n_emma",
    "amandatalk", "sandra_and_charly", "assayo444", "ethan_chloee",
    "luna_horny00", "kinga_da_vinci", "sashahoneyvice", "danyandannarearden",
    "kjbennet", "the_isa_bella", "jonnalinaproduction", "Litzy1_", "MaxMia",
    "Threesome-no-mercy", "crazycats_",
]
FAVOURITE_SET = {m.lower() for m in FAVOURITE_MODELS}

KNOWN_SOURCES = ["stripchat", "chaturbate", "bongacams", "camsoda", "cam4"]

# 0 = scrape ALL live models; set e.g. 200 to cap
MAX_LIVE_MODELS = 0

OUTPUT_DIR  = Path("playlists")
OUTPUT_FILE = OUTPUT_DIR / "live.m3u"

# Concurrency
API_WORKERS     = 10   # parallel direct-API calls
PAGE_WORKERS    = 8    # parallel requests-based page fetches
BROWSER_WORKERS = 2    # Playwright tabs (keep low)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         BASE_URL,
}

# Precompiled patterns
HLS_PATTERNS = [re.compile(r'(https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)', re.I)]
THUMB_PATTERNS = [
    re.compile(r'(https?://thumb[^\s"\'<>]+\.jpg[^\s"\'<>]*)',                  re.I),
    re.compile(r'(https?://[^\s"\'<>]*mmcdn\.com[^\s"\'<>]+\.jpg[^\s"\'<>]*)', re.I),
    re.compile(r'(https?://[^\s"\'<>]*thumbnail[^\s"\'<>]+\.jpg[^\s"\'<>]*)',  re.I),
    re.compile(r'(https?://[^\s"\'<>]*preview[^\s"\'<>]+\.jpg[^\s"\'<>]*)',    re.I),
]
LINK_RE = re.compile(r'/model/(\w+)/(\w[\w\-_.]*)')

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DATA CLASS
# ---------------------------------------------------------------------------

class ModelStream:
    __slots__ = ("name", "hls_url", "thumb_url", "source", "is_favourite")

    def __init__(self, name, hls_url, thumb_url="", source="", is_favourite=False):
        self.name         = name
        self.hls_url      = hls_url
        self.thumb_url    = thumb_url
        self.source       = source
        self.is_favourite = is_favourite

    def __repr__(self):
        return f"<ModelStream {self.name} fav={self.is_favourite}>"


# ---------------------------------------------------------------------------
# SHARED PLAYWRIGHT BROWSER (singleton, thread-safe tabs)
# ---------------------------------------------------------------------------

class BrowserPool:
    """Single Playwright browser; semaphore limits concurrent tabs."""

    def __init__(self, max_tabs=BROWSER_WORKERS):
        self._pw      = None
        self._browser = None
        self._sem     = threading.Semaphore(max_tabs)
        self._started = False

    def start(self):
        if not HAS_PLAYWRIGHT or self._started:
            return
        self._pw      = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-setuid-sandbox",
            "--disable-dev-shm-usage", "--disable-gpu",
        ])
        self._started = True
        logger.info("Browser pool started")

    def stop(self):
        if self._started:
            try:
                self._browser.close()
                self._pw.stop()
            except Exception:
                pass
            self._started = False

    def fetch(self, url, wait_ms=10000):
        """Open a tab, wait, return (html, [captured_m3u8_urls])."""
        if not self._started:
            return "", []
        self._sem.acquire()
        try:
            ctx  = self._browser.new_context(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1920, "height": 1080},
            )
            page = ctx.new_page()
            captured = []

            def _on_response(resp):
                if ".m3u8" in resp.url:
                    captured.append(resp.url)

            page.on("response", _on_response)
            try:
                r = page.goto(url, wait_until="domcontentloaded", timeout=25000)
                if r and r.status == 404:
                    return "", []
                page.wait_for_timeout(wait_ms)
                html = page.content()
            except Exception as e:
                logger.debug(f"Browser tab {url}: {e}")
                html = ""
            finally:
                try:
                    page.close()
                    ctx.close()
                except Exception:
                    pass
            return html, captured
        finally:
            self._sem.release()


BROWSER = BrowserPool()


# ---------------------------------------------------------------------------
# HTTP HELPERS  (thread-local sessions)
# ---------------------------------------------------------------------------

_local = threading.local()

def get_session():
    if not hasattr(_local, "s"):
        s = requests.Session()
        s.headers.update(HEADERS)
        _local.s = s
    return _local.s


def fetch_html(url, ref=None, tries=3):
    s = get_session()
    h = {"Referer": ref} if ref else {}
    for attempt in range(tries):
        try:
            r = s.get(url, headers=h, timeout=20, allow_redirects=True)
            if r.status_code == 200:
                return r.text
            if r.status_code in (404, 403):
                return ""
        except Exception as e:
            logger.debug(f"fetch attempt {attempt+1} {url}: {e}")
        time.sleep(0.4 * (attempt + 1))
    return ""


# ---------------------------------------------------------------------------
# EXTRACTION HELPERS
# ---------------------------------------------------------------------------

def clean_url(url):
    url = url.strip().rstrip("\\").strip("'\"")
    for ch in (" ", ">", "<", "'", '"'):
        url = url.split(ch)[0]
    return url


def extract_hls(html, captured=None):
    if captured:
        masters = [u for u in captured if "master" in u.lower()]
        return masters[0] if masters else captured[0]
    for pat in HLS_PATTERNS:
        ms = pat.findall(html)
        if ms:
            for m in ms:
                if "master" in m.lower():
                    return clean_url(m)
            return clean_url(ms[0])
    return ""


def extract_thumb(html, model_name=""):
    for pat in THUMB_PATTERNS:
        ms = pat.findall(html)
        if ms:
            if model_name:
                for m in ms:
                    if model_name.lower() in m.lower():
                        return clean_url(m)
            return clean_url(ms[0])
    soup = BeautifulSoup(html, "lxml")
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        return og["content"]
    return ""


# ---------------------------------------------------------------------------
# DIRECT PLATFORM APIs
# ---------------------------------------------------------------------------

def _try_stripchat(name):
    try:
        r = get_session().get(
            f"https://stripchat.com/api/front/v2/models/username/{name}/cam",
            headers={**HEADERS, "Referer": "https://stripchat.com/"}, timeout=12,
        )
        if r.status_code == 200:
            cam = r.json().get("cam", {})
            if cam.get("isCamAvailable"):
                server = cam.get("viewServers", {}).get("flashphoner-hls", "")
                sname  = cam.get("streamName", "")
                if server and sname:
                    return ModelStream(
                        name=name,
                        hls_url=f"https://b-{server}.stripst.com/hls/{sname}/{sname}.m3u8",
                        thumb_url=f"https://img.strpst.com/thumbs/{sname}_webp",
                        source="stripchat",
                        is_favourite=name.lower() in FAVOURITE_SET,
                    )
    except Exception as e:
        logger.debug(f"Stripchat API {name}: {e}")
    return None


def _try_chaturbate(name):
    try:
        r = get_session().post(
            "https://chaturbate.com/get_edge_hls_url_ajax/",
            data={"room_slug": name, "bandwidth": "high"},
            headers={**HEADERS,
                     "Referer": f"https://chaturbate.com/{name}/",
                     "X-Requested-With": "XMLHttpRequest"},
            timeout=12,
        )
        if r.status_code == 200:
            hls = r.json().get("url", "")
            if hls:
                return ModelStream(
                    name=name,
                    hls_url=hls,
                    thumb_url=f"https://thumb.live.mmcdn.com/ri/{name}.jpg",
                    source="chaturbate",
                    is_favourite=name.lower() in FAVOURITE_SET,
                )
    except Exception as e:
        logger.debug(f"Chaturbate API {name}: {e}")
    return None


def _try_bongacams(name):
    try:
        r = get_session().post(
            "https://bongacams.com/tools/amf.php",
            json={"method": "getRoomData", "args": [name, False]},
            headers={**HEADERS, "Referer": "https://bongacams.com/"}, timeout=12,
        )
        if r.status_code == 200:
            server = r.json().get("localData", {}).get("videoServerUrl", "")
            if server:
                return ModelStream(
                    name=name,
                    hls_url=f"https:{server}/hls/stream_{name}/playlist.m3u8",
                    source="bongacams",
                    is_favourite=name.lower() in FAVOURITE_SET,
                )
    except Exception as e:
        logger.debug(f"Bongacams API {name}: {e}")
    return None


_API_FUNCS = [_try_stripchat, _try_chaturbate, _try_bongacams]


def try_direct_apis(name):
    """Fire all platform APIs in parallel; return first hit."""
    with ThreadPoolExecutor(max_workers=len(_API_FUNCS)) as ex:
        futures = {ex.submit(fn, name): fn for fn in _API_FUNCS}
        for fut in as_completed(futures):
            result = fut.result()
            if result:
                for f in futures:
                    f.cancel()
                return result
    return None


# ---------------------------------------------------------------------------
# CHOCOCAMS PAGE SCRAPING
# ---------------------------------------------------------------------------

def scrape_model_requests(name):
    """Try all source URL patterns with plain HTTP."""
    urls = [(f"{BASE_URL}/model/{src}/{name}", src) for src in KNOWN_SOURCES]
    urls.append((f"{BASE_URL}/model/{name}", "unknown"))
    for url, source in urls:
        html = fetch_html(url, ref=BASE_URL)
        if not html:
            continue
        hls = extract_hls(html)
        if hls:
            return ModelStream(name, hls, extract_thumb(html, name), source,
                               name.lower() in FAVOURITE_SET)
    return None


def scrape_model_browser(name):
    """Browser fallback using shared BrowserPool."""
    if not HAS_PLAYWRIGHT or not BROWSER._started:
        return None
    for source in KNOWN_SOURCES:
        url = f"{BASE_URL}/model/{source}/{name}"
        html, captured = BROWSER.fetch(url, wait_ms=12000)
        if not html:
            continue
        hls = extract_hls(html, captured)
        if hls:
            return ModelStream(name, hls, extract_thumb(html, name), source,
                               name.lower() in FAVOURITE_SET)
    return None


def resolve_model(name):
    """Full pipeline: direct APIs → requests scrape → browser."""
    return (
        try_direct_apis(name)
        or scrape_model_requests(name)
        or scrape_model_browser(name)
    )


# ---------------------------------------------------------------------------
# LISTING / FAVOURITE-PAGE SCRAPING
# ---------------------------------------------------------------------------

def _models_from_html(html, page_url, seen):
    """Extract (name, source, thumb, full_url) tuples from a listing page."""
    soup   = BeautifulSoup(html, "lxml")
    items  = []

    card_selectors = [
        "div.model-card", "div.model-item", "div.cam-card", "div.performer",
        "div.thumb", "a.model-link", "div[class*='model']", "div[class*='cam']",
        "div[class*='performer']", "article", "div.grid-item", "li.model",
    ]

    cards = []
    for sel in card_selectors:
        cards = soup.select(sel)
        if len(cards) >= 3:
            break

    targets = cards if cards else soup.find_all("a", href=LINK_RE)

    for el in targets:
        link = (el.find("a", href=True) if el.name != "a" else el) or el
        if not link:
            continue
        href = link.get("href", "")
        m = LINK_RE.search(href)
        if not m:
            continue
        source, name = m.group(1), m.group(2)
        if source not in KNOWN_SOURCES:
            name, source = source, "unknown"
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        img   = link.find("img") or (el.find("img") if el != link else None)
        thumb = ""
        if img:
            thumb = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
        full_url = href if href.startswith("http") else BASE_URL + href
        items.append({"name": name, "source": source, "thumb": thumb, "url": full_url})

    return items


def scrape_pages(start_urls, seen, label="listing", is_favourite_page=False):
    """
    Paginate through start_urls, collecting model info dicts.
    Returns list of dicts with is_favourite pre-set.
    """
    session = get_session()
    collected = []

    page_urls = list(start_urls)
    for base in start_urls:
        for pg in range(2, 50):
            page_urls.append(f"{base}?page={pg}")
            page_urls.append(f"{base}/page/{pg}/")

    for url in page_urls:
        if MAX_LIVE_MODELS and len(collected) >= MAX_LIVE_MODELS:
            break
        try:
            resp = session.get(url, timeout=15, allow_redirects=True)
            if resp.status_code != 200:
                continue
            # Redirect away from paginated URL = end of pages
            if resp.url != url and ("page=" in url or "/page/" in url):
                break

            items = _models_from_html(resp.text, url, seen)
            if not items:
                # Try browser for JS-rendered listing
                html, _ = BROWSER.fetch(url, wait_ms=6000)
                if html:
                    items = _models_from_html(html, url, seen)
            if not items:
                break

            for item in items:
                item["is_favourite"] = is_favourite_page or item["name"].lower() in FAVOURITE_SET
            collected.extend(items)
            logger.info(f"  [{label}] {url} → +{len(items)} (total {len(collected)})")
            time.sleep(0.3)

        except Exception as e:
            logger.warning(f"  [{label}] {url}: {e}")

    return collected


# ---------------------------------------------------------------------------
# PARALLEL STREAM RESOLUTION
# ---------------------------------------------------------------------------

def resolve_batch(model_infos, label=""):
    """
    Resolve HLS streams for a list of model info dicts in parallel.
    Returns list of ModelStream.
    """
    results = []
    lock    = threading.Lock()
    total   = len(model_infos)
    done    = [0]

    def _resolve(info):
        name   = info["name"]
        stream = resolve_model(name)
        with lock:
            done[0] += 1
            tag = "✅" if stream else "❌"
            logger.info(f"  {tag} [{done[0]}/{total}] {name}")
        if not stream:
            return None
        stream.is_favourite = info.get("is_favourite", False) or name.lower() in FAVOURITE_SET
        if not stream.thumb_url:
            stream.thumb_url = info.get("thumb", "")
        return stream

    with ThreadPoolExecutor(max_workers=API_WORKERS) as ex:
        futures = {ex.submit(_resolve, info): info for info in model_infos}
        for fut in as_completed(futures):
            r = fut.result()
            if r:
                results.append(r)

    return results


# ---------------------------------------------------------------------------
# PLAYLIST GENERATION
# ---------------------------------------------------------------------------

def generate_playlist(streams):
    favourites = sorted([s for s in streams if s.is_favourite],     key=lambda s: s.name.lower())
    others     = sorted([s for s in streams if not s.is_favourite], key=lambda s: s.name.lower())

    lines = [
        "#EXTM3U",
        f"# Generated : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"# Favourites: {len(favourites)}",
        f"# Live      : {len(others)}",
        f"# Total     : {len(streams)}",
        "",
    ]

    for section, section_streams, group in [
        ("Favourites", favourites, "⭐ Favourites"),
        ("Live Models", others,    "📺 Live Models"),
    ]:
        if not section_streams:
            continue
        lines.append(f"# --- {section} ({len(section_streams)}) ---")
        for s in section_streams:
            logo = f' tvg-logo="{s.thumb_url}"' if s.thumb_url else ""
            src  = f" [{s.source}]"             if s.source    else ""
            lines.append(f'#EXTINF:-1{logo} group-title="{group}",{s.name}{src}')
            lines.append(s.hls_url)
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    logger.info("=" * 60)
    logger.info("Live Cam Playlist Generator — chococams.com (Optimized)")
    logger.info("=" * 60)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if HAS_PLAYWRIGHT:
        BROWSER.start()

    try:
        seen     = set()
        all_info = []

        # ── 1. Scrape FAVOURITE_PAGES (Indian girls etc.) ─────────────────
        logger.info("\n--- Scraping Favourite Pages ---")
        for url in FAVOURITE_PAGES:
            fav_items = scrape_pages([url], seen, label="fav-page", is_favourite_page=True)
            logger.info(f"  {url} → {len(fav_items)} models")
            all_info.extend(fav_items)

        # ── 2. Collect named favourites not already found ─────────────────
        for name in FAVOURITE_MODELS:
            if name.lower() not in seen:
                seen.add(name.lower())
                all_info.append({
                    "name": name, "source": "", "thumb": "",
                    "url": "", "is_favourite": True,
                })

        # ── 3. Scrape ALL live models from main listing ───────────────────
        logger.info("\n--- Scraping All Live Models from Listing ---")
        live_items = scrape_pages(
            [MODEL_LIST_URL], seen, label="listing", is_favourite_page=False
        )
        logger.info(f"  Listing → {len(live_items)} additional models")
        all_info.extend(live_items)

        # ── 4. Resolve all in parallel ────────────────────────────────────
        logger.info(f"\n--- Resolving {len(all_info)} models ---")
        streams = resolve_batch(all_info)

        # ── 5. Write playlist ─────────────────────────────────────────────
        if streams:
            playlist = generate_playlist(streams)
            OUTPUT_FILE.write_text(playlist, encoding="utf-8")
        else:
            OUTPUT_FILE.write_text(
                f"#EXTM3U\n# Generated: "
                f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
                "# No live streams found\n",
                encoding="utf-8",
            )

        # ── 6. Summary ────────────────────────────────────────────────────
        favs   = [s for s in streams if s.is_favourite]
        others = [s for s in streams if not s.is_favourite]
        logger.info("\n" + "=" * 60)
        logger.info("RESULTS")
        logger.info(f"  ⭐ Favourites live : {len(favs)}")
        logger.info(f"  📺 Others live     : {len(others)}")
        logger.info(f"  📋 Total           : {len(streams)}")
        logger.info(f"  💾 Saved           : {OUTPUT_FILE}")
        logger.info("=" * 60)

    finally:
        BROWSER.stop()

    logger.info("Done!")


if __name__ == "__main__":
    main()
