#!/usr/bin/env python3
"""
Debug script to examine HTML structure
"""

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup

def setup_driver():
    """Setup Chrome WebDriver with appropriate options"""
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1920,1080")

    driver = webdriver.Chrome(
        service=ChromeService(ChromeDriverManager().install()),
        options=chrome_options
    )

    return driver

def debug_html():
    """Debug the HTML structure"""
    url = "https://davrilsupply.com/collections/t-shirts"

    driver = None
    try:
        driver = setup_driver()
        driver.get(url)

        # Wait for page to load
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "body"))
        )

        html_content = driver.page_source
        soup = BeautifulSoup(html_content, 'html.parser')

        print("=== HTML Analysis ===")
        print(f"Total HTML length: {len(html_content)}")

        # Look for product-related elements
        print("\n=== Looking for product titles ===")

        # Try different selectors for titles
        title_selectors = [
            'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
            '.product-title', '.title', '[data-title]',
            '.product__title', '.product-title',
            'a[href*="products"]'
        ]

        for selector in title_selectors:
            elements = soup.select(selector)
            if elements:
                print(f"\nSelector '{selector}' found {len(elements)} elements:")
                for i, elem in enumerate(elements[:5]):  # Show first 5
                    text = elem.get_text().strip()
                    if text and len(text) > 3:
                        print(f"  {i+1}: '{text}'")

        # Look for prices
        print("\n=== Looking for prices ===")
        price_patterns = ['€', '$', 'price']
        for pattern in price_patterns:
            elements = soup.find_all(string=lambda text: text and pattern in text)
            if elements:
                print(f"\nPattern '{pattern}' found {len(elements)} strings:")
                for i, elem in enumerate(elements[:5]):
                    print(f"  {i+1}: '{elem.strip()}'")

        # Look for specific product structure
        print("\n=== Looking for product containers ===")
        product_containers = soup.find_all(['div', 'article', 'li'], class_=lambda c: c and any(word in c.lower() for word in ['product', 'item', 'card', 'grid']))
        print(f"Found {len(product_containers)} potential product containers")

        # Check the actual structure from the provided sample
        print("\n=== Checking for patterns from sample HTML ===")
        all_text = soup.get_text()
        lines = [line.strip() for line in all_text.split('\n') if line.strip()]

        product_like_lines = []
        for line in lines:
            # Look for lines that might be product titles
            if (len(line) > 5 and len(line) < 50 and
                line.replace(' ', '').replace('-', '').replace('_', '').isalnum() and
                not any(char.isdigit() for char in line) and
                '€' not in line and 'CART' not in line and 'HOME' not in line and
                'SEARCH' not in line and 'MY ACCOUNT' not in line and
                line.upper() == line):  # All caps
                product_like_lines.append(line)

        print(f"Found {len(product_like_lines)} all-caps lines that might be product titles:")
        for i, line in enumerate(product_like_lines[:10]):
            print(f"  {i+1}: '{line}'")

    finally:
        if driver:
            driver.quit()

if __name__ == "__main__":
    debug_html()