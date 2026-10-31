"""Immutable, role-specific responses authored after a complete review round.

The host renders the author's actual submitted responses. It cannot close an
objection, invent a response, or turn a saved document into approval.
"""
from __future__ import annotations

import copy
import json

from .implementation_review_routing import digest

FIELD = 'indexed_review_response_documents'
KIND = 'implementation_review_response'
PROTOCOL = 'implementation_indexed_review_protocol'
ROLES = ('director', 'engineer', 'scout')
AUTHOR_GUIDANCE = '''
这是收齐同版本三方独立意见后的统一修订，不逐角色、逐章节提前修改。
逐条回应 implementation_issue_response_contract.actions 所列本轮三方全部问题，逐项使用其中准确的 (objection_id, origin_ref_sha256)；历史 obligations 仅作审计背景，不能替代或扩充本轮 actions。
同一 objection_id 延续身份不表示来源哈希不变，不得复制历史回复的旧 origin_ref_sha256；有原文证据可反驳，不为迎合意见删减科学要求。
document_revision 可修订实际字段或给出具体反驳；external_dependency 和 review_incomplete 只能解释当前真实状态与恢复/补读条件，不能声称靠改写已消除。
每项回复须有当前成功保存的 SPEC 原文锚点。作者答复不代表原评审接受或异议关闭。
宿主将真实 issue_responses 按各原评审角色保存为独立 Markdown 回复文档，并将其 SHA 与本版 SPEC 及前轮真实意见绑定。
下一轮三方只收到当前 SPEC、已批准来源与各自回复的索引，各自使用原文工具完整复核后独立表决；旧 SPEC 不进入新评审输入。
后续复审按各角色原异议及其原核验标准确认修订，不通过提高标准、追加可选优化或提出未修改内容的普通问题扩大本轮工作。
仅修复已明确提出的实质问题，保留其他正确合同；有充分原文证据时可具体反驳。修订不得新引入违反批准科学、数据防泄漏、公平性、可复现性、资源或验收要求的严重回归。
'''


def _fail(message):
    from .engine import ResearchBlocked
    raise ResearchBlocked('实现文档整轮回复：' + message)


def previous_round(state, revision):
    rows = [row for row in state.get('specifications', []) if row.get('index') == revision - 1]
    if len(rows) != 1:
        _fail('统一修订缺少唯一紧邻的前轮版本。')
    row = rows[0]
    votes = row.get('review_votes', [])
    if (row.get('approved') is not False or len(votes) != 3
            or (row.get('review_delivery') or {}).get('collection_complete') is not True
            or {vote.get('role') for vote in votes} != set(ROLES)
            or len({vote.get('handoff_id') for vote in votes}) != 3
            or any(vote.get('spec_hash') != row['spec_hash']
                or vote.get('document_sha256') != row['document']['sha256']
                or not vote.get('handoff_id') for vote in votes)):
        _fail('必须收齐同一版本三个角色的真实完整评审，才能统一修订。')
    return row


def validate_previous(engine, run_id, state, revision):
    """Authenticate every prior delivery before admitting a new author turn."""
    from . import implementation_indexed_review as indexed
    previous = previous_round(state, revision)
    for vote in previous['review_votes']:
        result = {key: value for key, value in vote.items()
                  if key not in {'role', 'handoff_id', 'reading_proof'}}
        context = engine.store.get('handoff_inputs', vote['handoff_id'], {}).get('context', {})
        proof = indexed.validate_vote(engine, run_id, state, previous, vote['role'],
                                      vote['handoff_id'], result, context)
        if vote.get('reading_proof') != proof:
            _fail('前轮完整意见缺少不变的实际全文阅读证明，不能启动统一修订。')


def author_context(state, revision, value):
    from . import implementation_spec_progress as progress, implementation_issue_responses as responses
    result = copy.deepcopy(value)
    for name in ('previous_specifications', 'implementation_history_snapshot', 'implementation_issue_index',
                 'implementation_incremental_target', 'implementation_author_scope'):
        result.pop(name, None)
    result[PROTOCOL] = 1
    if revision == 0:
        result['previous_specifications'] = []
        result.pop('previous_implementation_review', None)
        return result
    previous = previous_round(state, revision)
    result.update(progress.context(state, {'index': revision}))
    pending = result['previous_implementation_review']['objections']
    known = {identity: item for item in pending for identity in [item['id'], *item.get('aliases', [])]}
    actions = []
    for vote in previous['review_votes']:
        routes = {item['issue_index']: item for item in vote.get('review_routing', {}).get('issues', [])}
        if set(routes) != set(range(len(vote.get('issues', [])))):
            _fail('最近三方意见缺少逐项真实路由，不能遗漏回复。')
        for index, issue in enumerate(vote.get('issues', [])):
            ref = responses.origin_ref(previous, vote, index)
            linked = known.get(routes[index].get('objection_id'))
            matching = linked or next((item for item in pending if ref in item.get('origin_refs', [])), None)
            if not matching or matching['role'] != vote['role']:
                _fail('本轮回复问题缺少原角色的稳定异议身份。')
            actions.append({'id': matching['id'], 'role': vote['role'], 'issue': issue,
                'disposition': routes[index]['kind'], 'fixes': [copy.deepcopy(fix) for fix in vote['fixes']
                    if fix['issue_index'] == index], 'source': {key: value for key, value in ref.items() if key != 'role'},
                'required_reads': copy.deepcopy(routes[index]['required_reads'])})
    if not actions:
        _fail('未通过的前轮没有可逐条回应的实际问题。')
    # The immediately preceding SPEC is available only to its revising author.
    result['previous_specifications'] = [{key: copy.deepcopy(previous.get(key))
        for key in ('index', 'spec_hash', 'document', 'review_votes')}]
    result['indexed_response_actions'] = True
    result['implementation_revision_actions'] = {'version': 1, 'status': 'complete_review_delivery',
        'scientific_approval': False, 'source': {'revision': previous['index'], 'spec_hash': previous['spec_hash']}}
    result['document_revision_objections'] = actions
    from .implementation_author_revision import attach
    return attach(state, revision, responses.author_context(state, revision, result))


