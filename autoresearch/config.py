from __future__ import annotations

import copy
import os
import tempfile
import threading
from pathlib import Path

from .storage import redact
from .codex_reasoning import DEFAULT_PROVIDER as CODEX_REASONING_PROVIDER

ROOT = Path(__file__).resolve().parent.parent
CONTEXT_TOKEN_MAX = 256000
DATA_DIR = Path(os.environ.get('AUTORESEARCH_DATA_DIR', ROOT / 'data')).resolve()
ENV_FILE = ROOT / '.env.local'
_SECRET_LOCK = threading.RLock()
_SECRET_NAMES = {'RUIKON_OPENAI_KEY', 'RUIKON_ANTHROPIC_KEY', 'RUIKON_GROK_KEY'}


def load_env():
    with _SECRET_LOCK:
        if ENV_FILE.exists():
            for line in ENV_FILE.read_text(encoding='utf-8-sig').splitlines():
                if '=' in line and not line.lstrip().startswith('#'):
                    key, value = line.split('=', 1)
                    os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def save_secrets(mapping: dict[str, str]):
    """Validate every credential, then replace the file once under one lock.

    Environment values change only after the complete file was written. A bad
    later entry cannot leave earlier provider credentials partially updated.
    """
    if not isinstance(mapping, dict):
        raise ValueError('Credentials must be a mapping')
    normalized = {}
    for name, value in mapping.items():
        if name not in _SECRET_NAMES:
            raise ValueError('Unknown credential variable')
        if not isinstance(value, str) or not value.strip() or any(c.isspace() or ord(c) < 32 for c in value.strip()) or len(value) > 512:
            raise ValueError('Invalid credential')
        normalized[name] = value.strip()
    if not normalized:
        return
    with _SECRET_LOCK:
        if ENV_FILE.is_symlink():
            raise ValueError('Credential file cannot be a symlink')
        lines = ENV_FILE.read_text(encoding='utf-8-sig').splitlines() if ENV_FILE.exists() else []
        lines = [line for line in lines if '=' not in line or line.split('=', 1)[0].strip() not in normalized]
        lines.extend(name + '=' + value for name, value in normalized.items())
        ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix='.env-save-', suffix='.tmp', dir=ENV_FILE.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as output:
                output.write('\n'.join(lines) + '\n')
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(ENV_FILE)
        finally:
            temporary.unlink(missing_ok=True)
        os.environ.update(normalized)


def save_secret(name: str, value: str):
    save_secrets({name: value})


DEFAULTS = {
    'providers': [
        {'id': 'openai', 'name': 'GPT · 研究统筹', 'protocol': 'responses', 'base_url': 'https://api.ruikon.com/v1', 'model': 'gpt-6-astra', 'api_key_env': 'RUIKON_OPENAI_KEY', 'context_window': 128000, 'max_output_tokens': 5000, 'input_price': 20.0, 'output_price': 100.0, 'cached_input_price': 20.0, 'cache_write_price': 20.0, 'price_verified': False, 'prompt_cache_enabled': True, 'responses_streaming': False, 'timeout_seconds': 120},
        {'id': 'anthropic', 'name': 'Claude · 架构与实现', 'protocol': 'anthropic', 'base_url': 'https://api.ruikon.com/v1', 'model': 'claude-sonnet-4-6', 'api_key_env': 'RUIKON_ANTHROPIC_KEY', 'context_window': 100000, 'max_output_tokens': 18000, 'input_price': 20.0, 'output_price': 100.0, 'cached_input_price': 20.0, 'cache_write_price': 20.0, 'price_verified': False, 'prompt_cache_enabled': True, 'responses_streaming': False, 'timeout_seconds': 120},
        {'id': 'grok', 'name': 'Grok · 文献与审查', 'protocol': 'chat', 'base_url': 'https://api.ruikon.com/v1', 'model': 'grok-composer-2.5-fast', 'api_key_env': 'RUIKON_GROK_KEY', 'context_window': 100000, 'max_output_tokens': 4000, 'input_price': 20.0, 'output_price': 100.0, 'cached_input_price': 20.0, 'cache_write_price': 20.0, 'price_verified': False, 'prompt_cache_enabled': True, 'responses_streaming': False, 'timeout_seconds': 120},
        copy.deepcopy(CODEX_REASONING_PROVIDER),
    ],
    'roles': {'director': 'openai', 'engineer': 'anthropic', 'scout': 'grok'},
    'defaults': {'ccf_ab_only': False, 'python_executable': '', 'gpu_index': None, 'vram_limit_gb': None, 'cpu_limit_cores': None, 'ram_limit_gb': None, 'disk_limit_gb': None, 'implementation_backend': 'local_codex', 'codex_project_dir': str(ROOT), 'codex_model': '', 'codex_reasoning_effort': '', 'codex_timeout_seconds': 1800, 'codex_max_revisions': 3, 'budget_usd': None, 'max_calls': 150, 'max_rounds': 3, 'discussion_mode': 'until_stalled', 'stagnation_rounds': 3, 'architecture_max_revisions': None, 'architecture_stagnation_rounds': 3, 'max_trials': 12, 'max_research_cycles': None, 'min_relative_improvement': 0.01, 'context_tokens': CONTEXT_TOKEN_MAX, 'experiment_timeout': 120, 'unknown_cost_reserve_usd': 0.25},
    'cache_note': '共享证据、检索与工具结果；模型响应按角色/供应商隔离。远程 KV 由供应商管理，无法跨模型共享；仅将服务端 usage 中的缓存命中计为 KV 命中。默认价格是保守的预算估算，不是瑞科实际单价。',
}

