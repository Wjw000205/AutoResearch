"""Instrument assertions without rewriting source into the host's Python syntax."""
import ast


def split_future_prefix(source):
    """Keep author future imports before the host instrumentation preamble."""
    tree = ast.parse(source)
    last = max((n.end_lineno for n in tree.body if isinstance(n,ast.ImportFrom) and n.module=='__future__'),default=0)
    lines=source.splitlines(keepends=True)
    return ''.join(lines[:last]), ''.join(lines[last:])


def instrument(source, counter='__dataset_passed'):
    if counter not in {'__dataset_passed', '__rapid_passed'}:
        raise ValueError('Unknown host assertion counter')
    tree = ast.parse(source)
    assertions = [node for node in ast.walk(tree) if isinstance(node, ast.Assert)]
    if not assertions:
        raise ValueError('Check code requires actual assertions')
    prefix = counter.rsplit('_', 1)[0] + '_'
    if any(isinstance(node, ast.Name) and node.id.startswith(prefix) for node in ast.walk(tree)):
        raise ValueError('Check code cannot overwrite host assertion counters')
    raw = source.encode('utf-8')
    lines = raw.splitlines(keepends=True)
    starts, offset = [], 0
    for line in lines:
        starts.append(offset)
        offset += len(line)
    # AST columns are UTF-8 byte offsets. Inserting after the assert (before a
    # comment or following statement) preserves quoting, Unicode and branches.
    edits = [(starts[node.end_lineno - 1] + node.end_col_offset,
              ('; globals()[%r].add(%d)' % (counter, index)).encode())
             for index, node in enumerate(assertions)]
    for position, marker in sorted(edits, reverse=True):
        raw = raw[:position] + marker + raw[position:]
    return raw.decode('utf-8'), len(assertions)
