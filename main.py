import concurrent
import configparser
import json
import logging
import math
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from urllib.parse import urlparse

import psutil
import undetected_chromedriver as uc
from selenium.common import NoSuchElementException, TimeoutException, WebDriverException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from connection.mongo_connection import get_campaign_setup_active
from connection.redis_connection import decrease_remain_traffic, get_remain_traffic, get_key_proxy_redis, \
    get_proxy_redis, del_proxy_redis


# Filter UA: chỉ giữ Chromium-pure (Chrome/Opera).
# fpscanner CHR_MEMORY check inconsistency giữa UA browser-name vs navigator.deviceMemory:
# UA Firefox/Safari/Edge/MiuiBrowser + deviceMemory=8 (Chrome behavior) → FAIL.
# STEALTH_JS đã spoof rất nhiều Chromium-specific (chrome.runtime, plugins, deviceMemory=8)
# nên dùng UA non-Chromium tạo nhiều inconsistency khác → giảm chất lượng stealth.
_CHROMIUM_UA = re.compile(r'Chrome/\d+|OPR/\d+')
_NON_CHROMIUM_UA = re.compile(r'Firefox|Edg/|CriOS|MiuiBrowser|SamsungBrowser|HuaweiBrowser|UCBrowser|FBAN|Version/.*Safari')

def _is_chromium_ua(ua: str) -> bool:
    return bool(_CHROMIUM_UA.search(ua)) and not _NON_CHROMIUM_UA.search(ua)

with open('user-agent-laptop.json') as f:
    user_agent_laptop = [ua for ua in json.load(f) if _is_chromium_ua(ua)]

with open('user-agent-mobile.json') as f:
    user_agent_mobile = [m for m in json.load(f) if _is_chromium_ua(m.get('user_agent', ''))]

with open('product_urls') as product_urls_file:
    product_url_list = list(filter(None, product_urls_file.read().split("\n")))

config = configparser.ConfigParser()
config.read('config.ini')

# Setup Config
cfg_base_url = os.getenv('USE_BASE_URL', '1') == '1'
# HEADLESS=1 trên Docker/server; mặc định tắt để chạy dev trên macOS không crash
USE_HEADLESS = os.getenv('HEADLESS', '0') == '1'
# Số lần retry khi Chrome fail to start (session not created)
CHROME_START_RETRIES = int(os.getenv('CHROME_START_RETRIES', '2'))
# Giới hạn số Chrome instance khởi động đồng thời — tránh port conflict và RAM spike
_chrome_start_sem = threading.Semaphore(int(os.getenv('CHROME_CONCURRENT_START', '3')))

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%d-%m-%y %H:%M:%S",
    stream=sys.stdout
)
logging.getLogger().setLevel(logging.INFO)


def detect_chrome_full_version(binary_path=None):
    """Lấy full version string của Chrome uc sẽ dùng (vd: 130.0.6723.116).

    Ưu tiên binary_path (CHROME_BINARY env) → fallback Chrome system.
    """
    if binary_path:
        candidates = [binary_path]
    elif sys.platform == 'darwin':
        candidates = [
            '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
            '/Applications/Chromium.app/Contents/MacOS/Chromium',
        ]
    else:
        candidates = ['google-chrome', 'google-chrome-stable', 'chromium-browser', 'chromium']

    for binary in candidates:
        try:
            out = subprocess.run(
                [binary, '--version'], capture_output=True, text=True, timeout=5
            ).stdout.strip()
            match = re.search(r'[\d.]+', out)
            if match:
                return match.group(0)
        except Exception:
            pass
    return 'unknown'


# CHROME_VERSION:
#   - Env CHROME_VERSION set (Docker)  → dùng giá trị đó (int)
#   - Không set                        → None: uc tự detect full version Chrome
#                                        và download ChromeDriver khớp chính xác
_env_ver = os.getenv('CHROME_VERSION', '')
CHROME_VERSION: int | None = int(_env_ver) if _env_ver.isdigit() else None
# CHROME_BINARY: trỏ uc tới binary Chrome cụ thể (vd Chrome for Testing 130 trên macOS
# khi Chrome user là 148 — uc 3.5.5 CDP không tương thích Chrome > ~135).
CHROME_BINARY: str | None = os.getenv('CHROME_BINARY') or None
logging.info(f"Chrome used: {detect_chrome_full_version(CHROME_BINARY)}, version_main={'auto' if CHROME_VERSION is None else CHROME_VERSION}")