DEFAULTS['defaults'].update({
    'memory_semantic_enabled': False,
    'memory_lexical_candidates': 64,
    'memory_semantic_candidates': 64,
    'memory_top_k': 6,
    'memory_rrf_k': 60,
    'memory_semantic_min_score': 0.2,
    'evidence_top_k': 8,
    'evidence_max_results': 12,
    'evidence_lexical_candidates': 64,
    'evidence_semantic_candidates': 64,
    'evidence_rrf_k': 60,
    'evidence_semantic_enabled': True,
    'execution_archive_inline_bytes': 65536,
    'execution_archive_retention_days': None,
    'implementation_review_strategy': 'acceptance_contract',
    'implementation_indexed_review_protocol': 1,
    'implementation_review_max_revisions': 2,
    'implementation_review_max_seconds': 14400,
    'implementation_review_time_basis': 'policy_active_time',
    'implementation_review_max_calls': 240,
})

RETIRED_IMPLEMENTATION_REVIEW_FIELDS = frozenset({
    'implementation_review_batch_protocol',
    'implementation_review_batch_max_units',
    'implementation_review_batch_max_chars',
    'implementation_incremental_review_protocol',
    'implementation_whole_alignment_protocol',
    'implementation_section_review_protocol',
})


def get_settings(store):
    load_env()
    saved = store.get('settings', 'main')
    current = copy.deepcopy(DEFAULTS)
    if not saved:
        return current
    # Existing installations keep their explicit choices while gaining newly
    # introduced fields. A shallow replacement breaks partial/older settings.
    current.update({key: copy.deepcopy(value) for key, value in saved.items() if key not in {'providers', 'roles', 'defaults'}})
    current['roles'].update(copy.deepcopy(saved.get('roles', {})))
    current['defaults'].update(copy.deepcopy(saved.get('defaults', {})))
    # Stored historical defaults remain untouched. New tasks have one active
    # implementation review protocol and cannot reactivate retired dispatch.
    for name in RETIRED_IMPLEMENTATION_REVIEW_FIELDS:
        current['defaults'].pop(name, None)
    current['defaults']['implementation_review_strategy'] = 'acceptance_contract'
    current['defaults']['implementation_indexed_review_protocol'] = 1
    current['defaults']['implementation_backend'] = 'local_codex'
    current['defaults']['implementation_review_time_basis'] = 'policy_active_time'
    for name in ('implementation_review_max_revisions', 'implementation_review_max_seconds', 'implementation_review_max_calls'):
        if current['defaults'].get(name) is None:
            current['defaults'][name] = DEFAULTS['defaults'][name]
    by_id = {provider['id']: provider for provider in current['providers']}
    for provider in saved.get('providers', []):
        existing = by_id.get(provider.get('id'))
        if existing is not None:
            existing.update(copy.deepcopy(provider))
        else:
            current['providers'].append(copy.deepcopy(provider))
    return current


def public_settings(settings):
    from .codex_executable import resolve_codex
    value = copy.deepcopy(settings)
    for provider in value['providers']:
        provider['configured'] = (resolve_codex()['available'] if provider.get('protocol') == 'local_codex'
                                  else bool(os.environ.get(provider['api_key_env'])))
        provider.pop('api_key', None)
    return redact(value)
