"""Recover missing asset-interface facts before another definition draft."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .engine import ResearchBlocked, digest

KEY = 'host_asset_definition_evidence'
CORRECTION = 'host_asset_author_correction'
FOLLOWUP = 'host_asset_next_action'
PROPOSALS = 'implementation_asset_interface_proposals'
ASSESSMENT = 'asset_interface_proposal_review'
PROPOSAL_GUIDANCE = (
    '\n对 operator_review_proposal 的方案本身独立报告 asset_interface_proposal_review：'
    'accept 表示技术上可行，具体实现和验证仍须按正常流程审查；revise 写明需要改动的条款；'
    'reject 写明与冻结协议冲突之处。单独判断本版工程定义是否完整，分别保留证据和理由。'
    '对候选设计的意见不授予数据访问、训练或科学验收。')
AUTHOR_GUIDANCE = (
    '\n本次新增宿主资产接口证据见 host_asset_definition_evidence。按其实际交付字段补齐资产接入，'
    '区分清单摘要和原文件 SHA256。冻结资产核验只证明保留文件；现有运行约束、分区身份和阶段权限各自核验。'
    '如仍需宿主另提供接口，指出确切已批准条款及缺失的能力，区分这个阻塞与已明确的资产接口。'
)
REVIEW_GUIDANCE = (
    '\n本版作者拿到了 host_asset_definition_evidence。针对原缺口核查现有宿主资产接口是否已正确接入定义。'
    '宿主资产核验、阶段访问权限和运行验收分别判断；如仍需定义，逐项指出约束来源及宿主缺少的输入。'
    'host_asset_next_action 只针对上述原问题：已解决填 resolved，利用本轮已有证据即可修正的填 author_revision，'
    '确实缺少本轮权限范围内无法提供的宿主能力或外部授权才填 external_dependency，并写明需要的确切能力或授权。'
)


def review_schema(schema, evidence):
    schema['properties'][FOLLOWUP] = {'type': 'string',
        'enum': ['resolved', 'author_revision', 'external_dependency']}
    schema['required'].append(FOLLOWUP)
    if 'operator_review_proposal' in evidence:
        schema['properties'][ASSESSMENT] = {'type': 'object', 'additionalProperties': False,
            'properties': {'proposal_sha256': {'type': 'string', 'const': evidence['operator_review_proposal']['sha256']},
                           'decision': {'type': 'string', 'enum': ['accept', 'revise', 'reject']},
                           'rationale': {'type': 'string', 'minLength': 1}},
            'required': ['proposal_sha256', 'decision', 'rationale']}
        schema['required'].append(ASSESSMENT)


def validate_review(evidence, result):
    if 'operator_review_proposal' in evidence:
        opinion = result.get(ASSESSMENT) or {}
        if (not isinstance(opinion, dict) or opinion.get('proposal_sha256') != evidence['operator_review_proposal']['sha256']
                or opinion.get('decision') not in ('accept', 'revise', 'reject')
                or not isinstance(opinion.get('rationale'), str) or not opinion['rationale'].strip()):
            raise ResearchBlocked('候选资产接口的独立意见未绑定本次方案。')
    unresolved = any(not item['resolved'] and item['issue_id'] in evidence['issue_ids']
                     for item in result['resolutions'])
    allowed = {'author_revision', 'external_dependency'} if unresolved else {'resolved'}
    if result.get(FOLLOWUP) not in allowed:
        raise ResearchBlocked('宿主资产的待补处理与原问题解决状态不一致。')


def _asset_dependency(request):
    issue = request['issue']
    text = issue['issue'].lower()
    return (bool(request['references'])
        and issue['binding'].get('disposition') == 'needs_definition'
        and ('宿主' in text or 'host' in text)
        and any(term in text for term in ('资产清单', 'asset manifest', 'manifest_sha256'))
        and any(term in text for term in ('哈希', 'sha256', 'hash'))
        and any(term in text for term in ('权限', 'permission', 'authorization')))


def _proposal_scope(run_id, state, record):
    return {'run_id': run_id, 'workflow_id': state['id'], 'cycle': state['cycle'],
        'implementation_generation': state.get('implementation_generation'), 'definition_id': record['id'],
        'definition_source_sha256': record['source_sha256'], 'request_ids': record['request_ids'],
        'base_row_sha256': record['base_row_sha256'], 'plan_sha256': digest(state['common']['experiment_plan'])}


def submit_proposal(engine, run_id, expected_definition_id, proposal):
    """Accept an operator's question for the next unfrozen definition, never an answer or a permit."""
    import json
    from .implementation_definition_repair import FIELD
    from .implementation_review_clock import _get, _put
    from .storage import now
    with engine.store._db() as db:
        db.execute('BEGIN IMMEDIATE')
        run = _get(db, 'runs', run_id, {})
        control = run.get('state', {})
        if run.get('stage') != 'implementation' or run.get('status') not in {'paused', 'blocked'}:
            raise ValueError('请在实现阶段暂停或阻断后提交，以免与下一份作者输入的冻结并发。')
        rows = db.execute("""SELECT data FROM records WHERE collection='codex_workflows'
            AND json_extract(data,'$.run_id')=? AND json_extract(data,'$.kind')='implementation'
            AND json_extract(data,'$.cycle')=? AND json_extract(data,'$.implementation_generation') IS ?""",
            (run_id, control.get('research_cycle', 1), control.get('implementation_generation'))).fetchall()
        if len(rows) != 1:
            raise ValueError('无法唯一定位当前实现工作流。')
        state = json.loads(rows[0]['data'])
        record = state.get(FIELD, {})
        if (record.get('id') != expected_definition_id or record.get('kind') != 'engineering_definition'
                or record.get('status') not in {'authoring', 'reviewing'}):
            raise ValueError('工程定义已变更，请核对当前范围。')
        scope = _proposal_scope(run_id, state, record)
        key = state['id'] + ':' + record['id']
        body = {'version': 1, 'run_id': run_id, 'scope': scope, 'operator_proposal': proposal, 'purpose': 'independent_review_only',
                'scientific_approval': False, 'execution_authorized': False}
        row = {**body, 'sha256': digest(body)}
        previous = _get(db, PROPOSALS, key)
        if previous is not None and previous != row:
            raise ValueError('本范围已有另一份待审方案，保留原提交。')
        if previous is None:
            _put(engine.store, db, PROPOSALS, key, row, now())
            db.execute('INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,?,?,?,?)',
                (run_id, 'implementation.asset_interface_proposal_received', '数据资产接口方案已提交独立审查。',
                 json.dumps({'definition_id': record['id'], 'sha256': row['sha256']}), now()))
        return row