# === macOS arm64: ChromeDriver code-signing fix ============================
# uc patch chromedriver (ghi đè chuỗi cdc_) làm HỎNG code signature. Trên Apple
# Silicon macOS thực thi chữ ký nghiêm ngặt → binary chữ ký hỏng vẫn "chạy" nhưng
# KHÔNG mở được cổng HTTP → selenium "Can not connect to the Service" (timeout 30s).
# Ngoài ra uc 3.5.5 chỉ tải bản mac-x64 (chạy qua Rosetta → chậm + flaky).
# Fix: tải ChromeDriver arm64 native khớp version, patch sẵn 1 lần rồi ad-hoc re-sign,
# truyền cho uc qua driver_executable_path. uc thấy is_binary_patched=True nên không
# sửa lại → chữ ký còn nguyên. CHỈ áp dụng darwin/arm64; Linux/Docker giữ nguyên uc default.
_MAC_DRIVER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".chromedriver-mac-arm64")
_mac_driver_path: str | None = None
_mac_driver_lock = threading.Lock()


def _download_arm64_chromedriver(full_version, dest):
    """Tải chromedriver-mac-arm64 đúng full_version từ Chrome for Testing → dest."""
    import io as _io
    import urllib.request
    import zipfile

    url = (
        f"https://storage.googleapis.com/chrome-for-testing-public/"
        f"{full_version}/mac-arm64/chromedriver-mac-arm64.zip"
    )
    with urllib.request.urlopen(url, timeout=60) as r:
        data = r.read()
    with zipfile.ZipFile(_io.BytesIO(data)) as z:
        member = next(n for n in z.namelist() if n.endswith("/chromedriver"))
        with z.open(member) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)
    os.chmod(dest, 0o755)
    subprocess.run(["xattr", "-d", "com.apple.quarantine", dest], capture_output=True)


def _prepare_mac_driver():
    """macOS arm64: trả về path chromedriver arm64 đã patch + ad-hoc signed.

    Trả None khi không phải mac arm64, hoặc khi không xác định được version Chrome
    (caller sẽ fallback về uc default). Kết quả được cache toàn cục (chuẩn bị 1 lần).
    """
    global _mac_driver_path
    if sys.platform != "darwin" or platform.machine() != "arm64":
        return None
    with _mac_driver_lock:
        if _mac_driver_path and os.path.exists(_mac_driver_path):
            return _mac_driver_path
        try:
            from undetected_chromedriver import Patcher

            full_version = detect_chrome_full_version(CHROME_BINARY)
            if not re.match(r"\d+\.\d+\.\d+\.\d+$", full_version):
                logging.warning(
                    f"Không xác định được full version Chrome ({full_version}) → "
                    f"bỏ qua native arm64 driver, để uc tự xử lý"
                )
                return None
            os.makedirs(_MAC_DRIVER_DIR, exist_ok=True)
            drv = os.path.join(_MAC_DRIVER_DIR, "chromedriver")
            if not os.path.exists(drv):
                logging.info(f"Tải ChromeDriver arm64 {full_version} cho macOS...")
                _download_arm64_chromedriver(full_version, drv)
            # Patch sẵn (idempotent) rồi ad-hoc re-sign — uc sẽ KHÔNG patch lại
            patcher = Patcher(executable_path=drv)
            if not patcher.is_binary_patched(drv):
                patcher.patch_exe()
            subprocess.run(["codesign", "--force", "--sign", "-", drv], capture_output=True)
            _mac_driver_path = drv
            logging.info(f"macOS arm64 driver sẵn sàng (patched + ad-hoc signed): {drv}")
            return drv
        except Exception as e:
            logging.warning(
                f"_prepare_mac_driver thất bại: {str(e).split(chr(10))[0]} → fallback uc default"
            )
            return None


