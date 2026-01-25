import os
import time
import json
import base64
import hashlib
import requests
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

    def upsert_products(self, products: List[Dict[str, Any]]) -> bool:
        """
        Upsert products into the database.
        Args:
            products: List of product dictionaries
        Returns:
            True if successful, False otherwise
        """
        if not products:
            return True

        try:
            # Format products for database
            formatted_products = []
            seen_ids = set()

            for product in products:
                formatted_product = self._format_product_for_db(product)
                if formatted_product:
                    # Deduplicate by id
                    product_id = formatted_product.get('id')
                    if product_id and product_id not in seen_ids:
                        seen_ids.add(product_id)
                        formatted_products.append(formatted_product)

            if not formatted_products:
                logger.warning("No valid products to upsert after formatting")
                return False

            logger.info(f"Upserting {len(formatted_products)} unique products")

            # Normalize all products to have the same keys
            all_keys = set()
            for p in formatted_products:
                all_keys.update(p.keys())

            # Ensure every product has all keys (fill missing with None)
            normalized_products = []
            for p in formatted_products:
                normalized = {key: p.get(key) for key in all_keys}
                normalized_products.append(normalized)

            # Use direct POST with Prefer header for upsert
            endpoint = f"{self.base_url}/rest/v1/products"
            headers = {
                "Prefer": "resolution=merge-duplicates,return=minimal",
            }

            # Insert in chunks to keep requests reasonable
            chunk_size = 50  # Smaller chunks for embeddings
            success_count = 0

            for i in range(0, len(normalized_products), chunk_size):
                chunk = normalized_products[i:i + chunk_size]

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
                    else:
                        logger.error(f"Failed to upsert batch: {resp.status_code} {resp.text}")
                        continue

                except Exception as batch_error:
                    logger.error(f"Failed to upsert batch: {batch_error}")
                    continue

            logger.info(f"Successfully upserted {success_count} products")
            return success_count > 0

        except Exception as e:
            logger.error(f"Failed to upsert products: {e}")
            return False

    def _format_product_for_db(self, product: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Format a product dictionary for database insertion.
        """
        try:
            # Required fields
            source = product.get('source', 'scraper')
            product_url = product.get('product_url')
            image_url = product.get('image_url')
            title = product.get('title')

            if not source or not product_url or not image_url or not title:
                logger.warning(f"Missing required fields: {product}")
                return None

            # Generate deterministic ID from source and product_url
            id_string = f"{source}:{product_url}"
            product_id = hashlib.sha256(id_string.encode('utf-8')).hexdigest()

            # Build the formatted product
            formatted = {
                'id': product_id,
                'source': source,
                'product_url': product_url,
                'image_url': image_url,
                'title': title,
                'brand': product.get('brand'),
                'gender': product.get('gender'),
                'price': product.get('price'),
                'currency': product.get('currency', 'USD'),
                'size': product.get('size'),
                'second_hand': product.get('second_hand', False)
            }

            # Optional fields
            for field in ['affiliate_url', 'description', 'category', 'embedding']:
                if field in product and product[field] is not None:
                    formatted[field] = product[field]

            # Optional metadata
            metadata = {}
            if 'metadata' in product and product['metadata']:
                if isinstance(product['metadata'], str):
                    try:
                        metadata = json.loads(product['metadata'])
                    except:
                        metadata = {'raw_metadata': product['metadata']}
                elif isinstance(product['metadata'], dict):
                    metadata = product['metadata']

            if metadata:
                formatted['metadata'] = json.dumps(metadata)

            return formatted

        except Exception as e:
            logger.error(f"Failed to format product: {e}")
            return None


class DavrilSupplyScraper:
    def __init__(self, supabase_url: str, supabase_key: str):
        self.supabase_url = supabase_url
        self.supabase_key = supabase_key
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

            if not image_url or not self._is_valid_image_url(image_url):
                logger.warning(f"No valid image URL for product: {title}")
                return None

            # Extract sizes if available (look for S, M, L, XL in the container)
            sizes = []
            container_text = container.get_text()
            size_matches = re.findall(r'\b(S|M|L|XL)\b', container_text)
            sizes = list(set(size_matches))  # Remove duplicates

            # Extract category from URL
            category = self.extract_category_from_url(category_url)

            product_data = {
                'source': 'scraper',
                'brand': 'Davril Supply',
                'title': title,
                'price': price,
                'currency': 'USD',
                'image_url': image_url,
                'product_url': product_url,
                'category': category,
                'gender': 'man',
                'second_hand': False,
                'size': ','.join(sizes) if sizes else None,
                'metadata': json.dumps({
                    'sizes': sizes,
                    'original_price_text': price_text,
                    'category_url': category_url,
                    'extraction_method': 'link_based'
                })
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

            # Extract category from URL
            category = self.extract_category_from_url(category_url)

            # Create basic product data
            product_data = {
                'source': 'scraper',
                'brand': 'Davril Supply',
                'title': title,
                'price': price,
                'currency': 'USD',
                'category': category,
                'gender': 'man',
                'second_hand': False,
                'metadata': json.dumps({
                    'original_price_text': price_text,
                    'category_url': category_url,
                    'extraction_method': 'text_analysis'
                })
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

            # Extract category from URL
            category = self.extract_category_from_url(category_url)

            product_data = {
                'source': 'scraper',
                'brand': 'Davril Supply',
                'title': title,
                'price': price,
                'currency': 'USD',  # Convert to USD as requested
                'image_url': image_url,
                'product_url': product_url,
                'category': category,
                'gender': 'man',
                'second_hand': False,
                'size': ','.join(sizes) if sizes else None,
                'metadata': json.dumps({
                    'sizes': sizes,
                    'original_price_text': price_text,
                    'category_url': category_url
                })
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
            inputs = self.processor(images=image, return_tensors="pt").to(self.device)
            with torch.no_grad():
                outputs = self.model(**inputs)
            embeddings = outputs.pooler_output.cpu().numpy().flatten().tolist()
            return embeddings
        except Exception as e:
            logger.error(f"Error generating embedding for {image_url[:80]}...: {e}")
            return None

    def save_product_to_supabase(self, product_data: dict):
        """Save product data to Supabase using REST API - images and embeddings required"""
        try:
            # Images are REQUIRED for embeddings
            if not product_data.get('image_url'):
                logger.error(f"No image URL for product: {product_data['title']} - cannot generate embedding")
                return False

            # Generate embedding (REQUIRED)
            logger.debug(f"Generating embedding for: {product_data['title']}")
            try:
                embedding = self.generate_image_embedding(product_data['image_url'])
                if embedding:
                    product_data['embedding'] = embedding
                    logger.debug(f"Successfully generated embedding for: {product_data['title']}")
                else:
                    logger.error(f"Could not generate embedding for product: {product_data['title']}")
                    return False
            except Exception as e:
                logger.error(f"Embedding generation failed for {product_data['title']}: {e}")
                return False

            # Use REST API to upsert the product with embedding
            success = self.supabase.upsert_products([product_data])

            if success:
                logger.info(f"Successfully saved product with embedding: {product_data['title']}")
            else:
                logger.error(f"Failed to save product to database: {product_data['title']}")

            return success

        except Exception as e:
            logger.error(f"Error saving product to Supabase: {e}")
            return False

    def scrape_all_categories(self):
        """Scrape all category pages"""
        total_products = 0

        for category_url in self.category_urls:
            logger.info(f"Scraping category: {category_url}")

            # Get page content
            html_content = self.get_page_content(category_url)

            if not html_content:
                logger.error(f"Could not fetch content for {category_url}")
                continue

            # Extract products
            products = self.extract_products_from_page(html_content, category_url)

            logger.info(f"Found {len(products)} products in {category_url}")

            # Save products to database
            for product in products:
                if self.save_product_to_supabase(product):
                    total_products += 1

            # Add delay between categories to be respectful
            time.sleep(2)

        logger.info(f"Total products scraped and saved: {total_products}")

    def run(self):
        """Main execution method"""
        logger.info("Starting Davril Supply scraper")
        self.scrape_all_categories()
        logger.info("Scraping completed")


if __name__ == "__main__":
    # Supabase credentials
    SUPABASE_URL = "https://yqawmzggcgpeyaaynrjk.supabase.co"
    SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InlxYXdtemdnY2dwZXlhYXlucmprIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc1NTAxMDkyNiwiZXhwIjoyMDcwNTg2OTI2fQ.XtLpxausFriraFJeX27ZzsdQsFv3uQKXBBggoz6P4D4"

    scraper = DavrilSupplyScraper(SUPABASE_URL, SUPABASE_KEY)
    scraper.run()