def _proposal(engine, run_id, state, record):
    row = engine.store.get(PROPOSALS, state['id'] + ':' + record['id'])
    if row is None:
        return None
    scope = _proposal_scope(run_id, state, record)
    if (row.get('scope') != scope or row.get('run_id') != run_id or row.get('version') != 1
            or row.get('purpose') != 'independent_review_only' or row.get('scientific_approval') is not False
            or row.get('execution_authorized') is not False
            or row.get('sha256') != digest({k:v for k,v in row.items() if k != 'sha256'})):
        raise ResearchBlocked('数据资产接口待审方案与冻结范围不一致。')
    return row


def collect(engine, run_id, state, record):
    """Derive versioned facts for the already authenticated definition source."""
    from . import dataset_runtime_contract, datasets, split_identity
    from .python_training import jobs

    if record.get('kind') != 'engineering_definition':
        return None
    requests = [engine.store.get('implementation_definition_requests', key) for key in record['request_ids']]
    proposal = _proposal(engine, run_id, state, record)
    if not requests or (proposal is None and not all(_asset_dependency(request) for request in requests)):
        return None
    plan = state['common']['experiment_plan']
    source_assets = set()
    for stage in ('experiments', 'tuning'):
        for _, params in jobs(plan, stage):
            for binding in datasets.bindings(params):
                source_assets.add((binding['asset_id'], binding['manifest_sha256']))
    assets = []
    for asset_id, sha in sorted(source_assets):
        try:
            if proposal is None:
                asset, _ = datasets.checked_asset(engine.store, engine.data_dir, run_id, asset_id, sha)
            else:
                asset = datasets.checked_manifest(engine.store, run_id, asset_id, sha)
                if asset.get('id') != asset_id or asset['manifest'].get('asset_id') != asset_id:
                    raise ValueError('Registered manifest belongs to another asset')
        except (OSError, ValueError) as exc:
            raise _blocked(state, record, '冻结计划绑定的宿主资产不可核验。',
                asset_id=asset_id, manifest_sha256=sha, asset_error=str(exc)) from exc
        assets.append({'asset_id': asset_id, 'manifest_sha256': sha,
            'manifest_version': asset['manifest']['version'],
            ('retained_files' if proposal is None else 'registered_files'): [
                {key: row[key] for key in ('path', 'trial_path', 'bytes', 'sha256')}
                for row in asset['manifest']['files']]})

    config = engine.store.get('runs', run_id)['config']
    sources = ('datasets.py', 'lab.py', 'dataset_file_identity.py', 'dataset_runtime_contract.py', 'split_identity.py')
    runtime = dataset_runtime_contract.asset_verification()
    # The general implementation runner writes the same file identity without
    # requiring the dataset-only workspace's helper bundle to be staged.
    runtime['stat_encoding'] = ('Host producer: autoresearch.dataset_file_identity.portable_stat. '
        'Use unsigned low 32 bits of st_dev on Windows and preserve the full device number elsewhere.')
    runtime['manifest_sha256_encoding'] = ('Compact sorted JSON of the acquired manifest, UTF-8, '
        'ensure_ascii=False, separators=(comma, colon), allow_nan=False.')
    body = {'version': 1, 'run_id': run_id, 'workflow_id': state['id'], 'definition_id': record['id'],
        'definition_source_sha256': record['source_sha256'], 'plan_sha256': digest(plan),
        'issue_ids': [request['issue']['issue_id'] for request in requests],
        'source_sha256s': {'autoresearch/' + name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in sources},
        'asset_interface': runtime,
        ('verified_retained_validation_assets' if proposal is None else 'registered_plan_assets'): assets,
        'authority': {'source': 'ExperimentRunner.run writes environment.json after stage_assets',
            'invocation_phase_field': 'params.json.phase',
            'invocation_sha256_field': 'environment.json.params_sha256',
            'invocation_sha256_encoding': 'Sorted UTF-8 JSON with default spaced separators, ensure_ascii=False.',
            'separate_asset_permissions_input': False,
            'phase_bound_split_identity_host_enabled':
                config.get('evaluation_contract_version', 1) >= split_identity.PROTOCOL,
            'phase_bound_split_identity_requires_approved_spec_opt_in': True},
        'staging_verified': False, 'execution_authorized': False, 'scientific_approval': False}
    if proposal is not None:
        body['version'] = 2
        from . import dataset_row_identity
        grant = dataset_row_identity.current(engine.store,run_id,state['cycle'])
        if grant and grant['proposal_sha256']==proposal['sha256'] and grant['plan_sha256']==body['plan_sha256']:
            body['validation_row_identity'] = {'authorization':grant,'interface':dataset_row_identity.interface()}
        body['operator_review_proposal'] = proposal
        body['proposal_review_instruction'] = ('逐项核对此候选接口是否满足冻结协议、资产与任务绑定和阶段权限。'
            '区分已核实的现有能力与需要新增的能力。作者可据此提出工程定义，复审人员独立判断。'
            '提交方案仅授权审查，不能充当接口已存在、数据访问获准或验证已执行的证据。')
    return {**body, 'sha256': digest(body)}


