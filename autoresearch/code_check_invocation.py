"""Frozen CLI invocations share the existing CPU allowance and archived inputs."""
from __future__ import annotations
import copy
import hashlib
from pathlib import Path,PureWindowsPath,PurePosixPath

from .code_repair_contract import obj,sha

VERSION=2
ROOT_TOKEN='{frozen_delivery}'
MAX_ARGUMENT_CHARS=2000
SCHEMA=obj({'input_scope':{'enum':['isolated_source','frozen_delivery']},
    'arguments':{'type':'array','maxItems':40,'items':{'type':'string','maxLength':MAX_ARGUMENT_CHARS,
        'pattern': r'^[^\u0000]*$', 'x-autoresearch-control':'bounded-string-v1'}}})


def validate(plan,manifest):
    invocation=plan['invocation']
    if invocation.get('input_scope') not in {'isolated_source','frozen_delivery'}:
        raise ValueError('Unknown bounded checker input scope')
    arguments=invocation.get('arguments')
    if not isinstance(arguments,list) or len(arguments)>40:
        raise ValueError('Checker arguments must be a bounded argv list')
    uses_root=False
    for token in arguments:
        if not isinstance(token,str) or len(token)>MAX_ARGUMENT_CHARS or '\x00' in token:
            raise ValueError('Invalid checker argument')
        value=token.split('=',1)[-1]
        if ROOT_TOKEN in value:
            if value!=ROOT_TOKEN:
                raise ValueError('Frozen input root must be a complete argument value')
            uses_root=True
        elif (PureWindowsPath(value).drive or PureWindowsPath(value).root or PurePosixPath(value).is_absolute()
                or '..' in value.replace('\\','/').split('/')):
            raise ValueError('Checker arguments cannot name external or parent paths')
    if uses_root != (invocation['input_scope']=='frozen_delivery'):
        raise ValueError('Frozen input scope requires its explicit root argument')
    from .code_verification import checked_path
    for name in plan['report_paths']:
        checked_path(Path('.'),name)
        if not name.startswith('test-results/') or not name.endswith('.json'):
            raise ValueError('Generated checker reports require a relative test-results JSON path')
        if name in manifest.get('delivery_files',[]) and name not in manifest.get('test_result_files',[]):
            raise ValueError('Checker output would overwrite an admitted source input')
    if not plan['report_paths'] or len(set(plan['report_paths']))!=len(plan['report_paths']):
        raise ValueError('Select distinct reports actually produced by this invocation')
    return copy.deepcopy(invocation)


def admit(engine,run_id,state,spec,attempt,source,task,plan,parent):
    """Add a step; never edit or reset the workflow's existing admission."""
    from . import code_verification as checks
    import json
    directory=Path(attempt['snapshot']['workspace'])
    manifest=json.loads((directory/'implementation.json').read_bytes())
    invocation=validate(plan,manifest)
    if parent is None:
        parent={'version':checks.VERSION,'run_id':run_id,'workflow_id':state['id'],
            'implementation_document_sha256':spec['document']['sha256'],'entrypoint':plan['entrypoint'],
            'timeout_seconds':float(plan['timeout_seconds']),'timeout_reference':copy.deepcopy(plan['allowance_reference']),
            'inherited_executions':[],'invocation_only':True,'authorization':'project_cpu_checks_default_and_independent_frozen_plan'}
        parent['sha256']=sha(parent)
        if engine.store.get(checks.POLICIES,state['id']) is not None:
            raise ValueError('Workflow admission changed while creating its checker step')
        engine.store.put(checks.POLICIES,state['id'],parent)
    if parent and (parent['entrypoint']!=plan['entrypoint']
            or parent['implementation_document_sha256']!=spec['document']['sha256']
            or plan['timeout_seconds']>parent['timeout_seconds']):
        raise ValueError('A checker step cannot replace the entry, approved document or shared allowance')
    binding={'task_id':task['id'],'handoff_id':task['handoff_id'],'plan_sha256':sha(plan),
        'snapshot_sha256':attempt['snapshot']['files_hash'],'parent_policy_sha256':parent.get('sha256') if parent else None}
    identity=state['id']+':checker-step:'+sha(binding)
    value={'version':checks.VERSION,'id':identity,'run_id':run_id,'workflow_id':state['id'],
        'base_snapshot_sha256':attempt['snapshot']['files_hash'],
        'implementation_document_sha256':spec['document']['sha256'],'source_sha256':sha(source),
        'entrypoint':plan['entrypoint'],'requirement_ids':copy.deepcopy(plan['requirement_ids']),
        'report_paths':copy.deepcopy(plan['report_paths']),'report_contract':'development_report_v1',
        'timeout_seconds':float(plan['timeout_seconds']),'shared_timeout_seconds':parent['timeout_seconds'],
        'timeout_reference':copy.deepcopy(plan['allowance_reference']),
        'inherited_executions':copy.deepcopy(parent.get('inherited_executions',[])) if parent else [],
        'invocation_version':VERSION,'invocation':invocation,'step_binding':binding,
        'training_during_implementation':False,'routed_task_id':task['id'],
        'routed_handoff_id':task['handoff_id'],'authorization':'project_cpu_checks_default_and_independent_frozen_plan',
        'deadline_environment_variable':checks.deadline_variable(directory/plan['entrypoint'])}
    value['checker_input_sha256']=checks._inventory(directory,manifest)
    if invocation['input_scope']=='frozen_delivery':value['frozen_delivery_sha256']=inventory(directory,manifest)
    if parent:value['allowance_policy_sha256']=parent.get('allowance_policy_sha256',parent['sha256'])
    value['sha256']=sha(value)
    old=engine.store.get(checks.POLICIES,identity)
    if old and old!=value:raise ValueError('Frozen checker step admission changed')
    if not old:engine.store.put(checks.POLICIES,identity,value)
    return value


