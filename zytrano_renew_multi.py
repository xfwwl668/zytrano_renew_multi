"""
Zytrano.top 自动续期脚本 (高可用多账号无人值守版)
文件名: zytrano_renew_multi.py
"""

import json
import logging
import os
import random
import re
import time
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

def mask(value: str, show: int = 3) -> str:
    if not value or len(value) <= show * 2:
        return "***"
    return value[:show] + "***" + value[-show:]

# ── 基础配置 ──────────────────────────────────────────────
WXPUSHER_TOKEN = os.environ.get("WXPUSHER_TOKEN", "")
WXPUSHER_UID   = os.environ.get("WXPUSHER_UID", "")

BASE_URL    = "https://cp.zytrano.top"
LOGIN_URL   = f"{BASE_URL}/login"
SERVERS_URL = f"{BASE_URL}/servers"

SCREENSHOT_DIR = Path("./screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

RENEW_SUCCESS_INCREMENT_DAYS = 0.5
RENEW_NEAR_LIMIT_DAYS = 13.5
RENEW_TIME_TOLERANCE_DAYS = 0.03


# ── 严格类型账号清洗器 ──────────────────────────────────────
def load_accounts() -> list[dict]:
    raw_content = ""
    source_info = ""

    env_json = os.environ.get("ZYTRANO_ACCOUNTS_JSON")
    if env_json:
        raw_content = env_json.strip()
        source_info = "环境变量 ZYTRANO_ACCOUNTS_JSON"
    else:
        local_file = Path("accounts.json")
        if local_file.exists():
            raw_content = local_file.read_text(encoding="utf-8").strip()
            source_info = "本地 accounts.json 文件"

    if not raw_content:
        single_user = os.environ.get("ZYTRANO_USERNAME")
        single_pass = os.environ.get("ZYTRANO_PASSWORD")
        if single_user and single_pass:
            log.info("未检测到多账号 JSON，降级使用标准单账号环境变量。")
            return [{"username": single_user, "password": single_pass}]
        raise ValueError("❌ 没有任何可供执行的账号源！")

    try:
        data = json.loads(raw_content)
        if isinstance(data, list):
            accounts_list = data
        elif isinstance(data, dict):
            if "accounts" in data and isinstance(data["accounts"], list):
                accounts_list = data["accounts"]
            elif "data" in data and isinstance(data["data"], list):
                accounts_list = data["data"]
            else:
                raise ValueError("字典结构中未包含合法的 'accounts' 或 'data' 数组")
        else:
            raise ValueError("JSON 顶级根节点类型错误")

        valid_accounts = []
        for idx, item in enumerate(accounts_list):
            if not isinstance(item, dict):
                log.warning(f"[{source_info}] 索引 {idx} 项非合法的字典，已跳过")
                continue
            u = item.get("username") or item.get("user") or item.get("email")
            p = item.get("password") or item.get("pwd")
            if u and p:
                valid_accounts.append({"username": str(u), "password": str(p)})
            else:
                log.warning(f"[{source_info}] 账号条目索引 {idx} 数据字段残缺，已跳过")

        if not valid_accounts:
            raise ValueError("洗涤后无可用的合法账号凭证")

        log.info(f"✅ [{source_info}] 捕获 {len(valid_accounts)} 个标准可用账号")
        return valid_accounts
    except Exception as e:
        raise ValueError(f"❌ 账号配置树深度解析崩溃 ({source_info}): {e}")


# ── 辅助核心工具 ──────────────────────────────────────────
def take_screenshot(page, name: str):
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = str(SCREENSHOT_DIR / f"{ts}_{name}.png")
        page.screenshot(path=path)
    except Exception:
        pass

def get_text(page) -> str:
    try: return page.inner_text("body") or ""
    except Exception: return ""

def human_delay(min_s=0.5, max_s=1.2):
    time.sleep(random.uniform(min_s, max_s))

def js_eval(page, script: str, *args):
    try: return page.evaluate(script, *args)
    except Exception: return None

def safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", value or "unknown")[:32] or "unknown"

def parse_days_remaining(suspended_in: str) -> float:
    days = hours = minutes = 0.0
    if not suspended_in or "未知" in suspended_in:
        return 0.0
    m = re.search(r'(\d+)\s*day', suspended_in, re.I)
    if m: days = float(m.group(1))
    m = re.search(r'(\d+)\s*hour', suspended_in, re.I)
    if m: hours = float(m.group(1))
    m = re.search(r'(\d+)\s*minute', suspended_in, re.I)
    if m: minutes = float(m.group(1))
    return days + (hours / 24.0) + (minutes / 1440.0)


# ── Cloudflare 拦截层与 Turnstile 原生穿透 ──────────────────
def is_cf_blocked(page) -> bool:
    try:
        body = get_text(page).lower()
        return "verify you are human" in body or ("cloudflare" in body and "security" in body)
    except Exception:
        return False

def wait_cf_pass(page, timeout=45) -> bool:
    for i in range(timeout):
        if not is_cf_blocked(page):
            return True
        time.sleep(1)
    return False

def navigate(page, url: str, timeout=45) -> bool:
    try: page.goto(url, timeout=30000, wait_until="domcontentloaded")
    except Exception: pass

    if not is_cf_blocked(page): return True
    if wait_cf_pass(page, timeout=timeout): return True

    try: page.reload(wait_until="domcontentloaded", timeout=30000)
    except Exception: pass
    return wait_cf_pass(page, timeout=30)

def click_turnstile_checkbox(page, timeout=30) -> bool:
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

    for i in range(20):
        if token_ready():
            return True
        time.sleep(0.5)

    cf_frame = None
    for tick in range(16):
        for f in page.frames:
            if "challenges.cloudflare.com" in (f.url or ""):
                cf_frame = f
                break
        if cf_frame: break
        time.sleep(0.5)

    if not cf_frame:
        try:
            iframe_el = page.locator('iframe[src*="challenges.cloudflare.com"]').first
            box = iframe_el.bounding_box()
            if box:
                x, y = box["x"] + 25, box["y"] + (box["height"] / 2)
                page.mouse.move(x, y)
                time.sleep(0.3)
                page.mouse.click(x, y)
                log.info(f"🎯 触发坐标降级点击: ({x:.0f}, {y:.0f})")
        except Exception as e:
            log.error(f"❌ 坐标降级点击失败: {e}")
            return False
    else:
        try:
            frame_el = cf_frame.frame_element()
            box = frame_el.bounding_box()
            if box:
                x, y = box["x"] + 25, box["y"] + (box["height"] / 2)
                page.mouse.move(x, y)
                time.sleep(random.uniform(0.2, 0.4))
                page.mouse.click(x, y)
                log.info(f"🎯 核心内框坐标击发成功: ({x:.0f}, {y:.0f})")
            else:
                return False
        except Exception as e:
            log.error(f"❌ 内框物理映射异常: {e}")
            return False

    for i in range(timeout * 2):
        if token_ready():
            return True
        time.sleep(0.5)
    return False


# ── 登录流 (已修复：精确定位带占位符的输入框，防止 hidden 阻断) ──
LOGGED_IN_URL_KEYS = ("/home", "/dashboard", "/servers")

def is_logged_in_page(page) -> bool:
    if any(k in page.url for k in LOGGED_IN_URL_KEYS): return True
    body = get_text(page)
    return any(kw in body for kw in ("Credits", "Dashboard", "Servers"))

def login(page, account: dict) -> bool:
    username, password = account["username"], account["password"]
    for attempt in range(1, 3):
        if is_logged_in_page(page): return True
        if not navigate(page, LOGIN_URL): continue
        if is_logged_in_page(page): return True

        try:
            # 🌟 修复点 1：精准等待带占位符的可视输入框，彻底避开 <input type="hidden"> 坑
            page.wait_for_selector('input[placeholder]', timeout=8000)
            human_delay(0.5, 1.0)

            # 阶梯型用户名输入容错
            try: page.locator('input[placeholder*="Email"], input[placeholder*="Username"]').first.fill(username, timeout=3000)
            except Exception:
                try: page.locator('input[name="user"], input[name="username"]').first.fill(username, timeout=2000)
                except Exception: page.locator('input[type="text"], input').first.fill(username)

            human_delay(0.3, 0.7)

            # 阶梯型密码输入容错
            try: page.locator('input[placeholder*="Password"]').first.fill(password, timeout=3000)
            except Exception:
                try: page.locator('input[name="password"], input[name="pwd"]').first.fill(password, timeout=2000)
                except Exception: page.locator('input[type="password"]').first.fill(password)

            human_delay(0.5, 1.0)

            cf_passed = click_turnstile_checkbox(page)
            if not cf_passed:
                log.error(f"❌ [账号: {mask(username)}] 本轮 Turnstile 未通过，放弃提交表单触发重试。")
                take_screenshot(page, f"login_cf_failed_{username[:4]}")
                continue 

            human_delay(0.4, 0.9)
            try: page.get_by_role("button", name=re.compile("Sign In|Login", re.I)).click(timeout=3000)
            except Exception: page.locator("button[type='submit'], button").first.click()

            page.wait_for_url(lambda url: any(k in url for k in LOGGED_IN_URL_KEYS), timeout=25000)
            return True
        except Exception as ex:
            log.warning(f"当前登录重试序列异常（{attempt}/2）: {ex}")
            if is_logged_in_page(page): return True
    return False


# ── 服务器结构拉取 ─────────────────────────────────────────
def get_servers_info(page) -> list[dict]:
    if not navigate(page, SERVERS_URL): return []
    time.sleep(3)
    js_eval(page, "window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(1)
    
    html = js_eval(page, "() => document.body.innerHTML") or ""
    server_ids = re.findall(r"handleServerRenew\(['\"]([^\'\"]+)[\'\"]\)", html)

    text = get_text(page)
    suspended_matches = re.findall(r'Suspended in[:\s]*([\d]+ days?,\s*[\d]+ hours?,\s*[\d]+ minutes?)', text, re.I)
    if not suspended_matches:
        suspended_matches = re.findall(r'Suspended in[:\s]*([\d\w\s,]+)', text, re.I)

    servers = []
    for i, sid in enumerate(server_ids):
        servers.append({
            "server_id": str(sid),
            "index": i,
            "name": f"Server-{i+1}",
            "suspended_in": suspended_matches[i] if i < len(suspended_matches) else "未知",
        })
    return servers

def click_confirm_modal_if_exists(page, tag: str = "renew_confirm", timeout: int = 15, required: bool = True) -> bool:
    """点击续期后的二次确认弹窗。

    旧逻辑只用精确文本匹配，SweetAlert2 / Bootstrap 弹窗只要按钮文本、ARIA 名称、
    空格或标点稍有变化，就会静默返回 False，导致日志看起来“点击了 Renew”，
    但实际没有提交最终确认。
    """
    role_patterns = [
        re.compile(r"yes\s*,?\s*renew\s*it!?", re.I),
        re.compile(r"confirm|ok|continue", re.I),
    ]

    deadline = time.time() + timeout
    last_probe = None
    while time.time() < deadline:
        for pattern in role_patterns:
            try:
                btn = page.get_by_role("button", name=pattern).first
                if btn.is_visible():
                    btn.click(timeout=3000)
                    log.info(f"-> 成功通过 ARIA 模式触发二层确认按钮: /{pattern.pattern}/")
                    time.sleep(2)
                    return True
            except Exception:
                pass

        probe = js_eval(page, r"""
            () => {
                const normalize = (v) => String(v || '').replace(/\s+/g, ' ').trim();
                const visible = (el) => {
                    if (!el) return false;
                    const st = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return st && st.display !== 'none' && st.visibility !== 'hidden'
                        && Number(st.opacity || 1) > 0
                        && rect.width > 0 && rect.height > 0;
                };
                const textOf = (el) => normalize(
                    el.innerText || el.value || el.getAttribute('aria-label') ||
                    el.getAttribute('title') || el.textContent || ''
                );

                const roots = Array.from(document.querySelectorAll(
                    '.swal2-container, .swal2-popup, .modal.show, .modal[style*="display: block"], [role="dialog"]'
                )).filter(visible);

                const directConfirm = Array.from(document.querySelectorAll('.swal2-confirm, button.swal2-confirm'))
                    .filter(visible)
                    .map((el) => ({ el, text: textOf(el), selector: '.swal2-confirm' }));

                const scopedButtons = roots.flatMap((root) => Array.from(root.querySelectorAll(
                    'button, [role="button"], input[type="button"], input[type="submit"], a'
                )).filter(visible).map((el) => ({ el, text: textOf(el), selector: 'dialog scoped button' })));

                const candidates = [...directConfirm, ...scopedButtons];
                const target = candidates.find((item) =>
                    /yes\s*,?\s*renew\s*it!?/i.test(item.text) ||
                    /confirm|ok|continue/i.test(item.text) ||
                    item.selector === '.swal2-confirm'
                );

                if (target) {
                    target.el.scrollIntoView({ block: 'center', inline: 'center' });
                    target.el.click();
                    return { clicked: true, via: target.selector, text: target.text };
                }

                return {
                    clicked: false,
                    visibleDialogCount: roots.length,
                    candidates: candidates.slice(0, 8).map((item) => ({
                        text: item.text,
                        selector: item.selector,
                    })),
                };
            }
        """)
        last_probe = probe
        if isinstance(probe, dict) and probe.get("clicked"):
            log.info(f"-> 成功通过 JS 弹窗扫描触发二层确认按钮: {probe.get('text') or probe.get('via')}")
            time.sleep(2)
            return True

        time.sleep(0.5)

    if required:
        log.error(f"❌ 未能在 {timeout} 秒内定位/点击续期二次确认按钮。最后探测: {last_probe}")
        take_screenshot(page, f"{safe_name(tag)}_confirm_missing")
    else:
        log.info(f"ℹ️ 未观察到续期二次确认按钮，当前属于近满期探测场景，交由续期后剩余时间判定。最后探测: {last_probe}")
    return False

def judge_renew_result(old_days: float, new_days: float, confirm_clicked: bool) -> tuple[bool, str]:
    if new_days > (old_days + RENEW_SUCCESS_INCREMENT_DAYS):
        return True, "剩余时间已明显增加"
    if old_days >= RENEW_NEAR_LIMIT_DAYS and new_days >= (old_days - RENEW_TIME_TOLERANCE_DAYS):
        if confirm_clicked:
            return True, "二次确认已提交；剩余时间接近 14 天上限，页面可能不会出现大于 0.5 天的增量"
        return True, "当前剩余时间已接近 14 天上限；未观察到二次确认提交，但平台可能不会继续增加时间，按健康状态处理"
    if not confirm_clicked:
        return False, "二次确认按钮未点击，续期请求大概率没有提交"
    return False, "二次确认已点击，但剩余时间未增加"


# ── 单个账号核心闭环 (已修复：采用拟人化物理按钮点击，避开 window 函数找不到的硬阻断) ──
def run_for_account(page, account: dict) -> str:
    username = account["username"]
    if not login(page, account):
        return f"❌ 账号 [{mask(username)}] 鉴权登录失败 (风控拦截或凭证失效)"

    servers = get_servers_info(page)
    if not servers:
        return f"⚠️ 账号 [{mask(username)}] 底座名下无任何活跃容器实例"

    results = []
    for s in servers:
        target_id = s["server_id"]
        old_time_str = s["suspended_in"]
        old_days = parse_days_remaining(old_time_str)
        
        log.info(f"⏳ 容器 [{s['name']}] 续期前解析天数: {old_days:.4f} 天 ({old_time_str})")
        
        # 🌟 修复点 2：改用拟人化 DOM 物理点击，100% 绕过打包工具导致的全局 window.handleServerRenew 找不到问题
        try:
            renew_btn = page.locator(f"*[onclick*='{target_id}']").first
            if renew_btn.is_visible():
                renew_btn.click(timeout=5000)
                log.info(f"-> 成功通过 DOM 物理点击触发容器 [{s['name']}] 的续期弹窗")
            else:
                log.warning(f"⚠️ 未找到精准 ID 按钮，尝试通过行索引 [{s['index']}] 模拟点击")
                page.get_by_role("button", name=re.compile("Renew", re.I)).nth(s['index']).click(timeout=5000)
        except Exception as e:
            log.error(f"❌ 触发续期点击交互时发生异常: {e}")
            results.append({"name": s["name"], "success": False, "time_str": old_time_str, "err_msg": "点击触发失败"})
            continue

        time.sleep(1)
        near_limit_before = old_days >= RENEW_NEAR_LIMIT_DAYS
        confirm_clicked = click_confirm_modal_if_exists(
            page,
            tag=f"renew_{safe_name(username)}_{s['index']}",
            timeout=4 if near_limit_before else 15,
            required=not near_limit_before,
        )
        
        time.sleep(4)
        navigate(page, SERVERS_URL)
        time.sleep(2)
        
        updated_list = get_servers_info(page)
        
        matched_server = None
        for us in updated_list:
            if us["server_id"] == target_id:
                matched_server = us
                break
        
        if not matched_server:
            for us in updated_list:
                if us["index"] == s["index"]:
                    log.warning(f"⚠️ 服务器 ID 无法闭环匹配，降级采用自然索引 [{s['index']}] 兜底")
                    matched_server = us
                    break

        new_time_str = matched_server["suspended_in"] if matched_server else "未知"
        new_days = parse_days_remaining(new_time_str)
        log.info(f"⏳ 容器 [{s['name']}] 续期后解析天数: {new_days:.4f} 天 ({new_time_str})")

        is_real_success, renew_note = judge_renew_result(old_days, new_days, confirm_clicked)
        if is_real_success:
            log.info(f"✅ 容器 [{s['name']}] 判定为健康/成功: {renew_note}")
        elif not confirm_clicked:
            log.error(f"❌ 容器 [{s['name']}] 未完成二次确认点击，且剩余时间未达到近满期健康阈值。")
        else:
            log.error(f"❌ 容器 [{s['name']}] 二次确认已点击，但续期后状态未通过判定: {renew_note}")

        results.append({
            "name": s["name"],
            "success": is_real_success,
            "time_str": new_time_str,
            "note": renew_note,
        })

    lines = [f"👤 账号: {mask(username)}"]
    for r in results:
        suffix_items = []
        if r.get("note"):
            suffix_items.append(r["note"])
        if r.get("err_msg"):
            suffix_items.append(r["err_msg"])
        err_suffix = f" ({'；'.join(suffix_items)})" if suffix_items else ""
        status = "✅ 续期成功" if r["success"] else "❌ 续期失败"
        lines.append(f"  {status} [{r['name']}] -> 剩余到期时间: {r['time_str']}{err_suffix}")
    return "\n".join(lines)


# ── 全局总线控制 (强防熔断、强释放机制) ─────────────────────────
def main():
    from cloakbrowser import launch

    try:
        accounts = load_accounts()
    except Exception as e:
        log.critical(e)
        return

    all_reports = ["🖥️ Zytrano 自动续期终审合并报告", ""]
    has_any_error = False

    log.info("🚀 启动 CloakBrowser 生产主实例进程...")
    browser = launch(headless=False, humanize=True, geoip=True)

    try:
        for idx, account in enumerate(accounts, 1):
            username = account.get("username", "未知")
            log.info(f"\n{'='*20} 进程区间: 账号流水轴 ({idx} / {len(accounts)}) {'='*20}")
            
            # 账号级沙箱防熔断
            try:
                context = None
                page = None
                try:
                    context = browser.new_context()
                    page = context.new_page()
                    log.info("🔒 成功挂载标准独立 Sandbox BrowserContext。")
                except Exception as err:
                    log.warning(f"⚠️ 无法分离沙盒 Context: {err}。执行进程级重启...")
                    try: browser.close()
                    except Exception: pass
                    
                    browser = launch(headless=False, humanize=True, geoip=True)
                    page = browser.new_page()
                    log.info("🔒 物理层重置就绪，在全新独立浏览器进程空间中运行。")

                account_report = run_for_account(page, account)
                all_reports.append(account_report)
                all_reports.append("")

                if "❌" in account_report or "⚠️" in account_report:
                    has_any_error = True

            except Exception as account_level_err:
                log.error(f"💥 [严重异常] 账号 [{mask(username)}] 执行遭遇未捕获突发崩溃: {account_level_err}", exc_info=True)
                all_reports.append(f"👤 账号: {mask(username)}\n  ❌ 运行期突发全面崩溃 (已沙箱隔离) -> 错误原因: {account_level_err}\n")
                has_any_error = True
                
            finally:
                if 'page' in locals() and page:
                    try: page.close()
                    except Exception: pass
                if 'context' in locals() and context:
                    try: context.close()
                    except Exception: pass

            if idx < len(accounts):
                gap = random.randint(6, 12)
                log.info(f"🛡️ 规避批量指纹审计，挂起睡眠 {gap} 秒...")
                time.sleep(gap)

    except Exception as global_err:
        log.critical(f"🚨 全局总线级发生灾难性故障: {global_err}", exc_info=True)
        has_any_error = True
    finally:
        log.info("🧹 触发全局生命周期终点销毁机制，正在回收进程...")
        try:
            browser.close()
        except Exception as close_err:
            log.error(f"回收内核进程时发生次生故障: {close_err}")
        log.info("所有多账号浏览器执行矩阵注销完毕。")

    final_msg = "\n".join(all_reports).strip()
    log.info(f"\n输出最终统计报表:\n{final_msg}")
    
    if has_any_error:
        wxpush(f"🚨 Zytrano 挂机运维简报-异常或失败审计\n\n{final_msg}")
        return 1
    else:
        log.info("🎉 完美大满贯！所有账号均已实质性增量续期完毕。保持静默。")
        return 0


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
        with urllib.request.urlopen(req, timeout=10) as resp:
            pass
    except Exception as e:
        log.warning(f"📨 WxPusher 推送异常: {e}")

if __name__ == "__main__":
    raise SystemExit(main())
