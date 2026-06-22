var currentPage = 1;
document.addEventListener('DOMContentLoaded', function() { loadRequests(1); });
function loadRequests(page) {
    currentPage = page || 1;
    var p = 'page=' + currentPage + '&per_page=20';
    var v;
    if (v = document.getElementById('filterUrl').value) p += '&url=' + encodeURIComponent(v);
    if (v = document.getElementById('filterMethod').value) p += '&method=' + v;
    if (v = document.getElementById('filterResult').value) p += '&match_result=' + v;
    if (v = document.getElementById('filterStart').value) p += '&start_time=' + v.replace('T', ' ');
    if (v = document.getElementById('filterEnd').value) p += '&end_time=' + v.replace('T', ' ');
    fetch('/mock-admin/api/requests?' + p).then(function(r) { return r.json(); }).then(function(d) { renderRequests(d); });
}
function renderRequests(data) {
    var tb = document.getElementById('requestsTableBody');
    var items = data.items || [];
    if (!items.length) { tb.innerHTML = '<tr><td colspan="8" class="text-center text-muted py-4">暂无记录</td></tr>'; return; }
    var mc = {'GET':'bg-success','POST':'bg-primary','PUT':'bg-warning','DELETE':'bg-danger','PATCH':'bg-info'};
    var rc = {'matched':'bg-success','forwarded':'bg-info','unmatched':'bg-secondary'};
    tb.innerHTML = items.map(function(r) {
        return '<tr><td>'+r.created_at+'</td><td><code>'+r.request_id+'</code></td><td><span class="badge '+(mc[r.method]||'bg-secondary')+'">'+r.method+'</span></td><td><code>'+escapeHtml(r.path)+'</code></td><td><span class="badge '+(rc[r.match_result]||'bg-secondary')+'">'+r.match_result+'</span></td><td>'+r.response_status+'</td><td>'+r.response_time_ms+'ms</td><td><button class="btn btn-sm btn-outline-primary" onclick="viewDetail(\''+r.request_id+'\')"><i class="bi bi-eye"></i></button></td></tr>';
    }).join('');
    document.getElementById('pageInfo').textContent = '共'+data.total+'条, 第'+data.page+'/'+(data.total_pages||1)+'页';
    var h = '<ul class="pagination pagination-sm">';
    if (data.page > 1) h += '<li class="page-item"><a class="page-link" href="javascript:loadRequests('+(data.page-1)+')">上一页</a></li>';
    for (var i = Math.max(1,data.page-2); i <= Math.min(data.total_pages,data.page+2); i++) h += '<li class="page-item '+(i===data.page?'active':'')+'"><a class="page-link" href="javascript:loadRequests('+i+')">'+i+'</a></li>';
    if (data.page < data.total_pages) h += '<li class="page-item"><a class="page-link" href="javascript:loadRequests('+(data.page+1)+')">下一页</a></li>';
    document.getElementById('pagination').innerHTML = h + '</ul>';
}
function viewDetail(rid) {
    fetch('/mock-admin/api/requests/' + rid).then(function(r) { return r.json(); }).then(function(r) {
        var h = '<table class="table table-sm"><tbody>';
        h += '<tr><th width="100">Request ID</th><td><code>'+r.request_id+'</code></td></tr>';
        h += '<tr><th>方法</th><td>'+r.method+'</td></tr>';
        h += '<tr><th>路径</th><td><code>'+escapeHtml(r.path)+'</code></td></tr>';
        h += '<tr><th>匹配结果</th><td>'+r.match_result+'</td></tr>';
        h += '<tr><th>状态码</th><td>'+r.response_status+'</td></tr>';
        h += '<tr><th>耗时</th><td>'+r.response_time_ms+'ms</td></tr>';
        h += '</tbody></table>';
        h += '<h6>Headers</h6><pre class="json-viewer">'+formatJson(r.headers||{})+'</pre>';
        h += '<h6 class="mt-2">Query</h6><pre class="json-viewer">'+formatJson(r.query_params||{})+'</pre>';
        h += '<h6 class="mt-2">Body</h6><pre class="json-viewer">'+escapeHtml(r.body||'(空)')+'</pre>';
        document.getElementById('requestDetailBody').innerHTML = h;
        new bootstrap.Modal(document.getElementById('requestModal')).show();
    });
}
