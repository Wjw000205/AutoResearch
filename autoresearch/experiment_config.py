"""Versioned experiment configuration and a portable command-line entry.

The resolved configuration is the execution input. Dataset changes never select
an architecture workflow. The same loader is copied into reviewed deliveries.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

INTERFACE = 'experiment_config_v1'


def load(argv=None):
    parser = argparse.ArgumentParser(description='Run one configured experiment')
    parser.add_argument('legacy_config', nargs='?')
    parser.add_argument('--config')
    parser.add_argument('--dataset')
    parser.add_argument('--variant', choices=['baseline', 'proposed', 'ablation'])
    parser.add_argument('--seed', type=int)
    parser.add_argument('--phase', choices=['validation', 'test'])
    args = parser.parse_args(argv)
    if bool(args.config) == bool(args.legacy_config):
        parser.error('Supply exactly one configuration file')
    value = json.loads(Path(args.config or args.legacy_config).read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Configuration must be a JSON object')
    value = copy.deepcopy(value)
    if 'datasets' in value:
        if not isinstance(value['datasets'], dict) or not args.dataset or args.dataset not in value['datasets']:
            raise ValueError('Select a configured dataset with --dataset')
        selected = value.pop('datasets')[args.dataset]
        if not isinstance(selected, dict):
            raise ValueError('Dataset profile must be a JSON object')
        if any(key.endswith('_params') for key in selected):
            variant = args.variant or value.get('variant')
            if variant not in {'baseline', 'proposed', 'ablation'}:
                raise ValueError('Select a comparison variant with --variant')
            selected = selected.get(variant + '_params')
            if not isinstance(selected, dict):
                raise ValueError('The selected comparison variant has no configuration')
        value.update(selected)
        value['dataset'] = args.dataset
    elif args.dataset is not None and value.get('dataset') != args.dataset:
        raise ValueError('Dataset argument does not match the resolved configuration')
    for field in ('variant', 'seed', 'phase'):
        argument = getattr(args, field)
        if argument is not None:
            value[field] = argument
    if type(value.get('seed')) is not int or not 0 <= value['seed'] < 2**32:
        raise ValueError('An explicit integer seed is required')
    if value.get('phase') not in {'validation', 'test'}:
        raise ValueError('An explicit validation/test phase is required')
    if value.get('variant') not in {'baseline', 'proposed', 'ablation'}:
        raise ValueError('An explicit comparison variant is required')
    return value


ENTRY = '''"""Run the saved experiment configuration using the reviewed data adapter."""
from pathlib import Path
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from dataset_runtime.config import load
from dataset_runtime.adapter import run
if __name__ == "__main__":
    result = run(load())
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
'''


def command_arguments(params):
    """No shell strings: each frozen selector is one argv element."""
    if not isinstance(params.get('dataset'), str) or not params['dataset']:
        raise ValueError('Configured execution requires a dataset selector')
    if type(params.get('seed')) is not int or params.get('phase') not in {'validation', 'test'}:
        raise ValueError('Configured execution requires explicit phase and seed')
    if params.get('variant') not in {'baseline', 'proposed', 'ablation'}:
        raise ValueError('Configured execution requires an explicit variant')
    return ['--config', 'params.json', '--dataset', params['dataset'], '--phase', params['phase'],
            '--variant', params['variant'], '--seed', str(params['seed'])]
