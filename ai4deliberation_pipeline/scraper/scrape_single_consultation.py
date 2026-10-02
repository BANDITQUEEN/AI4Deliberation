#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import logging

from sqlalchemy import func

from .db_models import DEFAULT_DB_URL, init_db, Ministry, Consultation, Article, Comment, Document
from .metadata_scraper import scrape_consultation_metadata
from .content_scraper import scrape_consultation_content
from .utils import (LOG_FORMAT, extract_ministry_code_from_url, extract_post_id, normalize_consultation_url,
                    opengov_url_variants, opengov_article_url_variants)

logger = logging.getLogger(__name__)


def find_existing_consultation(session, url, post_id=None):
    """The stored consultation for a URL: same normalized URL, else same post_id under the same ministry.

    Never trust post_id alone, because the same ?p= id exists under different ministries.
    """
    post_id = post_id or extract_post_id(url)
    if not post_id:
        return None
    candidates = session.query(Consultation).filter_by(post_id=str(post_id)).all()
    normalized_url = normalize_consultation_url(url)
    for cons in candidates:
        if normalize_consultation_url(cons.url) == normalized_url:
            return cons
    ministry = extract_ministry_code_from_url(url)
    ministry_matches = [cons for cons in candidates if extract_ministry_code_from_url(cons.url) == ministry]
    if len(ministry_matches) > 1:
        unfinished = [cons for cons in ministry_matches if not cons.is_finished]
        chosen = (unfinished or ministry_matches)[0]
        logger.warning(f"Found {len(ministry_matches)} consultations for post_id={post_id} under {ministry}; selected {chosen.url}")
        return chosen
    if not ministry_matches and candidates:
        logger.info(f"post_id={post_id} exists only under other ministries; treating {url} as new")
    return ministry_matches[0] if ministry_matches else None


def store_articles(session, consultation, articles_data):
    """Add scraped articles and their comments to a consultation, skipping ones already stored.

    Returns (new article count, new comment count) and refreshes consultation.accepted_comments.
    """
    article_count = 0
    comment_count = 0
    for article_data in articles_data:
        # Check if article already exists
        existing_article = session.query(Article).filter(Article.url.in_(opengov_article_url_variants(article_data['url']))).first()
        
        if existing_article:
            article = existing_article
            logger.info(f"Article already exists: {article.title}")
        else:
            logger.info(f"Adding article: {article_data['title']}")
            article = Article(
                title=article_data['title'],
                content=article_data['content'],
                raw_html=article_data.get('raw_html', ''),  # Include raw HTML content
                url=article_data['url'],
                consultation_id=consultation.id,
                extraction_method=article_data.get('extraction_method')
            )
            session.add(article)
            session.flush()  # Get the ID without committing
            article_count += 1
        
        # Add comments for this article
        for comment_data in article_data['comments']:
            # Check if comment already exists (by comment_id and article_id)
            existing_comment = session.query(Comment).filter_by(
                comment_id=comment_data['comment_id'],
                article_id=article.id
            ).first()
            
            if not existing_comment:
                logger.info(f"Adding comment by {comment_data.get('username', 'ANONYMIZED')}")
                comment = Comment(
                    comment_id=comment_data['comment_id'],
                    username=comment_data.get('username', 'ANONYMIZED'),
                    date=comment_data['date'],
                    content=comment_data['content'],
                    article_id=article.id,
                    extraction_method=article_data.get('extraction_method')
                )
                session.add(comment)
                comment_count += 1
    
    # Calculate accepted_comments as the sum of all comments in articles
    total_comment_count = session.query(func.count(Comment.id)).join(Article).filter(Article.consultation_id == consultation.id).scalar() or 0
    logger.info(f"Calculated actual comment count from articles: {total_comment_count}")
    
    # Update accepted_comments with the actual count
    consultation.accepted_comments = total_comment_count
    return article_count, comment_count


