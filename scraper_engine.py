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
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional, Dict, Any, List

import pandas as pd
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager


def check_internet(host="one.one.one.one", port=80, timeout=3) -> bool:
    """Check network connectivity."""
    try:
        addr = socket.gethostbyname(host)
        conn = socket.create_connection((addr, port), timeout)
        conn.close()
        return True
    except OSError:
        return False


def clean_text(val: Optional[str]) -> Optional[str]:
    """Clean text by stripping icon glyphs and whitespace."""
    if not val:
        return None
    cleaned = "".join(c for c in val if not (0xE000 <= ord(c) <= 0xF8FF)).strip()
    return cleaned if cleaned else None


def is_valid_phone(val: Optional[str]) -> bool:
    """Validate phone number string."""
    if not val:
        return False
    digits = re.sub(r"\D", "", str(val))
    if len(digits) < 7 or len(digits) > 15:
        return False
    cleaned_lower = str(val).lower()
    if any(w in cleaned_lower for w in ["send", "call", "mobile", "direction", "website", "share", "save", "nearby", "review"]):
        return False
    return True


def export_clean_details_csv(items: List[Dict[str, Any]], filepath: str):
    """
    Exports scraped items to a clean, RFC-4180 compliant CSV file.
    Guarantees:
    - Proper column order: Business Name, Phone Number, then Address, Category, Rating, Website, Hours, Google Maps Link.
    - Zero collapsed columns: eliminates internal newlines/tabs and quotes every cell with csv.QUOTE_ALL.
    - Full Microsoft Excel and Windows viewer compatibility via UTF-8 BOM (utf-8-sig).
    """
    if not filepath or not items:
        return

    headers = [
        "Business Name",
        "Phone Number",
        "Address",
        "Category",
        "Rating",
        "Website",
        "Opening Hours",
        "Google Maps Link"
    ]

    def sanitize_cell(v: Any) -> str:
        if v is None:
            return ""
        s = str(v)
        # Strip private Unicode icon glyphs (0xE000 - 0xF8FF)
        s = "".join(ch for ch in s if not (0xE000 <= ord(ch) <= 0xF8FF))
        # Replace newlines, carriage returns, tabs and multiple spaces with a single space to prevent row/column collapse
        s = re.sub(r"[\r\n\t]+", " ", s)
        s = re.sub(r"\s{2,}", " ", s)
        return s.strip()

    rows = []
    for item in items:
        name = sanitize_cell(item.get("name") or item.get("title") or "")
        phone = sanitize_cell(item.get("phone") or "")
        address = sanitize_cell(item.get("address") or "")
        category = sanitize_cell(item.get("category") or "")
        rating = sanitize_cell(item.get("rating") or "")
        website = sanitize_cell(item.get("website") or "")
        schedule = sanitize_cell(item.get("schedule") or item.get("opening_hours") or item.get("hours") or "")
        link = sanitize_cell(item.get("link") or item.get("url") or "")

        rows.append([name, phone, address, category, rating, website, schedule, link])

    try:
        os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f, quoting=csv.QUOTE_ALL)
            writer.writerow(headers)
            writer.writerows(rows)
    except Exception as e:
        logging.error(f"Error exporting CSV to {filepath}: {e}")


