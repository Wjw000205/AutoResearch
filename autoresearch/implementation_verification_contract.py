"""Compile finite verification obligations for prompt, schema and host checks.

No task-specific interpretation: canonical units, current citation intersections
and condition indices come exclusively from the frozen document/issue inventory.
Legacy v1/v2 packets retain their original contracts and original evidence.
"""
import copy
import json

from . import implementation_acceptance as policy
from .implementation_review_routing import digest

VERSION = 1
FIELD = 'verification_constraints'
SCOPE = 'implementation_document_review_scope'

TASK = (
    '按每项原问题和全部 close_when 定位当前 canonical unit_ids。'
    'verification_constraints 是与本请求 schema、宿主校验共用的冻结范围合同。'
    '每个 issue_id 和每个 condition index 必须各出现一次；所有单元均须来自该问题 eligible_unit_ids。'
    'conditions 精确定位各关闭条件。将全部 required_patch_unit_ids 放入 dependency_unit_ids，'
    '由该数组统一登记完整引用覆盖；与 conditions.unit_ids 的并集至少包含一个 target_unit_ids。'
    '补丁引用可能横跨多个规范单元；引用范围内的单元都是既有证据覆盖义务，不能只挑相关段落后遗漏其余引用。'
    '与关闭条件不直接对应的已引用单元放入 dependency_unit_ids，说明它们是引用覆盖或必要共享依赖；'
    '这不增加其他验收项的审核、科学批准或改码范围，也不要求整项全文复审。'
    '同一单元可服务多个条件；各数组内不得重复。理由简洁，不复制正文。'
    '根据目录实际读取需要核对的原文，不猜测单元或版本。缺项诊断指出准确 issue、引用和单元。'
    '这是作者定位澄清，不是核验或批准；后续仍由原独立职责核对定位及全部关闭条件。')


def candidates(saved, issue, *, dependencies=False):
    targets = {a['id'] for a in saved['units'] if issue['criterion_id'] in a['criteria']}
    if not dependencies:
        return targets
    document = saved.get('verification_document', {})
    sections = {s['id']: s for s in document.get('sections', [])}
    for ref in [*issue.get('evidence', []), *issue.get('patch', {}).get('evidence', [])]:
        if (ref.get('document_id'), ref.get('document_sha256')) != (
                document.get('document_id'), document.get('sha256')):
            continue
        section = sections.get(ref.get('section_id'))
        if section is None:
            policy.delivery_error('Verification evidence has no canonical section')
        start, end = section['start'] + ref['offset'], section['start'] + ref['offset'] + ref['limit']
        if ref['offset'] < 0 or ref['limit'] <= 0 or ('end' in section and end > section['end']):
            policy.delivery_error('Verification evidence exceeds canonical section')
        targets.update(a['id'] for a in saved['units'] if a['range']['id'] == section['id']
                       and a['range']['start'] < end and start < a['range']['end'])
    return targets


def patch_anchors(saved, issue, eligible):
    document = saved.get('verification_document', {})
    sections = {s['id']: s for s in document.get('sections', [])}
    anchors = []
    for ref in issue.get('patch', {}).get('evidence', []):
        if (ref['document_id'], ref['document_sha256']) != (document.get('document_id'), document.get('sha256')):
            continue  # Historical citations never manufacture current-version evidence.
        section = sections.get(ref['section_id'])
        if section is None:
            policy.delivery_error('Verification evidence has no canonical section')
        start = section['start'] + ref['offset']
        units = sorted(a['id'] for a in saved['units'] if a['id'] in eligible
            and a['range']['id'] == ref['section_id']
            and a['range']['start'] < start + ref['limit'] and start < a['range']['end'])
        anchors.append({'reference': copy.deepcopy(ref), 'unit_ids': units})
    return anchors


