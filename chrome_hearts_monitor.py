#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Chrome Hearts 官网上新 / 补货监控器
------------------------------------
抓取 chromehearts.com 各在线品类的商品（名称、价格、图片、库存、码数），
与上次保存的状态对比，发现「上新 / 补货 / 改价 / 下架」时通过微信（Server酱 / PushPlus）
或 Bark / 自定义 Webhook 推送到手机。

设计要点：
- 官网为 Salesforce Commerce Cloud（Demandware），品类页为服务器直出 HTML，
  用 requests + BeautifulSoup 即可解析，无需浏览器。
- 商品唯一标识 = 商品详情页 URL 文件名（SKU，如 162006CRYXXX271）。
- 码数 / 颜色 / 逐尺码库存来自商品详情页（PDP）的 JSON-LD 与尺码色卡，
  仅在「上新 / 补货」等需要通知时才抓取详情页，控制请求量。

用法：
    python chrome_hearts_monitor.py            # 正常跑一次（供 cron / GitHub Actions 调用）
    python chrome_hearts_monitor.py --dry-run  # 不真正发送，只打印将要推送的内容
    python chrome_hearts_monitor.py --selftest # 用内置样本离线自测解析器

所有配置走环境变量，见 README。
"""

import argparse
import html as htmllib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

BASE = "https://www.chromehearts.com"

# 已知在线品类的兜底清单（官网会动态增删品类，脚本每次运行还会从首页导航自动发现新品类）。
BASELINE_CATEGORIES = [
    "scents",
    "baccarat",
    "boxers-leggings",
    "intimates",
    "socks",
    "scarf",
    "hat",
]

# 首页导航里这些单段链接不是商品品类，发现时跳过。
IGNORE_SLUGS = {
    "", "home", "general", "stores", "store", "store-locator", "storelocator",
    "magazine", "world", "world-of-chrome-hearts", "about", "contact",
    "customer-care", "customercare", "care", "faq", "faqs", "shipping",
    "returns", "privacy", "terms", "legal", "accessibility", "careers",
    "jobs", "press", "account", "login", "logout", "register", "signin",
    "sign-in", "wishlist", "cart", "bag", "checkout", "search", "sitemap",
    "authorized-retailers", "retailers", "gift-card", "gift-cards",
    "newsletter", "subscribe", "en", "us", "en_us", "en-us",
}

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def env_bool(name, default=False):
    v = env(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on", "y")


class Config:
    def __init__(self):
        cats = env("CH_CATEGORIES")
        # CH_CATEGORIES：额外/指定要监控的品类（逗号分隔）；留空则用兜底清单 + 自动发现。
        self.extra_categories = [c.strip() for c in cats.split(",") if c.strip()] if cats else []
        # 每次运行从首页导航自动发现当前所有品类（应对官网动态增删品类，如新出的 hat）。
        self.discover = env_bool("CH_DISCOVER", True)
        self.state_file = env("STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "state.json"))
        self.detail_fetch = env_bool("DETAIL_FETCH", True)          # 通知时抓详情页取码数
        self.notify_price_change = env_bool("NOTIFY_PRICE_CHANGE", True)
        self.notify_removed = env_bool("NOTIFY_REMOVED", False)     # 下架是否通知
        self.notify_soldout = env_bool("NOTIFY_SOLDOUT", False)     # 由有货变售罄是否通知
        self.silent_first_run = env_bool("SILENT_FIRST_RUN", False) # 首次运行是否静默（不发启动提示）
        self.request_delay = float(env("REQUEST_DELAY", "0.8"))
        self.timeout = float(env("REQUEST_TIMEOUT", "25"))
        # 推送渠道
        self.serverchan_key = env("SERVERCHAN_SENDKEY")
        self.pushplus_token = env("PUSHPLUS_TOKEN")
        self.pushplus_topic = env("PUSHPLUS_TOPIC")                 # 群组编码，可空
        self.pushplus_template = env("PUSHPLUS_TEMPLATE", "markdown")
        self.bark_key = env("BARK_KEY")                            # 可为完整 URL 或 key
        self.webhook_url = env("WEBHOOK_URL")


# --------------------------------------------------------------------------- #
# 抓取
# --------------------------------------------------------------------------- #
def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    })
    return s


def fetch(session, url, cfg, tries=3):
    return fetch_full(session, url, cfg, tries=tries)[0]


def fetch_full(session, url, cfg, tries=3):
    """返回 (html, 最终URL)。requests 默认跟随重定向，最终URL用于识别品类页跳转到的单商品。"""
    last = None
    for i in range(tries):
        try:
            r = session.get(url, timeout=cfg.timeout)
            if r.status_code == 200 and r.text:
                return r.text, r.url
            last = f"HTTP {r.status_code}"
        except Exception as e:  # noqa
            last = str(e)
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"抓取失败 {url}: {last}")


# --------------------------------------------------------------------------- #
# 动态发现品类（官网会动态增删品类）
# --------------------------------------------------------------------------- #
CATEGORY_SLUG_RE = re.compile(r"^/([a-z0-9][a-z0-9-]*)/?$")


def discover_categories(session, cfg):
    """从首页导航自动发现当前的单段品类链接。失败时返回空表（不影响兜底清单）。"""
    try:
        html, _ = fetch_full(session, BASE, cfg)
    except Exception as e:  # noqa
        print(f"[warn] 首页抓取失败，跳过自动发现：{e}", file=sys.stderr)
        return []
    soup = BeautifulSoup(html, "html.parser")
    found = []
    seen = set()
    for a in soup.select("a[href]"):
        href = (a.get("href") or "").split("?")[0].split("#")[0]
        m = CATEGORY_SLUG_RE.match(href)
        if not m:
            continue
        slug = m.group(1)
        if slug in IGNORE_SLUGS or slug in seen:
            continue
        seen.add(slug)
        found.append(slug)
    return found[:40]


# --------------------------------------------------------------------------- #
# 抓取一个品类：可能是商品网格，也可能（品类只有一件时）跳转到单商品详情页
# --------------------------------------------------------------------------- #
def scrape_category(session, cat, cfg):
    url = f"{BASE}/{cat}"
    try:
        html, final_url = fetch_full(session, url, cfg)
    except Exception as e:  # noqa
        print(f"[warn] 品类 {cat} 抓取失败，跳过：{e}", file=sys.stderr)
        return {}
    # 解析出错也只跳过该品类，绝不拖垮整个任务
    try:
        products = parse_category(html, cat)
        if products:
            return products
        # 品类页跳转到了单个商品详情页（该品类当前只有一件）
        if "application/ld+json" in html and '"@type":"Product"' in html.replace(" ", ""):
            one = parse_single_product(html, final_url, cat)
            if one:
                return one
    except Exception as e:  # noqa
        print(f"[warn] 品类 {cat} 解析出错，跳过：{e}", file=sys.stderr)
    return {}


# --------------------------------------------------------------------------- #
# 解析：品类页
# --------------------------------------------------------------------------- #
def _clean(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def product_id_from_href(href):
    href = (href or "").split("?")[0].split("#")[0].rstrip("/")
    if not href:
        return None
    last = href.split("/")[-1]
    if last.endswith(".html"):
        last = last[:-5]
    return last or None


def parse_category(html_text, category):
    """返回 {pid: product_dict}"""
    soup = BeautifulSoup(html_text, "html.parser")
    products = {}
    for tile in soup.select(".product-tile"):
        a = tile.select_one('a[href*=".html"]')
        if not a:
            continue
        href = a.get("href", "")
        pid = product_id_from_href(href)
        if not pid:
            continue
        img = tile.select_one("img.tile-image")
        name = None
        if img:
            name = img.get("alt") or img.get("title")
        if not name:
            link = tile.select_one("a.link")
            name = link.get_text(" ", strip=True) if link else pid
        name = _clean(name)
        price_el = tile.select_one(".price")
        price = _clean(price_el.get_text(" ", strip=True)) if price_el else ""
        image = ""
        if img:
            image = img.get("src") or ""
            if not image:
                ss = img.get("srcset", "")
                if ss:
                    image = ss.split(",")[0].strip().split(" ")[0]
        image = htmllib.unescape(image)
        if image.startswith("//"):
            image = "https:" + image
        elif image.startswith("/"):
            image = BASE + image
        sold_out = tile.select_one("a.soldout") is not None
        if not sold_out and "sold out" in tile.get_text(" ", strip=True).lower():
            sold_out = True
        url = BASE + href.split("?")[0] if href.startswith("/") else href.split("?")[0]
        products[pid] = {
            "id": pid,
            "name": name,
            "price": price,
            "image": image,
            "sold_out": sold_out,
            "url": url,
            "category": category,
        }
    return products


def _first_product_image(soup):
    for img in soup.select("img"):
        src = img.get("src") or ""
        if not src:
            ss = img.get("srcset", "")
            if ss:
                src = ss.split(",")[0].strip().split(" ")[0]
        if src and "img_products/hi-res" in src:
            src = htmllib.unescape(src)
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = BASE + src
            return src
    return ""


def parse_single_product(html_text, final_url, category):
    """把一个（品类跳转到的）单商品详情页解析成 {pid: product}，字段与品类页一致。"""
    soup = BeautifulSoup(html_text, "html.parser")
    url = None
    link = soup.select_one('link[rel="canonical"]')
    if link and link.get("href"):
        url = link["href"]
    if not url:
        og = soup.select_one('meta[property="og:url"]')
        if og and og.get("content"):
            url = og["content"]
    if not url:
        url = final_url
    url = (url or "").split("?")[0]
    if url.startswith("/"):
        url = BASE + url
    pid = product_id_from_href(url)
    if not pid:
        return {}
    name = None
    availability = None
    for s in soup.select('script[type="application/ld+json"]'):
        raw = s.string or s.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:  # noqa
            continue
        for d in (data if isinstance(data, list) else [data]):
            if isinstance(d, dict) and d.get("@type") == "Product":
                name = name or d.get("name")
                offers = d.get("offers") or {}
                if isinstance(offers, dict):
                    availability = offers.get("availability")
    if not name:
        h1 = soup.select_one("h1")
        name = _clean(h1.get_text()) if h1 else pid
    name = _clean(name)
    price_el = soup.select_one(".price")
    price = _clean(price_el.get_text(" ", strip=True)) if price_el else ""
    sold_out = (availability and "instock" not in availability.lower()) or \
        (soup.select_one("a.soldout") is not None)
    image = _first_product_image(soup)
    return {pid: {
        "id": pid, "name": name, "price": price, "image": image,
        "sold_out": bool(sold_out), "url": url, "category": category,
    }}


def parse_pdp(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    info = {
        "sizes_available": [],
        "sizes_soldout": [],
        "colors": [],
        "availability": None,
        "price": None,
        "name": None,
    }
    for s in soup.select('script[type="application/ld+json"]'):
        raw = s.string or s.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:  # noqa
            continue
        items = data if isinstance(data, list) else [data]
        for d in items:
            if isinstance(d, dict) and d.get("@type") == "Product":
                info["name"] = d.get("name") or info["name"]
                offers = d.get("offers") or {}
                if isinstance(offers, dict):
                    info["availability"] = offers.get("availability")

                    def _fmt(node):
                        try:
                            return (node or {}).get("sales", {}).get("formattedWithoutDecimals")
                        except Exception:  # noqa
                            return None
                    lp = _fmt(offers.get("lowprice"))
                    hp = _fmt(offers.get("highprice"))
                    if lp and hp and lp != hp:
                        info["price"] = f"{lp} - {hp}"
                    elif lp:
                        info["price"] = lp
                    elif hp:
                        info["price"] = hp
    size_state = {}
    for sw in soup.select(".size-value.swatch-value"):
        size = _clean(sw.get_text(" ", strip=True)) or sw.get("data-attr-value") or sw.get("aria-label")
        size = _clean(size)
        if not size:
            continue
        cls = " ".join(sw.get("class", []))
        avail = not re.search(r"unselectable|unavailable|disabled|oos", cls, re.I)
        size_state[size] = size_state.get(size, False) or avail
    for size, avail in size_state.items():
        (info["sizes_available"] if avail else info["sizes_soldout"]).append(size)
    seen_c = set()
    for cv in soup.select(".colorVal-value.swatch-value, .color-value.swatch-value"):
        c = cv.get("data-attr-value") or cv.get("aria-label") or cv.get("title") or _clean(cv.get_text())
        c = _clean(c)
        if c and c.lower() not in seen_c:
            seen_c.add(c.lower())
            info["colors"].append(c)
    return info


def load_state(path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "products" in data:
                return data
        except Exception as e:  # noqa
            print(f"[warn] 读取状态失败，按空状态处理：{e}", file=sys.stderr)
    return {"products": {}, "updated_at": None}


def save_state(path, products):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {"products": products, "updated_at": datetime.now(timezone.utc).isoformat()}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)


def diff(old_products, current_products, fetched_categories, cfg):
    changes = []
    fetched = set(fetched_categories)
    for pid, cur in current_products.items():
        prev = old_products.get(pid)
        if prev is None:
            changes.append({"type": "new", "product": cur, "old": None})
        else:
            if prev.get("sold_out") and not cur.get("sold_out"):
                changes.append({"type": "restock", "product": cur, "old": prev})
            elif (not prev.get("sold_out")) and cur.get("sold_out") and cfg.notify_soldout:
                changes.append({"type": "soldout", "product": cur, "old": prev})
            if cfg.notify_price_change and prev.get("price") and cur.get("price") and prev["price"] != cur["price"]:
                changes.append({"type": "price", "product": cur, "old": prev})
    if cfg.notify_removed:
        cur_ids = set(current_products)
        for pid, prev in old_products.items():
            if prev.get("category") in fetched and pid not in cur_ids:
                changes.append({"type": "removed", "product": prev, "old": prev})
    return changes


def merge_state(old_products, current_products, fetched_categories):
    fetched = set(fetched_categories)
    merged = {pid: p for pid, p in old_products.items() if p.get("category") not in fetched}
    merged.update(current_products)
    return merged


TYPE_LABEL = {
    "new": "🆕 上新",
    "restock": "🔁 补货",
    "price": "💲 改价",
    "soldout": "⛔ 售罄",
    "removed": "❌ 下架",
}


def enrich_sizes(session, changes, cfg):
    if not cfg.detail_fetch:
        return
    want = {"new", "restock"}
    cache = {}
    for ch in changes:
        if ch["type"] not in want:
            continue
        p = ch["product"]
        url = p.get("url")
        if not url:
            continue
        if url in cache:
            ch["detail"] = cache[url]
            continue
        try:
            html_text = fetch(session, url, cfg)
            detail = parse_pdp(html_text)
            cache[url] = detail
            ch["detail"] = detail
        except Exception as e:  # noqa
            print(f"[warn] 详情页抓取失败 {url}: {e}", file=sys.stderr)
        time.sleep(cfg.request_delay)


def _sizes_line(detail):
    if not detail:
        return None
    avail = detail.get("sizes_available") or []
    soldout = detail.get("sizes_soldout") or []
    if not avail and not soldout:
        return None
    parts = []
    if avail:
        parts.append("有货：" + " / ".join(avail))
    if soldout:
        parts.append("售罄：" + " / ".join(soldout))
    return "；".join(parts)


def build_markdown(changes):
    counts = {}
    for ch in changes:
        counts[ch["type"]] = counts.get(ch["type"], 0) + 1
    summ = " / ".join(f"{TYPE_LABEL.get(t, t)} {n}" for t, n in counts.items())
    title = f"Chrome Hearts 更新 · {len(changes)} 项（{summ}）"
    lines = []
    for ch in changes:
        p = ch["product"]
        detail = ch.get("detail") or {}
        lines.append(f"### {TYPE_LABEL.get(ch['type'], ch['type'])} · {p.get('name','')}")
        price = detail.get("price") or p.get("price") or ""
        if ch["type"] == "price" and ch.get("old"):
            lines.append(f"- 价格：~~{ch['old'].get('price','')}~~ → **{p.get('price','')}**")
        elif price:
            lines.append(f"- 价格：{price}")
        lines.append(f"- 分类：{p.get('category','')}")
        sl = _sizes_line(detail)
        if sl:
            lines.append(f"- 码数：{sl}")
        if detail.get("colors"):
            lines.append(f"- 颜色：{' / '.join(detail['colors'])}")
        if p.get("image"):
            lines.append(f"\n![{p.get('name','')}]({p['image']})")
        if p.get("url"):
            lines.append(f"\n🔗 [查看商品]({p['url']})")
        lines.append("\n---")
    body = "\n".join(lines)
    return title, body


def build_plaintext(changes):
    out = []
    for ch in changes:
        p = ch["product"]
        detail = ch.get("detail") or {}
        seg = f"{TYPE_LABEL.get(ch['type'], ch['type'])} {p.get('name','')} {detail.get('price') or p.get('price','')}"
        sl = _sizes_line(detail)
        if sl:
            seg += f"（{sl}）"
        out.append(seg)
    return "\n".join(out)


def push_serverchan(cfg, title, markdown):
    key = cfg.serverchan_key
    url = f"https://sctapi.ftqq.com/{key}.send"
    r = requests.post(url, data={"title": title[:100], "desp": markdown}, timeout=cfg.timeout)
    ok = False
    try:
        ok = r.json().get("code", -1) == 0
    except Exception:  # noqa
        ok = r.status_code == 200
    print(f"[push] Server酱 -> {'OK' if ok else '失败'} ({r.status_code})")
    return ok


def push_pushplus(cfg, title, markdown):
    payload = {
        "token": cfg.pushplus_token,
        "title": title,
        "content": markdown,
        "template": cfg.pushplus_template,
    }
    if cfg.pushplus_topic:
        payload["topic"] = cfg.pushplus_topic
    r = requests.post("https://www.pushplus.plus/send", json=payload, timeout=cfg.timeout)
    ok = False
    try:
        ok = str(r.json().get("code")) == "200"
    except Exception:  # noqa
        ok = r.status_code == 200
    print(f"[push] PushPlus -> {'OK' if ok else '失败'} ({r.status_code})")
    return ok


def push_bark(cfg, title, plaintext, first_url=None):
    key = cfg.bark_key
    base = key if key.startswith("http") else f"https://api.day.app/{key}"
    payload = {"title": title, "body": plaintext, "group": "ChromeHearts"}
    if first_url:
        payload["url"] = first_url
    r = requests.post(base, json=payload, timeout=cfg.timeout)
    print(f"[push] Bark -> {'OK' if r.status_code == 200 else '失败'} ({r.status_code})")
    return r.status_code == 200


def push_webhook(cfg, title, markdown, changes):
    payload = {"title": title, "markdown": markdown,
               "changes": [{"type": c["type"], **c["product"]} for c in changes]}
    r = requests.post(cfg.webhook_url, json=payload, timeout=cfg.timeout)
    print(f"[push] Webhook -> {'OK' if r.ok else '失败'} ({r.status_code})")
    return r.ok


def send_all(cfg, title, markdown, plaintext, changes, dry_run=False):
    if dry_run:
        print("=" * 60)
        print("[dry-run] 标题：", title)
        print("-" * 60)
        print(markdown)
        print("=" * 60)
        return True
    sent_any = False
    first_url = changes[0]["product"].get("url") if changes else None
    if cfg.serverchan_key:
        sent_any = push_serverchan(cfg, title, markdown) or sent_any
    if cfg.pushplus_token:
        sent_any = push_pushplus(cfg, title, markdown) or sent_any
    if cfg.bark_key:
        sent_any = push_bark(cfg, title, plaintext, first_url) or sent_any
    if cfg.webhook_url:
        sent_any = push_webhook(cfg, title, markdown, changes) or sent_any
    if not (cfg.serverchan_key or cfg.pushplus_token or cfg.bark_key or cfg.webhook_url):
        print("[warn] 未配置任何推送渠道（SERVERCHAN_SENDKEY / PUSHPLUS_TOKEN / BARK_KEY / WEBHOOK_URL），只在日志打印。")
        print(title)
        print(markdown)
    return sent_any


def run(cfg, dry_run=False):
    session = make_session()
    categories = []
    seen = set()

    def _add(slugs):
        for s in slugs:
            if s and s not in seen:
                seen.add(s)
                categories.append(s)

    if cfg.discover:
        discovered = discover_categories(session, cfg)
        if discovered:
            print(f"[info] 自动发现品类：{', '.join(discovered)}")
        _add(discovered)
    _add(BASELINE_CATEGORIES)
    _add(cfg.extra_categories)

    current = {}
    fetched_categories = []
    for cat in categories:
        got = scrape_category(session, cat, cfg)
        if not got:
            time.sleep(cfg.request_delay)
            continue
        current.update(got)
        fetched_categories.append(cat)
        print(f"[ok] {cat}: {len(got)} 件")
        time.sleep(cfg.request_delay)

    if not fetched_categories:
        print("[warn] 本次未抓到任何品类（官网可能临时打不开），跳过本次，等待下次自动重试。",
              file=sys.stderr)
        return 0

    state = load_state(cfg.state_file)
    old_products = state.get("products", {})
    first_run = not old_products

    if first_run:
        merged = merge_state(old_products, current, fetched_categories)
        if not dry_run:
            save_state(cfg.state_file, merged)
        by_cat = {}
        for p in current.values():
            by_cat[p["category"]] = by_cat.get(p["category"], 0) + 1
        summary = "，".join(f"{k} {v}" for k, v in by_cat.items())
        title = f"Chrome Hearts 监控已启动 · 已收录 {len(current)} 件"
        body = (f"基线已建立：{summary}。\n\n此后出现**上新 / 补货 / 改价**会第一时间推送到你手机。")
        print(f"[info] 首次运行，建立基线 {len(current)} 件。")
        if not cfg.silent_first_run:
            send_all(cfg, title, body, body, [], dry_run=dry_run)
        return 0

    changes = diff(old_products, current, fetched_categories, cfg)
    merged = merge_state(old_products, current, fetched_categories)

    if not changes:
        changed_on_disk = json.dumps(old_products, sort_keys=True, ensure_ascii=False) \
            != json.dumps(merged, sort_keys=True, ensure_ascii=False)
        if changed_on_disk and not dry_run:
            save_state(cfg.state_file, merged)
        print("[info] 无变化。")
        return 0

    enrich_sizes(session, changes, cfg)
    title, markdown = build_markdown(changes)
    plaintext = build_plaintext(changes)
    send_all(cfg, title, markdown, plaintext, changes, dry_run=dry_run)

    if not dry_run:
        save_state(cfg.state_file, merged)
    print(f"[info] 本次 {len(changes)} 项变更已处理。")
    return 0


def selftest():
    here = os.path.dirname(os.path.abspath(__file__))
    fx = os.path.join(here, "tests", "sample_category.html")
    with open(fx, "r", encoding="utf-8") as f:
        html_text = f.read()
    products = parse_category(html_text, "scents")
    assert len(products) == 3
    p1 = products["162006CRYXXX271"]
    assert p1["name"] == "+22+ Eau de Parfum"
    assert p1["price"] == "$500"
    assert p1["sold_out"] is False
    p3 = products["162008CRYXXX271"]
    assert p3["sold_out"] is True
    print("selftest OK")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Chrome Hearts 上新/补货监控")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    cfg = Config()
    return run(cfg, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
