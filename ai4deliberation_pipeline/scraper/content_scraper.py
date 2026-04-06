#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import logging
import re
import time
from pathlib import Path
from random import uniform
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from .utils import (
    parse_greek_date,
    find_element_with_fallbacks,
    extract_post_id,
    build_absolute_url,
    get_request_headers,
)

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30
REQUEST_DELAY = (0.15, 0.25)

HTTP = requests.Session()


def http_get(url, allow_redirects=True):
    """Centralized GET helper with shared session."""
    response = HTTP.get(
        url,
        headers=get_request_headers(),
        timeout=REQUEST_TIMEOUT,
        allow_redirects=allow_redirects,
    )
    response.raise_for_status()
    return response


def set_query_param(url, key, value):
    """Set or replace a query parameter in a URL and keep #comments fragment."""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query[key] = str(value)
    return urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        urlencode(query),
        "comments"
    ))


def fetch_page_soup(url):
    """Fetch a page and return BeautifulSoup plus final URL after redirects."""
    response = http_get(url, allow_redirects=True)
    return BeautifulSoup(response.content, 'html.parser'), response.url


def discover_comment_page_urls(soup, article_url):
    """
    Discover all comment pagination pages from the current article page.
    If no pagination exists, return just the article URL.
    """
    page_numbers = set()

    selectors = (
        "div.nav a.page-numbers, "
        "div.nav span.page-numbers.current, "
        "div.navigation a.page-numbers, "
        "div.navigation span.page-numbers.current"
    )

    for el in soup.select(selectors):
        txt = el.get_text(strip=True)
        if txt.isdigit():
            page_numbers.add(int(txt))

        href = el.get("href")
        if href:
            try:
                qs = dict(parse_qsl(urlsplit(href).query, keep_blank_values=True))
                cpage = qs.get("cpage")
                if cpage and str(cpage).isdigit():
                    page_numbers.add(int(cpage))
            except Exception:
                pass

    max_page = max(page_numbers) if page_numbers else 1

    urls = [article_url]
    if max_page > 1:
        for n in range(2, max_page + 1):
            urls.append(set_query_param(article_url, "cpage", n))

    # deduplicate, preserve order
    seen = set()
    unique_urls = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique_urls.append(u)

    logger.info(f"Discovered {len(unique_urls)} comment page(s) for {article_url}")
    return unique_urls


def extract_comment_text(item):
    """Extract clean comment text after removing metadata and nested replies."""
    item_copy = BeautifulSoup(str(item), 'html.parser').find('li')
    if not item_copy:
        return ""

    for junk in item_copy.select(
        "div.user, div.meta-comment, a.permalink, div.rate, ul.children, ol.children"
    ):
        junk.decompose()

    p_tags = item_copy.find_all('p')
    parts = [p.get_text(" ", strip=True) for p in p_tags if p.get_text(" ", strip=True)]
    if parts:
        return "\n".join(parts).strip()

    return item_copy.get_text(" ", strip=True)


def extract_comments_from_single_page(soup, include_author=False):
    """
    Extract comments from one HTML page only.
    Safer for opengov.gr structure:
    - list: ul.comment_list / ol.comment_list
    - comment node: li[id^='comment-']
    """
    comments = []

    comment_section_selectors = [
        'div#comments',
        'div.comments-template',
        'div.comments_template',
        'div.comments-area',
        'div.comments'
    ]
    comments_div = find_element_with_fallbacks(soup, comment_section_selectors)
    if not comments_div:
        logger.warning("No comments section found")
        return comments

    comment_nodes = comments_div.select(
        "ul.comment_list > li[id^='comment-'], "
        "ol.comment_list > li[id^='comment-'], "
        "ul.commentlist > li[id^='comment-'], "
        "ol.commentlist > li[id^='comment-']"
    )

    logger.info(f"Found {len(comment_nodes)} raw comment nodes on current page")

    for item in comment_nodes:
        try:
            comment_id = item.get('id', '').replace('comment-', '').strip()
            if not comment_id:
                continue

            author_div = item.select_one("div.user div.author, div.author")
            permalink_tag = item.select_one("div.meta-comment a.permalink, a.permalink")

            date_obj = None
            username = None

            if author_div:
                author_text = author_div.get_text(" ", strip=True)

                date_match = re.search(
                    r'(\d+\s+[Α-Ωα-ωίϊΐόάέύϋΰήώ]+\s+\d{4},\s+\d{1,2}:\d{2})',
                    author_text
                )
                if date_match:
                    date_str = date_match.group(1).strip()
                    date_obj = parse_greek_date(date_str)

                if include_author:
                    strong_tag = author_div.find('strong')
                    if strong_tag:
                        username = strong_tag.get_text(" ", strip=True)
                    else:
                        username = "Anonymous"

            content = extract_comment_text(item)

            row = {
                'comment_id': comment_id,
                'username': (username or "ANONYMIZED") if include_author else "ANONYMIZED",
                'date': date_obj,
                'content': content,
                'permalink': permalink_tag['href'] if permalink_tag and permalink_tag.has_attr('href') else None
            }

            if row["content"]:
                comments.append(row)

        except Exception as e:
            logger.error(f"Error processing comment node: {e}")

    return comments


