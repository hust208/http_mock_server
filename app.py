# -*- coding: utf-8 -*-
"""HTTP Mock Server - 主应用入口

基于 Python 3.6.8 + Flask 构建，兼容 CentOS 7.9 运行环境。

应用架构：
  ┌─────────────────────────────────────────────────┐
  │                    Flask App                     │
  │  ┌──────────┐  ┌──────────┐  ┌────────────────┐ │
  │  │ Mock拦截  │  │ 管理后台  │  │  API 端点      │ │
  │  │ (catch-all)│ │ (Web UI) │  │ (RESTful API)  │ │
  │  └─────┬────┘  └────┬─────┘  └───────┬────────┘ │
  │        │            │                  │          │
  │  ┌─────▼────────────▼──────────────────▼──────┐  │
  │  │              核心模块层                     │  │
  │  │  RuleMatcher │ TemplateEngine │ ScriptEngine│  │
  │  │  Importer    │ RequestProxy    │  Database   │  │
  │  └─────────────────────────────────────────────┘  │
  └───────────────────────────────────────────────────┘

模块职责：
- app.py:          路由定义、请求处理、响应构建
- database.py:     数据存储（SQLite），表结构、CRUD、迁移
- matcher.py:      规则匹配引擎（URL + 方法 + 条件）
- template_engine.py: 动态变量模板渲染
- script_engine.py:  前置/后置脚本沙箱执行
- importer.py:     外部 API 定义导入（OpenAPI/Postman/MeterSphere）
- proxy.py:         代理转发（未匹配请求转发到真实后端）
- config.py:       全局配置
"""

import os
import sys
import uuid
import json
import time
import random
import hashlib
import requests as req_lib
from datetime import datetime

from flask import (
    Flask, request, jsonify, render_template,
    make_response, session, redirect, url_for
)

from config import Config
from database import Database
from matcher import RuleMatcher
from proxy import RequestProxy
from template_engine import TemplateEngine
from script_engine import ScriptEngine, ScriptResult
from importer import Importer

# ---- Flask 应用初始化 ----
app = Flask(__name__, static_url_path='/mock-admin/static', static_folder='static')
app.config['JSON_AS_ASCII'] = False   # 支持中文 JSON 响应
app.config['SECRET_KEY'] = 'mock-server-secret-key-2026'  # Session 加密密钥
app.config['MAX_CONTENT_LENGTH'] = int(Config.DEFAULTS.get('max_body_size', '10485760'))  # 请求体大小限制

# ---- 核心模块实例化（全局单例）----
db = Database()                        # 数据库操作
matcher = RuleMatcher(db)              # 规则匹配引擎
proxy_forwarder = RequestProxy(db)     # 代理转发器
template_engine = TemplateEngine()     # 模板引擎
script_engine = ScriptEngine()         # 脚本引擎
importer = Importer(db)                # 外部导入器


# ============================================================
#  辅助函数（Helper Functions）
# ============================================================

def _parse_body():
    """解析请求体：返回原始文本和解析后的 JSON（如果可解析）。

    支持两种请求体格式：
    - JSON Body: 直接 json.loads
    - Form Data: 转换为 urlencoded 字符串
    """
    body_text = ''
    body_json = None

    if request.data:
        try:
            body_text = request.data.decode('utf-8', errors='replace')
        except Exception:
            body_text = ''
    elif request.form:
        body_text = '&'.join(['{}={}'.format(k, v) for k, v in request.form.items()])

    if body_text:
        try:
            body_json = json.loads(body_text)
        except (json.JSONDecodeError, ValueError):
            body_json = None

    return body_text, body_json


def _get_headers_dict():
    """将请求头转为字典。"""
    headers = {}
    for key, value in request.headers.items():
        headers[key] = value
    return headers


def _get_query_params():
    """将查询参数转为字典。"""
    params = {}
    for key in request.args.keys():
        params[key] = request.args.get(key)
    return params


def _get_cookies():
    """将 Cookie 转为字典。"""
    cookies = {}
    for key, value in request.cookies.items():
        cookies[key] = value
    return cookies


def _generate_request_id():
    """生成唯一请求 ID（UUID 去横线格式，用于请求追踪和日志关联）。"""
    return str(uuid.uuid4()).replace('-', '')


def _should_trigger_random_exception(rule):
    """检查是否应触发随机异常（按概率判定）。

    当规则启用了 random_exception_enabled 且概率 > 0 时，
    随机生成 1-100 的数，小于等于概率值则触发异常。
    """
    if not rule.get('random_exception_enabled'):
        return False
    probability = rule.get('random_exception_probability', 0)
    if probability <= 0:
        return False
    return random.randint(1, 100) <= probability


# ============================================================
#  Mock 拦截端点 (Catch-All) - 核心 Mock 功能 (PRD 4.1-4.5)
# ============================================================

# 支持的 HTTP 方法列表（catch-all 路由拦截所有这些方法的请求）
MOCK_METHODS = ['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS', 'HEAD']