STEALTH_JS = r"""
// 1. navigator.webdriver -> false via prototype (khớp với real Chrome, vượt qua WebDriver(New) test)
(() => {
  const proto = Object.getPrototypeOf(navigator);
  const descriptor = Object.getOwnPropertyDescriptor(proto, 'webdriver');
  if (descriptor) {
    // Giữ nguyên configurable/enumerable, chỉ đổi getter trả về false
    Object.defineProperty(proto, 'webdriver', {
      ...descriptor,
      get: () => false
    });
  } else {
    Object.defineProperty(proto, 'webdriver', {
      get: () => false,
      configurable: true,
      enumerable: true
    });
  }
  // Xoá luôn override cũ trên instance nếu có
  try { delete navigator.webdriver; } catch (e) {}
})();

// 2. navigator.plugins -> PluginArray giả với 5 plugin chuẩn của Chrome
(() => {
  const makeMime = (type, suffixes, description, plugin) => {
    const mime = Object.create(MimeType.prototype);
    Object.defineProperties(mime, {
      type:          { value: type },
      suffixes:      { value: suffixes },
      description:   { value: description },
      enabledPlugin: { value: plugin }
    });
    return mime;
  };
  const makePlugin = (name, filename, description, mimes) => {
    const plugin = Object.create(Plugin.prototype);
    Object.defineProperties(plugin, {
      name:        { value: name },
      filename:    { value: filename },
      description: { value: description },
      length:      { value: mimes.length }
    });
    mimes.forEach((m, i) => {
      const mime = makeMime(m.type, m.suffixes, m.description, plugin);
      plugin[i] = mime;
      plugin[m.type] = mime;
    });
    return plugin;
  };

  const pdfMime  = { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' };
  const pdfMime2 = { type: 'text/pdf',        suffixes: 'pdf', description: 'Portable Document Format' };

  const plugins = [
    makePlugin('PDF Viewer',                'internal-pdf-viewer', 'Portable Document Format', [pdfMime, pdfMime2]),
    makePlugin('Chrome PDF Viewer',         'internal-pdf-viewer', 'Portable Document Format', [pdfMime, pdfMime2]),
    makePlugin('Chromium PDF Viewer',       'internal-pdf-viewer', 'Portable Document Format', [pdfMime, pdfMime2]),
    makePlugin('Microsoft Edge PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format', [pdfMime, pdfMime2]),
    makePlugin('WebKit built-in PDF',       'internal-pdf-viewer', 'Portable Document Format', [pdfMime, pdfMime2])
  ];

  const pluginArray = Object.create(PluginArray.prototype);
  Object.defineProperty(pluginArray, 'length', { value: plugins.length });
  plugins.forEach((p, i) => {
    pluginArray[i]      = p;
    pluginArray[p.name] = p;
  });
  pluginArray.item      = function (i) { return plugins[i] || null; };
  pluginArray.namedItem = function (n) { return plugins.find(p => p.name === n) || null; };
  pluginArray.refresh   = function () {};
  Object.defineProperty(navigator, 'plugins', { get: () => pluginArray });

  const mimeTypeArray = Object.create(MimeTypeArray.prototype);
  const mimes = [pdfMime, pdfMime2].map((m) => makeMime(m.type, m.suffixes, m.description, plugins[0]));
  Object.defineProperty(mimeTypeArray, 'length', { value: mimes.length });
  mimes.forEach((m, i) => { mimeTypeArray[i] = m; mimeTypeArray[m.type] = m; });
  mimeTypeArray.item      = function (i) { return mimes[i] || null; };
  mimeTypeArray.namedItem = function (t) { return mimes.find(m => m.type === t) || null; };
  Object.defineProperty(navigator, 'mimeTypes', { get: () => mimeTypeArray });
})();

// 3. navigator.languages
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

// 4. WebGL vendor / renderer spoof — fix SwiftShader detection
(() => {
  const spoof = (parameter, original) => {
    if (parameter === 37445) return 'Intel Inc.';               // UNMASKED_VENDOR_WEBGL
    if (parameter === 37446) return 'Intel Iris OpenGL Engine'; // UNMASKED_RENDERER_WEBGL
    return original;
  };
  const wrap = (proto) => {
    const orig = proto.getParameter;
    proto.getParameter = function (parameter) {
      return spoof(parameter, orig.call(this, parameter));
    };
  };
  if (window.WebGLRenderingContext)  wrap(WebGLRenderingContext.prototype);
  if (window.WebGL2RenderingContext) wrap(WebGL2RenderingContext.prototype);
})();

// 5. performance.memory — giữ logic conditional cũ (đủ dùng).
// CHR_MEMORY test của fpscanner thực ra check fingerprint.deviceMemory đối chiếu UA,
// KHÔNG check performance.memory — xem fix UA pool / deviceMemory conditional bên dưới (mục 8).
if (!performance.memory || Object.keys(performance.memory).length === 0) {
  Object.defineProperty(performance, 'memory', {
    get: () => ({
      jsHeapSizeLimit: 4294705152,
      totalJSHeapSize: 35244183,
      usedJSHeapSize:  16310015
    })
  });
}

// 6. window.chrome
if (!window.chrome) window.chrome = {};
window.chrome.runtime   = window.chrome.runtime   || {};
window.chrome.app       = window.chrome.app       || {
  isInstalled: false,
  InstallState:  { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
  RunningState:  { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' }
};
window.chrome.csi       = window.chrome.csi       || function () {};
window.chrome.loadTimes = window.chrome.loadTimes || function () {};

// 7. Permissions API
(() => {
  const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
  if (originalQuery) {
    window.navigator.permissions.query = (parameters) =>
      parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission, onchange: null })
        : originalQuery(parameters);
  }
})();

// 8. hardwareConcurrency & deviceMemory
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
Object.defineProperty(navigator, 'deviceMemory',         { get: () => 8 });

// 9. Xoá biến CDC của ChromeDriver
['cdc_adoQpoasnfa76pfcZLmcfl_Array',
 'cdc_adoQpoasnfa76pfcZLmcfl_Promise',
 'cdc_adoQpoasnfa76pfcZLmcfl_Symbol',
 'cdc_adoQpoasnfa76pfcZLmcfl_JSON',
 'cdc_adoQpoasnfa76pfcZLmcfl_Object',
 'cdc_adoQpoasnfa76pfcZLmcfl_Proxy'].forEach(k => { try { delete window[k]; } catch (e) {} });
"""


