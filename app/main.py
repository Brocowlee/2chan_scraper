import logging
import time
import os
import schedule
from app.database import engine
from app import models
from app.scraper import scrape_cycle

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

def run_scraper():
    """
    Main scraping function.
    """
    logger.info("Scraping job started...")
    try:
        scrape_cycle()
    except Exception as e:
        logger.error(f"Scraping cycle failed: {e}")
    logger.info("Scraping job finished.")

def init_db():
    """Initialize database tables"""
    logger.info("Initializing database...")
    # Import models to ensure they are registered with Base.metadata
    # (Already imported via from app import models)
    models.Base.metadata.create_all(bind=engine)
    logger.info("Database initialized.")

def main():
    logger.info("Starting 2chan Scraper Application")
    
    # Initialize DB (create tables if not exist)
    init_db()

    # Get interval from env
    interval_minutes = int(os.getenv("SCRAPE_INTERVAL_MINUTES", "15"))
    logger.info(f"Scheduling scraper to run every {interval_minutes} minutes")

    # Schedule the job
    schedule.every(interval_minutes).minutes.do(run_scraper)
    
    # Run immediately on startup
    run_scraper()

    # Loop
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    main()
