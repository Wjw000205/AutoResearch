"""Versioned full-material review policy and host-owned receipt adapters.

New reviews have two independent reviewers.  Absence of this explicit policy
means the historical three-role protocol; a vote count never selects a version.
The model's answer is retained byte-for-byte as its original decision result.
"""
from __future__ import annotations

import copy
import functools
import hashlib
import json
from .architecture_validation import operation, once

KEY = 'independent_full_review'
REVIEWERS = ('director', 'scout')
LEGACY_REVIEWERS = ('director', 'engineer', 'scout')
READ_LIMIT = 50000
PAGE_LIMIT = 6000
TOOL = 'reviews.read_material'
READING = 'independent_full_review_reading'
MATERIALS = 'independent_full_review_materials'


class ResponseCorrectionRequired(ValueError):
    """A source-authenticated answer needs revision, rather than more pages."""

    def __init__(self, message, problems):
        super().__init__(message)
        self.problems = copy.deepcopy(problems)


def purpose_matches(actual, original):
    if actual == original:
        return True
    for suffix in (':full-reading:', ':full-response:'):
        prefix = original + suffix
        if isinstance(actual, str) and actual.startswith(prefix):
            count = actual[len(prefix):]
            return count.isdecimal() and int(count) > 0
    return False


def validate_recovery(context):
    value = context.get(READING, {})
    if 'recovery_kind' not in value and 'version' not in value:
        return  # Frozen legacy missing-page continuation.
    if (value.get('version') != 1 or value.get('recovery_kind') != 'response_correction'
            or not isinstance(value.get('problems'), list) or not value['problems']
            or not value.get('parent_handoff_id')
            or sha(value.get('previous_own_result')) != value.get('parent_result_sha256')):
        raise ValueError('Unknown or inconsistent full-review response recovery contract')

GUIDANCE = ('两位审核者 director 和 scout 必须各自独立审核同一版本的完整材料。'
    '原文按索引通过 reviews.read_material 自行读完，每次最多50000字符；必要时继续使用原有证据工具。'
    '不得按角色、模块或专业分拆审核范围；工程师负责统一创作和修订，不充当独立审核者。'
    '本协议取代本请求中旧的审核职责分工说明。给出具体问题、位置和修改要求；'
    '缺少证据应自行补读，UNKNOWN不等于通过，不能继承另一审核者的读取或批准。'
    '完整材料读取不代替原有源码、实验、数据、预算及科学验收门禁。')


def code_delivery_scope(context):
    policy = context.get(KEY, {})
    return (policy.get('domain') == 'complete_code_delivery'
        and policy.get('source_identity', {}).get('code_delivery_reading_scope') == 1
        and context.get('code_delivery_reading_scope') == 1)


def reading_guidance(context):
    from . import experiment_review_reuse as experiment_reuse
    if context.get('rapid_revision', {}).get('material_format') == 'shared_text_v1':
        return GUIDANCE + ' 仅当材料顶层恰为 document 和 shared_text 两个字段时，按 shared_text 还原 document 中 {"$shared_text":"tN"} 引用的完整原文。仍须按改动范围核对源码。'
    if experiment_reuse.KEY in context:
        if context[experiment_reuse.KEY].get('version') == 4:
            return ('本席初步分析中已完整收到且逐项相同的基础证据和源码可复用。读完本轮提案、补充证据和评审意见，'
                    '用所需工具核对原文，独立判断并投本轮票。')
        if context[experiment_reuse.KEY].get('version') == 3:
            return ('两席已分别读过本版初步分析中的相同基础材料。请独立读完本次新增材料，'
                '用 experiments.compare 核对补充快照的 required_paths，保留本人回执并作出本次事实判断。')
        if context[experiment_reuse.KEY].get('version') == 2:
            return ('两席各自查看本次候选审码结果和变化的证据。previous_experiment_reading 标明本人在上次分析中'
                '已经收到且内容完全相同的区段。原数据和源码仍可按需读取；本轮仍须实际核对所引训练和实现证据并独立作答。')
        return ('两位评审各自读完本轮材料，重点核对新提案、上一版具体异议和修订。'
                'previous_experiment_reading 列出本人已读且逐项相同的归档材料。'
                '可按需用 experiments.compare、experiments.inspect_module 核对原文。请独立提交本轮投票。')
    if 'code_revision_review' in context:
        return '用 reviews.read_material 分页读完本次差异和上一轮意见，分别独立作出复审结论。'
    from . import implementation_definition_review_scope as definitions
    if definitions.enabled(context):
        if context.get('definition_code_source_version') == 2:
            return definitions.GUIDANCE + definitions.VERSION_GUIDANCE
        return definitions.GUIDANCE
    if code_delivery_scope(context):
        return GUIDANCE.replace('原文按索引通过 reviews.read_material 自行读完，每次最多50000字符；必要时继续使用原有证据工具。',
            '按索引完整读取当前批准实现文档、Codex 交付文本和一份批准架构设计。每页最多6000字符，每批最多50000字符。'
            'reviews.read_material 提供按需核对的宿主信息。')
    return GUIDANCE


def review_instruction(instruction, context=None):
    """Only new requests remove obsolete host-authored role partitions."""
    guidance = reading_guidance(context or {})
    if instruction.endswith('\n'+GUIDANCE) or instruction.endswith('\n'+guidance):return instruction
    obsolete=(
        'director 判断研究假设与最小下一步；engineer 核查实现、优化与架构；scout 核查反证、公平性和泄漏。\n',
        'director 负责目标与最小调整范围；engineer 负责梯度、优化器、学习率、数值尺度和架构路径；\nscout 负责反证、批次噪声、阶段切换、过拟合、基线公平性与数据泄漏。\n',
        'director: user success criteria and methodology; engineer: actual measurements, code, ablations and feasibility; scout: novelty evidence, fairness, leakage and strongest objections. ')
    for text in obsolete:instruction=instruction.replace(text,'')
    instruction=instruction.replace('本轮仅有三方诊断、一个最小方案和三方复核','本轮仅有独立诊断、一个最小方案和独立复核')
    return instruction+'\n'+guidance


def sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def make_binding(source_identity, domain):
    if not isinstance(source_identity, dict) or not source_identity or not isinstance(domain, str) or not domain:
        raise ValueError('Full review requires an exact source identity and domain')
    body = {'version': 1, 'reviewers': list(REVIEWERS), 'scope': 'full',
        'read_limit': READ_LIMIT, 'domain': domain, 'source_identity': copy.deepcopy(source_identity)}
    return {**body, 'sha256': sha(body)}


def validate_binding(binding):
    if (not isinstance(binding, dict) or set(binding) != {'version', 'reviewers', 'scope', 'read_limit',
            'domain', 'source_identity', 'sha256'} or binding.get('version') != 1
            or binding.get('reviewers') != list(REVIEWERS) or binding.get('scope') != 'full'
            or binding.get('read_limit') != READ_LIMIT or not isinstance(binding.get('domain'), str)
            or not binding['domain'] or not isinstance(binding.get('source_identity'), dict)
            or not binding['source_identity']
            or binding.get('sha256') != sha({k: v for k, v in binding.items() if k != 'sha256'})):
        raise ValueError('Unknown or inconsistent independent full review policy')
    return binding


def bind(context, binding):
    validate_binding(binding)
    result = copy.deepcopy(context)
    if KEY in result and result[KEY] != binding:
        raise ValueError('Cannot replace a frozen full review policy')
    result[KEY] = copy.deepcopy(binding)
    return result


def answer(vote):
    return {k: copy.deepcopy(v) for k, v in vote.items() if k not in {'role', KEY}}


def source_context(context):
    return {k: copy.deepcopy(v) for k, v in context.items() if k not in {KEY, READING, MATERIALS,'full_review_notes'}}


def resolve_materials(store, run_id, context):
    """Expose the complete authenticated scientific snapshot, never a role slice."""
    from .experiment_workflow import sha as evidence_sha
    ref = context.get('validation_evidence')
    if not isinstance(ref, dict) or not ref.get('id'):
        return None
    archive = store.get('experiment_evidence', ref['id']) or {}
    if archive.get('run_id') != run_id or evidence_sha(archive.get('summary')) != ref.get('sha256'):
        raise ValueError('Full-review scientific evidence snapshot changed')
    summary = archive['summary']
    result = {'evidence_reference': copy.deepcopy(ref), 'evidence': copy.deepcopy(summary), 'sources': []}
    supplemental_ref = context.get('experiment_decision', {}).get('artifact_evidence_handoff', {}).get('evidence')
    summaries = [summary]
    if supplemental_ref and supplemental_ref != ref:
        supplement = store.get('experiment_evidence', supplemental_ref['id']) or {}
        if supplement.get('run_id') != run_id or evidence_sha(supplement.get('summary')) != supplemental_ref.get('sha256'):
            raise ValueError('Full-review supplemental evidence snapshot changed')
        result['supplemental_evidence'] = {'reference': copy.deepcopy(supplemental_ref), 'evidence': copy.deepcopy(supplement['summary'])}
        summaries.append(supplement['summary'])
    for source_ref in [s.get(name) for s in summaries for name in ('candidate_code_sources', 'code_sources')]:
        if not source_ref or any(r['reference'] == source_ref for r in result['sources']):
            continue
        source = store.get('experiment_sources', source_ref['id']) or {}
        if source.get('run_id') != run_id or evidence_sha(source.get('files')) != source_ref.get('sha256'):
            raise ValueError('Full-review source archive changed')
        result['sources'].append({'reference': copy.deepcopy(source_ref), 'files': copy.deepcopy(source['files'])})
    return result


def stamp_vote(role, result, handoff_id, binding):
    validate_binding(binding)
    if role not in REVIEWERS or not isinstance(result, dict) or KEY in result or 'role' in result or not isinstance(handoff_id, str) or not handoff_id:
        raise ValueError('Invalid host full-review receipt wrapper')
    return {'role': role, **copy.deepcopy(result), KEY: {
        'policy': copy.deepcopy(binding), 'handoff_id': handoff_id}}


def reviewers(record_or_votes):
    if isinstance(record_or_votes, dict):
        if KEY in record_or_votes:
            value = record_or_votes[KEY]
            validate_binding(value.get('policy', value))
            return REVIEWERS
        record_or_votes = record_or_votes.get('votes', record_or_votes.get('review_votes', []))
    votes = list(record_or_votes or [])
    marked = [v for v in votes if isinstance(v, dict) and KEY in v]
    if marked:
        if len(marked) != len(votes):
            raise ValueError('Cannot mix legacy and full-review receipts')
        first = marked[0][KEY].get('policy')
        validate_binding(first)
        if any(v[KEY].get('policy') != first or not v[KEY].get('handoff_id') for v in marked):
            raise ValueError('Independent reviews have different frozen policies')
        return REVIEWERS
    return LEGACY_REVIEWERS


def members(votes, binding=None):
    try:
        roles = reviewers(votes)
        if binding is not None:
            validate_binding(binding)
            if roles != REVIEWERS or any(v[KEY]['policy'] != binding for v in votes):
                return False
        return (len(votes) == len(roles) and all(isinstance(v, dict) for v in votes)
            and sorted(v.get('role', '') for v in votes) == sorted(roles))
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def assignment_members(assignments):
    """Explicit request-map adapter; historical three-role maps stay legacy."""
    requests = list(assignments.values())
    if any(KEY in r.get('context', {}) for r in requests):
        receipts = [r.get('full_review_receipt') for r in requests]
        return members(receipts) and set(assignments) == set(REVIEWERS)
    return set(assignments) == set(LEGACY_REVIEWERS)


