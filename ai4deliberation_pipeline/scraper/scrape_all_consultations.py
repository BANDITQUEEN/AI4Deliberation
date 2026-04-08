#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import logging
import os
import time
from collections import defaultdict
from datetime import datetime
from random import uniform
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .db_models import Consultation, init_db
from .scrape_single_consultation import scrape_and_store

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Constants
BASE_URL = "https://www.opengov.gr/home/category/consultations"
REQUEST_DELAY = (0.15, 0.25)  # Random delay between requests in seconds


def normalize_consultation_url(url):
    """Normalize URL for robust matching across http/https and trailing slash differences."""
    if not url:
        return None
    try:
        parsed = urlparse(url.strip())
        netloc = parsed.netloc.lower()
        path = parsed.path or ""
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        query = parsed.query or ""
        if query:
            return f"{netloc}{path}?{query}"
        return f"{netloc}{path}"
    except Exception:
        return None


def extract_ministry_code_from_url(url):
    """Extract ministry code from URL path, e.g. '/yme/?p=5739' -> 'yme'."""
    try:
        parsed = urlparse((url or "").strip())
        path_parts = [part for part in parsed.path.strip("/").split("/") if part]
        return path_parts[0].lower() if path_parts else None
    except Exception:
        return None


def dedupe_consultation_links(consultation_links):
    """Deduplicate consultation links by normalized URL, preserving order."""
    seen = set()
    deduped = []

    for c in consultation_links:
        norm = normalize_consultation_url(c.get("url"))
        if not norm:
            continue
        if norm in seen:
            continue
        seen.add(norm)
        deduped.append(c)

    logger.info(f"Deduplicated consultations: {len(consultation_links)} -> {len(deduped)}")
    return deduped


def get_consultation_links_from_page(url):
    """Extract all consultation links and titles from a page."""
    logger.info(f"Fetching consultation links from: {url}")

    try:
        response = requests.get(url, timeout=30, allow_redirects=True)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, "html.parser")

        content_div = soup.find("div", class_="downspace_item_content archive_list")
        if not content_div:
            logger.error(f"Could not find consultation listings div on page: {url}")
            return [], None

        consultations = []
        list_items = content_div.find_all("li")

        for item in list_items:
            try:
                link_element = None
                for candidate in [
                    item.find("a"),
                    item.find("p").find("a") if item.find("p") else None,
                    item.find("h2").find("a") if item.find("h2") else None,
                    item.find("h3").find("a") if item.find("h3") else None,
                ]:
                    if candidate and candidate.has_attr("href") and candidate.get_text(strip=True):
                        link_element = candidate
                        break

                if not link_element:
                    logger.warning(
                        "Could not find a suitable link/title element in list item: "
                        f"{item.get_text(strip=True)[:100]}..."
                    )
                    continue

                raw_href = link_element["href"].strip()
                consultation_url = urljoin(url, raw_href)
                consultation_title = link_element.get_text(strip=True)

                date_span = item.find("span", class_="start")
                consultation_date = date_span.get_text(strip=True) if date_span else ""

                consultations.append(
                    {
                        "url": consultation_url,
                        "title": consultation_title,
                        "date": consultation_date,
                    }
                )

            except Exception as e:
                logger.error(f"Error extracting consultation details: {e}")

        pagination = soup.find("div", class_="wp-pagenavi")
        next_page_url = None

        if pagination:
            next_link = pagination.find("a", class_="nextpostslink")
            if next_link and next_link.has_attr("href"):
                next_page_url = next_link["href"]
                logger.info(f"Found next page link: {next_page_url}")

        return consultations, next_page_url

    except Exception as e:
        logger.error(f"Error fetching page {url}: {e}")
        return [], None


def get_all_consultation_links(start_page=1, end_page=None):
    """Get all consultation links from all pages within the specified range."""
    all_consultations = []
    current_url = BASE_URL
    page_number = 1

    while page_number < start_page and current_url:
        logger.info(f"Skipping to page {start_page}, currently at page {page_number}")
        _, next_page_url = get_consultation_links_from_page(current_url)
        if next_page_url:
            current_url = next_page_url
            page_number += 1
        else:
            logger.error(f"Could not navigate to page {start_page}")
            return []

    while current_url:
        if end_page and page_number > end_page:
            logger.info(f"Reached end page {end_page}. Stopping.")
            break

        logger.info(f"Processing page {page_number}")
        consultations, next_page_url = get_consultation_links_from_page(current_url)

        if consultations:
            logger.info(f"Found {len(consultations)} consultations on page {page_number}")
            all_consultations.extend(consultations)
        else:
            logger.warning(f"No consultations found on page {page_number}")

        if next_page_url:
            current_url = next_page_url
            page_number += 1

            delay = uniform(*REQUEST_DELAY)
            logger.info(f"Waiting {delay:.2f} seconds before next request...")
            time.sleep(delay)
        else:
            logger.info("No more pages found. Scraping complete.")
            current_url = None

    logger.info(f"Total consultation links found before dedupe: {len(all_consultations)}")
    return all_consultations