def extract_article_links(url):
    """Extract article links from a consultation page."""
    try:
        logger.info(f"Fetching article list from URL: {url}")
        response = http_get(url, allow_redirects=True)

        final_url = response.url
        if final_url != url:
            logger.info(f"URL was redirected: {url} -> {final_url}")
            url = final_url

        soup = BeautifulSoup(response.content, 'html.parser')

        articles = []
        seen_urls = set()

        def add_article(article_url, article_title):
            if not article_url:
                return
            if article_url in seen_urls:
                return
            seen_urls.add(article_url)

            articles.append({
                'post_id': extract_post_id(article_url),
                'title': article_title,
                'url': article_url
            })
            logger.info(f"Found article: {article_title}")

        nav_selectors = ['div#consnav', 'div.navigation']
        consnav_div = find_element_with_fallbacks(soup, nav_selectors)

        if consnav_div:
            list_selectors = ['ul.other_posts', 'ul.articlesList']
            articles_list = find_element_with_fallbacks(consnav_div, list_selectors)

            if articles_list:
                li_elements = articles_list.find_all('li')
                logger.info(f"Found {len(li_elements)} article links in navigation")

                for li in li_elements:
                    try:
                        link = li.find('a', class_='list_comments_link') or li.find('a')
                        if link and link.has_attr('href'):
                            article_url = build_absolute_url(url, link['href'])
                            article_title = link.get_text(strip=True)
                            add_article(article_url, article_title)
                    except Exception as e:
                        logger.error(f"Error processing article link: {e}")

        if not articles:
            logger.info("No articles found in navigation, trying content area")
            content_divs = soup.find_all('div', class_='post_content')

            for div in content_divs:
                links = div.find_all('a')

                for link in links:
                    if link.has_attr('href') and '?p=' in link['href']:
                        article_url = build_absolute_url(url, link['href'])
                        article_title = link.get_text(strip=True)

                        if len(article_title) < 3 or 'http' in article_title.lower():
                            continue

                        add_article(article_url, article_title)

        return articles

    except Exception as e:
        logger.error(f"Error extracting article links: {e}")
        return []


def scrape_article_content(article_url):
    """Scrape article content and all paginated comments."""
    try:
        logger.info(f"Fetching article from URL: {article_url}")
        soup, final_url = fetch_page_soup(article_url)

        if final_url != article_url:
            logger.info(f"URL was redirected: {article_url} -> {final_url}")
            article_url = final_url

        article_info = {
            'post_id': extract_post_id(article_url),
            'title': '',
            'content': '',
            'url': article_url,
            'comments': []
        }

        title_selectors = ['h3.blogpost-title', 'h3']
        title_element = find_element_with_fallbacks(soup, title_selectors)

        if title_element:
            article_info['title'] = title_element.get_text(strip=True)
            logger.info(f"Article title: {article_info['title']}")

        content_div = soup.find('div', class_='post_content')
        if content_div:
            article_info['raw_html'] = str(content_div)
            logger.info(f"Raw HTML content captured: {len(article_info['raw_html'])} chars")
        else:
            article_info['raw_html'] = ''

        article_info['comments'] = extract_comments(soup, article_url)
        logger.info(f"Extracted {len(article_info['comments'])} comments")

        return article_info

    except Exception as e:
        logger.error(f"Error scraping article content: {e}")
        return None


