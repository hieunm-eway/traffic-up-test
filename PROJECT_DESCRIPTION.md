# Traffic Buff Bot - Mô Tả Chi Tiết Project

## 1. Tổng Quan

Đây là một **hệ thống tăng traffic tự động (Traffic Buff Bot)** sử dụng Selenium kết hợp với `undetected_chromedriver` để mô phỏng người dùng thật truy cập vào các website thương mại điện tử (như `dienmayxanh.com`, `avakids`, `etsy`...). Mục tiêu của hệ thống là tạo ra lượng traffic giả lập theo các chiến dịch (campaign) đã được cấu hình sẵn, với khả năng:

- Giả lập user agent của thiết bị mobile và laptop.
- Sử dụng proxy động (rotating proxy) để tránh bị phát hiện/chặn IP.
- Thực hiện các hành động như cuộn trang, click ngẫu nhiên, mở trang sản phẩm.
- Lập lịch chạy theo từng giờ trong ngày dựa trên cấu hình từ MongoDB.
- Quản lý hạn ngạch (quota) traffic theo từng shop qua Redis.
- **Bypass anti-bot detection** qua stealth JS injection (WebDriver, Plugins, WebGL, memory...).

---

## 2. Cấu Trúc Thư Mục

```
app/
├── main.py                       # Entry point - scheduler, logic mô phỏng traffic, stealth
├── test_run.py                   # Chạy thử 1 campaign đơn lẻ (không cần MongoDB/scheduler)
├── config.ini                    # Cấu hình MongoDB, Redis, API key proxy
├── .env                          # Biến môi trường (override config.ini)
├── Dockerfile                    # Build image dựa trên tuna99/python-uc:13.120
├── requirements.txt              # Dependencies Python
├── product_urls                  # Danh sách URL sản phẩm (dạng URL-encoded)
├── user-agent-laptop.json        # Pool user agent cho laptop
├── user-agent-mobile.json        # Pool user agent cho mobile (kèm viewport)
├── undetected_chromedriver       # Binary chromedriver tùy biến (16MB)
├── .chromedriver-mac-arm64/      # (auto-gen, gitignored) Driver arm64 patched+signed cho macOS dev — xem §8.5
├── connection/
│   ├── mongo_connection.py       # Kết nối MongoDB - đọc campaign-setup
│   └── redis_connection.py       # Kết nối Redis - quản lý quota traffic & proxy
└── proxy/
    ├── tm_proxy.py               # Provider proxy: tmproxy.com (proxy VN)
    └── ww_proxy.py               # Provider proxy: wwproxy.com (proxy quốc tế)
```

---

## 3. Luồng Hoạt Động

### 3.1. Scheduler (`main.py:scheduler`)
1. Lấy giờ hiện tại (`current_hour`).
2. Truy vấn MongoDB collection `campaign-setup` để lấy các campaign đang `is_activated=True`.
3. Với mỗi campaign, đọc `hours_scheduler[current_hour]` → số lượng traffic cần chạy trong giờ này.
4. Tổng hợp `total_traffic` → tính `max_thread = ceil(total_traffic / 10)`.
5. Tạo danh sách `campaigns` nhân bản theo số traffic, **shuffle ngẫu nhiên**.
6. Dùng `ThreadPoolExecutor` chạy song song `run(campaign)`, submit cách nhau 3 giây.

### 3.2. Run Campaign (`main.py:run`)
Mỗi luồng thực hiện 1 phiên mô phỏng người dùng:

1. **Kiểm tra quota**: `get_remain_traffic(shop_id)` từ Redis. Nếu = 0 → bỏ qua.
2. **Setup Chrome Options**: headless (env-controlled), no-sandbox, disable GPU, ignore SSL, lang, window-size...
3. **Random thiết bị**: so `mobile_usage_rate` (mặc định 65%) để chọn mobile/laptop. Mobile set viewport qua CDP `Emulation.setDeviceMetricsOverride` + `Network.setUserAgentOverride`.
4. **Lấy proxy**: weighted random GEO từ `campaign.geo_config` (vd `{"vn":60,"us":30}`) qua `_pick_geo()` → scan Redis pattern `traffic-up-proxy{geo}-*` → random 1 key → lấy chuỗi proxy. Không có `geo_config` → dùng pool VN mặc định. Chuỗi proxy được `_parse_proxy()` nhận **cả proxy thường lẫn có authen** (xem mục 6.1):
   - **Không authen** (`host:port`) → `--proxy-server=http://host:port` như cũ.
   - **Có authen** (`host:port:user:pass` hoặc `user:pass@host:port`) → sinh **Chrome extension tạm** (Manifest V3, `_make_proxy_auth_extension`) load qua `--load-extension`; extension xử lý `chrome.webRequest.onAuthRequired` để cấp credentials (flag `--proxy-server` không nhúng được user/pass). Thư mục extension tạm được `shutil.rmtree` dọn ở `finally` sau khi driver quit.
