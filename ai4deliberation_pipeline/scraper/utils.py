#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import logging
import threading
import time
import urllib.parse
import unicodedata
from datetime import datetime
from random import uniform

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

LOG_FORMAT = '%(asctime)s - %(levelname)s - %(message)s'

REQUEST_TIMEOUT = 30
REQUEST_DELAY = (0.15, 0.25)  # seconds between requests to the site

_thread_local = threading.local()


def http_get(url, allow_redirects=True, retries=3):
    """GET with the shared headers, a per-thread session, a timeout, and retries on network/5xx errors."""
    if not hasattr(_thread_local, 'session'):
        _thread_local.session = requests.Session()
        _thread_local.session.headers.update(get_request_headers())
    for attempt in range(retries):
        try:
            response = _thread_local.session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=allow_redirects)
            response.raise_for_status()
            return response
        except requests.RequestException as e:
            client_error = isinstance(e, requests.HTTPError) and e.response is not None and e.response.status_code < 500
            if client_error or attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def polite_sleep(delay_range=REQUEST_DELAY):
    time.sleep(uniform(*delay_range))


# Genitive month names appear on consultation and comment pages ("12 Μαρτίου 2025, 14:30"),
# nominative ones in the consultation listing ("3 Δεκέμβριος, 2013").
GREEK_MONTHS = {
    'Ιανουαρίου': 1, 'Φεβρουαρίου': 2, 'Μαρτίου': 3, 'Απριλίου': 4, 'Μαΐου': 5, 'Ιουνίου': 6,
    'Ιουλίου': 7, 'Αυγούστου': 8, 'Σεπτεμβρίου': 9, 'Οκτωβρίου': 10, 'Νοεμβρίου': 11, 'Δεκεμβρίου': 12,
    'Ιανουάριος': 1, 'Φεβρουάριος': 2, 'Μάρτιος': 3, 'Απρίλιος': 4, 'Μάιος': 5, 'Ιούνιος': 6,
    'Ιούλιος': 7, 'Αύγουστος': 8, 'Σεπτέμβριος': 9, 'Οκτώβριος': 10, 'Νοέμβριος': 11, 'Δεκέμβριος': 12,
}
_GREEK_DATE = re.compile(r'^(\d{1,2})\s+(\S+?),?\s+(\d{4})(?:,\s*(\d{1,2}):(\d{2}))?$')


def parse_greek_date(date_string):
    """Parse 'DD Month YYYY[, HH:MM]' or 'DD Month, YYYY' (Greek month names) to a datetime, or None."""
    m = _GREEK_DATE.match(re.sub(r'\s+', ' ', date_string or '').strip())
    month = GREEK_MONTHS.get(m.group(2)) if m else None
    if not month:
        logger.error(f"Error parsing date string '{date_string}'")
        return None
    try:
        return datetime(int(m.group(3)), month, int(m.group(1)), int(m.group(4) or 0), int(m.group(5) or 0))
    except ValueError as e:
        logger.error(f"Error parsing date string '{date_string}': {e}")
        return None

def extract_content_text(element):
    """Extract text content from an HTML element, preserving some structure"""
    if not element:
        return ""
    
    # Get text with basic formatting preserved
    text_parts = []
    
    # Process paragraphs
    paragraphs = element.find_all('p')
    for p in paragraphs:
        text_parts.append(p.get_text(strip=True))
    
    # Process lists
    lists = element.find_all(['ul', 'ol'])
    for lst in lists:
        items = lst.find_all('li')
        for item in items:
            # Add bullet point for unordered lists
            if lst.name == 'ul':
                text_parts.append(f"• {item.get_text(strip=True)}")
            else:
                # For ordered lists, we don't know the exact number, so just use a bullet
                text_parts.append(f"- {item.get_text(strip=True)}")
    
    # Process headings
    headings = element.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
    for heading in headings:
        text_parts.append(heading.get_text(strip=True))
    
    # If no paragraphs or lists found, get all text
    if not text_parts:
        return element.get_text(strip=True)
    
    return "\n\n".join(text_parts)

