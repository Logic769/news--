"""
通用化新闻采集引擎
基于 monitor_sites 表的 scraper_config JSON 配置，爬取任意网站新闻
支持: meta 标签提取 / CSS 选择器提取 / 日期格式自适应
"""

import requests
from bs4 import BeautifulSoup
import pymysql
from pymysql.cursors import DictCursor
import json
import re
import time
import hashlib
import logging
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import unescape as html_unescape
from urllib.parse import urljoin, urlparse
from config import (
    DB_CONFIG, HEADERS, SCRAPER_DELAY, SCRAPER_DEFAULT_PAGES, AI_CONFIG,
    SCRAPER_TIMEOUT, SCRAPER_MAX_RETRIES, SCRAPER_BACKOFF,
    SCRAPER_DETAIL_WORKERS, SCRAPER_MAX_ITEMS_PER_PAGE,
    SCRAPER_JS_RENDER_ENABLED, SCRAPER_QUALITY_THRESHOLD,
)

# ==================== 配置 ====================

REQUEST_DELAY = SCRAPER_DELAY
DEFAULT_PAGES = SCRAPER_DEFAULT_PAGES

# ==================== 扫描进度追踪 ====================

_scan_status = {
    "active": False,
    "type": "",             # "full" | "single"
    "site_name": "",
    "total_sites": 0,
    "current_site_index": 0,
    "current_site_name": "",
    "current_page": 0,
    "total_pages": 0,
    "detail_index": 0,
    "detail_total": 0,
    "news_found": 0,
    "news_saved": 0,
    "error_count": 0,
    "filtered_count": 0,    # 被鉴别机制过滤掉的无效条目数
    "start_time": "",
    "status_text": "",
    "phase": "",            # "analyzing" | "listing" | "detail" | "saving" | "done"
}


def get_scan_status():
    """返回当前扫描状态的快照"""
    return dict(_scan_status)


def _reset_scan_status():
    for k in _scan_status:
        if isinstance(_scan_status[k], int):
            _scan_status[k] = 0
        else:
            _scan_status[k] = "" if k != "active" else False
    _scan_status["active"] = False


def _begin_scan(scan_type, site_name="", total_sites=1, total_pages=1):
    _reset_scan_status()
    _scan_status["active"] = True
    _scan_status["type"] = scan_type
    _scan_status["site_name"] = site_name
    _scan_status["total_sites"] = total_sites
    _scan_status["total_pages"] = total_pages
    _scan_status["start_time"] = datetime.now().strftime("%H:%M:%S")
    _scan_status["status_text"] = "扫描启动中..."


def _end_scan(news_saved=0, news_found=0, error_count=0):
    _scan_status["active"] = False
    _scan_status["phase"] = "done"
    _scan_status["news_saved"] = news_saved
    _scan_status["news_found"] = news_found
    _scan_status["error_count"] = error_count
    _scan_status["status_text"] = "扫描完成"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("site_scanner.log", encoding="utf-8"), logging.StreamHandler()])
logger = logging.getLogger("site_scanner")

_USER_AGENTS = [
    HEADERS.get("User-Agent", "Mozilla/5.0"),
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
]


def _request_headers(config=None):
    headers = dict(HEADERS)
    headers["User-Agent"] = random.choice(_USER_AGENTS)
    if config and config.get("headers"):
        headers.update(config.get("headers") or {})
    return headers


def _detect_js_required(html):
    if not html:
        return True
    soup = BeautifulSoup(html, "html.parser")
    body_text = soup.get_text("", strip=True)
    script_count = len(soup.find_all("script"))
    link_count = len(soup.find_all("a", href=True))
    markers = ("__NEXT_DATA__", "window.__INITIAL_STATE__", "id=\"app\"", "id=\"root\"", "data-reactroot", "vue")
    return len(body_text) < 300 and script_count >= 5 or link_count < 3 and any(m in html for m in markers)


def _render_with_playwright(url, config=None):
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=random.choice(_USER_AGENTS))
            page.goto(url, wait_until="networkidle", timeout=int(config.get("timeout", SCRAPER_TIMEOUT) * 1000) if config else SCRAPER_TIMEOUT * 1000)
            html = page.content()
            browser.close()
            return html
    except Exception as e:
        logger.warning(f"  JS渲染不可用或失败: {e}")
        return ""


def fetch_url(url, config=None, allow_js=True):
    config = config or {}
    timeout = int(config.get("timeout", SCRAPER_TIMEOUT))
    max_retries = int(config.get("max_retries", SCRAPER_MAX_RETRIES))
    backoff = float(config.get("backoff", SCRAPER_BACKOFF))
    force_js = bool(config.get("js_render", False))
    session = requests.Session()
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            resp = session.get(url, headers=_request_headers(config), timeout=timeout, allow_redirects=True)
            if resp.status_code in (403, 429, 500, 502, 503, 504) and attempt < max_retries:
                time.sleep((backoff ** attempt) + random.uniform(0.2, 0.8))
                continue
            resp.raise_for_status()
            encoding = config.get("encoding")
            if encoding:
                resp.encoding = encoding
            html = resp.text
            rendered = False
            if allow_js and SCRAPER_JS_RENDER_ENABLED and (force_js or _detect_js_required(html)):
                rendered_html = _render_with_playwright(url, config)
                if rendered_html and len(rendered_html) > len(html):
                    html = rendered_html
                    rendered = True
            return html, {"status_code": resp.status_code, "rendered": rendered, "attempts": attempt + 1, "url": resp.url}
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                time.sleep((backoff ** attempt) + random.uniform(0.2, 0.8))
    raise Exception(f"请求失败: {last_error}")


def normalize_content(text):
    lines = []
    seen = set()
    for raw in re.split(r"[\r\n]+", text or ""):
        line = re.sub(r"\s+", " ", html_unescape(raw)).strip()
        if len(line) < 2:
            continue
        key = hashlib.md5(line.encode("utf-8")).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        lines.append(line)
    return "\n".join(lines)


def clean_content_node(node, config=None):
    if not node:
        return ""
    for el in node.select("script,style,noscript,iframe,form,nav,header,footer,aside,.ad,.ads,.advert,.advertisement,.popup,.modal,.recommend,.related,.share,.breadcrumb,.comment,.comments"):
        el.decompose()
    filters = config.get("content_filter_keywords", []) if config else []
    paragraphs = []
    for p in node.find_all(["p", "section", "div"], recursive=True):
        text = p.get_text(" ", strip=True)
        if len(text) < 10:
            continue
        if filters and any(k in text for k in filters):
            continue
        paragraphs.append(text)
    if not paragraphs:
        paragraphs = [node.get_text(" ", strip=True)]
    return normalize_content("\n".join(paragraphs))


def evaluate_content_quality(title, content, detail=None):
    score = 0
    title = (title or "").strip()
    content = (content or "").strip()
    if 8 <= len(title) <= 120:
        score += 20
    if len(content) >= 300:
        score += 35
    elif len(content) >= 120:
        score += 20
    paragraphs = [p for p in content.split("\n") if len(p.strip()) >= 20]
    if len(paragraphs) >= 3:
        score += 20
    elif len(paragraphs) >= 1:
        score += 10
    if detail and detail.get("publish_date"):
        score += 10
    chinese_chars = sum(1 for c in content if '\u4e00' <= c <= '\u9fff')
    if content and chinese_chars / max(len(content), 1) >= 0.35:
        score += 10
    if len(set(paragraphs)) == len(paragraphs):
        score += 5
    return min(score, 100)


import os

_CRAWLERS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crawlers")


def _safe_filename(name):
    safe = re.sub(r"[^\w\-]", "_", name or "site")
    safe = re.sub(r"_{2,}", "_", safe).strip("_") or "site"
    return safe + ".py"


