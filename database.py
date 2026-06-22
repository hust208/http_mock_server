# -*- coding: utf-8 -*-
"""HTTP Mock Server - 数据库层 (SQLite)

本模块封装了所有数据库操作，采用单例模式确保全局只有一个 Database 实例。
为保证线程安全（Flask 默认多线程模式），每次操作通过 get_conn() 获取新连接，
并在操作完成后立即关闭连接。

数据表结构：
1. scenes       - 场景表（PRD 4.1），用于分组管理 Mock 规则，同一时刻只有一个场景激活
2. rules        - Mock 规则表（PRD 4.2-4.3），存储 URL 匹配条件和响应配置
3. request_logs - 请求日志表（PRD 7.1），记录每次请求的完整信息和响应
4. settings     - 全局设置表（PRD 8），键值对形式存储运行时配置
5. users        - 用户表（PRD 2.1-2.2），四级角色权限模型
6. audit_logs   - 审计日志表（PRD 2.3），记录用户操作行为

支持 SQLite WAL 模式以提升并发读取性能，并通过迁移机制平滑升级表结构。
"""

import sqlite3
import json
import hashlib
import threading
from datetime import datetime, timedelta

from config import Config


class Database(object):
    """SQLite 数据库封装类（单例模式）

    使用 __new__ + 线程锁实现线程安全的单例。
    每次数据库操作通过 get_conn() 获取独立连接，操作完成后关闭，
    避免多线程共享同一连接导致的并发问题。

    Attributes:
        db_path: SQLite 数据库文件路径
        _initialized: 是否已完成初始化（防止重复初始化）
    """

    _instance = None       # 单例实例
    _lock = threading.Lock()  # 线程锁，保证单例创建的原子性

    def __new__(cls, *args, **kwargs):
        """双重检查锁实现线程安全的单例创建。"""
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(Database, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, db_path=None):
        """初始化数据库连接路径并创建表结构。

        Args:
            db_path: SQLite 数据库文件路径，为 None 时使用 Config.DATABASE
        """
        if self._initialized:
            return
        self.db_path = db_path or Config.DATABASE
        self._initialized = True
        self.init_db()

    def get_conn(self):
        """获取一个新的 SQLite 连接（线程安全：每次调用返回独立连接）。

        启用 WAL 模式提升并发读性能，开启外键约束支持。
        调用方负责在使用完毕后关闭连接。

        Returns:
            sqlite3.Connection: 配置好 row_factory 的数据库连接
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row  # 使查询结果可通过列名访问
        conn.execute('PRAGMA journal_mode=WAL')   # WAL 模式：读写不互斥
        conn.execute('PRAGMA foreign_keys=ON')     # 开启外键约束
        return conn

    def init_db(self):
        """初始化数据库表结构和默认数据。

        执行流程：
        1. 创建所有数据表（如不存在）
        2. 创建查询索引（提升匹配和查询性能）
        3. 执行 rules 表迁移（添加新功能字段）
        4. 写入默认配置项到 settings 表
        5. 创建默认场景（Default）
        6. 创建默认管理员用户（admin/admin123）
        """
        conn = self.get_conn()
        cursor = conn.cursor()

        # 场景表 (PRD 4.1)
        # 场景用于分组管理 Mock 规则，同一时刻只有一个场景处于激活状态。
        # 例如可创建"正常场景"、"异常场景"、"压测场景"等，快速切换不同 Mock 体系。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scenes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,          -- 场景名称（唯一）
                description TEXT DEFAULT '',         -- 场景描述
                is_active INTEGER DEFAULT 0,         -- 是否激活（1=激活，0=未激活）
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        ''')

        # 规则表 (PRD 4.2-4.3)
        # 核心表：存储每条 Mock 规则的匹配条件和响应配置。
        # 字段说明：
        #   url_pattern / url_match_type  - URL 匹配模式与匹配方式（exact/regex/wildcard/prefix）
        #   method                       - HTTP 方法（GET/POST/... 或 ANY 通配）
        #   match_conditions             - 匹配条件列表（JSON 数组），支持多源多操作符
        #   match_logic                  - 条件间逻辑关系（and/or），迁移字段
        #   response_*                   - 响应状态码、Header、Body
        #   delay_ms / timeout_enabled   - 延迟返回与超时模拟
        #   random_exception_*           - 随机异常模拟（按概率返回错误状态码）
        #   redirect_url                 - 重定向目标 URL（PRD 4.3.5）
        #   set_cookies                   - Set-Cookie 配置（PRD 4.3.2）
        #   cors_enabled / cors_origin    - 每条规则独立的 CORS 设置
        #   pre_script / post_script     - 前置/后置脚本（PRD 5.2）
        #   version                       - 乐观锁版本号（PRD 12.4）
        #   priority                       - 优先级（数字越小优先级越高，匹配时先到先得）
        #   hit_count                      - 命中次数（统计用）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,                             -- 规则名称
                description TEXT DEFAULT '',                     -- 规则描述
                scene_id INTEGER,                                -- 所属场景 ID（外键）
                url_pattern TEXT NOT NULL,                        -- URL 匹配模式
                url_match_type TEXT DEFAULT 'exact',             -- URL 匹配方式
                method TEXT NOT NULL DEFAULT 'GET',               -- HTTP 方法
                match_conditions TEXT DEFAULT '[]',              -- 匹配条件（JSON）
                response_status INTEGER DEFAULT 200,              -- 响应状态码
                response_headers TEXT DEFAULT '{}',               -- 响应头（JSON）
                response_body TEXT DEFAULT '',                    -- 响应体（支持模板变量）
                delay_ms INTEGER DEFAULT 0,                      -- 响应延迟（毫秒）
                timeout_enabled INTEGER DEFAULT 0,                -- 是否启用超时模拟
                random_exception_enabled INTEGER DEFAULT 0,       -- 是否启用随机异常
                random_exception_probability INTEGER DEFAULT 0,  -- 随机异常概率（1-100）
                random_exception_status INTEGER DEFAULT 500,      -- 随机异常状态码
                priority INTEGER DEFAULT 100,                     -- 匹配优先级（越小越优先）
                enabled INTEGER DEFAULT 1,                        -- 是否启用
                hit_count INTEGER DEFAULT 0,                      -- 命中次数
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (scene_id) REFERENCES scenes(id) ON DELETE SET NULL
            )
        ''')

        # 请求日志表 (PRD 7.1)
        # 记录每次 Mock 请求的完整信息，包括请求方法、URL、Header、Body、
        # 匹配结果（matched/forwarded/unmatched）、响应数据、耗时等。
        # 支持按时间、URL、方法、场景等条件筛选，并支持自动化断言查询。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS request_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL UNIQUE,              -- 请求唯一标识（UUID）
                method TEXT NOT NULL,                          -- HTTP 方法
                url TEXT NOT NULL,                             -- 完整 URL
                path TEXT NOT NULL,                            -- URL 路径
                query_params TEXT DEFAULT '{}',                -- 查询参数（JSON）
                headers TEXT DEFAULT '{}',                     -- 请求头（JSON）
                body TEXT DEFAULT '',                           -- 请求体原文
                matched_rule_id INTEGER,                       -- 匹配的规则 ID
                matched_rule_name TEXT DEFAULT '',             -- 匹配的规则名称
                match_result TEXT DEFAULT 'unmatched',         -- 匹配结果
                response_status INTEGER DEFAULT 0,              -- 响应状态码
                response_body TEXT DEFAULT '',                  -- 响应体（截断至 5000 字符）
                response_headers TEXT DEFAULT '{}',             -- 响应头（JSON）
                response_time_ms INTEGER DEFAULT 0,             -- 响应耗时（毫秒）
                scene_id INTEGER,                               -- 所属场景 ID
                scene_name TEXT DEFAULT '',                     -- 所属场景名称
                client_ip TEXT DEFAULT '',                      -- 客户端 IP
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        ''')

        # 设置表 (PRD 8)
        # 键值对形式存储全局运行时配置，值统一为文本类型。
        # 初始值来自 Config.DEFAULTS，用户可通过管理界面动态修改。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,                          -- 配置键
                value TEXT,                                     -- 配置值
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        ''')

        # 用户表 (PRD 2.1-2.2)
        # 四级角色权限模型：super_admin > project_admin > editor > viewer
        # 密码使用 SHA256 哈希存储，首次启动时自动创建默认管理员 admin/admin123
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,                 -- 用户名（唯一）
                password TEXT NOT NULL,                         -- SHA256 哈希密码
                role TEXT DEFAULT 'viewer',                    -- 角色等级
                display_name TEXT DEFAULT '',                   -- 显示名称
                email TEXT DEFAULT '',                           -- 邮箱
                last_login TEXT,                                -- 最后登录时间
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        ''')

        # 审计日志表 (PRD 2.3)
        # 记录用户的操作行为，包括登录、规则增删改、设置变更等。
        # 用于安全审计和操作追溯。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,                                -- 操作用户 ID
                username TEXT DEFAULT '',                        -- 操作用户名
                action TEXT NOT NULL,                            -- 操作类型（login/logout/create/update/delete）
                target_type TEXT DEFAULT '',                     -- 操作对象类型（rule/scene/user/setting）
                target_id TEXT DEFAULT '',                       -- 操作对象 ID
                detail TEXT DEFAULT '',                           -- 操作详情
                ip TEXT DEFAULT '',                               -- 操作 IP
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        ''')

        # 创建查询索引（提升匹配和查询性能）
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_rules_scene ON rules(scene_id)')          # 按场景查询规则
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_rules_enabled ON rules(enabled)')         # 过滤启用的规则
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_requests_request_id ON request_logs(request_id)')  # 按请求 ID 查询
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_requests_created ON request_logs(created_at)')      # 按时间范围查询
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_requests_path ON request_logs(path)')              # 按 URL 路径查询
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at)')     # 审计日志按时间查询

        conn.commit()

        # 迁移：为 rules 表添加增强功能的新字段（ALTER TABLE 方式，已有列时跳过）
        self._migrate_rules_table(cursor)

        # 写入默认配置项到 settings 表（仅首次写入，不覆盖已有值）
        for key, value in Config.DEFAULTS.items():
            cursor.execute(
                'INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)',
                (key, value)
            )
        conn.commit()

        # 创建默认场景（如果场景表为空）
        cursor.execute('SELECT COUNT(*) as cnt FROM scenes')
        count = cursor.fetchone()['cnt']
        if count == 0:
            cursor.execute(
                "INSERT INTO scenes (name, description, is_active) VALUES (?, ?, 1)",
                ('Default', 'Default scene')
            )
            conn.commit()

        # 创建默认管理员用户（如果用户表为空），密码 admin123 的 SHA256 哈希
        cursor.execute('SELECT COUNT(*) as cnt FROM users')
        user_count = cursor.fetchone()['cnt']
        if user_count == 0:
            admin_pass = hashlib.sha256('admin123'.encode('utf-8')).hexdigest()
            cursor.execute(
                "INSERT INTO users (username, password, role, display_name) VALUES (?, ?, ?, ?)",
                ('admin', admin_pass, 'super_admin', 'Super Admin')
            )
            conn.commit()

        conn.close()

    def _migrate_rules_table(self, cursor):
        """迁移 rules 表：添加增强功能所需的列。

        使用 ALTER TABLE 逐列添加，如果列已存在则忽略（sqlite3.OperationalError）。
        这种方式支持从旧版本平滑升级，无需删表重建。

        新增字段说明：
        - match_logic:       条件间逻辑关系（and/or），默认 and
        - response_body_type: 响应体类型（json/xml/html/text）
        - redirect_url:      重定向目标 URL（PRD 4.3.5）
        - set_cookies:        Set-Cookie 配置（JSON 数组，PRD 4.3.2）
        - cors_enabled:       是否启用每规则独立 CORS
        - cors_origin:        CORS 允许的来源
        - pre_script:         前置脚本代码（PRD 5.2）
        - post_script:        后置脚本代码（PRD 5.2）
        - version:            乐观锁版本号（PRD 12.4），每次更新自增
        """
        new_columns = [
            ('match_logic', 'TEXT DEFAULT \'and\''),
            ('response_body_type', 'TEXT DEFAULT \'json\''),
            ('redirect_url', 'TEXT DEFAULT \'\''),
            ('set_cookies', 'TEXT DEFAULT \'[]\''),
            ('cors_enabled', 'INTEGER DEFAULT 0'),
            ('cors_origin', 'TEXT DEFAULT \'*\''),
            ('pre_script', 'TEXT DEFAULT \'\''),
            ('post_script', 'TEXT DEFAULT \'\''),
            ('version', 'INTEGER DEFAULT 1'),
        ]
        for col_name, col_def in new_columns:
            try:
                cursor.execute('ALTER TABLE rules ADD COLUMN {} {}'.format(col_name, col_def))
            except sqlite3.OperationalError:
                pass  # 列已存在，跳过

    # ==================== 场景管理 (PRD 4.1) ====================

    def list_scenes(self):
        """列出所有场景，按激活状态降序、创建时间升序排列，包含规则数量。"""
        conn = self.get_conn()
        rows = conn.execute(
            'SELECT s.*, '
            '(SELECT COUNT(*) FROM rules r WHERE r.scene_id = s.id) as rule_count '
            'FROM scenes s ORDER BY s.is_active DESC, s.created_at ASC'
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_scene(self, scene_id):
        """根据 ID 查询单个场景。"""
        conn = self.get_conn()
        row = conn.execute('SELECT * FROM scenes WHERE id = ?', (scene_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

    def create_scene(self, name, description=''):
        """创建新场景。名称必须唯一，否则抛出异常。"""
        conn = self.get_conn()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO scenes (name, description) VALUES (?, ?)',
            (name, description)
        )
        conn.commit()
        scene_id = cursor.lastrowid
        conn.close()
        return scene_id

    def update_scene(self, scene_id, name=None, description=None):
        """更新场景信息，仅更新非 None 的字段。"""
        conn = self.get_conn()
        scene = conn.execute('SELECT * FROM scenes WHERE id = ?', (scene_id,)).fetchone()
        if not scene:
            conn.close()
            return False
        new_name = name if name is not None else scene['name']
        new_desc = description if description is not None else scene['description']
        conn.execute(
            'UPDATE scenes SET name = ?, description = ?, updated_at = datetime("now","localtime") WHERE id = ?',
            (new_name, new_desc, scene_id)
        )
        conn.commit()
        conn.close()
        return True

    def delete_scene(self, scene_id):
        """删除场景，同时将该场景下所有规则的 scene_id 置为 NULL（解绑但不删除规则）。"""
        conn = self.get_conn()
        # Unbind rules from this scene
        conn.execute('UPDATE rules SET scene_id = NULL WHERE scene_id = ?', (scene_id,))
        conn.execute('DELETE FROM scenes WHERE id = ?', (scene_id,))
        conn.commit()
        conn.close()

    def activate_scene(self, scene_id):
        """激活指定场景：先将所有场景设为未激活，再激活目标场景。保证全局只有一个活跃场景。"""
        conn = self.get_conn()
        conn.execute('UPDATE scenes SET is_active = 0')
        conn.execute('UPDATE scenes SET is_active = 1 WHERE id = ?', (scene_id,))
        conn.commit()
        conn.close()

    def get_active_scene(self):
        """获取当前激活的场景。"""
        conn = self.get_conn()
        row = conn.execute('SELECT * FROM scenes WHERE is_active = 1').fetchone()
        conn.close()
        return dict(row) if row else None

    # ==================== 规则管理 (PRD 4.2-4.3) ====================

    def list_rules(self, scene_id=None, enabled_only=False):
        """列出规则，支持按场景和启用状态筛选，按优先级和创建时间排序。"""
        conn = self.get_conn()
        sql = ('SELECT r.*, s.name as scene_name FROM rules r '
               'LEFT JOIN scenes s ON r.scene_id = s.id WHERE 1=1')
        params = []
        if scene_id is not None:
            sql += ' AND r.scene_id = ?'
            params.append(scene_id)
        if enabled_only:
            sql += ' AND r.enabled = 1'
        sql += ' ORDER BY r.priority ASC, r.created_at ASC'
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def find_duplicate_rule(self, method, url_pattern, exclude_id=None):
        """PRD 12.1: 按方法+路径查找重复规则，用于创建前冲突检测。

        Args:
            method: HTTP 方法
            url_pattern: URL 路径
            exclude_id: 排除的规则 ID（更新时排除自身）
        Returns:
            匹配的规则 dict 或 None
        """
        conn = self.get_conn()
        sql = 'SELECT * FROM rules WHERE method = ? AND url_pattern = ?'
        params = [method.upper(), url_pattern]
        if exclude_id:
            sql += ' AND id != ?'
            params.append(exclude_id)
        row = conn.execute(sql, params).fetchone()
        conn.close()
        return dict(row) if row else None

    # 规则字段列表：驱动 create_rule / update_rule / import_config 的动态 SQL 构建。
    # 添加新字段时只需在此列表中追加，对应方法会自动适配。
    RULE_FIELDS = [
        'name', 'description', 'scene_id', 'url_pattern', 'url_match_type',
        'method', 'match_conditions', 'match_logic', 'response_status',
        'response_headers', 'response_body', 'response_body_type',
        'delay_ms', 'timeout_enabled',
        'random_exception_enabled', 'random_exception_probability',
        'random_exception_status', 'redirect_url', 'set_cookies',
        'cors_enabled', 'cors_origin', 'pre_script', 'post_script',
        'priority', 'enabled',
    ]

    def create_rule(self, data):
        """动态创建规则：根据 data 中包含的字段构建 INSERT SQL。

        布尔类型字段（timeout_enabled、enabled 等）会自动转为 0/1。
        """
        conn = self.get_conn()
        cursor = conn.cursor()
        fields_present = [f for f in self.RULE_FIELDS if f in data]
        columns = ', '.join(fields_present)
        placeholders = ', '.join(['?' for _ in fields_present])
        values = []
        for f in fields_present:
            val = data[f]
            if f in ('timeout_enabled', 'random_exception_enabled', 'enabled', 'cors_enabled'):
                val = 1 if val else 0
            values.append(val)
        sql = 'INSERT INTO rules ({}) VALUES ({})'.format(columns, placeholders)
        cursor.execute(sql, values)
        conn.commit()
        rule_id = cursor.lastrowid
        conn.close()
        return rule_id

    def update_rule(self, rule_id, data):
        """动态更新规则：仅更新 data 中包含的字段。

        支持乐观锁：如果 data 中包含 version 字段，会先校验版本号是否匹配，
        不匹配则返回 False（防止并发覆盖）。
        每次成功更新后 version 自动 +1（PRD 12.4）。
        """
        conn = self.get_conn()
        rule = conn.execute('SELECT * FROM rules WHERE id = ?', (rule_id,)).fetchone()
        if not rule:
            conn.close()
            return False

        # PRD 12.4: 乐观锁校验 —— 版本号不匹配则拒绝更新
        if 'version' in data and data['version'] is not None:
            current_version = rule['version'] if 'version' in rule.keys() else 1
            if data['version'] != current_version:
                conn.close()
                return False

        updates = []
        params = []
        for field in self.RULE_FIELDS:
            if field in data:
                val = data[field]
                if field in ('timeout_enabled', 'random_exception_enabled', 'enabled', 'cors_enabled'):
                    val = 1 if val else 0
                updates.append('{} = ?'.format(field))
                params.append(val)

        if updates:
            updates.append('updated_at = datetime("now","localtime")')
            updates.append('version = version + 1')  # 乐观锁版本自增
            sql = 'UPDATE rules SET {} WHERE id = ?'.format(', '.join(updates))
            params.append(rule_id)
            conn.execute(sql, params)
            conn.commit()

        conn.close()
        return True

    def delete_rule(self, rule_id):
        conn = self.get_conn()
        conn.execute('DELETE FROM rules WHERE id = ?', (rule_id,))
        conn.commit()
        conn.close()

    def batch_update_rules(self, rule_ids, action):
        """批量操作规则。

        Args:
            rule_ids: 规则 ID 列表
            action: 操作类型 ('enable' 启用 / 'disable' 禁用 / 'delete' 删除)
        Returns:
            受影响的行数
        """
        conn = self.get_conn()
        if not rule_ids:
            conn.close()
            return 0
        placeholders = ','.join(['?' for _ in rule_ids])
        if action == 'enable':
            conn.execute(
                'UPDATE rules SET enabled = 1 WHERE id IN ({})'.format(placeholders),
                rule_ids
            )
        elif action == 'disable':
            conn.execute(
                'UPDATE rules SET enabled = 0 WHERE id IN ({})'.format(placeholders),
                rule_ids
            )
        elif action == 'delete':
            conn.execute(
                'DELETE FROM rules WHERE id IN ({})'.format(placeholders),
                rule_ids
            )
        conn.commit()
        count = conn.total_changes
        conn.close()
        return count

    def get_rules_for_matching(self, active_scene_id):
        """获取用于匹配的规则集：包含当前激活场景的规则 + 全局规则（scene_id IS NULL）。

        按优先级升序排列，优先级数字越小越先匹配。
        """
        conn = self.get_conn()
        sql = '''
            SELECT * FROM rules
            WHERE enabled = 1
            AND (scene_id = ? OR scene_id IS NULL)
            ORDER BY priority ASC, created_at ASC
        '''
        rows = conn.execute(sql, (active_scene_id,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def increment_hit_count(self, rule_id):
        conn = self.get_conn()
        conn.execute(
            'UPDATE rules SET hit_count = hit_count + 1 WHERE id = ?',
            (rule_id,)
        )
        conn.commit()
        conn.close()

    # ==================== 请求日志 (PRD 7.1) ====================

    def create_request_log(self, data):
        """记录一条请求日志，包含完整的请求和响应信息。"""
        conn = self.get_conn()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO request_logs (
                request_id, method, url, path, query_params, headers, body,
                matched_rule_id, matched_rule_name, match_result,
                response_status, response_body, response_headers,
                response_time_ms, scene_id, scene_name, client_ip
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            data.get('request_id', ''),
            data.get('method', ''),
            data.get('url', ''),
            data.get('path', ''),
            data.get('query_params', '{}'),
            data.get('headers', '{}'),
            data.get('body', ''),
            data.get('matched_rule_id'),
            data.get('matched_rule_name', ''),
            data.get('match_result', 'unmatched'),
            data.get('response_status', 0),
            data.get('response_body', ''),
            data.get('response_headers', '{}'),
            data.get('response_time_ms', 0),
            data.get('scene_id'),
            data.get('scene_name', ''),
            data.get('client_ip', ''),
        ))
        conn.commit()
        log_id = cursor.lastrowid
        conn.close()
        return log_id

    def get_request_log(self, request_id):
        """按请求 ID 查询单条请求日志（供自动化断言使用）。"""
        conn = self.get_conn()
        row = conn.execute(
            'SELECT * FROM request_logs WHERE request_id = ?',
            (request_id,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def list_request_logs(self, filters=None, page=1, per_page=20):
        """分页查询请求日志，支持多条件筛选。

        Args:
            filters: 筛选条件 dict，支持 url/method/status_code/client_ip/scene_id/match_result/start_time/end_time
            page: 页码（从 1 开始）
            per_page: 每页条数
        Returns:
            dict: items 列表 + total 总数 + 分页信息
        """
        if filters is None:
            filters = {}

        conn = self.get_conn()
        sql = 'SELECT * FROM request_logs WHERE 1=1'
        params = []

        if filters.get('url'):
            sql += ' AND path LIKE ?'
            params.append('%{}%'.format(filters['url']))
        if filters.get('method'):
            sql += ' AND method = ?'
            params.append(filters['method'])
        if filters.get('status_code'):
            sql += ' AND response_status = ?'
            params.append(int(filters['status_code']))
        if filters.get('client_ip'):
            sql += ' AND client_ip = ?'
            params.append(filters['client_ip'])
        if filters.get('scene_id'):
            sql += ' AND scene_id = ?'
            params.append(filters['scene_id'])
        if filters.get('match_result'):
            sql += ' AND match_result = ?'
            params.append(filters['match_result'])
        if filters.get('start_time'):
            sql += ' AND created_at >= ?'
            params.append(filters['start_time'])
        if filters.get('end_time'):
            sql += ' AND created_at <= ?'
            params.append(filters['end_time'])

        # Count total
        count_sql = sql.replace('SELECT *', 'SELECT COUNT(*) as total', 1)
        total = conn.execute(count_sql, params).fetchone()['total']

        # Pagination
        offset = (page - 1) * per_page
        sql += ' ORDER BY created_at DESC LIMIT ? OFFSET ?'
        params.extend([per_page, offset])

        rows = conn.execute(sql, params).fetchall()
        conn.close()

        return {
            'items': [dict(r) for r in rows],
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': (total + per_page - 1) // per_page if per_page > 0 else 0,
        }

    def cleanup_old_logs(self, days):
        """清理指定天数之前的请求日志。

        Args:
            days: 保留天数，超过此天数的日志将被删除
        Returns:
            删除的行数
        """
        cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
        conn = self.get_conn()
        cursor = conn.execute(
            'DELETE FROM request_logs WHERE created_at < ?',
            (cutoff,)
        )
        conn.commit()
        deleted = cursor.rowcount
        conn.close()
        return deleted

    def get_request_count_today(self):
        """获取今日请求总数。"""
        today = datetime.now().strftime('%Y-%m-%d')
        conn = self.get_conn()
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM request_logs WHERE created_at >= ?",
            (today + ' 00:00:00',)
        ).fetchone()
        conn.close()
        return row['cnt']

    def get_stats(self):
        """获取仪表盘统计数据：总请求数、今日请求、命中/转发/未匹配数、
        错误数、Top 10 热门规则、近 7 天趋势、当前激活场景、规则总数。
        """
        conn = self.get_conn()
        today = datetime.now().strftime('%Y-%m-%d')

        total_requests = conn.execute('SELECT COUNT(*) as cnt FROM request_logs').fetchone()['cnt']
        today_requests = conn.execute(
            'SELECT COUNT(*) as cnt FROM request_logs WHERE created_at >= ?',
            (today + ' 00:00:00',)
        ).fetchone()['cnt']
        mock_hits = conn.execute(
            "SELECT COUNT(*) as cnt FROM request_logs WHERE match_result = 'matched'"
        ).fetchone()['cnt']
        forwarded = conn.execute(
            "SELECT COUNT(*) as cnt FROM request_logs WHERE match_result = 'forwarded'"
        ).fetchone()['cnt']
        unmatched = conn.execute(
            "SELECT COUNT(*) as cnt FROM request_logs WHERE match_result = 'unmatched'"
        ).fetchone()['cnt']
        error_count = conn.execute(
            'SELECT COUNT(*) as cnt FROM request_logs WHERE response_status >= 400'
        ).fetchone()['cnt']

        # Top 10 rules by hit count
        top_rules = conn.execute(
            'SELECT name, hit_count FROM rules WHERE hit_count > 0 ORDER BY hit_count DESC LIMIT 10'
        ).fetchall()

        # Last 7 days trend
        trend = []
        for i in range(6, -1, -1):
            day = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
            count = conn.execute(
                'SELECT COUNT(*) as cnt FROM request_logs WHERE created_at >= ? AND created_at < ?',
                (day + ' 00:00:00', day + ' 23:59:59')
            ).fetchone()['cnt']
            trend.append({'date': day, 'count': count})

        # Active scene
        active_scene = conn.execute(
            'SELECT name FROM scenes WHERE is_active = 1'
        ).fetchone()

        # Rule count
        rule_count = conn.execute('SELECT COUNT(*) as cnt FROM rules').fetchone()['cnt']

        conn.close()

        return {
            'total_requests': total_requests,
            'today_requests': today_requests,
            'mock_hits': mock_hits,
            'forwarded': forwarded,
            'unmatched': unmatched,
            'error_count': error_count,
            'top_rules': [dict(r) for r in top_rules],
            'trend': trend,
            'active_scene': active_scene['name'] if active_scene else 'None',
            'rule_count': rule_count,
        }

    # ==================== 全局设置 (PRD 8) ====================

    def get_setting(self, key, default=None):
        """读取单个配置项。"""
        conn = self.get_conn()
        row = conn.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
        conn.close()
        return row['value'] if row else default

    def set_setting(self, key, value):
        """写入单个配置项（覆盖式）。"""
        conn = self.get_conn()
        conn.execute(
            'INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, datetime("now","localtime"))',
            (key, str(value))
        )
        conn.commit()
        conn.close()

    def get_all_settings(self):
        """读取所有配置项，返回 key->value 字典。"""
        conn = self.get_conn()
        rows = conn.execute('SELECT key, value FROM settings').fetchall()
        conn.close()
        result = {}
        for row in rows:
            result[row['key']] = row['value']
        return result

    def update_settings(self, settings_dict):
        """批量更新配置项。"""
        conn = self.get_conn()
        for key, value in settings_dict.items():
            conn.execute(
                'INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, datetime("now","localtime"))',
                (key, str(value))
            )
        conn.commit()
        conn.close()

    # ==================== 用户管理 (PRD 2.1-2.2) ====================

    def list_users(self):
        """列出所有用户（不返回密码字段）。"""
        conn = self.get_conn()
        rows = conn.execute(
            'SELECT id, username, role, display_name, email, last_login, created_at FROM users ORDER BY created_at ASC'
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_user(self, user_id=None, username=None):
        """按 ID 或用户名查询用户（返回包含密码哈希的完整信息）。"""
        conn = self.get_conn()
        if username:
            row = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        else:
            row = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

    def create_user(self, username, password, role='viewer', display_name='', email=''):
        """创建新用户，密码使用 SHA256 哈希后存储。"""
        conn = self.get_conn()
        hashed = hashlib.sha256(password.encode('utf-8')).hexdigest()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO users (username, password, role, display_name, email) VALUES (?, ?, ?, ?, ?)',
            (username, hashed, role, display_name, email)
        )
        conn.commit()
        user_id = cursor.lastrowid
        conn.close()
        return user_id

    def update_user(self, user_id, data):
        """更新用户信息（角色、名称、邮箱、密码），仅更新 data 中包含的字段。"""
        conn = self.get_conn()
        user = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        if not user:
            conn.close()
            return False
        updates = []
        params = []
        if 'role' in data:
            updates.append('role = ?')
            params.append(data['role'])
        if 'display_name' in data:
            updates.append('display_name = ?')
            params.append(data['display_name'])
        if 'email' in data:
            updates.append('email = ?')
            params.append(data['email'])
        if 'password' in data:
            hashed = hashlib.sha256(data['password'].encode('utf-8')).hexdigest()
            updates.append('password = ?')
            params.append(hashed)
        if updates:
            updates.append('updated_at = datetime("now","localtime")')
            sql = 'UPDATE users SET {} WHERE id = ?'.format(', '.join(updates))
            params.append(user_id)
            conn.execute(sql, params)
            conn.commit()
        conn.close()
        return True

    def delete_user(self, user_id):
        """删除用户，但 admin 账户不可删除（防止误删超级管理员）。"""
        conn = self.get_conn()
        conn.execute('DELETE FROM users WHERE id = ? AND username != ?', (user_id, 'admin'))
        conn.commit()
        conn.close()

    def verify_user(self, username, password):
        """验证用户凭据：对比 SHA256 哈希密码，验证成功时更新最后登录时间。"""
        conn = self.get_conn()
        hashed = hashlib.sha256(password.encode('utf-8')).hexdigest()
        row = conn.execute(
            'SELECT * FROM users WHERE username = ? AND password = ?',
            (username, hashed)
        ).fetchone()
        if row:
            conn.execute(
                'UPDATE users SET last_login = datetime("now","localtime") WHERE id = ?',
                (row['id'],)
            )
            conn.commit()
        conn.close()
        return dict(row) if row else None

    # ==================== 审计日志 (PRD 2.3) ====================

    def create_audit_log(self, user_id, username, action, target_type='', target_id='', detail='', ip=''):
        """记录一条审计日志，用于操作追溯和安全审计。"""
        conn = self.get_conn()
        conn.execute(
            'INSERT INTO audit_logs (user_id, username, action, target_type, target_id, detail, ip) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (user_id, username, action, target_type, str(target_id), detail, ip)
        )
        conn.commit()
        conn.close()

    def list_audit_logs(self, page=1, per_page=20, action_filter=''):
        """分页查询审计日志，支持按操作类型筛选。"""
        conn = self.get_conn()
        sql = 'SELECT * FROM audit_logs WHERE 1=1'
        params = []
        if action_filter:
            sql += ' AND action = ?'
            params.append(action_filter)
        count_sql = sql.replace('SELECT *', 'SELECT COUNT(*) as total', 1)
        total = conn.execute(count_sql, params).fetchone()['total']
        offset = (page - 1) * per_page
        sql += ' ORDER BY created_at DESC LIMIT ? OFFSET ?'
        params.extend([per_page, offset])
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return {
            'items': [dict(r) for r in rows],
            'total': total, 'page': page, 'per_page': per_page,
            'total_pages': (total + per_page - 1) // per_page if per_page > 0 else 0,
        }

    # ==================== 备份/恢复 (PRD 11) ====================

    def export_all_config(self):
        """导出全量配置（场景+规则+设置），用于配置备份和迁移。"""
        conn = self.get_conn()
        scenes = [dict(r) for r in conn.execute('SELECT * FROM scenes').fetchall()]
        rules = [dict(r) for r in conn.execute('SELECT * FROM rules').fetchall()]
        settings = [dict(r) for r in conn.execute('SELECT * FROM settings').fetchall()]
        conn.close()
        return {
            'export_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'scenes': scenes,
            'rules': rules,
            'settings': settings,
        }

    def import_config(self, config_data):
        """从备份数据恢复配置：导入场景、规则和设置。

        场景和设置使用 INSERT OR REPLACE（按主键覆盖），
        规则使用 INSERT（新增，避免 ID 冲突）。

        Returns:
            dict: 各表导入数量统计
        """
        conn = self.get_conn()
        imported = {'scenes': 0, 'rules': 0, 'settings': 0}
        if 'scenes' in config_data:
            for scene in config_data['scenes']:
                conn.execute(
                    'INSERT OR REPLACE INTO scenes (id, name, description, is_active) VALUES (?, ?, ?, ?)',
                    (scene.get('id'), scene.get('name'), scene.get('description', ''), scene.get('is_active', 0))
                )
                imported['scenes'] += 1
        if 'rules' in config_data:
            for rule in config_data['rules']:
                fields = [f for f in self.RULE_FIELDS if f in rule]
                columns = ', '.join(fields)
                placeholders = ', '.join(['?' for _ in fields])
                values = [rule[f] for f in fields]
                sql = 'INSERT INTO rules ({}) VALUES ({})'.format(columns, placeholders)
                conn.execute(sql, values)
                imported['rules'] += 1
        if 'settings' in config_data:
            for s in config_data['settings']:
                conn.execute(
                    'INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, datetime("now","localtime"))',
                    (s.get('key'), s.get('value'))
                )
                imported['settings'] += 1
        conn.commit()
        conn.close()
        return imported
