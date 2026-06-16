"""
南昌航空大学新闻系统 - Flask 后端
新闻展示 + 用户认证 + 个人中心 + 系统管理 + 智能推送
"""

from flask import Flask, jsonify, request, render_template, session
from flask_cors import CORS
from functools import wraps
from bs4 import BeautifulSoup
import pymysql
import hashlib
import json
import re
import os
from datetime import datetime, date

app = Flask(__name__, template_folder="templates")
app.secret_key = "nchu-news-secret-key-2026"
CORS(app, supports_credentials=True)

# ==================== 数据库配置 ====================

DB_CONFIG = {
    "host": "localhost",
    "port": 3306,
    "user": "root",
    "password": "3.1415926tcy",
    "database": "nchu_news",
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
}


def get_db():
    return pymysql.connect(**DB_CONFIG)


# ==================== 辅助工具 ====================

def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def login_required(f):
    """登录验证装饰器"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"code": 401, "msg": "请先登录"}), 401
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    """管理员权限验证装饰器"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"code": 401, "msg": "请先登录"}), 401
        if session.get("role") != "admin":
            return jsonify({"code": 403, "msg": "需要管理员权限"}), 403
        return f(*args, **kwargs)
    return wrapper


def date_fmt(obj):
    """将字典中的 date/datetime 转为字符串"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (date, datetime)):
                obj[k] = str(v)
    elif isinstance(obj, list):
        for item in obj:
            date_fmt(item)
    return obj


# ==================== 页面路由 ====================

@app.route("/")
def index():
    return render_template("index.html")


# ==================== 认证 API ====================

@app.route("/api/auth/login", methods=["POST"])
def api_login():
    """用户登录"""
    try:
        data = request.get_json()
        username = data.get("username", "").strip()
        password = data.get("password", "")

        if not username or not password:
            return jsonify({"code": -1, "msg": "用户名和密码不能为空"})

        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, display_name, department, role, avatar_text, status "
                "FROM users WHERE username=%s AND password_hash=%s",
                [username, sha256(password)]
            )
            user = cur.fetchone()
        conn.close()

        if not user:
            return jsonify({"code": -1, "msg": "用户名或密码错误"})

        if user["status"] != "active":
            return jsonify({"code": -1, "msg": "账号已被禁用"})

        # 写入 session
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session["display_name"] = user["display_name"]
        session["role"] = user["role"]
        session["department"] = user["department"]

        return jsonify({
            "code": 0,
            "data": {
                "id": user["id"],
                "username": user["username"],
                "display_name": user["display_name"],
                "department": user["department"],
                "role": user["role"],
                "avatar_text": user["avatar_text"],
            },
        })
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"code": 0, "msg": "已退出登录"})


@app.route("/api/auth/current", methods=["GET"])
def api_current_user():
    """获取当前登录用户信息"""
    if not session.get("user_id"):
        return jsonify({"code": -1, "msg": "未登录"})
    return jsonify({
        "code": 0,
        "data": {
            "id": session["user_id"],
            "username": session["username"],
            "display_name": session["display_name"],
            "department": session.get("department"),
            "role": session["role"],
        },
    })


# ==================== 新闻 API ====================

@app.route("/api/stats")
def api_stats():
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM news")
            total = cur.fetchone()["total"]
            cur.execute("SELECT COUNT(*) AS today FROM news WHERE DATE(created_at) = CURDATE()")
            today = cur.fetchone()["today"]
            cur.execute("SELECT category, COUNT(*) AS count FROM news GROUP BY category ORDER BY count DESC")
            categories = cur.fetchall()
            # 监测站点统计
            cur.execute("SELECT COUNT(*) AS total FROM monitor_sites WHERE status='running'")
            site_count = cur.fetchone()["total"]
            cur.execute("SELECT COUNT(*) AS total_sites FROM monitor_sites")
            total_sites = cur.fetchone()["total_sites"]
            # 采集状态
            cur.execute("SELECT * FROM collection_logs ORDER BY id DESC LIMIT 1")
            last_collection = cur.fetchone()
            # 今日高匹配新闻数（对于当前用户偏好的匹配）
            cur.execute("SELECT COUNT(*) AS total FROM reading_history WHERE DATE(read_at)=CURDATE()")
            today_reads = cur.fetchone()["total"]
        conn.close()

        return jsonify({"code": 0, "data": {
            "total": total, "today": today, "categories": categories,
            "total_sites": total_sites, "site_count": site_count,
            "last_collection": date_fmt(last_collection) if last_collection else None,
            "today_reads": today_reads,
        }})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


@app.route("/api/news")
def api_news_list():
    try:
        page = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", 20))
        category = request.args.get("category", "")
        keyword = request.args.get("keyword", "")
        offset = (page - 1) * per_page

        conn = get_db()
        with conn.cursor() as cur:
            where = []
            params = []
            if category:
                where.append("category = %s")
                params.append(category)
            if keyword:
                where.append("(title LIKE %s OR content LIKE %s)")
                kw = f"%{keyword}%"
                params.extend([kw, kw])

            where_clause = " WHERE " + " AND ".join(where) if where else ""

            cur.execute(f"SELECT COUNT(*) AS total FROM news{where_clause}", params)
            total = cur.fetchone()["total"]

            cur.execute(
                f"""SELECT news_id, title, category, publish_date, source, views, url,
                    LEFT(content, 200) AS summary FROM news{where_clause}
                    ORDER BY publish_date DESC LIMIT %s OFFSET %s""",
                params + [per_page, offset]
            )
            items = cur.fetchall()
        conn.close()

        for item in items:
            if item["publish_date"]:
                item["publish_date"] = str(item["publish_date"])

        return jsonify({"code": 0, "data": {
            "items": items, "total": total, "page": page, "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page if total > 0 else 0,
        }})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


@app.route("/api/news/<news_id>")
def api_news_detail(news_id):
    try:
        conn = get_db()
        with conn.cursor() as cur:
            # 实时递增浏览量
            cur.execute("UPDATE news SET views = views + 1 WHERE news_id = %s", [news_id])
            cur.execute("SELECT * FROM news WHERE news_id = %s", [news_id])
            item = cur.fetchone()
        conn.commit()
        conn.close()
        if not item:
            return jsonify({"code": -1, "msg": "新闻不存在"}), 404
        date_fmt(item)
        # 确保 ai_summarized_at 也转为字符串
        if item.get("ai_summarized_at"):
            item["ai_summarized_at"] = str(item["ai_summarized_at"])
        return jsonify({"code": 0, "data": item})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


@app.route("/api/categories")
def api_categories():
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT category, COUNT(*) AS count FROM news GROUP BY category ORDER BY count DESC")
            categories = cur.fetchall()
        conn.close()
        return jsonify({"code": 0, "data": categories})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


# ==================== 阅读记录 API ====================

@app.route("/api/user/reading-history", methods=["POST"])
@login_required
def api_record_read():
    """记录阅读历史"""
    try:
        data = request.get_json()
        news_id = data.get("news_id")
        if not news_id:
            return jsonify({"code": -1, "msg": "缺少 news_id"})

        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO reading_history (user_id, news_id) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE read_at = CURRENT_TIMESTAMP",
                [session["user_id"], news_id]
            )
        conn.commit()
        conn.close()
        return jsonify({"code": 0, "msg": "已记录"})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


# ==================== 用户个人中心 API ====================

@app.route("/api/user/profile", methods=["GET"])
@login_required
def api_user_profile():
    """获取用户个人信息与统计数据"""
    try:
        user_id = session["user_id"]
        conn = get_db()
        with conn.cursor() as cur:
            # 用户基本信息
            cur.execute(
                "SELECT id, username, display_name, department, role, avatar_text, status FROM users WHERE id=%s",
                [user_id]
            )
            user = cur.fetchone()

            # 阅读统计
            cur.execute(
                "SELECT COUNT(*) AS total_reads FROM reading_history WHERE user_id=%s", [user_id]
            )
            total_reads = cur.fetchone()["total_reads"]

            cur.execute(
                "SELECT COUNT(*) AS month_reads FROM reading_history WHERE user_id=%s AND MONTH(read_at)=MONTH(CURDATE())",
                [user_id]
            )
            month_reads = cur.fetchone()["month_reads"]

            cur.execute(
                "SELECT COUNT(*) AS favorites FROM reading_history WHERE user_id=%s", [user_id]
            )
            favorites = cur.fetchone()["favorites"]

            # 用户偏好
            cur.execute("SELECT * FROM user_preferences WHERE user_id=%s", [user_id])
            prefs = cur.fetchone()

            # 未读通知数
            cur.execute(
                "SELECT COUNT(*) AS unread FROM notifications WHERE user_id=%s AND is_read=0",
                [user_id]
            )
            unread = cur.fetchone()["unread"]

        conn.close()

        return jsonify({"code": 0, "data": {
            "user": date_fmt(user),
            "stats": {"total_reads": total_reads, "month_reads": month_reads, "favorites": favorites},
            "preferences": date_fmt(prefs) if prefs else None,
            "unread_notifications": unread,
        }})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


@app.route("/api/user/preferences", methods=["PUT"])
@login_required
def api_update_preferences():
    """更新用户偏好设置"""
    try:
        data = request.get_json()
        user_id = session["user_id"]
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO user_preferences (user_id, preferred_categories, preferred_keywords, push_enabled, daily_digest, deadline_reminder)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE
                   preferred_categories=VALUES(preferred_categories),
                   preferred_keywords=VALUES(preferred_keywords),
                   push_enabled=VALUES(push_enabled),
                   daily_digest=VALUES(daily_digest),
                   deadline_reminder=VALUES(deadline_reminder)""",
                [
                    user_id,
                    data.get("preferred_categories", ""),
                    data.get("preferred_keywords", ""),
                    data.get("push_enabled", 1),
                    data.get("daily_digest", 1),
                    data.get("deadline_reminder", 1),
                ]
            )
        conn.commit()
        conn.close()
        return jsonify({"code": 0, "msg": "偏好设置已更新"})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


