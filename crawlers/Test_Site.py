"""
Test Site — 专属爬虫脚本
自动生成，独立运行，不依赖 site_scanner 模块。
用法: python Test_Site.py
"""
import sys, os, json, re, time, hashlib, random, logging
from datetime import datetime
from html import unescape as html_unescape
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
import pymysql

# ==================== 数据库 ====================
DB_CONFIG = json.loads(r"""{"host": "localhost", "port": 3306, "user": "root", "password": "3.1415926tcy", "database": "nchu_news", "charset": "utf8mb4"}""")

# ==================== HTTP 头 ====================
HEADERS = json.loads(r"""{"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}""")
_USER_AGENTS = [
    HEADERS.get("User-Agent", "Mozilla/5.0"),
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
]

# ==================== 站点专属爬虫配置 ====================
SITE_NAME = "Test Site"
SCRAPER_CONFIG = json.loads(r"""{
    "list_url": "https://www.example.com",
    "base_url": "https://www.example.com",
    "encoding": "utf-8",
    "detail_meta_source": "auto",
    "list_item_selector": "p a[href]",
    "detail_content_selector": "div.article-content",
    "news_id_pattern": "(\\d+)",
    "list_pagination": "",
    "detail_title_selector": "h1",
    "detail_date_selector": "",
    "detail_source_selector": "",
    "detail_author_selector": "",
    "content_filter_keywords": [
        "广告",
        "推荐阅读",
        "相关阅读"
    ],
    "quality_threshold": 60,
    "max_retries": 1,
    "timeout": 12,
    "detail_workers": 4,
    "max_items_per_page": 30,
    "js_render": false,
    "request_delay": 1.5
}""")

# ==================== 日志 ====================
LOG_FILE = os.path.splitext(os.path.basename(__file__))[0] + ".log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# ──────────────── 辅助函数 ────────────────

_NAV_FILTER = {
    "首页", "学校主页", "网站首页", "学院首页", "部门首页", "设为首页", "加入收藏",
    "登录", "注册", "English", "ENGLISH", "手机版", "网站地图", "返回首页", "主页",
    "Home", "HOME", "skip", "Skip", "菜单", "导航",
    "综合要闻", "科教在线", "工作动态", "通知公告", "学术动态", "媒体聚焦",
    "校园文化", "人才培养", "科学研究", "招生就业", "校园生活", "合作交流",
    "信息公开", "学校概况", "组织机构", "师资队伍", "学科建设", "教育教学",
    "媒体南航", "南航要闻", "菁菁校园", "理论学习", "服务指南", "信息门户",
}

_SKIP_TAGS = {
    "script", "style", "noscript", "iframe", "form", "nav", "header", "footer", "aside",
}

_SKIP_CLASSES = {
    ".ad", ".ads", ".advert", ".advertisement", ".popup", ".modal",
    ".recommend", ".related", ".share", ".breadcrumb", ".comment", ".comments",
}

def _make_headers(config=None):
    h = dict(HEADERS)
    h["User-Agent"] = random.choice(_USER_AGENTS)
    if config and config.get("headers"):
        h.update(config["headers"])
    return h


def fetch_url(url, config=None, allow_js=True):
    config = config or {}
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
            return resp.text, {"status_code": resp.status_code, "attempts": attempt + 1, "url": resp.url}
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                time.sleep((backoff ** attempt) + random.uniform(0.2, 0.8))
    raise Exception(f"请求失败: {last_error}")


def normalize_content(text):
    lines, seen = [], set()
    for raw in re.split(r"[\r\n]+", text or ""):
        line = re.sub(r"\s+", " ", html_unescape(raw)).strip()
        if len(line) < 2:
            continue
        key = hashlib.md5(line.encode()).hexdigest()
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


