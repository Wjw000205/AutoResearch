"""Actual LangGraph routing with local, durable SQLite checkpoints.

The graph keeps only routing identifiers. Model calls, tool continuations and
experiments use the existing transactional journal so replay is idempotent.
"""
import asyncio
from typing import TypedDict
from math import inf

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langsmith import tracing_context

STAGES = ['intake', 'literature', 'innovation', 'related_work', 'debate', 'design', 'implementation',
          'experiments', 'architecture_tuning', 'tuning', 'validation_review', 'revision', 'final_tests', 'report']
EDGES = [
    ('debate', 'intake', '用户重选研究问题'),
    ('innovation', 'intake', '用户重选研究问题'),
    ('related_work', 'intake', '用户重选研究问题'),
    ('intake', 'literature', '研究问题'), ('literature', 'innovation', '已保存文献与研究问题'),
    ('literature', 'debate', '兼容已有检查点'),
    ('innovation', 'debate', '三个独立候选比较后选定一个方向'),
    ('innovation', 'related_work', '选定idea后反向检索最相关论文'),
    ('innovation', 'literature', '讨论前更新当前日期的近期论文材料'),
    ('related_work', 'debate', '相关工作、经典基线与重复风险索引'),
    ('related_work', 'innovation', '相似性审查未通过，重新思考创新点'),
    ('debate', 'related_work', '补齐当前idea的相关论文分析'),
    ('debate', 'innovation', '补齐共识前的创新点讨论'),
    ('debate', 'design', '三位 Agent 一致同意'), ('design', 'implementation', '资源准入'),
    ('design', 'design', '已保存新架构版本，下一次独立评议交接'),
    ('implementation', 'design', '补齐同版本架构文档评议'),
    ('implementation', 'implementation', '同版本实验协议分段审计继续'),
    ('implementation', 'implementation', '实现规范有进展时继续修订与独立评议'),
    ('tuning', 'tuning', '运行修复规范继续独立评议'),
    ('final_tests', 'final_tests', '运行修复规范继续独立评议，不使用测试分数'),
    ('experiments', 'design', '补齐同版本架构文档评议'),
    ('implementation', 'experiments', '代码审查通过'), ('experiments', 'tuning', '验证集测量'),
    ('tuning', 'architecture_tuning', '评估局部架构调整与收敛条件'),
    ('architecture_tuning', 'experiments', '新架构通过独立复审与检查后验证'),
    ('architecture_tuning', 'tuning', '保留具体异议，继续有界优化'),
    ('architecture_tuning', 'validation_review', '验证齐备且三方同意结束本轮调整'),
    ('tuning', 'validation_review', '兼容旧已完成的参数微调检查点'), ('validation_review', 'revision', '未达标或有异议'),
    ('experiments', 'architecture_tuning', '三方批准局部架构微调'),
    ('architecture_tuning', 'architecture_tuning', '继续当前架构的证据评审'),
    ('architecture_tuning', 'revision', '独立复审后的局部修订'),
    ('architecture_tuning', 'report', '停止并保留未解决问题，不切换数据集'),
    ('tuning', 'report', '三方决策停止探索，保留未执行项并报告证据不足'),
    ('experiments', 'report', '三方决策停止探索，不宣称科学成功'),
    ('revision', 'debate', '修订后重新立项'), ('validation_review', 'final_tests', '达标且三票同意'),
    ('revision', 'experiments', '局部架构与代码修订通过复核及 CPU 检查，直接验证'),
    ('revision', 'tuning', '局部修订检查或复核未通过，依据新证据重新决策'),
    ('revision', 'implementation', '同版本实验协议补读恢复实现入口'),
    ('final_tests', 'report', '冻结后独立测试'),
    ('validation_review', 'report', '按预定规则停止并报告验证负结果'),
    ('tuning', 'experiments', '运行错误修复后重跑'),
    ('final_tests', 'experiments', '运行错误修复后重跑'),
    ('implementation', 'revision', '提供商实现明确要求科研修订时进入独立决策'),
    ('experiments', 'revision', '运行修复耗尽后修订研究'),
    ('tuning', 'revision', '运行修复耗尽后修订研究'),
    ('final_tests', 'revision', '仅根据执行失败修订 不使用测试分数'),
]
NEXT = dict(zip(STAGES, STAGES[1:]))
NEXT.update(validation_review='final_tests', revision='debate', report='end')
# Retain historical routing for saved pre-protocol tasks; new live tasks use
# dataset_optimization's explicit architecture and stability transitions.
NEXT.update(experiments='tuning', tuning='architecture_tuning', architecture_tuning='validation_review')


class GraphState(TypedDict):
    run_id: str
    cycle: int
    next_node: str


