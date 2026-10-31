"""Scoped, evidence-bound code amendments without restarting research design.

The original approvals remain immutable. A separate amendment authorizes only
its exact patch, bounded CPU assertions and a new paired validation pilot.
"""
from __future__ import annotations

import ast
import copy
import hashlib
from pathlib import Path

from .prose_schema import validate

from .experiment_workflow import encoded, sha
from .storage import now
from .training_adjustment import obj, _group
from .delivery_files import io_path

COLLECTION = 'rapid_experiment_revisions'
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 600}
HASH = {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}
PATCH = obj({'evidence_sha256': HASH, 'decision_sha256': HASH,
    'summary': TEXT,
    'edits': {'type': 'array', 'minItems': 1, 'maxItems': 6, 'items': obj({
        'path': {'type': 'string', 'minLength': 1, 'maxLength': 180},
        'before_sha256': HASH, 'change_index': {'type': 'integer', 'minimum': 0, 'maximum': 2},
        'old': {'type': 'string', 'minLength': 1},
        'new': {'type': 'string', 'minLength': 1}})},
    'checks': {'type': 'string', 'minLength': 1},
    'check_rationale': TEXT})
VOTE = obj({'amendment_sha256': HASH, 'candidate_sha256': HASH, 'checks_sha256': HASH,
    'approve': {'type': 'boolean'}, 'rationale': TEXT,
    'issues': {'type': 'array', 'items': TEXT, 'maxItems': 4},
    'read_receipts': {'type': 'array', 'items': HASH, 'minItems': 1, 'maxItems': 24, 'uniqueItems': True}})
WRITE = '''只实现本轮已获三方同意的局部修改单，不重新立项、查文献、重写整份架构或 SPEC。
先用 experiments.inspect_module 按需读取原文件，返回 SHA 绑定的精确文本替换；每项对应 changes 的 change_index。
同一文件可提交多处互不重叠的替换；每处 old 和 before_sha256 都绑定修改前的同一份原文件，分别注明对应的 change_index。仅修改已有运行源文件，不更改数据划分、baseline 定义、评估、验收阈值、预算和依赖。
保持原实验参数接口；新增诊断只能在已有冻结配置下采集证据，不增加搜索范围。
checks 是短小的 Python CPU 断言，验证修改模块的实际行为和关键不变量。禁止训练完整模型、下载或使用 GPU。
检查入口工作目录是隔离运行目录，src 在 sys.path；原 experiment.py 可通过 candidate_entry.py 导入。
检查入口的 sys.argv[1] 是 params.json 文件路径；需要参数时读取该文件再解析 JSON。
必须有实际 assert；不得伪造指标/测试结果。正常结束的标记由宿主添加。给出断言与问题的对应理由。
缺少局部可行修复时不能用无关修改冒充完成。完整源码和历史日志按需读取，不复述到回答。'''
REVIEW = '''独立审查这个局部架构/代码修订是否准确实现已批准修改单。
复用已有方案，只审查差异、受影响模块、baseline 公平性、数据泄漏、资源与断言充分性。
必须用 experiments.inspect_module 读取 candidate 中每个改动区间的 source，返回真实 read_receipts。
结合 rapid_revision 中的修改前后内容及 checks 核对因果假设和实现；需要上下游源码时按需读取。
批准只授权本次 CPU 检查和通过后的最小成对验证批次，不代表科学成功、完整 SPEC 重新验收或最终测试通过。
未解决问题列出具体 issues；不展开从零讨论，不把候选代码当成已经运行。'''


def _fail(message):
    from .engine import ResearchBlocked
    raise ResearchBlocked(message)


def _bytes_sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _bundle(engine, run_id, program):
    if program.get('rapid_revision_id'):
        return validate_program(engine, run_id, program)
    if program.get('dataset_runtime_id'):
        from .dataset_experiments import validate_program as configured
        return configured(engine, run_id, program)
    if program.get('implementation_backend') == 'local_codex':
        from .codex_workflow import validate_program as original
        return original(engine, run_id, program)
    return None


def latest(engine, run_id):
    """One terminal amendment, never accumulated review/source history."""
    rows = engine.store.list(COLLECTION, run_id)
    rows = [r for r in rows if r.get('cycle') == engine.cycle(run_id)
            and r.get('status') in {'activated', 'rejected', 'check_failed', 'invalid', 'diagnostic_checked'}]
    # An older work item may finish after recovery. Its newly measured failure
    # must take precedence over later-created, never-dispatched placeholders.
    return max(rows, key=lambda r: (r.get('completed_at') or r['created_at'], r['id']), default=None)


def count(engine, run_id):
    return sum(r.get('cycle') == engine.cycle(run_id) for r in engine.store.list(COLLECTION, run_id))


def _budget(engine, run_id, needed):
    with engine.store._read_db() as db:
        attempts = db.execute("SELECT coalesce(sum(coalesce(json_extract(data,'$.attempt'),1)),0) "
            "FROM records WHERE collection='experiments' AND json_extract(data,'$.run_id')=? "
            "AND coalesce(json_extract(data,'$.research_cycle'),1)=?", (run_id, engine.cycle(run_id))).fetchone()[0]
    from .rapid_continuation import trial_limit
    limit = trial_limit(engine.store, run_id)
    if limit is not None and attempts + needed > limit:
        _fail('快速修订剩余预算不足以完成 CPU 检查和同种子 baseline 对照；未扩大预算。')


def _base_unchanged(engine, run_id, record):
    engine.check_active(run_id)
    _validate_base(engine, run_id, record)


def _validate_base(engine, run_id, record):
    """Read-only identity checks, also usable during a stopped recovery."""
    from .experiment_decisions import _binding, _safe_rows
    from .experiment_workflow import validation_records
    if _binding(engine, run_id)[1] != record['binding']:
        _fail('快速修订期间代码、计划或运行配置发生变化。')
    rows = _safe_rows([r for r in validation_records(engine.store, run_id)
        if (r.get('research_cycle') or 1) == engine.cycle(run_id) and not r.get('superseded')])
    if sha(rows) != record['validation_rows_sha256']:
        _fail('快速修订的原始验证证据发生变化。')


def _write_files(root, files):
    """Idempotent immutable writes; reject stale or linked destinations."""
    from .code_bundle import relative_file
    for name, source in files.items():
        relative_file(name)
        path = io_path(root / name)
        for part in (path, *path.parents):
            if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
                _fail('快速修订目录不可包含符号链接或目录联接。')
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = source if isinstance(source, bytes) else source.encode('utf-8')
        if path.exists():
            if path.read_bytes() != raw:
                _fail('快速修订不可变快照已有不同内容。')
        else:
            with path.open('xb') as stream:
                stream.write(raw)
    from .code_bundle import digest
    manifest = sorted([{'path': n, 'bytes': len(v if isinstance(v, bytes) else v.encode()),
        'sha256': hashlib.sha256(v if isinstance(v, bytes) else v.encode()).hexdigest()}
        for n, v in files.items()], key=lambda r: r['path'])
    return {'version': 1, 'root': str(root.resolve()), 'files': manifest, 'sha256': digest(manifest)}


