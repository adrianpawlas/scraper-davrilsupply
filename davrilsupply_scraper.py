import os
import time
import json
import base64
import hashlib
import requests
from datetime import datetime, timezone
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup
import torch
from transformers import AutoProcessor, AutoModel
from PIL import Image
import io
import logging
from fake_useragent import UserAgent
import cloudscraper
from urllib.parse import urljoin, urlparse
import re
from typing import Dict, List, Any, Optional

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class SupabaseREST:
    """
    Minimal Supabase PostgREST helper for upserting into 'products' table.
    Uses direct REST API calls to avoid Edge Function requirements.
    """

    def __init__(self, url: str, key: str):
        self.base_url = url.rstrip("/")
        self.key = key
        self.session = requests.Session()
        self.session.headers.update({
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        })

    def get_all_products(self, source: str) -> Dict[str, Dict[str, Any]]:
        """
        Fetch all products for a given source with full data.
        Returns a dict mapping product_id -> product_data for smart comparison.
        """
        try:
            endpoint = f"{self.base_url}/rest/v1/products"
            params = {
                "source": f"eq.{source}",
                "select": "id,product_url,title,price,image_url,category,gender,size,description,additional_images,second_hand,metadata,created_at,updated_at,last_seen_at,stale_count"
            }
            resp = self.session.get(endpoint, params=params, timeout=60)
            if resp.status_code != 200:
                logger.warning(f"Failed to fetch existing products: {resp.status_code} {resp.text}")
                return {}
            rows = resp.json()
            return {r["id"]: r for r in rows if r.get("id")}
        except Exception as e:
            logger.error(f"Failed to fetch existing products: {e}")
            return {}

    def get_existing_product_ids(self, source: str) -> set:
        """
        Fetch all product IDs for a given source. Used for smart sync.
        Returns a set of id strings.
        """
        try:
            endpoint = f"{self.base_url}/rest/v1/products"
            params = {"source": f"eq.{source}", "select": "id"}
            resp = self.session.get(endpoint, params=params, timeout=60)
            if resp.status_code != 200:
                logger.warning(f"Failed to fetch existing products: {resp.status_code} {resp.text}")
                return set()
            rows = resp.json()
            return {r["id"] for r in rows if r.get("id")}
        except Exception as e:
            logger.error(f"Failed to fetch existing product IDs: {e}")
            return set()

    def delete_products_by_ids(self, ids: List[str]) -> bool:
        """
        Delete products by id. PostgREST: DELETE where id=in.(id1,id2,...)
        """
        if not ids:
            return True
        try:
            endpoint = f"{self.base_url}/rest/v1/products"
            # PostgREST in() filter: id=in.(id1,id2,...)
            chunk_size = 100
            for i in range(0, len(ids), chunk_size):
                chunk = ids[i : i + chunk_size]
                in_filter = ",".join(f'"{x}"' for x in chunk)
                params = {"id": f"in.({in_filter})"}
                resp = self.session.delete(endpoint, params=params, timeout=60)
                if resp.status_code not in (200, 204):
                    logger.error(f"Failed to delete products: {resp.status_code} {resp.text}")
                    return False
            return True
        except Exception as e:
            logger.error(f"Failed to delete products: {e}")
            return False

    def batch_upsert_products(self, products: List[Dict[str, Any]], max_retries: int = 3) -> tuple[int, int, List[Dict[str, Any]]]:
        """
        Batch upsert products with retry logic.
        Returns (success_count, failed_count, failed_products).
        """
        if not products:
            return 0, 0, []

        formatted_products = []
        seen_ids = set()

        for product in products:
            formatted_product = self._format_product_for_db(product)
            if formatted_product:
                product_id = formatted_product.get('id')
                if product_id and product_id not in seen_ids:
                    seen_ids.add(product_id)
                    formatted_products.append(formatted_product)

        if not formatted_products:
            logger.warning("No valid products to upsert after formatting")
            return 0, 0, []

        all_keys = set()
        for p in formatted_products:
            all_keys.update(p.keys())

        normalized_products = []
        for p in formatted_products:
            normalized = {key: p.get(key) for key in all_keys}
            normalized_products.append(normalized)

        endpoint = f"{self.base_url}/rest/v1/products"
        headers = {
            "Prefer": "resolution=merge-duplicates,return=minimal",
        }

        chunk_size = 50
        success_count = 0
        failed_products = []

        for i in range(0, len(normalized_products), chunk_size):
            chunk = normalized_products[i:i + chunk_size]
            
            for retry in range(max_retries):
                try:
                    resp = self.session.post(
                        endpoint,
                        headers=headers,
                        data=json.dumps(chunk),
                        timeout=60
                    )
                    if resp.status_code in (200, 201, 204):
                        success_count += len(chunk)
                        logger.debug(f"Successfully upserted batch of {len(chunk)} products")
                        break
                    else:
                        if retry == max_retries - 1:
                            logger.error(f"Failed to upsert batch after {max_retries} retries: {resp.status_code} {resp.text}")
                            failed_products.extend(chunk)
                        else:
                            logger.warning(f"Retry {retry + 1}/{max_retries} for batch: {resp.status_code}")
                            time.sleep(1)
                except Exception as batch_error:
                    if retry == max_retries - 1:
                        logger.error(f"Failed to upsert batch after {max_retries} retries: {batch_error}")
                        failed_products.extend(chunk)
                    else:
                        logger.warning(f"Retry {retry + 1}/{max_retries} for batch: {batch_error}")
                        time.sleep(1)

        return success_count, len(failed_products), failed_products

    def upsert_products(self, products: List[Dict[str, Any]]) -> bool:
        """
        Upsert products into the database.
        Args:
            products: List of product dictionaries
        Returns:
            True if successful, False otherwise
        """
        success_count, _, _ = self.batch_upsert_products(products)
        return success_count > 0

    def _format_product_for_db(self, product: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Format a product dictionary for database insertion.
        Schema: source, brand, image_embedding, image_url, additional_images,
        product_url, title, gender, price (e.g. "20.90EUR,22.57USD"), second_hand,
        metadata, created_at, info_embedding, category, last_seen_at, stale_count.
        """
        try:
            source = product.get('source', 'scraper')
            product_url = product.get('product_url')
            image_url = product.get('image_url')
            title = product.get('title')

            if not source or not product_url or not image_url or not title:
                logger.warning(f"Missing required fields: {product}")
                return None

            id_string = f"{source}:{product_url}"
            product_id = hashlib.sha256(id_string.encode('utf-8')).hexdigest()

            formatted = {
                'id': product_id,
                'source': source,
                'brand': product.get('brand', 'Davril Supply'),
                'image_url': image_url,
                'product_url': product_url,
                'title': title,
                'gender': product.get('gender', 'man'),
                'price': product.get('price'),
                'second_hand': product.get('second_hand', False),
                'last_seen_at': product.get('last_seen_at'),
                'stale_count': product.get('stale_count', 0),
            }

            # Optional fields aligned with products table
            for field in [
                'affiliate_url', 'description', 'category', 'size',
                'image_embedding', 'info_embedding', 'additional_images',
            ]:
                if field in product and product[field] is not None:
                    formatted[field] = product[field]

            metadata = {}
            if product.get('metadata'):
                m = product['metadata']
                if isinstance(m, str):
                    try:
                        metadata = json.loads(m)
                    except Exception:
                        metadata = {'raw_metadata': m}
                elif isinstance(m, dict):
                    metadata = m
            if metadata:
                formatted['metadata'] = json.dumps(metadata)

            return formatted

        except Exception as e:
            logger.error(f"Failed to format product: {e}")
            return None


# Unique source identifier for this scraper - required for smart sync and to avoid conflicts with other scrapers
SOURCE_NAME = "scraper-davrilsupply"


class DavrilSupplyScraper:
    def __init__(self, supabase_url: str, supabase_key: str, source: str = SOURCE_NAME):
        self.supabase_url = supabase_url
        self.supabase_key = supabase_key
        self.source = source
        # Initialize Supabase REST client
        self.supabase = SupabaseREST(supabase_url, supabase_key)

        # Initialize the embedding model
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        logger.info(f"Using device: {self.device}")

        self.processor = AutoProcessor.from_pretrained("google/siglip-base-patch16-384")
        self.model = AutoModel.from_pretrained("google/siglip-base-patch16-384").to(self.device)
        self.model.eval()

        # Base URL
        self.base_url = "https://davrilsupply.com"

        # EUR to USD conversion rate (approximate, you might want to use a real API)
        self.eur_to_usd_rate = 1.08  # Update this with current rate

        # Category URLs to scrape
        self.category_urls = [
            "https://davrilsupply.com/collections/denim-pants",
            "https://davrilsupply.com/collections/jackets",
            "https://davrilsupply.com/collections/t-shirts",
            "https://davrilsupply.com/collections/zips-hoodies",
            "https://davrilsupply.com/collections/knits-crewnecks",
            "https://davrilsupply.com/collections/tops-shirts",
            "https://davrilsupply.com/collections/jorts-shorts",
            "https://davrilsupply.com/collections/accessories"
        ]

        # Initialize cloudscraper for handling anti-bot measures
        self.scraper = cloudscraper.create_scraper()

    def setup_driver(self):
        """Setup Chrome WebDriver with appropriate options"""
        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)
        chrome_options.add_argument(f'--user-agent={UserAgent().random}')
        chrome_options.add_argument("--window-size=1920,1080")

        driver = webdriver.Chrome(
            service=ChromeService(ChromeDriverManager().install()),
            options=chrome_options
        )

        # Execute script to remove webdriver property
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        return driver

    def get_page_content(self, url: str) -> str:
        """Get page content using Selenium for dynamic content"""
        driver = None
        try:
            driver = self.setup_driver()
            driver.get(url)

            # Wait for products to load
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "body"))
            )

            # Scroll down to load all products
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)

            # Get the page source
            html_content = driver.page_source
            return html_content

        except Exception as e:
            logger.error(f"Error fetching {url} with Selenium: {e}")
            # Fallback to cloudscraper
            try:
                response = self.scraper.get(url)
                response.raise_for_status()
                return response.text
            except Exception as e2:
                logger.error(f"Error fetching {url} with cloudscraper: {e2}")
                return ""
        finally:
            if driver:
                driver.quit()

    def extract_products_from_page(self, html_content: str, category_url: str) -> list:
        """Extract product information from page HTML"""
        soup = BeautifulSoup(html_content, 'html.parser')
        products = []

        # Find all product links
        product_links = soup.find_all('a', href=re.compile(r'/products/'))
        logger.info(f"Found {len(product_links)} product links")

        # Extract products from links
        for link in product_links:
            try:
                product_data = self.extract_product_from_link(link, category_url, soup)
                if product_data:
                    products.append(product_data)
            except Exception as e:
                logger.error(f"Error extracting product from link: {e}")
                continue

        # Remove duplicates based on title
        unique_products = []
        seen_titles = set()
        for product in products:
            if product['title'] not in seen_titles:
                unique_products.append(product)
                seen_titles.add(product['title'])

        logger.info(f"Total unique products extracted: {len(unique_products)}")
        return unique_products

    # 1x1 transparent PNG used as lazy-load placeholder on many sites (incl. Shopify)
    _PLACEHOLDER_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVQYV2NgYAAAAAMAAWgmWQ0AAAAASUVORK5CYII="

    def _is_valid_image_url(self, url: Optional[str]) -> bool:
        """Return False for data: URLs, placeholders, or non-http(s)."""
        if not url or not isinstance(url, str):
            return False
        u = url.strip()
        if u.lower().startswith("data:"):
            return False
        if "data:image" in u.lower():
            return False
        if not (u.startswith("http://") or u.startswith("https://")):
            return False
        return True

    def _filter_bk_ft_images(self, urls: List[str]) -> List[str]:
        """Return only image URLs containing 'BK' or 'FT' in the path (case-sensitive)."""
        if not urls:
            return []
        return [u for u in urls if u and ("BK" in u or "FT" in u)]

    def _pick_image_for_embedding(self, urls: List[str]) -> Optional[str]:
        """
        Pick best image for embedding: prefer first URL containing BK or FT.
        Falls back to first valid URL if no BK/FT images exist.
        """
        if not urls:
            return None
        bk_ft = self._filter_bk_ft_images(urls)
        if bk_ft:
            return bk_ft[0]
        return urls[0] if urls else None

    def _parse_srcset_first_url(self, srcset: str) -> Optional[str]:
        """Parse srcset 'url1 1x, url2 2x' and return first URL."""
        if not srcset or not isinstance(srcset, str):
            return None
        part = srcset.strip().split(",")[0].strip().split()
        return part[0] if part else None

    def _get_best_image_src(self, img, base_url: str) -> Optional[str]:
        """
        Get best HTTP(S) image URL from an img element. Prefer lazy-load attrs
        (data-src, data-srcset, srcset) over src to avoid 1x1 placeholder.
        """
        if not img:
            return None
        candidates = []
        # Prefer lazy-load attributes over src
        for attr in ("data-src", "data-srcset", "srcset", "data-lazy-src", "data-original", "src"):
            val = img.get(attr)
            if not val:
                continue
            if attr in ("data-srcset", "srcset"):
                u = self._parse_srcset_first_url(val)
                if u:
                    candidates.append(u)
            else:
                candidates.append(val)
        for c in candidates:
            if self._is_valid_image_url(c):
                if not c.startswith("http"):
                    return urljoin(base_url, c)
                return c
        return None

    def _fetch_image_from_shopify_json(self, product_url: str) -> Optional[str]:
        """
        Fetch product image from Shopify JSON API (/products/{handle}.json).
        Returns product.image.src or product.images[0].src, or None.
        """
        info = self._fetch_product_json(product_url)
        return info[0] if info else None

    def _fetch_product_json(
        self, product_url: str
    ) -> Optional[tuple[Optional[str], List[str], Optional[str], Optional[Dict[str, Any]]]]:
        """
        Fetch product data from Shopify JSON API (/products/{handle}.json).
        Returns (main_image_url, list of additional image URLs, description, extra_metadata) or None.
        extra_metadata: {options, product_type, vendor, tags} for metadata field.
        """
        try:
            m = re.search(r"/products/([^/?#]+)", product_url)
            if not m:
                return None
            handle = m.group(1).strip()
            if not handle:
                return None
            json_url = f"{self.base_url.rstrip('/')}/products/{handle}.json"
            resp = self.scraper.get(json_url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            product = data.get("product")
            if not product:
                return None
            main_url: Optional[str] = None
            additional: List[str] = []
            img = product.get("image")
            if img and isinstance(img, dict) and img.get("src"):
                u = img["src"]
                if self._is_valid_image_url(u):
                    main_url = u
            images = product.get("images") or []
            if isinstance(images, list):
                for im in images:
                    if isinstance(im, dict) and im.get("src"):
                        u = im["src"]
                        if not self._is_valid_image_url(u):
                            continue
                        if main_url is None:
                            main_url = u
                        elif u != main_url and u not in additional:
                            additional.append(u)
                if main_url is None and images:
                    first = next(
                        (im["src"] for im in images if isinstance(im, dict) and im.get("src") and self._is_valid_image_url(im["src"])),
                        None
                    )
                    if first:
                        main_url = first
                        additional = [
                            im["src"] for im in images
                            if isinstance(im, dict) and im.get("src") and self._is_valid_image_url(im["src"]) and im["src"] != main_url
                        ]
            body = (product.get("body_html") or "").strip() or None
            extra = {
                "options": product.get("options"),
                "product_type": product.get("product_type"),
                "vendor": product.get("vendor"),
                "tags": product.get("tags"),
            }
            return (main_url, additional, body, extra)
        except Exception as e:
            logger.debug(f"Shopify JSON fetch failed for {product_url}: {e}")
            return None

    def extract_product_from_link(self, link, category_url: str, soup) -> dict:
        """Extract product data from a product link element"""
        try:
            # Get the product URL
            href = link.get('href')
            if not href.startswith('http'):
                product_url = urljoin(self.base_url, href)
            else:
                product_url = href

            # Get the title from the link text
            title = link.get_text().strip()
            if not title or len(title) < 3:
                return None

            # Clean up the title - remove price information and duplicates
            # Split by spaces and remove parts that look like prices or codes
            words = title.split()
            clean_words = []
            for word in words:
                # Skip if it looks like a price (contains digits and commas/periods)
                if re.match(r'.*\d.*[,|\.].*', word):
                    break
                # Skip strange characters
                if '�' in word or len(word) < 2:
                    continue
                clean_words.append(word)

            # Remove consecutive duplicates (like "T-SHIRT T-SHIRT")
            deduped_words = []
            for i, word in enumerate(clean_words):
                if i == 0 or word != clean_words[i-1]:
                    deduped_words.append(word)

            title = ' '.join(deduped_words)

            if not title or len(title) < 3:
                return None

            # Find the price - comprehensive search in multiple locations
            price_text = None
            container = link.parent

            # Method 1: Look in the entire container text for various price patterns
            container_text = container.get_text()

            # Look for European format prices like "1.113,00 €" or "1113,00€"
            price_patterns = [
                r'(\d{1,3}(?:\.\d{3})*,\d{2})\s*€?',  # 1.113,00 €
                r'€\s*(\d{1,3}(?:\.\d{3})*,\d{2})',    # € 1.113,00
                r'(\d{1,3}(?:\.\d{3})*,\d{2})',        # 1.113,00 (without €)
                r'(\d+(?:,\d{2})?)',                    # Simple format like 1113,00
            ]

            for pattern in price_patterns:
                match = re.search(pattern, container_text)
                if match:
                    price_str = match.group(1)
                    # Convert European format to standard decimal
                    if ',' in price_str and '.' in price_str:
                        # Handle thousands separator: 1.113,00 -> 1113.00
                        price_str = price_str.replace('.', '').replace(',', '.')
                    elif ',' in price_str:
                        # Handle decimal comma: 1113,00 -> 1113.00
                        price_str = price_str.replace(',', '.')
                    price_text = f"€{price_str}"
                    break

            # Method 2: If not found, look for price in data attributes
            if not price_text:
                price_attr = container.get('data-price') or link.get('data-price')
                if price_attr:
                    try:
                        # Convert to float and format as EUR
                        price_val = float(price_attr.replace(',', '.'))
                        price_text = f"€{price_val:.2f}"
                    except ValueError:
                        pass

            # Method 3: Look for price in child elements with price classes
            if not price_text:
                price_elements = container.find_all(['span', 'div'], class_=re.compile(r'.*price.*', re.IGNORECASE))
                for price_elem in price_elements:
                    elem_text = price_elem.get_text().strip()
                    match = re.search(r'(\d+(?:[.,]\d+)?)', elem_text)
                    if match:
                        price_str = match.group(1).replace(',', '.')
                        try:
                            price_val = float(price_str)
                            price_text = f"€{price_val:.2f}"
                            break
                        except ValueError:
                            continue

            # Method 4: Look in siblings as fallback
            if not price_text:
                for sibling in container.find_next_siblings()[:5]:  # Check more siblings
                    sibling_text = sibling.get_text()
                    for pattern in price_patterns:
                        match = re.search(pattern, sibling_text)
                        if match:
                            price_str = match.group(1)
                            if ',' in price_str and '.' in price_str:
                                price_str = price_str.replace('.', '').replace(',', '.')
                            elif ',' in price_str:
                                price_str = price_str.replace(',', '.')
                            price_text = f"€{price_str}"
                            break
                    if price_text:
                        break

            if not price_text:
                logger.warning(f"Could not find price for product: {title}")
                return None

            # Convert price to float
            price_clean = price_text.replace('€', '').replace(',', '.')
            try:
                eur_price = float(price_clean)
                price = self.convert_price_to_usd(eur_price)
            except ValueError:
                logger.warning(f"Could not parse price: {price_text}")
                return None

            # Find image - use _get_best_image_src to avoid data: placeholders; prefer data-src/srcset
            image_url = None
            container = link.parent

            # Method 1: img within product container
            img = container.find('img')
            if img:
                image_url = self._get_best_image_src(img, self.base_url)

            # Method 2: parent containers (up to 3 levels)
            if not image_url:
                parent = container.parent
                for _ in range(3):
                    if parent:
                        img = parent.find('img')
                        if img:
                            u = self._get_best_image_src(img, self.base_url)
                            if u and ('product' in u.lower() or 'image' in u.lower()):
                                image_url = u
                                break
                    parent = parent.parent if parent else None

            # Method 3: match by alt text
            if not image_url:
                title_lower = title.lower()
                for img in soup.find_all('img'):
                    alt_text = (img.get('alt') or '').lower()
                    if any(w in alt_text for w in title_lower.split() if len(w) > 2):
                        u = self._get_best_image_src(img, self.base_url)
                        if u and ('product' in u.lower() or 'http' in u):
                            image_url = u
                            break

            # Method 3b: Shopify product JSON API (reliable CDN image URLs)
            if not image_url and "/products/" in product_url:
                image_url = self._fetch_image_from_shopify_json(product_url)
                if image_url:
                    logger.debug(f"Got image from Shopify JSON: {image_url[:80]}...")

            # Method 4: visit product page for main image
            if not image_url:
                try:
                    logger.debug(f"Visiting product page for image: {product_url}")
                    product_html = self.get_page_content(product_url)
                    if product_html:
                        product_soup = BeautifulSoup(product_html, 'html.parser')
                        product_img = None
                        for sel in (
                            'img[data-image]', 'img.product-image', 'img.main-image',
                            '.product-image img', '.main-image img', '#product-image img',
                            '.gallery img', '.product-gallery img'
                        ):
                            product_img = product_soup.select_one(sel)
                            if product_img:
                                break
                        if not product_img:
                            for img in product_soup.find_all('img'):
                                u = self._get_best_image_src(img, self.base_url)
                                if u and ('product' in u.lower() or 'image' in u.lower()):
                                    product_img = img
                                    break
                        if product_img:
                            image_url = self._get_best_image_src(product_img, self.base_url)
                            if image_url:
                                logger.debug(f"Found product image: {image_url}")
                except Exception as e:
                    logger.debug(f"Could not extract image from product page: {e}")

            # Method 5: product card / row
            if not image_url:
                product_card = link
                for _ in range(5):
                    product_card = product_card.parent if product_card else None
                    if not product_card or not product_card.get('class'):
                        continue
                    classes = ' '.join(product_card.get('class', []))
                    if not any(k in classes.lower() for k in ('product', 'card', 'item', 'grid-item')):
                        continue
                    img = product_card.find('img')
                    if img:
                        image_url = self._get_best_image_src(img, self.base_url)
                    if image_url:
                        break

            # Enrich from Shopify JSON (additional images, description); prefer BK/FT images for embedding
            additional_images_list: List[str] = []
            description = None
            json_extra: Optional[Dict[str, Any]] = None
            if "/products/" in product_url:
                json_info = self._fetch_product_json(product_url)
                if json_info:
                    main_from_json, additional_from_json, desc_from_json = json_info[:3]
                    json_extra = json_info[3] if len(json_info) > 3 else None
                    all_json_images = ([main_from_json] if main_from_json else []) + (additional_from_json or [])
                    all_json_images = list(dict.fromkeys(u for u in all_json_images if u and self._is_valid_image_url(u)))
                    # Prefer BK/FT images for embedding and image_url (brand requirement)
                    chosen_main = self._pick_image_for_embedding(all_json_images)
                    if chosen_main:
                        image_url = chosen_main
                    elif main_from_json and not image_url:
                        image_url = main_from_json
                    bk_ft_images = self._filter_bk_ft_images(all_json_images)
                    additional_images_list = [u for u in bk_ft_images if u != image_url] if bk_ft_images else []
                    description = desc_from_json

            if not image_url or not self._is_valid_image_url(image_url):
                logger.warning(f"No valid image URL for product: {title}")
                return None

            # Extract sizes if available (look for S, M, L, XL in the container)
            sizes = []
            container_text = container.get_text()
            size_matches = re.findall(r'\b(S|M|L|XL)\b', container_text)
            sizes = list(set(size_matches))  # Remove duplicates

            # Extract category from URL and normalize: "Sweaters & Hoodies" -> "Sweaters, Hoodies"
            category = self.extract_category_from_url(category_url)
            if category:
                category = re.sub(r"\s+&\s+", ", ", category)
                category = re.sub(r"\s+and\s+", ", ", category, flags=re.IGNORECASE)

            # Price string: multiple currencies "20.90EUR,22.57USD"
            price_str = f"{eur_price:.2f}EUR,{price:.2f}USD"

            # additional_images format: "url1 , url2" (comma and space)
            additional_images_str = " , ".join(additional_images_list) if additional_images_list else None

            # Build comprehensive metadata with all product info
            metadata_obj: Dict[str, Any] = {
                "name": title,
                "price": price_str,
                "description": description,
                "colors": [],  # populated from options if available
                "size": ",".join(sizes) if sizes else None,
                "sizes": sizes,
                "category": category,
                "gender": "man",
                "brand": "Davril Supply",
                "product_url": product_url,
                "category_url": category_url,
                "original_price_text": price_text,
                "extraction_method": "link_based",
            }
            if json_extra:
                if json_extra.get("options"):
                    opts = json_extra["options"]
                    for o in opts if isinstance(opts, list) else []:
                        if isinstance(o, dict):
                            opt_name = (o.get("name") or "").lower()
                            opt_vals = o.get("values") or []
                            if "color" in opt_name or "colour" in opt_name:
                                metadata_obj["colors"] = opt_vals
                            elif "size" in opt_name and not metadata_obj.get("sizes"):
                                metadata_obj["sizes"] = opt_vals
                                metadata_obj["size"] = ",".join(opt_vals) if opt_vals else metadata_obj.get("size")
                if json_extra.get("product_type"):
                    metadata_obj["product_type"] = json_extra["product_type"]
                if json_extra.get("vendor"):
                    metadata_obj["vendor"] = json_extra["vendor"]
                if json_extra.get("tags"):
                    metadata_obj["tags"] = json_extra["tags"]

            product_data = {
                'source': self.source,
                'brand': 'Davril Supply',
                'title': title,
                'price': price_str,
                'image_url': image_url,
                'product_url': product_url,
                'category': category,
                'gender': 'man',
                'second_hand': False,
                'size': ','.join(sizes) if sizes else None,
                'additional_images': additional_images_str,
                'description': description,
                'metadata': json.dumps(metadata_obj)
            }

            return product_data

        except Exception as e:
            logger.error(f"Error extracting product from link: {e}")
            return None

    def create_product_from_title_price(self, title: str, price_text: str, category_url: str) -> dict:
        """Create product data from title and price text"""
        try:
            # Clean title
            title = title.strip()

            # Convert price to float
            price_clean = price_text.replace('€', '').replace(',', '.')
            try:
                eur_price = float(price_clean)
                price = self.convert_price_to_usd(eur_price)
            except ValueError:
                logger.warning(f"Could not parse price: {price_text}")
                return None

            category = self.extract_category_from_url(category_url)
            if category:
                category = re.sub(r"\s+&\s+", ", ", category)
                category = re.sub(r"\s+and\s+", ", ", category, flags=re.IGNORECASE)
            price_str = f"{eur_price:.2f}EUR,{price:.2f}USD"

            metadata_obj = {
                "name": title,
                "price": price_str,
                "description": None,
                "colors": [],
                "size": None,
                "sizes": [],
                "category": category,
                "gender": "man",
                "brand": "Davril Supply",
                "category_url": category_url,
                "original_price_text": price_text,
                "extraction_method": "text_analysis",
            }
            product_data = {
                'source': self.source,
                'brand': 'Davril Supply',
                'title': title,
                'price': price_str,
                'category': category,
                'gender': 'man',
                'second_hand': False,
                'metadata': json.dumps(metadata_obj)
            }
            return product_data

        except Exception as e:
            logger.error(f"Error creating product from title/price: {e}")
            return None

    def find_closest_image(self, image_list: list, product_title: str, soup) -> str:
        """Find the most relevant image for a product"""
        try:
            # Simple approach: return the first image that hasn't been used yet
            # In a more sophisticated implementation, we could use image alt text matching
            for img_url, alt_text, img_elem in image_list:
                # Check if alt text contains words from the product title
                title_words = set(product_title.lower().split())
                alt_words = set(alt_text.lower().split())

                if title_words.intersection(alt_words):
                    return img_url

            # If no matching alt text, return the first available image
            if image_list:
                return image_list[0][0]

        except Exception as e:
            logger.error(f"Error finding closest image: {e}")

        return None

    def extract_single_product(self, container, category_url: str) -> dict:
        """Extract data from a single product container"""
        try:
            # Extract title
            title_elem = container.find('h3') or container.find('h4') or \
                        container.find('a', class_=re.compile(r'.*title.*', re.IGNORECASE)) or \
                        container.find(string=re.compile(r'.*\w+.*'))  # Look for text that looks like a title

            if not title_elem:
                # Try to find title in the container text
                container_text = container.get_text()
                # Look for patterns that might be titles (usually all caps or specific format)
                lines = [line.strip() for line in container_text.split('\n') if line.strip()]
                title = None
                for line in lines:
                    if len(line) > 10 and not any(char.isdigit() for char in line) and '€' not in line:
                        title = line
                        break
            else:
                title = title_elem.get_text().strip() if hasattr(title_elem, 'get_text') else str(title_elem).strip()

            if not title:
                return None

            # Extract price
            price_text = None
            price_elem = container.find(string=re.compile(r'€\d+'))
            if price_elem:
                price_text = price_elem.strip()
            else:
                # Look for price in container text
                container_text = container.get_text()
                price_match = re.search(r'€(\d+,\d+)', container_text)
                if price_match:
                    price_text = f"€{price_match.group(1)}"

            if not price_text:
                return None

            # Convert price to float (remove € and convert , to .)
            price_clean = price_text.replace('€', '').replace(',', '.')
            try:
                eur_price = float(price_clean)
                price = self.convert_price_to_usd(eur_price)
            except ValueError:
                logger.warning(f"Could not parse price: {price_text}")
                return None

            # Extract image URL — use helper to avoid data: placeholders
            img_elem = container.find('img')
            image_url = self._get_best_image_src(img_elem, self.base_url) if img_elem else None
            if not image_url or not self._is_valid_image_url(image_url):
                return None

            # Extract product URL
            product_url = None
            link_elem = container.find('a', href=True)
            if link_elem:
                href = link_elem['href']
                if not href.startswith('http'):
                    product_url = urljoin(self.base_url, href)
                else:
                    product_url = href

            # Extract sizes if available
            sizes = []
            size_buttons = container.find_all('button', string=re.compile(r'S|M|L|XL'))
            for button in size_buttons:
                sizes.append(button.get_text().strip())

            category = self.extract_category_from_url(category_url)
            if category:
                category = re.sub(r"\s+&\s+", ", ", category)
                category = re.sub(r"\s+and\s+", ", ", category, flags=re.IGNORECASE)
            price_str = f"{eur_price:.2f}EUR,{price:.2f}USD"

            metadata_obj = {
                "name": title,
                "price": price_str,
                "description": None,
                "colors": [],
                "size": ",".join(sizes) if sizes else None,
                "sizes": sizes,
                "category": category,
                "gender": "man",
                "brand": "Davril Supply",
                "product_url": product_url,
                "category_url": category_url,
                "original_price_text": price_text,
                "extraction_method": "container",
            }
            product_data = {
                'source': self.source,
                'brand': 'Davril Supply',
                'title': title,
                'price': price_str,
                'image_url': image_url,
                'product_url': product_url,
                'category': category,
                'gender': 'man',
                'second_hand': False,
                'size': ','.join(sizes) if sizes else None,
                'metadata': json.dumps(metadata_obj)
            }
            return product_data

        except Exception as e:
            logger.error(f"Error extracting single product: {e}")
            return None

    def extract_category_from_url(self, url: str) -> str:
        """Extract category name from URL"""
        path = urlparse(url).path
        category = path.split('/')[-1].replace('-', ' ').title()
        return category

    def convert_price_to_usd(self, eur_price: float) -> float:
        """Convert EUR price to USD"""
        return round(eur_price * self.eur_to_usd_rate, 2)

    def generate_image_embedding(self, image_url: str) -> Optional[list]:
        """Generate 768-dim embedding from image URL (http(s) or data:)."""
        try:
            raw: bytes
            if image_url.strip().lower().startswith("data:"):
                # data:image/png;base64,... — decode and load; skip placeholders
                if self._PLACEHOLDER_B64 in image_url or "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB" in image_url:
                    logger.warning("Skipping embedding for 1x1 placeholder data URL")
                    return None
                idx = image_url.find("base64,")
                if idx == -1:
                    logger.warning("data: URL has no base64 payload")
                    return None
                raw = base64.b64decode(image_url[idx + 7 :].strip())
            else:
                if not self._is_valid_image_url(image_url):
                    logger.warning(f"Invalid image URL for embedding: {image_url[:80]}...")
                    return None
                response = requests.get(image_url, timeout=10)
                response.raise_for_status()
                raw = response.content

            image = Image.open(io.BytesIO(raw)).convert("RGB")
            # Use SigLIP image encoder only (768-dim)
            try:
                inputs = self.processor(images=image, return_tensors="pt").to(self.device)
            except Exception:
                inputs = self.processor(images=image, text=[""], return_tensors="pt").to(self.device)
            pixel_values = inputs["pixel_values"]
            with torch.no_grad():
                out = self.model.get_image_features(pixel_values=pixel_values)
            # Handle both tensor and BaseModelOutputWithPooling (transformers version variance)
            if hasattr(out, "pooler_output") and out.pooler_output is not None:
                feat = out.pooler_output
            elif hasattr(out, "last_hidden_state"):
                feat = out.last_hidden_state[:, 0, :]
            else:
                feat = out
            embeddings = feat.cpu().numpy().flatten().tolist()
            return embeddings
        except Exception as e:
            logger.error(f"Error generating embedding for {image_url[:80]}...: {e}")
            return None

    def generate_text_embedding(self, text: str) -> Optional[list]:
        """Generate 768-dim text embedding using SigLIP text encoder (same model as image)."""
        if not text or not text.strip():
            return None
        try:
            # SigLIP expects padding='max_length' for text
            inputs = self.processor(
                text=[text.strip()],
                padding="max_length",
                max_length=64,
                return_tensors="pt",
                truncation=True,
            ).to(self.device)
            kwargs = {"input_ids": inputs["input_ids"]}
            if "attention_mask" in inputs:
                kwargs["attention_mask"] = inputs["attention_mask"]
            with torch.no_grad():
                out = self.model.get_text_features(**kwargs)
            if hasattr(out, "pooler_output") and out.pooler_output is not None:
                feat = out.pooler_output
            elif hasattr(out, "last_hidden_state"):
                feat = out.last_hidden_state[:, 0, :]
            else:
                feat = out
            return feat.cpu().numpy().flatten().tolist()
        except Exception as e:
            logger.error(f"Error generating text embedding: {e}")
            return None

    def save_product_to_supabase(self, product_data: dict):
        """Save product data to Supabase - image_embedding and info_embedding required."""
        try:
            if not product_data.get('image_url'):
                logger.error(f"No image URL for product: {product_data['title']} - cannot generate embedding")
                return False

            # Image embedding (768-dim from google/siglip-base-patch16-384)
            logger.debug(f"Generating image embedding for: {product_data['title']}")
            image_emb = self.generate_image_embedding(product_data['image_url'])
            if not image_emb:
                logger.error(f"Could not generate image embedding for product: {product_data['title']}")
                return False
            product_data['image_embedding'] = image_emb

            # Info embedding: concatenate title, price, description, category, gender, metadata
            info_parts = [
                product_data.get('title') or '',
                product_data.get('price') or '',
                product_data.get('description') or '',
                product_data.get('category') or '',
                product_data.get('gender') or '',
            ]
            if product_data.get('metadata'):
                info_parts.append(product_data['metadata'] if isinstance(product_data['metadata'], str) else json.dumps(product_data['metadata']))
            info_text = " ".join(p for p in info_parts if p).strip()
            if info_text:
                info_emb = self.generate_text_embedding(info_text)
                if info_emb:
                    product_data['info_embedding'] = info_emb

            success = self.supabase.upsert_products([product_data])
            if success:
                logger.info(f"Successfully saved product: {product_data['title']}")
            else:
                logger.error(f"Failed to save product to database: {product_data['title']}")
            return success

        except Exception as e:
            logger.error(f"Error saving product to Supabase: {e}")
            return False

    def _compute_product_id(self, product: Dict[str, Any]) -> str:
        """Compute stable product id from source and product_url (same as DB)."""
        source = product.get("source", self.source)
        product_url = product.get("product_url", "")
        id_string = f"{source}:{product_url}"
        return hashlib.sha256(id_string.encode("utf-8")).hexdigest()

    def scrape_all_categories(self):
        """
        Scrape all category pages and perform smart sync:
        - New products: insert (with embeddings)
        - Existing products with changes: update (regenerate embeddings only if image changed)
        - Existing products unchanged: skip (no DB update, no embedding regeneration)
        - Products not seen for 2 consecutive runs: delete
        """
        from datetime import datetime, timezone
        
        stats = {
            "new": 0,
            "updated": 0,
            "unchanged": 0,
            "deleted": 0,
        }
        failed_products_log = []
        
        # 1. Collect all products from all categories
        all_products: List[Dict[str, Any]] = []
        for category_url in self.category_urls:
            logger.info(f"Scraping category: {category_url}")
            html_content = self.get_page_content(category_url)
            if not html_content:
                logger.error(f"Could not fetch content for {category_url}")
                continue
            products = self.extract_products_from_page(html_content, category_url)
            logger.info(f"Found {len(products)} products in {category_url}")
            all_products.extend(products)
            time.sleep(2)

        if not all_products:
            logger.warning("No products scraped - skipping sync")
            self._print_summary(stats)
            return

        # Deduplicate by product_url (same product may appear in multiple categories)
        seen_urls: set = set()
        unique_products: List[Dict[str, Any]] = []
        for p in all_products:
            url = p.get("product_url")
            if url and url not in seen_urls:
                seen_urls.add(url)
                unique_products.append(p)

        scraped_ids = {self._compute_product_id(p) for p in unique_products}
        logger.info(f"Total unique products scraped: {len(unique_products)} (ids: {len(scraped_ids)})")

        # 2. Fetch all existing products with full data for smart comparison
        existing_products = self.supabase.get_all_products(self.source)
        existing_ids = set(existing_products.keys())
        logger.info(f"Existing products in DB for source={self.source}: {len(existing_ids)}")

        # Current timestamp for last_seen_at
        now = datetime.now(timezone.utc).isoformat()

        # 3. Categorize products: new, updated, unchanged
        new_products = []
        updated_products = []
        unchanged_product_ids = []

        for product in unique_products:
            product_id = self._compute_product_id(product)
            
            if product_id not in existing_ids:
                # New product - will need embeddings
                new_products.append(product)
            else:
                # Existing product - check if anything changed
                existing = existing_products[product_id]
                
                # Compare key fields
                has_changes = (
                    product.get('title') != existing.get('title') or
                    product.get('price') != existing.get('price') or
                    product.get('image_url') != existing.get('image_url') or
                    product.get('category') != existing.get('category') or
                    product.get('gender') != existing.get('gender') or
                    product.get('size') != existing.get('size') or
                    product.get('description') != existing.get('description') or
                    product.get('additional_images') != existing.get('additional_images') or
                    product.get('second_hand') != existing.get('second_hand')
                )
                
                if has_changes:
                    # Product changed - will need embeddings if image changed
                    updated_products.append(product)
                else:
                    # Product unchanged - skip entirely
                    unchanged_product_ids.append(product_id)
                    stats["unchanged"] += 1

        # 4. Determine stale products (not seen in current scrape)
        # Products that were in DB but not in current scrape
        to_increment_stale = list(existing_ids - scraped_ids)
        
        # Products that have been stale for 2 consecutive runs - to delete
        to_delete_ids = []
        
        for pid in to_increment_stale:
            existing = existing_products.get(pid, {})
            current_stale_count = existing.get('stale_count', 0)
            new_stale_count = current_stale_count + 1
            
            if new_stale_count >= 2:
                # Stale for 2 consecutive runs - delete
                to_delete_ids.append(pid)
            else:
                # Increment stale count (will be updated in DB)
                pass

        logger.info(f"Smart sync: {len(new_products)} new, {len(updated_products)} updated, {len(unchanged_product_ids)} unchanged, {len(to_delete_ids)} to delete")

        # 5. Process new products (with embeddings)
        logger.info(f"Processing {len(new_products)} new products...")
        for i, product in enumerate(new_products):
            if i > 0 and i % 10 == 0:
                logger.info(f"Processing new products: {i}/{len(new_products)}")
            
            # Add last_seen_at
            product['last_seen_at'] = now
            product['stale_count'] = 0
            
            # Generate embeddings for new products
            success = self._save_product_with_embeddings(product)
            if success:
                stats["new"] += 1
            else:
                failed_products_log.append({"product": product.get('title', 'unknown'), "reason": "embedding failed"})
            time.sleep(0.5)  # Staggered embedding generation

        # 6. Process updated products (with embeddings only if image changed)
        logger.info(f"Processing {len(updated_products)} updated products...")
        for i, product in enumerate(updated_products):
            if i > 0 and i % 10 == 0:
                logger.info(f"Processing updated products: {i}/{len(updated_products)}")
            
            product_id = self._compute_product_id(product)
            existing = existing_products.get(product_id, {})
            
            # Add last_seen_at and reset stale_count
            product['last_seen_at'] = now
            product['stale_count'] = 0
            
            # Check if image changed - only regenerate embeddings if so
            image_changed = product.get('image_url') != existing.get('image_url')
            
            if image_changed:
                logger.debug(f"Image changed for {product.get('title')} - regenerating embeddings")
                success = self._save_product_with_embeddings(product)
            else:
                # Copy existing embeddings to avoid regenerating
                product['image_embedding'] = existing.get('image_embedding')
                product['info_embedding'] = existing.get('info_embedding')
                success = self._save_product_with_embeddings(product)
            
            if success:
                stats["updated"] += 1
            else:
                failed_products_log.append({"product": product.get('title', 'unknown'), "reason": "update failed"})
            time.sleep(0.5)  # Staggered embedding generation

        # 7. Update stale products (increment stale_count, reset last_seen_at)
        products_to_mark_stale = []
        for pid in to_increment_stale:
            if pid not in to_delete_ids:
                existing = existing_products.get(pid, {})
                products_to_mark_stale.append({
                    'id': pid,
                    'last_seen_at': now,
                    'stale_count': (existing.get('stale_count', 0)) + 1,
                    'source': self.source,
                })
        
        if products_to_mark_stale:
            # Batch update stale products
            success, _, failed = self.supabase.batch_upsert_products(products_to_mark_stale)
            if failed:
                logger.warning(f"Failed to update {len(failed)} stale product counts")

        # 8. Delete products that have been stale for 2 consecutive runs
        if to_delete_ids:
            if self.supabase.delete_products_by_ids(to_delete_ids):
                stats["deleted"] = len(to_delete_ids)
                logger.info(f"Deleted {len(to_delete_ids)} stale products")
            else:
                logger.error("Failed to delete stale products")

        # 9. Log failed products to file
        if failed_products_log:
            self._log_failed_products(failed_products_log)

        # 10. Print summary
        self._print_summary(stats)
        logger.info(f"Smart sync complete")

    def _save_product_with_embeddings(self, product_data: dict) -> bool:
        """Save product data to Supabase with embeddings, only generating embeddings when needed."""
        try:
            if not product_data.get('image_url'):
                logger.error(f"No image URL for product: {product_data.get('title')} - cannot generate embedding")
                return False

            # Check if embeddings already exist (for unchanged/updated products)
            has_image_emb = product_data.get('image_embedding') is not None
            has_info_emb = product_data.get('info_embedding') is not None

            # Generate image embedding if not present
            if not has_image_emb:
                logger.debug(f"Generating image embedding for: {product_data.get('title')}")
                image_emb = self.generate_image_embedding(product_data['image_url'])
                if not image_emb:
                    logger.error(f"Could not generate image embedding for product: {product_data.get('title')}")
                    return False
                product_data['image_embedding'] = image_emb

            # Generate info embedding if not present
            if not has_info_emb:
                info_parts = [
                    product_data.get('title') or '',
                    product_data.get('price') or '',
                    product_data.get('description') or '',
                    product_data.get('category') or '',
                    product_data.get('gender') or '',
                ]
                if product_data.get('metadata'):
                    info_parts.append(product_data['metadata'] if isinstance(product_data['metadata'], str) else json.dumps(product_data['metadata']))
                info_text = " ".join(p for p in info_parts if p).strip()
                if info_text:
                    info_emb = self.generate_text_embedding(info_text)
                    if info_emb:
                        product_data['info_embedding'] = info_emb

            success, failed_count, failed = self.supabase.batch_upsert_products([product_data])
            if success:
                logger.debug(f"Successfully saved product: {product_data.get('title')}")
                return True
            else:
                logger.error(f"Failed to save product to database: {product_data.get('title')}")
                return False

        except Exception as e:
            logger.error(f"Error saving product to Supabase: {e}")
            return False

    def _log_failed_products(self, failed_products: List[Dict[str, Any]]):
        """Log failed products to a local file."""
        try:
            log_file = "failed_products.log"
            timestamp = datetime.now(timezone.utc).isoformat()
            with open(log_file, 'a') as f:
                f.write(f"\n--- Failed products at {timestamp} ---\n")
                for fp in failed_products:
                    f.write(f"Product: {fp.get('product')}, Reason: {fp.get('reason')}\n")
            logger.info(f"Logged {len(failed_products)} failed products to {log_file}")
        except Exception as e:
            logger.error(f"Failed to log failed products: {e}")

    def _print_summary(self, stats: Dict[str, int]):
        """Print the run summary."""
        logger.info("=" * 50)
        logger.info("SCRAPER RUN SUMMARY")
        logger.info("=" * 50)
        logger.info(f"  {stats['new']} new products added")
        logger.info(f"  {stats['updated']} products updated")
        logger.info(f"  {stats['unchanged']} products unchanged (skipped)")
        logger.info(f"  {stats['deleted']} stale products deleted")
        logger.info("=" * 50)

    def run(self):
        """Main execution method"""
        logger.info("Starting Davril Supply scraper")
        self.scrape_all_categories()
        logger.info("Scraping completed")


if __name__ == "__main__":
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

    SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
    SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Set SUPABASE_URL and SUPABASE_KEY (env or .env)")

    scraper = DavrilSupplyScraper(SUPABASE_URL, SUPABASE_KEY)
    scraper.run()