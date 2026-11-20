"""Credit submitted review pages in an experiment analysis under their own receipt contract."""
import json

from . import independent_full_review as full
from .full_review_notes import _submitted_receipts
from .source_read_identity import source_files


def submitted_pages(store, run_id, role, owner, receipts):
    """Find exact tool rows in acknowledged requests belonging to the reader."""
    from .context_manifest import _sha, validate_manifest
    from .experiment_workflow import sha
    h = store.get('handoffs', owner) or {}
    if h.get('run_id') != run_id or h.get('to_role') != role or h.get('status') != 'completed':
        return {}
    wanted = {}
    for row in (store.get('handoff_progress', owner) or {}).get('tool_results', []):
        page = row.get('result')
        if (isinstance(page, dict) and page.get('receipt_sha256') in receipts and page.get('text')
                and page['receipt_sha256'] == sha({'call': row['call'], 'result': {k: v for k, v in page.items()
                    if k != 'receipt_sha256'}})):
            wanted[_sha(row)] = row
    found = {}
    for call in h.get('model_calls', []):
        if not wanted:
            break
        rid = call.get('request_id')
        manifest = store.get('context_manifests', rid) if rid else None
        if (not manifest or manifest.get('run_id') != run_id or manifest.get('role') != role
                or manifest.get('handoff_id') != owner or manifest.get('request_id') != rid
                or manifest.get('submission_status') != 'submitted'):
            continue
        matched = {m['representation_sha256'] for m in manifest['materials'] if m['kind'] == 'tool_receipt'
                   and m.get('compiled') and m.get('submitted') and m['representation_sha256'] in wanted}
        if matched and validate_manifest(store, manifest):
            for digest in sorted(matched):
                row = wanted.pop(digest)
                page, args = row['result'], row['call']['arguments']
                found[page['receipt_sha256']] = {'range': [page['offset'], page['end']], 'chars': page['total_chars'],
                    'path': args.get('path') or args.get('trial_id'), 'tool': row['call']['name'], 'evidence_id': args.get('evidence_id'),
                    'evidence_sha256': args.get('expected_sha256'), 'request_id': rid}
    return found


def inspections(engine, run_id, record):
    """Expose authenticated inspections to the next reviewer and to dissent mediation."""
    from . import experiment_evidence_handoff as handoff, artifact_review_pages
    from . import consensus_reading
    from .scope_read_session import reading
    from .training_read_dispatch import _gaps
    from .tuning_point_reads import _collect
    owners = {}
    directed = {}
    parent = engine.store.get('experiment_decisions', record.get('consensus_parent', '')) or {}
    for stage in (parent, record):
        if stage.get('evidence') != record['evidence']:
            continue
        for row in stage.get('analysis_reads', []):
            for key in ('handoff_id', 'supplemental_handoff_id'):
                if row.get(key):
                    owners[row[key]] = None
        for vote in stage.get('votes', []):
            if full.KEY in vote:
                full.authenticate(engine.store, run_id, vote)
                owners.update(dict.fromkeys(full.reading_handoffs(engine.store, run_id, vote)))
        for audit in stage.get('training_read_dispatch', {}).values():
            for assignments in audit['assignments'].values():
                for assignment in assignments:
                    if assignment.get('read'):
                        owners[assignment['read']['handoff_id']] = None
        for role, assignments in stage.get(consensus_reading.KEY, {}).get('assignments', {}).items():
            for assignment in assignments:
                if 'answer' in assignment:
                    from .training_read_dispatch import _verify
                    proof = _verify(engine, run_id, role, assignment)
                    owner = proof['handoff_id']
                    owners[owner] = None
                    directed[owner] = (role, proof, assignment['answer'])
    if not owners:
        return None
    with reading(engine) as reader:
        rows, _ = _collect(reader, run_id, record['evidence'], record, owners=owners,
                           tools={'experiments.analyze', 'experiments.inspect_module', 'experiments.compare'}, include_overlapping_receipts=True)
        facts = (record.get(handoff.KEY) or {}).get('independent_review') or {}
        result = {}
        for role in full.REVIEWERS:
            material, handoffs = [], []
            wanted = {}
            for item in rows[role]:
                for hid, receipt in item['receipts']:
                    wanted.setdefault(hid, set()).add(receipt)
            supplement = None
            if any(r.get('role') == role for r in facts.get('reads', [])):
                supplement = artifact_review_pages.verified_reads(reader, run_id, role, facts)
                for hid in [supplement['handoff_id'], *supplement.get('prior_handoff_ids', [])]:
                    wanted.setdefault(hid, set()).update(supplement['receipt_sha256s'])
            for hid, (seat, proof, _) in directed.items():
                if seat == role:
                    wanted.setdefault(hid, set()).update(proof['receipt_sha256s'])
            delivered = {hid: submitted_pages(reader.store, run_id, role, hid, receipts) for hid, receipts in wanted.items()}
            for item in rows[role]:
                if item['kind'] not in ('source', 'training', 'evidence'):
                    continue
                ids = list(dict.fromkeys(hid for hid, _ in item['receipts']))
                handoffs.extend(hid for hid in ids if hid not in handoffs)
                material.append({'kind': item['kind'], 'path_or_trial': item['key'],
                    'sha256': item['sha256'], 'chars': item['total_chars'],
                    'missing_ranges': _gaps(item['total_chars'], item['ranges']),
                    'undelivered_ranges': _gaps(item['total_chars'], [delivered[hid][r]['range'] for hid, r in
                        item['receipts'] if r in delivered[hid]]),
                    'handoff_indices': [handoffs.index(hid) for hid in ids]})
            detail = {'handoffs': handoffs, 'inspections': material}
            if supplement:
                ref = handoff.packet(record[handoff.KEY], for_review=True)['evidence']
                by_path = {}
                for pages in delivered.values():
                    for page in pages.values():
                        if page['tool'] == 'experiments.compare' and page['evidence_id'] == ref['id'] and page['path']:
                            by_path.setdefault(page['path'], []).append(page)
                detail['supplemental_read'] = {k: supplement[k] for k in ('handoff_id', 'paths', 'receipt_sha256s')}
                detail['supplemental_read']['evidence'] = ref
                detail['supplemental_read']['delivered_paths'] = sorted(path for path, pages in by_path.items()
                    if not _gaps(pages[0]['chars'], [p['range'] for p in pages]))
            reports = [{'handoff_id': hid, **{k: answer[k] for k in ('observations', 'hypotheses', 'missing_evidence')}}
                       for hid, (seat, _, answer) in directed.items() if seat == role]
            if reports:
                detail['reviewer_reports'] = reports
            sources = []
            for hid, pages in delivered.items():
                if pages and hid not in handoffs:
                    handoffs.append(hid)
                sources.extend({'handoff_index': handoffs.index(hid), 'receipt_sha256': receipt,
                    'request_id': page['request_id'], 'tool': page['tool'], 'path': page['path'], 'range': page['range']}
                    for receipt, page in pages.items() if page['path'])
            detail['note_sources'] = sources
            result[role] = detail
    return {'delivery_protocol': 1, 'roles': result, 'evidence': record['evidence'],
        'scope': 'missing_ranges 是本角色工具未取回的区间。undelivered_ranges 是宿主未确认进入该角色实际请求的区间，'
                 '为空表示同版原页已全部送达。补充快照的 delivered_paths 已按相同标准核对。'
                 '需要原文时，用当前 validation_evidence 的 ID 和 SHA 调用 experiments.analyze(kind=training, trial_id=…) 或'
                 ' experiments.inspect_module(view=source, path=…)；补充快照用 artifact_evidence_handoff.evidence 调用 experiments.compare。'
                 '自行核对仍缺的页和具体疑点，保留未知，独立评审本次方案。',
        'approval_inherited': False}


