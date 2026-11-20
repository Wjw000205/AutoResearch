"""Explicit additive correction of the demonstrated consumer-scope deadlock."""
import copy
import gzip
import json
from pathlib import Path

from .experiment_workflow import sha
from .storage import now, uid


SPECS = [
    ('autoresearch/bounded_diagnostics.py', 'execute_once',
     '正常终态已改为authority_confirmed，但实际消费者仍只认completed；补丁一直停留在_delivery中。',
     '核对并将_delivery/bounded_diagnostics_consumer.patch中适用的接续逻辑落实到这个真实函数。新执行和重入都调用当前validate_completion核验权威回执、返回观察、代码/输入/协议身份及资源有效性；只在全部通过时进入reviewing_results。兼容保留旧冻结协议；unknown/incomplete/取消/超限不得被当作成功，也不得触发重新训练或重复权威写入。',
     '直接导入并调用修改后的execute_once；正常authority_confirmed路径推进，缺失或过期观察、契约错配、确认丢失及失败检查点场景均按原门禁保留现场；不能靠未应用补丁、替代函数或跳过断言通过。'),
    ('autoresearch/diagnostic_terminal.py', 'finish',
     '原seal承担的experiment、attempt、confirmation权威写入移到了共享Job退出之后，迁出的实际工作尚未闭合CPU归属。',
     '重排权威写入与最终观察的边界，使原有实际写入工作仍受到原累计CPU预算的有效计量和约束；可将实际写入留在受计量工作进程，或提供等价且可验证的预算计入机制。只把有限观察视为观察，不能通过改名豁免原工作。维持同一原墙钟截止、原子一致性和原回执保留，不引入观察自身无限递归确认。',
     '零模型检查覆盖正常完成、近CPU额度写入、内核CPU增长、计量失败、取消及确认丢失；验证迁出的实际写入没有漏计或重置额度，失效不会被消费者作为成功使用，原事务和进度仍可追溯。'),
    ('autoresearch/diagnostic_supervisor.py', 'resolve_terminal',
     '监督器目前在共享Job归零后直接调用finish，需要与修正后的写入计量和终态边界一致。',
     '把最终写入的计量、退出和持久化结果按同一契约传递给finish及返回观察；保证正常路径可达。取消、超时、写入失败和只读恢复不得隐藏原错误、追加写重试或延长原执行截止；读回只解析已存在的精确事务。',
     '通过实际resolve_terminal与实际消费者的集成夹具验证正常推进、写入故障、确认丢失和迟到返回；权威状态、资源有效性和消费结果必须一致且不循环恢复。'),
    ('autoresearch/diagnostic_projection.py', 'validate_completion',
     '新生产者状态和旧消费者未接通，需要一个被真实调用的统一完成判定。',
     '与execute_once对齐返回契约，精确校验authority_confirmed对应的回执身份、原资源边界和有效返回观察；保留权威事实与资源/消费有效性的区分。不得只检查状态字符串，也不得将历史协议证据改写为新协议通过。',
     '覆盖正常输入、缺字段、错SHA、错协议、计量不可用及迟到观察的拒绝；测试证明execute_once确实消费这个验证器，而不只是测试孤立helper。'),
    ('tests/test_diagnostic_execute_once.py', 'test_execute_once_timely_path_advances_after_exact_contract_and_observation',
     '已有正常路径断言依赖尚未应用的消费者修改；测试必须验证真实源文件接续。',
     '保留并完善本测试及同文件失败路径用例，直接使用实际execute_once、实际终态生成/读回和validate_completion。移除对未应用补丁或未来授权的依赖；补充生产者到消费者的正常及异常端到端接口夹具，全部零模型更新。',
     '正常路径确实进入reviewing_results；失败、资源无效、过期和确认丢失路径不误推进、不重训、不覆盖旧回执；所有相关断言真实执行且不以skip/xfail掩盖未接通。'),
    ('autoresearch/diagnostic_resource_evidence.py', 'capture',
     '工程修订后，资源证据需要与生产者、消费者和测试的实际新版本绑定。',
     '同步实际消费的协议、源码SHA及检查来源，覆盖消费者接续与修复后的CPU归属。保留旧版本证据；未运行的检查明确not_run。磁盘范围按原attempt目录合同说明，不额外扩大为整个共享数据库配额，也不豁免原工作。',
     '新证据中的源码/协议/测试指向同一候选；篡改绑定被拒，旧回执仍可核查；新候选只有经过独立审查和真实检查后才能进入安装及诊断重新绑定。'),
]


