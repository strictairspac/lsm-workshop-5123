import{spacePlans}from'../stores/space.js';
import{datasets}from'../stores/dataset.js';
import{create,detail,advance}from'../api/space.js';
import{header}from'../components/common/header.js';
import{table}from'../components/common/table.js';
import{esc,json}from'../utils/html.js';
import{submit,notify}from'../hooks/feedback.js';
import{session}from'../stores/session.js';
import{SPACE_PLAN_STATUSES}from'../constants/enums.js';

const statusLabel=s=>SPACE_PLAN_STATUSES[s]||s;
const fmt=b=>b>=1024?`${(b/1024).toFixed(1)} KiB`:`${b} B`;

function tableList(list){
  return '<div class="layers">'+list.map(t=>
    '<div class="layer"><span class="mono">表'+t.index+'</span><div class="slabs"><span class="slab">'+
    t.entries+' 条 / '+t.tombstones+' 墓碑<br>'+esc(t.min_key)+' … '+esc(t.max_key)+'<br>'+fmt(t.bytes)+
    '</span></div></div>').join('')+'</div>';
}

function planOption(x,id){
  return `<option value="${x.id}" ${x.id===id?'selected':''}>#${x.id} ${esc(x.name)} · ${esc(statusLabel(x.status))}</option>`;
}

function planRow(x){
  return [
    esc(x.name)+'<br><small>#'+x.id+' · '+x.table_count+' 张刷出表',
    '批次 #'+x.dataset_id+'<br><small>上限 '+fmt(x.capacity_bytes)+' / 目标 '+fmt(x.target_bytes)+'</small>',
    '<span class="badge '+esc(x.status)+'">'+esc(statusLabel(x.status))+'</span>',
    '<strong>'+fmt(x.current_bytes)+'</strong> / '+fmt(x.target_bytes)+'<br><small>峰值 '+fmt(x.peak_bytes)+' · '+x.step_index+'/'+x.total_steps+' 步</small>',
  ];
}

function renderForm(ds,writable){
  if(!writable)return '<p class="helper">观察员只读。</p>';
  return `<form id="space-form">
<label>方案名称</label><input name="name" required maxlength="120" placeholder="site-disk-reclaim-01">
<label>刷出表来源批次</label><select name="dataset_id">${ds.map(x=>`<option value="${x.id}">${esc(x.name)} / ${x.operation_count}条</option>`).join('')}</select>
<label>Memtable 不同键容量 · 2–64</label><input type="number" name="memtable_limit" min="2" max="64" value="2" required>
<label>容量上限 · 字节</label><input type="number" name="capacity_bytes" min="1" value="4096" required>
<label>收缩目标 · 字节</label><input type="number" name="target_bytes" min="0" value="512" required>
<div class="actions"><button class="primary" type="submit">生成合并方案</button></div></form>
<p class="helper">最多 8 张刷出表。方案按固定编码计算字节；若不存在合规的合并顺序，会明确给出不可行原因。</p>`;
}

function renderAction(p,writable){
  const next=p.next_step;
  if(p.feasible&&next){
    return `<form id="advance-form"><button class="primary" type="button" ${writable?'':'disabled'} data-advance="${p.id}" data-step="${p.step_index}">执行第${next.index+1}步 · ${next.drop_tombstones?'完整合并并清墓碑':'合并保留墓碑'}</button></form>
<p class="helper">本步输入表 ${next.inputs.map(g=>g.map(i=>'#'+i).join('+')).join(' / ')}；切换峰值 ${fmt(next.peak_bytes)}；完成后占用 ${fmt(next.total_after)}。重试同一步不会重复占额。</p>`;
  }
  if(p.feasible)return '<p class="helper">全部步骤已执行，方案达到收缩目标。</p>';
  return '<p class="error">'+esc(p.reason||'该方案不可行')+'</p>';
}

