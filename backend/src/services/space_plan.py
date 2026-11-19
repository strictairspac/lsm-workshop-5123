from sqlalchemy import select
from fastapi import HTTPException
from ..models.space_plan import SpacePlan
from ..models.dataset import Dataset
from ..constants.enums import SPACE_PLAN_STATUSES
from .space_planner import (
    SpacePlanError,build_plan,flush_tables,initial_state,materialize,
    _table_summary,verify_reads,
)
def _status(x):
    plan,state=x.plan,x.state
    if not plan.get('feasible'):return SPACE_PLAN_STATUSES[3]
    if state['current_bytes']<=plan['target_bytes']:return SPACE_PLAN_STATUSES[2]
    return SPACE_PLAN_STATUSES[0] if state['step_index']==0 else SPACE_PLAN_STATUSES[1]
def _snapshot_tables(x):
    snap=x.input_snapshot
    return flush_tables(snap['entries'],snap['memtable_limit'])
def _summary(x,detail=False):
    plan,state=x.plan,x.state
    data={
        'id':x.id,'name':x.name,'dataset_id':x.dataset_id,
        'memtable_limit':x.memtable_limit,'capacity_bytes':x.capacity_bytes,'target_bytes':x.target_bytes,
        'status':_status(x),'created_at':str(x.created_at),'updated_at':str(x.updated_at),
        'table_count':plan['table_count'],'initial_bytes':plan['initial_bytes'],
        'minimum_bytes':plan['minimum_bytes'],'current_bytes':state['current_bytes'],
        'peak_bytes':state['peak_bytes'],'step_index':state['step_index'],
        'total_steps':len(plan['steps']),'feasible':plan['feasible'],
    }
    if detail:
        tables=materialize(_snapshot_tables(x),state['groups'],state.get('dropped',False))
        next_step=plan['steps'][state['step_index']] if state['step_index']<len(plan['steps']) else None
        data.update({
            'input_snapshot':x.input_snapshot,
            'plan':plan,'state':state,
            'current_tables':[_table_summary(i,t) for i,t in enumerate(tables)],
            'read_values':state['read_values'],
            'reads_match_oracle':state['reads_match_oracle'],
            'actual_checksum':state['actual_checksum'],'oracle_checksum':state['oracle_checksum'],
            'next_step':next_step,
            'reason':plan.get('reason',''),
        })
    return data
def listing(db):return [_summary(x) for x in db.scalars(select(SpacePlan).order_by(SpacePlan.id.desc()))]
def detail(db,ident):
    x=db.get(SpacePlan,ident)
    if not x:raise HTTPException(404,'空间回收方案不存在')
    return _summary(x,True)
def create(db,data):
    dataset=db.get(Dataset,data.dataset_id)
    if not dataset:raise HTTPException(404,'写入批次不存在')
    if db.scalar(select(SpacePlan).where(SpacePlan.name==data.name)):raise HTTPException(409,'空间回收方案名称已存在')
    tables=flush_tables(dataset.entries,data.memtable_limit)
    plan=build_plan(tables,data.capacity_bytes,data.target_bytes)
    state=initial_state(tables,plan)
    check=verify_reads(tables,state,dataset.entries)
    if not check['ok'] or check['current_bytes']!=plan['initial_bytes']:
        raise HTTPException(409,'刷出表的读值与最后写入 Oracle 不一致，无法生成方案')
    state.update(
        reads_match_oracle=True,
        actual_checksum=check['actual_checksum'],
        oracle_checksum=check['oracle_checksum'],
    )
    x=SpacePlan(**data.model_dump(),input_snapshot={'entries':dataset.entries,'memtable_limit':data.memtable_limit},plan=plan,state=state)
    db.add(x);db.commit();return _summary(x,True)
def advance(db,ident,expected_step):
    x=db.scalar(select(SpacePlan).where(SpacePlan.id==ident).with_for_update())
    if not x:raise HTTPException(404,'空间回收方案不存在')
    plan,state=x.plan,x.state
    total=len(plan['steps'])
    if not plan.get('feasible'):raise HTTPException(409,plan.get('reason') or '该方案不可行，不能推进合并')
    current=state['step_index']
    if expected_step>current:
        raise HTTPException(409,f'进度不一致：当前已推进到第 {current} 步，请求预期第 {expected_step} 步')
    if expected_step<current or current>=total:
        # Replay of an already committed attempt: return progress without
        # applying anything, so retries never count space twice.
        db.commit()
        return {**_summary(x,True),'applied':False,'replayed':True}
    tables=_snapshot_tables(x)
    if len(tables)!=plan['table_count'] or any(
        _table_summary(i,t)['bytes']!=plan['initial_tables'][i]['bytes'] for i,t in enumerate(tables)
    ):
        db.rollback();raise HTTPException(409,'输入快照与方案不一致，请重新创建方案')
    step=plan['steps'][current]
    from .space_planner import apply_step
    try:next_state=apply_step(tables,state,step)
    except SpacePlanError as exc:
        db.rollback();raise HTTPException(409,str(exc))
    check=verify_reads(tables,next_state,x.input_snapshot['entries'])
    if not check['ok'] or check['current_bytes']!=next_state['current_bytes']:
        db.rollback();raise HTTPException(409,'合并后的读值与 Oracle 不一致，已拒绝本次切换')
    next_state['reads_match_oracle']=True
    next_state['actual_checksum']=check['actual_checksum']
    next_state['oracle_checksum']=check['oracle_checksum']
    x.state=next_state
    db.commit()
    return {**_summary(x,True),'applied':True,'replayed':False}
