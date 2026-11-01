"""Versioned, source-bound YAML edits. Drafting is never execution approval."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path

import yaml

from .experiment_workflow import sha
from .storage import now
from .tuning_scope import safe_path, configured_value

COLLECTION = 'hyperparameter_yaml_versions'
TOOLS = ('hyperparameters.read', 'hyperparameters.revise')
RUNTIME_FILE = 'hyperparameters.yaml'
RECEIPT_FILE = 'hyperparameters.receipt.json'
MAX_BYTES = 128 * 1024


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if type(key) is not str or key in result:
            raise ValueError('YAML mapping keys must be strings and cannot be duplicated')
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def parse(text):
    if not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > MAX_BYTES:
        raise ValueError('YAML must be nonempty and at most 128 KiB')
    try:
        if any(isinstance(t, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken)) for t in yaml.scan(text)):
            raise ValueError('YAML anchors and aliases are not supported; write explicit values')
        value = yaml.load(text, Loader=UniqueLoader)
    except yaml.YAMLError as exc:
        raise ValueError('Invalid YAML syntax or unsupported YAML tag') from exc
    count = 0
    def check(v, depth=0):
        nonlocal count
        count += 1
        if count > 8192 or depth > 16:
            raise ValueError('YAML configuration exceeds its bounded shape')
        if isinstance(v, dict):
            for k, x in v.items():
                if type(k) is not str: raise ValueError('YAML keys must be strings')
                check(x, depth+1)
        elif isinstance(v, list):
            for x in v: check(x, depth+1)
        elif type(v) not in (str, int, float, bool, type(None)) or type(v) is float and not math.isfinite(v):
            raise ValueError('YAML values must be finite JSON-compatible configuration values')
    check(value)
    if not isinstance(value, dict) or set(value) != {'version', 'profiles'} or type(value['version']) is not int or value['version'] != 1:
        raise ValueError('YAML requires version: 1 and a profiles mapping')
    if not isinstance(value['profiles'], dict) or not set(value['profiles']) <= {'baseline', 'proposed', 'ablation'}:
        raise ValueError('YAML profiles may only name baseline, proposed and ablation')
    if not value['profiles'] or any(not isinstance(p, dict) for p in value['profiles'].values()):
        raise ValueError('Each YAML profile must be a mapping')
    return value


def render(value):
    text = yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
    parse(text)
    return text


def leaves(value, prefix=''):
    for key, child in value.items():
        path = prefix + '/' + key.replace('~', '~0').replace('/', '~1')
        if isinstance(child, dict):
            yield from leaves(child, path)
        else:
            yield path, child


def assign(tree, path, value):
    parts = [p.replace('~1', '/').replace('~0', '~') for p in path.strip('/').split('/')]
    target = tree
    for part in parts[:-1]:
        target = target.setdefault(part, {})
        if not isinstance(target, dict): raise ValueError('YAML path crosses a scalar')
    target[parts[-1]] = copy.deepcopy(value)


def profile(params, capabilities=()):
    result = {}
    for path, value in leaves(params):
        if safe_path(path): assign(result, path, value)
    for item in capabilities:
        if item['variant'] in ('shared', params['variant']):
            try: configured_value(params, item['path'], {'parameter_capabilities': []})
            except (ValueError, KeyError): assign(result, item['path'], item['default'])
    return result


def effective_profile(params, program, capabilities=()):
    defaults = program.get('hyperparameter_contract', {}).get('defaults', {}).get('profiles', {}).get(params['variant'], {})
    result = copy.deepcopy(defaults)
    for path, value in leaves(profile(params, capabilities)):
        assign(result, path, value)
    return result


def _store(engine, run_id, text, binding, *, parent=None, owner=None, evidence=None, controls=None, allowed=None):
    document = parse(text)
    checksum = hashlib.sha256(text.encode('utf-8')).hexdigest()
    body = {'binding': copy.deepcopy(binding), 'yaml_sha256': checksum, 'yaml': text,
            'document': document, 'parent': parent, 'owner_handoff_id': owner,
            'source_evidence': evidence, 'controls': controls or {}, 'allowed_paths': allowed or {},
            'execution_authorized': False}
    identifier = run_id + ':hyperparameters:' + sha(body)
    row = {'id': identifier, 'run_id': run_id, 'body': body, 'sha256': sha(body), 'created_at': now()}
    engine.store.mutate(COLLECTION, identifier, lambda old: old or row)
    return row


def read_version(engine, run_id, identifier, checksum=None):
    row = engine.store.get(COLLECTION, identifier) or {}
    if row.get('run_id') != run_id or sha(row.get('body')) != row.get('sha256'):
        raise ValueError('YAML version is missing, foreign or changed')
    body = row['body']
    if (hashlib.sha256(body['yaml'].encode('utf-8')).hexdigest() != body['yaml_sha256']
            or parse(body['yaml']) != body['document'] or checksum and checksum != body['yaml_sha256']):
        raise ValueError('YAML content identity changed')
    return row


def control_version(engine, run_id, evidence, ref, base_unit_id, seed):
    base = next((u for u in evidence['units'] if u['id'] == base_unit_id and u['complete']), None)
    if not base or not evidence['comparisons'].get(base_unit_id, {}).get('comparable'):
        raise ValueError('YAML drafting requires an exact completed paired control')
    params = [evidence['job_parameters'][n] for n in base['jobs'] if evidence['job_parameters'][n]['seed'] == seed]
    if sorted(p['variant'] for p in params) != ['baseline', 'proposed']:
        raise ValueError('Choose one existing paired control seed')
    program = engine.store.get('programs', run_id) or {}
    profiles = {p['variant']: effective_profile(p, program, evidence.get('parameter_capabilities', [])) for p in params}
    return _store(engine, run_id, render({'version': 1, 'profiles': profiles}), evidence['binding'], evidence=ref,
        controls={'base_unit_id': base_unit_id, 'validation_seed': seed},
        allowed={variant: [p for p, _ in leaves(tree)] for variant, tree in profiles.items()})


def revise(engine, run_id, base, args, owner):
    old = base['body']; document = copy.deepcopy(old['document'])
    if args['action'] == 'new_version':
        text = args['yaml']; document = parse(text)
    else:
        operations = args.get('operations', [])
        if not 1 <= len(operations) <= 64: raise ValueError('A YAML revision needs 1..64 explicit edits')
        for op in operations:
            parts = op['path'].split('/')
            if len(parts) < 4 or parts[1] != 'profiles': raise ValueError('Edit only a profile hyperparameter path')
            variant, relative = parts[2], '/' + '/'.join(parts[3:])
            if relative not in old['allowed_paths'].get(variant, []) or not safe_path(relative):
                raise ValueError('Parameter is outside the code-supported profile; request a Codex capability change')
            if op['op'] == 'set':
                assign(document, op['path'], op['value'])
            else:
                target = document
                for part in parts[1:-1]: target = target[part.replace('~1', '/').replace('~0', '~')]
                leaf = parts[-1].replace('~1', '/').replace('~0', '~')
                if leaf not in target: raise ValueError('Cannot remove an absent YAML key')
                del target[leaf]
        text = render(document)
    if set(document['profiles']) != set(old['document']['profiles']):
        raise ValueError('A revision must retain the comparison profile identities')
    for variant, tree in document['profiles'].items():
        if any(path not in old['allowed_paths'][variant] or not safe_path(path) for path, _ in leaves(tree)):
            raise ValueError('New YAML keys require a declared source capability, not an implicit code change')
    if document == old['document']: raise ValueError('YAML revision has no parameter change')
    engine.check_active(run_id)
    return _store(engine, run_id, text, old['binding'], parent=base['id'], owner=owner,
                  evidence=old['source_evidence'], controls=old['controls'], allowed=old['allowed_paths'])


def view(row, offset=0, limit=6000):
    text = row['body']['yaml']; end = min(len(text), offset + limit)
    if offset < 0 or not 1 <= limit <= 6000: raise ValueError('YAML reads require bounded character windows')
    missing = {v: sorted(set(row['body']['allowed_paths'][v]) - {p for p, _ in leaves(tree)})
               for v, tree in row['body']['document']['profiles'].items()}
    return {'revision_id': row['id'], 'sha256': row['body']['yaml_sha256'], 'text': text[offset:end],
            'offset': offset, 'end': end, 'total_chars': len(text), 'next_offset': end if end < len(text) else None,
            'parent': row['body']['parent'], 'missing_parameters': missing, 'execution_authorized': False,
            'note': 'Draft version only. Remove+set replaces a value; removed required parameters must be restored before execution.'}


async def execute(harness, run_id, role, owner, name, args):
    from .tool_authorization import authorize
    authorize(harness, run_id, role, owner, name, args)
    engine = harness.engine
    frozen = engine.store.get('handoff_inputs', owner) or {}
    context = frozen.get('context', {})
    if not context.get('experiment_decision', {}).get('hyperparameter_yaml'):
        raise ValueError('This frozen task has no YAML authoring contract')
    ref = context.get('validation_evidence', {})
    archive = engine.store.get('experiment_evidence', ref.get('id', '')) or {}
    evidence = archive.get('summary')
    if sha(evidence) != ref.get('sha256'):
        raise ValueError('YAML task evidence changed')
    if args.get('revision_id'):
        row = read_version(engine, run_id, args['revision_id'], args.get('expected_sha256'))
    else:
        if name != TOOLS[0]: raise ValueError('YAML edits require an exact base revision')
        row = control_version(engine, run_id, evidence, ref, args['base_unit_id'], args['validation_seed'])
    if row['body']['binding'] != evidence['binding']:
        raise ValueError('YAML version belongs to a different dataset/code/configuration')
    if name == TOOLS[1]:
        if role != 'engineer' or not frozen.get('instruction') or not engine.store.get('handoffs', owner)['purpose'].startswith('experiment_decide:'):
            raise ValueError('Only the current engineer proposal task may revise hyperparameters')
        row = revise(engine, run_id, row, args, owner)
    result = _view_with_schema(row, evidence, args)
    return {**result, 'receipt_sha256': sha({'call': {'name':name,'arguments':args}, 'result':result})}


def _view_with_schema(row, evidence, args):
    schema = evidence.get('hyperparameter_yaml', {}).get('schema')
    result = view(row, args.get('offset', 0), args.get('limit', 6000))
    if schema:
        from jsonschema import Draft202012Validator
        errors = list(Draft202012Validator(schema).iter_errors(row['body']['document']))
        result['schema_validation'] = {'valid': not errors, 'issues': [
            {'path': '/'+'/'.join(map(str,e.absolute_path)), 'constraint':e.validator} for e in errors[:12]],
            'schema_path': '/hyperparameter_yaml/schema'}
    return result


def replay_read(engine, run_id, context, args):
    ref = context['validation_evidence']
    archive = engine.store.get('experiment_evidence', ref['id']) or {}
    evidence = archive.get('summary')
    if sha(evidence) != ref['sha256']: raise ValueError('YAML read evidence changed')
    if args.get('revision_id'):
        row = read_version(engine,run_id,args['revision_id'],args['expected_sha256'])
    else:
        matches=[r for r in engine.store.list(COLLECTION,run_id) if r['body']['source_evidence']==ref
            and not r['body']['parent'] and r['body']['controls']=={
                'base_unit_id':args['base_unit_id'],'validation_seed':args['validation_seed']}]
        if len(matches)!=1:raise ValueError('YAML read has no unique saved control document')
        row=read_version(engine,run_id,matches[0]['id'])
    if row['body']['binding']!=evidence['binding']:raise ValueError('YAML read belongs to another code/dataset')
    return _view_with_schema(row,evidence,args)


def validate_candidate(engine, run_id, spec, evidence, unit):
    row = read_version(engine, run_id, spec['yaml_revision_id'], spec['yaml_sha256'])
    body = row['body']
    if body['binding'] != evidence['binding'] or body['controls'] != {
            'base_unit_id': spec['base_unit_id'], 'validation_seed': spec['validation_seed']}:
        raise ValueError('YAML revision is not bound to the proposed control and code')
    program = engine.store.get('programs', run_id) or {}
    for _, params in unit['jobs']:
        if body['document']['profiles'].get(params['variant']) != effective_profile(params, program, evidence.get('parameter_capabilities', [])):
            raise ValueError('YAML must equal the complete proposed configuration; hidden changes or deletions cannot execute')
    if program.get('hyperparameter_contract'):
        from jsonschema import validate
        validate(body['document'], program['hyperparameter_contract']['schema'])
    return row


def compact_sha(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def prepare_trial(engine, run_id, program, params, *, use_pending=True):
    if program.get('hyperparameter_yaml_protocol') != 1: return None
    from .experiment_decisions import session
    from jsonschema import validate
    contract = program['hyperparameter_contract']
    # Declared defaults exist in the immutable default YAML, not in Python.
    target = effective_profile(params, program)
    state = (session(engine, run_id) or {}) if use_pending else {}
    pending = state.get('pending') or {}
    decision = engine.store.get('experiment_decisions', pending.get('decision_id','')) or {}
    spec = decision.get('proposal', {}).get('parameter_search') or {}
    if spec.get('yaml_revision_id'):
        row = read_version(engine, run_id, spec['yaml_revision_id'], spec['yaml_sha256'])
        if row['body']['document']['profiles'].get(params['variant']) != target:
            raise ValueError('Selected YAML version does not match this admitted trial')
    else:
        from .python_training import code_hash
        binding = {'cycle': engine.cycle(run_id), 'code_sha256': code_hash(program), 'revision': program.get('revision',0),
                   'plan_sha256': sha(engine.experiment_plan(run_id))}
        row = _store(engine,run_id,render({'version':1,'profiles':{params['variant']:target}}),binding,
                     controls={'source':'already_approved_frozen_configuration'},
                     allowed={params['variant']:[p for p,_ in leaves(target)]})
    validate(row['body']['document'],contract['schema'])
    return {'revision_id': row['id'], 'yaml_sha256': row['body']['yaml_sha256'], 'yaml': row['body']['yaml'],
            'profile': params['variant'], 'params_sha256': sha(params),
            'effective_profile_sha256': compact_sha(target)}


def stage_trial(path, contract, params):
    if not contract: return None
    document = parse(contract['yaml'])
    if hashlib.sha256(contract['yaml'].encode()).hexdigest()!=contract['yaml_sha256'] or sha(params)!=contract['params_sha256']:
        raise ValueError('Staged YAML or approved parameter mirror changed')
    if contract['profile']!=params['variant'] or compact_sha(document['profiles'].get(params['variant']))!=contract['effective_profile_sha256']:
        raise ValueError('Staged YAML profile binding changed')
    receipt={k:contract[k] for k in ('revision_id','yaml_sha256','profile','params_sha256','effective_profile_sha256')}
    for name,raw in ((RUNTIME_FILE,contract['yaml'].encode()),(RECEIPT_FILE,json.dumps(receipt,ensure_ascii=False).encode())):
        target=Path(path)/name
        if target.exists() or target.is_symlink():raise ValueError('Runtime YAML control file already exists')
        target.write_bytes(raw)
    return receipt


def check_result(result, contract):
    if not contract:return []
    metadata=result.get('experiment_metadata',{})
    return [message for key,wanted,message in (
        ('hyperparameter_yaml_sha256',contract['yaml_sha256'],'Execution did not attest the admitted YAML bytes'),
        ('effective_hyperparameters_sha256',contract['effective_profile_sha256'],'Execution did not attest the selected effective YAML profile'))
        if metadata.get(key)!=wanted]


GUIDANCE = '''超参数通过 YAML 版本管理。先 hyperparameters.read(base_unit_id,validation_seed) 获取精确对照配置和 SHA；
工程师用 hyperparameters.revise 对 /profiles/<variant>/<参数路径> 执行 set/remove，或 action=new_version 提交完整 YAML。
同一路径 set 替换旧值；remove 后可在同次 operations 写入新值。旧版本永久保留，新版本不自动激活。
拒绝重复键、未知类型、非有限值、数据/种子/评价/预算修改。新源码能力先交 Codex，不能用增加 YAML 键冒充实现。
search_parameters 必须回传 yaml_revision_id、yaml_sha256 并与本次单因素候选完全一致；三方批准后宿主派发，原对照复用。
之后的参数调整只修订 YAML，架构/源码修改继续走代码审核与 Codex 实现。'''
