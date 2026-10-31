"""Host entry for a separately reviewed, single diagnostic batch.

Copied into the immutable runner bundle; no imports from the live application.
"""
import hashlib
import json
import math
import sys
from pathlib import Path


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    params = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    protocol = json.loads(Path('bounded_protocol.json').read_text(encoding='utf-8'))
    actual_protocol = hashlib.sha256(json.dumps(protocol, sort_keys=True, ensure_ascii=False,
        allow_nan=False).encode('utf-8')).hexdigest()
    if actual_protocol != params['bounded_protocol_sha256']:
        raise ValueError('Diagnostic protocol hash differs from the approved request')
    limits = json.loads(Path('bootstrap.json').read_text(encoding='utf-8'))
    from diagnostic_guard import constrain
    constrain(min(limits['cpu_cores'], protocol['limits']['cpu_cores']))
    protected = json.loads(Path('bounded_inputs.json').read_text(encoding='utf-8'))
    if any(digest(p) != v for p, v in protected.items()):
        raise ValueError('Diagnostic inputs differ from reviewed bytes')
    # Numerical imports happen only after the host CPU affinity gate.
    from bounded_diagnostic import run
    result = run(protocol, params)
    if (not isinstance(result, dict) or not isinstance(result.get('summary'), dict)
            or type(result.get('optimizer_steps')) is not int
            or not 0 <= result['optimizer_steps'] <= protocol['max_updates']):
        raise ValueError('Diagnostic result needs a summary and bounded actual update count')
    if ('required_updates' in protocol and result['optimizer_steps'] != protocol['required_updates']):
        raise ValueError('Incomplete diagnostic update count cannot be reported as completed')
    if any(digest(p) != v for p, v in protected.items()):
        raise ValueError('Diagnostic modified an original input or runtime module')
    metrics = result.get('metrics', {})
    if not metrics or any(type(v) not in (int, float) or not math.isfinite(v) for v in metrics.values()):
        raise ValueError('Diagnostic metrics must be finite measured numbers')
    report = {'protocol_sha256': actual_protocol, 'protocol': protocol,
              'execution_status': 'completed', 'input_hashes_unchanged': True,
              'optimizer_steps': result['optimizer_steps'], 'summary': result['summary'],
              'measurements': result.get('measurements', {}), 'scientific_approval': False}
    path = Path('artifacts/bounded_diagnostic.json')
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    print(json.dumps({'metrics': metrics, 'artifacts': [str(path)],
        'diagnostic_report_sha256': digest(path), 'bounded_protocol_sha256': params['bounded_protocol_sha256']}))


if __name__ == '__main__':
    main()