def _apply_edits(files, edits, change_count, new_files=(), *, allow_new=False,
                 inherited_paths=(), covered_changes=()):
    """Apply disjoint edits against one immutable base, preserving review ranges."""
    grouped = {}
    for edit in edits:
        path = edit['path']
        if path not in files or not path.endswith(('.py', '.json', '.yaml', '.toml')):
            _fail('快速补丁只允许修改已存在的运行源文件。')
        if not (edit['change_index'] is None and path in inherited_paths) and not (
                type(edit['change_index']) is int and 0 <= edit['change_index'] < change_count):
            _fail('代码修改没有对应本次批准修改项。')
        old = files[path].decode('utf-8')
        if (_bytes_sha(old) != edit['before_sha256'] or old.count(edit['old']) != 1
                or edit['old'] == edit['new']):
            _fail('补丁原文、SHA 或唯一位置不匹配；未修改候选文件。')
        start = old.index(edit['old'])
        grouped.setdefault(path, []).append((start, start + len(edit['old']), edit))
    if new_files and not allow_new:
        _fail('新增源码必须来自已批准的 Codex 修改任务。')
    if any(type(i) is not int or not 0 <= i < change_count for i in covered_changes):
        _fail('检查文件没有对应本次批准修改项。')
    if ({e['change_index'] for e in [*edits, *new_files] if e['change_index'] is not None}
            | set(covered_changes)) != set(range(change_count)):
        _fail('代码补丁没有覆盖本轮每一个已批准修改项。')
    result, ranges = dict(files), []
    for path, replacements in grouped.items():
        old, cursor, parts, local_ranges, length = files[path].decode('utf-8'), 0, [], [], 0
        for start, end, edit in sorted(replacements, key=lambda row: row[0]):
            if start < cursor:
                _fail('同一文件的补丁区间重叠；未修改候选文件。')
            prefix = old[cursor:start]
            parts.extend((prefix, edit['new']))
            length += len(prefix)
            local_ranges.append({'path': path, 'start': length, 'end': length + len(edit['new'])})
            length += len(edit['new'])
            cursor = end
        new = ''.join(parts) + old[cursor:]
        if path.endswith('.py'):
            compile(new, path, 'exec')
        if len(new.encode()) > 1024 * 1024:
            _fail('候选模块超过可独立读取验证的源码上限。')
        result[path] = new.encode('utf-8')
        ranges.extend({**r, 'sha256': _bytes_sha(new)} for r in local_ranges)
    from .code_bundle import relative_file
    for item in new_files:
        path = item['path']; relative_file(path)
        if not (item['change_index'] is None and path in inherited_paths) and not (
                type(item['change_index']) is int and 0 <= item['change_index'] < change_count):
            _fail('新增源码没有对应批准修改项或已绑定的候选来源。')
        if path in result or not path.endswith(('.py', '.json', '.yaml', '.yml', '.toml')):
            _fail('新增运行文件路径重复或类型不受支持。')
        source = item['source']
        if path.endswith('.py'): compile(source, path, 'exec')
        if len(source.encode()) > 1024*1024: _fail('新增运行文件超过大小上限。')
        result[path] = source.encode()
        ranges.append({'path': path, 'start': 0, 'end': len(source), 'sha256': _bytes_sha(source)})
    return result, ranges