# ==================== 通知 API ====================

@app.route("/api/user/notifications", methods=["GET"])
@login_required
def api_notifications():
    """获取用户通知列表"""
    try:
        user_id = session["user_id"]
        limit = int(request.args.get("limit", 10))
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, news_id, title, message, category, push_type, is_read, created_at
                   FROM notifications WHERE user_id=%s
                   ORDER BY created_at DESC LIMIT %s""",
                [user_id, limit]
            )
            items = cur.fetchall()
            # 未读数
            cur.execute("SELECT COUNT(*) AS unread FROM notifications WHERE user_id=%s AND is_read=0", [user_id])
            unread = cur.fetchone()["unread"]
        conn.close()

        for item in items:
            date_fmt(item)
            item["created_at_ago"] = time_ago(item["created_at"])

        return jsonify({"code": 0, "data": {"items": items, "unread": unread}})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


@app.route("/api/user/notifications/<int:notif_id>/read", methods=["PUT"])
@login_required
def api_mark_read(notif_id):
    """标记单条通知已读"""
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE notifications SET is_read=1 WHERE id=%s AND user_id=%s",
                [notif_id, session["user_id"]]
            )
        conn.commit()
        conn.close()
        return jsonify({"code": 0})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


@app.route("/api/user/notifications/read-all", methods=["PUT"])
@login_required
def api_mark_all_read():
    """标记全部通知已读"""
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE notifications SET is_read=1 WHERE user_id=%s AND is_read=0",
                [session["user_id"]]
            )
        conn.commit()
        conn.close()
        return jsonify({"code": 0})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


# ==================== 个性化推荐 API ====================

@app.route("/api/user/recommended", methods=["GET"])
@login_required
def api_recommended():
    """根据用户偏好推荐新闻"""
    try:
        user_id = session["user_id"]
        conn = get_db()
        with conn.cursor() as cur:
            # 获取用户偏好
            cur.execute("SELECT * FROM user_preferences WHERE user_id=%s", [user_id])
            prefs = cur.fetchone()

            if not prefs or not prefs.get("preferred_categories"):
                # 无偏好，返回最新新闻
                cur.execute(
                    """SELECT news_id, title, category, publish_date, source, views, url,
                        LEFT(content, 200) AS summary FROM news
                        ORDER BY publish_date DESC LIMIT 8"""
                )
                items = cur.fetchall()
            else:
                cats = prefs["preferred_categories"].split(",")
                placeholders = ",".join(["%s"] * len(cats))
                cur.execute(
                    f"""SELECT news_id, title, category, publish_date, source, views, url,
                        LEFT(content, 200) AS summary FROM news
                        WHERE category IN ({placeholders})
                        ORDER BY publish_date DESC LIMIT 8""",
                    cats
                )
                items = cur.fetchall()

        conn.close()
        for item in items:
            if item.get("publish_date"):
                item["publish_date"] = str(item["publish_date"])

        return jsonify({"code": 0, "data": items})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


# ==================== 管理员 API ====================

@app.route("/api/admin/stats", methods=["GET"])
@admin_required
def api_admin_stats():
    """系统管理统计数据"""
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM news")
            total_news = cur.fetchone()["total"]

            cur.execute("SELECT COUNT(*) AS total FROM users")
            total_all = cur.fetchone()["total"]

            cur.execute("SELECT COUNT(*) AS total FROM users WHERE role='admin'")
            total_admins = cur.fetchone()["total"]

            cur.execute("SELECT COUNT(*) AS total FROM users WHERE role='user'")
            total_users = cur.fetchone()["total"]

            cur.execute("SELECT COUNT(*) AS total FROM notifications")
            total_notifications = cur.fetchone()["total"]

            cur.execute("SELECT COUNT(*) AS total_sites FROM monitor_sites")
            total_sites = cur.fetchone()["total_sites"]

            cur.execute("SELECT COUNT(*) AS active_sites FROM monitor_sites WHERE status='running'")
            active_sites = cur.fetchone()["active_sites"]

            cur.execute(
                "SELECT COUNT(*) AS today FROM news WHERE publish_date = CURDATE()"
            )
            today_news = cur.fetchone()["today"]

            cur.execute("SELECT created_at FROM push_logs ORDER BY created_at DESC LIMIT 1")
            last_push = cur.fetchone()

        conn.close()
        return jsonify({"code": 0, "data": {
            "total_news": total_news,
            "total_all_users": total_all,
            "total_users": total_users,
            "total_admins": total_admins,
            "total_notifications": total_notifications,
            "total_sites": total_sites,
            "active_sites": active_sites,
            "today_news": today_news,
            "last_push": str(last_push["created_at"]) if last_push else "暂无",
        }})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


@app.route("/api/admin/users", methods=["GET"])
@admin_required
def api_admin_users():
    """用户列表"""
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT u.*, p.push_enabled, p.preferred_categories "
                "FROM users u LEFT JOIN user_preferences p ON u.id=p.user_id ORDER BY u.id"
            )
            users = cur.fetchall()
        conn.close()
        for u in users:
            date_fmt(u)
            u["password_hash"] = "***"
        return jsonify({"code": 0, "data": users})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


@app.route("/api/admin/users", methods=["POST"])
@admin_required
def api_create_user():
    """创建用户"""
    try:
        data = request.get_json()
        username = data.get("username", "").strip()
        password = data.get("password", "")
        display_name = data.get("display_name", username)
        department = data.get("department", "")
        role = data.get("role", "user")

        if not username or not password:
            return jsonify({"code": -1, "msg": "用户名和密码不能为空"})

        avatar_text = display_name[0] if display_name else username[0]

        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, password_hash, display_name, department, role, avatar_text) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                [username, sha256(password), display_name, department, role, avatar_text]
            )
            user_id = cur.lastrowid
            # 默认偏好
            cur.execute(
                "INSERT INTO user_preferences (user_id) VALUES (%s)", [user_id]
            )
        conn.commit()
        conn.close()
        return jsonify({"code": 0, "msg": "用户创建成功"})
    except pymysql.err.IntegrityError:
        return jsonify({"code": -1, "msg": "用户名已存在"})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


@app.route("/api/admin/users/<int:user_id>/status", methods=["PUT"])
@admin_required
def api_toggle_user_status(user_id):
    """切换用户状态"""
    try:
        data = request.get_json()
        status = data.get("status", "active")
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET status=%s WHERE id=%s", [status, user_id])
        conn.commit()
        conn.close()
        return jsonify({"code": 0})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


# ==================== 监测站点管理 API ====================

@app.route("/api/admin/sites", methods=["GET"])
@admin_required
def api_admin_sites():
    """获取监测站点列表"""
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM monitor_sites ORDER BY id ASC")
            sites = cur.fetchall()
        conn.close()
        for s in sites:
            date_fmt(s)
        return jsonify({"code": 0, "data": sites})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


@app.route("/api/admin/sites", methods=["POST"])
@admin_required
def api_create_site():
    """添加监测站点"""
    try:
        data = request.get_json()
        name = data.get("name", "").strip()
        url = data.get("url", "").strip()
        freq = data.get("frequency", "每日2次")
        config = data.get("scraper_config", "")
        if not name:
            return jsonify({"code": -1, "msg": "站点名称不能为空"})
        if isinstance(config, dict):
            config = json.dumps(config, ensure_ascii=False)
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO monitor_sites (name, url, frequency, scraper_config) VALUES (%s,%s,%s,%s)",
                [name, url, freq, config]
            )
        conn.commit()
        conn.close()
        return jsonify({"code": 0, "msg": "站点添加成功"})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


@app.route("/api/admin/sites/<int:site_id>", methods=["PUT"])
@admin_required
def api_update_site(site_id):
    """更新监测站点"""
    try:
        data = request.get_json()
        config = data.get("scraper_config")
        if isinstance(config, dict):
            config = json.dumps(config, ensure_ascii=False)
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE monitor_sites SET name=%s, url=%s, frequency=%s, status=%s, scraper_config=%s WHERE id=%s",
                [data.get("name"), data.get("url"), data.get("frequency"),
                 data.get("status", "running"), config, site_id]
            )
        conn.commit()
        conn.close()
        return jsonify({"code": 0})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


@app.route("/api/admin/sites/<int:site_id>", methods=["DELETE"])
@admin_required
def api_delete_site(site_id):
    """删除监测站点"""
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM monitor_sites WHERE id=%s", [site_id])
        conn.commit()
        conn.close()
        return jsonify({"code": 0})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


@app.route("/api/admin/sites/<int:site_id>/status", methods=["PUT"])
@admin_required
def api_toggle_site_status(site_id):
    """切换站点启停"""
    try:
        data = request.get_json()
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE monitor_sites SET status=%s WHERE id=%s",
                [data.get("status", "running"), site_id]
            )
        conn.commit()
        conn.close()
        return jsonify({"code": 0})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


@app.route("/api/admin/collection-status", methods=["GET"])
@admin_required
def api_collection_status():
    """采集运行状态"""
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS running FROM monitor_sites WHERE status='running'")
            running = cur.fetchone()["running"]
            cur.execute("SELECT COUNT(*) AS total FROM monitor_sites")
            total_sites = cur.fetchone()["total"]
            cur.execute("SELECT * FROM collection_logs ORDER BY id DESC LIMIT 1")
            latest = cur.fetchone()
            cur.execute("SELECT COUNT(*) AS today_collects FROM collection_logs WHERE DATE(created_at)=CURDATE()")
            today_collects = cur.fetchone()["today_collects"]
            cur.execute("SELECT COUNT(*) AS today_news FROM news WHERE DATE(created_at)=CURDATE()")
            today_news = cur.fetchone()["today_news"]
        conn.close()
        return jsonify({"code": 0, "data": {
            "running": running,
            "total_sites": total_sites,
            "latest": date_fmt(latest) if latest else None,
            "today_collects": today_collects,
            "today_news": today_news,
        }})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


# ==================== 全站扫描 API ====================

@app.route("/api/admin/scan-all", methods=["POST"])
@admin_required
def api_scan_all():
    """触发全站扫描（后台执行）"""
    try:
        data = request.get_json() or {}
        pages = max(1, min(data.get("pages", 3), 10))
        import threading, site_scanner
        t = threading.Thread(target=site_scanner.scan_all_sites, kwargs={"pages": pages, "status_filter": "running"}, daemon=True)
        t.start()
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM monitor_sites WHERE status='running'")
            count = cur.fetchone()["c"]
        conn.close()
        return jsonify({"code": 0, "msg": f"全站扫描已启动 ({count} 个站点, 每站 {pages} 页)", "sites": count, "pages": pages})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


@app.route("/api/admin/scan-site/<int:site_id>", methods=["POST"])
@admin_required
def api_scan_site(site_id):
    """触发单个站点扫描（后台执行）"""
    try:
        data = request.get_json() or {}
        pages = max(1, min(data.get("pages", 3), 20))
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM monitor_sites WHERE id=%s", [site_id])
            site = cur.fetchone()
        conn.close()
        if not site:
            return jsonify({"code": -1, "msg": "站点不存在"}), 404
        if not site.get("scraper_config"):
            return jsonify({"code": -1, "msg": "该站点未配置爬虫规则"}), 400

        import threading, site_scanner
        t = threading.Thread(target=site_scanner.scan_site, kwargs={"site": site, "pages": pages}, daemon=True)
        t.start()
        return jsonify({"code": 0, "msg": f"站点 [{site['name']}] 扫描已启动 ({pages} 页)", "site_id": site_id, "pages": pages})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


@app.route("/api/admin/scan-logs", methods=["GET"])
@admin_required
def api_scan_logs():
    """获取最近采集日志"""
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM collection_logs ORDER BY id DESC LIMIT 20")
            logs = cur.fetchall()
        conn.close()
        return jsonify({"code": 0, "data": logs})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


# ==================== AI 智能总结 API ====================

# -- AI 配置：支持 OpenAI / DeepSeek 兼容接口 -----
AI_CONFIG = {
    "api_url": "https://api.deepseek.com/chat/completions",
    "api_key": "sk-64b6673f5080428a97bf7e069af643f5",
    "model": "deepseek-chat",
    "max_tokens": 2000,
    "temperature": 0.3,  # 低温度保证客观性
}

if os.environ.get("AI_API_KEY"):
    AI_CONFIG["api_key"] = os.environ["AI_API_KEY"]


def _call_ai_api(prompt):
    """调用大模型 API 生成总结（失败时自动回退到规则提取）"""
    if not AI_CONFIG["api_key"]:
        return None
    try:
        import urllib.request as _urllib
        payload = json.dumps({
            "model": AI_CONFIG["model"],
            "messages": [
                {"role": "system", "content": "你是一个专业新闻编辑，请客观、完整地总结以下新闻内容。"},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": AI_CONFIG["max_tokens"],
            "temperature": AI_CONFIG["temperature"],
        }).encode("utf-8")
        req = _urllib.Request(AI_CONFIG["api_url"], data=payload, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {AI_CONFIG['api_key']}",
        })
        with _urllib.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode())
            return result["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[AI] API 调用失败，回退规则提取: {e}")
        return None


def _rule_based_summarize(_title, content):
    """规则引擎：从新闻正文中智能提取摘要和关键信息（无需 AI API）"""
    import re as _re

    text = (content or "").strip()
    if not text:
        return None, None

    # ---- 1. 生成摘要（300字以内）----
    # 取正文前300字作为摘要基础
    summary = text[:300]
    if len(text) > 300:
        # 尝试在句号处截断
        last_period = summary.rfind("。")
        if last_period > 150:
            summary = summary[:last_period + 1]

    # ---- 2. 提取关键信息点 ----
    keypoints = []

    # 时间提取
    time_patterns = [
        r"(\d{4}年\d{1,2}月\d{1,2}日)", r"(\d{1,2}月\d{1,2}日至\d{1,2}月\d{1,2}日)",
        r"(\d{4}-\d{2}-\d{2})", r"(\d{4}年)", r"(近日|日前|当前|目前)",
    ]
    for pat in time_patterns:
        m = _re.search(pat, text)
        if m and m.group(1) not in keypoints:
            keypoints.append(f"时间：{m.group(1)}")
            break

    # 人物提取
    person_patterns = [
        r"([\u4e00-\u9fa5]{2,4}(?:教授|院士|书记|院长|处长|主任|局长|部长|总经理|董事长|同学|老师|专家|学者))",
        r"学校([\u4e00-\u9fa5]{2,4}(?:书记|校长))",
    ]
    persons = []
    for pat in person_patterns:
        for m in _re.finditer(pat, text):
            p = m.group(1)
            if p not in persons:
                persons.append(p)
    if persons:
        keypoints.append(f"人物：{'、'.join(persons[:5])}")

    # 地点/机构提取
    org_patterns = [
        r"([\u4e00-\u9fa5]+(?:大学|学院|实验室|中心|研究所|研究院|部|委|局|处|公司))",
        r"([\u4e00-\u9fa5]+(?:校区|基地|园区))",
    ]
    orgs = []
    for pat in org_patterns:
        for m in _re.finditer(pat, text):
            o = m.group(1)
            if o not in orgs:
                orgs.append(o)
    if orgs:
        keypoints.append(f"机构：{'、'.join(orgs[:5])}")

    # 事件关键词
    event_kw = {
        "合作": r"(合作|签约|协议|共建|联合|战略合作)",
        "访问": r"(访问|考察|调研|交流|座谈|会见)",
        "获奖": r"(获奖|表彰|荣获|评选|授予)",
        "建设": r"(建设|成立|揭牌|启动|开工|竣工)",
        "学术": r"(学术|科研|课题|论文|专利|创新)",
        "会议": r"(会议|大会|论坛|研讨会|峰会|报告会)",
    }
    for label, pat in event_kw.items():
        m = _re.search(pat, text)
        if m:
            ctx_start = max(m.start() - 10, 0)
            ctx_end = min(m.end() + 30, len(text))
            ctx = text[ctx_start:ctx_end].strip()
            keypoints.append(f"{label}：{ctx}")
            if len(keypoints) >= 8:
                break

    # 结果/影响提取
    result_pat = r"(推动|促进|提升|加强|完善|实现|达成|标志着|意味着|有助于|有利于)([\u4e00-\u9fa5，、。；！？\d\w]+?)(?:[。；]|\Z)"
    for m in _re.finditer(result_pat, text):
        r = m.group(0)[:40]
        if len(r) > 8:
            keypoints.append(f"影响：{r}")
            break

    return summary, "\n".join(keypoints) if keypoints else "暂无关键信息提取"


@app.route("/api/news/<news_id>/summarize", methods=["POST"])
def api_summarize_news(news_id):
    """
    对单篇新闻进行 AI 智能总结，结果存入 MySQL
    对外 API 接口：第三方系统可调用此接口触发总结
    请求体可选: {"use_ai": true} 强制使用 AI 模型
    """
    try:
        use_ai = (request.get_json() or {}).get("use_ai", False)
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT title, content, ai_summary, ai_keypoints FROM news WHERE news_id=%s", [news_id])
            row = cur.fetchone()
        if not row:
            conn.close()
            return jsonify({"code": -1, "msg": "新闻不存在"}), 404

        # 已有 AI 总结则直接返回（含关键信息点）
        if row["ai_summary"]:
            conn.close()
            return jsonify({"code": 0, "data": {
                "news_id": news_id,
                "ai_summary": row["ai_summary"],
                "ai_keypoints": row["ai_keypoints"],
                "source": "cached",
            }})

        title, content = row["title"], row["content"]
        if not content:
            conn.close()
            return jsonify({"code": -1, "msg": "新闻正文为空"}), 400

        summary, keypoints = None, None
        source = "rule"

        # 尝试 AI 模型
        if use_ai and AI_CONFIG["api_key"]:
            prompt = f"""请对以下新闻进行客观、完整的信息梳理，输出两部分：

