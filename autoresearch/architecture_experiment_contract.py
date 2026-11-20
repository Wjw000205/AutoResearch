"""Versioned architecture reasoning; proposals are not execution permission."""
import copy

from .training_adjustment import obj

KEY = 'architecture_review'
PROTOCOL = 'architecture_review_protocol'
TEXT = {'type': 'string', 'minLength': 1}
TEXTS = {'type': 'array', 'items': TEXT, 'minItems': 1, 'uniqueItems': True}
CORRECTION = 'implementation_correction'
DEFECT = obj({'module_paths': TEXTS, 'approved_requirement': TEXT, 'observed_behavior': TEXT,
              'reproduction': TEXT, 'regression_check': TEXT})
CANDIDATE = obj({
    'id': TEXT, 'kind': {'enum': ['keep_current', 'replace_module', 'modify_module']},
    'module_paths': TEXTS, 'current_mechanism': TEXT, 'proposed_mechanism': TEXT,
    'evidence_paths': TEXTS, 'expected_benefit': TEXT, 'competing_explanation': TEXT,
    'capacity_and_cost': TEXT, 'interface_and_initialization': TEXT,
    'validation_prediction': TEXT, 'falsification': TEXT, 'tradeoff': TEXT})
SCHEMA = obj({'version': {'const': 1},
    'candidates': {'type': 'array', 'minItems': 2, 'maxItems': 4, 'items': CANDIDATE},
    'selected_id': TEXT, 'selection_rationale': TEXT,
    'control_trial_ids': TEXTS, 'rollback_condition': TEXT,
    'preserve_frozen_plan': {'const': True}, 'reuse_completed_controls': {'const': True}})

GUIDANCE = '''
架构评估协议 v1：根据当前模型的真实 validation 结果、对照/消融、训练轨迹和已读源码，
主动比较机制层面的局部优化，而不只寻找工程错误。区分工程维修、架构实验、YAML 参数搜索。
director 核查瓶颈与科学假设；engineer 比较模块实现、接口、容量和成本；scout 查已有 RAG、论文方法及反证。
先复用已有实验和读过的文献；需要方法依据时可 literature.rag_search/rag_read，缺少时定向检索并读原文。
论文中的较好结果不等于当前任务上更好；引用来源与适用条件，未验证的收益明确写成假设。
accept_architecture 或结构性 revise_architecture 必须填写 architecture_review：至少比较“保留当前架构”和
一个有依据的模块替换/模块机制修改候选（最多三个修改候选），只选择一个下一步。不要为制造架构变化而换模块。
每个候选写出已读模块位置、当前/拟议机制、具体实测 evidence_paths、预期收益、竞争解释、容量/成本、
接口和初始化公平性、可证伪的验证预期及取舍；control_trial_ids 引用已完成 validation 控制。
保持数据、划分、指标、baseline、预算及原冻结种子覆盖；新计划默认单个固定模型种子，不新增多种子实验。
只改一个机制；拆解不了的多机制组合先不执行。精确匹配的已完成控制必须复用，不重跑完成的实验。
结构性候选用 diagnosis=architecture、action=revise_architecture、unit_id=null 和具体 changes；有训练告警时
training_response.disposition=architecture_experiment，保留真实 cause/cause_status 和 followup_outcome。
未知训练根因不禁止提出可证伪的结构假设，也不证明结构有错；这条分支不解除告警、不宣称训练稳定。
只修日志、接口、回执或测试用 diagnose + instrument/repair_code；已请求的 YAML 首次迁移保留专门流程。
已支持的 YAML 参数调整走参数搜索。工程维修次数和 CPU 通过不得算架构改善。
若选择保留当前架构，具体解释为何候选证据/收益不足，并走 accept_architecture 的独立准入，不强制改码。
新候选仍须三方修改决策、Codex 交付、独立差异审查、真实 CPU 行为检查及资源门禁；
只有随后实际配对 validation 才能评价改善，若无增益或有回归，保留负结果并按回退条件处理。
'''


def enabled(evidence):
    scope = evidence.get('dataset_optimization') or {}
    return scope.get('phase') == 'architecture' and not scope.get('architecture_frozen')


