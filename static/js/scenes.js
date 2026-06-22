document.addEventListener('DOMContentLoaded', loadScenes);
function loadScenes() {
    fetch('/mock-admin/api/scenes').then(function(r) { return r.json(); }).then(function(data) {
        var tb = document.getElementById('scenesTableBody');
        if (!data.items.length) { tb.innerHTML = '<tr><td colspan="7" class="text-center text-muted py-4">暂无场景</td></tr>'; return; }
        tb.innerHTML = data.items.map(function(s) {
            var st = s.is_active ? '<span class="badge bg-success">生效中</span>' : '<span class="badge bg-secondary">未生效</span>';
            var btn = s.is_active ? '<button class="btn btn-sm btn-outline-secondary" disabled>已激活</button>' : '<button class="btn btn-sm btn-outline-success" onclick="activateScene(' + s.id + ')"><i class="bi bi-power"></i> 激活</button>';
            return '<tr><td>' + s.id + '</td><td><strong>' + escapeHtml(s.name) + '</strong></td><td>' + escapeHtml(s.description || '') + '</td><td>' + (s.rule_count || 0) + '</td><td>' + st + '</td><td>' + (s.created_at || '') + '</td><td>' + btn + ' <button class="btn btn-sm btn-outline-primary" onclick="editScene(' + s.id + ')"><i class="bi bi-pencil"></i></button> <button class="btn btn-sm btn-outline-danger" onclick="deleteScene(' + s.id + ')"><i class="bi bi-trash"></i></button></td></tr>';
        }).join('');
        var active = data.items.find(function(s) { return s.is_active; });
        var el = document.getElementById('current-scene');
        if (el && active) { el.textContent = active.name; } else if (el) { el.textContent = '无'; }
    });
}
function openSceneModal(id) {
    document.getElementById('sceneId').value = id || '';
    document.getElementById('sceneModalTitle').textContent = id ? '编辑场景' : '新建场景';
    document.getElementById('sceneName').value = '';
    document.getElementById('sceneDescription').value = '';
    if (id) {
        fetch('/mock-admin/api/scenes').then(function(r) { return r.json(); }).then(function(data) {
            var s = data.items.find(function(x) { return x.id === id; });
            if (s) { document.getElementById('sceneName').value = s.name; document.getElementById('sceneDescription').value = s.description || ''; }
        });
    }
    new bootstrap.Modal(document.getElementById('sceneModal')).show();
}
function saveScene() {
    var id = document.getElementById('sceneId').value;
    var data = { name: document.getElementById('sceneName').value, description: document.getElementById('sceneDescription').value };
    if (!data.name) { showToast('错误', '场景名称不能为空', 'danger'); return; }
    var u = '/mock-admin/api/scenes', m = 'POST';
    if (id) { u = '/mock-admin/api/scenes/' + id; m = 'PUT'; }
    fetch(u, { method: m, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) })
        .then(function(r) { return r.json(); })
        .then(function() { bootstrap.Modal.getInstance(document.getElementById('sceneModal')).hide(); loadScenes(); showToast('成功', '场景保存成功', 'success'); })
        .catch(function() { showToast('错误', '保存失败，名称可能重复', 'danger'); });
}
function activateScene(id) {
    confirmAction('确定要激活此场景吗？', function() {
        fetch('/mock-admin/api/scenes/' + id + '/activate', { method: 'POST' })
            .then(function(r) { return r.json(); })
            .then(function() { loadScenes(); showToast('成功', '场景已激活', 'success'); });
    });
}
function editScene(id) { openSceneModal(id); }
function deleteScene(id) {
    confirmAction('确定要删除此场景吗？关联规则将变为全局规则。', function() {
        fetch('/mock-admin/api/scenes/' + id, { method: 'DELETE' })
            .then(function(r) { return r.json(); })
            .then(function() { loadScenes(); showToast('成功', '场景已删除', 'success'); });
    });
}
