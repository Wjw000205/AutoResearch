"""The actual host/adapter interface, shared by code delivery and review."""


def describe():
    return {
        'version': 2,
        'working_directory': 'The isolated trial directory containing experiment.py, params.json, environment.json, dataset_runtime/ and staged datasets/.',
        'configuration': {
            'file': 'params.json', 'kind': 'fully resolved flat parameters',
            'selectors': ['dataset', 'variant', 'seed', 'phase'],
            'sha256': 'environment.json.params_sha256',
            'hash_encoding': "SHA256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode('utf-8')); use Python's default separators including spaces.",
            'authority': 'Host approved plan and exact invocation parameters; do not reread a fixed dataset profile to override CLI parameters.',
            'resolution': 'The host dispatches the selected *_params plus dataset, dataset_assets, benchmark metadata, variant, phase and seed. Root configuration fields are not automatically copied into these flat parameters. Declare required runtime fields in each profile, or obtain non-parameter metadata from its frozen configuration without overriding invocation values.',
        },
        'asset_verification': {
            'file': 'environment.json', 'field': 'dataset_assets',
            'entry_fields': ['asset_id', 'manifest_sha256', 'files', 'provenance'],
            'file_fields': ['path', 'trial_path', 'bytes', 'sha256', 'staged_stat'],
            'stat_fields': ['size', 'mtime_ns', 'ctime_ns', 'device', 'inode'],
            'stat_encoding': 'Use dataset_runtime.host_identity.portable_stat(path.stat()) for comparisons. On Windows device is the unsigned low 32 bits of st_dev so Python versions agree; other fields are exact. Other platforms retain the full device number. This helper is supplied and protected by the host.',
            'guarantee': 'Before execution the host verifies the saved manifest and every entire source/staged file by size and SHA256. Staged stat records the copied file identity.',
            'adapter_checks': 'Match config.dataset_assets and configured file binding to this receipt; use trial_path under cwd and verify file stat before/after reading permitted partitions. Never invent a receipt or substitute fixture metadata for a real asset.',
            'manifest_sha256': 'Canonical JSON identity of the saved manifest, not the raw-byte SHA of a staged manifest file.',
            'available_files': 'Only dataset files are staged. There is no acquisition manifest.json, dataset_runtime_binding or host-generated partition hash map in bootstrap.json.',
            'partitions': 'The adapter derives split fingerprints from the allowed data it actually reads. Validation must not load held-out values; full-asset byte verification is the host responsibility.',
        },
        'cpu_checks': {
            'execution': 'checks.py source is embedded into trial-root experiment.py; cwd is the trial directory. Use Path.cwd(), not the saved checks.py source location.',
            'params': {'phase': 'code_check', 'dataset_assets': 'current asset bindings'},
            'configuration_file': 'dataset_runtime/configurations.json',
            'dispatch_samples_file': 'dataset_runtime/dispatch_samples.json contains representative actual frozen host parameters for each variant. Check these flat invocations as well as CLI profile loading; they can differ in root metadata.',
            'environment': 'Real staged-asset receipts are present in environment.json. Its params_sha256 binds the code_check parameters, not any candidate training configuration loaded by assertions.',
            'scope': 'Read only allowed train/validation data, CPU shapes/forward/gradient and CLI contract assertions. No full training or held-out reads. Test-only metadata/miniature inputs must stay isolated from real asset checks.',
            'resource_caps': {'cpu_cores':2,'memory_mb':2048,'disk_mb':1024,'timeout_seconds':120,'cpu_seconds':120,'gpu_allowed':False},
            'resource_caps_policy': 'Each CPU check limit is the smaller of its cap and the admitted task resource limit; training retains its separately approved limits.',
        },
        'authorization': 'This interface describes evidence transport. Independent configuration review, approved dataset budget, resource limits and execution authorization remain host gates.',
    }


def dispatch_samples(plan):
    from .python_training import jobs
    result = {}
    for name, params in jobs(plan, 'experiments') + jobs(plan, 'tuning'):
        result.setdefault((params.get('benchmark_task_id'),params.get('dataset'),params['variant']),
            {'trial_name':name, 'params':params})
    return list(result.values())
