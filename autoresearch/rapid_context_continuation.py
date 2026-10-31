"""Explicit operator continuation after an author lost retained patch operands."""
import copy

from .experiment_workflow import sha
from .storage import now


def _verify_base(engine, run_id, record):
    from .experiment_decisions import _binding, _safe_rows
    from .experiment_workflow import validation_records
    rows = _safe_rows([r for r in validation_records(engine.store, run_id)
        if (r.get('research_cycle') or 1) == engine.cycle(run_id) and not r.get('superseded')])
    if _binding(engine, run_id)[1] != record['binding'] or sha(rows) != record['validation_rows_sha256']:
        raise ValueError('Original code, plan, configuration or validation evidence changed')


def register(engine, run_id, revision_id, authorization):
    from .engine import Harness, digest
    from .rapid_experiment_revision import COLLECTION
    from .experiment_tools import project_receipts
    from .rapid_context import _valid
    run = engine.store.get('runs', run_id)
    record = engine.store.get(COLLECTION, revision_id)
    if run['status'] != 'paused' or not authorization:
        raise ValueError('Explicit authorization and a paused run are required')
    if (not record or record['run_id'] != run_id or record['status'] != 'implementing'
            or record.get('candidate') or record.get('source_context_recovery')):
        raise ValueError('Only one unsubmitted local patch may receive this continuation')
    _verify_base(engine, run_id, record)
    purpose = 'rapid_implement:' + record['decision_sha256']
    handoffs = [h for h in engine.store.list('handoffs', run_id)
        if h.get('purpose') == purpose and h.get('to_role') == 'engineer']
    if len(handoffs) != 1 or handoffs[0]['status'] != 'blocked':
        raise ValueError('Expected one blocked original author request')
    h = handoffs[0]; frozen = engine.store.get('handoff_inputs', h['id'])
    payload = {k: frozen[k] for k in ('instruction', 'context', 'schema')}
    if digest(payload) != h['input_hash'] or Harness.decision_key(run_id, 'engineer', purpose, **payload) != h['id']:
        raise ValueError('Original paid assignment identity changed')
    progress = engine.store.get('handoff_progress', h['id'])
    final = progress.get('finalization') or {}
    if (progress.get('mode') != 'final_only'
            or not final.get('reason', '').startswith(('Repeated tool call without progress:', 'Retained source reread'))
            or 'maxLength=12000' not in h.get('error', '')):
        raise ValueError('Not the bounded lost-context/oversized-patch failure')
    ctx = copy.deepcopy(frozen['context'])
    projected = project_receipts({**ctx, '_purpose': purpose, 'tool_results': progress.get('tool_results', [])})
    material = [r for r in projected['tool_results'] if _valid(ctx, r)]
    sent = final.get('prepared', {}).get('context', {})
    visible = {r.get('result', {}).get('receipt_sha256') for r in sent.get('tool_results', [])}
    missing = [r for r in material if r['result']['receipt_sha256'] not in visible]
    if not missing or not any(r['call']['name'] == 'experiments.compare' for r in missing):
        raise ValueError('No previously read patch operand was omitted')
    provenance = {'original_handoff_id': h['id'], 'original_progress_sha256': sha(progress),
        'original_error': h['error'], 'source_receipts_sha256': sha(material),
        'retained_material': material, 'new_reading_credit': False,
        'authorization': authorization, 'created_at': now()}
    ctx['rapid_revision']['source_context_recovery'] = provenance
    instruction = frozen['instruction'] + '\n本次是用户授权的同一修改单交付补修。原失败回复和原始回执保留。'
    instruction += ('已读目标源码、依赖与旧摘要补丁全文随请求保留，直接复用，不要继续重复status或重读相同材料。'
        '完成已批准的摘要接入及fit→persist真实CPU集成断言和三版本对照，不改变训练语义，不重复补日志。'
        '已有原版本可用精确逆向移除本次替换构造在隔离检查目录中，须核对原SHA，不能复制候选冒充原版。'
        '最多6处互不重叠替换；old、new和checks不设字符上限。'
        '每处替换使用精确原文锚点，全部对应本轮唯一批准项change_index=0。'
        '提交完整可核验候选；尚未执行，不声称CPU通过。科学范围、原种子、预算和审批均保持。')
    record['source_context_recovery'] = {'purpose': purpose + ':working-material-recovery-1',
        'context': ctx, 'instruction': instruction, 'source_handoff_id': h['id'],
        'source_receipts_sha256': sha(material), 'original_progress_sha256': sha(progress),
        'authorization': authorization, 'created_at': now()}
    engine.store.put(COLLECTION, record['id'], record)
    engine.store.event(run_id, 'rapid.working_material_restored',
        '已保留旧请求，为同一工程师创建一次原文保留的局部交付补修；未授权或重跑训练',
        {'revision_id': record['id'], 'original_handoff_id': h['id'], 'material_sha256': sha(material)})
    return record['source_context_recovery']
