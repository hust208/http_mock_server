# -*- coding: utf-8 -*-
"""HTTP Mock Server - 动态变量模板引擎

PRD 5.1: 响应体动态变量渲染
支持在 Mock 响应体中使用 {{$variable}} 语法插入动态生成的值，
以及 {{request.xxx}} 或 {{$request.xxx}} 语法回显请求入参。

系统内置变量（20+）：
  时间类: {{$timestamp}}, {{$datetime}}, {{$date}}, {{$time}}, {{$year}} ...
  随机类: {{$random_number}}, {{$random_string}}, {{$random_phone}}, {{$random_id_card}},
          {{$random_email}}, {{$random_name}}, {{$random_ip}}, {{$random_bool}},
          {{$random_item(a,b,c)}}
  其他:   {{$uuid}}, {{$increment_id}}

入参回显变量：
  {{$request.path}}         - 请求路径
  {{$request.query.field}}  - 查询参数
  {{$request.header.field}} - 请求头
  {{$request.body.field}}   - 请求体（支持点号嵌套）
  {{$request.cookie.field}} - Cookie
  {{$request.path_var.id}}  - 路径变量

用法示例：
  模板: {"id": "{{$uuid}}", "name": "{{$random_name}}", "phone": "{{$random_phone}}"}
  渲染后: {"id": "a1b2c3d4-...", "name": "张伟", "phone": "13812345678"}
"""

import re
import time
import uuid
import random
import string
from datetime import datetime


