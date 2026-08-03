import configparser
from bson.regex import Regex
import os
from dotenv import load_dotenv
from pymongo import MongoClient
import logging
import re


load_dotenv()

config = configparser.ConfigParser()
config.read('config.ini')

logging.basicConfig(level=logging.DEBUG)
logging.getLogger("pymongo").setLevel(logging.DEBUG)


# Set up MongoDB client
mongodb_config = config['MONGODB']
mongo_uri = os.getenv('MONGO_URI', mongodb_config['MONGO_URI'])

mongo_database = os.getenv('MONGO_DATABASE', mongodb_config['MONGO_DATABASE'])
campaign_setup_collection_name = os.getenv(
    'COLLECTION_CAMPAIGN_SETUP', mongodb_config['COLLECTION_CAMPAIGN_SETUP'])
campaign_name = os.getenv('CAMPAIGN_NAME', mongodb_config['CAMPAIGN_NAME'])

mongo_client = MongoClient(mongo_uri)
safe_uri = re.sub(r"://([^:]+):([^@]+)@", r"://\1:******@", mongo_uri)

print("=" * 80)
print("MongoDB Debug")
print(f"URI: {safe_uri}")
print(f"Database: {mongo_database}")
print(f"Collection: {campaign_setup_collection_name}")
print("=" * 80)

mongo_client = MongoClient(
    mongo_uri,
    serverSelectionTimeoutMS=30000,
    connectTimeoutMS=30000,
    socketTimeoutMS=30000,
)

try:
    print("Ping...")
    print(mongo_client.admin.command("ping"))

    print("Hello...")
    hello = mongo_client.admin.command("hello")
    print(hello)

    print("Nodes:", mongo_client.nodes)
    print("Primary:", mongo_client.primary)
except Exception:
    import traceback
    traceback.print_exc()

traffic_buff_db = mongo_client[mongo_database]
campaign_setup_collection = traffic_buff_db[campaign_setup_collection_name]


def get_campaign_setup_active():
    print("Before find()")
    print("Nodes:", mongo_client.nodes)
    print("Primary:", mongo_client.primary)
    campaign_setup = campaign_setup_collection.find(
        {"is_activated": True, "campaign_id": {"$in": [x.strip() for x in campaign_name.split(',')]}},
        {"_id": 0, "campaign_id": 1, "shop_id": 1, "mobile_usage_rate": 1, "base_urls": 1, "product_urls": 1,
         "actions": 1, "hours_scheduler": 1, "use_proxy": 1, "use_base_url": 1, "extend_traffic_percent": 1,
         "geo_config": 1, "init_urls": 1})
    # Iterate through the cursor to access the results
    return [document for document in campaign_setup]
