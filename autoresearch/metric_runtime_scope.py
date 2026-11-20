"""Versioned source scope for metric measurements and decisions."""
from pathlib import Path
import hashlib

from .metric_contract import signature
from .metric_review_sources import _source_cuts

KEY = 'metric_runtime_scope'
VERSION = 1
ROOT = Path(__file__).resolve().parents[1]

# Select execution/contract entry points in mixed modules. The source compiler
# includes their same-module bindings; direct imports have their own entries.
SYMBOLS = {
    'engine': ('Engine._trial', 'Engine.check_evaluation_contract', 'Engine.cycle_validation_summary'),
    'lab': ('ExperimentRunner.run',),
    'metric_contract': ('compile_contract', 'validate_contract', 'runner_criteria', 'evaluate', 'decide'),
    'metric_protocol_revisions': ('verify_source', 'checked'),
    'benchmark_matrix': ('verify_sources', 'build', 'validate', 'task_for', 'task_trials', 'jobs',
                         'validation_summary', 'select', 'final_jobs', 'final_outcome', 'decide'),
    'comparison_task_policy': ('assignment',),
    'fair_comparison': ('canonical_dimensions', 'strict_equal', 'task_signature', 'assert_same_task',
                        'validate_plan', 'validate_invocation'),
    'experiment_plan': ('build_plan', 'check_outcome'),
    'experiment_workflow': ('coverage', 'ledger_and_searches'),
    'research_cycle': ('validation_summary', 'decide_cycle'),
    'performance_policy': ('classify', 'outcome', 'assess'),
    'evaluation': ('evaluate_trials',),
    'split_identity': ('validate', 'compatible'),
    'parameter_search': ('jobs', 'validate_invocation'),
    'python_training': ('jobs',),
    'comparison_transport': ('runtime_transport', 'read_final_output', 'acceptance_metric',
                             'scientific_params', 'ordered_validation'),
    'experiment_decisions': ('units', 'authorize_job'),
    'protocol_amendments': ('replay', 'replay_threshold', 'revision_hash'),
    'paper_baselines': ('guard', 'own_validation'),
    'parameter_grid': ('certified_selection', 'selected_summary'),
}


def sources(root=ROOT):
    rows = []
    for module, symbols in SYMBOLS.items():
        path = f'autoresearch/{module}.py'
        raw = (root / path).read_bytes()
        rows.append({'path': path, 'sha256': hashlib.sha256(raw).hexdigest(),
            'source': raw.decode('utf-8-sig'), 'source_policy': {'version': 2, 'required_symbols': sorted(symbols)},
            'provenance': 'exact_host_metric_runtime_source'})
    return rows


def identity(rows):
    """Hash the required declarations without the owning file's appendix bytes."""
    required = []
    if len(rows) != len(SYMBOLS):
        raise ValueError('Metric capability has an incomplete source inventory')
    for row, (module, symbols) in zip(rows, SYMBOLS.items()):
        if (row['path'] != f'autoresearch/{module}.py'
                or row.get('source_policy') != {'version': 2, 'required_symbols': sorted(symbols)}):
            raise ValueError('Metric capability source inventory differs from its versioned scope')
        source = row['source']
        start, pieces = 0, []
        for low, high, _ in _source_cuts(row):
            pieces.append(source[start:low])
            start = high
        pieces.append(source[start:])
        required.append((row['path'], ''.join(pieces).replace('\r\n', '\n').replace('\r', '\n')))
    return {'version': VERSION, 'sha256': signature(required)}
