"""Instructions for newly constructed implementation author requests.

Frozen handoffs own their original prose/schema; these builders never migrate
stored packets. Tool parameters remain owned by implementation_authoring.
"""
import json


AUTHOR_PROTOCOLS = (
    'implementation_whole_document_review_protocol',
    'implementation_acceptance_author_submission_protocol',
    'implementation_inventory_output_protocol',
)


def _author_protocols(context):
    """Read explicit versions from a frozen handoff or its certified phase view.

    The explicit top-level to hashed phase-contract adapter preserves absence
    for legacy inputs. This leaf validator must not recurse into assert_context.
    """
    from . import implementation_phase_context as phase
    contract = context.get(phase.CONTRACT, {})
    if not isinstance(contract, dict):
        raise ValueError('Invalid author phase contract')
    versions = {}
    if 'author_protocols' in contract:
        projected = contract['author_protocols']
        if not phase.enabled(context):
            raise ValueError('Author protocol binding requires a certified phase view')
        from .implementation_review_routing import digest
        if contract.get('sha256') != digest({k: v for k, v in contract.items() if k != 'sha256'}):
            # A damaged certified view is a host context-integrity failure,
            # just like the same hash failure in phase.assert_context. Keep
            # unsupported/conflicting protocol versions as contract errors.
            phase._fail('Invalid author protocol phase hash')
        if not isinstance(projected, dict) or set(projected) - set(AUTHOR_PROTOCOLS):
            raise ValueError('Unsupported projected author protocol binding')
        versions.update(projected)
    for key in AUTHOR_PROTOCOLS:
        if key in context:
            if key in versions and versions[key] != context[key]:
                raise ValueError('Conflicting author protocol versions')
            versions[key] = context[key]
    if any(type(version) is not int or version != 1 for version in versions.values()):
        raise ValueError('Unsupported author protocol version')
    return versions


def author_protocols(context):
    from . import implementation_phase_context as phase
    versions = _author_protocols(context)
    if 'author_protocols' in context.get(phase.CONTRACT, {}):
        phase.assert_context(context)
    return versions


def project_author(context):
    """Preserve new author protocol versions through both normal projections.

    Called only while constructing a new request. Replaying an already frozen
    transport packet never calls this adapter or upgrades an absent marker.
    """
    from . import implementation_phase_context as phase
    author_protocols(context)
    return phase.project_author(context)


def author_task(context):
    from . import implementation_authoring as author
    from . import implementation_acceptance as acceptance
    editing = (author.editing_enabled(context) or
               bool(context.get('implementation_document_authoring', {}).get('revision')))
    if editing and context.get('implementation_definition_repair_protocol') == 1:
        return ('\n用 implementation.edit_document 编辑 algorithm 的定义补充，绑定当前 document_id、'
            'expected_sha256、base_spec_hash。保留原 algorithm 及其他字段，未决事项写在补充小节。'
            '保存后核读同一版本的新增正文，按提交 schema 返回真实回执。随后由冻结的评审角色独立复核。')
    if not acceptance.enabled(context):
        return author.EDIT_TASK if editing else author.TASK
    fields = list(author.submission_schema(context)['properties'])
    from .implementation_whole_review import request_enabled
    next_review = ('总监和研究员分别结合架构整篇独立审核，收齐意见后工程师统一修订，两人通过同一版本才进入代码阶段。'
                   if request_enabled(context) else '后续按冻结职责派发独立岗位、同行、接口和全局审查；')
    return ('\n使用 ' + (author.EDIT_TOOL if editing else author.TOOL)
        + ' 保存本交接的真实当前稿；使用本请求工具 schema，标识取自实际基线或最新保存回执。'
        '保存失败时按具体诊断修复。成功保存后读取报告引用的同版本范围。'
        '最终返回完整对象，字段为 ' + ', '.join(fields)
        + '；文档指针来自成功保存回执，报告覆盖冻结的全部原问题和条件。'
        '可复用自检保留原始证据版本，不能替代本交接保存和报告阅读。'
        'revision_plan 和自检每次最多64项；分批保存并核对剩余清单，最终报告保留全部原问题和条件。'
        '保存和作者 complete 均不代表独立批准。' + next_review +
        '保留所有批准架构、方案、任务和科学条件，缺失或冲突交由正常来源澄清。')


def author_tool_help(context, name):
    from . import implementation_authoring as author
    from . import implementation_acceptance as acceptance
    if name == author.EDIT_TOOL and context.get('implementation_definition_repair_protocol') == 1:
        return ('Bind document_id, expected_sha256 and base_spec_hash to the current draft. '
            'Edit only /algorithm. For an indexed revision use index_id=algorithm and retain the '
            'original prefix when appending the definition. The host validates the full result before saving.')
    if not acceptance.enabled(context):
        return author.EDIT_HELP if name == author.EDIT_TOOL else author.HELP
    schema = (author.edit_arguments_schema(context) if name == author.EDIT_TOOL else
              author.write_arguments_schema(context['implementation_document_authoring']['specification_schema']))
    return ('Only the assigned author may save. arguments must satisfy this exact schema: '
        + json.dumps(schema, ensure_ascii=False) + '. Saving returns a document pointer, not approval. '
        'Submit the complete final schema including all required reports. Independent review follows frozen duties.')


def successor_instruction(instruction, context):
    """Explicit adapter for known host prose on a NEW successor only.

    Keep task-specific instructions verbatim. The original stored predecessor
    and every previously prepared transport packet stay untouched.
    """
    from . import implementation_authoring as author
    from .implementation_acceptance import enabled
    if not enabled(context):
        return instruction
    for legacy in (author.TASK, author.EDIT_TASK, author.PRESERVATION_TASK):
        instruction = instruction.replace(legacy, '')
    return instruction
