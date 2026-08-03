import configparser
from bson.regex import Regex
import os
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

config = configparser.ConfigParser()
config.read('config.ini')

# Set up MongoDB client
mongodb_config = config['MONGODB']
mongo_uri = os.getenv('MONGO_URI', mongodb_config['MONGO_URI'])
mongo_database = os.getenv('MONGO_DATABASE', mongodb_config['MONGO_DATABASE'])
campaign_setup_collection_name = os.getenv(
    'COLLECTION_CAMPAIGN_SETUP', mongodb_config['COLLECTION_CAMPAIGN_SETUP'])
campaign_name = os.getenv('CAMPAIGN_NAME', mongodb_config['CAMPAIGN_NAME'])
mongo_client = MongoClient(
    mongo_uri,
    serverSelectionTimeoutMS=60000, 
    connectTimeoutMS=40000,          
    socketTimeoutMS=40000,        
)
traffic_buff_db = mongo_client[mongo_database]
campaign_setup_collection = traffic_buff_db[campaign_setup_collection_name]


def get_campaign_setup_active():
    campaign_setup = campaign_setup_collection.find(
        {"is_activated": True, "campaign_id": {"$in": [x.strip() for x in campaign_name.split(',')]}},
        {"_id": 0, "campaign_id": 1, "shop_id": 1, "mobile_usage_rate": 1, "base_urls": 1, "product_urls": 1,
         "actions": 1, "hours_scheduler": 1, "use_proxy": 1, "use_base_url": 1, "extend_traffic_percent": 1,
         "geo_config": 1, "init_urls": 1})
    # Iterate through the cursor to access the results
    return [document for document in campaign_setup]