function renderDetail(p,writable){
  const stepRows=p.plan.steps.map(s=>[
    s.index+1,
    s.drop_tombstones?'完整合并·清墓碑':'合并·保留墓碑',
    fmt(s.total_before)+' → '+fmt(s.total_after),
    fmt(s.peak_bytes),
  ]);
  const peakRows=p.state.peak_history.map(h=>[esc(h.label),fmt(h.bytes)]);
  const initialRows=p.plan.initial_tables.map(t=>['#'+t.index,t.entries,t.tombstones,fmt(t.bytes)]);
  return `<section class="panel">
<div class="actions" style="margin-top:0"><h2 style="margin:0">${esc(p.name)}</h2><span class="badge ${esc(p.status)}">${esc(statusLabel(p.status))}</span></div>
<div class="metrics">
<div><small>当前占用 / 目标</small><strong>${fmt(p.current_bytes)} / ${fmt(p.target_bytes)}</strong></div>
<div><small>已记录峰值</small><strong>${fmt(p.peak_bytes)}</strong></div>
<div><small>容量上限</small><strong>${fmt(p.capacity_bytes)}</strong></div>
<div><small>完整合并后最少</small><strong>${fmt(p.minimum_bytes)}</strong></div>
</div>
${renderAction(p,writable)}
<div class="split"><section>
<h2>当前表 ${p.current_tables.length} 张</h2>
${p.current_tables.length?tableList(p.current_tables):'<div class="empty">当前没有表（全部键已删除）。</div>'}
<h2>读值核对</h2>
<p class="helper">当前读值摘要 ${esc(p.actual_checksum.slice(0,16))}… / Oracle ${esc(p.oracle_checksum.slice(0,16))}… · ${p.reads_match_oracle?'一致':'不一致'}</p>
${table(['键','当前读值'],Object.entries(p.read_values).map(([k,v])=>[esc(k),esc(v)]))}
</section><aside>
<h2>逐步方案</h2>
${p.plan.steps.length?table(['步','动作','输入→输出','峰值'],stepRows):'<div class="empty">无需合并步骤。</div>'}
<h2 style="margin-top:25px">峰值记录</h2>
${table(['阶段','占用字节'],peakRows)}
<h2 style="margin-top:25px">初始刷出表</h2>
${table(['表','条数','墓碑','字节'],initialRows)}
<details><summary>查看输入快照</summary><pre class="json">${json(p.input_snapshot)}</pre></details>
</aside></div></section>`;
}

export async function spacePage(el){
  const plans=await spacePlans.load();
  const ds=await datasets.load();
  const writable=session.get().role!=='viewer';
  const id=Number(location.hash.split('/')[1])||plans[0]?.id;
  const p=id?await detail(id):null;
  el.innerHTML=header('05','把刷出的表压回容量线','按固定编码统计每张刷出表的字节；新表落稳前新旧同时占盘，峰值按切换瞬间计算。只有覆盖全部表的完整合并才能清理墓碑。')
    +'<p class="modelnote">字节口径为 canonical-json-v1：UTF-8 规范 JSON，键排序；空表按 0 字节。这是规划模型，不代表真实磁盘块或物理 I/O。</p>'
    +'<div class="split"><section class="panel"><h2>空间回收方案</h2>'
    +'<div class="actions" style="margin:0 0 16px"><select id="plan-choose">'+plans.map(x=>planOption(x,id)).join('')+'</select></div>'
    +table(['方案','输入/限额','状态','当前/目标'],plans.map(planRow))
    +'</section><aside class="panel"><h2>'+(writable?'登记回收方案':'限额参数')+'</h2>'+renderForm(ds,writable)+'</aside></div>'
    +(p?renderDetail(p,writable):'<div class="empty">还没有空间回收方案。先选择批次与限额，生成可执行方案。</div>');

  const choose=el.querySelector('#plan-choose');
  if(choose)choose.onchange=e=>{location.hash='space/'+e.target.value;spacePage(el)};

  const form=el.querySelector('#space-form');
  if(form)form.onsubmit=e=>{
    e.preventDefault();
    submit(e.target,async()=>{
      const d=Object.fromEntries(new FormData(e.target));
      for(const k of ['dataset_id','memtable_limit','capacity_bytes','target_bytes'])d[k]=Number(d[k]);
      const created=await create(d);
      notify('空间回收方案已生成');
      location.hash='space/'+created.id;
      await spacePage(el);
    });
  };

  el.querySelectorAll('[data-advance]').forEach(button=>{
    button.onclick=()=>{
      button.disabled=true;
      submit(button,async()=>{
        try{
          const r=await advance(Number(button.dataset.advance),Number(button.dataset.step));
          notify(r.replayed?'该步骤已执行过，进度未重复占额':(r.applied?'合并切换完成':'没有可推进的步骤'));
          location.hash='space/'+r.id;
          await spacePage(el);
        }catch(e){
          button.disabled=false;
          throw e;
        }
      });
    };
  });
}
