import os

# No background ESPN fetching from the daily-results warmer during tests.
os.environ.setdefault("SPORTIQ_RESULTS_WARM", "0")
os.environ.setdefault("INDEXNOW_ENABLED", "0")   # never ping IndexNow from tests
os.environ.setdefault("JJANGDOL_LINKS_ENABLED", "0")   # never call jjangdoltv.com from tests
