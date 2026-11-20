"""Versioned authoring of one executable template for a paper's window matrix."""
import copy
import json

from .experiment_workflow import sha
from .fair_comparison import strict_equal, task_signature

GROUPS = 'paper_matrix_protocol_groups'


def ready_to_write(context, progress):
    """Close out repetitive exploration after the author has the actual budget, asset and settings."""
    facts = context.get('dataset_planning',{}).get('matrix_contract',{})
    if not facts:
        return False
    required={'paper','budget','asset','proposed','ablation'}
    observed=set()
    signatures=set()
    repeated=0
    for row in progress.get('tool_results',[]):
        call,value=row.get('call',{}),row.get('result')
        if not isinstance(value,dict) or value.get('error'):
            continue
        name,args=call.get('name'),call.get('arguments',{})
        signature=sha(call)
        if signature in signatures:
            repeated+=1
        signatures.add(signature)
        if name=='experiments.compare' and args.get('expected_sha256')==context['evidence_sha256']:
            path=args.get('path','')
            if path=='/paper' and value.get('text'):
                observed.add('paper')
            if path.startswith('/prior_profiles/proposed_params') and value.get('text'):
                observed.add('proposed')
            if path.startswith('/prior_profiles/ablation_params') and value.get('text'):
                observed.add('ablation')
        if name=='datasets.inspect' and any(args.get('asset_id')==b['asset_id'] and
                args.get('expected_sha256')==b['manifest_sha256'] for b in facts['dataset_assets']):
            observed.add('asset')
        if name=='resources.assess' and value.get('approved') and value.get('trial_accounting')==facts['execution_constraints']['trial_accounting']:
            observed.add('budget')
    return required <= observed and repeated >= 2


def writer_material(engine,rid,context):
    ref=context['validation_evidence']
    record=engine.store.get('experiment_evidence',ref['id']) or {}
    evidence=record.get('summary',{})
    facts=context['dataset_planning']['matrix_contract']
    if record.get('run_id')!=rid or sha(evidence)!=ref['sha256'] or evidence.get('matrix_contract')!=facts:
        raise ValueError('Matrix template evidence changed before delivery')
    sources=evidence['code_sources']
    archive=engine.store.get('experiment_sources',sources['id']) or {}
    if archive.get('run_id')!=rid or sha(archive.get('files'))!=sources['sha256'] or sources!=facts['prior_model_sources']:
        raise ValueError('Matrix template source changed before delivery')
    modules={}
    for name in ('src/models/fredn.py','src/models/clock.py','src/models/layers.py','dataset_runtime/adapter.py'):
        item=archive['files'].get(name,{})
        if item.get('text'):
            modules[name]={'sha256':item['sha256'],'text':item['text'][:6500],
                           'end':min(6500,len(item['text'])),'total':len(item['text'])}
    profiles=evidence['prior_profiles']
    profiles_view={}
    for role in ('proposed','ablation'):
        p=profiles[role+'_params']
        profiles_view[role]={k:p[k] for k in ('algorithm','common','loss','clock','fredn','runtime','comparison_task') if k in p}
        # The same data object is present in comparison_task. Keep it once.
        if 'comparison_task' in profiles_view[role]:
            profiles_view[role]['comparison_task_paths']={r['name']:r['paths'] for r in profiles_view[role].pop('comparison_task')['dimensions']}
    return {'prior_profiles':profiles_view,'prior_modules':modules,'prior_seed_lists':{k:profiles[k] for k in ('validation_seeds','final_test_seeds')},
            'instruction':'这些是已核验的旧对照输入与源码，用于设计新模板。按 matrix_contract 改为新的动态 L/H；不要原样复制旧窗口或授权。'}


