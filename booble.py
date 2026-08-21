#!/usr/bin/env python3
"""
Live Playlist Updater — booble.com
- Parallel stream fetching (multiple Chrome tabs via thread pool)
- Full model discovery: paginates ALL live models (not just top N)
- Favorites checked first with priority
- Async avatar checks
- Single browser, multiple windows/tabs
"""

import re
import os
import json
import time
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

# ─────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────
MODELS_FILE   = "models.txt"
PLAYLIST_FILE = "playlist.m3u"
SITE_BASE     = "https://booble.com"
AVATAR_BASE   = "https://booble.com/avatar"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Referer":    "https://booble.com/",
    "Accept":     "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

PAGE_WAIT      = 8      # seconds to wait for HLS after page load (reduced from 10)
TIMEOUT        = 25
MAX_LIVE_MODELS = 0     # 0 = scrape ALL live models; set e.g. 100 to cap
STREAM_WORKERS = 5      # parallel Chrome tabs for stream fetching
AVATAR_WORKERS = 20     # parallel avatar HEAD checks

SKIP_WORDS = {
    "girl", "couple", "trans", "guy", "login", "signup", "register",
    "terms", "privacy", "contact", "about", "faq", "help", "support",
    "search", "categories", "tags", "popular", "new", "top", "index",
    "page", "home", "cam", "category", "male", "female", "couples",
    "girls", "boys", "men", "women", "lang", "en", "de", "es", "fr",
    "settings", "favorites", "tokens", "premium", "vip", "join",
    "undefined", "null", "true", "false", "api", "static", "assets",
    "avatar", "images", "css", "js", "fonts", "embed", "player",
}

M3U8_PATTERNS = [
    re.compile(r'(https?://edge-hls[^\s"\'\\\]<>]+\.m3u8[^\s"\'\\\]<>]*)',        re.I),
    re.compile(r'(https?://[^\s"\'\\\]<>]*saawsedge\.com[^\s"\'\\\]<>]+\.m3u8[^\s"\'\\\]<>]*)', re.I),
    re.compile(r'(https?://[^\s"\'\\\]<>]+/master/[^\s"\'\\\]<>]+\.m3u8[^\s"\'\\\]<>]*)', re.I),
    re.compile(r'(https?://[^\s"\'\\\]<>]+_auto\.m3u8[^\s"\'\\\]<>]*)',            re.I),
    re.compile(r'(https?://[^\s"\'\\\]<>]+/hls/\d+/[^\s"\'\\\]<>]+\.m3u8[^\s"\'\\\]<>]*)', re.I),
    re.compile(r'(https?://[^\s"\'\\\]<>]+\.m3u8\?[^\s"\'\\\]<>]*)',               re.I),
    re.compile(r'(https?://[^\s"\'\\\]<>]+\.m3u8)',                                re.I),
]

STREAM_KEYWORDS = {"hls", "edge", "saaws", "master", "auto", "stream", "live", "cdn"}

JS_EXTRACT = """
(function() {
    for (var v of document.querySelectorAll('video')) {
        if (v.src && v.src.includes('m3u8')) return v.src;
        if (v.currentSrc && v.currentSrc.includes('m3u8')) return v.currentSrc;
    }
    for (var s of document.querySelectorAll('video source')) {
        if (s.src && s.src.includes('m3u8')) return s.src;
    }
    try {
        if (typeof Hls !== 'undefined') {
            for (var v of document.querySelectorAll('video')) {
                if (v.hlsPlayer) return v.hlsPlayer.url;
                if (v._hls) return v._hls.url;
            }
        }
    } catch(e) {}
    var keys = ['hlsUrl','streamUrl','videoUrl','playUrl','liveUrl',
                'streamSrc','playerSrc','hlsSrc','masterUrl'];
    for (var k of keys) {
        if (window[k] && typeof window[k] === 'string' && window[k].includes('m3u8'))
            return window[k];
    }
    try {
        if (window.playerConfig && window.playerConfig.hlsUrl)
            return window.playerConfig.hlsUrl;
    } catch(e) {}
    return null;
})();
"""


