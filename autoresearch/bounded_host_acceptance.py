"""Independent host-patch review, isolated checks, and authenticated rework."""

from .async_ownership import gather as gather_owned
import asyncio
import copy
import difflib
import json
from itertools import zip_longest
from pathlib import Path
import sys
import tempfile

from . import bounded_evidence_review as packets
from .experiment_workflow import sha
from .parameter_resolution import owner
from .storage import now, uid
from .training_adjustment import obj

VOTE = obj({'packet_sha256': packets.HASH, 'approve': {'type': 'boolean'},
            'rationale': packets.TEXT,
            'required_changes': {'type': 'array', 'maxItems': 12, 'items': packets.TEXT}})
TRIAGE = '''独立检查这份宿主工程交付是否完整，尚不批准代码、安装或实验。
阅读作者说明、测试计划、已定位问题和修改清单。缺少可运行的正常路径、把全部诊断永久阻断、
尚未实现磁盘写入保障或最终权威状态协议，都不能算完成修复。作者坦诚说明部分修复值得保留，但应继续工程修订。
测试尚未执行本身不是源码交付不完整：完整候选在源码审查后由宿主执行测试。
不要要求通过运行模型才能修好宿主，不扩大科学判据、种子或原资源额度；指出可实现的最少剩余修改及验收条件。
对照原合同和原计量代码确认资源范围；若某项要求超出了冻结范围或造成观察者无限递归，请明确说明依据和需要澄清的边界，
不能把未经批准的新要求当作旧验收，也不能自行豁免原有计量范围。
approve仅表示可以进入完整代码审查；不完整则approve=false且required_changes列具体问题。'''
CODE_REVIEW = '''根据本角色完整源码读取观察，独立审核宿主修订与测试代码。
候选需满足原问题的可运行正常路径及失败关闭，保留原始回执；禁止将永远拒绝执行当成完成。
核对发布和退出计量、异常传播、磁盘实际写入路径以及源代码与测试契约。测试随后由宿主在隔离CPU环境执行，
不能要求先有训练结果或已执行的这些测试才允许进行代码审核。
approve只授权本候选的指定离线CPU检查，不授权安装、正式诊断或科学结论。存在具体问题则给出required_changes。'''
CHECK_REVIEW = '''独立审核本候选的真实宿主测试回执和原验收要求。
通过数仅支持实际测试的行为；接口替身不能冒充真实Windows行为，禁止把永久阻断测试当正常路径闭合。
核对是否实际覆盖所有阻塞项；失败、缺失或not_run必须保留，不得根据作者说明补造成功。
approve只表示这份精确SHA的宿主修订具备安装条件，科学门禁和原诊断的重新绑定/独立审核仍保留。'''