def compile_contract(saved):
    units = saved['units']
    if len({a['id'] for a in units}) != len(units):
        policy.fail('Verification unit inventory contains duplicate identities')
    obligations = []
    for issue in saved['verification_issues']:
        eligible = candidates(saved, issue, dependencies=True)
        anchors = patch_anchors(saved, issue, eligible)
        if (not eligible or not candidates(saved, issue) or not issue['close_when']
                or any(not a['unit_ids'] for a in anchors)):
            policy.delivery_error('Verification scope has no representable canonical evidence',
                code='unrepresentable_verification_scope', issue_id=issue['issue_id'])
        obligations.append({'issue_id': issue['issue_id'],
            'condition_indices': list(range(len(issue['close_when']))),
            'target_unit_ids': sorted(candidates(saved, issue)),
            'eligible_unit_ids': sorted(eligible),
            'required_patch_unit_ids': sorted({uid for a in anchors for uid in a['unit_ids']}),
            'patch_anchors': anchors})
    if len({o['issue_id'] for o in obligations}) != len(obligations):
        policy.fail('Verification issue inventory contains duplicate identities')
    document = saved.get('verification_document', {})
    body = {'version': VERSION, 'coverage_representation':'citation_units_in_dependencies',
        'identity': saved.get('identity'),
        'document': {k: document.get(k) for k in ('document_id','sha256')},
        'units_sha256': digest(units),
        'issues_sha256': digest(saved['verification_issues']), 'obligations': obligations}
    return {**body, 'sha256': digest(body)}


def assert_contract(contract):
    if (not isinstance(contract, dict) or type(contract.get('version')) is not int
            or contract['version'] != VERSION
            or contract.get('coverage_representation') != 'citation_units_in_dependencies'
            or contract.get('sha256') != digest({k:v for k,v in contract.items() if k != 'sha256'})):
        policy.fail('Unsupported or altered verification constraint contract')


def _contains_id(field, value):
    return {'contains': {'type': 'object', 'required': [field], 'properties': {field: {'const': value}}},
            'minContains': 1, 'maxContains': 1}


def _selected_contains(item):
    return {'anyOf': [
        {'properties': {'dependency_unit_ids': {'contains': item}}},
        {'properties': {'conditions': {'contains': {'type': 'object', 'required': ['unit_ids'],
            'properties': {'unit_ids': {'contains': item}}}}}}]}


def schema(contract):
    """The complete finite inventory, including more than 64 cited units."""
    assert_contract(contract)
    contract = copy.deepcopy(contract)
    obligations = contract['obligations']
    if not obligations:
        return policy.obj({'bindings': {'type':'array','minItems':0,'maxItems':0,'items':False}})
    eligible = sorted({uid for o in obligations for uid in o['eligible_unit_ids']})
    indices = sorted({i for o in obligations for i in o['condition_indices']})
    unit_array = {'type':'array','items':{'enum':eligible},'uniqueItems':True,'maxItems':len(eligible)}
    entry = policy.obj({'issue_id': {'enum':[o['issue_id'] for o in obligations]},
        'conditions': {'type':'array','minItems':1,'maxItems':len(indices),'items':policy.obj({
            'index': {'enum':indices}, 'unit_ids': {**copy.deepcopy(unit_array),'minItems':1},
            'reason': policy.TEXT})},
        'dependency_unit_ids': unit_array, 'dependency_reason': policy.TEXT})
    entry['allOf'] = []
    for obligation in obligations:
        allowed = obligation['eligible_unit_ids']
        per_issue = {'items':{'enum':allowed},'maxItems':len(allowed)}
        constraints = [_selected_contains({'enum':obligation['target_unit_ids']})]
        dependencies = copy.deepcopy(per_issue)
        required = obligation['required_patch_unit_ids']
        if required:
            # Unique array values plus N distinct enum matches require every
            # member, without repeating a whole union clause for each ID.
            dependencies.update(contains={'enum':required},minContains=len(required),maxContains=len(required))
        entry['allOf'].append({'if':{'properties':{'issue_id':{'const':obligation['issue_id']}},
            'required':['issue_id']}, 'then': {'properties': {
                'dependency_unit_ids':dependencies,
                'conditions': {'minItems':len(obligation['condition_indices']),
                    'maxItems':len(obligation['condition_indices']),
                    'items': {'properties': {'index':{'enum':obligation['condition_indices']},
                        'unit_ids':copy.deepcopy(per_issue)}},
                    'allOf':[_contains_id('index',i) for i in obligation['condition_indices']]}},
                'allOf':constraints}})
    bindings = {'type':'array','minItems':len(obligations),'maxItems':len(obligations),'items':entry,
        'allOf':[_contains_id('issue_id',o['issue_id']) for o in obligations]}
    return policy.obj({'bindings':bindings})