@app.route('/', defaults={'path': ''}, methods=MOCK_METHODS)
@app.route('/<path:path>', methods=MOCK_METHODS)
def mock_endpoint(path):
    """Mock 请求拦截端点（catch-all 路由）。

    这是整个系统的核心入口，拦截所有非管理后台路径的 HTTP 请求，
    按照「匹配规则 → 生成响应 → 记录日志」的流程处理。

    完整处理流程：
    1. 解析请求数据（方法、路径、Header、Query、Body、Cookie）
    2. 获取当前激活场景
    3. 规则匹配引擎匹配（含路径变量提取）
    4. 如果匹配成功：
       a. 执行前置脚本（PRD 5.2）→ 可强制返回
       b. 超时模拟（PRD 4.3.4）
       c. 延迟返回
       d. 随机异常模拟（PRD 4.3.3）
       e. 正常响应：模板渲染（PRD 5.1）→ 重定向 → Set-Cookie → CORS
       f. 执行后置脚本（PRD 5.2）→ 可修改响应
    5. 如果未匹配：
       a. 代理转发到真实后端（PRD 4.4）
       b. 或返回默认响应
    6. 记录请求日志（PRD 7.1）
    7. 构建并返回 HTTP 响应

    Args:
        path: URL 路径（Flask catch-all 路由参数）

    Returns:
        Flask Response 对象
    """
    start_time = time.time()
    request_id = _generate_request_id()
    full_path = '/' + path

    # ---- 1. 解析请求数据 ----
    method = request.method
    headers = _get_headers_dict()
    query_params = _get_query_params()
    body_text, body_json = _parse_body()
    cookies = _get_cookies()
    full_url = request.full_path.rstrip('?') if request.full_path else full_path

    # ---- 2. 获取当前激活场景 ----
    active_scene = db.get_active_scene()
    scene_id = active_scene['id'] if active_scene else None
    scene_name = active_scene['name'] if active_scene else ''

    # ---- 3. 规则匹配（含路径变量提取和 Cookie 支持）----
    matched_rule, path_vars = matcher.match(method, full_path, headers, query_params, body_text, body_json, cookies)

    # 构建请求上下文（供模板引擎和脚本引擎使用）
    request_ctx = {
        'method': method, 'path': full_path, 'headers': headers,
        'query_params': query_params, 'body_text': body_text,
        'body_json': body_json, 'cookies': cookies, 'path_vars': path_vars,
    }

    # ---- 4. 准备响应 ----
    response_status = 404
    response_headers = {}
    response_body = ''
    match_result = 'unmatched'
    matched_rule_id = None
    matched_rule_name = ''

    if matched_rule:
        # ---- 4a. 匹配成功：处理 Mock 响应 ----
        match_result = 'matched'
        matched_rule_id = matched_rule['id']
        matched_rule_name = matched_rule['name']

        # 更新命中次数
        db.increment_hit_count(matched_rule['id'])

        # ---- PRD 5.2: 执行前置脚本（校验入参/JWT/强制返回）----
        pre_script = matched_rule.get('pre_script', '')
        if pre_script and pre_script.strip():
            pre_result = script_engine.execute_pre_script(pre_script, request_ctx)
            if pre_result.skip_mock:
                # 前置脚本强制直接返回（跳过 Mock 响应流程）
                response_status = pre_result.forced_status or 200
                response_headers = pre_result.forced_headers or {'Content-Type': 'application/json'}
                response_body = pre_result.forced_body or ''
                elapsed_ms = int((time.time() - start_time) * 1000)
                # 记录请求日志后直接返回
                db.create_request_log({
                    'request_id': request_id, 'method': method, 'url': full_url,
                    'path': full_path,
                    'query_params': json.dumps(query_params, ensure_ascii=False),
                    'headers': json.dumps(headers, ensure_ascii=False),
                    'body': body_text, 'matched_rule_id': matched_rule_id,
                    'matched_rule_name': matched_rule_name, 'match_result': 'matched',
                    'response_status': response_status, 'response_body': response_body[:5000] if response_body else '',
                    'response_headers': json.dumps(response_headers, ensure_ascii=False),
                    'response_time_ms': elapsed_ms, 'scene_id': scene_id,
                    'scene_name': scene_name, 'client_ip': request.remote_addr or '',
                })
                response_headers['X-Mock-Request-Id'] = request_id
                resp = make_response(response_body, response_status)
                for k, v in response_headers.items():
                    resp.headers[k] = v
                return resp

        # ---- PRD 4.3.4: 超时模拟 ----
        if matched_rule.get('timeout_enabled'):
            elapsed = int((time.time() - start_time) * 1000)
            db.create_request_log({
                'request_id': request_id, 'method': method, 'url': full_url,
                'path': full_path,
                'query_params': json.dumps(query_params, ensure_ascii=False),
                'headers': json.dumps(headers, ensure_ascii=False),
                'body': body_text, 'matched_rule_id': matched_rule_id,
                'matched_rule_name': matched_rule_name, 'match_result': 'matched',
                'response_status': 0, 'response_body': '', 'response_headers': '{}',
                'response_time_ms': elapsed, 'scene_id': scene_id,
                'scene_name': scene_name, 'client_ip': request.remote_addr or '',
            })
            time.sleep(Config.MOCK_TIMEOUT_DURATION)
            return make_response(
                json.dumps({'error': 'Gateway Timeout', 'message': 'Mock timeout simulation ended'}),
                504,
                {'Content-Type': 'application/json', 'X-Mock-Request-Id': request_id}
            )

        # ---- 延迟返回 ----
        delay_ms = matched_rule.get('delay_ms', 0)
        if delay_ms and delay_ms > 0:
            time.sleep(delay_ms / 1000.0)

        # ---- PRD 4.3.3: 随机异常模拟 ----
        if _should_trigger_random_exception(matched_rule):
            response_status = matched_rule.get('random_exception_status', 500)
            response_headers = {'Content-Type': 'application/json'}
            response_body = json.dumps({
                'error': 'Simulated Exception', 'status': response_status,
                'message': 'Random exception triggered by mock rule'
            }, ensure_ascii=False)
        else:
            # ---- 正常响应：解析响应头 + 模板渲染 + 重定向 + Set-Cookie + CORS ----
            response_status = matched_rule.get('response_status', 200)
            try:
                response_headers = json.loads(matched_rule.get('response_headers', '{}'))
            except (json.JSONDecodeError, ValueError):
                response_headers = {'Content-Type': 'application/json'}

            # PRD 5.1: 渲染动态变量（{{$timestamp}}、{{request.xxx}} 等）
            raw_body = matched_rule.get('response_body', '')
            response_body = template_engine.render(raw_body, request_ctx)

            # PRD 4.3.5: 重定向处理（支持 301/302/303/307/308）
            redirect_url = matched_rule.get('redirect_url', '')
            if redirect_url and response_status in (301, 302, 303, 307, 308):
                redirect_url = template_engine.render(redirect_url, request_ctx)
                response_headers['Location'] = redirect_url

            # PRD 4.3.2: Set-Cookie 处理
            try:
                set_cookies = json.loads(matched_rule.get('set_cookies', '[]'))
                if isinstance(set_cookies, list) and set_cookies:
                    response_headers['_set_cookies'] = set_cookies
            except (json.JSONDecodeError, ValueError):
                pass

            # PRD 4.3.2: 每规则独立 CORS
            if matched_rule.get('cors_enabled'):
                response_headers['Access-Control-Allow-Origin'] = matched_rule.get('cors_origin', '*')
                response_headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, PATCH, OPTIONS, HEAD'
                response_headers['Access-Control-Allow-Headers'] = '*'

        # ---- PRD 5.2: 执行后置脚本（运算组装响应/脱敏/修改状态码）----
        post_script = matched_rule.get('post_script', '')
        if post_script and post_script.strip():
            response_data = {
                'status': response_status,
                'headers': response_headers,
                'body': response_body,
            }
            post_result = script_engine.execute_post_script(post_script, request_ctx, response_data)
            if post_result.success:
                # 应用后置脚本的修改
                if post_result.modified_status is not None:
                    response_status = post_result.modified_status
                if post_result.modified_headers is not None:
                    response_headers = post_result.modified_headers
                if post_result.modified_body is not None:
                    response_body = post_result.modified_body

    else:
        # ---- 4b. 未匹配：尝试代理转发或返回默认响应 ----
        proxy_enabled = db.get_setting('proxy_enabled', 'false') == 'true'
        if proxy_enabled:
            # PRD 4.4: 代理转发到真实后端
            result = proxy_forwarder.forward(
                method, full_path, headers, query_params,
                request.data, full_url
            )
            if result:
                match_result = 'forwarded'
                response_status, response_headers, response_body = result
            else:
                # 代理目标未配置，使用默认响应
                response_status = int(db.get_setting('default_response_status', '404'))
                try:
                    response_headers = json.loads(
                        db.get_setting('default_response_headers', '{"Content-Type": "application/json"}')
                    )
                except (json.JSONDecodeError, ValueError):
                    response_headers = {'Content-Type': 'application/json'}
                response_body = db.get_setting('default_response_body', '{"error": "No match found"}')
        else:
            # 未开启代理，使用默认响应
            response_status = int(db.get_setting('default_response_status', '404'))
            try:
                response_headers = json.loads(
                    db.get_setting('default_response_headers', '{"Content-Type": "application/json"}')
                )
            except (json.JSONDecodeError, ValueError):
                response_headers = {'Content-Type': 'application/json'}
            response_body = db.get_setting('default_response_body', '{"error": "No match found"}')

    # ---- 5. 记录请求日志 (PRD 7.1) ----
    elapsed_ms = int((time.time() - start_time) * 1000)
    db.create_request_log({
        'request_id': request_id,
        'method': method,
        'url': full_url,
        'path': full_path,
        'query_params': json.dumps(query_params, ensure_ascii=False),
        'headers': json.dumps(headers, ensure_ascii=False),
        'body': body_text,
        'matched_rule_id': matched_rule_id,
        'matched_rule_name': matched_rule_name,
        'match_result': match_result,
        'response_status': response_status,
        'response_body': response_body[:5000] if response_body else '',
        'response_headers': json.dumps(response_headers, ensure_ascii=False),
        'response_time_ms': elapsed_ms,
        'scene_id': scene_id,
        'scene_name': scene_name,
        'client_ip': request.remote_addr or '',
    })

    # ---- 6. 构建并返回 HTTP 响应 ----
    # 确保 response_body 为字符串
    if isinstance(response_body, (dict, list)):
        response_body = json.dumps(response_body, ensure_ascii=False)

    # 添加请求追踪 ID 到响应头
    response_headers['X-Mock-Request-Id'] = request_id

    # 全局 CORS 设置（如果开启且规则未单独配置 CORS）
    cors_enabled = db.get_setting('cors_enabled', 'false') == 'true'
    if cors_enabled:
        response_headers['Access-Control-Allow-Origin'] = '*'
        response_headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, PATCH, OPTIONS, HEAD'
        response_headers['Access-Control-Allow-Headers'] = '*'

    resp = make_response(response_body, response_status)
    for key, value in response_headers.items():
        resp.headers[key] = value

    return resp


