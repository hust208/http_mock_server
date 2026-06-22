document.addEventListener('DOMContentLoaded', loadRules);
function loadRules() {
    var s = document.getElementById('filterScene').value;
    var u = '/mock-admin/api/rules' + (s ? '?scene_id=' + s : '');
    fetch(u).then(function(r){return r.json();}).then(function(d){renderRules(d.items);});
}
function renderRules(rules) {
    var tb = document.getElementById('rulesTableBody');
    if (!rules.length) { tb.innerHTML = '<tr><td colspan="13" class="text-center text-muted py-4">暂无规则</td></tr>'; return; }
    var mc = {'GET':'bg-success','POST':'bg-primary','PUT':'bg-warning','DELETE':'bg-danger','PATCH':'bg-info','ANY':'bg-secondary'};
    tb.innerHTML = rules.map(function(r){
        var cond = (r.match_conditions&&r.match_conditions.length) ? r.match_conditions.map(function(c){
            return '<span class="badge bg-info">'+c.source+'.'+c.field+' '+c.operator+' '+(c.value||'')+'</span>';
        }).join(' ') : '<span class="text-muted">无</span>';
        var b = '<span class="badge '+(mc[r.method]||'bg-secondary')+'">'+r.method+'</span>';
        var sw = '<div class="form-check form-switch"><input type="checkbox" class="form-check-input" '+(r.enabled?'checked':'')+' onchange="toggleRule('+r.id+',this.checked)"></div>';
        return '<tr><td><input type="checkbox" class="form-check-input rule-checkbox" value="'+r.id+'"></td><td>'+sw+'</td><td>'+r.priority+'</td><td>'+escapeHtml(r.name)+'</td><td>'+b+'</td><td><code>'+escapeHtml(r.url_pattern)+'</code></td><td>'+r.url_match_type+'</td><td style="max-width:300px;">'+cond+'</td><td>'+r.response_status+'</td><td>'+(r.delay_ms>0?r.delay_ms:'-')+'</td><td>'+(r.scene_name||'全局')+'</td><td>'+r.hit_count+'</td><td><button class="btn btn-sm btn-outline-primary" onclick="editRule('+r.id+')"><i class="bi bi-pencil"></i></button> <button class="btn btn-sm btn-outline-info" onclick="copyRule('+r.id+')"><i class="bi bi-files"></i></button> <button class="btn btn-sm btn-outline-danger" onclick="deleteRule('+r.id+')"><i class="bi bi-trash"></i></button></td></tr>';
    }).join('');
}
function toggleSelectAll(cb) { document.querySelectorAll('.rule-checkbox').forEach(function(c){c.checked=cb.checked;}); }
function toggleRule(id, en) {
    fetch('/mock-admin/api/rules/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:en})}).then(function(r){return r.json();}).then(function(){loadRules();});
}
function openRuleModal(id) {
    document.getElementById('ruleId').value = id || '';
    document.getElementById('ruleModalTitle').textContent = id ? '编辑规则' : '新增规则';
    var f = ['ruleName','ruleUrl','ruleDescription','ruleResponseBody'];
    f.forEach(function(i){ document.getElementById(i).value = ''; });
    document.getElementById('ruleMethod').value='GET';
    document.getElementById('rulePriority').value='100';
    document.getElementById('ruleUrlMatchType').value='exact';
    document.getElementById('ruleScene').value='';
    document.getElementById('ruleResponseStatus').value='200';
    document.getElementById('ruleResponseHeaders').value='{"Content-Type": "application/json"}';
    document.getElementById('ruleDelayMs').value='0';
    document.getElementById('ruleTimeout').checked=false;
    document.getElementById('ruleRandomException').checked=false;
    document.getElementById('ruleExceptionProbability').value='50';
    document.getElementById('ruleExceptionStatus').value='500';
    document.getElementById('matchConditions').innerHTML='';
    addMatchCondition();
    if (id) {
        fetch('/mock-admin/api/rules/'+id).then(function(r){return r.json();}).then(function(r){
            document.getElementById('ruleName').value=r.name||'';
            document.getElementById('ruleMethod').value=r.method||'GET';
            document.getElementById('rulePriority').value=r.priority||100;
            document.getElementById('ruleUrl').value=r.url_pattern||'';
            document.getElementById('ruleUrlMatchType').value=r.url_match_type||'exact';
            document.getElementById('ruleScene').value=r.scene_id||'';
            document.getElementById('ruleDescription').value=r.description||'';
            document.getElementById('ruleResponseStatus').value=r.response_status||200;
            document.getElementById('ruleResponseHeaders').value=JSON.stringify(r.response_headers||{},null,2);
            document.getElementById('ruleResponseBody').value=r.response_body||'';
            document.getElementById('ruleDelayMs').value=r.delay_ms||0;
            document.getElementById('ruleTimeout').checked=!!r.timeout_enabled;
            document.getElementById('ruleRandomException').checked=!!r.random_exception_enabled;
            document.getElementById('ruleExceptionProbability').value=r.random_exception_probability||50;
            document.getElementById('ruleExceptionStatus').value=r.random_exception_status||500;
            document.getElementById('matchConditions').innerHTML='';
            if(r.match_conditions&&r.match_conditions.length){r.match_conditions.forEach(function(c){addMatchCondition(c);});}else{addMatchCondition();}
        });
    }
    new bootstrap.Modal(document.getElementById('ruleModal')).show();
}
function addMatchCondition(d) {
    var div = document.createElement('div');
    div.className = 'match-condition-row row mb-2 align-items-end';
    div.innerHTML = '<div class="col-md-2"><select class="form-select form-select-sm match-source"><option value="header">Header</option><option value="query">Query</option><option value="body">Body</option></select></div><div class="col-md-3"><input type="text" class="form-control form-control-sm match-field" placeholder="字段名"></div><div class="col-md-2"><select class="form-select form-select-sm match-operator"><option value="equals">等于</option><option value="not_equals">不等于</option><option value="contains">包含</option><option value="not_contains">不包含</option><option value="regex">正则匹配</option><option value="exists">存在</option><option value="not_exists">不存在</option><option value="greater_than">大于</option><option value="less_than">小于</option></select></div><div class="col-md-4"><input type="text" class="form-control form-control-sm match-value" placeholder="匹配值"></div><div class="col-md-1"><button type="button" class="btn btn-sm btn-outline-danger" onclick="removeMatchCondition(this)"><i class="bi bi-dash"></i></button></div>';
    document.getElementById('matchConditions').appendChild(div);
    if (d) { div.querySelector('.match-source').value=d.source||'header'; div.querySelector('.match-field').value=d.field||''; div.querySelector('.match-operator').value=d.operator||'equals'; div.querySelector('.match-value').value=d.value||''; }
}
function removeMatchCondition(b) { if(document.querySelectorAll('.match-condition-row').length>1){b.closest('.match-condition-row').remove();} }
function saveRule() {
    var id = document.getElementById('ruleId').value;
    var cond = [];
    document.querySelectorAll('.match-condition-row').forEach(function(row){
        var f = row.querySelector('.match-field').value.trim();
        if(f){cond.push({source:row.querySelector('.match-source').value,field:f,operator:row.querySelector('.match-operator').value,value:row.querySelector('.match-value').value});}
    });
    var data = {name:document.getElementById('ruleName').value,method:document.getElementById('ruleMethod').value,priority:parseInt(document.getElementById('rulePriority').value)||100,url_pattern:document.getElementById('ruleUrl').value,url_match_type:document.getElementById('ruleUrlMatchType').value,scene_id:document.getElementById('ruleScene').value?parseInt(document.getElementById('ruleScene').value):null,description:document.getElementById('ruleDescription').value,response_status:parseInt(document.getElementById('ruleResponseStatus').value)||200,response_headers:document.getElementById('ruleResponseHeaders').value,response_body:document.getElementById('ruleResponseBody').value,delay_ms:parseInt(document.getElementById('ruleDelayMs').value)||0,timeout_enabled:document.getElementById('ruleTimeout').checked,random_exception_enabled:document.getElementById('ruleRandomException').checked,random_exception_probability:parseInt(document.getElementById('ruleExceptionProbability').value)||0,random_exception_status:parseInt(document.getElementById('ruleExceptionStatus').value)||500,match_conditions:cond};
    if(!data.name||!data.url_pattern){showToast('错误','规则名称和URL不能为空','danger');return;}
    var u='/mock-admin/api/rules',m='POST';
    if(id){u='/mock-admin/api/rules/'+id;m='PUT';}
    fetch(u,{method:m,headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}).then(function(r){return r.json();}).then(function(){bootstrap.Modal.getInstance(document.getElementById('ruleModal')).hide();loadRules();showToast('成功','规则保存成功','success');}).catch(function(){showToast('错误','保存失败','danger');});
}
function editRule(id){openRuleModal(id);}
function copyRule(id){fetch('/mock-admin/api/rules/'+id).then(function(r){return r.json();}).then(function(r){r.name=r.name+'_copy';delete r.id;fetch('/mock-admin/api/rules',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(r)}).then(function(){loadRules();showToast('成功','规则已复制','success');});});}
function deleteRule(id){confirmAction('确定要删除此规则吗？',function(){fetch('/mock-admin/api/rules/'+id,{method:'DELETE'}).then(function(r){return r.json();}).then(function(){loadRules();showToast('成功','规则已删除','success');});});}
function batchAction(action){var ids=[];document.querySelectorAll('.rule-checkbox:checked').forEach(function(c){ids.push(parseInt(c.value));});if(!ids.length){showToast('提示','请先选择规则','warning');return;}var msg=action==='delete'?'确定要删除选中的规则吗？':'确定要'+(action==='enable'?'启用':'禁用')+'选中的规则吗？';confirmAction(msg,function(){fetch('/mock-admin/api/rules/batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids:ids,action:action})}).then(function(r){return r.json();}).then(function(){loadRules();showToast('成功','批量操作完成','success');});});}