def force_quit_driver(driver):
    """Đóng Chrome và kill toàn bộ process tree, tránh zombie processes."""
    if driver is None:
        return

    # Lấy PID của chromedriver service trước khi quit
    service_pid = None
    try:
        if hasattr(driver, 'service') and driver.service and driver.service.process:
            service_pid = driver.service.process.pid
    except Exception:
        pass

    for fn in (driver.close, driver.quit):
        try:
            fn()
        except Exception:
            pass

    # Đợi Chrome tự exit sau khi quit() — tránh SIGKILL Chrome đang cleanup
    time.sleep(2)

    # Chỉ force-kill nếu process VẪN CÒN CHẠY sau graceful quit + wait
    if service_pid:
        try:
            parent = psutil.Process(service_pid)
            if parent.is_running():
                logging.warning(f"ChromeDriver PID {service_pid} still alive after quit, force killing")
                for child in parent.children(recursive=True):
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                parent.kill()
        except psutil.NoSuchProcess:
            pass  # Đã tự exit — đây là trường hợp bình thường
        except Exception:
            try:
                os.kill(service_pid, 9)
            except Exception:
                pass


def _kill_orphaned_chrome():
    """Kill Chrome orphan từ Ctrl+C hoặc crash — chỉ kill process cũ hơn 60 giây.

    Không kill Chrome đang trong quá trình shutdown bình thường (< 60s) để tránh
    làm macOS window manager bị stuck, gây lỗi cho lần launch tiếp theo.
    """
    MIN_AGE_SECONDS = 60
    try:
        now = time.time()
        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time']):
            try:
                name = (proc.info['name'] or '').lower()
                if 'chrome' not in name:
                    continue
                cmdline_str = ' '.join(proc.info.get('cmdline') or [])
                if '--disable-blink-features=AutomationControlled' not in cmdline_str:
                    continue
                age = now - (proc.info.get('create_time') or now)
                if age > MIN_AGE_SECONDS:
                    logging.warning(f"Killing orphaned Chrome PID {proc.pid} (age {age:.0f}s)")
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:
        pass



def _start_chrome(options_factory):
    """Khởi động Chrome với semaphore + retry.

    options_factory: callable trả về ChromeOptions mới mỗi lần gọi.
    uc mutate ChromeOptions sau khi dùng nên KHÔNG thể tái sử dụng object cũ.
    """
    last_error = None
    for attempt in range(CHROME_START_RETRIES + 1):
        with _chrome_start_sem:
            try:
                kwargs = {'options': options_factory(), 'version_main': CHROME_VERSION}
                if CHROME_BINARY:
                    kwargs['browser_executable_path'] = CHROME_BINARY
                # macOS arm64: dùng driver arm64 đã patch + re-sign (xem _prepare_mac_driver)
                mac_driver = _prepare_mac_driver()
                if mac_driver:
                    kwargs['driver_executable_path'] = mac_driver
                driver = uc.Chrome(**kwargs)
                logging.info(f"Chrome started (attempt {attempt + 1})")
                return driver
            except Exception as e:
                last_error = e
                err_msg = str(e).split('\n')[0]
                is_startup_error = any(kw in err_msg for kw in (
                    "session not created", "cannot connect to chrome",
                    "failed to start", "chrome not reachable",
                    "cannot reuse"
                ))
                if is_startup_error and attempt < CHROME_START_RETRIES:
                    wait = 2 ** (attempt + 1) + random.uniform(0, 1)
                    logging.warning(
                        f"Chrome startup failed (attempt {attempt + 1}/{CHROME_START_RETRIES + 1}), "
                        f"retry in {wait:.1f}s — {err_msg}"
                    )
                    _kill_orphaned_chrome()
                    time.sleep(wait)
                else:
                    raise
    raise last_error


