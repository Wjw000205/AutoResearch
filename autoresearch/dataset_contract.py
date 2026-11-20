"""Dataset identity, prose and approved assets have separate request fields.

Version 2 applies only to newly frozen requests. Legacy replies remain readable;
no saved request, paid decision or experiment is rewritten by these adapters.
"""
from __future__ import annotations

import copy

from .experiment_workflow import sha

VERSION = 2
CONTEXT_GUIDANCE = ('数据集机器身份和绑定资产单独见 dataset_catalog；说明文字不是标识。'
    '数据集名称、实际路径和清单须与当前目录一致。按本次输出 Schema 提交实现或决策，'
    '目录引用不授予执行或科学批准；参数配置继续从获批 YAML 读取。')
GUIDANCE = '''
数据集使用独立的 dataset_catalog，不从叙述文字推断身份。目录由宿主从选型回执或同一冻结设计的绑定资产给出，引用不授予执行权限。
dataset_description 只写数据说明、协议缺项和限制；任务表 benchmark_tasks 每行只用 dataset_ref 引用目录，
若当前 Schema 提供紧凑 benchmark_io_matrix，也显式填写 dataset_ref。不要返回旧的根级 dataset 或任务元数据 dataset。
dataset_ref 必须逐字复制目录值，不能拼接状态、改名或计算新引用。宿主解析为精确名称和资产后再走原有校验。
实际运行参数及 comparison_task 仍填写真实消费者所需的精确数据集名称/路径；这些值须与所引用目录一致。
目录仅授权选中的资产身份，不把选型说明中的自有协议升级为论文协议。论文比较条件以已登记来源为准。
模型、训练和数据加载的 YAML 配置继续按已批准接口提供；未解决条件留在限制中，不污染任何身份字段。
'''


def name_schema(allowed=None, **overrides):
    result = {'type': 'string', 'minLength': 1,
              'description': 'Exact dataset name only. Put explanations, settings and status in their separate prose fields.'}
    if allowed is not None:
        result['enum'] = sorted(set(allowed))
    result.update(overrides)
    return result


def _assets(params):
    from .datasets import bindings
    return sorted(copy.deepcopy(bindings(params)), key=lambda asset: (asset['asset_id'], asset['manifest_sha256']))


def catalog(selection):
    rows = []
    for item in selection['datasets']:
        target = item['target']
        name = target['dataset']
        assets = _assets({'dataset_assets': item.get('dataset_assets') or [{
            'asset_id': target.get('asset_id'), 'manifest_sha256': target.get('manifest_sha256')}]})
        if not isinstance(name, str) or not name.strip():
            raise ValueError('Dataset catalog requires exact approved names and asset manifests')
        identity = {'dataset_name': name, 'dataset_assets': assets}
        row = {'dataset_ref': 'dataset:' + sha(identity), **identity}
        if target.get('note_id'):
            row['note_id'] = target['note_id']
        rows.append(row)
    rows.sort(key=lambda row: row['dataset_ref'])
    if not rows or len({row['dataset_name'] for row in rows}) != len(rows):
        raise ValueError('Dataset catalog must contain unique selected dataset identities')
    body = {'version': VERSION, 'selection_id': selection['id'],
            'selection_sha256': selection['sha256'], 'datasets': rows}
    return {**body, 'sha256': sha(body)}


def checked(value):
    from .datasets import bindings
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value.get('version') != VERSION
            or value.get('sha256') != sha({k: v for k, v in value.items() if k != 'sha256'})):
        raise ValueError('Frozen dataset catalog identity changed')
    rows = value.get('datasets')
    if (not isinstance(rows, list) or not rows or any(not isinstance(row, dict)
            or not isinstance(row.get('dataset_ref'), str)
            or not isinstance(row.get('dataset_name'), str) or not row['dataset_name'].strip() for row in rows)
            or len({row['dataset_ref'] for row in rows}) != len(rows)
            or len({row['dataset_name'] for row in rows}) != len(rows)):
        raise ValueError('Frozen dataset catalog requires unique nonempty dataset entries')
    for row in rows:
        bindings({'dataset_assets': row.get('dataset_assets')})
        identity = {key: row[key] for key in ('dataset_name', 'dataset_assets')}
        if row.get('dataset_ref') != 'dataset:' + sha(identity):
            raise ValueError('Dataset reference no longer matches its frozen identity/assets')
    return value


