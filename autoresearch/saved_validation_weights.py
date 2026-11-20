"""Bounded read-only evidence from weight files in a completed validation attempt."""
import hashlib
import io
import pickle
import re
import zipfile
from collections.abc import Mapping

from .experiment_workflow import sha

LIMIT = 128 * 1024 * 1024


def bind_prior_files(engine, run_id, record, evidence, proposal, selected, prior):
    """Bind an explicitly cited inventory row to an already audited validation file."""
    from . import experiment_evidence_handoff as handoff
    from .experiment_files import _reports, _validation_trial, WEIGHT_SUFFIXES
    audit = (prior or {}).get('as_observed_in_prior_review', {}).get('saved_weight_audit')
    if not audit:
        return None
    current = handoff.bound_evidence(engine, run_id, record, proposal)
    parent = engine.store.get('experiment_decisions', prior['decision_id'])
    earlier = handoff.archived_evidence(engine, run_id, parent, prior['artifact_evidence_sha256'])
    old_rows = {t['summary']['trial_id']: t['summary'] for t in earlier['trials']}
    trials = {r['id']: r for r in evidence['records'] if r['id'] in selected and _validation_trial(r, run_id)}
    bound = {}
    for i, row in enumerate(current['trials']):
        summary = row['summary']
        tid = summary['trial_id']
        if tid not in trials:
            continue
        audited = {f['path']: f for f in audit['result']['weight_files'].get(tid, [])
                   if f.get('file_sha256') and 'matches_prior_file' not in f}
        for j, entry in enumerate(summary.get('saved_weight_files', [])):
            pointer = f'/trials/{i}/summary/saved_weight_files/{j}'
            path = entry['path']
            if pointer not in proposal['evidence_paths'] or path not in audited:
                continue
            trial = trials[tid]
            identity = {'record_projection_sha256': sha(trial), 'params_sha256': sha(trial['params'])}
            if any(s.get(k) != v for s in (summary, old_rows[tid]) for k, v in identity.items()):
                raise ValueError('Cited weight inventories refer to different validation attempts')
            if path not in dict(_reports(engine, run_id, trial, WEIGHT_SUFFIXES)):
                continue
            observed = handoff._file(engine, run_id, trial, path)
            if observed['status'] != 'available':
                continue
            same = (entry['bytes'] == observed['bytes'] == audited[path]['bytes']
                    and observed['sha256'] == audited[path]['file_sha256'])
            bound.setdefault(tid, []).append({**identity, 'path': path, 'inventory_pointer': pointer,
                'inventory_bytes': entry['bytes'], 'bytes': observed['bytes'], 'file_sha256': observed['sha256'],
                'prior_file_sha256': audited[path]['file_sha256'], 'matches_prior_file': same})
    if bound:
        return {'weight_files': bound, 'fit_histories': {}, 'prior_audit_sha256': audit['sha256'],
                'prior_archive_sha256': prior['artifact_evidence_sha256'],
                'current_archive_sha256': proposal['artifact_evidence_sha256'],
                'scope': 'Current archived file bytes only. Prior tensor fingerprints apply only on a byte match. '
                         'Historical in-memory state and scoring inputs remain unobserved.'}


def tensor_maps(raw):
    import torch
    if not zipfile.is_zipfile(io.BytesIO(raw)):
        return {}
    with zipfile.ZipFile(io.BytesIO(raw)) as container:
        if sum(item.file_size for item in container.infolist()) > LIMIT:
            return {}
    state = torch.load(io.BytesIO(raw), map_location='cpu', weights_only=True)
    if not isinstance(state, Mapping):
        return {}
    def is_map(value):
        return isinstance(value, Mapping) and bool(value) and all(
            isinstance(name, str) and isinstance(tensor, torch.Tensor) for name, tensor in value.items())
    groups = {'/': state} if is_map(state) else {k: v for k, v in state.items() if isinstance(k, str) and is_map(v)}
    result = {}
    for group, values in groups.items():
        if sum(t.numel() * t.element_size() for t in values.values()) > LIMIT:
            continue
        descriptors = []
        for name, tensor in sorted(values.items()):
            data = tensor.detach().contiguous().reshape(-1).view(torch.uint8).numpy()
            descriptors.append([name, str(tensor.dtype), list(tensor.shape), hashlib.sha256(data).hexdigest()])
        result[group] = {'sha256': sha(descriptors), 'tensor_count': len(values),
                         'state_elements_including_buffers': sum(t.numel() for t in values.values())}
    return result


def collect(engine, run_id, evidence, selected, verification, bound=None):
    from .experiment_files import _reports, _validation_trial, WEIGHT_SUFFIXES
    from .experiment_views import _safe_file
    from .training_investigation import fit_histories
    material = {}
    for trial in evidence['records']:
        if trial['id'] not in selected or not _validation_trial(trial, run_id):
            continue
        files = []
        linked = {f['path'] for f in (bound or {}).get('weight_files', {}).get(trial['id'], [])}
        for name, size in sorted(_reports(engine, run_id, trial, WEIGHT_SUFFIXES)):
            # Only inspect files the author requested, within the original trial scope.
            path_ref = '(?:' + re.escape(trial['id'] + '/') + ')?' + re.escape(name)
            if name in linked or not re.search(r'(?<![\w/.-])' + path_ref + r'(?![\w/-]|\.[\w.-])', verification.replace('\\', '/'), re.ASCII):
                continue
            item = {'path': name, 'bytes': size}
            files.append(item)
            if size > LIMIT:
                item['unavailable'] = 'Saved file exceeds the bounded audit size'
                continue
            path = _safe_file(engine, run_id, trial, name)
            if path is None:
                raise ValueError('Weight file is no longer available in the selected validation attempt')
            with path.open('rb') as stream:
                raw = stream.read(LIMIT + 1)
            if len(raw) != size:
                raise ValueError('Weight file changed during collection')
            item['file_sha256'] = hashlib.sha256(raw).hexdigest()
            try:
                item['tensor_maps'] = tensor_maps(raw)
            except (ImportError, RuntimeError, TypeError, ValueError, pickle.UnpicklingError, zipfile.BadZipFile) as exc:
                item['tensor_read_status'] = 'Unsupported safe reader or layout: ' + type(exc).__name__
        if files:
            material[trial['id']] = files
    if not any('file_sha256' in f for files in material.values() for f in files):
        return bound
    if bound:
        for tid, rows in bound['weight_files'].items():
            material.setdefault(tid, []).extend(rows)
    return {**(bound or {}), 'weight_files': material, 'fit_histories': fit_histories(engine, run_id, evidence, selected),
            'scope': 'File and state-dictionary digests identify saved bytes. They do not certify initial weights, '
                     'which weights were loaded by an earlier evaluation, trainable parameter counts, or scientific acceptance.'}
