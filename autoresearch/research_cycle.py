"""Pure validation-only evidence and stopping gates for recoverable research cycles.

These functions never execute experiments or inspect held-out metric values.
``finalize`` authorizes freezing a candidate for the final test; it does not mean
the hypothesis is proven, the final test succeeded, or a publication is ready.
The engine must persist the summary and votes before acting on the decision and
supersede prior comparisons whenever a reviewed code/design revision changes.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import defaultdict

from .fair_comparison import assert_same_task


ROLES = ("director", "engineer", "scout")
STOP_WARNING_CODES = {"small_sample", "unverified_tuning_budget"}
ASSUMPTIONS = [
    "Candidate selection uses completed, non-superseded validation records only; held-out test scores are never selection evidence.",
    "Configurations are grouped by all actual parameters except phase, variant and seed; matching typed seeds identify paired runs.",
    "Reported improvement is a descriptive paired validation difference, not statistical significance or a generalization guarantee.",
    "Adaptive validation reuse can overfit the validation set; freeze the selected design, code and parameters before one final test evaluation.",
    "Dataset and split declarations are checked for known conflicts, not independently verified; the engine must enforce the data protocol.",
]


def _finite(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _mean(values):
    scale = max(abs(value) for value in values)
    return math.fsum(value / scale for value in values) / len(values) * scale if scale else 0.0


def _seed_key(seed):
    if isinstance(seed, bool) or not isinstance(seed, (int, str)) or isinstance(seed, str) and not seed.strip():
        return None
    return json.dumps([type(seed).__name__, seed], ensure_ascii=False)


def _data_conflict(first, second):
    """Only split identity is read here, never a held-out measurement."""
    left = first.get("evaluation") if isinstance(first.get("evaluation"), dict) else {}
    right = second.get("evaluation") if isinstance(second.get("evaluation"), dict) else {}
    from . import split_identity
    if any('split_identity_protocol' in item for item in (left, right)):
        if any(split_identity.validate(item, item.get('phase'), host_version=2) for item in (left, right)):
            return True
        if any(split_identity.enabled(item) for item in (left, right)):
            return not split_identity.compatible(left, right)
    if left.get("dataset_id") and right.get("dataset_id") and left["dataset_id"] != right["dataset_id"]:
        return True
    for key in ("train", "validation", "test"):
        a = left.get("split_fingerprints") or {}
        b = right.get("split_fingerprints") or {}
        if isinstance(a, dict) and isinstance(b, dict) and a.get(key) and b.get(key) and a[key] != b[key]:
            return True
    return False


def validation_summary(trials, metric, direction):
    """Summarize actual parameter configurations and compare shared seed runs.

    Invalid measurements and duplicate configuration/seed records are excluded.
    The strongest baseline configuration by its validation mean is fixed first.
    Proposed configurations are compared with that baseline on exact shared
    seeds, and the largest paired relative improvement wins. A zero baseline
    mean has no defined relative improvement and cannot pass the stopping gate.
    Returned ``best_*['params']`` contains configuration parameters only; the
    caller supplies phase/variant/seed when scheduling a frozen final trial.
    Different declared comparison tasks remain separate descriptive groups;
    this single-winner summary never selects across input/output conditions.
    """
    if not isinstance(metric, str) or not metric.strip().startswith("val_") or metric.strip() == "val_":
        raise ValueError("metric must name a validation-only metric prefixed val_")
    if direction not in {"minimize", "maximize"}:
        raise ValueError("direction must be minimize or maximize")
    if not isinstance(trials, (list, tuple)):
        raise ValueError("trials must be a list of recorded trial objects")
    metric = metric.strip()
    buckets, issues, excluded = defaultdict(list), [], set()
    parameters = {}

    def issue(code, ids, message):
        issues.append({"code": code, "trial_ids": sorted(ids), "message": message})

    for index, trial in enumerate(trials):
        if not isinstance(trial, dict) or trial.get("status") != "completed" or trial.get("superseded"):
            continue
        from .comparison_transport import scientific_params
        params = scientific_params(trial)
        if not isinstance(params, dict) or params.get("phase") != "validation":
            continue
        identifier = str(trial.get("id", f"unnamed-trial-{index}"))
        variant = params.get("variant")
        if variant not in {"baseline", "proposed", "ablation"}:
            excluded.add(identifier)
            issue("invalid_variant", [identifier], "Validation trial has no recognized experiment variant.")
            continue
        values = trial.get("metrics") if isinstance(trial.get("metrics"), dict) else {}
        if not _finite(values.get(metric)):
            excluded.add(identifier)
            issue("invalid_metric", [identifier], f"Validation trial has no finite numeric {metric}.")
            continue
        seed = params.get("seed")
        seed_key = _seed_key(seed)
        if seed_key is None:
            excluded.add(identifier)
            issue("invalid_seed", [identifier], "Validation comparison requires an integer or nonempty string seed.")
            continue
        actual = {key: value for key, value in params.items() if key not in {"phase", "variant", "seed"}}
        try:
            encoded = json.dumps(actual, sort_keys=True, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, OverflowError):
            excluded.add(identifier)
            issue("invalid_parameters", [identifier], "Actual parameters must be finite JSON data.")
            continue
        configuration = hashlib.sha256(encoded.encode()).hexdigest()[:16]
        key = (variant, configuration)
        parameters[key] = json.loads(encoded)
        buckets[key].append({"id": identifier, "seed": seed, "seed_key": seed_key,
            "value": float(values[metric]), "trial": trial})

    groups, rows_by_group = [], {}
    for key, rows in sorted(buckets.items()):
        by_seed = defaultdict(list)
        for row in rows:
            by_seed[row["seed_key"]].append(row)
        usable = []
        for seed_rows in by_seed.values():
            if len(seed_rows) != 1:
                ids = [row["id"] for row in seed_rows]
                excluded.update(ids)
                issue("duplicate_seed", ids, "Duplicate records for one configuration and seed are ambiguous and excluded.")
            else:
                usable.append(seed_rows[0])
        if not usable:
            continue
        usable.sort(key=lambda row: (0, row["seed"]) if isinstance(row["seed"], int) else (1, row["seed"]))
        rows_by_group[key] = {row["seed_key"]: row for row in usable}
        groups.append({"variant": key[0], "configuration": key[1], "params": parameters[key],
            "mean": _mean([row["value"] for row in usable]), "n": len(usable),
            "seeds": [row["seed"] for row in usable], "trial_ids": [row["id"] for row in usable]})

    selection_groups = [group for group in groups if group["variant"] in {"baseline", "proposed"}]
    comparable_tasks = True
    if selection_groups:
        reference = selection_groups[0]
        for group in selection_groups:
            try:
                assert_same_task(reference["params"], group["params"])
            except ValueError as exc:
                comparable_tasks = False
                issue("comparison_task_mismatch", set(reference["trial_ids"] + group["trial_ids"]),
                      f"Validation configurations do not describe one comparable task: {exc}. "
                      "No best configuration is selected across different task conditions.")

    baseline_groups = [group for group in selection_groups if group["variant"] == "baseline"] if comparable_tasks else []
    baseline = min(baseline_groups, key=lambda group: (group["mean"] if direction == "minimize" else -group["mean"], group["configuration"])) if baseline_groups else None
    comparisons = []
    if baseline:
        baseline_rows = rows_by_group[("baseline", baseline["configuration"])]
        for candidate in (group for group in groups if group["variant"] == "proposed"):
            proposed_rows = rows_by_group[("proposed", candidate["configuration"])]
            pairs = []
            for seed_key, first in baseline_rows.items():
                second = proposed_rows.get(seed_key)
                if second is None:
                    continue
                if _data_conflict(first["trial"], second["trial"]):
                    issue("dataset_or_split_mismatch", [first["id"], second["id"]], "This seed pair has conflicting dataset/split declarations and is excluded.")
                    continue
                delta = first["value"] - second["value"] if direction == "minimize" else second["value"] - first["value"]
                if not _finite(delta):
                    issue("invalid_difference", [first["id"], second["id"]], "This seed pair has a nonfinite derived difference and is excluded.")
                    continue
                pairs.append({"seed": first["seed"], "baseline": first["value"], "proposed": second["value"],
                    "improvement": delta, "baseline_trial_id": first["id"], "proposed_trial_id": second["id"]})
            if not pairs:
                continue
            bmean = _mean([pair["baseline"] for pair in pairs])
            pmean = _mean([pair["proposed"] for pair in pairs])
            improvement = _mean([pair["improvement"] for pair in pairs])
            relative = improvement / abs(bmean) if bmean else None
            if not _finite(relative):
                relative = None
            comparisons.append({"baseline": baseline, "proposed": candidate, "baseline_mean": bmean,
                "proposed_mean": pmean, "improvement": improvement, "relative_improvement": relative,
                "n_pairs": len(pairs), "paired_seeds": [pair["seed"] for pair in pairs], "pairs": pairs})

    chosen = max(comparisons, key=lambda item: (item["relative_improvement"] if item["relative_improvement"] is not None else -math.inf,
        item["improvement"], item["proposed"]["configuration"])) if comparisons else None
    included = sorted({identifier for group in groups for identifier in group["trial_ids"]})
    provenance = sorted({str(row["trial"].get("provenance", "unverified"))
        for grouped_rows in rows_by_group.values() for row in grouped_rows.values()})
    if not chosen:
        issue("no_comparable_pairs", [], "No finite baseline/proposed validation measurements share comparable seeds.")
    return {"phase": "validation", "metric": metric, "direction": direction, "groups": groups,
        "best_baseline": copy.deepcopy(chosen["baseline"] if chosen else baseline),
        "best_proposed": copy.deepcopy(chosen["proposed"]) if chosen else None,
        "baseline_score": chosen["baseline_mean"] if chosen else None,
        "proposed_score": chosen["proposed_mean"] if chosen else None,
        "improvement": chosen["improvement"] if chosen else None,
        "relative_improvement": chosen["relative_improvement"] if chosen else None,
        "comparable": chosen is not None, "n_pairs": chosen["n_pairs"] if chosen else 0,
        "paired_seeds": chosen["paired_seeds"] if chosen else [], "pairs": chosen["pairs"] if chosen else [],
        "issues": issues, "provenance": {"sources": provenance, "included_trial_ids": included,
            "excluded_trial_ids": sorted(excluded), "scope": "Recorded validation measurements only; no independent verification of genuine research."},
        "assumptions": list(ASSUMPTIONS)}


def _recorded_validation_failure(summary, threshold):
    """Require complete finite evidence before a policy can stop with a negative.

    The two allowed warnings limit inference but do not invalidate descriptive
    failure of a frozen numeric criterion. All other evidence issues fail closed.
    """
    if (summary.get("phase") != "validation" or summary.get("comparable") is not True
            or not isinstance(summary.get("metric"), str) or not summary["metric"].startswith("val_")
            or summary["metric"] == "val_" or summary.get("direction") not in {"minimize", "maximize"}
            or summary.get("issues") != [] or type(summary.get("n_pairs")) is not int
            or summary["n_pairs"] <= 0):
        return False, []
    for field in ("baseline_score", "proposed_score", "improvement", "relative_improvement"):
        if not _finite(summary.get(field)):
            return False, []
    pairs = summary.get("pairs")
    if not isinstance(pairs, list) or len(pairs) != summary["n_pairs"]:
        return False, []
    seeds, baseline, proposed, improvements = set(), [], [], []
    for pair in pairs:
        if not isinstance(pair, dict) or any(not _finite(pair.get(key)) for key in ("baseline", "proposed", "improvement")):
            return False, []
        seed = _seed_key(pair.get("seed"))
        if seed is None or seed in seeds or any(not isinstance(pair.get(key), str) or not pair[key].strip()
                                               for key in ("baseline_trial_id", "proposed_trial_id")):
            return False, []
        delta = pair["baseline"] - pair["proposed"] if summary["direction"] == "minimize" else pair["proposed"] - pair["baseline"]
        if not _finite(delta) or not math.isclose(delta, pair["improvement"], rel_tol=1e-9, abs_tol=1e-12):
            return False, []
        seeds.add(seed)
        baseline.append(pair["baseline"])
        proposed.append(pair["proposed"])
        improvements.append(delta)
    bmean, pmean, gain = _mean(baseline), _mean(proposed), _mean(improvements)
    relative = gain / abs(bmean) if bmean else None
    computed = (bmean, pmean, gain, relative)
    if any(not _finite(value) or not math.isclose(value, summary[field], rel_tol=1e-9, abs_tol=1e-12)
           for field, value in zip(("baseline_score", "proposed_score", "improvement", "relative_improvement"), computed)):
        return False, []
    acceptance = summary.get("acceptance_checks")
    if not isinstance(acceptance, dict) or acceptance.get("phase") != "validation" or acceptance.get("criteria_complete") is not True:
        return False, []
    checks = acceptance.get("checks")
    if not isinstance(checks, list) or not checks:
        return False, []
    failed, primary_present, warnings = False, False, []
    for check in checks:
        if isinstance(check, dict) and check.get('required') is False:
            continue
        if (not isinstance(check, dict) or not isinstance(check.get("metric"), str)
                or not check["metric"].startswith("val_") or check["metric"] == "val_"
                or check.get("direction") not in {"minimize", "maximize"}
                or check.get("comparison", "at_least") not in {"at_least", "greater_than"}
                or check.get("complete_seed_coverage") is not True
                or type(check.get("expected_pairs")) is not int or check["expected_pairs"] <= 0
                or type(check.get("n_pairs")) is not int or check["n_pairs"] != check["expected_pairs"]
                or type(check.get("passed")) is not bool
                or any(not _finite(check.get(key)) for key in ("baseline_mean", "proposed_mean", "relative_improvement", "minimum_relative_improvement"))):
            return False, []
        issues = check.get("issues")
        if not isinstance(issues, list) or any(not isinstance(issue, dict) or issue.get("code") not in STOP_WARNING_CODES
                                             or issue.get("severity") == "error" for issue in issues):
            return False, []
        warnings.extend(copy.deepcopy(issues))
        baseline_mean = check["baseline_mean"]
        delta = baseline_mean - check["proposed_mean"] if check["direction"] == "minimize" else check["proposed_mean"] - baseline_mean
        relative = delta / abs(baseline_mean) if baseline_mean else None
        if not _finite(relative) or not math.isclose(relative, check["relative_improvement"], rel_tol=1e-9, abs_tol=1e-12):
            return False, []
        passed = relative > check["minimum_relative_improvement"] if check.get("comparison") == "greater_than" else relative >= check["minimum_relative_improvement"]
        if passed != check["passed"]:
            return False, []
        failed = failed or not passed
        if check["metric"] == summary["metric"] and check["direction"] == summary["direction"]:
            if check["n_pairs"] != summary["n_pairs"] or any(
                    not math.isclose(check[field], summary[target], rel_tol=1e-9, abs_tol=1e-12)
                    for field, target in (("baseline_mean", "baseline_score"), ("proposed_mean", "proposed_score"),
                                          ("relative_improvement", "relative_improvement"))):
                return False, []
            primary_present = True
    return primary_present and (failed or gain <= 0 or summary["relative_improvement"] < threshold), warnings


def decide_cycle(summary, reviews, cycle, max_cycles, min_relative_improvement=0.01, iteration_policy=None):
    """Return an auditable decision without mutating the summary or review list.

    Cycles are one-based; max_cycles is an execution/recovery bound, not a money
    budget. The engine must attach nonempty proposal_hash and revision_hash to
    the summary and each current vote. The revision hash should bind the cycle,
    complete design/code, selected parameters and exact validation summary.
    A predeclared stop policy can end a numerically failed validation study;
    it cannot convert missing evidence or a reviewer's dissent into a negative.
    """
    if isinstance(summary, dict) and summary.get('benchmark_matrix') is True:
        from .benchmark_matrix import decide
        return decide(summary, reviews, cycle, max_cycles, min_relative_improvement, iteration_policy)
    if type(cycle) is not int or cycle < 1 or max_cycles is not None and (type(max_cycles) is not int or max_cycles < 1):
        raise ValueError("cycle must be positive; max_cycles must be null or a positive integer")
    if not _finite(min_relative_improvement) or min_relative_improvement < 0:
        raise ValueError("min_relative_improvement must be a finite nonnegative number")
    if not isinstance(summary, dict) or not isinstance(reviews, (list, tuple)):
        raise ValueError("summary must be an object and reviews must be a list")
    if iteration_policy is not None and (not isinstance(iteration_policy, dict)
            or iteration_policy.get("on_validation_failure") not in {"stop", "revise"}
            or not isinstance(iteration_policy.get("rationale"), str) or not iteration_policy["rationale"].strip()):
        raise ValueError("iteration_policy must declare stop or revise and a nonempty rationale")
    reasons = []
    enough = summary.get("phase") == "validation" and isinstance(summary.get("metric"), str) and summary["metric"].startswith("val_") \
        and summary.get("comparable") is True and type(summary.get("n_pairs")) is int and summary["n_pairs"] > 0 \
        and _finite(summary.get("improvement")) and summary["improvement"] > 0 \
        and _finite(summary.get("relative_improvement")) and summary["relative_improvement"] >= min_relative_improvement
    if not enough:
        reasons.append("Comparable validation evidence does not demonstrate a positive improvement meeting the required relative threshold.")
    acceptance = summary.get('acceptance_checks', {})
    malformed_acceptance = not isinstance(acceptance, dict)
    criteria = acceptance.get('checks', []) if not malformed_acceptance else []
    malformed_acceptance = malformed_acceptance or not isinstance(criteria, list) or any(not isinstance(check, dict) for check in criteria)
    if malformed_acceptance:
        enough = False
        reasons.append('The recorded quantitative acceptance checks are malformed; repair the evidence before making a research decision.')
    if isinstance(acceptance, dict) and acceptance.get('criteria_complete') is False:
        enough = False
        reasons.append('The predeclared quantitative acceptance contract is incomplete; resolve the missing conditions before final testing.')
    if not malformed_acceptance and criteria and any(check.get('passed') is not True for check in criteria if check.get('required') is not False):
        enough = False
        reasons.append('The selected validation pair failed or lacks evidence for at least one predeclared primary/secondary numeric criterion.')
    targets = (summary.get("proposal_hash"), summary.get("revision_hash"))
    bound = all(isinstance(value, str) and value.strip() for value in targets)
    exact_roles = len(reviews) == len(ROLES) and all(isinstance(review, dict) for review in reviews) \
        and sorted(str(review.get("role")) for review in reviews) == sorted(ROLES)
    unanimous = bound and exact_roles and all(review.get("approve") is True
        and review.get("proposal_hash") == targets[0] and review.get("revision_hash") == targets[1]
        and review.get("required_changes", []) == [] for review in reviews)
    if not unanimous:
        reasons.append("Director, engineer and scout must each explicitly approve the same current proposal and revision with no required changes.")
    failure, warnings = _recorded_validation_failure(summary, min_relative_improvement) if iteration_policy else (False, [])
    if max_cycles is not None and cycle > max_cycles:
        reasons.append("The configured cycle limit has already been exceeded.")
        decision = "blocked"
    elif enough and unanimous and not reported_negative_stop_hold(summary, iteration_policy):
        decision = "finalize"
        reasons.append("The validation improvement meets the threshold and all three roles accept this exact candidate; freeze it before held-out evaluation.")
    elif bound and failure and iteration_policy and iteration_policy["on_validation_failure"] == "stop":
        decision = "stop"
        reasons.append("The predeclared stop policy ends this study with a descriptive negative validation result. "
                       "This does not establish method inferiority, generalization, held-out test success or research success. "
                       "Policy rationale: " + iteration_policy["rationale"])
    elif reported_negative_stop_hold(summary, iteration_policy):
        decision = "blocked"
        reasons.append("The frozen stop policy forbids automatically revising a reported negative validation. "
                       "The evidence is not yet sufficient for the formal negative-stop classifier; reconcile "
                       "the recorded acceptance completeness and comparison evidence without new training or final tests.")
    elif max_cycles is None or cycle < max_cycles:
        decision = "iterate"
        reasons.append("Revise the design, code or parameters using validation evidence and unresolved objections, then evaluate another cycle.")
    else:
        decision = "blocked"
        reasons.append("The cycle limit is exhausted; research success is not established.")
    metadata = {"iteration_policy": copy.deepcopy(iteration_policy), "policy_applied": decision == "stop",
                "validation_failure_established": bool(bound and failure), "validation_warnings": warnings} if iteration_policy else {}
    if decision == 'blocked' and reported_negative_stop_hold(summary, iteration_policy):
        metadata['continuation_hold_reason'] = 'reported_negative_under_stop_policy'
    if decision == "stop":
        metadata.update(validation_outcome="negative", scope="validation-only descriptive stopping, not proof of method inferiority/generalization")
    return {"decision": decision, "rationale": " ".join(reasons), "cycle": cycle, "max_cycles": max_cycles,
        "threshold_met": bool(enough), "unanimous": bool(unanimous), "min_relative_improvement": min_relative_improvement,
        "proposal_hash": targets[0], "revision_hash": targets[1], "assumptions": list(ASSUMPTIONS), **metadata}


def reported_negative_stop_hold(summary, iteration_policy):
    """A reported negative is not permission to search past a frozen stop rule.

    This hold does not certify scientific failure or fill missing evidence. The
    existing stricter negative-stop classifier remains responsible for that.
    """
    acceptance = summary.get('acceptance_checks') if isinstance(summary, dict) else None
    return (isinstance(iteration_policy, dict) and iteration_policy.get('on_validation_failure') == 'stop'
            and isinstance(acceptance, dict) and acceptance.get('phase') == 'validation'
            and acceptance.get('research_outcome') == 'negative')


def revision_policy_blocker(context):
    """Also protect saved revision checkpoints made by an older route policy."""
    if not str(context.get('_purpose', '')).startswith('revise:cycle:'):
        return None
    review = context.get('validation_review')
    if not isinstance(review, dict):
        return None
    plan = context.get('experiment_plan')
    policies = [review.get('iteration_policy'), plan.get('iteration_policy') if isinstance(plan, dict) else None]
    if any(reported_negative_stop_hold(review.get('validation_summary'), policy) for policy in policies):
        return ('验证结果与停止规则待核对：已批准的策略要求验证失败后停止，本轮验收记录已报告负结果，'
                '不能自动修改方案或重新训练。已保存的修订流转与停止策略不一致，'
                '须先核对已保存验收证据并按停止规则收尾；三方评议与已有实验保留，未发送新的修订请求。')
    return None
