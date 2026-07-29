import json
import random
import threading
import time
from datetime import datetime, timedelta

import requests


class ProxyManager:

    API_PROXY_CURRENT = "current"
    API_PROXY_NEW = "new"

    def __init__(self, api_keys):
        self.api_keys = api_keys
        self.proxies = {}

        # Init proxies
        self._update_proxies(api_keys)

        self.refresh_interval = 180  # 3 minutes
        self._refresh_thread = threading.Thread(target=self._refresh_proxies, daemon=True)
        self._refresh_thread.start()

    def _refresh_proxies(self):
        while True:
            needed_refresh_keys = self._get_expired_keys()
            self._update_proxies(needed_refresh_keys)

            time.sleep(self.refresh_interval)

    def _update_proxies(self, api_keys):
        for api_key in api_keys:
            proxy_data = self._get_proxy(api_key, self.API_PROXY_NEW) or self._get_proxy(api_key, self.API_PROXY_CURRENT)
            if proxy_data:
                self.proxies[api_key] = proxy_data
                continue

    def _get_proxy(self, api_key, api_type):
        """
        :type api_key: str
        :type api_type: str
        :rtype dict:
        """
        url = f"https://tmproxy.com/api/proxy/get-{api_type}-proxy"
        payload = {"api_key": api_key, "id_location": 1}

        response = requests.post(url, json=payload)
        response_json = response.json()

        response_code = response_json["code"]
        if response_code != 0:
            return None

        response_data = {
            "code": response_json["code"],
            "message": "",
            "data": {
                "https": f"{response_json['data']['https']}",
                "expired_at": response_json['data']["expired_at"]
            }
        }

        data = response_data["data"]

        expired_time = datetime.strptime(data['expired_at'], '%H:%M:%S %d/%m/%Y')
        created_time = expired_time - timedelta(minutes=30)

        data['created_at'] = created_time
        data['expired_at'] = expired_time

        return data

    def _get_ready_proxy(self):
        readiness_proxies = self._get_ready_proxies()
        if len(readiness_proxies) == 0:
            return None

        random_proxy_document = random.choice(readiness_proxies)
        return random_proxy_document.get('https', None)

    def _get_ready_proxies(self):
        current_time = datetime.now()
        readiness_proxies = [
            value for key, value in self.proxies.items() if value['created_at'] > (current_time - timedelta(minutes=4))
        ]
        return readiness_proxies

    def _get_expired_keys(self):
        current_time = datetime.now()
        expired_keys = {key for key, value in self.proxies.items() if value['created_at'] < (current_time - timedelta(minutes=7))}
        return expired_keys


# Example usage
if __name__ == "__main__":
    proxy_manager = ProxyManager([
        "0ddcba20db303761f0bd98c48a25eeac",
        "125d10de37da3b78d150907e18368f23",
        "0a0f7ca4812d8aae861048e8565344e5",
        "dfe7b6f4ae0ae661bfdfe78f7d517c2b",
        "f1811835a972b29e188c95615a097954",
        "a1b724097f396ed9987fea8a20f11ba8",
        "ebb75c50a47e471b8581b6cecb11ae2d",
        "7e8cefc48693296c1b97a26845fa2d8d",
        "f8c85f0ae6b3110ec6970cad62beddee",
        "976002a2f574c2aa14edfef0e674c352",
        "222d36e196341ab9d47d0211344ef8ba",
        "42d92b313f321702e13e07afa64dee67",
        "b35b3101cffcf673a6b8e415d85a22dc",
        "e23356eeda35ad4c85f69afd2e14702e",
        "d9777d2fc40850252d4560318bb7d810",
        "e92e593b9c4abcabca3eac4e0cfc97f5",
        "26e7d5d88f96338688ac0b966ee573c7",
        "1c6af0b00da093fc000b68992893137a",
        "fef4dcf633b34d32c263cc87d9a73a3b",
        "f791f6d239ea4c20b0623869fabd94b1"
    ])

    # Keep the program running for demonstration purposes
    try:
        while True:
            # You can access the proxies dictionary anytime
            print("Initial proxies:", proxy_manager.proxies)
            time.sleep(180)
    except KeyboardInterrupt:
        pass
