# Davril Supply Scraper

This scraper extracts product data from Davril Supply fashion store and saves it to a Supabase database with image embeddings.

## Features

- Scrapes all product categories from Davril Supply
- Extracts product information (title, price, images, etc.)
- Generates 768-dimensional image embeddings using Google SigLIP model
- Handles deduplication and error recovery
- Saves data to Supabase with proper schema

## Requirements

- Python 3.8+
- Supabase account and database
- Internet connection for scraping and downloading images

## Installation

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Make sure you have the required models downloaded (they will be downloaded automatically on first run)

## Configuration

The scraper is configured with:
- Supabase URL and API key (hardcoded in the script)
- EUR to USD conversion rate (currently 1.08)
- Category URLs to scrape

## Database Schema

The scraper expects a `products` table with the following structure:

```sql
CREATE TABLE products (
  id TEXT NOT NULL,
  source TEXT NULL,
  product_url TEXT NULL,
  affiliate_url TEXT NULL,
  image_url TEXT NOT NULL,
  brand TEXT NULL,
  title TEXT NOT NULL,
  description TEXT NULL,
  category TEXT NULL,
  gender TEXT NULL,
  price DOUBLE PRECISION NULL,
  currency TEXT NULL,
  search_tsv TSVECTOR NULL,
  created_at TIMESTAMP WITH TIME ZONE NULL DEFAULT NOW(),
  metadata TEXT NULL,
  size TEXT NULL,
  second_hand BOOLEAN NULL DEFAULT FALSE,
  embedding PUBLIC.VECTOR NULL,
  country TEXT NULL,
  compressed_image_url TEXT NULL,
  tags TEXT[] NULL,
  search_vector TSVECTOR NULL,
  CONSTRAINT products_pkey PRIMARY KEY (id),
  CONSTRAINT products_source_product_url_key UNIQUE (source, product_url)
);
```

## Usage

Run the scraper:

```bash
python davrilsupply_scraper.py
```

The scraper will:
1. Visit each category page
2. Extract product information
3. Download and process product images
4. Generate embeddings using SigLIP
5. Save data to Supabase

## Output

- Console logs showing progress
- Products saved to Supabase database
- Embeddings generated for each product image

## Notes

- The scraper uses Selenium for reliable page loading
- Images are processed to generate 768-dim embeddings
- Duplicate products are automatically skipped
- EUR prices are converted to USD
- Error handling ensures the scraper continues even if some products fail