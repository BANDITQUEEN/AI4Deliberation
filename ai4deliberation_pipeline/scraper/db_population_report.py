#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Reports on a scraped database.

- default: how populated each field is;
- --integrity: duplicates, misfiled articles, count mismatches and other structural problems;
- --site-stats: comparison with opengov's own per-consultation statistics (StatsPerMinistry.php).

The stats page lists every consultation with its article count and approved/total comments; the
homepage "Το OpenGov σε Αριθμούς" widget follows the approved column. It omits a few newer ministry
blogs (MINISTRIES_NOT_ON_STATS_PAGE), and its HTML leaves <tr> unclosed, so rows are split with a regex.
"""

import argparse
import html
import os
import pprint
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from difflib import SequenceMatcher

from sqlalchemy import text

from .db_models import DEFAULT_DB_URL, init_db, Ministry, Consultation, Article, Comment, Document
from .utils import extract_ministry_code_from_url, http_get, opengov_url_key

STATS_URL = "https://archive.opengov.gr/opengov/StatsPerMinistry.php"

# Ministry blogs that the stats page does not list (checked 2026-10)
MINISTRIES_NOT_ON_STATS_PAGE = ('civilprotection', 'koinsynoik', 'ggee', 'pste')


def count_empty_or_default(session, model, fields, defaults=None):
    if defaults is None:
        defaults = {}
    results = {}
    total = session.query(model).count()
    results['total'] = total
    for field in fields:
        col = getattr(model, field)
        null_count = session.query(model).filter(col == None).count()
        empty_count = session.query(model).filter(col == '').count()
        default_count = 0
        if field in defaults:
            default_val = defaults[field]
            default_count = session.query(model).filter(col == default_val).count()
        results[field] = {
            'null': null_count,
            'empty': empty_count,
            'default': default_count,
            'populated': total - null_count - empty_count - default_count
        }
    return results


def population_report(session):
    schema = {
        Ministry: ['code', 'name', 'url'],
        Consultation: ['post_id', 'title', 'start_minister_message', 'end_minister_message', 'start_date', 'end_date', 'is_finished', 'url', 'total_comments', 'accepted_comments', 'ministry_id'],
        Article: ['post_id', 'title', 'content', 'url', 'consultation_id'],
        Comment: ['comment_id', 'username', 'date', 'content', 'article_id'],
        Document: ['title', 'url', 'type', 'consultation_id']
    }
    defaults = {
        'is_finished': False,
        'total_comments': 0,
        'accepted_comments': 0,
        'username': 'Anonymous',
        'comment_id': '',
        'content': '',
        'type': 'unknown',
        'title': '',
        'url': '',
    }

    grand_results = {}
    for model, fields in schema.items():
        try:
            grand_results[model.__tablename__] = count_empty_or_default(session, model, fields, defaults)
            print(f"Analyzed table: {model.__tablename__}")
        except Exception as e:
            print(f"Error analyzing {model.__tablename__}: {str(e)}")

    print('\n=== Field Population Report ===')
    total_entities = 0
    total_attributes = 0
    populated_attributes = 0
    for model, fields in schema.items():
        model_name = model.__tablename__
        if model_name not in grand_results:
            continue

        entity_count = grand_results[model_name]['total']
        total_entities += entity_count
        for field in fields:
            total_attributes += entity_count
            populated = grand_results[model_name][field]['populated']
            populated_attributes += populated
            print(f"{model_name}.{field}: {populated}/{entity_count} populated")

    print(f'\nTotal entities: {total_entities}')
    print(f'Total attributes: {total_attributes}')
    print(f'Populated attributes: {populated_attributes}')
    if total_attributes > 0:
        print(f'Population rate: {populated_attributes/total_attributes*100:.1f}%')
    else:
        print('Population rate: N/A')

    print('\nDetailed field stats:')
    pprint.pprint(grand_results)


# --- integrity -------------------------------------------------------------------------------

def url_key(url):
    """opengov_url_key without the '#comments' fragment or a trailing slash."""
    return (opengov_url_key(url) or '').split('#')[0].rstrip('/')


def find_misfiled_articles(session):
    """Articles whose URL is the root post of a different consultation: [(article, owning consultation)]."""
    roots = {url_key(c.url): c for c in session.query(Consultation)}
    misfiled = []
    for article in session.query(Article):
        owner = roots.get(url_key(article.url))
        if owner is not None and owner.id != article.consultation_id:
            misfiled.append((article, owner))
    return misfiled


def integrity_report(session):
    """{check: number of offending rows}; every value should be 0."""
    q = lambda sql: session.execute(text(sql)).scalar()
    cons_keys = Counter(url_key(u) for (u,) in session.query(Consultation.url))
    art_keys = Counter(url_key(u) for (u,) in session.query(Article.url))
    in_blog = Counter((extract_ministry_code_from_url(u), cid) for cid, u in session.execute(text(
        "select m.comment_id, a.url from comments m join articles a on a.id = m.article_id")))
    return {
        'duplicate consultation URLs': sum(n > 1 for n in cons_keys.values()),
        'duplicate article URLs': sum(n > 1 for n in art_keys.values()),
        'duplicate (comment_id, article_id)': q(
            "select count(*) from (select 1 from comments group by comment_id, article_id having count(*) > 1)"),
        'comment_id repeated within a ministry blog': sum(n > 1 for n in in_blog.values()),
        'articles filed under another consultation': len(find_misfiled_articles(session)),
        "usernames other than 'ANONYMIZED'": q("select count(*) from comments where username != 'ANONYMIZED'"),
        'empty comments': q("select count(*) from comments where trim(coalesce(content, '')) = ''"),
        'accepted_comments mismatches': q(
            "select count(*) from consultations c where coalesce(accepted_comments, -1) != "
            "(select count(*) from comments m join articles a on a.id = m.article_id where a.consultation_id = c.id)"),
        'consultations without articles': q(
            "select count(*) from consultations c where not exists (select 1 from articles a where a.consultation_id = c.id)"),
    }


# --- site statistics -------------------------------------------------------------------------

def norm_title(value):
    value = unicodedata.normalize('NFD', value or '').lower()
    value = ''.join(ch for ch in value if unicodedata.category(ch) != 'Mn')
    return re.sub(r'\W+', ' ', value).strip()


def is_placeholder_title(title):
    """TEST / ΔΟΚΙΜΗ / template entries left on the stats page."""
    t = norm_title(title)
    return 'test' in t or 'tset' in t or 'δοκιμ' in t or 'συντομος τιτλος' in t


def _parse_stats_date(value):
    for fmt in ('%d-%m-%y', '%d-%m-%Y'):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return None


def site_rows():
    """Every consultation on the stats page: [{start, articles, approved, total, ministry, title}]."""
    page = http_get(STATS_URL).content.decode('utf-8', 'replace')
    rows = []
    for chunk in re.split(r'<tr[^>]*>', page, flags=re.I)[1:]:
        cells = [html.unescape(re.sub(r'<[^>]+>', '', c)).strip()
                 for c in re.findall(r'<t[dh][^>]*>(.*?)(?=<t[dh]|</tr|<tr|$)', chunk, flags=re.S | re.I)]
        if len(cells) == 8 and cells[0].isdigit():
            num = lambda x: int(x) if x.lstrip('-').isdigit() else 0
            rows.append({'start': _parse_stats_date(cells[2]), 'articles': num(cells[4]), 'approved': num(cells[5]),
                         'total': num(cells[6]), 'ministry': cells[7], 'title': cells[1]})
    return rows


def consultation_rows(session):
    """Every DB consultation with its ministry slug, article count (root-comment articles excluded) and comment count."""
    rows = []
    for cid, url, start, title, message, n_articles, n_comments in session.execute(text("""
            select c.id, c.url, c.start_date, c.title, coalesce(c.start_minister_message, ''),
                   (select count(*) from articles a where a.consultation_id = c.id
                       and coalesce(a.extraction_method, '') != 'consultation_root'),
                   (select count(*) from comments m join articles a on a.id = m.article_id where a.consultation_id = c.id)
            from consultations c""")):
        if isinstance(start, str):
            start = datetime.strptime(start[:10], '%Y-%m-%d')
        rows.append({'id': cid, 'url': url, 'slug': extract_ministry_code_from_url(url), 'start': start,
                     'title': title, 'text': norm_title(f"{title} {message[:600]}"),
                     'articles': n_articles, 'comments': n_comments})
    return rows


def _title_overlap(site_row, db_row):
    title = norm_title(site_row['title'])[:80]
    if not title:
        return 0
    match = SequenceMatcher(None, title, db_row['text']).find_longest_match(0, len(title), 0, len(db_row['text']))
    return match.size / len(title)


# Each pass pairs remaining rows that start within a few days; a lower score is a better pair.
MATCH_PASSES = (
    lambda s, d, days: (days, abs(d['articles'] - s['articles']))
        if days <= 3 and s['approved'] > 0 and s['approved'] == d['comments'] and abs(d['articles'] - s['articles']) <= 3 else None,
    lambda s, d, days: (-_title_overlap(s, d), days) if days <= 3 and _title_overlap(s, d) >= 0.5 else None,
    lambda s, d, days: (abs(d['articles'] - s['articles']), days) if days <= 3 and abs(d['articles'] - s['articles']) <= 1 else None,
    lambda s, d, days: (-_title_overlap(s, d), days) if days <= 31 and _title_overlap(s, d) >= 0.7 else None,
)


def match_site_rows(session, site=None):
    """Pair stats-page rows with DB consultations: exact comment count first, then title, then article count.

    Returns (pairs [(site_row, db_row)], site-only rows, DB-only rows); ministries the stats page omits are left out.
    """
    site = site_rows() if site is None else site
    db = [r for r in consultation_rows(session) if r['slug'] not in MINISTRIES_NOT_ON_STATS_PAGE]
    used_site, used_db, pairs = set(), set(), []
    for score in MATCH_PASSES:
        for i, s in enumerate(site):
            if i in used_site or not s['start']:
                continue
            best = None
            for d in db:
                if d['id'] in used_db or not d['start']:
                    continue
                result = score(s, d, abs((d['start'] - s['start']).days))
                if result is not None and (best is None or result < best[0]):
                    best = (result, d)
            if best:
                used_site.add(i)
                used_db.add(best[1]['id'])
                pairs.append((s, best[1]))
    site_only = [s for i, s in enumerate(site) if i not in used_site]
    db_only = [d for d in db if d['id'] not in used_db]
    return pairs, site_only, db_only


def site_stats_report(session):
    site = site_rows()
    all_db = consultation_rows(session)
    by_year = defaultdict(lambda: [0, 0, 0, 0])
    for s in site:
        y = s['start'].year if s['start'] else '?'
        by_year[y][0] += 1
        by_year[y][1] += s['approved']
    omitted = [0, 0]
    for d in all_db:
        if d['slug'] in MINISTRIES_NOT_ON_STATS_PAGE:
            omitted[0] += 1
            omitted[1] += d['comments']
            continue
        y = d['start'].year if d['start'] else '?'
        by_year[y][2] += 1
        by_year[y][3] += d['comments']

    print(f"DB consultations of ministries the stats page omits ({', '.join(MINISTRIES_NOT_ON_STATS_PAGE)}): "
          f"{omitted[0]}, comments {omitted[1]} (excluded below)\n")
    print(f"{'year':6}{'site cons':>10}{'db cons':>9}{'site approved':>15}{'db comments':>13}{'short by':>10}")
    for y in sorted(by_year, key=str):
        sc, sa, dc, dm = by_year[y]
        print(f"{y!s:6}{sc:>10}{dc:>9}{sa:>15}{dm:>13}{sa - dm:>10}")
    tot = [sum(v[i] for v in by_year.values()) for i in range(4)]
    print(f"{'total':6}{tot[0]:>10}{tot[2]:>9}{tot[1]:>15}{tot[3]:>13}{tot[1] - tot[3]:>10}")

    pairs, site_only, db_only = match_site_rows(session, site)
    day = lambda r: r['start'].date() if r['start'] else '?'
    real = [s for s in site_only if not is_placeholder_title(s['title']) and (s['articles'] > 0 or s['approved'] > 0)]
    print(f"\nStats-page consultations not in the DB: {len(site_only)} "
          f"({len(site_only) - len(real)} are TEST/ΔΟΚΙΜΗ placeholders or have no articles and no comments)")
    for s in sorted(real, key=lambda s: -s['approved']):
        print(f"  {day(s)} articles={s['articles']:>3} approved={s['approved']:>4} | {s['ministry'][:30]:30} | {s['title'][:70]}")
    print(f"\nDB consultations with no stats-page row: {len(db_only)}")
    for d in db_only:
        print(f"  {day(d)} articles={d['articles']:>3} comments={d['comments']:>4} {d['url']} | {d['title'][:50]}")
    diffs = sorted((p for p in pairs if p[0]['approved'] != p[1]['comments']), key=lambda p: p[1]['comments'] - p[0]['approved'])
    print(f"\nMatched consultations whose comment count differs: {len(diffs)}")
    for s, d in diffs:
        print(f"  site {s['approved']:>5} db {d['comments']:>5} ({d['comments'] - s['approved']:+d}) {d['url']} | {s['title'][:50]}")


def main():
    parser = argparse.ArgumentParser(description='Reports on a scraped consultations database')
    parser.add_argument('--db-path', default=DEFAULT_DB_URL, help=f'Database URL (default: {DEFAULT_DB_URL})')
    parser.add_argument('--integrity', action='store_true', help='Check duplicates, misfiled articles and count mismatches')
    parser.add_argument('--site-stats', action='store_true', help="Compare with the site's StatsPerMinistry.php")
    args = parser.parse_args()

    if args.db_path.startswith('sqlite:///') and not os.path.exists(args.db_path[len('sqlite:///'):]):
        print(f"Error: Database file not found at {args.db_path}")
        sys.exit(1)
    print(f"Using database: {args.db_path}")
    _, Session = init_db(args.db_path)
    session = Session()
    try:
        if args.integrity:
            print('\n=== Integrity ===')
            for check, n in integrity_report(session).items():
                print(f"{'OK ' if n == 0 else '!! '} {check}: {n}")
        if args.site_stats:
            print('\n=== Comparison with the site statistics ===')
            site_stats_report(session)
        if not (args.integrity or args.site_stats):
            population_report(session)
    finally:
        session.close()


if __name__ == "__main__":
    main()