「摘要」（150-250字，概述新闻核心内容，保持信息完整、客观，不添加评论）

「关键信息点」（用列表形式列出时间、地点、人物/机构、核心事件、数据/成果、影响意义等关键要素）

新闻标题：{title}

新闻正文：
{content[:3000]}"""
            ai_result = _call_ai_api(prompt)
            if ai_result:
                # 解析 AI 输出
                parts = ai_result.split("「关键信息点」")
                if len(parts) >= 2:
                    summary = parts[0].replace("「摘要」", "").strip()
                    keypoints = parts[1].strip()
                else:
                    summary = ai_result[:600]
                    keypoints = ai_result[600:]
                source = "ai"

        # 回退到规则引擎
        if not summary:
            summary, keypoints = _rule_based_summarize(title, content)

        # 写入 MySQL
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE news SET ai_summary=%s, ai_keypoints=%s, ai_summarized_at=NOW() WHERE news_id=%s",
                [summary, keypoints, news_id]
            )
        conn.commit()
        conn.close()

        return jsonify({"code": 0, "data": {
            "news_id": news_id,
            "title": title,
            "ai_summary": summary,
            "ai_keypoints": keypoints,
            "source": source,
        }})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


@app.route("/api/news/<news_id>/summarize", methods=["GET"])
def api_get_summary(news_id):
    """获取已存储的 AI 总结"""
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT title, ai_summary, ai_keypoints, ai_summarized_at, url FROM news WHERE news_id=%s", [news_id])
            row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({"code": -1, "msg": "新闻不存在"}), 404
        return jsonify({"code": 0, "data": {
            "title": row["title"],
            "ai_summary": row["ai_summary"],
            "ai_keypoints": row["ai_keypoints"],
            "ai_summarized_at": str(row["ai_summarized_at"]) if row["ai_summarized_at"] else None,
            "url": row["url"],
        }})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


@app.route("/api/admin/batch-summarize", methods=["POST"])
@admin_required
def api_batch_summarize():
    """批量 AI 总结：对未总结的新闻逐一进行智能提取并入库"""
    try:
        data = request.get_json() or {}
        limit = int(data.get("limit", 20))
        use_ai = data.get("use_ai", False)

        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT news_id, title, content FROM news WHERE (ai_summary IS NULL OR ai_summary='') AND content IS NOT NULL AND content!='' LIMIT %s",
                [limit]
            )
            rows = cur.fetchall()
        conn.close()

        results = []
        for row in rows:
            title, content = row["title"], row["content"]
            if not content:
                continue

            summary, keypoints = None, None
            source = "rule"
            if use_ai and AI_CONFIG["api_key"]:
                prompt = f"""请对以下新闻进行客观、完整的信息梳理，输出两部分：
