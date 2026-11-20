"""The engineer authors the selection program. Only bounded numeric Python is executable."""
import ast
import math
from functools import lru_cache

POLICY = 'agent_bayesian_batch_v1'
FIELDS = {'index', 'ei', 'gain', 'sigma', 'cost', 'baseline', 'axis'}
GUIDANCE = """由工程师根据本数据集的源码能力和验证目标自行选取参数、范围、预算和贝叶斯采集策略，
先调用 bayes.write_script 保存 Python 源码 selection_script，再在 combination_settings
（planner=agent_bayesian_batch_v1）提交同一份源码、工具返回的 script_sha256、所选参数和范围，由其他角色审阅原文。
def select_next(candidates, completed, budget): 遍历 for point in candidates，返回所选 point['index']；
可在至少一个候选已实测后返回 -1 提前收尾。completed 是已准入且完成的训练次数，budget 是整批训练上限。
候选是固定种子下从已测配置可达的联合参数向量，单步改变一个可执行因素，可累积多因素变化。
point 支持数值字段 index、ei（GP预期改进）、gain（相对于已测最好值的后验均值增益，越大越好）、
sigma（后验标准差）、cost（需新增训练数）、baseline（基线为1）、axis（当前方法的维度索引）。
脚本仅允许数值局部变量赋值、一次有界 for、if/else、比较、四则运算、return，禁止导入、调用外部函数和修改候选。
工程师自行决定搜索维度、取值、相关预算及脚本逻辑；不要照抄固定选点方案。请三方逐行审查脚本、参数、
方向、数据隔离、停止和预算。批准范围与脚本即授权宿主在这个版本内自动执行，判定依据只用已测验证集。
不要求遍历全部单因素点；不逐点评审；脚本或范围变更须重新审查，不继承旧批准。"""


@lru_cache(maxsize=64)
def validated_tree(source):
    if not isinstance(source, str) or len(source) > 12000:
        raise ValueError('Invalid Bayesian selection script size')
    try:
        tree = ast.parse(source)
    except (SyntaxError, RecursionError) as exc:
        raise ValueError('Invalid Bayesian selection script syntax') from exc
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise ValueError('Submit one select_next function')
    fn = tree.body[0]
    args = fn.args
    if (fn.name != 'select_next' or fn.decorator_list or fn.returns or getattr(fn, 'type_params', None)
            or args.posonlyargs or args.kwonlyargs or args.vararg or args.kwarg or args.defaults
            or [a.arg for a in args.args] != ['candidates', 'completed', 'budget'] or any(a.annotation for a in args.args)):
        raise ValueError('Invalid select_next(candidates, completed, budget) signature')
    nodes = list(ast.walk(fn))
    loops = [n for n in nodes if isinstance(n, ast.For)]
    if (len(nodes) > 500 or len(loops) != 1 or not isinstance(loops[0].iter, ast.Name)
            or loops[0].iter.id != 'candidates' or not isinstance(loops[0].target, ast.Name)
            or loops[0].target.id != 'point' or loops[0].orelse):
        raise ValueError('The script must use one bounded for point in candidates loop')
    protected = {'point', 'candidates', 'completed', 'budget', 'select_next'}
    local = set()
    for n in nodes:
        if isinstance(n, ast.Assign):
            if len(n.targets) != 1 or not isinstance(n.targets[0], ast.Name) or n.targets[0].id in protected:
                raise ValueError('Only local scalar variables may be assigned')
            local.add(n.targets[0].id)
    attributes = set()
    def expression(n):
        if isinstance(n, ast.Constant) and type(n.value) in (float, int) and abs(n.value) <= 1e100:
            n.value = float(n.value)  # bound arithmetic to floating point, including repeated products
        elif isinstance(n, ast.Name) and n.id in local | {'budget', 'completed'}:
            pass
        elif (isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) and n.value.id == 'point'
              and isinstance(n.slice, ast.Constant) and n.slice.value in FIELDS):
            attributes.add(n.slice.value)
        elif isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            expression(n.left)
            expression(n.right)
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd, ast.Not)):
            expression(n.operand)
        elif isinstance(n, ast.Compare) and all(isinstance(op, (ast.Gt, ast.GtE, ast.Lt, ast.LtE, ast.Eq, ast.NotEq)) for op in n.ops):
            for item in [n.left, *n.comparators]:
                expression(item)
        elif isinstance(n, ast.BoolOp) and isinstance(n.op, (ast.And, ast.Or)):
            for item in n.values:
                expression(item)
        else:
            raise ValueError('Unsupported Bayesian script expression')
    def statements(items):
        for n in items:
            if isinstance(n, (ast.Assign, ast.Return)):
                expression(n.value)
            elif isinstance(n, ast.If):
                expression(n.test)
                statements(n.body)
                statements(n.orelse)
            elif isinstance(n, ast.For):
                statements(n.body)
            else:
                raise ValueError('Unsupported Bayesian script statement')
    statements(fn.body)
    if not attributes.intersection({'ei', 'gain', 'sigma'}) or 'index' not in attributes:
        raise ValueError('Select a candidate index using the Bayesian posterior')
    return tree


@lru_cache(maxsize=64)
def compile_policy(source):
    env = {'__builtins__': {}}
    exec(compile(validated_tree(source), '<reviewed Bayesian selector>', 'exec'), env)
    return env['select_next']


def select(source, candidates, completed, budget):
    try:
        index = compile_policy(source)(candidates, float(completed), float(budget))
    except (ArithmeticError, UnboundLocalError) as exc:
        raise ValueError('Reviewed Bayesian script failed on the current validation frontier') from exc
    if (type(index) not in (float, int) or not math.isfinite(index) or int(index) != index
            or not (-1 if completed else 0) <= index < len(candidates)):
        raise ValueError('Reviewed Bayesian script returned an invalid candidate or premature stop')
    return int(index)
