"""Implementation-only CPU fixture admission and delivery contract."""
import hashlib
import json
from pathlib import Path

from .engine import ResearchBlocked, digest

GRANTS = 'implementation_fixture_grants'
RECEIPTS = 'implementation_fixture_receipts'
MANIFEST = 'tests/implementation-fixture.json'
OBSERVATION = 'artifacts/fixture-observation.json'


def runtime_binding():
    from .delivery_files import identity
    sources=('implementation_fixture_runner','dataset_row_identity','datasets','hyperparameter_yaml','lab','code_bundle')
    return {'version':1,'sources':{name:identity(Path(__file__).with_name(name+'.py'))['sha256'] for name in sources}}


def authorize(engine,run_id,document_sha256,requirement_ids,instruction,catalog_path,catalog_scope,catalog_prefixes):
    from . import implementation_cycle as cycle
    run=engine.store.get('runs',run_id)
    if run['stage']!='implementation' or run['status'] not in ('blocked','paused'):
        raise ResearchBlocked('夹具核验许可必须绑定已停止的实现工作。')
    workflows=[w for w in engine.store.list('codex_workflows',run_id) if w.get('kind')=='implementation'
        and w.get('cycle')==engine.cycle(run_id) and cycle.active(w)
        and w.get('implementation_generation')==run.get('state',{}).get('implementation_generation')]
    if len(workflows)!=1:
        raise ResearchBlocked('当前实现工作流不唯一。')
    state,=workflows
    doc=state['specifications'][-1]
    if doc.get('approved') is not True or doc['document']['sha256']!=document_sha256:
        raise ResearchBlocked('夹具许可须使用当前已批准文档。')
    cycle.validate_document(engine,run_id,state,doc)
    declared={c['id']:c for c in doc['spec']['acceptance_checks']}
    if (not requirement_ids or len(set(requirement_ids))!=len(requirement_ids)
            or any(i not in declared for i in requirement_ids)):
        raise ResearchBlocked('夹具核验范围须来自批准文档。')
    key=state['id']+':'+document_sha256
    previous=engine.store.get(GRANTS,key)
    if previous and (previous['scope']==instruction and previous['catalog']['path']==catalog_path
            and previous['catalog']['scope']==catalog_scope and previous['catalog']['prefixes']==catalog_prefixes
            and [c['id'] for c in previous['requirements']]==requirement_ids):
        return current(engine,state,doc)
    from .code_revision_review import _directory
    from .implementation_alignment_evidence import _code_text
    reviewed=next((a for a in reversed(state['attempts']) if a.get('review_votes')),None)
    if not reviewed: raise ResearchBlocked('缺少核验所依据的已审用例目录。')
    cycle.check_votes(engine,run_id,reviewed['review_votes'],cycle.code_materials(engine,run_id,state,doc,reviewed))
    source=_directory(engine,run_id,reviewed).get(catalog_path)
    if not source: raise ResearchBlocked('用例目录不属于已审交付。')
    catalog=json.loads(_code_text(engine,run_id,source['artifact_id'],source))
    obligations={}
    selected=set()
    for item in catalog:
        if item['scope']==catalog_scope and item['prefix'] in catalog_prefixes and set(requirement_ids)<=set(item['requirements']):
            selected.add(item['prefix'])
            for parameter in item['parameters']:
                case=item['prefix']+parameter
                if case in obligations: raise ResearchBlocked('批准的用例标识重复。')
                obligations[case]={'expected':item['expected'],'observation_fields':item.get('observation_fields',[])}
    if not obligations or selected!=set(catalog_prefixes): raise ResearchBlocked('已审目录中没有所选的全部用例。')
    body={'run_id':run_id,'workflow_id':state['id'],'cycle':state['cycle'],'document_sha256':document_sha256,
        'spec_hash':doc['spec_hash'],'requirements':[declared[i] for i in requirement_ids],'scope':instruction,
        'catalog':{'path':catalog_path,'sha256':source['sha256'],'scope':catalog_scope,'prefixes':catalog_prefixes,
            'delivery_hash':reviewed['delivery_hash'],'obligations':obligations},
        'plan_sha256':digest(state['common']['experiment_plan']),'phase':'implementation','mode':'isolated_cpu_fixture'}
    grant={**body,'sha256':digest(body)}
    if previous==grant: return grant
    if previous: raise ResearchBlocked('该版本已有不同的夹具许可。')
    engine.store.batch(puts=[(GRANTS,key,grant)],expected=[(GRANTS,key,None),('runs',run_id,run),('codex_workflows',state['id'],state)])
    return grant


