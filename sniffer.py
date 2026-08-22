#!/usr/bin/env python3
"""
PornHao Batch Scraper – automatically reads model.txt
Usage: python pornhao_batch.py
"""

import re
import time
import json
import logging
import argparse
import os
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from urllib.parse import urljoin

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

BASE_URL = "https://pornhao.com"
HEADLESS = False
SCROLL_PAUSE = 2
REQUEST_DELAY = 1

# ─── Chrome Driver ──────────────────────────────────────────────

def get_driver():
    options = webdriver.ChromeOptions()
    if HEADLESS:
        options.add_argument('--headless=new')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
    options.add_argument('--window-size=1920,1080')
    options.add_argument(
        'user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    )
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)

# ─── Scroll to Load All Videos ──────────────────────────────────

def scroll_to_load_all(driver, max_scrolls=50):
    last_height = driver.execute_script("return document.body.scrollHeight")
    scroll_count = 0
    while scroll_count < max_scrolls:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(SCROLL_PAUSE)
        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == last_height:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight - 200);")
            time.sleep(SCROLL_PAUSE)
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                log.info("Reached bottom of page.")
                break
        last_height = new_height
        scroll_count += 1
        log.info(f"Scrolled {scroll_count} times, height: {new_height}")
    driver.execute_script("window.scrollTo(0, 0);")
    time.sleep(1)

# ─── Extract Video Page URLs ──────────────────────────────────

def extract_video_urls(driver):
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH, "//a[contains(@href, '/video/')]"))
        )
    except:
        log.warning("No video links found on page.")
        return []

    links = driver.find_elements(By.XPATH, "//a[contains(@href, '/video/')]")
    video_urls = []
    seen = set()
    for a in links:
        href = a.get_attribute('href')
        if href and href not in seen:
            full_url = urljoin(BASE_URL, href)
            if full_url.startswith(BASE_URL) and '/video/' in full_url:
                seen.add(full_url)
                video_urls.append(full_url)

    if not video_urls:
        links = driver.find_elements(By.XPATH, "//a[@data-href and contains(@data-href, '/video/')]")
        for a in links:
            href = a.get_attribute('data-href')
            if href:
                full_url = urljoin(BASE_URL, href)
                if full_url not in seen:
                    seen.add(full_url)
                    video_urls.append(full_url)

    log.info(f"Found {len(video_urls)} video page URLs.")
    return video_urls

# ─── Extract MP4 from a Video Page ──────────────────────

def get_mp4_from_video_page(video_url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Referer': BASE_URL + '/'
    }
    try:
        r = requests.get(video_url, headers=headers, timeout=15)
        r.raise_for_status()
    except Exception as e:
        log.warning(f"Failed to fetch {video_url}: {e}")
        return None

    soup = BeautifulSoup(r.text, 'html.parser')

    video_tag = soup.find('video')
    if video_tag and video_tag.get('src'):
        mp4 = video_tag['src']
        mp4 = mp4.replace('&amp;', '&')
        return mp4

    mp4_pattern = re.compile(r'(https?://[^\s"\']+\.mp4[^\s"\']*)')
    match = mp4_pattern.search(r.text)
    if match:
        return match.group(1)

    return None

# ─── Scrape Single Model ────────────────────────────────────────────────

def scrape_model(model_name, max_videos=None):
    model_url = urljoin(BASE_URL, f"/models/{model_name}/")
    log.info(f"Scraping model: {model_url}")

    driver = get_driver()
    try:
        driver.get(model_url)
        time.sleep(3)
        scroll_to_load_all(driver)
        video_page_urls = extract_video_urls(driver)
        log.info(f"Found {len(video_page_urls)} video pages.")
    finally:
        driver.quit()

    if not video_page_urls:
        log.warning(f"No videos found for model {model_name}.")
        return []

    if max_videos:
        video_page_urls = video_page_urls[:max_videos]

    results = []
    for idx, v_url in enumerate(video_page_urls, 1):
        log.info(f"[{idx}/{len(video_page_urls)}] Processing: {v_url}")
        mp4 = get_mp4_from_video_page(v_url)
        if mp4:
            title = v_url.split('/')[-1].replace('-', ' ').title()
            results.append({
                "model": model_name,
                "title": title,
                "page_url": v_url,
                "mp4_url": mp4
            })
            log.info(f"  ✅ MP4 found: {mp4[:80]}...")
        else:
            log.warning(f"  ❌ No MP4 found for {v_url}")
        time.sleep(REQUEST_DELAY)

    log.info(f"Successfully extracted {len(results)} MP4 URLs for {model_name}.")
    return results

# ─── Batch Scrape ──────────────────────────────────────────────

def scrape_models(model_names, max_videos=None):
    all_results = []
    for model in model_names:
        log.info(f"\n{'='*60}")
        log.info(f"Processing model: {model}")
        log.info(f"{'='*60}")
        results = scrape_model(model.strip(), max_videos)
        all_results.extend(results)
        time.sleep(2)
    return all_results

# ─── Save Playlist (Extended M3U Format) ────────────────────────────

def save_playlist(entries, json_file="playlist.json", m3u_file="playlist.m3u", group_title="Movies - Filmes | [XXX] Adultos"):
    with open(json_file, 'w', encoding='utf-8') as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)

    m3u_lines = ['#EXTM3U', '']
    for e in entries:
        title = e.get('title', 'Unknown')
        tvg_name = f"[XXX] {title} [Adulto]"
        tvg_id = ""
        tvg_logo = ""
        group = group_title
        extinf = f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{tvg_name}" tvg-logo="{tvg_logo}" group-title="{group}",{tvg_name}'
        m3u_lines.append(extinf)
        m3u_lines.append(e['mp4_url'])
        m3u_lines.append('')

    with open(m3u_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(m3u_lines))

    log.info(f"Saved {len(entries)} videos to {json_file} and {m3u_file}")

# ─── Command Line ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Scrape multiple models from pornhao.com (auto-reads model.txt)")
    parser.add_argument('--models', default='', help='Comma-separated list of model names (optional)')
    parser.add_argument('--models-file', default='model.txt', help='File containing model names (default: model.txt)')
    parser.add_argument('--max', type=int, default=None, help='Max videos per model')
    parser.add_argument('--group-title', default='Movies - Filmes | [XXX] Adultos', help='Group title for M3U')
    parser.add_argument('--output-json', default='playlist.json', help='Output JSON file')
    parser.add_argument('--output-m3u', default='playlist.m3u', help='Output M3U file')
    args = parser.parse_args()

    model_names = []
    if args.models:
        model_names = [m.strip() for m in args.models.split(',') if m.strip()]
    else:
        file_path = args.models_file
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    model_names = [line.strip() for line in f if line.strip() and not line.startswith('#')]
                log.info(f"Read {len(model_names)} models from {file_path}.")
            except Exception as e:
                log.error(f"Failed to read file: {e}")
                return
        else:
            log.warning(f"File {file_path} not found. Using default model list.")
            default_models = ["violet-myers", "penny-barber", "sheena-ryder", "ava-addams", 
                              "comatoss", "brandi-love", "kayla-kayden", "ariella-ferrera"]
            log.info(f"Default models: {default_models}")
            model_names = default_models

    if not model_names:
        log.error("No models specified. Exiting.")
        return

    log.info(f"Processing {len(model_names)} models: {model_names}")

    entries = scrape_models(model_names, max_videos=args.max)
    if entries:
        save_playlist(entries, json_file=args.output_json, m3u_file=args.output_m3u, 
                      group_title=args.group_title)
    else:
        log.warning("No videos found.")

if __name__ == "__main__":
    main()
