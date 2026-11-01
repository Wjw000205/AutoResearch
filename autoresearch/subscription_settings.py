"""Validation for subscription-backed connections (no API credentials/rates)."""
import math


def validate(found, candidate):
    protocol = found['protocol']
    allowed = {'name', 'context_window', 'max_output_tokens', 'timeout_seconds', 'deep_timeout_seconds'}
    allowed |= {'model', 'reasoning_effort', 'deep_reasoning_effort'}
    if candidate.get('protocol', protocol) != protocol or candidate.get('api_key'):
        raise ValueError('套餐通道不接受 API 密钥或其他协议')
    value = {**found, **{k: v for k, v in candidate.items() if k in allowed}}
    for field in ('name', 'model'):
        if not isinstance(value.get(field), str) or not value[field].strip() or len(value[field]) > 160:
            raise ValueError('名称和模型标识必须为非空文本')
    for field in ('timeout_seconds', 'deep_timeout_seconds'):
        if type(value.get(field)) not in (int, float) or not math.isfinite(value[field]) or not 60 <= value[field] <= 14400:
            raise ValueError('套餐通道等待时间必须在 60 至 14400 秒之间')
    for field, low, high in [('context_window', 4000, 256000), ('max_output_tokens', 64, 32000)]:
        if type(value.get(field)) is not int or not low <= value[field] <= high:
            raise ValueError('上下文或输出上限无效')
    if any(value.get(k) not in {'low', 'medium', 'high', 'xhigh', 'max', 'ultra'}
                                        for k in ('reasoning_effort', 'deep_reasoning_effort')):
        raise ValueError('本机 Codex 推理强度无效')
    return value
