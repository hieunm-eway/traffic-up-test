import configparser
import logging
import os
import random
from datetime import datetime
from typing import Optional
import redis

config = configparser.ConfigParser()
config.read('config.ini')
# Set up Redis client
redis_config = config['REDIS']
redis_host = os.getenv('REDIS_HOST', redis_config['HOST'])
redis_port = os.getenv('REDIS_PORT', redis_config['PORT'])
redis_upstream_db = os.getenv('UPSTREAM_DB', 0)
redis_forward_db = os.getenv('FORWARD_DB', 1)
redis_cli = redis.Redis(host=redis_host, port=redis_port, db=redis_upstream_db,decode_responses=True)
forward_proxy_redis_cli = redis.Redis(
    host=redis_host,
    port=redis_port,
    db=redis_forward_db,
    decode_responses=True
)

def get_remain_traffic(shop_id):
    try:
        current_date = datetime.now().strftime('%Y%m%d')
        remain = redis_cli.get(f"traffic-up-{shop_id}-{current_date}")
        return int(remain) if remain else 0
    except redis.exceptions.ResponseError:
        return 0


def decrease_remain_traffic(shop_id):
    try:
        current_date = datetime.now().strftime('%Y%m%d')
        redis_cli.decr(f"traffic-up-{shop_id}-{current_date}", 1)
    except redis.exceptions.ResponseError:
        pass


def increase_remain_traffic(shop_id, amount=1):
    try:
        current_date = datetime.now().strftime('%Y%m%d')
        key = f"traffic-up-{shop_id}-{current_date}"
        redis_cli.incr(key, amount)
        # Calculate seconds until 23:59:59 of the current day
        now = datetime.now()
        end_of_day = datetime.combine(now.date(), datetime.max.time())
        seconds_until_end_of_day = int((end_of_day - now).total_seconds())

        # Set expiration time for the key if it's a new key or reset TTL each time
        if redis_cli.ttl(key) == -1:  # -1 means key has no expiration
            redis_cli.expire(key, seconds_until_end_of_day)
    except redis.exceptions.ResponseError:
        pass


def get_key_proxy_redis(geo: str = "") -> Optional[str]:
    """
    Lấy ngẫu nhiên một proxy key từ Redis theo GEO chỉ định.
    Args:
        geo (str): Mã GEO (us, uk, ...). Mặc định "" là vn.
    Returns:
        Optional[str]: Key proxy ngẫu nhiên, hoặc 0 nếu không tìm thấy hoặc có lỗi.
    """
    try:
        # Chuẩn hóa GEO: lowercase, loại bỏ khoảng trắng
        geo_code = str(geo).strip().lower() if geo else ""
        # Tạo pattern key theo GEO
        pattern = f"traffic-up-proxy{geo_code}-*"
        # Sử dụng scan thay vì keys để tránh block Redis trên production
        proxy_keys = []
        cursor = 0
        while True:
            cursor, keys = forward_proxy_redis_cli.scan(
                cursor,
                match=pattern,
                count=50  # Lấy 50 key mỗi lần để tránh quá tải
            )
            proxy_keys.extend(keys)
            if cursor == 0:
                break
        # Nếu không tìm thấy key nào
        if not proxy_keys:
            logging.warning(f"No proxy keys found for GEO: {geo_code} with pattern: {pattern}")
            return 0
        # Random chọn một key
        selected_key = random.choice(proxy_keys)
        return selected_key
    except redis.exceptions.ResponseError as e:
        logging.error(f"Redis ResponseError when getting proxy for GEO {geo}: {str(e)}")
        return 0
    except redis.exceptions.ConnectionError as e:
        logging.error(f"Redis ConnectionError when getting proxy for GEO {geo}: {str(e)}")
        return 0
    except redis.exceptions.TimeoutError as e:
        logging.error(f"Redis TimeoutError when getting proxy for GEO {geo}: {str(e)}")
        return 0
    except Exception as e:
        # Bắt tất cả các exception khác không ngờ tới
        logging.error(f"Unexpected error when getting proxy for GEO {geo}: {str(e)}", exc_info=True)
        return 0



def get_proxy_redis(key):
    try:
        return forward_proxy_redis_cli.get(key)
    except redis.exceptions.ResponseError:
        return 0


def del_proxy_redis(key):
    try:
        return redis_cli.delete(key)
    except redis.exceptions.ResponseError:
        return 0
