"""Resource semantics for an ordinary, independently reviewed diagnostic."""
from .experiment_workflow import sha

FIELD = 'diagnostic_disk_control'
REQUESTS = 'diagnostic_resource_requests'
MODES = ['sampled_workspace', 'continuous_quota']
GUIDANCE = ('普通 CPU 插桩或维修请求的 schema 提供 diagnostic_disk_control 时，写清所需磁盘控制方式。普通本机和容器工作目录使用 '
            'sampled_workspace，定期检查并在发现超限时终止；continuous_quota 要求全程硬配额。'
            '研究所需方式与宿主能力不同时保留该缺口。每份新方案独立投票，批准旧方案不授权更改限制。')


def ordinary_cpu(context, evidence):
    task = (evidence.get('parameter_resolution') or {}).get('next_task') or {}
    return bool(context['experiment_decision'].get('cpu_behavior_check')) and task.get('kind') != 'diagnose'


def bind_schema(schema, *, measured=False):
    # Match the same action and dispositions used by scoped_repair at dispatch.
    condition = {'properties': {'action': {'const': 'diagnose'}, 'unit_id': {'type': 'null'},
        'training_response': {'type': 'object', 'properties': {'disposition': {'enum': ['instrument', 'repair_code']}},
                              'required': ['disposition']}}, 'required': ['action', 'unit_id', 'training_response']}
    schema['properties'][FIELD] = {'enum': MODES}
    schema.setdefault('allOf', []).append({'if': condition, 'then': {'required': [FIELD]},
                                         'else': {'properties': {FIELD: False}}})
    if measured:
        from .diagnostic_observations import FIELD as OBSERVATIONS, NAME_PATTERN
        schema['properties']['diagnostic_write_control'] = {'enum': ['trusted_process', 'no_process_writes']}
        schema['allOf'].append({'if': condition, 'then': {'required': ['diagnostic_write_control']},
                                'else': {'properties': {'diagnostic_write_control': False}}})
        instrument = {'allOf': [condition, {'properties': {'training_response': {'properties': {
            'disposition': {'const': 'instrument'}}}}}]}
        schema['properties'][OBSERVATIONS] = {'type': 'array', 'minItems': 1, 'maxItems': 256, 'uniqueItems': True,
                                              'items': {'type': 'string', 'pattern': NAME_PATTERN}}
        schema['allOf'].append({'if': instrument, 'then': {'required': [OBSERVATIONS]},
                                'else': {'properties': {OBSERVATIONS: False}}})
    return schema


def _identity(record):
    c = record['candidate']
    return {'run_id': record['run_id'], 'cycle': record['cycle'], 'revision_id': record['id'],
            'decision_sha256': record['decision_sha256'], 'amendment_sha256': c['amendment_sha256'],
            'checks_sha256': c['checks_sha256'], 'check_bundle_sha256': c['check_bundle']['sha256']}


def disk_control(host):
    return 'sampled_workspace' if host.get('execution_mode') in ('local', 'docker') else 'unavailable'


def register(engine, run_id, instruction, required_disk_control, expected_checks_sha256):
    from . import tuning_diagnostics
    from .experiment_decisions import revision_context
    from .rapid_experiment_revision import COLLECTION, _candidate_unchanged
    from .training_investigation import scoped_repair
    run = engine.store.get('runs', run_id)
    cursor = tuning_diagnostics.current(engine, run_id) or {}
    if (run['status'] not in {'paused', 'blocked'} or run['stage'] != cursor.get('stage', 'tuning') or cursor.get('status') != 'running'
            or cursor.get('run_id') != run_id or cursor.get('cycle') != engine.cycle(run_id)):
        raise ValueError('Resource reconsideration needs the stopped, current diagnostic')
    decision = revision_context(engine, run_id, cursor['review'])
    if (not decision or decision['decision_id'] != cursor['decision_id'] or not scoped_repair(decision['proposal'])
            or required_disk_control not in MODES or not isinstance(instruction, str) or not 0 < len(instruction.strip()) <= 4000):
        raise ValueError('Resource reconsideration requires the approved diagnostic and explicit operator instruction')
    key = run_id + ':rapid:' + sha(cursor['decision_id'])
    record = engine.store.get(COLLECTION, key) or {}
    if (record.get('status') not in {'reviewing', 'checking'} or record.get('run_id') != run_id
            or record.get('cycle') != cursor['cycle'] or record.get('decision_id') != cursor['decision_id']
            or record.get('decision') != decision['proposal'] or record.get('decision_sha256') != sha(decision['proposal'])
            or record.get('check') or not (record.get('candidate') or {}).get('diagnostic_execution')):
        raise ValueError('No unexecuted diagnostic candidate for the active work order')
    c = record['candidate']
    if (expected_checks_sha256 != c['checks_sha256'] or required_disk_control == disk_control(c['diagnostic_execution'])
            or record['decision'].get(FIELD, required_disk_control) != required_disk_control):
        raise ValueError('Specify the unchanged candidate and its unmet original disk requirement')
    trial = 'rapid-check-' + sha({'amendment': c['amendment_sha256'], 'checks': c['checks_sha256']})[:24]
    if engine.store.get('experiments', run_id + ':' + trial):
        raise ValueError('An existing diagnostic attempt cannot be replaced by resource reconsideration')
    _candidate_unchanged(record)
    body = {'source': _identity(record), 'operator_instruction': instruction.strip(),
            'required_disk_control': required_disk_control, 'original_proposal': record['decision'],
            'host_execution': c['diagnostic_execution'], 'execution_authorized': False, 'approval_inherited': False}
    request = {'id': key, 'run_id': run_id, 'body': body, 'sha256': sha(body)}
    def preserve(old):
        if old and old != request:
            raise ValueError('A different resource request is already bound to this candidate')
        return request
    return engine.store.mutate(REQUESTS, key, preserve)


def request_for(store, record):
    request = store.get(REQUESTS, record['id'])
    if request and (sha(request['body']) != request['sha256'] or request['body']['source'] != _identity(record)
                    or request['body']['host_execution'] != record['candidate']['diagnostic_execution']):
        raise ValueError('Diagnostic resource request belongs to a different candidate or execution contract')
    return request


def admission(store, record):
    request = request_for(store, record)
    required = request['body']['required_disk_control'] if request else record['decision'].get(FIELD)
    if not required:
        return None
    host = record['candidate'].get('diagnostic_execution', {})
    mode = host.get('execution_mode')
    actual = disk_control(host)
    writes = record['decision'].get('diagnostic_write_control')
    if request or required != actual or writes == 'no_process_writes':
        return {'required_disk_control': required, 'available_disk_control': actual,
                'required_write_control': writes, 'available_write_control': 'trusted_process',
                'execution_mode': mode, 'execution_authorized': False}