def author_evidence(engine, run_id, state, row):
    handoff_id = row.get('authoring_handoff_id')
    if not handoff_id:
        return None
    frozen = engine.store.get('handoff_inputs', handoff_id, {})
    evidence = frozen.get('context', {}).get(KEY)
    if evidence is None:
        return None
    if evidence['sha256'] != digest({key: value for key, value in evidence.items() if key != 'sha256'}):
        raise ResearchBlocked('补定义作者的冻结宿主证据身份不一致。')
    if 'operator_review_proposal' in evidence and evidence['operator_review_proposal'] != _proposal(
            engine, run_id, state, state['implementation_definition_repair']):
        raise ResearchBlocked('补定义作者收到的候选接口与操作员实际送审记录不一致。')
    from .implementation_authoring import validate_submission
    validate_submission(engine, run_id, state, row)
    return evidence


def _blocked(state, record, message, **details):
    from .implementation_failures import ImplementationCodeBlocked
    return ImplementationCodeBlocked(message, kind='codex_definition_required',
        details={'workflow_id': state['id'], 'definition_id': record['id'], 'recovery_target': 'dataset_runtime',
                 **details})


def correction_needed(engine, run_id, state, record, previous, evidence):
    """Allow one actionable author correction on the same host evidence."""
    if previous.get('approved') is not False:
        return False
    prior = author_evidence(engine, run_id, state, previous)
    if prior is None:
        return False
    metadata = {'sha256', 'source_sha256s'}
    if ({key: value for key, value in prior.items() if key not in metadata}
            != {key: value for key, value in evidence.items() if key not in metadata}):
        return False
    from .implementation_definition_repair import _review_members, validate_vote
    packets = previous.get('definition_reviews', [])
    if not _review_members(packets) or all(packet['result']['approve'] for packet in packets):
        raise ResearchBlocked('宿主资产定义的已退回版本缺少完整独立意见。')
    opinions = [validate_vote(engine, run_id, state, previous, packet) for packet in packets]
    actions = {opinion[FOLLOWUP] for opinion in opinions} - {'resolved'}
    if not actions:
        return False
    authored = engine.store.get('handoff_inputs', previous['authoring_handoff_id'])['context']
    if 'author_revision' in actions and not authored.get(CORRECTION):
        return True
    reason = ('同一宿主证据已完成一次定向作者更正，原问题仍未通过。'
        if 'author_revision' in actions else '独立复审确认原问题仍需要宿主能力或外部授权。')
    raise _blocked(state, record,
        reason + ' 已保存具体问题和审核回执。',
        evidence_sha256=prior['sha256'], spec_revision=previous['index'],
        review_handoffs=[packet['handoff_id'] for packet in packets],
        issues=[issue for packet in packets for issue in packet['result']['issues']])