# ─────────────────────────────────────────
#  BROWSER POOL
# ─────────────────────────────────────────

_driver_pool: list = []          # list of (driver, lock) pairs
_pool_lock = threading.Lock()
_pool_ready = threading.Event()


def get_chrome_binary():
    paths = [
        "/usr/bin/google-chrome-stable", "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser", "/usr/bin/chromium",
    ]
    for p in paths:
        try:
            r = subprocess.run([p, "--version"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return p
        except Exception:
            pass
    return None


def _make_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--mute-audio")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    chrome_bin = get_chrome_binary()
    if chrome_bin:
        options.binary_location = chrome_bin

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(TIMEOUT)
    return driver


def init_pool(size=STREAM_WORKERS):
    """Start `size` Chrome instances in parallel threads."""
    print(f"[POOL] Starting {size} Chrome instances...")

    def _start_one(idx):
        try:
            d = _make_driver()
            with _pool_lock:
                _driver_pool.append((d, threading.Lock()))
            print(f"[POOL] Driver {idx+1}/{size} ready")
        except Exception as e:
            print(f"[POOL] Driver {idx+1} failed: {e}")

    with ThreadPoolExecutor(max_workers=size) as ex:
        list(ex.map(_start_one, range(size)))

    print(f"[POOL] {len(_driver_pool)} drivers ready")
    _pool_ready.set()


def close_pool():
    for driver, lock in _driver_pool:
        try:
            driver.quit()
        except Exception:
            pass
    _driver_pool.clear()
    print("[POOL] All drivers closed")


class _DriverCtx:
    """Context manager: acquire a free driver from the pool."""
    def __init__(self):
        self._driver = None
        self._lock = None

    def __enter__(self):
        # Wait until a driver is free (spin with short sleep)
        while True:
            with _pool_lock:
                for driver, lock in _driver_pool:
                    if lock.acquire(blocking=False):
                        self._driver = driver
                        self._lock = lock
                        return driver
            time.sleep(0.1)

    def __exit__(self, *_):
        if self._lock:
            self._lock.release()


# ─────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────

def load_models(filepath):
    if not os.path.exists(filepath):
        print(f"[ERROR] {filepath} not found!")
        return []
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read().strip()
    models = [n.strip() for n in content.split(",") if n.strip()]
    print(f"[INFO] Loaded {len(models)} favorite(s): {models}")
    return models


def get_avatar_url(model_name, session=None):
    """Try avatar extensions in parallel, return first 200 OK."""
    extensions = [".jpeg", ".jpg", ".png", ".webp"]
    s = session or requests.Session()

    def _try(ext):
        url = f"{AVATAR_BASE}/{model_name}{ext}"
        try:
            r = s.head(url, headers=HEADERS, timeout=4, allow_redirects=True)
            if r.status_code == 200 and "image" in r.headers.get("Content-Type", ""):
                return url
        except Exception:
            pass
        return None

    with ThreadPoolExecutor(max_workers=len(extensions)) as ex:
        futures = [ex.submit(_try, ext) for ext in extensions]
        for fut in as_completed(futures):
            result = fut.result()
            if result:
                for f in futures:
                    f.cancel()
                return result

    return f"{AVATAR_BASE}/{model_name}.jpeg"


def _extract_from_logs(logs):
    found = []
    for entry in logs:
        try:
            msg = json.loads(entry["message"])["message"]
            if msg["method"] in ("Network.requestWillBeSent", "Network.responseReceived"):
                params = msg["params"]
                url = (
                    params.get("request", {}).get("url") or
                    params.get("response", {}).get("url") or ""
                )
                if ".m3u8" in url and any(k in url for k in STREAM_KEYWORDS):
                    found.append(url)
        except Exception:
            pass
    return found


def _pick_best(urls):
    if not urls:
        return None
    for u in urls:
        if "master" in u:
            return u
    for u in urls:
        if "auto" in u:
            return u
    return urls[0]


def _clean_m3u8(url):
    url = url.replace("\\u002F", "/").replace("\\/", "/")
    url = re.sub(r'["\'\]}>\\]+$', '', url)
    return url


# ─────────────────────────────────────────
#  STREAM FETCHER (uses pool)
# ─────────────────────────────────────────

def fetch_stream(model_name):
    """Grab an available Chrome driver and extract the HLS stream URL."""
    page_url = f"{SITE_BASE}/{model_name}"

    try:
        with _DriverCtx() as driver:
            # Clear previous page
            try:
                driver.get("about:blank")
            except Exception:
                pass

            try:
                driver.get(page_url)
            except Exception as e:
                print(f"  [{model_name}] Page load warning: {e}")

            time.sleep(PAGE_WAIT)

            # ── 1. Network logs ──
            try:
                logs = driver.get_log("performance")
                urls = _extract_from_logs(logs)
                if urls:
                    best = _pick_best(urls)
                    if best:
                        print(f"  ✅ [{model_name}] network log: {best[:80]}")
                        return best
            except Exception:
                pass

            # ── 2. Page source regex ──
            try:
                source = driver.page_source
                for pat in M3U8_PATTERNS:
                    hits = pat.findall(source)
                    if hits:
                        clean = _clean_m3u8(hits[0])
                        if any(k in clean.lower() for k in STREAM_KEYWORDS):
                            print(f"  ✅ [{model_name}] source regex: {clean[:80]}")
                            return clean
            except Exception:
                pass

            # ── 3. JS extraction ──
            try:
                result = driver.execute_script(JS_EXTRACT)
                if result and isinstance(result, str) and "m3u8" in result:
                    if result.startswith("http"):
                        print(f"  ✅ [{model_name}] JS: {result[:80]}")
                        return result
                    hits = re.findall(r'https?://[^\s"\'\\\]<>]+\.m3u8[^\s"\'\\\]<>]*',
                                      result, re.I)
                    if hits:
                        clean = _clean_m3u8(hits[0])
                        print(f"  ✅ [{model_name}] JS-JSON: {clean[:80]}")
                        return clean
            except Exception:
                pass

            # ── 4. Retry after brief extra wait ──
            time.sleep(4)
            try:
                logs = driver.get_log("performance")
                urls = _extract_from_logs(logs)
                best = _pick_best(urls)
                if best:
                    print(f"  ✅ [{model_name}] retry log: {best[:80]}")
                    return best
            except Exception:
                pass

            # ── 5. Offline check ──
            try:
                low = driver.page_source.lower()
                offline = any(x in low for x in [
                    "offline", "is not online", "currently offline",
                    "room is offline", "model is offline", "not broadcasting",
                ])
                status = "OFFLINE" if offline else "stream not found"
                print(f"  ❌ [{model_name}] {status}")
            except Exception:
                print(f"  ❌ [{model_name}] unknown error")

            return None

    except Exception as e:
        print(f"  [ERROR] [{model_name}] {e}")
        return None


# ─────────────────────────────────────────
#  PARALLEL BATCH RESOLVER
# ─────────────────────────────────────────

def resolve_batch(names, label="models"):
    """
    Resolve stream URLs for a list of model names in parallel.
    Returns dict: {name: {"stream": url, "avatar": url}}
    """
    results = {}
    total = len(names)
    done_count = [0]
    lock = threading.Lock()

    def _resolve(name):
        stream = fetch_stream(name)
        with lock:
            done_count[0] += 1
            tag = "✅ LIVE" if stream else "❌ offline"
            print(f"  [{done_count[0]}/{total}] {name} → {tag}")
        if not stream:
            return name, None
        avatar = get_avatar_url(name)
        return name, {"stream": stream, "avatar": avatar}

    with ThreadPoolExecutor(max_workers=STREAM_WORKERS) as ex:
        futures = {ex.submit(_resolve, name): name for name in names}
        for fut in as_completed(futures):
            name, info = fut.result()
            if info:
                results[name] = info

    return results


# ─────────────────────────────────────────
#  FULL SITE SCRAPER — ALL LIVE MODELS
# ─────────────────────────────────────────

def _parse_names_from_html(html, seen):
    """Extract valid model names from raw HTML."""
    names = []
    raw = set()

    raw.update(re.findall(
        r'href=["\'](?:https?://[^"\']*)?/([a-zA-Z0-9_-]{3,50})["\']',
        html, re.I
    ))
    raw.update(re.findall(
        r'data-(?:model|performer|username|name|slug)=["\']([a-zA-Z0-9_-]{3,50})["\']',
        html, re.I
    ))

    for name in raw:
        name = name.strip().strip("-_")
        nl = name.lower()
        if (
            nl not in seen
            and nl not in SKIP_WORDS
            and 3 <= len(name) <= 50
            and re.match(r'^[a-zA-Z0-9_-]+$', name)
            and not nl.endswith((".js", ".css", ".png", ".jpg", ".gif", ".svg"))
        ):
            seen.add(nl)
            names.append(name)

    return names


def scrape_all_live_models(fav_set=None):
    """
    Scrape ALL live models from booble.com across all categories and pages.
    Returns dict: {"girl": [names], "couple": [names], "trans": [names], ...}
    """
    fav_set = fav_set or set()

    categories = [
        ("girl",   [f"{SITE_BASE}/", f"{SITE_BASE}/girls"]),
        ("couple", [f"{SITE_BASE}/couple", f"{SITE_BASE}/couples"]),
        ("trans",  [f"{SITE_BASE}/trans"]),
        ("guy",    [f"{SITE_BASE}/guys", f"{SITE_BASE}/guy"]),
    ]

    result = {cat: [] for cat, _ in categories}
    seen_global = set()
    session = requests.Session()
    session.headers.update(HEADERS)

    for category, start_urls in categories:
        seen_cat = set()
        pages_to_try = list(start_urls)

        # Add paginated variants
        for base_url in start_urls:
            for pg in range(2, 30):
                pages_to_try.append(f"{base_url}?page={pg}")
                pages_to_try.append(f"{base_url}/page/{pg}/")

        print(f"\n[SCRAPE] Category: {category.upper()}")

        for page_url in pages_to_try:
            if MAX_LIVE_MODELS and len(result[category]) >= MAX_LIVE_MODELS:
                break

            try:
                resp = session.get(page_url, timeout=12, allow_redirects=True)
                if resp.status_code != 200:
                    continue
                if resp.url != page_url and "page" in page_url:
                    # Redirected away from paginated URL — end of pages
                    break

                html = resp.text

                # Detect end-of-pagination (no model cards)
                if not re.search(r'href=["\'][^"\']{3,50}["\']', html):
                    break

                names = _parse_names_from_html(html, seen_global)
                new_names = [n for n in names if n.lower() not in seen_cat and n.lower() not in fav_set]

                if not new_names:
                    # Likely hit a duplicate/empty page
                    break

                for n in new_names:
                    seen_cat.add(n.lower())
                    result[category].append(n)

                print(f"  {page_url} → +{len(new_names)} ({category}, total {len(result[category])})")
                time.sleep(0.3)

            except Exception as e:
                print(f"  [WARN] {page_url}: {e}")

        # Selenium fallback if requests got nothing
        if not result[category]:
            print(f"  [SCRAPE] Selenium fallback for {category}...")
            try:
                with _DriverCtx() as driver:
                    target = start_urls[0]
                    driver.get(target)
                    time.sleep(4)
                    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                    time.sleep(2)
                    html = driver.page_source
                    names = _parse_names_from_html(html, seen_global)
                    new = [n for n in names if n.lower() not in fav_set]
                    result[category].extend(new)
                    print(f"  Selenium → +{len(new)} {category}")
            except Exception as e:
                print(f"  Selenium error: {e}")

    print("\n[SCRAPE] Summary:")
    for cat, names in result.items():
        print(f"  {cat}: {len(names)}")
    return result


# ─────────────────────────────────────────
#  PLAYLIST WRITER
# ─────────────────────────────────────────

CATEGORY_LABELS = {
    "girl":   "👩 Girls",
    "couple": "👫 Couples",
    "trans":  "🏳️‍⚧️ Trans",
    "guy":    "👨 Guys",
}


def generate_m3u(favorite_live, category_live):
    total = len(favorite_live) + sum(len(v) for v in category_live.values())
    lines = [
        "#EXTM3U",
        f"# Updated : {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
        f"# Source  : booble.com",
        f"# Favs    : {len(favorite_live)}",
        *[f"# {cat.capitalize():<8}: {len(v)}" for cat, v in category_live.items()],
        f"# Total   : {total}",
        "",
    ]

    for model, info in favorite_live.items():
        lines += [
            f'#EXTINF:-1 tvg-id="{model}" tvg-name="{model}" '
            f'tvg-logo="{info["avatar"]}" group-title="⭐ Favorites",⭐ {model}',
            info["stream"],
        ]

    for cat, live_dict in category_live.items():
        group = CATEGORY_LABELS.get(cat, cat.capitalize())
        for model, info in live_dict.items():
            lines += [
                f'#EXTINF:-1 tvg-id="{model}" tvg-name="{model}" '
                f'tvg-logo="{info["avatar"]}" group-title="{group}",{model}',
                info["stream"],
            ]

    return "\n".join(lines)


# ─────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────

def main():
    print("=" * 60)
    print("  Live Playlist Updater — booble.com  (Optimized)")
    print(f"  {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print("=" * 60)

    # Start Chrome pool
    init_pool(STREAM_WORKERS)

    try:
        # ── 1. Load favorites ──────────────────────────────────────────────
        favorite_names = load_models(MODELS_FILE)
        fav_set = {n.lower() for n in favorite_names}

        # ── 2. Scrape ALL live models from site ────────────────────────────
        print("\n--- Discovering ALL Live Models ---")
        discovered = scrape_all_live_models(fav_set=fav_set)

        # ── 3. Resolve favorites (parallel) ───────────────────────────────
        favorite_live = {}
        if favorite_names:
            print(f"\n--- Resolving {len(favorite_names)} Favorites ---")
            favorite_live = resolve_batch(favorite_names, "favorites")
            for s in favorite_live.values():
                pass  # is_favourite flag not needed here; generate_m3u handles it

        # ── 4. Resolve all discovered models (parallel) ───────────────────
        category_live = {}
        for cat, names in discovered.items():
            if not names:
                continue
            print(f"\n--- Resolving {len(names)} {cat.upper()} models ---")
            category_live[cat] = resolve_batch(names, cat)

        # ── 5. Write playlist ──────────────────────────────────────────────
        playlist = generate_m3u(favorite_live, category_live)
        Path(PLAYLIST_FILE).write_text(playlist, encoding="utf-8")

        # ── 6. Summary ─────────────────────────────────────────────────────
        total = len(favorite_live) + sum(len(v) for v in category_live.values())
        print("\n" + "=" * 60)
        print("  📊 RESULTS")
        print("=" * 60)
        print(f"\n  ⭐ Favorites: {len(favorite_live)}/{len(favorite_names)}")
        for n in favorite_names:
            print(f"     {'✅' if n in favorite_live else '❌'} {n}")

        for cat, live in category_live.items():
            label = CATEGORY_LABELS.get(cat, cat)
            found = len(discovered.get(cat, []))
            print(f"\n  {label}: {len(live)} live / {found} checked")
            for n in live:
                print(f"     ✅ {n}")

        print(f"\n  📺 Total live: {total}")
        print(f"  💾 Saved: {PLAYLIST_FILE}")
        print("=" * 60)

    finally:
        close_pool()


if __name__ == "__main__":
    main()