# ============================================================
#  查询 API - 供自动化断言使用 (PRD 7.2.1)
# ============================================================

@app.route('/api/requests/<request_id>', methods=['GET'])
def query_request(request_id):
    """PRD 7.2.1: 按请求 ID 查询请求数据，供自动化测试断言使用。

    返回完整的请求和响应信息，包括：
    - 请求方法、URL、查询参数、请求头、请求体
    - 匹配结果、匹配的规则信息
    - 响应状态码、响应体、响应头
    - 响应耗时、客户端 IP、时间戳

    所有 JSON 字段会自动解析为对象，方便断言脚本使用。
    """
    log = db.get_request_log(request_id)
    if not log:
        return jsonify({'error': 'Not Found', 'message': 'Request ID not found'}), 404

    # Parse JSON fields for response
    result = dict(log)
    try:
        result['headers'] = json.loads(log['headers']) if log['headers'] else {}
    except (json.JSONDecodeError, ValueError):
        result['headers'] = {}
    try:
        result['query_params'] = json.loads(log['query_params']) if log['query_params'] else {}
    except (json.JSONDecodeError, ValueError):
        result['query_params'] = {}
    try:
        result['response_headers'] = json.loads(log['response_headers']) if log['response_headers'] else {}
    except (json.JSONDecodeError, ValueError):
        result['response_headers'] = {}

    # Try to parse body as JSON
    try:
        result['body_json'] = json.loads(log['body']) if log['body'] else None
    except (json.JSONDecodeError, ValueError):
        result['body_json'] = None

    # Try to parse response_body as JSON
    try:
        result['response_body_json'] = json.loads(log['response_body']) if log['response_body'] else None
    except (json.JSONDecodeError, ValueError):
        result['response_body_json'] = None

    return jsonify(result)