def find_element_with_fallbacks(soup, selectors):
    """Find an element using a list of CSS selectors, trying each in order"""
    for selector in selectors:
        element = soup.select_one(selector)
        if element:
            logger.info(f"Found element with selector: {selector}")
            return element
    return None

def extract_post_id(url):
    """Extract the post ID from a URL ('...?p=123&cpage=2#comments' -> '123')."""
    m = re.search(r'[?&]p=(\d+)', url or '')
    return m.group(1) if m else None

# opengov.gr moved to archive.opengov.gr; older DB rows keep the www host.
OPENGOV_HOSTS = ('www.opengov.gr', 'opengov.gr', 'archive.opengov.gr')

def strip_default_port(url):
    """Drop an explicit default port: redirects sometimes yield 'https://archive.opengov.gr:443/...'."""
    return re.sub(r'^(https://[^/:]+):443(?=/|$)|^(http://[^/:]+):80(?=/|$)', lambda m: m.group(1) or m.group(2), url or '') if url else url

def opengov_url_key(url):
    """Host-, port- and scheme-independent key for an opengov URL (e.g. 'minenv/?p=13883')."""
    if not url:
        return url
    parsed = urllib.parse.urlparse(url.strip())
    if (parsed.hostname or '') not in OPENGOV_HOSTS:
        return url.strip()
    key = parsed.path.lstrip('/')
    if parsed.query:
        key += '?' + parsed.query
    return key

def opengov_url_variants(url):
    """All spellings of an opengov URL across its old and archive hosts, for DB lookups."""
    key = opengov_url_key(url)
    if key == (url or '').strip():
        return [url]
    return ([f"{scheme}://{host}/{key}" for scheme in ('https', 'http') for host in OPENGOV_HOSTS]
            + [f"https://{host}:443/{key}" for host in OPENGOV_HOSTS])

def opengov_article_url_variants(url):
    """Like opengov_url_variants, plus the '#comments' spelling that article links from consultation navigation carry."""
    variants = opengov_url_variants(url)
    return variants + [v + '#comments' for v in variants if '#' not in v]

def normalize_consultation_url(url):
    """Normalize URL for matching across http/https, opengov host and trailing slash differences."""
    if not url:
        return None
    try:
        parsed = urllib.parse.urlparse(url.strip())
        netloc = parsed.netloc.lower()
        if (parsed.hostname or '') in OPENGOV_HOSTS:
            # www.opengov.gr (older DB rows) and archive.opengov.gr are the same site
            netloc = "opengov.gr"
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
        parsed = urllib.parse.urlparse((url or "").strip())
        path_parts = [part for part in parsed.path.strip("/").split("/") if part]
        return path_parts[0].lower() if path_parts else None
    except Exception:
        return None

# Homepage announcement posts, e.g. archive.opengov.gr/home/2026/07/10/10155
ANNOUNCEMENT_PATH = re.compile(r'^/home/\d{4}/\d{2}/\d{2}/\d+/?$')

def resolve_announcement_url(url):
    """The consultation listing sometimes links to a homepage announcement instead of the
    consultation itself; return the ministry consultation URL it points to, or `url` unchanged."""
    parsed = urllib.parse.urlparse(url)
    if (parsed.hostname or '') not in OPENGOV_HOSTS or not ANNOUNCEMENT_PATH.match(parsed.path):
        return url
    try:
        response = http_get(url)
        post = BeautifulSoup(response.content, 'html.parser').select_one('div.single_post')
        for a in (post.find_all('a', href=True) if post else []):
            target = urllib.parse.urljoin(url, a['href'].strip())
            target_parsed = urllib.parse.urlparse(target)
            if (target_parsed.netloc.lower() in OPENGOV_HOSTS and '?p=' in target
                    and not target_parsed.path.startswith('/home/')):
                logger.info(f"Announcement {url} -> consultation {target}")
                return target
        logger.warning(f"Announcement {url} has no consultation link; keeping it as is")
    except Exception as e:
        logger.error(f"Could not resolve announcement {url}: {e}")
    return url

