"""Bounded, receipt-authenticated working material for a local patch author."""
from .experiment_workflow import sha


def _valid(context, row):
    call, result = row.get('call', {}), row.get('result', {})
    args = call.get('arguments', {})
    ref = context.get('validation_evidence') or {}
    return (args.get('evidence_id') == ref.get('id')
        and args.get('expected_sha256') == ref.get('sha256')
        and not result.get('error') and isinstance(result.get('text'), str)
        and result.get('end', 0) - result.get('offset', 0) == len(result['text'])
        and result.get('receipt_sha256') == sha({'call': call,
            'result': {k: v for k, v in result.items() if k != 'receipt_sha256'}}))


def _patch_path(path):
    return isinstance(path, str) and (path == '/rapid_revision/patch'
        or path.startswith('/rapid_revision/patch/'))


def support_windows(context, rows, targets):
    """Keep dependencies and previous patch operands, never whole transcripts.

    The base source has its separate 24k budget. Dependencies have 12k and the
    prior patch has 24k, each with eight windows. Checks and edits share the
    patch budget, so a complete ordinary patch can survive a dependency read.
    Full parent JSON pages supersede overlapping children;
    overlapping pagination cannot consume extra slots or reading credit.
    """
    if str(context.get('_purpose', '')).split(':', 1)[0] != 'rapid_implement':
        return set()
    target_paths = {rows[i]['call']['arguments']['path'] for i in targets}
    groups = {}
    for i, row in enumerate(rows):
        if not _valid(context, row):
            continue
        call, r = row['call'], row['result']; a = call['arguments']; path = a.get('path', '')
        if (call['name'] == 'experiments.inspect_module' and a.get('view') == 'source'
                and a.get('snapshot', 'current') == 'current' and r.get('source_read')
                and r.get('source_sha256') and path not in target_paths):
            kind, version = 'dependency', r['source_sha256']
        elif call['name'] == 'experiments.compare' and _patch_path(path):
            kind, version = 'patch', a['expected_sha256']
        else:
            continue
        key = kind, path
        if key not in groups or groups[key][0] != version:
            groups[key] = version, []
        groups[key][1].append(i)
    kept = set()
    for kind in ('dependency', 'patch'):
        size, count, parents = 0, 0, set()
        budget = 24000 if kind == 'patch' else 12000
        # Prefer compact complete nodes, including a whole edits array when read.
        ordered = sorted((k for k in groups if k[0] == kind), key=lambda k: (k[1].count('/'), k[1]))
        for key in ordered:
            path = key[1]
            if kind == 'patch' and any(path.startswith(p + '/') for p in parents):
                continue
            candidates = groups[key][1]; chosen = []; spans = []
            while candidates and count + len(chosen) < 8:
                ranked = []
                for i in candidates:
                    r = rows[i]['result']; lo, hi = r['offset'], r['end']; pieces = [(lo, hi)]
                    for a, b in spans:
                        pieces = [(x, y) for l, h in pieces for x, y in ((l, min(h, a)), (max(l, b), h)) if x < y]
                    gain = sum(b - a for a, b in pieces)
                    if gain and size + sum(len(rows[j]['result']['text']) for j in chosen) + len(r['text']) <= budget:
                        ranked.append((gain, i))
                if not ranked:
                    break
                _, index = max(ranked); chosen.append(index)
                spans.append((rows[index]['result']['offset'], rows[index]['result']['end']))
                candidates = [i for i in candidates if i != index]
            kept.update(chosen); count += len(chosen)
            size += sum(len(rows[i]['result']['text']) for i in chosen)
            cursor = 0
            for lo, hi in sorted(spans):
                if lo <= cursor: cursor = max(cursor, hi)
            if chosen and cursor == rows[chosen[-1]]['result'].get('total_chars'):
                parents.add(path)
    return kept


def patch_reference(context, call):
    """A reference only when the exact requested bytes are already delivered."""
    args = call.get('arguments', {})
    if call.get('name') != 'experiments.compare' or not _patch_path(args.get('path')):
        return None
    start = args.get('offset', 0); end = start + args.get('limit', 4000)
    rows = [r for r in context.get('tool_results', []) if _valid(context, r)
        and r['call'].get('name') == call['name']
        and all(r['call'].get('arguments', {}).get(k) == args.get(k)
            for k in ('path', 'evidence_id', 'expected_sha256'))]
    cursor, refs = start, []
    for row in sorted(rows, key=lambda r: (r['result']['offset'], -r['result']['end'])):
        r = row['result']
        if r['offset'] <= cursor < r['end']:
            cursor = r['end']; refs.append(r['receipt_sha256'])
            if cursor >= end or cursor == r.get('total_chars'):
                return {'kind': 'prior_patch', 'path': args['path'], 'evidence_sha256': args['expected_sha256'],
                    'offset': start, 'end': min(end, cursor), 'receipt_sha256s': refs}
    return None
