"""Program-owned recovery of completed, unsaved author continuations.

The rejected reply remains history. A new same-role assignment must save and
read its actual draft before the ordinary author and independent review gates.
"""
import copy

from . import implementation_acceptance_author as author_flow
from . import implementation_authoring as author
from .engine import ResearchBlocked
from .implementation_review_routing import digest


def validate(engine, run_id, state, payload):
    """Authenticate both the failed child and its exact inherited draft edge."""
    recovery = payload['submission_recovery']
    if (recovery.get('version') != 1
            or payload.get('workflow') != author_flow._workflow_scope(state)
            or recovery.get('diagnostic', {}).get('code') != 'completed_continuation_without_own_save'):
        raise ResearchBlocked('不支持的作者提交修复合同或工作流范围。')
    hid = recovery['handoff_id']
    if author_flow._source_records(engine.store, hid) != recovery['records']:
        raise ResearchBlocked('作者提交修复的原请求、回复或工具回执已改变。')
    audit = recovery['base_audit']
    if engine.store.get('stage_contexts', audit['id']) != audit:
        raise ResearchBlocked('作者提交修复的原继承审计已改变。')
    context = recovery['records']['handoff_inputs']['context']
    ref = context.get(author_flow.CONTINUATION, {})
    if (ref.get('source_handoff_id') != recovery['base_handoff_id']
            or ref.get('audit_id') != audit['id']
            or audit.get('successor_id') != hid
            or audit.get('successor_context_sha256') != digest(context)
            or audit.get('sha256') != digest(audit.get('payload'))
            or ref.get('sha256') != audit.get('sha256')
            or not author_flow._matches_source(engine, run_id, state,
                recovery['base_handoff_id'], payload.get('source', {}))):
        raise ResearchBlocked('作者提交修复与控制器冻结的失败交接、父稿不一致。')


def prepare(engine, run_id, state, hid, result, context):
    """Diagnose an authenticated terminal reply and construct fresh author work."""
    from .harness_delivery import authenticate
    from .prose_schema import validate as validate_schema
    records = author_flow._source_records(engine.store, hid)
    handoff, progress = records['handoffs'], records['handoff_progress'] or {}
    if (not handoff or handoff.get('status') != 'completed'
            or records[author.COLLECTION] is not None
            or progress.get('turn') != 0 or progress.get('tool_results')
            or progress.get('pending_tool_calls') or progress.get('seen')
            or progress.get('mode') or progress.get('finalization')
            or len(handoff.get('model_calls', [])) != 1
            or engine.store.record_keys('tool_invocations', run_id,
                predicate="json_extract(data,'$.handoff_id')=?", parameters=[hid])):
        raise ResearchBlocked('作者续接尚有未确认工作或非直接交付，未派发提交修复。')
    frozen = authenticate(engine, run_id, 'engineer', hid, result)
    if frozen != context:
        raise ResearchBlocked('作者提交修复与原冻结上下文不一致。')
    validate_schema(result, records['handoff_inputs']['schema'])
    author_flow._report_commit_records(engine, run_id, hid, progress, result)
    draft = author_flow.continuation_base(engine, run_id, state, context)
    if {key: result.get(key) for key in author.SUBMISSION['properties']} != author.pointer({'row': draft}):
        raise ResearchBlocked('作者提交修复回复未绑定原继承稿，不能猜测替代版本。')
    ref = context[author_flow.CONTINUATION]
    audit = engine.store.get('stage_contexts', ref['audit_id'])
    source = author_flow._source_records(engine.store, ref['source_handoff_id'])
    recovery = {'version': 1, 'handoff_id': hid,
        'base_handoff_id': ref['source_handoff_id'], 'base_audit': copy.deepcopy(audit),
        'records': records, 'diagnostic': {
            'code': 'completed_continuation_without_own_save',
            'boundary': 'author_final_to_saved_submission',
            'message': '旧续接返回了继承稿指针和报告，但本交接没有保存回执；不能转成父交接批准。',
            'source_input_hash': handoff['input_hash'], 'source_output_hash': handoff['output_hash'],
            'scientific_approval': False, 'reading_credit': False}}
    pending = [{'issue_id': issue['issue_id'], 'condition_index': index,
        'requirement': condition, 'status': 'incomplete', 'report_repair_only': True,
        'reason': '先在本新交接真实保存当前继承稿，再回读报告引用范围并核对完整累计修改。'
            '正文无需修改时用 revisions=[] 和真实 revision_plan 登记同版保存。'
            '旧报告是待核对的作者陈述，不是保存、阅读或独立审查证据。'}
        for issue in context['acceptance_issues'] for index, condition in enumerate(issue['close_when'])]
    return author_flow.continuation_plan(engine, run_id, state, ref['source_handoff_id'],
        context, source, pending, recovery=recovery)