def build_candidate(engine, run_id, record, patch):
    patch_schema = copy.deepcopy(PATCH)
    if record.get('native_codex_protocol') == 1:
        patch_schema['properties']['edits']['maxItems'] = 64
        patch_schema['properties']['new_files'] = {'type':'array','maxItems':32,'items':obj({
            'path':{'type':'string','minLength':1,'maxLength':180},
            'change_index':{'type':'integer','minimum':0,'maximum':2},
            'source':{'type':'string','minLength':1}})}
    modern = record.get('native_delivery_protocol') == 2
    if modern and record.get('native_codex_protocol') != 1:
        validate(patch, PATCH)  # Legacy requests cannot acquire the new delivery protocol.
        _fail('检查辅助文件协议需要已批准的本机 Codex 工作项。')
    if modern:
        patch_schema['properties']['edits']['minItems'] = 0
        for field in ('edits', 'new_files'):
            patch_schema['properties'][field]['items']['properties']['change_index'] = {
                'type': ['integer', 'null'], 'minimum': 0, 'maximum': 2}
        patch_schema['properties'].update(
            delivery_context_files={'type': 'object', 'additionalProperties': False,
                'properties': {n: {'type': 'string'} for n in
                    ('work_order.json', '_delivery/changes.json', '_delivery/rationale.txt')},
                'required': ['work_order.json', '_delivery/changes.json', '_delivery/rationale.txt']},
            repair_source_sha256={'type': ['string', 'null']},
            check_change_indices={'type': 'array', 'items': {'type': 'integer'}, 'uniqueItems': True},
            retained_runtime={'type': 'array', 'items': obj({'path': {'type': 'string'},
                'change_index': {'type': 'integer'}, 'sha256': HASH})},
            check_files={'type': 'array', 'maxItems': 64, 'items': obj({
                'path': {'type': 'string'}, 'source': {'type': 'string'},
                'change_index': {'type': ['integer', 'null']}})})
        patch_schema['required'] += ['repair_source_sha256', 'check_change_indices', 'check_files', 'retained_runtime']
    validate(patch, patch_schema)
    if (patch['evidence_sha256'] != record['evidence']['sha256']
            or patch['decision_sha256'] != record['decision_sha256']):
        _fail('快速代码补丁未绑定本次批准的修改单和证据。')
    base = record['base_program']
    bundle = _bundle(engine, run_id, base)
    if bundle:
        from .code_bundle import validate as check
        check(bundle, base['code'])
        files = {r['path']: io_path(Path(bundle['root']) / r['path']).read_bytes() for r in bundle['files']}
    else:
        files = {'experiment.py': base['code'].encode()}
    control_files = copy.deepcopy(files)
    inherited = set()
    if modern:
        from .candidate_repair_source import inherited_paths
        inherited = inherited_paths(engine, run_id, record, patch)
    files, ranges = _apply_edits(files, patch['edits'], len(record['decision']['changes']),
                                patch.get('new_files', []), allow_new=record.get('native_codex_protocol') == 1,
                                inherited_paths=inherited, covered_changes=[*patch.get('check_change_indices', []),
                                    *[r['change_index'] for r in patch.get('retained_runtime', [])]])
    if modern:
        for retained in patch['retained_runtime']:
            if (retained['path'] not in files
                    or hashlib.sha256(files[retained['path']]).hexdigest() != retained['sha256']):
                _fail('候选最终文件与声明保留的实现不一致。')
    supports = {}
    if modern:
        from .code_bundle import relative_file
        from .candidate_repair_source import file_limit, SUPPORT_TOTAL_BYTES
        for item in patch['check_files']:
            name = item['path']; relative_file(name)
            if (not name.startswith('_delivery/') or name in supports
                    or name in {'_delivery/checks.py', '_delivery/changes.json', '_delivery/rationale.txt'}
                    or not name.endswith(('.py', '.json', '.yaml', '.yml', '.txt', '.diff'))
                    or len(item['source'].encode()) > file_limit(name)):
                _fail('检查辅助文件路径、类型或大小无效。')
            index = item['change_index']
            if index is not None and not 0 <= index < len(record['decision']['changes']):
                _fail('检查辅助文件没有对应修改项。')
            if name.endswith('.py'):
                compile(item['source'], name, 'exec')
            supports[name] = item['source'].encode()
            if sum(map(len, supports.values())) > SUPPORT_TOTAL_BYTES:
                _fail('检查辅助文件总大小超过上限。')
            if name.endswith(('.py', '.json', '.yaml', '.yml')):
                ranges.append({'path': name, 'start': 0, 'end': len(item['source']),
                               'sha256': _bytes_sha(item['source']), 'scope': 'check_support'})
        ranges.append({'path': '_delivery/checks.py', 'start': 0, 'end': len(patch['checks']),
                       'sha256': _bytes_sha(patch['checks']), 'scope': 'check_entry'})
    tree = ast.parse(patch['checks'])
    if not any(isinstance(n, ast.Assert) for n in ast.walk(tree)):
        _fail('快速检查必须包含实际行为断言。')
    root = engine.run_dir(run_id).resolve() / 'rapid-revisions' / sha(patch)
    bundle = _write_files(root / 'candidate', files)
    code = files['experiment.py'].decode('utf-8')
    program = {**base, 'code': code, 'code_sha256': _bytes_sha(code),
        'revision': base.get('revision', 0) + 1, 'rapid_revision_id': record['id'],
        'reviews': [], 'base_approval_reference': {k: base.get(k) for k in
            ('delivery_id', 'delivery_hash', 'spec_hash', 'implementation_document_sha256', 'rapid_revision_id')},
        'acceptance_scope': 'Reviewed scoped amendment; paired validation still required',
        'scientific_approval': False}
    if record.get('yaml_migration') or base.get('hyperparameter_yaml_protocol') == 1:
        from .yaml_migration import inspect_contract
        program['hyperparameter_contract'] = inspect_contract(bundle)
        program['hyperparameter_yaml_protocol'] = 1
        program['dependencies'] = list(dict.fromkeys([*base.get('dependencies', []), 'PyYAML>=6,<7']))
    from .experiment_analysis import freeze_sources
    compatibility_files = {}
    compatibility_prefix = ''
    if record.get('check_runtime_compatibility') == 1:
        from . import pathlib_check_runtime
        from .rapid_pathlib_recovery import prefix
        raw = Path(pathlib_check_runtime.__file__).read_bytes()
        compatibility_files['host_pathlib_compat.py'] = raw
        compatibility_prefix = prefix(raw)
        ranges.append({'path':'host_pathlib_compat.py','start':0,'end':len(raw.decode()),
                       'sha256':hashlib.sha256(raw).hexdigest(),'scope':'host_check_compatibility'})
    delivery_context = {n: v.encode() for n, v in patch.get('delivery_context_files', {}).items()}
    review_bundle = (_write_files(root / 'review', {**files, **supports, **compatibility_files,
        **delivery_context,
        **{'_control/' + n:raw for n,raw in control_files.items()},
        '_delivery/checks.py': patch['checks'].encode()}) if modern else bundle)
    sources = freeze_sources(engine, run_id, program, review_bundle, delivery_support=modern)
    if any(isinstance(n, ast.Name) and n.id.startswith('__rapid_') for n in ast.walk(tree)):
        _fail('检查源码不能覆盖宿主断言计数器。')
    # An uncalled test function or swallowed failed assertion is not a pass.
    from .check_instrumentation import instrument
    instrumented, assertion_count = instrument(patch['checks'], '__rapid_passed')
    from .check_instrumentation import split_future_prefix
    future, instrumented = split_future_prefix(instrumented)
    # Check scripts may verify the exact authored source and delivery hashes.
    # Keep that complete layout intact below the host's instrumented launcher.
    check_preamble = ''
    if delivery_context:
        if any(len(raw) > 1024 * 1024 for raw in delivery_context.values()):
            _fail('交付上下文文件过大。')
        check_preamble = ('import os\n'
            'sys.argv[1] = str(Path(sys.argv[1]).resolve())\n'
            '__rapid_cwd = Path.cwd()\n'
            'os.chdir(Path.cwd() / "_delivery_snapshot")\n'
            'sys.path.insert(0, str(Path.cwd()))\n')
    checks = (future + compatibility_prefix + 'import sys, json\nfrom pathlib import Path\n'
        + check_preamble + 'sys.path.insert(0, str(Path.cwd() / "src"))\n__rapid_passed = set()\n' + instrumented
        + ('\nos.chdir(__rapid_cwd)\n' if delivery_context else '')
        + f'\nassert len(__rapid_passed) == {assertion_count}, "Not every declared assertion was exercised successfully"\n'
        + 'print(json.dumps({"metrics": {"rapid_check_passed": 1.0, "rapid_assertions": len(__rapid_passed)}}))\n')
    if 'candidate_entry.py' in files:
        _fail('候选项目已占用快速检查入口名称 candidate_entry.py。')
    check_files = {**files, **supports, **compatibility_files,
        **({'_control/' + n:raw for n,raw in control_files.items()} if modern else {}),
        'candidate_entry.py': files['experiment.py'], 'experiment.py': checks.encode()}
    if delivery_context:
        snapshot = {**files, **supports, **delivery_context,
            **{'_control/' + n:raw for n,raw in control_files.items()},
            '_delivery/checks.py': patch['checks'].encode(),
            'candidate_entry.py': files['experiment.py']}
        check_files.update({'_delivery_snapshot/' + n: raw for n,raw in snapshot.items()})
    check_identity = sha({'amendment': sha(patch), 'checks': _bytes_sha(checks)})
    # Leave room for relative source paths on Windows inside the preserved layout.
    check_root = engine.run_dir(run_id).resolve() / 'rapid-checks' / (
        check_identity[:20] if delivery_context else check_identity)
    check_bundle = _write_files(check_root, check_files)
    control_bundle = _write_files(root / 'control', control_files) if modern else None
    candidate = {'program': program, 'bundle': bundle, 'code_sources': sources,
        **({'check_workspace_layout':'rapid-v1'} if compatibility_files else {}),
        **({'review_bundle': review_bundle, 'repair_source': copy.deepcopy(record.get('candidate_repair_source')),
            'control_bundle': control_bundle,
            'approval_inherited': False} if modern else {}),
        'changed_ranges': ranges, 'check_code': checks, 'check_bundle': check_bundle,
        'checks_sha256': _bytes_sha(checks), 'assertion_count': assertion_count, 'amendment_sha256': sha(patch)}
    from .rapid_check_budget import apply_future
    return apply_future(engine,run_id,record,candidate)