def attach_articles(session, consultation, source_url):
    """Store the articles listed under another post (source_url) in `consultation`, for consultations whose
    own page lists none. Commits; returns (new article count, new comment count)."""
    articles_data = scrape_consultation_content(source_url)
    counts = store_articles(session, consultation, articles_data)
    session.commit()
    logger.info(f"Attached {counts[0]} articles and {counts[1]} comments from {source_url} to {consultation.url}")
    return counts

def scrape_and_store(url, session, selective_update=False, existing_cons=None):
    """Scrape a consultation URL and store all data in the database.
    
    If selective_update is True and existing_cons is provided, only updates
    minister messages, comments, and document links for unfinished consultations.
    
    Returns True if successful, and a dictionary of changes when selective_update is True.
    """
    logger.info(f"Starting {'selective update' if selective_update else 'full scrape'} of URL: {url}")
    
    # Initialize change tracking dictionary
    changes = {
        'new_comments': 0,
        'new_documents': 0,
        'status_change': False,
        'total_comments_change': 0,
        'start_message_changed': False,
        'end_message_changed': False
    }
    
    # Step 1: Scrape metadata
    metadata_result = scrape_consultation_metadata(url)
    if not metadata_result:
        logger.error(f"Failed to scrape metadata from {url}")
        return False, None
    
    # Validate post_id - if it's None, we can't proceed
    consultation_data = metadata_result['consultation']
    if not consultation_data['post_id']:
        logger.error(f"Missing post_id for URL: {url}. This consultation was likely redirected or no longer exists.")
        return False, None
    
    # Step 2: Extract ministry data and find or create ministry record
    ministry_data = metadata_result['ministry']
    ministry = session.query(Ministry).filter_by(code=ministry_data['code']).first()
    
    if not ministry:
        logger.info(f"Creating new ministry record for {ministry_data['name']}")
        ministry = Ministry(
            code=ministry_data['code'],
            name=ministry_data['name'],
            url=ministry_data['url']
        )
        session.add(ministry)
        session.flush()  # Get the ID without committing
    
    # Step 3: Strict consultation existence check
    existing_consultation = find_existing_consultation(session, url, consultation_data["post_id"])

    # STRICT HANDLING LOGIC
    if existing_consultation is not None:
        logger.info(f"Consultation already exists: {existing_consultation.title}")
        logger.info(f"  ID: {existing_consultation.id}")
        logger.info(f"  Finished: {existing_consultation.is_finished}")
        logger.info(f"  URL: {existing_consultation.url}")

        if existing_consultation.is_finished:
            logger.warning("⚠️  CONSULTATION IS FINISHED - No updates allowed")
            logger.info("📋 Existing consultation data remains unchanged")
            session.commit()

            if selective_update:
                return True, {
                    "new_comments": 0,
                    "new_documents": 0,
                    "status_change": False,
                    "total_comments_change": 0,
                    "start_message_changed": False,
                    "end_message_changed": False,
                    "message": "Consultation is finished - no updates performed",
                }
            else:
                return True, existing_consultation.id

        logger.info("🔄 EXISTING CONSULTATION IS UNFINISHED - Checking for new comments and documents")

        old_total_comments = existing_consultation.total_comments or 0
        existing_consultation.total_comments = consultation_data["total_comments"]
        existing_consultation.end_minister_message = consultation_data["end_minister_message"]

        was_unfinished = not existing_consultation.is_finished
        existing_consultation.is_finished = consultation_data["is_finished"]

        if was_unfinished and consultation_data["is_finished"]:
            logger.info("🏁 Consultation status changed: UNFINISHED → FINISHED")

        session.flush()
        consultation = existing_consultation

    else:
        logger.info(f"✅ NEW CONSULTATION - Creating new record: {consultation_data['title']}")
        consultation = Consultation(
            post_id=consultation_data["post_id"],
            title=consultation_data["title"],
            start_minister_message=consultation_data["start_minister_message"],
            end_minister_message=consultation_data["end_minister_message"],
            start_date=consultation_data["start_date"],
            end_date=consultation_data["end_date"],
            is_finished=consultation_data["is_finished"],
            url=url,
            total_comments=consultation_data["total_comments"],
            accepted_comments=0,
            ministry_id=ministry.id,
        )
        session.add(consultation)
        session.flush()
    
    # Step 4: Create document records
    for doc_data in metadata_result['documents']:
        # Check if document already exists
        existing_doc = session.query(Document).filter(Document.url.in_(opengov_url_variants(doc_data['url']))).first()
        if not existing_doc:
            logger.info(f"Adding document: {doc_data['title']}")
            document = Document(
                title=doc_data['title'],
                url=doc_data['url'],
                type=doc_data['type'],
                consultation_id=consultation.id
            )
            session.add(document)
            
            # If we're doing a selective update, track new documents
            if selective_update:
                changes['new_documents'] += 1
    
    # Step 5: Scrape article content and comments
    articles_data = scrape_consultation_content(url)
    if not articles_data:
        logger.error(f"Failed to scrape articles from {url}")
        articles_data = []
    
    # If we have neither articles nor a title, abort to avoid storing empty shells from redirects
    if not articles_data and not consultation_data.get('title'):
        logger.error(f"No articles or title found for {url}; skipping creation to avoid empty record.")
        session.rollback()
        return False, None
        
    # If we're doing a selective update of an unfinished consultation that is now finished,
    # record this status change
    if selective_update and existing_cons:
        if not existing_cons.is_finished and consultation_data['is_finished']:
            changes['status_change'] = True
            logger.info("Consultation status changed from unfinished to finished")
    
    # Step 6: Create article and comment records
    # If this is a selective update, we only need to track changes to comments
    if selective_update and existing_cons:
        # Track changes to minister messages
        old_start_message = existing_cons.start_minister_message or ""
        new_start_message = consultation_data['start_minister_message'] or ""
        if old_start_message != new_start_message:
            changes['start_message_changed'] = True
            logger.info(f"Start minister message changed: {len(old_start_message)} chars -> {len(new_start_message)} chars")
            
        old_end_message = existing_cons.end_minister_message or ""
        new_end_message = consultation_data['end_minister_message'] or ""
        if old_end_message != new_end_message:
            changes['end_message_changed'] = True
            logger.info(f"End minister message changed: {len(old_end_message)} chars -> {len(new_end_message)} chars")
            
        # Track changes to total comments
        old_total = existing_cons.total_comments or 0
        new_total = consultation_data['total_comments'] or 0
        if old_total != new_total:
            changes['total_comments_change'] = new_total - old_total
            logger.info(f"Total comments changed: {old_total} -> {new_total} (change: {changes['total_comments_change']})")
    
    article_count, comment_count = store_articles(session, consultation, articles_data)
    if selective_update:
        changes['new_comments'] += comment_count
    
    try:
        session.commit()
        logger.info(f"Successfully processed and committed data for consultation ID: {consultation.id}")
        if selective_update:
            return True, changes
        else:
            return True, consultation.id
    except Exception as e:
        logger.error(f"Database commit failed for {url}: {e}")
        session.rollback()
        if selective_update:
            return False, changes
        else:
            return False, None

def main():
    """Main entry point for the program"""
    parser = argparse.ArgumentParser(description='Scrape consultation data from OpenGov.gr and store in database')
    parser.add_argument('urls', metavar='URL', type=str, nargs='+',
                        help='One or more consultation URLs to scrape')
    parser.add_argument('--db-path', type=str, default=DEFAULT_DB_URL,
                        help=f'Database URL (default: {DEFAULT_DB_URL})')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)

    engine, Session = init_db(args.db_path)
    session = Session()
    try:
        success_count = 0
        for url in args.urls:
            ok, _ = scrape_and_store(url, session)
            success_count += bool(ok)
        logger.info(f"Completed {success_count}/{len(args.urls)} consultations successfully")
    finally:
        session.close()

if __name__ == "__main__":
    main()
