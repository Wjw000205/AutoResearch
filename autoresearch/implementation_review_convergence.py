"""Finite follow-up scope for newly frozen implementation review assignments.

The first review discovers blockers. Later reviews verify their own original
objections, with a narrow exception for an actual revision-induced regression.
Historical paid assignments without this contract keep their original rules.
"""
from __future__ import annotations

import copy
import re

from .implementation_review_routing import digest

VERSION = 1
FIELD = 'convergence'
CONTRACT = 'implementation_indexed_review_contract'
CATEGORIES = ('scientific_alignment', 'data_leakage', 'fairness',
              'reproducibility', 'resources', 'acceptance')


def _fail(message):
    from .engine import ResearchBlocked
    raise ResearchBlocked('实现文档收敛复核：' + message)


def _own(context, role):
    return [item for item in (context.get('previous_implementation_review') or {}).get('objections', [])
            if item.get('role') == role]


def freeze(state, row, role, context, response_documents):
    """Derive scope from the actual preceding vote, never model declarations."""
    value = {'version': VERSION, 'mode': 'initial' if row['index'] == 0 else 'followup',
             'role': role, 'allowed_objection_ids': [], 'changed_fields': [],
             'previous_review_sha256': None}
    if row['index'] == 0:
        return value
    previous = [item for item in state.get('specifications', []) if item.get('index') == row['index'] - 1]
    if len(previous) != 1:
        _fail('复核范围缺少唯一紧邻前轮规范。')
    old = previous[0]
    votes = [vote for vote in old.get('review_votes', []) if vote.get('role') == role]
    if len(votes) != 1:
        _fail('复核范围缺少本角色前轮真实意见。')
    vote = votes[0]
    value['previous_review_sha256'] = digest(vote)
    if not response_documents or any(doc.get('previous_review_sha256') != digest(vote)
                                     for doc in response_documents):
        _fail('复核范围与原角色回复来源不一致。')
    known = _own(context, role)
    routes = {item['issue_index']: item for item in vote.get('review_routing', {}).get('issues', [])}
    from .implementation_issue_responses import origin_ref
    for index in range(len(vote.get('issues', []))):
        identity = routes.get(index, {}).get('objection_id')
        ref = origin_ref(old, vote, index)
        candidates = [item for item in known if identity in [item['id'], *item.get('aliases', [])]
                      or ref in item.get('origin_refs', [])]
        if len(candidates) != 1:
            _fail('前轮问题缺少唯一的本角色原异议身份。')
        item = candidates[0]
        value['allowed_objection_ids'].extend([item['id'], *item.get('aliases', [])])
    value['allowed_objection_ids'] = sorted(set(value['allowed_objection_ids']))
    before, after = old['spec'], row['spec']
    value['changed_fields'] = sorted(key for key in set(before) | set(after)
        if (key in before) != (key in after) or digest(before.get(key)) != digest(after.get(key)))
    return value


