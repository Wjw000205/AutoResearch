"""Room for complete frozen per-objection output, without changing paid inputs."""
from __future__ import annotations

MAX_OUTPUT = 48000
TOKENS_PER_ITEM = 600
STAGES = {'implementation_spec', 'implementation_document_review'}


def output_limit(context, purpose, base):
    stage = purpose.split(':', 1)[0]
    if stage not in STAGES:
        return base
    from . import implementation_issue_responses as issues

    def enabled(value):
        return type(value.get(issues.PROTOCOL)) is int and value[issues.PROTOCOL] == issues.VERSION

    if not enabled(context):
        return base
    key = 'actions' if stage == 'implementation_spec' else 'requirements'
    count = len(issues._contract(context)[key])
    completion_tokens = 0
    if stage == 'implementation_spec':
        from .implementation_author_revision import contract
        revision = contract(context)
        if revision is not None:
            completion_tokens = sum(300 + 150 * len(item['fields']) for item in revision['items'])
    # This bounds the extra reservation. A higher existing alignment cap is
    # preserved; provider/account and execution budget caps still apply.
    return max(base, min(MAX_OUTPUT, base + TOKENS_PER_ITEM * count + completion_tokens))