def generate_site_crawler_code(site_name, config):
    site_label = json.dumps(site_name, ensure_ascii=False)
    config_text = json.dumps(config, ensure_ascii=False, indent=4)
    db_dumps = json.dumps({k: v for k, v in DB_CONFIG.items() if k != "cursorclass"}, ensure_ascii=False)
    headers_dumps = json.dumps(HEADERS, ensure_ascii=False)

    return f'''"""
{site_name or "未命名站点"} — 专属爬虫脚本
自动生成，独立运行，不依赖 site_scanner 模块。
用法: python {_safe_filename(site_name)}
"""
import sys, os, json, re, time, hashlib, random, logging
from datetime import datetime
from html import unescape as html_unescape
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
import pymysql

# ==================== 数据库 ====================
DB_CONFIG = json.loads(r"""{db_dumps}""")

# ==================== HTTP 头 ====================
HEADERS = json.loads(r"""{headers_dumps}""")
_USER_AGENTS = [
    HEADERS.get("User-Agent", "Mozilla/5.0"),
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
]

# ==================== 站点专属爬虫配置 ====================
SITE_NAME = {site_label}
SCRAPER_CONFIG = json.loads(r"""{config_text}""")

# ==================== 日志 ====================
LOG_FILE = os.path.splitext(os.path.basename(__file__))[0] + ".log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# ──────────────── 辅助函数 ────────────────

_NAV_FILTER = {{
    "首页", "学校主页", "网站首页", "学院首页", "部门首页", "设为首页", "加入收藏",
    "登录", "注册", "English", "ENGLISH", "手机版", "网站地图", "返回首页", "主页",
    "Home", "HOME", "skip", "Skip", "菜单", "导航",
    "综合要闻", "科教在线", "工作动态", "通知公告", "学术动态", "媒体聚焦",
    "校园文化", "人才培养", "科学研究", "招生就业", "校园生活", "合作交流",
    "信息公开", "学校概况", "组织机构", "师资队伍", "学科建设", "教育教学",
    "媒体南航", "南航要闻", "菁菁校园", "理论学习", "服务指南", "信息门户",
}}

_SKIP_TAGS = {{
    "script", "style", "noscript", "iframe", "form", "nav", "header", "footer", "aside",
}}

_SKIP_CLASSES = {{
    ".ad", ".ads", ".advert", ".advertisement", ".popup", ".modal",
    ".recommend", ".related", ".share", ".breadcrumb", ".comment", ".comments",
}}

def _make_headers(config=None):
    h = dict(HEADERS)
    h["User-Agent"] = random.choice(_USER_AGENTS)
    if config and config.get("headers"):
        h.update(config["headers"])
    return h


def fetch_url(url, config=None, allow_js=True):
    config = config or {{}}
    timeout = int(config.get("timeout", 30))
    max_retries = int(config.get("max_retries", 3))
    backoff = float(config.get("backoff", 1.6))
    session = requests.Session()
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            resp = session.get(url, headers=_make_headers(config), timeout=timeout, allow_redirects=True)
            if resp.status_code in (403, 429, 500, 502, 503, 504) and attempt < max_retries:
                time.sleep((backoff ** attempt) + random.uniform(0.2, 0.8))
                continue
            resp.raise_for_status()
            if config.get("encoding"):
                resp.encoding = config["encoding"]
            return resp.text, {{"status_code": resp.status_code, "attempts": attempt + 1, "url": resp.url}}
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                time.sleep((backoff ** attempt) + random.uniform(0.2, 0.8))
    raise Exception(f"请求失败: {{last_error}}")


def normalize_content(text):
    lines, seen = [], set()
    for raw in re.split(r"[\\r\\n]+", text or ""):
        line = re.sub(r"\\s+", " ", html_unescape(raw)).strip()
        if len(line) < 2:
            continue
        key = hashlib.md5(line.encode()).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        lines.append(line)
    return "\\n".join(lines)


def clean_content_node(node, config=None):
    if not node:
        return ""
    for el in node.select("script,style,noscript,iframe,form,nav,header,footer,aside,.ad,.ads,.advert,.advertisement,.popup,.modal,.recommend,.related,.share,.breadcrumb,.comment,.comments"):
        el.decompose()
    filters = config.get("content_filter_keywords", []) if config else []
    paragraphs = []
    for p in node.find_all(["p", "section", "div"], recursive=True):
        text = p.get_text(" ", strip=True)
        if len(text) < 10:
            continue
        if filters and any(k in text for k in filters):
            continue
        paragraphs.append(text)
    if not paragraphs:
        paragraphs = [node.get_text(" ", strip=True)]
    return normalize_content("\\n".join(paragraphs))


def parse_date(text):
    if not text:
        return None
    text = str(text).strip()
    for pat, df in [
        (r"(\\d{{4}})-(\\d{{1,2}})-(\\d{{1,2}})", "%Y-%m-%d"),
        (r"(\\d{{4}})年(\\d{{1,2}})月(\\d{{1,2}})日", "%Y-%m-%d"),
        (r"(\\d{{4}})/(\\d{{1,2}})/(\\d{{1,2}})", "%Y/%m/%d"),
    ]:
        m = re.search(pat, text)
        if m:
            try:
                return datetime.strptime(m.group(), df).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return None


def parse_list(html, config):
    soup = BeautifulSoup(html, "html.parser")
    items = []
    selector = config.get("list_item_selector", "a[href]")
    if selector.startswith(".") or selector.startswith("#"):
        links = soup.select(selector)
    else:
        links = soup.select(selector) if any(c in selector for c in ".#[>") else soup.find_all(selector.split("[")[0])
    if not links and selector:
        tag_name = selector.split("[")[0].strip() if "[" in selector else selector
        links = soup.find_all(tag_name)

    id_pattern = config.get("news_id_pattern", r"(\\d+)")
    id_patterns = [id_pattern] if isinstance(id_pattern, str) else id_pattern
    base_url = config.get("base_url", "").rstrip("/")
    max_items = int(config.get("max_items_per_page", 30))
    seen_urls = set()

    for link in links:
        href = link.get("href", "")
        if not isinstance(href, str) or not href:
            continue
        if re.match(r"^(javascript|mailto|tel|ftp):", href, re.I):
            continue
        m = None
        for pat in id_patterns:
            m = re.search(pat, href)
            if m:
                break
        raw_id = m.group(1) if m else href.replace("/", "_").strip("_")
        news_id = "s" + hashlib.md5(raw_id.encode()).hexdigest()[:19]
        title = link.get_text(strip=True)
        if not title or len(title) < 4 or title in _NAV_FILTER:
            continue
        full_url = urljoin(base_url + "/", href)
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)
        parent = link.parent
        parent_text = parent.get_text(strip=True) if parent else ""
        date_match = re.search(r"(\\d{{4}}[-/年]\\d{{1,2}}[-/月]\\d{{1,2}})", parent_text)
        date_str = date_match.group(1) if date_match else ""
        items.append({{"news_id": news_id, "title": title[:500], "url": full_url, "date_str": date_str}})
        if len(items) >= max_items:
            break
    return items


def parse_detail(html, config):
    soup = BeautifulSoup(html, "html.parser")
    detail = {{"source": "", "author": "", "views": 0, "publish_date": "", "content": ""}}
    for el in soup.select("script,style,noscript,iframe,form,nav,header,footer,aside"):
        el.decompose()

    # 标题
    for meta_name in ("ArticleTitle", "title"):
        meta = soup.find("meta", attrs={{"name": meta_name}})
        if meta:
            c = meta.get("content")
            if c and isinstance(c, str) and 4 < len(c.strip()) < 200:
                detail["title"] = c.strip()
                break
    if not detail.get("title"):
        title_sel = config.get("detail_title_selector", "")
        if title_sel:
            el = soup.select_one(title_sel)
            if el:
                detail["title"] = el.get_text(strip=True)[:500]
    if not detail.get("title"):
        h1 = soup.find("h1")
        if h1:
            detail["title"] = h1.get_text(strip=True)[:500]

    # 日期
    for meta_name in ("PubDate", "publishdate", "date"):
        meta = soup.find("meta", attrs={{"name": meta_name}}) or soup.find("meta", attrs={{"property": meta_name}})
        if meta:
            c = meta.get("content")
            if c and isinstance(c, str):
                detail["publish_date"] = parse_date(c)
                break
    if not detail.get("publish_date"):
        date_sel = config.get("detail_date_selector", "")
        if date_sel:
            el = soup.select_one(date_sel)
            if el:
                detail["publish_date"] = parse_date(el.get_text())

    # 来源
    for meta_name in ("ContentSource", "source"):
        meta = soup.find("meta", attrs={{"name": meta_name}}) or soup.find("meta", attrs={{"property": meta_name}})
        if meta:
            c = meta.get("content")
            if c and isinstance(c, str):
                detail["source"] = c.strip()
                break
    if not detail.get("source"):
        src_sel = config.get("detail_source_selector", "")
        if src_sel:
            el = soup.select_one(src_sel)
            if el:
                detail["source"] = el.get_text(strip=True)

    # 作者
    for meta_name in ("author", "article:author", "byl"):
        meta = soup.find("meta", attrs={{"name": meta_name}}) or soup.find("meta", attrs={{"property": meta_name}})
        if meta:
            c = meta.get("content")
            if c and isinstance(c, str):
                detail["author"] = c.strip()
                break
    if not detail.get("author"):
        author_sel = config.get("detail_author_selector", "")
        if author_sel:
            el = soup.select_one(author_sel)
            if el:
                detail["author"] = el.get_text(strip=True)

    # 文本回退提取日期/来源/作者
    text_only = re.sub(r"<[^>]+>", " ", html)
    text_only = re.sub(r"\\s+", " ", text_only)
    for pat, key in [(r"来源[：:]\\s*(\\S+)", "source"), (r"作者[：:]\\s*(\\S+)", "author"),
                     (r"发布时间[：:]\\s*(\\S+)", "publish_date"), (r"时间[：:]\\s*(\\S+)", "publish_date")]:
        m = re.search(pat, text_only)
        if m and not detail.get(key):
            val = m.group(1).rstrip("作者来源供图时间浏览")
            detail[key] = val[:100] if key != "publish_date" else parse_date(val)
    if not detail.get("publish_date"):
        for pat in [r"(\\d{{4}}-\\d{{1,2}}-\\d{{1,2}})", r"(\\d{{4}}年\\d{{1,2}}月\\d{{1,2}}日)"]:
            m = re.search(pat, text_only)
            if m:
                detail["publish_date"] = parse_date(m.group(1))
                break

    # 正文
    content_sel = config.get("detail_content_selector", "")
    if content_sel:
        article = soup.select_one(content_sel)
        if article:
            detail["content"] = clean_content_node(article, config)
    if not detail.get("content"):
        for cls in ("wp_articlecontent", "TRS_Editor", "v_news_content", "article-content", "article_content", "content", "article", "main"):
            article = soup.find("div", class_=re.compile(cls, re.IGNORECASE))
            if article:
                content = clean_content_node(article, config)
                if content:
                    detail["content"] = content
                    break
    if not detail.get("content"):
        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p") if len(p.get_text(strip=True)) > 10]
        detail["content"] = normalize_content("\\n".join(paragraphs))
    return detail

# ──────────────── 保存 ────────────────

def _content_digest(title, content):
    text = f"{{title or ''}}|{{(content or '')[:2000]}}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def save_news_batch(news_list):
    if not news_list:
        return 0
    conn = pymysql.connect(**DB_CONFIG)
    sql = (
        "INSERT INTO news (news_id, title, category, publish_date, source, author, views, content, url, content_hash) "
        "VALUES (%(news_id)s, %(title)s, %(category)s, %(publish_date)s, %(source)s, %(author)s, %(views)s, %(content)s, %(url)s, %(content_hash)s) "
        "ON DUPLICATE KEY UPDATE "
        "title = VALUES(title), category = VALUES(category), "
        "publish_date = VALUES(publish_date), source = VALUES(source), author = VALUES(author), "
        "views = VALUES(views), content = VALUES(content), url = VALUES(url), "
        "content_hash = VALUES(content_hash)"
    )
    saved = 0
    skipped = 0
    seen = set()
    with conn.cursor() as cur:
        for news in news_list:
            digest = _content_digest(news.get("title"), news.get("content"))
            if digest in seen:
                skipped += 1
                continue
            seen.add(digest)
            news["content_hash"] = digest
            try:
                cur.execute(sql, news)
                saved += 1
            except pymysql.err.IntegrityError:
                skipped += 1
                continue
            except Exception as e:
                logger.error(f"  保存失败 [{{news.get('news_id')}}]: {{e}}")
    conn.commit()
    conn.close()
    if skipped:
        logger.info(f"  去重跳过: {{skipped}} 条")
    return saved

# ──────────────── 分类 ────────────────

_CATEGORIES = ["党建思政", "人物风采", "荣誉获奖", "教育教学", "学术科研", "交流合作", "校园活动", "会议要闻", "综合新闻"]

def classify_news(title, content=""):
    text = title + " " + (content[:500] if content else "")
    rules = [
        ("党建思政", ["习近平", "党建", "思政", "党史", "红色", "求是", "政治"]),
        ("人物风采", ["榜样", "风采", "人物", "校友", "微光成炬"]),
        ("荣誉获奖", ["喜报", "获奖", "荣获", "表彰", "佳绩", "冠军", "第一", "金牌", "荣誉"]),
        ("教育教学", ["教学", "教育", "课程", "专业认证", "思政课", "立德树人", "培养", "育人"]),
        ("学术科研", ["学术", "科研", "论坛", "研讨", "创新", "科技", "实验室", "研究"]),
        ("交流合作", ["出访", "访问", "交流", "合作", "调研", "考察", "来访", "签署"]),
        ("校园活动", ["活动", "文化", "节", "比赛", "竞赛", "运动会", "讲座", "宣讲", "仪式"]),
        ("会议要闻", ["会议", "召开", "大会", "座谈", "推进会", "工作会", "常务", "党委"]),
    ]
    for category, keywords in rules:
        for kw in keywords:
            if kw in text:
                return category
    return "综合新闻"


def evaluate_quality(title, content, detail=None):
    score = 0
    if 8 <= len(title or "") <= 120:
        score += 20
    if len(content or "") >= 300:
        score += 35
    elif len(content or "") >= 120:
        score += 20
    paragraphs = [p for p in (content or "").split("\\n") if len(p.strip()) >= 20]
    if len(paragraphs) >= 3:
        score += 20
    elif len(paragraphs) >= 1:
        score += 10
    if detail and detail.get("publish_date"):
        score += 10
    chinese = sum(1 for c in content or "" if '\\u4e00' <= c <= '\\u9fff')
    if content and chinese / max(len(content), 1) >= 0.35:
        score += 10
    if len(set(paragraphs)) == len(paragraphs):
        score += 5
    return min(score, 100)

# ──────────────── 主爬取逻辑 ────────────────

def crawl(pages=None):
    pages = pages or int(SCRAPER_CONFIG.get("default_pages", 3))
    delay = float(SCRAPER_CONFIG.get("request_delay", 1.5))
    quality_threshold = int(SCRAPER_CONFIG.get("quality_threshold", 60))
    all_news = []
    pagination = SCRAPER_CONFIG.get("list_pagination", "")
    list_url = SCRAPER_CONFIG["list_url"]

    for page in range(1, pages + 1):
        if pagination:
            page_url = urljoin(list_url, pagination.replace("{{page}}", str(page)))
        else:
            page_url = list_url if page == 1 else None
            if page_url is None:
                break

        logger.info(f"获取第{{page}}/{{pages}}页: {{page_url}}")
        try:
            html, meta = fetch_url(page_url, SCRAPER_CONFIG)
        except Exception as e:
            logger.error(f"列表页请求失败: {{e}}")
            continue

        items = parse_list(html, SCRAPER_CONFIG)
        logger.info(f"  第{{page}}页: {{len(items)}} 条链接")

        for idx, item in enumerate(items):
            time.sleep(delay + random.uniform(0, 0.3))
            try:
                detail_html, _ = fetch_url(item["url"], SCRAPER_CONFIG)
                detail = parse_detail(detail_html, SCRAPER_CONFIG)
            except Exception as e:
                logger.error(f"  详情页失败 [{{item['url'][:60]}}]: {{e}}")
                continue

            title = detail.get("title") or item["title"]
            content = detail.get("content", "")
            if not content or len(content) < 30:
                continue

            score = evaluate_quality(title, content, detail)
            if score < quality_threshold:
                logger.info(f"  [跳过] 质量评分{{score}}: {{title[:40]}}")
                continue

            all_news.append({{
                "news_id": item["news_id"],
                "title": title,
                "category": classify_news(title, content),
                "publish_date": detail.get("publish_date") or parse_date(item.get("date_str", "")),
                "source": detail.get("source", ""),
                "author": detail.get("author", ""),
                "views": detail.get("views", 0),
                "content": normalize_content(content),
                "url": item["url"],
            }})
            logger.info(f"  [✓] ({{score}}) {{title[:50]}}")

    saved = save_news_batch(all_news)
    logger.info(f"===== 完成: {{saved}}/{{len(all_news)}} 条入库 =====")
    return all_news


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=f"SITE_NAME 专属爬虫")
    parser.add_argument("-p", "--pages", type=int, default=None, help="爬取页数")
    args = parser.parse_args()
    crawl(pages=args.pages)
'''