# ============================================================
#  管理后台 Web UI 页面
# ============================================================

@app.route('/mock-admin/')
def admin_dashboard():
    """仪表盘页面：显示统计数据和场景概览。"""
    stats = db.get_stats()
    scenes = db.list_scenes()
    return render_template('dashboard.html', stats=stats, scenes=scenes, active_page='dashboard')


@app.route('/mock-admin/rules')
def admin_rules():
    """规则管理页面：查看、创建、编辑、删除 Mock 规则。"""
    scenes = db.list_scenes()
    return render_template('rules.html', scenes=scenes, active_page='rules')


@app.route('/mock-admin/scenes')
def admin_scenes():
    """场景管理页面：创建、切换、删除场景。"""
    return render_template('scenes.html', active_page='scenes')


@app.route('/mock-admin/requests')
def admin_requests():
    """请求记录页面：查看历史请求日志和匹配详情。"""
    return render_template('requests.html', active_page='requests')


@app.route('/mock-admin/settings')
def admin_settings():
    """设置页面：配置代理、CORS、脱敏、认证等全局参数。"""
    settings = db.get_all_settings()
    return render_template('settings.html', settings=settings, active_page='settings')


# ============================================================
#  管理后台 API - 规则管理 (PRD 4.2-4.3)
# ============================================================

@app.route('/mock-admin/api/rules', methods=['GET'])
def api_list_rules():
    """列出规则，支持按场景和启用状态筛选。返回时解析 JSON 字段为对象。"""
    scene_id = request.args.get('scene_id', type=int)
    enabled_only = request.args.get('enabled_only', 'false') == 'true'
    rules = db.list_rules(scene_id=scene_id, enabled_only=enabled_only)
    # Parse JSON fields
    for rule in rules:
        try:
            rule['match_conditions'] = json.loads(rule.get('match_conditions', '[]'))
        except (json.JSONDecodeError, ValueError):
            rule['match_conditions'] = []
        try:
            rule['response_headers'] = json.loads(rule.get('response_headers', '{}'))
        except (json.JSONDecodeError, ValueError):
            rule['response_headers'] = {}
        try:
            rule['set_cookies'] = json.loads(rule.get('set_cookies', '[]'))
        except (json.JSONDecodeError, ValueError):
            rule['set_cookies'] = []
    return jsonify({'items': rules, 'total': len(rules)})


@app.route('/mock-admin/api/rules', methods=['POST'])
def api_create_rule():
    """创建新规则。JSON 字段（match_conditions/response_headers/set_cookies）自动序列化。"""
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({'error': 'Invalid JSON'}), 400
    if not data.get('url_pattern'):
        return jsonify({'error': 'url_pattern is required'}), 400
    if not data.get('method'):
        return jsonify({'error': 'method is required'}), 400

    # Serialize JSON fields
    if isinstance(data.get('match_conditions'), (list, dict)):
        data['match_conditions'] = json.dumps(data['match_conditions'], ensure_ascii=False)
    if isinstance(data.get('response_headers'), (list, dict)):
        data['response_headers'] = json.dumps(data['response_headers'], ensure_ascii=False)
    if isinstance(data.get('set_cookies'), (list, dict)):
        data['set_cookies'] = json.dumps(data['set_cookies'], ensure_ascii=False)

    rule_id = db.create_rule(data)
    return jsonify({'id': rule_id, 'message': 'Rule created successfully'}), 201


