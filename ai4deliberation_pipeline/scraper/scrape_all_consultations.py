#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build and maintain the consultations database.

Steps, run in this order and combinable:
  listing (default)         scrape every consultation in the central listing;
  --repair                  re-fetch comments wherever the site shows more than the DB holds
                            (articles and consultation root posts);
  --discover-by-id MODE     scan post IDs for consultations the listing misses (see list_consultations.py):
                            'known' scans KNOWN_RANGES (~25k requests), 'stats-gaps' only the ID ranges around
                            consultations the site's statistics page lists but the DB lacks.
When --repair or --discover-by-id is given, the listing step runs only with --listing.
"""

import argparse
import logging
import re
from datetime import datetime

from sqlalchemy import func

from .content_scraper import (CONSULTATION_ROOT, extract_comments, fetch_page_soup, parse_article_nav,
                              scrape_article_content)
from .db_models import DEFAULT_DB_URL, Article, Comment, Consultation, init_db
from .db_population_report import match_site_rows, url_key
from .list_consultations import (KNOWN_RANGES, dedupe_consultation_links, duplicate_of_group,
                                 find_unlisted_consultations, get_all_consultations, ranges_for_stats_gaps,
                                 scan_post_ids)
from .scrape_single_consultation import attach_articles, find_existing_consultation, scrape_and_store, store_articles
from .utils import LOG_FORMAT, extract_post_id, opengov_article_url_variants, opengov_url_key, polite_sleep

logger = logging.getLogger(__name__)


def format_changes(changes):
    """Human-readable list of what a selective update changed."""
    stats = []
    if changes["new_comments"] > 0:
        stats.append(f"+{changes['new_comments']} comments")
    if changes["new_documents"] > 0:
        stats.append(f"+{changes['new_documents']} documents")
    if changes["total_comments_change"] != 0:
        stats.append(f"{changes['total_comments_change']:+d} total comments")
    if changes["start_message_changed"]:
        stats.append("start minister message updated")
    if changes["end_message_changed"]:
        stats.append("end minister message updated")
    if changes["status_change"]:
        stats.append("status: unfinished → finished")
    return stats


def save_update_report(update_reports, output_file="unfinished_consultation_updates.txt"):
    """Save the update reports to a file."""
    try:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write("# Update Report for Unfinished Consultations\n\n")
            f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            for url, title, changes in update_reports:
                f.write(f"## {title}\nURL: {url}\n\n")
                stats = format_changes(changes)
                if stats:
                    f.write("Changes:\n" + "".join(f"- {s}\n" for s in stats))
                else:
                    f.write("No changes detected\n")
                f.write("\n")
        logger.info(f"Update report saved to {output_file}")
        return True
    except Exception as e:
        logger.error(f"Error saving update report: {e}")
        return False


def scrape_consultations_to_db(consultation_links, db_url, batch_size=20, max_count=None, force_scrape=False, fresh_db=False):
    """Scrape consultations and store them; finished ones already stored are skipped, unfinished ones updated."""
    engine, Session = init_db(db_url)
    session = Session()

    total_count = len(consultation_links)
    processed_count = success_count = skipped_count = 0
    update_reports = []
    logger.info(f"Starting to scrape {total_count} consultations to database")
    if fresh_db:
        logger.info("Fresh DB mode enabled: skipping existing-record checks")

    try:
        for i, consultation in enumerate(consultation_links):
            if max_count and processed_count >= max_count:
                logger.info(f"Reached maximum count of {max_count} consultations. Stopping.")
                break

            url = consultation["url"]
            existing = None if (force_scrape or fresh_db) else find_existing_consultation(session, url)

            if existing and existing.is_finished:
                logger.info(f"Skipping finished consultation: {url}")
                skipped_count += 1
            elif existing:
                logger.info(f"Updating unfinished consultation {processed_count + 1}/{total_count}: {url}")
                try:
                    ok, changes = scrape_and_store(url, session, selective_update=True, existing_cons=existing)
                    if ok:
                        success_count += 1
                        stats = format_changes(changes)
                        if stats:
                            logger.info(f"Update summary for {existing.title}: {', '.join(stats)}")
                            update_reports.append((url, existing.title, changes))
                    else:
                        logger.warning(f"Failed to update unfinished consultation: {url}")
                except Exception as e:
                    logger.error(f"Error updating unfinished consultation {url}: {e}")
                    session.rollback()
            else:
                logger.info(f"Processing consultation {processed_count + 1}/{total_count}: {url}")
                try:
                    ok, _ = scrape_and_store(url, session)
                    if ok:
                        success_count += 1
                    else:
                        logger.warning(f"Consultation not stored: {url}")
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
                polite_sleep()

        session.commit()
    except Exception as e:
        logger.error(f"Error in batch processing: {e}")
        session.rollback()
    finally:
        session.close()

    logger.info("=== Scraping Results ===")
    logger.info(f"Processed {processed_count} consultations. Stored/updated: {success_count}, "
                f"skipped (finished): {skipped_count}, failed: {processed_count - success_count - skipped_count}")
    if update_reports:
        save_update_report(update_reports)
    return success_count


# --- repair ----------------------------------------------------------------------------------

def repair_consultation(session, consultation, dry_run=False):
    """Compare one consultation with the comment counts its page shows; re-fetch whatever the DB is short of."""
    soup, final_url = fetch_page_soup(consultation.url)
    if extract_post_id(final_url) != consultation.post_id:
        return [{'consultation': consultation.url, 'error': f'page redirects to {final_url}'}]
    stored = {}
    for article, n in session.query(Article, func.count(Comment.id)).outerjoin(Comment).filter(
            Article.consultation_id == consultation.id).group_by(Article.id):
        stored[url_key(article.url)] = n

    todo = []
    nav = parse_article_nav(soup, final_url)
    for entry in nav:
        key = url_key(entry['url'])
        if key in stored:
            if entry['site_comments'] is not None and entry['site_comments'] > stored[key]:
                todo.append({'url': entry['url'], 'reason': 'article short', 'site': entry['site_comments'], 'stored': stored[key]})
        elif not session.query(Article.id).filter(Article.url.in_(opengov_article_url_variants(entry['url']))).first():
            todo.append({'url': entry['url'], 'reason': 'article missing', 'site': entry['site_comments'], 'stored': 0})

    # Comments on the root post: the site shows no count for it, so count what its comment pages hold.
    root_key = url_key(consultation.url)
    if root_key not in {url_key(e['url']) for e in nav} and soup.select("li[id^='comment-']"):
        on_site = len(extract_comments(soup, final_url))
        if on_site > stored.get(root_key, 0):
            todo.append({'url': consultation.url, 'reason': 'root comments', 'site': on_site,
                         'stored': stored.get(root_key, 0), 'root': True})

    for item in todo:
        item['consultation'] = consultation.url
        if dry_run:
            continue
        polite_sleep()
        data = scrape_article_content(item['url'])
        if not data:
            item['error'] = 'scrape failed'
            continue
        if item.get('root') and not session.query(Article.id).filter(
                Article.url.in_(opengov_article_url_variants(consultation.url))).first():
            data['extraction_method'] = CONSULTATION_ROOT
        item['added_articles'], item['added_comments'] = store_articles(session, consultation, [data])
        session.commit()
    return todo


def repair_consultations(session, dry_run=False):
    """--repair: compare every consultation with its page's comment counts."""
    report = {'consultations': []}
    consultations = session.query(Consultation).order_by(Consultation.id).all()
    for i, consultation in enumerate(consultations, 1):
        try:
            found = repair_consultation(session, consultation, dry_run)
        except Exception as e:
            session.rollback()
            found = [{'consultation': consultation.url, 'error': str(e)}]
        report['consultations'].extend(found)
        for item in found:
            logger.info(f"[{i}/{len(consultations)}] {item}")
        if i % 100 == 0:
            logger.info(f"Repair progress: {i}/{len(consultations)}")
        polite_sleep()
    added = sum(item.get('added_comments', 0) for item in report['consultations'])
    unfixed = sum(1 for item in report['consultations'] if not dry_run and 'error' not in item
                  and not item.get('added_comments') and not item.get('added_articles'))
    logger.info(f"Repair: {len(report['consultations'])} findings, "
                f"{added} comments added{' (dry run)' if dry_run else f', {unfixed} findings with nothing to add'}")
    return report