def apply_stealth(driver):
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": STEALTH_JS})
        logging.info("Stealth JS injected OK")
    except Exception as e:
        logging.warning(f"apply_stealth failed: {str(e).split(chr(10))[0]}")


def _pick_geo(geo_config: dict | None) -> str:
    """Weighted random chọn geo từ geo_config của campaign.

    geo_config format: {"vn": 60, "us": 30, "uk": 10} (tổng không cần = 100).
    Trả về chuỗi geo (vd "vn", "us") hoặc "" nếu không có config.
    """
    if not geo_config:
        return ""
    geos = list(geo_config.keys())
    weights = [geo_config[g] for g in geos]
    return random.choices(geos, weights=weights, k=1)[0]


def _is_valid_url(url) -> bool:
    """URL hợp lệ: chuỗi có scheme http(s) và domain."""
    if not isinstance(url, str):
        return False
    try:
        p = urlparse(url.strip())
        return p.scheme in ('http', 'https') and bool(p.netloc)
    except Exception:
        return False


def _parse_proxy(raw):
    """Parse chuỗi proxy → dict(host, port, username, password) hoặc None nếu rỗng/lỗi.

    Nhận cả 3 dạng:
      - ip:port                 → không authen (username/password = None)
      - ip:port:user:pass       → authen (nối tiếp định dạng cũ)
      - user:pass@ip:port       → authen (dạng URL chuẩn)
    Tiền tố scheme (http://, socks5://, ...) nếu có sẽ được bỏ qua.
    """
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if '://' in s:
        s = s.split('://', 1)[1]

    username = password = None
    if '@' in s:
        # user:pass@host:port — password ưu tiên giữ nguyên phần còn lại
        cred, _, host_part = s.rpartition('@')
        username, _, password = cred.partition(':')
    else:
        parts = s.split(':')
        if len(parts) >= 4:
            # host:port:user:pass — password có thể chứa ':' → gộp phần dư
            host_part = f"{parts[0]}:{parts[1]}"
            username = parts[2]
            password = ':'.join(parts[3:])
        else:
            host_part = s

    host, _, port = host_part.rpartition(':')
    if not host or not port:
        return None
    return {
        'host': host,
        'port': port,
        'username': username or None,
        'password': password or None,
    }


