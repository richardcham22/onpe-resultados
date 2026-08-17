"""Scrape both rounds of the 2026 general election in full.

Mesa codes exist in two blocks (probed against the official portals):
000001-088xxx (domestic + overseas) and 900001-904xxx.
"""
import time

import scraper
from db import init_db

RANGES = [(1, 88999), (900001, 905999)]
WORKERS = 20

init_db()

for proceso in ("EG2026", "SEP2026"):
    for lo, hi in RANGES:
        print(f"=== {proceso}: {lo:06d}-{hi:06d} ===", flush=True)
        scraper.start(start=lo, end=hi, workers=WORKERS, proceso=proceso)
        time.sleep(2)
        while scraper.is_running():
            time.sleep(15)
        print(f"=== {proceso}: {lo:06d}-{hi:06d} terminado ===", flush=True)

print("SCRAPE COMPLETO — ambas vueltas", flush=True)