def save_site_crawler_file(site_name, code):
    os.makedirs(_CRAWLERS_DIR, exist_ok=True)
    filename = _safe_filename(site_name)
    filepath = os.path.join(_CRAWLERS_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(code)
    logger.info(f"  已写入专属爬虫脚本: {filepath}")
    return filepath


def build_site_profile(url, name="", settings=None):
    settings = settings or {}
    if not url.startswith("http"):
        url = "https://" + url
    ai_config, ai_report = ai_generate_scraper_config(url)
    if ai_config:
        html_test, _ = fetch_url(url, {"timeout": 25, "max_retries": 1, "encoding": "utf-8"}, allow_js=False)
        from bs4 import BeautifulSoup as _BS
        test_soup = _BS(html_test, "html.parser")
        ai_sel = ai_config.get("list_item_selector", "")
        direct_hits = len(test_soup.select(ai_sel)) if ai_sel else 0
        test_items = parse_list(html_test, ai_config)
        nav_titles = {"首页", "学校主页", "网站首页", "学院首页", "部门首页", "设为首页", "加入收藏",
                      "登录", "注册", "English", "ENGLISH", "手机版", "网站地图", "返回首页",
                      "综合要闻", "科教在线", "工作动态", "通知公告", "学术动态", "媒体聚焦",
                      "校园文化", "人才培养", "科学研究", "招生就业", "校园生活", "合作交流",
                      "信息公开", "学校概况", "组织机构", "师资队伍", "学科建设", "教育教学"}
        valid_items = [it for it in (test_items or [])
                       if len(it.get("title", "")) >= 6 and it.get("title", "").strip() not in nav_titles]
        if direct_hits >= 3 and len(valid_items) >= 3:
            config = ai_config
            report = {"summary": f"AI 分析: {ai_report}", "engine": "ai", "site_name": name or urlparse(url).netloc}
        else:
            logger.warning(f"  AI 配置验证失败 (有效链接={len(valid_items)}/{len(test_items or [])})，回退规则分析")
            config, report = auto_analyze_site(url)
            report["engine"] = "rule"
    else:
        config, report = auto_analyze_site(url)
        report["engine"] = "rule"
    config.update(settings)
    config.setdefault("request_delay", REQUEST_DELAY)
    config.setdefault("max_retries", SCRAPER_MAX_RETRIES)
    config.setdefault("timeout", SCRAPER_TIMEOUT)
    config.setdefault("quality_threshold", SCRAPER_QUALITY_THRESHOLD)
    config.setdefault("max_items_per_page", SCRAPER_MAX_ITEMS_PER_PAGE)
    config.setdefault("detail_workers", SCRAPER_DETAIL_WORKERS)
    config.setdefault("js_render", False)
    crawler_code = generate_site_crawler_code(name or report.get("site_name") or urlparse(url).netloc, config)
    crawler_file = save_site_crawler_file(name or report.get("site_name") or urlparse(url).netloc, crawler_code)
    return config, report, crawler_code, crawler_file


# ==================== 分类规则（与 news_scraper.py 保持一致）====================

_CATEGORIES = ["党建思政", "人物风采", "荣誉获奖", "教育教学", "学术科研", "交流合作", "校园活动", "会议要闻", "综合新闻"]


def classify_news(title, content=""):
    """
    新闻分类: AI 优先 → 规则回退。
    返回: 分类名 (str)
    """
    # 策略0: AI 分类
    if AI_CONFIG.get("api_key"):
        cat = _ai_classify(title, content)
        if cat and cat in _CATEGORIES:
            return cat

    # 策略1: 规则分类 (回退)
    text = title + " " + (content[:500] if content else "")
    rules = [
        ("党建思政", ["习近平","党建","思政","党史","红色","求是","政治"]),
        ("人物风采", ["榜样","风采","人物","校友","微光成炬"]),
        ("荣誉获奖", ["喜报","获奖","荣获","表彰","佳绩","冠军","第一","金牌","荣誉"]),
        ("教育教学", ["教学","教育","课程","专业认证","思政课","立德树人","培养","育人"]),
        ("学术科研", ["学术","科研","论坛","研讨","创新","科技","实验室","研究"]),
        ("交流合作", ["出访","访问","交流","合作","调研","考察","来访","签署"]),
        ("校园活动", ["活动","文化","节","比赛","竞赛","运动会","讲座","宣讲","仪式"]),
        ("会议要闻", ["会议","召开","大会","座谈","推进会","工作会","常务","党委"]),
    ]
    for category, keywords in rules:
        for kw in keywords:
            if kw in text:
                return category
    return "综合新闻"


def _ai_classify(title, content=""):
    """调用 AI 对单条新闻分类，失败返回 None"""
    try:
        import urllib.request as _urllib
        text_snippet = (title + " " + (content or "")[:300]).strip()
        prompt = f"""你是一个新闻分类专家。请将以下新闻标题和内容片段分类到以下9个类别之一：

类别列表: 党建思政, 人物风采, 荣誉获奖, 教育教学, 学术科研, 交流合作, 校园活动, 会议要闻, 综合新闻

新闻内容:
标题: {title}
内容片段: {text_snippet}

请仅输出一个分类名称，不要输出任何其他文字。"""

        payload = json.dumps({
            "model": AI_CONFIG["model"],
            "messages": [
                {"role": "system", "content": "你是一个新闻分类专家，只输出分类名称。"},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 32,
            "temperature": 0.1,
        }).encode("utf-8")
        req = _urllib.Request(AI_CONFIG["api_url"], data=payload, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {AI_CONFIG['api_key']}",
        })
        with _urllib.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            category = result["choices"][0]["message"]["content"].strip()
        # 清洗可能的多余字符
        for cat in _CATEGORIES:
            if cat in category:
                return cat
        return category if category in _CATEGORIES else None
    except Exception:
        return None


# ==================== 新闻有效性鉴别 ====================

# 明确无效标题/内容的特征词（广告、导航、登录、空页面等）
_INVALID_TITLE_PATTERNS = [
    r'^(登录|注册)$',
    r'^首页$',
    r'^(关于我们|联系我们|版权|免责|隐私|网站地图)$',
    r'^(广告|推广|赞助|商)$',
    r'^(搜索|检索)',
    r'^(404|403|500|502)',
    r'(页面|网页|网站).{0,3}(不存在|已删除|已迁移)',
    r'^(暂无|暂时没有).*(内容|新闻|信息|文章)',
    r'^提示$|^消息$',
    r'^(确认|验证|安全).{0,3}(码|问题)',
]

_INVALID_CONTENT_KEYWORDS = [
    '广告位招租', '商务合作', '广告投放', '立即购买', '点击购买',
    '下载App', '扫码下载', '关注公众号', '免费领取',
    '验证码', '请输入密码', '用户名或密码',
    '浏览器不支持', '请升级浏览器', 'IE浏览器',
    '版权所有', 'Copyright',
    '网站建设', '技术支持', 'Powered by',
    '导航菜单', '网站导航', '快速链接',
    '系统维护中', '正在升级', '临时关闭',
    '页面未找到', 'Page Not Found', '404 Not Found',
]

# 有效新闻必须满足的最小特征
_MIN_TITLE_LENGTH = 4          # 标题至少4个汉字
_MIN_CONTENT_LENGTH = 30       # 正文至少30个字符
_MAX_TITLE_LENGTH = 400        # 标题过长可能是无意义文本
_MIN_P_CONTENT = 10            # 段落中有效内容至少10个字符


def validate_news(title, content, url=""):
    """
    多级鉴别: 规则快速过滤 → AI 深度校验。
    返回: (is_valid: bool, reason: str)
    """
    # ── 第一级: 基础规则过滤 ──
    text_content = (content or "").strip()

    # 空内容
    if not title or not title.strip():
        return False, "标题为空"
    if not text_content:
        return False, "正文为空"

    title_clean = title.strip()

    # 标题长度异常
    title_len = len(title_clean)
    if title_len < _MIN_TITLE_LENGTH:
        return False, f"标题过短 ({title_len}字)"

    # 标题匹配无效模式
    for pat in _INVALID_TITLE_PATTERNS:
        if re.fullmatch(pat, title_clean):
            return False, f"标题命中无效模式: {pat}"

    # 正文内容量不足
    if len(text_content) < _MIN_CONTENT_LENGTH:
        return False, f"正文过短 ({len(text_content)}字)"

    # 纯数字/符号标题
    digit_ratio = sum(1 for c in title_clean if c.isdigit() or c in '·.．.·-—/\\') / max(title_len, 1)
    if digit_ratio > 0.7:
        return False, "标题以数字/符号为主"

    # ── 第二级: 特征词规则 ──
    check_text = title_clean + " " + text_content[:500]

    # 广告/无效特征词命中数
    invalid_hits = 0
    hit_details = []
    for kw in _INVALID_CONTENT_KEYWORDS:
        if kw in check_text:
            invalid_hits += 1
            hit_details.append(kw)

    # 如果命中 ≥3 个无效特征词，直接拒绝
    if invalid_hits >= 3:
        return False, f"命中{invalid_hits}个无效特征词: {', '.join(hit_details[:3])}"

    # ── 第三级: 正文段落质量检测 ──
    # 提取所有段落文本
    paragraphs = re.split(r'\n{2,}|\r\n{2,}', text_content)
    valid_paragraphs = [p.strip() for p in paragraphs if len(p.strip()) >= _MIN_P_CONTENT]
    if not valid_paragraphs:
        return False, "无有效段落 (所有段落均 <10字)"

    total_text = "".join(valid_paragraphs)
    chinese_chars = sum(1 for c in total_text if '\u4e00' <= c <= '\u9fff')
    alpha_words = re.findall(r"[A-Za-z]{3,}", total_text)
    if len(total_text) > 0 and chinese_chars / len(total_text) < 0.15 and len(alpha_words) < 30:
        return False, "正文自然语言特征不足"

    # ── 第四级: AI 深度校验 (可选，耗时) ──
    # 如果规则检测有疑问（命中 1-2 个特征词），启用 AI
    if invalid_hits >= 1 and AI_CONFIG.get("api_key"):
        ai_result = _ai_validate_content(title, text_content[:800])
        if ai_result is False:
            return False, f"AI判定无效: {', '.join(hit_details)}"
        if ai_result is True:
            return True, "AI确认有效"

    if invalid_hits >= 1:
        # 未配 AI Key 但有可疑特征词，标记为有效但记录原因
        return True, f"规则存疑({', '.join(hit_details)})，未启用AI"

    return True, "通过"


def _ai_validate_content(title, text_snippet):
    """
    调用 AI 判断内容是否为有效新闻。
    返回: True(有效), False(无效), None(调用失败)
    """
    try:
        import urllib.request as _urllib
        prompt = f"""你是一个新闻内容审核员。请判断以下内容是否为有效的新闻文章，还是广告、导航页、登录页、空页面等无效内容。

标题: {title}
正文片段: {text_snippet}

请仅回答 「有效」 或 「无效」，不要输出其他内容。"""

        payload = json.dumps({
            "model": AI_CONFIG["model"],
            "messages": [
                {"role": "system", "content": "你是新闻审核员。仅输出「有效」或「无效」。"},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 8,
            "temperature": 0.05,
        }).encode("utf-8")
        req = _urllib.Request(AI_CONFIG["api_url"], data=payload, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {AI_CONFIG['api_key']}",
        })
        with _urllib.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            answer = result["choices"][0]["message"]["content"].strip()
        if "有效" in answer:
            return True
        if "无效" in answer:
            return False
        return None
    except Exception:
        return None


def save_filtered_item(title, content, url, source, reason, site_id=0):
    """保存被过滤的无效内容到 filtered_news 表，供管理员审核"""
    try:
        conn = pymysql.connect(**DB_CONFIG)
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO filtered_news (title, url, source, content, reason, site_id)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                [title[:500], url, source, content[:20000], reason[:300], site_id]
            )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.warning(f"  保存过滤项失败: {e}")
        return False


