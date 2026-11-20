"""Versioned review obligations, with exact original sources available on demand.

The required reading is the complete declared review scope, not an expanded
provenance archive. This never changes the frozen source or scientific rules.
"""
from __future__ import annotations

import copy
import functools
import hashlib
import json

PROTOCOL = 'review_material_protocol'
VERSION = 3
SCOPES = {2: 'metric_runtime_compatibility_v2', 3: 'metric_runtime_compatibility_v3'}
REUSE = 'review_material_read_reuse'


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def enabled(context):
    value=context.get(PROTOCOL)
    if value is not None and (type(value) is not int or value not in SCOPES):
        raise ValueError('Unknown review material packet protocol')
    return value in SCOPES


def supported(context):
    return enabled(context) and context.get('review_scope') == SCOPES[context[PROTOCOL]]


def source(context):
    from .independent_full_review import source_context,MATERIALS
    result=source_context(context)
    if MATERIALS in context:result[MATERIALS]=context[MATERIALS]
    result.pop(REUSE,None)
    return result


def compile_packet(context):
    if not enabled(context):raise ValueError('Review material packet was not explicitly frozen')
    return _compile(encoded(source(context)))


@functools.lru_cache(maxsize=16)
def _compile(original_json):
    from . import experiment_materials as experiments
    from .task_document_projection import compile_view,restore
    from .metric_review_sources import compile_sources
    original=json.loads(original_json)
    from .metric_capability_evidence_recovery import KEY as RECOVERY, focus_packet
    if RECOVERY in original:
        base = {k: v for k, v in original.items() if k != RECOVERY}
        return focus_packet(original, _compile(encoded(base)))
    # The existing typed DAG rejects ambiguous literals, missing/foreign nodes,
    # cycles and incomplete reconstruction before admitting any derived view.
    lossless=compile_view(original)
    if encoded(restore(lossless))!=original_json:
        raise ValueError('Review material source cannot be reconstructed exactly')
    nodes={};labels={};required=set();formats={}
    def add(value,label,*,mandatory=False,plain=False):
        text=value if plain else encoded(value)
        identity=sha(encoded(['text' if plain else 'json',value]))
        if identity in nodes and nodes[identity]!=text:raise ValueError('Material content identity collision')
        nodes[identity]=text;formats[identity]='text' if plain else 'json';labels.setdefault(identity,[]).append(label)
        if mandatory:required.add(identity)
        return ['material',identity]
    # Only this explicit versioned producer adapter defines optional scope.
    # Generic future callers must declare a supported adapter, never guess it.
    if not supported(original):
        raise ValueError('Unsupported full-review material scope')
    current=original['runtime_capability'];previous=original['previous_runtime_capability']
    sources=compile_sources(current,previous)
    root={k:copy.deepcopy(v) for k,v in original.items() if k not in {
        'runtime_capability','previous_runtime_capability','changed_runtime_sources',
        'approved_base_plan','approved_source_design'}}
    root['source_sha256_of_exact_original']=sha(original_json)
    checks = root.get('capability_check_evidence')
    if checks and checks.get('version') == 2:
        for report in checks.get('reports', []):
            for kind, document in report.get('documents', {}).items():
                text = document['text']
                if (document.get('encoding') != 'utf-8'
                        or hashlib.sha256(text.encode('utf-8')).hexdigest() != document.get('sha256')):
                    raise ValueError('Frozen check evidence document bytes changed')
                document['text'] = add(text, 'Check '+kind+' '+document['path'], plain=True)
            for case in report.get('failure_cases', []):
                case['traceback'] = add(case['traceback'],
                    'Check failure '+str(case.get('classname'))+'::'+str(case.get('name')), plain=True)
        checks['reading_policy'] = ('Report, authenticated JUnit, full log and failure tracebacks are frozen '
            'materials. Read their text descriptors with reviews.read_material/material_id; filesystem '
            'paths are provenance, not run artifact IDs. Baseline controls do not establish acceptance.')
    root['approved_source_design']=add(original['approved_source_design'],'Approved complete design',mandatory=True)
    plan=original['approved_base_plan'];plan_view=experiments.compile_view(plan)
    if plan_view is None:
        root['approved_base_plan']=add(plan,'Approved complete plan',mandatory=True)
    else:
        if encoded(experiments.restore(plan_view))!=encoded(plan):raise ValueError('Approved combination table changed')
        protocols=plan_view.pop('protocols')
        root['approved_base_plan_view']=plan_view
        root['protocol_sources']={key:add(body,'Original protocol '+key) for key,body in protocols.items()}
        rebuilt={**plan_view,'protocols':protocols}
        if encoded(experiments.restore(rebuilt))!=encoded(plan):raise ValueError('Projected plan does not preserve every job')
    root['runtime_capability']=[{**{k:v for k,v in row.items() if k!='source'},
        'source':add(row['source'],'Current scope '+str(row.get('path',index)),mandatory=True,plain=True)}
        for index,row in enumerate(sources['required_current_sources'])]
    scope={k:v for k,v in sources.items() if k not in {'required_current_sources','optional_sources','normalized_diff_index'}}
    root['scope_manifest']=add(scope,'Exact source coverage and reconstruction manifest')
    root['normalized_diff_index']=add(sources['normalized_diff_index'],'LF-normalized comparison for positioning (not a substitute for current code)')
    for index,row in enumerate(sources['optional_sources']):
        label=(row['path']+' '+', '.join(row['scope_symbols']) if row.get('scope_symbols') else str(index))
        add(row,'Source appendix '+label)
    for row in current:add(row,'Original current '+row['path'])
    for row in previous:add(row,'Original previous '+row['path'])
    archive = add(original,'Exact original request source (archive; not mandatory repeated reading)')
    if original[PROTOCOL] == 3:
        from .review_code_materials import scoped_root
        required.intersection_update(row['source'][1] for row in root['runtime_capability'])
        root = scoped_root(original, root, archive, add)
    else:
        # Frozen historical formatting only; new work uses the scoped root.
        root['reading_policy']={'version':2,'required':'Root, complete design, shared plan template and all combinations, every current scoped source',
            'on_demand':'Original provenance, prior source versions, complete caller bodies and normalized diff; follow exact references when needed',
            'unchanged_scientific_criteria':True,'read_reuse':'Only this role\'s authenticated exact submitted source bytes; never another role\'s approval'}
    # Descriptors are host-owned protocol tuples, never detected inside user data.
    catalog=[{'id':key,'sha256':sha(text),'encoding':formats[key],'chars':len(text),'labels':labels[key],'required':key in required}
        for key,text in nodes.items()]
    return {'root':encoded(root),'nodes':nodes,'formats':formats,'required':sorted(required),'catalog':catalog,
        'source_sha256':sha(original_json),'lossless_source_sha256':lossless['source_sha256']}


