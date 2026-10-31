"""Version-bound review dispositions; no text heuristics or approval inference."""
from __future__ import annotations

import copy
import hashlib
import json

VERSION = 1
KINDS = ('document_revision', 'external_dependency', 'review_incomplete')
GUIDANCE = '''
逐项填写 review_routing.issues，与 issues 的 issue_index 一一对应：
document_revision 仅用于已核实的文档实质错误，给出实际修改及验证；
external_dependency 用于文档改写不能解决的宿主能力、资源或来源接入缺口，明确恢复条件；
自动恢复仅观察宿主能力指纹、已入索引来源及持久资源配置；瞬时资源/访问状态需先保存新的可核验来源证据。
review_incomplete 用于尚需读取/复核的范围，不因此要求重写正确条款，required_reads 列精确原文范围。
required_reads.start/end 是固定文档全文的绝对字符坐标，取 indexed_documents.sections 的 start/end；
不是 documents.read 的章节相对 offset。相对 offset 加该章节 start 才是绝对 start，end=start+实际长度。
已有异议使用输入中同角色的原 objection_id 延续身份；新问题用 null，不按改写文字另建旧问题。
整篇核查架构与实现的一致性，并覆盖受当前修改影响的关联章节；原问题身份与未解决意见继续保留。
review_routing 只决定后继工作，不是批准；批准仍须 issues/fixes 为空且完成真实独立阅读。
progress.milestones 仅引用输入 objections.milestone_checks 中本角色已实际满足的检查项 ID；
局部改善但未完成任何原检查项时该数组为空，仍可如实描述 made_progress，不重复认领旧检查项。
进展以本版相对上一文档版本的累计修改为基准；未编辑的部分保留已核实的改善。
尚未完成包含未来交付或科研执行的整条检查项，不妨碍确认本阶段已核实的局部实质修复；
该确认必须实际读取对应 changed_fields，并在 review_routing 中延续本角色受影响的原 objection_id，说明已修复部分和仍未完成部分。
未核实的变化、仅改措辞或 limitations 说明不能算推进；局部推进不关闭未解决异议，也不代替完整独立审核和执行批准。
'''
AUTHOR_GUIDANCE = '''
本轮文本修订仅落实当前审查明确标为 document_revision 的实质文档错误。
external_dependency 是宿主能力/资源/来源依赖，review_incomplete 是尚需复核范围；保留其原意见和恢复条件，
不得用重写文档、删减验收项或宣称能力已实现来消除这两类阻塞。
历史未分类意见仍需独立复核，不因保留记录而要求从头改写；未受实质错误影响的正确合同保持原样。
'''


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def schema(base):
    result = copy.deepcopy(base)
    text = {'type': 'string', 'minLength': 1}
    def obj(fields):
        return {'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False}
    anchor = obj({'document_id': text, 'sha256': text, 'section_id': text,
                  'start': {'type': 'integer', 'minimum': 0, 'description': '全文绝对字符坐标，必须在该section的绝对start/end之间；不是章节相对offset。'},
                  'end': {'type': 'integer', 'minimum': 1, 'description': '全文绝对字符结束坐标，不含end位置；必须在同一固定section内。'}})
    issue = obj({'issue_index': {'type': 'integer', 'minimum': 0},
        'objection_id': {'type': ['string', 'null'], 'minLength': 1},
        'kind': {'enum': list(KINDS)}, 'recovery_condition': text,
        'required_reads': {'type': 'array', 'items': anchor}})
    result['properties']['review_routing'] = obj({'version': {'const': VERSION},
        'issues': {'type': 'array', 'items': issue}})
    result['required'].append('review_routing')
    progress = result['properties'].get('progress')
    if progress:
        progress['properties']['milestones'] = {'type': 'array', 'uniqueItems': True, 'items': text}
        progress['required'].append('milestones')
    return result


def milestones(issue):
    """Finite, original acceptance checks, not model-invented achievement names."""
    if 'milestone_checks' in issue:
        return copy.deepcopy(issue['milestone_checks'])
    return [{'id': digest([issue['id'], fix]), 'verification': fix.get('verification', ''),
             'change': fix.get('change', ''), 'objection_id': issue['id']}
            for fix in issue.get('fixes', []) if fix.get('verification')]


def validate(result, context, role):
    value = result.get('review_routing')
    if value is None:
        return  # A frozen legacy schema cannot be retroactively extended.
    entries = value.get('issues', [])
    if value.get('version') != VERSION or sorted(item['issue_index'] for item in entries) != list(range(len(result['issues']))):
        raise ValueError('实现评审路由必须逐项覆盖原问题，不能遗漏或重复')
    old = context.get('previous_implementation_review', {}).get('objections', [])
    known = {identity: item for item in old for identity in [item['id'], *item.get('aliases', [])]}
    docs = {doc['document_id']: doc for doc in context.get('indexed_documents', [])}
    for item in entries:
        identity = item['objection_id']
        if identity is not None and (identity not in known or known[identity]['role'] != role):
            raise ValueError('实现评审只能延续本角色已有的原问题身份')
        if item['kind'] not in KINDS or not item['recovery_condition'].strip():
            raise ValueError('实现评审缺少明确路由或恢复条件')
        if item['kind'] == 'review_incomplete' and not item['required_reads']:
            raise ValueError('未完成复核必须指出可读取的具体范围')
        for target in item['required_reads']:
            doc = docs.get(target['document_id'])
            section = next((s for s in (doc or {}).get('sections', []) if s['id'] == target['section_id']), None)
            if not doc or target['sha256'] != doc['sha256'] or not section or not section['start'] <= target['start'] < target['end'] <= section['end']:
                raise ValueError('复核后继引用了固定索引外的文档或范围')
    allowed = {m['id'] for item in old if item['role'] == role for m in milestones(item)}
    if not set(result.get('progress', {}).get('milestones', [])) <= allowed:
        raise ValueError('实现进展只能确认原异议已声明的验证检查项')


def disposition(vote):
    if vote.get('approve') is True:
        return None
    value = vote.get('review_routing')
    if not value:
        return 'legacy_review'  # Requires a new, explicit independent judgment.
    kinds = {item['kind'] for item in value['issues']}
    return next(kind for kind in KINDS if kind in kinds)


def evidence_key(context, *, external=False):
    capability = (context.get('implementation_capability_evidence') or {}).get('capability_fingerprint')
    if external:
        # A new spec/binding is not a repaired host or an acquired source.
        # Index entries are immutable evidence identities, not read/approval claims.
        limits = context.get('execution_constraints') or {}
        current = context.get('implementation_document') or {}
        return digest({'capability': capability,
            'sources': sorted((d['document_id'], d['sha256']) for d in context.get('indexed_documents', [])
                if d.get('kind') != 'implementation_specification' and d['document_id'] != current.get('document_id')),
            'resource_limits': {name: limits.get(name) for name in
                ('max_trials_per_cycle', 'max_research_cycles', 'per_trial_seconds')}})
    return digest({'capability': capability,
        'documents': sorted((d['document_id'], d['sha256']) for d in context.get('indexed_documents', []))})


def read_spans(store, handoff_id, targets=None):
    """Canonical read coverage; re-pagination cannot manufacture new progress."""
    grouped = {}
    for receipt in store.get('handoff_progress', handoff_id, {}).get('tool_results', []):
        page = receipt.get('result', {})
        if (receipt.get('call', {}).get('name') != 'documents.read' or not isinstance(page, dict)
                or page.get('error') or not isinstance(page.get('text'), str)):
            continue
        start, end = page.get('char_start'), page.get('char_end')
        if type(start) is int and type(end) is int and 0 <= start < end and end - start == len(page['text']):
            key = json.dumps([page.get('document_id'), page.get('sha256'), page.get('section_id')])
            if targets is None:
                grouped.setdefault(key, []).append([start, end])
            else:
                for target in targets:
                    if (target['document_id'], target['sha256'], target['section_id']) == (
                            page.get('document_id'), page.get('sha256'), page.get('section_id')):
                        left, right = max(start, target['start']), min(end, target['end'])
                        if left < right:
                            grouped.setdefault(key, []).append([left, right])
    return merge_spans(grouped)


def merge_spans(*values):
    merged = {}
    for value in values:
        for key, ranges in value.items():
            merged.setdefault(key, []).extend(copy.deepcopy(ranges))
    for key, ranges in merged.items():
        result = []
        for start, end in sorted(ranges):
            if result and start <= result[-1][1]:
                result[-1][1] = max(result[-1][1], end)
            else:
                result.append([start, end])
        merged[key] = result
    return merged