def inventory(directory,manifest):
    from .code_verification import checked_path
    return {name:hashlib.sha256(checked_path(directory,name).read_bytes()).hexdigest()
        for name in manifest['delivery_files']}


def stage(directory,work,manifest,limits):
    """Build a separate complete copy; real frozen deliveries stay untouched."""
    import shutil
    from .code_verification import checked_path
    invocation=limits['invocation'];arguments=copy.deepcopy(invocation['arguments'])
    record={'input_scope':invocation['input_scope'],'arguments':copy.deepcopy(arguments)}
    if invocation['input_scope']=='frozen_delivery':
        expected=limits['frozen_delivery_sha256'];actual=inventory(directory,manifest)
        if actual!=expected:raise ValueError('Complete frozen delivery changed before staging')
        root=work/'.host-frozen-delivery'
        if 'disk_mb' in limits:
            from .disk_usage import tree_bytes
            if (tree_bytes(work)+sum(checked_path(directory,n).stat().st_size for n in expected)
                    >limits['disk_mb']*1024*1024):
                raise ValueError('Complete frozen input exceeds the admitted disk allowance')
        for name,digest in expected.items():
            destination=checked_path(root,name);destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(checked_path(directory,name),destination)
            if hashlib.sha256(destination.read_bytes()).hexdigest()!=digest:
                raise ValueError('Frozen input copy differs from the admitted delivery')
        record['frozen_delivery_sha256']=copy.deepcopy(expected)
        arguments=[t.replace(ROOT_TOKEN,str(root)) for t in arguments]
    return arguments,record


def unchanged(work,manifest,limits):
    if limits.get('invocation',{}).get('input_scope')=='frozen_delivery':
        return inventory(work/'.host-frozen-delivery',manifest)==limits['frozen_delivery_sha256']
    return True


def retained(engine,run_id,identity,reports):
    """Task IDs and a new plan receipt do not authorize repeating the same call."""
    from . import code_verification as checks
    allowance=identity.get('allowance_policy_sha256',identity['policy_sha256'])
    for receipt in engine.store.list(checks.RECEIPTS,run_id):
        old=receipt.get('identity',{})
        if old.get('allowance_policy_sha256',old.get('policy_sha256'))!=allowance:continue
        if any(old.get(k)!=identity.get(k) for k in ('source_sha256','entrypoint','python_executable',
                'resource_limits','timeout_seconds')):continue
        old_call=old.get('invocation',{'input_scope':'isolated_source','arguments':[]})
        if old_call!=identity['invocation'] or old.get('frozen_delivery_sha256')!=identity.get('frozen_delivery_sha256'):continue
        if receipt.get('status')=='running':
            raise ValueError('The same checker invocation is still owned by an active execution')
        checks._sealed_receipt(engine,receipt,old)
        if receipt.get('passed') and not set(reports)<=set(receipt.get('test_reports',{})):
            raise ValueError('The completed checker invocation did not produce the requested output; retain it without rerunning')
        return receipt
    return None


INSTRUCTION=('invocation 描述你实际读取的既有入口支持的参数，宿主不猜测阶段或文件名。'
    'invocation-v2 的 arguments 最多40项，每项最多2000字符，禁止 NUL；这是执行控制边界，不是说明文字长度限制。'
    '只执行未完成的原检查，保留已通过阶段；arguments 是 argv 列表，不是 shell 命令。'
    '需要完整冻结目录作静态检查时选择 input_scope=frozen_delivery，并以 {frozen_delivery} 作为实际参数值；'
    '宿主会提供与本交付哈希绑定的独立完整副本，不能传真实工作区或外部路径。'
    '其他检查选择 isolated_source；不要为选阶段而重跑整个清单。'
    'report_paths 是本次入口实际生成的宿主输出，可不在归档 test_result_files 中；只允许 test-results/ 下 JSON，'
    '不能复制旧报告或覆盖源码。归档交付清单与新宿主输出是不同范围，不为宿主输出重写冻结清单。'
    '已有 CPU 总截止、已用时间、资源和原断言保持约束；该计划不授权科研训练或科学批准。')