# --- discovery by post ID --------------------------------------------------------------------

# A consultation whose root post is gone is stored from one of its articles; give it the stats-page title then.
ARTICLE_TITLE = re.compile(r'^\s*(Άρθρο|ΑΡΘΡΟ|ΚΕΦΑΛΑΙΟ|Κεφάλαιο|ΜΕΡΟΣ|Μέρος)\b')


def discover_unlisted(session, mode, scan_file, workers=4, dry_run=False, skip=()):
    """--discover-by-id: scan post IDs, then scrape (or attach) the consultations the DB lacks."""
    if mode == 'stats-gaps':
        ranges, stats_gaps = ranges_for_stats_gaps(session)
    else:
        ranges = KNOWN_RANGES
        pairs, site_only, _ = match_site_rows(session)
        stats_gaps = (site_only, pairs)
    scan_post_ids(ranges, scan_file, workers)
    groups = find_unlisted_consultations(scan_file, session, stats_gaps)
    logger.info(f"Found {len(groups)} consultations missing from the DB")

    skip_keys = {url_key(u) for u in skip}
    report = []
    for g in groups:
        skipped = bool(skip_keys & {url_key(f"{g['url'].split('?')[0]}?p={p}") for p in g['members']})
        duplicate, comments = (None, 0) if skipped else duplicate_of_group(session, g)
        g['duplicate_of'] = opengov_url_key(duplicate.url) if duplicate is not None else None
        note = ''
        if duplicate is not None and comments:
            # a second posting with comments of its own: store it, so no comments are lost
            note = f" (same articles as {g['duplicate_of']}, but {comments} comments of its own: review)"
            logger.warning(f"{g['url']}{note}")
        if skipped:
            g['action'] = 'skipped (--skip)'
        elif duplicate is not None and not comments:
            g['action'] = f"{'would skip' if dry_run else 'skipped'}: same articles as {g['duplicate_of']}, no comments"
        elif dry_run:
            g['action'] = f"would attach to {g['attach_to']}" if g['attach_to'] else 'would scrape'
        elif g['attach_to']:
            target = session.query(Consultation).filter_by(url=g['attach_to']).one()
            g['articles'], g['comments'] = attach_articles(session, target, g['url'])
            g['action'] = f"attached to {g['attach_to']}"
        else:
            ok, cid = scrape_and_store(g['url'], session)
            g['action'] = 'scraped' if ok else 'failed'
            g['consultation_id'] = cid
            consultation = session.get(Consultation, cid) if ok else None
            if consultation is not None and g.get('root_is_article'):
                # the root is one of the articles, and its own navigation omits it
                data = scrape_article_content(g['url'])
                if data:
                    store_articles(session, consultation, [data])
            if consultation is not None and g.get('stats_title') and ARTICLE_TITLE.match(consultation.title or ''):
                consultation.title = g['stats_title']
            session.commit()
            polite_sleep()
        g['action'] += note
        logger.info(f"{g['url']} articles={g['n_articles']} comments_seen={g['comments_seen']} "
                    f"stats_row={g['stats_row']}: {g['action']}")
        report.append(g)
    return {'ranges': {s: list(v) if isinstance(v, list) else [v] for s, v in ranges.items()}, 'consultations': report}