@app.route('/mock-admin/api/rules/<int:rule_id>', methods=['GET'])
def api_get_rule(rule_id):
    """查询单条规则详情，返回时解析 JSON 字段为对象。"""
    rule = db.get_rule(rule_id)
    if not rule:
        return jsonify({'error': 'Rule not found'}), 404
    # Parse JSON fields
    try:
        rule['match_conditions'] = json.loads(rule.get('match_conditions', '[]'))
    except (json.JSONDecodeError, ValueError):
        rule['match_conditions'] = []
    try:
        rule['response_headers'] = json.loads(rule.get('response_headers', '{}'))
    except (json.JSONDecodeError, ValueError):
        rule['response_headers'] = {}
    return jsonify(rule)


@app.route('/mock-admin/api/rules/<int:rule_id>', methods=['PUT'])
def api_update_rule(rule_id):
    """更新规则。支持乐观锁（传入 version 字段时校验）。JSON 字段自动序列化。"""
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({'error': 'Invalid JSON'}), 400

    # Serialize JSON fields
    if isinstance(data.get('match_conditions'), (list, dict)):
        data['match_conditions'] = json.dumps(data['match_conditions'], ensure_ascii=False)
    if isinstance(data.get('response_headers'), (list, dict)):
        data['response_headers'] = json.dumps(data['response_headers'], ensure_ascii=False)
    if isinstance(data.get('set_cookies'), (list, dict)):
        data['set_cookies'] = json.dumps(data['set_cookies'], ensure_ascii=False)

    success = db.update_rule(rule_id, data)
    if not success:
        return jsonify({'error': 'Rule not found'}), 404
    return jsonify({'message': 'Rule updated successfully'})


@app.route('/mock-admin/api/rules/<int:rule_id>', methods=['DELETE'])
def api_delete_rule(rule_id):
    """删除规则。"""
    db.delete_rule(rule_id)
    return jsonify({'message': 'Rule deleted successfully'})


@app.route('/mock-admin/api/rules/batch', methods=['POST'])
def api_batch_rules():
    """批量操作规则（enable/disable/delete）。"""
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({'error': 'Invalid JSON'}), 400
    rule_ids = data.get('ids', [])
    action = data.get('action', '')
    if not rule_ids or action not in ('enable', 'disable', 'delete'):
        return jsonify({'error': 'Invalid ids or action'}), 400
    count = db.batch_update_rules(rule_ids, action)
    return jsonify({'message': 'Batch operation completed', 'affected': count})


@app.route('/mock-admin/api/rules/export', methods=['GET'])
def api_export_rules():
    """导出所有规则为 JSON，用于备份或迁移。"""
    rules = db.list_rules()
    for rule in rules:
        try:
            rule['match_conditions'] = json.loads(rule.get('match_conditions', '[]'))
        except (json.JSONDecodeError, ValueError):
            rule['match_conditions'] = []
        try:
            rule['response_headers'] = json.loads(rule.get('response_headers', '{}'))
        except (json.JSONDecodeError, ValueError):
            rule['response_headers'] = {}
    export_data = {
        'export_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'rule_count': len(rules),
        'rules': rules,
    }
    return jsonify(export_data)


@app.route('/mock-admin/api/rules/import', methods=['POST'])
def api_import_rules():
    """从 JSON 导入规则（批量创建，清除 id/时间戳/命中次数字段）。"""
    data = request.get_json(force=True, silent=True)
    if not data or 'rules' not in data:
        return jsonify({'error': 'Invalid format, expected {"rules": [...]}'}), 400
    imported = 0
    for rule_data in data['rules']:
        # Remove fields that shouldn't be imported
        for field in ('id', 'created_at', 'updated_at', 'hit_count'):
            rule_data.pop(field, None)
        if isinstance(rule_data.get('match_conditions'), (list, dict)):
            rule_data['match_conditions'] = json.dumps(rule_data['match_conditions'], ensure_ascii=False)
        if isinstance(rule_data.get('response_headers'), (list, dict)):
            rule_data['response_headers'] = json.dumps(rule_data['response_headers'], ensure_ascii=False)
        db.create_rule(rule_data)
        imported += 1
    return jsonify({'message': 'Import completed', 'imported': imported})


# ============================================================
#  管理后台 API - 场景管理 (PRD 4.1)
# ============================================================

@app.route('/mock-admin/api/scenes', methods=['GET'])
def api_list_scenes():
    """列出所有场景。"""
    scenes = db.list_scenes()
    return jsonify({'items': scenes, 'total': len(scenes)})


@app.route('/mock-admin/api/scenes', methods=['POST'])
def api_create_scene():
    """创建新场景。"""
    data = request.get_json(force=True, silent=True)
    if not data or not data.get('name'):
        return jsonify({'error': 'name is required'}), 400
    try:
        scene_id = db.create_scene(data['name'], data.get('description', ''))
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'id': scene_id, 'message': 'Scene created successfully'}), 201


