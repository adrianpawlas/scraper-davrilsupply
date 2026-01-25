import os
import time
import json
import uuid
import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup
from supabase import create_client, Client
import torch
from transformers import AutoProcessor, AutoModel
from PIL import Image
import io
import logging
from fake_useragent import UserAgent
import cloudscraper
from urllib.parse import urljoin, urlparse
import re

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class DavrilSupplyScraper:
    def __init__(self, supabase_url: str, supabase_key: str):
        self.supabase_url = supabase_url
        self.supabase_key = supabase_key
        self.supabase: Client = create_client(supabase_url, supabase_key)

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

            # Find image - look for img tag near the link
            image_url = None
            container = link.parent
            img = container.find('img') or link.find('img')
            if img:
                src = img.get('src')
                if src:
                    if not src.startswith('http'):
                        image_url = urljoin(self.base_url, src)
                    else:
                        image_url = src

            # Extract sizes if available (look for S, M, L, XL in the container)
            sizes = []
            container_text = container.get_text()
            size_matches = re.findall(r'\b(S|M|L|XL)\b', container_text)
            sizes = list(set(size_matches))  # Remove duplicates

            # Generate unique ID
            product_id = str(uuid.uuid4())

            # Extract category from URL
            category = self.extract_category_from_url(category_url)

            product_data = {
                'id': product_id,
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

            # Generate unique ID
            product_id = str(uuid.uuid4())

            # Extract category from URL
            category = self.extract_category_from_url(category_url)

            # Create basic product data
            product_data = {
                'id': product_id,
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

            # Extract image URL
            img_elem = container.find('img')
            image_url = None
            if img_elem:
                image_url = img_elem.get('src') or img_elem.get('data-src')
                if image_url and not image_url.startswith('http'):
                    image_url = urljoin(self.base_url, image_url)

            if not image_url:
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

            # Generate unique ID
            product_id = str(uuid.uuid4())

            # Extract category from URL
            category = self.extract_category_from_url(category_url)

            product_data = {
                'id': product_id,
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

    def generate_image_embedding(self, image_url: str) -> list:
        """Generate 768-dim embedding from image URL"""
        try:
            # Download image
            response = requests.get(image_url, timeout=10)
            response.raise_for_status()

            # Open image
            image = Image.open(io.BytesIO(response.content)).convert('RGB')

            # Process image for the model
            inputs = self.processor(images=image, return_tensors="pt").to(self.device)

            with torch.no_grad():
                outputs = self.model(**inputs)

            # Get the image embeddings (768-dim)
            embeddings = outputs.pooler_output.cpu().numpy().flatten().tolist()

            return embeddings

        except Exception as e:
            logger.error(f"Error generating embedding for {image_url}: {e}")
            return None

    def save_product_to_supabase(self, product_data: dict):
        """Save product data to Supabase with deduplication"""
        try:
            # Check if product already exists (by source and product_url or title)
            existing_products = self.supabase.table('products').select('id').eq('source', 'scraper').eq('title', product_data['title']).execute()

            if existing_products.data:
                logger.info(f"Product already exists: {product_data['title']}")
                return True  # Consider it successful since it already exists

            # Generate embedding if image_url exists
            if product_data.get('image_url'):
                logger.info(f"Generating embedding for: {product_data['title']}")
                embedding = self.generate_image_embedding(product_data['image_url'])
                if embedding:
                    product_data['embedding'] = embedding
                else:
                    logger.warning(f"Could not generate embedding for product: {product_data['title']}")
                    # Still save the product but without embedding
                    pass

            # Insert into Supabase
            result = self.supabase.table('products').insert(product_data).execute()

            logger.info(f"Successfully saved product: {product_data['title']}")
            return True

        except Exception as e:
            logger.error(f"Error saving product to Supabase: {e}")
            # Try to update if it already exists with different data
            try:
                # Check if the error is due to unique constraint violation
                if 'duplicate key' in str(e).lower() or 'unique constraint' in str(e).lower():
                    logger.info(f"Product already exists (constraint violation): {product_data['title']}")
                    return True
            except:
                pass
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