5. **Khởi tạo trình duyệt** qua `_start_chrome()` (có retry + semaphore — xem mục 8).
6. **Inject STEALTH_JS** qua CDP `Page.addScriptToEvaluateOnNewDocument` trước khi navigate.
7. **Truy cập URL đích** — 2 chế độ tuỳ `campaign.init_urls`:
   - **7.1 Referrer flow** (`init_urls` có URL hợp lệ): pick random 1 referrer (filter qua `_is_valid_url`) → `driver.get(referrer)` → JS inject `<a href=target_url>` → `a.click()` → `WebDriverWait` đợi URL đổi. Target site thấy `Referer: <init_url>`, tự nhiên hơn direct.
   - **7.2 Direct nav** (`init_urls` rỗng / không URL nào hợp lệ / referrer fail): `driver.get(target_url)` thẳng với `base_url + product_url + "&mo_source=cpc_2311"`.
8. **Trừ quota** Redis (`decrease_remain_traffic`).
9. **Thực thi chuỗi actions**: scroll, click, load, random page — có retry nếu gặp error page.
10. **Cleanup**: `force_quit_driver()` kill toàn bộ process tree (xem mục 8).

---

## 4. Anti-Bot Stealth (`STEALTH_JS`)

Script JS được inject qua CDP `Page.addScriptToEvaluateOnNewDocument` (chạy TRƯỚC page script):

| Test trên sannysoft.com | Override | Kết quả |
|---|---|---|
| WebDriver (Old) | `navigator.webdriver` = `undefined` (instance) | ✓ PASS |
| **WebDriver (New)** | Override `Navigator.prototype.webdriver` getter → `false` | ✓ PASS |
| Plugins Length | `navigator.plugins` = PluginArray giả 5 plugin PDF | ✓ PASS |
| Plugins instanceof PluginArray | Dùng `Object.create(PluginArray.prototype)` | ✓ PASS |
| WebGL Renderer (SwiftShader) | Override `WebGLRenderingContext.getParameter` → Intel GPU | ✓ PASS |
| CHR_MEMORY | `performance.memory` được định nghĩa với giá trị thực | ✓ PASS |
| window.chrome | `chrome.runtime`, `.app`, `.csi`, `.loadTimes` đủ bộ | ✓ PASS |
| Permissions | `notifications` query trả về đúng `Notification.permission` | ✓ PASS |
| hardwareConcurrency | 8 | ✓ PASS |
| deviceMemory | 8 | ✓ PASS |
| CDC variables | Xoá `cdc_adoQpoasnfa76pfcZLmcfl_*` | ✓ PASS |

### 4.1. UA Pool — chỉ Chromium-pure
`user-agent-laptop.json` và `user-agent-mobile.json` được **filter ngay khi load** (`main.py:_is_chromium_ua`), chỉ giữ UA chứa `Chrome/` hoặc `OPR/` và KHÔNG chứa `Firefox|Edg/|CriOS|MiuiBrowser|SamsungBrowser|HuaweiBrowser|UCBrowser|FBAN|Version/.*Safari`.

Lý do: fpscanner `CHR_MEMORY` check inconsistency UA-browser vs `navigator.deviceMemory`:
- UA Firefox/Safari/Edge + `deviceMemory=8` (Chrome behavior) → INCONSISTENT → FAIL.

STEALTH_JS đã spoof nhiều Chromium-specific (chrome.runtime, plugins PDF, deviceMemory=8) — dùng UA non-Chromium tạo thêm inconsistency, không tăng diversity hữu ích.

Khi thêm UA mới vào JSON, theo regex trên để tránh fail CHR_MEMORY.

---

## 5. Lưu Trữ Dữ Liệu

