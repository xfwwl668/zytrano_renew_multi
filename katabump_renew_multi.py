"""
KataBump 自动续期脚本（多账号 / GitHub Actions / CloakBrowser 版）

默认目标来自用户给出的页面：
https://dashboard.katabump.com/servers/edit?id=303320

环境变量：
- KATABUMP_ACCOUNTS_JSON: 多账号 JSON，支持 list 或 {"accounts": [...]}。
  账号字段支持 email/username/user + password/pwd。
  每个账号可选 server_ids/server_urls 覆盖全局目标。
- KATABUMP_EMAIL / KATABUMP_USERNAME + KATABUMP_PASSWORD: 单账号降级。
- KATABUMP_SERVER_IDS: 全局服务器 ID，逗号/空格/换行分隔。默认 303320。
- KATABUMP_SERVER_URLS: 全局服务器编辑页 URL，逗号/空格/换行分隔。
- WXPUSHER_TOKEN / WXPUSHER_UID: 可选微信推送。
"""

import json
import logging
import os
import random
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


BASE_URL = "https://dashboard.katabump.com"
LOGIN_URL = f"{BASE_URL}/auth/login"
SERVERS_URL = f"{BASE_URL}/servers"
DEFAULT_SERVER_IDS = ["303320"]
SCRIPT_VERSION = "katabump-renew-20260605-modal-strict-v3"

SCREENSHOT_DIR = Path("./katabump_screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

WXPUSHER_TOKEN = os.environ.get("WXPUSHER_TOKEN", "")
WXPUSHER_UID = os.environ.get("WXPUSHER_UID", "")

RESULT_NOTICE_TIMEOUT = 15


def mask(value: str, show: int = 3) -> str:
    if not value or len(value) <= show * 2:
        return "***"
    return value[:show] + "***" + value[-show:]


def split_items(raw: str) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in re.split(r"[,\s]+", raw) if item.strip()]


def safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", value or "unknown")[:48] or "unknown"


