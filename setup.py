"""
一键部署工具
用途：在新电脑上运行此脚本，自动完成依赖安装、数据库创建和初始化
用法：
    python setup.py           # 使用 config.py 默认配置
    python setup.py --interactive  # 交互式输入配置
"""

import sys
import os
import subprocess
import pymysql


def run_cmd(cmd):
    """运行命令并检查结果"""
    print(f"  >>> {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0 and result.stderr:
        print(f"  [!] {result.stderr.strip()}")
    return result.returncode == 0


def install_deps():
    """安装 Python 依赖"""
    print("\n[1/3] 安装 Python 依赖...")
    return run_cmd(f'"{sys.executable}" -m pip install -r requirements.txt -q')


def setup_db(user, password, host, port, database):
    """创建数据库（如果不存在）"""
    print(f"\n[2/3] 创建数据库 {database} ...")
    try:
        conn = pymysql.connect(host=host, port=port, user=user, password=password, charset="utf8mb4")
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{database}` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        conn.commit()
        conn.close()
        print(f"  ✓ 数据库 {database} 已就绪")
        return True
    except Exception as e:
        print(f"  ✗ 数据库连接失败: {e}")
        print(f"  请确保 MySQL 已启动，且 {user} 用户密码正确")
        return False


def init_tables():
    """初始化表结构和默认数据"""
    print("\n[3/3] 初始化表结构...")
    from app import init_db
    init_db()
    print("  ✓ 表结构初始化完成")


def interactive_config():
    """交互式输入数据库配置"""
    print("\n=== 数据库配置 ===")
    host = input("MySQL 主机地址 [localhost]: ").strip() or "localhost"
    port = input("MySQL 端口 [3306]: ").strip() or "3306"
    user = input("MySQL 用户名 [root]: ").strip() or "root"
    password = input("MySQL 密码: ").strip()
    database = input("数据库名 [nchu_news]: ").strip() or "nchu_news"

    # 写入 .env 文件
    with open(".env", "w", encoding="utf-8") as f:
        f.write(f"MYSQL_HOST={host}\n")
        f.write(f"MYSQL_PORT={port}\n")
        f.write(f"MYSQL_USER={user}\n")
        f.write(f"MYSQL_PASSWORD={password}\n")
        f.write(f"MYSQL_DATABASE={database}\n")

    # 设置环境变量
    os.environ["MYSQL_HOST"] = host
    os.environ["MYSQL_PORT"] = port
    os.environ["MYSQL_USER"] = user
    os.environ["MYSQL_PASSWORD"] = password
    os.environ["MYSQL_DATABASE"] = database

    return user, password, host, int(port), database


def main():
    print("=" * 50)
    print("  南昌航空大学新闻系统 — 一键部署")
    print("=" * 50)

    interactive = "--interactive" in sys.argv or "-i" in sys.argv

    if interactive:
        user, pwd, host, port, db = interactive_config()
    else:
        from config import DB_CONFIG
        user = DB_CONFIG["user"]
        pwd = DB_CONFIG["password"]
        host = DB_CONFIG["host"]
        port = DB_CONFIG["port"]
        db = DB_CONFIG["database"]
        # 先尝试加载 .env
        if os.path.exists(".env"):
            with open(".env", encoding="utf-8") as f:
                for line in f:
                    if "=" in line:
                        k, v = line.strip().split("=", 1)
                        os.environ[k] = v

    # 1. 安装依赖
    if not install_deps():
        print("\n[!] 依赖安装失败，请手动运行: pip install -r requirements.txt")
    else:
        print("  ✓ 依赖安装完成")

    # 2. 创建数据库
    if not setup_db(user, pwd, host, port, db):
        print("\n[!] 部署失败，请检查 MySQL 连接配置")
        return

    # 3. 初始化表
    init_tables()

    print("\n" + "=" * 50)
    print("  ✓ 部署完成！")
    print(f"  启动服务: python app.py")
    print(f"  访问地址: http://127.0.0.1:5000")
    print("=" * 50)


if __name__ == "__main__":
    main()
