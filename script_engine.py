# -*- coding: utf-8 -*-
"""HTTP Mock Server - 脚本引擎（前置/后置脚本沙箱）

PRD 5.2: 前置脚本与后置脚本
- 前置脚本：在规则匹配后、响应生成前执行，可校验入参、JWT 鉴权、动态修改请求、
  或直接强制返回（跳过 Mock 响应流程）
- 后置脚本：在响应生成后、发送前执行，可运算组装响应、数据脱敏、修改状态码、抛异常等

安全机制：
- 使用受限的 Python 内置函数集合（SAFE_BUILTINS），禁止访问文件系统、子进程等危险操作
- 通过正则黑名单（FORBIDDEN_PATTERNS）拦截 import os / exec / eval / open 等危险调用
- 脚本通过 exec() 在受限全局命名空间中执行

脚本可用变量：
- 前置脚本：ctx（请求上下文）、request（同 ctx）、skip_mock、force_status、force_body、force_headers
- 后置脚本：ctx、request、response、status、headers、body、modified_status、modified_headers、modified_body
"""

import json
import re
import traceback
import datetime

# 受限的内置函数集合：仅允许安全的内置函数，屏蔽 os/sys/subprocess 等危险模块
SAFE_BUILTINS = {
    'abs': abs, 'all': all, 'any': any, 'bool': bool, 'dict': dict,
    'enumerate': enumerate, 'filter': filter, 'float': float,
    'hash': hash, 'int': int, 'isinstance': isinstance, 'len': len,
    'list': list, 'map': map, 'max': max, 'min': min, 'print': print,
    'range': range, 'round': round, 'set': set, 'sorted': sorted,
    'str': str, 'sum': sum, 'tuple': tuple, 'type': type, 'zip': zip,
    'True': True, 'False': False, 'None': None,
    'json': json, 're': re, 'datetime': datetime,   # 允许使用 json/re/datetime 模块
    'hasattr': hasattr, 'getattr': getattr, 'setattr': setattr,
    # 辅助函数：安全地获取 dict 的 keys/values/items
    'keys': lambda d: list(d.keys()) if isinstance(d, dict) else [],
    'values': lambda d: list(d.values()) if isinstance(d, dict) else [],
    'items': lambda d: list(d.items()) if isinstance(d, dict) else [],
    'isinstance': isinstance,
}


class ScriptResult(object):
    """脚本执行结果封装。

    前置脚本和后置脚本执行后返回此对象，调用方根据字段判断后续行为。

    前置脚本相关字段：
    - skip_mock: 是否跳过 Mock 响应，直接返回 forced_* 指定的内容
    - forced_status / forced_headers / forced_body: 强制返回的响应
    - modified_request: 动态修改后的请求（预留字段）

    后置脚本相关字段：
    - modified_status / modified_headers / modified_body: 修改后的响应

    通用字段：
    - success: 脚本是否执行成功
    - error: 错误信息（执行失败时）
    """

    def __init__(self):
        self.success = True
        self.error = ''
        self.skip_mock = False
        self.forced_status = None
        self.forced_headers = None
        self.forced_body = None
        self.modified_request = None
        self.modified_status = None
        self.modified_headers = None
        self.modified_body = None


