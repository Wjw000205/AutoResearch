"""A user-requested YAML migration is an agent-directed Codex work item."""
import copy
import hashlib
import json
from pathlib import Path

from .experiment_workflow import sha
from .storage import now
from .hyperparameter_yaml import parse, RUNTIME_FILE, RECEIPT_FILE

COLLECTION = 'yaml_hyperparameter_migrations'
CONTRACT_PATH = 'configs/hyperparameters.contract.json'
DEFAULT_PATH = 'configs/hyperparameters.yaml'
SCHEMA_PATH = 'configs/hyperparameters.schema.json'
INSTRUCTION = '''超参数必须由 YAML 动态加载，不能在 Python 中硬编码或把修改后的 YAML 忽略掉。
交付 configs/hyperparameters.yaml（version:1, profiles:{baseline,proposed,ablation}）、
configs/hyperparameters.schema.json、configs/hyperparameters.contract.json 以及实际 YAML 加载器。
contract.json 声明 version:1、format:yaml、defaults:configs/hyperparameters.yaml、schema:configs/hyperparameters.schema.json、
runtime_file:hyperparameters.yaml、receipt_file:hyperparameters.receipt.json、loader:<实际相对.py路径>。
YAML 包含模型深度、宽度、embedding、dropout，以及 optimizer、lr、weight_decay、betas、batch_size 等已支持超参数。
参数类型、必填字段、范围、条件依赖和派生维度必须有校验；未知键、重复键、无效 YAML、非有限值和缺失必填值要拒绝。
科学任务、数据来源/划分、种子、评价指标及预算留在宿主控制，不由 YAML 修改。
正式运行时宿主提供 hyperparameters.yaml 及 hyperparameters.receipt.json；后者含 yaml_sha256、revision_id、
profile、params_sha256、effective_profile_sha256。读取并验证实际 YAML 字节；选择 params.json.variant 对应 profile。
params.json 保留已批准配置的审计镜像；YAML 是超参数读取入口，不能缺失时悄悄退回硬编码。
YAML 中已有字段须与已批准镜像一致；新增缺省参数只允许已声明默认值，不改写历史 params。
保留原调用绑定验证，模型/优化器实际使用 YAML 解析值，输出 hyperparameter_yaml_sha256 和 effective_hyperparameters_sha256。
effective_profile_sha256 对 YAML 所选 profile 使用 UTF-8、ensure_ascii=False、sort_keys=True、separators=(',',':')。
params_sha256 对应宿主现有 JSON 默认空格编码（sort_keys=True,ensure_ascii=False,allow_nan=False），与 environment.json 一致。
默认 YAML 可含三个 profile；每次运行的 YAML 允许只含当前 profile 或本次成对 profile，schema 必须支持这些子集。
算法/variant 身份由宿主选择，YAML 不能切换方法族。缺省参数来自默认 YAML，不能从 Python 常量回填。
默认 YAML 必须保留原模型行为、张量名、初始化随机流和检查点兼容性；容量参数改变时派生维度、参数闭包和构造器同步适配。
必须有真实 CPU 检查：默认控制等价；只改 YAML 深度/宽度后模型层数/参数形状实际改变；只改 weight_decay 后实际优化器参数和 toy 更新改变；
非法/重复/缺失参数拒绝，错误 YAML SHA 或镜像不符拒绝。不得只断言文件存在或参数列表长度。
只实现获批修改单，不改变科学机制和验收，不训练正式模型、不访问留出集、不重跑历史实验、不安装依赖。
以后已支持的超参数调整只修订 YAML；新增模型组件或架构行为才派发 Codex 修改源码。'''


def request(engine, run_id, text):
    body = {'run_id': run_id, 'request': text, 'contract': INSTRUCTION}
    old = engine.store.get(COLLECTION, run_id)
    if old and old.get('sha256') == sha(body): return old
    if old: engine.store.put(COLLECTION, run_id+':history:'+sha(old), {**old, 'archived': True})
    row = {'id': run_id, 'run_id': run_id, 'body': body, 'sha256': sha(body), 'status': 'requested', 'created_at': now()}
    engine.store.put(COLLECTION, run_id, row)
    return row


