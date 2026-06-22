# -*- coding: utf-8 -*-
"""HTTP Mock Server - 代理转发模块

PRD 4.4: 反向代理兜底能力
当请求未匹配任何 Mock 规则时，可选择将请求转发到真实后端服务器。
支持：
- 转发前改写请求 Header（追加/覆盖指定 Header）
- 指定 Header 不透传（剥离 X-Mock-* 前缀和自定义黑名单）
- 追加 Query 参数
- 自定义代理超时时间
- 响应过滤 hop-by-hop Header

代理转发流程：
未匹配请求 → 读取代理配置 → 构建转发请求 → 发送到目标 → 过滤响应 → 返回
"""

import json
import requests
from config import Config


class RequestProxy(object):
    """代理转发器：将未匹配的请求转发到真实后端服务器。

    Attributes:
        db: Database 实例，用于读取代理配置
    """

    # HTTP/1.1 逐跳头（hop-by-hop headers）：这些头在代理转发时不应透传
    HOP_BY_HOP_HEADERS = (
        'host', 'content-length', 'transfer-encoding',
        'connection', 'keep-alive', 'proxy-authenticate',
        'proxy-authorization', 'te', 'trailers', 'upgrade',
    )

    def __init__(self, db):
        self.db = db

    def forward(self, method, path, headers, query_params, body, original_url):
        """将请求转发到真实后端服务器。

        转发前的处理：
        1. 剥离 hop-by-hop 头和 X-Mock-* 前缀头
        2. 剥离用户配置的 strip_headers
        3. 追加/覆盖用户配置的 rewrite_headers
        4. 设置 Host 头为目标域名
        5. 合并用户配置的额外 Query 参数

        Args:
            method: HTTP 方法
            path: URL 路径
            headers: 原始请求头
            query_params: 原始查询参数
            body: 原始请求体
            original_url: 原始完整 URL

        Returns:
            tuple: (status_code, headers_dict, response_body) 或 None（未配置代理）
        """
        target = self.db.get_setting('proxy_target', '')
        if not target:
            return None  # 未配置代理目标，返回 None 让调用方走默认响应

        target = target.rstrip('/')
        url = target + path

        # 读取代理配置
        strip_mock_header = self.db.get_setting('proxy_strip_mock_header', 'true') == 'true'
        custom_timeout = int(self.db.get_setting('proxy_timeout', str(Config.PROXY_TIMEOUT)))

        # 解析需要剥离的 Header 黑名单（逗号分隔）
        strip_headers_str = self.db.get_setting('proxy_strip_headers', '')
        strip_headers = set()
        if strip_headers_str:
            for h in strip_headers_str.split(','):
                h = h.strip().lower()
                if h:
                    strip_headers.add(h)

        # 解析需要改写/追加的 Header（JSON 格式）
        rewrite_headers = {}
        try:
            rewrite_headers = json.loads(self.db.get_setting('proxy_rewrite_headers', '{}'))
        except (json.JSONDecodeError, ValueError):
            rewrite_headers = {}

        # 构建转发 Header：剥离 hop-by-hop、X-Mock-* 和黑名单 Header
        forward_headers = {}
        for key, value in headers.items():
            key_lower = key.lower()
            if key_lower in self.HOP_BY_HOP_HEADERS:
                continue  # 跳过逐跳头
            if strip_mock_header and key_lower.startswith('x-mock'):
                continue  # 剥离 Mock 前缀头
            if key_lower in strip_headers:
                continue  # 剥离用户指定的 Header
            forward_headers[key] = value

        # 追加/覆盖改写 Header
        for key, value in rewrite_headers.items():
            forward_headers[key] = value

        # 设置 Host 头为目标服务器域名
        from urllib.parse import urlparse
        parsed = urlparse(target)
        forward_headers['Host'] = parsed.netloc

        # 合并额外 Query 参数
        add_params = {}
        try:
            add_params = json.loads(self.db.get_setting('proxy_add_params', '{}'))
        except (json.JSONDecodeError, ValueError):
            add_params = {}

        merged_params = dict(query_params) if query_params else {}
        merged_params.update(add_params)

        try:
            # 发送转发请求（不跟随重定向，交由 Mock 层处理）
            response = requests.request(
                method=method.upper(),
                url=url,
                headers=forward_headers,
                params=merged_params if merged_params else None,
                data=body if body else None,
                timeout=custom_timeout,
                allow_redirects=False,
            )

            # 过滤响应头：移除 hop-by-hop 和编码相关头
            resp_headers = {}
            for key, value in response.headers.items():
                if key.lower() not in ('content-length', 'transfer-encoding',
                                       'connection', 'keep-alive', 'content-encoding'):
                    resp_headers[key] = value

            return (response.status_code, resp_headers, response.text)

        except requests.exceptions.Timeout:
            # 代理超时
            return (504, {'Content-Type': 'application/json'},
                    json.dumps({'error': 'Gateway Timeout',
                                'message': 'Proxy target did not respond within {}s'.format(custom_timeout)},
                               ensure_ascii=False))
        except requests.exceptions.ConnectionError as e:
            # 连接失败
            return (502, {'Content-Type': 'application/json'},
                    json.dumps({'error': 'Bad Gateway',
                                'message': 'Failed to connect to proxy target: {}'.format(str(e))},
                               ensure_ascii=False))
        except Exception as e:
            # 其他异常
            return (500, {'Content-Type': 'application/json'},
                    json.dumps({'error': 'Internal Server Error',
                                'message': 'Proxy error: {}'.format(str(e))},
                               ensure_ascii=False))

    def test_connectivity(self, target_url):
        """PRD 14.5: 测试代理目标服务器连通性。

        发送一个 GET 请求到目标地址，返回可达性、状态码和响应时间。
        """
        try:
            resp = requests.get(target_url, timeout=5, allow_redirects=False)
            return {
                'reachable': True,
                'status_code': resp.status_code,
                'response_time_ms': int(resp.elapsed.total_seconds() * 1000),
            }
        except requests.exceptions.Timeout:
            return {'reachable': False, 'error': 'Connection timeout'}
        except requests.exceptions.ConnectionError as e:
            return {'reachable': False, 'error': 'Connection failed: {}'.format(str(e))}
        except Exception as e:
            return {'reachable': False, 'error': str(e)}
