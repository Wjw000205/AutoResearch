"""Restore exact saved candidate/control documents for independent re-review."""
import copy

from .experiment_workflow import sha
from .storage import now

COLLECTION = 'yaml_review_successors'
KIND = 'yaml_candidate_audit'
PATHS = ['/yaml_candidate_audit/control', '/yaml_candidate_audit/candidate',
         '/yaml_candidate_audit/differences', '/yaml_candidate_audit/validation']
GUIDANCE = '''
yaml_candidate_review是从原持久化版本恢复的两份完整YAML、解析内容和逐字段差异，版本及SHA均已核验。
本次重点复审原配置正文/单因素差异异议，原否决票保留，不继承批准。正文就在该材料的control、candidate中，
可以独立比对；也可用hyperparameters.read按精确版本核对，已读两份正文将成对保留。
不要将旧请求中“只剩归档编号”的状态当作本次仍然缺失，不新建同一候选，不改源码或重跑控制。
宿主静态校验不证明运行消费或科学收益。只审当前单因素批次，仍须三方、资源和执行门禁。
consensus_resolution是协调指令，不能作为实验快照路径引用。配置补充证据使用/yaml_candidate_audit下的实际路径。
'''


def differences(left, right, path=''):
    if type(left) is not type(right):
        return [{'path': path, 'old': left, 'new': right, 'type_changed': True}]
    if isinstance(left, dict):
        rows = []
        for key in sorted(set(left) | set(right)):
            location = path + '/' + key.replace('~', '~0').replace('/', '~1')
            if key not in left or key not in right:
                rows.append({'path': location, 'old': left.get(key), 'new': right.get(key), 'membership_changed': True})
            else:
                rows += differences(left[key], right[key], location)
        return rows
    return [] if left == right else [{'path': path, 'old': left, 'new': right, 'type_changed': False}]


def collect(engine, run_id, source, evidence, ref):
    from . import hyperparameter_yaml as hp
    from .parameter_search import propose
    from jsonschema import Draft202012Validator
    spec = source.get('proposal', {}).get('parameter_search') or {}
    if not spec.get('yaml_revision_id'):
        return None
    if source.get('run_id') != run_id or source.get('evidence') != ref or sha(evidence) != ref['sha256']:
        raise ValueError('Candidate review source is not bound to this run and frozen evidence')
    candidate = hp.validate_candidate(engine, run_id, spec, evidence, propose(spec, evidence))
    parent = candidate['body'].get('parent')
    if not parent:
        raise ValueError('Candidate review requires an exact retained parent YAML version')
    control = hp.read_version(engine, run_id, parent)
    if control['body']['binding'] != candidate['body']['binding'] or control['body']['binding'] != evidence['binding']:
        raise ValueError('Candidate/control YAML bindings differ')
    schema = evidence['hyperparameter_yaml'].get('schema')
    def document(row):
        body = row['body']; parsed = hp.parse(body['yaml'])
        if parsed != body['document']:
            raise ValueError('Saved parsed YAML differs from the original text')
        if not schema:
            raise ValueError('Candidate review requires the frozen YAML schema')
        errors = [{'path': '/'+'/'.join(map(str, e.absolute_path)), 'constraint': e.validator}
                  for e in Draft202012Validator(schema).iter_errors(parsed)]
        return {'revision_id': row['id'], 'yaml_sha256': body['yaml_sha256'],
                'yaml': body['yaml'], 'document': parsed, 'schema_errors': errors}
    a, b = document(control), document(candidate)
    changes = differences(a['document'], b['document'])
    variants = ('baseline', 'proposed') if spec.get('variant', 'shared') == 'shared' else (spec['variant'],)
    expected = sorted('/profiles/' + variant + spec['factor_path'] for variant in variants)
    valid = (sorted(r['path'] for r in changes) == expected
             and all(not r.get('membership_changed') and r['new'] == spec['value'] for r in changes)
             and not a['schema_errors'] and not b['schema_errors'])
    return {'binding': copy.deepcopy(evidence['binding']), 'source_evidence': copy.deepcopy(ref),
        KIND: {'source_decision_id': source['id'], 'proposal_sha256': sha(source['proposal']),
            'control': a, 'candidate': b, 'differences': changes,
            'validation': {'candidate_matches_proposed_jobs': True, 'expected_changed_paths': expected,
                'only_declared_factor_changed': valid, 'control_schema_valid': not a['schema_errors'],
                'candidate_schema_valid': not b['schema_errors'], 'proposed_profile_unchanged':
                    a['document']['profiles'].get('proposed') == b['document']['profiles'].get('proposed'),
                'base_unit_id': spec['base_unit_id'], 'validation_seed': spec['validation_seed'],
                'scope_sha256': spec.get('scope_sha256'),
                'scope': 'Saved configuration identity and static comparison only; no runtime or scientific acceptance.'}},
        'execution_authorized': False, 'scientific_approval': False}