def request(record, role, phase, payload, *, read=False, existing=None):
    from . import host_review_compaction as compact
    use_compact = (existing['context'][packets.KEY].get('compact_review_protocol') == compact.VERSION
                   if existing else record.get('compact_review_protocol') == compact.VERSION)
    mapping = None
    if use_compact:
        payload, mapping = compact.project(record, 'read' if read else phase, payload)
    packet = {'version': 1, 'phase': 'read' if read else phase, 'host_repair_id': record['id'],
              'candidate_sha256': record['delivery']['all_files_sha256'], 'role': role, 'payload': payload}
    if use_compact:
        packet['compact_review_protocol'] = compact.VERSION
        if not read and (existing is None or existing['context'][packets.KEY].get('receipt_bound_vote') == 1):
            packet['receipt_bound_vote'] = 1
        if read and (existing is None or existing['context'][packets.KEY].get('section_id_catalog') == 1):
            packet['section_id_catalog'] = 1
            if existing is None or existing['context'][packets.KEY].get('receipt_bound_read') == 1:
                packet['receipt_bound_read'] = 1
    packet['packet_sha256'] = sha(packet)
    schema = copy.deepcopy(packets.PACKET_SCHEMA if read else VOTE)
    instruction = packets.READ if read else {'triage': TRIAGE, 'code': CODE_REVIEW, 'checks': CHECK_REVIEW}[phase]
    if packet.get('section_id_catalog') == 1:
        ids = [section['id'] for section in payload['sections']]
        observations = schema['properties']['observations']
        # Use a new property schema: the two hash fields share the HASH object.
        observations['items']['properties']['section_id'] = {**packets.HASH, 'enum': ids}
        observations.update(minItems=len(ids), maxItems=len(ids))
        schema['properties']['packet_sha256'] = {'const': packet['packet_sha256']}
        if packet.get('receipt_bound_read') == 1:
            schema['required'].remove('packet_sha256')
            instruction += '\n只返回 observations。宿主通过本角色的交接回执绑定整批 SHA；每条观察仍须提供准确的 section_id 和逐字引文。'
    if not read and record.get('location_protocol') == 1:
        from .host_repair_locations import vote_schema, GUIDANCE
        scope = payload.get('review_scope', payload)
        schema = vote_schema([{'location_id': alias} for alias in mapping] if use_compact else scope['location_catalog'], packets.TEXT, packets.HASH)
        instruction += ('\n使用响应schema中的F编号::函数名选择修改位置；文件编号见location_files。宿主绑定当前SHA和行号，不要生成或抄写SHA。'
                        '每项仍须提供具体problem、change、acceptance，原资源与科学门禁不变。' if use_compact else '\n' + GUIDANCE)
    if packet.get('receipt_bound_vote') == 1:
        schema['properties']['packet_sha256'] = {'const': packet['packet_sha256']}
        schema['required'].remove('packet_sha256')
        instruction += '\n只填写 approve、rationale、required_changes。宿主用本角色的交接回执绑定本候选及当前评审阶段。'
    if use_compact:
        instruction += '\n本请求为分阶段精确投影；原材料保留。复用的同角色观察不是批准，须结合当前改动作出新判断；不得把目录或源码覆盖当作行为验证。'
    return {'purpose': 'rapid_review:host-repair:' + packet['packet_sha256'],
            'instruction': instruction,
            'context': {'training_context_protocol': 1, packets.KEY: packet},
            'schema': schema}


async def ask(engine, harness, record, role, req, expected, save, *, read=False):
    if any(req[k] != expected[k] for k in expected):
        raise ValueError('Host review request changed')
    if 'answer' not in req:
        save()
        try:
            req['answer'] = await harness.ask(record['run_id'], role, req['purpose'], req['instruction'], req['context'], req['schema'])
        except Exception:
            from .host_review_compaction import interrupted_legacy_read
            proof = interrupted_legacy_read(harness, record, role, req) if record.get('compact_review_protocol') == 1 else None
            if not proof:
                raise
            previous = copy.deepcopy(req)
            packet = req['context'][packets.KEY]
            replacement = request(record, role, 'read', packet['payload'], read=True)
            req.clear(); req.update(replacement)
            req['compact_predecessor'] = {'request': previous, 'terminal_readback': proof,
                                          'scope': 'Confirmed interrupted read only; original frozen input and receipts retained.'}
            save()
            engine.store.event(record['run_id'], 'review.compact_read_successor',
                '原大请求已确认中断且无完成答复；保留原回执，用精简新请求补齐未完成读取。',
                {'original_handoff_id': proof['handoff_id'], 'role': role})
            req['answer'] = await harness.ask(record['run_id'], role, req['purpose'], req['instruction'], req['context'], req['schema'])
        save()
    if read:
        from .host_read_citations import ensure
        await ensure(engine, harness, record, role, req, save)
    else:
        from .host_review_compaction import vote_packet_matches
        a = req['answer']
        if not vote_packet_matches(req['purpose'], req['context'][packets.KEY], a) or a['approve'] != (a['required_changes'] == []):
            raise ValueError('Host review identity or disposition is inconsistent')
        if record.get('location_protocol') == 1:
            from .host_repair_locations import bind
            from .host_review_compaction import normalize
            payload = req['context'][packets.KEY]['payload']
            catalog = (record['compact_material']['body']['location_catalog']
                       if req['context'][packets.KEY].get('compact_review_protocol') else payload.get('review_scope', payload)['location_catalog'])
            bind(normalize(record, req)['required_changes'], catalog, role=role)
    return owner(engine, record['run_id'], role, req)