def parse_date(text, fmt="auto"):
    """尝试多种格式解析日期"""
    if not text:
        return None
    text = str(text).strip()
    # 常见格式
    patterns = [
        (r"(\d{4})-(\d{1,2})-(\d{1,2})", "%Y-%m-%d"),
        (r"(\d{4})年(\d{1,2})月(\d{1,2})日", "%Y-%m-%d"),
        (r"(\d{4})/(\d{1,2})/(\d{1,2})", "%Y/%m/%d"),
        (r"(\d{1,2})-(\d{1,2})\s+(\d{2}:\d{2})", None),  # 只有月-日，不给年份
    ]
    for pat, df in patterns:
        m = re.search(pat, text)
        if m:
            try:
                if df:
                    return datetime.strptime(m.group(), df).strftime("%Y-%m-%d")
                else:
                    return m.group()
            except ValueError:
                continue
    return None


# ==================== 数据库存取 ====================

def _content_digest(title, content):
    text = f"{title or ''}|{(content or '')[:2000]}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def save_news_batch(news_list):
    if not news_list:
        return 0
    conn = pymysql.connect(**DB_CONFIG, cursorclass=DictCursor)
    sql = """
        INSERT INTO news (news_id, title, category, publish_date, source, author, views, content, url, content_hash)
        VALUES (%(news_id)s, %(title)s, %(category)s, %(publish_date)s, %(source)s, %(author)s, %(views)s, %(content)s, %(url)s, %(content_hash)s)
        ON DUPLICATE KEY UPDATE
            title = VALUES(title), category = VALUES(category),
            publish_date = VALUES(publish_date), source = VALUES(source), author = VALUES(author),
            views = VALUES(views), content = VALUES(content), url = VALUES(url),
            content_hash = VALUES(content_hash)
    """
    saved = 0
    skipped = 0
    seen = set()
    with conn.cursor() as cur:
        for news in news_list:
            digest = _content_digest(news.get("title"), news.get("content"))
            if digest in seen:
                skipped += 1
                continue
            seen.add(digest)
            news["content_hash"] = digest
            try:
                cur.execute(sql, news)
                saved += 1
            except pymysql.err.IntegrityError:
                skipped += 1
                continue
            except Exception as e:
                logger.error(f"  保存失败 [{news.get('news_id')}]: {e}")
    conn.commit()
    conn.close()
    if skipped:
        logger.info(f"  去重跳过: {skipped} 条")
    return saved


def backfill_content_hash():
    conn = pymysql.connect(**DB_CONFIG, cursorclass=DictCursor)
    with conn.cursor() as cur:
        cur.execute("SELECT id, title, content, content_hash FROM news WHERE content_hash IS NULL OR content_hash=''")
        rows = cur.fetchall()
    if not rows:
        conn.close()
        return 0
    updated = 0
    with conn.cursor() as cur:
        for row in rows:
            digest = _content_digest(row.get("title"), row.get("content"))
            try:
                cur.execute("UPDATE news SET content_hash=%s WHERE id=%s", [digest, row["id"]])
                updated += 1
            except pymysql.err.IntegrityError:
                cur.execute("DELETE FROM news WHERE id=%s", [row["id"]])
                updated += 1
    conn.commit()
    conn.close()
    logger.info(f"  回填哈希: {updated} 条")
    return updated