@app.route('/mock-admin/api/scenes/<int:scene_id>', methods=['PUT'])
def api_update_scene(scene_id):
    """更新场景信息（名称、描述）。"""
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({'error': 'Invalid JSON'}), 400
    success = db.update_scene(scene_id, data.get('name'), data.get('description'))
    if not success:
        return jsonify({'error': 'Scene not found'}), 404
    return jsonify({'message': 'Scene updated successfully'})


@app.route('/mock-admin/api/scenes/<int:scene_id>', methods=['DELETE'])
def api_delete_scene(scene_id):
    """删除场景（规则解绑但不删除）。"""
    db.delete_scene(scene_id)
    return jsonify({'message': 'Scene deleted successfully'})


@app.route('/mock-admin/api/scenes/<int:scene_id>/activate', methods=['POST'])
def api_activate_scene(scene_id):
    """激活指定场景（同一时刻只有一个场景激活）。"""
    scene = db.get_scene(scene_id)
    if not scene:
        return jsonify({'error': 'Scene not found'}), 404
    db.activate_scene(scene_id)
    return jsonify({'message': 'Scene activated successfully', 'scene_name': scene['name']})


# ============================================================
#  管理后台 API - 请求日志 (PRD 7.1)
# ============================================================

@app.route('/mock-admin/api/requests', methods=['GET'])
def api_list_requests():
    """分页查询请求日志，支持多条件筛选（URL/方法/场景/匹配结果/时间范围）。"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    filters = {
        'url': request.args.get('url', ''),
        'method': request.args.get('method', ''),
        'scene_id': request.args.get('scene_id', type=int),
        'match_result': request.args.get('match_result', ''),
        'start_time': request.args.get('start_time', ''),
        'end_time': request.args.get('end_time', ''),
    }
    result = db.list_request_logs(filters=filters, page=page, per_page=per_page)
    return jsonify(result)


@app.route('/mock-admin/api/requests/<request_id>', methods=['GET'])
def api_get_request_detail(request_id):
    """查询单条请求日志详情，返回时解析 JSON 字段为对象。"""
    log = db.get_request_log(request_id)
    if not log:
        return jsonify({'error': 'Request not found'}), 404
    # Parse JSON fields
    result = dict(log)
    try:
        result['headers'] = json.loads(log['headers']) if log['headers'] else {}
    except (json.JSONDecodeError, ValueError):
        result['headers'] = {}
    try:
        result['query_params'] = json.loads(log['query_params']) if log['query_params'] else {}
    except (json.JSONDecodeError, ValueError):
        result['query_params'] = {}
    try:
        result['response_headers'] = json.loads(log['response_headers']) if log['response_headers'] else {}
    except (json.JSONDecodeError, ValueError):
        result['response_headers'] = {}
    return jsonify(result)


# ============================================================
#  管理后台 API - 仪表盘统计
# ============================================================

@app.route('/mock-admin/api/dashboard', methods=['GET'])
def api_dashboard_stats():
    """获取仪表盘统计数据（总请求数、今日请求、Top规则、7天趋势等）。"""
    stats = db.get_stats()
    return jsonify(stats)


# ============================================================
#  管理后台 API - 全局设置 (PRD 8)
# ============================================================

@app.route('/mock-admin/api/settings', methods=['GET'])
def api_get_settings():
    """读取所有全局设置。"""
    settings = db.get_all_settings()
    return jsonify(settings)


@app.route('/mock-admin/api/settings', methods=['PUT'])
def api_update_settings():
    """批量更新全局设置。"""
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({'error': 'Invalid JSON'}), 400
    db.update_settings(data)
    return jsonify({'message': 'Settings updated successfully'})


@app.route('/mock-admin/api/settings/cleanup', methods=['POST'])
def api_cleanup_logs():
    """清理旧请求日志，可指定保留天数（默认从设置中读取）。"""
    data = request.get_json(force=True, silent=True) or {}
    days = int(data.get('days', db.get_setting('request_retention_days', '7')))
    deleted = db.cleanup_old_logs(days)
    return jsonify({'message': 'Cleanup completed', 'deleted': deleted, 'days': days})


@app.route('/mock-admin/api/settings/proxy-test', methods=['POST'])
def api_proxy_test():
    """PRD 14.5: 测试代理目标服务器连通性。"""
    data = request.get_json(force=True, silent=True) or {}
    target = data.get('target', db.get_setting('proxy_target', ''))
    if not target:
        return jsonify({'error': 'No proxy target configured'}), 400
    result = proxy_forwarder.test_connectivity(target)
    return jsonify(result)


# ============================================================
#  认证 API (PRD 2.1-2.2)
# ============================================================

@app.route('/mock-admin/api/auth/login', methods=['POST'])
def api_login():
    """用户登录：验证凭据，创建 Session，记录审计日志。"""
    data = request.get_json(force=True, silent=True) or {}
    username = data.get('username', '')
    password = data.get('password', '')
    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400
    user = db.verify_user(username, password)
    if not user:
        return jsonify({'error': 'Invalid credentials'}), 401
    session['user_id'] = user['id']
    session['username'] = user['username']
    session['role'] = user['role']
    session.permanent = True
    db.create_audit_log(user['id'], user['username'], 'login', 'user', user['id'], 'User logged in', request.remote_addr or '')
    return jsonify({
        'id': user['id'], 'username': user['username'],
        'role': user['role'], 'display_name': user['display_name'],
    })


@app.route('/mock-admin/api/auth/logout', methods=['POST'])
def api_logout():
    """用户登出：清除 Session，记录审计日志。"""
    username = session.get('username', '')
    db.create_audit_log(session.get('user_id'), username, 'logout', 'user', session.get('user_id'), 'User logged out', request.remote_addr or '')
    session.clear()
    return jsonify({'message': 'Logged out'})


@app.route('/mock-admin/api/auth/me', methods=['GET'])
def api_auth_me():
    """获取当前登录用户信息（用于前端判断登录状态和角色权限）。"""
    if 'user_id' not in session:
        return jsonify({'authenticated': False}), 401
    return jsonify({
        'authenticated': True,
        'id': session.get('user_id'),
        'username': session.get('username'),
        'role': session.get('role'),
    })


# ============================================================
#  用户管理 API (PRD 2.2)
# ============================================================

@app.route('/mock-admin/api/users', methods=['GET'])
def api_list_users():
    """列出所有用户（不返回密码）。"""
    users = db.list_users()
    return jsonify({'items': users, 'total': len(users)})


@app.route('/mock-admin/api/users', methods=['POST'])
def api_create_user():
    """创建新用户（密码 SHA256 哈希存储）。"""
    data = request.get_json(force=True, silent=True) or {}
    if not data.get('username') or not data.get('password'):
        return jsonify({'error': 'username and password required'}), 400
    try:
        user_id = db.create_user(
            data['username'], data['password'],
            data.get('role', 'viewer'), data.get('display_name', ''), data.get('email', '')
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'id': user_id, 'message': 'User created'}), 201


@app.route('/mock-admin/api/users/<int:user_id>', methods=['PUT'])
def api_update_user(user_id):
    """更新用户信息（角色、名称、邮箱、密码）。"""
    data = request.get_json(force=True, silent=True) or {}
    success = db.update_user(user_id, data)
    if not success:
        return jsonify({'error': 'User not found'}), 404
    return jsonify({'message': 'User updated'})


@app.route('/mock-admin/api/users/<int:user_id>', methods=['DELETE'])
def api_delete_user(user_id):
    """删除用户（admin 不可删除）。"""
    db.delete_user(user_id)
    return jsonify({'message': 'User deleted'})


# ============================================================
#  审计日志 API (PRD 2.3)
# ============================================================

@app.route('/mock-admin/api/audit-logs', methods=['GET'])
def api_list_audit_logs():
    """分页查询审计日志，支持按操作类型筛选。"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    action_filter = request.args.get('action', '')
    result = db.list_audit_logs(page=page, per_page=per_page, action_filter=action_filter)
    return jsonify(result)


