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


def is_cf_blocked(page) -> bool:
    body = get_text(page).lower()
    return "verify you are human" in body or ("cloudflare" in body and "security" in body)


def wait_cf_pass(page, timeout=45) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_cf_blocked(page):
            return True
        time.sleep(1)
    return False


def navigate(page, url: str, timeout=45) -> bool:
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
    except Exception:
        pass
    if not is_cf_blocked(page):
        return True
    if wait_cf_pass(page, timeout=timeout):
        return True
    try:
        page.reload(wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass
    return wait_cf_pass(page, timeout=30)


def has_turnstile(page) -> bool:
    marker = js_eval(page, """
        () => Boolean(
            document.querySelector('.cf-turnstile') ||
            document.querySelector('iframe[src*="challenges.cloudflare.com"]') ||
            document.querySelector('input[name="cf-turnstile-response"]')
        )
    """)
    return bool(marker)


def click_turnstile_checkbox(page, timeout=35) -> bool:
    if not has_turnstile(page):
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

    for _ in range(20):
        if token_ready():
            return True
        time.sleep(0.5)

    cf_frame = None
    for _ in range(20):
        for frame in page.frames:
            if "challenges.cloudflare.com" in (frame.url or ""):
                cf_frame = frame
                break
        if cf_frame:
            break
        time.sleep(0.5)

    try:
        if cf_frame:
            frame_el = cf_frame.frame_element()
            box = frame_el.bounding_box()
        else:
            iframe_el = page.locator('iframe[src*="challenges.cloudflare.com"]').first
            box = iframe_el.bounding_box()
        if box:
            x, y = box["x"] + 25, box["y"] + (box["height"] / 2)
            page.mouse.move(x, y)
            time.sleep(random.uniform(0.2, 0.5))
            page.mouse.click(x, y)
            log.info(f"🎯 已尝试触发 KataBump Turnstile: ({x:.0f}, {y:.0f})")
    except Exception as e:
        log.error(f"❌ KataBump Turnstile 点击失败: {e}")
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        if token_ready():
            return True
        time.sleep(0.5)
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
    for attempt in range(1, 3):
        if is_logged_in_page(page):
            return True
        if not navigate(page, LOGIN_URL):
            continue

        try:
            page.wait_for_selector('input[name="email"], input[type="email"]', timeout=10000)
            human_delay()
            page.locator('input[name="email"], input[type="email"]').first.fill(email)
            human_delay(0.2, 0.6)
            page.locator('input[name="password"], input[type="password"]').first.fill(password)
            human_delay(0.3, 0.8)

            if not click_turnstile_checkbox(page):
                log.error(f"❌ [账号: {mask(email)}] Turnstile 未通过，跳过本轮提交。")
                take_screenshot(page, f"login_turnstile_failed_{email}")
                continue

            human_delay(0.4, 0.9)
            try:
                page.get_by_role("button", name=re.compile(r"^\s*login\s*$", re.I)).click(timeout=3000)
            except Exception:
                page.locator('#submit, button[type="submit"], input[type="submit"]').first.click(timeout=3000)

            deadline = time.time() + 25
            while time.time() < deadline:
                if is_logged_in_page(page):
                    return True
                if is_login_page(page) and re.search(r"invalid|incorrect|failed|error", get_text(page), re.I):
                    break
                time.sleep(0.7)
            take_screenshot(page, f"login_not_finished_{email}_{attempt}")
        except Exception as ex:
            log.warning(f"KataBump 登录重试序列异常（{attempt}/2）: {ex}")
            if is_logged_in_page(page):
                return True
    return False


def extract_page_clues(page) -> str:
    text = get_text(page)
    lines = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        if re.search(r"renew|expir|suspend|due|remaining|server|plan|status|active|inactive|trial|days?", line, re.I):
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
        r"cannot\s+renew\s+yet|no\s+renewal\s+required|active\s+and\s+up\s+to\s+date",
        text,
        re.I,
    ))


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
            return True, detail
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
        probe = js_eval(page, r"""
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
                const roots = Array.from(document.querySelectorAll(
                    '.swal2-container, .swal2-popup, .modal.show, .modal[style*="display: block"], [role="dialog"], .offcanvas.show'
                )).filter(visible);
                if (!roots.length) {
                    return { clicked: false, noDialog: true };
                }
                const rootText = normalize(roots.map(textOf).join(' '));
                if (/delete|terminate|destroy|remove|wipe|cancel\s+server|suspend/i.test(rootText)) {
                    return { clicked: false, dangerDialog: true, rootText: rootText.slice(0, 300) };
                }
                const buttons = roots.flatMap((root) => Array.from(root.querySelectorAll(
                    'button, [role="button"], input[type="button"], input[type="submit"], a, .swal2-confirm'
                ))).filter(visible).map((el) => ({ el, text: textOf(el) }));
                const rejectRe = /cancel|close|no|back|delete|terminate|destroy|remove|suspend|stop/i;
                const target = buttons.find((item) =>
                    /renew|extend|confirm|yes|continue|proceed|ok|okay/i.test(item.text) && !rejectRe.test(item.text)
                );
                if (!target) {
                    return { clicked: false, rootText: rootText.slice(0, 300), buttons: buttons.map((item) => item.text).slice(0, 10) };
                }
                target.el.scrollIntoView({ block: 'center', inline: 'center' });
                target.el.click();
                return { clicked: true, text: target.text, rootText: rootText.slice(0, 300) };
            }
        """)
        last_probe = probe
        if isinstance(probe, dict) and probe.get("clicked"):
            log.info(f"-> KataBump 二次确认按钮已点击: {probe.get('text')}")
            time.sleep(2)
            return True, f"已点击确认按钮: {probe.get('text')}"
        if isinstance(probe, dict) and probe.get("dangerDialog"):
            log.error(f"❌ 检测到危险弹窗，已拒绝确认: {probe.get('rootText')}")
            return False, "检测到危险弹窗，已拒绝点击"
        time.sleep(0.5)

    log.info(f"ℹ️ 未观察到 KataBump 二次确认弹窗，最后探测: {last_probe}")
    return False, "未观察到二次确认弹窗"


def wait_for_result_notice(page, timeout: int = RESULT_NOTICE_TIMEOUT) -> dict:
    success_re = re.compile(r"renew(?:ed|al)?\s+(?:success|complete)|successfully\s+(?:renewed|extended)|server\s+renewed|extended\s+successfully", re.I)
    skip_re = re.compile(r"already\s+renewed|too\s+early|not\s+eligible|can\s+renew\s+in|cannot\s+renew\s+yet|no\s+renewal\s+required", re.I)
    fail_re = re.compile(r"renew(?:al)?\s+failed|failed\s+to\s+renew|error|unable|insufficient|forbidden|unauthori[sz]ed|invalid", re.I)

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
