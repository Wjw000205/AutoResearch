"""Codex executes an already reviewed migration work order in an isolated copy."""
import copy
import hashlib
import json
from pathlib import Path

from .experiment_workflow import sha
from .storage import now, uid
from .delivery_files import io_path


async def build(engine, harness, run_id, record):
    from .rapid_experiment_revision import _bundle, _write_files, _base_unchanged, build_candidate, COLLECTION
    from .code_bundle import relative_file
    from .engine import ResearchBlocked
    bundle = _bundle(engine, run_id, record['base_program'])
    if not bundle: raise ResearchBlocked('Codex migration requires the exact saved modular source bundle')
    base = {r['path']: io_path(Path(bundle['root'])/r['path']).read_bytes() for r in bundle['files']}
    job = record.get('codex_work_item')
    if not job:
        from .scoped_work_order_contract import contract
        work_kind, work_contract = contract(record)
        if work_kind == 'architecture_experiment':
            from .architecture_experiment_contract import validate as validate_architecture
            from .source_read_identity import source_files
            from .experiment_decisions import _pointer
            material = engine.store.get('experiment_evidence', record['evidence']['id'])['summary']
            validate_architecture(record['decision'], material, _pointer,
                source_files=source_files(engine.store, run_id, {
                    'validation_evidence': record['evidence'], 'evidence_sha256': record['evidence']['sha256']}))
        from .candidate_repair_source import prepare, tests_only, KEY as REPAIR_SOURCE
        try:
            inherited = prepare(engine, run_id, record, base)
        except ValueError as exc:
            # No code was dispatched. Keep this work item pending instead of
            # manufacturing an invalid candidate as the next repair's parent.
            raise ResearchBlocked('Previous candidate delivery requires reconciliation: ' + str(exc)) from exc
        input_runtime, input_support = inherited if inherited else (base, {})
        record['native_delivery_protocol'] = 2
        from .research_anchor import freeze as freeze_anchor, read as read_anchor
        anchor = record.get('research_anchor') or freeze_anchor(engine, run_id)
        if anchor:
            record['research_anchor'] = anchor
        workspace = engine.run_dir(run_id).resolve() / 'codex-amendments' / sha(record['id'])[:20] / 'workspace'
        input_bundle = _write_files(workspace, {**input_runtime, **input_support})
        _write_files(workspace / '_control', base)
        instruction = ('按 work_order.json 中三方已批准的修改单修改本工作目录文件。先读原源码和控制配置。'
            '这是当前模型的有界修改，不重新立项。\n' + work_contract +
            '\n交付 _delivery/changes.json 数组：每个修改或新增运行文件写 {path,change_index}，对应已审 changes 索引。'
            '另交 _delivery/checks.py 和 _delivery/rationale.txt；checks.py 是由宿主运行的 CPU 行为断言。'
            '必须包含实际 Python assert，在当前进程执行并正常返回；不得自启动子进程或 SystemExit 跳过宿主回执。'
            '不得删除旧运行文件、改写 work_order.json、修改工作目录外内容或自行运行正式训练。'
            '可以做语法检查；候选需要再次独立代码审查和 CPU 验证，完成文字不能代替磁盘产物。')
        instruction += ('\n交付协议v2：_control/是原生效控制版本，只读，不列入changes.json；'
            '运行文件与_delivery/测试辅助文件分别收集。changes.json只需列本次涉及的文件及change_index，'
            '允许列_delivery/checks.py及其.py/.json辅助文件；这些文件不会安装到运行模型中。'
            '所有检查依赖均须实际交付，供独立源码审阅和宿主CPU执行。')
        instruction += ('\nchanges.json 必须覆盖每一个已批准 changes 索引，包括交付分类、摘要和测试映射项。'
            '元数据项可登记其实际交付的 _delivery/verification_map.json 等辅助文件，'
            '不能因没有运行源码差异而漏记；不得为凑覆盖虚构修改。')
        if inherited:
            instruction += ('\n当前目录已装入上一轮未通过的完整候选及检查文件，不从控制版重做迁移。'
                'repair_source绑定本次维修来源，_control仍是默认等价对照。保留已交付实现，'
                '只修本次批准的异议；继承文件不继承批准，修后重新独立审核。')
            instruction += ('已实现且无需修改的运行文件，也可在changes.json列原路径及本次change_index，'
                '表示保留并重新核验；宿主会标记为继承实现，不必为制造差异重写代码。')
            if tests_only(record):
                instruction += '\n本次修改单仅涉及检查文件：全部运行源码和配置只读，只修_delivery/；不可重写模型、加载器或优化器。'
        work = {'decision': record['decision'], 'evidence': record['evidence'], 'binding': record['binding'],
                'base_manifest': [{k:v for k,v in f.items()} for f in input_bundle['files']
                                  if not f['path'].startswith('_delivery/')],
                'control_manifest': bundle['files'], 'control_root': '_control',
                'repair_source': record.get(REPAIR_SOURCE), 'approval_inherited': False,
                'work_kind': work_kind, 'source_contract_version': 1,
                'yaml_contract': work_contract,
                'cpu_check_input': 'sys.argv[1] is the host code_check envelope, not a training config; use separate isolated fixtures'}
        from .rapid_check_budget import future_policy
        check_policy = future_policy(engine, run_id, record)
        if check_policy:
            work['cpu_check_policy'] = check_policy
            instruction += ('\nCPU 验收采用 work_order.cpu_check_policy 中已保存的任务预算，实际以宿主任务上限约束；'
                '该契约会绑定检查入口并重新交三方审核，不因预算扩大而省略行为断言。')
        from .hyperparameter_yaml import profile, render
        evidence = engine.store.get('experiment_evidence', record['evidence']['id'])['summary']
        profiles = {}
        for params in evidence['job_parameters'].values():
            if params.get('variant') in ('baseline','proposed','ablation'):
                profiles.setdefault(params['variant'], profile(params, evidence.get('parameter_capabilities', [])))
        work['initial_yaml'] = render({'version':1,'profiles':profiles})
        work['instruction_note'] = ('Start from the existing hyperparameter controls; keep task/data/seed/host-resource fields outside YAML. '
            + ('Only the approved target mechanism may change behavior; preserve untouched controls.'
               if work_kind == 'architecture_experiment' else 'New supported defaults must preserve original behavior.'))
        if anchor:
            work['research_anchor'] = read_anchor(engine, run_id, anchor)
            instruction += ('\nresearch_anchor保留科学目标及反证；本工作项仅落实已批准修改单，不扩大范围或宣告科研成功。'
                '原目标中的预算和停止条件是历史记录，当前权限以已批准工作单和宿主门禁为准；历史负结果不否决本次工程维修，也不因工程完成而关闭。')
        path = io_path(workspace/'work_order.json'); path.write_text(json.dumps(work,ensure_ascii=False,indent=2),encoding='utf-8')
        job = {'operation_id': uid(), 'workspace': str(workspace), 'instruction': instruction,
               'input_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'created_at': now()}
        record['codex_work_item'] = job
        engine.store.put(COLLECTION, record['id'], record)
    workspace = Path(job['workspace'])
    if hashlib.sha256(io_path(workspace/'work_order.json').read_bytes()).hexdigest() != job['input_sha256']:
        raise ResearchBlocked('Codex migration work order changed')
    def save():
        record['updated_at'] = now()
        engine.store.put(COLLECTION, record['id'], record)
    def verify():
        if hashlib.sha256(io_path(workspace/'work_order.json').read_bytes()).hexdigest() != job['input_sha256']:
            raise ResearchBlocked('Codex migration work order changed')
        _base_unchanged(engine, run_id, record)
        if record.get('native_delivery_protocol') == 2:
            from .candidate_repair_source import read_file, read as read_repair, tests_only, KEY as REPAIR_SOURCE
            for name, raw in base.items():
                if read_file(workspace, '_control/' + name) != raw:
                    raise ResearchBlocked('Codex repair changed its original control source')
            if record.get(REPAIR_SOURCE) and tests_only(record):
                runtime, _ = read_repair(engine, run_id, record)
                if any(read_file(workspace, n) != raw for n,raw in runtime.items()):
                    raise ResearchBlocked('Test-only Codex repair changed candidate runtime')
    from .scoped_codex_recovery import run as run_work_item
    await run_work_item(engine, harness, run_id, job, save, verify, label='Codex 迁移任务')
    _base_unchanged(engine,run_id,record)
    def raw_file(name):
        from .candidate_repair_source import read_file
        return read_file(workspace, name)
    def read(name):
        return raw_file(name).decode('utf-8')
    # Complete the delivery mapping before collecting a candidate or repairing checks.
    # The original native workspace and checkpoint remain immutable.
    delivery_context = {}
    if record.get('native_delivery_protocol') == 2:
        from .candidate_repair_source import read as read_repair, support_files, KEY as REPAIR_SOURCE
        parent_runtime, _ = read_repair(engine, run_id, record) if record.get(REPAIR_SOURCE) else (base, {})
        from .scoped_delivery_manifest import entries, repair as repair_manifest
        initial = entries(raw_file('_delivery/changes.json'), len(record['decision']['changes']))
        delivered = {n: raw_file(n) for n in {*parent_runtime, *(r['path'] for r in initial),
            *support_files(workspace), '_delivery/checks.py', '_delivery/rationale.txt',
            '_delivery/changes.json', 'work_order.json'}}
        delivered.update({'_control/' + n: raw_file('_control/' + n) for n in base})
        delivered = await repair_manifest(engine, harness, run_id, record, delivered)
        original_raw_file = raw_file
        def raw_file(name):
            return delivered[name] if name in delivered else original_raw_file(name)
        delivery_context = {n: raw_file(n).decode('utf-8') for n in
            ('work_order.json', '_delivery/changes.json', '_delivery/rationale.txt')}
    changes=json.loads(read('_delivery/changes.json'))
    if not isinstance(changes,list) or not changes or len(changes)>128:raise ValueError('Codex delivery needs a bounded change manifest')
    declared={r['path'] for r in changes}
    if len(declared)!=len(changes):raise ValueError('Codex change manifest contains duplicate files')
    modern = record.get('native_delivery_protocol') == 2
    from .candidate_repair_source import read as read_repair, support_files, KEY as REPAIR_SOURCE
    parent_runtime, _ = read_repair(engine, run_id, record) if record.get(REPAIR_SOURCE) else (base, {})
    for name,raw in parent_runtime.items():
        if raw_file(name)!=raw and name not in declared:raise ValueError('Undeclared source change: '+name)
    edits=[];new=[];retained=[]
    by_path = {item['path']: item for item in changes}
    runtime_names = set(parent_runtime) | {n for n in declared if not n.startswith('_delivery/')}
    if any(n == 'work_order.json' or n.startswith('_control/') for n in runtime_names):
        raise ValueError('Delivery cannot alter host control inputs')
    check_indices = sorted({x['change_index'] for x in changes if x['path'].startswith('_delivery/')}) if modern else []
    for name in sorted(runtime_names if modern else declared):
        item = by_path.get(name)
        raw = raw_file(name)
        inherited_file = (record.get(REPAIR_SOURCE) and name in parent_runtime
                          and raw == parent_runtime[name])
        if inherited_file and item is not None:
            retained.append({'path': name, 'change_index': item['change_index'],
                             'sha256': hashlib.sha256(raw).hexdigest()})
        index = None if inherited_file else item['change_index'] if item else None
        if name in base and raw == base[name]:
            continue
        if index is None and not inherited_file:
            raise ValueError('Runtime change has no approved change index: ' + name)
        item = {'path': name, 'change_index': index}
        name=item['path'];text=read(name)
        if name.startswith('_delivery/') or name=='work_order.json':raise ValueError('Delivery metadata is not a runtime source')
        if name in base:
            old=base[name].decode('utf-8')
            if text!=old:edits.append({'path':name,'before_sha256':hashlib.sha256(base[name]).hexdigest(),
                'change_index':item['change_index'],'old':old,'new':text})
        else:new.append({'path':name,'change_index':item['change_index'],'source':text})
    patch={'evidence_sha256':record['evidence']['sha256'],'decision_sha256':record['decision_sha256'],
           'summary':read('_delivery/rationale.txt'),'edits':edits,'new_files':new,
           'checks':read('_delivery/checks.py'),'check_rationale':(
               'Approved structural intervention; actual CPU mechanism behavior and untouched-control checks required'
               if record['decision'].get('architecture_review') else
               'Agent-approved work order; actual Codex disk delivery; CPU effects and default equivalence required')}
    if modern:
        verify()
        assets = {name: raw_file(name) for name in support_files(workspace)}
        patch.update(check_files=[{'path':name, 'source':raw.decode('utf-8'),
            'change_index': by_path.get(name, {}).get('change_index')} for name,raw in sorted(assets.items())],
            check_change_indices=check_indices,
            retained_runtime=retained,
            repair_source_sha256=(record.get(REPAIR_SOURCE) or {}).get('sha256'),
            delivery_context_files=delivery_context)
    record['patch']=patch;engine.store.put(COLLECTION,record['id'],record)
    from .scoped_codex_checks import repair
    delivered={name:raw_file(name) for name in {*parent_runtime,*declared,
               *(assets if modern else {}),'_delivery/changes.json',
               '_delivery/checks.py','_delivery/rationale.txt','work_order.json'}}
    if modern:
        delivered.update({'_control/' + name: raw for name,raw in base.items()})
    checks,rationale=await repair(engine,harness,run_id,record,delivered)
    if checks!=patch['checks'] or rationale!=patch['check_rationale']:
        record.setdefault('native_original_patch',copy.deepcopy(patch))
        patch={**patch,'checks':checks,'check_rationale':rationale}
        record['patch']=patch;engine.store.put(COLLECTION,record['id'],record)
    return await engine._read_consensus_check(run_id,build_candidate,engine,run_id,record,patch)