def dedup_news():
    conn = pymysql.connect(**DB_CONFIG, cursorclass=DictCursor)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT content_hash, COUNT(*) AS cnt, MIN(id) AS keep_id
            FROM news WHERE content_hash IS NOT NULL AND content_hash != ''
            GROUP BY content_hash HAVING cnt > 1
        """)
        dups = cur.fetchall()
    deleted = 0
    for dup in dups:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM news WHERE content_hash=%s AND id != %s",
                    [dup["content_hash"], dup["keep_id"]]
                )
                deleted += cur.rowcount
        except Exception:
            continue
    conn.commit()
    conn.close()
    logger.info(f"  清理冗余: {deleted} 条")
    return deleted


# ==================== 通用列表页解析 ====================

def parse_list(html, config):
    """
    通用列表页解析。
    config 关键字段:
      - list_item_selector: CSS 选择器，如 "a[href*='content_']"
      - list_item_container: (可选) 包裹元素选择器，如 "li"
      - news_id_pattern: 从 URL 中提取 ID 的正则
      - base_url: 拼接相对路径的基础 URL
    """
    soup = BeautifulSoup(html, "html.parser")
    items = []

    # 找到匹配的链接
    selector = config.get("list_item_selector", "a[href]")
    if selector.startswith(".") or selector.startswith("#"):
        links = soup.select(selector)
    else:
        links = soup.select(selector) if any(c in selector for c in ".#[>") else soup.find_all(selector.split("[")[0])

    if not links and selector:
        tag_name = selector.split("[")[0].strip() if "[" in selector else selector
        links = soup.find_all(tag_name)

    id_pattern = config.get("news_id_pattern", r"(\d+)")
    id_patterns = [id_pattern] if isinstance(id_pattern, str) else id_pattern
    base_url = config.get("base_url", "").rstrip("/")
    max_items = int(config.get("max_items_per_page", SCRAPER_MAX_ITEMS_PER_PAGE))
    seen_urls = set()

    _nav_filter = {"首页", "学校主页", "网站首页", "学院首页", "部门首页", "设为首页",
                   "加入收藏", "登录", "注册", "English", "ENGLISH", "手机版", "网站地图",
                   "返回首页", "主页", "Home", "HOME", "skip", "Skip", "菜单", "导航",
                   "综合要闻", "科教在线", "工作动态", "通知公告", "学术动态", "媒体聚焦",
                   "校园文化", "人才培养", "科学研究", "招生就业", "校园生活", "合作交流",
                   "信息公开", "学校概况", "组织机构", "师资队伍", "学科建设", "教育教学",
                   "媒体南航", "南航要闻", "菁菁校园", "理论学习", "服务指南", "信息门户"}

    for link in links:
        href = link.get("href", "")
        if not isinstance(href, str) or not href:
            continue
        if re.match(r"^(javascript|mailto|tel|ftp):", href, re.I):
            continue

        # 提取 news_id (限制20字符，用hash确保唯一)
        m = None
        for pat in id_patterns:
            m = re.search(pat, href)
            if m:
                break
        raw_id = m.group(1) if m else href.replace("/", "_").strip("_")
        news_id = "s" + hashlib.md5(raw_id.encode()).hexdigest()[:19]

        title = link.get_text(strip=True)
        if not title or len(title) < 4 or title in _nav_filter:
            continue

        full_url = urljoin(base_url + "/", href)
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        # 尝试从父元素提取日期
        parent = link.parent
        parent_text = parent.get_text(strip=True) if parent else ""
        date_match = re.search(r"(\d{4}[-/年]\d{1,2}[-/月]\d{1,2})", parent_text)
        date_str = date_match.group(1) if date_match else ""

        items.append({
            "news_id": news_id,
            "title": title[:500],
            "url": full_url,
            "date_str": date_str,
        })
        if len(items) >= max_items:
            break

    return items


# ==================== 通用详情页解析 ====================

def parse_detail(html, config):
    """
    通用详情页解析。
    config 关键字段:
      - detail_meta_source: "meta" / "selector" / "auto"
      - detail_content_selector: CSS 选择器，如 "div.article-content"
      - detail_title_selector: 标题选择器（可选）
      - detail_date_selector: 日期选择器（可选）
      - detail_source_selector: 来源选择器（可选）
    """
    soup = BeautifulSoup(html, "html.parser")
    detail = {"source": "", "author": "", "views": 0, "publish_date": "", "content": ""}
    for el in soup.select("script,style,noscript,iframe,form,nav,header,footer,aside"):
        el.decompose()

    meta_source = config.get("detail_meta_source", "auto")

    # ---- 标题提取 ----
    if meta_source in ("meta", "auto"):
        for meta_name in ("ArticleTitle", "title"):
            meta = soup.find("meta", attrs={"name": meta_name})
            if meta:
                c = meta.get("content")
                if c and isinstance(c, str) and 4 < len(c.strip()) < 200:
                    detail["title"] = c.strip()
                    break
        if not detail.get("title"):
            meta = soup.find("meta", attrs={"name": "description"})
            if meta:
                c = meta.get("content")
                if c and isinstance(c, str) and 10 < len(c.strip()) < 200:
                    detail["title"] = c.strip()
    if not detail.get("title"):
        title_sel = config.get("detail_title_selector", "")
        if title_sel:
            el = soup.select_one(title_sel)
            if el:
                detail["title"] = el.get_text(strip=True)[:500]
    if not detail.get("title"):
        h1 = soup.find("h1")
        if h1:
            detail["title"] = h1.get_text(strip=True)[:500]

    # ---- 日期提取 ----
    if meta_source in ("meta", "auto"):
        for meta_name in ("PubDate", "publishdate", "date"):
            meta = soup.find("meta", attrs={"name": meta_name})
            if meta:
                c = meta.get("content")
                if c and isinstance(c, str):
                    detail["publish_date"] = parse_date(c)
                    break
    if not detail.get("publish_date"):
        date_sel = config.get("detail_date_selector", "")
        if date_sel:
            el = soup.select_one(date_sel)
            if el:
                detail["publish_date"] = parse_date(el.get_text())

    # ---- 来源提取 ----
    if meta_source in ("meta", "auto"):
        for meta_name in ("ContentSource", "source"):
            meta = soup.find("meta", attrs={"name": meta_name}) or soup.find("meta", attrs={"property": meta_name})
            if meta:
                c = meta.get("content")
                if c and isinstance(c, str):
                    detail["source"] = c.strip()
                    break
        for meta_name in ("author", "article:author", "byl"):
            meta = soup.find("meta", attrs={"name": meta_name}) or soup.find("meta", attrs={"property": meta_name})
            if meta:
                c = meta.get("content")
                if c and isinstance(c, str):
                    detail["author"] = c.strip()
                    break
    if not detail.get("source"):
        src_sel = config.get("detail_source_selector", "")
        if src_sel:
            el = soup.select_one(src_sel)
            if el:
                detail["source"] = el.get_text(strip=True)
    if not detail.get("author"):
        author_sel = config.get("detail_author_selector", "")
        if author_sel:
            el = soup.select_one(author_sel)
            if el:
                detail["author"] = el.get_text(strip=True)

    meta_text = ""
    h1_el = soup.find("h1")
    if h1_el:
        next_node = h1_el.find_next(string=True)
        if next_node:
            parent = next_node.parent if hasattr(next_node, "parent") else h1_el.parent
            meta_text = parent.get_text("", strip=True) if parent else ""
    if meta_text:
        meta_clean = re.sub(r"\s+", " ", meta_text)
        for pat, key in [(r"来源[：:]\s*(\S+)", "source"), (r"作者[：:]\s*(\S+)", "author"),
                         (r"发布时间[：:]\s*(\S+)", "publish_date"), (r"时间[：:]\s*(\S+)", "publish_date")]:
            m = re.search(pat, meta_clean)
            if m and not detail.get(key):
                val = m.group(1).rstrip("作者来源供图时间浏览")
                detail[key] = val[:100] if key != "publish_date" else parse_date(val)

    if not detail.get("publish_date"):
        text_only = re.sub(r"<[^>]+>", " ", html)
        text_only = re.sub(r"\s+", " ", text_only)
        for pat in (r"(\d{4}-\d{1,2}-\d{1,2})", r"(\d{4}年\d{1,2}月\d{1,2}日)"):
            m = re.search(pat, text_only)
            if m:
                detail["publish_date"] = parse_date(m.group(1))
                break

    if not detail.get("author"):
        text_only = re.sub(r"<[^>]+>", " ", html)
        text_only = re.sub(r"\s+", " ", text_only)
        for pat in (r"作者[：:]\s*(\S+)", r"作者[：:]\s*([^<\s]+)"):
            m = re.search(pat, text_only)
            if m:
                detail["author"] = m.group(1).rstrip("来源供图时间浏览")[:50]
                break

    if not detail.get("source"):
        text_only = re.sub(r"<[^>]+>", " ", html)
        text_only = re.sub(r"\s+", " ", text_only)
        for pat in (r"来源[：:]\s*(\S+)", r"来源[：:]\s*([^供图<\s]+)"):
            m = re.search(pat, text_only)
            if m:
                val = m.group(1).rstrip("作者供图时间浏览")
                if len(val) <= 40 and val:
                    detail["source"] = val
                    break

    # ---- 正文提取 ----
    content_sel = config.get("detail_content_selector", "")
    if content_sel:
        article = soup.select_one(content_sel)
        if article:
            detail["content"] = clean_content_node(article, config)

    # 回退：自动寻找正文区域
    if not detail.get("content"):
        for cls in ("wp_articlecontent", "TRS_Editor", "v_news_content", "article-content", "article_content", "content", "article", "main", "wp_articlecontent"):
            article = soup.find("div", class_=re.compile(cls, re.IGNORECASE))
            if article:
                content = clean_content_node(article, config)
                if content:
                    detail["content"] = content
                    break

    # 最终回退：所有 <p> 标签
    if not detail.get("content"):
        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p") if len(p.get_text(strip=True)) > 10]
        detail["content"] = normalize_content("\n".join(paragraphs))

    return detail


# ==================== 单站点扫描 ====================

# 详情页正文区域备选选择器（按优先级排列）
_FALLBACK_CONTENT_SELECTORS = [
    "div.wp_articlecontent", "div.TRS_Editor", "div.article-content", "div.article_body", "div#article",
    "div.news-content", "div.main-content", "div.content",
    "article", "section.main", "div.post-body",
    "div.text-content", "div.detail-content",
]

# 列表页链接备选选择器
_FALLBACK_LIST_SELECTORS = [
    "li a[href]", "div.list a[href]", "ul a[href]",
    "a[href*='article']", "a[href*='news']", "a[href*='detail']",
    "a[href*='content']", "a[href*='info']",
]


def ai_generate_scraper_config(url):
    """
    调用 AI 分析目标网站，返回爬虫配置 dict。
    这是 _call_ai_for_scraping 的公开封装，用于管理员手动操作。
    返回: (config_dict, ai_report_text)
    """
    if not AI_CONFIG.get("api_key"):
        return None, "AI API 未配置"

    if not url.startswith("http"):
        url = "https://" + url

    try:
        html, _ = fetch_url(url, {"encoding": "utf-8"})
    except Exception as e:
        return None, f"无法访问目标网站: {e}"

    success, selectors, report = _call_ai_for_scraping(html, url, "manual_fix")
    if not success:
        return None, report

    # 构建完整配置
    base_url = "/".join(url.split("/")[:3])
    config = {
        "list_url": url.rstrip("/"),
        "base_url": base_url,
        "encoding": "utf-8",
        "detail_meta_source": "auto",
        "list_item_selector": selectors.get("list_item_selector", "a[href]"),
        "detail_content_selector": selectors.get("detail_content_selector", "div.article-content"),
        "detail_title_selector": selectors.get("detail_title_selector", "h1"),
        "detail_date_selector": selectors.get("detail_date_selector", ""),
        "detail_source_selector": selectors.get("detail_source_selector", ""),
        "detail_author_selector": selectors.get("detail_author_selector", ""),
        "list_pagination": selectors.get("list_pagination", ""),
        "news_id_pattern": selectors.get("news_id_pattern", r"(\d+)"),
        "js_render": bool(selectors.get("js_render", False)),
        "content_filter_keywords": selectors.get("content_filter_keywords", []),
        "quality_threshold": SCRAPER_QUALITY_THRESHOLD,
        "max_retries": SCRAPER_MAX_RETRIES,
        "timeout": SCRAPER_TIMEOUT,
        "detail_workers": SCRAPER_DETAIL_WORKERS,
        "max_items_per_page": SCRAPER_MAX_ITEMS_PER_PAGE,
    }
    return config, report


def _call_ai_for_scraping(html_sample, list_url, error_type):
    """
    调用 AI 大模型分析网页 HTML，提取正确的 CSS 选择器。
    返回: (success, dict_of_selectors, ai_report)
    """
    if not AI_CONFIG.get("api_key"):
        return False, {}, "AI API 未配置 (设置 AI_API_KEY 环境变量)"

    # 截取 HTML 片段（前 8000 字符足够分析结构）
    html_snippet = html_sample[:8000] if len(html_sample) > 8000 else html_sample
    # 去掉大量空格压缩
    html_snippet = re.sub(r'\s{2,}', ' ', html_snippet)

    prompt = f"""你是一个专业爬虫工程师。以下是目标新闻网站 "{list_url}" 的 HTML 代码片段。

{html_snippet}

