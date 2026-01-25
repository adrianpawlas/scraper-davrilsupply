#!/usr/bin/env python3
"""
Full scraper runner for Davril Supply
"""

import os
import logging
from davrilsupply_scraper import DavrilSupplyScraper

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('scraper.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def main():
    """Run the full scraper"""
    # Supabase credentials from environment variables
    SUPABASE_URL = os.getenv("SUPABASE_URL")
    SUPABASE_KEY = os.getenv("SUPABASE_KEY")

    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.error("SUPABASE_URL and SUPABASE_KEY environment variables are required")
        raise ValueError("Missing Supabase credentials")

    logger.info("Starting Davril Supply full scraper")

    try:
        scraper = DavrilSupplyScraper(SUPABASE_URL, SUPABASE_KEY)
        scraper.run()
        logger.info("Scraping completed successfully!")
    except Exception as e:
        logger.error(f"Scraping failed: {e}")
        raise

if __name__ == "__main__":
    main()