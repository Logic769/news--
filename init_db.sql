-- =============================================
-- 南昌航空大学新闻系统 - 完整数据库初始化
-- =============================================

CREATE DATABASE IF NOT EXISTS `nchu_news`
    DEFAULT CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

USE `nchu_news`;

-- ==================== 新闻表 ====================
CREATE TABLE IF NOT EXISTS `news` (
    `id`            INT AUTO_INCREMENT PRIMARY KEY    COMMENT '自增主键',
    `news_id`       VARCHAR(20)  NOT NULL UNIQUE      COMMENT '新闻ID（如 content_192396）',
    `title`         VARCHAR(500) NOT NULL             COMMENT '新闻标题',
    `category`      VARCHAR(50)  NOT NULL DEFAULT '综合新闻' COMMENT '分类',
    `publish_date`  DATE                              COMMENT '发布日期',
    `source`        VARCHAR(200)                      COMMENT '来源部门',
    `views`         INT          DEFAULT 0            COMMENT '浏览次数（实时递增）',
    `content`       LONGTEXT                          COMMENT '新闻正文',
    `url`           VARCHAR(500)                      COMMENT '新闻原始链接',
    `ai_summary`    TEXT                              COMMENT 'AI 智能总结',
    `ai_keypoints`  TEXT                              COMMENT 'AI 提取关键信息点',
    `ai_summarized_at` TIMESTAMP NULL                 COMMENT 'AI 总结生成时间',
    `created_at`    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    INDEX `idx_category`     (`category`),
    INDEX `idx_publish_date` (`publish_date`),
    INDEX `idx_news_id`      (`news_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='南航要闻';

-- ==================== 用户表 ====================
CREATE TABLE IF NOT EXISTS `users` (
    `id`            INT AUTO_INCREMENT PRIMARY KEY,
    `username`      VARCHAR(50)  NOT NULL UNIQUE,
    `password_hash` VARCHAR(255) NOT NULL,
    `display_name`  VARCHAR(50)  NOT NULL,
    `department`    VARCHAR(100),
    `role`          VARCHAR(20)  NOT NULL DEFAULT 'user',
    `avatar_text`   VARCHAR(10),
    `status`        VARCHAR(20)  DEFAULT 'active',
    `created_at`    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    INDEX `idx_role` (`role`),
    INDEX `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='系统用户';

-- ==================== 用户偏好表 ====================
CREATE TABLE IF NOT EXISTS `user_preferences` (
    `id`                  INT AUTO_INCREMENT PRIMARY KEY,
    `user_id`             INT NOT NULL UNIQUE,
    `preferred_categories` VARCHAR(500),
    `preferred_keywords`  VARCHAR(500),
    `push_enabled`        TINYINT(1)  DEFAULT 1,
    `daily_digest`        TINYINT(1)  DEFAULT 1,
    `deadline_reminder`   TINYINT(1)  DEFAULT 1,
    FOREIGN KEY (`user_id`) REFERENCES `users`(`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户偏好设置';

-- ==================== 阅读历史表 ====================
CREATE TABLE IF NOT EXISTS `reading_history` (
    `id`            INT AUTO_INCREMENT PRIMARY KEY,
    `user_id`       INT NOT NULL,
    `news_id`       VARCHAR(20) NOT NULL,
    `read_at`       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX `idx_user_read` (`user_id`, `news_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户阅读历史';

-- ==================== 通知/推送表 ====================
CREATE TABLE IF NOT EXISTS `notifications` (
    `id`            INT AUTO_INCREMENT PRIMARY KEY,
    `user_id`       INT NOT NULL,
    `news_id`       VARCHAR(20),
    `title`         VARCHAR(300) NOT NULL,
    `message`       VARCHAR(500),
    `category`      VARCHAR(50),
    `push_type`     VARCHAR(30)  DEFAULT 'new_news',
    `is_read`       TINYINT(1)   DEFAULT 0,
    `created_at`    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    INDEX `idx_user_read` (`user_id`, `is_read`),
    INDEX `idx_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='推送通知';

-- ==================== 推送任务日志表 ====================
CREATE TABLE IF NOT EXISTS `push_logs` (
    `id`            INT AUTO_INCREMENT PRIMARY KEY,
    `push_type`     VARCHAR(30),
    `target_count`  INT DEFAULT 0,
    `success_count` INT DEFAULT 0,
    `news_count`    INT DEFAULT 0,
    `details`       TEXT,
    `created_at`    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='推送任务日志';

-- ==================== 监测站点表 ====================
CREATE TABLE IF NOT EXISTS `monitor_sites` (
    `id`            INT AUTO_INCREMENT PRIMARY KEY,
    `name`          VARCHAR(100) NOT NULL              COMMENT '站点名称',
    `url`           VARCHAR(200)                       COMMENT '站点地址',
    `frequency`     VARCHAR(30)  DEFAULT '每日2次'      COMMENT '采集频率',
    `status`        VARCHAR(20)  DEFAULT 'running'     COMMENT 'running / warning / stopped',
    `last_checked`  TIMESTAMP NULL                     COMMENT '最后采集时间',
    `created_at`    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='监测站点';

-- ==================== 采集日志表 ====================
CREATE TABLE IF NOT EXISTS `collection_logs` (
    `id`              INT AUTO_INCREMENT PRIMARY KEY,
    `total_sites`     INT DEFAULT 0                    COMMENT '总站点数',
    `success_count`   INT DEFAULT 0                    COMMENT '成功数',
    `fail_count`      INT DEFAULT 0                    COMMENT '失败数',
    `new_news_count`  INT DEFAULT 0                    COMMENT '新增新闻数',
    `duration_seconds` INT DEFAULT 0                   COMMENT '耗时(秒)',
    `status`          VARCHAR(20) DEFAULT 'completed'   COMMENT 'completed / running / failed',
    `details`         TEXT,
    `created_at`      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='采集日志';

-- ==================== 初始数据 ====================

-- 默认管理员（密码: admin123，sha256）
INSERT IGNORE INTO `users` (`username`, `password_hash`, `display_name`, `department`, `role`, `avatar_text`, `status`)
VALUES ('admin', '240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9', '方裕骁', '科信管理部', 'admin', '方', 'active');

-- 默认用户 user01（密码: user123）
INSERT IGNORE INTO `users` (`username`, `password_hash`, `display_name`, `department`, `role`, `avatar_text`, `status`)
VALUES ('user01', 'b9ece18c950afbfa6b0fdbfa4ff731d3d9fe027f17e79e82060a6a95f8f1c0c9', '周世炯', '新能源研发中心', 'user', '周', 'active');

-- 默认用户 user02（密码: user123）
INSERT IGNORE INTO `users` (`username`, `password_hash`, `display_name`, `department`, `role`, `avatar_text`, `status`)
VALUES ('user02', 'ee11cbb19052e40b07aac0ca060c23ee7e3c4bd41ca5b3781554a2f05be1c92d', '耿旌轩', '碳中和研发中心', 'user', '耿', 'active');

-- 默认偏好
INSERT IGNORE INTO `user_preferences` (`user_id`, `preferred_categories`, `preferred_keywords`, `push_enabled`, `daily_digest`, `deadline_reminder`)
VALUES (1, '党建思政,教育教学,学术科研', '双一流,人才,创新', 1, 1, 1);

INSERT IGNORE INTO `user_preferences` (`user_id`, `preferred_categories`, `preferred_keywords`, `push_enabled`, `daily_digest`, `deadline_reminder`)
VALUES (2, '学术科研,荣誉获奖,交流合作', '新能源,技术,科研', 1, 1, 1);

INSERT IGNORE INTO `user_preferences` (`user_id`, `preferred_categories`, `preferred_keywords`, `push_enabled`, `daily_digest`, `deadline_reminder`)
VALUES (3, '教育教学,学术科研,荣誉获奖', '碳中和,环保,绿色', 1, 0, 1);

-- 默认监测站点
INSERT IGNORE INTO `monitor_sites` (`name`, `url`, `frequency`, `status`) VALUES
('南昌航空大学-南航要闻', 'www.nchu.edu.cn/chyw', '每日2次', 'running'),
('南昌航空大学-通知公告', 'www.nchu.edu.cn/tzgg', '每日2次', 'running'),
('南昌航空大学-学术动态', 'www.nchu.edu.cn/xsdt', '每日2次', 'running'),
('南昌航空大学-媒体南航', 'www.nchu.edu.cn/mtnh', '每日1次', 'running'),
('南昌航空大学-校园文化', 'www.nchu.edu.cn/xywh', '每日1次', 'running');

-- 默认采集日志
INSERT IGNORE INTO `collection_logs` (`total_sites`, `success_count`, `fail_count`, `new_news_count`, `duration_seconds`)
VALUES (5, 5, 0, 20, 45);
