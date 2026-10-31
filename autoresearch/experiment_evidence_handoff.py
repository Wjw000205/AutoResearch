"""Read-only artifact requests have an owner; they are never code revisions."""
from __future__ import annotations

import copy
import hashlib
import json

from .experiment_workflow import encoded, sha

ERROR = 'A new architecture/diagnostic needs concrete reviewed changes'
KEY = 'artifact_evidence_handoff'
SCOPE_SCHEMA = {'type': 'array', 'minItems': 1, 'maxItems': 32, 'uniqueItems': True,
                'items': {'type': 'string', 'minLength': 1}}
GUIDANCE = '''
若下一步只是读取既有运行产物，使用 diagnose、unit_id=null、changes=[]、
diagnosis=insufficient_evidence；有 training_response 时 disposition=architecture_assessment。
宿主负责提供既有产物的有界哈希快照，交回 engineer 核验并形成具体下一步，再由三方独立复核。
这不是训练、代码修改或架构通过。已返回的产物仍不能回答的问题须明确保留，不重复索要相同证据。
'''


def requested(proposal):
    return (proposal.get('action') == 'diagnose'
            and proposal.get('diagnosis') == 'insufficient_evidence'
            and proposal.get('unit_id') is None and proposal.get('changes') == []
            and not proposal.get('parameter_search')
            and proposal.get('training_response', {}).get('disposition', 'architecture_assessment')
            == 'architecture_assessment')


def scope_ids(evidence):
    return sorted({r['id'] for r in evidence.get('records', []) if r.get('status') == 'completed'
                   and not r.get('superseded') and r.get('params', {}).get('phase') == 'validation'})


def validate_scope(proposal, evidence):
    from .prose_schema import validate
    ids = proposal['artifact_trial_ids']
    validate(ids, SCOPE_SCHEMA)
    if not requested(proposal) or not set(ids).issubset(scope_ids(evidence)):
        raise ValueError('Artifact scope requires a read-only request for completed current validation trials')


def recoverable(record):
    return (record.get('status') == 'invalid' and record.get('rejection') == ERROR
            and requested(record.get('rejected_proposal') or {}) and not record.get(KEY))


def retained_analysis_instruction(engine, run_id, context, instruction, schema):
    """Resume either already-dispatched instruction version under its own key."""
    from .engine import Harness
    candidates = []
    purpose = 'experiment_diagnose:' + context['evidence_sha256']
    for text in (instruction, instruction + GUIDANCE):
        for role in ('director', 'engineer', 'scout'):
            key = Harness.decision_key(run_id, role, purpose, text, context, schema)
            owner = engine.store.get('handoffs', key) or {}
            if owner.get('run_id') != run_id or owner.get('to_role') != role:
                continue
            frozen = engine.store.get('handoff_inputs', key) or {}
            if frozen.get('instruction') == text and frozen.get('context') == context and frozen.get('schema') == schema:
                candidates.append((owner.get('created_at', ''), text))
    return max(candidates, key=lambda item: item[0])[1] if candidates else instruction


def _validation_only(value):
    """The source may contain rejected output; never expose held-out fields."""
    if isinstance(value, dict):
        if value.get('phase') in {'test', 'final_test', 'held_out'}:
            return {'withheld': 'held_out'}
        return {k: _validation_only(v) for k, v in value.items()
                if not (k.lower().startswith(('test', 'held_out', 'heldout', 'final_test'))
                        or k.lower() in {'stdout', 'stderr'})}
    if isinstance(value, list):
        return [_validation_only(v) for v in value]
    return value


def _file(engine, run_id, trial, relative, *, json_content=False):
    from .experiment_views import _safe_file
    path = _safe_file(engine, run_id, trial, relative)
    result = {'relative_path': relative, 'status': 'unavailable',
              'source': 'saved_run_artifact', 'scientific_approval': False}
    if path is None:
        return {**result, 'reason': 'Missing, inaccessible or outside the original run directory'}
    limit = 512 * 1024 if json_content else 128 * 1024 * 1024
    try:
        if path.stat().st_size > limit:
            return {**result, 'reason': 'Exceeds bounded read limit', 'limit_bytes': limit}
        digest, size, chunks = hashlib.sha256(), 0, []
        with path.open('rb') as stream:
            while chunk := stream.read(min(1024 * 1024, limit + 1 - size)):
                size += len(chunk)
                if size > limit:
                    return {**result, 'reason': 'File grew beyond bounded read limit'}
                digest.update(chunk)
                if json_content:
                    chunks.append(chunk)
        result.update(status='available', sha256=digest.hexdigest(), bytes=size)
        if json_content:
            value = json.loads(b''.join(chunks).decode('utf-8'),
                               parse_constant=lambda _: None)
            result['data'] = _validation_only(value)
        return result
    except (OSError, ValueError, UnicodeError):
        return {**result, 'status': 'unavailable', 'reason': 'Saved artifact cannot be read as requested'}


