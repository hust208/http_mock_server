function saveSettings() {
    var data = {
        proxy_enabled: document.getElementById('proxyEnabled').checked ? 'true' : 'false',
        proxy_target: document.getElementById('proxyTarget').value,
        proxy_strip_mock_header: document.getElementById('proxyStripHeader').checked ? 'true' : 'false',
        default_response_status: document.getElementById('defaultResponseStatus').value,
        default_response_headers: document.getElementById('defaultResponseHeaders').value,
        default_response_body: document.getElementById('defaultResponseBody').value,
        request_retention_days: document.getElementById('retentionDays').value,
        cors_enabled: document.getElementById('corsEnabled').checked ? 'true' : 'false'
    };
    fetch('/mock-admin/api/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) })
        .then(function(r) { return r.json(); })
        .then(function() { showToast('成功', '设置已保存', 'success'); })
        .catch(function() { showToast('错误', '保存失败', 'danger'); });
}
function cleanupNow() {
    var days = parseInt(document.getElementById('retentionDays').value) || 7;
    confirmAction('确定要清理' + days + '天前的请求记录吗？', function() {
        fetch('/mock-admin/api/settings/cleanup', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ days: days }) })
            .then(function(r) { return r.json(); })
            .then(function(d) { showToast('完成', '已清理' + d.deleted + '条记录', 'success'); });
    });
}