def _source(engine, rid, body, notes=None):
    from . import experiment_protocols as protocols
    from .paper_baselines import read
    paper = read(engine.store,rid,body['paper_reference'])
    notes = protocols._notes(engine,rid) if notes is None else notes
    fields = {}
    settings = set()
    for ref in body['parents']:
        if not ref['id'].startswith(rid+':experiment-protocol:'):
            raise ValueError('A protocol group requires original literature registration receipts')
        row = protocols.checked(engine,rid,ref['id'],ref['sha256'],notes=notes)
        if any(row[k] != paper[k] for k in ('paper_id','source_id','text_sha256')) or row['dataset'] != paper['protocol']['dataset']:
            raise ValueError('Matrix sources belong to different papers, source versions or datasets')
        settings.add(row['setting'])
        for p in row['parameters']:
            old = fields.setdefault(p['name'],p)
            if old['status'] != p['status'] or not strict_equal(old['values'],p['values']):
                raise ValueError('Conflicting source field: '+p['name'])
    if len(settings) != 1:
        raise ValueError('Matrix sources describe different experimental settings')
    for name, key in [('input_length','input_lengths'),('output_length','output_lengths')]:
        field = fields.get(name,{})
        if field.get('status') != 'reported' or not strict_equal(field['values'],paper['protocol'][key]):
            raise ValueError('Source window inventory differs from reviewed paper: '+name)
    return {'id':rid+':paper-matrix-protocol:'+sha(body),'protocol_sha256':sha(body),'run_id':rid,
            'kind':'paper_matrix_protocol','dataset':paper['protocol']['dataset'],'setting':next(iter(settings)),
            'parameters':list(fields.values()),'parent_protocols':body['parents'],
            **{k:paper[k] for k in ('paper_id','source_id','text_sha256')}}


def checked(engine, rid, key, expected, notes=None):
    row = engine.store.get(GROUPS,key) or {}
    body = row.get('body')
    if not body or row.get('run_id')!=rid or sha(body)!=expected or key!=rid+':paper-matrix-protocol:'+expected:
        raise ValueError('Invalid paper matrix source binding')
    return _source(engine,rid,body,notes)


def _basis(engine,rid,transition):
    from .dataset_transition import current, _archived
    from .code_bundle import validate
    old_program = _archived(current(engine.store,rid),'programs')
    prior = engine.store.get('dataset_experiment_setups',old_program.get('dataset_runtime_id','')) or {}
    if prior.get('run_id')!=rid or prior.get('program')!=old_program or prior.get('status')!='ready':
        raise ValueError('No verified previous dataset implementation for the scoped design')
    bundle = prior['candidate']['bundle']
    validate(bundle,old_program['code'])
    config=json.loads(prior['draft']['configuration_json'])['datasets'][transition['datasets'][0]['target']['dataset']]
    refs=config['proposed_params']['protocol_binding']['protocols']
    body={'run_id':rid,'paper_reference':transition['paper_reference'],'parents':refs}
    protocol=_source(engine,rid,body)
    key=protocol['id']
    record={'id':key,'run_id':rid,'body':body}
    if engine.store.mutate(GROUPS,key,lambda old:old or record)!=record:
        raise ValueError('Conflicting paper matrix source group')
    # The current program's entry SHA belongs to its candidate bundle. Its
    # inherited artifact_id can point to a different, older implementation.
    manifests = prior['candidate']['code_sources']
    source = engine.store.get('experiment_sources',manifests['id'])
    if source.get('run_id')!=rid or sha(source['files'])!=manifests['sha256']:
        raise ValueError('Previous dataset runtime source archive changed')
    current_files = {f['path']:f['sha256'] for f in bundle['files']}
    if any(current_files.get(p)!=f['sha256'] for p,f in source['files'].items()):
        raise ValueError('Archived source file differs from the verified runtime manifest')
    return protocol, config, manifests, bundle['sha256']


def expand(draft, packet, source):
    from .io_matrix import instantiate
    template=copy.deepcopy(draft)
    paper=packet['paper_benchmark']['protocol']
    data,=packet['datasets']
    if template['dataset'] != data['target']['dataset']:
        raise ValueError('Template dataset must equal the bound dataset name')
    # Funding a new paper baseline starts no baseline job. Keep a metadata-only
    # descriptor for existing fair-comparison validators.
    proposed=template['proposed_params']
    if template['baseline_params']:
        raise ValueError('Set baseline_params to {}; the host supplies the paper task descriptor')
    comparison=proposed['comparison_task']
    values={d['name']:copy.deepcopy(d['value']) for d in comparison['dimensions']}
    if set(values) & {'dataset_assets','comparison_task'}:
        raise ValueError('Reserved field in comparison dimensions')
    template['baseline_params']={**values,'dataset_assets':data['dataset_assets'],'comparison_task':{'version':1,
        'dimensions':[{'name':k,'value':v,'paths':['/'+k]} for k,v in values.items()]}}
    for role in ('baseline','proposed','ablation'):
        p=template[role+'_params']
        if p.get('dataset_assets')!=data['dataset_assets']:
            raise ValueError(role+'_params/dataset_assets differs from the verified source')
        if not {'dataset','input_length','output_length'}<=task_signature(p).keys():
            raise ValueError(role+' lacks declared dataset and input/output pointers')
    tasks = [instantiate(template, {'task_id': f'l{length}-h{horizon}', 'dataset': template['dataset'],
             'input_length': length, 'output_length': horizon, 'protocol_id': source['id'],
             'protocol_sha256': source['protocol_sha256']}, {'input_length': length, 'output_length': horizon})
             for horizon in paper['output_lengths'] for length in paper['input_lengths']]
    design={**template,'benchmark_tasks':tasks,'baseline_mode':'paper_reported',
            'paper_reference':packet['paper_reference'],'paper_selection_axis':'input_length'}
    return design