def normalize_edit_url(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if value.isdigit():
        return f"{BASE_URL}/servers/edit?{urlencode({'id': value})}"
    if value.startswith("/"):
        return urljoin(BASE_URL, value)
    return urljoin(BASE_URL + "/", value)


def server_id_from_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        query_id = parse_qs(parsed.query).get("id", [""])[0]
        if query_id:
            return query_id
        match = re.search(r"/(\d+)(?:[/#?]|$)", parsed.path)
        return match.group(1) if match else ""
    except Exception:
        return ""


def load_accounts() -> list[dict]:
    raw_content = ""
    source_info = ""

    env_json = os.environ.get("KATABUMP_ACCOUNTS_JSON")
    if env_json:
        raw_content = env_json.strip()
        source_info = "环境变量 KATABUMP_ACCOUNTS_JSON"
    else:
        local_file = Path("katabump_accounts.json")
        if local_file.exists():
            raw_content = local_file.read_text(encoding="utf-8").strip()
            source_info = "本地 katabump_accounts.json 文件"

    if not raw_content:
        email = os.environ.get("KATABUMP_EMAIL") or os.environ.get("KATABUMP_USERNAME")
        password = os.environ.get("KATABUMP_PASSWORD")
        if email and password:
            log.info("未检测到 KataBump 多账号 JSON，降级使用单账号环境变量。")
            return [{"email": email, "password": password}]
        raise ValueError("❌ 没有任何可供执行的 KataBump 账号源。")

    try:
        data = json.loads(raw_content)
        if isinstance(data, list):
            accounts_list = data
        elif isinstance(data, dict):
            if isinstance(data.get("accounts"), list):
                accounts_list = data["accounts"]
            elif isinstance(data.get("data"), list):
                accounts_list = data["data"]
            else:
                raise ValueError("JSON 字典结构中未包含 accounts 或 data 数组")
        else:
            raise ValueError("JSON 顶层必须是 list 或 dict")

        valid_accounts = []
        for idx, item in enumerate(accounts_list):
            if not isinstance(item, dict):
                log.warning(f"[{source_info}] 索引 {idx} 不是对象，已跳过")
                continue
            email = item.get("email") or item.get("username") or item.get("user")
            password = item.get("password") or item.get("pwd")
            if not email or not password:
                log.warning(f"[{source_info}] 索引 {idx} 缺少 email/password，已跳过")
                continue

            account = {"email": str(email), "password": str(password)}
            if item.get("server_ids") is not None:
                account["server_ids"] = item.get("server_ids")
            if item.get("server_urls") is not None:
                account["server_urls"] = item.get("server_urls")
            valid_accounts.append(account)

        if not valid_accounts:
            raise ValueError("清洗后没有可用账号")

        log.info(f"✅ [{source_info}] 捕获 {len(valid_accounts)} 个 KataBump 可用账号")
        return valid_accounts
    except Exception as e:
        raise ValueError(f"❌ KataBump 账号配置解析失败 ({source_info}): {e}")


def to_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return split_items(str(value))


def account_targets(account: dict) -> list[str]:
    raw_urls = to_list(account.get("server_urls")) or split_items(os.environ.get("KATABUMP_SERVER_URLS", ""))
    raw_ids = to_list(account.get("server_ids")) or split_items(os.environ.get("KATABUMP_SERVER_IDS", ""))

    targets = [normalize_edit_url(url) for url in raw_urls]
    if not targets:
        targets.extend(normalize_edit_url(server_id) for server_id in (raw_ids or DEFAULT_SERVER_IDS))

    seen = set()
    unique_targets = []
    for url in targets:
        if url and url not in seen:
            unique_targets.append(url)
            seen.add(url)
    return unique_targets


def take_screenshot(page, name: str):
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        page.screenshot(path=str(SCREENSHOT_DIR / f"{ts}_{safe_name(name)}.png"), full_page=True)
    except Exception:
        pass


def get_text(page) -> str:
    try:
        return page.inner_text("body") or ""
    except Exception:
        return ""


def human_delay(min_s=0.4, max_s=1.1):
    time.sleep(random.uniform(min_s, max_s))


def js_eval(page, script: str, *args):
    try:
        return page.evaluate(script, *args)
    except Exception:
        return None


def ensure_viewport(page, width: int = 1280, height: int = 800) -> bool:
    """确保页面 viewport 已正确初始化，避免 'Viewport size not available' 错误。"""
    try:
        vp = page.viewport_size
        if vp and vp.get("width") and vp.get("height"):
            return True
        page.set_viewport_size({"width": width, "height": height})
        time.sleep(0.3)
        return True
    except Exception as e:
        log.warning(f"⚠️ KataBump viewport 初始化异常: {e}")
        try:
            page.set_viewport_size({"width": width, "height": height})
            return True
        except Exception:
            return False


def is_cf_blocked(page) -> bool:
    """检测当前页面是否被 Cloudflare 拦截（含 URL + 文本双重检测）。"""
    try:
        url = (page.url or "").lower()
        # CF 挑战页面会跳转到这些路径
        if any(p in url for p in ("/cdn-cgi/challenge-platform/", "/cdn-cgi/", "challenges.cloudflare.com")):
            return True
        body = get_text(page).lower()
        if "verify you are human" in body:
            return True
        if "cloudflare" in body and any(k in body for k in ("security check", "security challenge", "checking your browser")):
            return True
        if "just a moment" in body and "cloudflare" in body:
            return True
        return False
    except Exception:
        return False


def wait_cf_pass(page, timeout=45) -> bool:
    """等待 CF 拦截解除，每秒检测一次，解除后额外等待页面稳定。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_cf_blocked(page):
            # 额外等待 0.8s 让页面 JS 完成渲染
            time.sleep(0.8)
            if not is_cf_blocked(page):
                return True
        time.sleep(1)
    return False


def navigate(page, url: str, timeout=45) -> bool:
    """导航到目标 URL，自动处理 CF 拦截、重载和超时。"""
    ensure_viewport(page)
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
    except Exception:
        pass
    # 等待页面稳定
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    if not is_cf_blocked(page):
        return True
    log.info(f"🛡️ KataBump 检测到 CF 拦截，等待通过（最多 {timeout}s）...")
    if wait_cf_pass(page, timeout=timeout):
        return True
    # 第一次 reload 重试
    try:
        page.reload(wait_until="domcontentloaded", timeout=30000)
        time.sleep(1)
    except Exception:
        pass
    if wait_cf_pass(page, timeout=30):
        return True
    # 第二次 reload 重试（等待更长）
    log.warning("⚠️ KataBump CF 拦截持续，二次 reload 重试...")
    take_screenshot(page, "katabump_cf_stuck")
    try:
        page.reload(wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
    except Exception:
        pass
    return wait_cf_pass(page, timeout=20)


def has_turnstile(page) -> bool:
    """检测页面是否存在 CF Turnstile 组件。"""
    marker = js_eval(page, """
        () => Boolean(
            document.querySelector('.cf-turnstile') ||
            document.querySelector('iframe[src*="challenges.cloudflare.com"]') ||
            document.querySelector('input[name="cf-turnstile-response"]') ||
            document.querySelector('[data-sitekey]')
        )
    """)
    return bool(marker)


def _safe_bounding_box(locator_or_element, retries: int = 5, interval: float = 0.6):
    """安全获取元素 bounding_box，带重试，避免 viewport 未就绪时崩溃。"""
    for i in range(retries):
        try:
            box = locator_or_element.bounding_box()
            if box and box.get("width", 0) > 0 and box.get("height", 0) > 0:
                return box
        except Exception as e:
            if i == retries - 1:
                log.warning(f"⚠️ bounding_box 获取失败（已重试 {retries} 次）: {e}")
        time.sleep(interval)
    return None


def _clamp_coords(x: float, y: float, vp: dict) -> tuple[float, float]:
    """将坐标限制在 viewport 范围内，防止 out-of-bounds 点击。"""
    margin = 5
    x = max(margin, min(x, vp.get("width", 1280) - margin))
    y = max(margin, min(y, vp.get("height", 800) - margin))
    return x, y


def _try_click_turnstile_frame(page, cf_frame) -> bool:
    """
    精确点击 Turnstile iframe 内的 checkbox。
    策略优先级：
      1. 在 frame 内用 locator 定位 input[type=checkbox] 直接 click（最准确）
      2. 在 frame 内用 label 点击（部分实现用 label 包裹）
      3. frame_element bounding_box 坐标点击（fallback）
    """
    vp = page.viewport_size or {"width": 1280, "height": 800}

    # 策略 1：在 iframe 内部直接定位 checkbox
    if cf_frame:
        for sel in ['input[type="checkbox"]', 'label', '[role="checkbox"]']:
            try:
                inner = cf_frame.locator(sel).first
                inner.wait_for(state="visible", timeout=3000)
                inner.click(timeout=3000)
                log.info(f"🎯 KataBump Turnstile 内部精确点击: {sel}")
                return True
            except Exception:
                continue

    # 策略 2：frame_element bounding_box 坐标点击
    try:
        if cf_frame:
            frame_el = cf_frame.frame_element()
            box = _safe_bounding_box(frame_el)
        else:
            iframe_locator = page.locator('iframe[src*="challenges.cloudflare.com"]').first
            box = _safe_bounding_box(iframe_locator)

        if box:
            # Turnstile checkbox 约在 iframe 左侧 25px、垂直居中位置
            x = box["x"] + min(28, box["width"] * 0.18)
            y = box["y"] + box["height"] / 2
            x, y = _clamp_coords(x, y, vp)
            # 模拟人类移动轨迹：先移动到附近，再移到目标
            page.mouse.move(x + random.uniform(-10, 10), y + random.uniform(-8, 8))
            time.sleep(random.uniform(0.15, 0.35))
            page.mouse.move(x, y)
            time.sleep(random.uniform(0.2, 0.5))
            page.mouse.click(x, y)
            log.info(f"🎯 KataBump Turnstile 坐标点击: ({x:.0f}, {y:.0f})")
            return True
    except Exception as e:
        log.warning(f"⚠️ KataBump Turnstile 坐标点击失败: {e}")

    # 策略 3：JS 事件注入（最后手段，不依赖坐标）
    js_eval(page, """
        () => {
            const iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
            if (!iframe) return;
            const rect = iframe.getBoundingClientRect();
            const cx = rect.left + Math.min(28, rect.width * 0.18);
            const cy = rect.top + rect.height / 2;
            ['mousemove','mousedown','mouseup','click'].forEach(type => {
                iframe.dispatchEvent(new MouseEvent(type, {
                    bubbles: true, cancelable: true, view: window,
                    clientX: cx, clientY: cy
                }));
            });
        }
    """)
    log.info("🎯 KataBump Turnstile JS 事件注入完成。")
    return False  # 返回 False 表示不确定是否成功，继续等待 token


def click_turnstile_checkbox(page, timeout=50) -> bool:
    """
    CF Turnstile 全流程处理器（深度优化版）。
    流程：
      1. 确保 viewport 就绪
      2. 等待 Turnstile 组件出现（最多 3s）
      3. 等待 CF iframe 加载完成（最多 12s）
      4. 多策略点击 checkbox
      5. 等待 token 就绪（最多 timeout 秒）
      6. 若 token 未就绪，补救重点击，再等 15s
    """
    ensure_viewport(page)

    # 等待 turnstile 组件出现（最多 3s）
    for _ in range(6):
        if has_turnstile(page):
            break
        time.sleep(0.5)
    if not has_turnstile(page):
        log.info("ℹ️ KataBump 页面无 Turnstile 组件，直接通过。")
        return True

    def token_ready() -> bool:
        val = js_eval(page, """
            (() => {
                function deepQuery(root, sel) {
                    let el = root.querySelector(sel);
                    if (el) return el;
                    for (const host of root.querySelectorAll('*')) {
                        if (host.shadowRoot) {
                            el = deepQuery(host.shadowRoot, sel);
                            if (el) return el;
                        }
                    }
                    return null;
                }
                const el = deepQuery(document, 'input[name="cf-turnstile-response"]');
                return el ? (el.value || '').length > 10 : false;
            })()
        """)
        return bool(val)

    def token_value() -> str:
        val = js_eval(page, """
            (() => {
                const el = document.querySelector('input[name="cf-turnstile-response"]');
                return el ? (el.value || '') : '';
            })()
        """)
        return str(val or "")

    # 等待 CF iframe 加载，最多 12 秒
    cf_frame = None
    for _ in range(24):
        # 同时检查 token（有时 CloakBrowser 直接完成，无需等 iframe）
        if token_ready():
            log.info("✅ KataBump Turnstile token 已自动完成，无需点击。")
            return True
        for frame in page.frames:
            if "challenges.cloudflare.com" in (frame.url or ""):
                cf_frame = frame
                break
        if cf_frame:
            break
        time.sleep(0.5)

    if not cf_frame:
        log.warning("⚠️ KataBump CF iframe 未找到，将尝试 locator 降级点击。")

    # 第一次点击
    _try_click_turnstile_frame(page, cf_frame)

    # 等待 token，最多 timeout 秒；每 3s 检查一次是否需要重点击
    deadline = time.time() + timeout
    last_retry_at = time.time()
    retry_interval = 12  # 每 12 秒没有 token 就再点一次
    click_count = 1

    while time.time() < deadline:
        if token_ready():
            tv = token_value()
            log.info(f"✅ KataBump Turnstile token 验证通过（token 长度={len(tv)}）。")
            return True
        # 定期重点击（最多补点 2 次）
        if click_count < 3 and (time.time() - last_retry_at) >= retry_interval:
            log.info(f"🔄 KataBump Turnstile 补救重点击（第 {click_count + 1} 次）...")
            # 刷新 cf_frame 引用（iframe 可能重新加载）
            cf_frame = None
            for frame in page.frames:
                if "challenges.cloudflare.com" in (frame.url or ""):
                    cf_frame = frame
                    break
            _try_click_turnstile_frame(page, cf_frame)
            last_retry_at = time.time()
            click_count += 1
        time.sleep(0.5)

    log.error(f"❌ KataBump Turnstile 在 {timeout}s 内未完成验证（已点击 {click_count} 次）。")
    take_screenshot(page, "katabump_turnstile_failed")
    return False


def is_login_page(page) -> bool:
    url = page.url or ""
    body = get_text(page).lower()
    return "/auth/login" in url or ("login" in body and "password" in body and "email" in body)


def is_logged_in_page(page) -> bool:
    url = page.url or ""
    if "/auth/login" in url:
        return False
    body = get_text(page)
    return any(kw in body for kw in ("Dashboard", "Servers", "Logout", "Account", "Profile"))


def login(page, account: dict) -> bool:
    email, password = account["email"], account["password"]
    # 登录前先确保 viewport 就绪
    ensure_viewport(page)

    for attempt in range(1, 4):  # 增加到 3 次重试
        if is_logged_in_page(page):
            return True
        log.info(f"🔐 KataBump 登录尝试 ({attempt}/3): {mask(email)}")
        if not navigate(page, LOGIN_URL):
            log.warning(f"⚠️ KataBump 导航到登录页失败（{attempt}/3），CF 拦截未解除")
            continue

        # navigate 完成后再次确保 viewport
        ensure_viewport(page)

        if is_logged_in_page(page):
            return True

        try:
            # 等待输入框出现，扩大选择器范围
            try:
                page.wait_for_selector(
                    'input[name="email"], input[type="email"], input[placeholder*="email" i], input[placeholder*="Email"]',
                    timeout=12000,
                )
            except Exception:
                # 降级等待任意可见输入框
                page.wait_for_selector('input[type="text"], input[type="email"]', timeout=8000)

            human_delay()

            # 填写 email（多种选择器容错）
            filled_email = False
            for sel in [
                'input[name="email"]',
                'input[type="email"]',
                'input[placeholder*="email" i]',
                'input[placeholder*="Email"]',
            ]:
                try:
                    locator = page.locator(sel).first
                    if locator.is_visible():
                        locator.fill(email)
                        filled_email = True
                        break
                except Exception:
                    continue
            if not filled_email:
                page.locator('input[type="text"], input').first.fill(email)

            human_delay(0.2, 0.6)

            # 填写 password
            filled_pwd = False
            for sel in ['input[name="password"]', 'input[type="password"]', 'input[placeholder*="password" i]']:
                try:
                    locator = page.locator(sel).first
                    if locator.is_visible():
                        locator.fill(password)
                        filled_pwd = True
                        break
                except Exception:
                    continue
            if not filled_pwd:
                page.locator('input[type="password"]').first.fill(password)

            human_delay(0.3, 0.8)

            # 处理 Turnstile
            if not click_turnstile_checkbox(page):
                log.error(f"❌ [账号: {mask(email)}] Turnstile 未通过（{attempt}/3），跳过本轮提交。")
                take_screenshot(page, f"login_turnstile_failed_{email}_{attempt}")
                continue

            human_delay(0.4, 0.9)

            # 点击登录按钮
            submit_clicked = False
            for btn_try in [
                lambda: page.get_by_role("button", name=re.compile(r"^\s*login\s*$", re.I)).click(timeout=3000),
                lambda: page.locator('button[type="submit"]').first.click(timeout=3000),
                lambda: page.locator('input[type="submit"]').first.click(timeout=3000),
                lambda: page.locator('#submit').first.click(timeout=3000),
                lambda: page.locator('button').filter(has_text=re.compile(r"login", re.I)).first.click(timeout=3000),
            ]:
                try:
                    btn_try()
                    submit_clicked = True
                    break
                except Exception:
                    continue

            if not submit_clicked:
                log.warning(f"⚠️ 登录按钮点击均失败（{attempt}/3），按 Enter 提交...")
                try:
                    page.locator('input[type="password"]').first.press("Enter")
                except Exception:
                    pass

            # 等待登录结果
            deadline = time.time() + 30
            while time.time() < deadline:
                if is_logged_in_page(page):
                    log.info(f"✅ KataBump 登录成功: {mask(email)}")
                    return True
                if is_login_page(page) and re.search(r"invalid|incorrect|failed|error", get_text(page), re.I):
                    log.warning(f"⚠️ 页面显示凭证错误（{attempt}/3）")
                    break
                time.sleep(0.7)

            take_screenshot(page, f"login_not_finished_{email}_{attempt}")

        except Exception as ex:
            log.warning(f"KataBump 登录重试序列异常（{attempt}/3）: {ex}")
            if is_logged_in_page(page):
                return True

        if attempt < 3:
            time.sleep(random.uniform(2.0, 4.0))

    return False


def extract_page_clues(page) -> str:
    alert_text = js_eval(page, r"""
        () => {
            const normalize = (v) => String(v || '').replace(/\s+/g, ' ').trim();
            const visible = (el) => {
                if (!el) return false;
                const st = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return st && st.display !== 'none' && st.visibility !== 'hidden'
                    && Number(st.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
            };
            return Array.from(document.querySelectorAll('.alert, [role="alert"], .toast, [aria-live]'))
                .filter(visible)
                .map((el) => normalize(el.innerText || el.textContent || ''))
                .filter(Boolean)
                .join(' | ');
        }
    """) or ""
    text = "\n".join([str(alert_text), get_text(page)])
    lines = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        if re.search(
            r"renew|expir|suspend|due|remaining|server|plan|status|active|inactive|trial|days?|"
            r"can't|cannot|able\s+to|as\s+of|captcha|verified|altcha",
            line,
            re.I,
        ):
            lines.append(line)
        if len(lines) >= 8:
            break
    return " | ".join(lines)[:500] if lines else text[:300].replace("\n", " | ")


def classify_page_problem(page) -> str:
    text = get_text(page)
    if is_login_page(page):
        return "页面仍停留在登录态，可能 Cookie 未建立或账号无效"
    if re.search(r"not\s+found|404|does\s+not\s+exist", text, re.I):
        return "服务器页面不存在或 ID 错误"
    if re.search(r"permission|unauthori[sz]ed|forbidden|not\s+allowed|access\s+denied", text, re.I):
        return "账号无权访问该服务器页面"
    return ""


def page_has_non_due_state(page) -> bool:
    text = get_text(page)
    return bool(re.search(
        r"already\s+renewed|not\s+eligible|too\s+early|renew\s+in|can\s+renew\s+in|"
        r"cannot\s+renew\s+yet|can't\s+renew|can\s*not\s+renew|will\s+be\s+able|able\s+to\s+as\s+of|"
        r"no\s+renewal\s+required|active\s+and\s+up\s+to\s+date",
        text,
        re.I,
    ))


def probe_renew_modal(page) -> dict:
    """探测 KataBump 续期二次确认模态框。

    只返回模态框内部的验证码坐标和第二个 Renew 坐标；不会扫描页面底部原始 Renew，
    这样可以避免误点页面上的第一个 Renew。
    """
    return js_eval(page, r"""
        () => {
            const normalize = (v) => String(v || '').replace(/\s+/g, ' ').trim();
            const visible = (el) => {
                if (!el) return false;
                const st = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return st && st.display !== 'none' && st.visibility !== 'hidden'
                    && Number(st.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
            };
            const textOf = (el) => normalize(
                el.innerText || el.value || el.getAttribute('aria-label') ||
                el.getAttribute('title') || el.textContent || ''
            );
            const deepAll = (root, selector, out = []) => {
                try {
                    if (root.querySelectorAll) out.push(...Array.from(root.querySelectorAll(selector)));
                    const all = root.querySelectorAll ? Array.from(root.querySelectorAll('*')) : [];
                    for (const el of all) {
                        if (el.shadowRoot) deepAll(el.shadowRoot, selector, out);
                    }
                } catch (_) {}
                return out;
            };
            const rectInfo = (el) => {
                const rect = el.getBoundingClientRect();
                return {
                    x: rect.left + rect.width / 2,
                    y: rect.top + rect.height / 2,
                    left: rect.left,
                    top: rect.top,
                    width: rect.width,
                    height: rect.height,
                    area: rect.width * rect.height,
                };
            };

            const standardRoots = Array.from(document.querySelectorAll(
                '.modal.show, .modal[aria-modal="true"], .modal[style*="display: block"], .modal-content, .modal-dialog, [role="dialog"], .swal2-popup, .offcanvas.show, .modal, .modal-backdrop + *'
            ));
            const broadRoots = Array.from(document.querySelectorAll('div, section, article, form'));
            const seen = new Set();
            const roots = [...standardRoots, ...broadRoots]
                .filter((el) => {
                    if (seen.has(el) || !visible(el)) return false;
                    seen.add(el);
                    const rect = el.getBoundingClientRect();
                    if (rect.width < 260 || rect.height < 130) return false;
                    const text = textOf(el);
                    if (/delete\s+server|terminate|destroy|remove|wipe|suspend/i.test(text)) return false;
                    const hasRenew = /\brenew\b/i.test(text);
                    const hasCaptcha = /captcha|altcha|verified|protected\s+by/i.test(text);
                    const hasExtendText = /extend\s+the\s+life|life\s+of\s+your\s+server|will\s+extend/i.test(text);
                    const hasClose = /\bclose\b/i.test(text);
                    return hasRenew && (hasCaptcha || hasExtendText || hasClose);
                })
                .map((el) => {
                    const rect = rectInfo(el);
                    const text = textOf(el);
                    const marker = String(el.className || '') + ' ' + String(el.getAttribute('role') || '') + ' ' + String(el.id || '');
                    const modalLike = /modal|dialog|swal|offcanvas/i.test(marker) ? 1 : 0;
                    const exactText = /this\s+will\s+extend\s+the\s+life\s+of\s+your\s+server/i.test(text) ? 1 : 0;
                    const captchaText = /captcha|altcha|verified|protected\s+by/i.test(text) ? 1 : 0;
                    const fullScreenPenalty = (rect.width > window.innerWidth * 0.92 || rect.height > window.innerHeight * 0.92) ? 120 : 0;
                    return { el, rect, text, score: modalLike * 1200 + exactText * 900 + captchaText * 500 - fullScreenPenalty - rect.area / 5000 };
                })
                .sort((a, b) => b.score - a.score || a.rect.area - b.rect.area);

            const rootItem = roots[0];
            if (!rootItem) {
                const visibleButtons = Array.from(document.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"], a'))
                    .filter(visible)
                    .map((el) => {
                        const rect = el.getBoundingClientRect();
                        return { text: textOf(el), cls: String(el.className || '').slice(0, 120), x: Math.round(rect.left + rect.width / 2), y: Math.round(rect.top + rect.height / 2) };
                    })
                    .filter((item) => item.text)
                    .slice(0, 30);
                const modalish = Array.from(document.querySelectorAll('.modal, .modal-content, .modal-dialog, [role="dialog"], [class*="modal"], [class*="dialog"], [class*="captcha"], [class*="altcha"]'))
                    .filter(visible)
                    .map((el) => ({ tag: el.tagName, cls: String(el.className || '').slice(0, 120), text: textOf(el).slice(0, 260) }))
                    .slice(0, 12);
                return { found: false, reason: 'renew_modal_not_found', visibleButtons, modalish, bodyText: textOf(document.body).slice(0, 500) };
            }

            const root = rootItem.el;
            const rootText = rootItem.text;
            if (/delete\s+server|terminate|destroy|remove|wipe|suspend/i.test(rootText)) {
                return { found: true, danger: true, rootText: rootText.slice(0, 500) };
            }

            const buttons = deepAll(root, 'button, [role="button"], input[type="button"], input[type="submit"], a')
                .filter(visible)
                .map((el) => {
                    const text = textOf(el);
                    const attrs = normalize([
                        el.getAttribute('class'), el.getAttribute('onclick'), el.getAttribute('formaction'),
                        el.getAttribute('data-bs-dismiss'), el.getAttribute('data-dismiss')
                    ].filter(Boolean).join(' '));
                    return { text, attrs, rect: rectInfo(el) };
                });
            const rejectRe = /close|cancel|no|back|delete|terminate|destroy|remove|suspend|stop/i;
            const renewButton = buttons.find((item) => /^\s*renew\s*$/i.test(item.text) && !rejectRe.test(`${item.text} ${item.attrs}`))
                || buttons.find((item) => /renew|extend|confirm|yes|continue|proceed|ok|okay/i.test(item.text) && !rejectRe.test(`${item.text} ${item.attrs}`));

            const captchaNodes = deepAll(root, 'input[type="checkbox"], [role="checkbox"], altcha-widget, .altcha, [class*="altcha"], [data-altcha], label, iframe')
                .filter(visible)
                .map((el) => {
                    const rect = rectInfo(el);
                    const text = textOf(el);
                    const checked = Boolean(el.checked)
                        || /true|checked/i.test(String(el.getAttribute('aria-checked') || ''))
                        || /checked|verified/i.test(String(el.className || ''));
                    const isCheckbox = String(el.tagName || '').toLowerCase() === 'input' || String(el.getAttribute('role') || '').toLowerCase() === 'checkbox';
                    const priority = isCheckbox ? 10000 : (/altcha|captcha|protected|verified/i.test(`${text} ${el.className || ''}`) ? 5000 : 0);
                    return { el, rect, text, checked, isCheckbox, priority };
                })
                .sort((a, b) => b.priority - a.priority || a.rect.area - b.rect.area);

            const captcha = captchaNodes[0] || null;
            const verified = /\bverified\b|completed|success/i.test(rootText) || Boolean(captcha && captcha.checked);
            let captchaClick = null;
            if (captcha) {
                captchaClick = {
                    x: captcha.isCheckbox ? captcha.rect.x : captcha.rect.left + Math.min(26, Math.max(10, captcha.rect.width * 0.12)),
                    y: captcha.rect.y,
                    text: captcha.text,
                    isCheckbox: captcha.isCheckbox,
                    checked: captcha.checked,
                };
            }

            return {
                found: true,
                danger: false,
                rootText: rootText.slice(0, 500),
                rootRect: rootItem.rect,
                verified,
                hasCaptcha: Boolean(captcha),
                captchaClick,
                renewButton: renewButton ? { text: renewButton.text, x: renewButton.rect.x, y: renewButton.rect.y } : null,
                buttons: buttons.map((item) => item.text).filter(Boolean).slice(0, 12),
            };
        }
    """) or {"found": False, "reason": "probe_failed"}


def wait_altcha_or_modal_captcha_ready(page, timeout: int = 35) -> tuple[bool, str]:
    """等待并处理 KataBump 续期弹窗内 ALTCHA/验证码。"""
    deadline = time.time() + timeout
    clicked_captcha = False
    logged_found = False
    last_state = None
    while time.time() < deadline:
        state = probe_renew_modal(page)
        last_state = state

        if not state.get("found"):
            time.sleep(0.5)
            continue

        if state.get("danger"):
            return False, f"检测到危险弹窗，拒绝操作: {state.get('rootText')}"

        if not logged_found:
            log.info(
                "🔎 已检测到 KataBump Renew 二次认证模态框: "
                f"buttons={state.get('buttons')}, hasCaptcha={state.get('hasCaptcha')}, verified={state.get('verified')}"
            )
            logged_found = True

        if state.get("verified"):
            log.info("✅ KataBump 弹窗内 ALTCHA/Captcha 已显示 Verified。")
            return True, state.get("rootText") or "ALTCHA 已 Verified"

        captcha_click = state.get("captchaClick") or {}
        if state.get("hasCaptcha") and captcha_click and not clicked_captcha:
            try:
                x, y = float(captcha_click["x"]), float(captcha_click["y"])
                page.mouse.move(x, y)
                time.sleep(random.uniform(0.25, 0.55))
                page.mouse.click(x, y)
                clicked_captcha = True
                log.info(
                    "🎯 已物理点击 KataBump Renew 模态框内 ALTCHA/Captcha 区域: "
                    f"({x:.0f}, {y:.0f}), isCheckbox={captcha_click.get('isCheckbox')}, text='{captcha_click.get('text')}'"
                )
            except Exception as e:
                log.warning(f"⚠️ ALTCHA/Captcha 区域物理点击失败，继续等待: {e}")

        time.sleep(0.7)

    return False, f"验证码未在 {timeout} 秒内完成，最后状态: {last_state}"


def find_renew_button_probe(page) -> dict:
    return js_eval(page, r"""
        () => {
            const normalize = (v) => String(v || '').replace(/\s+/g, ' ').trim();
            const visible = (el) => {
                if (!el) return false;
                const st = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return st && st.display !== 'none' && st.visibility !== 'hidden'
                    && Number(st.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
            };
            const textOf = (el) => normalize(
                el.innerText || el.value || el.getAttribute('aria-label') ||
                el.getAttribute('title') || el.textContent || ''
            );
            const attrOf = (el) => normalize([
                el.getAttribute('href'),
                el.getAttribute('action'),
                el.getAttribute('onclick'),
                el.getAttribute('data-url'),
                el.getAttribute('data-action'),
                el.getAttribute('data-route'),
                el.getAttribute('formaction'),
            ].filter(Boolean).join(' '));

            const positiveRe = /\b(renew|extend|extension|keep\s*alive|keepalive|reactivate)\b/i;
            const strictTextRe = /^\s*(renew|renew server|renew now|extend|extend server|keep alive)\s*$/i;
            const rejectRe = /cancel|delete|remove|terminate|destroy|suspend|stop|restart|reinstall|reset|wipe|archive/i;

            const selectors = [
                'button', 'a', '[role="button"]', 'input[type="button"]', 'input[type="submit"]',
                '[onclick]', '[data-action]', '[data-url]', '[formaction]'
            ];
            const seen = new Set();
            const items = selectors.flatMap((sel) => Array.from(document.querySelectorAll(sel)))
                .filter((el) => {
                    if (seen.has(el)) return false;
                    seen.add(el);
                    return visible(el);
                })
                .map((el) => {
                    const text = textOf(el);
                    const attrs = attrOf(el);
                    const combined = `${text} ${attrs}`;
                    const rect = el.getBoundingClientRect();
                    const href = el.getAttribute('href') || '';
                    return { el, text, attrs, combined, href, x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
                });

            const candidates = items
                .filter((item) => positiveRe.test(item.combined) && !rejectRe.test(item.combined))
                .map((item) => {
                    let score = 0;
                    if (/renew/i.test(item.attrs)) score += 40;
                    if (strictTextRe.test(item.text)) score += 35;
                    if (/server/i.test(item.text)) score += 10;
                    if (/btn|button/i.test(item.combined)) score += 5;
                    return { ...item, score };
                })
                .sort((a, b) => b.score - a.score || a.y - b.y)
                .slice(0, 8);

            const target = candidates[0];
            if (!target) {
                return {
                    found: false,
                    visibleActions: items.slice(0, 20).map((item) => ({ text: item.text, attrs: item.attrs })).filter((item) => item.text || item.attrs),
                };
            }
            target.el.scrollIntoView({ block: 'center', inline: 'center' });
            const rect = target.el.getBoundingClientRect();
            return {
                found: true,
                text: target.text,
                attrs: target.attrs,
                href: target.href,
                score: target.score,
                x: rect.left + rect.width / 2,
                y: rect.top + rect.height / 2,
                candidates: candidates.map((item) => ({ text: item.text, attrs: item.attrs, score: item.score })),
            };
        }
    """) or {"found": False}


def trigger_renew_button_by_dom(page) -> dict:
    """用 DOM 事件触发页面上的第一个 Renew，作为物理坐标点击失败后的重试。"""
    return js_eval(page, r"""
        () => {
            const normalize = (v) => String(v || '').replace(/\s+/g, ' ').trim();
            const visible = (el) => {
                if (!el) return false;
                const st = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return st && st.display !== 'none' && st.visibility !== 'hidden'
                    && Number(st.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
            };
            const textOf = (el) => normalize(
                el.innerText || el.value || el.getAttribute('aria-label') ||
                el.getAttribute('title') || el.textContent || ''
            );
            const rejectRe = /close|cancel|delete|remove|terminate|destroy|suspend|stop|restart|reinstall|reset|wipe|archive/i;
            const modalRe = /modal|dialog|swal|offcanvas/i;
            const inModal = (el) => Boolean(el.closest('.modal, .modal-content, .modal-dialog, [role="dialog"], .swal2-popup, .offcanvas'));
            const candidates = Array.from(document.querySelectorAll('button, a, [role="button"], input[type="button"], input[type="submit"]'))
                .filter(visible)
                .filter((el) => !inModal(el))
                .map((el) => {
                    const rect = el.getBoundingClientRect();
                    const text = textOf(el);
                    const attrs = normalize([
                        el.getAttribute('class'), el.getAttribute('onclick'), el.getAttribute('href'),
                        el.getAttribute('data-bs-toggle'), el.getAttribute('data-bs-target'), el.getAttribute('data-target'),
                        el.getAttribute('formaction')
                    ].filter(Boolean).join(' '));
                    let score = 0;
                    if (/^\s*renew\s*$/i.test(text)) score += 100;
                    if (/renew/i.test(attrs)) score += 40;
                    if (modalRe.test(attrs)) score += 30;
                    if (rect.top > window.innerHeight * 0.35) score += 10;
                    return { el, text, attrs, x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, score };
                })
                .filter((item) => /\brenew\b/i.test(`${item.text} ${item.attrs}`) && !rejectRe.test(`${item.text} ${item.attrs}`))
                .sort((a, b) => b.score - a.score || b.y - a.y);
            const target = candidates[0];
            if (!target) {
                return { clicked: false, candidates: candidates.map((item) => ({ text: item.text, attrs: item.attrs, score: item.score })) };
            }
            target.el.scrollIntoView({ block: 'center', inline: 'center' });
            target.el.focus && target.el.focus();
            target.el.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, cancelable: true, view: window }));
            target.el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
            target.el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
            target.el.click();
            return { clicked: true, text: target.text, attrs: target.attrs, x: Math.round(target.x), y: Math.round(target.y), score: target.score };
        }
    """) or {"clicked": False}


def wait_for_renew_modal(page, timeout: int = 6) -> dict:
    deadline = time.time() + timeout
    last_probe = None
    while time.time() < deadline:
        last_probe = probe_renew_modal(page)
        if isinstance(last_probe, dict) and last_probe.get("found"):
            return last_probe
        time.sleep(0.4)
    return last_probe or {"found": False, "reason": "modal_wait_timeout"}


def click_renew_button(page, target_url: str) -> tuple[bool, str]:
    probe = find_renew_button_probe(page)
    if isinstance(probe, dict) and probe.get("found"):
        try:
            x, y = float(probe["x"]), float(probe["y"])
            page.mouse.move(x, y)
            time.sleep(random.uniform(0.2, 0.5))
            page.mouse.click(x, y)
            detail = f"text='{probe.get('text')}', attrs='{probe.get('attrs')}', score={probe.get('score')}"
            log.info(f"-> KataBump 续期按钮已点击: {detail}")
            modal_probe = wait_for_renew_modal(page, timeout=6)
            if isinstance(modal_probe, dict) and modal_probe.get("found"):
                log.info(
                    "✅ 第一次 Renew 点击后已确认二次认证模态框出现: "
                    f"buttons={modal_probe.get('buttons')}, hasCaptcha={modal_probe.get('hasCaptcha')}"
                )
                return True, detail

            log.warning(f"⚠️ 坐标点击后未出现 Renew 模态框，准备 DOM 事件重试。最后探测: {modal_probe}")
            dom_result = trigger_renew_button_by_dom(page)
            log.info(f"-> KataBump DOM 事件重试触发 Renew: {dom_result}")
            modal_probe = wait_for_renew_modal(page, timeout=7)
            if isinstance(modal_probe, dict) and modal_probe.get("found"):
                log.info(
                    "✅ DOM 事件重试后已确认二次认证模态框出现: "
                    f"buttons={modal_probe.get('buttons')}, hasCaptcha={modal_probe.get('hasCaptcha')}"
                )
                return True, f"{detail}; DOM 重试={dom_result}"

            take_screenshot(page, f"katabump_first_renew_no_modal_{server_id_from_url(target_url)}")
            return False, f"第一个 Renew 已点击但二次认证模态框未出现。最后探测: {modal_probe}"
        except Exception as e:
            log.warning(f"⚠️ KataBump 续期按钮坐标点击失败: {e}")

    problem = classify_page_problem(page)
    if problem:
        take_screenshot(page, f"katabump_page_problem_{server_id_from_url(target_url)}")
        return False, problem
    if page_has_non_due_state(page):
        return False, "页面显示当前无需续期或尚未到可续期时间"

    log.warning(f"⚠️ 未找到 KataBump 安全续期按钮，候选元素: {probe}")
    take_screenshot(page, f"katabump_renew_button_missing_{server_id_from_url(target_url)}")
    return False, "未找到安全的 Renew/Extend 按钮"


def click_confirm_modal_if_exists(page, timeout: int = 12) -> tuple[bool, str]:
    deadline = time.time() + timeout
    last_probe = None
    while time.time() < deadline:
        probe = probe_renew_modal(page)
        last_probe = probe

        if isinstance(probe, dict) and probe.get("danger"):
            log.error(f"❌ 检测到危险弹窗，已拒绝确认: {probe.get('rootText')}")
            return False, "检测到危险弹窗，已拒绝点击"

        if isinstance(probe, dict) and probe.get("found"):
            log.info(
                "🔎 KataBump 已定位 Renew 二次认证模态框，准备处理验证码和第二个 Renew: "
                f"buttons={probe.get('buttons')}, hasCaptcha={probe.get('hasCaptcha')}, verified={probe.get('verified')}"
            )
            captcha_ready, captcha_detail = wait_altcha_or_modal_captcha_ready(page, timeout=45)
            if not captcha_ready:
                take_screenshot(page, "katabump_modal_altcha_not_ready")
                return False, captcha_detail

            renew_probe = probe_renew_modal(page)
            renew_button = (renew_probe or {}).get("renewButton") or {}
            if not renew_button:
                take_screenshot(page, "katabump_modal_second_renew_missing")
                return False, f"已完成验证码，但未找到弹窗内部第二个 Renew。最后探测: {renew_probe}"

            try:
                x, y = float(renew_button["x"]), float(renew_button["y"])
                page.mouse.move(x, y)
                time.sleep(random.uniform(0.25, 0.55))
                page.mouse.click(x, y)
                log.info(
                    "-> KataBump 已物理点击 Renew 模态框内部第二个 Renew: "
                    f"text='{renew_button.get('text')}', x={x:.0f}, y={y:.0f}, buttons={renew_probe.get('buttons')}"
                )
                time.sleep(2)
                return True, f"已完成弹窗验证码并点击模态框内部第二个 Renew；{captcha_detail}"
            except Exception as e:
                take_screenshot(page, "katabump_modal_second_renew_click_failed")
                return False, f"弹窗内部第二个 Renew 点击失败: {e}"

        time.sleep(0.5)

    log.error(f"❌ 未观察到 KataBump Renew 二次认证模态框，最后探测: {last_probe}")
    take_screenshot(page, "katabump_renew_modal_missing")
    return False, "未观察到 Renew 二次认证模态框"


def wait_for_result_notice(page, timeout: int = RESULT_NOTICE_TIMEOUT) -> dict:
    success_re = re.compile(r"renew(?:ed|al)?\s+(?:success|complete)|successfully\s+(?:renewed|extended)|server\s+renewed|extended\s+successfully", re.I)
    skip_re = re.compile(
        r"already\s+renewed|too\s+early|not\s+eligible|can\s+renew\s+in|cannot\s+renew\s+yet|"
        r"can't\s+renew|can\s*not\s+renew|will\s+be\s+able|able\s+to\s+as\s+of|no\s+renewal\s+required",
        re.I,
    )
    # 注意：不使用裸词 "error"/"unable"，避免误匹配页面无关内容（如广告加载错误、日志字段）
    fail_re = re.compile(
        r"renew(?:al)?\s+failed|failed\s+to\s+renew|renew(?:al)?\s+error|unable\s+to\s+renew"
        r"|insufficient\s+(?:credit|fund|balance|permission)|forbidden|unauthori[sz]ed|invalid\s+(?:token|session|request)",
        re.I,
    )

    deadline = time.time() + timeout
    last_candidates = []
    while time.time() < deadline:
        notices = js_eval(page, r"""
            () => {
                const normalize = (v) => String(v || '').replace(/\s+/g, ' ').trim();
                const visible = (el) => {
                    if (!el) return false;
                    const st = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return st && st.display !== 'none' && st.visibility !== 'hidden'
                        && Number(st.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
                };
                const textOf = (el) => normalize(
                    el.innerText || el.value || el.getAttribute('aria-label') ||
                    el.getAttribute('title') || el.textContent || ''
                );
                const selectors = [
                    '.toast', '.toast-message', '.toast-body', '.Toastify__toast', '.iziToast', '.notyf__toast',
                    '.swal2-toast', '.swal2-popup', '.notification', '.alert', '.notify', '.message',
                    '[role="status"]', '[role="alert"]', '[aria-live]'
                ];
                const seen = new Set();
                return selectors.flatMap((sel) => Array.from(document.querySelectorAll(sel)))
                    .filter((el) => {
                        if (seen.has(el)) return false;
                        seen.add(el);
                        return visible(el);
                    })
                    .map((el) => ({ text: textOf(el) }))
                    .filter((item) => item.text)
                    .slice(0, 12);
            }
        """) or []
        body_text = extract_page_clues(page)
        candidates = notices + ([{"text": body_text}] if body_text else [])
        if candidates:
            last_candidates = candidates
        for item in candidates:
            text = str(item.get("text", ""))
            if success_re.search(text):
                return {"status": "success", "text": text, "candidates": candidates}
            if skip_re.search(text):
                return {"status": "skipped", "text": text, "candidates": candidates}
            if fail_re.search(text):
                return {"status": "failed", "text": text, "candidates": candidates}
        time.sleep(0.5)
    return {"status": "unknown", "text": "", "candidates": last_candidates}


def renew_target(page, target_url: str) -> dict:
    target_label = server_id_from_url(target_url) or target_url
    log.info(f"🔎 打开 KataBump 服务器页面: {target_url}")
    if not navigate(page, target_url):
        return {"target": target_label, "status": "failed", "message": "Cloudflare/网络导航失败"}

    time.sleep(2)
    if is_login_page(page):
        return {"target": target_label, "status": "failed", "message": "访问目标页后仍处于登录页"}

    js_eval(page, "window.scrollTo(0, document.body.scrollHeight / 2);")
    time.sleep(0.6)
    js_eval(page, "window.scrollTo(0, 0);")
    time.sleep(0.6)

    before_clues = extract_page_clues(page)
    clicked, click_detail = click_renew_button(page, target_url)
    if not clicked:
        status = "skipped" if "无需续期" in click_detail or "尚未" in click_detail else "failed"
        return {"target": target_label, "status": status, "message": click_detail, "before": before_clues}

    time.sleep(1.0)
    confirm_clicked, confirm_detail = click_confirm_modal_if_exists(page)
    result = wait_for_result_notice(page)
    time.sleep(2)
    navigate(page, target_url)
    time.sleep(1.5)
    after_clues = extract_page_clues(page)

    status = result.get("status", "unknown")
    if status == "unknown" and confirm_clicked:
        message = f"已点击续期和确认，但未捕获明确提示；页面线索: {after_clues or '无'}"
    elif status == "unknown":
        message = f"已点击续期按钮，未捕获明确提示；{confirm_detail}；页面线索: {after_clues or '无'}"
    else:
        message = result.get("text") or confirm_detail or click_detail

    return {
        "target": target_label,
        "status": status,
        "message": message,
        "before": before_clues,
        "after": after_clues,
        "confirm_clicked": confirm_clicked,
    }


def run_for_account(page, account: dict) -> str:
    email = account["email"]
    if not login(page, account):
        return f"👤 账号: {mask(email)}\n  ❌ 登录失败：Turnstile 拦截、凭证错误或登录页结构变化"

    targets = account_targets(account)
    if not targets:
        return f"👤 账号: {mask(email)}\n  ❌ 未配置任何 KataBump 服务器目标"

    lines = [f"👤 账号: {mask(email)}"]
    for target_url in targets:
        try:
            result = renew_target(page, target_url)
        except Exception as e:
            log.error(f"💥 KataBump 目标执行崩溃: {target_url}: {e}", exc_info=True)
            take_screenshot(page, f"katabump_crash_{server_id_from_url(target_url)}")
            result = {"target": server_id_from_url(target_url) or target_url, "status": "failed", "message": str(e)}

        status = result.get("status")
        target = result.get("target", target_url)
        message = result.get("message") or "无详细信息"
        after = result.get("after") or ""
        clue_suffix = f"；页面线索: {after}" if after and after not in message else ""

        if status == "success":
            prefix = "✅ 续期成功"
        elif status == "skipped":
            prefix = "ℹ️ 跳过"
        elif status == "unknown":
            prefix = "⚠️ 结果未知"
        else:
            prefix = "❌ 续期失败"
        lines.append(f"  {prefix} [Server-{target}] -> {message}{clue_suffix}")

        time.sleep(random.uniform(2.0, 4.5))
    return "\n".join(lines)


def wxpush(content: str):
    if not WXPUSHER_TOKEN or not WXPUSHER_UID:
        return
    import urllib.request

    payload = json.dumps({
        "appToken": WXPUSHER_TOKEN,
        "content": content,
        "contentType": 1,
        "uids": [WXPUSHER_UID],
    }).encode()
    try:
        req = urllib.request.Request(
            "https://wxpusher.zjiecode.com/api/send/message",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        log.warning(f"📨 WxPusher 推送异常: {e}")


def main():
    from cloakbrowser import launch

    log.info(f"🧩 KataBump renew script version: {SCRIPT_VERSION}")

    try:
        accounts = load_accounts()
    except Exception as e:
        log.critical(e)
        return 1

    all_reports = ["🖥️ KataBump 自动续期合并报告", ""]
    has_error = False
    has_unknown = False

    log.info("🚀 启动 CloakBrowser KataBump 主实例进程...")
    browser = launch(headless=False, humanize=True, geoip=True)

    try:
        for idx, account in enumerate(accounts, 1):
            email = account.get("email", "未知")
            log.info(f"\n{'=' * 20} KataBump 账号 ({idx} / {len(accounts)}) {'=' * 20}")
            context = None
            page = None
            try:
                try:
                    context = browser.new_context()
                    page = context.new_page()
                except Exception as err:
                    log.warning(f"⚠️ 无法创建独立 BrowserContext: {err}，重启浏览器进程。")
                    try:
                        browser.close()
                    except Exception:
                        pass
                    browser = launch(headless=False, humanize=True, geoip=True)
                    page = browser.new_page()

                report = run_for_account(page, account)
                all_reports.append(report)
                all_reports.append("")
                if "❌" in report:
                    has_error = True
                if "⚠️" in report:
                    has_unknown = True
            except Exception as account_err:
                log.error(f"💥 [严重异常] KataBump 账号 [{mask(email)}] 崩溃: {account_err}", exc_info=True)
                all_reports.append(f"👤 账号: {mask(email)}\n  ❌ 运行期崩溃 -> {account_err}\n")
                has_error = True
            finally:
                if page:
                    try:
                        page.close()
                    except Exception:
                        pass
                if context:
                    try:
                        context.close()
                    except Exception:
                        pass

            if idx < len(accounts):
                gap = random.randint(5, 10)
                log.info(f"🛡️ 账号间隔睡眠 {gap} 秒...")
                time.sleep(gap)
    finally:
        log.info("🧹 回收 KataBump 浏览器实例...")
        try:
            browser.close()
        except Exception as close_err:
            log.error(f"浏览器回收异常: {close_err}")

    final_msg = "\n".join(all_reports).strip()
    log.info(f"\nKataBump 最终报表:\n{final_msg}")

    if has_error or has_unknown:
        wxpush(f"🚨 KataBump 自动续期异常或未知审计\n\n{final_msg}")
    else:
        log.info("🎉 KataBump 自动续期流程完成，无失败项。")

    return 1 if has_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