def current(engine,state,doc):
    value=engine.store.get(GRANTS,state['id']+':'+doc['document']['sha256'])
    if value and (value.get('sha256')!=digest({k:v for k,v in value.items() if k!='sha256'})
            or value['workflow_id']!=state['id'] or value['spec_hash']!=doc['spec_hash']
            or value['run_id']!=state['run_id'] or value['cycle']!=state['cycle']
            or value['plan_sha256']!=digest(state['common']['experiment_plan'])):
        raise ResearchBlocked('夹具许可与实现任务不符。')
    return value


GUIDE = """为本次明确授权的 CPU 小型夹具交付 tests/implementation-fixture.json 及 tests/ 下独立 Python 探针。两者均列入 delivery_files；正式 runtime_files 仅列正式运行模块。
清单为 {version:1,entrypoint:"tests/实际探针.py",assets:[{id,filename,rows,columns:[{name,start,step}]}],params:{...},cases:[{id,obligation,asset,expect_exit,expect:{...}}],comparisons:[{left,right,field,result_field,delta}]}。仅提交已批准用例。id 使用英文字母、数字及下划线或连字符。columns 的整数 start/step 表示逐行整数序列；ISO 日期时间字符串 start 和整数 step 表示起始时刻及秒步长，输出按 YYYY-MM-DD HH:MM:SS。CSV 使用 UTF-8 无 BOM、逗号、LF。本协议是成对 CSV，恰好两个资产，均有成功用例；正例 A/B 的 obligation 都取授权 catalog.obligations 中的同一正例标识。全部负例逐一填原 obligation 标识并含 failure:{error_type, message_contains}。核对原 artifacts/failure.json 和非零退出。负例 observed 的 exit_code、success_output（布尔值）、failure_archived（布尔值）、error_type、message 与宿主实际原件相等；stage 和 field 须在生产失败文件或其 details 中按实际失败位置明确记录并匹配。每个用例在 observed 中真实提供目录列出的 observation_fields；跨资产独立计算的字段在 comparisons.result_field 声明，field 是两个过程输出的有限数值数组。
params 是完整测试专用参数模板（含测试专用任务镜像），用 {"$fixture":"trial_path"} 表示本次真实暂存路径。宿主设置 dataset_assets，模板必须指定 phase=validation。宿主按交付的 YAML 合同从参数生成和暂存真实 YAML。每个 case 只绑定一个已列资产。
宿主在独立临时任务生成资产清单、用途授权、YAML 和行身份回执，使用真实 ExperimentRunner，在配置的解释器及 CPU 限额内，每个 case 启动一个独立进程。宿主生成测试启动文件，调用冻结探针 run_case(case_id, expected_grant_sha256, params_path)；探针文件同时以 fixture_probe.py 暂存。可导入完整交付的正式运行模块，勿依赖未声明的测试辅助文件或 pytest。expected_grant_sha256 由可信测试启动文件传入。生产入口继续使用其正式授权来源；测试入口对同一消费实现传入独立测试授权。不得替换生产常量，不手造行数、YAML或授权，不加载正式资产、训练或评分留出。
探针在当前真实 trial 根目录读取环境。按冻结标准采集实际观测（含原路径、指纹、被调用的行或窗范围、断言和错误），始终写 artifacts/fixture-observation.json，含 case_id、observed（对象）。expect 是必须在 observed 中匹配的字段和值。正例末行输出 JSON，metrics 为实际断言数等非空有限数值。负例须由实际受测入口返回非零，留存真实异常、阶段、失败文件和原日志；禁止打印成功指标。expect_exit=0 表示通过进程，正整数指定精确拒绝退出码。允许在探针中对真实输入按该负例指定的单项故障作隔离变异，完整采集生产错误路径，负例的 observation JSON 顶层另存 mutation:{target,before,after}，来自实际调用时注入的值。若负例需要改动或删除真实 staged CSV 或 YAML，在该 case 可选 changed_inputs 数组列 csv 或 yaml；宿主在调用前保全独立原件，并比较执行后实际字节或缺失。不要事后恢复以掩盖变异。comparisons 在两个正例 observed[field] 同形有限数值数组上独立核对 right-left==delta。引用实际原始数组而非配置回显。完整覆盖许可内的原 A/B、全部原负例及零调用断言。不要自报宿主执行结果。"""


