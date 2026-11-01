"""Keep bounded authenticated decision evidence visible across tool turns."""
from .rapid_context import _valid
from .experiment_workflow import sha
import hashlib
import re


def _yaml_read(context, row):
    call, value = row.get('call', {}), row.get('result', {})
    args = call.get('arguments', {})
    if (call.get('name') not in {'hyperparameters.read', 'hyperparameters.revise'}
            or not context.get('experiment_decision', {}).get('hyperparameter_yaml')
            or value.get('error') or not isinstance(value.get('text'), str)
            or not isinstance(value.get('revision_id'), str)
            or not re.fullmatch('[a-f0-9]{64}', str(value.get('sha256', '')))
            or value.get('receipt_sha256') != sha({'call': call,
                'result': {k: v for k, v in value.items() if k != 'receipt_sha256'}})):
        return False
    start, end, total = (value.get(k) for k in ('offset', 'end', 'total_chars'))
    if (any(type(x) is not int for x in (start, end, total))
            or not 0 <= start < end <= total <= 128 * 1024 or end-start != len(value['text'])):
        return False
    if call['name'] == 'hyperparameters.read' and args.get('revision_id') and (
            args['revision_id'] != value['revision_id'] or args.get('expected_sha256') != value['sha256']):
        return False
    if call['name'] == 'hyperparameters.revise' and value.get('parent') != args.get('revision_id'):
        return False
    return start != 0 or end != total or hashlib.sha256(value['text'].encode()).hexdigest() == value['sha256']


def windows(context, rows):
    purpose = str(context.get('_purpose', '')).split(':', 1)[0]
    planning = purpose in {'dataset_work_order', 'dataset_plan_review'} and context.get('dataset_planning')
    if purpose not in {'experiment_decide', 'experiment_vote'} and not planning:
        return set()
    groups = {}
    for i, row in enumerate(rows):
        yaml_read = _yaml_read(context, row)
        if not yaml_read and not _valid(context, row):
            continue
        call, value = row['call'], row['result']
        args = call['arguments']; path = args.get('path', '')
        if yaml_read:
            kind, identity = 'yaml', (value['revision_id'], value['sha256'])
        elif call['name'] == 'experiments.compare' and (path.startswith('/job_parameters/')
                or path in {'/definitions', '/comparisons', '/units'}
                or path.startswith('/training_investigation/causal_followup/')
                or path == '/yaml_candidate_audit' or path.startswith('/yaml_candidate_audit/')
                or planning and any(path == p or path.startswith(p + '/') for p in (
                    '/task_requirements', '/dataset_binding', '/dataset_configuration', '/source_design',
                    '/dataset_budget', '/configuration_repair', '/previous_delivery', '/proposal'))):
            kind, identity = 'parameters', path
        elif call['name'] == 'experiments.analyze':
            kind, identity = 'statistics', (args.get('kind'), args.get('trial_id'), args.get('unit_id'))
        elif call['name'] == 'experiments.inspect_module' and value.get('source_read'):
            kind, identity = 'source', (path, value.get('source_sha256'))
        elif planning and call['name'] == 'experiments.inspect_module' and args.get('view') in {'symbols', 'index'}:
            kind, identity = 'source_index', (args.get('snapshot', 'current'), path, args['view'])
        else:
            continue
        groups.setdefault((kind, identity), []).append(i)
    kept = set()
    target = context.get('decision_proposal', {}).get('parameter_search', {}).get('yaml_revision_id')
    parent = next((row['result'].get('parent') for row in rows
                   if _yaml_read(context, row) and row['result']['revision_id'] == target), None)
    for kind, budget in [('yaml', 24000), ('parameters', 24000), ('statistics', 16000), ('source', 12000), ('source_index', 6000)]:
        count, used, parents = 0, 0, set()
        ordered = sorted((key for key in groups if key[0] == kind),
            key=lambda key: (not str(key[1]).startswith('/job_parameters/'), str(key[1]).count('/'), str(key[1])))
        if kind == 'yaml':
            ordered.sort(key=lambda key: (key[1][0] not in {target, parent}, -max(groups[key])))
        for key in ordered:
            if kind == 'parameters' and any(str(key[1]).startswith(p + '/') for p in parents):
                continue
            spans = []; candidates = list(groups[key]); chosen = []
            while candidates and count < 10:
                ranked = []
                for index in candidates:
                    value = rows[index]['result']; pieces = [(value['offset'], value['end'])]
                    for lo, hi in spans:
                        pieces = [(a,b) for start,end in pieces
                            for a,b in ((start,min(end,lo)),(max(start,hi),end)) if a < b]
                    gain = sum(b-a for a,b in pieces)
                    if gain and used + len(value['text']) <= budget:
                        ranked.append((gain, index))
                if not ranked:
                    break
                _, index = max(ranked); value = rows[index]['result']
                kept.add(index); chosen.append(index); candidates.remove(index)
                spans.append((value['offset'], value['end']))
                used += len(value['text']); count += 1
            end = 0
            for lo,hi in sorted(spans):
                if lo <= end: end = max(end,hi)
            if kind == 'parameters' and chosen and end == rows[chosen[-1]]['result'].get('total_chars'):
                parents.add(str(key[1]))
    return kept
