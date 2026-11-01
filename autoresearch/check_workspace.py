"""Compact, plain directories for explicitly reviewed rapid CPU checks."""
import re

LAYOUT = 'rapid-v1'


def attempt_path(root, run_id, trial_id, attempt, params, limits):
    layout=limits.get('check_workspace_layout')
    if layout is None:
        return root/run_id/trial_id/f'attempt-{attempt:03d}'
    if (layout!=LAYOUT or params.get('phase')!='code_check'
            or not re.fullmatch(r'rapid-check-[a-f0-9]{24}',trial_id)):
        raise ValueError('Compact workspace requires a rapid CPU check')
    # Keep the full existing 96-bit check ID and run directory. Shorten only
    # redundant labels; no junction, drive alias, relocation or result reuse.
    return root/run_id/'_c'/trial_id.removeprefix('rapid-check-')/str(attempt)
