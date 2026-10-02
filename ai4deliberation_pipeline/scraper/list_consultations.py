#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Consultation discovery: everything that finds consultation URLs on archive.opengov.gr.

1. The central listing (home/category/consultations): get_all_consultations(), or as a script that
   writes the listing to a CSV (`--update` only fetches entries newer than the CSV's latest date).

2. Post-ID scanning, for consultations the listing does not reach. The listing links most 2009-2011
   consultations through dead '?option=...' or bare ministry URLs, lists a few entries with no link at
   all, and omits some later ones. The posts still exist under '?p=<id>', and an ID that does not exist
   redirects to the ministry homepage, so scanning IDs finds them:
   - scan_post_ids(): fetch every '?p=<id>' in the given ranges, recording each post's article navigation;
   - find_unlisted_consultations(): a post absent from the DB whose navigation lists articles none of
     which are in the DB is the root of a missing consultation (posts listing mostly the same articles
     are merged; the root is the one listing the most, since an article's navigation omits itself).
   KNOWN_RANGES holds the ranges that contained such consultations; ranges_for_stats_gaps() derives
   ranges from the consultations that the site's statistics page lists but the DB lacks.
"""

import argparse
import csv
import json
import logging
import os
import re
import threading
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .content_scraper import parse_article_nav, scrape_article_content
from .db_models import Article, Consultation
from .db_population_report import is_placeholder_title, match_site_rows, norm_title
from .metadata_scraper import scrape_consultation_metadata
from .utils import (LOG_FORMAT, OPENGOV_HOSTS, extract_ministry_code_from_url, extract_post_id, http_get,
                    normalize_consultation_url, parse_greek_date, polite_sleep, resolve_announcement_url,
                    strip_default_port)

logger = logging.getLogger(__name__)

BASE_URL = "https://archive.opengov.gr/home/category/consultations"
ARCHIVE = "https://archive.opengov.gr"
OUTPUT_CSV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "all_consultations.csv")


# --- the central listing ---------------------------------------------------------------------

def get_consultation_links_from_page(url, latest_known_date=None):
    """Consultation links on one listing page, and the next page's URL (None when done or past the cutoff)."""
    logger.info(f"Fetching consultation links from: {url}")
    try:
        soup = BeautifulSoup(http_get(url).content, 'html.parser')
    except Exception as e:
        logger.error(f"Error fetching page {url}: {e}")
        return [], None

    content_div = soup.find('div', class_='downspace_item_content archive_list')
    if not content_div:
        logger.error(f"Could not find consultation listings div on page: {url}")
        return [], None

    consultations = []
    for item in content_div.find_all('li'):
        try:
            link_element = None
            for candidate in [
                item.find('a'),
                item.find('p').find('a') if item.find('p') else None,
                item.find('h2').find('a') if item.find('h2') else None,
                item.find('h3').find('a') if item.find('h3') else None,
            ]:
                if candidate and candidate.has_attr('href') and candidate.get_text(strip=True):
                    link_element = candidate
                    break

            if not link_element:
                # A few entries have a title but no link; post-ID scanning finds those consultations.
                logger.warning(f"Listing entry without a link: {item.get_text(strip=True)[:100]}...")
                continue

            consultation_url = resolve_announcement_url(urljoin(url, link_element['href'].strip()))
            consultation_title = link_element.get_text(strip=True)
            if not normalize_consultation_url(consultation_url) or \
                    not any(f"//{host}/" in consultation_url for host in OPENGOV_HOSTS):
                logger.warning(f"Skipping URL with unexpected host/structure: {consultation_url} (Title: {consultation_title})")
                continue

            date_span = item.find('span', class_='start')
            consultation_date_str = date_span.get_text(strip=True) if date_span else ""
            if latest_known_date and consultation_date_str:
                parsed = parse_greek_date(consultation_date_str)
                if parsed and parsed <= latest_known_date:
                    logger.info(f"Reached '{consultation_title}' ({parsed.date()}), not newer than {latest_known_date.date()}; stopping.")
                    return consultations, None

            consultations.append({'url': consultation_url, 'title': consultation_title, 'date': consultation_date_str})
        except Exception as e:
            logger.error(f"Error extracting consultation details: {e}")

    next_page_url = None
    pagination = soup.find('div', class_='wp-pagenavi')
    if pagination:
        next_link = pagination.find('a', class_='nextpostslink')
        if next_link and next_link.has_attr('href'):
            next_page_url = next_link['href']
            logger.info(f"Found next page link: {next_page_url}")
    return consultations, next_page_url


def get_all_consultations(start_page=1, end_page=None, latest_known_date=None):
    """Consultation links from listing pages start_page..end_page, newest first, stopping at latest_known_date."""
    all_consultations = []
    current_url = BASE_URL
    page_number = 1

    while page_number < start_page and current_url:
        _, current_url = get_consultation_links_from_page(current_url)
        page_number += 1
    if not current_url:
        logger.error(f"Could not navigate to page {start_page}")
        return []

    while current_url and not (end_page and page_number > end_page):
        logger.info(f"Processing page {page_number}")
        consultations, next_page_url = get_consultation_links_from_page(current_url, latest_known_date)
        if consultations:
            logger.info(f"Found {len(consultations)} consultations on page {page_number}")
            all_consultations.extend(consultations)
        current_url = next_page_url
        page_number += 1
        if current_url:
            polite_sleep()

    logger.info(f"Total consultation links found: {len(all_consultations)}")
    return all_consultations


def dedupe_consultation_links(consultation_links):
    """Deduplicate consultation links by normalized URL, preserving order."""
    seen = set()
    deduped = []
    for c in consultation_links:
        norm = normalize_consultation_url(c.get("url"))
        if norm and norm not in seen:
            seen.add(norm)
            deduped.append(c)
    logger.info(f"Deduplicated consultations: {len(consultation_links)} -> {len(deduped)}")
    return deduped


def latest_date_in_csv(csv_path):
    """Latest listing date in a CSV written by this script, or None."""
    if not os.path.exists(csv_path):
        return None
    with open(csv_path, newline='', encoding='utf-8') as f:
        dates = [d for d in (parse_greek_date(row.get('date')) for row in csv.DictReader(f)) if d]
    return max(dates) if dates else None


def write_consultations_to_csv(consultations, csv_path):
    """Write consultation data to a CSV file"""
    try:
        with open(csv_path, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=['url', 'title', 'date'])
            writer.writeheader()
            for consultation in consultations:
                writer.writerow(consultation)
        logger.info(f"Successfully wrote {len(consultations)} consultations to {csv_path}")
        return True
    except Exception as e:
        logger.error(f"Error writing to CSV file: {e}")
        return False


def analyze_consultations(consultations):
    """Analyze the consultations data for completeness"""
    total_count = len(consultations)
    missing_url = sum(1 for c in consultations if not c.get('url'))
    missing_title = sum(1 for c in consultations if not c.get('title'))
    missing_date = sum(1 for c in consultations if not c.get('date'))

    logger.info("=== Consultation Data Analysis ===")
    logger.info(f"Total consultations: {total_count}")
    logger.info(f"Missing URLs: {missing_url} ({missing_url/total_count*100:.2f}%)")
    logger.info(f"Missing titles: {missing_title} ({missing_title/total_count*100:.2f}%)")
    logger.info(f"Missing dates: {missing_date} ({missing_date/total_count*100:.2f}%)")
    logger.info("================================")


# --- post-ID scanning ------------------------------------------------------------------------

# ID ranges that held consultations missing from the listing (scanned 2026-10): each ministry's
# 2009-2011 posts, plus the ranges of the link-less ERT (2013), defence (2016) and epy (2016) entries.
KNOWN_RANGES = {
    'consultations': (1, 1438), 'ggk': (1, 60), 'minenv': (1, 3227), 'minfin': (1, 1053),
    'ministryofjustice': (1, 1784), 'minlab': (1, 2332), 'minreform': (1, 171), 'tourism': (1, 590),
    'yme': (1, 2294), 'ynanp': (1, 107), 'ypaat': (1, 463), 'ypep': (1, 161), 'ypepth': (1, 1113),
    'ypes': (1, 1300), 'ypoian': (1, 2584), 'yptp': (1, 689), 'yyka': (1, 728),
    'ert': (1, 4146), 'mindefence': (5148, 5600), 'epy': (5950, 6400),
}


def _as_range_lists(ranges):
    return {slug: spans if isinstance(spans, list) else [spans] for slug, spans in ranges.items()}


def probe_post(slug, post_id):
    """One post's title, article-navigation IDs and visible comment count; None if it does not exist."""
    url = f"{ARCHIVE}/{slug}/?p={post_id}"
    try:
        response = http_get(url)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        return {'slug': slug, 'p': post_id, 'error': str(e)}
    except Exception as e:
        return {'slug': slug, 'p': post_id, 'error': str(e)}
    if f"p={post_id}" not in response.url:
        return None
    final_url = strip_default_port(response.url)
    soup = BeautifulSoup(response.content, 'html.parser')
    title = soup.select_one('h3') or soup.select_one('h2')
    nav = {int(e['post_id']) for e in parse_article_nav(soup, final_url) if e['post_id']}
    return {'slug': slug, 'p': post_id, 'url': final_url,
            'title': title.get_text(' ', strip=True)[:200] if title else '',
            'nav': sorted(nav), 'comments_on_page': len(soup.select("li[id^='comment-']"))}


def scan_post_ids(ranges, out_path, workers=4):
    """Probe every ID in `ranges` ({slug: (start, end) or [(start, end), ...]}), appending one JSONL line per ID
    to `out_path` (nonexistent ones as {'missing': true}). IDs already in the file are skipped, so a scan resumes."""
    done = set()
    if os.path.exists(out_path):
        with open(out_path) as f:
            done = {(r['slug'], r['p']) for r in map(json.loads, f) if 'error' not in r}
    jobs = sorted({(slug, p) for slug, spans in _as_range_lists(ranges).items() for a, b in spans
                   for p in range(a, b + 1)} - done)
    logger.info(f"Scanning {len(jobs)} post IDs ({len(done)} already in {out_path})")
    lock = threading.Lock()

    def task(job):
        result = probe_post(*job) or {'slug': job[0], 'p': job[1], 'missing': True}
        polite_sleep((0.05, 0.15))
        with lock, open(out_path, 'a') as f:
            f.write(json.dumps(result, ensure_ascii=False) + '\n')

    with ThreadPoolExecutor(workers) as ex:
        for i, _ in enumerate(ex.map(task, jobs), 1):
            if i % 1000 == 0:
                logger.info(f"Scanned {i}/{len(jobs)}")


def _post_ids_in_db(session):
    known = defaultdict(set)
    for (url,) in session.query(Consultation.url).union_all(session.query(Article.url)):
        post_id = extract_post_id(url)
        if post_id:
            known[extract_ministry_code_from_url(url)].add(int(post_id))
    return known


def _slugs_by_stats_ministry(pairs):
    """Stats-page ministry name -> DB ministry slugs, learned from matched consultations."""
    seen = defaultdict(Counter)
    for site_row, db_row in pairs:
        seen[site_row['ministry']][db_row['slug']] += 1
    return {ministry: set(slugs) for ministry, slugs in seen.items()}


def _matches_stats_row(group, members, rows, slugs_by_ministry, windows=()):
    """A stats-page row (of a consultation missing from the DB) that this group corresponds to: same ministry,
    article count within 2, and a title in common or the group inside the ID window scanned for that row."""
    titles = [norm_title(m['title'])[:80] for m in members]
    for row in rows:
        if group['slug'] not in slugs_by_ministry.get(row['ministry'], ()) or abs(row['articles'] - group['n_articles']) > 2:
            continue
        target = norm_title(row['title'])[:80]
        if target and any(SequenceMatcher(None, target, t).find_longest_match(0, len(target), 0, len(t)).size
                          / len(target) >= 0.5 for t in titles if t):
            return row
    for slug, (low, high), row in windows:
        if slug == group['slug'] and low <= group['root'] <= high and row['articles'] > 0 \
                and abs(row['articles'] - group['n_articles']) <= 2:
            return row
    return None


def _empty_consultation_for(session, slug, title):
    """A stored consultation of the same ministry, with the same title and no articles, if there is one."""
    target = norm_title(title)[:60]
    for cons in session.query(Consultation).filter(Consultation.url.like(f"%/{slug}/?p=%")):
        if norm_title(cons.title)[:60] == target and not session.query(Article).filter_by(consultation_id=cons.id).first():
            return cons
    return None


# --- duplicate postings ----------------------------------------------------------------------
# A bill is sometimes posted twice under one ministry, e.g. an early posting whose root post was removed
# (minenv/?p=13997-13999) next to the consultation that ran (minenv/?p=14078). Post IDs repeat across
# ministries, so postings are compared only within the same ministry slug. Short boilerplate articles
# ("Πατήστε εδώ για να κατεβάσετε το αρχείο") are shared by unrelated consultations, so a match also needs
# MIN_SHARED_CHARS of common text.

MIN_SHARED_CHARS = 500

def _title_key(title):
    return norm_title(title)[:150]


def _text_lines(html):
    """Normalized non-empty text lines of an article body."""
    text = unicodedata.normalize('NFC', BeautifulSoup(html or '', 'html.parser').get_text('\n'))
    return {line for line in (re.sub(r'\s+', ' ', raw).strip() for raw in text.split('\n')) if line}


def _shared_text(candidate, stored, threshold):
    """The text lines two articles share, when they have the same title and at least `threshold` of the shorter
    text's lines appear in the other one; None when they differ."""
    if _title_key(candidate['title']) != _title_key(stored.title):
        return None
    a, b = _text_lines(candidate.get('raw_html')), _text_lines(stored.raw_html)
    if not a or not b:
        return set() if not a and not b else None
    return a & b if len(a & b) / min(len(a), len(b)) >= threshold else None


def _consultations_with_titles(session, slug, titles, exclude_id=None):
    """{consultation id: [article ids]} of the slug's stored articles whose title is in `titles`."""
    keys = {_title_key(t) for t in titles if t}
    found = defaultdict(list)
    for article_id, title, consultation_id, url in session.query(
            Article.id, Article.title, Article.consultation_id, Consultation.url).join(Consultation).filter(
            Consultation.url.like(f"%/{slug}/?p=%")):
        if consultation_id != exclude_id and extract_ministry_code_from_url(url) == slug and _title_key(title) in keys:
            found[consultation_id].append(article_id)
    return found


def find_duplicate_consultation(session, slug, articles, start=None, end=None, exclude_id=None, threshold=0.9):
    """A stored consultation of the same ministry slug that already holds every one of `articles`
    ([{'title', 'raw_html'}]) with the same title and (nearly) the same text, at least MIN_SHARED_CHARS of it in
    all, and whose consultation period overlaps start..end when both are known. It may hold more articles.
    Returns the Consultation or None."""
    if not articles:
        return None
    for consultation_id, article_ids in _consultations_with_titles(
            session, slug, [a['title'] for a in articles], exclude_id).items():
        stored = session.query(Article).filter(Article.id.in_(article_ids)).all()
        shared = []
        for a in articles:
            match = next((t for t in (_shared_text(a, s, threshold) for s in stored) if t is not None), None)
            if match is None:
                break
            shared.append(sum(map(len, match)))
        if len(shared) < len(articles) or sum(shared) < MIN_SHARED_CHARS:
            continue
        consultation = session.get(Consultation, consultation_id)
        if start and end and consultation.start_date and consultation.end_date and \
                (start > consultation.end_date or end < consultation.start_date):
            continue
        return consultation
    return None


def duplicate_of_group(session, group):
    """For a group from find_unlisted_consultations: (stored consultation that already holds its articles or None,
    number of comments on the group's articles). Fetches the group's pages only when a title matches."""
    if not _consultations_with_titles(session, group['slug'], group.get('article_titles', [])):
        return None, group['comments_seen']
    articles = []
    for post_id in group['article_ids']:
        polite_sleep()
        data = scrape_article_content(f"{ARCHIVE}/{group['slug']}/?p={post_id}")
        if data:
            articles.append(data)
    consultation = (scrape_consultation_metadata(group['url']) or {}).get('consultation') or {}
    duplicate = find_duplicate_consultation(session, group['slug'], articles,
                                            consultation.get('start_date'), consultation.get('end_date'))
    return duplicate, sum(len(a['comments']) for a in articles)


def find_unlisted_consultations(scan_path, session, stats_gaps=None):
    """Consultations in a post-ID scan that are absent from the DB.

    Returns [{slug, root, url, title, n_articles, comments_seen, attach_to, stats_row}]: 'attach_to' is the URL of a
    stored consultation without articles that the group's articles belong to (its own page lists none).
    Groups in which no article shows a comment are kept only when they match a stats-page row of a consultation
    missing from the DB (`stats_gaps` = (site-only rows, pairs[, scan windows]) from match_site_rows or
    ranges_for_stats_gaps); otherwise they are report pages whose navigation lists unrelated posts.
    Some consultations have lost their root post and only their articles remain, each listing the others: the
    'root' is then one of the articles ('root_is_article'), and 'stats_title' gives the consultation's title.
    """
    posts = defaultdict(dict)
    with open(scan_path) as f:
        for r in map(json.loads, f):
            if 'error' in r:
                logger.warning(f"Scan error, re-run the scan to retry: {r}")
            elif not r.get('missing'):
                posts[r['slug']][r['p']] = r
    known = _post_ids_in_db(session)
    site_only, pairs, *rest = stats_gaps or ([], [])
    windows = rest[0] if rest else ()
    gap_rows = [r for r in site_only if not is_placeholder_title(r['title'])]
    slugs_by_ministry = _slugs_by_stats_ministry(pairs)

    groups = []
    for slug, ps in posts.items():
        K = known[slug]
        candidates = [r for p, r in ps.items()
                      if p not in K and r['nav'] and p not in r['nav'] and not set(r['nav']) & K]
        parent = {r['p']: r['p'] for r in candidates}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        # Same consultation: one lists the other, or their navigation lists mostly the same articles.
        for i, a in enumerate(candidates):
            for b in candidates[i + 1:]:
                A, B = set(a['nav']), set(b['nav'])
                if a['p'] in B or b['p'] in A or len(A & B) / len(A | B) >= 0.5:
                    parent[find(a['p'])] = find(b['p'])
        members = defaultdict(list)
        for r in candidates:
            members[find(r['p'])].append(r)

        for group_members in members.values():
            root = sorted(group_members, key=lambda r: (-len(r['nav']), r['p']))[0]
            root_is_article = any(root['p'] in m['nav'] for m in group_members)
            articles = set(root['nav']) | ({root['p']} if root_is_article else set())
            group = {'slug': slug, 'root': root['p'], 'url': root['url'], 'title': root['title'],
                     'members': sorted(m['p'] for m in group_members), 'article_ids': sorted(articles),
                     'article_titles': [ps[a]['title'] for a in sorted(articles) if ps.get(a, {}).get('title')],
                     'n_articles': len(articles), 'root_is_article': root_is_article,
                     'comments_seen': sum(ps.get(a, {}).get('comments_on_page', 0) for a in articles)}
            target = _empty_consultation_for(session, slug, root['title'])
            group['attach_to'] = target.url if target else None
            row = _matches_stats_row(group, group_members, gap_rows, slugs_by_ministry, windows)
            group['stats_row'] = f"{row['start'].date() if row['start'] else '?'} {row['title'][:80]}" if row else None
            group['stats_title'] = row['title'] if row else None
            if group['comments_seen'] or group['attach_to'] or group['stats_row']:
                groups.append(group)
            else:
                logger.info(f"Ignoring {root['url']} ({root['title'][:60]}): no comments and no stats-page row")
    return groups


def ranges_for_stats_gaps(session, margin=50):
    """Post-ID ranges to scan for the consultations the stats page lists but the DB lacks.

    For each such row (TEST/ΔΟΚΙΜΗ placeholders and rows with no articles and no comments are skipped), scan
    each ministry slug the row's stats-page ministry maps to, between the root IDs of that slug's DB
    consultations that started just before and just after it.
    Returns ({slug: [(start, end)]}, (site-only rows, pairs, [(slug, (start, end), stats row)])).
    """
    pairs, site_only, _ = match_site_rows(session)
    slugs_by_ministry = _slugs_by_stats_ministry(pairs)
    by_slug = defaultdict(list)
    for cons in session.query(Consultation):
        post_id = extract_post_id(cons.url)
        if post_id and cons.start_date:
            by_slug[extract_ministry_code_from_url(cons.url)].append((cons.start_date, int(post_id)))
    ranges = defaultdict(list)
    windows = []
    for row in site_only:
        if is_placeholder_title(row['title']) or not (row['articles'] > 0 or row['approved'] > 0) or not row['start']:
            continue
        for slug in slugs_by_ministry.get(row['ministry'], ()):
            before = [p for d, p in by_slug[slug] if d < row['start']]
            after = [p for d, p in by_slug[slug] if d >= row['start']]
            if not before and not after:
                continue
            low = max(before) if before else min(after) - 1000
            high = min(after) if after else max(before) + 1000
            low, high = sorted((low, high))
            ranges[slug].append((max(1, low - margin), high + margin))
            windows.append((slug, ranges[slug][-1], row))
            logger.info(f"Gap {row['start'].date()} '{row['title'][:50]}': scan {slug} {ranges[slug][-1]}")
    merged = {}
    for slug, spans in ranges.items():
        spans.sort()
        out = [list(spans[0])]
        for a, b in spans[1:]:
            if a <= out[-1][1] + 1:
                out[-1][1] = max(out[-1][1], b)
            else:
                out.append([a, b])
        merged[slug] = [tuple(s) for s in out]
    return merged, (site_only, pairs, windows)


def main():
    """Write the consultation listing to a CSV."""
    parser = argparse.ArgumentParser(description="List consultations from opengov.gr into a CSV")
    parser.add_argument("--update", action="store_true",
                        help="Only fetch consultations newer than the latest one already in the CSV")
    parser.add_argument("--output", default=OUTPUT_CSV, help=f"CSV path (default: {OUTPUT_CSV})")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)

    latest = latest_date_in_csv(args.output) if args.update else None
    if args.update:
        logger.info(f"Update mode: latest consultation date in {args.output} is {latest}")
    consultations = get_all_consultations(latest_known_date=latest)
    if consultations:
        analyze_consultations(consultations)
        write_consultations_to_csv(consultations, args.output)
        logger.info("Consultation listing complete!")
    else:
        logger.error("No consultations were found.")


if __name__ == "__main__":
    main()