def collect(engine, run_id, evidence, ref, trial_ids=None):
    """Read original validation records only; do not run/import candidate code."""
    from .experiment_decisions import _safe_rows
    from .experiment_views import _projection
    rows = [r for r in evidence['records'] if r.get('status') == 'completed' and not r.get('superseded')]
    if trial_ids is not None:
        validate_scope({'action': 'diagnose', 'diagnosis': 'insufficient_evidence',
                        'unit_id': None, 'changes': [], 'artifact_trial_ids': trial_ids}, evidence)
        rows = [r for r in rows if r['id'] in trial_ids]
    requested_ids = [r['id'] for r in rows]
    from .artifact_pairs import dependencies, enrich
    rows, pairs = dependencies(evidence, rows)
    if not rows or len(rows) > 32:
        raise ValueError('Read-only evidence handoff requires 1..32 completed current validation trials')
    trials = []
    for row in rows:
        if row.get('run_id') != run_id or row.get('params', {}).get('phase') != 'validation':
            raise ValueError('Artifact request is outside current validation evidence')
        key = run_id + ':' + row['id']
        with engine.store._read_db() as db:
            saved = db.execute(
                "SELECT updated_at, CASE WHEN length(json_extract(data,'$.evaluation.details'))<=524288 "
                "THEN json_extract(data,'$.evaluation.details') END FROM records "
                "WHERE collection='experiments' AND key=?", (key,)).fetchone()
        current = _projection(engine.store, key, saved[0]) if saved else None
        if not current or _safe_rows([current])[0] != row:
            raise ValueError('Original validation record changed before artifact handoff')
        details = ({'status': 'available', 'source': 'saved_evaluation.details',
                    'data': _validation_only(json.loads(saved[1])), 'sha256': hashlib.sha256(saved[1].encode()).hexdigest()}
                   if saved[1] else {'status': 'unavailable', 'reason': 'Missing or exceeds bounded read limit'})
        model = _file(engine, run_id, current, 'artifacts/model_provenance.json', json_content=True)
        checkpoint = _file(engine, run_id, current, 'artifacts/best.pt')
        data = model.get('data') or {}
        detail_data = details.get('data') or {}
        if not isinstance(data, dict) or not isinstance(detail_data, dict):
            raise ValueError('Runtime provenance must be a JSON object')
        inventory = data.get('inventory') or {}
        summary = {
            'trial_id': row['id'], 'seed': row['params'].get('seed'), 'variant': row['params'].get('variant'),
            'algorithm': row['params'].get('algorithm'), 'params_sha256': sha(row['params']),
            'code_sha256': row.get('code_sha256'), 'plan_sha256': row.get('experiment_plan_sha256'),
            'record_projection_sha256': sha(row), 'metrics': row['metrics'],
            'model_provenance': {k: v for k, v in model.items() if k != 'data'},
            'evaluation_details': {k: v for k, v in details.items() if k != 'data'},
            'checkpoint': checkpoint, 'training': detail_data.get('training'),
            'capacity': data.get('capacity'), 'reported_capacity': detail_data.get('capacity'),
            'parameter_counts': {k: inventory.get(k) for k in ('P_arch', 'P_train')},
            'parameter_shapes': [{k: p.get(k) for k in ('name', 'shape', 'real_dof', 'requires_grad')}
                                 for p in inventory.get('parameters', []) if isinstance(p, dict)],
            'initialization': [{k: p.get(k) for k in ('name', 'shape', 'sha256')}
                               for p in data.get('initialization', []) if isinstance(p, dict)],
            'common_initialization_names': data.get('common_initialization_names'),
            'scope': 'Saved program-reported provenance plus current file-byte hashes. No independent model execution or checkpoint restore proof.',
        }
        trials.append({'summary': summary, 'evaluation_details': details, 'model_provenance': model})
    material = {'binding': evidence['binding'], 'source_evidence': ref, 'trials': trials,
                'execution_authorized': False, 'scientific_approval': False}
    if trial_ids is not None:
        material['artifact_trial_ids'] = sorted(trial_ids)
    return enrich(material, pairs, requested_ids)