def build_graph(engine, harness, saver, *, entry='intake'):
    builder = StateGraph(GraphState)
    for stage in STAGES:
        def make_node(name):
            async def execute(state):
                if name == 'architecture_tuning':
                    from .optimization_loop import node
                    return await node(engine,harness,state)
                return await engine.graph_node(name, state, harness)
            return execute
        builder.add_node(stage, make_node(stage))
    if entry not in {'intake', 'design', 'implementation', 'experiments', 'tuning', 'architecture_tuning'}:
        raise ValueError('Unsupported research graph entry')
    builder.add_edge(START, entry)
    for stage in STAGES:
        destinations = {target: target for source, target, _ in EDGES if source == stage}
        if stage == 'experiments':
            destinations['experiments'] = 'experiments'
        if stage == 'report':
            destinations['end'] = END
        builder.add_conditional_edges(stage, lambda state: state['next_node'], destinations)
    return builder.compile(checkpointer=saver)


async def execute_graph(engine, run_id, harness):
    run = engine.store.get('runs', run_id)
    from .dataset_transition import current as dataset_transition, route as adaptation_route
    transition = await asyncio.to_thread(dataset_transition, engine.store, run_id)
    adaptation = await asyncio.to_thread(adaptation_route, engine.store, run_id)
    if transition and not adaptation:
        raise ValueError('数据集切换需要数据适配入口，不能重新进入想法共识。')
    from .implementation_restart import graph_identity
    thread_id = await asyncio.to_thread(graph_identity, engine, run_id)
    from .rapid_continuation import graph_entry
    continuation = await asyncio.to_thread(graph_entry, engine, run_id)
    if continuation:
        thread_id = continuation[0]
    if adaptation:
        thread_id = adaptation['graph_thread_id']
    from .dataset_experiments import tuning_entry
    tuning = await asyncio.to_thread(tuning_entry, engine, run_id)
    if tuning:
        thread_id = tuning[0]
    from .dataset_configuration_queue import batch as configuration_batch
    queued = await asyncio.to_thread(configuration_batch, engine.store, run_id)
    if queued:
        thread_id = queued['graph_thread_id']
    from .tuning_diagnostics import graph_entry as diagnostic_entry
    diagnostic = await asyncio.to_thread(diagnostic_entry, engine, run_id)
    if diagnostic:
        thread_id = diagnostic[0]
    from .dataset_optimization_recovery import graph_entry as optimization_entry
    optimization = await asyncio.to_thread(optimization_entry, engine, run_id)
    if optimization:
        thread_id = optimization[0]
    from .research_iteration import graph_entry as iteration_entry, limit as cycle_limit
    iteration = await asyncio.to_thread(iteration_entry, engine, run_id)
    if iteration:
        thread_id = iteration[0]
    from .scoped_delivery_recovery import graph_entry as delivery_entry
    delivery = await asyncio.to_thread(delivery_entry, engine, run_id)
    if delivery:
        thread_id = delivery[0]
    from .rapid_pathlib_recovery import graph_entry as host_check_entry
    host_check = await asyncio.to_thread(host_check_entry, engine, run_id)
    if host_check:
        thread_id = host_check[0]
    config = {'configurable': {'thread_id': thread_id},
              'recursion_limit': inf if (cycle_limit(engine.store, run_id) is None or run['config'].get('implementation_indexed_review_protocol') == 1
                                        or run['config'].get('architecture_progress_protocol') == 1
                                        or run['config'].get('discussion_mode', 'until_stalled') == 'until_stalled')
              else 40 + 20 * cycle_limit(engine.store, run_id)}
    async with AsyncSqliteSaver.from_conn_string(str(engine.data_dir / 'graph.sqlite3')) as saver:
        await saver.setup()
        await saver.conn.execute('PRAGMA busy_timeout=30000')
        await saver.conn.execute('PRAGMA foreign_keys=ON')
        entry = tuning[1] if tuning else adaptation['body']['entry'] if adaptation else continuation[1] if continuation else ('implementation' if thread_id != run_id else 'intake')
        if queued:
            entry = 'tuning'
        if diagnostic:
            entry = 'tuning'
        if optimization:
            entry = optimization[1]
        if iteration:
            entry = iteration[1]
        if delivery:
            entry = delivery[1]
        if host_check:
            entry = host_check[1]
        graph = build_graph(engine, harness, saver, entry=entry)
        snapshot = await graph.aget_state(config)
        initial = None if snapshot.next else {'run_id': run_id, 'cycle': engine.cycle(run_id), 'next_node': entry}
        # This is a local research workbench: no external LangSmith tracing.
        with tracing_context(enabled=False):
            await graph.ainvoke(initial, config, durability='sync')
