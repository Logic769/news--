# 南昌航空大学新闻系统

基于 Flask 的校园新闻聚合与智能分析平台，支持多站爬虫采集、AI 智能总结、个性化推送。

## 功能概览

| 模块 | 功能 |
|---|---|
| 新闻浏览 | 首页推荐、分类筛选、关键词搜索、分页浏览 |
| 新闻详情 | 完整正文展示、原文链接、浏览计数 |
| AI 智能总结 | DeepSeek / OpenAI 大模型摘要 + 关键信息提取，未配置 API 时自动降级为规则引擎 |
| 用户系统 | 注册/登录、偏好设置（分类+关键词）、阅读历史 |
| 个人中心 | 阅读统计、偏好管理、推送开关 |
| 系统管理 | 监测站点管理、用户管理、推送日志 |
| 多站爬虫 | 基于 JSON 配置的通用爬虫引擎，支持任意网站 |
| 智能推送 | 用户偏好匹配评分（分类 +3 / 关键词 +2），阈值 ≥3 触发通知 |

## 快速开始

### 环境要求

- Python 3.8+
- MySQL 5.7+（需预先安装并启动）

### 1. 配置

```bash
# 复制配置模板
copy .env.example .env
```

编辑 `.env`，修改数据库密码：

```ini
MYSQL_PASSWORD=你的MySQL密码
```

可选：配置 AI 大模型密钥（不填则使用内置规则摘要）：

```ini
AI_API_KEY=sk-your-key-here
```

### 2. 一键部署

```bash
python setup.py
```

自动完成依赖安装、数据库创建、表结构初始化。

### 3. 启动

```bash
python app.py
```

或双击 `run.bat` 启动。

访问 `http://127.0.0.1:5000`，默认管理员：`admin` / `admin123`。

## 项目结构

```
├── app.py              # Flask 主服务（路由、API、初始化）
├── config.py           # 统一配置中心（自动加载 .env）
├── setup.py            # 一键部署脚本
├── run.bat             # Windows 启动脚本
├── news_scraper.py     # 单一站点爬虫（南昌航空大学）
├── site_scanner.py     # 通用多站爬虫引擎
├── requirements.txt    # Python 依赖
├── .env                # 环境变量配置（不入库）
├── .env.example        # 配置模板
├── .gitignore
├── init_db.sql         # 数据库初始化 SQL（参考）
├── templates/
│   └── index.html      # 前端 SPA 页面
└── AI-assistant.html   # 设计参考文件
```

## API 接口

### 公开接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/news` | 新闻列表（分页、分类、关键词） |
| GET | `/api/news/<id>` | 新闻详情 |
| GET | `/api/stats` | 首页统计数据 |
| GET | `/api/categories` | 分类列表 |

### AI 总结

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/news/<id>/summarize` | 生成 AI 总结（`{"use_ai":true}` 启用大模型） |
| GET | `/api/news/<id>/summarize` | 获取已有总结 |
| POST | `/api/news/<id>/refetch-content` | 重新抓取原文正文 |

### 管理后台（需 admin 权限）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/api/admin/sites` | 站点 CRUD（含 scraper_config JSON） |
| PUT | `/api/admin/sites/<id>` | 更新站点配置 |
| DELETE | `/api/admin/sites/<id>` | 删除站点 |
| PUT | `/api/admin/sites/<id>/status` | 切换站点状态 |
| POST | `/api/admin/scan-all` | 全站扫描 |
| POST | `/api/admin/scan-site/<id>` | 单站扫描 |
| GET | `/api/admin/scan-logs` | 采集日志 |
| POST | `/api/admin/push` | 触发智能推送 |
| POST | `/api/admin/batch-summarize` | 批量 AI 总结 |

## 站点爬虫配置

在系统管理页面添加站点时，填入 JSON 爬虫规则：

```json
{
  "list_url": "https://xxx.edu.cn/news/list",
  "list_pagination": "/page/{page}",
  "list_item_selector": "a.news-link",
  "detail_content_selector": "div.article-body",
  "news_id_pattern": "(\\d+)",
  "base_url": "https://xxx.edu.cn",
  "encoding": "utf-8"
}
```

| 字段 | 说明 |
|---|---|
| `list_url` | 列表页地址（必填） |
| `list_pagination` | 分页模板，`{page}` 为页码占位 |
| `list_item_selector` | 新闻链接 CSS 选择器 |
| `detail_content_selector` | 正文区域 CSS 选择器 |
| `base_url` | 拼接相对路径的基础 URL |
| `encoding` | 页面编码（`utf-8` / `gb2312`） |

## 配置项

所有配置通过 `.env` 文件或环境变量控制，优先级：环境变量 > `.env` > `config.py` 默认值。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MYSQL_HOST` | `localhost` | 数据库地址 |
| `MYSQL_PORT` | `3306` | 数据库端口 |
| `MYSQL_USER` | `root` | 数据库用户 |
| `MYSQL_PASSWORD` | `root` | 数据库密码 |
| `MYSQL_DATABASE` | `nchu_news` | 数据库名 |
| `AI_API_URL` | `https://api.deepseek.com/chat/completions` | AI 接口地址 |
| `AI_API_KEY` | （空） | AI 密钥，不填则使用规则引擎 |
| `AI_MODEL` | `deepseek-chat` | 模型名称 |
| `ADMIN_USER` | `admin` | 默认管理员账号 |
| `ADMIN_PASS` | `admin123` | 默认管理员密码 |
| `SCRAPER_DELAY` | `2` | 爬虫请求间隔（秒） |
| `SCRAPER_PAGES` | `3` | 每站默认爬取页数 |

## 用户偏好推送算法

推送匹配基于用户设置的偏好分类和关键词：

| 匹配条件 | 得分 |
|---|---|
| 新闻分类 ∈ 用户偏好分类 | +3 |
| 关键词命中新闻标题/摘要 | +2（每个关键词） |
| 匹配阈值 | ≥3 分即触发推送 |

## 数据库表

| 表名 | 说明 |
|---|---|
| `news` | 新闻主表（含 AI 摘要字段） |
| `users` | 用户表（SHA-256 密码哈希） |
| `user_preferences` | 用户偏好（分类、关键词、推送开关） |
| `reading_history` | 阅读历史 |
| `notifications` | 推送通知 |
| `push_logs` | 推送执行日志 |
| `monitor_sites` | 监测站点（含 JSON 爬虫配置） |
| `collection_logs` | 采集运行日志 |