def build_absolute_url(base_url, relative_url):
    """Build an absolute URL from a base URL and a relative URL"""
    return urllib.parse.urljoin(base_url, relative_url)

def extract_ministry_info(url):
    """Extract ministry information from the URL and page content"""
    try:
        # Parse URL to get ministry code
        parsed_url = urllib.parse.urlparse(url)
        hostname = parsed_url.hostname or ''  # without port: redirects can add ':443'
        path_parts = parsed_url.path.strip('/').split('/')
        
        # Try to extract ministry code from URL
        ministry_code = None
        if hostname in OPENGOV_HOSTS:
            ministry_code = path_parts[0] if path_parts else None
        else:
            # Handle case where ministry code is in subdomain
            domain_parts = hostname.split('.')
            if len(domain_parts) > 0 and 'opengov' in hostname:
                potential_code = domain_parts[0]
                if potential_code != 'www':
                    ministry_code = potential_code
        
        # Construct ministry base URL
        if ministry_code:
            ministry_base_url = f"https://{hostname if hostname in OPENGOV_HOSTS else 'archive.opengov.gr'}/{ministry_code}/"
        else:
            ministry_base_url = url[:url.find('?')] if '?' in url else url
            
        return {
            'code': ministry_code,
            'url': ministry_base_url,
            'name': None  # We'll populate this with actual content scrape
        }
    except Exception as e:
        logger.error(f"Error extracting ministry info from URL {url}: {e}")
        return {
            'code': None,
            'url': None,
            'name': None
        }

def get_request_headers():
    """Return headers for HTTP requests"""
    return {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Accept-Language': 'el-GR,el;q=0.9,en-US;q=0.8,en;q=0.7'
    }

def normalize_text(text):
    """
    Normalizes text by:
    1. Removing accents
    2. Converting to UPPERCASE
    
    Args:
        text: Original text
    
    Returns:
        Normalized text
    """
    if not text:
        return ""
    
    # Remove accents
    normalized = unicodedata.normalize('NFKD', text)
    normalized = ''.join([c for c in normalized if not unicodedata.combining(c)])
    
    # Convert to uppercase
    normalized = normalized.upper()
    
    return normalized


def categorize_document(title):
    """
    Categorizes a document based on its normalized title.
    
    Args:
        title: Document title
        
    Returns:
        Document type: 'law_draft', 'analysis', 'deliberation_report', 'other_draft',
        'other_report', or 'other'
    """
    # Normalize the title
    normalized_title = normalize_text(title)
    
    # Check for law draft - look for both words separately
    if 'ΣΧΕΔΙΟ' in normalized_title and 'ΝΟΜΟΥ' in normalized_title:
        return 'law_draft'
    
    # Check for analysis - look for both words separately
    if 'ΑΝΑΛΥΣΗ' in normalized_title and 'ΣΥΝΕΠΕΙΩΝ' in normalized_title:
        return 'analysis'
    
    # Check for deliberation report - this was already looking for both words
    if 'ΕΚΘΕΣΗ' in normalized_title and 'ΔΙΑΒΟΥΛΕΥΣΗ' in normalized_title:
        return 'deliberation_report'
    
    # Check for non-law draft documents (only if none of the above matched)
    # These are documents that contain ΣΧΕΔΙΟ but not ΝΟΜΟΥ
    if 'ΣΧΕΔΙΟ' in normalized_title and 'ΝΟΜΟΥ' not in normalized_title:
        return 'other_draft'
    
    # Check for general reports (only if none of the above matched)
    # These are documents that contain ΕΚΘΕΣΗ but not ΔΙΑΒΟΥΛΕΥΣΗ
    if 'ΕΚΘΕΣΗ' in normalized_title and 'ΔΙΑΒΟΥΛΕΥΣΗ' not in normalized_title:
        return 'other_report'
        
    # Default to 'other'
    return 'other'
