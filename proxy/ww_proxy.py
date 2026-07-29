import json
import random
import threading
import time
from datetime import datetime, timedelta

import requests


class ProxyManager:
    API_PROXY_CURRENT = "current"
    API_PROXY_NEW = "available"

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
            proxy_data = self._get_proxy(api_key, self.API_PROXY_NEW) or self._get_proxy(api_key,
                                                                                         self.API_PROXY_CURRENT)
            if proxy_data:
                self.proxies[api_key] = proxy_data
                continue

    def _get_proxy(self, api_key, api_type):
        """
        :type api_key: str
        :type api_type: str
        :rtype dict:
        """
        url = f"https://wwproxy.com/api/client/proxy/{api_type}"
        payload = {"key": api_key, "provinceId": -1}

        response = requests.get(url, params=payload)
        response_json = response.json()

        response_code = response_json["errorCode"]
        if response_code != 0:
            return None

        response_data = {
            "code": response_json["errorCode"],
            "message": "",
            "data": {
                "https": f"{response_json['data']['ipAddress']}:{response_json['data']['port']}",
                "expired_at": response_json['data']["expiredTime"]
            }
        }

        data = response_data["data"]

        expired_time = datetime.strptime(data['expired_at'], '%Y/%m/%d %H:%M:%S')
        created_time = expired_time - timedelta(minutes=30)

        data['created_at'] = created_time
        data['expired_at'] = expired_time

        return data

    def _get_ready_proxy(self):
        readiness_proxies = self._get_ready_proxies()
        if len(readiness_proxies) == 0:
            return None

        random_proxy_document = random.choice(readiness_proxies)
        return random_proxy_document.get('ipAddress', None)

    def _get_ready_proxies(self):
        current_time = datetime.now()
        readiness_proxies = [
            value for key, value in self.proxies.items() if value['created_at'] > (current_time - timedelta(minutes=4))
        ]
        return readiness_proxies

    def _get_expired_keys(self):
        current_time = datetime.now()
        expired_keys = {key for key, value in self.proxies.items() if
                        value['created_at'] < (current_time - timedelta(minutes=7))}
        return expired_keys


# Example usage
if __name__ == "__main__":
    proxy_manager = ProxyManager([
        "UK-c0628b4f-754c-4291-b559-2b6422e8703f"
    ])

    # Keep the program running for demonstration purposes
    try:
        while True:
            # You can access the proxies dictionary anytime
            print("Initial proxies:", proxy_manager.proxies)
            print("Initial proxies:", [proxy_info['https'] for proxy_info in proxy_manager.proxies.values() if 'https' in proxy_info])
            time.sleep(180)
    except KeyboardInterrupt:
        pass
