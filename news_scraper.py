"""
南昌航空大学新闻爬虫 - 南航要闻 (https://www.nchu.edu.cn/xwzx/chyw)
爬取新闻列表+详情，分类存入MySQL数据库
"""

import requests
from bs4 import BeautifulSoup
import pymysql
import re
import time
from datetime import datetime
import logging

# ==================== 配置 ====================

DB_CONFIG = {
    "host": "localhost",
    "port": 3306,
    "user": "root",
    "password": "3.1415926tcy",
    "database": "nchu_news",
    "charset": "utf8mb4",
}

# 爬取范围：start_page 到 end_page（共619页，建议先少量测试）
START_PAGE = 1
END_PAGE = 3  # 测试用，正式爬取改为 619

# 请求间隔（秒），避免被封
REQUEST_DELAY = 2

LIST_URL = "https://www.nchu.edu.cn/xwzx/chyw"
BASE_URL = "https://www.nchu.edu.cn"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# ==================== 日志 ====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("scraper.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ==================== 分类规则 ====================

def classify_news(title, content=""):
    """根据标题和内容关键词进行新闻分类"""
    text = title + " " + (content[:500] if content else "")

    rules = [
        ("党建思政", ["树立和践行正确政绩观", "习近平", "党建", "思政", "党史", "红色", "井冈山", "求是", "政治"]),
        ("人物风采", ["微光成炬", "昌航温度", "榜样", "风采", "人物", "校友"]),
        ("荣誉获奖", ["喜报", "获奖", "荣获", "表彰", "佳绩", "冠军", "第一", "金牌"]),
        ("教育教学", ["深化特色年", "教学", "教育", "课程", "专业认证", "思政课", "立德树人", "培养"]),
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


# ==================== 数据库 ====================

def create_database():
    """创建数据库（如果不存在）"""
    conn = pymysql.connect(
        host=DB_CONFIG["host"],
        port=DB_CONFIG["port"],
        user=DB_CONFIG["user"],
        password=DB_CONFIG["password"],
        charset="utf8mb4",
    )
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE DATABASE IF NOT EXISTS `{DB_CONFIG['database']}` "
            "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
    conn.close()
    logger.info(f"数据库 {DB_CONFIG['database']} 已就绪")


def create_table():
    """创建新闻表"""
    conn = pymysql.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS news (
                id INT AUTO_INCREMENT PRIMARY KEY,
                news_id VARCHAR(20) UNIQUE COMMENT '新闻ID（content_xxxxxx）',
                title VARCHAR(500) NOT NULL COMMENT '新闻标题',
                category VARCHAR(50) NOT NULL DEFAULT '综合新闻' COMMENT '新闻分类',
                publish_date DATE COMMENT '发布日期',
                source VARCHAR(200) COMMENT '来源部门',
                views INT DEFAULT 0 COMMENT '浏览次数',
                content LONGTEXT COMMENT '新闻正文',
                url VARCHAR(500) COMMENT '新闻链接',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_category (category),
                INDEX idx_publish_date (publish_date),
                INDEX idx_news_id (news_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            COMMENT='南航要闻'
        """)
    conn.commit()
    conn.close()
    logger.info("数据表 news 已就绪")


def save_news(news_list):
    """批量插入/更新新闻到数据库"""
    if not news_list:
        return 0

    conn = pymysql.connect(**DB_CONFIG)
    sql = """
        INSERT INTO news (news_id, title, category, publish_date, source, views, content, url)
        VALUES (%(news_id)s, %(title)s, %(category)s, %(publish_date)s, %(source)s, %(views)s, %(content)s, %(url)s)
        ON DUPLICATE KEY UPDATE
            title = VALUES(title),
            category = VALUES(category),
            publish_date = VALUES(publish_date),
            source = VALUES(source),
            views = VALUES(views),
            content = VALUES(content),
            url = VALUES(url)
    """
    saved = 0
    with conn.cursor() as cur:
        for news in news_list:
            try:
                cur.execute(sql, news)
                saved += 1
            except Exception as e:
                logger.error(f"保存失败 [{news.get('news_id')}]: {e}")
    conn.commit()
    conn.close()
    logger.info(f"成功保存 {saved} 条新闻")
    return saved


# ==================== 爬取逻辑 ====================

def fetch_page(url):
    """获取页面HTML"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.encoding = "utf-8"
        if resp.status_code == 200:
            return resp.text
        else:
            logger.warning(f"请求失败 [{resp.status_code}]: {url}")
            return None
    except Exception as e:
        logger.error(f"请求异常: {url} - {e}")
        return None


def parse_list_page(html):
    """解析列表页，提取新闻链接和日期"""
    soup = BeautifulSoup(html, "html.parser")
    news_items = []

    # 查找 <a> 标签中的新闻链接
    for a in soup.find_all("a", href=re.compile(r"/xwzx/chyw/content_\d+")):
        href = a.get("href")
        if not isinstance(href, str):
            continue
        news_id = href.split("/")[-1]  # content_192396
        title = a.get_text(strip=True)
        if not title:
            continue

        # 尝试获取日期（在 <a> 前面的文本节点中）
        parent_text = a.parent.get_text(strip=True) if a.parent else ""
        date_match = re.search(r"(\d{4}-\d{2}-\d{2}|\d{2}-\d{2})", parent_text)
        date_str = date_match.group(1) if date_match else ""

        # 补全年份
        if date_str and len(date_str) == 5:  # MM-DD 格式
            date_str = f"2026-{date_str}"

        news_items.append({
            "news_id": news_id,
            "title": title,
            "date_str": date_str,
            "url": str(BASE_URL) + str(href),
        })

    return news_items


def parse_detail_page(html, url):
    """解析详情页，通过 meta 标签提取新闻元数据和正文"""
    soup = BeautifulSoup(html, "html.parser")
    detail = {
        "source": "",
        "views": 0,
        "publish_date": "",
        "content": "",
        "images": [],
    }

    # ----- 通过 meta 标签提取元数据（最可靠）-----
    meta_map = {
        "ArticleTitle": "title_meta",
        "PubDate": "pub_date_meta",
        "ContentSource": "source_meta",
    }

    for name, key in meta_map.items():
        meta = soup.find("meta", attrs={"name": name})
        if meta:
            content = meta.get("content")
            if content and isinstance(content, str):
                detail[key] = content.strip()

    # 来源部门
    detail["source"] = detail.get("source_meta", "")

    # 发布日期
    pub = detail.get("pub_date_meta", "")
    if pub:
        detail["publish_date"] = pub.split(" ")[0]

    # ----- 正文：优先从页面正文区域提取完整内容 -----
    article_area = soup.find("div", class_="v_news_content")
    if not article_area:
        article_area = soup.find("div", class_=re.compile(r"article|content|main"))
    if article_area:
        paragraphs = [p.get_text(strip=True) for p in article_area.find_all("p") if len(p.get_text(strip=True)) > 10]
        detail["content"] = "\n".join(paragraphs)

    # 如果正文区域提取失败，回退到所有 <p> 标签
    if not detail["content"]:
        paragraphs = [p.get_text(strip=True) for p in soup.find_all("p") if len(p.get_text(strip=True)) > 10]
        detail["content"] = "\n".join(paragraphs)

    # 最后兜底：使用 Description meta
    if not detail["content"]:
        desc_meta = soup.find("meta", attrs={"name": "Description"})
        if desc_meta:
            desc = desc_meta.get("content")
            if desc and isinstance(desc, str):
                from html import unescape
                desc = unescape(desc)
                desc_soup = BeautifulSoup(desc, "html.parser")
                detail["content"] = desc_soup.get_text(separator="\n").strip()

    # 验证是否是有效的详情页（有 ArticleTitle meta 才是真文章）
    if not detail.get("title_meta"):
        logger.warning(f"  该页面无 ArticleTitle meta，可能不是有效文章页: {url}")
        return None

    # ----- 文章中图片 -----
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if src and isinstance(src, str) and not src.endswith((".gif", ".ico")):
            if not src.startswith("http"):
                src = str(BASE_URL) + src
            detail["images"].append(src)

    return detail


def scrape_list_range():
    """爬取指定页码范围内的新闻列表"""
    all_news = []

    for page in range(START_PAGE, END_PAGE + 1):
        if page == 1:
            url = f"{LIST_URL}?page=1"
        else:
            url = f"{LIST_URL}?page={page}"

        logger.info(f"正在爬取列表: 第 {page} 页 - {url}")
        html = fetch_page(url)
        if not html:
            continue

        items = parse_list_page(html)
        logger.info(f"第 {page} 页共提取 {len(items)} 条新闻")

        # 去重（同一页可能重复）
        seen = set()
        for item in items:
            if item["news_id"] not in seen:
                seen.add(item["news_id"])
                all_news.append(item)

        time.sleep(REQUEST_DELAY)

    logger.info(f"共提取 {len(all_news)} 条新闻（去重后）")
    return all_news


def scrape_details(news_list):
    """爬取每条新闻的详情页"""
    results = []

    for i, news in enumerate(news_list, 1):
        logger.info(f"[{i}/{len(news_list)}] 正在爬取详情: {news['title'][:40]}...")
        html = fetch_page(news["url"])
        if not html:
            logger.warning(f"  请求失败，跳过: {news['url']}")
            continue

        detail = parse_detail_page(html, news["url"])
        if detail is None:
            logger.warning(f"  无效文章页，跳过: {news['title'][:40]}")
            continue

        # 合并信息
        news["source"] = detail.get("source", "")
        news["views"] = detail.get("views", 0)
        news["publish_date"] = detail.get("publish_date") or news.get("date_str", "")
        news["content"] = detail.get("content", "")

        # 分类
        news["category"] = classify_news(news["title"], news["content"])

        # 格式化日期
        if not news["publish_date"]:
            news["publish_date"] = None

        results.append(news)
        time.sleep(REQUEST_DELAY)

    logger.info(f"成功解析 {len(results)}/{len(news_list)} 条文章详情")
    return results


# ==================== 主流程 ====================

def main():
    logger.info("=" * 50)
    logger.info("南昌航空大学新闻爬虫启动")
    logger.info("=" * 50)

    # 1. 初始化数据库
    create_database()
    create_table()

    # 2. 爬取列表
    news_list = scrape_list_range()
    if not news_list:
        logger.warning("未爬取到任何新闻，请检查网络或网站状态")
        return

    # 3. 爬取详情
    full_news = scrape_details(news_list)

    # 4. 分类统计
    from collections import Counter
    cat_count = Counter(n["category"] for n in full_news)
    logger.info("分类统计: " + ", ".join(f"{k}:{v}" for k, v in cat_count.items()))

    # 5. 存入数据库
    save_news(full_news)

    logger.info("=" * 50)
    logger.info("爬取完成！")
    logger.info("=" * 50)


if __name__ == "__main__":
    main()