def validate(saved, packet):
    """Legacy semantics plus exact new-schema alignment, never altered replies."""
    scope = packet.get('context', {}).get(SCOPE, {})
    version = scope.get('version',1)
    if type(version) is not int or version not in (1,2):
        policy.delivery_error('Unsupported verification scope version')
    contract = compile_contract(saved) if FIELD in scope else None
    if contract is not None and (version != 2 or scope[FIELD] != contract):
        policy.fail('Verification constraint contract differs from frozen evidence')
    entries = packet['result']['bindings']
    issues = {i['issue_id']:i for i in saved['verification_issues']}
    if len(entries) != len(issues) or {e['issue_id'] for e in entries} != set(issues):
        policy.delivery_error('Verification scope must bind every patched issue once')
    for entry in entries:
        issue = issues[entry['issue_id']]
        eligible = candidates(saved,issue,dependencies=version == 2)
        conditions = entry['conditions']
        if (len(conditions) != len(issue['close_when']) or
                {c['index'] for c in conditions} != set(range(len(issue['close_when'])))):
            policy.delivery_error('Verification scope omitted a close_when condition',
                code='missing_verification_condition',issue_id=issue['issue_id'])
        if (not set(entry['dependency_unit_ids']) <= eligible or not entry['dependency_reason'].strip()
                or (version == 2 and len(set(entry['dependency_unit_ids'])) != len(entry['dependency_unit_ids']))):
            policy.delivery_error('Verification dependency is outside canonical scope')
        for condition in conditions:
            if (not condition['unit_ids'] or not set(condition['unit_ids']) <= eligible
                    or len(set(condition['unit_ids'])) != len(condition['unit_ids']) or not condition['reason'].strip()):
                policy.delivery_error('Verification condition lacks exact canonical units')
        selected = set(entry['dependency_unit_ids']) | {uid for co in conditions for uid in co['unit_ids']}
        if version == 2 and not selected & candidates(saved,issue):
            policy.delivery_error('Verification scope omitted the original issue target')
        anchors = patch_anchors(saved,issue,eligible)
        missing = sorted({uid for a in anchors for uid in a['unit_ids']} - selected)
        if missing or any(not a['unit_ids'] for a in anchors):
            policy.delivery_error('Verification scope omitted canonical patch evidence',
                code='missing_canonical_patch_units',issue_id=issue['issue_id'],
                document={k:saved.get('verification_document',{}).get(k) for k in ('document_id','sha256')},
                missing_unit_ids=missing, patch_anchors=[a for a in anchors
                    if not a['unit_ids'] or set(a['unit_ids']) & set(missing)])
    if contract is not None:
        from .prose_schema import validate as validate_schema
        validate_schema(packet['result'],schema(contract))


def output_limit(context, base):
    contract = context.get(SCOPE,{}).get(FIELD)
    if contract is None:
        return base
    assert_contract(contract)
    # Reserve room for every distinct identifier plus concise condition reasons.
    # Provider/budget admission still decides whether this complete request fits.
    from .intelligence import token_estimate
    required = 4000 + sum(
        (5*token_estimate(json.dumps(o['eligible_unit_ids'],ensure_ascii=False))+3)//4
        + 400*len(o['condition_indices']) for o in contract['obligations'])
    return max(base,required)
