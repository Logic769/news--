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
from datetime import datetime
from html import unescape as html_unescape
from config import DB_CONFIG, HEADERS, SCRAPER_DELAY, SCRAPER_DEFAULT_PAGES

# ==================== 配置 ====================

REQUEST_DELAY = SCRAPER_DELAY
DEFAULT_PAGES = SCRAPER_DEFAULT_PAGES

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("site_scanner.log", encoding="utf-8"), logging.StreamHandler()])
logger = logging.getLogger("site_scanner")


# ==================== 分类规则（与 news_scraper.py 保持一致）====================

def classify_news(title, content=""):
    text = title + " " + (content[:500] if content else "")
    rules = [
        ("党建思政", ["习近平","党建","思政","党史","红色","井冈山","求是","政治"]),
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


# ==================== 日期解析 ====================

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

def save_news_batch(news_list):
    if not news_list:
        return 0
    conn = pymysql.connect(**DB_CONFIG, cursorclass=DictCursor)
    sql = """
        INSERT INTO news (news_id, title, category, publish_date, source, views, content, url)
        VALUES (%(news_id)s, %(title)s, %(category)s, %(publish_date)s, %(source)s, %(views)s, %(content)s, %(url)s)
        ON DUPLICATE KEY UPDATE
            title = VALUES(title), category = VALUES(category),
            publish_date = VALUES(publish_date), source = VALUES(source),
            views = VALUES(views), content = VALUES(content), url = VALUES(url)
    """
    saved = 0
    with conn.cursor() as cur:
        for news in news_list:
            try:
                cur.execute(sql, news)
                saved += 1
            except Exception as e:
                logger.error(f"  保存失败 [{news.get('news_id')}]: {e}")
    conn.commit()
    conn.close()
    return saved


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
        # 回退: 尝试作为标签名
        tag_name = selector.split("[")[0].strip() if "[" in selector else selector
        links = soup.find_all(tag_name)

    id_pattern = config.get("news_id_pattern", r"(\d+)")
    base_url = config.get("base_url", "").rstrip("/")

    for link in links:
        href = link.get("href", "")
        if not isinstance(href, str) or not href:
            continue

        # 提取 news_id (限制20字符，用hash确保唯一)
        m = re.search(id_pattern, href)
        raw_id = m.group(1) if m else href.replace("/", "_").strip("_")
        news_id = "s" + hashlib.md5(raw_id.encode()).hexdigest()[:19]

        title = link.get_text(strip=True)
        if not title or len(title) < 4:
            continue

        # 完整 URL
        if href.startswith("http"):
            full_url = href
        elif href.startswith("/"):
            full_url = base_url + href
        else:
            full_url = base_url + "/" + href.lstrip("/")

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
    detail = {"source": "", "views": 0, "publish_date": "", "content": ""}

    meta_source = config.get("detail_meta_source", "auto")

    # ---- 标题提取 ----
    if meta_source in ("meta", "auto"):
        for meta_name in ("ArticleTitle", "title", "description"):
            meta = soup.find("meta", attrs={"name": meta_name})
            if meta:
                c = meta.get("content")
                if c and isinstance(c, str) and len(c.strip()) > 4:
                    detail["title"] = c.strip()
                    break
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
        for meta_name in ("ContentSource", "source", "author"):
            meta = soup.find("meta", attrs={"name": meta_name})
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

    # ---- 正文提取 ----
    content_sel = config.get("detail_content_selector", "")
    if content_sel:
        article = soup.select_one(content_sel)
        if article:
            paragraphs = [p.get_text(strip=True) for p in article.find_all("p") if len(p.get_text(strip=True)) > 10]
            detail["content"] = "\n".join(paragraphs)

    # 回退：自动寻找正文区域
    if not detail.get("content"):
        for cls in ("v_news_content", "article-content", "article_content", "content", "article", "main"):
            article = soup.find("div", class_=re.compile(cls))
            if article:
                paragraphs = [p.get_text(strip=True) for p in article.find_all("p") if len(p.get_text(strip=True)) > 10]
                if paragraphs:
                    detail["content"] = "\n".join(paragraphs)
                    break

    # 最终回退：所有 <p> 标签
    if not detail.get("content"):
        paragraphs = [p.get_text(strip=True) for p in soup.find_all("p") if len(p.get_text(strip=True)) > 10]
        detail["content"] = "\n".join(paragraphs)

    return detail


# ==================== 单站点扫描 ====================

def scan_site(site, pages=DEFAULT_PAGES):
    """
    扫描单个监测站点。
    site: 数据库行字典, 必须含 scraper_config (JSON 字符串)
    """
    config = {}
    if site.get("scraper_config"):
        try:
            config = json.loads(site["scraper_config"])
        except json.JSONDecodeError:
            logger.warning(f"  [{site['name']}] scraper_config JSON 解析失败")
            return 0

    list_url = config.get("list_url")
    if not list_url:
        list_url = f"https://{site['url']}" if site.get("url") else None
    if not list_url:
        logger.warning(f"  [{site['name']}] 缺少 list_url")
        return 0

    logger.info(f"--- 开始扫描: {site['name']} ({list_url}) ---")
    all_news = []

    pagination = config.get("list_pagination", "")
    encoding = config.get("encoding", "utf-8")

    for page in range(1, pages + 1):
        if pagination:
            page_url = list_url.rstrip("/") + pagination.replace("{page}", str(page))
        else:
            page_url = list_url

        logger.info(f"  列表页 [{page}/{pages}]: {page_url}")
        try:
            resp = requests.get(page_url, headers=HEADERS, timeout=30)
            resp.encoding = encoding
            if resp.status_code != 200:
                logger.warning(f"    请求失败 [{resp.status_code}]")
                continue
        except Exception as e:
            logger.error(f"    请求异常: {e}")
            continue

        items = parse_list(resp.text, config)
        logger.info(f"    发现 {len(items)} 条新闻链接")

        for item in items:
            time.sleep(REQUEST_DELAY)
            try:
                detail_resp = requests.get(item["url"], headers=HEADERS, timeout=30)
                detail_resp.encoding = encoding

                d = parse_detail(detail_resp.text, config)

                # 标题优先取正文页提取的
                title = d.get("title") or item["title"]
                category = classify_news(title, d.get("content", ""))

                all_news.append({
                    "news_id": item["news_id"],
                    "title": title,
                    "category": category,
                    "publish_date": d.get("publish_date") or parse_date(item.get("date_str", "")),
                    "source": d.get("source", ""),
                    "views": d.get("views", 0),
                    "content": d.get("content", ""),
                    "url": item["url"],
                })
            except Exception as e:
                logger.error(f"    详情页失败 [{item['url']}]: {e}")
                continue

    # 批量入库
    saved = save_news_batch(all_news)
    logger.info(f"  入库: {saved} 条 (共解析 {len(all_news)} 条)")

    # 更新 last_checked
    conn = pymysql.connect(**DB_CONFIG, cursorclass=DictCursor)
    with conn.cursor() as cur:
        cur.execute("UPDATE monitor_sites SET last_checked=NOW() WHERE id=%s", [site["id"]])
    conn.commit()
    conn.close()

    return saved


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

    for site in sites:
        config = site.get("scraper_config", "")
        if not config:
            logger.warning(f"跳过 [{site['name']}]: 无 scraper_config")
            fail += 1
            continue
        try:
            saved = scan_site(site, pages=pages)
            total += saved
        except Exception as e:
            logger.error(f"站点 [{site['name']}] 扫描失败: {e}")
            fail += 1

    duration = int(time.time() - start_time)

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
    "nchu": {
        "name": "南昌航空大学",
        "list_url": "https://www.nchu.edu.cn/xwzx/chyw",
        "list_pagination": "?page={page}",
        "list_item_selector": "a[href*='content_']",
        "detail_meta_source": "meta",
        "detail_content_selector": "div.v_news_content",
        "date_format": "meta",
        "encoding": "utf-8",
        "news_id_pattern": "content_\\d+",
        "base_url": "https://www.nchu.edu.cn",
    },
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


# ==================== 入口 ====================

if __name__ == "__main__":
    import sys
    pages = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PAGES
    scan_all_sites(pages=pages)
