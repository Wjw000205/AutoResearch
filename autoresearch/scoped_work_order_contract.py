"""Separate migration equivalence from an approved structural intervention."""
import os

from .yaml_migration import INSTRUCTION as MIGRATION

RUNTIME = '''保留动态 YAML 作为超参数入口、默认 YAML、JSON Schema 及宿主 runtime/receipt 契约。
模型和优化器实际消费 YAML，验证 SHA、profile、参数镜像、必填值与范围；缺失配置不得回退硬编码。
不改宿主控制的数据、划分、种子、指标、资源或科学验收。已支持的 YAML 值调整不在本源码工作项中搜索。
按修改单更新受影响参数的 schema 和行为断言，保留深度、宽度、weight_decay 的真实 CPU 检查。'''
STRUCTURAL = '''这是已独立批准的单项架构实验实现。读取 decision.architecture_review 中被选候选及其反证。
允许替换目标模块或增加必要运行文件；仅修改获批机制及其接口适配，不顺带改变训练目标或其他模块。
_control 是只读原架构对照。目标模块允许输出、张量结构、初始化和检查点格式发生已声明的必要变化；
不得对该模块套用“新旧默认行为完全等价”。未改部分仍需行为、配置与随机流公平性检查。
CPU 断言验证新机制实际进入 forward/梯度路径、形状/因果边界、配置生效、容量/成本及接口契约；
记录必要的不兼容项与处理方式，不能通过跳过检查或修改控制文件取得通过。
CPU 通过仅证明交付行为，不能证明预测改善。保持原科学目标与回退条件，随后仍需独立评审和成对验证。
不执行正式训练、不访问留出集、不重跑历史实验、不安装依赖。'''
ENGINEERING = '''这是获批的工程维修，保留模型科学机制和默认行为。只落实修改单，
用真实 CPU 行为断言复现故障并验证修复；不把日志、接口或测试维修当成架构收益。
不执行正式训练、不访问留出集、不重跑历史实验。'''


def native_cpu_checks(program):
    return program.get('implementation_backend') == 'local_codex' and program.get('hyperparameter_yaml_protocol') == 1


STAGED_OBSERVATIONS = {
    'required': '方案的 required_cpu_observations 明列全部必需数值名称，经两席批准后原样入单。diagnostic_write_control 分别表示受信进程或要求整树禁止写盘。当前普通入口只支持 trusted_process。',
    'stdout': '每次本次调用得到一个有限数值时，print("CPU_OBSERVATION " + json.dumps([name, value], allow_nan=False), flush=True)，并写入 cpu_observations 字典。',
    'windows_job': '本机 Windows 在释放诊断进程之前设置并回读 Job 的整树处理器掩码、内存和用户 CPU 时间限制，实际回读值保存在 pre_release_job_limits。',
    'archive': '宿主保存整行收到的数值和终止原因。正常完成要求最终指标与全部声明的观测及其最后一次收到的数值逐项一致。',
    'inputs': '宿主在创建子进程前复制两份启动原件、记录摘要并将快照文件设为只读。Windows 在子进程存续期间以共享只读句柄保护原件与已有快照文件的数据和删除操作。启动器核验 argv 和原件。普通受信本地进程仍可在目录新建文件；整树禁止写盘与文件系统硬配额均不可用。'}


def staged_contract():
    from pathlib import Path
    from hashlib import sha256
    directory = Path(__file__).parent
    return {**STAGED_OBSERVATIONS, 'host_source_sha256': {name: sha256((directory / name).read_bytes()).hexdigest()
        for name in ('check_workspace.py', 'diagnostic_observations.py', 'lab.py', 'pathlib_check_runtime.py', 'rapid_experiment_revision.py')}}


def diagnostic_execution(run, candidate=None):
    """Describe the ordinary CPU route before admission; measured outcomes arrive after exit."""
    from .rapid_check_budget import caps
    mode = run['config'].get('execution_mode', 'local')
    result = {'execution_mode': mode, 'resource_ceilings': caps(candidate or {}, run),
        'preflight': '宿主按任务硬件和资源配置准入，逐项取准入与本检查上限的较小值。启动器在兼容层及夹具前检查 Python 断言已启用。',
        'attempt': '按修改单及检查摘要定位试次。只复用匹配的已完成回执，失败或未确认的同一试次不能自动重跑。',
        'observations': '检查脚本负责断言工作单所需观测完整并输出实际值。宿主按 cpu_observation.* 保存数值；核对终态、代码与目录摘要、断言总数、非空观测及原回执。异常输出和终止原因保存在试次中，未产生的值保持未知。',
        'sources': ['rapid_check_budget.py:caps', 'rapid_experiment_revision.py:_check',
                    'rapid_experiment_revision.py:_check_passed', 'lab.py:ExperimentRunner.run'],
        'execution_authorized': False}
    if mode == 'local' and os.name == 'nt':
        result['process_limits'] = ('本机用 Python -I 启动等待 .start 的 bootstrap。在释放候选代码前将进程加入 Windows Job，'
            '设置作业内存、用户 CPU 时间和启动进程亲和性。运行中轮询墙钟、进程树和工作目录大小，越限终止，退出后复查磁盘。'
            '目录用量是采样检查，采样之间可能超限。')
        result['sources'].append('lab.py:_WindowsJob')
    if candidate:
        result['checks_sha256'] = candidate['checks_sha256']
        result['check_bundle_sha256'] = candidate['check_bundle']['sha256']
        if 'required_cpu_observations' in candidate:
            result['observation_delivery'] = staged_contract()
            result['required_cpu_observations'] = candidate['required_cpu_observations']
    return result