def assert_context(state, row, context):
    policy = (context.get(CONTRACT) or {}).get(FIELD)
    if policy is None:
        return  # Never retrofit an immutable historical assignment.
    role = (context.get(CONTRACT) or {}).get('role')
    expected_keys = {'version', 'mode', 'role', 'allowed_objection_ids',
                     'changed_fields', 'previous_review_sha256'}
    if (not isinstance(policy, dict) or set(policy) != expected_keys
            or type(policy.get('version')) is not int or policy['version'] != VERSION
            or policy.get('role') != role
            or policy.get('mode') != ('initial' if row['index'] == 0 else 'followup')):
        _fail('冻结复核策略或角色身份不一致。')
    for name in ('allowed_objection_ids', 'changed_fields'):
        values = policy[name]
        if (not isinstance(values, list) or any(not isinstance(item, str) or not item for item in values)
                or values != sorted(set(values))):
            _fail('冻结复核清单必须为唯一的有序身份或字段。')
    own = {identity for item in _own(context, role) for identity in [item['id'], *item.get('aliases', [])]}
    if not set(policy['allowed_objection_ids']) <= own:
        _fail('复核不能借用其他角色或已关闭的原问题。')
    if row['index'] == 0:
        if policy['allowed_objection_ids'] or policy['changed_fields'] or policy['previous_review_sha256'] is not None:
            _fail('首轮不能继承复核问题或旧意见。')
    else:
        replies = context.get('implementation_review_response_documents', [])
        if (not isinstance(policy['previous_review_sha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', policy['previous_review_sha256'])
                or not replies or any(doc.get('previous_review_sha256') != policy['previous_review_sha256']
                                      for doc in replies)):
            _fail('复核必须绑定本角色前轮原意见与回复。')
    # Compiled-input checks carry only the current binding. Actual assignment
    # checks also have the immutable workflow and can recompute every field.
    if 'specifications' in state and 'spec' in row:
        if policy != freeze(state, row, role, context,
                            context.get('implementation_review_response_documents', [])):
            _fail('复核范围与真实前轮问题或实际变更不一致。')


def schema(base, context):
    policy = (context.get(CONTRACT) or {}).get(FIELD)
    if policy is None:
        return base
    result = copy.deepcopy(base)
    text = {'type': 'string', 'minLength': 1, 'pattern': r'\S'}
    fields = {'issue_index': {'type': 'integer', 'minimum': 0}, 'changed_field': text,
              'requirement_id': text, 'category': {'enum': list(CATEGORIES)},
              'rationale': {**text, 'maxLength': 2400}}
    result['properties']['new_regressions'] = {'type': 'array', 'items': {
        'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False}}
    if policy['mode'] == 'initial':
        result['properties']['new_regressions']['maxItems'] = 0
    else:
        if 'review_routing' in result['properties']:
            route = result['properties']['review_routing']['properties']['issues']['items']['properties']
            route['objection_id'] = {'enum': [None, *policy['allowed_objection_ids']]}
        if policy['changed_fields']:
            fields['changed_field'] = {'enum': policy['changed_fields']}
        else:
            result['properties']['new_regressions']['maxItems'] = 0
        fields['requirement_id'] = {'enum': context['implementation_alignment_acceptance_ids']}
    if 'new_regressions' not in result['required']:
        result['required'].append('new_regressions')
    result.setdefault('allOf', []).append({'if': {'properties': {'approve': {'const': True}},
        'required': ['approve']}, 'then': {'properties': {'new_regressions': {'maxItems': 0}}}})
    return result


def guidance(context):
    policy = (context.get(CONTRACT) or {}).get(FIELD)
    if policy is None:
        return ''
    common = '''
本冻结合同的 convergence 是本轮问题范围规则，以下规则限定前文的“完整审核”和“新问题”：
审核目标是足以正确实现已批准方案的文档，不追求无穷完善；没有问题时直接通过，不为了证明独立性强行找问题。
只阻断违反批准科学要求、数据泄漏、公平性、复现、资源或可执行验收的明确实质错误；
可选优化、文字风格、额外细节、等价工程选择、未经证实的假设风险不得成为否决理由。
本阶段审核的是实现计划，不要求尚未编写的代码、尚未执行的实验或科研成功证据。
完整真实阅读和全部当前验收项对齐仍须完成；有证据的作者反驳可以接受，不要求为回应而修改。
'''
    if policy['mode'] == 'initial':
        return common + '''
首轮一次完整列出全部已核实的必要问题及具体修改/验证标准；不要把问题拆到后续轮逐次追加。
首轮新问题使用 objection_id=null；new_regressions=[]。
'''
    return common + '''
本轮是原问题验收复核：逐项读取你自己的工程师回复，按你原来提出的 fixes/verification 核对当前修订。
只能延续 convergence.allowed_objection_ids 中的本角色紧邻上一轮问题；不接管其他角色问题、不重开已关闭问题。
原验证标准已满足或原条款已被证据证明正确，就确认 resolved；全部满足后 approve=true。
原问题未解决时仅描述原标准尚缺的部分；不能借原 objection_id 提出新要求、提高标准或换角度重复追问。
上轮没提出问题的角色仍阅读当前版本并核对实际修改的影响，无严重回归就通过。
全文阅读用于确认原问题及修改影响，不启动新一轮自由找错；首轮漏提且本次没有新引入的问题不再追加。
新增问题的唯一例外是本次实际修改新引入的严重回归。必须逐项填 new_regressions：
issue_index 对应 objection_id=null 的问题，changed_field 必须在 convergence.changed_fields 中，
requirement_id 必须是当前验收ID，对应 alignment_findings.status=mismatch，category 使用指定实质类型；
rationale 必须说明哪个实际修改新引入了什么错误、违反哪条批准要求、为何并非原有问题或可选优化。
同一问题的 fixes 和 fix_locations 必须定位该真实修改字段的当前原文。已有原问题不得伪装成新增回归。
没有这样的严重回归时 new_regressions=[]，不得通过 null 身份绕开原问题范围。
'''


def _field_root(value):
    if not isinstance(value, str):
        return None
    if value.startswith('/'):
        return value[1:].split('/', 1)[0].replace('~1', '/').replace('~0', '~')
    return value


def diagnostic(result, context, error, attempt, limit):
    """Ask the original reviewer to correct an unsealed, out-of-scope answer."""
    return {'host_diagnostic': 'implementation_review_scope_required', 'error': error,
        'alignment_read_correction': {'attempt': attempt, 'max_attempts': limit,
            'candidate_sha256': digest(result),
            'convergence': copy.deepcopy(context[CONTRACT][FIELD]),
            'instruction': '原候选和付费回复已保留，尚未封存为审核票。请在同一冻结交接修正上述越界意见，'
                '返回原 Schema 的完整独立评议。按你原来提出的问题和原验证标准核对工程师回复与当前文档，'
                '继续保留未解决的原问题；已被证据满足或反驳的原问题逐项确认解决。'
                '普通漏提、可选优化、无关新要求不能升级为阻塞或冒充修改回归。'
                '如果存在本次修改新引入的严重回归，须用真实改变字段、当前原文位置和批准验收冲突说明。'
                '不得为通过范围校验虚构证据或修改实际科研要求，宿主不会代替你修改意见或登记批准。'}}


def validate(result, context, role):
    policy = (context.get(CONTRACT) or {}).get(FIELD)
    if policy is None:
        return
    def reject(message):
        from .engine import ResearchBlocked
        error = ResearchBlocked('实现文档收敛复核：' + message)
        error.implementation_convergence_violation = True
        raise error
    if policy.get('role') != role:
        _fail('当前复核角色与冻结策略不一致。')
    regressions = result.get('new_regressions')
    if not isinstance(regressions, list):
        reject('新冻结意见必须明确返回 new_regressions。')
    if policy['mode'] == 'initial':
        if regressions:
            reject('首轮问题应一次提出，不属于修改回归。')
        return
    routes = result.get('review_routing', {}).get('issues', [])
    allowed = set(policy['allowed_objection_ids'])
    for route in routes:
        if route.get('objection_id') is not None and route['objection_id'] not in allowed:
            reject('后续轮次只能验收本角色上一轮原问题，不能新增或重开历史问题。')
    new = {route['issue_index'] for route in routes if route.get('objection_id') is None}
    if (any(not isinstance(item, dict) or type(item.get('issue_index')) is not int for item in regressions)
            or len({item['issue_index'] for item in regressions}) != len(regressions)
            or {item['issue_index'] for item in regressions} != new):
        reject('新增问题只允许有逐项实证的本次修改严重回归。')
    findings = {item['requirement_id']: item for item in result.get('alignment_findings', [])}
    acceptance = set(context.get('implementation_alignment_acceptance_ids', []))
    for item in regressions:
        field = item.get('changed_field')
        finding = findings.get(item.get('requirement_id'), {})
        fixes = {index for index, fix in enumerate(result.get('fixes', []))
                 if fix.get('issue_index') == item['issue_index']}
        locations = [entry for entry in result.get('fix_locations', []) if entry.get('fix_index') in fixes]
        if (field not in policy['changed_fields'] or item.get('requirement_id') not in acceptance
                or finding.get('status') != 'mismatch' or not finding.get('source_refs')
                or item.get('category') not in CATEGORIES
                or not isinstance(item.get('rationale'), str) or not item['rationale'].strip()
                or not locations or any(_field_root(entry.get('field')) != field for entry in locations)):
            reject('新增回归必须绑定实际修改字段、批准验收冲突及当前原文修改位置。')
    # Confirmations already authenticate role, origin, reply SHA and read
    # evidence. Prevent inventing a new issue after resolving the old one, or
    # silently dropping an unresolved original issue from the repair queue.
    routed = {route.get('objection_id') for route in routes}
    canonical = {identity: item['id'] for item in _own(context, role)
                 for identity in [item['id'], *item.get('aliases', [])]}
    routed = {canonical.get(identity, identity) for identity in routed if identity is not None}
    confirmations = result.get('issue_confirmations', [])
    unresolved = {canonical.get(item['objection_id'], item['objection_id'])
                  for item in confirmations if item.get('outcome') == 'unresolved'}
    resolved = {canonical.get(item['objection_id'], item['objection_id'])
                for item in confirmations if item.get('outcome') == 'resolved'} - unresolved
    if confirmations and (not unresolved <= routed or resolved & routed):
        reject('未解决的原问题必须延续路由，已解决问题不能再换名要求修改。')