class TemplateEngine(object):
    """模板引擎：将包含动态变量占位符的文本渲染为最终结果。

    支持两种变量语法：
    1. 系统内置变量: {{$variable}} 或 {{$variable(param)}}
    2. 入参回显变量: {{request.xxx}} 或 {{$request.xxx}}（兼容两种写法）
    """

    # 系统变量正则：匹配 {{$variable}} 或 {{$variable(param)}}
    # 例：{{$timestamp}}, {{$random_number(1,100)}}, {{$date(%Y-%m-%d)}}
    VAR_PATTERN = re.compile(r'\{\{\$(\w+)(?:\(([^)]*)\))?\}\}')

    # 入参回显正则：匹配 {{request.xxx}} 或 {{$request.xxx}}（兼容两种语法）
    # $? 表示 $ 可有可无，确保两种写法都能匹配
    ECHO_PATTERN = re.compile(r'\{\{\$?request\.([\w.]+)\}\}')

    def render(self, template_text, request_ctx=None):
        """渲染模板：将动态变量占位符替换为实际值。

        渲染顺序：
        1. 先替换系统内置变量 {{$var}}
        2. 再替换入参回显变量 {{request.xxx}}

        Args:
            template_text: 模板字符串，包含 {{$var}} 和 {{request.xxx}} 占位符
            request_ctx: 请求上下文字典，包含：
                - path: 请求路径
                - method: HTTP 方法
                - query_params: 查询参数字典
                - headers: 请求头字典
                - body_json: 解析后的 JSON 请求体（可能为 None）
                - body_text: 原始请求体文本
                - cookies: Cookie 字典
                - path_vars: 路径变量字典

        Returns:
            渲染后的字符串（占位符已被替换为实际值）
        """
        if not template_text:
            return template_text

        if request_ctx is None:
            request_ctx = {}

        result = template_text

        # 1. Replace system built-in variables
        result = self.VAR_PATTERN.sub(
            lambda m: self._replace_var(m, request_ctx),
            result
        )

        # 2. Replace input echo variables
        result = self.ECHO_PATTERN.sub(
            lambda m: self._replace_echo(m, request_ctx),
            result
        )

        return result

    def _replace_var(self, match, ctx):
        """替换单个 {{$variable}} 占位符。

        根据 var_name 从 handlers 字典中查找对应的生成函数并调用。
        如果变量名未知则保留原占位符文本。
        """
        var_name = match.group(1)   # 变量名，如 timestamp
        param = match.group(2) or ''  # 可选参数，如 random_number(1,100) 中的 "1,100"

        # 系统内置变量生成器映射表
        handlers = {
            'timestamp': lambda: str(int(time.time())),
            'timestamp_ms': lambda: str(int(time.time() * 1000)),
            'uuid': lambda: str(uuid.uuid4()),
            'uuid_short': lambda: str(uuid.uuid4()).replace('-', ''),
            'random_number': lambda: self._random_number(param),
            'random_string': lambda: self._random_string(param),
            'random_phone': lambda: self._random_phone(),
            'random_id_card': lambda: self._random_id_card(),
            'random_email': lambda: self._random_email(),
            'random_name': lambda: self._random_name(),
            'date': lambda: datetime.now().strftime(param if param else '%Y-%m-%d'),
            'time': lambda: datetime.now().strftime(param if param else '%H:%M:%S'),
            'datetime': lambda: datetime.now().strftime(param if param else '%Y-%m-%d %H:%M:%S'),
            'year': lambda: str(datetime.now().year),
            'month': lambda: str(datetime.now().month).zfill(2),
            'day': lambda: str(datetime.now().day).zfill(2),
            'hour': lambda: str(datetime.now().hour).zfill(2),
            'minute': lambda: str(datetime.now().minute).zfill(2),
            'second': lambda: str(datetime.now().second).zfill(2),
            'random_item': lambda: self._random_item(param),
            'random_bool': lambda: str(random.choice([True, False])),
            'random_ip': lambda: self._random_ip(),
            'increment_id': lambda: self._increment_id(),
        }

        handler = handlers.get(var_name)
        if handler:
            try:
                return str(handler())
            except Exception:
                return match.group(0)  # 生成失败时保留原占位符

        # 未知变量，保留原占位符不替换
        return match.group(0)

    def _replace_echo(self, match, ctx):
        """替换单个 {{request.xxx}} 入参回显占位符。

        支持的路径前缀：
        - request.path / request.method / request.body: 直接取请求上下文
        - request.query.field:   从查询参数取值
        - request.header.field:  从请求头取值（大小写不敏感）
        - request.body.field:    从 JSON Body 取值（支持点号嵌套）
        - request.cookie.field:  从 Cookie 取值
        - request.path_var.field: 从路径变量取值
        """
        path = match.group(1)

        if path == 'path':
            return str(ctx.get('path', ''))
        elif path == 'method':
            return str(ctx.get('method', ''))
        elif path == 'body':
            return str(ctx.get('body_text', ''))
        elif path.startswith('query.'):
            field = path[6:]
            query = ctx.get('query_params', {})
            val = query.get(field, '')
            return str(val) if val is not None else ''
        elif path.startswith('header.'):
            field = path[7:]
            headers = ctx.get('headers', {})
            for k, v in headers.items():
                if k.lower() == field.lower():
                    return str(v)
            return ''
        elif path.startswith('body.'):
            field = path[5:]
            body_json = ctx.get('body_json')
            if body_json and isinstance(body_json, dict):
                val = self._get_nested(body_json, field)
                return str(val) if val is not None else ''
            return ''
        elif path.startswith('cookie.'):
            field = path[7:]
            cookies = ctx.get('cookies', {})
            val = cookies.get(field, '')
            return str(val) if val is not None else ''
        elif path.startswith('path_var.'):
            field = path[9:]
            path_vars = ctx.get('path_vars', {})
            val = path_vars.get(field, '')
            return str(val) if val is not None else ''

        return match.group(0)

    @staticmethod
    def _get_nested(data, field_path):
        """使用点号表示法从嵌套 dict/list 中取值（与 matcher 中实现一致）。"""
        if not field_path:
            return data
        keys = field_path.split('.')
        current = data
        for key in keys:
            if isinstance(current, dict) and key in current:
                current = current[key]
            elif isinstance(current, list):
                try:
                    idx = int(key)
                    if 0 <= idx < len(current):
                        current = current[idx]
                    else:
                        return None
                except ValueError:
                    return None
            else:
                return None
        return current

    @staticmethod
    def _random_number(param):
        """生成随机数字。

        参数格式：
        - 无参数: 返回 0-999999 的随机数
        - 'min,max': 返回 [min, max] 区间随机整数
        - 'length': 返回指定长度的随机数字串
        """
        if not param:
            return str(random.randint(0, 999999))
        if ',' in param:
            parts = param.split(',')
            return str(random.randint(int(parts[0]), int(parts[1])))
        else:
            length = int(param)
            return ''.join([str(random.randint(0, 9)) for _ in range(length)])

    @staticmethod
    def _random_string(param):
        """生成随机字符串（字母+数字），参数为长度，默认 8。"""
        length = int(param) if param else 8
        return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

    @staticmethod
    def _random_phone():
        """生成随机中国大陆手机号（合法号段开头 + 8位随机数字）。"""
        prefixes = ['130', '131', '132', '133', '134', '135', '136', '137',
                     '138', '139', '150', '151', '152', '153', '155', '156',
                     '158', '159', '170', '176', '177', '178', '180', '181',
                     '182', '183', '184', '185', '186', '187', '188', '189',
                     '191', '198', '199']
        prefix = random.choice(prefixes)
        suffix = ''.join([str(random.randint(0, 9)) for _ in range(8)])
        return prefix + suffix

    @staticmethod
    def _random_id_card():
        """生成随机中国身份证号（18位，简化校验位）。"""
        # Region code (Beijing)
        region = random.choice(['110101', '310101', '440101', '440301', '510101'])
        # Birth date
        year = random.randint(1960, 2005)
        month = random.randint(1, 12)
        day = random.randint(1, 28)
        birth = '{:04d}{:02d}{:02d}'.format(year, month, day)
        # Sequence
        seq = '{:03d}'.format(random.randint(0, 999))
        # Check digit (simplified)
        check = str(random.randint(0, 9))
        return region + birth + seq + check

    @staticmethod
    def _random_email():
        """生成随机邮箱地址。"""
        domains = ['gmail.com', 'outlook.com', 'qq.com', '163.com', 'foxmail.com']
        name_len = random.randint(6, 12)
        name = ''.join(random.choices(string.ascii_lowercase + string.digits, k=name_len))
        return '{}@{}'.format(name, random.choice(domains))

    @staticmethod
    def _random_name():
        """生成随机中文姓名（常见姓氏 + 1-2 字名）。"""
        surnames = list('赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜')
        given_chars = list('伟芳娜秀英敏静丽强磊军洋勇艳杰娟涛明超秀霞平刚桂英华健鑫颖琳玉萍红娥玲芬芳燕彩春金')
        surname = random.choice(surnames)
        given = ''.join(random.choices(given_chars, k=random.randint(1, 2)))
        return surname + given

    @staticmethod
    def _random_item(param):
        """从逗号分隔的列表中随机选取一项。参数示例: 'A,B,C'。"""
        if not param:
            return ''
        items = [item.strip() for item in param.split(',')]
        return random.choice(items)

    @staticmethod
    def _random_ip():
        """生成随机 IP 地址。"""
        return '{}.{}.{}.{}'.format(
            random.randint(1, 255),
            random.randint(0, 255),
            random.randint(0, 255),
            random.randint(1, 255)
        )

    _id_counter = 0  # 自增 ID 计数器（类级别，全局递增）

    @classmethod
    def _increment_id(cls):
        """生成自增 ID（每次调用 +1，进程级递增，非持久化）。"""
        cls._id_counter += 1
        return str(cls._id_counter)

    def render_response(self, rule, request_ctx):
        """完整的响应渲染管线：模板变量渲染 + 响应头解析 + Set-Cookie + 重定向 + CORS。

        Args:
            rule: 匹配到的规则 dict
            request_ctx: 请求上下文

        Returns:
            tuple: (status, headers, body) 三元组
        """
        body = rule.get('response_body', '')

        # Render template variables
        body = self.render(body, request_ctx)

        # Parse response headers
        try:
            import json
            headers = json.loads(rule.get('response_headers', '{}'))
        except (ValueError, TypeError):
            headers = {'Content-Type': 'application/json'}

        status = rule.get('response_status', 200)

        # Handle Set-Cookie
        try:
            import json
            set_cookies = json.loads(rule.get('set_cookies', '[]'))
            if set_cookies and isinstance(set_cookies, list):
                headers['_set_cookies'] = set_cookies
        except (ValueError, TypeError):
            pass

        # Handle redirect
        redirect_url = rule.get('redirect_url', '')
        if redirect_url and status in (301, 302, 303, 307, 308):
            # Render redirect URL with variables
            redirect_url = self.render(redirect_url, request_ctx)
            headers['Location'] = redirect_url

        # Per-rule CORS
        if rule.get('cors_enabled'):
            headers['Access-Control-Allow-Origin'] = rule.get('cors_origin', '*')
            headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, PATCH, OPTIONS, HEAD'
            headers['Access-Control-Allow-Headers'] = '*'

        return status, headers, body
