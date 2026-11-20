"""Read-only admission for a saved scientific definition before worker startup."""


def resume_blockers(run, revision, definition):
    if run.get('stage') != 'design' or run.get('config', {}).get('architecture_revision_protocol') != 1:
        return []
    problems = []
    cycle = run.get('state', {}).get('research_cycle', 1)
    if (not revision or revision.get('run_id') != run.get('id')
            or revision.get('cycle') != cycle):
        return ['Current architecture review binding is missing or belongs to another cycle']
    key = revision.get('definition_handoff_id')
    if not key:
        return []
    if (not definition or definition.get('id') != key
            or definition.get('run_id') != run.get('id') or definition.get('cycle') != cycle):
        return ['Current definition review binding is missing or belongs to another cycle']
    status = definition.get('status')
    from .architecture_definition_full import FIELD as FULL
    from . import independent_full_review as full_review
    full_policy=definition.get(FULL)
    if full_policy is not None and (not isinstance(full_policy,dict) or full_policy.get('version')!=1):
        return problems+['Unknown complete definition review protocol']
    expected_roles=full_review.REVIEWERS if full_policy else full_review.LEGACY_REVIEWERS
    if status in {'rejected', 'blocked'} and not full_policy:
        problems.append('Current definition candidate has a terminal veto or unresolved definition gate')
    elif status not in {'prepared', 'drafting', 'reviewing', 'approved','rejected','blocked'}:
        problems.append('Current definition status does not identify a resumable original handoff')
    votes = definition.get('votes', [])
    if not isinstance(votes, list):
        return problems + ['Current definition votes are malformed']
    document = definition.get('document') or {}
    document_sha = document.get('sha256') if isinstance(document, dict) else None
    if (votes or status in {'reviewing', 'approved'}) and (not isinstance(document_sha, str) or not document_sha):
        problems.append('Current definition document version is missing')
    seen, valid_votes = set(), []
    for vote in votes:
        if not isinstance(vote, dict):
            problems.append('Current definition vote is malformed')
            continue
        valid_votes.append(vote)
        role = vote.get('role')
        if not isinstance(role, str) or role not in expected_roles or role in seen:
            problems.append('Current definition votes have duplicate or unknown roles')
        if isinstance(role, str):
            seen.add(role)
        if (type(vote.get('approve')) is not bool
                or not isinstance(vote.get('required_changes'), list)
                or any(not isinstance(change, str) for change in vote.get('required_changes', []))):
            problems.append('Current definition vote conclusion is malformed')
        if vote.get('document_sha256') != document_sha:
            problems.append('Current definition vote refers to a different document version')
    full = seen == set(expected_roles) and len(valid_votes) == len(votes) == len(expected_roles)
    if full_policy and votes and not full_review.members(votes):
        problems.append('Complete definition reviews have mixed or invalid frozen policies')
    if full and not full_policy and any(vote.get('approve') is not True or vote.get('required_changes') for vote in valid_votes):
        problems.append('All independent opinions are saved and the current definition has not passed')
    if status == 'approved' and not full:
        problems.append('Approved definition is missing its three independent opinions')
    return sorted(set(problems))


def assert_definition_resumable(engine, run):
    if (run.get('status') not in {'paused', 'blocked', 'failed'} or run.get('stage') != 'design'
            or run.get('config', {}).get('architecture_revision_protocol') != 1):
        return
    from . import architecture_revisions
    revision = architecture_revisions.state(engine, run['id'])
    if revision is None:
        # A first architecture review has no saved definition to replay. Its
        # existing graph gates still decide whether work may proceed.
        return
    definition = engine.store.get('architecture_definitions', revision.get('definition_handoff_id', ''))
    if resume_blockers(run, revision, definition):
        raise ValueError('当前科学定义仍有否决、未闭合意见或版本绑定无法核验，不能直接恢复执行。'
                         '请先处理保存的具体意见并准备下一版定义评议；原检查点与投票保留。')