任务: 分析这段 HTML，找出以下 CSS 选择器（仅输出 JSON，不要任何其他文字）:

{{
  "list_item_selector": "列表中每个新闻条目的链接选择器（如 a[href*='content_'] 或 li.news-item a）",
  "detail_content_selector": "新闻正文所在的容器选择器（如 div.article-content）",
  "detail_title_selector": "新闻标题的 CSS 选择器（如 h1.title 或 .article-title）",
  "detail_date_selector": "发布日期的 CSS 选择器（如 span.date 或 .publish-time）",
  "detail_source_selector": "来源字段的 CSS 选择器，没有则为空字符串",
  "detail_author_selector": "作者字段的 CSS 选择器，没有则为空字符串",
  "list_pagination": "分页链接的 URL 模式，将页码替换为 {{page}}（如 ?page={{page}}，无分页则为空字符串）",
  "news_id_pattern": "从 URL 中提取新闻 ID 的正则表达式（如 content_(\\\\d+) 或 article/(\\\\d+)）",
  "js_render": false,
  "content_filter_keywords": ["广告", "推荐阅读", "相关阅读"],
  "analysis": "简要说明你的分析过程（30字以内）"
}}

只输出 JSON，不要 markdown 代码块标记。"""

    _scan_status["status_text"] = "AI 正在分析网页结构..."

    try:
        import urllib.request as _urllib
        payload = json.dumps({
            "model": AI_CONFIG["model"],
            "messages": [
                {"role": "system", "content": "你是一个爬虫工程师，只输出 JSON 格式结果，不输出任何其他内容。"},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max(AI_CONFIG.get("max_tokens", 4096), 2048),
            "temperature": 0.1,  # 低温度确保确定性输出
        }).encode("utf-8")
        req = _urllib.Request(AI_CONFIG["api_url"], data=payload, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {AI_CONFIG['api_key']}",
        })
        with _urllib.urlopen(req, timeout=45) as resp:
            result = json.loads(resp.read().decode())
            content = result["choices"][0]["message"]["content"]

        # 清洗 AI 输出：去掉可能的 markdown 标记
        content = re.sub(r'^```(?:json)?\s*\n?', '', content.strip())
        content = re.sub(r'\n?```\s*$', '', content.strip())
        content = content.strip()

        ai_data = json.loads(content)
        analysis = ai_data.get("analysis", "AI分析完成")

        selectors = {
            "list_item_selector": ai_data.get("list_item_selector", ""),
            "detail_content_selector": ai_data.get("detail_content_selector", ""),
            "detail_title_selector": ai_data.get("detail_title_selector", ""),
            "detail_date_selector": ai_data.get("detail_date_selector", ""),
            "detail_source_selector": ai_data.get("detail_source_selector", ""),
            "detail_author_selector": ai_data.get("detail_author_selector", ""),
            "list_pagination": ai_data.get("list_pagination", ""),
            "news_id_pattern": ai_data.get("news_id_pattern", r"(\d+)"),
            "js_render": bool(ai_data.get("js_render", False)),
            "content_filter_keywords": ai_data.get("content_filter_keywords", []),
        }
        logger.info(f"  AI 分析完成: {analysis}")
        return True, selectors, analysis
    except Exception as e:
        logger.warning(f"  AI 分析失败: {e}")
        return False, {}, f"AI 调用失败: {str(e)[:60]}"


def _auto_heal_config(site, config, error_context):
    """
    自适应修复爬虫配置（AI 优先）。
    策略顺序: AI分析 → 重新分析 → 备选选择器 → URL变体
    返回: (new_config, healed, heal_report)
    每次策略间有延时，确保前端进度面板可见。
    """
    site_name = site.get("name", "未知站点")
    healed = False
    strategies = []
    new_config = dict(config) if config else {}
    site_url = site.get("url", "") or new_config.get("list_url", "")

    _scan_status["phase"] = "healing"
    _scan_status["status_text"] = f"检测到采集异常: {error_context}，启动自动修复..."
    strategies.append(f"触发修复 (原因: {error_context})")
    time.sleep(0.5)  # 确保前端轮询捕获 healing 阶段

    # ── 策略0: AI 大模型分析（优先）──
    if AI_CONFIG.get("api_key"):
        _scan_status["status_text"] = "AI 正在分析网页结构..."
        strategies.append("启动 AI 大模型分析网页结构")
        time.sleep(0.3)

        list_url = new_config.get("list_url", site_url)
        if not list_url.startswith("http"):
            list_url = "https://" + list_url
        try:
            resp = requests.get(list_url, headers=HEADERS, timeout=30)
            resp.encoding = "utf-8"
            success, ai_selectors, ai_report = _call_ai_for_scraping(
                resp.text, list_url, error_context
            )
            if success and ai_selectors.get("list_item_selector"):
                # 合并 AI 结果到配置
                for k, v in ai_selectors.items():
                    if v:
                        new_config[k] = v
                if not new_config.get("base_url"):
                    new_config["base_url"] = "/".join(list_url.split("/")[:3])
                healed = True
                strategies.append(f"AI 分析成功: {ai_report}")
                logger.info(f"  [{site_name}] AI 修复成功")
            else:
                strategies.append(f"AI 分析未提供有效选择器: {ai_report}")
        except Exception as e:
            strategies.append(f"AI 请求失败: {str(e)[:50]}")
    else:
        strategies.append("跳过 AI (未配置 API Key)")

    time.sleep(0.3)

    # ── 策略1: 重新完整智能分析 ──
    if not healed and site_url:
        _scan_status["status_text"] = "重新智能分析网站结构..."
        strategies.append("重新扫描网站结构")
        time.sleep(0.3)
        try:
            try:
                new_config, report = auto_analyze_site(site_url)
            except Exception:
                if not site_url.startswith("http"):
                    new_config, report = auto_analyze_site("https://" + site_url)
                else:
                    base = "/".join(site_url.split("/")[:3])
                    new_config, report = auto_analyze_site(base)
            healed = bool(new_config.get("list_url") and new_config.get("list_item_selector"))
            strategies.append(f"结构扫描{'成功' if healed else '未找到链接'}")
            logger.info(f"  [{site_name}] 重新分析{'成功' if healed else '失败'}")
        except Exception as e:
            strategies.append(f"结构扫描异常: {str(e)[:40]}")

    time.sleep(0.2)

    # ── 策略2: 备选列表选择器 ──
    if not healed and error_context in ("list_empty", "list_parse_fail") and new_config.get("list_url"):
        _scan_status["status_text"] = "尝试备选列表选择器..."
        strategies.append("测试备选列表选择器")
        for sel in _FALLBACK_LIST_SELECTORS:
            try:
                resp = requests.get(new_config["list_url"], headers=HEADERS, timeout=20)
                soup = BeautifulSoup(resp.text, "html.parser")
                links = soup.select(sel)
                if links and len(links) >= 3:
                    new_config["list_item_selector"] = sel
                    healed = True
                    strategies.append(f"生效: {sel} ({len(links)}条)")
                    break
            except Exception:
                continue
        if not healed:
            strategies.append("备选列表选择器均失败")

    time.sleep(0.2)

    # ── 策略3: 备选正文选择器 ──
    if not healed and error_context in ("detail_parse_fail",):
        _scan_status["status_text"] = "测试备选正文选择器..."
        strategies.append("测试备选正文选择器")
        for sel in _FALLBACK_CONTENT_SELECTORS:
            if sel == new_config.get("detail_content_selector", ""):
                continue
            try:
                resp = requests.get(new_config.get("list_url", site_url), headers=HEADERS, timeout=20)
                soup_all = BeautifulSoup(resp.text, "html.parser")
                test_link = soup_all.find("a", href=True)
                if test_link:
                    test_url = test_link["href"]
                    if test_url.startswith("/"):
                        test_url = new_config.get("base_url", "") + test_url
                    detail_resp = requests.get(test_url, headers=HEADERS, timeout=20)
                    detail_soup = BeautifulSoup(detail_resp.text, "html.parser")
                    article = detail_soup.select_one(sel)
                    if article:
                        p_count = len([p for p in article.find_all("p") if len(p.get_text(strip=True)) > 10])
                        if p_count >= 2:
                            new_config["detail_content_selector"] = sel
                            healed = True
                            strategies.append(f"生效: {sel} ({p_count}段)")
                            break
            except Exception:
                continue
        if not healed:
            strategies.append("备选正文选择器均失败")

    # ── 策略4: URL 变体 ──
    if not healed and error_context in ("list_empty",):
        _scan_status["status_text"] = "尝试常见 URL 变体..."
        base = new_config.get("base_url", "")
        url_variants = [base + "/news", base + "/xwzx", base + "/article", base + "/info", base + "/list"]
        for alt_url in url_variants:
            try:
                strategies.append(f"尝试 {alt_url}")
                alt_config, alt_report = auto_analyze_site(alt_url)
                if alt_config.get("list_url") and alt_config.get("list_item_selector"):
                    new_config = alt_config
                    healed = True
                    strategies.append(f"URL变体生效: {alt_url}")
                    break
            except Exception:
                continue

    # ── 回写配置 ──
    if healed:
        _scan_status["status_text"] = "修复成功，保存配置..."
        try:
            conn_w = pymysql.connect(**DB_CONFIG)
            with conn_w.cursor() as cur:
                cur.execute(
                    "UPDATE monitor_sites SET scraper_config=%s WHERE id=%s",
                    [json.dumps(new_config, ensure_ascii=False), site.get("id", 0)]
                )
            conn_w.commit()
            conn_w.close()
            strategies.append("配置已存入数据库")
        except Exception as e:
            strategies.append(f"保存配置失败: {e}")

    heal_report = "；".join(strategies) if strategies else "无需修复"
    time.sleep(0.3)  # 让前端看到最终修复结果
    return new_config, healed, heal_report


def _try_parse_list(html, config):
    """尝试解析列表，返回 items 和解析日志"""
    items = parse_list(html, config)
    if not items:
        # 如果 config 选择器返回0条，尝试通用回退
        soup = BeautifulSoup(html, "html.parser")
        for fallback in _FALLBACK_LIST_SELECTORS:
            links = soup.select(fallback)
            if links:
                items = parse_list(html, {"list_item_selector": fallback, "news_id_pattern": r"(\d+)", "base_url": config.get("base_url", "")})
                if items:
                    break
    return items


def _try_parse_detail(html, config):
    """尝试解析详情页，失败时用备选选择器"""
    detail = parse_detail(html, config)
    if not detail.get("content") and config.get("detail_content_selector"):
        # 爬虫规则的选择器没提取到内容，尝试所有备选选择器
        soup = BeautifulSoup(html, "html.parser")
        for fallback in _FALLBACK_CONTENT_SELECTORS:
            article = soup.select_one(fallback)
            if article:
                content = clean_content_node(article, config)
                if content:
                    detail["content"] = content
                    detail["_fallback_used"] = fallback
                    break
    return detail

def scan_site(site, pages=DEFAULT_PAGES):
    """
    扫描单个监测站点（带自动修复）。
    - 列表页解析失败/0 结果 → 自动修复并重试
    - 详情页大面积失败 → 自动修复正文选择器
    - 所有修复过程实时反映在进度面板
    """
    standalone = not _scan_status["active"]
    if standalone:
        _begin_scan("single", site_name=site.get("name", ""), total_pages=pages)

    _scan_status["current_site_name"] = site.get("name", "")
    _scan_status["total_pages"] = pages
    site_id = site.get("id", 0)

    # ── 加载/分析配置 ──
    config = {}
    raw_config = site.get("scraper_config", "")
    if raw_config:
        try:
            config = json.loads(raw_config)
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"  [{site.get('name')}] scraper_config JSON 解析失败，将自动分析")

    if not config.get("list_url"):
        site_url = site.get("url", "")
        if site_url:
            _scan_status["phase"] = "analyzing"
            _scan_status["status_text"] = "自动分析网站结构..."
            try:
                config, report = auto_analyze_site(site_url)
                _save_config(site_id, config)
            except Exception:
                config = {}

    list_url = config.get("list_url")
    if not list_url:
        _scan_status["status_text"] = "错误: 无有效 URL，已尝试自动分析"
        try:
            conn = pymysql.connect(**DB_CONFIG, cursorclass=DictCursor)
            with conn.cursor() as cur:
                cur.execute("UPDATE monitor_sites SET last_status=%s, last_error=%s WHERE id=%s", ["failed", "无有效URL或爬虫配置", site_id])
            conn.commit()
            conn.close()
        except Exception:
            pass
        if standalone:
            _end_scan(news_saved=0, news_found=0, error_count=1)
        return 0

    # ── 主扫描循环（支持最多1次修复重试）──
    pagination = config.get("list_pagination", "")
    encoding = config.get("encoding", "utf-8")
    retry_count = 0
    max_retries = 1
    all_news = []
    heal_log = []

    while retry_count <= max_retries:
        all_news = []
        total_list_errors = 0
        total_detail_errors = 0
        total_detail_attempts = 0

        logger.info(f"--- 扫描: {site.get('name')} ({list_url}) [尝试 {retry_count+1}/{max_retries+1}] ---")
        if retry_count > 0:
            _scan_status["status_text"] = f"自动修复后重试 (第{retry_count+1}次)..."

        for page in range(1, pages + 1):
            _scan_status["phase"] = "listing"
            _scan_status["current_page"] = page
            _scan_status["status_text"] = f"获取第 {page}/{pages} 页列表..."

            if pagination:
                page_url = urljoin(list_url, pagination.replace("{page}", str(page)))
            else:
                page_url = list_url

            try:
                config["encoding"] = encoding
                html, fetch_meta = fetch_url(page_url, config)
                if fetch_meta.get("rendered"):
                    config["js_render"] = True
                    _save_config(site_id, config)
                    logger.info(f"    已自动启用JS渲染: {page_url}")
            except Exception as e:
                total_list_errors += 1
                _scan_status["status_text"] = f"列表页请求异常 (第{page}页)"
                logger.error(f"    请求异常: {e}")
                continue

            # 解析列表 (带备选选择器回退)
            items = _try_parse_list(html, config)
            if not items:
                total_list_errors += 1
                _scan_status["status_text"] = f"第{page}页未提取到新闻链接"
                logger.warning(f"    第{page}页 0 条新闻链接")
                continue

            logger.info(f"    第{page}页: {len(items)} 条链接")

            # 采集详情
            _scan_status["phase"] = "detail"
            _scan_status["detail_total"] = len(items)
            page_errors = 0

            for idx, item in enumerate(items):
                _scan_status["detail_index"] = idx + 1
                _scan_status["status_text"] = f"详情 {idx+1}/{len(items)} (第{page}页)"
                total_detail_attempts += 1
                time.sleep(float(config.get("request_delay", REQUEST_DELAY)) + random.uniform(0, 0.3))

                try:
                    detail_html, detail_meta = fetch_url(item["url"], config)
                    if detail_meta.get("rendered") and not config.get("js_render"):
                        config["js_render"] = True
                        _save_config(site_id, config)
                    d = _try_parse_detail(detail_html, config)
                except Exception as e:
                    page_errors += 1
                    _scan_status["error_count"] += 1
                    logger.error(f"    详情页失败 [{item['url'][:60]}]: {e}")
                    continue

                title = d.get("title") or item["title"]
                content = d.get("content", "")
                if not content:
                    page_errors += 1
                    _scan_status["error_count"] += 1
                    continue

                content = normalize_content(content)
                quality_score = evaluate_content_quality(title, content, d)
                quality_threshold = int(config.get("quality_threshold", SCRAPER_QUALITY_THRESHOLD))
                is_valid, validate_reason = validate_news(title, content, item.get("url", ""))
                if not is_valid or quality_score < quality_threshold:
                    _scan_status["filtered_count"] += 1
                    reason = validate_reason if not is_valid else f"质量评分过低: {quality_score}/{quality_threshold}"
                    logger.info(f"    [过滤] {reason}: {title[:40]}")
                    save_filtered_item(title, content, item.get("url", ""),
                                       d.get("source", ""), reason, site_id)
                    continue

                all_news.append({
                    "news_id": item["news_id"],
                    "title": title,
                    "category": classify_news(title, content),
                    "publish_date": d.get("publish_date") or parse_date(item.get("date_str", "")),
                    "source": d.get("source", ""),
                    "author": d.get("author", ""),
                    "views": d.get("views", 0),
                    "content": content,
                    "url": item["url"],
                })
                _scan_status["news_found"] = len(all_news)

            total_detail_errors += page_errors
            if page_errors:
                logger.warning(f"    第{page}页 {page_errors}/{len(items)} 详情失败")

        # ── 判断是否需要自动修复 ──
        need_heal = False
        heal_reason = ""

        # 情况1: 所有页面都没提取到任何新闻
        if len(all_news) == 0 and retry_count < max_retries:
            need_heal = True
            heal_reason = "list_empty"
            heal_log.append(f"列表解析失败 (0 条新闻)")
        # 情况2: 详情页大面积失败 (>50%)
        elif total_detail_attempts > 0 and total_detail_errors / total_detail_attempts > 0.5 and retry_count < max_retries:
            need_heal = True
            heal_reason = "detail_parse_fail"
            heal_log.append(f"详情采集大面积失败 ({total_detail_errors}/{total_detail_attempts})")

        if need_heal:
            _scan_status["phase"] = "healing"
            _scan_status["status_text"] = f"检测到采集错误，正在自动修复..."
            logger.warning(f"  [{site.get('name')}] 触发自动修复: {heal_reason}")
            new_config, healed, report = _auto_heal_config(site, config, heal_reason)
            heal_log.append(report)
            if healed:
                config = new_config
                list_url = config.get("list_url", list_url)
                pagination = config.get("list_pagination", pagination)
                encoding = config.get("encoding", encoding)
                retry_count += 1
                _scan_status["status_text"] = f"修复成功，重试采集..."
                continue
            else:
                logger.warning(f"  [{site.get('name')}] 自动修复未能解决，终止重试")
                break  # 修复无效，放弃
        else:
            break  # 正常完成或已达到最大重试次数

    # ── 入库 ──
    _scan_status["phase"] = "saving"
    _scan_status["status_text"] = "正在保存到数据库..."
    saved = save_news_batch(all_news)
    _scan_status["news_saved"] = saved

    # 更新 last_checked
    try:
        conn = pymysql.connect(**DB_CONFIG, cursorclass=DictCursor)
        with conn.cursor() as cur:
            cur.execute("UPDATE monitor_sites SET last_checked=NOW(), last_status=%s, last_error=%s WHERE id=%s", ["completed", "；".join(heal_log[-3:]) if heal_log else "", site_id])
        conn.commit()
        conn.close()
    except Exception:
        pass

    logger.info(f"  [{site.get('name')}] 完成: {saved} 条入库 / {len(all_news)} 条解析")

    if standalone:
        _end_scan(news_saved=saved, news_found=len(all_news),
                  error_count=_scan_status["error_count"])
        if heal_log:
            _scan_status["status_text"] = f"采集完成（已自动修复 {len(heal_log)} 次）: {heal_log[-1][:60]}"

    return saved


def _save_config(site_id, config):
    """将配置写入数据库"""
    try:
        conn_w = pymysql.connect(**DB_CONFIG)
        with conn_w.cursor() as cur:
            cur.execute(
                "UPDATE monitor_sites SET scraper_config=%s WHERE id=%s",
                [json.dumps(config, ensure_ascii=False), site_id]
            )
        conn_w.commit()
        conn_w.close()
        return True
    except Exception as e:
        logger.warning(f"  回写配置失败: {e}")
        return False


# ==================== 全站扫描 ====================

def scan_all_sites(pages=DEFAULT_PAGES, status_filter="running"):
    """扫描所有启用的站点"""
    conn = pymysql.connect(**DB_CONFIG, cursorclass=DictCursor)
    with conn.cursor() as cur:
        if status_filter:
            cur.execute("SELECT * FROM monitor_sites WHERE status=%s ORDER BY id", [status_filter])
        else:
            cur.execute("SELECT * FROM monitor_sites ORDER BY id")
        sites = cur.fetchall()
    conn.close()

    total = 0
    fail = 0
    start_time = time.time()

    _begin_scan("full", total_sites=len(sites), total_pages=pages)

    for idx, site in enumerate(sites):
        _scan_status["current_site_index"] = idx + 1
        _scan_status["current_site_name"] = site.get("name", "")
        _scan_status["status_text"] = f"扫描站点 {idx+1}/{len(sites)}: {site.get('name', '')}"
        try:
            saved = scan_site(site, pages=pages)
            total += saved
        except Exception as e:
            fail += 1
            _scan_status["error_count"] += 1
            logger.error(f"站点 [{site['name']}] 扫描失败: {e}")

    duration = int(time.time() - start_time)

    _end_scan(news_saved=total, news_found=total, error_count=fail)

    # 写入采集日志
    conn = pymysql.connect(**DB_CONFIG, cursorclass=DictCursor)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO collection_logs (total_sites, success_count, fail_count, new_news_count, duration_seconds, status)
               VALUES (%s,%s,%s,%s,%s,'completed')""",
            [len(sites), len(sites) - fail, fail, total, duration]
        )
    conn.commit()
    conn.close()

    logger.info(f"======= 全站扫描完成: {total} 条新闻, {fail} 个站点失败, 耗时 {duration}s =======")
    return total


