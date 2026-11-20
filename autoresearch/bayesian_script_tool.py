"""Write versioned researcher-authored scripts next to their immutable architecture sources."""
import ast
import hashlib
import json
from pathlib import Path
import re

from .bayesian_policy import compile_policy, validated_tree
from .experiment_workflow import sha

WRITE = 'bayes.write_script'
START = 'bayes.start_search'
COLLECTION = 'dataset_bayesian_scripts'


def source_key(run_id, evidence, source):
    return run_id + ':bayes-script:' + sha([evidence['binding'], evidence['code_sources']['sha256'], source])


def workspace(engine, run_id, evidence, key):
    # Keep Windows launch paths short; the manifest still binds both full digests.
    return engine.run_dir(run_id) / 'architecture-versions' / evidence['binding']['code_sha256'][:18] / key.rsplit(':', 1)[1][:20]


def write_enabled(context, purpose, role):
    return (role == 'engineer' and purpose.startswith(('experiment_decide:tuning-scope:',
                                                       'experiment_decide:combination-settings:'))
            and context.get('experiment_decision', {}).get('bayesian_script_authoring') == 1)


def entry(source, run_id, identifier):
    # Both CLI modes use the same reviewed selector. The stdio driver never has to read the research DB.
    return ast.unparse(validated_tree(source)) + '\n\n' + f'''if __name__ == "__main__":
    import argparse, json, sys, urllib.request
    parser = argparse.ArgumentParser(description="Launch the approved dataset Bayesian search")
    parser.add_argument("--search", action="store_true", required=True)
    parser.add_argument("--stdio", action="store_true", help="Use the host validation and training driver")
    args = parser.parse_args()
    if args.stdio:
        for line in sys.stdin:
            packet = json.loads(line)
            print(json.dumps(select_next(packet["candidates"], packet["completed"], packet["budget"]), allow_nan=False), flush=True)
    else:
        req = urllib.request.Request("http://127.0.0.1:8000/api/runs/{run_id}/bayesian-search",
             data=json.dumps({{"script_id": {identifier!r}}}).encode(), headers={{"Content-Type": "application/json"}})
        with urllib.request.urlopen(req, timeout=600) as response:
            print(response.read().decode())
'''


def write(harness, run_id, role, owner, args):
    from .tool_authorization import authorize
    from .rapid_experiment_revision import _write_files
    authorize(harness, run_id, role, owner, WRITE, args)
    engine = harness.engine
    frozen = engine.store.get('handoff_inputs', owner) or {}
    handoff = engine.store.get('handoffs', owner) or {}
    context = frozen.get('context', {})
    if engine.store.get('runs', run_id)['stage'] != 'tuning' or not write_enabled(context, handoff.get('purpose', ''), role):
        raise ValueError('This assignment cannot author a Bayesian script')
    ref = context['validation_evidence']
    archived = engine.store.get('experiment_evidence', ref['id'])
    evidence = archived['summary']
    if archived.get('run_id') != run_id or sha(evidence) != ref['sha256'] or evidence['binding']['cycle'] != engine.cycle(run_id):
        raise ValueError('Wrong dataset or evidence version for script authoring')
    source = args['selection_script']
    compile_policy(source)
    dataset = (evidence.get('configuration_queue') or {}).get('active_dataset') or 'dataset'
    name = re.sub('[^a-z0-9_]+', '_', dataset.lower()).strip('_')[:64] or 'dataset'
    key = source_key(run_id, evidence, source)
    relative = 'scripts/' + name + '_bayes.py'
    script = entry(source, run_id, key).encode()
    basis_ref = evidence['code_sources']
    sources = engine.store.get('experiment_sources', basis_ref['id']) or {}
    if sources.get('run_id') != run_id or sha(sources['files']) != basis_ref['sha256']:
        raise ValueError('Architecture sources changed before script creation')
    files = {p: v['text'].encode() for p, v in sources['files'].items() if v.get('text') is not None}
    missing = set(sources['files']) - set(files)
    if missing:
        from .experiment_decisions import _binding
        from .code_bundle import relative_file
        basis, current = _binding(engine, run_id)
        if current != evidence['binding'] or not basis['bundle']:
            raise ValueError('Cannot reconstruct the complete architecture version for this dataset')
        for name_ in missing:
            files[name_] = (Path(basis['bundle']['root']) / relative_file(name_)).read_bytes()
    if relative in sources['files'] or 'experiment.py' not in files or any(
            hashlib.sha256(v).hexdigest() != sources['files'][p]['sha256'] for p, v in files.items()):
        raise ValueError('Invalid or conflicting architecture source workspace')
    root = workspace(engine, run_id, evidence, key)
    _write_files(root, {**files, relative: script})
    body = {'binding': evidence['binding'], 'dataset': dataset, 'code_sources': basis_ref,
            'source': source, 'path': str(root / relative), 'relative_path': relative,
            'file_sha256': hashlib.sha256(script).hexdigest()}
    row = {'id': key, 'run_id': run_id, 'body': body, 'sha256': sha(body), 'owner': owner}
    stored = engine.store.mutate(COLLECTION, key, lambda old: old or row)
    if stored['body'] != body:
        raise ValueError('Existing Bayesian script artifact differs')
    return {'script_id': key, 'path': body['path'], 'script_sha256': body['file_sha256'], 'execution_authorized': False}


def verified(engine, run_id, evidence, settings):
    source = settings['selection_script']
    key = source_key(run_id, evidence, source)
    row = engine.store.get(COLLECTION, key) or {}
    body = row.get('body', {})
    owner = engine.store.get('handoffs', row.get('owner', '')) or {}
    if (row.get('run_id') != run_id or row.get('sha256') != sha(body) or body.get('binding') != evidence['binding']
            or body.get('source') != source or body.get('code_sources') != evidence['code_sources']
            or body.get('file_sha256') != settings.get('script_sha256')
            or owner.get('to_role') != 'engineer' or owner.get('run_id') != run_id):
        raise ValueError('工程师须先用 bayes.write_script 保存与本数据集、架构及计划完全一致的脚本')
    path = Path(body['path'])
    root = workspace(engine, run_id, evidence, key)
    if (path != root / body['relative_path'] or path.parent != root / 'scripts' or path.resolve() != path.absolute()
            or any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)() for p in (path, *path.parents))
            or path.read_bytes() != entry(source, run_id, key).encode()
            or hashlib.sha256(path.read_bytes()).hexdigest() != body['file_sha256']):
        raise ValueError('Reviewed Bayesian script file changed')
    return row
