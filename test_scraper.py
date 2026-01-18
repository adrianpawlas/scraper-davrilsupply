#!/usr/bin/env python3
"""
Test script for Davril Supply scraper
"""

import sys
import logging
from davrilsupply_scraper import DavrilSupplyScraper

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def test_scraper_initialization():
    """Test that the scraper can initialize properly"""
    try:
        # Supabase credentials
        SUPABASE_URL = "https://yqawmzggcgpeyaaynrjk.supabase.co"
        SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InlxYXdtemdnY2dwZXlhYXlucmprIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc1NTAxMDkyNiwiZXhwIjoyMDcwNTg2OTI2fQ.XtLpxausFriraFJeX27ZzsdQsFv3uQKXBBggoz6P4D4"

        logger.info("Initializing scraper...")
        scraper = DavrilSupplyScraper(SUPABASE_URL, SUPABASE_KEY)
        logger.info("Scraper initialized successfully")
        return True
    except Exception as e:
        logger.error(f"Failed to initialize scraper: {e}")
        return False

def test_single_category():
    """Test scraping a single category"""
    try:
        # Supabase credentials
        SUPABASE_URL = "https://yqawmzggcgpeyaaynrjk.supabase.co"
        SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InlxYXdtemdnY2dwZXlhYXlucmprIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc1NTAxMDkyNiwiZXhwIjoyMDcwNTg2OTI2fQ.XtLpxausFriraFJeX27ZzsdQsFv3uQKXBBggoz6P4D4"

        logger.info("Testing single category scrape...")
        scraper = DavrilSupplyScraper(SUPABASE_URL, SUPABASE_KEY)

        # Test just one category
        test_category = "https://davrilsupply.com/collections/t-shirts"

        html_content = scraper.get_page_content(test_category)
        if not html_content:
            logger.error("Failed to fetch page content")
            return False

        logger.info(f"Fetched {len(html_content)} characters of HTML")

        # Extract products
        products = scraper.extract_products_from_page(html_content, test_category)
        logger.info(f"Extracted {len(products)} products from test category")

        # Show first few products with details
        for i, product in enumerate(products[:3]):
            logger.info(f"Product {i+1}: {product['title']} - ${product['price']}")
            logger.info(f"  Image URL: {product.get('image_url', 'None')}")
            logger.info(f"  Product URL: {product.get('product_url', 'None')}")

        # Test embedding generation on first product with image
        if products:
            product_with_image = None
            for product in products:
                if product.get('image_url'):
                    product_with_image = product
                    break

            if product_with_image:
                logger.info(f"Testing embedding generation for: {product_with_image['title']}")
                embedding = scraper.generate_image_embedding(product_with_image['image_url'])
                if embedding:
                    logger.info(f"Successfully generated {len(embedding)}-dimensional embedding")
                    product_with_image['embedding'] = embedding

                    # Test saving to Supabase
                    logger.info("Testing save to Supabase...")
                    success = scraper.save_product_to_supabase(product_with_image)
                    if success:
                        logger.info("Successfully saved product to Supabase!")
                    else:
                        logger.warning("Failed to save product to Supabase")
                else:
                    logger.warning("Failed to generate embedding")

        return True

    except Exception as e:
        logger.error(f"Failed to test single category: {e}")
        return False

def main():
    """Run all tests"""
    logger.info("Starting Davril Supply scraper tests")

    # Test 1: Initialization
    if not test_scraper_initialization():
        logger.error("Initialization test failed")
        sys.exit(1)

    # Test 2: Single category scrape
    if not test_single_category():
        logger.error("Single category test failed")
        sys.exit(1)

    logger.info("All tests passed!")

if __name__ == "__main__":
    main()