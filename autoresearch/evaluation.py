"""Pure, provenance-aware analysis of recorded research trials.

evaluate_trials never executes code, calls a model, modifies records, or verifies
dataset truth. It compares completed, non-superseded final test records by exact
seed and by separate parameter configurations. Delta is proposed minus baseline;
improvement is an absolute difference with positive meaning better in `direction`.

Trial contract:
  params = {phase: 'test'|'validation', variant: 'baseline'|'proposed', seed: int,...}
  metrics = {test_mse: finite_number, ...}
  evaluation = {dataset_id: nonempty_string,
                split_fingerprints: {train: str, validation: str, test: str},
                tuning_budget?: {baseline_trials: int, proposed_trials: int}}

`consistent` only means declared dataset/split fields agree. It does not prove
independent samples, genuine dataset origin, correct split code, or reproducibility.
The interval reporting threshold (five unique seed pairs) is an explicit product
policy, not a claim that five seeds establish statistical adequacy.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import defaultdict
from typing import Any


MIN_CI_PAIRS = 5
VERIFICATION_SCOPE = "Only consistency of recorded dataset IDs and split fingerprints; no independent verification of dataset truth, sample independence, or scientific claims."
_T95 = {4: 2.7764451052, 5: 2.5705818356, 6: 2.4469118488, 7: 2.3646242510,
    8: 2.3060041350, 9: 2.2621571629, 10: 2.2281388520, 11: 2.2009851601,
    12: 2.1788128297, 13: 2.1603686565, 14: 2.1447866879, 15: 2.1314495456,
    16: 2.1199052992, 17: 2.1098155778, 18: 2.1009220402, 19: 2.0930240544,
    20: 2.0859634473, 21: 2.0796138447, 22: 2.0738730679, 23: 2.0686576104,
    24: 2.0638985616, 25: 2.0595385528, 26: 2.0555294386, 27: 2.0518305165,
    28: 2.0484071418, 29: 2.0452296421, 30: 2.0422724563, 40: 2.0210753903,
    60: 2.0002978220, 120: 1.9799304051}


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()[:16]


def _finite(value) -> bool:
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _mean(values) -> float:
    values = list(values)
    scale = max(abs(value) for value in values)
    return math.fsum(value / scale for value in values) / len(values) * scale if scale else 0.0


def _std(values) -> float | None:
    try:
        value = statistics.stdev(values)
        return value if math.isfinite(value) else None
    except (ArithmeticError, ValueError):
        return None


def _configuration(params: dict) -> tuple[str, dict]:
    fields = {key: value for key, value in params.items() if key not in {"seed", "phase", "variant"}}
    return _hash(fields), fields


def _seed_key(seed) -> str | None:
    # JSON number and string seeds are not silently equated: training code can
    # interpret them differently. Boolean seeds are never accepted as integers.
    if isinstance(seed, bool) or seed is None or not isinstance(seed, (int, str)):
        return None
    if isinstance(seed, str) and not seed.strip():
        return None
    return json.dumps([type(seed).__name__, seed], ensure_ascii=False)


def _seed_sort(seed):
    return (0, seed) if isinstance(seed, int) else (1, str(seed))


def _evaluation_fields(trial: dict) -> dict:
    evidence = trial.get("evaluation")
    evidence = evidence if isinstance(evidence, dict) else {}
    splits = evidence.get("split_fingerprints")
    splits = splits if isinstance(splits, dict) else {}
    def text(value):
        return value.strip() if isinstance(value, str) and value.strip() else None
    return {"dataset_id": text(evidence.get("dataset_id")), "split_fingerprints": {
        split: text(splits.get(split)) for split in ("train", "validation", "test")}}


def _pair_provenance(baseline: dict, proposed: dict) -> tuple[str, str | None, dict]:
    from . import split_identity
    raw = [trial.get('evaluation') if isinstance(trial.get('evaluation'), dict) else {}
           for trial in (baseline, proposed)]
    if any('split_identity_protocol' in item for item in raw):
        details = {name: {key: item.get(key) for key in (
            'dataset_id', 'split_fingerprints', 'phase', 'split_identity_protocol', 'split_materialization')}
            for name, item in zip(('baseline', 'proposed'), raw)}
        if any(split_identity.validate(item, item.get('phase'), host_version=2) for item in raw):
            return 'inconsistent', 'invalid_split_identity', details
        if any(split_identity.enabled(item) for item in raw):
            if not all(isinstance(item.get('dataset_id'), str) and item['dataset_id'].strip() for item in raw):
                return 'unverified', None, details
            if raw[0]['dataset_id'] != raw[1]['dataset_id']:
                return 'inconsistent', 'dataset_mismatch', details
            same = split_identity.compatible(*raw)
            return ('consistent' if same else 'inconsistent'), (None if same else 'split_fingerprint_mismatch'), details
    first, second = _evaluation_fields(baseline), _evaluation_fields(proposed)
    if first["dataset_id"] and second["dataset_id"] and first["dataset_id"] != second["dataset_id"]:
        return "inconsistent", "dataset_mismatch", {"baseline": first, "proposed": second}
    different = [name for name in first["split_fingerprints"] if first["split_fingerprints"][name]
        and second["split_fingerprints"][name] and first["split_fingerprints"][name] != second["split_fingerprints"][name]]
    if different:
        return "inconsistent", "split_fingerprint_mismatch", {"different_splits": different, "baseline": first, "proposed": second}
    complete = all(record["dataset_id"] and all(record["split_fingerprints"].values()) for record in (first, second))
    return ("consistent" if complete else "unverified"), None, {"baseline": first, "proposed": second}


def _t_interval(deltas: list[float]) -> dict:
    df = len(deltas) - 1
    # For degrees of freedom beyond/between table entries, use the next lower
    # tabulated df, giving a slightly wider (conservative) interval.
    critical_df = max(item for item in _T95 if item <= df)
    mean = _mean(deltas)
    deviation = _std(deltas)
    if deviation is None:
        raise ArithmeticError("nonfinite standard deviation")
    margin = _T95[critical_df] * deviation / math.sqrt(len(deltas))
    return {"low": mean - margin, "high": mean + margin, "confidence": .95,
        "method": "Paired Student t interval over recorded seed differences; conservative tabulated critical value",
        "estimand": "proposed_minus_baseline", "n_min": MIN_CI_PAIRS,
        "degrees_of_freedom": df, "critical_df_used": critical_df,
        "assumptions": "Independent seed-run differences with an approximately normal mean; these assumptions are not verified by this function."}


def unconfigured_evaluation():
    return {'primary_metric': None, 'test_metric': None, 'direction': None,
            'paired_comparisons': [], 'tuning_budget': {},
            'issues': [{'code': 'metric_not_configured', 'message': '任务尚未声明评价指标和优化方向。'}],
            'provenance': {'status': 'unverified', 'source': 'persisted_trial_records'}}


def evaluate_trials(trials: list[dict], primary_metric: str, direction: str) -> dict:
    """Return JSON-compatible descriptive comparisons without modifying input.

    `configuration` is the proposed-parameter hash; `baseline_configuration` is
    separate. `comparison_id` uniquely names the B/P configuration combination.
    Duplicates within a configuration/seed and known dataset/split mismatches are
    excluded instead of arbitrarily selecting a trial. Missing metadata permits
    explicitly unverified descriptive differences, but no confidence interval.
    """
    if direction not in {"minimize", "maximize"}:
        raise ValueError("direction must be minimize or maximize")
    if not isinstance(primary_metric, str) or not primary_metric.strip():
        raise ValueError("primary_metric must be a nonempty metric name")
    primary_metric = primary_metric.strip()
    if not isinstance(trials, (list, tuple)):
        raise ValueError("trials must be a list of recorded trial objects")
    metric = primary_metric.replace("val_", "test_", 1) if primary_metric.startswith("val_") else primary_metric
    issues = []
    completed = [trial for trial in trials if isinstance(trial, dict)
        and trial.get("status") == "completed" and not trial.get("superseded")]
    groups = {"baseline": defaultdict(list), "proposed": defaultdict(list)}
    parameters = {"baseline": {}, "proposed": {}}
    eligible_ids, excluded_ids = [], []

    def issue(code, message, ids=(), severity="warning", comparison_id=None, **details):
        item = {"code": code, "severity": severity, "message": message,
            "trial_ids": sorted(set(str(value) for value in ids)), **details}
        if comparison_id:
            item["comparison_id"] = comparison_id
        issues.append(item)

    def trial_id(trial, index=0):
        return str(trial.get("id", f"unnamed-trial-{index}"))

    for index, trial in enumerate(completed):
        from .comparison_transport import scientific_params
        params = scientific_params(trial)
        if not isinstance(params, dict):
            issue("invalid_parameters", "Completed trial has no parameter object and cannot be assigned to a configuration.", [trial_id(trial, index)])
            continue
        if params.get("phase") != "test" or params.get("variant") not in groups:
            continue
        identifier = trial_id(trial, index)
        values = trial.get("metrics") if isinstance(trial.get("metrics"), dict) else {}
        if not _finite(values.get(metric)):
            excluded_ids.append(identifier)
            issue("invalid_metric", f"Final test trial has no finite numeric {metric}; it is excluded.", [identifier])
            continue
        seed = params.get("seed")
        if _seed_key(seed) is None:
            excluded_ids.append(identifier)
            issue("missing_seed", "Final trial needs an integer or nonempty string seed to form an exact pair.", [identifier])
            continue
        try:
            configuration, config_params = _configuration(params)
        except (TypeError, ValueError):
            excluded_ids.append(identifier)
            issue("invalid_parameters", "Trial configuration is not finite JSON data; it is excluded.", [identifier])
            continue
        variant = params["variant"]
        groups[variant][configuration].append({"trial": trial, "id": identifier, "seed": seed,
            "seed_key": _seed_key(seed), "value": float(values[metric])})
        parameters[variant][configuration] = config_params
        eligible_ids.append(identifier)

    validation_configs = {"baseline": set(), "proposed": set()}
    validation_counts = {"baseline": 0, "proposed": 0}
    for trial in completed:
        from .comparison_transport import scientific_params
        params = scientific_params(trial)
        if not isinstance(params, dict) or params.get("phase") != "validation" or params.get("variant") not in validation_configs:
            continue
        try:
            configuration, _ = _configuration(params)
        except (TypeError, ValueError):
            continue
        validation_configs[params["variant"]].add(configuration)
        validation_counts[params["variant"]] += 1
    declared_budgets = set()
    invalid_budgets = False
    for trial in completed:
        evidence = trial.get("evaluation")
        budget = evidence.get("tuning_budget") if isinstance(evidence, dict) else None
        if budget is None:
            continue
        if not isinstance(budget, dict) or any(not isinstance(budget.get(key), int) or isinstance(budget[key], bool) or budget[key] < 0
                for key in ("baseline_trials", "proposed_trials")):
            invalid_budgets = True
            continue
        declared_budgets.add((budget["baseline_trials"], budget["proposed_trials"]))
    budget_summary = {"observed_completed_validation_trials": validation_counts,
        "observed_validation_configurations": {key: len(value) for key, value in validation_configs.items()},
        "declared_trial_budget": {"baseline": next(iter(declared_budgets))[0], "proposed": next(iter(declared_budgets))[1]} if len(declared_budgets) == 1 else None,
        "scope": "Completed validation records and optional declared trial counts; search-space size, failed searches, compute time and tuning quality are not verified."}
    budget_limitations = []
    unequal_observed = (validation_counts["baseline"] != validation_counts["proposed"]
        or len(validation_configs["baseline"]) != len(validation_configs["proposed"]))
    unequal_declared = any(baseline != proposed for baseline, proposed in declared_budgets)
    if unequal_observed or unequal_declared:
        message = ("Baseline/proposed tuning budgets are not balanced in the supplied evidence: "
            f"completed validation trials {validation_counts['baseline']}/{validation_counts['proposed']}, "
            f"distinct configurations {len(validation_configs['baseline'])}/{len(validation_configs['proposed'])}. "
            "Reported improvements may partly reflect unequal search effort.")
        if unequal_declared:
            message += f" Declared trial budgets: {sorted(declared_budgets)}."
        issue("unequal_tuning_budget", message)
        budget_limitations.append(message)
    if len(declared_budgets) != 1 or invalid_budgets:
        code = "conflicting_tuning_budget" if len(declared_budgets) > 1 or invalid_budgets else "unverified_tuning_budget"
        message = "A single valid baseline/proposed tuning-budget declaration is unavailable; observed completed candidates do not establish equal tuning or compute budgets."
        issue(code, message)
        budget_limitations.append(message)
    else:
        budget_limitations.append("Declared trial counts alone do not establish equal compute time or equally strong baseline tuning.")

    paired_comparisons = []
    all_used_ids = set()
    detected_conflict = False
    for baseline_config in sorted(groups["baseline"]):
        for proposed_config in sorted(groups["proposed"]):
            comparison_id = _hash([baseline_config, proposed_config, metric])
            baseline_rows = groups["baseline"][baseline_config]
            proposed_rows = groups["proposed"][proposed_config]
            by_seed = {"baseline": defaultdict(list), "proposed": defaultdict(list)}
            seed_values = {}
            for variant, rows in (("baseline", baseline_rows), ("proposed", proposed_rows)):
                for row in rows:
                    by_seed[variant][row["seed_key"]].append(row)
                    seed_values[row["seed_key"]] = row["seed"]
            pairs, limitations, signatures = [], list(budget_limitations), set()
            missing, duplicate, unverified, conflicts = [], [], [], []
            for seed_key in sorted(seed_values, key=lambda key: _seed_sort(seed_values[key])):
                baseline, proposed = by_seed["baseline"][seed_key], by_seed["proposed"][seed_key]
                seed = seed_values[seed_key]
                if len(baseline) > 1 or len(proposed) > 1:
                    ids = [row["id"] for row in baseline + proposed]
                    duplicate.append(seed)
                    excluded_ids.extend(ids)
                    issue("duplicate_seed", f"Seed {seed!r} has multiple final trials within one configuration; ambiguous records are excluded rather than averaged.", ids, comparison_id=comparison_id, seed=seed)
                    continue
                if not baseline or not proposed:
                    ids = [row["id"] for row in baseline + proposed]
                    missing.append(seed)
                    issue("missing_pair", f"Seed {seed!r} lacks a baseline or proposed trial for this configuration combination; no pair is manufactured.", ids, comparison_id=comparison_id, seed=seed)
                    continue
                first, second = baseline[0], proposed[0]
                from .fair_comparison import assert_same_task
                try:
                    assert_same_task(parameters["baseline"][baseline_config], parameters["proposed"][proposed_config])
                except ValueError as exc:
                    ids = [first["id"], second["id"]]
                    conflicts.append(seed)
                    excluded_ids.extend(ids)
                    detected_conflict = True
                    issue("comparison_task_mismatch", f"Seed {seed!r} has incompatible comparison tasks: {exc}; this pair is excluded.", ids,
                          severity="error", comparison_id=comparison_id, seed=seed)
                    continue
                status, conflict, evidence = _pair_provenance(first["trial"], second["trial"])
                if conflict:
                    ids = [first["id"], second["id"]]
                    conflicts.append(seed)
                    excluded_ids.extend(ids)
                    detected_conflict = True
                    issue(conflict, f"Seed {seed!r} has conflicting dataset/split declarations; this pair is excluded.", ids,
                        severity="error", comparison_id=comparison_id, seed=seed, evidence=evidence)
                    continue
                if status == "unverified":
                    unverified.append(seed)
                else:
                    signatures.add(json.dumps(evidence["baseline"], sort_keys=True))
                delta = second["value"] - first["value"]
                if not math.isfinite(delta):
                    ids = [first["id"], second["id"]]
                    excluded_ids.extend(ids)
                    issue("invalid_metric", f"Seed {seed!r} has a nonfinite derived difference; the pair is excluded.", ids, comparison_id=comparison_id)
                    continue
                pairs.append({"seed": seed, "baseline": first["value"], "proposed": second["value"], "delta": delta,
                    "improvement": -delta if direction == "minimize" else delta,
                    "baseline_trial_id": first["id"], "proposed_trial_id": second["id"],
                    "provenance_status": status, "evaluation": evidence["baseline"] if status == "consistent" else evidence})
                all_used_ids.update((first["id"], second["id"]))
            n = len(pairs)
            if missing:
                limitations.append(f"Missing counterpart seeds excluded: {missing}; complete-pair selection may bias the observed difference.")
            if duplicate:
                limitations.append(f"Duplicate seeds excluded within a configuration: {duplicate}; these are not independent repetitions.")
            if conflicts:
                limitations.append(f"Comparison task, dataset or split mismatch excluded seeds: {conflicts}.")
            if unverified:
                message = f"Dataset/split provenance is unverified for seeds {unverified}; matching seed labels alone do not prove comparable data."
                limitations.append(message)
                issue("unverified_evaluation", message, [row[key] for row in pairs if row["provenance_status"] == "unverified"
                    for key in ("baseline_trial_id", "proposed_trial_id")], comparison_id=comparison_id)
            across_seed_conflict = len(signatures) > 1
            if across_seed_conflict:
                detected_conflict = True
                message = "Matched pairs use different declared datasets/split fingerprints across seeds; their pooled mean is descriptive over mixed partitions and no confidence interval is reported."
                limitations.append(message)
                issue("split_fingerprint_mismatch", message, [row[key] for row in pairs for key in ("baseline_trial_id", "proposed_trial_id")],
                    severity="error", comparison_id=comparison_id)
            if n < MIN_CI_PAIRS:
                message = f"Only {n} unique matched seed pairs; fewer than the {MIN_CI_PAIRS}-pair interval reporting threshold. No 95% interval is reported."
                limitations.append(message)
                issue("small_sample", message, comparison_id=comparison_id)
            if not n:
                issue("no_comparable_pairs", "No unambiguous comparable final test pairs remain for this configuration combination.", comparison_id=comparison_id)
            provenance_status = "inconsistent" if conflicts or across_seed_conflict else "unverified" if unverified or not n else "consistent"
            deltas = [row["delta"] for row in pairs]
            interval = None
            if n >= MIN_CI_PAIRS and provenance_status == "consistent":
                try:
                    interval = _t_interval(deltas)
                    if not all(math.isfinite(interval[key]) for key in ("low", "high")):
                        raise ArithmeticError("nonfinite interval")
                except (ArithmeticError, ValueError):
                    interval = None
                    limitations.append("The interval calculation exceeded finite numeric range; no interval is reported.")
            if n >= MIN_CI_PAIRS and provenance_status != "consistent":
                limitations.append("The confidence interval is withheld because common dataset/split provenance is not consistently established by the records.")
            limitations.append("Recorded seed differences do not establish sample independence, correctness of the evaluation code, or generalization to other datasets. No automatic significance or publication-readiness conclusion is made.")
            paired_comparisons.append({"comparison_id": comparison_id, "configuration": proposed_config,
                "baseline_configuration": baseline_config, "proposed_configuration": proposed_config,
                "baseline_parameters": parameters["baseline"][baseline_config],
                "proposed_parameters": parameters["proposed"][proposed_config], "metric": metric, "direction": direction,
                "n_pairs": n, "seeds": [row["seed"] for row in pairs], "pairs": pairs,
                "baseline_mean": _mean(row["baseline"] for row in pairs) if n else None,
                "proposed_mean": _mean(row["proposed"] for row in pairs) if n else None,
                "mean_delta": _mean(deltas) if n else None,
                "improvement": (-1 if direction == "minimize" else 1) * _mean(deltas) if n else None,
                "improvement_definition": "Positive absolute metric difference means better in the requested direction; this is not a percentage.",
                "delta_std": _std(deltas) if n > 1 else None,
                "ci95": interval, "limitations": limitations,
                "provenance": {"status": provenance_status, "verification_scope": VERIFICATION_SCOPE,
                    "declared_evaluation_signatures": len(signatures), "unverified_pair_count": len(unverified)}})

    if not paired_comparisons:
        issue("no_comparable_pairs", "Completed final test records do not contain both a baseline and proposed configuration with valid seeds and metrics.")
    if len(paired_comparisons) > 1:
        issue("multiple_configurations", "Parameter configurations are compared separately. Their trials and reused baselines must not be pooled as independent repetitions; multiple comparisons are not adjusted here.")
    source_records = [trial for trial in completed if str(trial.get("id", "")) in all_used_ids]
    source_tags = sorted({str(trial.get("provenance", "unverified")) for trial in source_records})
    simulated = any(any(word in tag.casefold() for word in ("fixture", "simulation", "simulated")) for tag in source_tags)
    unknown_source = not source_records or any(tag not in {"real_experiment_execution", "real_cpu_execution_on_synthetic_data"} for tag in source_tags)
    if simulated:
        issue("simulation_records", "Some paired records explicitly identify fixture/simulation provenance; these numbers are not represented as live scientific measurements.", all_used_ids)
        for comparison in paired_comparisons:
            comparison["limitations"].append("Input records include fixture/simulation provenance; numerical analysis does not convert them into real research evidence.")
    if len(source_tags) > 1:
        issue("mixed_provenance", f"Paired trial records carry different execution provenance tags: {source_tags}.", all_used_ids)
    identity_statuses = {comparison["provenance"]["status"] for comparison in paired_comparisons}
    overall_status = "inconsistent" if detected_conflict else "unverified" if unknown_source or not all_used_ids or identity_statuses != {"consistent"} else "consistent"
    return {"primary_metric": primary_metric, "test_metric": metric, "direction": direction,
        "paired_comparisons": paired_comparisons, "issues": issues, "tuning_budget": budget_summary,
        "provenance": {"status": overall_status, "source": "persisted_trial_records",
            "verification_scope": VERIFICATION_SCOPE, "execution_tags": source_tags,
            "contains_simulation": simulated, "execution_provenance_unverified": unknown_source,
            "eligible_trial_ids": sorted(set(eligible_ids)), "included_trial_ids": sorted(all_used_ids),
            "excluded_trial_ids": sorted(set(excluded_ids)), "completed_test_records": sum(len(rows) for variant in groups.values() for rows in variant.values()),
            "note": "All differences and intervals are derived from supplied records; no new measurements or external verification were performed."}}