def bind_request(instruction, context, schema, *, template=False):
    """Freeze a v2 output schema and a separate catalog for a new request."""
    selection = context.get('dataset_selection')
    if (not selection and not context.get('dataset_catalog')) or context.get('dataset_request_protocol') == 1:
        return instruction, context, schema
    frozen = checked(context['dataset_catalog']) if context.get('dataset_catalog') else catalog(selection)
    if selection and (selection.get('id') != frozen['selection_id'] or selection.get('sha256') != frozen['selection_sha256']):
        raise ValueError('Dataset selection and catalog must refer to the same frozen receipt')
    if selection is None:
        selection = {'id': frozen['selection_id'], 'sha256': frozen['selection_sha256'],
            'datasets': [{'target': {'dataset': row['dataset_name'], **({'note_id': row['note_id']} if row.get('note_id') else {})}}
                         for row in frozen['datasets']]}
    refs = [row['dataset_ref'] for row in frozen['datasets']]
    result = copy.deepcopy(schema)
    properties = result['properties']
    prose = properties.pop('dataset', properties.get('dataset_description', {'type': 'string', 'minLength': 1}))
    properties['dataset_description'] = {**prose, 'description': 'Dataset prose and protocol limitations; never a machine identity.'}
    # Legacy single-dataset templates may have constrained the former name field.
    for key in ('enum', 'const'):
        properties['dataset_description'].pop(key, None)
    result['required'] = [key for key in result.get('required', []) if key != 'dataset']
    if 'dataset_description' not in result['required']:
        result['required'].append('dataset_description')
    reference = {'type': 'string', 'enum': refs, 'description': 'Copy one dataset_ref from the frozen dataset_catalog.'}
    if template:
        if len(refs) != 1:
            raise ValueError('A shared paper template requires one selected dataset')
        properties['dataset_ref'] = reference
        result['required'] = list(dict.fromkeys([*result['required'], 'dataset_ref']))
        properties.pop('benchmark_tasks', None)
        properties.pop('benchmark_io_matrix', None)
    else:
        tasks = properties['benchmark_tasks']
        task = tasks['items']
        task['properties'].pop('dataset', None)
        task['properties']['dataset_ref'] = copy.deepcopy(reference)
        task['required'] = list(dict.fromkeys([key for key in task['required'] if key != 'dataset'] + ['dataset_ref']))
        tasks['allOf'] = [{'contains': {'type': 'object', 'properties': {'dataset_ref': {'const': ref}},
                                      'required': ['dataset_ref']}, 'minContains': 1} for ref in refs]
        if 'benchmark_io_matrix' in properties:
            if len(refs) == 1:
                matrix = properties['benchmark_io_matrix']
                matrix['properties']['dataset_ref'] = copy.deepcopy(reference)
                matrix['required'] = list(dict.fromkeys([*matrix['required'], 'dataset_ref']))
            else:
                properties.pop('benchmark_io_matrix')
    packet = copy.deepcopy(context)
    packet['dataset_request_protocol'] = VERSION
    packet['dataset_catalog'] = copy.deepcopy(frozen)
    # Keep the selection receipt and source targets; do not blend planning prose
    # into the authoritative data identity supplied to the design author.
    packet['dataset_selection'] = {**{k: selection[k] for k in ('id', 'sha256')},
        'datasets': [{'target': copy.deepcopy(row['target'])} for row in selection['datasets']]}
    guidance = GUIDANCE
    if template:
        guidance += '\n本次仅返回一个完整共享模板：在根级用 dataset_ref 引用唯一目录项，dataset_description 填说明，不返回任务表。'
    return instruction + '\n' + guidance, packet, result


