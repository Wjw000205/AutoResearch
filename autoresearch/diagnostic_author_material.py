"""Keep the checkpoint producer's exact interfaces in every authoring turn."""
import ast
import hashlib
import io
import json

from .experiment_views import _safe_file


def _sources(files):
    # The probe includes nested restore/evaluate functions and its actual
    # optimizer construction and update loop; keep the whole file together.
    required = {
        'diagnostic_probe.py': (),
        'diagnostic_persistence.py': ('snapshot', 'save_state', 'Journal'),
        'src/models/factory.py': ('build_model',),
        'dataset_runtime/data.py': ('PreparedData', 'load_data'),
        'dataset_runtime/integrity.py': ('verify_invocation', 'verify_asset'),
        'dataset_runtime/training.py': ('predict_metrics',),
        'bounded_host.py': ('main',),
    }
    result = {}
    for path, symbols in required.items():
        raw = files[path]
        text = raw.decode('utf-8')
        if symbols:
            nodes = {n.name: n for n in ast.parse(text).body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
            lines = text.splitlines(keepends=True)
            snippets = []
            for symbol in symbols:
                node = nodes[symbol]
                start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
                snippets.append({'first_line': start, 'last_line': node.end_lineno,
                                 'text': ''.join(lines[start-1:node.end_lineno])})
        else:
            snippets = [{'first_line': 1, 'last_line': len(text.splitlines()), 'text': text}]
        result[path] = {'file_sha256': hashlib.sha256(raw).hexdigest(), 'source': snippets}
    return result


def material(engine, rid, binding, catalog, files):
    import torch
    entries = {r['path']: r for r in catalog}
    checkpoint = entries[binding['checkpoint_path']]
    config = entries[binding['config_path']]
    producer = engine.store.get('experiments', rid + ':' + binding['producer_trial_id'])
    path = _safe_file(engine, rid, producer, checkpoint['original_path'])
    if path is None or path.stat().st_size != checkpoint['bytes'] or checkpoint['bytes'] > 256 * 1024**2:
        raise ValueError('Continuation state unavailable for authoring')
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != checkpoint['sha256'] or digest != binding['checkpoint']['sha256']:
        raise ValueError('Continuation state changed before inspection')
    state = torch.load(io.BytesIO(raw), map_location='cpu', weights_only=True)
    item = binding['original_input']
    history = engine.store.get('causal_training_followups', f'{rid}:{engine.cycle(rid)}')
    if (state['input'] != item or state['protocol'] != history['protocol']
            or state['completed_updates'] != binding['checkpoint']['completed_updates']
            or state['resume_eligible'] is not True or state['next_batch_index'] != state['completed_updates']):
        raise ValueError('Continuation state differs from the committed input or cursor')
    opt = state['optimizer']
    return {'version': 1, 'scope': 'Host inspection of saved validation inputs, without model updates or evaluation.',
            'input_paths': [checkpoint['path'], config['path']], 'input_params': checkpoint['params'],
            'config_sha256': binding['config_sha256'], 'original_config_text': binding['config_text'],
            'original_input': item, 'checkpoint_sha256': digest,
            'state': {'keys': sorted(state), 'completed_updates': state['completed_updates'],
                'next_batch_index': state['next_batch_index'], 'resume_eligible': state['resume_eligible'],
                'remaining_batch_lengths': [len(b) for b in state['remaining_epoch_batches']],
                'model_shapes': {k: list(v.shape) for k, v in state['model'].items()},
                'adam_groups': opt['param_groups'],
                'adam_slots': {str(k): {n: list(t.shape) for n, t in slots.items()} for k, slots in opt['state'].items()},
                'torch_rng_shape': list(state['torch_rng'].shape),
                'numpy_rng_generator': state['permutation_rng']['bit_generator']},
            'sources': _sources(files)}


def instruction(packet):
    return ('\n以下为宿主核对本轮检查点后随任务固定交付的原文和结构，普通回合和最终交付均可直接使用。'
            '不授予任何投票或运行权限。按 next_task 直接编写完整候选；仅对具体缺项补读。'
            '使用原生产者的构造、训练损失与评估接口；从续接 state 恢复，两分支分别核对实际 Adam 参数顺序、'
            '随机状态和剩余批次。旧 probe 入口和前缀循环仅供核对。\n'
            + json.dumps(packet, ensure_ascii=False, sort_keys=True, allow_nan=False))