async def author(engine,harness,rid,common,schema):
    import asyncio
    from . import dataset_planning
    from .dataset_configuration_queue import budget,trial_limit
    from .dataset_transition import validate_design
    from .engine import ResearchBlocked
    from .execution_contract import constraints
    from .experiment_plan import build_plan
    from .research_iteration import effective_config
    packet=common['dataset_transition']
    source,profiles,manifest,bundle_sha=await asyncio.to_thread(_basis,engine,rid,packet)
    cfg=effective_config(engine.store,rid,engine.store.get('runs',rid)['config'])
    limit=trial_limit(engine.store,rid,cfg['max_trials'])
    policy=constraints(cfg,dataset_budget=budget(engine.store,rid))
    paper=packet['paper_benchmark']['protocol']
    assets=packet['datasets'][0]['dataset_assets']
    hw=common.get('hardware') or {}
    installed=(hw.get('software') or {}).get('installed_distributions',{})
    facts={'dataset':source['dataset'],'dataset_assets':assets,'paper_reference':packet['paper_reference'],
           'source_group':{'id':source['id'],'sha256':source['protocol_sha256'],'parents':source['parent_protocols']},
           'source_fields':{p['name']:p['values'] for p in source['parameters'] if p['status']=='reported'},
           'input_lengths':paper['input_lengths'],'output_lengths':paper['output_lengths'],
           'review_id':packet['adaptation_review']['id'],'trial_budget':limit,'execution_constraints':policy,
           'root_bundle_sha256':bundle_sha,'prior_model_sources':manifest,
           'prior_common_parameter_paths':list(profiles['proposed_params'].get('common',{})),
           'experiment_interpreter_packages':{k:v for k,v in installed.items() if k.lower() in ('numpy','torch','scipy','pandas','pyyaml')},
           'prior_resource_plan':profiles['proposed_params'].get('resource_plan'),
           'method':{k:v for k,v in (common.get('proposal') or {}).items() if k in ('title','hypothesis','method')},
           'task_requirements':common['objective'],'operator_instruction':packet['operator_instruction']}
    outcomes=len(paper['output_lengths'])
    n=len(paper['input_lengths'])*outcomes
    facts['illustrative_single_seed_count']={'proposed':n,'one_own_ablation_per_condition':n,
        'heldout_after_validation_selection':outcomes,'baseline':0,'total':2*n+outcomes}
    if limit is not None and limit < n+outcomes:
        raise ResearchBlocked(f'有效实验预算 {limit} 无法覆盖至少 {n} 个自有验证和 {outcomes} 个独立测试。')
    schema=copy.deepcopy(schema)
    schema['properties'].pop('benchmark_tasks',None)
    schema['properties']['dataset']={'const':source['dataset']}
    schema['properties']['baseline_params']={'type':'object','maxProperties':0}
    for k in ('validation_seeds','final_test_seeds','ablation_search_space'):
        if k not in schema['required']: schema['required'].append(k)
    # One compact, version-bound handoff; all source, budget and author input
    # facts are frozen independently of any failed older 'design' decision.
    scope='dataset_work_order:paper-design:'+sha(facts)
    ev={'binding':{'run_id':rid,'cycle':engine.cycle(rid),'bundle_sha256':bundle_sha,'paper_reference':packet['paper_reference']},
        'code_sources':manifest,'matrix_contract':facts,'prior_profiles':profiles,'paper':packet['paper_benchmark']}
    ctx=dataset_planning._context(engine,rid,scope,ev,'work_order')
    ctx['dataset_planning'].update(matrix_contract=facts,read_paths=['/matrix_contract','/prior_profiles','/paper'])
    instruction=('定向修正论文窗口设计。提交一个完整设计模板。宿主按 source_group 自动展开所有 L×H 并逐项编译和提交架构评审。'
        'trial_budget=null 表示此任务已有不限尝试次数授权，单次资源、调用额度和独立评审仍生效。'
        'baseline_params={}，外部基线无训练或测试任务。数据身份与说明分开，按本轮返回 Schema 填写。为首个 L/H 提交 proposed_params、'
        'ablation_params 和显式 ablation_search_space。validation_seeds 和 final_test_seeds 按已审本地种子设置填写（见 /prior_profiles），'
        '默认各一枚固定种子；多种子须有本轮明确要求。'
        '各自有分支须包含确切 dataset_assets，comparison_task={version:1,dimensions:[{name,value,paths}]}。'
        '至少声明 dataset、input_length、output_length，可选 channels；每个 paths 指向本分支实际存在的 JSON 字段，'
        '例如 /dataset、/common/L、/common/H、/common/C。只声明真实源字段。宿主只替换已声明的窗口路径；'
        '模型和适配应从 L/H 动态推导 FFT、头部、投影和输出形状，勿保留固定128/32形状或旧项目协议授权。'
        '数据处理及指标舍取归入配置和 limitations 并交评审；论文未知项不得伪造。search_space=[{}] 表示固定初始配置。'
        '用自有验证选择每个H的L，保留CPU检查和留出隔离。读取源码用 experiments.inspect_module，'
        'evidence_id/expected_sha256 取 validation_evidence，snapshot=current。先读 /prior_profiles 的必要叶子。'
        '不要把实现入口摘要当成旧 artifact 文件的摘要。资源声明用实际可用的包名，不能沿用未验证的依赖版本。'
        '模型的输入投影、频谱和时钟宽度按当前窗口推导。跨窗口不变的内部因素显式给出。'
        '请只声明需要替换的窗口位置，基线只计论文，最终测试只运行每个H的自有胜出者。')
    record_id=rid+':paper-design:'+sha(facts)
    row=engine.store.get('paper_matrix_designs',record_id) or {'id':record_id,'run_id':rid,'assignments':[]}
    if not row['assignments']:
        from .dataset_contract import bind_request
        ctx['dataset_selection'] = {'id': packet['adaptation_review']['id'],
            'sha256': sha(packet['datasets']), 'datasets': packet['datasets']}
        instruction, ctx, schema = bind_request(instruction, ctx, schema, template=True)
        row['assignments'].append({'purpose':scope,'instruction':instruction,'context':ctx,'schema':schema})
        engine.store.put('paper_matrix_designs',record_id,row)
    for i in range(3):
        task=row['assignments'][i]
        reply=await harness.ask(rid,'engineer',**task)
        try:
            from .dataset_contract import materialize as resolve_datasets
            normalized = resolve_datasets(reply, task['context'].get('dataset_catalog'), template=True)
            result=expand(normalized,packet,source)
            validate_design(engine,rid,result,packet)
            plan=build_plan(result,limit,protocols={source['id']:source})
        except (ValueError,KeyError,TypeError) as exc:
            error=f'{type(exc).__name__}: {exc}'
            if i+1 == len(row['assignments']):
                if i==2:
                    raise ResearchBlocked('同一论文矩阵模板已保留定向补正回执，仍有字段待修复：'+error) from exc
                follow=copy.deepcopy(row['assignments'][0])
                follow['purpose']+=f':correct-{i+1}:'+sha(reply)
                follow['context']=copy.deepcopy(row['assignments'][0]['context']
                    if row['assignments'][0]['context'].get('dataset_catalog') else ctx)
                follow['context']['dataset_planning']['template_repair']={'previous_template':reply,'issue':error}
                follow['instruction']+='\n保留已有交付，只修正 template_repair.issue，返回完整模板。'
                row['assignments'].append(follow)
                engine.store.put('paper_matrix_designs',record_id,row)
            continue
        receipt={'template_sha256':sha(reply),'design_sha256':sha(result),'plan_sha256':sha(plan),
                 'tasks':len(result['benchmark_tasks']),'planned_trials':plan['planned_trials'],
                 'final_trials':plan['final_pairs'],'execution_authorized':False}
        if row.get('receipt',receipt)!=receipt:
            raise ValueError('Replayed paper design changed')
        if 'receipt' not in row:
            row.update(receipt=receipt, status='compiled')
            engine.store.put('paper_matrix_designs',record_id,row)
            engine.store.event(rid,'design.paper_matrix_compiled','论文候选矩阵已实际编译，进入独立设计评审。',receipt)
        return result