def save_update_report(update_reports, output_file="unfinished_consultation_updates.txt"):
    """Save the update reports to a file."""
    try:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write("# Update Report for Unfinished Consultations\n\n")
            f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

            for url, title, changes in update_reports:
                f.write(f"## {title}\n")
                f.write(f"URL: {url}\n\n")

                update_stats = []
                if changes["new_comments"] > 0:
                    update_stats.append(f"+{changes['new_comments']} comments")
                if changes["new_documents"] > 0:
                    update_stats.append(f"+{changes['new_documents']} documents")
                if changes["total_comments_change"] != 0:
                    update_stats.append(f"{changes['total_comments_change']:+d} total comments")
                if changes["start_message_changed"]:
                    update_stats.append("start minister message updated")
                if changes["end_message_changed"]:
                    update_stats.append("end minister message updated")
                if changes["status_change"]:
                    update_stats.append("status: unfinished → finished")

                if update_stats:
                    f.write("Changes:\n")
                    for stat in update_stats:
                        f.write(f"- {stat}\n")
                else:
                    f.write("No changes detected\n")
                f.write("\n")

        logger.info(f"Update report saved to {output_file}")
        return True
    except Exception as e:
        logger.error(f"Error saving update report: {e}")
        return False


def build_existing_indexes(session):
    """Load existing consultations once into lookup indexes."""
    existing_by_url = {}
    existing_by_post_id = defaultdict(list)

    all_existing = session.query(Consultation).all()
    logger.info(f"Loaded {len(all_existing)} existing consultations from database for matching")

    for cons in all_existing:
        norm_url = normalize_consultation_url(cons.url)
        if norm_url:
            existing_by_url[norm_url] = cons

        if cons.post_id:
            existing_by_post_id[cons.post_id].append(cons)

    return existing_by_url, existing_by_post_id


def find_existing_consultation(url, post_id, existing_by_url, existing_by_post_id):
    """
    Find an existing consultation using:
    1. normalized URL
    2. post_id + ministry code

    Never trust post_id alone, because the same ?p= id can exist under different ministries.
    """
    normalized_url = normalize_consultation_url(url)
    if normalized_url and normalized_url in existing_by_url:
        return existing_by_url[normalized_url]

    if not post_id:
        return None

    post_id_matches = existing_by_post_id.get(post_id, [])
    target_ministry = extract_ministry_code_from_url(url)

    ministry_matches = [
        cons for cons in post_id_matches
        if extract_ministry_code_from_url(cons.url) == target_ministry
    ]

    if len(ministry_matches) == 1:
        return ministry_matches[0]

    if len(ministry_matches) > 1:
        unfinished = [cons for cons in ministry_matches if not cons.is_finished]
        chosen = unfinished[0] if unfinished else ministry_matches[0]
        logger.warning(
            f"Found {len(ministry_matches)} ministry-matching consultations "
            f"for post_id={post_id}; selected URL={chosen.url}"
        )
        return chosen

    if post_id_matches:
        logger.warning(
            f"Found post_id={post_id} in DB, but only under different ministry/ministries. "
            f"Treating as new consultation: {url}"
        )

    return None


