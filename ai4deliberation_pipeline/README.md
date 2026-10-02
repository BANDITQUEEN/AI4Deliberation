# AI4Deliberation Pipeline Documentation

This document provides a detailed overview of the AI4Deliberation pipeline, its components, workflow, and programmatic usage.

## 1. Overview
The pipeline is designed to automate the collection, processing, and storage of Greek online public consultation data from opengov.gr.

Key functionalities include:
- Comprehensive web scraping of consultation metadata, articles, comments, and official documents.
- HTML to Markdown conversion for web content.
- PDF document download and text extraction (utilizing `docling`).
- Rust-based text cleaning for document content.
- Database integration using SQLAlchemy for persistent storage.
- Modular orchestration of the entire workflow.

### Data source
opengov.gr has moved to **https://archive.opengov.gr** (the old `www.opengov.gr` address no longer serves the site). The scrapers read the consultation listing from `https://archive.opengov.gr/home/category/consultations`. As of September 2026 the newest listed consultation is from 10 July 2026.

Some listing entries are homepage announcements (`archive.opengov.gr/home/YYYY/MM/DD/ID`) rather than consultation pages; the listers follow them to the ministry consultation they link to (`scraper/utils.py`: `resolve_announcement_url`).

Databases scraped before the move store `www.opengov.gr` URLs. URL matching treats `www.opengov.gr`, `opengov.gr` and `archive.opengov.gr` as the same site (`scraper/utils.py`: `opengov_url_key`, `opengov_url_variants`), so updating an older database does not create duplicate consultations, articles or documents.

## 2. Core Components & Workflow
The pipeline is primarily orchestrated by `master/pipeline_orchestrator.py`.

### Main Workflow Steps:
1.  **Discovery:** (`scraper/list_consultations.py`) Identifies new and existing consultations.
2.  **Scraping:** (`scraper/scrape_single_consultation.py`) Fetches raw data for each consultation.
3.  **Content Processing:** (`utils/data_flow.py` - `ContentProcessor`)
    *   HTML articles/comments: `markdownify` conversion.
    *   Documents (PDFs, etc.): Download, text extraction (`docling`), and then cleaning (`text_cleaner_rs`).
4.  **Storage:** Data is stored in an SQLite database using models defined in `scraper/db_models.py`.

### Key Modules:
-   **`master/`**: Orchestration and main pipeline entry points.
    -   `pipeline_orchestrator.py`: Contains the `PipelineOrchestrator` class.
-   **`scraper/`**: Handles all web scraping tasks (see section 3).
    -   `scrape_all_consultations.py`: Builds and maintains a database (listing scrape, repair, discovery by post ID).
    -   `scrape_single_consultation.py`: Scrapes and stores a single consultation.
    -   `list_consultations.py`: Discovers consultations (central listing and post-ID scanning).
    -   `content_scraper.py` / `metadata_scraper.py`: Articles and comments / consultation metadata.
    -   `db_population_report.py`: Field-population, integrity and site-statistics reports.
    -   `db_models.py`: SQLAlchemy database models.
    -   `utils.py`: Shared helpers (HTTP client, URL keys, Greek dates).
-   **`html_processor/`**: Converts HTML to Markdown. (Note: Main logic seems integrated into `utils.data_flow.ContentProcessor`)
-   **`pdf_processor/`**: Handles PDF downloading and coordinates extraction. (Note: Main logic seems integrated into `utils.data_flow.ContentProcessor` using `docling`).
-   **`rust_processor/`**: Interface for the Rust-based text cleaner (`text_cleaner_rs`). (Note: Main logic seems integrated into `utils.data_flow.ContentProcessor`).
-   **`utils/`**: Shared utilities.
    -   `data_flow.py`: `ContentProcessor` for HTML, PDF, and cleaning operations.
    -   `database.py`: Database connection utilities.
-   **`config/`**: Configuration management.
    -   `config_manager.py`: Loads `pipeline_config.yaml`.
-   **`tests_and_analysis/`**: (Placeholder for tests and analysis scripts related to the pipeline).
-   **`requirements.txt`**: Python dependencies.

## 3. Building the consultations database

Run these from the repository root. `--db-path` takes an SQLAlchemy URL; it defaults to `sqlite:///ai4deliberation_pipeline/deliberation_data_gr.db`.