HOST_INTERFACE = """宿主按每个用例生成 experiment.py，其实际入口是 from fixture_probe import run_case，然后调用 run_case(case_id, expected_grant_sha256, sys.argv[1])。探针定义该函数即可。程序按冻结清单生成真实 CSV 和清单，用实际准入、暂存及 ExperimentRunner 执行。
清单 version:2 保留第 1 版格式。可在负例声明 source_edit:{op:"drop_column",column:列名}、{op:"set_cell",row:零基行号,column:列名,value:字符串} 或 {op:"copy_cell",row:目标行号,column:列名,from_row:原值行号}。宿主先修改真实 CSV、重新登记摘要和清单、再暂存签发行身份。语义故障直接用真实解析器读取这些字节。fixture-controls.json 的 source_edit 含 target（操作）、before 和 after（原/变异文件 SHA256），用于关联实际变异。该变异发生在暂存之前，changed_inputs 仅用于暂存后变异。
可在负例指定 bindings:[资产名称,...]，主 asset 必须包含在内，宿主按原顺序暂存每个真实资产。错序时倒置实际序列，保留数量和成员。可指定 replay_from:先前通过的正例 ID，宿主在当前 trial 的 fixture-controls.json 供给 replay:{case_id,row_identity,sha256}，取自该先前试次已核实的真实回执。探针必须使用这份回执复现跨试次错误。
暂存前内容摘要负例使用同格式 integrity_edit 做等字节长单点变异，expect_exit:null，failure:{error_type:"ValueError",message_contains:"size/SHA256 changed"}，expect 匹配 stage="asset_integrity"、field="sha256"、exit_code=null 等真实宿主观测。宿主在隔离副本保留原清单、调用真实 stage_assets、归档原始拒绝和前后摘要。该用例不启动模型进程。其他负例仍记录实际进程的非零退出和错误。"""

GUIDE += "\n"+HOST_INTERFACE


def failure_context(engine,run_id,attempt):
    """Return the archived first failure to the next writer, with its evidence binding."""
    if not attempt.get('fixture_receipt_sha256'): return None
    from .implementation_cycle import _stored
    vote=attempt['fixture_admission_votes'][0]
    packet=_stored(engine,run_id,vote['role'],vote['handoff_id'])[1]['materials']['fixture_check']
    if packet['binding']['delivery_hash']!=attempt['delivery_hash']:
        raise ResearchBlocked('夹具审核引用的交付与实际改码基础不一致。')
    receipt=read_receipt(engine,run_id,packet)
    if not receipt or receipt['sha256']!=attempt['fixture_receipt_sha256']:
        raise ResearchBlocked('修订所引用的隔离执行回执与交付不一致。')
    failed=next((c for c in receipt['cases'] if not c['passed']),None)
    if not failed: return None
    from .delivery_files import io_path
    root=engine.run_dir(run_id)/'implementation-fixtures'/packet['sha256'][:20]
    result={'case':failed,'receipt_sha256':receipt['sha256'],'not_run':receipt['not_run']}
    for row in receipt['archives']:
        path=Path(row['path'])
        parts=path.parts
        if (path.name in ('failure.json','fixture-observation.json') and (parts[:3]==('experiments',receipt['isolated_run_id'],failed['id'])
                or parts[:2]==('integrity',failed['id'])) and row['bytes']<=4*1024**2):
            from .implementation_fixture_runner import _read_report
            report=_read_report(io_path(root/path))
            name='failure' if path.name=='failure.json' else 'observation'
            result[name]=report
            result[name+'_file']=row
    return result


def declaration(engine,run_id,state,doc,attempt):
    from .implementation_alignment_evidence import _code_text
    grant=current(engine,state,doc)
    if not grant or attempt.get('fixture_grant_sha256')!=grant['sha256']: return None
    files={r['path']:r for r in attempt['delivery_artifacts']}
    if MANIFEST not in files: return None
    def read(path):
        row=files[path]
        return _code_text(engine,run_id,row['artifact_id'],row)
    manifest=json.loads(read(MANIFEST))
    from .implementation_fixture_runner import validate
    validate(manifest,files,grant['catalog']['obligations'])
    probe=files[manifest['entrypoint']]
    binding={'grant_sha256':grant['sha256'],'document_sha256':doc['document']['sha256'],
        'snapshot_sha256':attempt['snapshot']['files_hash'],'delivery_hash':attempt['delivery_hash'],
        'manifest_sha256':files[MANIFEST]['sha256'],'probe_sha256':probe['sha256'],'runtime':attempt['fixture_runtime']}
    packet={'sha256':digest(binding),'binding':binding,'grant':grant,'manifest':manifest,'probe':read(manifest['entrypoint'])}
    if attempt.get('fixture_host_interface'): packet['host_interface']=attempt['fixture_host_interface']
    return packet