def scrape_consultations_to_db(
    consultation_links,
    db_url,
    batch_size=20,
    max_count=None,
    force_scrape=False,
    fresh_db=False,
):
    """Scrape consultations and store in database."""
    engine, Session = init_db(db_url)
    session = Session()

    total_count = len(consultation_links)
    processed_count = 0
    success_count = 0
    skipped_count = 0
    update_reports = []

    logger.info(f"Starting to scrape {total_count} consultations to database")

    existing_by_url = {}
    existing_by_post_id = defaultdict(list)

    if not force_scrape and not fresh_db:
        existing_by_url, existing_by_post_id = build_existing_indexes(session)
    elif fresh_db:
        logger.info("Fresh DB mode enabled: skipping existing-record checks")

    try:
        for i, consultation in enumerate(consultation_links):
            if max_count and processed_count >= max_count:
                logger.info(f"Reached maximum count of {max_count} consultations. Stopping.")
                break

            url = consultation["url"]
            post_id = url.split("?p=")[-1] if "?p=" in url else None

            existing = None
            if not force_scrape and not fresh_db:
                existing = find_existing_consultation(
                    url=url,
                    post_id=post_id,
                    existing_by_url=existing_by_url,
                    existing_by_post_id=existing_by_post_id,
                )

            if existing and not force_scrape:
                if existing.is_finished:
                    logger.info(f"Skipping finished consultation: {url}")
                    skipped_count += 1
                else:
                    logger.info(f"Updating unfinished consultation {processed_count + 1}/{total_count}: {url}")
                    try:
                        result, changes = scrape_and_store(
                            url,
                            session,
                            selective_update=True,
                            existing_cons=existing,
                        )
                        if result:
                            success_count += 1

                            update_stats = []
                            if changes["new_comments"] > 0:
                                update_stats.append(f"+{changes['new_comments']} comments")
                            if changes["new_documents"] > 0:
                                update_stats.append(f"+{changes['new_documents']} documents")
                            if changes["total_comments_change"] != 0:
                                update_stats.append(f"{changes['total_comments_change']:+d} total comments")
                            if changes["start_message_changed"]:
                                update_stats.append("start minister message updated")
                            if changes["end_message_changed"]:
                                update_stats.append("end minister message updated")
                            if changes["status_change"]:
                                update_stats.append("status: unfinished → finished")

                            if update_stats:
                                stats_str = ", ".join(update_stats)
                                logger.info(f"Update summary for {existing.title}: {stats_str}")
                                update_reports.append((url, existing.title, changes))
                        else:
                            logger.warning(f"Failed to update unfinished consultation: {url}")
                    except Exception as e:
                        logger.error(f"Error updating unfinished consultation {url}: {e}")
                        session.rollback()
            else:
                logger.info(f"Processing consultation {processed_count + 1}/{total_count}: {url}")
                try:
                    result = scrape_and_store(url, session)
                    if result:
                        success_count += 1
                    else:
                        logger.warning(f"Skipping consultation that returned False: {url}")
                except Exception as e:
                    logger.error(f"Error processing consultation {url}: {e}")
                    session.rollback()

            processed_count += 1

            if processed_count % batch_size == 0:
                try:
                    logger.info(f"Committing batch of {batch_size} consultations")
                    session.commit()
                except Exception as e:
                    logger.error(f"Error in batch processing: {e}")
                    session.rollback()

            if i < len(consultation_links) - 1:
                delay = uniform(*REQUEST_DELAY)
                logger.info(f"Waiting {delay:.2f} seconds before next consultation...")
                time.sleep(delay)

        if processed_count % batch_size != 0:
            try:
                logger.info(f"Committing final batch of {processed_count % batch_size} consultations")
                session.commit()
            except Exception as e:
                logger.error(f"Error in final batch processing: {e}")
                session.rollback()

    except Exception as e:
        logger.error(f"Error in batch processing: {e}")
        session.rollback()
    finally:
        session.close()

    logger.info("=== Scraping Results ===")
    logger.info(f"Batch processing complete. Processed {processed_count} consultations.")
    logger.info(f"Success: {success_count}, Skipped: {skipped_count}")

    if update_reports:
        save_update_report(update_reports)

    logger.info(f"Failed: {processed_count - success_count - skipped_count}")
    logger.info("=======================")

    return success_count


def main():
    """Main function to scrape all consultations and store in DB."""
    parser = argparse.ArgumentParser(
        description="Scrape all consultations from OpenGov.gr and store in database"
    )

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    default_db_path = f"sqlite:///{os.path.join(project_root, 'deliberation_data_gr.db')}"

    parser.add_argument(
        "--db-path",
        type=str,
        default=default_db_path,
        help=f"Database URL (default: {default_db_path})",
    )
    parser.add_argument(
        "--start-page",
        type=int,
        default=1,
        help="Starting page number (default: 1)",
    )
    parser.add_argument(
        "--end-page",
        type=int,
        default=None,
        help="Ending page number (default: scrape all pages)",
    )
    parser.add_argument(
        "--max-count",
        type=int,
        default=None,
        help="Maximum number of consultations to scrape (default: all)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help="Commit to database after processing this many consultations (default: 10)",
    )
    parser.add_argument(
        "--force-scrape",
        action="store_true",
        help="Force scrape even if consultation already exists in database",
    )
    parser.add_argument(
        "--fresh-db",
        action="store_true",
        help="Skip existing-record checks; use only for a brand-new empty database",
    )

    args = parser.parse_args()

    logger.info("Starting mass consultation scraper")
    logger.info(f"Using database: {args.db_path}")
    logger.info(f"Fresh DB mode: {args.fresh_db}")

    consultation_links = get_all_consultation_links(args.start_page, args.end_page)
    consultation_links = dedupe_consultation_links(consultation_links)

    if not consultation_links:
        logger.error("No consultation links found to process")
        return

    success_count = scrape_consultations_to_db(
        consultation_links,
        args.db_path,
        batch_size=args.batch_size,
        max_count=args.max_count,
        force_scrape=args.force_scrape,
        fresh_db=args.fresh_db,
    )

    logger.info(f"Consultation scraping complete! Successfully stored {success_count} consultations.")


if __name__ == "__main__":
    main()