def schema(base, evidence, *, generation=False, implementation_correction=False):
    result = copy.deepcopy(base)
    result['properties'][KEY] = copy.deepcopy(SCHEMA)
    response = result['properties'].get('training_response')
    if response:
        values = response['properties']['disposition']['enum']
        if 'architecture_experiment' not in values:
            values.append('architecture_experiment')
    if generation and enabled(evidence):
        conditions = [{'properties': {'action': {'const': 'accept_architecture'}}, 'required': ['action']},
                      {'properties': {'action': {'const': 'revise_architecture'},
                                      'diagnosis': {'const': 'architecture'}}, 'required': ['action', 'diagnosis']}]
        result.setdefault('allOf', []).append({'if': {'anyOf': conditions}, 'then': {'required': [KEY]}})
        if not evidence.get('hyperparameter_yaml', {}).get('migration_required') and not implementation_correction:
            result['allOf'].append({'if': {'properties': {'action': {'const': 'revise_architecture'}}, 'required': ['action']},
                'then': {'properties': {'diagnosis': {'const': 'architecture'}}, 'required': [KEY]}})
    if implementation_correction and enabled(evidence) and not evidence.get('hyperparameter_yaml', {}).get('migration_required'):
        result['properties'][CORRECTION] = copy.deepcopy(DEFECT)
        repair = {'action': {'const': 'revise_architecture'}, 'diagnosis': {'const': 'implementation'}}
        result.setdefault('allOf', []).extend([
            {'if': {'properties': repair, 'required': list(repair)}, 'then': {'required': [CORRECTION]}},
            {'if': {'required': [CORRECTION]}, 'then': {'properties': {**repair, KEY: False,
                'unit_id': {'type': 'null'}, 'changes': {'minItems': 1},
                'training_response': {'properties': {'disposition': {'const': 'repair_code'}}}}}},
            {'if': {'properties': {'action': {'const': 'revise_architecture'}}, 'required': ['action']},
             'then': {'properties': {'diagnosis': {'enum': ['implementation', 'architecture']}}}}])
    return result


def instruction(text, *, proposal_version=3):
    # Applied only to new versioned requests, never to an old paid assignment.
    text = text.replace('仅修改既有模块', '允许按批准修改单替换既有模块或新增其必要实现文件')
    text = text.replace('若需局部架构修改或诊断插桩，unit_id=null 并明确 changes，转快速代码修订和小批验证。',
        'diagnose 的代码修改限于诊断插桩或工程维修；结构性修改使用 revise_architecture。')
    text = text.replace('缺少可区分原因的观测时优先 instrument：diagnose、unit_id=null、最小插桩 changes，交给 Codex 修复后按原计划复验。',
        '若缺少拟议验证必需的观测，提出最小取证；已有观测能定义可证伪的结构预测时可用 architecture_experiment，'
        '无须先认定训练根因。工程插桩用 diagnose、unit_id=null、最小 changes。')
    return upgrade_instruction(text + GUIDANCE, 3, proposal_version)


def upgrade_instruction(text, previous_version, proposal_version):
    if previous_version < 5 <= proposal_version:
        from .independent_full_review import REVIEWERS
        roles = '、'.join(REVIEWERS)
        text = text.replace('architecture 阶段：三方先读', 'architecture 阶段：独立评审先读')
        text = text.replace('新候选仍须三方修改决策', '新候选仍须通过修改提案的独立评审')
        text = text.replace('director 核查瓶颈与科学假设；engineer 比较模块实现、接口、容量和成本；scout 查已有 RAG、论文方法及反证。',
            f'{roles} 分别审阅整批结果、机制与反证。engineer 回读验证结果和相关源码，综合独立分析后提出一个可验证的方案，交由独立评审。')
        text += ('\n在当前请求的观察或理由字段中列出各任务的实测指标。使用论文参照时，列明对应表格数值、划分、'
                 '指标尺度及聚合范围，说明差距的大小。只报告有依据且适合该指标的差值或倍数；'
                 '跨划分仅作描述，汇总值只与同范围的汇总对照。缺少对应表项时明确指出，论文值不得用于验证选参。')
    if previous_version < 6 <= proposal_version:
        text += ('\n如结果落后于论文参照，先依照已审规范核查实际源码，优先核对预处理、指标计算、模型及训练入口。'
                 '区分已证实的实现偏差、仍缺的证据和机制局限。实现偏差明确时，使用修订入口 revise_architecture，'
                 '填写 diagnosis=implementation 和 implementation_correction，写明原规范、实测偏差、复现与回归检查；'
                 '有告警时使用 repair_code。此提案批准后修复实际代码并用原条件复验。仅收集观测仍用 diagnose。'
                 '已核对实现且现有机制解释落后时，优先选择一个可检验的局部机制修订。架构文档和实现文档'
                 '按获批候选的机制和 changes 中的前后定义发布新版本，随后派发实现并重新验证。')
    if previous_version < 13 <= proposal_version:
        text += ('\n先用同口径的已测结果说明误差集中在哪些任务或条件。把拟查的实现问题与一个从预测实际调用链得到的'
                 '机制假设并列，说明各自能解释哪部分差距，以及什么观察会推翻它。现有数字能界定影响范围时给出数字；'
                 '缺少可比测量时写明未知，不能许诺收益。若诊断夹具反复退回，说明即使通过后仍有哪些误差待解释。'
                 '在现有 rationale、hypothesis、verification 中提出能区分解释的最小验证，明确唯一改动、'
                 '复用的对照、验证指标及回退条件。若已有证据可支持单因素架构实验，直接提出供独立评审。')
    return text