def _review_context(engine, run_id, record):
    from .experiment_decisions import _archive
    from .performance_policy import context_fields
    from .rapid_host_check_repair import review_scope
    candidate, patch = record['candidate'], record['patch']
    summary = {'binding': record['binding'], 'code_sources': candidate['code_sources'],
        'candidate_not_executed': True, 'rapid_revision': {'patch': patch, 'decision': record['decision']},
        'original_validation_evidence': record['evidence']}
    ref = _archive(engine, run_id, summary)
    from .research_anchor import read as read_anchor
    context = {'training_context_protocol': 1, 'experiment_workflow_protocol': 1,
        **({'research_anchor': read_anchor(engine, run_id, record['research_anchor'])}
           if record.get('research_anchor') else {}),
        **({'native_codex_protocol': 1} if record.get('native_codex_protocol') == 1 else {}),
        **context_fields(engine.store, run_id, binding=record['binding']),
        'research_cycle': record['cycle'], 'evidence_sha256': ref['sha256'], 'validation_evidence': ref,
        'experiment_decision': {'decision_id': record['decision_id']},
        'rapid_revision': {'id': record['id'], 'amendment_sha256': candidate['amendment_sha256'],
            **({'host_check_repair': {**{k: record['host_check_repair'][k] for k in
                ('original', 'reason', 'failed_trial_id')},
                'scope': review_scope(record)}}
                if record.get('host_check_repair') else {}),
            **({'source_read_recovery': record['source_read_recovery']} if record.get('source_read_recovery') else {}),
            'candidate_sha256': candidate['bundle']['sha256'], 'checks_sha256': candidate['checks_sha256'],
            **({'check_execution_limits':copy.deepcopy(candidate['check_budget'])} if candidate.get('check_budget') else {}),
            'changes': record['decision']['changes'], 'hypothesis': record['decision']['hypothesis'],
            **({'architecture_review': copy.deepcopy(record['decision']['architecture_review'])}
                if record['decision'].get('architecture_review') else {}),
            **({'candidate_repair_source': copy.deepcopy(record['candidate_repair_source']),
                'retained_runtime': copy.deepcopy(patch.get('retained_runtime', [])),
                'inheritance_scope': 'Retained unapproved candidate runtime; all current reviews and CPU checks remain required'}
               if record.get('candidate_repair_source') else {}),
            **({'check_support_files': [{'path': x['path'], 'sha256': _bytes_sha(x['source'])}
                                       for x in patch.get('check_files', [])],
                'check_entry_path': '_delivery/checks.py', 'check_assets_are_runtime': False,
                'control_source_root': '_control',
                'control_bundle_sha256': candidate['control_bundle']['sha256'],
                'review_read_protocol': 2,
                'reading_guidance': '运行源码、检查辅助源码及_control/下的原生效对照均可通过inspect_module(snapshot=candidate)读取；大文件优先limit=6000，只补未读区间。宿主按实际范围保留阅读容量，覆盖不是批准。'}
               if record.get('native_delivery_protocol') == 2 else {}),
            **({'training_response': record['decision']['training_response']} if record['decision'].get('training_response') else {}),
            'verification': record['decision']['verification'], 'stop_condition': record['decision']['stop_condition'],
            'changed_ranges': candidate['changed_ranges'], 'diff_path': '/rapid_revision/patch/edits',
            'checks': candidate['check_code'], 'check_rationale': patch['check_rationale'],
            'scientific_approval': False}}
    from .rapid_review_plan import context as planned_context
    return planned_context(engine, run_id, record, context)


def review_receipts(engine, run_id, record, vote, context, *, purpose=None, instruction=None, schema=None,
                    require_complete=True):
    from .engine import Harness
    if record.get('structured_review'):
        context = record.get('review_contexts', {}).get(vote['role'], context)
    citation_correction = record.get('citation_corrections', {}).get(vote['role'])
    correction_handoff = None
    if citation_correction and {k:v for k,v in vote.items() if k!='role'} != citation_correction['body']['original_vote']:
        from .rapid_review_citations import verify
        correction_handoff = verify(engine,run_id,record,vote,context,citation_correction)
    clarification = record.get('review_clarifications', {}).get(vote['role']) if purpose is None else None
    if clarification:
        original = clarification['original_vote']
        prior = clarification['original_handoff']
        if ((engine.store.get('decisions', prior) or {}).get('result') != original
                or vote['read_receipts'] != original['read_receipts']):
            _fail('局部复核澄清缺少同角色原答复及原始源码回执。')
        key = Harness.decision_key(run_id, vote['role'], clarification['purpose'],
            clarification['instruction'], clarification['context'], clarification['schema'])
        if (engine.store.get('decisions', key) or {}).get('result') != {k:v for k,v in vote.items() if k != 'role'}:
            _fail('局部复核澄清没有对应的真实最终答复。')
        reads = review_receipts(engine, run_id, record, {'role':vote['role'], **original}, context,
            purpose=record.get('review_purpose') or 'rapid_review:' + record['candidate']['amendment_sha256'],
            require_complete=require_complete)
        return {**reads, 'clarification_handoff_id': key, 'approval_inherited': False}
    purpose = purpose or record.get('review_purpose') or 'rapid_review:' + record['candidate']['amendment_sha256']
    key = Harness.decision_key(run_id, vote['role'], purpose, instruction or REVIEW, context, schema or record.get('review_schema') or VOTE)
    owner = engine.store.get('handoffs', key) or {}
    frozen = (engine.store.get('handoff_inputs', key) or {}).get('context', {})
    if (owner.get('run_id') != run_id or owner.get('to_role') != vote['role']
            or frozen.get('validation_evidence') != context['validation_evidence']):
        _fail('局部代码复审缺少本角色的冻结源码读取任务。')
    valid, windows = set(), {}
    progress = engine.store.get('handoff_progress', key) or {}
    from .dataset_review_protocol import inherited_rows
    inherited = inherited_rows(engine,run_id,vote['role'],context)
    if record.get('structured_review'):
        from .rapid_review_plan import inherited_rows as previous_reads
        inherited += previous_reads(engine,run_id,vote['role'],context)
    inherited_refs = {r['result']['receipt_sha256'] for r in inherited}
    for row in [*inherited,*progress.get('tool_results', [])]:
        call, result = row.get('call') or {}, row.get('result') or {}
        identity = result.get('receipt_sha256')
        if (identity not in vote['read_receipts']
                or identity != sha({'call': call, 'result': {k: v for k, v in result.items() if k != 'receipt_sha256'}})):
            continue
        if call.get('name') == 'experiments.compare':
            from .experiment_decisions import _supplementary_analysis_read
            if _supplementary_analysis_read(engine, run_id, context, call, result):
                valid.add(identity)
            continue
        if call.get('name') == 'experiments.read':
            from .rapid_host_check_repair import authentic_failed_check_read
            if authentic_failed_check_read(engine, run_id, context, call, result):
                valid.add(identity)
            continue
        if (call.get('name') != 'experiments.inspect_module' or result.get('source_read') is not True
                or (identity not in inherited_refs and result.get('evidence_sha256') != context['evidence_sha256'])):
            continue
        valid.add(identity)
        windows.setdefault((result['path'], result['source_sha256']), []).append((result['offset'], result['end']))
    if valid != set(vote['read_receipts']):
        _fail('局部代码复审引用了未实际读取的源码回执。')
    from .dataset_review_protocol import remaining_ranges
    required = context['rapid_revision']['changed_ranges'] if record.get('structured_review') else record['candidate']['changed_ranges']
    missing = remaining_ranges(required, [
        {'path': path, 'source_sha256': digest, 'offset': left, 'end': right}
        for (path, digest), ranges in windows.items() for left, right in ranges])
    if require_complete and missing:
        _fail('三个角色必须分别读完全部改动区间，不能只读取目录或符号。'
              f' 当前 {vote["role"]} 的最终引用未覆盖 {len(missing)} 个区间；首项 {missing[0]["path"]}。')
    proof = {'role': vote['role'], 'handoff_id': key, 'receipt_sha256s': sorted(valid)}
    if correction_handoff:
        proof.update(citation_correction_handoff_id=correction_handoff,
                     original_vote_sha256=sha(citation_correction['body']['original_vote']))
    if not require_complete:
        proof.update(coverage_complete=not missing, missing_ranges=missing, authorization_eligible=False)
    return proof