async def prepare(engine, run_id, record, proposal, evidence, ref, checkpoint):
    if record.get(KEY):
        raise ValueError('Read-only artifact request is still unresolved after the assigned engineer handoff; retain the concrete evidence blocker')
    from .experiment_decisions import _archive
    from .parameter_reference_recovery import audit
    material = audit(record, evidence, ref)
    is_search_audit = material is not None
    is_yaml_audit = False
    if material is None:
        from .yaml_review_recovery import for_handoff
        material = await engine._read_consensus_check(run_id, for_handoff,
            engine, run_id, record, proposal, evidence, ref)
        is_yaml_audit = material is not None
    if material is None:
        material = await engine._read_consensus_check(
            run_id, collect, engine, run_id, evidence, ref, proposal.get('artifact_trial_ids'))
    supplement = _archive(engine, run_id, material)
    prior_reads = record.get('analysis_reads', [])
    engineer_reads = next((r for r in prior_reads if r['role'] == 'engineer'), {})
    source_paths = {p for r in prior_reads for p in r.get('source_paths', [])}
    modules = proposal.get('training_response', {}).get('module_paths', [])
    value = {'version': 1, 'status': 'assigned', 'owner': 'engineer', 'provider': 'host',
             'reviewers': ['director', 'engineer', 'scout'], 'request': copy.deepcopy(proposal),
             'request_sha256': sha(proposal), 'evidence': supplement,
            'required_paths': (['/parameter_search_audit'] if is_search_audit else
                               ['/yaml_candidate_audit/control','/yaml_candidate_audit/candidate',
                                '/yaml_candidate_audit/differences','/yaml_candidate_audit/validation'] if is_yaml_audit else
                               [f'/trials/{i}/summary' for i in range(len(material['trials']))]),
             'initial_analysis_reads': copy.deepcopy(prior_reads),
             'author_additional_reads': {
                 'analysis_kinds': ([] if set(engineer_reads.get('analysis_kinds', [])) & {'comparison', 'ablation'} else ['comparison']),
                 'module_paths': sorted(set(modules) - source_paths)},
             'execution_authorized': False, 'scientific_approval': False}
    if material.get('paired_review_protocol') == 1:
        value['required_paths'] += ['/pair_scope','/pair_comparisons']
    if is_search_audit:
        value['kind'] = 'parameter_search_audit'
    if is_yaml_audit:
        value['kind'] = 'yaml_candidate_audit'
    await checkpoint(**{KEY: value}, status='collecting_evidence')
    engine.store.event(run_id, 'experiment.evidence_handoff',
                       ('宿主已定位参数路径拒绝并提供只读配置差异，交工程师核验；仍须三方独立评审。'
                        if is_search_audit else '宿主已恢复候选与控制YAML全文及配置差异，待独立复核。' if is_yaml_audit
                        else '宿主已提供既有运行产物，交给工程师核验；随后由三方独立复核。'),
                       {'decision_id': record['id'], 'owner': 'engineer', 'evidence': supplement,
                        'execution_authorized': False})
    return value


def packet(saved, *, for_review=False):
    value = {**({'kind': saved['kind']} if saved.get('kind') else {}),
            **{k: copy.deepcopy(saved[k]) for k in ('version', 'owner', 'provider', 'reviewers', 'request_sha256',
                                     'evidence', 'required_paths', 'author_additional_reads',
                                     'execution_authorized', 'scientific_approval')},
            'request': {k: saved['request'][k] for k in ('hypothesis', 'verification', 'stop_condition')}}
    if 'artifact_trial_ids' in saved['request']:
        value['request']['artifact_trial_ids'] = copy.deepcopy(saved['request']['artifact_trial_ids'])
    if for_review:
        from .artifact_fact_review import review_packet
        value = review_packet(saved, value)
    return value


