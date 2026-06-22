# -*- coding: utf-8 -*-
"""HTTP Mock Server - 全局配置模块

负责管理应用级别的配置参数，包括：
- 服务器监听地址和端口（支持环境变量覆盖）
- SQLite 数据库文件路径
- 代理转发超时与 Mock 超时模拟时长
- 数据库 settings 表的默认初始值（代理、CORS、脱敏、认证等）

所有配置均优先从环境变量读取，环境变量不存在时使用内置默认值，
方便在 Docker / 不同环境中灵活部署。
"""

import os
import json

# 项目根目录（用于拼接数据库文件等相对路径）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class Config(object):
    """全局配置类

    所有属性均为类属性（非实例属性），可在全局直接引用 Config.XXX。
    支持通过环境变量覆盖，环境变量名以 MOCK_ 为前缀。
    """

    # ---- 服务器基础设置 ----
    HOST = os.environ.get('MOCK_HOST', '0.0.0.0')       # 监听地址，默认所有网卡
    PORT = int(os.environ.get('MOCK_PORT', '5000'))      # 监听端口
    DEBUG = os.environ.get('MOCK_DEBUG', 'false').lower() == 'true'  # 调试模式

    # ---- 数据库设置 ----
    # SQLite 数据库文件路径，默认存放在项目根目录
    DATABASE = os.environ.get('MOCK_DB_PATH', os.path.join(BASE_DIR, 'mock_server.db'))

    # ---- 代理转发设置 ----
    # 代理转发请求的超时时间（秒），超过此时间将返回 504
    PROXY_TIMEOUT = int(os.environ.get('MOCK_PROXY_TIMEOUT', '30'))

    # ---- Mock 超时模拟设置 ----
    # 当规则启用 timeout_enabled 时，模拟网关超时的等待时间（秒），默认 5 分钟
    MOCK_TIMEOUT_DURATION = int(os.environ.get('MOCK_TIMEOUT_DURATION', '300'))

    # ---- 数据库 settings 表的默认值 ----
    # 这些值在数据库初始化时写入 settings 表，用户可通过管理界面动态修改。
    # 对应 PRD 各章节中的全局配置项。
    DEFAULTS = {
        # 代理转发相关 (PRD 4.4)
        'proxy_enabled': 'false',                        # 是否启用未匹配请求的代理转发
        'proxy_target': '',                               # 代理目标地址（如 http://real-server:8080）
        'proxy_strip_mock_header': 'true',                # 转发时是否去除 X-Mock-* 前缀的 Header
        'proxy_strip_headers': '',                        # 转发时需要额外剥离的 Header（逗号分隔）
        'proxy_rewrite_headers': '{}',                     # 转发时需要改写/追加的 Header（JSON 格式）
        'proxy_add_params': '{}',                          # 转发时需要追加的 Query 参数（JSON 格式）
        'proxy_timeout': '30',                             # 代理转发自定义超时（秒）

        # 默认响应（当请求未匹配任何规则且未开启代理时返回）
        'default_response_status': '404',
        'default_response_body': json.dumps({
            'code': 404,
            'message': 'No matching mock rule found'
        }, ensure_ascii=False),
        'default_response_headers': json.dumps({
            'Content-Type': 'application/json'
        }, ensure_ascii=False),

        # 日志与数据管理 (PRD 7.1)
        'request_retention_days': '7',                    # 请求日志保留天数，超过自动清理

        # 跨域设置 (PRD 4.3.2)
        'cors_enabled': 'false',                          # 是否全局开启 CORS

        # 请求体大小限制
        'max_body_size': '10485760',                      # 最大请求体 10MB

        # 脱敏设置 (PRD 7.3)
        'desensitize_enabled': 'true',                    # 是否在请求日志中对敏感字段脱敏

        # 认证设置 (PRD 2.1)
        'auth_enabled': 'false',                          # 是否开启管理后台登录认证
        'session_timeout': '1800',                        # 会话超时时间（秒），默认 30 分钟
    }