def for_handoff(engine, run_id, record, proposal, evidence, ref):
    """Route a YAML-specific disagreement to its saved configuration, not trials."""
    if proposal.get('artifact_trial_ids') or not record.get('consensus_parent'):
        return None
    parent = engine.store.get('experiment_decisions', record['consensus_parent']) or {}
    spec = parent.get('proposal', {}).get('parameter_search') or {}
    dissent = [v for v in parent.get('votes', []) if not v.get('approve') or v.get('issues')]
    checksum = spec.get('yaml_sha256')
    if (not checksum or not dissent or parent.get('status') != 'disputed'
            or any(not v.get('issues') or any(checksum not in issue for issue in v['issues']) for v in dissent)):
        return None  # Other or mixed disputes retain their original evidence route.
    return collect(engine, run_id, parent, evidence, ref)


def register(engine, run_id, root_id, instruction):
    from .record_archive import fingerprint, save
    from .experiment_decisions import _archive, validate_proposal
    from .diagnostic_planning_recovery import original, authenticate
    from .proposal_request_contract import VERSION
    run = engine.store.get('runs', run_id) or {}
    root = engine.store.get('experiment_decisions', root_id) or {}
    task = engine.store.get('experiment_consensus_tasks', root_id + ':consensus') or {}
    blocked = engine.store.get('experiment_decisions', task.get('current_id', '')) or {}
    if engine.store.get(COLLECTION, root_id):
        return engine.store.get('experiment_decisions', route(engine, run_id, root_id, root['evidence']))
    lease = engine.store.get('runtime', 'worker', {})
    if (run.get('status') not in {'blocked', 'paused'} or not instruction.strip()
            or lease.get('held') and lease.get('run_id') == run_id and engine.owner_alive(lease)
            or root.get('run_id') != run_id or root.get('status') != 'disputed'
            or root.get('proposal_sha256') != sha(root.get('proposal'))
            or {v.get('role') for v in root.get('votes', [])} != {'director','engineer','scout'}
            or blocked.get('consensus_parent') != root_id or blocked.get('status') != 'invalid'
            or blocked.get('proposal') or blocked.get('votes')
            or not blocked.get('rejection', '').startswith('Decision cites missing evidence')):
        raise ValueError('Explicit YAML recovery requires a settled, unapproved candidate and failed evidence handoff')
    from .experiment_consensus import source
    if (not task.get('rounds') or task['rounds'][-1]['parent_id'] != root_id
            or task['rounds'][-1]['source_sha256'] != sha(source(root))
            or task['rounds'][-1]['resolution'] != blocked.get('consensus_resolution')):
        raise ValueError('Candidate recovery lost the original consensus lineage')
    owner = original(engine, run_id, root, root['proposal'])
    mediation = task['rounds'][-1].get('mediation') or {}
    authenticate(engine, run_id, 'director', mediation.get('purpose'),
        {k: mediation.get(k) for k in ('instruction','context','schema')}, mediation.get('answer'))
    resolution = blocked['consensus_resolution']
    if resolution['source_sha256'] != sha({'source':source(root),'issues':resolution['issues']}):
        raise ValueError('Consensus resolution no longer matches the disputed candidate and votes')
    for vote in root['votes']:
        expected = {k:v for k,v in vote.items() if k != 'role'}
        found = False
        for hid in engine.store.record_keys('decisions',run_id,
                predicate="json_extract(data,'$.role')=? AND json_extract(data,'$.purpose')=?",
                parameters=(vote['role'],'experiment_vote:'+root['proposal_sha256'])):
            answer=engine.store.get('decisions',hid)
            if answer.get('result') != expected:continue
            frozen=engine.store.get('handoff_inputs',hid) or {}
            if frozen.get('context',{}).get('decision_proposal') != root['proposal']:continue
            authenticate(engine,run_id,vote['role'],answer['purpose'],
                {k:frozen.get(k) for k in ('instruction','context','schema')},expected)
            found=True;break
        if not found:raise ValueError('Original independent YAML dissent is not authenticated')
    ref = root['evidence']; archive = engine.store.get('experiment_evidence', ref['id']) or {}
    if archive.get('run_id') != run_id or sha(archive.get('summary')) != ref['sha256']:
        raise ValueError('Original candidate experiment evidence changed')
    material = for_handoff(engine, run_id, blocked, blocked['artifact_evidence_handoff']['request'], archive['summary'], ref)
    if not material or not material[KIND]['validation']['only_declared_factor_changed']:
        raise ValueError('The exact saved YAML pair does not resolve a single-factor configuration handoff')
    validate_proposal(root['proposal'], archive['summary'], ref['sha256'])
    supplement = _archive(engine, run_id, material)
    sources = [fingerprint(engine.store, col, key) for col,key in (
        ('experiment_decisions',root_id), ('experiment_decisions',blocked['id']),
        ('experiment_consensus_tasks',task['id']))]
    archives = [save(engine.store, run_id, s) for s in sources]
    child_id = root_id + ':yaml-review:' + sha([sources, supplement, instruction])
    child = {'id':child_id,'run_id':run_id,'cycle':root['cycle'],'evidence':copy.deepcopy(ref),
        'status':'reviewing','analyses':copy.deepcopy(root['analyses']),
        'analysis_reads':copy.deepcopy(root['analysis_reads']), 'proposal':copy.deepcopy(root['proposal']),
        'proposal_sha256':root['proposal_sha256'],'votes':[], 'created_at':now(),
        'scientific_approval':False,'provenance':'live','parameter_path_contract_version':1,
        'proposal_request_contract_version':VERSION,'proposal_reference_contract_version':2,
        'yaml_review_recovery':{'root_id':root_id,'supplement':supplement,'source_author_handoff_id':owner}}
    body = {'run_id':run_id,'evidence':ref,'retained_sources':sources,'archives':archives,
        'child_id':child_id,'supplement':supplement,'instruction':instruction,
        'source_proposal_sha256':root['proposal_sha256'],'execution_authorized':False,'scientific_approval':False}
    if engine.store.get('runs',run_id) != run or any(fingerprint(engine.store,s['collection'],s['key']) != s for s in sources):
        raise ValueError('YAML recovery sources changed during registration')
    if engine.store.get('experiment_decisions',child_id):
        raise ValueError('Unexpected existing YAML review successor')
    engine.store.batch([('experiment_decisions',child_id,child),(COLLECTION,root_id,
        {'id':root_id,'run_id':run_id,'body':body,'sha256':sha(body)})])
    engine.store.event(run_id,'experiment.yaml_review_recovered',
        '已恢复原候选与控制YAML正文及完整差异；原异议和错误交接保留，三方重新独立投票，未授权训练。',
        {'decision_id':child_id,'source_decision_id':root_id,'execution_authorized':False})
    return child


