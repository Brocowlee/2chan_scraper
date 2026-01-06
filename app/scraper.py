import logging
import time
import os
import requests
from bs4 import BeautifulSoup
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app import models

logger = logging.getLogger(__name__)

# Constants
BASE_URL = "https://nov.2chan.net/35"
IMAGE_DIR = "images"

def fetch_page(url: str) -> str:
    """Fetch a page and decode using Shift_JIS."""
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        # 2chan uses Shift_JIS
        response.encoding = 'shift_jis' 
        return response.text
    except Exception as e:
        logger.error(f"Failed to fetch {url}: {e}")
        return None

def download_image(url: str, post_number: int) -> str:
    """Download image and return local path."""
    if not url:
        return None
    
    try:
        # Create images dir if not exists
        if not os.path.exists(IMAGE_DIR):
            os.makedirs(IMAGE_DIR)

        # Basic filename from post number + extension
        ext = url.split('.')[-1]
        filename = f"{post_number}.{ext}"
        filepath = os.path.join(IMAGE_DIR, filename)

        # If already exists, skip
        if os.path.exists(filepath):
            return filepath

        # Download
        # 2chan images might need headers or referrer sometimes, but usually open
        # The image URL is often relative or absolute
        if not url.startswith('http'):
            url = f"https://nov.2chan.net/35/{url}"

        r = requests.get(url, stream=True, timeout=10)
        if r.status_code == 200:
            with open(filepath, 'wb') as f:
                for chunk in r.iter_content(1024):
                    f.write(chunk)
            return filepath
    except Exception as e:
        logger.error(f"Failed to download image {url}: {e}")
        return None
    return None

def parse_and_save(html: str):
    """Parse the HTML and save threads/posts."""
    soup = BeautifulSoup(html, 'lxml')
    db: Session = SessionLocal()
    
    try:
        # Threads are usually wrapped in div with class 'thre'
        threads_divs = soup.find_all('div', class_='thre')
        
        logger.info(f"Found {len(threads_divs)} threads on page.")

        for thre in threads_divs:
            # The OP post is directly inside specific tags, but structure varies.
            # Typically structure:
            # <div class="thre" data-res="12345">
            #   Images, Checkbox, <span class="cnm">Name</span>, <span class="csb">Title</span>...
            #   <blockquote>Content</blockquote>
            #   <table... repliest...>
            # </div>

            # Thread ID from data-res
            thread_num_str = thre.get('data-res')
            if not thread_num_str:
                continue
            thread_num = int(thread_num_str)

            # --- UPSERT THREAD ---
            # We don't have the subject readily available as a property of the thread sometimes, 
            # but usually the OP's title is the thread subject.
            # For robust upsert in Postgres:
            stmt = insert(models.Thread).values(
                thread_number=thread_num,
                board_id='35',
                created_at=datetime.utcnow()
            ).on_conflict_do_nothing(
                index_elements=['thread_number']
            )
            db.execute(stmt)
            db.commit()

            # Retrieve the thread ID from DB to link posts
            thread_obj = db.query(models.Thread).filter_by(thread_number=thread_num).first()
            if not thread_obj:
                continue

            # --- PROCESS OP ---
            # Extract OP info
            # Post number is usually checking the input checkbox value or a span.cno
            # 2chan structure for OP:
            # <input type=checkbox name=del value=12345> ...
            # <span class="cnm">Name</span> ...
            # <span class="csb">Title</span> (Subject)
            # <blockquote>Content</blockquote>
            
            # Helper to extract post data
            def extract_post_data(element, is_op=False):
                post_data = {}
                
                # Number
                # Checkbox value is robust
                checkbox = element.find('input', {'name': 'del'})
                if checkbox:
                    post_data['post_number'] = int(checkbox.get('value'))
                else:
                    return None
                    
                # Name
                cnm = element.find('span', class_='cnm')
                post_data['name'] = cnm.get_text(strip=True) if cnm else "Anonymous"
                
                # Title (Subject) - only relevant for OP mostly
                csb = element.find('span', class_='csb')
                subject = csb.get_text(strip=True) if csb else None
                if is_op and subject:
                     # Update thread subject if we have it
                     thread_obj.subject = subject
                
                # Content
                blockquote = element.find('blockquote')
                post_data['content'] = blockquote.get_text('\n', strip=True) if blockquote else ""
                
                # Metadata (Date/ID) in .cnw usually
                # Skipping strict parsing for now, just raw if found ? 
                # Actually not specified in prompt to parse strictly, just "metadata_raw"
                # Often it's mixed in text nodes. Let's look for simple approach or skip for now if tricky.
                
                # Image
                # <a href="/35/src/123.jpg" target=_blank>...</a>
                # Usually look for 'src' in href
                img_link = element.find('a', href=lambda x: x and '/src/' in x)
                if img_link:
                    post_data['image_url'] = img_link['href']
                else:
                    post_data['image_url'] = None
                
                post_data['is_op'] = is_op
                return post_data

            # OP extraction
            op_data = extract_post_data(thre, is_op=True)
            if op_data:
                save_post(db, thread_obj.id, op_data)

            # --- PROCESS REPLIES ---
            # Replies are in <table> inside or after. 
            # <table border=0><tr><td class="rtd">...</td></tr></table>
            replies = thre.find_all('td', class_='rtd')
            for reply in replies:
                reply_data = extract_post_data(reply, is_op=False)
                if reply_data:
                    save_post(db, thread_obj.id, reply_data)
                    
            db.commit()
            
    except Exception as e:
        logger.error(f"Error parsing page: {e}")
        db.rollback()
    finally:
        db.close()

from datetime import datetime

def save_post(db: Session, thread_id: int, data: dict):
    # Download image if exists
    local_path = None
    if data.get('image_url'):
        local_path = download_image(data['image_url'], data['post_number'])

    # Upsert Post
    stmt = insert(models.Post).values(
        post_number=data['post_number'],
        thread_id=thread_id,
        name=data.get('name'),
        content=data.get('content'),
        image_url=data.get('image_url'),
        local_image_path=local_path,
        is_op=data['is_op'],
        # metadata_raw TODO
    ).on_conflict_do_update(
        index_elements=['post_number'],
        set_={
            'content': data.get('content'),
            'local_image_path': local_path # Update if we downloaded it now
        }
    )
    db.execute(stmt)


def scrape_cycle():
    """Main loop for scraping pages 0-10."""
    logger.info("Starting scrape cycle for pages 0-10")
    
    for i in range(11):
        if i == 0:
            url = f"{BASE_URL}/futaba.htm"
        else:
            url = f"{BASE_URL}/{i}.htm"
            
        logger.info(f"Scraping {url}...")
        html = fetch_page(url)
        
        if html:
            parse_and_save(html)
        
        # Be polite
        time.sleep(2)
        
    logger.info("Scrape cycle completed.")
