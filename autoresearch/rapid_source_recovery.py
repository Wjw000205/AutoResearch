"""One same-author repair of edit operands lost by a legacy projection.

Never rewrites a dispatched request or retries a scientific/schema failure.
Original source receipts are references, not newly credited source reads.
"""
import copy

from .experiment_workflow import sha


class CandidateRejected(ValueError):
    """A completed patch failed local validation; never a model-call failure."""


async def _build(engine, run_id, record, patch):
    from .rapid_experiment_revision import build_candidate
    from .engine import ResearchBlocked
    try:
        return await engine._read_consensus_check(run_id, build_candidate, engine, run_id, record, patch)
    except (ValueError, SyntaxError, UnicodeError, ResearchBlocked) as exc:
        raise CandidateRejected(str(exc)) from exc


def prepare(engine, harness, run_id, record, context, instruction, patch, error):
    from .rapid_experiment_revision import PATCH
    from .experiment_tools import _implementation_source_windows
    if str(error) != '补丁原文、SHA 或唯一位置不匹配；未修改候选文件。':
        return None
    purpose = 'rapid_implement:' + record['decision_sha256']
    from .rapid_output_length import assignment_schema
    schema = assignment_schema(engine, run_id, purpose, instruction, context, PATCH)
    key = harness.decision_key(run_id, 'engineer', purpose, instruction, context, schema)
    progress = engine.store.get('handoff_progress', key) or {}
    rows = progress.get('tool_results', [])
    indexes = _implementation_source_windows({**context, '_purpose':purpose}, rows)
    selected = [rows[i] for i in sorted(indexes)]
    if not selected or any(not r['result'].get('receipt_sha256') for r in selected):
        return None
    # Only correct an actual legacy source omission, not arbitrary bad patches.
    sent = (progress.get('finalization') or progress.get('normal_request') or {}).get('prepared', {}).get('context', {})
    visible = {r.get('result', {}).get('receipt_sha256') for r in sent.get('tool_results', [])}
    if all(r['result']['receipt_sha256'] in visible for r in selected):
        return None
    payload = copy.deepcopy(context)
    payload['tool_results'] = copy.deepcopy(selected)
    payload['rapid_revision']['source_context_recovery'] = {
        'original_handoff_id': key, 'original_patch': copy.deepcopy(patch),
        'error': str(error), 'source_receipts_sha256': sha(selected), 'new_reading_credit': False}
    return {'purpose': purpose + ':source-context-recovery-1', 'context': payload,
        'instruction': instruction + '\n原请求的部分已读源码被旧投影规则移出上下文；完整原始回执仍保留。'
            '现在只按恢复的精确源码纠正 old/before_sha256 及必要的对应替换，保持原修改单和断言目标。'
            '不得重做方案讨论、扩大修改、重跑训练或编造通过。已有源码不必再读，缺少范围可定向读取。',
        'original_patch': copy.deepcopy(patch), 'source_handoff_id':key, 'source_receipts_sha256':sha(selected)}


async def candidate(engine, harness, run_id, record, context, instruction):
    from .rapid_experiment_revision import PATCH, COLLECTION, build_candidate, _base_unchanged
    from .engine import ResearchBlocked
    from .rapid_output_length import ask
    if record.get('native_codex_protocol') == 1:
        from .scoped_codex_candidate import build
        from .candidate_repair_source import DeliveryCollectionError
        try:
            return await build(engine, harness, run_id, record)
        except DeliveryCollectionError as exc:
            raise ResearchBlocked('Saved Codex delivery requires reconciliation: ' + str(exc)) from exc
        except (ValueError, SyntaxError, UnicodeError) as exc:
            raise CandidateRejected(str(exc)) from exc
    recovery = record.get('source_context_recovery')
    if not recovery:
        patch = await ask(engine, harness, run_id, 'rapid_implement:' + record['decision_sha256'], instruction, context, PATCH)
        _base_unchanged(engine, run_id, record)
        record['patch'] = patch
        try:
            return await _build(engine, run_id, record, patch)
        except CandidateRejected as exc:
            recovery = prepare(engine, harness, run_id, record, context, instruction, patch, exc)
            if recovery is None:
                raise
            record['source_context_recovery'] = recovery
            engine.store.put(COLLECTION, record['id'], record)
    patch = await ask(engine, harness, run_id, recovery['purpose'], recovery['instruction'], recovery['context'], PATCH)
    _base_unchanged(engine, run_id, record)
    record['patch'] = patch
    built = await _build(engine, run_id, record, patch)
    record['source_context_recovery']['completed_patch_sha256'] = sha(patch)
    return built