def material(record):
    from .bounded_host_repair import collect, checksum
    if collect(record) != record['delivery']:
        raise ValueError('Host candidate changed after delivery')
    root = Path(record['job']['workspace'])
    order = json.loads((root / 'work_order.json').read_bytes())
    original_path = root / '_evidence/candidate/host_evidence.json'
    original = json.loads(original_path.read_bytes()) if original_path.is_file() else {}
    protocol_path = root / '_evidence/candidate/bounded_protocol.json'
    protocol = json.loads(protocol_path.read_bytes()) if protocol_path.is_file() else None
    # Earlier partial deliveries have never passed source review. Review/test
    # the cumulative candidate, not merely this revision's newly edited files.
    changes = copy.deepcopy(record['delivery']['changed_files'])
    declared = {r['path'] for r in changes}
    for name in record.get('revision_editable', []):
        if name not in declared:
            changes.append({'path': name, 'sha256': record['delivery']['files'][name],
                            'reason': 'Retained earlier candidate change; requires current independent review'})
    result = {'reviewer_findings': order['reviewer_findings'], 'limits': order['original_limits'],
            'original_resource_scope': original.get('scope'),
            'original_disk_watchdog': original.get('source', {}).get('watchdog'),
            'diagnostic_protocol': protocol,
            'rationale': (root / '_delivery/rationale.md').read_text(encoding='utf-8'),
            'test_plan': json.loads((root / '_delivery/test_plan.json').read_bytes()),
            'changes': changes}
    if record.get('location_protocol') == 1:
        from .host_repair_locations import locations
        result['location_catalog'] = locations(root, set(order['allowed_files']) | {r['path'] for r in changes}, include_readonly=True)
        if record['delivery'].get('repair_responses'):
            result['repair_responses'] = record['delivery']['repair_responses']
    return result


async def votes(engine, harness, record, stage, payload, save):
    state = record.setdefault('acceptance', {}).setdefault(stage, {})
    result = {}
    for role in packets.ROLES:
        expected = request(record, role, stage, payload, existing=state.get(role))
        req = state.setdefault(role, expected)
        await ask(engine, harness, record, role, req, expected, save)
        from .host_review_compaction import normalize
        result[role] = normalize(record, req)
    return result


def rejected(votes):
    return {role: v['required_changes'] for role, v in votes.items() if not v['approve']}


