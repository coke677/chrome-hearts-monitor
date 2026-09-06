# Chrome Hearts 上新 / 补货监控（微信推送）

自动监控 [chromehearts.com](https://www.chromehearts.com) 各**在线可购品类**的商品：
名称、价格、图片、库存、码数。发现 **上新 / 补货 / 改价 / 下架** 时，第一时间推送到你的手机（微信 / Bark）。

用 **GitHub Actions** 每小时云端自动跑，**免费、无需自己的服务器、不占用你电脑**。

---

## ⚠️ 先看这两点

1. **能监控的品类有限**。Chrome Hearts 官网线上只卖这几类：**香氛 Scents、Baccarat 水晶、内衣裤 Boxers & Leggings、Intimates、袜子 Socks、围巾 Scarf**。大部分珠宝、眼镜、皮具等是**门店专供、官网不上架**，因此无法监控——这是官网本身的限制，不是脚本的问题。
2. **这不是真·手机短信**。真正发短信到你手机号需要 Twilio 这类付费短信网关（要注册账号、绑卡）。这里用的是**微信推送 / Bark**，同样是「秒到手机」、而且**免费**。如果你以后确实想要短信，脚本里预留了 Webhook 接口，可以接 Twilio，我可以再帮你加。

---

## 一、准备一个推送渠道（二选一，微信推荐）

### 方式 A：Server酱（微信，最简单）
1. 手机/电脑打开 https://sct.ftqq.com ，用**微信扫码登录**。
2. 进 **SendKey** 页面，复制那串 `SCTxxxxxxxxxxxx`。
3. 按提示关注它的服务号（这样推送才会进你微信）。
> 免费版每天有条数上限，日常上新提醒足够。

### 方式 B：PushPlus（微信，条数更宽松）
1. 打开 https://www.pushplus.plus ，**微信登录**。
2. 首页「一对一推送」里复制你的 **token**。

（iPhone 也可选 **Bark**：App Store 装 Bark，复制它给的 `https://api.day.app/xxxx` 里的 key。）

---

## 二、放到 GitHub 上跑（每小时自动）

1. **建仓库**：登录 github.com → 右上角 `+` → **New repository** → 名字随意（如 `ch-monitor`）→ 建议选 **Private** → Create。
2. **上传文件**：把本文件夹里所有内容（`chrome_hearts_monitor.py`、`requirements.txt`、`.github/`、`data/`、`README.md` 等）上传到仓库。
   - 网页上传：仓库页 **Add file → Upload files**，把文件拖进去，Commit。
   - 或用 git：
     ```bash
     git init && git add . && git commit -m "init"
     git branch -M main
     git remote add origin https://github.com/你的用户名/ch-monitor.git
     git push -u origin main
     ```
3. **填推送密钥**：仓库 **Settings → Secrets and variables → Actions → New repository secret**，
   按你选的渠道添加（名字必须完全一致）：
   - Server酱：名 `SERVERCHAN_SENDKEY`，值 = 你的 SendKey
   - 或 PushPlus：名 `PUSHPLUS_TOKEN`，值 = 你的 token
   - 或 Bark：名 `BARK_KEY`，值 = 你的 key
4. **开放写权限**（让它能把「上次状态」存回仓库）：
   **Settings → Actions → General → Workflow permissions → 选 “Read and write permissions” → Save**。
5. **首次运行 / 建立基线**：**Actions** 标签页 → 左侧 `Chrome Hearts Monitor` → **Run workflow**。
   第一次会把当前所有商品记为「基线」，并给你发一条「监控已启动，已收录 N 件」。
6. 完成 ✅ 之后**每小时自动**检测，一有上新/补货就推到你手机。

> 想改频率：编辑 `.github/workflows/monitor.yml` 里的 `cron`。
> `0 * * * *` = 每小时；`*/30 * * * *` = 每 30 分钟（GitHub 定时最快约 5 分钟，且常有几分钟延迟）。

---

## 三、本地测试（可选）

```bash
pip install -r requirements.txt
python chrome_hearts_monitor.py --selftest    # 离线自测解析器（用内置真实样本）
cp .env.example .env                            # 填入你的密钥
set -a; source .env; set +a
python chrome_hearts_monitor.py --dry-run       # 真抓官网，但只打印不推送
python chrome_hearts_monitor.py                 # 真抓 + 真推送
```

---

## 四、可调项（环境变量 / Actions Variables）

| 变量 | 默认 | 说明 |
|---|---|---|
| `CH_CATEGORIES` | 上述 6 类 | 逗号分隔，自定义要监控的品类路径 |
| `NOTIFY_PRICE_CHANGE` | `1` | 改价是否通知 |
| `NOTIFY_REMOVED` | `0` | 下架是否通知 |
| `NOTIFY_SOLDOUT` | `0` | 由有货变售罄是否通知 |
| `DETAIL_FETCH` | `1` | 上新/补货时抓详情页补充码数、颜色 |
| `SILENT_FIRST_RUN` | `0` | 首次是否静默（不发启动提示） |
| `REQUEST_DELAY` | `0.8` | 请求间隔秒数（对官网友好些） |

在 GitHub 里改这些：**Settings → Secrets and variables → Actions → Variables** 里加同名变量。

---

## 五、工作原理（简述）

- 官网是 Salesforce Commerce Cloud，品类页为**服务器直出 HTML**，用 `requests + BeautifulSoup` 直接解析，无需浏览器。
- 每个商品以详情页 URL 里的 **SKU**（如 `162006CRYXXX271`）作唯一标识。
- **码数 / 颜色 / 逐尺码库存** 来自详情页的 JSON-LD 与尺码色卡，只在需要通知时才抓，控制请求量。
- 上次结果存在 `data/state.json`，每次对比得出变更；Actions 会把它提交回仓库以保持记忆。
- 某个品类抓取失败或解析到 0 件时会**跳过**该品类（不更新其状态），避免把整类误报成「下架」。

## 六、可能需要维护的地方
- 若官网**改版**（换了 class 名）或**上了反爬**（返回验证码/403），Actions 日志会报 `未解析到商品` 或 `抓取失败`。届时需要更新选择器，或改用带无头浏览器（Playwright）的方案。
- 若你想要**真短信**、或推送到**飞书/企业微信/Telegram**，用 `WEBHOOK_URL` 接你自己的转发服务即可，或找我加。
