#!/usr/bin/env python3
"""Bounded, persistent multi-country dispatcher. Execute under Slurm, not on login.

Uses the existing country scripts without changing biological inputs or outputs.
The country concurrency limit is explicit. CPU budgets count PENDING tasks as well
as RUNNING tasks, so independent country arrays cannot multiply the budget.
Scheduler polling is once per 120 s, in a single grouped accounting query.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from datetime import datetime, timezone

from backup_critical_hpg import sha256

STAGES = ("sites", "calendar", "gm_configs", "gm_manifest", "gm_members", "gm_merge",
          "annual_configs", "annual_first", "annual_rest", "master", "analysis")
ARRAY_STAGES = {"gm_configs", "gm_members", "annual_first", "annual_rest"}
BURST_STAGES = {"gm_members", "annual_first", "annual_rest"}
ATTEMPT_LIMITS = {
    "gm_members": 3,       # V3 third attempt can publish an audited empty shard.
    "annual_first": 2,
    "annual_rest": 2,
    "gm_merge": 2,         # Reuse immutable shard selections under 20/24 policy.
    "analysis": 2,         # Reuse numerical tables when only plotting failed.
}
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL",
            "PREEMPTED", "BOOT_FAIL", "DEADLINE", "REVOKED", "SPECIAL_EXIT"}
EXPECTED = {"dssat_executable": "811a53c4d85d2f47ecf7815d9c46bc75fbc5fe5dbd2dd26aae3fce606f2ba41e",
            "disease_parameters": "608dc1ca3d5354d5507bd6a60ac95e6820eab66212fa0dcb1bb3262c4cc4780a"}


def explicit_temporary_rejection(stderr, stdout=""):
    """Only the exact negative scheduler response; never transport errors/timeouts."""
    messages = [s.strip() for s in stderr.splitlines() if s.strip()]
    final = "sbatch: error: Batch job submission failed: Resource temporarily unavailable"
    warning = "sbatch: error: Slurm temporarily unable to accept job, sleeping and retrying"
    return (not stdout.strip() and bool(messages) and messages[-1] == final
            and all(s in {final, warning} for s in messages))


def scheduler_read(command):
    """Retry only read-only accounting/queue queries, never sbatch."""
    if command[0] not in {"sacct", "squeue"}:
        raise ValueError("Scheduler retries are restricted to read-only commands")
    for attempt in range(3):
        try:
            return subprocess.run(command, check=True, capture_output=True, text=True, timeout=180)
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
            if attempt == 2:
                raise
            print(f"SCHEDULER READ RETRY {attempt + 1}/2: {command[0]}", flush=True)
            time.sleep(5 * (attempt + 1))


def validate_capacity(primary, burst, countries):
    # Verified by the user on HPG: wpavan=90 CPUs, wpavan-b=810 CPUs.
    # The controller itself occupies one primary CPU outside these worker budgets.
    if not 4 <= primary <= 89 or not 0 <= burst <= 810:
        raise ValueError("Worker CPU budgets: primary 4..89, burst 0..810; controller adds 1 primary CPU")
    if not 1 <= countries <= 12:
        raise ValueError("Active countries must be 1..12")


def save_json(path, data):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".partial")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def check_frozen_inputs(state):
    profile = state["project"]["profiles"]["hipergator"]
    for key, expected in EXPECTED.items():
        if sha256(Path(profile[key])) != expected:
            raise ValueError(f"Approved {key} changed; refusing global submission")
    python = Path(state["root"]) / "venvs/analysis/bin/python"
    if not python.is_file():
        raise FileNotFoundError(python)


def lines(path):
    return [Path(s) for s in path.read_text(encoding="utf-8").splitlines() if s.strip()]


def paths(state, country):
    base = Path(state["output"]) / country["key"]
    return base, base / "production", Path(state["results"]) / country["key"]


def annual_config_dir(state, country):
    """V4 retries use a fresh directory; V2/V3 outputs remain immutable."""
    _, ready, _ = paths(state, country)
    name = country.get("annual_config_dir", "annual_configs")
    if name not in {"annual_configs", "annual_configs_v4"}:
        raise ValueError(f"Unexpected annual configuration directory: {name!r}")
    return ready / name


def specification(state, country):
    """script, positional arguments, CPU/task, array task indices, hours."""
    base, ready, result = paths(state, country)
    project, sites, harea = base / "project.json", base / "base.gpkg", base / "harea.tif"
    resolved = ready / "calendars/project.json"
    shards, configs = ready / "shards/manifest.txt", ready / "gm_configs"
    annual_dir = annual_config_dir(state, country)
    members, annual = ready / "gm_all_members.txt", annual_dir / "manifest.txt"
    stage = STAGES[country["stage"]]
    if stage in {"gm_configs", "gm_members"}:
        n = len(lines(shards if stage == "gm_configs" else members))
        if not n:
            raise ValueError("Empty maturity manifest")
        ids = list(range(n))
    else:
        ids = [0]
    specs = {
        "sites": ("build_country_sites_wpavan.slurm", [project, state["index"], sites, harea, "nearest"], 1, [0], 12),
        "calendar": ("prepare_country_calendars_wpavan.slurm", [project, sites, ready], 1, [0], 12),
        "gm_configs": ("generate_maturity_shards_wpavan.slurm", [resolved, shards, harea, configs], 4, ids, 12),
        "gm_manifest": ("collect_maturity_members_wpavan.slurm", [shards, configs, members], 1, [0], 1),
        "gm_members": ("run_maturity_ensemble_member.slurm", [members, result / "gm"], 4, ids, 48),
        "gm_merge": ("select_covered_country_wpavan.slurm", [ready, ready, result], 1, [0], 12),
        "annual_configs": ("generate_annual_country_wpavan.slurm", [resolved, ready / "calibrated.gpkg", harea, annual_dir], 4, [0], 12),
        "annual_first": ("run_pythia_annual_wpavan.slurm", [annual, result / "annual"], 4, [0], 24),
        "annual_rest": ("run_pythia_annual_wpavan.slurm", [annual, result / "annual"], 4, list(range(1, 920)), 24),
        "master": ("merge_country_shards_wpavan.slurm", [annual, result / "annual", resolved], 1, [0], 24),
    }
    if stage == "analysis":
        masters = list((result / "annual/country_summaries").glob("*/summary_master_*.csv.gz"))
        coverage = list((result / "annual/_pipeline/merged_wide").glob("*/country_coverage.json"))
        if len(masters) != 1 or len(coverage) != 1:
            raise ValueError("Missing/ambiguous final master or coverage audit")
        return ("analyze_country_master_wpavan.slurm", [masters[0], resolved, result / "analysis", coverage[0],
                state["project"]["profiles"]["hipergator"]["countries"]], 1, [0], 24)
    return specs[stage]


def parse_accounting(text):
    result = {}
    for line in text.splitlines():
        fields = [s.strip() for s in line.split("|")]
        if len(fields) < 3:
            continue
        job, status, code = fields[:3]
        if not re.fullmatch(r"\d+(?:_\d+)?", job):
            continue
        status = status.split()[0].rstrip("+")
        result[job] = "COMPLETED" if status == "COMPLETED" and code == "0:0" else (
            "FAILED" if status == "COMPLETED" else status)
    return result


def terminal(batch, index):
    return batch["states"].get(str(index), "UNKNOWN") in TERMINAL


def reservations(state):
    used = {qos: 0 for qos in state["budget_cpus"]}
    for country in state["countries"]:
        for batch in country["batches"]:
            used[batch["qos"]] += batch["cpus"] * sum(not terminal(batch, i) for i in batch["indices"])
    return used


def task_status(country, indices):
    success, active, attempts = set(), set(), {i: 0 for i in indices}
    for batch in country["batches"]:
        for i in batch["indices"]:
            attempts[i] += 1
            status = batch["states"].get(str(i), "UNKNOWN")
            if status == "COMPLETED":
                success.add(i)
            elif status not in TERMINAL:
                active.add(i)
    attempt_limit = ATTEMPT_LIMITS.get(STAGES[country["stage"]], 1)
    exhausted = {i for i in indices if i not in success | active and attempts[i] >= attempt_limit}
    available = [i for i in indices if i not in success | active | exhausted]
    # Recover failed cases first; never repeat already successful indices.
    available.sort(key=lambda i: (-attempts[i], i))
    return success, active, exhausted, available


def recover_published(state, country, index):
    """A task may publish atomically then lose its scheduler exit notification."""
    stage = STAGES[country["stage"]]
    if stage not in {"gm_members", "annual_first", "annual_rest"}:
        return False
    _, ready, result = paths(state, country)
    gm = stage == "gm_members"
    manifest = ready / "gm_all_members.txt" if gm else annual_config_dir(state, country) / "manifest.txt"
    config = lines(manifest)[index]
    target = result / ("gm/calibration_members" if gm else "annual/tasks") / config.stem
    audit_path = target / "audit.json"
    if not audit_path.is_file():
        return False
    audit = json.loads(audit_path.read_text())
    return (audit.get("status") in ({"ok", "empty"} if gm else {"ok"}) and
            audit.get("source_config_sha256") == sha256(config))


def refresh(state):
    batches = [(c, b) for c in state["countries"] for b in c["batches"]
               if any(not terminal(b, i) for i in b["indices"])]
    if not batches:
        return
    if any(not b.get("job") for _, b in batches):
        raise RuntimeError("Unresolved submission intent. Inspect state.json and Slurm by recorded job name before resuming; do not submit duplicates.")
    ids = ",".join(b["job"] for _, b in batches)
    result = scheduler_read(["sacct", "-X", "-n", "-P", "--array", "-j", ids,
                             "--format=JobID%100,State%30,ExitCode"])
    current = parse_accounting(result.stdout)
    for country, batch in batches:
        for i in batch["indices"]:
            if terminal(batch, i):
                continue
            job = batch["job"] + (f"_{i}" if batch["array"] else "")
            status = current.get(job, "UNKNOWN")
            if status in TERMINAL and status != "COMPLETED" and recover_published(state, country, i):
                status = "COMPLETED"
            batch["states"][str(i)] = status


def submit(state, country, qos, indices, state_path):
    script, arguments, cpus, _, hours = specification(state, country)
    stage = STAGES[country["stage"]]
    code = Path(state["code"])
    if not (code / "slurm" / script).is_file():
        raise FileNotFoundError(script)
    base, ready, _ = paths(state, country)
    if stage == "calendar":
        ready.mkdir(parents=True, exist_ok=True)
        save_json(ready / "recovery.json", {"reference_sites": str(base / "base.gpkg"),
                                           "mode": "global_coverage95"})
    exports = (f"ALL,SBRUST_CODE_ROOT={code},SBRUST_ANALYSIS_CODE_ROOT={code},"
               f"SBRUST_ALLOW_EXCLUSIONS=1,SBRUST_COVERAGE_REFERENCE={base / 'base.gpkg'}")
    # Explicitly clear inherited values so an exception cannot leak to another country.
    exception_path = ""
    if stage == "master" and country.get("coverage_exception"):
        from country_coverage_exception import validate_exception
        validate_exception(country["coverage_exception"], country["country"])
        exception_path = str(base / "coverage_exception_v42.json")
        save_json(Path(exception_path), country["coverage_exception"])
    exports += f",SBRUST_COVERAGE_EXCEPTION={exception_path}"
    token = hashlib.sha256(state["output"].encode()).hexdigest()[:6]
    name = f"sw-{token}-{country['key'][:24]}-{stage}-{len(country['batches'])}"
    epoch = state.get("submission_epoch")
    if epoch:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,16}", epoch):
            raise ValueError("Invalid submission epoch")
        name += f"-{epoch}"
    if country.get("rejected_submissions"):
        name += f"-r{len(country['rejected_submissions'])}"
    # Journal BEFORE calling sbatch. A crash in the acceptance window is explicit,
    # not silently retried. Completed simulation directories remain untouched.
    batch = {"job": None, "name": name, "qos": qos, "cpus": cpus, "indices": indices,
             "submission_started_utc": datetime.now(timezone.utc).isoformat(),
             "array": stage in ARRAY_STAGES, "states": {str(i): "UNKNOWN" for i in indices}}
    country["batches"].append(batch)
    command = ["sbatch", "--parsable", "--account=wpavan", f"--qos={qos}", f"--job-name={name}",
               f"--cpus-per-task={cpus}", f"--time={hours}:00:00", f"--export={exports}",
               "--no-requeue"]
    if batch["array"]:
        command.append("--array=" + ",".join(map(str, indices)) + f"%{len(indices)}")
    command.extend([str(code / "slurm" / script), *map(str, arguments)])
    batch["command"] = command
    save_json(state_path, state)
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        batch["submission_error"] = "sbatch response timed out after 300s; acceptance is unknown"
        save_json(state_path, state)
        raise RuntimeError(
            f"Unresolved sbatch submission {name}: response timed out; "
            "reconcile queue/accounting before retrying. No duplicate was submitted."
        ) from None
    if result.returncode:
        batch["submission_error"] = result.stderr
        batch["submission_stdout"] = result.stdout
        if explicit_temporary_rejection(result.stderr, result.stdout):
            # This is an explicit rejection, not an uncertain acceptance window.
            # Keep the receipt but do not count it as a model execution attempt.
            country.setdefault("rejected_submissions", []).append({
                **batch, "resolution": "explicit_temporary_rejection", "returncode": result.returncode})
            country["batches"].remove(batch)
            count = min(state.get("submission_rejection_streak", 0) + 1, 5)
            delay = min(120 * 2 ** (count - 1), 1800)
            state["submission_rejection_streak"] = count
            state["next_submission_after"] = time.time() + delay
            save_json(state_path, state)
            print(f"SUBMISSION DEFERRED {country['country']} {stage}: scheduler rejected; retry after {delay}s", flush=True)
            return False
        save_json(state_path, state)
        raise RuntimeError(f"sbatch failed; stopped safely with journal preserved: {result.stderr}")
    job = result.stdout.strip().split(";", 1)[0]
    if not re.fullmatch(r"\d+", job):
        raise RuntimeError(f"Ambiguous sbatch response: {result.stdout!r}")
    batch["job"] = job
    state.pop("next_submission_after", None)
    state.pop("submission_rejection_streak", None)
    save_json(state_path, state)
    print(f"SUBMITTED {country['country']} {stage} {qos} tasks={len(indices)} job={job}", flush=True)
    return True


def advance(state, country):
    stage = STAGES[country["stage"]]
    if stage == "sites":
        import fiona
        base, _, _ = paths(state, country)
        with fiona.open(base / "base.gpkg") as source:
            country["site_count"] = len(source)
        if not country["site_count"]:
            raise ValueError("Positive-SPAM country generated no simulation sites")
    if stage == "annual_configs":
        _, ready, _ = paths(state, country)
        if len(lines(annual_config_dir(state, country) / "manifest.txt")) != 920:
            raise ValueError("Annual matrix is not 920 configurations")
    country["history"][stage] = country["batches"]
    country["batches"] = []
    country["stage"] += 1
    if country["stage"] == len(STAGES):
        country["status"] = "completed"
    print(f"FINISHED {country['country']} {stage}", flush=True)


def summary(state):
    from collections import Counter
    counts = Counter(c["status"] for c in state["countries"])
    out = ["GLOBAL CAMPAIGN — configured rainfed soybean", json.dumps(counts, sort_keys=True),
           f"Outstanding reserved CPUs: {reservations(state)}; controller adds 1 wpavan CPU",
           "COUNTRY | STATUS | STAGE | BASE POINTS | DETAIL"]
    for c in state["countries"]:
        if c["status"] == "no_soy":
            continue
        stage = STAGES[c["stage"]] if c["stage"] < len(STAGES) else "done"
        out.append(f"{c['country']} | {c['status']} | {stage} | {c.get('site_count', '?')} | {c.get('error', '')}")
    return "\n".join(out) + "\n"


def tick(state, state_path):
    refresh(state)
    # Advance successful stages; isolate country-local failures, never conceal them.
    for country in state["countries"]:
        if country["status"] not in {"active", "draining"}:
            continue
        try:
            _, _, _, indices, _ = specification(state, country)
            success, active, exhausted, _ = task_status(country, indices)
            if exhausted:
                country["error"] = f"{STAGES[country['stage']]} failed indices={sorted(exhausted)}; outputs retained"
                country["status"] = "draining" if active else "failed"
            elif country["status"] == "draining":
                if not active:
                    country["status"] = "failed"
            elif success == set(indices):
                advance(state, country)
        except (ValueError, FileNotFoundError, KeyError, IndexError) as exc:
            # Specifications only touch outputs of preceding COMPLETED stages.
            country["error"] = str(exc)
            country["status"] = "draining" if any(
                not terminal(b, i) for b in country["batches"] for i in b["indices"]) else "failed"
    active_count = sum(c["status"] in {"active", "draining"} for c in state["countries"])
    for c in state["countries"]:
        if c["status"] == "pending" and active_count < state["max_active_countries"]:
            c["status"] = "active"
            active_count += 1
    used = reservations(state)
    # Rotate the dispatch order, avoiding permanent preference for the first country.
    candidates = [c for c in state["countries"] if c["status"] == "active"]
    if time.time() < state.get("next_submission_after", 0):
        candidates = []
    turn = state.get("turn", 0)
    if candidates:
        shift = turn % len(candidates)
        candidates = candidates[shift:] + candidates[:shift]
    deferred = False
    for c in candidates:
        try:
            _, _, cpus, indices, _ = specification(state, c)
        except (ValueError, FileNotFoundError, KeyError) as exc:
            c["status"], c["error"] = "failed", str(exc)
            continue
        _, _, _, available = task_status(c, indices)
        for qos, limit in state["budget_cpus"].items():
            # Serial preparation/merge/analysis use primary; burst is for 4-core simulation arrays.
            if qos == "wpavan-b" and STAGES[c["stage"]] not in BURST_STAGES:
                continue
            # Fill the globally shared budget, not the old 20/80 per-dispatch caps.
            n = min(len(available), (limit - used[qos]) // cpus)
            if n <= 0:
                continue
            selected, available = available[:n], available[n:]
            if submit(state, c, qos, selected, state_path) is False:
                deferred = True
                break
            used[qos] += cpus * n
        if deferred:
            break
    state["turn"] = turn + 1
    outstanding = any(c["status"] in {"pending", "active", "draining"} for c in state["countries"])
    state["status"] = "running" if outstanding else "finished_with_failures" if any(
        c["status"] == "failed" for c in state["countries"]) else "finished"
    save_json(state_path, state)
    (Path(state["output"]) / "STATUS.txt").write_text(summary(state), encoding="utf-8")
    return outstanding


def run(state_path):
    import fcntl
    if not os.environ.get("SLURM_JOB_ID"):
        raise RuntimeError("Submit run_world_campaign_wpavan.slurm; do not run this loop on login")
    with state_path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        validate_capacity(state["budget_cpus"]["wpavan"], state["budget_cpus"]["wpavan-b"],
                          state["max_active_countries"])
        check_frozen_inputs(state)
        state.setdefault("controllers", []).append(os.environ["SLURM_JOB_ID"])
        save_json(state_path, state)
        while True:
            try:
                more = tick(state, state_path)
            except Exception as exc:
                state["controller_error"] = str(exc)
                save_json(state_path, state)
                raise
            if not more:
                print(summary(state), flush=True)
                break
            time.sleep(120)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", type=Path)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.status:
        print(summary(json.loads(args.state.read_text(encoding="utf-8"))))
    else:
        run(args.state)
