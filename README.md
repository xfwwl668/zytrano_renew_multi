# Zytrano Auto Renew

自动续期 [Zytrano.top](https://cp.zytrano.top) 免费服务器，防止因超过 14 天未续期而被暂停。基于 GitHub Actions 定时运行，无需本地环境。

---

## 功能特性

- **定时自动续期** — 每两天自动执行一次，无需手动操作
- **Cloudflare 自动绕过** — 支持 Turnstile 验证，自动通过人机校验
- **人类行为模拟** — 随机延迟、humanize 输入、鼠标轨迹模拟，降低被识别风险
- **WebDriver 指纹隐藏** — 注入反检测脚本，伪装为普通用户浏览器
- **WxPusher 推送通知** — 续期完成后推送结果到微信（可选）
- **截图存档** — 每次运行保存关键步骤截图，便于排查问题
- **日志自动清理** — 只保留最近 2 条 workflow 运行记录，避免积累
- **敏感信息脱敏** — 日志中账号、Token、服务器 ID 均自动打码

---

## 使用方法

### 1. Fork 本仓库

点击右上角 **Fork**，将仓库复制到自己的 GitHub 账号下。

### 2. 配置 Secrets

进入仓库 **Settings → Secrets and variables → Actions**，添加以下 Secret：

| Secret 名称 | 说明 | 是否必填 |
|---|---|---|
| `ZYTRANO_USERNAME` | Zytrano 登录账号（邮箱或用户名） | ✅ 必填 |
| `ZYTRANO_PASSWORD` | Zytrano 登录密码 | ✅ 必填 |
| `WXPUSHER_TOKEN` | WxPusher AppToken | ⬜ 可选 |
| `WXPUSHER_UID` | WxPusher 接收用户 UID | ⬜ 可选 |

> 不配置 WxPusher 相关参数时，脚本仍正常运行续期，仅跳过消息推送。

### 3. 开启 Workflow 写权限

进入仓库 **Settings → Actions → General → Workflow permissions**，选择 **Read and write permissions** 并保存。

> 此权限用于自动删除旧的 workflow 运行记录。

### 4. 手动触发测试

进入 **Actions → Zytrano Auto Renew → Run workflow**，点击 **Run workflow** 手动触发一次，确认运行正常。

运行成功后可在 Actions 页面的 **Artifacts** 中下载截图，查看续期结果。

---

## 运行计划

默认每两天 UTC 02:00（北京时间 10:00）自动运行一次：

```yaml
schedule:
  - cron: '0 2 */2 * *'
```

如需修改频率，编辑 `.github/workflows/zytrano_renew.yml` 中的 `cron` 表达式即可。

---

## 工作流程

```
启动 Xvfb 虚拟显示
    ↓
启动 Chromium（反检测模式）
    ↓
导航到登录页，自动绕过 Cloudflare
    ↓
填写账号密码并登录
    ↓
读取服务器列表及剩余时间
    ↓
对每台服务器调用续期操作
    ↓
点击确认弹窗（"Yes, renew it!"）
    ↓
读取续期后最新到期时间
    ↓
推送结果到 WxPusher（可选）
    ↓
上传截图 Artifact / 清理旧运行记录
```

---

## 通知示例

续期完成后 WxPusher 推送内容示例：

```
🖥️ Zytrano 自动续期报告

✅ 已续期 [Server-1]
Suspended in: 14 days, 0 hours, 0 minutes
```

---

## 文件说明

```
.
├── .github/
│   └── workflows/
│       └── zytrano_renew.yml   # GitHub Actions 工作流
├── zytrano_renew.py            # 自动续期主脚本
└── README.md
```

---

## 依赖说明

| 依赖 | 用途 |
|---|---|
| `pydoll-python` | Chrome DevTools Protocol 浏览器控制 |
| `httpx` | HTTP 客户端（备用） |
| `chromium-browser` | 无头浏览器 |
| `xvfb` | 虚拟显示服务器（供 Chromium 渲染） |

---

## 注意事项

- 本项目仅用于自动化续期个人免费服务器，请勿用于其他用途。
- Zytrano 平台规则如有变化（如页面结构调整），可能需要更新脚本中的选择器逻辑。
- 若连续多次运行失败，请下载 Artifacts 中的截图排查具体原因。



已新增：

katabump_renew_multi.py：KataBump 多账号自动续期脚本。

默认目标就是你给的服务器页面 303320。
登录入口使用 LOGIN_URL。
支持 KataBump 登录页的 Turnstile 验证处理，登录逻辑在 login()。
续期目标执行逻辑在 renew_target()。
支持截图、结果识别、二次确认弹窗、安全过滤危险按钮、WxPusher 异常推送。
.github/workflows/katabump_renew_multi.yml：KataBump 独立定时运行工作流。

每两天北京时间 10:30 自动跑一次，对应 cron。
支持手动运行，对应 workflow_dispatch。
会录屏和上传截图，方便排查，对应 artifact 配置在 .github/workflows/katabump_renew_multi.yml。
验证结果：

已执行 python -m py_compile katabump_renew_multi.py，语法检查通过。
GitHub Secrets 配置方式：

单账号最简单配置：

KATABUMP_EMAIL：KataBump 邮箱账号
KATABUMP_PASSWORD：KataBump 密码
KATABUMP_SERVER_IDS：服务器 ID，默认可不填，因为脚本已默认 303320
多账号配置：

KATABUMP_ACCOUNTS_JSON，示例：
[
  {
    "email": "your-email@example.com",
    "password": "your-password",
    "server_ids": ["303320"]
  }
]

如果后面你有多个 KataBump 服务器，可以把 server_ids 改成多个 ID，或者用 KATABUMP_SERVER_IDS 写成逗号分隔。脚本会优先续期账号内配置的 server_ids / server_urls，否则使用全局变量，最后才使用默认的 30332
