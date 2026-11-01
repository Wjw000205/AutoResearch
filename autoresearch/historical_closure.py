"""Read-only evidence snapshots for a separately approved historical report.

This module neither approves closure nor changes workflow state. Old schemas,
contracts, superseded flags and trial files remain untouched. Every comparison
uses an explicit local projection of the source cycle's reviewed validation IDs.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import re

from .experiment_plan import build_plan, validation_seeds
from .research_cycle import validation_summary, _mean


ROLES = {"director", "engineer", "scout"}
REAL_SOURCES = {"real_experiment_execution", "real_cpu_execution_on_synthetic_data"}


class HistoricalClosureError(ValueError):
    """The saved evidence cannot support a safe historical closure snapshot."""


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _require(condition, message):
    if not condition:
        raise HistoricalClosureError("Historical closure rejected: " + message)


def _finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _step(store, run_id, cycle, stage):
    archived = store.get("cycle_steps", f"{run_id}:{cycle}:{stage}")
    current = store.get("steps", f"{run_id}:{stage}")
    step = archived or (current if current and current.get("cycle", 1) == cycle else None)
    _require(step and step.get("status") == "completed" and isinstance(step.get("output"), dict),
             f"missing completed source {stage} checkpoint")
    return step


def _attempt_evidence(trial, root, run_id):
    trial_id, attempt = trial.get("id"), trial.get("attempt")
    _require(isinstance(trial_id, str) and re.fullmatch(r"[A-Za-z0-9_-]+", trial_id), "invalid trial identifier")
    _require(type(attempt) is int and attempt > 0, "missing positive attempt number")
    path = root / "experiments" / run_id / trial_id / f"attempt-{attempt:03d}"
    _require(path.resolve().is_relative_to(root), "attempt path escapes data directory")
    _require(trial.get("artifact_path") and Path(trial["artifact_path"]).resolve() == path.resolve(),
             "attempt directory differs from saved trial")
    _require(not any(parent.is_symlink() for parent in (path, *path.parents) if parent.is_relative_to(root)),
             "symlink attempt directory is not accepted")

    def read(name, maximum):
        file = path / name
        _require(file.is_file() and not file.is_symlink() and file.stat().st_size <= maximum,
                 f"missing, symlinked or oversized attempt {name}")
        return file.read_text(encoding="utf-8")

    code = read("experiment.py", 300000)
    try:
        compile(code, "<historical-experiment>", "exec")
        params = json.loads(read("params.json", 65536))
        environment = json.loads(read("environment.json", 2 * 1024**2))
    except (SyntaxError, ValueError, TypeError) as exc:
        raise HistoricalClosureError("Historical closure rejected: invalid attempt code or JSON") from exc
    _require(isinstance(params, dict) and isinstance(environment, dict), "invalid attempt metadata objects")
    code_hash = hashlib.sha256(code.encode()).hexdigest()
    _require(_digest(params) == _digest(trial["params"]), "attempt params do not match saved actual inputs")
    _require(environment.get("params_sha256") == _digest(params), "attempt parameter hash mismatch")
    _require(environment.get("code_sha256") == code_hash, "attempt code hash mismatch")
    _require(trial.get("fingerprint") == _digest({"code": code, "params": params}), "trial code/parameter fingerprint mismatch")
    # The runner's result.json and any explicitly exported metric/evaluation
    # artifacts independently bind saved numbers and identity. Never copy raw
    # output, arbitrary metadata, or a held-out measurement into the snapshot.
    artifacts, metric_sources = {}, []
    expected_metrics = {key: value for key, value in trial["metrics"].items() if key.startswith("val_")}
    expected_evaluation = trial.get("evaluation") or trial.get("experiment_metadata", {}).get("evaluation") or {}

    def validation_only(value):
        if isinstance(value, dict):
            _require(value.get("phase") != "test" and not any(str(key).startswith("test_") for key in value),
                     "held-out measurement in attempt artifact")
            for key, child in value.items():
                if key not in {"stdout", "stderr"}:
                    validation_only(child)
        elif isinstance(value, list):
            for child in value:
                validation_only(child)

    for name in ("result.json", "metrics.json", "evaluation.json"):
        file = path / name
        if not file.exists() and not file.is_symlink():
            continue
        artifact = json.loads(read(name, 2 * 1024**2))
        _require(isinstance(artifact, dict), "invalid saved measurement artifact")
        validation_only(artifact)
        if name in {"result.json", "metrics.json"}:
            raw_metrics = artifact.get("metrics", artifact if name == "metrics.json" else None)
            _require(isinstance(raw_metrics, dict), "saved measurement artifact lacks metrics")
            metrics = {key: value for key, value in raw_metrics.items() if key.startswith("val_")}
            _require(metrics.keys() == expected_metrics.keys() and all(_finite(value) for value in metrics.values())
                     and all(metrics[key] == expected_metrics[key] for key in metrics), "saved validation metric artifact mismatch")
            metric_sources.append(name)
        evaluation = artifact if name == "evaluation.json" else (
            artifact.get("evaluation") or artifact.get("experiment_metadata", {}).get("evaluation"))
        if evaluation is not None:
            _require(isinstance(evaluation, dict) and all(
                _digest(evaluation.get(key)) == _digest(expected_evaluation.get(key))
                for key in ("dataset_id", "split_fingerprints", "phase", "seed", "actual_params",
                            "split_identity_protocol", "split_materialization")),
                "saved evaluation identity artifact mismatch")
        artifacts[name] = hashlib.sha256(file.read_bytes()).hexdigest()
    _require(metric_sources, "missing saved validation measurement artifact")
    return {"path": path.relative_to(root).as_posix(), "code_sha256": code_hash, "code_digest": _digest(code),
            "code_bytes_sha256": hashlib.sha256((path / "experiment.py").read_bytes()).hexdigest(),
            "params_sha256": _digest(params), "measurement_artifact_hashes": artifacts,
            "validation_metric_sources": metric_sources}


def _build_historical_closure(store, run_id, data_dir, source_cycle=None):
    """Return a deterministic unapproved snapshot; never write records or files.

    Incomplete original acceptance contracts remain inconclusive even when the
    recorded primary comparison demonstrably fails the task's harder gate.
    Any held-out attempt anywhere in the task excludes this minimal pathway.
    """
    _require(isinstance(run_id, str) and run_id.isalnum(), "invalid run identifier")
    run = store.get("runs", run_id)
    _require(run is not None, "run is missing")
    all_trials = store.list("experiments", run_id)
    attempts = store.list("experiment_attempts", run_id)
    for trial in [*all_trials, *attempts]:
        evaluation = trial.get("evaluation") or trial.get("experiment_metadata", {}).get("evaluation") or {}
        _require(trial.get("params", {}).get("phase") != "test" and evaluation.get("phase") != "test"
                 and not any(str(key).startswith("test_") for key in (trial.get("metrics") or {})),
                 "held-out attempt or measurement exists")
    cycles = store.list("research_cycles", run_id)
    if source_cycle is None:
        available = [row["cycle"] for row in cycles if row.get("validation_summary", {}).get("comparable") is True]
        _require(available, "no reviewed historical validation cycle")
        source_cycle = max(available)
    _require(type(source_cycle) is int and source_cycle > 0, "invalid source cycle")
    _require(source_cycle <= run.get("state", {}).get("research_cycle", 1), "source cycle is ahead of current research")
    review = next((row for row in cycles if row.get("cycle") == source_cycle), None)
    _require(review is not None, "source cycle review is missing")
    plan = review.get("experiment_plan") or store.get("experiment_plans", run_id)
    _require(isinstance(plan, dict) and plan.get("cycle") == source_cycle and plan.get("run_id") == run_id,
             "source frozen experiment plan is missing")
    design_step, debate_step = (_step(store, run_id, source_cycle, stage) for stage in ("design", "debate"))
    design, debate = review.get("design") or design_step["output"], debate_step["output"]
    _require(plan.get("design_hash") == _digest(design), "frozen plan/design hash mismatch")
    proposal_hash = _digest(debate.get("proposal"))
    proposal_votes = debate.get("votes", [])
    _require(debate.get("unanimous") is True and debate.get("proposal_hash") == proposal_hash
             and review.get("proposal_hash") == proposal_hash and len(proposal_votes) == 3
             and {vote.get("role") for vote in proposal_votes} == ROLES
             and all(vote.get("approve") is True and not vote.get("required_changes")
                     and vote.get("proposal_hash") == proposal_hash for vote in proposal_votes),
             "source proposal lacks exact three-role approval")
    # Rebuild only the parameter definitions, never the historical final-pair
    # budget or an old schema. Legacy single-point ablations remain single-point.
    definitions = build_plan(design, 100)
    pairs = plan.get("validation_pairs")
    ablations = plan.get("ablation_candidates", [plan.get("ablation_params")])
    _require(pairs == definitions["validation_pairs"] and plan.get("grid") == definitions["grid"]
             and ablations == definitions["ablation_candidates"], "frozen grid differs from source design")
    seed = plan.get("selection_seed")
    _require(type(seed) is int and plan.get("validation_trials_per_method") == len(pairs), "invalid frozen selection protocol")
    seeds = validation_seeds(plan)
    _require(seeds == validation_seeds(definitions), "frozen validation seeds differ from source design")
    metric, direction = design.get("primary_metric"), design.get("direction")
    _require(isinstance(metric, str) and metric.startswith("val_") and metric != "val_"
             and direction in {"minimize", "maximize"}, "invalid source validation metric")
    contract = plan.get("acceptance")
    _require(isinstance(contract, dict) and type(contract.get("complete")) is bool
             and isinstance(contract.get("criteria"), list) and contract["criteria"], "missing original numeric acceptance contract")
    criteria = contract["criteria"]
    for criterion in criteria:
        _require(isinstance(criterion, dict) and isinstance(criterion.get("metric"), str)
                 and criterion["metric"].startswith("val_") and criterion["metric"] != "val_"
                 and criterion.get("direction") in {"minimize", "maximize"}
                 and _finite(criterion.get("minimum_relative_improvement"))
                 and criterion.get("comparison", "at_least") in {"at_least", "greater_than"}, "invalid saved acceptance criterion")
    _require(any(item["metric"] == metric and item["direction"] == direction for item in criteria), "primary criterion missing")
    prior = review.get("validation_summary") or {}
    ids = prior.get("provenance", {}).get("included_trial_ids")
    _require(prior.get("comparable") is True and prior.get("issues") == [] and isinstance(ids, list)
             and ids and len(set(ids)) == len(ids), "missing clean source validation review")
    source_trials = [trial for trial in all_trials if trial.get("research_cycle", 1) == source_cycle]
    selected = [trial for trial in source_trials if trial.get("id") in ids]
    _require(len(selected) == len(ids), "reviewed source trial is missing")
    revisions = {trial.get("code_revision") for trial in selected}
    _require(len(revisions) == 1 and all(type(value) is int and value >= 0 for value in revisions), "mixed or missing code revisions")
    revision = next(iter(revisions))
    _require(not any(trial.get("status") == "completed" and trial.get("code_revision", -1) > revision for trial in source_trials),
             "review does not cover the final completed code revision")
    expected = {_digest({**pair[variant], "phase": "validation", "seed": pair.get('seed', seed)})
                for pair in pairs for variant in ('baseline', 'proposed')}
    expected_ablations = {_digest({**params, "phase": "validation", "seed": params.get('seed', seed)}) for params in ablations}
    _require(len(expected) == 2 * len(pairs), "duplicate planned comparison parameters")
    rows, observed, signatures, code_hashes = [], set(), set(), set()
    root = Path(data_dir).resolve()
    for trial in sorted(selected, key=lambda row: row["id"]):
        params = trial.get("params") or {}
        signature = _digest(params)
        _require(trial.get("status") == "completed" and params.get("phase") == "validation"
                 and type(params.get("seed")) is int and params["seed"] in seeds
                 and signature in expected | expected_ablations and signature not in observed, "invalid, duplicate or unplanned validation trial")
        observed.add(signature)
        values = {key: value for key, value in (trial.get("metrics") or {}).items() if key.startswith("val_")}
        _require(values and all(_finite(value) for value in values.values())
                 and all(item["metric"] in values for item in criteria) and metric in values, "missing or nonfinite validation metric")
        evaluation = trial.get("evaluation") or trial.get("experiment_metadata", {}).get("evaluation") or {}
        splits = evaluation.get("split_fingerprints") or {}
        from . import split_identity
        phase_identity_valid = (not split_identity.validate(evaluation, 'validation', host_version=2)
                                and (split_identity.enabled(evaluation) or
                                all(isinstance(splits.get(key), str) and re.fullmatch(r"[a-fA-F0-9]{64}", splits[key])
                                    for key in ("train", "validation", "test"))
                                and len({splits[key] for key in ("train", "validation", "test")}) == 3))
        _require(not trial.get("evaluation_errors") and evaluation.get("identity_consistent") is not False
                 and isinstance(evaluation.get("dataset_id"), str) and evaluation["dataset_id"].strip()
                 and evaluation.get("phase") == "validation" and type(evaluation.get("seed")) is int
                 and evaluation["seed"] == params["seed"] and _digest(evaluation.get("actual_params")) == signature
                 and phase_identity_valid, "invalid actual parameters or evaluation identity")
        signatures.add(_digest({"dataset_id": evaluation["dataset_id"], "splits": splits}))
        file_evidence = _attempt_evidence(trial, root, run_id)
        code_hashes.add(file_evidence["code_sha256"])
        rows.append({"id": trial["id"], "source_id": f"experiments:{run_id}:{trial['id']}",
            "research_cycle": source_cycle, "code_revision": revision, "attempt": trial["attempt"],
            "status": trial["status"], "params": copy.deepcopy(params), "metrics": values,
            "superseded": trial.get("superseded", False), "superseded_reason": trial.get("superseded_reason"),
            "provenance": trial.get("provenance", "unverified"), "evaluation": {key: copy.deepcopy(evaluation.get(key))
                for key in ("dataset_id", "split_fingerprints", "seed", "phase", "actual_params",
                            "split_identity_protocol", "split_materialization") if key in evaluation},
            "files": file_evidence})
    _require(expected <= observed, "incomplete paired validation grid")
    _require(len(signatures) == 1 and len(code_hashes) == 1, "mixed dataset/split identity or code files")
    # This view is never persisted or passed to current experiment selection.
    projection = [{**row, "superseded": False} for row in rows]
    summary = validation_summary(projection, metric, direction)
    _require(summary["comparable"] and not summary["issues"], "historical comparison cannot be reconstructed")
    for key in ("baseline_score", "proposed_score", "improvement", "relative_improvement"):
        _require(_finite(prior.get(key)) and _finite(summary[key])
                 and math.isclose(prior[key], summary[key], rel_tol=1e-9, abs_tol=1e-12), "source summary differs from recorded validation")
    threshold = run.get("config", {}).get("min_relative_improvement", .01)
    _require(_finite(threshold) and 0 <= threshold <= 1, "invalid task primary threshold")
    primary_failed = summary["improvement"] <= 0 or summary["relative_improvement"] < threshold
    by_id = {row["id"]: row for row in rows}
    baseline = by_id[summary["best_baseline"]["trial_ids"][0]]
    proposed = by_id[summary["best_proposed"]["trial_ids"][0]]
    original_summary = copy.deepcopy(prior)
    original_revision = original_summary.pop("revision_hash", None)
    reconstructed_revision = _digest({"cycle": source_cycle, "design": design,
        "code_sha": rows[0]["files"]["code_digest"], "selected_params": proposed["params"],
        "baseline_params": baseline["params"], "experiment_plan": plan, "validation_summary": original_summary})
    _require(original_revision == review.get("revision_hash") == reconstructed_revision,
             "source revision hash does not bind the saved code, plan and selected validation")
    checks = []
    for criterion in criteria:
        def measured(group, single):
            ids = summary[group]['trial_ids']
            return single['metrics'][criterion['metric']] if len(ids) == 1 else _mean(
                [by_id[identifier]['metrics'][criterion['metric']] for identifier in ids])
        b, p = measured('best_baseline', baseline), measured('best_proposed', proposed)
        relative = ((b - p) if criterion["direction"] == "minimize" else (p - b)) / abs(b) if b else None
        _require(_finite(relative), "criterion has undefined/nonfinite relative comparison")
        passed = relative > criterion["minimum_relative_improvement"] if criterion.get("comparison") == "greater_than" else relative >= criterion["minimum_relative_improvement"]
        checks.append({**copy.deepcopy(criterion), "baseline": b, "proposed": p,
                       "relative_improvement": relative, "passed": passed})
    _require(primary_failed or any(check["passed"] is False for check in checks), "records do not establish a failed numeric gate")
    full_ablations = [{**{k: v for k, v in design["ablation_params"].items()
                        if k not in {"variant", "phase", "seed", "description"}}, **point,
                       "variant": "ablation", "phase": "validation", "seed": model_seed}
                      for point in plan["grid"] for model_seed in seeds]
    coverage = {"baseline": len(pairs), "proposed": len(pairs),
        "executed_ablations": sum(row["params"]["variant"] == "ablation" for row in rows),
        "planned_ablations": len(ablations), "shared_grid_points": len(plan['grid']) if plan.get('version') == 2 else len(pairs),
        "missing_planned_ablations": [params for params in ablations if _digest({**params, "phase": "validation", "seed": params.get('seed', seed)}) not in observed],
        "unmeasured_full_grid_ablations": [params for params in full_ablations if _digest(params) not in observed],
        "prediction_equivalence_verified": False}
    failures = [{"source_id": f"experiments:{run_id}:{trial['id']}", "id": trial["id"],
        "status": trial.get("status"), "code_revision": trial.get("code_revision"), "attempt": trial.get("attempt"),
        "superseded": trial.get("superseded", False),
        "reason": "evaluation_contract_failed" if trial.get("evaluation_errors") else "execution_not_completed"}
        for trial in sorted(source_trials, key=lambda row: row["id"]) if trial.get("status") != "completed"]
    provenance = sorted({row["provenance"] for row in rows})
    verified_execution = all(tag in REAL_SOURCES for tag in provenance)
    outcome = {"research_outcome": "negative" if contract["complete"] and verified_execution else "inconclusive",
        "phase": "validation", "basis": "historical_evidence", "source_cycles": [source_cycle],
        "success_criteria_met": False, "failed_task_primary_gate": primary_failed,
        "task_primary_gate": {"metric": metric, "minimum_relative_improvement": threshold,
                              "relative_improvement": summary["relative_improvement"], "passed": not primary_failed},
        "criteria_complete": contract["complete"], "checks": checks, "final_tests_performed": False,
        "new_trials_performed": False, "publication_ready": False, "predeclared_stop_applied": False,
        "scope": "事后整理已记录的历史验证；不是预声明停止，不代表未执行周期、完整消融、泛化或发表达标。"}
    evidence = {"run_id": run_id, "source_cycle": source_cycle,
        "source_ids": [f"research_cycles:{review['id']}", *[
            f"{'cycle_steps' if store.get('cycle_steps', step['id']) else 'steps'}:{step['id']}"
            for step in (design_step, debate_step)]],
        "source_proposal_hash": proposal_hash, "source_revision_hash": review.get("revision_hash"),
        "source_plan": copy.deepcopy(plan), "original_acceptance": copy.deepcopy(contract),
        "selection_seed": seed, "code_revision": revision, "code_sha256": next(iter(code_hashes)),
        "validation_trials": rows, "validation_summary": summary, "coverage": coverage,
        "failed_trials": failures, "provenance": provenance,
        "limitations": [*contract.get("limitations", []), *design.get("limitations", []),
            "历史 superseded 标记原样保留；本地比较投影不恢复任何旧实验为当前有效记录。",
            "只核对记录、代码哈希与报告的数据划分一致性，不独立证明数据真实性或预测等价。",
            "原始 stdout/stderr 和未执行周期的结果不在快照中；未新增实验或读取测试指标。"]}
    if plan.get('version') == 2:
        evidence['validation_seeds'] = seeds
    evidence_hash = _digest(evidence)
    closure = {"action": "report_existing_negative", "run_id": run_id, "evidence_hash": evidence_hash,
        "source_cycle": source_cycle, "current_cycle": run.get("state", {}).get("research_cycle", 1),
        "new_trials_allowed": False, "heldout_allowed": False, "retrospective_reporting_only": True,
        "outcome": outcome}
    return {"schema_version": 1, "action": "report_existing_negative", "source_cycle": source_cycle,
            "scope": outcome["scope"], "coverage_gaps": copy.deepcopy(coverage),
            "evidence": evidence, "evidence_hash": evidence_hash,
            "closure": closure, "closure_hash": _digest(closure), "outcome": copy.deepcopy(outcome)}


def build_historical_closure(store, run_id, data_dir, source_cycle=None):
    """Build an unapproved, deterministic, read-only historical evidence snapshot."""
    try:
        return _build_historical_closure(store, run_id, data_dir, source_cycle)
    except HistoricalClosureError:
        raise
    except (OSError, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise HistoricalClosureError("Historical closure rejected: missing or invalid source evidence ("
                                     + type(exc).__name__ + ")") from exc


def render_historical_closure(snapshot, votes):
    """Render supplied evidence; never create a vote or finalize a run.

    The caller must authenticate canonical approval receipts. Role/hash checks
    here do not prove authorship and must never authorize client-supplied votes.
    """
    _require(snapshot.get("evidence_hash") == _digest(snapshot["evidence"])
             and snapshot.get("closure_hash") == _digest(snapshot["closure"])
             and snapshot["closure"].get("evidence_hash") == snapshot["evidence_hash"]
             and snapshot.get("outcome") == snapshot["closure"].get("outcome")
             and snapshot.get("action") == snapshot["closure"].get("action") == "report_existing_negative"
             and snapshot.get("source_cycle") == snapshot["closure"].get("source_cycle")
             and snapshot.get("scope") == snapshot["outcome"].get("scope")
             and snapshot.get("coverage_gaps") == snapshot["evidence"].get("coverage"), "snapshot hashes changed")
    evidence, outcome = snapshot["evidence"], snapshot["outcome"]
    votes = list(votes) if isinstance(votes, (list, tuple)) else []
    well_formed = all(isinstance(vote, dict) and isinstance(vote.get("role"), str)
                     and type(vote.get("approve")) is bool and isinstance(vote.get("rationale"), str)
                     and isinstance(vote.get("required_changes"), list)
                     and all(isinstance(change, str) for change in vote["required_changes"]) for vote in votes)
    approved = well_formed and len(votes) == 3 and {vote.get("role") for vote in votes} == ROLES and all(
        vote.get("approve") is True and vote.get("required_changes") == []
        and vote.get("evidence_hash") == snapshot["evidence_hash"] and vote.get("closure_hash") == snapshot["closure_hash"]
        and vote.get("action") == "report_existing_negative" for vote in votes)
    def cell(value):
        return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "&#124;").replace("\r", " ").replace("\n", " ")
    summary, coverage = evidence["validation_summary"], evidence["coverage"]
    lines = ["# 历史验证证据整理", "", "审批：" + ("三方明确同意仅整理这些证据。" if approved else "尚未取得同证据、同范围的三票批准；这是待审批快照，不终结任务。"),
        "", outcome["scope"], f"整体结论：{outcome['research_outcome']}；原验收合同完整：{outcome['criteria_complete']}。",
        f"来源周期：{evidence['source_cycle']}；本次整理所在周期：{snapshot['closure']['current_cycle']}。未执行周期没有实验结论。",
        "success_criteria_met=false；publication_ready=false；不新增 trial，不执行 test。", "",
        f"## 已测主指标：{summary['metric']}",
        f"基线 {summary['baseline_score']:.8g}；候选 {summary['proposed_score']:.8g}；相对改善 {summary['relative_improvement']:.6%}。",
        f"任务硬门槛 {outcome['task_primary_gate']['minimum_relative_improvement']:.6%}；未通过：{outcome['failed_task_primary_gate']}。",
        "原验收合同不完整时，即使该主指标确定未达标，整体仍为 inconclusive；不补写或放宽原合同。", "",
        "## 原验收标准及实际检查", "| 指标 | 基线 | 候选 | 相对改善 | 原比较方式 | 原门槛 | 通过 |", "|---|---|---|---|---|---|---|"]
    for check in outcome["checks"]:
        lines.append("| " + " | ".join(cell(value) for value in (
            check["metric"], f"{check['baseline']:.8g}", f"{check['proposed']:.8g}",
            f"{check['relative_improvement']:.6%}", check.get("comparison", "at_least"),
            f"{check['minimum_relative_improvement']:.6%}", check["passed"])) + " |")
    lines.extend(["", "## 实际覆盖与缺口", f"共享网格：基线 {coverage['baseline']} 项，候选 {coverage['proposed']} 项；消融实际 {coverage['executed_ablations']} 项，源冻结计划 {coverage['planned_ablations']} 项。",
        f"全共享网格仍未测的消融点数：{len(coverage['unmeasured_full_grid_ablations'])}；原计划未完成消融点数：{len(coverage['missing_planned_ablations'])}。",
        "没有将单点消融称为全网格覆盖，也未核验逐样本预测等价。", "",
        "## 验证记录", "| 来源 | 方法 | 参数 | 验证指标 | superseded |", "|---|---|---|---|---|"])
    for trial in evidence["validation_trials"]:
        lines.append("| " + " | ".join(cell(value) for value in (trial["source_id"], trial["params"]["variant"],
            json.dumps(trial["params"], ensure_ascii=False), json.dumps(trial["metrics"], ensure_ascii=False), trial["superseded"])) + " |")
    lines.extend(["", "## 失败记录"])
    lines.extend(f"- {row['source_id']}：{row['status']}，代码版本 {row['code_revision']}，{row['reason']}；superseded={row['superseded']}。" for row in evidence["failed_trials"])
    lines.extend(["", "## 合同与局限", f"证据来源标签：{', '.join(evidence['provenance'])}。"])
    lines.extend("- " + limitation for limitation in evidence["limitations"])
    lines.extend(["", "## 关闭评议"])
    lines.extend(f"- {cell(vote.get('role'))}：approve={vote.get('approve')}；{cell(vote.get('rationale', ''))}；待解决={cell(json.dumps(vote.get('required_changes', []), ensure_ascii=False))}" for vote in votes if isinstance(vote, dict))
    if not well_formed:
        lines.append("- 评议格式无效；不能作为批准。")
    lines.extend(["", f"evidence_hash：`{snapshot['evidence_hash']}`", f"closure_hash：`{snapshot['closure_hash']}`"])
    return "\n".join(lines) + "\n"