def note_origins(engine, run_id, role, context, cited):
    prior = context.get('experiment_decision', {}).get('prior_inspections', {})
    if prior.get('delivery_protocol') != 1:
        return {}
    detail = prior.get('roles', {}).get(role, {})
    owners = detail.get('handoffs', [])
    sources = detail.get('note_sources', [])
    accepted = {}
    for index in {s['handoff_index'] for s in sources if s['receipt_sha256'] in cited}:
        hid = owners[index]
        before = (engine.store.get('handoff_inputs', hid) or {}).get('context') or {}
        if not before.get('validation_evidence') or before['validation_evidence'] != context.get('validation_evidence'):
            continue
        allowed = {s['receipt_sha256'] for s in sources if s['handoff_index'] == index} & cited
        from .experiment_tools import comparison_reference
        for receipt, page in submitted_pages(engine.store, run_id, role, hid, allowed).items():
            eid = page['evidence_id']
            ref = comparison_reference(context, eid)
            if (page['tool'] in {'experiments.inspect_module', 'experiments.compare', 'experiments.analyze'} and ref
                    and comparison_reference(before, eid) == ref and ref['sha256'] == page['evidence_sha256']):
                accepted[receipt] = hid
    return accepted


def material_reads(engine, run_id, role, handoff_id, context, cited):
    _, delivered = _submitted_receipts(engine, run_id, role, handoff_id)
    submitted = cited.intersection(delivered)
    valid = set()
    paths = set()
    if not submitted:
        return valid, paths
    files = source_files(engine.store, run_id, context)
    for owner in {delivered[receipt] for receipt in submitted}:
        frozen = engine.store.get('handoff_inputs', owner)['context']
        materials = frozen.get(full.MATERIALS) or {}
        if materials.get('evidence_reference') != context['validation_evidence']:
            continue
        valid.update(receipt for receipt in submitted if delivered[receipt] == owner)
        root = full.material_text(frozen)
        ranges = []
        for item in engine.store.get('handoff_progress', owner)['tool_results']:
            if item.get('call', {}).get('name') != full.TOOL:
                continue
            page = item.get('result', {})
            if (page.get('receipt_sha256') in valid
                    and page.get('text') == root[page['char_start']:page['char_end']]):
                ranges.append((page['char_start'], page['char_end']))
        merged = []
        for start, end in sorted(ranges):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        for archive in materials.get('sources', []):
            for path, entry in archive['files'].items():
                if files.get(path) != entry or not entry.get('text'):
                    continue
                # Include the path and complete typed entry. Identical source snippets alone carry no file identity.
                fragment = json.dumps({path: entry}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))[1:-1]
                start = root.find(fragment)
                if start >= 0 and root.find(fragment, start + 1) < 0 and any(
                        left <= start and start + len(fragment) <= right for left, right in merged):
                    paths.add(path)
    return valid, paths
