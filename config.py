"""
项目统一配置文件
所有敏感信息优先从环境变量读取，支持开箱即用部署
可通过以下方式配置（优先级从高到低）：
  1. 系统环境变量（如 export MYSQL_HOST=xxx）
  2. 项目根目录 .env 文件
  3. config.py 中的默认值
"""

import os

# 尝试加载 .env 文件
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # 手动加载 .env
    def _load_dotenv():
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        if os.path.exists(env_path):
            with open(env_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip().strip('"').strip("'")
                        if k not in os.environ:
                            os.environ[k] = v
    _load_dotenv()

# ==================== MySQL 数据库 ====================

DB_CONFIG = {
    "host": os.environ.get("MYSQL_HOST", "localhost"),
    "port": int(os.environ.get("MYSQL_PORT", "3306")),
    "user": os.environ.get("MYSQL_USER", "root"),
    "password": os.environ.get("MYSQL_PASSWORD", "root"),
    "database": os.environ.get("MYSQL_DATABASE", "nchu_news"),
    "charset": "utf8mb4",
}

# ==================== AI 大模型（OpenAI / DeepSeek 兼容接口）====================

AI_CONFIG = {
    "api_url": os.environ.get("AI_API_URL", "https://api.deepseek.com/chat/completions"),
    "api_key": os.environ.get("AI_API_KEY", ""),          # 不填则使用规则引擎
    "model": os.environ.get("AI_MODEL", "deepseek-chat"),
    "max_tokens": int(os.environ.get("AI_MAX_TOKENS", "4096")),
    "temperature": float(os.environ.get("AI_TEMPERATURE", "0.3")),
}

# ==================== Flask ====================

SECRET_KEY = os.environ.get("SECRET_KEY", "nchu-news-secret-key-2026")

# ==================== 管理员默认账号 ====================

DEFAULT_ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
DEFAULT_ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin123")
DEFAULT_ADMIN_NAME = os.environ.get("ADMIN_NAME", "管理员")

# ==================== 爬虫通用配置 ====================

SCRAPER_DELAY = int(os.environ.get("SCRAPER_DELAY", "2"))      # 请求间隔秒
SCRAPER_DEFAULT_PAGES = int(os.environ.get("SCRAPER_PAGES", "3"))  # 默认爬取页数

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def print_config():
    """打印当前配置（隐藏密码）"""
    safe_db = {**DB_CONFIG, "password": "***"}
    safe_ai = {**AI_CONFIG, "api_key": "***" if AI_CONFIG["api_key"] else "(未设置)"}
    print(f"MySQL : {safe_db['user']}@{safe_db['host']}:{safe_db['port']}/{safe_db['database']}")
    print(f"AI API: {safe_ai['api_url']} | 模型: {safe_ai['model']} | Key: {safe_ai['api_key']}")
    print(f"管理员: {DEFAULT_ADMIN_USER} / {DEFAULT_ADMIN_PASS}")