def _contents(state, row):
    from . import implementation_issue_responses as responses
    previous = previous_round(state, row['index'])
    receipt = responses._receipt(row)
    values = []
    for role in ROLES:
        vote = next(item for item in previous['review_votes'] if item['role'] == role)
        replies = [item for item in receipt['responses'] if item['role'] == role]
        by_index = {}
        for reply in replies:
            sources = [ref for ref in reply['latest_action_sources']
                if ref['revision'] == previous['index'] and ref['handoff_id'] == vote['handoff_id']]
            if len(sources) != 1 or sources[0]['issue_index'] in by_index:
                _fail('回复必须逐条绑定紧邻前轮本角色真实意见，不能重复或借用其他角色。')
            by_index[sources[0]['issue_index']] = reply
        if set(by_index) != set(range(len(vote['issues']))):
            _fail('每个角色的全部问题必须有实际工程师回复。')
        bound = {'response_role': role, 'previous_review_sha256': digest(vote),
            'previous_review_handoff_id': vote['handoff_id'], 'previous_spec_hash': previous['spec_hash'],
            'previous_document_sha256': previous['document']['sha256'], 'target_spec_hash': row['spec_hash'],
            'target_document_sha256': row['document']['sha256'], 'authoring_handoff_id': row['authoring_handoff_id'],
            'issue_response_receipt_sha256': receipt['sha256'], 'scientific_approval': False,
            'provenance': 'host_rendered_author_response'}
        lines = [f'# 对 {role} 评审意见的逐项回复', '',
            '本文由宿主按工程师真实提交的逐项答复生成；不代表评审接受、异议关闭或代码执行批准。', '',
            '## 版本与来源绑定', '', '```json', json.dumps(bound, ensure_ascii=False, sort_keys=True, indent=2), '```', '',
            '## 前轮独立评审', '', vote['rationale'], '']
        for index, issue in enumerate(vote['issues']):
            reply = by_index[index]
            original_fixes = [{**copy.deepcopy(fix), 'fix_index': number}
                for number, fix in enumerate(vote['fixes']) if fix['issue_index'] == index]
            numbers = {fix['fix_index'] for fix in original_fixes}
            original = {'fixes': original_fixes, 'fix_locations': [copy.deepcopy(location)
                for location in vote.get('fix_locations', []) if location['fix_index'] in numbers],
                'review_routing': [copy.deepcopy(route) for route in vote['review_routing']['issues']
                                  if route['issue_index'] == index]}
            lines.extend([f'## 问题 {index + 1}', '', issue, '', '### 原评审的修改与核验要求', '',
                '```json', json.dumps(original, ensure_ascii=False, sort_keys=True, indent=2), '```', '',
                '### 工程师回复', '', reply['rationale'], '',
                '```json', json.dumps(reply, ensure_ascii=False, sort_keys=True, indent=2), '```', ''])
        if not vote['issues']:
            lines.extend(['## 前轮没有提出修改问题', '',
                '本角色前轮没有逐项异议，因此没有作者问题答复可渲染；当前 SPEC 仍须由本角色全文独立复核。', ''])
        values.append((bound, '\n'.join(lines)))
    return values


def save_documents(engine, h, run_id, state, row):
    from . import implementation_documents as documents
    if row['index'] == 0:
        return []
    expected = _contents(state, row)
    if row.get(FIELD):
        validate_documents(engine, run_id, state, row)
        return copy.deepcopy(row[FIELD])
    result = []
    for bound, text in expected:
        engine.check_active(run_id)
        # Short path components keep Windows artifact paths below MAX_PATH.
        # Document and binding hashes remain full SHA256 in the immutable index.
        name = (f'cycle-{engine.cycle(run_id)}/implementation/replies/{digest(state["id"])[:12]}/'
                f'{row["index"]}-{bound["response_role"]}-{digest(text)[:24]}.md')
        directory = documents._save(engine, h, run_id, name, text, KIND)
        result.append({**directory, **bound})
    row[FIELD] = result
    return copy.deepcopy(result)


def validate_documents(engine, run_id, state, row):
    from . import document_index
    values = row.get(FIELD, [])
    if row['index'] == 0:
        if values:
            _fail('首轮不能借用其他版本的回复文档。')
        return
    expected = _contents(state, row)
    if len(values) != 3 or {value.get('response_role') for value in values} != set(ROLES):
        _fail('后续复审缺少三个角色各自的不可变回复文档。')
    for bound, text in expected:
        value = next(item for item in values if item.get('response_role') == bound['response_role'])
        manifest, actual = document_index.load(engine, run_id, value['document_id'], value['sha256'])
        if (value.get('kind') != KIND or manifest.get('document_kind') != KIND or actual != text
                or any(value.get(key) != item for key, item in bound.items())):
            _fail('回复文档正文、角色、前轮意见或当前 SPEC 的固定绑定已改变。')