```bash
# Full build into a new database: the listing, then repair, then the consultations the listing misses
python -m ai4deliberation_pipeline.scraper.scrape_all_consultations --db-path sqlite:///new.db --fresh-db
python -m ai4deliberation_pipeline.scraper.scrape_all_consultations --db-path sqlite:///new.db --repair
python -m ai4deliberation_pipeline.scraper.scrape_all_consultations --db-path sqlite:///new.db --discover-by-id known

# Update an existing database: new consultations are added, unfinished ones re-scraped, finished ones skipped
python -m ai4deliberation_pipeline.scraper.scrape_all_consultations --db-path sqlite:///existing.db
# (the orchestrator's update mode does the same through list_consultations.py --update, then processes content)

# Check a database against the site
python -m ai4deliberation_pipeline.scraper.db_population_report --db-path sqlite:///existing.db --integrity --site-stats
# Fill what the site-statistics comparison reports missing (scans only the post-ID ranges around the gaps)
python -m ai4deliberation_pipeline.scraper.scrape_all_consultations --db-path sqlite:///existing.db --discover-by-id stats-gaps

# One consultation
python -m ai4deliberation_pipeline.scraper.scrape_single_consultation --db-path sqlite:///existing.db https://archive.opengov.gr/minenv/?p=7
```

`--repair` and `--discover-by-id` accept `--dry-run`, which logs what they would change without changing the database. The post-ID scan records every probed ID in `--id-scan-file` (default `id_scan.jsonl`), so later or interrupted scans skip IDs already probed.

### What the site does, and how the scraper handles it
- **Comment pages.** An article's bare URL can show its *last* comment page, so comments are always fetched from `cpage=1..N`.
- **Comments on the consultation page itself.** Early consultations took comments on the root post (the minister's introduction), which is not one of its articles. These are stored on an extra article with `extraction_method='consultation_root'` (also set on its comments), so they can be filtered out.
- **Consultations the listing misses.** The listing links most 2009-2011 consultations through dead `?option=…` or bare ministry URLs, has a few entries with no link at all, and omits some later consultations. Their posts still exist under `?p=<id>`, and a nonexistent ID redirects to the ministry homepage, so `--discover-by-id` scans post IDs. A post absent from the database whose navigation lists articles, none of which are stored, is the root of a missing consultation. Consultations whose own page lists no articles get the articles found under a sibling post. Some consultations have lost their root post and only their articles remain; those are stored from the articles, with the title from the statistics page. Candidates without comments are stored only when they match a consultation on the statistics page, since report pages often list unrelated posts. A bill is sometimes posted twice under one ministry (for example `minenv/?p=13997`, an early posting of `minenv/?p=14078`): a candidate is skipped as a duplicate when a stored consultation of the same ministry holds all of its articles with the same titles and nearly the same text (at least 500 characters in common, since short boilerplate articles are shared by unrelated consultations), their consultation periods overlap, and the candidate has no comments; a duplicate with comments of its own is stored and flagged for review. Review a `--dry-run` first: `--skip URL…` leaves out further candidates containing those posts.
- **`--repair`** re-fetches comments wherever the per-article counts on a consultation page exceed what is stored.
- **Site statistics.** `https://archive.opengov.gr/opengov/StatsPerMinistry.php` lists every consultation with its article count and approved/total comments; the homepage widget follows the approved column. Its counts are not always right, so `--site-stats` is a cross-check that shows where to look, not a measure of completeness: every difference has to be checked on the consultation's own pages. For example, `ggk/?p=12` is counted with 535 comments that its pages do not show, and `gengk/?p=358` shows 67 comments that the page does not count. The page also omits the `civilprotection`, `koinsynoik`, `ggee` and `pste` ministries, so `--site-stats` leaves those out of the comparison.
- **Comment counts in the database.** `consultations.accepted_comments` is the number of comments stored for the consultation, counted from the `comments` table (root-post comments included); use it for per-consultation counts. `consultations.total_comments` is copied from the "Στατιστικά" box of the consultation page, whose "Όλα τα Σχόλια" figure is the total for the ministry's whole site, so it is the same for every consultation of a ministry and is not a per-consultation count.

## 4. Programmatic Usage

```python
# Example (conceptual)
# from ai4deliberation_pipeline.master import run_pipeline_entry
#
# # Process a single consultation
# run_pipeline_entry(mode='single', url='https://example.com/consultation/123')
#
# # Update with new consultations
# run_pipeline_entry(mode='update')
```

## 5. Configuration
The pipeline is configured via `config/pipeline_config.yaml`. Key settings include database paths, API keys (if any), and processing parameters.

## 6. Dependencies
See `requirements.txt`. Key dependencies include:
- `sqlalchemy`
- `requests`
- `markdownify`
- `pandas`
- `docling`
- `PyYAML`
- `text-cleaner-rs` (custom Rust bindings)

## 7. Future Enhancements & TODOs
Refer to the main `TODO_DOCUMENTATION.md` in the project root and the `PIPELINE_WORKFLOW_CHECKLIST.md` for planned improvements. 