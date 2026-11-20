"""Trace the original reviewers' exact issues to evidence for a fresh vote."""
from pathlib import Path
import hashlib
import json

from .experiment_workflow import sha


# Stable identifiers for the already frozen observations. Unknown issues stay open.
GROUPS = {
    'config': ('4148517ca8965618b1','ba71c128169e795f08'),
    'source': ('a10718513901c34610','694c85a7635179561a'),
    'journal': ('d31938ce58187348b8',),
    'ownership': ('3105b61643d5210e47',),
    'resources': ('91ced2cb050910f8ae','e83e16c8fed76fd422','4a42b8ef6ab32256ab'),
    'admission': ('1fdba3a6a5729096c0','46c337ec75803b3021'),
    'cpu': ('5352aeac8d86247a19','cc3a5a87f4819fcf08','7ef568ad389f6d14f3'),
    'deadline': ('aabf83172adb5303b9','53d34c7d167b2372dc','eb9eef8d88dcaeb792',
                 'db7cd2562f694e9d10','8f0b532c0c0acd55d8'),
    'table': ('ddd1cf008d8b73b95a','713f14da7a5e75297e','9041e54838ef4d57f9'),
    'terminal': ('9e47bb1a73528163b2','e53dafd8560ead756e','34e8e7706ca7fd3414','008e57618aac90f789'),
    'reading': ('d80625b1cf0bed3f2e','05f618b3ff31121386'),
}


def collect(engine, rid, predecessors):
    from .parameter_resolution import owner
    result = {}
    for key in sorted(predecessors):
        saved = engine.store.get('bounded_diagnostics',key)
        for index, rd in enumerate(saved['rounds']):
            requests = [('engineer', rd['author'], 'blockers'), *(
                (role, req.get('read_correction') or req, 'issues') for role, req in rd.get('votes',{}).items())]
            for role, req, field in requests:
                answer = req['answer']
                if not answer.get(field):
                    continue
                hid = owner(engine,rid,role,req)
                for i, issue in enumerate(answer[field]):
                    result.setdefault((hid,i), {'role':role,'source_diagnostic':key,'source_round':index,
                        'handoff_id':hid,'answer_sha256':sha(answer),'original_approve':answer.get('approve'),
                        'issue_index':i,'original_issue':issue,'issue_sha256':sha(issue),
                        'original_read_receipts':answer.get('read_receipts',[])})
    return list(result.values())


def table(record):
    proof = record['resource_evidence']
    candidate = record['candidate']
    archive = 'host_evidence.json'
    # Locations are checked against the actual candidate (or bound host module)
    # rather than copied from a prior bundle's line numbers.
    actions = {
        'config': ('原配置和续接输入按原 SHA 暂存，仍须在运行时核对。','bounded_protocol.json','"inputs"','/controlled_protocol/inputs'),
        'source': ('生产者接口和状态结构已随独立作者任务交付，可与候选实现对照。','bounded_diagnostic.py','def run(', '/continuation_evidence'),
        'journal': ('候选显式报告双重异常并保留原子进度。候选专属异常注入结论按附回执核对。','bounded_diagnostic_io.py','def fail(', '/candidate_io_checks'),
        'ownership': ('列出每个评审文件的生成主体、原 SHA 和候选 SHA。',archive,'"source_sha256"','/controlled_admission/resource_contract/file_ownership'),
        'resources': ('外部 runner 强制资源限额；核对限额交集、计量范围与原六项离线探针。',archive,'"offline_checks"','/controlled_admission/resource_contract'),
        'admission': ('保留为派发前条件：三方独立批准并即时复核账本、硬件及预算。','bounded_diagnostics.py','async def execute_once(', '/controlled_admission'),
        'cpu': ('同一 Windows Job 跨计算、归档与事务确认累计用户和内核 CPU。保留提前终止余量；探针为采样证据。','diagnostic_supervisor.py','def _cpu_remaining(', '/cpu_checks'),
        'deadline': ('独立监督器对归档、事务提交及退出共用总截止，保留已提交现场，实际故障回执可查。','diagnostic_supervisor.py','async def supervise(', '/closeout_checks'),
        'table': ('逐项处理及原交付身份已冻结到当前候选的评审快照，表本身不授予批准。',archive,'"shared_cpu_job"','/issues'),
        'terminal': ('seal 第一阶段只暂存；最终 CPU、墙钟及取消检查通过后才进入原子发布。暂存后故障均验证权威状态为 incomplete。','diagnostic_supervisor.py',"if phase == 'stage':",'/closeout_checks'),
        'reading': ('保留为本轮独立复核条件。作者修订与评审现在共享完整冻结的处理表及源回执，读取尚须由各角色完成。','bounded_diagnostics.py','def author_context(', '/prior_review_receipts'),
    }
    rows = []
    for item in record['prior_review_items']:
        group = next((group for group, ids in GROUPS.items() if item['issue_sha256'][:18] in ids),None)
        row = {**item,'candidate_sha256':candidate['bundle']['sha256'],'approval_inherited':False,
               'original_blocking':True,'read_receipts_path':'/prior_review_receipts/'+item['handoff_id']}
        row.pop('original_read_receipts')
        if group is None:
            rows.append({**row,'disposition':'open','handling':'未匹配到可验证的修复，保留原异议。'})
            continue
        action, name, marker, evidence_path = actions[group]
        host = name in proof['source_sha256']
        path = Path(__file__).with_name(name) if host else Path(candidate['bundle']['root'])/name
        if not path.is_file():
            rows.append({**row,'disposition':'open','handling':'候选未提供原措施所指文件，需独立复核。'})
            continue
        text = path.read_bytes()
        checksum = hashlib.sha256(text).hexdigest()
        expected = proof['source_sha256'][name] if host else next(r['sha256'] for r in candidate['bundle']['files'] if r['path']==name)
        if expected != checksum:
            raise ValueError('Issue resolution source changed: ' + name)
        line = next((i for i,s in enumerate(text.decode('utf8').splitlines(),1) if marker in s),None)
        if line is None or group == 'journal' and checksum != record['candidate_io_checks']['body']['module_sha256']:
            rows.append({**row,'disposition':'open','handling':'当前实现与原定位或离线检查不匹配；须重新核对。'})
            continue
        disposition = ('pending_dispatch_gate' if group=='admission' else
                       'pending_independent_reading' if group=='reading' else 'submitted_for_independent_verification')
        rows.append({**row,'disposition':disposition,
                     'handling':action,'file':name,'line':line,'file_sha256':checksum,'acceptance_evidence_path':evidence_path})
    return rows


def journal_receipt(store, prior):
    module = next(r for r in prior['candidate']['bundle']['files'] if r['path']=='bounded_diagnostic_io.py')
    path = store.path.parent/'diagnostics'/('bounded-journal-'+module['sha256']+'.json')
    checks = json.loads(path.read_bytes())
    body = checks['body']
    script = Path(__file__).resolve().parent.parent/'scripts'/'verify_diagnostic_journal.py'
    if (sha(body)!=checks['sha256'] or body['module_sha256']!=module['sha256']
            or body['source_bundle_sha256']!=prior['candidate']['bundle']['sha256']
            or hashlib.sha256(script.read_bytes()).hexdigest()!=body['probe_sha256']
            or any(body['cases'][k]['passed'] is not True for k in ('compute_failure','double_failure'))):
        raise ValueError('Candidate journal fault-check identity changed')
    return checks
