"""Proportionate, actionable architecture reviews for new frozen assignments."""
from copy import deepcopy

VERSION = 1

DESIGN = '''
提交一个完整、自洽的实现合同，明确输入输出、关键接口、张量轴、数值参数、训练与评估步骤、
配置优先级、边界情况和验收方式。存在多个合理实现时选择一个并说明依据，不保留互斥方案供审批者猜测。
已批准方案未冻结的实现细节可以补充明确合理的默认值，标明为本次设计选择；
只有确实需要改变批准科学范围时才提出新共识请求。不要要求提前取得代码或实验结果才能完成架构。
'''

REVIEW = '''
本轮目标是把同一版本收束为可实现、可检验的统一设计。审批尺度是足够明确且没有实质阻塞，
不要求设计完美，也不要求此阶段提供代码、测量结果或最终论文证明。
只否决会改变批准科学范围、造成数据泄漏、比较不公平、关键步骤不可复现、无法满足资源约束，
或使验收无法执行的具体问题。表达风格、个人偏好、可选优化、合理但尚未实测的设计选择，
以及不影响当前实施的局限放入 suggestions；这些建议不阻止 approve=true。
作者可以为未冻结的实现细节选择一种明确、合理、与批准范围相容的 proposed_definition；
不因它是新增定义就要求重做科学共识，也不因存在其他合理实现就否决。
分页、当前窗口未包含的正文、后续组尚未读到的内容不能证明设计缺失；仅补读决定性范围。
同一轮一次列出已发现的全部实质阻塞，避免每轮只给一部分意见、反复加码或重提已解决问题。
每个必改项同时给出 fixes：issue_index 对应必改数组的下标，target 定位章节或字段，
reason 说明实际影响，proposed_change 写出可直接采用的完整修改，verification_plan 给出明确通过条件。
缺少决定性证据时，具体说明缺哪段原文、如何核验以及可行的最小处理方案；不虚构原文或测量。
多种可行修正中推荐一条自洽路径；跨项冲突必须指出并给出取舍，不能只让作者自行研究解决。
非阻塞建议写在 suggestions，必改数组只含真正阻塞项；批准时必改数组和 fixes 均为空。
保留独立判断、实际读取和版本绑定；建议或作者申报不代替真实批准。
'''

AUTHOR = '''
将全部实质修改意见合并成一个自洽的完整版本，并说明每项意见采用的修改和核验方式。
对冲突建议选择一条与批准科学范围相容的实现路径，说明选择依据，不并列留下互斥方案。
对未冻结且不改变科学范围的实现细节给出明确合理的默认值和规则；这些候选由本轮独立评议确认，
无需仅因需要补充实现定义就要求重做共识。只有科学问题、机制、比较、种子、阈值或试次等
批准范围确实必须改变时才请求新共识。不要用仅改措辞或扩大局限清单替代必改问题的实质修正。
'''

# Proposal and code schemas keep their existing remedy fields. Optional notes
# belong in rationale there, rather than an unrequested output property.
SCALE = REVIEW.split('每个必改项同时给出 fixes：', 1)[0].replace(
    '放入 suggestions', '在 rationale 中标明为非阻塞建议') + '''
每个否决项必须说明问题位置、实际影响、推荐的具体修改和可检验的通过条件。
使用当前输出协议已有的修改字段；不要增加未请求字段，也不要用可选建议阻塞批准。
'''


def schema(base, field):
    """Keep historical schemas intact; freeze this shape only on new work."""
    result = deepcopy(base)
    text = {'type': 'string', 'minLength': 1, 'maxLength': 1800, 'pattern': r'\S'}
    fix = {'type': 'object', 'properties': {
        'issue_index': {'type': 'integer', 'minimum': 0},
        **{key: deepcopy(text) for key in ('target', 'reason', 'proposed_change', 'verification_plan')}},
        'required': ['issue_index', 'target', 'reason', 'proposed_change', 'verification_plan'],
        'additionalProperties': False}
    result['properties'].update(
        fixes={'type': 'array', 'maxItems': 40, 'items': fix},
        suggestions={'type': 'array', 'maxItems': 20, 'items': deepcopy(text)})
    result['required'] = [*result['required'], 'fixes', 'suggestions']
    result.setdefault('allOf', []).append({
        'if': {'properties': {'approve': {'const': True}}, 'required': ['approve']},
        'then': {'properties': {field: {'maxItems': 0}, 'fixes': {'maxItems': 0}}},
        'else': {'properties': {field: {'minItems': 1}, 'fixes': {'minItems': 1}}}})
    return result


def validate(result, field):
    """Require a remedy for every blocker without judging scientific merit."""
    issues, fixes = result.get(field), result.get('fixes')
    if (type(result.get('approve')) is not bool or not isinstance(issues, list)
            or not isinstance(fixes, list) or not isinstance(result.get('suggestions'), list)):
        raise ValueError('架构评议必须明确批准状态、必改项、完整修改方案和非阻塞建议。')
    if result['approve']:
        if issues or fixes:
            raise ValueError('架构批准不能同时保留必改项。')
        return
    if not issues or any(not isinstance(f, dict) or type(f.get('issue_index')) is not int
                         for f in fixes):
        raise ValueError('架构否决必须说明具体阻塞并逐项提供修改方案。')
    if {f['issue_index'] for f in fixes} != set(range(len(issues))):
        raise ValueError('架构否决的每个必改项都必须有完整修改方案，不能漏项或引用未知问题。')
    if any(not isinstance(f.get(key), str) or not f[key].strip() for f in fixes
           for key in ('target', 'reason', 'proposed_change', 'verification_plan')):
        raise ValueError('架构修改方案须包含问题位置、影响、具体修改和验收方法。')


def validate_summary(result, context, field):
    validate(result, field)
    sections = context.get('section_reviews', context.get('definition_unit_reviews', []))
    if result['approve'] and any(v.get('approve') is not True or v.get(field) for v in sections):
        raise ValueError('分段仍有未解决必改问题，终审不能通过汇总意见放行。')