class ScraperEngine:
    """
    Ultra-Fast Google Maps Scraper Engine.
    Operates in a single, blazing-fast mode:
    - Real-time in-memory card extraction during search feed scrolling.
    - Zero slow individual page loads in Chrome.
    - Concurrent multi-threaded HTTP micro-enrichment for missing phone numbers.
    """

    def __init__(
        self,
        query: Optional[str] = None,
        queries: Optional[List[str]] = None,
        url: Optional[str] = None,
        from_links: Optional[str] = None,
        output_name: str = "leads",
        output_dir: str = "output",
        headless: bool = True,
        links_only: bool = False,
        max_results: Optional[int] = 50,
        require_phone: bool = False,
        phone_filter: str = "all",
        website_filter: str = "all",
        min_rating: float = 0.0,
        district_deep: bool = True,
        on_log: Optional[Callable[[str, str], None]] = None,
        on_phase: Optional[Callable[[str], None]] = None,
        on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_item_scraped: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_complete: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        **kwargs,
    ):
        self.query = query
        self.queries = [q.strip() for q in queries if q and q.strip()] if queries else None
        self.url = url
        self.from_links = from_links
        self.output_name = output_name or "leads"
        self.output_dir = output_dir or "output"
        self.headless = headless
        self.links_only = links_only
        self.max_results = max_results if (max_results and max_results > 0) else None
        self.require_phone = require_phone
        self.phone_filter = (phone_filter or ("with_phone" if require_phone else "all")).lower()
        self.website_filter = (website_filter or "all").lower()
        try:
            self.min_rating = float(min_rating or 0.0)
        except (ValueError, TypeError):
            self.min_rating = 0.0
        self.district_deep = district_deep

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
        self.existing_items: List[Dict[str, Any]] = kwargs.get("existing_items", []) or []
        self.already_scraped_links: set = set(kwargs.get("already_scraped_links", []) or [])

        os.makedirs(self.output_dir, exist_ok=True)

    def log(self, message: str, level: str = "INFO"):
        """Emit log event."""
        if self.on_log:
            try:
                self.on_log(message, level)
            except Exception:
                pass

    def stop(self):
        """Request graceful scraper termination."""
        self._stop_requested.set()
        self.is_running = False
        self.log("Stopping scraper...", "WARN")
        try:
            if self.driver:
                self.driver.quit()
        except Exception:
            pass

    @property
    def is_stopped(self) -> bool:
        return self._stop_requested.is_set()

    def _create_driver(self) -> webdriver.Chrome:
        """Create a stripped-down, ultra-lightweight Chrome driver."""
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

        # Block heavy images, WebGL 3D maps, and remote fonts for maximum speed
        prefs = {
            "profile.managed_default_content_settings.images": 2,
            "profile.default_content_setting_values.notifications": 2,
        }
        opts.add_experimental_option("prefs", prefs)
        opts.add_argument("--blink-settings=imagesEnabled=false")
        opts.add_argument("--disable-remote-fonts")

        opts.add_experimental_option(
            "excludeSwitches", ["enable-logging", "enable-automation"]
        )
        opts.add_experimental_option("useAutomationExtension", False)

        if self.headless:
            opts.add_argument("--headless=new")
            opts.add_argument("--window-size=1920,1080")
        else:
            opts.add_argument("--window-size=1280,800")

        # Check for system Chromium / Google Chrome binary (for Docker & Linux)
        system_chrome = os.environ.get("CHROME_BIN")
        if not system_chrome:
            for p in ["/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"]:
                if os.path.exists(p):
                    system_chrome = p
                    break
        if system_chrome:
            opts.binary_location = system_chrome

        # Check for system chromedriver binary (for Docker & Linux)
        system_driver = os.environ.get("CHROMEDRIVER_PATH")
        if not system_driver:
            for p in ["/usr/bin/chromedriver", "/usr/lib/chromium/chromedriver", "/usr/local/bin/chromedriver"]:
                if os.path.exists(p):
                    system_driver = p
                    break

        if system_driver:
            service = Service(executable_path=system_driver)
        else:
            try:
                service = Service(ChromeDriverManager().install())
            except Exception:
                service = Service()

        driver = webdriver.Chrome(service=service, options=opts)
        driver.set_page_load_timeout(20)
        return driver

    def _enrich_single_http(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch missing phone number or website via lightweight background HTTP request."""
        if not item or not item.get("link"):
            return item
        if item.get("phone") and item.get("website"):
            return item

        link = item["link"]
        target_url = link + ("&hl=en" if "?" in link else "?hl=en")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }

        try:
            req = urllib.request.Request(target_url, headers=headers)
            with urllib.request.urlopen(req, timeout=6) as resp:
                html = resp.read().decode("utf-8", errors="ignore")

            # 1. Search for telephone property in JSON-LD / schema or tel: href
            if not item.get("phone"):
                tel_links = re.findall(r'href=["\']tel:([^"\']+)["\']', html)
                for tl in tel_links:
                    cleaned_tl = clean_text(tl)
                    if is_valid_phone(cleaned_tl):
                        item["phone"] = cleaned_tl
                        break

            # 2. Search for schema telephone
            if not item.get("phone"):
                schema_phones = re.findall(r'["\']telephone["\']\s*:\s*["\']([^"\']+)["\']', html)
                for sp in schema_phones:
                    cleaned_sp = clean_text(sp)
                    if is_valid_phone(cleaned_sp):
                        item["phone"] = cleaned_sp
                        break

            # 3. Search for window.APP_INITIALIZATION_STATE blob
            if not item.get("phone"):
                m_blob = re.search(r'window\.APP_INITIALIZATION_STATE\s*=\s*(\[.*?\]);', html, re.DOTALL)
                if m_blob:
                    blob = m_blob.group(1)
                    phones = re.findall(r'"(\+?\d{1,3}[\s-]?(?:\(?\d{2,5}\)?[\s-]?)?\d{3,5}[\s-]?\d{3,5})"', blob)
                    valid_phones = [p for p in phones if len(re.sub(r'\D', '', p)) >= 10 and not p.startswith(('2024', '2025', '2026', '1920', '1080'))]
                    if valid_phones:
                        item["phone"] = clean_text(valid_phones[0])

            # 4. Search for general Indian phone numbers (10 digits starting with 6-9, or landline with 0)
            if not item.get("phone"):
                raw_matches = re.findall(r'(?:(?:\+91|0)[\s-]?)?[6-9]\d{4}[\s-]?\d{5}', html)
                valid_raw = [p for p in raw_matches if not any(x in p for x in ['2024', '2025', '2026', '1920', '1080', '0000'])]
                if valid_raw:
                    item["phone"] = clean_text(valid_raw[0])

            # Website extraction
            if not item.get("website"):
                urls = re.findall(r'"(https?://[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}[^"]*)"', html)
                biz_urls = [u for u in urls if not any(x in u for x in ['google.', 'gstatic.', 'schema.org', 'ggpht.', 'w3.org', 'facebook.com/tr', 'googletagmanager'])]
                if biz_urls:
                    item["website"] = biz_urls[0]
        except Exception:
            pass

        return item

    def _scrape_feed(self, search_url: str):
        """Scroll search results and extract all place cards in real-time."""
        self.log("Opening Google Maps search feed...", "INFO")
        feed_url = search_url + ("&hl=en" if "?" in search_url else "?hl=en")
        self.driver.get(feed_url)

        try:
            WebDriverWait(self.driver, 12).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'div[role="feed"], .m6QErb[role="feed"]'))
            )
        except TimeoutException:
            pass

        processed_links = set(x.get("link") for x in self.scraped_items if x.get("link"))
        if self.already_scraped_links:
            processed_links.update(self.already_scraped_links)
        stall_count = 0
        scroll_count = 0

        while not self.is_stopped:
            scroll_count += 1
            prev_total = len(self.scraped_items)

            # In-browser lightning extraction of all visible place cards
            try:
                cards = self.driver.execute_script("""
                    const items = [];
                    const cardEls = document.querySelectorAll('div[role="feed"] div.Nv2PK, .m6QErb div.Nv2PK');
                    cardEls.forEach(c => {
                        const titleEl = c.querySelector('.qBF1Pd, .fontHeadlineSmall, h1');
                        const name = titleEl ? titleEl.innerText.trim() : null;
                        const linkEl = c.querySelector('a.hfpxzc, a[href*="/maps/place/"]');
                        const link = linkEl ? linkEl.href : null;
                        if (!name || !link) return;

                        const ratingEl = c.querySelector('.MW4etd');
                        const rating = ratingEl ? ratingEl.innerText.trim() : null;
                        const revEl = c.querySelector('.UY7F9');
                        const reviews = revEl ? revEl.innerText.replace(/[()]/g, '').trim() : null;

                        const webBtn = c.querySelector('a[data-value="Website"], a[aria-label*="website" i], a.lcr4fd');
                        const website = webBtn ? webBtn.href : null;

                        const fullText = c.innerText || '';
                        const lines = fullText.split('\\n').map(l => l.trim()).filter(l => l.length > 0);

                        let category = null;
                        let address = null;
                        let schedule = null;
                        let phone = null;

                        const telLink = c.querySelector('a[href^="tel:"], button[data-item-id^="phone:"], button[aria-label*="phone" i]');
                        if (telLink) {
                            const raw = (telLink.getAttribute('href') || '').replace('tel:', '').trim() || telLink.innerText.trim() || (telLink.getAttribute('aria-label') || '').replace(/Phone:\\s*/i, '').trim();
                            if (raw && raw.replace(/\\D/g, '').length >= 7) phone = raw;
                        }

                        lines.forEach(line => {
                            if (!phone && /(\\+?\\d{1,4}[\\s\\-]?)?(\\(?\\d{2,5}\\)?[\\s\\-]?)?\\d{3,5}[\\s\\-]?\\d{3,5}/.test(line)) {
                                const digits = line.replace(/\\D/g, '');
                                if (digits.length >= 7 && digits.length <= 15 && !line.toLowerCase().includes('open') && !line.toLowerCase().includes('closed')) {
                                    phone = line;
                                }
                            }
                            if (line.includes('Open') || line.includes('Closed') || line.includes('hours')) {
                                schedule = line;
                            }
                            if (line.includes('·')) {
                                const parts = line.split('·').map(p => p.trim());
                                parts.forEach(p => {
                                    if (!category && !/\\d/.test(p) && p.length < 35 && !p.includes('Open') && !p.includes('Closed')) {
                                        category = p;
                                    } else if (!address && p.length > 3 && !p.includes('Open') && !p.includes('Closed') && !/^\\d\\.\\d$/.test(p)) {
                                        address = p;
                                    }
                                });
                            }
                        });

                        items.push({ name, link, rating, reviews, website, category, address, schedule, phone });
                    });
                    return items;
                """) or []

                for card in cards:
                    clink = card.get("link")
                    if not clink or clink in processed_links:
                        continue

                    processed_links.add(clink)
                    if clink not in self.links:
                        self.links.append(clink)

                    item_data = {
                        "name": clean_text(card.get("name")),
                        "phone": clean_text(card.get("phone")),
                        "address": clean_text(card.get("address")),
                        "website": card.get("website"),
                        "category": clean_text(card.get("category")),
                        "rating": clean_text(card.get("rating")),
                        "schedule": clean_text(card.get("schedule")),
                        "link": clink,
                    }

                    # Check phone and stream immediately to UI
                    has_phone = is_valid_phone(item_data["phone"])
                    if not has_phone:
                        item_data["_needs_enrich"] = True
                    self.scraped_items.append(item_data)
                    self.log(f"+ Found: {item_data['name']} (Phone: {item_data['phone'] or 'Pending scan'})", "SUCCESS")
                    if self.on_item_scraped:
                        self.on_item_scraped(item_data)

                    if self.details_file and len(self.scraped_items) % 3 == 0:
                        export_clean_details_csv(self.scraped_items, self.details_file)

                    if self.max_results and len(self.scraped_items) >= self.max_results:
                        break

            except Exception as e:
                self.log(f"Feed notice: {e}", "WARN")

            cur_count = len(self.scraped_items)
            self.log(f"Discovered {cur_count} places so far...", "INFO")
            if self.on_progress:
                self.on_progress({
                    "phase": "scraping",
                    "links_count": len(processed_links),
                    "scraped_count": cur_count,
                    "target_limit": self.max_results,
                })

            if self.max_results and cur_count >= self.max_results:
                self.log(f"Reached requested goal of {self.max_results} places!", "SUCCESS")
                break

            # Check official end of Google Maps feed
            try:
                is_end = self.driver.execute_script("""
                    const marker = document.querySelector('.HlvSq, span[class*="HlvSq"]');
                    const txt = document.body.innerText || '';
                    return (marker !== null) || txt.includes("You've reached the end of the list") || txt.includes("end of the list");
                """)
                if is_end:
                    self.log(f"Google Maps reached the end of the list ({cur_count} places found).", "INFO")
                    break
            except Exception:
                pass

            if cur_count == prev_total:
                stall_count += 1
                if stall_count % 3 == 0:
                    try:
                        self.driver.execute_script("""
                            const f = document.querySelector('div[role="feed"]') || document.querySelector('.m6QErb[role="feed"]');
                            if (f) f.scrollTop -= 350;
                        """)
                        time.sleep(0.4)
                    except Exception:
                        pass
                if stall_count >= 10:
                    self.log(f"Feed reached maximum available listings ({cur_count} places).", "INFO")
                    break
            else:
                stall_count = 0

            # Dynamic rapid scroll
            try:
                self.driver.execute_script("""
                    const f = document.querySelector('div[role="feed"]') || document.querySelector('.m6QErb[role="feed"]');
                    if (f) { f.scrollTop = f.scrollHeight; } else { window.scrollBy(0, 1000); }
                """)
            except Exception:
                pass

            time.sleep(0.7)

    def run(self) -> Dict[str, Any]:
        """Execute the Ultra-Fast Scraping Pipeline."""
        self.is_running = True
        self._stop_requested.clear()
        if self.existing_items:
            self.scraped_items = [dict(x) for x in self.existing_items]
            self.links = [x.get("link") for x in self.scraped_items if x.get("link")]
        else:
            self.scraped_items = []
            self.links = []

        start_time = time.time()
        self.log("Starting Ultra-Fast Scraping Engine...", "INFO")
        if self.on_phase:
            self.on_phase("Ultra-Fast Scraping")

        if not check_internet():
            err_msg = "No internet connection detected."
            self.log(err_msg, "ERROR")
            if self.on_error:
                self.on_error(err_msg)
            self.is_running = False
            return {"status": "error", "error": err_msg}

        # Resolve search targets
        clean_name = "".join(c for c in self.output_name if c.isalnum() or c in ("_", "-")).strip() or "leads"
        self.links_file = os.path.join(self.output_dir, f"{clean_name}_links.csv")
        self.details_file = os.path.join(self.output_dir, f"{clean_name}_details.csv")

        try:
            self.driver = self._create_driver()

            if self.from_links and os.path.isfile(self.from_links):
                self.log(f"Loading links from {self.from_links}...", "INFO")
                with open(self.from_links, "r", encoding="utf-8") as f:
                    for row in csv.reader(f):
                        if row and "/maps/place/" in row[0]:
                            self.links.append(row[0])
                if self.max_results:
                    self.links = self.links[:self.max_results]
                # Populate blank items for parallel HTTP micro-enrichment
                for lk in self.links:
                    self.scraped_items.append({"name": "Place", "link": lk, "phone": None, "_needs_enrich": True})

            elif self.queries:
                self.log(f"Starting batch search for {len(self.queries)} zones...", "INFO")
                for q_idx, q_str in enumerate(self.queries, 1):
                    if self.is_stopped:
                        break
                    self.log(f"[{q_idx}/{len(self.queries)}] Searching zone: '{q_str}'...", "INFO")
                    clean_q = q_str.strip().replace(" ", "+")
                    q_url = f"https://www.google.com/maps/search/{clean_q}/"
                    self._scrape_feed(q_url)
                    if self.max_results and len(self.scraped_items) >= self.max_results:
                        break

            elif self.url:
                self._scrape_feed(self.url.strip())

            elif self.query:
                sub_queries = [self.query]
                if self.district_deep:
                    try:
                        from district_expander import expand_district_query, parse_query_location
                        expanded = expand_district_query(self.query)
                        if len(expanded) > 1:
                            sub_queries = expanded
                            _, loc_name = parse_query_location(self.query)
                            self.log(f"District Deep Search activated! Covering all {len(sub_queries)} localities in {loc_name.title() or self.query}.", "INFO")
                    except Exception as e:
                        self.log(f"District expansion note: {e}", "WARN")

                if len(sub_queries) > 1:
                    for s_idx, sq in enumerate(sub_queries, 1):
                        if self.is_stopped:
                            break
                        self.log(f"[{s_idx}/{len(sub_queries)}] Scanning locality: '{sq}'...", "INFO")
                        if self.on_phase:
                            self.on_phase(f"Area {s_idx}/{len(sub_queries)}: {sq}")
                        clean_q = sq.strip().replace(" ", "+")
                        q_url = f"https://www.google.com/maps/search/{clean_q}/"
                        self._scrape_feed(q_url)
                        if self.max_results and len(self.scraped_items) >= self.max_results:
                            self.log(f"Reached limit of {self.max_results} places across district.", "SUCCESS")
                            break
                else:
                    clean_q = self.query.strip().replace(" ", "+")
                    q_url = f"https://www.google.com/maps/search/{clean_q}/"
                    self._scrape_feed(q_url)

            # Close browser immediately after feed scroll to free all RAM
            try:
                if self.driver:
                    self.driver.quit()
                    self.driver = None
            except Exception:
                pass

            # Fast Parallel Micro-Enrichment (Concurrent HTTP threads for any items missing phone)
            enrich_candidates = [i for i in self.scraped_items if i.get("_needs_enrich")]
            if enrich_candidates and not self.is_stopped:
                self.log(f"Running high-speed phone enrichment on {len(enrich_candidates)} places...", "INFO")
                with ThreadPoolExecutor(max_workers=10) as executor:
                    futures = {executor.submit(self._enrich_single_http, it): it for it in enrich_candidates}
                    for fut in as_completed(futures):
                        if self.is_stopped:
                            break
                        try:
                            updated = fut.result()
                            if updated:
                                if is_valid_phone(updated.get("phone")):
                                    self.log(f"  + Enriched phone: {updated['name']} -> {updated['phone']}", "SUCCESS")
                                    if self.on_item_scraped:
                                        self.on_item_scraped(updated)
                        except Exception:
                            pass

            # Finalize items and apply user filter criteria
            final_items = []
            for it in self.scraped_items:
                it.pop("_needs_enrich", None)
                if it.get("name"):
                    it["has_phone"] = "Yes" if is_valid_phone(it.get("phone")) else "No"
                    has_site = bool(it.get("website") and str(it.get("website")).strip() and str(it.get("website")).strip().lower() not in ["none", "null", ""])
                    it["has_website"] = "Yes" if has_site else "No"
                    final_items.append(it)

            # 1. Filter by Phone Number Requirement
            if self.phone_filter == "with_phone" or self.require_phone:
                final_items = [x for x in final_items if x.get("has_phone") == "Yes"]
            elif self.phone_filter == "without_phone":
                final_items = [x for x in final_items if x.get("has_phone") != "Yes"]

            # 2. Filter by Website Requirement
            if self.website_filter == "with_website":
                final_items = [x for x in final_items if x.get("has_website") == "Yes"]
            elif self.website_filter == "without_website":
                final_items = [x for x in final_items if x.get("has_website") != "Yes"]

            # 3. Filter by Minimum Google Rating
            if self.min_rating > 0:
                rated_items = []
                for x in final_items:
                    try:
                        r = float(str(x.get("rating") or "0").replace(",", ".").split()[0])
                        if r >= self.min_rating:
                            rated_items.append(x)
                    except Exception:
                        pass
                final_items = rated_items

            # 4. Limit to max_results if specified
            if self.max_results and self.max_results > 0:
                final_items = final_items[:self.max_results]

            self.scraped_items = final_items

            # Save clean outputs (always save if we found places)
            if self.links and self.links_file:
                with open(self.links_file, "w", newline="", encoding="utf-8-sig") as f:
                    w = csv.writer(f)
                    w.writerow(["link"])
                    for lk in self.links:
                        w.writerow([lk])
            if self.scraped_items and self.details_file:
                export_clean_details_csv(self.scraped_items, self.details_file)

            duration = round(time.time() - start_time, 1)
            self.log(f"Scraping Completed in {duration}s! Extracted {len(self.scraped_items)} leads.", "SUCCESS")

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

        except Exception as e:
            err_msg = f"Scraper error: {str(e)}"
            self.log(err_msg, "ERROR")
            if self.on_error:
                self.on_error(err_msg)
            return {"status": "error", "error": err_msg}

        finally:
            self.is_running = False
            try:
                if self.scraped_items and self.details_file:
                    export_clean_details_csv(self.scraped_items, self.details_file)
            except Exception:
                pass
            try:
                if self.driver:
                    self.driver.quit()
            except Exception:
                pass