def _make_proxy_auth_extension(host, port, username, password, scheme="http"):
    """Sinh thư mục extension tạm (Manifest V3) để Chrome tự authen proxy.

    Chrome flag --proxy-server không nhúng được credentials; extension xử lý
    chrome.webRequest.onAuthRequired để trả username/password. Trả về đường dẫn
    thư mục tạm — caller có trách nhiệm xóa sau khi driver quit.
    """
    ext_dir = tempfile.mkdtemp(prefix="proxy_auth_ext_")

    manifest = {
        "name": "Proxy Auth",
        "version": "1.0.0",
        "manifest_version": 3,
        "permissions": ["proxy", "webRequest", "webRequestAuthProvider"],
        "host_permissions": ["<all_urls>"],
        "background": {"service_worker": "background.js"},
        "minimum_chrome_version": "108",
    }

    # json.dumps để escape an toàn mọi ký tự đặc biệt trong credentials/host
    background_js = f"""
const config = {{
  mode: "fixed_servers",
  rules: {{
    singleProxy: {{
      scheme: {json.dumps(scheme)},
      host: {json.dumps(host)},
      port: parseInt({json.dumps(str(port))}, 10)
    }},
    bypassList: ["localhost", "127.0.0.1"]
  }}
}};
chrome.proxy.settings.set({{ value: config, scope: "regular" }}, function () {{}});

chrome.webRequest.onAuthRequired.addListener(
  function (details) {{
    return {{ authCredentials: {{ username: {json.dumps(username)}, password: {json.dumps(password)} }} }};
  }},
  {{ urls: ["<all_urls>"] }},
  ["blocking"]
);
"""

    with open(os.path.join(ext_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f)
    with open(os.path.join(ext_dir, "background.js"), "w") as f:
        f.write(background_js)
    return ext_dir


def run(campaign):
    campaign_id = campaign.get("campaign_id")
    shop_id = campaign.get("shop_id")

    campaign_start_dt = datetime.now()
    campaign_start = time.time()

    logging.info(
        f"[START] Campaign={campaign_id}, "
        f"Shop={shop_id}, "
        f"Thread={threading.get_ident()}, "
        f"Start={campaign_start_dt.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    if get_remain_traffic(campaign.get('shop_id')) <= 0:
        logging.info(f"Shop ID: {campaign.get('shop_id')} has no traffic")
        return None

    actions = campaign.get('actions', [])
    base_urls = campaign.get('base_urls', [])
    product_urls = campaign.get('product_urls', []) if campaign.get('product_urls') else product_url_list
    use_base_url = campaign.get('use_base_url', True)

    # --- Chọn device & proxy TRƯỚC, cố định cho cả session (kể cả retry) ---
    is_device_mobile = random.randint(0, 100) <= campaign.get('mobile_usage_rate', 65)
    device_mobile_selected = None
    user_agent_str = None

    if is_device_mobile:
        device_mobile_selected = random.choice(user_agent_mobile)
        user_agent_str = device_mobile_selected['user_agent']
        logging.info(f"Device: mobile — {device_mobile_selected['device_name']}")
    else:
        user_agent_str = random.choice(user_agent_laptop)
        logging.info(f"Device: laptop — {user_agent_str}")

    use_proxy = campaign.get('use_proxy', True)
    geo = _pick_geo(campaign.get('geo_config'))
    key_proxy = get_key_proxy_redis(geo) if use_proxy else None
    proxy = get_proxy_redis(key_proxy) if key_proxy else None
    logging.info(f"Proxy: {proxy} (use_proxy={use_proxy}, geo={geo or 'default vn'})")

    # Parse proxy: có authen → dùng extension; không authen → --proxy-server như cũ
    proxy_info = _parse_proxy(proxy)
    proxy_ext_dir = None
    proxy_server_arg = None
    if proxy_info and proxy_info['username'] and proxy_info['password']:
        try:
            proxy_ext_dir = _make_proxy_auth_extension(
                proxy_info['host'], proxy_info['port'],
                proxy_info['username'], proxy_info['password'],
            )
            logging.info(f"Proxy authen → extension ({proxy_info['host']}:{proxy_info['port']})")
        except Exception as e:
            logging.error(f"Tạo proxy-auth extension thất bại: {str(e).split(chr(10))[0]}")
    elif proxy_info:
        proxy_server_arg = f"http://{proxy_info['host']}:{proxy_info['port']}"
    elif proxy:
        # Parser không nhận dạng được nhưng vẫn có chuỗi proxy → giữ hành vi cũ
        proxy_server_arg = f"http://{proxy}"

    # Factory: tạo ChromeOptions MỚI mỗi lần gọi (uc mutate object sau khi dùng)
    def make_options():
        opts = uc.ChromeOptions()
        if USE_HEADLESS:
            opts.add_argument("--headless=new")
            opts.add_argument("--disable-gpu")

        # --disable-extensions sẽ tắt LUÔN extension proxy → khi có proxy authen
        # dùng --disable-extensions-except để chỉ cho phép đúng extension đó chạy
        if proxy_ext_dir:
            opts.add_argument(f"--disable-extensions-except={proxy_ext_dir}")
        else:
            opts.add_argument("--disable-extensions")
        opts.add_argument("--mute-audio")
        opts.add_argument("--no-first-run")
        opts.add_argument("--disable-default-apps")
        # --no-sandbox và --disable-dev-shm-usage chỉ cần trên Linux/Docker
        if sys.platform != 'darwin':
            opts.add_argument("--no-sandbox")
            opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument("--lang=en-US,en")
        opts.add_argument("--enable-precise-memory-info")
        opts.add_argument("--disable-features=ChromeWhatsNewUI,Translate")
        opts.add_argument("--ignore-certificate-errors")
        opts.add_argument("--ignore-ssl-errors")
        opts.add_argument("--allow-insecure-localhost")
        opts.add_argument("--allow-running-insecure-content")
        opts.add_argument(f"--user-agent={user_agent_str}")
        if proxy_ext_dir:
            opts.add_argument(f"--load-extension={proxy_ext_dir}")
        elif proxy_server_arg:
            opts.add_argument(f"--proxy-server={proxy_server_arg}")
        return opts

    logging.info(f"Chrome version_main={'auto' if CHROME_VERSION is None else CHROME_VERSION}, headless={USE_HEADLESS}")

    chrome_driver = None
    try:
        chrome_start = time.time()
        chrome_driver = _start_chrome(make_options)
        logging.info(f"Chrome ready ({time.time() - chrome_start:.2f}s)")

        chrome_driver.set_page_load_timeout(60)

        # Inject stealth trước khi navigate bất kỳ URL nào
        apply_stealth(chrome_driver)

        if device_mobile_selected is not None:
            chrome_driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
                "width": device_mobile_selected['viewport_width'],
                "height": device_mobile_selected['viewport_height'],
                "deviceScaleFactor": 3.5,
                "mobile": True,
                "screenWidth": device_mobile_selected['viewport_width'],
                "screenHeight": device_mobile_selected['viewport_height']
            })
            chrome_driver.execute_cdp_cmd("Network.setUserAgentOverride", {
                "userAgent": device_mobile_selected['user_agent'],
                "platform": "Android" if "Android" in device_mobile_selected['user_agent'] else "iPhone"
            })

        if use_base_url:
            target_url = random.choice(base_urls) + random.choice(product_urls) + "&mo_source=cpc_2311"
        else:
            target_url = random.choice(base_urls)

        # 7.1: Nếu campaign có init_urls (referrer pool), truy cập referrer trước
        # rồi click thẻ <a href=target> để giả lập user navigate từ trang khác.
        # Target site sẽ thấy Referer header = init_url, tự nhiên hơn direct navigation.
        # Filter chỉ giữ URL hợp lệ — nếu pool toàn URL invalid → coi như rỗng (direct nav)
        init_urls = [u for u in (campaign.get('init_urls') or []) if _is_valid_url(u)]
        referrer = random.choice(init_urls) if init_urls else None
        referrer_ok = False

        if referrer:
            logging.info(f"Referrer: {referrer} → target: {target_url}")
            try:
                chrome_driver.get(referrer)
                # 7.2.1: Inject <a> và click — preserve Referer header khi navigate
                chrome_driver.execute_script(
                    "var a = document.createElement('a');"
                    "a.href = arguments[0];"
                    "a.id = '__bot_target_link';"
                    "a.style.display = 'none';"
                    "document.body.appendChild(a);"
                    "a.click();",
                    target_url
                )
                WebDriverWait(chrome_driver, 30).until(EC.url_changes(referrer))
                referrer_ok = True
            except (WebDriverException, TimeoutException) as e:
                logging.warning(f"Referrer flow failed: {str(e).split(chr(10))[0]} — fallback direct")

        # 7.2.2: Không có referrer hợp lệ hoặc referrer fail → direct navigation
        if not referrer_ok:
            logging.info(f"Navigating to: {target_url}")
            try:
                chrome_driver.get(target_url)
            except WebDriverException as e:
                error_message = str(e).split('\n')[0]
                if "ERR_PROXY_CONNECTION_FAILED" in error_message:
                    del_proxy_redis(key_proxy)
                    logging.error(f"Proxy failed — removed from pool. Proxy: {proxy}, Time: {round(time.time() - start_time, 2)}s")
                elif "ERR_TIMED_OUT" in error_message or "timeout" in error_message.lower():
                    logging.error(f"Page load timeout. Proxy: {proxy}, Time: {round(time.time() - start_time, 2)}s")
                else:
                    logging.error(f"Navigation error. Time: {round(time.time() - start_time, 2)}s, Error: {error_message}")
                return None

        if chrome_driver is not None and is_error_page(chrome_driver):
            logging.error(f"Error page detected after navigation, skipping campaign")
            return None

        decrease_remain_traffic(campaign.get('shop_id'))

        for action in actions:
            if chrome_driver is None:
                break
            # Retry nếu gặp error page giữa chừng
            for index in range(5):
                if not is_error_page(chrome_driver):
                    break
                if index < 4:
                    logging.warning(f"Error page detected, refreshing... ({index + 1}/4)")
                    chrome_driver.refresh()
                    time.sleep(2)
                else:
                    raise Exception("Error page persisted after 4 retries")

            action_type = action['type']
            if action_type == "random page":
                action_random_page(chrome_driver, action.get('source', ''), By.CSS_SELECTOR)
            elif action_type == "load":
                action_load(chrome_driver, action.get('source', ''), By.CSS_SELECTOR)
            elif action_type == "click":
                action_click(chrome_driver)
            elif action_type == "scroll":
                action_scroll(chrome_driver)
            time.sleep(action['delay'])

        campaign_end_dt = datetime.now()
        campaign_duration = time.time() - campaign_start

        logging.info(
            f"[SUCCESS] Campaign={campaign_id}, "
            f"Shop={shop_id}, "
            f"Thread={threading.get_ident()}, "
            f"End={campaign_end_dt.strftime('%Y-%m-%d %H:%M:%S')}, "
            f"Duration={campaign_duration:.2f}s"
        )

    except Exception as e:
        error_message = str(e).split('\n')[0]
        campaign_end_dt = datetime.now()
        campaign_duration = time.time() - campaign_start

        logging.error(
            f"[FAILED] Campaign={campaign_id}, "
            f"Shop={shop_id}, "
            f"Thread={threading.get_ident()}, "
            f"End={campaign_end_dt.strftime('%Y-%m-%d %H:%M:%S')}, "
            f"Duration={campaign_duration:.2f}s, "
            f"Error={error_message}"
        )
        return False
    finally:
        force_quit_driver(chrome_driver)
        if proxy_ext_dir:
            shutil.rmtree(proxy_ext_dir, ignore_errors=True)