def request(record, context, instruction, schema):
    saved = record[KEY]
    context = copy.deepcopy(context)
    context['experiment_decision'][KEY] = packet(saved)
    schema = copy.deepcopy(schema)
    schema['properties']['artifact_evidence_sha256'] = {'const': saved['evidence']['sha256']}
    schema['required'].append('artifact_evidence_sha256')
    if saved.get('kind') == 'yaml_candidate_audit':
        from .yaml_review_recovery import GUIDANCE as yaml_guidance
        return {'purpose': 'experiment_decide:' + context['evidence_sha256'] + ':yaml-candidate-audit-1:' + saved['evidence']['sha256'],
            'context': context, 'schema': schema, 'instruction': instruction + '\n' + yaml_guidance +
            '\n用experiments.compare按required_paths读完两份版本、完整差异与静态校验。'
            '核对旧值、新值、未变字段和本次参数搜索绑定，明确未决的真实冲突；不重新索取训练产物。'
            '补充证据路径位于/yaml_candidate_audit，绑定artifact_evidence_sha256。'
            '核验后提交实际下一步，仍需三方独立投票；不要把协调记录路径写入evidence_paths。'}
    if saved.get('kind') == 'parameter_search_audit':
        return {'purpose': 'experiment_decide:' + context['evidence_sha256'] + ':parameter-path-audit-1:' + saved['evidence']['sha256'],
                'context': context, 'schema': schema,
                'instruction': instruction + GUIDANCE + (
                    '\n宿主已定位先前搜索在参数路径校验阶段被拒，拒绝前未生成候选配置。'
                    '用 experiments.compare 逐页读取 artifact_evidence_handoff.required_paths 中的配置审计。'
                    '审计中的 prospective 是按原控制重建的只读候选，不是已执行或已批准实验。'
                    '核对两侧各自控制SHA、完整参数及唯一差异；不要再索要同一拒绝事件的不存在运行产物。'
                    'parameter_search.factor_path 与 training_response.factor_paths 使用参数相对路径，'
                    '如 /common/lr；原证据引用仍用 /job_parameters/<job>/common/lr。'
                    '补齐 author_additional_reads 要求的原证据比较与源码读取。'
                    '必须处理 remaining_proposal_gate，路径纠正不消除训练告警或证明根因；不得猜测或改写实测证据。'
                    '复用原三方分析与已读运行产物，基于审计提出唯一下一步，仍须独立投票。'
                    '补充引用 /parameter_search_audit 绑定 artifact_evidence_sha256，'
                    '原比较和训练证据绑定 evidence_sha256。不运行训练、代码或留出集。')}
    text = ('\n接续你提出的既有产物核验，宿主已提供 artifact_evidence_handoff.evidence。'
            '先用 experiments.compare 按 required_paths 逐页读完各运行 summary，再按需读取同一快照的'
            ' evaluation_details/data、model_provenance/data；原始参数仍在 validation_evidence 的 /job_parameters。'
            '核验模型/初始化/容量/检查点绑定，区分文件哈希、程序自报和独立证明。'
            '还须补齐 author_additional_reads 中列出的比较分析和模块源码读取：用原 validation_evidence'
            ' 调用 experiments.analyze(kind=comparison) 和 experiments.inspect_module(view=source)。'
            '只引用本角色或原三方已有真实读取回执支持的模块，不凭模块名声称已核验。'
            '只完成这次只读核验，然后提交具体下一步。不要再把同一取证请求当作新代码修订；'
            '真实缺失或冲突明确保留为阻塞，不能补造证据、运行训练或宣称已通过。'
            '最终 artifact_evidence_sha256 必须绑定本次补充快照；之后三方仍须独立核对。')
    if 'artifact_trial_ids' in saved['request']:
        text = text.replace('核验模型/初始化/容量/检查点绑定，区分文件哈希、程序自报和独立证明。',
            '只针对原 request.verification 的具体问题核验所选运行，区分文件哈希、程序自报和独立证明。'
            '不要扩展为其他运行的初始化或容量全量复查；范围外问题仍保留在原研究证据与原门禁中。')
    return {'purpose': 'experiment_decide:' + context['evidence_sha256'] + ':artifact-evidence-1:' + saved['request_sha256'],
            'context': context, 'instruction': instruction + GUIDANCE + text, 'schema': schema}


def bound_evidence(engine, run_id, record, proposal):
    """Resolve only this decision's authenticated supplemental namespace."""
    digest = proposal.get('artifact_evidence_sha256')
    if not digest:
        return None
    saved = record.get(KEY) or {}
    from .artifact_fact_review import reference
    ref = reference(saved) if saved.get('independent_review') else saved.get('evidence') or {}
    archive = engine.store.get('experiment_evidence', ref.get('id', '')) or {}
    material = archive.get('summary')
    if (record.get('run_id') != run_id or archive.get('run_id') != run_id
            or digest != ref.get('sha256') or sha(material) != digest
            or not isinstance(material, dict) or material.get('source_evidence') != record['evidence']):
        raise ValueError('Supplemental proposal evidence does not match this decision and snapshot')
    return material


def assignment_key(engine, run_id, role, purpose, instruction, context, schema):
    """Authenticate the exact frozen assignment before using any of its reads."""
    from .engine import Harness
    key = Harness.decision_key(run_id, role, purpose, instruction, context, schema)
    owner = engine.store.get('handoffs', key) or {}
    frozen = engine.store.get('handoff_inputs', key) or {}
    if (owner.get('to_role') != role or owner.get('run_id') != run_id
            or frozen.get('run_id') != run_id or frozen.get('context') != context
            or frozen.get('instruction') != instruction or frozen.get('schema') != schema):
        raise ValueError('Artifact reads need the exact role-owned handoff')
    return key