def describe_cpu_check(engine, run_id, context, *, work_order_identity=False):
    """Expose the same code-check entry to the proposal author, mediator and voters."""
    if context['experiment_decision'].get('architecture_revision_workflow'):
        return
    from .python_training import code_hash
    from .rapid_check_budget import caps, DIAGNOSTIC_LIMITS_FILE
    program = engine.store.get('programs', run_id) or (
        engine.store.get('steps', run_id + ':implementation') or {}).get('output', {})
    if not native_cpu_checks(program):
        return
    packet = context['experiment_decision']
    binding = packet['binding']
    if program['revision'] != binding['revision'] or code_hash(program) != binding['code_sha256']:
        raise ValueError('CPU check capability must refer to the assessed program')
    packet['cpu_behavior_check'] = {
        'route': 'diagnose / instrument', 'entry': '_delivery/checks.py',
        'helpers': '检查辅助模块放在 _delivery/ 下，由入口显式调用；入口用 Python assert 验证实际行为。',
        'inputs': 'cwd 保留候选运行模块的相对路径。sys.argv[1] 是含 phase=code_check 和 rapid_revision_id 的宿主 JSON 路径。需要其他输入时按获批范围在隔离目录构造夹具。',
        'execution': '宿主执行检查入口。请依据当前模块的实际签名说明 CPU 调用路径和约束；需要 GPU 的训练入口须另有适用执行路径。',
        'outputs': '入口在模块作用域以 cpu_observations 字典提供实测数值，宿主随回执保存。夹具只证明本次运行观察到的行为。',
        'resource_ceilings': caps({}, engine.store.get('runs', run_id)),
        'limits_file': DIAGNOSTIC_LIMITS_FILE,
        'resources': '实际 CPU 限额取宿主、任务和修改单上限的最小值。算法步数等限制由夹具落实。',
        'execution_authorized': False}
    if work_order_identity:
        packet['cpu_behavior_check']['work_order_identity'] = (
            '方案通过后由宿主登记候选并分配 rapid_revision_id。码工的 work_order.json 和隔离 CPU 检查的 cwd '
            '均保留同一份冻结工作单。普通入口从 sys.argv[1] 读取宿主 JSON，逐字段与 '
            'work_order.json 的 check_input_contract.params 核对 phase 和 rapid_revision_id。'
            '具体 ID 在建单时绑定；设计中说明获取和比较方式。')
    if work_order_identity == 3:
        packet['cpu_behavior_check']['host_execution'] = diagnostic_execution(engine.store.get('runs', run_id))
    if work_order_identity in (2, 3) and packet.get('selected_validation_runs'):
        packet['cpu_behavior_check']['configuration_source'] = (
            '获批工作单 work_order.json 的 selected_validation_inputs.trials 按 trial_id、attempt 提供原 params、'
            'YAML 原文 yaml 和 yaml_receipt。每项 files.params、files.yaml、files.receipt 给出候选内相对路径和文件字节 SHA256，'
            '位于 _validation_inputs/<序号>/，在码工、审核和 CPU cwd 中相同。params_sha256 按冻结 JSON sort_keys=True、ensure_ascii=False、默认分隔符及 UTF-8 '
            '计算，yaml_sha256 对 YAML 原 UTF-8 字节计算。按当前归档加载器处理二者并核对 profile 与宿主字段；'
            '以深拷贝构造经批准的夹具差分，保留原输入。')
        if work_order_identity == 3:
            packet['cpu_behavior_check']['configuration_source'] += (
                '两席核对过同次暂存原件时，files.params 指向归档字节的 params.json；'
                '仅有准入对象时则供给规范编码的 admitted-params.json。读取 files 中的实际路径及文件 SHA。')


def contract(record):
    if record.get('yaml_migration'):
        from .yaml_migration import instructions
        return 'yaml_migration', instructions(record.get('yaml_configuration_scope', 'hyperparameters'))
    if record['decision'].get('architecture_review') and record['decision']['action'] == 'revise_architecture':
        return 'architecture_experiment', RUNTIME + '\n' + STRUCTURAL
    if record['decision'].get('implementation_correction'):
        return 'implementation_correction', (RUNTIME + '\n依照 implementation_correction 复现原实现偏差，'
            '将目标模块修复为已审核设计规定的行为，并把复现用例作为 CPU 回归检查。保留其余控制；审核通过后由宿主重跑验证。')
    if record['decision'].get('action') == 'diagnose' and record['decision'].get('training_response', {}).get('disposition') == 'instrument':
        text = ('依据已审修改单，用 _delivery/checks.py 和必要的检查辅助文件验证真实模块行为。'
            '只要求夹具和断言的修改单不需要改动运行文件。只有明确批准源码插桩时才修改对应运行模块。'
            '按原数据范围、CPU 与资源上限检查并保存实际观察；此工作单只产生诊断证据。')
        if record.get('check_measurement_protocol') == 1:
            text += ('在 checks.py 的模块作用域将修改单要求的实测数值写入 cpu_observations 字典，'
                     '名称对应验证条件，数值来自本次实际调用。宿主随 CPU 回执保存，供下一次架构评估引用。'
                     '若检查中途失败，在异常信息中保留已知的关键观测，未知值不补写。'
                     '在 _delivery/cpu_limits.json 逐字段写出批准修改单中的资源上限，支持 cpu_cores、'
                     'memory_mb、disk_mb、timeout_seconds、cpu_seconds 正整数；没有额外上限时写空对象。'
                     '宿主取交付、任务和 CPU 宿主上限的最小值。更新次数等算法限制由夹具实现并检查。')
        return 'cpu_behavior_check', text
    return 'engineering_repair', RUNTIME + '\n' + ENGINEERING