# ============================================================
#  外部导入 API (PRD 5.3)
# ============================================================

@app.route('/mock-admin/api/import/openapi', methods=['POST'])
def api_import_openapi():
    """导入 OpenAPI 3.0 / Swagger 2.0 规范文件。"""
    data = request.get_json(force=True, silent=True) or {}
    spec_text = data.get('spec', '')
    strategy = data.get('strategy', 'skip')
    scene_id = data.get('scene_id')
    if not spec_text:
        return jsonify({'error': 'spec is required'}), 400
    result = importer.import_openapi(spec_text, scene_id, strategy)
    return jsonify(result)


@app.route('/mock-admin/api/import/postman', methods=['POST'])
def api_import_postman():
    """导入 Postman 集合 JSON 文件。"""
    data = request.get_json(force=True, silent=True) or {}
    collection_text = data.get('collection', '')
    strategy = data.get('strategy', 'skip')
    scene_id = data.get('scene_id')
    if not collection_text:
        return jsonify({'error': 'collection is required'}), 400
    result = importer.import_postman(collection_text, scene_id, strategy)
    return jsonify(result)


@app.route('/mock-admin/api/import/metersphere', methods=['POST'])
def api_import_metersphere():
    """导入 MeterSphere API 测试用例 JSON。"""
    data = request.get_json(force=True, silent=True) or {}
    ms_text = data.get('data', '')
    strategy = data.get('strategy', 'skip')
    scene_id = data.get('scene_id')
    if not ms_text:
        return jsonify({'error': 'data is required'}), 400
    result = importer.import_metersphere(ms_text, scene_id, strategy)
    return jsonify(result)


# ============================================================
#  MSW 脚本生成 (PRD 6.1)
# ============================================================

@app.route('/mock-admin/api/msw-script', methods=['GET'])
def api_msw_script():
    """PRD 6.1: 生成 MSW (Mock Service Worker) 脚本，用于浏览器端 Mock。

    将所有启用的 Mock 规则转换为 MSW 的 http handler 格式，
    生成的脚本可直接在前端项目中使用（import { handlers } from './handlers'）。
    """
    rules = db.list_rules(enabled_only=True)
    handlers = []
    for rule in rules:
        method = rule['method'].lower()
        if method == 'any':
            method = 'get'
        path = rule['url_pattern']
        status = rule.get('response_status', 200)
        body = rule.get('response_body', '{}')
        try:
            headers = json.loads(rule.get('response_headers', '{}'))
        except (json.JSONDecodeError, ValueError):
            headers = {'Content-Type': 'application/json'}
        handlers.append(
            "  http.{}('{}', ({}) => {{\n"
            "    return HttpResponse.json({}, {{ status: {}, headers: {} }})\n"
            "  }})".format(
                method, path, '{ request }',
                body.replace('\n', '\n    '), status,
                json.dumps(headers, ensure_ascii=False)
            )
        )
    script = "import {{ http, HttpResponse }} from 'msw'\n\nexport const handlers = [\n{}\n]\n".format(',\n'.join(handlers))
    return script, 200, {'Content-Type': 'application/javascript'}