def material(context,material_id=None):
    packet=compile_packet(context)
    if material_id in (None,'root'):return packet['root']
    if material_id not in packet['nodes']:raise ValueError('Unknown frozen review source identity')
    return packet['nodes'][material_id]


def identities(context):
    packet=compile_packet(context)
    return ['root',*packet['nodes']]


def required(context):
    return ['root',*compile_packet(context)['required']]


def index(context,material_id=None):
    from .independent_full_review import KEY,PAGE_LIMIT,READ_LIMIT,validate_binding,TOOL
    binding=validate_binding(context[KEY]);key=material_id or 'root';text=material(context,key)
    result={'policy_sha256':binding['sha256'],'material_sha256':sha(material(context)),
        'material_id':key,'document_id':'independent-full-review:'+binding['sha256']+(':'+key if key!='root' else ''),
        'sha256':sha(text),'section_id':key,'total_chars':len(text),'offset':0,
        'max_chars':PAGE_LIMIT,'batch_max_chars':READ_LIMIT,'read_with':TOOL}
    if key=='root':
        packet=compile_packet(context)
        result.update(protocol=context[PROTOCOL],materials=copy.deepcopy(packet['catalog']),
            required_total_chars=len(packet['root'])+sum(len(packet['nodes'][k]) for k in packet['required']))
    return result