def _recover_source_review(engine, run_id, record, votes, context):
    """One fresh review after a proven host selector failure, never an approval."""
    from .engine import Harness
    from .experiment_analysis import inspect_module
    if (record.get('structured_review') or record.get('source_read_recovery') or len(votes) != 3
            or {v.get('role') for v in votes} != {'director', 'engineer', 'scout'}
            or not any(v.get('approve') is False for v in votes)):
        return False
    purpose = 'rapid_review:' + record['candidate']['amendment_sha256']
    owners = []
    for vote in votes:
        key = Harness.decision_key(run_id, vote['role'], purpose, REVIEW, context, record.get('review_schema') or VOTE)
        owner = engine.store.get('handoffs', key) or {}
        frozen = (engine.store.get('handoff_inputs', key) or {}).get('context', {})
        progress = engine.store.get('handoff_progress', key) or {}
        if (owner.get('run_id') != run_id or owner.get('to_role') != vote['role']
                or frozen.get('validation_evidence') != context['validation_evidence']):
            return False
        failed = any(row.get('call', {}).get('name') == 'experiments.inspect_module'
            and row['call'].get('arguments', {}).get('snapshot') == 'candidate'
            and row['call']['arguments'].get('evidence_id') == context['validation_evidence']['id']
            and row['call']['arguments'].get('expected_sha256') == context['evidence_sha256']
            and row.get('result', {}).get('error') == 'No candidate source snapshot is available in this evidence'
            for row in progress.get('tool_results', []))
        if not failed and vote.get('approve') is False:
            return False
        owners.append(key)
    # Demonstrate the fixed selector against this exact immutable candidate.
    # These host probes are not agent reading receipts and cannot approve code.
    verified = []
    for changed in record['candidate']['changed_ranges']:
        offset = changed['start']
        while offset < changed['end']:
            result = inspect_module(engine, run_id, context, {
                'evidence_id': context['validation_evidence']['id'], 'expected_sha256': context['evidence_sha256'],
                'snapshot': 'candidate', 'path': changed['path'], 'view': 'source',
                'offset': offset, 'limit': min(6000, changed['end'] - offset)})
            if (result.get('source_sha256') != changed['sha256'] or not result.get('source_read')
                    or result.get('end', offset) <= offset):
                return False
            offset = result['end']
        verified.append({k: changed[k] for k in ('path', 'start', 'end', 'sha256')})
    recovery = {'amendment_sha256': record['candidate']['amendment_sha256'],
        'candidate_sha256': record['candidate']['bundle']['sha256'], 'previous_handoffs': owners,
        'host_verified_ranges': verified, 'recorded_at': now(),
        'instruction': '上次候选源码选择器在宿主端出错，现已修复并验证这些区间可读。补丁版本未变。请重新调用 experiments.inspect_module(snapshot="candidate", view="source") 读取改动区间，再独立判断；宿主探测不是你的读取回执或批准。'}
    record.setdefault('review_history', []).append({'purpose': purpose, 'votes': copy.deepcopy(votes),
        'reason': 'host_candidate_selector_unavailable', 'recorded_at': now()})
    record['source_read_recovery'] = recovery
    record['review_purpose'] = purpose + ':source-recovery:' + sha(recovery)
    engine.store.put(COLLECTION, record['id'], record)
    engine.store.event(run_id, 'experiment.review_source_recovered',
        '候选源码读取工具已修复，同版本补丁重新派发独立源码复核。',
        {'revision_id': record['id'], 'amendment_sha256': recovery['amendment_sha256']})
    return True


async def _clarify_review_votes(engine, harness, run_id, record, votes, context):
    """Ask the original reviewer once to resolve approve=true with issues."""
    from .engine import Harness
    clarified = []
    for vote in votes:
        if vote.get('approve') is not True or not vote.get('issues'):
            clarified.append(vote)
            continue
        role = vote['role']
        assignments = record.setdefault('review_clarifications', {})
        if role not in assignments:
            purpose = record.get('review_purpose') or 'rapid_review:' + record['candidate']['amendment_sha256']
            prior = Harness.decision_key(run_id, role, purpose, REVIEW, context, record.get('review_schema') or VOTE)
            original = {k:v for k,v in vote.items() if k != 'role'}
            if (engine.store.get('decisions', prior) or {}).get('result') != original:
                _fail('局部复核澄清必须绑定原审阅人的真实答复。')
            review_receipts(engine, run_id, record, vote, context, purpose=purpose)
            updated = copy.deepcopy(context)
            updated['rapid_revision']['approval_clarification'] = {
                'prior_vote': original, 'previous_handoff_id': prior, 'approval_inherited': False}
            strict = copy.deepcopy(record.get('review_schema') or VOTE)
            strict['allOf'] = [{'if':{'properties':{'approve':{'const':True}}},
                'then':{'properties':{'issues':{'maxItems':0}}}}]
            for field in ('amendment_sha256', 'candidate_sha256', 'checks_sha256', 'read_receipts'):
                strict['properties'][field] = {'const': original[field]}
            assignments[role] = {'purpose': purpose + ':approval-clarification', 'context': updated,
                'schema': strict, 'original_vote': original, 'original_handoff': prior,
                'instruction': REVIEW + '\n你上一答复 approve=true 且 issues 非空。请依据自己已读的同版本源码澄清：issues 只放阻止当前补丁执行的具体缺陷；有缺陷则 approve=false，否则 approve=true 且 issues=[]。已知局限、未运行状态及后续门禁写入 rationale。版本哈希和原始读取回执保持不变，不改变实验范围，无需重读或重做设计。'}
            engine.store.put(COLLECTION, record['id'], record)
        assignment = assignments[role]
        response = await harness.ask(run_id, role, assignment['purpose'], assignment['instruction'],
            assignment['context'], assignment['schema'])
        _base_unchanged(engine, run_id, record)
        clarified.append({'role': role, **response})
    return clarified


def _reviewed(record):
    candidate = record['candidate']
    votes = record.get('votes', [])
    return (len(votes) == 3 and {v.get('role') for v in votes} == {'director', 'engineer', 'scout'}
        and all(v.get('approve') is True and v.get('issues') == []
            and v.get('amendment_sha256') == candidate['amendment_sha256']
            and v.get('candidate_sha256') == candidate['bundle']['sha256']
            and v.get('checks_sha256') == candidate['checks_sha256']
            and (not candidate.get('check_budget') or v.get('check_budget_sha256')==sha(candidate['check_budget'])) for v in votes))


def _candidate_unchanged(record):
    from .code_bundle import validate as check
    candidate = record['candidate']
    from .rapid_check_budget import checked as checked_budget
    checked_budget(candidate)
    if sha(record['patch']) != candidate['amendment_sha256'] or _bytes_sha(candidate['check_code']) != candidate['checks_sha256']:
        _fail('快速修订的补丁或检查代码发生变化。')
    check(candidate['bundle'], candidate['program']['code'])
    check(candidate['check_bundle'], candidate['check_code'])
    if candidate.get('review_bundle'):
        check(candidate['review_bundle'], candidate['program']['code'])
    if candidate.get('control_bundle'):
        check(candidate['control_bundle'], record['base_program']['code'])
        if (record.get('binding', {}).get('bundle_sha256')
                and candidate['control_bundle']['sha256'] != record['binding']['bundle_sha256']):
            _fail('候选默认等价控制不再是原生效版本。')
    if candidate.get('review_bundle') and (candidate.get('repair_source') != record.get('candidate_repair_source')
            or record['patch'].get('repair_source_sha256') != (record.get('candidate_repair_source') or {}).get('sha256')):
        _fail('候选维修来源与已审核补丁不一致。')


def check_parameters(record):
    return {'phase': 'code_check', 'rapid_revision_id': record['id']}