# ==================== 站点配置模板 ====================

SITE_TEMPLATES = {
    "general_pagination": {
        "name": "通用分页站点",
        "list_url": "https://example.com/news",
        "list_pagination": "/page/{page}",
        "list_item_selector": "a.news-title",
        "detail_meta_source": "auto",
        "detail_content_selector": "div.article-body",
        "date_format": "auto",
        "encoding": "utf-8",
        "news_id_pattern": "(\\d+)",
        "base_url": "https://example.com",
    },
}


# ==================== 智能网站分析 ====================

def auto_analyze_site(url, pages_to_check=3):
    """
    智能分析网站结构，自动探测：
      - 网站名称（从 <title> 提取）
      - 新闻列表页中的链接选择器
      - 分页规则
      - 详情页的正文选择器
      - news_id 提取规则
    返回: (config_dict, analysis_report_dict)
    """
    # 规范化 URL
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url
    base_url = "/".join(url.split("/")[:3])  # https://example.com
    list_url = url.rstrip("/")

    logger.info(f"===== 智能分析站点: {list_url} =====")

    config = {
        "list_url": list_url,
        "base_url": base_url,
        "encoding": "utf-8",
        "detail_meta_source": "auto",
        "list_item_selector": "",
        "detail_content_selector": "",
        "news_id_pattern": r"(\d+)",
        "list_pagination": "",
        "detail_title_selector": "h1",
        "detail_date_selector": "",
        "detail_source_selector": "",
        "detail_author_selector": "",
        "content_filter_keywords": ["广告", "推荐阅读", "相关阅读"],
        "quality_threshold": SCRAPER_QUALITY_THRESHOLD,
        "max_retries": SCRAPER_MAX_RETRIES,
        "timeout": SCRAPER_TIMEOUT,
        "detail_workers": SCRAPER_DETAIL_WORKERS,
        "max_items_per_page": SCRAPER_MAX_ITEMS_PER_PAGE,
        "js_render": False,
    }

    report = {
        "site_name": "",
        "list_candidates": 0,
        "detail_sampled": False,
        "pagination_detected": "",
        "sample_titles": [],
        "summary": "",
    }

    try:
        html, fetch_meta = fetch_url(list_url, config)
        config["js_render"] = bool(fetch_meta.get("rendered"))
    except Exception as e:
        raise Exception(f"无法访问目标网站: {e}")

    soup = BeautifulSoup(html, "html.parser")

    # ---- 1. 提取网站名称 ----
    title_tag = soup.find("title")
    if title_tag:
        title_text = title_tag.get_text(strip=True)
        # 常见格式: "新闻中心 - XX大学" -> "XX大学"
        for sep in (" - ", " | ", " _ ", "—", "·"):
            parts = title_text.split(sep)
            if len(parts) >= 2:
                report["site_name"] = parts[-1].strip()
                break
        if not report["site_name"]:
            report["site_name"] = title_text[:50]

    if not report["site_name"] or len(report["site_name"]) < 4:
        report["site_name"] = urlparse(base_url).netloc.replace("www.", "")

    # ---- 2. 探测新闻链接选择器 ----
    # 策略：找所有 <a> 标签，按父容器聚类，找重复模式（列表中的链接）
    all_links = []
    for a in soup.find_all("a", href=True):
        href = a.get("href")
        if not isinstance(href, str):
            continue
        text = a.get_text(strip=True)
        if len(text) < 6:
            continue  # 太短不是新闻标题
        # 跳过明显非新闻链接
        skip_patterns = ["javascript:", "mailto:", "#", "登录", "注册", "首页", "关于", "联系"]
        if any(s in href for s in skip_patterns) or any(s == text for s in skip_patterns):
            continue

        parent = a.parent
        parent_tag = parent.name if parent else ""
        parent_cls = " ".join(parent.get("class", [])) if parent and parent.get("class") else ""

        all_links.append({
            "a": a,
            "href": href,
            "text": text,
            "parent_tag": parent_tag,
            "parent_cls": parent_cls,
        })

    # 按父容器标签+class 分组，找出现次数最多的组 -> 即为列表项
    from collections import Counter
    group_key_counts = Counter(f"{l['parent_tag']}|{l['parent_cls']}" for l in all_links)
    best_group_key = group_key_counts.most_common(1)[0] if group_key_counts else ("li|", 0)
    best_count = best_group_key[1]

    logger.info(f"  检测到 {len(all_links)} 个候选链接, 最优分组: {best_group_key[0]} ({best_count} 条)")

    href_samples = [l["href"] for l in all_links[:20]]

    if best_count < 3 and all_links:
        url_patterns = [
            (r"/c\d+a\d+/page", "a[href*='/c']"),
            (r"/c\d+a\d+/page", "a[href*='/page']"),
            (r"/page\.htm", "a[href*='/page.htm']"),
            (r"/pagem\.htm", "a[href*='/pagem.htm']"),
            (r"content_\d+", "a[href*='content_']"),
            (r"/article/\d+", "a[href*='/article/']"),
            (r"/news/\d+", "a[href*='/news/']"),
            (r"/detail/\d+", "a[href*='/detail/']"),
            (r"/info/\d+", "a[href*='/info/']"),
            (r"\d+\.html?", "a[href$='.html']"),
            (r"\d+\.shtml?", "a[href$='.shtml']"),
        ]
        for pat, selector in url_patterns:
            matches = [h for h in href_samples if re.search(pat, h)]
            if len(matches) >= 2:
                config["list_item_selector"] = selector
                config["news_id_pattern"] = pat
                break

    if not config["list_item_selector"]:
        tag, cls = best_group_key[0].split("|", 1)
        if cls:
            cls_sel = "." + ".".join(cls.split())
            config["list_item_selector"] = f"{tag}{cls_sel} a[href]"
        else:
            config["list_item_selector"] = f"{tag} a[href]"

    if config["news_id_pattern"] == r"(\d+)" and config["list_item_selector"]:
        c_pat = re.compile(r"c\d+a\d+")
        if any(c_pat.search(h) for h in href_samples):
            config["news_id_pattern"] = r"/c(\d+a\d+)/page"

    # ---- 3. 探测分页规则 ----
    page_links = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        text = a.get_text(strip=True)
        if text.isdigit() and 1 <= int(text) <= 100:
            page_links.append((int(text), href))

    if not page_links:
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            m = re.search(r"listm?(\d+)\.htm", href)
            if m:
                page_links.append((int(m.group(1)), href))

    if page_links:
        page_links.sort()
        sample_href = page_links[0][1]
        pagination = re.sub(r"\d+(?=\.htm)", "{page}", sample_href)
        if pagination == sample_href:
            pagination = re.sub(r"\d+", "{page}", sample_href, count=1)
        if pagination.startswith("http"):
            pagination = pagination.replace(base_url, "")
        config["list_pagination"] = pagination
        report["pagination_detected"] = f"检测到 {len(page_links)} 个分页链接"
        logger.info(f"  检测到分页: {pagination}")

    # ---- 4. 取样详情页，探测正文选择器 ----
    if all_links:
        # 取前3个链接作为详情页样本
        sample_links = all_links[:min(3, len(all_links))]
        for sl in sample_links:
            try:
                href = sl["href"]
                detail_url = urljoin(base_url + "/", href)

                logger.info(f"  取样详情页: {detail_url}")
                report["sample_titles"].append(sl["text"][:40])

                detail_html, detail_meta = fetch_url(detail_url, config)
                if detail_meta.get("rendered"):
                    config["js_render"] = True
                detail_soup = BeautifulSoup(detail_html, "html.parser")

                # 探测正文区域: 优先匹配已知内容类名
                best_div = None
                best_p_count = 0

                for cls_pat in ("wp_articlecontent", "TRS_Editor", "article-content", "article_content", "article-body", "post-content", "entry-content"):
                    el = detail_soup.find(class_=re.compile(cls_pat, re.IGNORECASE))
                    if el:
                        p_count = len([p for p in el.find_all("p") if len(p.get_text(strip=True)) > 10])
                        if p_count >= 2:
                            best_div = el
                            best_p_count = p_count
                            cls_name = " ".join(el.get("class", []))
                            config["detail_content_selector"] = f"div.{cls_name.split()[0]}" if cls_name else f"div.{cls_pat}"
                            break

                if not best_div:
                    for div in detail_soup.find_all("div"):
                        cls_str = " ".join(div.get("class", [])) + " " + (div.get("id", "") or "")
                        if any(kw in cls_str.lower() for kw in ("footer", "nav", "header", "sidebar", "menu", "copyright", "inner")):
                            continue
                        p_count = len([p for p in div.find_all("p") if len(p.get_text(strip=True)) > 10])
                    if p_count > best_p_count:
                        best_p_count = p_count
                        best_div = div

                if best_div and best_p_count >= 2:
                    cls = best_div.get("class", [])
                    if cls:
                        config["detail_content_selector"] = f"div.{cls[0]}"
                    else:
                        config["detail_content_selector"] = f"div#{best_div.get('id')}" if best_div.get("id") else "div.article-content"

                    for sel in (".date", ".time", ".publish-time", ".pubtime", "span.date", "span.time"):
                        if detail_soup.select_one(sel):
                            config["detail_date_selector"] = sel
                            break
                    for sel in (".source", ".origin", ".from", "span.source"):
                        if detail_soup.select_one(sel):
                            config["detail_source_selector"] = sel
                            break
                    for sel in (".author", ".writer", ".byline", "span.author"):
                        if detail_soup.select_one(sel):
                            config["detail_author_selector"] = sel
                            break
                    report["detail_sampled"] = True
                    logger.info(f"    正文选择器: {config['detail_content_selector']} ({best_p_count} 段落)")
                    time.sleep(float(config.get("request_delay", REQUEST_DELAY)))
                    break  # 找到一个就够了
            except Exception as e:
                logger.warning(f"    详情页取样失败: {e}")
                continue

    # ---- 5. 回退选择器 ----
    if not config["detail_content_selector"]:
        config["detail_content_selector"] = "div.article-content"

    if not config["list_item_selector"]:
        config["list_item_selector"] = "a[href]"

    # ---- 6. 构建分析报告 ----
    report["list_candidates"] = len(all_links)
    summary_parts = [f"发现 {len(all_links)} 个候选新闻链接"]
    if report["pagination_detected"]:
        summary_parts.append(report["pagination_detected"])
    if report["detail_sampled"]:
        summary_parts.append("已取样详情页并探测正文区域")
    else:
        summary_parts.append("未能取样详情页，使用通用选择器")
    report["summary"] = "；".join(summary_parts)

    logger.info(f"  分析完成: {report['summary']}")
    logger.info(f"  配置: {json.dumps(config, ensure_ascii=False)}")

    return config, report


# ==================== 入口 ====================

if __name__ == "__main__":
    import sys
    pages = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PAGES
    scan_all_sites(pages=pages)