「摘要」（150-250字）
「关键信息点」（列表形式）
新闻标题：{title}
新闻正文：{content[:3000]}"""
                ai_result = _call_ai_api(prompt)
                if ai_result:
                    parts = ai_result.split("「关键信息点」")
                    summary = (parts[0].replace("「摘要」", "").strip())[:600] if parts else ai_result[:600]
                    keypoints = parts[1].strip() if len(parts) >= 2 else ai_result[600:]
                    source = "ai"

            if not summary:
                summary, keypoints = _rule_based_summarize(title, content)

            conn = get_db()
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE news SET ai_summary=%s, ai_keypoints=%s, ai_summarized_at=NOW() WHERE news_id=%s",
                    [summary, keypoints, row["news_id"]]
                )
            conn.commit()
            conn.close()
            results.append({"news_id": row["news_id"], "title": title[:30], "source": source})

        return jsonify({"code": 0, "data": {"processed": len(results), "items": results}})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


@app.route("/api/news/<news_id>/refetch-content", methods=["POST"])
@admin_required
def api_refetch_content(news_id):
    """重新抓取新闻详情页的完整正文（修复旧数据内容不完整问题）"""
    try:
        import requests as _req
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT url FROM news WHERE news_id=%s", [news_id])
            row = cur.fetchone()
        if not row or not row["url"]:
            conn.close()
            return jsonify({"code": -1, "msg": "新闻不存在或无 URL"}), 404

        url = row["url"]
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = _req.get(url, headers=headers, timeout=30)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")

        # 提取完整正文
        content = ""
        article = soup.find("div", class_="v_news_content")
        if not article:
            article = soup.find("div", class_=re.compile(r"article|content"))
        if article:
            paragraphs = [p.get_text(strip=True) for p in article.find_all("p") if len(p.get_text(strip=True)) > 10]
            content = "\n".join(paragraphs)
        if not content:
            # 回退：取所有 <p> 标签
            paragraphs = [p.get_text(strip=True) for p in soup.find_all("p") if len(p.get_text(strip=True)) > 10]
            content = "\n".join(paragraphs)

        if content:
            with conn.cursor() as cur:
                cur.execute("UPDATE news SET content=%s WHERE news_id=%s", [content, news_id])
            conn.commit()
        conn.close()

        return jsonify({"code": 0, "data": {"news_id": news_id, "content_length": len(content), "url": url}})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


# ==================== 推送机制 API ====================

@app.route("/api/admin/push", methods=["POST"])
@admin_required
def api_trigger_push():
    """手动触发推送：根据用户偏好匹配新新闻并生成通知"""
    try:
        data = request.get_json() or {}
        push_type = data.get("push_type", "new_news")
        days_back = data.get("days_back", 1)

        conn = get_db()
        total_notifications = 0
        users_affected = 0

        with conn.cursor() as cur:
            # 1. 获取所有开启了推送的用户及其偏好
            cur.execute(
                """SELECT u.id AS user_id, u.display_name,
                    p.preferred_categories, p.preferred_keywords, p.push_enabled
                    FROM users u JOIN user_preferences p ON u.id=p.user_id
                    WHERE u.status='active' AND p.push_enabled=1"""
            )
            users = cur.fetchall()

            # 2. 获取最近 N 天的新新闻
            cur.execute(
                """SELECT news_id, title, category, LEFT(content,150) AS summary, publish_date
                   FROM news WHERE created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
                   ORDER BY publish_date DESC""",
                [days_back]
            )
            recent_news = cur.fetchall()

            if not recent_news:
                conn.close()
                return jsonify({"code": 0, "msg": "没有可推送的新闻"})

            # 3. 为每个用户匹配新闻并生成通知
            for user in users:
                prefs = user.get("preferred_categories", "")
                keywords = user.get("preferred_keywords", "")
                pref_cats = [c.strip() for c in prefs.split(",") if c.strip()] if prefs else []
                pref_kws = [k.strip() for k in keywords.split(",") if k.strip()] if keywords else []

                matched = []
                for news in recent_news:
                    score = 0
                    if news["category"] in pref_cats:
                        score += 3
                    if pref_kws:
                        for kw in pref_kws:
                            if kw in (news.get("title", "") + news.get("summary", "")):
                                score += 2
                    if score >= 3:  # 至少分类匹配
                        matched.append((news, score))

                if matched:
                    users_affected += 1
                    for news, score in matched:
                        cur.execute(
                            """INSERT INTO notifications (user_id, news_id, title, message, category, push_type)
                               VALUES (%s,%s,%s,%s,%s,%s)""",
                            [
                                user["user_id"],
                                news["news_id"],
                                f"[个性化推荐] {news['title'][:80]}",
                                f"根据您的偏好推荐：{news['summary'][:120]}",
                                news["category"],
                                push_type,
                            ]
                        )
                        total_notifications += 1

            # 4. 记录推送日志
            cur.execute(
                """INSERT INTO push_logs (push_type, target_count, success_count, news_count, details)
                   VALUES (%s,%s,%s,%s,%s)""",
                [
                    push_type,
                    len(users),
                    users_affected,
                    len(recent_news),
                    json.dumps({
                        "new_news_ids": [n["news_id"] for n in recent_news],
                        "affected_users": users_affected,
                    }, ensure_ascii=False),
                ]
            )

        conn.commit()
        conn.close()

        return jsonify({"code": 0, "data": {
            "users_scanned": len(users),
            "users_affected": users_affected,
            "notifications_created": total_notifications,
            "news_available": len(recent_news),
            "msg": f"推送完成：扫描 {len(users)} 个用户，为 {users_affected} 个用户生成了 {total_notifications} 条通知",
        }})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


@app.route("/api/admin/push-logs", methods=["GET"])
@admin_required
def api_push_logs():
    """推送日志"""
    try:
        limit = int(request.args.get("limit", 10))
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM push_logs ORDER BY created_at DESC LIMIT %s", [limit])
            logs = cur.fetchall()
        conn.close()
        for log in logs:
            date_fmt(log)
        return jsonify({"code": 0, "data": logs})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


# ==================== 辅助函数 ====================

def time_ago(dt_str):
    """计算相对时间"""
    if not dt_str:
        return ""
    if isinstance(dt_str, str):
        dt = datetime.strptime(dt_str[:19], "%Y-%m-%d %H:%M:%S")
    else:
        dt = dt_str
    diff = datetime.now() - dt
    seconds = int(diff.total_seconds())
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    days = seconds // 86400
    if days < 7:
        return f"{days} 天前"
    return dt.strftime("%m-%d %H:%M")


# ==================== 初始化数据库表 ====================

def init_db():
    """确保所有表存在"""
    conn = pymysql.connect(
        host=DB_CONFIG["host"], port=DB_CONFIG["port"],
        user=DB_CONFIG["user"], password=DB_CONFIG["password"],
        charset="utf8mb4"
    )
    with conn.cursor() as cur:
        cur.execute(
            "CREATE DATABASE IF NOT EXISTS `nchu_news` "
            "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
    conn.close()

    db = get_db()
    with db.cursor() as cur:
        # users
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(50) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                display_name VARCHAR(50) NOT NULL,
                department VARCHAR(100),
                role VARCHAR(20) NOT NULL DEFAULT 'user',
                avatar_text VARCHAR(10),
                status VARCHAR(20) DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        # user_preferences
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_preferences (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id INT NOT NULL UNIQUE,
                preferred_categories VARCHAR(500),
                preferred_keywords VARCHAR(500),
                push_enabled TINYINT(1) DEFAULT 1,
                daily_digest TINYINT(1) DEFAULT 1,
                deadline_reminder TINYINT(1) DEFAULT 1,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        # reading_history
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reading_history (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id INT NOT NULL,
                news_id VARCHAR(20) NOT NULL,
                read_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                INDEX idx_user_read (user_id, news_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        # notifications
        cur.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id INT NOT NULL,
                news_id VARCHAR(20),
                title VARCHAR(300) NOT NULL,
                message VARCHAR(500),
                category VARCHAR(50),
                push_type VARCHAR(30) DEFAULT 'new_news',
                is_read TINYINT(1) DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                INDEX idx_user_read (user_id, is_read)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        # push_logs
        cur.execute("""
            CREATE TABLE IF NOT EXISTS push_logs (
                id INT AUTO_INCREMENT PRIMARY KEY,
                push_type VARCHAR(30),
                target_count INT DEFAULT 0,
                success_count INT DEFAULT 0,
                news_count INT DEFAULT 0,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        # monitor_sites - 监测站点
        cur.execute("""
            CREATE TABLE IF NOT EXISTS monitor_sites (
                id INT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                url VARCHAR(500),
                frequency VARCHAR(30) DEFAULT '每日2次',
                status VARCHAR(20) DEFAULT 'running' COMMENT 'running / warning / stopped',
                scraper_config JSON COMMENT '爬取配置: {list_url,detail_url_pattern,list_selector,detail_content_selector,...}',
                last_checked TIMESTAMP NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        # 兼容：为已有表新增 scraper_config 列
        try:
            cur.execute("ALTER TABLE monitor_sites ADD COLUMN scraper_config JSON AFTER status")
        except Exception:
            pass
        # collection_logs - 采集日志
        cur.execute("""
            CREATE TABLE IF NOT EXISTS collection_logs (
                id INT AUTO_INCREMENT PRIMARY KEY,
                total_sites INT DEFAULT 0,
                success_count INT DEFAULT 0,
                fail_count INT DEFAULT 0,
                new_news_count INT DEFAULT 0,
                duration_seconds INT DEFAULT 0,
                status VARCHAR(20) DEFAULT 'completed',
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        # news - 添加 AI 总结字段
        cur.execute("""
            CREATE TABLE IF NOT EXISTS news (
                id INT AUTO_INCREMENT PRIMARY KEY,
                news_id VARCHAR(20) NOT NULL UNIQUE,
                title VARCHAR(500) NOT NULL,
                category VARCHAR(50) NOT NULL DEFAULT '综合新闻',
                publish_date DATE,
                source VARCHAR(200),
                views INT DEFAULT 0,
                content LONGTEXT,
                url VARCHAR(500),
                ai_summary TEXT,
                ai_keypoints TEXT,
                ai_summarized_at TIMESTAMP NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_category (category),
                INDEX idx_publish_date (publish_date),
                INDEX idx_news_id (news_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        # 兼容已有表：补充 ai_summary / ai_keypoints 列
        try:
            cur.execute("ALTER TABLE news ADD COLUMN ai_summary TEXT AFTER url")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE news ADD COLUMN ai_keypoints TEXT AFTER ai_summary")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE news MODIFY COLUMN news_id VARCHAR(64) NOT NULL COMMENT '新闻ID'")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE news ADD COLUMN ai_summarized_at TIMESTAMP NULL AFTER ai_keypoints")
        except Exception:
            pass
       
        cur.execute("SELECT COUNT(*) AS c FROM monitor_sites")
        if cur.fetchone()["c"] == 0:
            import json as _json
            default_sites = [
                ("南昌航空大学-南航要闻", "www.nchu.edu.cn", "每日2次", "running",
                    _json.dumps({"list_url":"https://www.nchu.edu.cn/xwzx/chyw","list_pagination":"?page={page}","list_item_selector":"a[href*='content_']","detail_meta_source":"meta","detail_content_selector":"div.v_news_content","date_format":"meta","encoding":"utf-8","news_id_pattern":"content_","base_url":"https://www.nchu.edu.cn"}, ensure_ascii=False)),
                ("南昌航空大学-通知公告", "www.nchu.edu.cn", "每日2次", "running",
                    _json.dumps({"list_url":"https://www.nchu.edu.cn/xwzx/tzgg","list_pagination":"?page={page}","list_item_selector":"a[href*='content_']","detail_meta_source":"meta","detail_content_selector":"div.v_news_content","date_format":"meta","encoding":"utf-8","news_id_pattern":"content_","base_url":"https://www.nchu.edu.cn"}, ensure_ascii=False)),
                ("南昌航空大学-学术动态", "www.nchu.edu.cn", "每日2次", "running",
                    _json.dumps({"list_url":"https://www.nchu.edu.cn/xwzx/xsdt","list_pagination":"?page={page}","list_item_selector":"a[href*='content_']","detail_meta_source":"meta","detail_content_selector":"div.v_news_content","date_format":"meta","encoding":"utf-8","news_id_pattern":"content_","base_url":"https://www.nchu.edu.cn"}, ensure_ascii=False)),
                ("南昌航空大学-媒体南航", "www.nchu.edu.cn", "每日1次", "running",
                    _json.dumps({"list_url":"https://www.nchu.edu.cn/xwzx/mtnh","list_pagination":"?page={page}","list_item_selector":"a[href*='content_']","detail_meta_source":"meta","detail_content_selector":"div.v_news_content","date_format":"meta","encoding":"utf-8","news_id_pattern":"content_","base_url":"https://www.nchu.edu.cn"}, ensure_ascii=False)),
                ("南昌航空大学-校园文化", "www.nchu.edu.cn", "每日1次", "running",
                    _json.dumps({"list_url":"https://www.nchu.edu.cn/xwzx/xywh","list_pagination":"?page={page}","list_item_selector":"a[href*='content_']","detail_meta_source":"meta","detail_content_selector":"div.v_news_content","date_format":"meta","encoding":"utf-8","news_id_pattern":"content_","base_url":"https://www.nchu.edu.cn"}, ensure_ascii=False)),
            ]
            for name, url, freq, status, config in default_sites:
                cur.execute(
                    "INSERT INTO monitor_sites (name, url, frequency, status, scraper_config) VALUES (%s,%s,%s,%s,%s)",
                    [name, url, freq, status, config]
                )
        # 默认采集日志
        cur.execute("SELECT COUNT(*) AS c FROM collection_logs")
        if cur.fetchone()["c"] == 0:
            cur.execute(
                "INSERT INTO collection_logs (total_sites, success_count, fail_count, new_news_count, duration_seconds) "
                "VALUES (5, 5, 0, 20, 45)"
            )
        # 默认管理员
        cur.execute(
            "INSERT IGNORE INTO users (username, password_hash, display_name, department, role, avatar_text) "
            "VALUES ('admin', %s, '方裕骁', '科信管理部', 'admin', '方')",
            [sha256("admin123")]
        )
        # 给管理员创建默认偏好
        cur.execute(
            "INSERT INTO user_preferences (user_id, preferred_categories, preferred_keywords) "
            "VALUES (1, '党建思政,教育教学,学术科研', '双一流,人才,创新') "
            "ON DUPLICATE KEY UPDATE preferred_categories=VALUES(preferred_categories)"
        )
        # 默认用户 user01
        cur.execute(
            "INSERT IGNORE INTO users (username, password_hash, display_name, department, role, avatar_text) "
            "VALUES ('user01', %s, '周世炯', '新能源研发中心', 'user', '周')",
            [sha256("user123")]
        )
        cur.execute(
            "INSERT IGNORE INTO user_preferences (user_id, preferred_categories, preferred_keywords) "
            "VALUES (2, '学术科研,荣誉获奖,交流合作', '新能源,技术,科研')"
        )
        # 默认用户 user02
        cur.execute(
            "INSERT IGNORE INTO users (username, password_hash, display_name, department, role, avatar_text) "
            "VALUES ('user02', %s, '耿旌轩', '碳中和研发中心', 'user', '耿')",
            [sha256("user123")]
        )
        cur.execute(
            "INSERT IGNORE INTO user_preferences (user_id, preferred_categories, preferred_keywords) "
            "VALUES (3, '教育教学,学术科研,荣誉获奖', '碳中和,环保,绿色')"
        )
    db.commit()
    db.close()


# ==================== 启动 ====================

if __name__ == "__main__":
    init_db()
    print("=" * 50)
    print("南昌航空大学新闻系统")
    print("访问地址: http://127.0.0.1:5000")
    print("默认管理员: admin / admin123")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=True)