### 5.1. MongoDB (`connection/mongo_connection.py`)
- **Database**: `traffic-meta` / **Collection**: `campaign-setup`
- Filter theo `CAMPAIGN_NAME` env (nhiều campaign phân cách bằng dấu phẩy).

**Document campaign mẫu:**
```json
{
  "campaign_id": "etsy-llm-rkt_3000_...",
  "shop_id": "etsy-llm-rkt",
  "is_activated": true,
  "mobile_usage_rate": 65,
  "base_urls": ["https://..."],
  "product_urls": ["https%3A%2F%2F..."],
  "actions": [
    {"type": "scroll", "delay": 25},
    {"type": "click",  "delay": 30}
  ],
  "hours_scheduler": {"0": 125, "1": 125, ...},
  "use_proxy": true,
  "use_base_url": true,
  "geo_config": {"vn": 60, "us": 30, "uk": 10},
  "init_urls": ["https://www.lorddecor.com/", "https://example.com/"]
}
```

**Field tuỳ chọn:**
- `geo_config` (dict): tỉ lệ traffic theo GEO. Tổng không cần = 100, weight sẽ tự normalize. Vắng → dùng pool VN mặc định.
- `init_urls` (list[str]): pool referrer URL. Mỗi run random 1 URL hợp lệ → visit referrer → click `<a>` đến target. URL invalid bị filter; pool rỗng / toàn invalid → direct nav.

### 5.2. Redis (`connection/redis_connection.py`)

| Key Pattern | Mục đích |
|---|---|
| `traffic-up-{shop_id}-{YYYYMMDD}` | Quota traffic còn lại theo shop, TTL đến hết ngày |
| `traffic-up-proxy{geo}-*` | Pool proxy theo GEO (vn, us, uk...) |

---

## 6. Quản Lý Proxy (`proxy/`)

| Provider | File | Endpoint |
|---|---|---|
| tmproxy.com (VN) | `tm_proxy.py` | `POST /api/proxy/get-{new\|current}-proxy` |
| wwproxy.com (quốc tế) | `ww_proxy.py` | `GET /api/client/proxy/{available\|current}` |

Cả hai chạy daemon thread refresh proxy mỗi 3 phút. Proxy được push vào Redis bởi một worker riêng — `main.py` chỉ đọc từ Redis (không dùng trực tiếp 2 class này).

### 6.1. Định dạng proxy & proxy authen

`_parse_proxy()` (`main.py`) nhận **3 dạng** chuỗi proxy lưu trong Redis (tiền tố scheme như `http://`, `socks5://` nếu có sẽ được bỏ qua):

| Dạng | Authen? | Xử lý |
|---|---|---|
| `host:port` | Không | `--proxy-server=http://host:port` (luồng cũ, tương thích ngược) |
| `host:port:user:pass` | Có | Extension proxy-auth (nối tiếp định dạng cũ — chỉ cần thêm đuôi `:user:pass`) |
| `user:pass@host:port` | Có | Extension proxy-auth (dạng URL chuẩn) |

**Vì sao dùng extension thay vì MITM (mitmproxy/selenium-wire):** extension giữ Chrome kết nối **thẳng** tới proxy upstream nên **TLS fingerprint Chrome thật được bảo toàn** — quan trọng cho stealth. MITM terminate TLS → ClientHello bị thay bằng Python, dễ bị detect, lại tốn thêm process/RAM. Extension dùng đúng API native của Chrome (`chrome.proxy` + `webRequestAuthProvider`), tương thích `--headless=new`.

> **Lưu ý:** khi load extension proxy, `make_options` đổi `--disable-extensions` → `--disable-extensions-except={ext}` để không vô hiệu hóa nhầm chính extension đó.
> **Edge-case:** password chứa cả `:` lẫn `@` trong dạng `host:port:user:pass` có thể parse sai — hiếm gặp; ưu tiên dạng `user:pass@host:port` cho password phức tạp.

---

## 7. Cấu Hình

### 7.1. `config.ini`
- `[REDIS]` HOST/PORT local.
- `[MONGODB]` URI MongoDB local (port 27019).
- `[TM_PROXY]` / `[WW_PROXY]` API keys.

### 7.2. Biến Môi Trường (`.env` hoặc Docker env)