async def _check(engine, run_id, record):
    from .lab import assess_resources
    from .training_adjustment import _basis
    engine.check_active(run_id)
    await engine._read_consensus_check(run_id, engine._execution_gate, run_id)
    resource = _basis(engine, run_id)['design']['resource_plan']
    from .rapid_check_budget import caps as check_caps,resource_request
    run = engine.store.get('runs',run_id)
    caps=check_caps(record['candidate'],run)
    resource=resource_request(resource,record['candidate'],run)
    required = sorted(set(resource.get('required_packages', []) + record['candidate']['program'].get('dependencies', [])))
    assessed = assess_resources({**resource, 'required_packages': required}, await engine.execution_hardware(run_id))
    if not assessed['approved']:
        _fail('快速 CPU 检查未通过当前资源准入。')
    run = engine.store.get('runs', run_id)
    c = record['candidate']
    if record.get('post_exit_protocol') == 1:
        from . import rapid_post_exit
        from .rapid_review_plan import checked
        contract = checked(record)['post_exit_contract']
        if hashlib.sha256(Path(rapid_post_exit.__file__).read_bytes()).hexdigest() != contract['source_sha256']:
            _fail('退出后宿主复核源码已变化，必须重新独立审核。')
    _candidate_unchanged(record)
    limits = {**assessed['limits'], 'execution_mode': run['config'].get('execution_mode', 'local'),
        'required_packages': required, 'code_bundle': c['check_bundle'], 'gpu_allowed': False,
        'experiment_binding': {'research_cycle': record['cycle'], 'code_revision': c['program']['revision'],
            'code_sha256': c['checks_sha256'], 'experiment_plan_sha256': record['binding']['plan_sha256']}}
    if c.get('check_workspace_layout'):
        from .check_workspace import LAYOUT
        if c['check_workspace_layout']!=LAYOUT:_fail('Unsupported CPU check workspace layout')
        limits['check_workspace_layout']=LAYOUT
    for field, cap in caps.items():
        limits[field] = min(limits.get(field, cap), cap)
    identifier = 'rapid-check-' + sha({'amendment': c['amendment_sha256'], 'checks': c['checks_sha256']})[:24]
    existing = engine.store.get('experiments', run_id + ':' + identifier)
    if existing:
        # Never repeat a crashed/failed assertion run automatically.
        if (existing.get('status') != 'completed' or existing.get('code_sha256') != c['checks_sha256']
                or existing.get('limits', {}).get('code_bundle') != c['check_bundle']):
            _fail('快速 CPU 检查已有失败或未确认的运行记录；保留现场，不自动重跑。')
        if record.get('post_exit_protocol') == 1:
            record['post_exit_verification'] = rapid_post_exit.capture(engine,run_id,record,existing)
            engine.store.put(COLLECTION,record['id'],record)
        return existing
    _budget(engine, run_id, 1)
    _base_unchanged(engine, run_id, record)
    try:
        result = await engine.runner.run(run_id, c['check_code'], check_parameters(record),
            identifier, limits, engine.cancellations[run_id])
    finally:
        # Runner commits its terminal receipt only after process termination.
        # Preserve the host audit for success, failure, timeout and cancellation.
        if record.get('post_exit_protocol') == 1:
            terminal = engine.store.get('experiments',run_id+':'+identifier) or {}
            if terminal.get('finished_at'):
                record['post_exit_verification'] = rapid_post_exit.capture(engine,run_id,record,terminal)
                engine.store.put(COLLECTION,record['id'],record)
    engine.check_active(run_id)
    return result


def _check_passed(engine, run_id, record):
    saved = record.get('check') or {}
    result = engine.store.get('experiments', run_id + ':' + saved.get('id', '')) or {}
    c = record['candidate']
    if record.get('post_exit_protocol') == 1:
        from .rapid_post_exit import passed
        if not passed(engine.store,record,result):
            return False
    return (sha(result) == saved.get('receipt_sha256') and result.get('run_id') == run_id
        and result.get('status') == 'completed' and result.get('code_sha256') == c['checks_sha256']
        and result.get('limits', {}).get('code_bundle') == c['check_bundle']
        and result.get('metrics', {}).get('rapid_check_passed') == 1
        and result.get('metrics', {}).get('rapid_assertions') == c['assertion_count'])


def runtime_bundle(store, run_id, program, base_program, plan_sha256):
    """Runner-level authentication of every amended source/check/read receipt.

    The Engine also rechecks the full original native delivery before dispatch.
    This narrower store-only boundary prevents borrowing a nullable-time policy
    merely by setting a candidate id or changing the runner's bundle argument.
    """
    from types import SimpleNamespace
    from .experiment_decisions import _approved
    engine = SimpleNamespace(store=store)
    original, seen, bundle = program, set(), None
    while program.get('rapid_revision_id'):
        key = program['rapid_revision_id']
        if key in seen or len(seen) >= 20:
            _fail('快速修订运行来源链无效。')
        seen.add(key)
        record = store.get(COLLECTION, key) or {}
        candidate = record.get('candidate', {})
        decision = store.get('experiment_decisions', record.get('decision_id', '')) or {}
        run = store.get('runs', run_id)
        if (record.get('run_id') != run_id or record.get('cycle') != run.get('state', {}).get('research_cycle', 1)
                or record.get('status') != 'activated' or candidate.get('program') != program
                or record.get('binding', {}).get('plan_sha256') != plan_sha256
                or record.get('binding', {}).get('config_sha256') != sha(run['config'])
                or not _reviewed(record) or not _check_passed(engine, run_id, record)
                or not _approved(decision) or decision.get('run_id') != run_id
                or decision.get('proposal_sha256') != record.get('decision_sha256')):
            _fail('快速候选的运行授权缺少同版本决策、复审或检查回执。')
        _candidate_unchanged(record)
        context = _review_context(engine, run_id, record)
        for vote in record['votes']:
            review_receipts(engine, run_id, record, vote, context)
        if bundle is None:
            bundle = candidate['bundle']
        program = record['base_program']
    if program != base_program or not original.get('rapid_revision_id'):
        _fail('快速运行候选不属于本次显式授权的原交付。')
    return bundle


def validate_program(engine, run_id, program, _seen=None):
    """An amendment never borrows the original code's approval claims."""
    seen = set(_seen or ())
    key = program.get('rapid_revision_id')
    if not key or key in seen or len(seen) > 20:
        _fail('快速修订来源链无效。')
    seen.add(key)
    record = engine.store.get(COLLECTION, key) or {}
    if (record.get('run_id') != run_id or record.get('cycle') != engine.cycle(run_id)
            or record.get('status') != 'activated' or record.get('candidate', {}).get('program') != program
            or sha(record.get('patch')) != record['candidate']['amendment_sha256']
            or not _reviewed(record) or not _check_passed(engine, run_id, record)):
        _fail('快速修订缺少同版本代码复审或真实 CPU 断言回执。')
    from .experiment_decisions import _approved
    decision = engine.store.get('experiment_decisions', record['decision_id']) or {}
    if not _approved(decision) or decision.get('proposal_sha256') != record['decision_sha256']:
        _fail('快速修订缺少原三方修改决策。')
    base = record['base_program']
    if base.get('rapid_revision_id'):
        validate_program(engine, run_id, base, seen)
    else:
        _bundle(engine, run_id, base)
    context = _review_context(engine, run_id, record)
    for vote in record['votes']:
        review_receipts(engine, run_id, record, vote, context)
    from .code_bundle import validate as check
    check(record['candidate']['bundle'], program['code'])
    from .training_adjustment import _basis
    run = engine.store.get('runs', run_id)
    if sha(_basis(engine, run_id)['plan']) != record['binding']['plan_sha256'] or sha(run['config']) != record['binding']['config_sha256']:
        _fail('快速修订的冻结实验协议或资源配置已变化。')
    return record['candidate']['bundle']