async def review_wave(engine, harness, record, wave, save):
    """Recover started handoffs first; admit new role-isolated requests together."""
    from .engine import Harness
    run_id = record['run_id']

    async def send(item):
        role, req, expected, read = item
        await ask(engine, harness, record, role, req, expected, save, read=read)

    fresh = []
    for item in wave:
        role, req, expected, _ = item
        if any(req[k] != v for k, v in expected.items()):
            raise ValueError('Host review request changed')
        key = Harness.decision_key(run_id, role, req['purpose'], req['instruction'], req['context'], req['schema'])
        progress = engine.store.get('handoff_progress', key, {})
        if ('answer' in req or engine.store.get('decisions', key)
                or progress.get('finalization', {}).get('status') in ('dispatching', 'blocked', 'response_saved')
                or progress.get('normal_request') or progress.get('pending_tool_calls')):
            await send(item)
        else:
            fresh.append(item)
    if len(fresh) < 2:
        for item in fresh:
            await send(item)
        return
    save()
    if isinstance(harness, Harness):
        prepared = []
        for role, req, _, _ in fresh:
            harness._handoff(run_id, role, req['purpose'], req['instruction'], req['context'], req['schema'])
            prepared.append(await harness.next_request_async(run_id, role, req['purpose'], req['instruction'],
                                                            req['context'], req['schema']))
        if hasattr(harness.intelligence, 'preflight_batch'):
            await harness.intelligence.prepare_context_batch(run_id, prepared)
            await engine._read_consensus_check(run_id, harness.intelligence.preflight_batch, run_id, prepared)
    engine.store.event(run_id, 'review.parallel_wave', '已按独立角色并行审查当前宿主候选。',
                       {'roles': [item[0] for item in fresh], 'purposes': [item[1]['purpose'] for item in fresh]})
    jobs = [asyncio.create_task(send(item)) for item in fresh]
    try:
        # Keep successful sibling receipts if one role fails. No next wave starts.
        results = await gather_owned(*jobs, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
    finally:
        for job in jobs:
            if not job.done():
                job.cancel()
        await gather_owned(*jobs, return_exceptions=True)


async def evaluate(engine, harness, record, save):
    from .bounded_host_repair import collect, checksum
    from .engine import ResearchBlocked
    context = material(record)
    if record.get('compact_review_protocol') == 1:
        record['compact_material'] = {'sha256': sha(context), 'body': copy.deepcopy(context),
                                      'candidate_sha256': record['delivery']['all_files_sha256']}
    prior_triage = record.get('acceptance', {}).get('triage', {})
    if any(any(req[k] != v for k,v in request(record, role, 'triage', context, existing=req).items())
           for role,req in prior_triage.items()):
        # Additive recovery for a prior controller that only listed the latest
        # delta. Paid requests stay immutable and grant no broader coverage.
        record.setdefault('acceptance_archives', []).append(copy.deepcopy(record['acceptance']))
        record.pop('acceptance')
        record.pop('installation_reviewed', None)
        save()
    record['status'] = 'reviewing_delivery'; save()
    triage = await votes(engine, harness, record, 'triage', context, save)
    if rejected(triage):
        return {'stage': 'delivery_completeness', 'feedback': rejected(triage)}
    root = Path(record['job']['workspace'])
    sections = []
    for file in context['changes']:
        text = (root / file['path']).read_text(encoding='utf-8')
        sections += packets._document(file['path'], text)
    groups, current, size = [], [], 0
    for section in sections:
        if current and (size + len(section['text']) > packets.PACKET_CHARS or len(current) >= 16):
            groups.append(current); current, size = [], 0
        current.append(section); size += len(section['text'])
    if current:
        groups.append(current)
    code_votes = {}
    from .host_review_compaction import ReadReuse, normalize
    reuse = ReadReuse(engine, record, context) if record.get('compact_review_protocol') == 1 else None
    def assignments(role):
        state = record['acceptance'].setdefault('source_reads', {}).setdefault(role, {})
        observations = []
        for i, group in enumerate(groups):
            if reuse and str(i) not in state:
                inherited = reuse.group(role, group)
                previous_reuse = record['acceptance'].get('source_reuse', {}).get(role, {}).get(str(i))
                if previous_reuse is not None and previous_reuse != inherited:
                    raise ValueError('Frozen same-role read reuse no longer verifies')
                if inherited:
                    ledger = record['acceptance'].setdefault('source_reuse', {}).setdefault(role, {})
                    if str(i) in ledger and ledger[str(i)] != inherited:
                        raise ValueError('Frozen same-role read reuse changed')
                    if str(i) not in ledger:
                        ledger[str(i)] = inherited
                        save()
                    observations += inherited['observations']
                    continue
            expected = request(record, role, 'read', {'sections': group, 'review_scope': context}, read=True, existing=state.get(str(i)))
            req = state.setdefault(str(i), expected)
            yield role, req, expected, True
            from .host_read_citations import annotations
            observations += annotations(req)
        expected = request(record, role, 'code', {'review_scope': context, 'same_role_observations': observations},
                           existing=record['acceptance'].get('code', {}).get(role))
        req = record['acceptance'].setdefault('code', {}).setdefault(role, expected)
        yield role, req, expected, False
        code_votes[role] = normalize(record, req)
    parallel = engine.store.get('runs', record['run_id'])['state'].get('parallel_host_code_review') is True
    if parallel:
        for wave in zip_longest(*(assignments(role) for role in packets.ROLES)):
            await review_wave(engine, harness, record, [item for item in wave if item is not None], save)
    else:
        for role in packets.ROLES:
            for _, req, expected, read in assignments(role):
                await ask(engine, harness, record, role, req, expected, save, read=read)
    if rejected(code_votes):
        return {'stage': 'code_review', 'feedback': rejected(code_votes)}
    if 'checks' not in record:
        if record.get('check_claim'):
            raise ResearchBlocked('原宿主检查已派发但缺少已保存终态；先核对原检查，不自动重跑。')
        record['status'] = 'checking'; save()
        await engine._read_consensus_check(record['run_id'], engine._execution_gate, record['run_id'])
        selected = [f['path'] for f in context['changes']
                    if f['path'].startswith('tests/test_') and f['path'].endswith('.py')]
        if not selected:
            return {'stage': 'check_contract', 'feedback': {'host': ['交付可执行的零模型CPU测试；不能只交测试命令或无断言说明。']}}
        if collect(record) != record['delivery']:
            raise ValueError('Reviewed candidate changed before checks')
        from .lab import ExperimentRunner
        from .storage import Store
        from importlib.util import find_spec
        if any(find_spec(name) is None for name in ('pytest', 'pytest_asyncio')):
            raise ResearchBlocked('宿主现有Python缺少pytest或pytest_asyncio；不自动安装，也不把环境缺项交给模型改代码。')
        dependency_paths = [p for p in sys.path if p and 'site-packages' in Path(p).parts]
        from .host_check_inputs import test_root
        with tempfile.TemporaryDirectory(prefix='autoresearch-host-check-') as tmp:
            checked_root, extra_data = test_root(root, record['delivery']['files'], Path(tmp))
            code = ('import os,sys,json,xml.etree.ElementTree as ET\n'
                'os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"]="1"\n'
                'os.environ["AUTORESEARCH_DATA_DIR"]=os.path.abspath("isolated-app-data")\n'
                'sys.path[:0]=' + repr([str(checked_root), *dependency_paths]) + '\n'
                'import pytest\n'
                'args=' + repr(['-q', '-p', 'no:cacheprovider', '-p', 'pytest_asyncio.plugin',
                    '--asyncio-mode=auto', '--disable-warnings', '--junitxml=host-tests.xml',
                    *[str(checked_root / p) for p in selected]]) + '\n'
                'rc=pytest.main(args)\n'
                'tree=ET.parse("host-tests.xml");cases=tree.findall(".//testcase")\n'
                'assert rc==0 and cases and not any(c.find("failure") is not None or c.find("error") is not None for c in cases)\n'
                'passed=sum(c.find("skipped") is None for c in cases);assert passed>0\n'
                'print(json.dumps({"metrics":{"offline_tests_passed":passed},"provenance":"isolated_host_repair_checks_no_model_updates"}))\n')
            store = Store(Path(tmp) / 'checks.sqlite3')
            runner = ExperimentRunner(store, tmp)
            ceilings = {'cpu_cores': 1, 'memory_mb': 2048, 'disk_mb': 256,
                        'timeout_seconds': 1800, 'cpu_seconds': 1800}
            limits = {'execution_mode': 'local', **{k: min(v, context['limits'][k]) for k,v in ceilings.items()}, 'gpu_allowed': False,
                      'python_executable': sys.executable, 'required_packages': []}
            from .lab import assess_resources
            assessment = assess_resources({'cpu_cores': limits['cpu_cores'], 'ram_gb': limits['memory_mb']/1024,
                'disk_gb': limits['disk_mb']/1024, 'gpu_required': False, 'required_packages': [],
                'timeout_seconds': limits['timeout_seconds'], 'cpu_seconds': limits['cpu_seconds']},
                await engine.execution_hardware(record['run_id']))
            if not assessment['approved']:
                raise ResearchBlocked('宿主检查资源准入未通过：' + str(assessment.get('reasons')))
            record['check_claim'] = {'candidate_sha256': record['delivery']['all_files_sha256'],
                                     'supplemental_package_data': extra_data,
                                     'code_sha256': checksum(code.encode()), 'started_at': now()}
            save()
            result = await runner.run('host-repair', code, {'phase': 'code_check'}, 'checks', limits,
                                      engine.cancellations.get(record['run_id']))
            record['checks'] = {'candidate_sha256': record['delivery']['all_files_sha256'],
                                'supplemental_package_data': extra_data,
                                'limits': limits, 'result': copy.deepcopy(result), 'selected_tests': selected,
                                'scope': 'Isolated engineering tests only; no research experiments or scientific approval'}
            report = Path(result['artifact_path']) / 'host-tests.xml' if result.get('artifact_path') else None
            if report and report.resolve().is_relative_to(Path(tmp).resolve()) and report.is_file() and report.stat().st_size <= 1024 * 1024:
                record['checks']['junit_xml'] = report.read_text(encoding='utf-8')
            save()
    if record['checks']['candidate_sha256'] != record['delivery']['all_files_sha256']:
        raise ValueError('Host check receipt belongs to another candidate')
    if collect(record) != record['delivery']:
        raise ValueError('Host candidate changed while checks were running')
    if record['checks']['result']['status'] != 'completed':
        if record.get('location_protocol') == 1:
            from .host_check_inputs import failed_cases
            feedback = failed_cases(record['checks'], context['location_catalog'])
            if not feedback:
                raise ResearchBlocked('宿主 CPU 检查失败，回执未定位到具体用例；请核对检查环境和原始 stdout/stderr：'
                                      + str(record['checks']['result'].get('error', ''))[:400])
            return {'stage': 'cpu_checks', 'feedback': {'host': feedback}}
        return {'stage': 'cpu_checks', 'feedback': {'host': [record['checks']['result'].get('stderr', '')[-6000:]
            or str(record['checks']['result'].get('error', '检查未完成'))]}}
    judgments = await votes(engine, harness, record, 'checks', {'review_scope': context, 'checks': record['checks']}, save)
    if rejected(judgments):
        return {'stage': 'check_review', 'feedback': rejected(judgments)}
    record.update(status='ready_for_installation', installation_reviewed=True)
    save()
    return None


def revise(engine, record, feedback, save):
    from .bounded_host_repair import checksum, ALLOWED, INSTRUCTION
    from .rapid_experiment_revision import _write_files
    from .engine import ResearchBlocked
    history = record.setdefault('revision_history', [])
    maximum = engine.store.get('runs', record['run_id'])['config'].get('codex_max_revisions', 3)
    if maximum is not None and len(history) + 1 >= maximum:
        record.update(status='revision_limit', last_feedback=feedback); save()
        raise ResearchBlocked('宿主修订已达到原配置的交付次数上限；保留每版候选、独立评审和失败证据，未安装。')
    root = Path(record['job']['workspace'])
    old = {k: copy.deepcopy(v) for k,v in record.items() if k not in ('revision_history', 'id', 'run_id')}
    def code_files(delivery):
        return {k: v for k, v in delivery['files'].items() if k.endswith('.py')}
    if history and code_files(record['delivery']) == code_files(history[-1]['delivery']):
        record.update(status='no_repair_progress', last_feedback=feedback); save()
        raise ResearchBlocked('工程修订未产生新交付，保留具体否决与原回执，不重复相同任务。')
    files = {name: (root / name).read_bytes() for name in record['delivery']['files']}
    from .host_check_inputs import add_package_data
    add_package_data(files)
    order = json.loads(files['work_order.json'])
    order.update(revision=len(history)+1, independent_review_feedback=feedback,
                 previous_delivery_sha256=record['delivery']['all_files_sha256'])
    editable = set(record.get('revision_editable', [])) | {f['path'] for f in record['delivery']['changed_files']}
    order['allowed_files'] = sorted(set(ALLOWED) | editable)
    if record.get('location_protocol') == 1:
        from .host_repair_locations import bind, render, verify_tasks
        catalog = material(record)['location_catalog']
        tasks = []
        for role, changes in feedback['feedback'].items():
            if role == 'host' and feedback['stage'] != 'cpu_checks':
                # Host check failures are local receipts, not reviewer prose.
                # Attach an exact delivered test location (or the new-test slot).
                location = next((r for r in catalog if r['path'].startswith('tests/') and r['symbol'] != '<module>'), None)
                if location is None:
                    raise ValueError('Host check failure has no actionable test location')
                changes = [{'location_id': location['location_id'], 'problem': str(change),
                    'change': '按本条失败回执修复定位测试及对应实现；保留原断言，补齐缺失测试，禁止将跳过或永久阻断算通过。',
                    'acceptance': '原失败场景与累计已审测试均通过，保存真实测试回执；不重跑研究训练。'} for change in changes]
            tasks += bind(changes, catalog, role=role)
        try:
            verify_tasks(tasks, {name: checksum(raw) for name,raw in files.items()}, set(order['allowed_files']))
        except ValueError as exc:
            record.update(status='scope_conflict', last_feedback=feedback, scope_error=str(exc))
            save()
            raise ResearchBlocked('修改单存在权限或源码定位冲突，已保留具体要求；不在相同范围内重复派发：' + str(exc)) from exc
        prior_tasks = order.get('edit_tasks', [])
        prior = {(t['location']['path'], t['location']['symbol']): t['location'] for t in prior_tasks}
        if tasks and all((t['location']['path'], t['location']['symbol']) in prior and
                         t['location'].get('symbol_sha256') == prior[t['location']['path'], t['location']['symbol']].get('symbol_sha256')
                         for t in tasks):
            record.update(status='no_repair_progress', last_feedback=feedback,
                          scope_error='All still-required edit targets are unchanged')
            save()
            raise ResearchBlocked('仍被否决的具体修改位置均未改变；保留位置与反馈，不用其他文件变化冒充修复进展。')
        order['edit_tasks'] = tasks
        order['location_protocol'] = 1
        order['location_catalog'] = catalog
        files['EDIT_TASKS.md'] = render(tasks).encode('utf-8')
        order['edit_tasks_path'] = 'EDIT_TASKS.md'
    instruction = INSTRUCTION + ('\n本轮继续修复上一版，不从原版重写。上一版源码和检查已放入本目录；独立评审意见见work_order.independent_review_feedback。'
        '不允许用永久关闭全部正常路径代替完整维修。保留已有正确修复和夹具，完成剩余可运行正常路径、失败关闭及持久化协议。'
        '若原资源合同确有不可同时满足的约束，明确给出冲突条款、精确实现证据及最小澄清方案，不能自行放宽或伪造通过。')
    if record.get('compact_review_protocol') == 1:
        from .host_review_compaction import compact_work_order
        compact_work_order(order, files)
    files['work_order.json'] = json.dumps(order, ensure_ascii=False, indent=2).encode()
    files['AGENTS.md'] = instruction.encode()
    target = root.parent / ('revision-' + str(len(history)+1))
    _write_files(target, files)
    history.append(old)
    for key in ('delivery', 'acceptance', 'checks', 'check_claim', 'delivered_at', 'installation_reviewed', 'compact_material'):
        record.pop(key, None)
    record.update(status='implementing', revision_editable=sorted(editable),
                  input_manifest={name: checksum(raw) for name,raw in files.items()},
                  job={'operation_id': uid(), 'workspace': str(target), 'instruction': instruction, 'created_at': now()})
    save()
    engine.store.event(record['run_id'], 'diagnostic.host_repair_revision_requested',
        '独立评审未通过，已带具体意见自动派发下一版宿主修订；旧交付和回执保留。',
        {'repair_id': record['id'], 'revision': len(history), 'stage': feedback['stage']})
