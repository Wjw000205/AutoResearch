"""Keep a bounded receipt index visible when diagnostic source pages are evicted."""
from .experiment_workflow import sha
from .training_read_dispatch import _gaps


def project(context, rows):
    if str(context.get('_purpose', '')).split(':', 1)[0] != 'experiment_diagnose':
        return None
    decision = context.get('experiment_decision') or {}
    repair = decision.get('training_read_repair') or {}
    required = repair.get('required_training_trials') or decision.get('training_investigation', {}).get('focus_trial_ids', [])
    if not required or len(required) > 32:
        return None
    ref = context.get('validation_evidence') or {}
    reports = {trial: [] for trial in required}
    sources, seen = [], set()
    retained = (repair.get('retained_reads') or {}).get('reads', [])
    inputs = [(r['call'], r, 'retained_same_role_index') for r in retained if isinstance(r.get('call'), dict)]
    for row in rows:
        call, result = row.get('call') or {}, row.get('result') or {}
        if (not result.get('text') or result.get('error')
                or result.get('receipt_sha256') != sha({'call': call,
                    'result': {k: v for k, v in result.items() if k != 'receipt_sha256'}})):
            continue
        inputs.append((call, result, 'current_assignment'))
    for call, result, origin in inputs:
        args = call.get('arguments') or {}
        if (args.get('evidence_id') != ref.get('id') or args.get('expected_sha256') != ref.get('sha256')
                or not result.get('receipt_sha256') or result['receipt_sha256'] in seen):
            continue
        start, end, total = (result.get(k) for k in ('offset', 'end', 'total_chars'))
        if not all(type(x) is int for x in (start, end, total)) or not 0 <= start < end <= total:
            continue
        page = {'receipt_sha256': result['receipt_sha256'], 'offset': start, 'end': end,
                'total_chars': total, 'origin': origin}
        if (call.get('name') == 'experiments.analyze' and args.get('kind') == 'training'
                and args.get('trial_id') in reports):
            reports[args['trial_id']].append(page)
        elif (call.get('name') == 'experiments.inspect_module' and result.get('source_read') is True
                and args.get('snapshot', 'current') == 'current' and len(sources) < 32):
            sources.append({'path': args.get('path'), **page})
        seen.add(result['receipt_sha256'])
    summaries = []
    for trial, pages in reports.items():
        pages = pages[:32]
        totals = {p['total_chars'] for p in pages}
        missing = _gaps(next(iter(totals)), [(p['offset'], p['end']) for p in pages]) if len(totals) == 1 else None
        summaries.append({'trial_id': trial, 'pages': pages, 'missing_ranges': missing,
                          'delivered_complete': bool(pages) and missing == []})
    return {'version': 1, 'evidence_sha256': ref.get('sha256'), 'training_reports': summaries,
            'source_pages': sources, 'new_reading_credit': False, 'scientific_approval': False,
            'guidance': '这些是已交付页面的持久回执索引，不因正文窗口归档而消失。最终 read_receipts 须引用你实际使用的页面；'
                        '同一报告分页需列齐回执。delivered_complete 表示页面已交付，不是科学批准；宿主仍重新核验内容与归属。'
                        '只补缺失范围，不请求超过 total_chars 的空页，不因旧笔记写着未读而重复完整读取。'}
