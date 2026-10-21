import os
from datetime import date

# Only posts published from this date are analyzed (earlier posts were only
# partially collected: just those still alive when scraping started)
START_DATE = date.fromisoformat(os.getenv("DASHBOARD_START_DATE", "2026-01-01"))