def prepare_assignments(owner, field, binding):
    """Keep completed legacy groups; preserve partial groups before replacing."""
    validate_binding(binding)
    requests = owner.setdefault(field, {})
    if requests and all(KEY not in r.get('context', {}) for r in requests.values()):
        if set(requests) == set(LEGACY_REVIEWERS) and all('answer' in r for r in requests.values()):
            return LEGACY_REVIEWERS, None
        owner.setdefault('independent_full_review_legacy', []).append({
            'field': field, 'requests': copy.deepcopy(requests)})
        owner[field] = {}
    groups = owner.setdefault('independent_full_review_groups', {})
    if field in groups and groups[field] != binding:
        raise ValueError('Full-review request group changed its frozen source identity')
    groups[field] = copy.deepcopy(binding)
    return REVIEWERS, binding


async def ask_assignment(harness, run_id, role, request, *, binding=None,validator=None):
    """Persistable request-map adapter retaining the exact original decision."""
    binding = binding or request.get('context', {}).get(KEY)
    if binding is None:
        result = await harness.ask(run_id, role, request['purpose'], request['instruction'], request['context'], request['schema'])
        request['answer'] = result
        return result
    if request.get('full_review_receipt'):
        authenticate(harness.engine.store, run_id, request['full_review_receipt'])
        validate_delivery(harness.engine.store, run_id, request['full_review_receipt'], engine=harness.engine)
        if validator is not None:validator(request['full_review_receipt'])
        return request['answer']
    vote = await ask(harness, run_id, role, request['purpose'], request['instruction'],
        request['context'], request['schema'], binding=binding,validator=validator)
    frozen = authenticate(harness.engine.store, run_id, vote)
    request.update({k: copy.deepcopy(frozen[k]) for k in ('instruction','context','schema')},
        purpose=harness.engine.store.get('handoffs', vote[KEY]['handoff_id'])['purpose'],
        answer=answer(vote), full_review_receipt=vote)
    return request['answer']


def authenticate(store, run_id, vote):
    if getattr(store, 'is_read_snapshot', False):
        return _authenticate_snapshot(store, run_id, vote)
    return _authenticate(store, run_id, vote)


@once
def _authenticate_snapshot(store, run_id, vote):
    return _authenticate(store, run_id, vote)


def _authenticate(store, run_id, vote):
    from .engine import Harness, digest
    from .prose_schema import validate
    marker = vote.get(KEY)
    if marker is None:
        raise ValueError('Full-review authentication requires its explicit host wrapper')
    binding = validate_binding(marker.get('policy'))
    role, hid = vote.get('role'), marker.get('handoff_id')
    owner = store.get('handoffs', hid) or {}
    frozen = store.get('handoff_inputs', hid) or {}
    decision = store.get('decisions', hid) or {}
    payload = {k: frozen.get(k) for k in ('instruction', 'context', 'schema')}
    if (role not in REVIEWERS or owner.get('run_id') != run_id or owner.get('to_role') != role
            or owner.get('status') != 'completed'
            or frozen.get('run_id') != run_id or frozen.get('context', {}).get(KEY) != binding
            or owner.get('input_hash') != digest(payload)
            or owner.get('output_hash') != digest(answer(vote))
            or Harness.decision_key(run_id, role, owner.get('purpose', ''), **payload) != hid
            or decision.get('run_id') != run_id or decision.get('role') != role
            or decision.get('purpose') != owner.get('purpose')
            or decision.get('result') != answer(vote)):
        raise ValueError('Full-review receipt lacks this role\'s exact frozen decision')
    validate(answer(vote), frozen['schema'])
    validate_recovery(frozen['context'])
    identity = binding['source_identity']
    if identity.get('context_sha256') and not purpose_matches(owner.get('purpose', ''), binding['domain']):
        raise ValueError('Full-review policy is not bound to this producer purpose')
    if identity.get('context_sha256') and identity['context_sha256'] != sha(source_context(frozen['context'])):
        raise ValueError('Full-review policy does not bind the actual original scientific material')
    if identity.get('schema_sha256') and identity['schema_sha256'] != sha(frozen['schema']):
        raise ValueError('Full-review policy output contract drifted')
    if identity.get('instruction_sha256') and identity['instruction_sha256'] != sha(frozen['instruction']):
        raise ValueError('Full-review policy producer instruction drifted')
    resolved = resolve_materials(store, run_id, frozen['context'])
    if frozen['context'].get(MATERIALS) != resolved:
        raise ValueError('Full-review material no longer matches the complete original evidence and source archive')
    return frozen


def authenticate_complete(store, run_id, votes, binding=None):
    if not members(votes, binding):
        raise ValueError('Independent review requires the exact policy reviewer set')
    if reviewers(votes) == REVIEWERS:
        for vote in votes:
            authenticate(store, run_id, vote)
    return True