def materialize(reply, frozen, *, template=False):
    """Resolve references without changing the saved LLM response or its hash."""
    if frozen is None:
        if 'dataset_description' in reply or 'dataset_ref' in reply:
            raise ValueError('A structured dataset reply requires its own frozen catalog')
        return reply
    frozen = checked(frozen)
    if not isinstance(reply.get('dataset_description'), str) or 'dataset' in reply:
        raise ValueError('Dataset prose must use dataset_description; identity comes from dataset_ref')
    rows = {row['dataset_ref']: row for row in frozen['datasets']}
    result = copy.deepcopy(reply)
    selected = set()
    def resolve(item):
        if (not isinstance(item, dict) or 'dataset' in item
                or not isinstance(item.get('dataset_ref'), str) or item['dataset_ref'] not in rows):
            raise ValueError('Task dataset_ref must exactly reference the frozen selected catalog')
        row = rows[item.pop('dataset_ref')]
        selected.add(row['dataset_ref'])
        item['dataset'] = row['dataset_name']
        return row
    def validate_profiles(item, row, roles=('baseline', 'proposed', 'ablation')):
        from .fair_comparison import strict_equal, task_signature
        for role in roles:
            params = item.get(role + '_params')
            if (not isinstance(params, dict)
                    or task_signature(params).get('dataset') != row['dataset_name']
                    or not strict_equal(_assets(params), row['dataset_assets'])):
                raise ValueError(f'{role} parameters must consume the dataset name and exact assets resolved from dataset_ref')
    if template:
        row = resolve(result)
        validate_profiles(result, row, ('proposed', 'ablation'))
    elif 'benchmark_tasks' in result:
        for task in result['benchmark_tasks']:
            row = resolve(task)
            validate_profiles(task, row)
    elif 'benchmark_io_matrix' in result:
        row = resolve(result['benchmark_io_matrix'])
        result['benchmark_io_matrix'].pop('dataset')
        # I/O expansion additionally checks each concrete branch parameter path.
        validate_profiles(result, row)
    else:
        raise ValueError('Dataset catalog design requires explicit tasks or an I/O matrix')
    if selected != set(rows):
        raise ValueError('Dataset references do not cover the complete approved selection')
    result.setdefault('dataset', ', '.join(row['dataset_name'] for row in frozen['datasets']))
    result['dataset_request_protocol'] = VERSION
    result['dataset_catalog_sha256'] = frozen['sha256']
    result['dataset_catalog'] = copy.deepcopy(frozen)
    return result


def to_request(design, frozen):
    """Show the same reference-based shape to revision/patch authors."""
    frozen = checked(frozen)
    refs = {row['dataset_name']: row['dataset_ref'] for row in frozen['datasets']}
    result = copy.deepcopy(design)
    result['dataset_description'] = result.get('dataset_description', result.get('dataset', ''))
    result.pop('dataset', None)
    for key in ('dataset_request_protocol', 'dataset_catalog_sha256', 'dataset_catalog'):
        result.pop(key, None)
    for task in result.get('benchmark_tasks', []):
        name = task.pop('dataset')
        if name not in refs:
            raise ValueError('Internal task dataset no longer belongs to its frozen catalog')
        task['dataset_ref'] = refs[name]
    return result


def from_design(design):
    """Create a new request view of existing concrete bindings; keep the base intact."""
    if design.get('dataset_catalog'):
        frozen = checked(design['dataset_catalog'])
        materialize(to_request(design, frozen), frozen)
        return frozen
    tasks = design.get('benchmark_tasks')
    if not tasks:
        return None  # Legacy simulations/no-asset contracts retain their own interface.
    from .fair_comparison import strict_equal, task_signature
    rows = {}
    for task in tasks:
        params = task.get('proposed_params', {})
        if 'dataset_assets' not in params:
            return None
        assets = _assets(params)
        name = task['dataset']
        for role in ('baseline', 'proposed', 'ablation'):
            branch = task[role + '_params']
            if task_signature(branch).get('dataset') != name or not strict_equal(_assets(branch), assets):
                raise ValueError('Frozen design has contradictory dataset identities/assets; no catalog was inferred')
        if name in rows and not strict_equal(rows[name]['dataset_assets'], assets):
            raise ValueError('One frozen dataset name refers to different assets')
        rows[name] = {'target': {'dataset': name}, 'dataset_assets': assets}
    checksum = sha(design)
    return catalog({'id': 'frozen-design:' + checksum, 'sha256': checksum, 'datasets': list(rows.values())})


def from_packet(packet):
    checksum = packet.get('sha256') or sha(packet)
    return catalog({'id': packet.get('id') or 'dataset-packet:' + checksum,
                    'sha256': checksum, 'datasets': packet['datasets']})


def materialize_patch(patch, frozen):
    """Resolve complete task-array replacements while retaining unmodified fields."""
    checked(frozen)
    if 'dataset' in patch or 'dataset_ref' in patch:
        raise ValueError('A dataset patch must keep host identities and change only dataset_description or bound task references')
    if 'benchmark_tasks' not in patch:
        return copy.deepcopy(patch)
    result = materialize({**patch, 'dataset_description': patch.get('dataset_description', '')}, frozen)
    return {key: result[key] for key in patch}