def is_error_page(driver):
    try:
        h1_elements = driver.find_elements(By.TAG_NAME, 'h1')
        if not h1_elements:
            return False
        return "site can’t be reached" in h1_elements[0].text or "site can't be reached" in h1_elements[0].text
    except Exception:
        return False


# Chưa được map trong run() action loop (chỉ action_click được dùng cho type='click').
# Giữ lại như click strategy thay thế khi cần scope hẹp theo 1 area cụ thể.
def action_click_v2(driver, source, by=By.XPATH):
    try:
        source_a_tag = "//a[starts-with(@href,'/') and not(@target) and not(@aria-label='slide')]"
        action = ActionChains(driver)
        area_click = driver.find_element(by, source)
        elements = area_click.find_elements(By.XPATH, source_a_tag)
        elements = list(filter(lambda x: x.is_displayed(), elements))
        if len(elements) > 0:
            element = random.choice(elements)
            action.scroll_to_element(element).click(element).perform()
    except NoSuchElementException:
        logging.error("Element not found")
    except Exception as e:
        logging.error(f"action_click_v2 error: {str(e).split(chr(10))[0]}")


def action_click(driver):
    try:
        driver.execute_script("""
            var elements = document.querySelectorAll('a[href^="/"]:not([href*="cart"])');
            if (elements.length > 0) {
                var randomIndex = Math.floor(Math.random() * elements.length);
                elements[randomIndex].scrollIntoView({ behavior: 'smooth', block: 'center' });
                setTimeout(() => { elements[randomIndex].click(); }, 1000);
            }
        """)
    except Exception as e:
        logging.warning(f"action_click error: {str(e).split(chr(10))[0]}")


