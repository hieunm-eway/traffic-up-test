"""
Test thủ công một campaign đơn lẻ mà không cần MongoDB/scheduler.

Setup traffic/proxy:
set traffic-up-etsy-stb-rkt-20260610 3000
set traffic-up-proxyus-1 51.79.191.62:8034:admin123:123123

Chạy: python3 test_run.py
"""
import logging
import sys

from main import run

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%d-%m-%y %H:%M:%S",
    stream=sys.stdout,
)
logging.getLogger().setLevel(logging.INFO)

TEST_CAMPAIGN = {
    "_id": "etsy-stb-rkt_2500_1778812021641",
    "campaign_id": "etsy-stb-rkt_2500_1778812021641",
    "shop_id": "etsy-stb-rkt",
    "is_activated": True,
    "mobile_usage_rate": 65,
    "base_urls": ["https://bot.sannysoft.com/?url="],
    "product_urls": [
        "https%3A%2F%2Fwww.etsy.com%2F"
    ],
    "actions": [
        {"type": "scroll", "delay": 1},
        {"type": "click",  "delay": 1},
        {"type": "scroll", "delay": 1},
    ],
    "geo_config": {"us": 100},
    "use_proxy": True,
    "use_base_url": True,
    "hours_scheduler": {str(h): 1 for h in range(24)},
    "offer_global": False,
    "worker_machine": "",
    "updated_at": "2026-05-15 00:00:00",
}

if __name__ == "__main__":
    try:
        result = run(campaign=TEST_CAMPAIGN)
        if result is False:
            logging.error("----- Test FAILED -----")
            sys.exit(1)
        logging.info("----- Test finished -----")
    except Exception as e:
        logging.error(str(e))
        sys.exit(1)