def observations(engine,run_id,packet,receipt):
    """Project already authenticated case reports for the evidence-only review."""
    from .delivery_files import io_path
    root=engine.run_dir(run_id)/'implementation-fixtures'/packet['sha256'][:20]
    result=[]
    for file in receipt['archives']:
        path=Path(file['path'])
        if path.name!=Path(OBSERVATION).name: continue
        raw=io_path(root/path).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=file['sha256']: raise ResearchBlocked('读取夹具观测时原件已变化。')
        try: report=json.loads(raw)
        except (ValueError,UnicodeError): report={}
        result.append({'file':file,'report':report})
    return result


def matching_receipt(engine,run_id,state,doc,attempt,packet):
    """Reuse a reviewed execution only for byte-identical executable inputs."""
    from . import implementation_cycle as cycle
    target=packet['binding']
    fields=('grant_sha256','document_sha256','manifest_sha256','probe_sha256','runtime')
    def runtime_files(row):
        from .code_revision_review import _directory
        from . import code_bundle
        from .delivery_files import io_path
        files=_directory(engine,run_id,row)
        root=Path(row['snapshot']['workspace'])
        manifest=json.loads(io_path(root/'implementation.json').read_bytes())
        actual=code_bundle.describe(root,manifest,list(files.values()))
        if actual!=row['acceptance']['runtime_bundle']:
            raise ResearchBlocked('交付运行清单与已认证快照中的声明和文件不一致。')
        return actual
    for old in reversed(state['attempts']):
        if (old['index']>=attempt['index'] or not old.get('fixture_receipt_sha256') or not old.get('review_votes')
                or old.get('fixture_grant_sha256')!=target['grant_sha256'] or old.get('code_sha256')!=attempt.get('code_sha256')
                or old['acceptance']['runtime_bundle']['files']!=attempt['acceptance']['runtime_bundle']['files']):
            continue
        vote=old['fixture_admission_votes'][0]
        prior=cycle._stored(engine,run_id,vote['role'],vote['handoff_id'])[1]['materials']['fixture_check']
        if any(prior['binding'][k]!=target[k] for k in fields): continue
        if runtime_files(old)['files']!=runtime_files(attempt)['files']: continue
        original=read_receipt(engine,run_id,prior)
        if not original or original['sha256']!=old['fixture_receipt_sha256'] or not original['passed']: continue
        if original['limits']['python_executable']!=attempt.get('fixture_python_executable'): continue
        materials=cycle.code_materials(engine,run_id,state,doc,old)
        cycle.check_votes(engine,run_id,old['review_votes'],materials)
        if materials['fixture_receipt']!=original: raise ResearchBlocked('原代码审查未绑定该夹具执行回执。')
        return {'source_attempt':old['index'],'source_delivery':old['delivery_hash'],'target_delivery':attempt['delivery_hash'],
            'source_review_handoffs':[v['handoff_id'] for v in old['review_votes']],
            'source_receipt_sha256':original['sha256'],'runtime_bundle_sha256':attempt['acceptance']['runtime_bundle']['sha256'],
            'configured_interpreter_path':attempt['fixture_python_executable'],'measured_environment':'source_receipt',
            'matching_inputs':{k:target[k] for k in fields}},original
    return None


def read_receipt(engine,run_id,packet):
    from .deletion import _plain_path
    from .delivery_files import identity
    key=packet['sha256']
    receipt=engine.store.get(RECEIPTS,key)
    if receipt:
        if (receipt['sha256']!=digest({k:v for k,v in receipt.items() if k!='sha256'})
                or receipt['run_id']!=run_id or receipt['binding']!=packet['binding']):
            raise ResearchBlocked('夹具核验记录与冻结交付不一致。')
        root=engine.run_dir(run_id)/'implementation-fixtures'/key[:20]
        for file in receipt['archives']:
            path=root/file['path']
            _plain_path(path,root)
            if identity(path)!={k:file[k] for k in ('sha256','bytes')}: raise ResearchBlocked('夹具原始记录发生变化。')
    return receipt