def required(engine, run_id):
    row = engine.store.get(COLLECTION, run_id)
    if not row: return False
    if sha(row['body']) != row['sha256']: raise ValueError('YAML migration request changed')
    return (engine.store.get('programs', run_id) or {}).get('hyperparameter_yaml_protocol') != 1


def describe(engine, run_id, program):
    row = engine.store.get(COLLECTION, run_id)
    ready = program.get('hyperparameter_yaml_protocol') == 1
    if not row and not ready: return None
    if row and sha(row['body']) != row['sha256']: raise ValueError('YAML migration request changed')
    return {'protocol': 1, 'ready': ready, 'migration_required': bool(row and not ready),
            'request_id': row['id'] if row else None, 'request': row['body']['request'] if row else None,
            'contract': INSTRUCTION if row and not ready else None,
            'tools': ['hyperparameters.read', 'hyperparameters.revise'], 'execution_authorized': False,
            **({'schema': program['hyperparameter_contract']['schema'], 'schema_path':'/hyperparameter_yaml/schema'} if ready else {})}


def inspect_contract(bundle):
    from .code_bundle import relative_file
    from .delivery_files import io_path
    manifest = {r['path']: r for r in bundle['files']}
    required = {CONTRACT_PATH, DEFAULT_PATH, SCHEMA_PATH}
    if not required <= set(manifest): raise ValueError('YAML delivery lacks contract/defaults/schema files')
    def read(name):
        relative_file(name)
        path = io_path(Path(bundle['root']) / name)
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != manifest[name]['sha256']: raise ValueError('YAML delivery file changed')
        return raw.decode('utf-8')
    contract = json.loads(read(CONTRACT_PATH))
    expected = {'version': 1, 'format': 'yaml', 'defaults': DEFAULT_PATH, 'schema': SCHEMA_PATH,
                'runtime_file': RUNTIME_FILE, 'receipt_file': RECEIPT_FILE}
    if any(contract.get(k) != v for k, v in expected.items()) or contract.get('loader') not in manifest:
        raise ValueError('YAML delivery contract does not match the host runtime interface')
    if not contract['loader'].endswith('.py'): raise ValueError('YAML loader must be an actual Python module')
    from jsonschema import Draft202012Validator, ValidationError, SchemaError
    schema = json.loads(read(SCHEMA_PATH))
    def local_refs(value):
        if isinstance(value,dict):
            for k,v in value.items():
                if k in {'$ref','$dynamicRef'} and (not isinstance(v,str) or not v.startswith('#')):
                    raise ValueError('Hyperparameter schemas cannot fetch remote references')
                local_refs(v)
        elif isinstance(value,list):
            for v in value:local_refs(v)
    local_refs(schema)
    try:
        Draft202012Validator.check_schema(schema)
        defaults = parse(read(DEFAULT_PATH)); Draft202012Validator(schema).validate(defaults)
        if set(defaults['profiles']) != {'baseline','proposed','ablation'}:
            raise ValueError('Default YAML must cover baseline, proposed and ablation')
        for name, profile in defaults['profiles'].items():
            Draft202012Validator(schema).validate({'version':1,'profiles':{name:profile}})
    except (ValidationError,SchemaError) as exc:
        raise ValueError('YAML schema/defaults contract failed: '+str(exc.validator)) from exc
    from .hyperparameter_yaml import leaves
    from .tuning_scope import safe_path
    if any(not safe_path(path) for profile in defaults['profiles'].values() for path,_ in leaves(profile)):
        raise ValueError('YAML defaults contain a host-owned task/data/seed/resource field')
    return {'contract': contract, 'schema': schema, 'defaults': defaults,
            'file_hashes': {p: manifest[p]['sha256'] for p in [*sorted(required), contract['loader']]}}


def track(engine, run_id, decision):
    row = engine.store.get(COLLECTION, run_id)
    if not row or (engine.store.get('programs', run_id) or {}).get('hyperparameter_yaml_protocol') == 1: return
    row.update(status=decision['status'], decision_id=decision['id'], updated_at=now())
    engine.store.put(COLLECTION, run_id, row)
