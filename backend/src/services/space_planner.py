"""Deterministic space-reclamation planner over a batch's memtable flush tables.

The planner deliberately works on the raw SSTables produced by memtable
flushes (before the ordinary tiered compaction).  That is the set where
duplicate versions and tombstones still occupy space, which is what the
space-reclamation experiment is about.

Byte accounting uses one fixed, versioned encoding (``canonical-json-v1``):

    utf8(json.dumps({key: [seq, deleted, value], ...},
                    ensure_ascii=False, sort_keys=True,
                    separators=(',', ':')))

Keys are ordered, so the encoding is deterministic.  An empty table is not
written to disk and counts as 0 bytes.

A merge step replaces a subset of the current tables with one new table.
Until the new table is durable, the old subset and the new table occupy disk
together, therefore the step peak is::

    sum(bytes of all current tables) + bytes of the new table

Tombstones may only be removed by a merge that covers *all* current tables
(a full-history merge).  Any partial merge keeps every tombstone, because a
tombstone that still shadows an older value in a table outside the merge must
never be dropped.
"""
from __future__ import annotations

import json
from collections import deque
from itertools import combinations

MAX_TABLES = 8
ENCODING = 'canonical-json-v1'
ENCODING_NOTE = (
    'canonical-json-v1：UTF-8 字节数；每张表编码为 '
    '{"key":[seq,deleted,value],...} 的紧凑规范 JSON（键排序，空表 0 字节）'
)


class SpacePlanError(ValueError):
    """Invalid source data or plan parameters."""


def _check_entries(entries, memtable_limit):
    if not isinstance(entries, list) or not 1 <= len(entries) <= 2000:
        raise SpacePlanError('操作数必须在1到2000之间')
    if not isinstance(memtable_limit, int) or not 2 <= memtable_limit <= 64:
        raise SpacePlanError('memtable键容量必须在2到64之间')
    for e in entries:
        if not isinstance(e, dict) or e.get('op') not in ('put', 'delete'):
            raise SpacePlanError('操作或键不合法')
        key = e.get('key')
        if not isinstance(key, str) or not key or len(key) > 64:
            raise SpacePlanError('操作或键不合法')
        if e['op'] == 'put':
            value = e.get('value')
            if not isinstance(value, str) or len(value) > 200:
                raise SpacePlanError('put必须包含不超过200字的字符串')


def flush_tables(entries, memtable_limit):
    """Return the raw L0 tables produced by memtable flushes.

    No tiered compaction is applied.  Every table maps key -> row with the
    global operation sequence number, so later versions always win.
    """
    _check_entries(entries, memtable_limit)
    tables = []
    mem = {}

    def flush():
        if mem:
            tables.append(dict(sorted(mem.items())))
            mem.clear()

    for seq, e in enumerate(entries, 1):
        deleted = e['op'] == 'delete'
        mem[e['key']] = {
            'seq': seq,
            'value': None if deleted else e['value'],
            'deleted': deleted,
        }
        if len(mem) >= memtable_limit:
            flush()
    flush()
    return tables


def merge_tables(tables, drop_tombstones=False):
    """Latest-sequence merge; tombstones are kept unless explicitly dropped.

    ``drop_tombstones`` is only safe when the caller has proved that the
    tables cover the complete history of the batch.
    """
    latest = {}
    for table in tables:
        for key, row in table.items():
            current = latest.get(key)
            if current is None or current['seq'] < row['seq']:
                latest[key] = row
    if drop_tombstones:
        latest = {k: v for k, v in latest.items() if not v['deleted']}
    return dict(sorted(latest.items()))