def _finish(engine, run_id, record, *, activate):
    from .experiment_decisions import SESSIONS
    _base_unchanged(engine, run_id, record)
    from .training_investigation import scoped_repair
    if scoped_repair(record['decision']):
        # CPU evidence is retained independently. Activating a changed program
        # here used to supersede every validation result and replay the pilot.
        # A diagnostic check does not authorize that scientific reset.
        record.update(status='diagnostic_checked' if activate else record['status'], completed_at=now(),
                      activation_scope='candidate_only', training_authorized=False)
        engine.store.put(COLLECTION, record['id'], record)
        engine.artifact(run_id, f'rapid-revisions/{sha(record["id"])}/amendment.json', encoded(record), 'code')
        engine.store.event(run_id, 'tuning.diagnostic_checked', record['status'],
                           {'revision_id': record['id'], 'training_authorized': False})
        return {'rapid_revision': record['id'], 'status': record['status'], 'tuning_pending': True,
                'next_node': 'tuning', 'scientific_approval': False}
    puts, deletes = [], []
    if activate:
        from .dataset_optimization import check_frozen
        check_frozen(engine, run_id, record['candidate']['bundle'], record['candidate']['program'])
    record.update(status='activated' if activate else record['status'], completed_at=now())
    puts.append((COLLECTION, record['id'], record))
    stages = ['revision', 'tuning', 'validation_review', 'final_tests', 'report'] + (['experiments'] if activate else [])
    for stage in stages:
        key = run_id + ':' + stage
        step = engine.store.get('steps', key)
        if step:
            archived = record['id'] + ':' + stage
            puts.append(('cycle_steps', archived, {**step, 'id': archived, 'rapid_revision_id': record['id']}))
        deletes.extend((name, key) for name in ('steps', 'stage_contexts'))
    if activate:
        _candidate_unchanged(record)
        puts.append(('programs', run_id, record['candidate']['program']))
        state = engine.store.get(SESSIONS, f'{run_id}:{record["cycle"]}')
        if state:
            archived = record['id'] + ':previous-session'
            puts.append((SESSIONS, archived, {**state, 'id': archived, 'archived': True}))
        deletes.append((SESSIONS, f'{run_id}:{record["cycle"]}'))
        deletes.extend((name, run_id) for name in ('selections', 'tuning_plans'))
    engine.store.batch(puts, deletes, supersede_validation=(run_id, record['cycle'],
        'Retained before scoped amendment ' + record['id']) if activate else None)
    engine.artifact(run_id, f'rapid-revisions/{sha(record["id"])}/amendment.json', encoded(record), 'code')
    engine.store.event(run_id, 'experiment.rapid_revision', record['status'],
        {'revision_id': record['id'], 'decision_id': record['decision_id'], 'scientific_approval': False})
    return {'rapid_revision': record['id'], 'status': record['status'],
            'next_node': 'experiments' if activate else 'tuning', 'scientific_approval': False}


