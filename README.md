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

## Setup

### Local Development

1. Clone the repository:
```bash
git clone https://github.com/adrianpawlas/scraper-davrilsupply.git
cd scraper-davrilsupply
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

### GitHub Repository Setup

1. **Set up repository secrets** (Settings → Secrets and variables → Actions):
   - `SUPABASE_URL`: Your Supabase project URL (`https://yqawmzggcgpeyaaynrjk.supabase.co`)
   - `SUPABASE_KEY`: Your Supabase service role key

2. **Enable workflows** (automatically enabled when you push the workflow files)

## Configuration

The scraper is configured with:
- EUR to USD conversion rate (currently 1.08)
- 8 category URLs from Davril Supply
- Supabase credentials (loaded from environment variables)

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

### Local Execution

Run the scraper locally:

```bash
python run_full_scraper.py
```

Test the scraper:

```bash
python test_scraper.py
```

### GitHub Actions Automation

The scraper runs automatically every day at midnight UTC, but you can also trigger it manually:

#### Daily Automated Runs
- **Schedule**: Every day at 00:00 UTC (midnight)
- **Trigger**: Automatic via cron schedule

#### Manual Runs
Go to the repository → Actions tab → "Manual Davril Supply Scrape" workflow:

1. Click "Run workflow"
2. Configure options:
   - **Categories**: Leave empty for all categories, or specify comma-separated list
   - **Test mode**: Check to run only on first category for testing
3. Click "Run workflow"

### What the Scraper Does

1. Visits each category page using Selenium
2. Extracts product information from HTML
3. Downloads and processes product images (when available)
4. Generates 768-dimensional embeddings using SigLIP model
5. Converts EUR prices to USD
6. Saves all data to Supabase with deduplication

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