def route(engine, run_id, root_id, ref):
    row = engine.store.get(COLLECTION,root_id)
    if not row:return root_id
    body=row.get('body',{})
    from .record_archive import fingerprint
    if (row.get('run_id')!=run_id or sha(body)!=row.get('sha256') or body.get('evidence')!=ref
            or body.get('execution_authorized') is not False
            or any(fingerprint(engine.store,s['collection'],s['key']) != s for s in body['retained_sources'])):
        raise ValueError('YAML review recovery lost its original evidence or dissent')
    child=engine.store.get('experiment_decisions',body['child_id']) or {}
    if (child.get('run_id')!=run_id or child.get('evidence')!=ref
            or sha(child.get('proposal'))!=body['source_proposal_sha256']
            or child.get('yaml_review_recovery',{}).get('root_id')!=root_id
            or child['yaml_review_recovery'].get('supplement')!=body['supplement']):
        raise ValueError('YAML review successor changed its original candidate')
    return child['id']


def review_packet(engine, run_id, record):
    saved=record['yaml_review_recovery'];ref=saved['supplement']
    if route(engine,run_id,saved['root_id'],record['evidence']) != record['id']:
        raise ValueError('YAML review is not the registered successor')
    archive=engine.store.get('experiment_evidence',ref['id']) or {}
    material=archive.get('summary')
    root=engine.store.get('experiment_decisions',saved['root_id'])
    evidence=engine.store.get('experiment_evidence',record['evidence']['id'])['summary']
    if (archive.get('run_id')!=run_id or sha(material)!=ref['sha256']
            or material!=collect(engine,run_id,root,evidence,record['evidence'])):
        raise ValueError('Restored YAML documents differ from their exact saved versions')
    return {'evidence':ref,**copy.deepcopy(material[KIND]), 'prior_votes':copy.deepcopy(root['votes']),
        'prior_votes_are_historical':True,'execution_authorized':False,'scientific_approval':False}