# ============================================================
#  健康检查 & 备份恢复 (PRD 11)
# ============================================================

@app.route('/health', methods=['GET'])
def health_check():
    """PRD 11: 健康检查端点，返回服务状态和 Python 版本。"""
    return jsonify({
        'status': 'ok',
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'python': sys.version.split()[0],
    })


@app.route('/mock-admin/api/backup', methods=['GET'])
def api_backup():
    """PRD 11: 导出全量配置（场景+规则+设置），用于备份。"""
    config_data = db.export_all_config()
    return jsonify(config_data)


@app.route('/mock-admin/api/backup/restore', methods=['POST'])
def api_restore():
    """PRD 11: 从备份数据恢复配置（场景+规则+设置）。"""
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({'error': 'Invalid JSON'}), 400
    result = db.import_config(data)
    return jsonify({'message': 'Restore completed', 'imported': result})


# ============================================================
#  请求重放 (PRD 7.2.2)
# ============================================================

@app.route('/mock-admin/api/requests/<request_id>/replay', methods=['POST'])
def api_replay_request(request_id):
    """PRD 7.2: 重放历史请求（将记录的请求重新发送到 Mock 服务器）。

    用于调试和验证 Mock 规则的匹配效果。
    """
    log = db.get_request_log(request_id)
    if not log:
        return jsonify({'error': 'Request not found'}), 404
    try:
        headers = json.loads(log['headers']) if log['headers'] else {}
        params = json.loads(log['query_params']) if log['query_params'] else {}
    except (json.JSONDecodeError, ValueError):
        headers = {}
        params = {}
    try:
        resp = req_lib.request(
            method=log['method'],
            url='http://localhost:{}/{}'.format(Config.PORT, log['path'].lstrip('/')),
            headers=headers,
            params=params,
            data=log['body'] if log['body'] else None,
            timeout=30,
        )
        return jsonify({
            'status': resp.status_code,
            'body': resp.text[:5000],
            'headers': dict(resp.headers),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ============================================================
#  脚本/正则校验 (PRD 12.2)
# ============================================================

@app.route('/mock-admin/api/validate/script', methods=['POST'])
def api_validate_script():
    """PRD 12.2: 校验脚本安全性（危险模式检查 + 语法编译检查）。"""
    data = request.get_json(force=True, silent=True) or {}
    script = data.get('script', '')
    is_valid, error = script_engine.validate_script(script)
    return jsonify({'valid': is_valid, 'error': error})


@app.route('/mock-admin/api/validate/regex', methods=['POST'])
def api_validate_regex():
    """PRD 12.2: 校验正则表达式语法是否正确。"""
    import re as re_mod
    data = request.get_json(force=True, silent=True) or {}
    pattern = data.get('pattern', '')
    try:
        re_mod.compile(pattern)
        return jsonify({'valid': True})
    except re_mod.error as e:
        return jsonify({'valid': False, 'error': str(e)})


# ============================================================
#  路径冲突检测 (PRD 12.1)
# ============================================================

@app.route('/mock-admin/api/rules/check-duplicate', methods=['POST'])
def api_check_duplicate():
    """PRD 12.1: 创建规则前检查路径冲突（同 method + url_pattern 是否已存在）。"""
    data = request.get_json(force=True, silent=True) or {}
    method = data.get('method', '')
    url_pattern = data.get('url_pattern', '')
    exclude_id = data.get('exclude_id')
    if not method or not url_pattern:
        return jsonify({'duplicate': False})
    existing = db.find_duplicate_rule(method, url_pattern, exclude_id)
    return jsonify({
        'duplicate': existing is not None,
        'existing_rule': {'id': existing['id'], 'name': existing['name']} if existing else None,
    })


# ============================================================
#  程序入口
# ============================================================

if __name__ == '__main__':
    # 启动时打印服务信息
    print('=' * 60)
    print('  HTTP Mock Server V1.0 (PRD Enhanced)')
    print('  Python ' + sys.version.split()[0])
    print('  Listening on {}:{}'.format(Config.HOST, Config.PORT))
    print('  Admin UI:  http://localhost:{}/mock-admin/'.format(Config.PORT))
    print('  Query API: http://localhost:{}/api/requests/<request_id>'.format(Config.PORT))
    print('  Health:    http://localhost:{}/health'.format(Config.PORT))
    print('  Default login: admin / admin123')
    print('  Database:  {}'.format(Config.DATABASE))
    print('=' * 60)
    # 启动 Flask 开发服务器（生产环境建议使用 gunicorn + gevent）
    app.run(
        host=Config.HOST,
        port=Config.PORT,
        debug=Config.DEBUG,
        threaded=True   # 多线程模式，支持并发请求
    )
