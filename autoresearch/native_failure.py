"""Conservative classification of sanitized native service errors.

Intermediate retry notices describe the past. Only the final notice determines
whether retries are still active; every observed cause must remain transient.
These predicates never authorize dispatch without the host recovery gates.
"""


def transient_error(error):
    return (isinstance(error, dict)
        and isinstance(error.get('category'), str)
        and type(error.get('will_retry')) is bool
        and (error.get('http_status') is None or type(error.get('http_status')) is int)
        and (error.get('category') in {'serverOverloaded', 'internalServerError'}
            or error.get('category') in {'httpConnectionFailed', 'responseStreamConnectionFailed',
                'responseStreamDisconnected', 'responseTooManyFailedAttempts'}
            and error.get('http_status') in {None, 408, 429, 500, 502, 503, 504}))


def retryable_failure(checkpoint):
    errors = checkpoint.get('server_errors')
    return (checkpoint.get('status') == 'failed'
        and checkpoint.get('error_code') == 'codex_turn_failed'
        and checkpoint.get('public_messages') == [] and not checkpoint.get('activities')
        and not checkpoint.get('non_retryable_server_error')
        and not checkpoint.get('public_output_truncated') and not checkpoint.get('native_tool_violation')
        and isinstance(errors, list) and bool(errors)
        and all(transient_error(error) for error in errors)
        and errors[-1]['will_retry'] is False)