def require_reads(engine, run_id, role, purpose, instruction, context, schema):
    """Full summary reads must belong to this role's exact paid assignment."""
    key = assignment_key(engine, run_id, role, purpose, instruction, context, schema)
    saved = context['experiment_decision'][KEY]
    ref = saved['evidence']
    archive = engine.store.get('experiment_evidence', ref['id']) or {}
    if archive.get('run_id') != run_id or sha(archive.get('summary')) != ref['sha256']:
        raise ValueError('Read-only artifact evidence changed')
    progress = engine.store.get('handoff_progress', key) or {}
    coverage = {p: [] for p in saved['required_paths']}
    kinds, sources, receipts = set(), set(), []
    from .experiment_decisions import _pointer
    texts = {p: encoded(_pointer(archive['summary'], p)) for p in coverage}
    for row in progress.get('tool_results', []):
        call, result = row.get('call') or {}, row.get('result') or {}
        args = call.get('arguments') or {}
        authenticated = result.get('receipt_sha256') == sha({'call': call,
            'result': {k: v for k, v in result.items() if k != 'receipt_sha256'}})
        if (authenticated and result.get('text') and result.get('evidence_sha256') == context['evidence_sha256']
                and args.get('evidence_id') == context['validation_evidence']['id']
                and args.get('expected_sha256') == context['evidence_sha256']):
            if call.get('name') == 'experiments.analyze':
                kinds.add(result['kind'])
                receipts.append(result['receipt_sha256'])
            if (call.get('name') == 'experiments.inspect_module' and result.get('source_read') is True
                    and args.get('snapshot', 'current') == 'current'):
                sources.add(result['path'])
                receipts.append(result['receipt_sha256'])
        path = args.get('path')
        start, end = result.get('offset'), result.get('end')
        if (call.get('name') != 'experiments.compare' or path not in coverage
                or args.get('evidence_id') != ref['id'] or args.get('expected_sha256') != ref['sha256']
                or not authenticated
                or type(start) is not int or type(end) is not int
                or not 0 <= start < end <= len(texts[path]) or result.get('text') != texts[path][start:end]):
            continue
        coverage[path].append((start, end))
        receipts.append(result['receipt_sha256'])
    missing = []
    for path, windows in coverage.items():
        cursor = 0
        for start, end in sorted(windows):
            if start > cursor:
                break
            cursor = max(cursor, end)
        if cursor < len(texts[path]):
            missing.append(path)
    if missing:
        raise ValueError(f'{role} must read the supplied artifact summaries before deciding: {missing}')
    resolutions = []
    if role == 'engineer' and purpose.startswith('experiment_decide:'):
        required = saved['author_additional_reads']
        if not set(required['analysis_kinds']).issubset(kinds):
            raise ValueError('Engineer must complete the assigned comparison/source reads before the next decision')
        unresolved = set(required['module_paths']) - sources
        if unresolved:
            # Old paid assignments may name a function while tool receipts
            # identify its file. Resolve the frozen reference, never rewrite it
            # or give a directory/symbol listing credit for reading source.
            from .source_read_identity import source_files, verified_alias
            try:
                files = source_files(engine.store, run_id, context)
                resolutions = [verified_alias(engine.store, run_id, context,
                    progress.get('tool_results', []), reference, files=files)
                    for reference in sorted(unresolved)]
            except ValueError as exc:
                raise ValueError('Engineer must complete the assigned comparison/source reads before the next decision: '
                                 + str(exc)) from exc
    return {'role': role, 'handoff_id': key, 'evidence_sha256': ref['sha256'], 'paths': list(coverage),
            'analysis_kinds': sorted(kinds), 'source_paths': sorted(sources), 'receipt_sha256s': sorted(set(receipts)),
            **({'source_reference_resolutions': resolutions} if resolutions else {})}


def merge_reads(original, additional):
    reads = copy.deepcopy(original)
    for row in reads:
        if row['role'] == additional['role']:
            for field in ('analysis_kinds', 'source_paths', 'receipt_sha256s'):
                row[field] = sorted(set(row.get(field, [])) | set(additional.get(field, [])))
            row['module_source_read'] = bool(row.get('module_source_read') or additional.get('source_paths'))
            row['supplemental_handoff_id'] = additional['handoff_id']
    return reads