def material_text(context):
    from . import review_material_packet as packets
    if packets.enabled(context):return packets.material(context)
    values = source_context(context)
    if MATERIALS in context:
        from . import experiment_review_reuse as experiment_reuse
        values[MATERIALS] = experiment_reuse.reduced(context) if experiment_reuse.KEY in context else context[MATERIALS]
    original = json.dumps(values,
        ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if context.get('rapid_revision', {}).get('material_format') == 'shared_text_v1':
        return _shared_material(original)
    return original


@functools.lru_cache(maxsize=8)
def _shared_material(original):
    from .context_artifacts import shared_text_view
    from .task_document_projection import encoded
    if '"$shared_text":' in original:
        return original
    view = shared_text_view(json.loads(original))
    compact = encoded(view)
    if len(compact) >= len(original):
        return original
    restored = json.loads(encoded(view['document']), object_hook=lambda item:
        view['shared_text'][item['$shared_text']] if set(item) == {'$shared_text'} else item)
    if encoded(restored) != original:
        raise ValueError('Shared review text does not reconstruct the frozen material')
    return compact


def required_root_chars(context):
    from .implementation_definition_review_scope import enabled as scoped_definition
    return 0 if code_delivery_scope(context) or scoped_definition(context) else len(material_text(context))


def material_index(context):
    from . import review_material_packet as packets
    if packets.enabled(context):return packets.index(context)
    binding = validate_binding(context[KEY])
    text = material_text(context)
    digest = hashlib.sha256(text.encode()).hexdigest()
    return {'policy_sha256': binding['sha256'], 'material_sha256': digest,
        'document_id': 'independent-full-review:' + binding['sha256'], 'sha256': digest,
        'section_id': 'material', 'total_chars': len(text), 'offset': 0,
        'max_chars': PAGE_LIMIT, 'batch_max_chars': READ_LIMIT, 'read_with': TOOL}


def tool_available(context):
    if KEY not in context:
        return False
    validate_binding(context[KEY])
    return True


def project(context, *, role=None):
    """The host freezes original material; the model receives only its index."""
    if KEY not in context:
        return context
    result={KEY: copy.deepcopy(context[KEY]), 'review_material': material_index(context),
        'review_guidance': reading_guidance(context), **({READING: copy.deepcopy(context[READING])} if READING in context else {})}
    if context.get('experiment_vote_notes_protocol') == 1:
        result['experiment_vote_notes_protocol'] = 1
        result['review_guidance'] += ' 实验提案的阅读笔记用 key、statement、receipt_sha256s 记录本人已读页和观察。'
    if context.get('experiment_vote_focus_protocol') in (1, 2):
        from .experiment_workflow import sha as experiment_sha
        proposal = context['decision_proposal']
        digest = context['proposal_sha256']
        if (experiment_sha(proposal) != digest or context[KEY]['domain'] != 'experiment_vote:' + digest
                or proposal.get('evidence_sha256') != context['evidence_sha256']
                or context['validation_evidence']['sha256'] != context['evidence_sha256']):
            raise ValueError('Experiment vote does not match the frozen current proposal')
        result.update(decision_proposal=copy.deepcopy(proposal), proposal_sha256=digest,
                      evidence_sha256=context['evidence_sha256'])
        result['review_guidance'] += (' 本轮投票对象是随每次请求显示的 decision_proposal。根据当前原文核对哪些旧异议'
            '已经回应、哪些仍影响该动作；引用具体未决字段或调用。已有材料继续按本次完整阅读要求核对。')
        selected = context.get('experiment_decision', {}).get('selected_validation_runs')
        if context['experiment_vote_focus_protocol'] == 2 and selected:
            result['selected_validation_inputs'] = {'evidence': copy.deepcopy(selected['evidence']),
                'trials': [{k: copy.deepcopy(r[k]) for k in ('trial_id', 'attempt', 'fit_id', 'yaml_receipt',
                    'effective_profile', 'admitted_params', 'actual_parameter_comparison') if k in r}
                    for r in selected['result']['trials']]}
            result['review_guidance'] += (' effective_profile 指所选 YAML 段。admitted_params 是同次运行的宿主 JSON，'
                '可用 experiments.compare 按 evidence 与 path 读取原页，评分参数的实际来源以归档加载器为准。')
            if selected.get('field_sources'):
                result['selected_validation_inputs']['field_paths'] = copy.deepcopy(selected['field_sources']['trials'])
            if selected.get('previous_proposal_reads'):
                result['selected_validation_inputs']['original_author_pages'] = copy.deepcopy(selected['previous_proposal_reads'])
        pages = context.get('experiment_decision', {}).get('cited_proposal_reads')
        if pages:
            result['cited_proposal_reads'] = copy.deepcopy(pages)
            result['review_guidance'] += ' cited_proposal_reads 是当前提案作者真实收到的原页；本人独立复核可按其中 call 在本交接读取并引用自己的回执。'
    from .host_asset_definition_recovery import KEY as asset_evidence_key
    asset_evidence = context.get(asset_evidence_key, {})
    if asset_evidence.get('version') == 2 and 'operator_review_proposal' in asset_evidence:
        result[asset_evidence_key] = copy.deepcopy(asset_evidence)
    from .implementation_acceptance import enabled as acceptance_enabled
    if acceptance_enabled(context):
        result.update({k:copy.deepcopy(context[k]) for k in ('acceptance_budget','acceptance_workflow_id') if k in context})
    if code_delivery_scope(context):
        result['review_material']['required'] = False
        result['code_delivery_reading_scope'] = 1
    if context.get('whole_code_review'):
        from .code_whole_review import RESPONSE_GUIDANCE
        result['review_guidance'] += '\n' + RESPONSE_GUIDANCE
    from . import review_material_packet as packets
    if packets.enabled(context):
        if context.get('review_memory_context_policy') == 'index-v1':
            result['review_memory_context_policy'] = 'index-v1'
        if context[packets.PROTOCOL] == 3:
            result['review_guidance'] += (' 本请求采用材料协议3：先读简短root，再直接读catalog中required=true的当前实现说明、代码及明确未决要求。'
                '科研设计、实验计划与组合表、历史检查目录和旧版本均按具体问题按需读取，不要求先遍历。'
                '复用本人已投递的精确源码范围，补读缺口；每页6000、每批至多8页。原科学验收和执行门禁保持不变。')
        else:
            result['review_guidance'] += (' 本请求采用材料协议2：先读root，读完catalog中required=true的每份唯一材料；'
            'root含完整共享计划模板和全部组合。required=false是按需可追溯附录，不要求遍历旧源码、历史引文或重复展开对象。'
            '通过同一工具的material_id选择具体材料，每页6000、每批至多8页。差异只是定位，不能代替当前完整必要源码。')
        reuse=context.get(packets.REUSE,{}).get(role,{})
        if reuse:result['review_material']['same_role_read_reuse']=copy.deepcopy({k:v for k,v in reuse.items() if k!='observations'})
        from .metric_capability_evidence_recovery import KEY as RECOVERY
        if RECOVERY in context:
            result['review_memory_context_policy'] = 'index-v1'
            packet = packets.compile_packet(context)
            result['review_guidance'] = ('This is a source-bound evidence continuation of your own original review. '
                'Read only missing required ranges; authenticated old pages below are re-exhibited verbatim, '
                'not new tool reads or approval. Use the original material catalog for additional decisive evidence. '
                'Keep every unresolved objection and all original scientific/execution gates.')
            result['_review_evidence_reuse'] = {'version': 1, 'role': role,
                'source_sha256': packet['source_sha256'], 'scientific_approval': False,
                'fragments': [{'material_id': mid, 'sha256': packets.sha(packet['nodes'][mid]),
                    'char_start': start, 'char_end': end, 'text': packet['nodes'][mid][start:end]}
                    for mid in packet['required'] for start, end in reuse.get('ranges', {}).get(mid, [])]}
            result['review_navigation_indexes'] = copy.deepcopy(context[RECOVERY].get('navigation_indexes', {}))
    return result


def tool_schema(context):
    index = context.get('review_material') or material_index(context)
    validate_binding(context[KEY])
    if index.get('policy_sha256') != context[KEY]['sha256']:
        raise ValueError('Projected full-review material has another policy identity')
    result={'type': 'object', 'properties': {
        'policy_sha256': {'const': index['policy_sha256']},
        'material_sha256': {'const': index['material_sha256']},
        'offset': {'type': 'integer', 'minimum': 0, 'maximum': index['total_chars']},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': PAGE_LIMIT}},
        'required': ['policy_sha256', 'material_sha256', 'offset', 'limit'], 'additionalProperties': False}
    from . import review_material_packet as packets
    if packets.enabled(context) or index.get('protocol') in packets.SCOPES:
        ids=['root',*[r['id'] for r in index['materials']]]
        result['properties']['material_id']={'enum':ids}
        result['properties']['offset']['maximum']=max(index['total_chars'],*[r['chars'] for r in index['materials']])
    return result


class MaterialReader:
    """One operation's fixed source bytes; no global mutable-context cache.

    Compile/serialize once, then reconstruct the original page/receipt format.
    Never cache a role's delivery, decision, failed attempt or note authority.
    """
    def __init__(self, context):
        from . import review_material_packet as packets
        self._binding = copy.deepcopy(validate_binding(context[KEY]))
        self._packet = copy.deepcopy(packets.compile_packet(context)) if packets.enabled(context) else None
        self._packet_version = context[packets.PROTOCOL] if self._packet else None
        self._texts = ({'root': self._packet['root'], **self._packet['nodes']} if self._packet
            else {'root': material_text(context)})
        self._root_sha = hashlib.sha256(self._texts['root'].encode()).hexdigest()
        self._indices = {}
        root = copy.deepcopy(context.get('review_material') or self.index())
        self._schema = tool_schema({KEY: self._binding, 'review_material': root})
        # Match prose_schema.validate's relaxed schema and best-error selection,
        # but check the fixed catalog schema once per reader, not once per page.
        from .prose_schema import relax
        from jsonschema.validators import validator_for
        schema = relax(self._schema)
        validator_class = validator_for(schema)
        validator_class.check_schema(schema)
        self._validator = validator_class(schema)

    def text(self, material_id=None):
        key = (material_id or 'root') if self._packet else 'root'
        if key not in self._texts:
            raise ValueError('Unknown frozen review source identity')
        return self._texts[key]

    def encoding(self, material_id=None):
        return self._packet['formats'].get(material_id or 'root', 'json') if self._packet else 'json'

    def index(self, material_id=None):
        key = (material_id or 'root') if self._packet else 'root'
        if key not in self._indices:
            text = self.text(key)
            value = {'policy_sha256': self._binding['sha256'], 'material_sha256': self._root_sha,
                'document_id': 'independent-full-review:' + self._binding['sha256']
                    + (':' + key if self._packet and key != 'root' else ''),
                'sha256': hashlib.sha256(text.encode()).hexdigest(),
                'section_id': key if self._packet else 'material', 'total_chars': len(text),
                'offset': 0, 'max_chars': PAGE_LIMIT, 'batch_max_chars': READ_LIMIT, 'read_with': TOOL}
            if self._packet:
                value['material_id'] = key
                if key == 'root':
                    value.update(protocol=self._packet_version, materials=copy.deepcopy(self._packet['catalog']),
                        required_total_chars=len(text) + sum(len(self._texts[k]) for k in self._packet['required']))
            self._indices[key] = value
        return copy.deepcopy(self._indices[key])

    def read(self, args):
        from jsonschema.exceptions import best_match
        error = best_match(self._validator.iter_errors(args))
        if error is not None:
            raise error
        text, index = self.text(args.get('material_id')), self.index(args.get('material_id'))
        start, end = args['offset'], min(len(text), args['offset'] + args['limit'])
        if start > len(text):
            raise ValueError('Read offset exceeds this exact material')
        body = {**index, 'offset': start, 'end': end, 'char_start': start, 'char_end': end,
            'text': text[start:end], 'next_offset': end if end < len(text) else None, 'reading_only': True}
        call = {'name': TOOL, 'arguments': copy.deepcopy(args)}
        return {**body, 'receipt_sha256': sha({'call': call, 'result': body})}


def read_material(context, args):
    return MaterialReader(context).read(args)


def reading_handoffs(store, run_id, vote):
    if getattr(store, 'is_read_snapshot', False):
        return _reading_handoffs_snapshot(store, run_id, vote)
    return _reading_handoffs(store, run_id, vote)


@once
def _reading_handoffs_snapshot(store, run_id, vote):
    return _reading_handoffs(store, run_id, vote)


def _reading_handoffs(store, run_id, vote):
    """Authenticate every same-role, same-material read ancestor before reuse."""
    frozen = authenticate(store, run_id, vote)
    context, text = frozen['context'], material_text(frozen['context'])
    current, result = vote[KEY]['handoff_id'], []
    while current:
        if current in result or len(result) >= 32:
            raise ValueError('Cyclic full-review reading ancestry')
        owner = store.get('handoffs', current) or {}
        prior = (store.get('handoff_inputs', current) or {}).get('context', {})
        raw = (store.get('decisions', current) or {}).get('result')
        authenticate(store, run_id, stamp_vote(vote['role'], raw, current, context[KEY]))
        if (owner.get('run_id') != run_id or owner.get('to_role') != vote['role']
                or prior.get(KEY) != context[KEY] or material_text(prior) != text):
            raise ValueError('Full-review reading ancestor changed role or material')
        result.append(current)
        parent = prior.get(READING, {})
        current = parent.get('parent_handoff_id')
        if current:
            decision = store.get('decisions', current) or {}
            if (decision.get('run_id') != run_id or decision.get('role') != vote['role']
                    or sha(decision.get('result')) != parent.get('parent_result_sha256')):
                raise ValueError('Full-review reading parent lacks its original decision')
    return result


def read_progress(store, run_id, vote, tools):
    """Count the union of real page intervals; pagination is not progress."""
    groups = {}
    for hid in reading_handoffs(store,run_id,vote):
        for row in (store.get('handoff_progress',hid) or {}).get('tool_results',[]):
            call, page = row.get('call',{}), row.get('result',{})
            if call.get('name') not in tools or not isinstance(page.get('text'),str):
                continue
            start,end=page.get('char_start',page.get('offset')),page.get('char_end',page.get('end'))
            if type(start) is not int or type(end) is not int or not 0<=start<end or end-start!=len(page['text']):
                continue
            identity={'tool':call['name'],'arguments':{k:v for k,v in call.get('arguments',{}).items()
                if k not in {'offset','limit'}},'source':{k:page.get(k) for k in
                ('document_id','sha256','source_sha256','content_sha256','evidence_sha256','path')}}
            groups.setdefault(sha(identity),[]).append((start,end))
    total=0
    for spans in groups.values():
        cursor=0
        for a,b in sorted(spans):
            total+=max(0,b-max(cursor,a));cursor=max(cursor,b)
    return total


def validate_delivery(store, run_id, vote, *, engine=None):
    """Acquisition is insufficient: credit only exact submitted source ranges."""
    if engine is not None and engine.store is not store:
        raise ValueError('Full-review document verification requires its owning engine')
    if not getattr(store, 'is_read_snapshot', False) and hasattr(store, 'read_snapshot'):
        with store.read_snapshot() as snapshot:
            owner = copy.copy(engine) if engine is not None else None
            if owner is not None:
                owner.store = snapshot
            return validate_delivery(snapshot, run_id, vote, engine=owner)
    from .harness_activity import checkpoint
    checkpoint('review_validation', 0, 0, '项', vote.get('role'))
    from .evidence_delivery import delivery_spans, tracking_required
    frozen = authenticate(store, run_id, vote)
    context = frozen['context']
    return validate_reading(store, run_id, vote['role'], vote[KEY]['handoff_id'], context,
        reading_handoffs(store, run_id, vote), engine=engine)


def validate_reading(store, run_id, role, hid, context, owners, *, engine=None):
    """Pure submitted-byte proof, shared by final acceptance and closeout.

    Callers authenticate the current frozen input and each completed ancestor;
    an unfinished request supplies no substitute result or approval.
    """
    from .evidence_delivery import delivery_spans, tracking_required
    from . import review_material_packet as packets
    from . import implementation_definition_review_scope as definitions
    from . import experiment_review_reuse as experiment_reuse
    if experiment_reuse.KEY in context:
        from types import SimpleNamespace
        experiment_reuse.validate(engine or SimpleNamespace(store=store), run_id, role, context)
    if definitions.enabled(context):
        if engine is None or engine.store is not store or not tracking_required(store, run_id, hid):
            raise ValueError('Definition review requires the owning engine and actual submitted inventory')
        return definitions.validate_reading(engine, run_id, role, context, owners)
    if code_delivery_scope(context):
        if not tracking_required(store, run_id, hid):
            raise ValueError('Code review requires an actual submitted material inventory')
        return _validate_reading_documents(store, run_id, context, owners, engine)
    if packets.enabled(context):
        from .review_material_reuse import validate_reading as validate_packet
        validate_packet(store, run_id, role, hid, context, owners)
        # Referenced documents keep their independent original coverage gate.
        return _validate_reading_documents(store, run_id, context, owners, engine)
    index, text = material_index(context), material_text(context)
    if not tracking_required(store, run_id, hid):
        raise ValueError('Full review requires an actual submitted material inventory')
    spans = []
    visited = owners
    for current in visited:
        prior = (store.get('handoff_inputs', current) or {}).get('context', {})
        from .full_review_receipts import successful_reads
        pages = successful_reads(store, run_id, role, current, prior,
            (store.get('handoff_progress', current) or {}).get('tool_results', []))
        actual = [(page['char_start'], page['char_end']) for _, page in pages]
        submitted = delivery_spans(store, run_id, role, index['document_id'], index['sha256'],
            identity_key='document_id', handoff_id=current, text=text)
        for row in submitted:
            for start, end in actual:
                a, b = max(start, row['char_start']), min(end, row['char_end'])
                if a < b:
                    spans.append({'char_start': a, 'char_end': b})
    cursor = 0
    for span in sorted(spans, key=lambda x: (x['char_start'], x['char_end'])):
        if span['char_start'] > cursor:
            break
        cursor = max(cursor, span['char_end'])
    if cursor != len(text):
        error = ValueError('Full review material has not been completely submitted to this reviewer')
        error.missing_offset = cursor
        raise error
    return _validate_reading_documents(store, run_id, context, owners, engine)


def validate_group_delivery(engine, run_id, votes):
    """One transaction shares immutable inventory operands, never approvals."""
    if not getattr(engine.store, 'is_read_snapshot', False):
        with engine.store.read_snapshot() as snapshot:
            owner = copy.copy(engine)
            owner.store = snapshot
            return validate_group_delivery(owner, run_id, votes)
    authenticate_complete(engine.store, run_id, votes)
    for vote in votes:
        validate_delivery(engine.store, run_id, vote, engine=engine)
    return True


def _validate_documents(store,run_id,vote,engine):
    context=authenticate(store,run_id,vote)['context']
    visited=reading_handoffs(store,run_id,vote)
    return _validate_reading_documents(store, run_id, context, visited, engine)


def _validate_reading_documents(store, run_id, context, visited, engine):
    if context.get('independent_full_review_documents'):
        root_chars = required_root_chars(context)
        from .implementation_reading import coverage
        if engine is None or engine.store is not store:
            raise ValueError('Full-review document verification requires its owning engine')
        missing_documents, document_covered = [], 0
        for doc in context['independent_full_review_documents']:
            required = [{'result': {'section_id': s['id'], 'char_start': s['start'], 'char_end': s['end']}}
                for s in doc['sections'] if s['end'] > s['start']]
            check = coverage(engine, run_id, doc, required, sorted(visited))
            document_covered += check['covered_chars']
            if not check['complete']:
                missing_documents.append({'document_id': doc['document_id'], 'sha256': doc['sha256'],
                    'missing_ranges': check['missing_ranges']})
        if missing_documents:
            error = ValueError('Full-review referenced document has missing actual submitted pages')
            error.missing_offset = root_chars
            error.covered_chars = root_chars + document_covered
            error.missing_documents = missing_documents
            raise error
    return True


def decision_key(engine, run_id, vote, purpose, instruction, context, schema):
    """Validate the original scientific request before selecting a new wrapper."""
    from .engine import Harness
    if KEY not in vote:
        return Harness.decision_key(run_id, vote['role'], purpose, instruction, context, schema)
    frozen = authenticate(engine.store, run_id, vote)
    binding = vote[KEY]['policy']
    clean = source_context(context)
    owner = engine.store.get('handoffs', vote[KEY]['handoff_id']) or {}
    if (not purpose_matches(owner.get('purpose', ''), purpose)
            or frozen.get('instruction') not in (instruction, review_instruction(instruction, frozen['context']))
            or frozen.get('schema') != schema):
        raise ValueError('Full-review purpose, prompt or schema drifted')
    if source_context(frozen.get('context', {})) != clean:
        raise ValueError('Full-review material differs from the expected scientific context')
    return vote[KEY]['handoff_id']


async def ask(harness, run_id, role, purpose, instruction, context, schema, *, binding=None, validator=None,
              scoped_validator=None):
    """Read-only missing-source recovery belongs to the same independent role."""
    from .engine import Harness
    from .prose_schema import validate
    if validator is not None and scoped_validator is not None:
        raise ValueError('A review must select one owning semantic validator')
    original = source_context(context)
    binding = binding or make_binding({'context_sha256': sha(original), 'instruction_sha256': sha(review_instruction(instruction, original)),
        'schema_sha256': sha(schema)}, purpose)
    store = harness.engine.store
    resolved = resolve_materials(store, run_id, original)
    material = {**original, **({MATERIALS: resolved} if resolved is not None else {})}
    frozen = bind(material, binding)
    previous_progress = {}
    reading_attempt, response_attempt = 0, 0
    current_purpose = purpose
    for attempt in range(17):
        current_instruction = review_instruction(instruction, frozen)
        hid = Harness.decision_key(run_id, role, current_purpose, current_instruction, frozen, schema)
        cached=store.get('decisions',hid)
        if cached:
            if hasattr(harness.engine,'check_active'):harness.engine.check_active(run_id)
            if hasattr(harness,'_moderation_gate'):harness._moderation_gate(run_id)
            response=copy.deepcopy(cached['result'])
        else:
            response = await harness.ask(run_id, role, current_purpose, current_instruction, frozen, schema)
        validate(response, schema)
        vote = stamp_vote(role, response, hid, binding)
        from .async_ownership import to_thread
        await to_thread(authenticate, store, run_id, vote)
        try:
            @operation
            def check_vote():
                from contextlib import nullcontext
                with (store.read_snapshot() if hasattr(store, 'read_snapshot') else nullcontext(store)) as snapshot:
                    owner = copy.copy(harness.engine)
                    owner.store = snapshot
                    validate_delivery(snapshot, run_id, vote, engine=owner)
                    if scoped_validator is not None:
                        scoped_validator(owner, vote)
                    elif validator is not None:
                        validator(vote)
            from .harness_activity import validate as validate_locally
            await validate_locally(harness.engine, run_id, check_vote,
                label='核验独立审核的原始材料与交付回执', _activity_handoff_id=hid, _activity_role=role)
            if ((response.get('approve') is True or response.get('approve_for_implementation') is True)
                    and response.get('issues')):
                raise ResponseCorrectionRequired('原答复同时批准并列出issues；请独立判断哪些问题阻断，批准须issues为空，否则保留具体问题并拒绝。',
                    [{'kind': 'approval_with_issues'}])
            return vote
        except ValueError as exc:
            if isinstance(exc, ResponseCorrectionRequired):
                source_repair = None
                if attempt != 16 and response_attempt >= 3 and 'code_revision_review' in frozen:
                    from .code_revision_review import recover_answer_contract
                    source_repair = await to_thread(recover_answer_contract,harness,run_id,vote,exc.problems)
                if attempt == 16 or response_attempt >= 3 and not source_repair:
                    from .engine import ReviewConvergenceBlocked
                    raise ReviewConvergenceBlocked('独立审核答复修订未收敛，原回复和交付回执保留：' + str(exc)) from exc
                response_attempt += 1
                current_purpose = purpose + ':full-response:' + str(response_attempt)
                frozen = bind(material, binding)
                frozen[READING] = {'version': 1, 'recovery_kind': 'response_correction',
                    'parent_handoff_id': hid, 'parent_result_sha256': sha(response),
                    'diagnostic': str(exc), 'problems': copy.deepcopy(exc.problems),
                    'previous_own_result': copy.deepcopy(response),
                    'instruction': ('这是原审核答复的定向修订，不是补读任务。复用本人同版本已认证阅读和原始依据；'
                        '按problems修订完整答复，可拆分跨验收项的问题，保持真实异议及依赖，不能为了格式编造依据或批准。'
                        '仅在解决具体证据缺口时读取。原请求、答复和费用保留，宿主仍执行全部原验收门禁。')}
                if source_repair:
                    frozen[READING]['answer_source_recovery'] = source_repair
                continue
            # Only an explicit missing-range diagnostic authorizes a reading
            # continuation. Identity/receipt failures are never pagination.
            if not any(hasattr(exc, field) for field in
                    ('missing_offset', 'missing_documents', 'missing_materials', 'missing_read_targets')):
                raise
            cursor = getattr(exc, 'missing_offset', 0)
            progress = getattr(exc, 'covered_chars', cursor)
            # Root/document delivery and domain-tool reads count different source bytes.
            dimension = next(field for field in ('missing_read_targets', 'missing_documents', 'missing_materials', 'missing_offset')
                             if hasattr(exc, field))
            if attempt == 16 or progress <= previous_progress.get(dimension, -1):
                from .engine import ReviewConvergenceBlocked
                raise ReviewConvergenceBlocked('独立全文审核补读未收敛，原回复和交付回执保留：' + str(exc)) from exc
            previous_progress[dimension] = progress
            reading_attempt += 1
            current_purpose = purpose + ':full-reading:' + str(reading_attempt)
            frozen = bind(material, binding)
            frozen[READING] = {'parent_handoff_id': hid, 'parent_result_sha256': sha(response),
                'missing_offset': cursor, 'required_end': required_root_chars(frozen),
                'missing_documents': getattr(exc, 'missing_documents', []),
                'missing_materials': getattr(exc, 'missing_materials', []),
                'missing_read_targets': getattr(exc, 'missing_read_targets', []),
                'diagnostic': str(exc), 'previous_own_result': copy.deepcopy(response),
                'instruction': '已有真实交付页按同角色原回执复用，从缺失位置继续读取；随后独立重新审核完整材料。'}
            targets = getattr(exc, 'next_read_targets', None)
            if targets:
                prior_id = Harness.decision_key(run_id, role, current_purpose, review_instruction(instruction, frozen), frozen, schema)
                if not store.get('handoffs', prior_id):
                    frozen[READING]['missing_offset'] = frozen[READING]['required_end']
                    frozen[READING]['missing_read_targets'] = targets
                    frozen[READING]['instruction'] = '基础材料已交付。仅补 missing_read_targets 指明的领域读取，引用本人实际回执并重新作答。'


async def collect_independent(ask_role):
    from .async_ownership import gather
    from .engine import ReviewConvergenceBlocked

    async def settle(role):
        try:
            return await ask_role(role)
        except ReviewConvergenceBlocked as error:
            return error

    results = await gather(*(settle(role) for role in REVIEWERS))
    for result in results:
        if isinstance(result, ReviewConvergenceBlocked):
            raise result
    return results


async def group(harness, run_id, purpose, instruction, context, schema, *, validator=None):
    """Freeze a fresh full group; already completed legacy groups remain reusable."""
    from .engine import Harness
    from .prose_schema import validate
    store = harness.engine.store if hasattr(harness, 'engine') else harness.store
    old = [store.get('decisions', Harness.decision_key(run_id, role, purpose, instruction, context, schema))
        for role in LEGACY_REVIEWERS]
    if all(row and isinstance(row.get('result'), dict) for row in old):
        from .engine import digest
        for role, row in zip(LEGACY_REVIEWERS, old):
            hid = Harness.decision_key(run_id, role, purpose, instruction, context, schema)
            owner = store.get('handoffs', hid) or {}
            frozen = store.get('handoff_inputs', hid) or {}
            expected = {'instruction': instruction, 'context': context, 'schema': schema}
            if (row.get('run_id') != run_id or row.get('role') != role or row.get('purpose') != purpose
                    or owner.get('run_id') != run_id or owner.get('to_role') != role
                    or owner.get('status') != 'completed' or owner.get('input_hash') != digest(expected)
                    or owner.get('output_hash') != digest(row['result'])
                    or {k: frozen.get(k) for k in expected} != expected or frozen.get('run_id') != run_id):
                raise ValueError('Legacy full-group reuse lacks its exact original completed requests')
            validate(row['result'], schema)
        return [{'role': role, **copy.deepcopy(row['result'])} for role, row in zip(LEGACY_REVIEWERS, old)]
    original = source_context(context)
    from . import experiment_review_reuse as experiment_reuse
    guidance_context = original if (experiment_reuse.KEY in original
        or original.get('rapid_revision', {}).get('material_format') == 'shared_text_v1') else None
    binding = make_binding({'context_sha256': sha(original), 'instruction_sha256': sha(review_instruction(instruction, guidance_context)),
        'schema_sha256': sha(schema)}, purpose)
    return await collect_independent(lambda role: ask(harness, run_id, role, purpose, instruction,
        original, schema, binding=binding, validator=validator))
