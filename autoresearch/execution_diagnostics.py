"""Bounded process-reported failure details; never execution/approval evidence."""
import json

from .storage import redact


def failure_diagnostic(record):
    """Expose the cause hidden by nonzero-exit handling, including old records.

    Failed stdout must never populate accepted metrics or evaluation metadata.
    The complete original output remains in the immutable execution record.
    """
    if record.get('status') == 'completed':
        return None
    output = record.get('stdout')
    if not isinstance(output, str) or not output.strip():
        return None
    line = output[-65536:].rstrip().rsplit('\n', 1)[-1]
    try:
        payload = json.loads(line)
    except (ValueError, RecursionError):
        return None
    evaluation = payload.get('evaluation') if isinstance(payload, dict) else None
    failure = evaluation.get('failure') if isinstance(evaluation, dict) else None
    if not isinstance(failure, dict) or not isinstance(failure.get('reason'), str):
        return None
    reason = failure['reason'].strip()[:240]
    if not reason:
        return None
    result = {'reason': reason, 'source': 'failed_process_stdout'}
    details = failure.get('details')
    if isinstance(details, dict):
        receipt = details.get('receipt')
        if isinstance(receipt, dict):
            if isinstance(receipt.get('reason'), str):
                result['detail_reason'] = receipt['reason'][:240]
            coverage = receipt.get('timing_coverage')
            if isinstance(coverage, dict) and isinstance(coverage.get('missing'), list):
                result['missing'] = [v[:120] for v in coverage['missing'][:8] if isinstance(v, str)]
        trace = details.get('traceback')
        if isinstance(trace, list):
            result['locations'] = [{'file': row['file'][:240], 'line': row['line']}
                for row in trace[-4:] if isinstance(row, dict) and isinstance(row.get('file'), str)
                and type(row.get('line')) is int and 0 < row['line'] < 10**7]
    return redact(result)


def failure_message(diagnostic):
    message = '本地进程失败：' + diagnostic['reason']
    if diagnostic.get('detail_reason'):
        message += '；明细：' + diagnostic['detail_reason']
    if diagnostic.get('missing'):
        message += '；未覆盖：' + ', '.join(diagnostic['missing'])
    if diagnostic.get('locations'):
        message += '；代码位置：' + ', '.join(f"{r['file']}:{r['line']}" for r in diagnostic['locations'])
    return message
