# -*- coding: utf-8 -*-
"""HTTP Mock Server - 规则匹配引擎

PRD 4.2: 分支匹配规则引擎
- 匹配数据源: Query/Form/JSON Body/Header/Cookie/路径变量
- 匹配运算符: 等于/不等于/包含/不包含/正则/为空/非空/数值大于小于/in/not_in
- 逻辑关系: 单分支内多条件支持且/或逻辑组合（and/or）
- 路径变量: 支持 {id} 格式路径变量提取，如 /api/users/{id}/posts/{postId}

匹配流程：
  1. 获取当前激活场景 → 2. 获取该场景+全局的启用规则 → 3. 按优先级遍历规则
  → 4. 每条规则依次校验：HTTP方法 → URL匹配 → 条件匹配
  → 5. 首条匹配的规则即为最终结果（先到先得）
"""

import re
import json
import random


class RuleMatcher(object):
    """请求匹配引擎：将收到的 HTTP 请求与配置的 Mock 规则逐一匹配。

    Attributes:
        db: Database 实例，用于获取规则和场景数据
    """

    def __init__(self, db):
        self.db = db

    def match(self, method, path, headers, query_params, body_text, body_json, cookies=None):
        """将请求与所有启用的规则进行匹配。

        匹配策略：按优先级从高到低遍历规则，返回第一条匹配的规则。
        规则范围：当前激活场景的规则 + 全局规则（scene_id IS NULL）。

        Args:
            method: HTTP 方法（GET/POST/...）
            path: URL 路径（如 /api/users/123）
            headers: 请求头字典
            query_params: 查询参数字典
            body_text: 原始请求体文本
            body_json: 解析后的 JSON 请求体（可能为 None）
            cookies: Cookie 字典（可选）

        Returns:
            tuple: (matched_rule_dict, path_vars_dict) 或 (None, {})
            其中 path_vars 包含从 URL 中提取的路径变量，如 {'id': '123'}
        """
        active_scene = self.db.get_active_scene()
        active_scene_id = active_scene['id'] if active_scene else 0

        rules = self.db.get_rules_for_matching(active_scene_id)

        for rule in rules:
            path_vars = {}
            if self._match_rule(rule, method, path, headers, query_params, body_text, body_json, cookies, path_vars):
                return rule, path_vars

        return None, {}

    def _match_rule(self, rule, method, path, headers, query_params, body_text, body_json, cookies, path_vars):
        """校验单条规则是否匹配当前请求。

        匹配步骤：
        1. HTTP 方法匹配（支持 ANY 通配）
        2. URL 路径匹配（含路径变量提取）
        3. 匹配条件校验（多条件 AND/OR 逻辑组合）

        Returns:
            bool: 是否匹配成功
        """
        # 1. 匹配 HTTP 方法（ANY 表示匹配所有方法）
        if rule['method'].upper() != 'ANY' and rule['method'].upper() != method.upper():
            return False

        # 2. 匹配 URL 路径，同时提取路径变量到 path_vars
        if not self._match_url(rule, path, path_vars):
            return False

        # 3. 匹配条件（无条件时直接匹配成功）
        conditions = json.loads(rule.get('match_conditions', '[]'))
        if not conditions:
            return True

        # 多条件支持 AND/OR 逻辑组合：每个条件可携带 logic 字段（'and'/'or'）
        # 默认为 and（全部满足），logic='or' 的条件与前一结果取或
        return self._match_conditions_grouped(conditions, headers, query_params, body_text, body_json, cookies, path_vars)

    def _match_conditions_grouped(self, conditions, headers, query_params, body_text, body_json, cookies, path_vars):
        """PRD 4.2.3: 多条件 AND/OR 逻辑组合匹配。

        条件从左到右依次求值：
        - 第一个条件的结果作为初始值
        - 后续条件如果 logic='or'，则与前一结果取 OR
        - 后续条件如果 logic='and'（默认），则与前一结果取 AND

        示例：条件A(logic=and) AND 条件B(logic=or) AND 条件C(logic=and)
        等价于：A AND (B OR ...) AND C
        """
        if not conditions:
            return True

        result = True
        for i, condition in enumerate(conditions):
            matched = self._match_condition(condition, headers, query_params, body_text, body_json, cookies, path_vars)
            logic = condition.get('logic', 'and').lower()

            if i == 0:
                result = matched
            else:
                if logic == 'or':
                    result = result or matched
                else:
                    result = result and matched

        return result

    def _match_url(self, rule, path, path_vars):
        """匹配 URL 路径，支持四种匹配模式。

        匹配模式：
        - exact:   精确匹配，但支持 {var} 路径变量提取
        - regex:   正则表达式匹配
        - wildcard: 通配符匹配（* → .*），也支持 {var}
        - prefix:  前缀匹配（path.startswith(pattern)）

        Args:
            rule: 规则 dict
            path: 实际请求路径
            path_vars: 用于接收提取的路径变量（可变 dict）

        Returns:
            bool: 是否匹配成功
        """
        pattern = rule.get('url_pattern', '')
        match_type = rule.get('url_match_type', 'exact')

        if match_type == 'exact':
            # 精确匹配，但路径中包含 {var} 时启用变量提取
            if '{' in pattern and '}' in pattern:
                return self._match_path_variables(pattern, path, path_vars)
            return path == pattern
        elif match_type == 'regex':
            # 正则匹配
            try:
                return re.match(pattern, path) is not None
            except re.error:
                return False
        elif match_type == 'wildcard':
            # 通配符匹配，* 转换为 .*；同时也支持 {var} 路径变量
            if '{' in pattern and '}' in pattern:
                return self._match_path_variables(pattern, path, path_vars)
            regex_pattern = '^' + re.escape(pattern).replace('\\*', '.*') + '$'
            try:
                return re.match(regex_pattern, path) is not None
            except re.error:
                return False
        elif match_type == 'prefix':
            # 前缀匹配
            return path.startswith(pattern)
        else:
            return path == pattern

    def _match_path_variables(self, pattern, path, path_vars):
        """匹配带路径变量的 URL，并提取变量值。

        示例：
            pattern: /api/users/{id}/posts/{postId}
            path:    /api/users/123/posts/456
            结果:    path_vars = {'id': '123', 'postId': '456'}

        实现原理：
        1. 从 pattern 中提取所有 {varName} 格式的变量名
        2. 将 pattern 转为正则：re.escape 转义特殊字符后，将 \\{varName\\} 替换为 ([^/]+)
        3. 用正则匹配实际路径，提取各变量值

        注意：re.escape 将 { 转义为 \\{，所以替换时需用 '\\{' + var_name + '\\}' 格式。
        """
        var_names = re.findall(r'\{(\w+)\}', pattern)
        # re.escape 会将 { 和 } 转义为 \{ 和 \}
        regex_pattern = re.escape(pattern)
        for var_name in var_names:
            # 将 \{var_name\} 替换为 ([^/]+) 捕获组
            escaped_var = '\\{' + var_name + '\\}'
            regex_pattern = regex_pattern.replace(escaped_var, '([^/]+)')
        try:
            match = re.match('^' + regex_pattern + '$', path)
            if match:
                for i, var_name in enumerate(var_names):
                    path_vars[var_name] = match.group(i + 1)
                return True
        except re.error:
            pass
        return False

    def _match_condition(self, condition, headers, query_params, body_text, body_json, cookies, path_vars):
        """校验单个匹配条件。

        条件结构：
            {
                'source': 'header|query|body|cookie|path_var',  -- 数据来源
                'field': '字段名（支持点号嵌套）',                -- 字段名
                'operator': 'equals|not_equals|contains|...',    -- 运算符
                'value': '期望值',                                -- 期望值
                'logic': 'and|or'                                -- 与前一条件的逻辑关系
            }

        支持的运算符：
        - equals / not_equals: 精确匹配/不匹配
        - contains / not_contains: 包含/不包含子串
        - regex: 正则匹配
        - exists / not_exists: 字段存在/不存在
        - greater_than / less_than / greater_equal / less_equal: 数值比较
        - in / not_in: 在/不在逗号分隔的列表中
        """
        source = condition.get('source', '')
        field = condition.get('field', '')
        operator = condition.get('operator', 'equals')
        expected_value = condition.get('value', '')

        actual_value = self._get_field_value(source, field, headers, query_params, body_text, body_json, cookies, path_vars)

        if operator == 'equals':
            return str(actual_value) == str(expected_value)
        elif operator == 'not_equals':
            return str(actual_value) != str(expected_value)
        elif operator == 'contains':
            if actual_value is None:
                return False
            return str(expected_value) in str(actual_value)
        elif operator == 'not_contains':
            if actual_value is None:
                return True
            return str(expected_value) not in str(actual_value)
        elif operator == 'regex':
            if actual_value is None:
                return False
            try:
                return re.search(str(expected_value), str(actual_value)) is not None
            except re.error:
                return False
        elif operator == 'exists':
            return actual_value is not None
        elif operator == 'not_exists':
            return actual_value is None
        elif operator == 'greater_than':
            try:
                return float(actual_value) > float(expected_value)
            except (ValueError, TypeError):
                return False
        elif operator == 'less_than':
            try:
                return float(actual_value) < float(expected_value)
            except (ValueError, TypeError):
                return False
        elif operator == 'greater_equal':
            try:
                return float(actual_value) >= float(expected_value)
            except (ValueError, TypeError):
                return False
        elif operator == 'less_equal':
            try:
                return float(actual_value) <= float(expected_value)
            except (ValueError, TypeError):
                return False
        elif operator == 'in':
            if actual_value is None:
                return False
            items = [x.strip() for x in str(expected_value).split(',')]
            return str(actual_value) in items
        elif operator == 'not_in':
            if actual_value is None:
                return True
            items = [x.strip() for x in str(expected_value).split(',')]
            return str(actual_value) not in items
        return False

    def _get_field_value(self, source, field, headers, query_params, body_text, body_json, cookies, path_vars):
        """根据 source 和 field 从请求数据中提取字段值。

        支持的数据源：
        - header:    请求头（大小写不敏感匹配）
        - query:     查询参数
        - body:      请求体（JSON Body 支持点号嵌套取值；非 JSON 时 $ 或空表示整个 body）
        - cookie:    Cookie
        - path_var:  路径变量（由 _match_path_variables 提取）
        - form:      表单数据（暂未实现，返回 None）

        Returns:
            字段值（可能为 None 表示不存在）
        """
        if source == 'header':
            for key, value in headers.items():
                if key.lower() == field.lower():
                    return value
            return None
        elif source == 'query':
            return query_params.get(field)
        elif source == 'body':
            if body_json is not None:
                return self._get_nested_value(body_json, field)
            else:
                if field == '' or field == '$':
                    return body_text
                return None
        elif source == 'cookie':
            if cookies:
                return cookies.get(field)
            return None
        elif source == 'path_var':
            return path_vars.get(field)
        elif source == 'form':
            # Form data is typically in body_text as urlencoded
            return None
        return None

    @staticmethod
    def _get_nested_value(data, field_path):
        """使用点号表示法从嵌套 dict/list 中取值。

        示例：
            data = {'user': {'address': {'city': '北京'}}}
            _get_nested_value(data, 'user.address.city') → '北京'

            data = {'items': [{'name': 'A'}, {'name': 'B'}]}
            _get_nested_value(data, 'items.0.name') → 'A'
        """
        if not field_path:
            return data
        keys = field_path.split('.')
        current = data
        for key in keys:
            if isinstance(current, dict):
                if key in current:
                    current = current[key]
                else:
                    return None
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

    def should_trigger_random_exception(self, rule):
        """检查是否触发随机异常（按概率判定）。

        当规则启用了 random_exception_enabled 且概率 > 0 时，
        随机生成 1-100 的数，小于等于概率值则触发异常。
        """
        if not rule.get('random_exception_enabled'):
            return False
        probability = rule.get('random_exception_probability', 0)
        if probability <= 0:
            return False
        return random.randint(1, 100) <= probability