def encode_table(table):
    """Return the canonical-json-v1 text for one table."""
    if not table:
        return ''
    payload = {
        key: [row['seq'], bool(row['deleted']), row['value']]
        for key, row in sorted(table.items())
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def table_bytes(table):
    """Fixed-encoding byte size of one table (0 for an empty table)."""
    return len(encode_table(table).encode('utf-8'))


def read_values(tables):
    """Values a reader sees from the current table set (latest wins)."""
    latest = merge_tables(tables)
    return {k: v['value'] for k, v in sorted(latest.items()) if not v['deleted']}


def oracle_values(entries):
    oracle = {}
    for e in entries:
        if e['op'] == 'delete':
            oracle.pop(e['key'], None)
        else:
            oracle[e['key']] = e['value']
    return dict(sorted(oracle.items()))


def checksum(values):
    import hashlib
    raw = json.dumps(values, ensure_ascii=False, sort_keys=True).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _table_summary(index, table):
    return {
        'index': index,
        'entries': len(table),
        'tombstones': sum(1 for row in table.values() if row['deleted']),
        'min_key': min(table) if table else None,
        'max_key': max(table) if table else None,
        'bytes': table_bytes(table),
    }


def _evenly_sorted_groups(blocks):
    return tuple(sorted(blocks, key=lambda mask: ((mask & -mask), mask)))


def _mask_members(mask):
    members = []
    i = 0
    while mask:
        if mask & 1:
            members.append(i)
        mask >>= 1
        i += 1
    return members


def _group_lists(blocks):
    return [sorted(_mask_members(mask)) for mask in blocks]


def _plan_peak(initial_bytes, steps):
    return max([initial_bytes] + [s['peak_bytes'] for s in steps])


def _empty_plan_result(tables, capacity_bytes, target_bytes):
    n = len(tables)
    initial_bytes = sum(table_bytes(t) for t in tables)
    full = merge_tables(tables)
    dropped = merge_tables(tables, drop_tombstones=True)
    return {
        'encoding': ENCODING,
        'encoding_note': ENCODING_NOTE,
        'max_tables': MAX_TABLES,
        'table_count': n,
        'initial_tables': [_table_summary(i, t) for i, t in enumerate(tables)],
        'initial_bytes': initial_bytes,
        'full_merge_bytes': table_bytes(full),
        'minimum_bytes': table_bytes(dropped),
        'capacity_bytes': capacity_bytes,
        'target_bytes': target_bytes,
        'peak_bytes': initial_bytes,
        'steps': [],
        'feasible': True,
        'reason': '',
    }


def build_plan(tables, capacity_bytes, target_bytes):
    """Find a compliant merge order, or explain why none exists.

    State is the current partition of the original flush tables.  BFS over
    partitions returns a shortest executable sequence; capacity filters every
    transition by its before-settle peak.  The only transition allowed to drop
    tombstones is a merge of every current table (complete history).
    """
    n = len(tables)
    if not 1 <= n <= MAX_TABLES:
        raise SpacePlanError(f'批次刷出表数量为 {n}，必须为 1 到 {MAX_TABLES} 张')
    if capacity_bytes <= 0:
        raise SpacePlanError('容量上限必须大于0字节')
    if target_bytes < 0:
        raise SpacePlanError('收缩目标不能为负数')
    if target_bytes >= capacity_bytes:
        raise SpacePlanError('收缩目标必须小于容量上限')

    if sum(table_bytes(t) for t in tables) <= target_bytes:
        # Already under target: no merge is necessary, the current usage is
        # the peak.  target_bytes < capacity_bytes is enforced by the API, so
        # the current usage is necessarily within the limit.
        return _empty_plan_result(tables, capacity_bytes, target_bytes)

    size = 1 << n
    full_mask = size - 1
    subset_bytes = [0] * size
    for mask in range(1, size):
        subset_bytes[mask] = table_bytes(
            merge_tables([tables[i] for i in _mask_members(mask)])
        )
    drop_table = merge_tables(tables, drop_tombstones=True)
    drop_bytes = table_bytes(drop_table)
    full_keep_bytes = subset_bytes[full_mask]
    initial_bytes = sum(subset_bytes[1 << i] for i in range(n))
    initial_blocks = _evenly_sorted_groups(tuple(1 << i for i in range(n)))

    plan = _empty_plan_result(tables, capacity_bytes, target_bytes)
    plan['full_merge_bytes'] = full_keep_bytes
    plan['minimum_bytes'] = drop_bytes

    if initial_bytes > capacity_bytes:
        plan.update(
            feasible=False,
            reason=(
                f'初始 {n} 张表已占 {initial_bytes} 字节，高于容量上限 '
                f'{capacity_bytes} 字节，任何合并的前置占用都已超限'
            ),
        )
        return plan
    if drop_bytes > target_bytes:
        plan.update(
            feasible=False,
            reason=(
                f'即使完整合并并安全清理墓碑，最少仍需 {drop_bytes} 字节，高于收缩目标 '
                f'{target_bytes} 字节'
            ),
        )
        return plan

    def blocks_total(blocks):
        return sum(subset_bytes[b] for b in blocks)

    start = (initial_blocks, False)
    queue = deque([start])
    parent = {start: None}
    goal = None

    def push(state, previous, step, total_after):
        nonlocal goal
        if state in parent:
            return
        parent[state] = (previous, step)
        if total_after <= target_bytes:
            goal = state
        else:
            queue.append(state)

    while queue and goal is None:
        blocks, dropped = queue.popleft()
        total = blocks_total(blocks)
        k = len(blocks)

        # A single table covering the complete history may be rewritten with
        # tombstones dropped (same rule as the full-history compaction).
        if k == 1 and not dropped and blocks[0] == full_mask:
            peak = total + drop_bytes
            if peak <= capacity_bytes and total > drop_bytes:
                total_after = 0 if not drop_table else drop_bytes
                step = {
                    'inputs': [sorted(_mask_members(blocks[0]))],
                    'merged': sorted(range(n)),
                    'input_bytes': total,
                    'output_bytes': drop_bytes,
                    'total_before': total,
                    'total_after': total_after,
                    'peak_bytes': peak,
                    'drop_tombstones': True,
                    'tombstones_removed': sum(
                        1 for row in merge_tables(tables).values() if row['deleted']
                    ),
                }
                push(((), True), (blocks, dropped), step, total_after)
                if goal is not None:
                    break

        # Merge at least two current tables; larger subsets first so compact
        # orders are found early, but every partition stays reachable.
        for r in range(k, 1, -1):
            for combo in combinations(range(k), r):
                merged_mask = 0
                input_bytes = 0
                for i in combo:
                    merged_mask |= blocks[i]
                    input_bytes += subset_bytes[blocks[i]]
                remaining = tuple(
                    blocks[i] for i in range(k) if i not in combo
                ) + (merged_mask,)
                inputs = [sorted(_mask_members(blocks[i])) for i in combo]

                # A merge of every current table covers the complete history,
                # so it is the only kind allowed to drop tombstones.
                whole_history = r == k and merged_mask == full_mask
                variants = [False]
                if whole_history and not dropped:
                    variants.append(True)

                for drop in variants:
                    output_bytes = drop_bytes if drop else subset_bytes[merged_mask]
                    peak = total + output_bytes
                    if peak > capacity_bytes:
                        continue
                    if drop:
                        state = ((), True)
                        total_after = 0 if not drop_table else drop_bytes
                    else:
                        state = (_evenly_sorted_groups(remaining), False)
                        total_after = total - input_bytes + output_bytes
                    step = {
                        'inputs': inputs,
                        'merged': sorted(_mask_members(merged_mask)),
                        'input_bytes': input_bytes,
                        'output_bytes': output_bytes,
                        'total_before': total,
                        'total_after': total_after,
                        'peak_bytes': peak,
                        'drop_tombstones': drop,
                        'tombstones_removed': (
                            sum(1 for row in merge_tables(tables).values() if row['deleted'])
                            if drop else 0
                        ),
                    }
                    push(state, (blocks, dropped), step, total_after)
            if goal is not None:
                break

    if goal is None:
        plan.update(
            feasible=False,
            reason=(
                f'在容量上限 {capacity_bytes} 字节内找不到达到收缩目标 '
                f'{target_bytes} 字节的合规合并顺序（完整合并后最少 {drop_bytes} 字节）'
            ),
        )
        return plan

    steps = []
    state = goal
    while parent[state] is not None:
        previous, step = parent[state]
        steps.append(step)
        state = previous
    steps.reverse()
    for i, step in enumerate(steps):
        step['index'] = i
        step['event'] = (
            'full_merge_drop_tombstones'
            if step['drop_tombstones']
            else 'merge_keep_tombstones'
        )
        step['atomic_swap'] = True
    plan['steps'] = steps
    plan['peak_bytes'] = _plan_peak(initial_bytes, steps)
    plan['feasible'] = True
    plan['reason'] = ''
    return plan


def initial_state(original_tables, plan):
    """Persistable execution state for a freshly created plan."""
    values = read_values(original_tables)
    return {
        'step_index': 0,
        'groups': [[i] for i in range(len(original_tables))],
        'dropped': False,
        'generation': 0,
        'current_bytes': plan['initial_bytes'],
        'peak_bytes': plan['initial_bytes'],
        'peak_history': [{'label': '初始', 'bytes': plan['initial_bytes']}],
        'applied_steps': [],
        'read_values': values,
        'reads_match_oracle': True,
        'actual_checksum': checksum(values),
        'oracle_checksum': checksum(values),
    }


def materialize(original_tables, groups, dropped=False):
    """Current table set implied by the persisted groups."""
    result = []
    for group in groups:
        table = merge_tables([original_tables[i] for i in group])
        if dropped:
            table = {k: v for k, v in table.items() if not v['deleted']}
        if table:
            result.append(table)
    return result


def apply_step(original_tables, state, step):
    """Apply one plan step atomically and return the next state.

    Raises SpacePlanError when the step does not match the persisted progress;
    callers translate that into a conflict without modifying anything.
    """
    groups = [list(g) for g in state['groups']]
    inputs = [list(g) for g in step['inputs']]
    remaining = [list(g) for g in groups]
    for group in inputs:
        if group not in remaining:
            raise SpacePlanError('步骤与当前表集合不一致，请刷新方案后重试')
        remaining.remove(group)

    merged_group = sorted({i for group in inputs for i in group})
    if merged_group != list(step['merged']):
        raise SpacePlanError('步骤输入与合并范围不一致，请刷新方案后重试')
    remaining.append(merged_group)
    remaining.sort(key=lambda g: (g[0] if g else -1, len(g)))

    dropped = bool(state.get('dropped')) or bool(step.get('drop_tombstones'))
    if dropped:
        if remaining != [sorted(range(len(original_tables)))]:
            raise SpacePlanError('只有覆盖全部表的完整合并才能清理墓碑')
    if step.get('drop_tombstones') and step['merged'] != sorted(range(len(original_tables))):
        raise SpacePlanError('只有覆盖全部表的完整合并才能清理墓碑')

    new_tables = materialize(original_tables, remaining, dropped)
    output_bytes = table_bytes(
        merge_tables(
            [original_tables[i] for i in step['merged']],
            drop_tombstones=bool(step.get('drop_tombstones')),
        )
    )
    if output_bytes != step['output_bytes']:
        raise SpacePlanError('新表字节与方案不一致，请重新生成方案')

    total_before = state['current_bytes']
    peak = total_before + output_bytes
    if peak > step['peak_bytes']:
        raise SpacePlanError('实际峰值超过方案峰值，请重新生成方案')

    if dropped and not new_tables:
        # The full-history merge dropped every key; no table remains.
        remaining = []
    current_bytes = sum(table_bytes(t) for t in new_tables)
    values = read_values(new_tables)
    if current_bytes != step['total_after']:
        raise SpacePlanError('切换后的占用与方案不一致，请重新生成方案')

    next_state = {
        **state,
        'step_index': state['step_index'] + 1,
        'groups': remaining,
        'dropped': dropped,
        'generation': state.get('generation', 0) + 1,
        'current_bytes': current_bytes,
        'peak_bytes': max(state['peak_bytes'], peak),
        'peak_history': state['peak_history'] + [
            {'label': f'第{state["step_index"] + 1}步', 'bytes': peak}
        ],
        'applied_steps': state['applied_steps'] + [step['index']],
        'read_values': values,
    }
    return next_state


def verify_reads(original_tables, state, entries):
    """Re-materialize current tables and verify the fixed read contract."""
    tables = materialize(original_tables, state['groups'], state.get('dropped', False))
    actual = read_values(tables)
    expected = oracle_values(entries)
    return {
        'ok': actual == expected,
        'actual': actual,
        'oracle': expected,
        'actual_checksum': checksum(actual),
        'oracle_checksum': checksum(expected),
        'current_bytes': sum(table_bytes(t) for t in tables),
        'table_count': len(tables),
    }
