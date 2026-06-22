/* HTTP Mock Server - Shared JS utilities */
function showToast(title, message, type) {
    var toast = document.getElementById('toast');
    document.getElementById('toast-title').textContent = title;
    document.getElementById('toast-body').textContent = message;
    toast.className = 'toast';
    if (type === 'success') { toast.classList.add('text-bg-success'); }
    else if (type === 'danger') { toast.classList.add('text-bg-danger'); }
    else if (type === 'warning') { toast.classList.add('text-bg-warning'); }
    else { toast.classList.add('text-bg-primary'); }
    new bootstrap.Toast(toast, { delay: 3000 }).show();
}
function escapeHtml(str) {
    if (!str) return '';
    var div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}
function confirmAction(message, callback) {
    document.getElementById('confirmModalBody').textContent = message;
    var modal = new bootstrap.Modal(document.getElementById('confirmModal'));
    var okBtn = document.getElementById('confirmModalOk');
    okBtn.onclick = function() { modal.hide(); if (callback) callback(); };
    modal.show();
}
function formatJson(obj) {
    try { return JSON.stringify(obj, null, 2); } catch (e) { return String(obj); }
}
function copyToClipboard(text) {
    var textarea = document.createElement('textarea');
    textarea.value = text;
    document.body.appendChild(textarea);
    textarea.select();
    try { document.execCommand('copy'); showToast('已复制', '内容已复制到剪贴板', 'success'); }
    catch (e) { showToast('复制失败', '', 'danger'); }
    document.body.removeChild(textarea);
}
document.addEventListener('DOMContentLoaded', function() {
    fetch('/mock-admin/api/scenes').then(function(r) { return r.json(); }).then(function(data) {
        var active = data.items.find(function(s) { return s.is_active; });
        var el = document.getElementById('current-scene');
        if (el && active) { el.textContent = active.name; }
        else if (el) { el.textContent = '无'; }
    }).catch(function() {});
});