def extract_comments(soup, article_url, delay_range=(0.15, 0.25), include_author=False):
    """
    Extract comments from all comment pages of an article.
    Deduplicate by comment_id.
    """
    all_comments = []
    seen_comment_ids = set()

    try:
        page_urls = discover_comment_page_urls(soup, article_url)

        for i, page_url in enumerate(page_urls):
            try:
                if i == 0:
                    page_soup = soup
                else:
                    delay = uniform(*delay_range)
                    logger.info(f"Waiting {delay:.2f} seconds before fetching comment page {page_url}")
                    time.sleep(delay)
                    page_soup, _ = fetch_page_soup(page_url)

                page_comments = extract_comments_from_single_page(
                    page_soup,
                    include_author=include_author
                )

                logger.info(f"Extracted {len(page_comments)} comments from page {i + 1}/{len(page_urls)}")

                for comment in page_comments:
                    cid = comment['comment_id']
                    if cid not in seen_comment_ids:
                        seen_comment_ids.add(cid)
                        all_comments.append(comment)

            except Exception as e:
                logger.error(f"Error while scraping comment page {page_url}: {e}")

        logger.info(f"Extracted {len(all_comments)} unique comments from {article_url}")
        return all_comments

    except Exception as e:
        logger.error(f"Error extracting comments: {e}")
        return all_comments


def scrape_consultation_content(consultation_url, delay_range=(0.15, 0.25)):
    """Scrape all articles and comments from a consultation."""
    try:
        articles_links = extract_article_links(consultation_url)
        logger.info(f"Found {len(articles_links)} article links")

        articles_content = []
        for i, article in enumerate(articles_links):
            if i > 0:
                delay = uniform(*delay_range)
                logger.info(f"Waiting {delay:.2f} seconds before next request...")
                time.sleep(delay)

            article_data = scrape_article_content(article['url'])
            if article_data:
                articles_content.append(article_data)

        article_count = len(articles_content)
        comment_count = sum(len(article['comments']) for article in articles_content)
        logger.info(f"Successfully scraped {article_count} articles with {comment_count} total comments")

        return articles_content

    except Exception as e:
        logger.error(f"Error scraping consultation content: {e}")
        return []


def save_comments_to_csv(articles, consultation_url, out_path):
    """Save all scraped comments to a flat CSV file."""
    rows = []

    for article in articles:
        for comment in article.get("comments", []):
            rows.append({
                "consultation_url": consultation_url,
                "article_url": article.get("url"),
                "article_post_id": article.get("post_id"),
                "article_title": article.get("title"),
                "comment_id": comment.get("comment_id"),
                "comment_date": comment.get("date").isoformat() if comment.get("date") else None,
                "comment_permalink": comment.get("permalink"),
                "comment_content": comment.get("content"),
            })

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "consultation_url",
                "article_url",
                "article_post_id",
                "article_title",
                "comment_id",
                "comment_date",
                "comment_permalink",
                "comment_content",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    logger.info(f"Saved {len(rows)} comments to {out_path}")


if __name__ == "__main__":
    test_url = "http://www.opengov.gr/ministryofjustice/?p=18058"
    articles = scrape_consultation_content(test_url)

    if articles:
        print(
            f"\nScraped {len(articles)} articles with a total of "
            f"{sum(len(a['comments']) for a in articles)} comments"
        )

        for i, article in enumerate(articles[:3]):
            print(f"\nArticle {i + 1}: {article['title']}")
            print(f"  ID: {article['post_id']}")
            print(f"  URL: {article['url']}")
            print(f"  Content length: {len(article.get('raw_html', ''))} chars")
            print(f"  Comments: {len(article['comments'])}")

            for j, comment in enumerate(article['comments'][:2]):
                print(f"    Comment {j + 1}: {comment.get('username', 'ANONYMIZED')} ({comment['date']})")
                print(f"      Content: {comment['content'][:100]}...")

        print("\n(Showing only first 3 articles and 2 comments per article)")