async def run(engine, harness, run_id, review):
    from .engine import ResearchBlocked
    from .experiment_decisions import revision_context, _binding, _safe_rows, packet, units
    from .experiment_workflow import validation_records
    from .validation_hold import current
    run_state = engine.store.get('runs', run_id)
    hold = current(engine.store, run_state)
    if hold:
        _fail(hold['reason'])
    # A graph crash after atomic activation must route directly to its pilot.
    decision_id = review.get('failure', {}).get('details', {}).get('decision_id')
    key = run_id + ':rapid:' + sha(decision_id)
    record = engine.store.get(COLLECTION, key)
    if record and record.get('status') == 'review_blocked':
        _fail('候选已有经原始回执验证的独立否决；保留其余阅读进度，先处理具体问题，不自动重开修改轮次。')
    if record and record['status'] == 'diagnostic_checked':
        _base_unchanged(engine, run_id, record)
        return {'rapid_revision': key, 'status': record['status'], 'next_node': 'tuning', 'tuning_pending': True}
    if record and record['status'] == 'activated':
        validate_program(engine, run_id, record['candidate']['program'])
        return {'rapid_revision': key, 'status': 'activated', 'next_node': 'experiments'}
    scoped = revision_context(engine, run_id, review)
    if not scoped:
        _fail('快速修订必须来源于实验后已批准的局部修改决策。')
    basis, binding = await engine._read_consensus_check(run_id, _binding, engine, run_id)
    from .rapid_continuation import effective_policy, validated
    if (effective_policy(engine.store, run_id, basis['plan'].get('iteration_policy')) or {}).get('on_validation_failure') == 'stop':
        _fail('冻结停止规则禁止自动启动局部架构修订。')
    if run_state.get('state', {}).get('runtime_training_policy') and not validated(engine.store, run_id):
        _fail('当前交付使用单独批准的运行策略；快速修改不能沿用其旧代码授权。')
    if not record:
        from .training_investigation import scoped_repair
        from .dataset_optimization import revision_allowed
        if not scoped_repair(scoped['proposal']) and not revision_allowed(engine, run_id):
            _fail('本轮快速修订次数已达研究迭代上限。')
        pilot = next(u for u in units(basis['plan']) if u['kind'] == 'pilot')
        _budget(engine, run_id, 1 if scoped_repair(scoped['proposal']) else 1 + len(pilot['jobs']))
        evidence = engine.store.get('experiment_evidence', scoped['evidence']['id'])['summary']
        rows = _safe_rows([r for r in validation_records(engine.store, run_id)
            if (r.get('research_cycle') or 1) == engine.cycle(run_id) and not r.get('superseded')])
        record = {'id': key, 'run_id': run_id, 'cycle': engine.cycle(run_id), 'status': 'implementing',
            'binding': binding, 'base_program': basis['program'], 'decision': scoped['proposal'],
            'decision_id': scoped['decision_id'], 'decision_sha256': sha(scoped['proposal']),
            'evidence': scoped['evidence'], 'validation_rows_sha256': sha(rows),
            'created_at': now(), 'votes': [], 'scientific_approval': False, 'check_input_contract_version': 2,
            'source_basis': {'base_sources': copy.deepcopy(evidence.get('code_sources')),
                'inherits_previous_candidate': False,
                'previous_candidate': {k: copy.deepcopy((evidence.get('rapid_revision') or {}).get(k))
                                       for k in ('id', 'status', 'code_sources')}}}
        if scoped['proposal'].get('architecture_review') or evidence.get('hyperparameter_yaml', {}).get('migration_required') or (
                basis['program'].get('hyperparameter_yaml_protocol') == 1 and scoped['proposal']['action'] == 'revise_architecture'):
            record.update(native_codex_protocol=1,
                          check_runtime_compatibility=1,
                          yaml_migration=bool(evidence.get('hyperparameter_yaml', {}).get('migration_required')))
            record['review_schema'] = copy.deepcopy(VOTE)
            record['review_schema']['properties']['read_receipts']['maxItems'] = 256
        engine.store.put(COLLECTION, key, record)
    _base_unchanged(engine, run_id, record)
    from . import rapid_check_repair
    if record['status'] == 'check_repairing' or rapid_check_repair.eligible(engine, run_id, record):
        await rapid_check_repair.repair(engine, harness, run_id, record)
    if record['status'] in {'rejected', 'check_failed', 'invalid'}:
        return _finish(engine, run_id, record, activate=False)
    if not record.get('candidate'):
        evidence = engine.store.get('experiment_evidence', record['evidence']['id'])['summary']
        context = packet(evidence, record['evidence'])
        context['rapid_revision'] = {'id': key, 'decision_sha256': record['decision_sha256'],
            'changes': record['decision']['changes'], 'hypothesis': record['decision']['hypothesis'],
            **({'training_response': record['decision']['training_response']} if record['decision'].get('training_response') else {}),
            'verification': record['decision']['verification'], 'stop_condition': record['decision']['stop_condition']}
        from .training_investigation import INSTRUMENTATION, scoped_repair
        instruction = WRITE + '\n' + INSTRUMENTATION if scoped_repair(record['decision']) else WRITE
        if evidence.get('tuning_scope', {}).get('status') == 'accepted' and record['decision']['action'] == 'revise_architecture':
            from .tuning_scope import compact as compact_tuning_scope
            context['rapid_revision']['tuning_scope'] = compact_tuning_scope(evidence['tuning_scope'])
            instruction += ('\n本次已审 changes 可以落实 tuning_scope 中 requires_implementation 的参数化能力。'
                '原控制配置的参数、输出和随机流保持不变；新增能力由配置驱动，修改构造器、形状闭包、配置校验和相应调用链，'
                '不能只扩大参数白名单或改 JSON 而不改变模型实际行为。CPU 断言覆盖控制等价性、候选边界和非法值拒绝。'
                '新增缺省参数可在既有 configs/method_configs.json 的 _tuning_capabilities 数组声明'
                '{path,variant,value_kind,default,source_paths}；源码必须读取该字段，缺省值保留原行为，'
                '默认控制的原 params 不改写。宽度相关派生维度和深度相关层数由实现一致派生，不能留下互相矛盾的形状常量。'
                '仅开放已审修改单范围；已有训练不重跑，新增版本须通过后续验证、架构评审和新的源绑定调参计划。')
        if record.get('check_input_contract_version') in (1, 2):
            # New assignments only: never rewrite an already dispatched request.
            context['rapid_revision']['check_input_contract'] = {
                'argv1': 'Path to the host-owned params.json', 'params': check_parameters(record),
                'training_configuration': False}
            instruction += ('\nrapid_revision.check_input_contract 给出 CPU 检查的真实入参。'
                'sys.argv[1] 指向宿主 code_check 信封，不包含 seed、variant 或训练配置；'
                '不要把该信封直接交给训练配置加载器。需要测试加载器时，在隔离目录另写符合'
                '已批准固定种子和 validation 阶段的最小配置夹具，再把该夹具路径传给加载器。'
                '不得覆盖宿主 params.json，也不得放松生产配置校验。')
            if record['check_input_contract_version'] == 2:
                context['rapid_revision']['check_input_contract']['module_paths_relative_to_cwd'] = True
                instruction += ('\n运行文件按清单中的相对路径复制到 cwd；src 只是额外的导入搜索路径，'
                    '不代表全部模块都位于 src 下。源码身份断言应使用已读取的清单路径，'
                    '例如 dataset_runtime/training.py 对应 cwd/dataset_runtime/training.py，不得擅自添加 src/。')
        if record.get('source_basis'):
            context['rapid_revision']['source_basis'] = copy.deepcopy(record['source_basis'])
            if record['source_basis'].get('inherits_previous_candidate'):
                instruction += ('\n本轮维修来源为已绑定的未批准候选；原生效版本仍为默认等价对照。'
                    '保留已交付实现，只补当前修改单；继承源码不继承批准，须重新独立复审。')
            else:
                instruction += ('\nsource_basis.base_sources 是本轮实际补丁基底（snapshot=current）。'
                '上一轮未激活候选仅作历史证据，宿主不会自动叠加其中的修改。'
                '检查依赖的函数、字段和辅助模块必须存在于本次完整候选；逐项核对后再提交。'
                '获批修改范围内需要保留的旧候选实现，应作为本次 SHA 绑定补丁显式提交并重新复审；'
                '若超出范围，如实指出冲突，不删除断言或伪造实现已存在。')
        from .rapid_source_recovery import candidate as draft_candidate, CandidateRejected
        try:
            record['candidate'] = await draft_candidate(engine, harness, run_id, record, context, instruction)
        except CandidateRejected as exc:
            _base_unchanged(engine, run_id, record)
            record.update(status='invalid', error=str(exc)[:1200])
            return _finish(engine, run_id, record, activate=False)
        record['status'] = 'reviewing'
        engine.store.put(COLLECTION, key, record)
    from .rapid_check_budget import review_schema as budget_review_schema
    if not record['votes'] and record['candidate'].get('check_budget'):
        record['review_schema']=budget_review_schema(record['candidate'],record.get('review_schema') or VOTE)
        engine.store.put(COLLECTION,key,record)
    context = _review_context(engine, run_id, record)
    if not record['votes']:
        if record.get('structured_review'):
            from .rapid_review_plan import collect
            votes = await collect(engine,harness,run_id,record,context)
        else:
            votes = await _group(harness, run_id,
                record.get('review_purpose') or 'rapid_review:' + record['candidate']['amendment_sha256'], REVIEW, context, record.get('review_schema') or VOTE)
        _base_unchanged(engine, run_id, record)
        if _recover_source_review(engine, run_id, record, votes, context):
            context = _review_context(engine, run_id, record)
            votes = await _group(harness, run_id, record['review_purpose'], REVIEW, context, record.get('review_schema') or VOTE)
            _base_unchanged(engine, run_id, record)
        from .rapid_review_rejection import rejection_reads
        rejected = rejection_reads(engine, run_id, record, votes, context)
        if rejected is not None:
            # A genuine veto needs authentic evidence, not approval-level full
            # coverage. Preserve every vote and gap; no check or activation.
            _candidate_unchanged(record)
            _base_unchanged(engine, run_id, record)
            if record.get('structured_review'):
                record.update(votes=copy.deepcopy(votes), review_reads=rejected, status='review_blocked',
                    review_outcome='authenticated_veto', scientific_approval=False)
                engine.store.put(COLLECTION,record['id'],record)
                _fail('独立审核已提出具体否决，已停止其余长篇阅读；保留原票与进度，等待定向处理，不增加调整轮次。')
            record.update(votes=copy.deepcopy(votes), review_reads=rejected, status='rejected',
                          review_outcome='authenticated_veto', scientific_approval=False)
            return _finish(engine, run_id, record, activate=False)
        votes = await _clarify_review_votes(engine, harness, run_id, record, votes, context)
        from .rapid_review_citations import repair as repair_citations
        record['votes'] = await repair_citations(engine,harness,run_id,record,votes,context)
        record['review_reads'] = [review_receipts(engine, run_id, record, v, context) for v in record['votes']]
        record['status'] = 'checking' if _reviewed(record) else 'rejected'
        engine.store.put(COLLECTION, key, record)
    if not _reviewed(record):
        return _finish(engine, run_id, record, activate=False)
    for vote in record['votes']:
        review_receipts(engine, run_id, record, vote, context)
    if not record.get('check'):
        result = await _check(engine, run_id, record)
        _base_unchanged(engine, run_id, record)
        record['check'] = {'id': result['id'], 'receipt_sha256': sha(result), 'status': result['status'],
                           'stderr_tail': str(result.get('stderr') or '')[-1800:]}
        engine.store.put(COLLECTION, key, record)
    if not _check_passed(engine, run_id, record):
        record['status'] = 'check_failed'
        return _finish(engine, run_id, record, activate=False)
    from .training_investigation import scoped_repair
    if not scoped_repair(record['decision']):
        _budget(engine, run_id, len(next(u for u in units(basis['plan']) if u['kind'] == 'pilot')['jobs']))
    return _finish(engine, run_id, record, activate=True)
