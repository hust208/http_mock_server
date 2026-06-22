# -*- coding: utf-8 -*-
"""HTTP Mock Server - 外部导入模块

PRD 5.3: 支持 OpenAPI 3.0 / Swagger 2.0、Postman 集合、MeterSphere API 用例包的导入。
PRD 9.1: MeterSphere 导入导出准确性保障。

导入策略（strategy）：
- skip:      跳过已存在的同方法+同路径规则（默认）
- overwrite: 覆盖已存在的规则
- merge:     合并（当前实现与 skip 一致，预留扩展）

各导入器的处理流程：
1. 解析外部格式 JSON/YAML → 提取 API 定义列表
2. 逐条创建 Mock 规则（URL、方法、响应体、响应头等）
3. 按导入策略处理重复规则
"""

import json


class Importer(object):
    """外部 API 定义导入器：将 OpenAPI/Postman/MeterSphere 格式转为 Mock 规则。

    Attributes:
        db: Database 实例，用于创建和查询规则
    """

    def __init__(self, db):
        self.db = db

    def import_openapi(self, spec_text, scene_id=None, strategy='skip'):
        """导入 OpenAPI 3.0 / Swagger 2.0 规范文件。

        解析流程：
        1. 优先尝试 JSON 解析，失败后尝试 YAML（需安装 PyYAML）
        2. 检测规范版本（openapi 字段 = 3.x 或 swagger 字段 = 2.0）
        3. 遍历 paths 对象，为每个 HTTP 方法创建 Mock 规则
        4. 自动从 responses 中提取示例响应体

        Args:
            spec_text: 规范文件文本（JSON 或 YAML）
            scene_id: 导入到指定场景，None 表示全局
            strategy: 重复处理策略（skip/overwrite/merge）

        Returns:
            dict: {success, imported, skipped, failed, errors}
        """
        result = {'success': True, 'imported': 0, 'skipped': 0, 'failed': 0, 'errors': []}
        try:
            spec = json.loads(spec_text)
        except (json.JSONDecodeError, ValueError):
            try:
                import yaml
                spec = yaml.safe_load(spec_text)
            except ImportError:
                result['success'] = False
                result['errors'].append('Invalid JSON and PyYAML not installed')
                return result
            except Exception:
                result['success'] = False
                result['errors'].append('Failed to parse spec')
                return result

        if not isinstance(spec, dict):
            result['success'] = False
            result['errors'].append('Invalid spec format')
            return result

        is_openapi3 = 'openapi' in spec and spec['openapi'].startswith('3')
        is_swagger2 = 'swagger' in spec and spec['swagger'] == '2.0'
        if not is_openapi3 and not is_swagger2:
            result['success'] = False
            result['errors'].append('Not a valid OpenAPI 3.0 or Swagger 2.0 spec')
            return result

        paths = spec.get('paths', {})
        if not paths:
            result['errors'].append('No paths found in spec')
            return result

        for path, path_item in paths.items():
            if not isinstance(path_item, dict):
                continue
            for method, operation in path_item.items():
                method = method.upper()
                if method not in ('GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS', 'HEAD'):
                    continue
                if not isinstance(operation, dict):
                    continue
                try:
                    rule_data = self._parse_openapi_operation(path, method, operation, spec, is_openapi3, scene_id)
                    existing = self._find_duplicate(rule_data['method'], rule_data['url_pattern'])
                    if existing and strategy == 'skip':
                        result['skipped'] += 1
                        continue
                    elif existing and strategy == 'overwrite':
                        rd = dict(rule_data)
                        rd['match_conditions'] = json.dumps(rd.get('match_conditions', []), ensure_ascii=False)
                        rd['response_headers'] = json.dumps(rd.get('response_headers', {}), ensure_ascii=False)
                        self.db.update_rule(existing['id'], rd)
                        result['imported'] += 1
                        continue
                    rd = dict(rule_data)
                    rd['match_conditions'] = json.dumps(rd.get('match_conditions', []), ensure_ascii=False)
                    rd['response_headers'] = json.dumps(rd.get('response_headers', {}), ensure_ascii=False)
                    self.db.create_rule(rd)
                    result['imported'] += 1
                except Exception as e:
                    result['failed'] += 1
                    result['errors'].append('{} {}: {}'.format(method, path, str(e)))
        return result

    def _parse_openapi_operation(self, path, method, operation, spec, is_openapi3, scene_id):
        """解析单个 OpenAPI operation，构建 Mock 规则数据。

        提取信息：
        - URL 路径和匹配方式（含 {param} 时使用 wildcard）
        - 规则名称（operationId > summary > "METHOD /path"）
        - 响应状态码和响应体（从 responses 中提取 example 或 schema 示例）
        """
        url_pattern = path
        url_match_type = 'exact'
        if '{' in path:
            url_match_type = 'wildcard'
        summary = operation.get('summary', '')
        description = operation.get('description', '')
        operation_id = operation.get('operationId', '')
        name = operation_id or summary or '{} {}'.format(method, path)
        responses = operation.get('responses', {})
        response_status = 200
        response_body = ''
        response_headers = {}
        for code, resp_info in responses.items():
            try:
                response_status = int(code)
            except (ValueError, TypeError):
                continue
            if not isinstance(resp_info, dict):
                continue
            if not is_openapi3:
                examples = resp_info.get('examples', {})
                if examples:
                    first_example = list(examples.values())[0]
                    if isinstance(first_example, dict) and 'value' in first_example:
                        response_body = json.dumps(first_example['value'], ensure_ascii=False, indent=2)
                        response_headers['Content-Type'] = 'application/json'
                        break
            content = resp_info.get('content', {})
            app_json = content.get('application/json', {})
            if app_json:
                if app_json.get('example'):
                    response_body = json.dumps(app_json['example'], ensure_ascii=False, indent=2)
                    response_headers['Content-Type'] = 'application/json'
                    break
                elif app_json.get('schema'):
                    sample = self._generate_sample(app_json['schema'], spec)
                    if sample is not None:
                        response_body = json.dumps(sample, ensure_ascii=False, indent=2)
                        response_headers['Content-Type'] = 'application/json'
                        break
            break
        return {
            'name': name[:100], 'description': description[:500], 'scene_id': scene_id,
            'url_pattern': url_pattern, 'url_match_type': url_match_type,
            'method': method, 'match_conditions': [],
            'response_status': response_status, 'response_headers': response_headers,
            'response_body': response_body, 'delay_ms': 0, 'enabled': 1, 'priority': 100,
        }

    def _generate_sample(self, schema, root_spec=None):
        """根据 OpenAPI Schema 定义生成示例响应数据。

        递归处理：
        - $ref: 跟随引用到 components/schemas 中查找实际定义
        - object: 遍历 properties 生成示例 dict
        - array: 取 items 的第一个示例
        - string/integer/number/boolean: 返回类型对应的默认值
        - 支持 example 字段、enum、format（date-time/email/uuid 等）
        """
        if not isinstance(schema, dict):
            return None
        if '$ref' in schema and root_spec:
            ref_path = schema['$ref'].replace('#/', '').split('/')
            ref = root_spec
            for part in ref_path:
                ref = ref.get(part, {})
            return self._generate_sample(ref, root_spec)
        schema_type = schema.get('type', 'string')
        example = schema.get('example')
        if example is not None:
            return example
        if schema_type == 'object':
            result = {}
            properties = schema.get('properties', {})
            for prop_name, prop_schema in properties.items():
                result[prop_name] = self._generate_sample(prop_schema, root_spec)
            return result
        elif schema_type == 'array':
            items = schema.get('items', {})
            return [self._generate_sample(items, root_spec)]
        elif schema_type == 'string':
            fmt = schema.get('format', '')
            if fmt == 'date-time':
                return '2026-01-01T00:00:00Z'
            elif fmt == 'date':
                return '2026-01-01'
            elif fmt == 'email':
                return 'user@example.com'
            elif fmt == 'uuid':
                return '00000000-0000-0000-0000-000000000000'
            enum = schema.get('enum')
            if enum:
                return enum[0]
            return 'string'
        elif schema_type == 'integer':
            return 0
        elif schema_type == 'number':
            return 0.0
        elif schema_type == 'boolean':
            return True
        return None

    def import_postman(self, collection_text, scene_id=None, strategy='skip'):
        """导入 Postman 集合 JSON 文件。

        解析流程：
        1. 解析 JSON 格式的 Postman Collection
        2. 递归遍历 item 列表（支持文件夹嵌套）
        3. 从 request 中提取 URL、方法、名称
        4. 从 response（saved responses）中提取示例响应
        5. 按策略处理重复规则
        """
        result = {'success': True, 'imported': 0, 'skipped': 0, 'failed': 0, 'errors': []}
        try:
            collection = json.loads(collection_text)
        except (json.JSONDecodeError, ValueError) as e:
            result['success'] = False
            result['errors'].append('Invalid JSON: {}'.format(str(e)))
            return result
        if not isinstance(collection, dict):
            result['success'] = False
            result['errors'].append('Invalid collection format')
            return result
        items = collection.get('item', [])

        def process_items(item_list):
            """递归处理 Postman item 列表，支持文件夹嵌套。"""
            for item in item_list:
                if 'item' in item:
                    process_items(item['item'])
                    continue
                req = item.get('request', {})
                if not isinstance(req, dict):
                    continue
                method = req.get('method', 'GET').upper()
                url_info = req.get('url', {})
                if isinstance(url_info, str):
                    from urllib.parse import urlparse
                    parsed = urlparse(url_info)
                    url_pattern = parsed.path or '/'
                elif isinstance(url_info, dict):
                    raw = url_info.get('raw', '')
                    if raw:
                        from urllib.parse import urlparse
                        parsed = urlparse(raw)
                        url_pattern = parsed.path or '/'
                    else:
                        path_parts = url_info.get('path', [])
                        if isinstance(path_parts, list):
                            url_pattern = '/' + '/'.join(path_parts)
                        else:
                            url_pattern = '/'
                else:
                    url_pattern = '/'
                name = item.get('name', '{} {}'.format(method, url_pattern))
                responses = item.get('response', [])
                response_body = ''
                response_status = 200
                response_headers = {'Content-Type': 'application/json'}
                if responses:
                    first_resp = responses[0]
                    response_status = int(first_resp.get('code', 200))
                    response_body = first_resp.get('body', '')
                    resp_headers = first_resp.get('header', [])
                    if isinstance(resp_headers, list):
                        response_headers = {}
                        for h in resp_headers:
                            if isinstance(h, dict) and 'key' in h and 'value' in h:
                                response_headers[h['key']] = h['value']
                rule_data = {
                    'name': name[:100], 'description': 'Imported from Postman', 'scene_id': scene_id,
                    'url_pattern': url_pattern, 'url_match_type': 'exact', 'method': method,
                    'match_conditions': [], 'response_status': response_status,
                    'response_headers': response_headers, 'response_body': response_body,
                    'delay_ms': 0, 'enabled': 1, 'priority': 100,
                }
                try:
                    existing = self._find_duplicate(method, url_pattern)
                    if existing and strategy == 'skip':
                        result['skipped'] += 1
                        continue
                    elif existing and strategy == 'overwrite':
                        rd = dict(rule_data)
                        rd['match_conditions'] = json.dumps([], ensure_ascii=False)
                        rd['response_headers'] = json.dumps(response_headers, ensure_ascii=False)
                        self.db.update_rule(existing['id'], rd)
                        result['imported'] += 1
                        continue
                    rd = dict(rule_data)
                    rd['match_conditions'] = json.dumps([], ensure_ascii=False)
                    rd['response_headers'] = json.dumps(response_headers, ensure_ascii=False)
                    self.db.create_rule(rd)
                    result['imported'] += 1
                except Exception as e:
                    result['failed'] += 1
                    result['errors'].append('{} {}: {}'.format(method, url_pattern, str(e)))

        process_items(items)
        return result

    def import_metersphere(self, ms_text, scene_id=None, strategy='skip'):
        """导入 MeterSphere API 测试用例 JSON。PRD 9.1。

        支持文件大小限制（20MB），递归提取 API 定义，
        从 response 中提取状态码、响应体和响应头。
        """
        result = {'success': True, 'imported': 0, 'skipped': 0, 'failed': 0, 'errors': []}
        if len(ms_text) > 20 * 1024 * 1024:
            result['success'] = False
            result['errors'].append('File exceeds 20MB limit')
            return result
        try:
            data = json.loads(ms_text)
        except (json.JSONDecodeError, ValueError) as e:
            result['success'] = False
            result['errors'].append('Invalid JSON: {}'.format(str(e)))
            return result
        api_list = []
        self._extract_ms_apis(data, api_list)
        if not api_list:
            result['errors'].append('No API definitions found in MeterSphere data')
            return result
        for api_def in api_list:
            try:
                method = api_def.get('method', 'GET').upper()
                path = api_def.get('path', '/')
                if not path:
                    result['skipped'] += 1
                    continue
                name = api_def.get('name', '{} {}'.format(method, path))
                response_body = ''
                response_status = 200
                response_headers = {'Content-Type': 'application/json'}
                resp = api_def.get('response', {})
                if resp:
                    response_status = int(resp.get('statusCode', resp.get('code', 200)))
                    body = resp.get('body', '')
                    if isinstance(body, dict):
                        response_body = json.dumps(body, ensure_ascii=False, indent=2)
                    elif isinstance(body, str):
                        response_body = body
                    headers = resp.get('headers', [])
                    if isinstance(headers, list):
                        response_headers = {}
                        for h in headers:
                            if isinstance(h, dict):
                                k = h.get('name', h.get('key', ''))
                                v = h.get('value', '')
                                if k:
                                    response_headers[k] = v
                    elif isinstance(headers, dict):
                        response_headers = headers
                rule_data = {
                    'name': name[:100], 'description': 'Imported from MeterSphere', 'scene_id': scene_id,
                    'url_pattern': path, 'url_match_type': 'wildcard' if '{' in path else 'exact',
                    'method': method, 'match_conditions': [], 'response_status': response_status,
                    'response_headers': response_headers, 'response_body': response_body,
                    'delay_ms': 0, 'enabled': 1, 'priority': 100,
                }
                existing = self._find_duplicate(method, path)
                if existing and strategy == 'skip':
                    result['skipped'] += 1
                    continue
                elif existing and strategy == 'overwrite':
                    rd = dict(rule_data)
                    rd['match_conditions'] = json.dumps([], ensure_ascii=False)
                    rd['response_headers'] = json.dumps(response_headers, ensure_ascii=False)
                    self.db.update_rule(existing['id'], rd)
                    result['imported'] += 1
                    continue
                rd = dict(rule_data)
                rd['match_conditions'] = json.dumps([], ensure_ascii=False)
                rd['response_headers'] = json.dumps(response_headers, ensure_ascii=False)
                self.db.create_rule(rd)
                result['imported'] += 1
            except Exception as e:
                result['failed'] += 1
                result['errors'].append(str(e))
        return result

    def _extract_ms_apis(self, data, api_list):
        """递归从 MeterSphere JSON 中提取 API 定义。

        遍历所有嵌套的 dict/list，查找包含 request 或 method+path 的节点。
        支持的嵌套键名：data/items/apiDefinitions/scenarios/testCases 等。
        """
        if isinstance(data, dict):
            if 'request' in data or ('method' in data and 'path' in data):
                req = data.get('request', data)
                api_info = {
                    'name': data.get('name', req.get('name', '')),
                    'method': req.get('method', 'GET'),
                    'path': req.get('path', ''),
                    'response': req.get('response', data.get('response', {})),
                }
                if api_info['path']:
                    api_list.append(api_info)
            for key in ('data', 'items', 'apiDefinitions', 'scenarios', 'testCases'):
                if key in data:
                    self._extract_ms_apis(data[key], api_list)
            for k, v in data.items():
                if isinstance(v, (list, dict)):
                    self._extract_ms_apis(v, api_list)
        elif isinstance(data, list):
            for item in data:
                self._extract_ms_apis(item, api_list)

    def _find_duplicate(self, method, url_pattern):
        """PRD 12.1: 按 method+url_pattern 查找已存在的规则（导入去重用）。"""
        conn = self.db.get_conn()
        row = conn.execute(
            'SELECT * FROM rules WHERE method = ? AND url_pattern = ? LIMIT 1',
            (method.upper(), url_pattern)
        ).fetchone()
        conn.close()
        return dict(row) if row else None