def validate(proposal, evidence, pointer, *, source_files=None):
    if CORRECTION in proposal:
        if (not enabled(evidence) or evidence.get('hyperparameter_yaml', {}).get('migration_required')
                or proposal['action'] != 'revise_architecture' or proposal['diagnosis'] != 'implementation'
                or proposal.get(KEY) or proposal.get('unit_id') is not None or not proposal['changes']
                or proposal.get('training_response', {}).get('disposition', 'repair_code') != 'repair_code'):
            raise ValueError('Implementation correction must restore the approved design and repeat validation')
        from .prose_schema import validate as validate_json
        validate_json(proposal[CORRECTION], DEFECT)
        if source_files is not None:
            from .source_read_identity import resolve
            for path in proposal[CORRECTION]['module_paths']:
                resolve(source_files, path)
        return
    value = proposal.get(KEY)
    disposition = proposal.get('training_response', {}).get('disposition')
    if not value:
        if disposition == 'architecture_experiment':
            raise ValueError('Architecture experiment requires a bounded architecture_review')
        return
    from .prose_schema import validate as validate_json
    validate_json(value, SCHEMA)
    if not enabled(evidence) or evidence.get('hyperparameter_yaml', {}).get('migration_required'):
        raise ValueError('Architecture candidates require the unfrozen architecture stage after YAML migration')
    candidates = value['candidates']
    by_id = {c['id']: c for c in candidates}
    if len(by_id) != len(candidates) or value['selected_id'] not in by_id:
        raise ValueError('Architecture candidates need unique IDs and one selected candidate')
    if sum(c['kind'] == 'keep_current' for c in candidates) != 1:
        raise ValueError('Architecture review must include exactly one keep-current control')
    for candidate in candidates:
        if candidate['kind'] != 'keep_current' and candidate['current_mechanism'] == candidate['proposed_mechanism']:
            raise ValueError('Architecture candidate must describe a changed mechanism')
        for path in candidate['evidence_paths']:
            if not path.startswith(('/records/', '/comparisons/', '/diagnostics/', '/training_investigation/')):
                raise ValueError('Architecture candidate needs specific measured validation evidence')
            pointer(evidence, path)
        if source_files is not None:
            from .source_read_identity import resolve
            for path in candidate['module_paths']:
                resolve(source_files, path)
    known = {r['id']: r for r in evidence['records']}
    for tid in value['control_trial_ids']:
        row = known.get(tid, {})
        if row.get('status') != 'completed' or row.get('params', {}).get('phase') != 'validation':
            raise ValueError('Architecture control must be an existing completed validation trial')
    selected = by_id[value['selected_id']]
    if selected['kind'] == 'keep_current':
        if proposal['action'] != 'accept_architecture' or proposal['changes'] or proposal['unit_id'] is not None:
            raise ValueError('Keep-current assessment cannot dispatch a source change or training')
    elif (proposal['action'] != 'revise_architecture' or proposal['diagnosis'] != 'architecture'
            or proposal['unit_id'] is not None or not proposal['changes']):
        raise ValueError('A structural candidate requires one concrete architecture revision')
    if evidence.get('training_investigation', {}).get('required'):
        expected = 'architecture_assessment' if selected['kind'] == 'keep_current' else 'architecture_experiment'
        if disposition != expected:
            raise ValueError('Architecture choice cannot masquerade as engineering repair or resolved training')
        response = proposal['training_response']
        if response['factor_paths'] or (response['cause'] == 'unknown' and response['cause_status'] != 'hypothesis'):
            raise ValueError('Architecture hypothesis must preserve unknown training cause and cannot combine parameter search')


def literature_tools(context, purpose):
    packet = context.get('experiment_decision') or {}
    if (packet.get(PROTOCOL) != 1 or not enabled(packet)
            or purpose.split(':', 1)[0] not in {'experiment_diagnose', 'experiment_decide', 'experiment_vote'}
            or packet.get('training_read_dispatch')):
        return []
    return ['literature.rag_search', 'literature.rag_read', 'literature.search', 'literature.verify',
            'literature.read_papers', 'sources.read']


def module_paths(proposal):
    paths = set(proposal.get('training_response', {}).get('module_paths', []))
    paths.update(proposal.get(CORRECTION, {}).get('module_paths', []))
    for candidate in proposal.get(KEY, {}).get('candidates', []):
        paths.update(candidate['module_paths'])
    return paths