| Biến | Mặc định | Mô tả |
|---|---|---|
| `MONGO_URI` | (config.ini) | MongoDB URI |
| `CAMPAIGN_NAME` | — | Tên campaign cần chạy |
| `HEADLESS` | `0` | `1` = chạy headless (bắt buộc trên Docker/server), `0` = visible (macOS dev) |
| `CHROME_VERSION` | _(auto)_ | Pin major version cho uc (Docker: `120`, macOS dev: `130`). Không set → uc tự detect & download ChromeDriver khớp. |
| `CHROME_BINARY` | _(Chrome system)_ | Trỏ uc tới binary cụ thể qua `browser_executable_path`. Bắt buộc trên macOS dev nếu Chrome user > ~135 (uc 3.5.5 incompat). |
| `CHROME_START_RETRIES` | `2` | Số lần retry khi Chrome fail to start |
| `CHROME_CONCURRENT_START` | `3` | Max Chrome instance khởi động đồng thời |
| `USE_BASE_URL` | `1` | Ghép base_url + product_url |
| `PROXY_NAME` | `tm_proxy` | Provider proxy đang dùng |

---

## 8. Cơ Chế Ổn Định & Tối Ưu Resource

### 8.1. Pin Chrome Version (`CHROME_VERSION` + `CHROME_BINARY`)
uc 3.5.5 chỉ tương thích Chrome ≤ ~135. Chrome user thường (148+) làm uc fail `session not created: cannot connect to chrome` ngay khi spawn.

Giải pháp: pin cả **major version** và **binary path**:
- **Docker (prod)**: `CHROME_VERSION=120` — Chrome trong image `tuna99/python-uc:13.120`. Không cần `CHROME_BINARY` (uc dùng Chrome system trong image).
- **macOS dev**: `CHROME_VERSION=130` + `CHROME_BINARY=./chrome-for-testing/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing`. Tải Chrome for Testing 130 từ https://googlechromelabs.github.io/chrome-for-testing/ — binary chính thức Google, không auto-update.

> ⚠️ **Phải đúng kiến trúc máy.** Trên Apple Silicon (`uname -m` = `arm64`) bắt buộc trỏ tới thư mục `chrome-mac-arm64`, KHÔNG phải `chrome-mac-x64`. Trỏ sai path (vd `chrome-mac-x64` không tồn tại) làm `uc.Chrome()` chết ngay ~2s với **error message rỗng** (`Campaign failed. Error:`), đồng thời dòng `Chrome used: unknown` ở log startup là dấu hiệu nhận biết (`detect_chrome_full_version` không chạy được `--version`).

Khi đổi `CHROME_VERSION` hoặc `CHROME_BINARY`, **phải xóa cache uc patched driver** để uc re-download bản khớp:
```bash
rm -f ~/Library/Application\ Support/undetected_chromedriver/undetected_chromedriver
# macOS arm64: xóa thêm driver native đã cache (xem §8.5) để buộc tải lại đúng version
rm -rf ./.chromedriver-mac-arm64
```

### 8.2. Chrome Startup Retry (`_start_chrome`)
Với các lỗi transient còn lại, cơ chế retry với cleanup:

```
Attempt 1 → fail → kill orphaned Chrome → wait 2s
Attempt 2 → fail → kill orphaned Chrome → wait 4s
Attempt 3 → raise
```

Trước mỗi retry gọi `_kill_orphaned_chrome()` để kill Chrome còn sót từ lần trước (nhận dạng bằng flag `--disable-blink-features=AutomationControlled` trong cmdline, tránh kill Chrome của user).

Các lỗi được retry: `session not created`, `cannot connect to chrome`, `failed to start`, `chrome not reachable`.
Lỗi khác → raise ngay.

### 8.2. Semaphore Startup (`_chrome_start_sem`)
`threading.Semaphore(CHROME_CONCURRENT_START)` — giới hạn số Chrome instance được phép khởi động đồng thời. Ngăn port conflict và RAM spike khi nhiều thread cùng gọi `uc.Chrome()`.

### 8.3. Force Quit Driver (`force_quit_driver`)
`driver.close()` + `driver.quit()` từ Selenium đôi khi để lại zombie Chrome process. `force_quit_driver()` bổ sung:
1. Lấy PID của chromedriver service trước khi quit.
2. Sau khi `quit()`, dùng `psutil` kill toàn bộ process tree (bao gồm Chrome child process).
3. Fallback `os.kill(pid, 9)` nếu `psutil` không khả dụng.