def action_load(driver, source="", by=By.CSS_SELECTOR):
    try:
        action = ActionChains(driver)
        elements = driver.find_elements(by, source)
        if len(elements) > 0:
            element = random.choice(elements)
            action.scroll_to_element(element).click(element).perform()
    except Exception as e:
        logging.warning(f"action_load error: {str(e).split(chr(10))[0]}")


def action_scroll(driver):
    try:
        total_distance = random.randint(600, 1400)
        scrolled = 0
        while scrolled < total_distance:
            step = random.randint(40, 120)
            driver.execute_script(f"window.scrollBy({{top: {step}, left: 0, behavior: 'smooth'}});")
            scrolled += step
            time.sleep(random.uniform(0.15, 0.45))
        if random.random() < 0.3:
            driver.execute_script(
                f"window.scrollBy({{top: -{random.randint(80, 200)}, left: 0, behavior: 'smooth'}});"
            )
            time.sleep(random.uniform(0.3, 0.8))
    except Exception as e:
        logging.warning(f"action_scroll error: {str(e).split(chr(10))[0]}")


def action_random_page(driver, source="", by=By.CSS_SELECTOR):
    try:
        action = ActionChains(driver)
        elements = driver.find_elements(by, source)
        if len(elements) > 0:
            element = random.choice(elements)
            action.click(element).perform()
    except Exception as e:
        logging.warning(f"action_random_page error: {str(e).split(chr(10))[0]}")


def scheduler():
    current_time = datetime.now()
    logging.info(f"Start scheduler at: {current_time.strftime('%d/%m/%Y %H:%M:%S')}")
    current_hour = current_time.hour
    campaign_setup_active = get_campaign_setup_active()
    total_traffic = 0
    for campaign in campaign_setup_active:
        hourly_traffic = campaign['hours_scheduler'].get(str(current_hour), 0)
        total_traffic += hourly_traffic
    if total_traffic == 0:
        logging.info(f"No traffic at hour {current_hour}")
        return None
    max_thread = math.ceil(total_traffic / 10)
    logging.info(f"Total traffic: {total_traffic}, Max thread: {max_thread}")
    campaigns = []
    for campaign in campaign_setup_active:
        logging.info(f"Start campaign: {campaign['campaign_id']}")
        hourly = campaign['hours_scheduler'][str(current_hour)]
        for _ in range(round(hourly)):
            campaigns.append(campaign)
    random.shuffle(campaigns)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_thread) as executor:
        futures = []
        for campaign in campaigns:
            future = executor.submit(run, campaign)
            futures.append(future)
            time.sleep(3)
        for future in concurrent.futures.as_completed(futures):
            future.result()


if __name__ == '__main__':
    scheduler()
