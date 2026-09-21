import csv
import logging
import os
import random
import re
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
from typing import Callable, Optional, Dict, Any, List

from bs4 import BeautifulSoup
import pandas as pd
from selenium import webdriver
from selenium.common.exceptions import (
    TimeoutException,
    WebDriverException,
    InvalidSessionIdException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager


def check_internet(host="one.one.one.one", port=80, timeout=3) -> bool:
    """Return True if we can reach the internet."""
    try:
        addr = socket.gethostbyname(host)
        conn = socket.create_connection((addr, port), timeout)
        conn.close()
        return True
    except OSError:
        return False


def clean_text(val: Optional[str]) -> Optional[str]:
    """Clean text by stripping icon glyphs (Unicode private use area) and extraneous whitespace."""
    if not val:
        return None
    cleaned = "".join(c for c in val if not (0xE000 <= ord(c) <= 0xF8FF)).strip()
    return cleaned if cleaned else None


def is_valid_phone(val: Optional[str]) -> bool:
    """Return True if string contains a legitimate phone number (minimum 7-10 digits, no 'Send to phone' labels)."""
    if not val:
        return False
    digits = re.sub(r"\D", "", str(val))
    if len(digits) < 7 or len(digits) > 15:
        return False
    cleaned_lower = str(val).lower()
    if any(w in cleaned_lower for w in ["send", "phone", "call", "mobile", "direction", "website", "share", "save", "nearby"]):
        return False
    return True


class ScraperEngine:
    """Thread-safe, crash-resilient Google Maps scraper engine with session recycling and smart scroll."""

    def __init__(
        self,
        query: Optional[str] = None,
        queries: Optional[List[str]] = None,
        url: Optional[str] = None,
        from_links: Optional[str] = None,
        output_name: str = "output",
        output_dir: str = "output",
        headless: bool = True,
        links_only: bool = False,
        max_results: Optional[int] = None,
        default_delay: tuple = (2.0, 4.0),
        page_load_timeout: int = 15,
        scroll_pause: float = 1.5,
        max_scroll_stalls: int = 15,
        recycle_interval: int = 35,
        engine_mode: str = "turbo",
        block_images: bool = True,
        workers: int = 1,
        require_phone: bool = True,
        on_log: Optional[Callable[[str, str], None]] = None,
        on_phase: Optional[Callable[[str], None]] = None,
        on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_item_scraped: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_complete: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ):
        self.query = query
        self.queries = [q.strip() for q in queries if q and q.strip()] if queries else None
        self.url = url
        self.from_links = from_links
        self.output_name = output_name or "output"
        self.output_dir = output_dir or "output"
        self.headless = headless
        self.links_only = links_only
        self.max_results = max_results if (max_results and max_results > 0) else None
        self.default_delay = default_delay
        self.page_load_timeout = page_load_timeout
        self.scroll_pause = scroll_pause
        self.max_scroll_stalls = max_scroll_stalls
        self.recycle_interval = recycle_interval
        self.engine_mode = engine_mode or "turbo"
        self.block_images = block_images
        self.workers = max(1, min(workers or 1, 4))
        self.require_phone = require_phone

        # Callbacks
        self.on_log = on_log
        self.on_phase = on_phase
        self.on_progress = on_progress
        self.on_item_scraped = on_item_scraped
        self.on_complete = on_complete
        self.on_error = on_error

        # Control flags & state
        self._stop_requested = threading.Event()
        self.is_running = False
        self.driver: Optional[webdriver.Chrome] = None

        # Scraping results
        self.links: List[str] = []
        self.scraped_items: List[Dict[str, Any]] = []
        self.links_file: Optional[str] = None
        self.details_file: Optional[str] = None

        os.makedirs(self.output_dir, exist_ok=True)

    def log(self, message: str, level: str = "INFO"):
        """Emit log event."""
        if self.on_log:
            try:
                self.on_log(message, level)
            except Exception:
                pass

    def stop(self):
        """Signal engine to stop."""
        self._stop_requested.set()
        self.log("Cancellation requested by user. Finishing current operation...", "WARN")

    @property
    def is_stopped(self) -> bool:
        return self._stop_requested.is_set()

    def _wait_for_internet(self) -> bool:
        """Pause execution if internet is lost and auto-resume once connection is restored."""
        if check_internet():
            return True

        self.log("⚠️ Internet connection disconnected! Pausing scraper and waiting for network to reconnect...", "WARN")
        if self.on_phase:
            self.on_phase("Network Lost — Waiting to Reconnect")

        elapsed = 0
        while not check_internet():
            if self.is_stopped:
                return False
            time.sleep(3.0)
            elapsed += 3
            if elapsed % 15 == 0:
                self.log(f"Waiting for internet to reconnect... ({elapsed}s elapsed)", "WARN")

        self.log("✅ Internet reconnected successfully! Resuming scrape automatically...", "SUCCESS")
        if self.on_phase:
            self.on_phase("Resumed: Extracting")
        return True

    def _create_driver(self) -> webdriver.Chrome:
        """Create and configure Chrome WebDriver with high stability options."""
        self.log(f"Initializing Chrome WebDriver (Headless: {self.headless})...", "INFO")
        opts = Options()
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--lang=en")
        opts.add_argument("--no-first-run")
        opts.add_argument("--no-default-browser-check")
        opts.add_argument("--disable-notifications")
        opts.add_argument("--disable-popup-blocking")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--disable-software-rasterizer")
        opts.add_argument("--dns-prefetch-disable")
        opts.page_load_strategy = "eager"

        # Anti-freeze memory guard: Block heavy photos, WebGL 3D maps, and remote fonts
        if self.block_images or self.engine_mode == "turbo":
            prefs = {
                "profile.managed_default_content_settings.images": 2,
                "profile.default_content_setting_values.notifications": 2,
            }
            opts.add_experimental_option("prefs", prefs)
            opts.add_argument("--blink-settings=imagesEnabled=false")
            opts.add_argument("--disable-remote-fonts")

        # Anti-lag process guard: Set Windows background priority
        try:
            import psutil
            if sys.platform == "win32":
                psutil.Process(os.getpid()).nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        except Exception:
            pass

        opts.add_experimental_option(
            "excludeSwitches", ["enable-logging", "enable-automation"]
        )
        opts.add_experimental_option("useAutomationExtension", False)

        if self.headless:
            opts.add_argument("--headless=new")
            opts.add_argument("--window-size=1920,1080")
        else:
            opts.add_argument("--window-size=1280,800")

        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=opts)

        if not self.headless:
            try:
                driver.maximize_window()
            except Exception:
                pass

        driver.set_page_load_timeout(35)
        return driver

    def _recycle_driver(self):
        """Cleanly close old driver and spin up a fresh one to release Chrome memory leak."""
        try:
            if self.driver:
                self.driver.quit()
        except Exception:
            pass
        finally:
            self.driver = None

        time.sleep(1.0)
        self.driver = self._create_driver()

    def _random_delay(self):
        """Sleep for random delay within range."""
        if self.is_stopped:
            return
        if self.engine_mode == "turbo":
            delay = random.uniform(0.4, 0.8)
        elif self.engine_mode == "browserless_http":
            delay = random.uniform(0.2, 0.5)
        elif self.engine_mode == "feed_fast":
            delay = 0.1
        else:
            delay = random.uniform(*self.default_delay)
        slice_time = 0.2
        elapsed = 0.0
        while elapsed < delay and not self.is_stopped:
            time.sleep(min(slice_time, delay - elapsed))
            elapsed += slice_time

    def _collect_links(self, search_url: str) -> List[str]:
        """Phase 1: Scroll Google Maps feed with native JS extraction and end-of-list detection."""
        self.log("Phase 1 — Loading Google Maps search feed...", "INFO")
        if self.on_phase:
            self.on_phase("Phase 1: Collecting Links")

        feed_url = search_url + ("&hl=en" if "?" in search_url else "?hl=en")
        self.driver.get(feed_url)

        # Wait for feed container
        try:
            WebDriverWait(self.driver, self.page_load_timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'div[role="feed"], .m6QErb[role="feed"]'))
            )
        except TimeoutException:
            self.log("Results feed container not immediately found. Searching for direct place cards...", "WARN")

        links = set()
        stall_count = 0

        while not self.is_stopped:
            prev_count = len(links)

            # High-speed in-browser JS link extraction (100x faster than parsing full page source)
            try:
                batch = self.driver.execute_script("""
                    const found = [];
                    document.querySelectorAll('a[href*="/maps/place/"]').forEach(a => {
                        if (a.href) found.push(a.href);
                    });
                    return found;
                """) or []
                for href in batch:
                    links.add(href)
                    if self.max_results and len(links) >= self.max_results:
                        break

                # If feed_fast mode is active, directly extract business info from search feed cards
                if self.engine_mode == "feed_fast":
                    try:
                        feed_cards = self.driver.execute_script("""
                            const items = [];
                            const cards = document.querySelectorAll('div[role="feed"] div.Nv2PK, .m6QErb div.Nv2PK');
                            cards.forEach(c => {
                                const titleEl = c.querySelector('.qBF1Pd');
                                const name = titleEl ? titleEl.innerText.trim() : null;
                                const linkTag = c.querySelector('a.hfpxzc');
                                const link = linkTag ? linkTag.href : null;
                                if (!name || !link) return;

                                const lines = c.innerText.split('\\n').map(l => l.trim()).filter(l => l.length > 0);
                                let rating = null;
                                let category = null;
                                let address = null;
                                let schedule = null;
                                let phone = null;

                                lines.forEach(line => {
                                    if (/^\\d\\.\\d$/.test(line)) rating = line;
                                    else if (line.includes('Open') || line.includes('Closed') || line.includes('hours')) schedule = line;
                                    else if (line.includes('·')) {
                                        const parts = line.split('·').map(p => p.trim());
                                        parts.forEach(p => {
                                            if (p.includes('Gym') || p.includes('Fitness') || p.includes('Center') || p.includes('Studio')) category = p;
                                            else if (/\\d/.test(p) && p.length > 5) address = p;
                                        });
                                    }
                                    else if (/\\+?\\d[\\d\\s\\-()]{7,}\\d/.test(line)) phone = line;
                                });

                                items.push({name, link, rating, category, address, schedule, phone});
                            });
                            return items;
                        """) or []

                        existing_links = {x.get("link") for x in self.scraped_items}
                        for card in feed_cards:
                            clink = card.get("link")
                            if clink and clink not in existing_links:
                                existing_links.add(clink)
                                item_data = {
                                    "name": clean_text(card.get("name")),
                                    "phone": clean_text(card.get("phone")),
                                    "address": clean_text(card.get("address")),
                                    "website": None,
                                    "schedule": clean_text(card.get("schedule")),
                                    "rating": clean_text(card.get("rating")),
                                    "category": clean_text(card.get("category")),
                                    "link": clink,
                                }
                                self.scraped_items.append(item_data)
                                if self.on_item_scraped:
                                    self.on_item_scraped(item_data)
                                if self.details_file and len(self.scraped_items) % 3 == 0:
                                    pd.DataFrame(self.scraped_items).to_csv(self.details_file, index=False, encoding="utf-8")
                    except Exception as e_fc:
                        pass
            except Exception as e:
                self.log(f"Notice during link extraction: {e}", "WARN")

            new_count = len(links)
            self.log(f"Places discovered so far: {new_count}", "INFO")
            if self.on_progress:
                self.on_progress({
                    "phase": "collecting_links",
                    "links_count": new_count,
                    "target_limit": self.max_results,
                })

            if self.max_results and new_count >= self.max_results:
                self.log(f"Reached requested limit of {self.max_results} places.", "INFO")
                break

            # Detect official Google Maps "You've reached the end of the list" indicator
            try:
                is_end_of_list = self.driver.execute_script("""
                    const endMarker = document.querySelector('.HlvSq') || document.querySelector('span[class*="HlvSq"]');
                    const text = document.body.innerText || '';
                    return (endMarker !== null) || text.includes("You've reached the end of the list") || text.includes("end of the list");
                """)
                if is_end_of_list:
                    self.log(f"Google Maps reached the end of its list for this search viewport ({new_count} places total).", "INFO")
                    break
            except Exception:
                pass

            if new_count == prev_count:
                stall_count += 1
                # Smart jiggle scroll to trigger lazy-load if Google Maps feed is sluggish
                if stall_count % 3 == 0:
                    try:
                        self.driver.execute_script("""
                            const feed = document.querySelector('div[role="feed"]') || document.querySelector('.m6QErb[role="feed"]');
                            if (feed) {
                                feed.scrollTop -= 400;
                            }
                        """)
                        time.sleep(0.5)
                    except Exception:
                        pass

                if stall_count >= self.max_scroll_stalls:
                    self.log(f"Feed stopped yielding new places after {self.max_scroll_stalls} scrolls. Google Maps feed limit reached ({new_count} places).", "INFO")
                    break
            else:
                stall_count = 0

            # Dynamic feed scroll
            try:
                scrolled = self.driver.execute_script("""
                    const feed = document.querySelector('div[role="feed"]') || document.querySelector('.m6QErb[role="feed"]');
                    if (feed) {
                        feed.scrollTop = feed.scrollHeight;
                        return true;
                    }
                    window.scrollBy(0, 1000);
                    return false;
                """)
            except Exception as e:
                self.log(f"Feed scroll notice: {e}", "WARN")

            # Sleep in slices
            slice_time = 0.2
            elapsed = 0.0
            while elapsed < self.scroll_pause and not self.is_stopped:
                time.sleep(min(slice_time, self.scroll_pause - elapsed))
                elapsed += slice_time

        sorted_links = sorted(list(links))
        if self.max_results and len(sorted_links) > self.max_results:
            sorted_links = sorted_links[:self.max_results]

        self.log(f"Phase 1 Complete — Found {len(sorted_links)} place links.", "SUCCESS")
        return sorted_links

    def _extract_details(self, link: str) -> Dict[str, Any]:
        """Phase 2: Extract business info with timeout protection."""
        target_url = link + ("&hl=en" if "?" in link else "?hl=en")
        try:
            self.driver.get(target_url)
        except TimeoutException:
            # Eager strategy might timeout on heavy assets, but DOM is often loaded
            pass

        try:
            WebDriverWait(self.driver, self.page_load_timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "h1"))
            )
        except TimeoutException:
            self.log(f"Timeout waiting for place card: {link[:65]}...", "WARN")
            return {}

        # High-Speed in-browser JS extraction first (1 millisecond, avoids 10MB BeautifulSoup DOM parsing)
        try:
            fast_data = self.driver.execute_script("""
                const h1 = document.querySelector('h1');
                const name = h1 ? h1.innerText.trim() : null;
                const addrBtn = document.querySelector('button[data-item-id="address"]');
                const address = addrBtn ? addrBtn.innerText.trim() : null;
                const webBtn = document.querySelector('a[data-item-id="authority"]');
                const website = webBtn ? webBtn.href : null;
                let phone = null;
                const phoneBtn = document.querySelector('button[data-item-id^="phone:tel:" i], a[href^="tel:"]');
                if (phoneBtn) {
                    const raw = phoneBtn.innerText.trim() || (phoneBtn.getAttribute('href') || '').replace('tel:', '');
                    const digits = raw.replace(/\\D/g, '');
                    if (digits.length >= 7 && !raw.toLowerCase().includes('send')) {
                        phone = raw;
                    }
                }
                if (!phone) {
                    document.querySelectorAll('button[data-item-id*="phone:" i], button[aria-label*="Phone" i]').forEach(b => {
                        if (phone) return;
                        const aria = b.getAttribute('aria-label') || '';
                        const text = b.innerText.trim();
                        if (aria.toLowerCase().includes('send') || text.toLowerCase().includes('send')) return;
                        const textDigits = text.replace(/\\D/g, '');
                        if (textDigits.length >= 7) {
                            phone = text;
                        } else {
                            const ariaDigits = aria.replace(/\\D/g, '');
                            if (ariaDigits.length >= 7) {
                                phone = aria.replace(/^Phone(\\s*number)?:\\s*/i, '').trim();
                            }
                        }
                    });
                }
                let schedule = null;
                const hoursEl = document.querySelector('div[aria-label*="opening hours" i], div[aria-label*="open hours" i]');
                if (hoursEl) {
                    const lbl = hoursEl.getAttribute('aria-label') || '';
                    const p = lbl.split('.');
                    if (p.length > 0) schedule = p[0].replace(/,/g, ' -> ');
                }
                return {name, address, website, phone, schedule};
            """)
            if fast_data and fast_data.get("name"):
                raw_ph = clean_text(fast_data.get("phone"))
                val_ph = raw_ph if is_valid_phone(raw_ph) else None
                return {
                    "name": clean_text(fast_data.get("name")),
                    "address": clean_text(fast_data.get("address")),
                    "phone": val_ph,
                    "website": fast_data.get("website").strip() if fast_data.get("website") else None,
                    "schedule": clean_text(fast_data.get("schedule")),
                    "link": link,
                }
        except Exception:
            pass

        # Fallback to BeautifulSoup if JS evaluation was incomplete
        soup = BeautifulSoup(self.driver.page_source, "html.parser")
        data = {
            "name": None,
            "address": None,
            "website": None,
            "phone": None,
            "schedule": None,
            "link": link,
        }

        # Business Name
        h1 = soup.find("h1")
        if h1:
            data["name"] = h1.get_text(strip=True)

        # Info panel cards and buttons
        for div in soup.find_all("div", attrs={"aria-label": True}):
            label = div["aria-label"]

            if "Information for" in label:
                btn = div.find("button", attrs={"data-item-id": "address"})
                if btn:
                    data["address"] = btn.get_text(strip=True)

                a_tag = div.find("a", attrs={"data-item-id": "authority"})
                if a_tag and a_tag.get("href"):
                    data["website"] = a_tag["href"]

                for button in div.find_all("button", attrs={"aria-label": True}):
                    aria = button.get("aria-label", "")
                    if "phone" in aria.lower() and "send" not in aria.lower():
                        cand = clean_text(button.get_text(strip=True))
                        if is_valid_phone(cand):
                            data["phone"] = cand
                            break

            elif "opening hours" in label.lower() or "open hours" in label.lower():
                parts = label.split(".")
                if parts and len(parts[0]) > 0:
                    data["schedule"] = parts[0].replace(",", " -> ")

        # Secondary fallback lookups
        if not data["address"]:
            addr_btn = soup.find("button", attrs={"data-item-id": "address"})
            if addr_btn:
                data["address"] = addr_btn.get_text(strip=True)

        if not data["website"]:
            web_a = soup.find("a", attrs={"data-item-id": "authority"})
            if web_a and web_a.get("href"):
                data["website"] = web_a["href"]

        if not data["phone"]:
            phone_btn = soup.find("button", attrs={"data-item-id": lambda x: x and "phone:tel:" in x.lower()})
            if phone_btn:
                cand = clean_text(phone_btn.get_text(strip=True))
                if is_valid_phone(cand):
                    data["phone"] = cand

        # Sanitize text
        data["name"] = clean_text(data["name"])
        data["address"] = clean_text(data["address"])
        clean_p = clean_text(data["phone"])
        data["phone"] = clean_p if is_valid_phone(clean_p) else None
        data["schedule"] = clean_text(data["schedule"])
        data["website"] = data["website"].strip() if data["website"] else None

        return data

    def _extract_details_http(self, link: str) -> Dict[str, Any]:
        """Direct HTTP / Browserless extraction: Uses zero Chrome memory (~30MB RAM) via lightweight network requests."""
        import urllib.request
        import urllib.parse
        import re

        target_url = link + ("&hl=en" if "?" in link else "?hl=en")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        data = {
            "name": None,
            "address": None,
            "website": None,
            "phone": None,
            "schedule": None,
            "link": link,
        }

        try:
            req = urllib.request.Request(target_url, headers=headers)
            with urllib.request.urlopen(req, timeout=self.page_load_timeout) as resp:
                html = resp.read().decode("utf-8", errors="ignore")

            # Extract business name from URL path
            m_path_name = re.search(r'/maps/place/([^/]+)/', link)
            if m_path_name:
                raw_name = urllib.parse.unquote_plus(m_path_name.group(1))
                if not raw_name.startswith("@") and not raw_name.startswith("data="):
                    data["name"] = clean_text(raw_name)

            if not data["name"]:
                m_og_title = re.search(r'<meta property="og:title" content="([^"]+)"', html)
                if m_og_title:
                    raw_title = re.sub(r'\s*·\s*Google Maps.*$', '', m_og_title.group(1))
                    if raw_title and "Google Maps" not in raw_title:
                        data["name"] = clean_text(raw_title)

            # Extract address/category from og:description
            m_desc = re.search(r'<meta property="og:description" content="([^"]+)"', html)
            if m_desc:
                desc = m_desc.group(1)
                parts = [p.strip() for p in desc.split('·')]
                for p in parts:
                    if any(c in p for c in ['St', 'Road', 'Ave', 'Nagar', 'Chennai', 'Blvd', 'Floor', 'Dr', 'Lane', 'Colony', 'Main']):
                        data["address"] = clean_text(p)
                        break

            # Extract phone and website from APP_INITIALIZATION_STATE
            m_blob = re.search(r'window\.APP_INITIALIZATION_STATE\s*=\s*(\[.*?\]);', html, re.DOTALL)
            if m_blob:
                blob = m_blob.group(1)
                phones = re.findall(r'"(\+?\d{1,3}[\s-]?\(?\d{2,5}\)?[\s-]?\d{3,5}[\s-]?\d{3,5})"', blob)
                valid_phones = [p for p in phones if len(re.sub(r'\D', '', p)) >= 10 and not p.startswith(('2025', '2026'))]
                if valid_phones:
                    data["phone"] = clean_text(valid_phones[0])

                urls = re.findall(r'"(https?://[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}[^"]*)"', blob)
                biz_urls = [u for u in urls if not any(x in u for x in ['google.com', 'gstatic.com', 'schema.org', 'ggpht.com', 'w3.org'])]
                if biz_urls:
                    data["website"] = biz_urls[0]

        except Exception as e:
            self.log(f"HTTP extraction notice for {link[:50]}: {e}", "WARN")

        return data

    def run(self) -> Dict[str, Any]:
        """Execute the complete scraping pipeline with auto-recovery."""
        self.is_running = True
        self._stop_requested.clear()
        self.scraped_items = []
        self.links = []

        start_time = time.time()
        self.log("Verifying internet connectivity...", "INFO")
        if not check_internet():
            err_msg = "No internet connection detected. Please check network and retry."
            self.log(err_msg, "ERROR")
            if self.on_error:
                self.on_error(err_msg)
            self.is_running = False
            return {"status": "error", "error": err_msg}

        # Resolve Search Target
        search_url = None
        if self.from_links:
            if not os.path.isfile(self.from_links):
                err_msg = f"Input links file not found: {self.from_links}"
                self.log(err_msg, "ERROR")
                if self.on_error:
                    self.on_error(err_msg)
                self.is_running = False
                return {"status": "error", "error": err_msg}
        elif self.queries:
            search_url = None
        elif self.url:
            search_url = self.url.strip()
        elif self.query:
            clean_q = self.query.strip().replace(" ", "+")
            search_url = f"https://www.google.com/maps/search/{clean_q}/"
        else:
            err_msg = "No query, URL, or links CSV provided."
            self.log(err_msg, "ERROR")
            if self.on_error:
                self.on_error(err_msg)
            self.is_running = False
            return {"status": "error", "error": err_msg}

        clean_name = "".join(c for c in self.output_name if c.isalnum() or c in ("_", "-")).strip() or "output"
        self.links_file = os.path.join(self.output_dir, f"{clean_name}_links.csv")
        self.details_file = os.path.join(self.output_dir, f"{clean_name}_details.csv")

        try:
            self.driver = self._create_driver()

            # Phase 1: Collect Links
            if self.from_links:
                self.log(f"Reading links directly from {self.from_links}...", "INFO")
                if self.on_phase:
                    self.on_phase("Phase 1: Loading links file")
                with open(self.from_links, "r", encoding="utf-8") as f:
                    reader = csv.reader(f)
                    for row in reader:
                        if row and "/maps/place/" in row[0]:
                            self.links.append(row[0])
                if self.max_results:
                    self.links = self.links[:self.max_results]
            elif self.queries:
                valid_queries = [q.strip() for q in self.queries if q.strip() and not q.strip().startswith(("#", "//", "---", "==="))]
                self.log(f"Phase 1 — Starting Multi-Location Batch Link Discovery for {len(valid_queries)} search areas...", "INFO")
                all_found_links = set()
                for q_idx, q_str in enumerate(valid_queries, 1):
                    if self.is_stopped:
                        break
                    if not self._wait_for_internet():
                        break
                    self.log(f"[{q_idx}/{len(valid_queries)}] Scraping zone: '{q_str}'...", "INFO")
                    clean_q = q_str.strip().replace(" ", "+")
                    q_url = f"https://www.google.com/maps/search/{clean_q}/"
                    sub_links = self._collect_links(q_url)
                    for lk in sub_links:
                        all_found_links.add(lk)
                    self.log(f"Zone '{q_str}' added {len(sub_links)} links. Master deduplicated total: {len(all_found_links)} places.", "SUCCESS")
                    if self.max_results and len(all_found_links) >= self.max_results:
                        self.log(f"Reached overall cap of {self.max_results} places across all queries.", "INFO")
                        break
                self.links = sorted(list(all_found_links))
                if self.links:
                    pd.DataFrame({"link": self.links}).to_csv(self.links_file, index=False)
                    self.log(f"Saved {len(self.links)} unique master links to {self.links_file}", "SUCCESS")
            else:
                self.links = self._collect_links(search_url)
                if self.links:
                    pd.DataFrame({"link": self.links}).to_csv(self.links_file, index=False)
                    self.log(f"Saved {len(self.links)} links to {self.links_file}", "SUCCESS")

            if not self.links:
                self.log("No place links discovered for this query. Completed.", "WARN")
                if self.on_complete:
                    self.on_complete({
                        "status": "completed",
                        "total_links": 0,
                        "total_scraped": 0,
                        "links_file": self.links_file,
                        "details_file": None,
                        "duration": round(time.time() - start_time, 1),
                    })
                return {"status": "completed", "total_links": 0, "total_scraped": 0}

            # Phase 2: Details Extraction
            if self.engine_mode == "feed_fast":
                self.log(f"Feed-Fast mode complete! Extracted {len(self.scraped_items)} places directly from search list cards without opening separate URLs.", "SUCCESS")
                if self.scraped_items:
                    pd.DataFrame(self.scraped_items).to_csv(self.details_file, index=False, encoding="utf-8")
                duration = round(time.time() - start_time, 1)
                summary = {
                    "status": "completed",
                    "total_links": len(self.links),
                    "total_scraped": len(self.scraped_items),
                    "links_file": self.links_file,
                    "details_file": self.details_file if self.scraped_items else None,
                    "duration": duration,
                }
                if self.on_complete:
                    self.on_complete(summary)
                return summary

            if self.links_only:
                self.log("Links-only mode selected. Skipping Phase 2 business detail extraction.", "INFO")
                if self.on_complete:
                    self.on_complete({
                        "status": "completed",
                        "total_links": len(self.links),
                        "total_scraped": 0,
                        "links_file": self.links_file,
                        "details_file": None,
                        "duration": round(time.time() - start_time, 1),
                    })
                return {"status": "completed", "total_links": len(self.links), "total_scraped": 0}

            # Auto-Resume from existing details file if present
            already_scraped_links = set()
            if os.path.exists(self.details_file):
                try:
                    prev_df = pd.read_csv(self.details_file)
                    if "link" in prev_df.columns:
                        for item_dict in prev_df.to_dict("records"):
                            if self.require_phone and not is_valid_phone(item_dict.get("phone")):
                                continue
                            self.scraped_items.append(item_dict)
                            already_scraped_links.add(str(item_dict.get("link")).strip())
                    if already_scraped_links:
                        self.log(f"Auto-Resume: Loaded {len(self.scraped_items)} verified leads from previous save! Skipping already-processed places.", "SUCCESS")
                except Exception as e:
                    self.log(f"Auto-resume notice: {e}", "INFO")

            total = len(self.links)
            mode_label = "Browserless Direct HTTP" if self.engine_mode == "browserless_http" else ("Turbo Headless (Images Blocked)" if self.engine_mode == "turbo" else "Standard Chrome")
            self.log(f"Phase 2 — Extracting business details for {total} places using [{mode_label}]...", "INFO")
            if self.require_phone:
                self.log("Phone Filter Active: Only businesses with verified phone numbers will be saved to your list.", "INFO")
            if self.on_phase:
                self.on_phase(f"Phase 2: Extracting Details ({mode_label})")

            session_count = 0
            for i, link in enumerate(self.links, 1):
                if self.is_stopped:
                    self.log("Scraping stopped by user before full completion.", "WARN")
                    break

                # Auto-skip places that were already saved
                if link.strip() in already_scraped_links:
                    continue

                # Auto-pause and reconnect if network disconnects
                if not self._wait_for_internet():
                    break

                # Memory refresh for Chrome drivers
                if self.engine_mode != "browserless_http":
                    session_count += 1
                    if session_count >= self.recycle_interval:
                        self.log(f"Refreshing Chrome session to clear RAM cache after {session_count} items...", "INFO")
                        self._recycle_driver()
                        session_count = 0

                self.log(f"[{i}/{total}] Scraping details...", "INFO")
                data = None
                try:
                    if self.engine_mode == "browserless_http":
                        data = self._extract_details_http(link)
                    else:
                        data = self._extract_details(link)
                except (WebDriverException, InvalidSessionIdException) as wde:
                    if self.engine_mode != "browserless_http":
                        self.log(f"Browser connection hiccup on item {i}: {wde}. Auto-resurrecting Chrome...", "WARN")
                        self._recycle_driver()
                        session_count = 0
                        try:
                            data = self._extract_details(link)
                        except Exception as e2:
                            self.log(f"Skip failed item {i}: {e2}", "WARN")
                    else:
                        self.log(f"HTTP skip item {i}: {wde}", "WARN")

                if data and data.get("name"):
                    has_phone = is_valid_phone(data.get("phone"))
                    if self.require_phone and not has_phone:
                        self.log(f"  ⏭ Skipped '{data['name']}' (No phone number listed)", "INFO")
                    else:
                        self.scraped_items.append(data)
                        already_scraped_links.add(link.strip())
                        self.log(f"  ✓ {data['name']} (Phone: {data.get('phone') or 'N/A'})", "SUCCESS")
                        if self.on_item_scraped:
                            self.on_item_scraped(data)

                        # Crash-safe instant CSV save after every verified record
                        df = pd.DataFrame(self.scraped_items)
                        df.to_csv(self.details_file, index=False, encoding="utf-8")
                else:
                    self.log("  ✗ Incomplete details or non-standard card", "WARN")

                if self.on_progress:
                    self.on_progress({
                        "phase": "extracting_details",
                        "current": i,
                        "total": total,
                        "scraped_count": len(self.scraped_items),
                        "percent": round((i / total) * 100, 1),
                    })

                self._random_delay()

            duration = round(time.time() - start_time, 1)
            status_text = "stopped" if self.is_stopped else "completed"
            self.log(f"Job {status_text}! Successfully extracted {len(self.scraped_items)} businesses in {duration}s.", "SUCCESS")

            summary = {
                "status": status_text,
                "total_links": len(self.links),
                "total_scraped": len(self.scraped_items),
                "links_file": self.links_file,
                "details_file": self.details_file if self.scraped_items else None,
                "duration": duration,
            }
            if self.on_complete:
                self.on_complete(summary)
            return summary

        except Exception as exc:
            err_str = f"Unexpected engine failure: {exc}"
            self.log(err_str, "ERROR")
            if self.on_error:
                self.on_error(err_str)
            return {"status": "error", "error": err_str}
        finally:
            self.is_running = False
            if self.driver:
                try:
                    self.driver.quit()
                except Exception:
                    pass
                self.driver = None
            self.log("Browser instance closed. Idle.", "INFO")