def migrate(engine, repair_id, *, instruction):
    from .bounded_host_repair import collect, checksum, ALLOWED, INSTRUCTION, COLLECTION
    from .host_repair_locations import locations, bind, verify_tasks, render
    from .rapid_experiment_revision import _write_files
    from .scoped_codex_recovery import current, _bound
    before = engine.store.get(COLLECTION, repair_id)
    run = engine.store.get('runs', before['run_id'])
    if run['status'] not in ('paused', 'blocked'):
        raise ValueError('Stop the research before correcting its native work scope')
    if before.get('located_scope_correction'):
        return before
    checkpoint = current(before['job']).get('checkpoint', {})
    if checkpoint.get('status') not in ('completed', 'failed', 'interrupted') or not checkpoint.get('recovered_read_only'):
        raise ValueError('Read back and confirm the old native turn before replacing its scope')
    from .code_model_migration import job_profile
    selected = job_profile(engine, run['id'], before['job'], work=current(before['job']))
    if not _bound(checkpoint, run['id'], current(before['job']), Path(before['job']['workspace']),
                  engine.run_dir(run['id']), selected['model'] or None,
                  selected['effort'] or None):
        raise ValueError('Native readback is not bound to this original work item')
    base = before if before.get('delivery') and checkpoint['status'] == 'completed' else next(
        (r for r in reversed(before.get('revision_history', [])) if r.get('delivery')), None)
    if base is None or collect(base) != base['delivery']:
        raise ValueError('No unchanged completed candidate is available for scoped continuation')
    source = Path(base['job']['workspace'])
    allowed = set(ALLOWED) | set(base.get('revision_editable', [])) | {r['path'] for r in base['delivery']['changed_files']}
    catalog = locations(source, allowed, include_readonly=True)
    index = {(r['path'], r['symbol']): r for r in catalog}
    changes = []
    for path, symbol, problem, change, acceptance in SPECS:
        loc = index.get((path, symbol))
        if loc is None:
            raise ValueError('Correction location does not exist in this candidate: ' + path + '::' + symbol)
        changes.append({'location_id': loc['location_id'], 'problem': problem, 'change': change, 'acceptance': acceptance})
    tasks = bind(changes, catalog, role='operator_requested_scope_correction')
    files = {name: (source / name).read_bytes() for name in base['delivery']['files']}
    verify_tasks(tasks, {name: checksum(raw) for name,raw in files.items()}, allowed)
    order = json.loads(files['work_order.json'])
    order.update(location_protocol=1, location_catalog=catalog, edit_tasks=tasks, edit_tasks_path='EDIT_TASKS.md',
        allowed_files=sorted(allowed), scope_correction_instruction=instruction,
        previous_delivery_sha256=base['delivery']['all_files_sha256'],
        suspended_native_operation_id=current(before['job'])['operation_id'],
        source_manifest={name: checksum(raw) for name,raw in files.items() if name not in ('work_order.json','AGENTS.md','EDIT_TASKS.md')})
    text = INSTRUCTION + '\n本次用户明确要求修复工单范围并提供精确修改位置。先读EDIT_TASKS.md，逐项完成；execute_once所在文件已明确允许修改。保留此前已完成的正确修订，不从原版重写。'
    files['work_order.json'] = json.dumps(order, ensure_ascii=False, indent=2).encode()
    if before.get('compact_review_protocol') == 1:
        from .host_review_compaction import compact_work_order
        order['source_manifest'] = {k:v for k,v in order['source_manifest'].items() if not k.startswith('_control/')}
        compact_work_order(order, files)
        files['work_order.json'] = json.dumps(order, ensure_ascii=False, indent=2).encode()
    files['AGENTS.md'] = text.encode()
    files['EDIT_TASKS.md'] = render(tasks).encode()
    identity = sha({'before': sha(before), 'tasks': tasks, 'instruction': instruction})
    target = source.parent / ('located-' + identity[:16])
    _write_files(target, files)
    # Keep the interrupted partial directory and the full prior controller
    # record. No old instruction, paid request, vote or file is rewritten.
    archive = engine.run_dir(before['run_id']) / 'host-repairs' / ('scope-before-' + sha(before) + '.json.gz')
    raw = json.dumps(before, ensure_ascii=False, sort_keys=True).encode()
    if not archive.exists():
        archive.write_bytes(gzip.compress(raw, mtime=0))
    if gzip.decompress(archive.read_bytes()) != raw:
        raise ValueError('Scope correction history archive changed')
    after = copy.deepcopy(before)
    for field in ('delivery','acceptance','checks','check_claim','delivered_at','installation_reviewed','last_feedback','scope_error'):
        after.pop(field, None)
    after.update(status='implementing', location_protocol=1,
        revision_editable=sorted(set(base.get('revision_editable', [])) | {r['path'] for r in base['delivery']['changed_files']}),
        input_manifest={name: checksum(raw) for name,raw in files.items()},
        job={'operation_id': uid(), 'workspace': str(target), 'instruction': text, 'created_at': now()},
        located_scope_correction={'id': identity, 'before_sha256': sha(before), 'original_record_path': str(archive),
            'source_delivery_sha256': base['delivery']['all_files_sha256'], 'edit_tasks_sha256': sha(tasks),
            'instruction': instruction, 'approval_inherited': False, 'created_at': now()})
    engine.store.batch(puts=[(COLLECTION, repair_id, after)], expected=[(COLLECTION, repair_id, before), ('runs', run['id'], run)])
    engine.store.event(run['id'], 'diagnostic.host_repair_scope_corrected',
        '已新增带文件/符号/行号/SHA/改法/验收条件的修改单，并明确授权消费者文件；旧工单与中断目录保留。',
        {'repair_id': repair_id, 'scope_correction_id': identity, 'edit_task_count': len(tasks)})
    return after