def main():
    """Main function to scrape all consultations and store in DB."""
    parser = argparse.ArgumentParser(description="Build and maintain the OpenGov.gr consultations database",
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("--db-path", type=str, default=DEFAULT_DB_URL, help=f"Database URL (default: {DEFAULT_DB_URL})")
    listing = parser.add_argument_group("listing step")
    listing.add_argument("--listing", action="store_true", help="Run the listing step together with --repair/--discover-by-id")
    listing.add_argument("--start-page", type=int, default=1, help="Starting page number (default: 1)")
    listing.add_argument("--end-page", type=int, default=None, help="Ending page number (default: scrape all pages)")
    listing.add_argument("--max-count", type=int, default=None, help="Maximum number of consultations to scrape (default: all)")
    listing.add_argument("--batch-size", type=int, default=10, help="Commit after this many consultations (default: 10)")
    listing.add_argument("--force-scrape", action="store_true", help="Force scrape even if consultation already exists in database")
    listing.add_argument("--fresh-db", action="store_true", help="Skip existing-record checks; use only for a brand-new empty database")
    steps = parser.add_argument_group("repair and discovery steps")
    steps.add_argument("--repair", action="store_true", help="Re-fetch comments the DB is short of")
    steps.add_argument("--discover-by-id", choices=["known", "stats-gaps"], help="Scan post IDs for consultations the listing misses")
    steps.add_argument("--id-scan-file", default="id_scan.jsonl", help="Scan results (JSONL); an existing file is resumed (default: id_scan.jsonl)")
    steps.add_argument("--workers", type=int, default=4, help="Parallel requests while scanning post IDs (default: 4)")
    steps.add_argument("--skip", nargs="+", metavar="URL", help="With --discover-by-id: do not store the candidates containing these posts (e.g. after a --dry-run)")
    steps.add_argument("--dry-run", action="store_true", help="Report what --repair/--discover-by-id would change, without changing the DB")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    logger.info(f"Using database: {args.db_path}")

    if args.listing or not (args.repair or args.discover_by_id):
        links = dedupe_consultation_links(get_all_consultations(args.start_page, args.end_page))
        if not links:
            logger.error("No consultation links found to process")
        else:
            stored = scrape_consultations_to_db(links, args.db_path, batch_size=args.batch_size, max_count=args.max_count,
                                                force_scrape=args.force_scrape, fresh_db=args.fresh_db)
            logger.info(f"Listing step complete: {stored} consultations stored or updated")

    if args.repair or args.discover_by_id:
        _, Session = init_db(args.db_path)
        session = Session()
        try:
            if args.repair:
                repair_consultations(session, args.dry_run)
            if args.discover_by_id:
                discover_unlisted(session, args.discover_by_id, args.id_scan_file, args.workers, args.dry_run,
                                  args.skip or ())
        finally:
            session.close()


if __name__ == "__main__":
    main()