### 8.4. Scroll Tự Nhiên
`action_scroll()` cuộn theo bước nhỏ (40–120px), random pause (0.15–0.45s), 30% xác suất cuộn ngược → qua behavior analytics (Akamai, DataDome...).

### 8.5. macOS arm64: ChromeDriver code-signing fix (`_prepare_mac_driver`)

**Vấn đề.** `undetected_chromedriver` patch binary chromedriver (ghi đè block `{window.cdc...}` để né detection). Việc sửa bytes này **làm hỏng code signature** của binary. Trên **Apple Silicon (arm64)**, macOS thực thi chữ ký nghiêm ngặt: binary chữ ký hỏng vẫn khởi động được nhưng **không mở nổi cổng HTTP của chromedriver** → selenium poll cổng 60×0.5s rồi báo lỗi:

```
WebDriverException: Can not connect to the Service .../undetected_chromedriver   (~30s)
```

Thêm nữa, uc 3.5.5 trên macOS **chỉ tải bản `mac-x64`** (hardcode trong `Patcher._set_platform_name`), chạy qua Rosetta → chậm và flaky (đặc biệt lần chạy đầu sau khi patch). Lỗi này **chỉ xảy ra ở dev local macOS arm64**; Linux/Docker (prod) không bị nên không cần đổi gì.

**Cách chẩn đoán** (đã kiểm chứng): `codesign -v <driver>` báo `invalid signature (code or signature have been modified)`; chạy driver standalone thấy tiến trình sống nhưng cổng đóng. Sau `codesign --force --sign - <driver>` thì cổng mở lại ngay.

**Giải pháp** (`main.py:_prepare_mac_driver` + `_download_arm64_chromedriver`), chỉ kích hoạt khi `sys.platform == 'darwin'` và `platform.machine() == 'arm64'`:

1. Tải **ChromeDriver arm64 native** đúng full version (lấy từ `detect_chrome_full_version(CHROME_BINARY)`) từ Chrome for Testing → `./.chromedriver-mac-arm64/chromedriver`. Dùng arm64 để **bỏ hẳn Rosetta**.
2. **Patch sẵn 1 lần** bằng `Patcher.patch_exe()`, rồi **ad-hoc re-sign**: `codesign --force --sign - <driver>`.
3. Truyền `driver_executable_path=<driver>` vào `uc.Chrome()`. uc thấy `is_binary_patched()=True` nên **không sửa lại** → chữ ký vẫn hợp lệ.

Kết quả được **cache toàn cục** (chuẩn bị 1 lần, thread-safe qua `threading.Lock`). Nếu không xác định được version Chrome (vd `CHROME_BINARY` trỏ sai) → trả `None`, fallback về hành vi uc mặc định. Thư mục `.chromedriver-mac-arm64/` được gitignore.

> Linux/Docker: `_prepare_mac_driver()` trả `None` ngay → `uc.Chrome()` giữ nguyên luồng cũ (uc tự tải/patch driver, không cần re-sign).

---

## 9. Triển Khai

- **Docker base image**: `tuna99/python-uc:13.120` (Chrome 120).
- **Env bắt buộc trên server**: `HEADLESS=1`, `CHROME_VERSION=120`.
- Lệnh khởi chạy: `pythozsn3 main.py` (entrypoint Dockerfile).

### Dependencies (`requirements.txt`)
```
selenium==4.16.0            # WebDriver
undetected_chromedriver~=3.5.4  # Anti-detection ChromeDriver
pymongo~=4.6.1             # MongoDB client
redis~=5.0.2               # Redis client
requests~=2.31.0           # HTTP (proxy API)
python-dotenv~=1.0.0       # .env loader
psutil~=5.9.0              # Process management (force kill Chrome)
schedule~=1.2.1            # (khai báo nhưng chưa dùng trực tiếp)
chardet~=4.0.0
setuptools
```

---

## 10. Điểm Cần Lưu Ý
- Để test thủ công: `python3 test_run.py` (không cần MongoDB, không cần quota Redis).
- Sau khi đổi `CHROME_VERSION` / `CHROME_BINARY`: xóa cache uc driver patched (xem §8.1) để uc re-patch ChromeDriver mới.
