"""Host-owned phase routing for known reviewer-only historical conditions.

Unknown or mixed requirements remain author work. Never infer ownership from
the author's explanation or from a substring such as 'review' or 'receipt'.
The original issue and independent verification contract remain unchanged.
"""
from .implementation_review_routing import digest

REVIEW_RECEIPT = (
    '工程师回应后，以新交付回执绑定的同一SHA256实际读取修订范围并正式复核；'
    '关闭只核验定义完整和权限一致，不要求科学阈值已达成，作者自检不构成关闭、共识或执行授权。')


def review_pending(context):
    from .implementation_acceptance import enabled
    from .implementation_acceptance_author import enabled as author_enabled
    if not enabled(context) or not author_enabled(context):
        return []
    reviewers = {c['id']: c.get('reviewer') for c in context.get('acceptance_contract', {}).get('criteria', [])}
    result = []
    for issue in context.get('acceptance_issues', []):
        reviewer = issue.get('verifier')
        if (issue.get('owner') != 'engineer' or issue.get('confirmed') is not True
                or reviewer not in {'director', 'scout'}
                or reviewers.get(issue.get('criterion_id')) != reviewer):
            continue
        for index, requirement in enumerate(issue['close_when']):
            if requirement != REVIEW_RECEIPT:
                continue
            result.append({'issue_id': issue['issue_id'], 'index': index,
                'requirement': requirement, 'requirement_sha256': digest(requirement),
                'owner': reviewer, 'phase': 'independent_review', 'status': 'pending_review',
                'routing_version': 1, 'verified': False})
    return result


def review_keys(context):
    return {(r['issue_id'], r['index']) for r in review_pending(context)}


GUIDANCE = ('\nreview_pending 是宿主按冻结条件识别的后续独立复核职责。'
    '这些条件不要求作者取得审核回执，不计入作者修改、自检阻塞或补读待办；'
    '最终报告仍保留该条件，说明待职责审查者复核，不得声称作者已使其通过。'
    '所有作者职责完成且没有科学来源阻塞后，按正常保存和提交门禁送审。'
    '独立审核仍核验原 close_when 的全部条件；pending_review 不等于通过或关闭。')