class ScriptEngine(object):
    """脚本引擎：在受限环境中执行前置/后置脚本。

    安全策略：
    1. 正则黑名单：拦截 import os/sys/subprocess、exec、eval、open 等危险调用
    2. 编译检查：使用 compile() 验证语法正确性
    3. 受限执行：exec() 时仅传入 SAFE_BUILTINS，屏蔽危险内置函数
    """

    # 禁止的代码模式（正则黑名单）
    FORBIDDEN_PATTERNS = [
        r'import\s+os',
        r'import\s+sys',
        r'import\s+subprocess',
        r'import\s+shutil',
        r'__import__',
        r'exec\s*\(',
        r'eval\s*\(',
        r'open\s*\(',
        r'file\s*\(',
        r'compile\s*\(',
        r'globals\s*\(\s*\)',
        r'locals\s*\(\s*\)',
        r'__builtin__',
        r'__builtins__',
    ]

    def validate_script(self, script_code):
        """校验脚本安全性：检查危险模式 + 编译语法检查。

        Returns:
            tuple: (is_valid, error_message)
        """
        if not script_code or not script_code.strip():
            return True, ''
        for pattern in self.FORBIDDEN_PATTERNS:
            if re.search(pattern, script_code):
                return False, 'Forbidden pattern: {}'.format(pattern)
        try:
            compile(script_code, '<script>', 'exec')
        except SyntaxError as e:
            return False, 'Syntax error line {}: {}'.format(e.lineno, e.msg)
        return True, ''

    def execute_pre_script(self, script_code, ctx):
        """执行前置脚本。

        前置脚本在规则匹配成功后、响应生成前执行。
        脚本可通过设置变量来控制行为：
        - skip_mock = True: 跳过 Mock 响应，使用 force_status/force_body/force_headers 直接返回
        - force_status: 强制返回的状态码
        - force_body: 强制返回的响应体
        - force_headers: 强制返回的响应头
        - modify_request: 修改后的请求（预留）

        Args:
            script_code: 脚本代码字符串
            ctx: 请求上下文字典

        Returns:
            ScriptResult: 执行结果
        """
        result = ScriptResult()
        if not script_code or not script_code.strip():
            return result

        is_valid, error = self.validate_script(script_code)
        if not is_valid:
            result.success = False
            result.error = error
            result.skip_mock = True
            result.forced_status = 500
            result.forced_body = json.dumps({'error': 'Script validation failed', 'message': error})
            result.forced_headers = {'Content-Type': 'application/json'}
            return result

        # 脚本可用变量：脚本代码中可直接读写这些变量
        script_vars = {
            'ctx': ctx, 'request': ctx,         # 请求上下文
            'skip_mock': False, 'force_status': None,
            'force_body': None, 'force_headers': None,
            'modify_request': None,
        }
        # 构建受限全局命名空间：仅包含安全内置函数 + 脚本变量
        restricted_globals = {'__builtins__': SAFE_BUILTINS}
        restricted_globals.update(script_vars)

        try:
            # 在受限环境中执行脚本
            exec(script_code, restricted_globals)
        except Exception as e:
            # 脚本执行异常：返回 500 错误
            result.success = False
            result.error = str(e)
            result.skip_mock = True
            result.forced_status = 500
            result.forced_body = json.dumps({'error': 'Pre-script error', 'message': str(e)})
            result.forced_headers = {'Content-Type': 'application/json'}
            return result

        # 从脚本变量中提取执行结果
        result.skip_mock = script_vars.get('skip_mock', False)
        result.forced_status = script_vars.get('force_status')
        result.forced_body = script_vars.get('force_body')
        result.forced_headers = script_vars.get('force_headers')
        result.modified_request = script_vars.get('modify_request')
        return result

    def execute_post_script(self, script_code, ctx, response_data):
        """执行后置脚本。

        后置脚本在响应生成后、发送前执行。
        脚本可通过设置变量来修改响应：
        - modified_status: 修改后的状态码
        - modified_headers: 修改后的响应头
        - modified_body: 修改后的响应体

        Args:
            script_code: 脚本代码字符串
            ctx: 请求上下文字典
            response_data: 响应数据 dict {'status', 'headers', 'body'}

        Returns:
            ScriptResult: 执行结果
        """
        result = ScriptResult()
        if not script_code or not script_code.strip():
            return result

        is_valid, error = self.validate_script(script_code)
        if not is_valid:
            result.success = False
            result.error = error
            return result

        # 后置脚本可用变量：脚本代码中可直接读写
        script_vars = {
            'ctx': ctx, 'request': ctx,           # 请求上下文
            'response': response_data,             # 完整响应数据
            'status': response_data.get('status', 200),    # 当前状态码
            'headers': response_data.get('headers', {}),    # 当前响应头
            'body': response_data.get('body', ''),          # 当前响应体
            'modified_status': None,                # 脚本设置后用于覆盖状态码
            'modified_headers': None,               # 脚本设置后用于覆盖响应头
            'modified_body': None,                  # 脚本设置后用于覆盖响应体
        }
        restricted_globals = {'__builtins__': SAFE_BUILTINS}
        restricted_globals.update(script_vars)

        try:
            exec(script_code, restricted_globals)
        except Exception as e:
            # 脚本异常：不修改响应，仅记录错误
            result.success = False
            result.error = str(e)
            return result

        # 从脚本变量中提取修改后的值
        result.modified_status = script_vars.get('modified_status')
        result.modified_headers = script_vars.get('modified_headers')
        result.modified_body = script_vars.get('modified_body')
        return result

    @staticmethod
    def mask_field(value, mask_type='default'):
        """PRD 7.3: 对敏感字段进行脱敏处理。

        支持的脱敏类型：
        - phone:    手机号脱敏，如 138****5678
        - id_card:  身份证脱敏，如 110101********1234
        - email:    邮箱脱敏，如 u***@example.com
        - token/key/secret: 令牌类脱敏，仅显示后 4 位
        - default:  通用脱敏，保留首尾字符

        Args:
            value: 原始值
            mask_type: 脱敏类型（可自动推断）

        Returns:
            脱敏后的字符串
        """
        if not value or not isinstance(value, str):
            return value
        s = str(value)
        if mask_type == 'phone' or (len(s) == 11 and s.isdigit() and s.startswith('1')):
            if len(s) >= 7:
                return s[:3] + '****' + s[-4:]
        if mask_type == 'id_card' or (len(s) == 18 and s[:-1].isdigit()):
            if len(s) >= 10:
                return s[:6] + '********' + s[-4:]
        if mask_type == 'email' or '@' in s:
            parts = s.split('@')
            if len(parts) == 2 and len(parts[0]) > 1:
                return parts[0][0] + '***@' + parts[1]
        if mask_type in ('token', 'key', 'secret') or any(k in s.lower() for k in ('token', 'key', 'secret', 'password', 'authorization')):
            if len(s) > 4:
                return '****' + s[-4:]
        if len(s) > 2:
            return s[0] + '*' * (len(s) - 2) + s[-1]
        return s

    @staticmethod
    def desensitize_data(data, fields=None):
        """递归对数据中的敏感字段进行脱敏。

        自动识别的敏感字段关键词：
        phone/mobile/tel, id_card/identity, token/secret/password,
        authorization/auth/key/apikey, email/mail

        Args:
            data: 原始数据（dict / list / 标量）
            fields: 额外指定需要脱敏的字段名集合

        Returns:
            脱敏后的数据（同类型结构）
        """
        sensitive_keywords = [
            'phone', 'mobile', 'tel', 'telephone',
            'id_card', 'idcard', 'identity',
            'token', 'secret', 'password', 'passwd', 'pwd',
            'authorization', 'auth', 'key', 'apikey',
            'email', 'mail',
        ]
        if isinstance(data, dict):
            result = {}
            for k, v in data.items():
                key_lower = k.lower()
                should_mask = False
                if fields and k in fields:
                    should_mask = True
                elif any(kw in key_lower for kw in sensitive_keywords):
                    should_mask = True
                if should_mask and isinstance(v, str):
                    result[k] = ScriptEngine.mask_field(v)
                elif isinstance(v, (dict, list)):
                    result[k] = ScriptEngine.desensitize_data(v, fields)
                else:
                    result[k] = v
            return result
        elif isinstance(data, list):
            return [ScriptEngine.desensitize_data(item, fields) for item in data]
        return data