def parse_date(text):
    if not text:
        return None
    text = str(text).strip()
    for pat, df in [
        (r"(\d{4})-(\d{1,2})-(\d{1,2})", "%Y-%m-%d"),
        (r"(\d{4})年(\d{1,2})月(\d{1,2})日", "%Y-%m-%d"),
        (r"(\d{4})/(\d{1,2})/(\d{1,2})", "%Y/%m/%d"),
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

    id_pattern = config.get("news_id_pattern", r"(\d+)")
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
        raw_id = m.group(1) if (m and m.lastindex and m.lastindex >= 1) else href.replace("/", "_").strip("_")
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
        date_match = re.search(r"(\d{4}[-/年]\d{1,2}[-/月]\d{1,2})", parent_text)
        date_str = date_match.group(1) if date_match else ""
        if not date_str:
            date_match = re.search(r"(\d{2}-\d{2})", parent_text)
            if date_match:
                date_str = datetime.now().strftime("%Y-") + date_match.group(1)
        items.append({"news_id": news_id, "title": title[:500], "url": full_url, "date_str": date_str})
        if len(items) >= max_items:
            break
    return items


def parse_detail(html, config):
    soup = BeautifulSoup(html, "html.parser")
    detail = {"source": "", "author": "", "views": 0, "publish_date": "", "content": ""}
    for el in soup.select("script,style,noscript,iframe,form,nav,header,footer,aside"):
        el.decompose()

    # 标题
    for meta_name in ("ArticleTitle", "title"):
        meta = soup.find("meta", attrs={"name": meta_name})
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
        meta = soup.find("meta", attrs={"name": meta_name}) or soup.find("meta", attrs={"property": meta_name})
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
        meta = soup.find("meta", attrs={"name": meta_name}) or soup.find("meta", attrs={"property": meta_name})
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
        meta = soup.find("meta", attrs={"name": meta_name}) or soup.find("meta", attrs={"property": meta_name})
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
    text_only = re.sub(r"\s+", " ", text_only)
    for pat, key in [(r"来源[：:]\s*(\S+)", "source"), (r"作者[：:]\s*(\S+)", "author"),
                     (r"发布时间[：:]\s*(\S+)", "publish_date"), (r"时间[：:]\s*(\S+)", "publish_date")]:
        m = re.search(pat, text_only)
        if m and not detail.get(key):
            val = m.group(1).rstrip("作者来源供图时间浏览")
            detail[key] = val[:100] if key != "publish_date" else parse_date(val)
    if not detail.get("publish_date"):
        for pat in [r"(\d{4}-\d{1,2}-\d{1,2})", r"(\d{4}年\d{1,2}月\d{1,2}日)"]:
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
            if not article and cls == "article":
                article = soup.find("article")
            if article:
                content = clean_content_node(article, config)
                if content:
                    detail["content"] = content
                    break
    if not detail.get("content"):
        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p") if len(p.get_text(strip=True)) > 10]
        detail["content"] = normalize_content("\n".join(paragraphs))
    return detail

# ──────────────── 保存 ────────────────

def _content_digest(title, content):
    text = f"{title or ''}|{(content or '')[:2000]}"
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
                logger.error(f"  保存失败 [{news.get('news_id')}]: {e}")
    conn.commit()
    conn.close()
    if skipped:
        logger.info(f"  去重跳过: {skipped} 条")
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
    paragraphs = [p for p in (content or "").split("\n") if len(p.strip()) >= 20]
    if len(paragraphs) >= 3:
        score += 20
    elif len(paragraphs) >= 1:
        score += 10
    if detail and detail.get("publish_date"):
        score += 10
    chinese = sum(1 for c in content or "" if '\u4e00' <= c <= '\u9fff')
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
            page_url = urljoin(list_url, pagination.replace("{page}", str(page)))
        else:
            page_url = list_url if page == 1 else None
            if page_url is None:
                break

        logger.info(f"获取第{page}/{pages}页: {page_url}")
        try:
            html, meta = fetch_url(page_url, SCRAPER_CONFIG)
        except Exception as e:
            logger.error(f"列表页请求失败: {e}")
            continue

        items = parse_list(html, SCRAPER_CONFIG)
        logger.info(f"  第{page}页: {len(items)} 条链接")

        for idx, item in enumerate(items):
            time.sleep(delay + random.uniform(0, 0.3))
            try:
                detail_html, _ = fetch_url(item["url"], SCRAPER_CONFIG)
                detail = parse_detail(detail_html, SCRAPER_CONFIG)
            except Exception as e:
                logger.error(f"  详情页失败 [{item['url'][:60]}]: {e}")
                continue

            title = detail.get("title") or item["title"]
            content = detail.get("content", "")
            if not content or len(content) < 30:
                continue

            score = evaluate_quality(title, content, detail)
            if score < quality_threshold:
                logger.info(f"  [跳过] 质量评分{score}: {title[:40]}")
                continue

            all_news.append({
                "news_id": item["news_id"],
                "title": title,
                "category": classify_news(title, content),
                "publish_date": detail.get("publish_date") or parse_date(item.get("date_str", "")),
                "source": detail.get("source", ""),
                "author": detail.get("author", ""),
                "views": detail.get("views", 0),
                "content": normalize_content(content),
                "url": item["url"],
            })
            logger.info(f"  [✓] ({score}) {title[:50]}")

    saved = save_news_batch(all_news)
    logger.info(f"===== 完成: {saved}/{len(all_news)} 条入库 =====")
    return all_news


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=f"SITE_NAME 专属爬虫")
    parser.add_argument("-p", "--pages", type=int, default=None, help="爬取页数")
    args = parser.parse_args()
    crawl(pages=args.pages)
