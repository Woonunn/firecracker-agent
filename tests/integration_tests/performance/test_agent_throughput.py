# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Performance experiment for max synthetic agent throughput.

The experiment is intentionally env-driven and non-CI. Run it twice with the
same generated sequence file and different binaries:

    AGENT_THROUGHPUT_MODE=agent pytest ... --binary-dir=/path/to/firecracker-agent
    AGENT_THROUGHPUT_MODE=baseline \
        AGENT_THROUGHPUT_SEQ_FILE=/path/to/agent_throughput_sequence.json \
        pytest ... --binary-dir=/path/to/normal-firecracker
"""

import concurrent.futures
import json
import math
import os
import random
import shutil
import statistics
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

from framework.artifacts import GUEST_KERNELS_6_1, pin_guest_kernel
from framework.utils import get_resident_memory
from host_tools.network import NetNs

NS_IN_MSEC = 1_000_000
SEQUENCE_SCHEMA = "firecracker-agent-throughput-sequence-v1"
DEFAULT_SEED = 20260603
DEFAULT_SWEEP = "8,16,32,64,96,128"
DEFAULT_RUNNING_RATIO = 0.30
DEFAULT_DURATION_SEC = 30.0
DEFAULT_RUN_TARGET_SEC = 0.20
DEFAULT_GUEST_MEM_MIB = 512
DEFAULT_JOB_TIMEOUT_SEC = 30.0
DEFAULT_LATENCY_SLO_MS = 10_000.0
DEFAULT_FAILURE_THRESHOLD = 0.0
DEFAULT_THROUGHPUT_REGRESSION_THRESHOLD = 0.10
DEFAULT_RSS_SAMPLE_INTERVAL_SEC = 1.0

FAILURE_KINDS = (
    "vm_start_failure",
    "api_failure",
    "ssh_failure",
    "job_timeout",
    "oom",
    "swap_exhaustion",
)

REQUIRED_CONCURRENCY_RESULT_FIELDS = {
    "mode",
    "concurrency",
    "duration_sec",
    "completed_jobs",
    "completed_jobs_per_sec",
    "aggregate_cpu_utilization",
    "job_latency_ms",
    "wait_entry_latency_ms",
    "resume_latency_ms",
    "running_phase_latency_ms",
    "aggregate_rss_kib",
    "memory_efficiency_jobs_per_sec_per_gib_host_rss",
    "failure_counts",
    "failure_rate",
    "usable",
}


@dataclass(frozen=True)
class ThroughputConfig:
    """Configuration parsed from AGENT_THROUGHPUT_* environment variables."""

    mode: str
    seed: int
    sweep: tuple[int, ...]
    running_ratio: float
    duration_sec: float
    run_target_sec: float
    guest_mem_mib: int
    job_timeout_sec: float
    latency_slo_ms: float
    failure_threshold: float
    throughput_regression_threshold: float
    sequence_file: Path | None
    comparison_summary_file: Path | None
    pool_dir: Path | None
    rss_sample_interval_sec: float

    @property
    def max_concurrency(self):
        return max(self.sweep)


@dataclass(frozen=True)
class ThroughputPool:
    """Pre-created jailer and netns resources for the throughput test."""

    chroot_base: Path
    netns_names: tuple[str, ...]
    microvm_ids: tuple[str, ...]

    @classmethod
    def load(cls, pool_dir, required_concurrency):
        manifest_path = pool_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema") != "firecracker-agent-throughput-pool-v1":
            raise ValueError(f"Unsupported throughput pool schema in {manifest_path}")

        resources = manifest.get("resources", [])
        if len(resources) < required_concurrency:
            raise ValueError(
                f"{manifest_path} has {len(resources)} resources, "
                f"but {required_concurrency} are required"
            )

        return cls(
            chroot_base=Path(manifest["chroot_base"]),
            netns_names=tuple(
                resource["netns_name"]
                for resource in resources[:required_concurrency]
            ),
            microvm_ids=tuple(
                resource["microvm_id"]
                for resource in resources[:required_concurrency]
            ),
        )


class _RssSampler:
    """Samples aggregate RSS for a list of Firecracker processes."""

    def __init__(self, microvms, interval_sec):
        self._microvms = microvms
        self._interval_sec = interval_sec
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.samples = []

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join()
        return self.samples

    def _run(self):
        while not self._stop.is_set():
            sample = _aggregate_rss_sample(self._microvms)
            if sample is not None:
                self.samples.append(sample)
            self._stop.wait(self._interval_sec)


def _patch_agent_runtime(microvm, **kwargs):
    return microvm.api.vm.request("PATCH", "/agent/runtime", **kwargs)


def _host_swap_enabled():
    try:
        with open("/proc/swaps", encoding="utf-8") as file:
            return len([line for line in file.read().splitlines() if line.strip()]) > 1
    except OSError:
        return False


def _ensure_mode_preconditions(mode):
    if mode == "agent" and not _host_swap_enabled():
        pytest.skip("Host swap is disabled; MADV_PAGEOUT reclaim is unavailable.")


def _expose_proc_swaps_to_jail(microvm):
    proc_dir = Path(microvm.chroot()) / "proc"
    proc_dir.mkdir(exist_ok=True)
    (proc_dir / "swaps").write_text(
        Path("/proc/swaps").read_text(encoding="utf-8"),
        encoding="utf-8",
    )


def _rss_kib(microvm):
    return get_resident_memory(microvm.ps)


def _aggregate_rss_sample(microvms):
    rss_kib = 0
    live_vms = 0
    for microvm in microvms:
        try:
            rss_kib += _rss_kib(microvm)
            live_vms += 1
        except Exception:  # best-effort sampler; failures are recorded elsewhere
            continue
    if live_vms == 0:
        return None
    return {
        "timestamp_monotonic_sec": time.monotonic(),
        "rss_kib": rss_kib,
        "live_vms": live_vms,
    }


def _process_cpu_time_sec(microvm):
    cpu_times = microvm.ps.cpu_times()
    return cpu_times.user + cpu_times.system


def _aggregate_cpu_time_sec(microvms):
    total = 0.0
    live_vms = 0
    for microvm in microvms:
        try:
            total += _process_cpu_time_sec(microvm)
            live_vms += 1
        except Exception:  # best-effort accounting; failures are recorded elsewhere
            continue
    return total, live_vms


def _empty_cpu_utilization():
    return {
        "sample_count": 0,
        "host_cpu_count": os.cpu_count() or 1,
        "window_sec": 0.0,
        "total_cpu_time_sec": 0.0,
        "average_one_cpu_percent": 0.0,
        "average_host_capacity_ratio": 0.0,
        "average_host_capacity_percent": 0.0,
        "average_per_vm_one_cpu_percent": 0.0,
    }


def _build_cpu_utilization(
    *,
    cpu_start_sec,
    cpu_end_sec,
    start_monotonic,
    end_monotonic,
    concurrency,
):
    if cpu_start_sec is None or cpu_end_sec is None:
        return _empty_cpu_utilization()

    window_sec = max(end_monotonic - start_monotonic, 0.0)
    total_cpu_time_sec = max(cpu_end_sec - cpu_start_sec, 0.0)
    host_cpu_count = os.cpu_count() or 1
    average_one_cpu_percent = (
        100.0 * total_cpu_time_sec / window_sec if window_sec > 0 else 0.0
    )
    average_host_capacity_ratio = (
        total_cpu_time_sec / (window_sec * host_cpu_count) if window_sec > 0 else 0.0
    )
    return {
        "sample_count": 2,
        "host_cpu_count": host_cpu_count,
        "window_sec": window_sec,
        "total_cpu_time_sec": total_cpu_time_sec,
        "average_one_cpu_percent": average_one_cpu_percent,
        "average_host_capacity_ratio": average_host_capacity_ratio,
        "average_host_capacity_percent": average_host_capacity_ratio * 100.0,
        "average_per_vm_one_cpu_percent": (
            average_one_cpu_percent / concurrency if concurrency > 0 else 0.0
        ),
    }


def _env_path(name):
    value = os.environ.get(name)
    return Path(value) if value else None


def _env_int(name, default):
    return int(os.environ.get(name, default))


def _env_float(name, default):
    return float(os.environ.get(name, default))


def _parse_sweep(value):
    sweep = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    if not sweep:
        raise ValueError("AGENT_THROUGHPUT_SWEEP must contain at least one integer")
    if any(concurrency <= 0 for concurrency in sweep):
        raise ValueError("AGENT_THROUGHPUT_SWEEP values must be positive")
    return tuple(sorted(dict.fromkeys(sweep)))


def _parse_config():
    mode = os.environ.get("AGENT_THROUGHPUT_MODE", "agent")
    if mode not in ("agent", "baseline"):
        raise ValueError("AGENT_THROUGHPUT_MODE must be 'agent' or 'baseline'")

    running_ratio = _env_float(
        "AGENT_THROUGHPUT_RUNNING_RATIO", DEFAULT_RUNNING_RATIO
    )
    if not 0 < running_ratio < 1:
        raise ValueError("AGENT_THROUGHPUT_RUNNING_RATIO must be between 0 and 1")

    failure_threshold = _env_float(
        "AGENT_THROUGHPUT_FAILURE_THRESHOLD", DEFAULT_FAILURE_THRESHOLD
    )
    if not 0 <= failure_threshold <= 1:
        raise ValueError("AGENT_THROUGHPUT_FAILURE_THRESHOLD must be between 0 and 1")

    return ThroughputConfig(
        mode=mode,
        seed=_env_int("AGENT_THROUGHPUT_SEED", DEFAULT_SEED),
        sweep=_parse_sweep(os.environ.get("AGENT_THROUGHPUT_SWEEP", DEFAULT_SWEEP)),
        running_ratio=running_ratio,
        duration_sec=_env_float(
            "AGENT_THROUGHPUT_DURATION_SEC", DEFAULT_DURATION_SEC
        ),
        run_target_sec=_env_float(
            "AGENT_THROUGHPUT_RUN_TARGET_SEC", DEFAULT_RUN_TARGET_SEC
        ),
        guest_mem_mib=_env_int(
            "AGENT_THROUGHPUT_GUEST_MEM_MIB", DEFAULT_GUEST_MEM_MIB
        ),
        job_timeout_sec=_env_float(
            "AGENT_THROUGHPUT_JOB_TIMEOUT_SEC", DEFAULT_JOB_TIMEOUT_SEC
        ),
        latency_slo_ms=_env_float(
            "AGENT_THROUGHPUT_LATENCY_SLO_MS", DEFAULT_LATENCY_SLO_MS
        ),
        failure_threshold=failure_threshold,
        throughput_regression_threshold=_env_float(
            "AGENT_THROUGHPUT_REGRESSION_THRESHOLD",
            DEFAULT_THROUGHPUT_REGRESSION_THRESHOLD,
        ),
        sequence_file=_env_path("AGENT_THROUGHPUT_SEQ_FILE"),
        comparison_summary_file=_env_path("AGENT_THROUGHPUT_COMPARE_SUMMARY"),
        pool_dir=_env_path("AGENT_THROUGHPUT_POOL_DIR"),
        rss_sample_interval_sec=_env_float(
            "AGENT_THROUGHPUT_RSS_SAMPLE_INTERVAL_SEC",
            DEFAULT_RSS_SAMPLE_INTERVAL_SEC,
        ),
    )


def _percentile(values, percentile):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = round((len(ordered) - 1) * percentile / 100)
    return ordered[index]


def _latency_summary(values):
    if not values:
        return {
            "count": 0,
            "min": 0.0,
            "max": 0.0,
            "mean": 0.0,
            "p50": 0.0,
            "p95": 0.0,
            "p99": 0.0,
        }
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "mean": statistics.mean(values),
        "p50": _percentile(values, 50),
        "p95": _percentile(values, 95),
        "p99": _percentile(values, 99),
    }


def _empty_failure_counts():
    return {kind: 0 for kind in FAILURE_KINDS}


def _classify_failure(exc):
    exc_type = type(exc).__name__.lower()
    text = str(exc).lower()
    if "timeout" in text or "timed out" in text:
        return "job_timeout"
    if "out of memory" in text or "oom" in text or "cannot allocate memory" in text:
        return "oom"
    if "swap" in text:
        return "swap_exhaustion"
    if (
        "ssh" in text
        or "connection" in text
        or "kex" in text
        or exc_type in ("childprocesserror", "timeoutexpired")
    ):
        return "ssh_failure"
    return "api_failure"


def _failure_detail(agent_index, cycle_index, kind, exc):
    return {
        "agent_index": agent_index,
        "cycle_index": cycle_index,
        "kind": kind,
        "error": str(exc),
        "exception_type": type(exc).__name__,
        "exception_repr": repr(exc),
    }


def _write_json(path, obj):
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _generate_sequence(
    *,
    seed,
    max_concurrency,
    duration_sec,
    running_ratio,
    run_target_sec,
):
    """Generate a deterministic staggered wait/run schedule."""

    rng = random.Random(seed)
    wait_to_run = (1.0 - running_ratio) / running_ratio
    typical_cycle_sec = run_target_sec + run_target_sec * wait_to_run
    horizon_sec = duration_sec + typical_cycle_sec * 2

    agents = []
    for agent_index in range(max_concurrency):
        cycles = []
        elapsed = 0.0
        while elapsed < horizon_sec:
            run_sec = run_target_sec * rng.uniform(0.85, 1.15)
            wait_sec = run_sec * wait_to_run
            cycles.append(
                {
                    "wait_sec": round(wait_sec, 6),
                    "run_target_sec": round(run_sec, 6),
                }
            )
            elapsed += wait_sec + run_sec

        agents.append(
            {
                "agent_index": agent_index,
                "initial_delay_sec": round(rng.uniform(0, typical_cycle_sec), 6),
                "cycles": cycles,
            }
        )

    return {
        "schema": SEQUENCE_SCHEMA,
        "seed": seed,
        "max_concurrency": max_concurrency,
        "duration_sec": duration_sec,
        "running_ratio": running_ratio,
        "run_target_sec": run_target_sec,
        "agents": agents,
    }


def _sequence_running_ratio(sequence):
    run_total = 0.0
    wait_total = 0.0
    for agent in sequence["agents"]:
        for cycle in agent["cycles"]:
            run_total += cycle["run_target_sec"]
            wait_total += cycle["wait_sec"]
    return run_total / (run_total + wait_total)


def _load_or_generate_sequence(config, results_dir):
    if config.sequence_file is not None:
        sequence = json.loads(config.sequence_file.read_text(encoding="utf-8"))
        _validate_sequence(sequence, config.max_concurrency)
        return sequence, config.sequence_file

    sequence = _generate_sequence(
        seed=config.seed,
        max_concurrency=config.max_concurrency,
        duration_sec=config.duration_sec,
        running_ratio=config.running_ratio,
        run_target_sec=config.run_target_sec,
    )
    sequence_path = results_dir / "agent_throughput_sequence.json"
    _write_json(sequence_path, sequence)
    return sequence, sequence_path


def _validate_sequence(sequence, required_concurrency):
    if sequence.get("schema") != SEQUENCE_SCHEMA:
        raise ValueError(
            f"Unsupported throughput sequence schema: {sequence.get('schema')}"
        )
    agents = sequence.get("agents", [])
    if len(agents) < required_concurrency:
        raise ValueError(
            "AGENT_THROUGHPUT_SEQ_FILE has fewer agents than the requested sweep "
            f"requires: {len(agents)} < {required_concurrency}"
        )
    for index, agent in enumerate(agents[:required_concurrency]):
        if agent.get("agent_index") != index:
            raise ValueError("AGENT_THROUGHPUT_SEQ_FILE agents must be index ordered")
        if not agent.get("cycles"):
            raise ValueError("AGENT_THROUGHPUT_SEQ_FILE agents must contain cycles")


def _enter_wait(microvm, mode):
    if mode != "agent":
        return 0.0
    start_ns = time.perf_counter_ns()
    _patch_agent_runtime(microvm, state="LlmWaiting", pause_on_wait=True)
    return (time.perf_counter_ns() - start_ns) / NS_IN_MSEC


def _exit_wait(microvm, mode):
    if mode != "agent":
        return 0.0
    start_ns = time.perf_counter_ns()
    _patch_agent_runtime(microvm, state="Running")
    return (time.perf_counter_ns() - start_ns) / NS_IN_MSEC


def _throughput_ssh(microvm):
    ssh = getattr(microvm, "_agent_throughput_ssh", None)
    if ssh is None:
        ssh = microvm.ssh
        microvm._agent_throughput_ssh = ssh
    return ssh


def _start_fast_page_fault_helper(microvm, timeout_sec):
    ssh = _throughput_ssh(microvm)
    ssh.check_output(
        "rm -f /tmp/fast_page_fault_helper.out; "
        "nohup /usr/local/bin/fast_page_fault_helper >/dev/null 2>&1 </dev/null &",
        timeout=timeout_sec,
    )
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        result = ssh.run("pidof fast_page_fault_helper", timeout=timeout_sec)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split()[0]
        time.sleep(0.05)
    raise TimeoutError("Timed out waiting for fast_page_fault_helper to start")


def _trigger_sandbox_memory_touch(microvm, pid, timeout_sec):
    ssh = _throughput_ssh(microvm)
    ssh.check_output("rm -f /tmp/fast_page_fault_helper.out", timeout=timeout_sec)
    start_ns = time.perf_counter_ns()
    ssh.check_output(f"kill -s SIGUSR1 {pid}", timeout=timeout_sec)
    duration_ns = int(
        ssh.check_output(
            "while ! grep -Eq '^[0-9]+$' /tmp/fast_page_fault_helper.out 2>/dev/null; "
            "do sleep 0.01; done; "
            "cat /tmp/fast_page_fault_helper.out",
            timeout=timeout_sec,
        ).stdout.strip()
    )
    total_ns = time.perf_counter_ns() - start_ns
    return duration_ns / NS_IN_MSEC, total_ns / NS_IN_MSEC


def _sleep_until(monotonic_deadline):
    remaining = monotonic_deadline - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)


def _run_one_agent(microvm, mode, schedule, window_start, deadline, job_timeout_sec):
    samples = []
    failure_counts = Counter()
    failure_details = []

    _sleep_until(window_start + schedule["initial_delay_sec"])

    for cycle_index, cycle in enumerate(schedule["cycles"]):
        wait_sec = cycle["wait_sec"]
        run_target_sec = cycle["run_target_sec"]
        if time.monotonic() + wait_sec + run_target_sec >= deadline:
            break

        job_start_ns = time.perf_counter_ns()
        pid = None
        try:
            helper_start_ns = time.perf_counter_ns()
            pid = _start_fast_page_fault_helper(microvm, job_timeout_sec)
            helper_start_ms = (time.perf_counter_ns() - helper_start_ns) / NS_IN_MSEC

            wait_entry_latency_ms = _enter_wait(microvm, mode)
            time.sleep(wait_sec)
            resume_latency_ms = _exit_wait(microvm, mode)

            run_start_ns = time.perf_counter_ns()
            sandbox_touch_ms, sandbox_total_ms = _trigger_sandbox_memory_touch(
                microvm, pid, job_timeout_sec
            )
            running_phase_ms = (time.perf_counter_ns() - run_start_ns) / NS_IN_MSEC

            remaining_run_sec = run_target_sec - running_phase_ms / 1000.0
            if remaining_run_sec > 0:
                time.sleep(remaining_run_sec)
                running_phase_ms += remaining_run_sec * 1000.0

            job_latency_ms = (time.perf_counter_ns() - job_start_ns) / NS_IN_MSEC
            if time.monotonic() <= deadline:
                samples.append(
                    {
                        "agent_index": schedule["agent_index"],
                        "cycle_index": cycle_index,
                        "wait_sec": wait_sec,
                        "run_target_sec": run_target_sec,
                        "helper_start_latency_ms": helper_start_ms,
                        "wait_entry_latency_ms": wait_entry_latency_ms,
                        "resume_latency_ms": resume_latency_ms,
                        "running_phase_latency_ms": running_phase_ms,
                        "sandbox_touch_latency_ms": sandbox_touch_ms,
                        "sandbox_total_latency_ms": sandbox_total_ms,
                        "job_latency_ms": job_latency_ms,
                    }
                )
        except Exception as exc:  # failures are part of the capacity signal
            kind = _classify_failure(exc)
            failure_counts[kind] += 1
            failure_details.append(
                _failure_detail(
                    schedule["agent_index"],
                    cycle_index,
                    kind,
                    exc,
                )
            )
            if mode == "agent":
                try:
                    _exit_wait(microvm, mode)
                except Exception:
                    pass
            try:
                _throughput_ssh(microvm).run(
                    "pkill -9 fast_page_fault_helper 2>/dev/null || true",
                    timeout=job_timeout_sec,
                )
            except Exception:
                pass
            break
        finally:
            pid = None

    return {
        "samples": samples,
        "failure_counts": dict(failure_counts),
        "failure_details": failure_details,
    }


def _build_microvms(microvm_factory, guest_kernel, rootfs, pci_enabled, count, config):
    microvms = []
    failure_details = []
    failure_counts = Counter()
    pool = (
        ThroughputPool.load(config.pool_dir, count)
        if config.pool_dir is not None
        else None
    )

    for vm_index in range(count):
        try:
            build_kwargs = {}
            if pool is not None:
                build_kwargs = {
                    "microvm_id": pool.microvm_ids[vm_index],
                    "netns": NetNs(pool.netns_names[vm_index]),
                    "jailer_kwargs": {"chroot_base": pool.chroot_base},
                }
            vm = microvm_factory.build(
                guest_kernel,
                rootfs,
                pci=pci_enabled,
                monitor_memory=False,
                **build_kwargs,
            )
            vm.spawn(log_level="Info", emit_metrics=False)
            if config.mode == "agent":
                _expose_proc_swaps_to_jail(vm)
            vm.basic_config(vcpu_count=1, mem_size_mib=config.guest_mem_mib)
            vm.add_net_iface()
            vm.start()
            vm._agent_throughput_ssh = vm.ssh
            microvms.append(vm)
        except Exception as exc:
            failure_counts["vm_start_failure"] += 1
            failure_details.append(
                {"vm_index": vm_index, "kind": "vm_start_failure", "error": str(exc)}
            )
            break

    return microvms, dict(failure_counts), failure_details


def _cleanup_microvms(microvm_factory, preserve_pool):
    if not preserve_pool:
        microvm_factory.kill()
        return

    for vm in microvm_factory.vms:
        vm.kill()
        vm.jailer.cleanup()
        chroot_base_with_id = vm.jailer.chroot_base_with_id()
        if len(vm.jailer.jailer_id) > 0 and chroot_base_with_id.exists():
            shutil.rmtree(chroot_base_with_id)

    microvm_factory.vms.clear()


def _run_concurrency_point(
    *,
    microvm_factory,
    guest_kernel,
    rootfs,
    pci_enabled,
    config,
    sequence,
    concurrency,
):
    setup_start = time.monotonic()
    microvms = []
    rss_samples = []
    try:
        microvms, setup_failures, failure_details = _build_microvms(
            microvm_factory,
            guest_kernel,
            rootfs,
            pci_enabled,
            concurrency,
            config,
        )
        if len(microvms) != concurrency:
            return _build_concurrency_result(
                config=config,
                concurrency=concurrency,
                setup_duration_sec=time.monotonic() - setup_start,
                samples=[],
                rss_samples=rss_samples,
                cpu_utilization=_empty_cpu_utilization(),
                failure_counts=setup_failures,
                failure_details=failure_details,
            )

        rss_sampler = _RssSampler(microvms, config.rss_sample_interval_sec)
        rss_sampler.start()
        cpu_start_sec = None
        cpu_end_sec = None
        cpu_window_start = 0.0
        cpu_window_end = 0.0
        try:
            window_start = time.monotonic() + 0.5
            deadline = window_start + config.duration_sec
            schedules = sequence["agents"][:concurrency]
            samples = []
            failure_counts = Counter(setup_failures)
            _sleep_until(window_start)
            cpu_window_start = time.monotonic()
            cpu_start_sec, _ = _aggregate_cpu_time_sec(microvms)

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=concurrency
            ) as executor:
                futures = [
                    executor.submit(
                        _run_one_agent,
                        microvm,
                        config.mode,
                        schedule,
                        window_start,
                        deadline,
                        config.job_timeout_sec,
                    )
                    for microvm, schedule in zip(microvms, schedules)
                ]
                for future in concurrent.futures.as_completed(futures):
                    outcome = future.result()
                    samples.extend(outcome["samples"])
                    failure_counts.update(outcome["failure_counts"])
                    failure_details.extend(outcome["failure_details"])
        finally:
            cpu_window_end = time.monotonic()
            if cpu_start_sec is not None:
                cpu_end_sec, _ = _aggregate_cpu_time_sec(microvms)
            rss_samples = rss_sampler.stop()

        return _build_concurrency_result(
            config=config,
            concurrency=concurrency,
            setup_duration_sec=time.monotonic() - setup_start,
            samples=samples,
            rss_samples=rss_samples,
            cpu_utilization=_build_cpu_utilization(
                cpu_start_sec=cpu_start_sec,
                cpu_end_sec=cpu_end_sec,
                start_monotonic=cpu_window_start,
                end_monotonic=cpu_window_end,
                concurrency=concurrency,
            ),
            failure_counts=dict(failure_counts),
            failure_details=failure_details,
        )
    finally:
        _cleanup_microvms(microvm_factory, preserve_pool=config.pool_dir is not None)


def _build_concurrency_result(
    *,
    config,
    concurrency,
    setup_duration_sec,
    samples,
    rss_samples,
    cpu_utilization,
    failure_counts,
    failure_details,
):
    completed_jobs = len(samples)
    throughput = completed_jobs / config.duration_sec if config.duration_sec else 0.0
    failure_counts = _empty_failure_counts() | failure_counts
    total_failures = sum(failure_counts.values())
    failure_rate = total_failures / max(completed_jobs + total_failures, 1)

    rss_values = [sample["rss_kib"] for sample in rss_samples]
    aggregate_rss = {
        "sample_count": len(rss_values),
        "min_kib": min(rss_values, default=0),
        "max_kib": max(rss_values, default=0),
        "mean_kib": statistics.mean(rss_values) if rss_values else 0,
        "samples": rss_samples,
    }
    rss_gib = aggregate_rss["max_kib"] / (1024 * 1024)
    memory_efficiency = throughput / rss_gib if rss_gib > 0 else 0.0

    result = {
        "mode": config.mode,
        "concurrency": concurrency,
        "duration_sec": config.duration_sec,
        "setup_duration_sec": setup_duration_sec,
        "completed_jobs": completed_jobs,
        "completed_jobs_per_sec": throughput,
        "aggregate_cpu_utilization": cpu_utilization,
        "job_latency_ms": _latency_summary(
            [sample["job_latency_ms"] for sample in samples]
        ),
        "wait_entry_latency_ms": _latency_summary(
            [sample["wait_entry_latency_ms"] for sample in samples]
        ),
        "resume_latency_ms": _latency_summary(
            [sample["resume_latency_ms"] for sample in samples]
        ),
        "running_phase_latency_ms": _latency_summary(
            [sample["running_phase_latency_ms"] for sample in samples]
        ),
        "sandbox_touch_latency_ms": _latency_summary(
            [sample["sandbox_touch_latency_ms"] for sample in samples]
        ),
        "aggregate_rss_kib": aggregate_rss,
        "memory_efficiency_jobs_per_sec_per_gib_host_rss": memory_efficiency,
        "failure_counts": failure_counts,
        "failure_rate": failure_rate,
        "failure_details": failure_details,
        "samples": samples,
        "usable": False,
        "unusable_reasons": [],
    }
    _assert_concurrency_result_schema(result)
    return result


def _assert_concurrency_result_schema(result):
    missing = REQUIRED_CONCURRENCY_RESULT_FIELDS - set(result)
    if missing:
        raise AssertionError(f"concurrency result missing fields: {sorted(missing)}")
    for kind in FAILURE_KINDS:
        if kind not in result["failure_counts"]:
            raise AssertionError(f"failure_counts missing {kind}")


def _mark_capacity(config, results):
    previous = None
    for result in results:
        reasons = []
        if result["completed_jobs"] == 0:
            reasons.append("no_completed_jobs")
        if result["failure_rate"] > config.failure_threshold:
            reasons.append("failure_rate")
        if result["job_latency_ms"]["p95"] > config.latency_slo_ms:
            reasons.append("latency_slo")
        if (
            previous is not None
            and previous["completed_jobs_per_sec"] > 0
            and result["completed_jobs_per_sec"]
            < previous["completed_jobs_per_sec"]
            * (1.0 - config.throughput_regression_threshold)
        ):
            reasons.append("throughput_regression")
        result["usable"] = not reasons
        result["unusable_reasons"] = reasons
        previous = result


def _load_comparison_summary(path):
    if path is None:
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _throughput_delta_agent_over_baseline(current_summary, comparison_summary):
    if comparison_summary is None:
        return None

    summaries = {
        current_summary["mode"]: current_summary,
        comparison_summary["mode"]: comparison_summary,
    }
    agent = summaries.get("agent")
    baseline = summaries.get("baseline")
    if not agent or not baseline:
        return None

    baseline_throughput = baseline["max_completed_jobs_per_sec"]
    if baseline_throughput <= 0:
        return None
    return agent["max_completed_jobs_per_sec"] / baseline_throughput


def _summarize_results(config, sequence_path, results, comparison_summary=None):
    _mark_capacity(config, results)
    usable_results = [result for result in results if result["usable"]]
    max_throughput_result = max(
        usable_results,
        key=lambda result: result["completed_jobs_per_sec"],
        default=None,
    )
    best_concurrency_result = max(
        usable_results,
        key=lambda result: result["concurrency"],
        default=None,
    )

    summary = {
        "mode": config.mode,
        "sequence_file": str(sequence_path),
        "sweep": list(config.sweep),
        "running_ratio": config.running_ratio,
        "duration_sec": config.duration_sec,
        "latency_slo_ms": config.latency_slo_ms,
        "failure_threshold": config.failure_threshold,
        "throughput_regression_threshold": config.throughput_regression_threshold,
        "best_usable_concurrency": (
            best_concurrency_result["concurrency"] if best_concurrency_result else None
        ),
        "max_completed_jobs_per_sec": (
            max_throughput_result["completed_jobs_per_sec"]
            if max_throughput_result
            else 0.0
        ),
        "max_throughput_concurrency": (
            max_throughput_result["concurrency"] if max_throughput_result else None
        ),
        "memory_efficiency_jobs_per_sec_per_gib_host_rss": (
            max_throughput_result[
                "memory_efficiency_jobs_per_sec_per_gib_host_rss"
            ]
            if max_throughput_result
            else 0.0
        ),
        "throughput_delta_agent_over_baseline": None,
        "results": results,
    }
    summary["throughput_delta_agent_over_baseline"] = (
        _throughput_delta_agent_over_baseline(summary, comparison_summary)
    )
    return summary


@pytest.mark.nonci
@pytest.mark.timeout(7200)
@pin_guest_kernel(GUEST_KERNELS_6_1)
def test_agent_throughput(
    microvm_factory,
    guest_kernel,
    rootfs,
    pci_enabled,
    metrics,
    results_dir,
):
    """Sweep VM concurrency and report completed synthetic agent jobs/sec."""

    config = _parse_config()
    _ensure_mode_preconditions(config.mode)
    sequence, sequence_path = _load_or_generate_sequence(config, results_dir)

    metrics.set_dimensions(
        {
            "performance_test": "test_agent_throughput",
            "mode": config.mode,
            "running_ratio": str(config.running_ratio),
            "duration_sec": str(config.duration_sec),
        }
    )

    results = []
    for concurrency in config.sweep:
        result = _run_concurrency_point(
            microvm_factory=microvm_factory,
            guest_kernel=guest_kernel,
            rootfs=rootfs,
            pci_enabled=pci_enabled,
            config=config,
            sequence=sequence,
            concurrency=concurrency,
        )
        results.append(result)

    comparison_summary = _load_comparison_summary(config.comparison_summary_file)
    summary = _summarize_results(config, sequence_path, results, comparison_summary)
    for result in results:
        _write_json(
            results_dir
            / f"agent_throughput_{config.mode}_n{result['concurrency']}.json",
            result,
        )
    _write_json(results_dir / f"agent_throughput_summary_{config.mode}.json", summary)

    metrics.put_metric(
        "best_usable_concurrency",
        summary["best_usable_concurrency"] or 0,
        "Count",
    )
    metrics.put_metric(
        "max_completed_jobs_per_sec",
        summary["max_completed_jobs_per_sec"],
        "Count/Second",
    )
    metrics.put_metric(
        "memory_efficiency",
        summary["memory_efficiency_jobs_per_sec_per_gib_host_rss"],
        "Count/Second",
    )


def test_agent_throughput_sequence_generation_is_deterministic():
    first = _generate_sequence(
        seed=1234,
        max_concurrency=8,
        duration_sec=5.0,
        running_ratio=0.30,
        run_target_sec=0.20,
    )
    second = _generate_sequence(
        seed=1234,
        max_concurrency=8,
        duration_sec=5.0,
        running_ratio=0.30,
        run_target_sec=0.20,
    )
    assert first == second


def test_agent_throughput_sequence_running_ratio_is_close_to_target():
    sequence = _generate_sequence(
        seed=5678,
        max_concurrency=64,
        duration_sec=30.0,
        running_ratio=0.30,
        run_target_sec=0.20,
    )
    assert math.isclose(_sequence_running_ratio(sequence), 0.30, abs_tol=0.001)


def test_agent_throughput_sequence_wait_entries_are_staggered():
    sequence = _generate_sequence(
        seed=9012,
        max_concurrency=32,
        duration_sec=10.0,
        running_ratio=0.30,
        run_target_sec=0.20,
    )
    initial_delays = [agent["initial_delay_sec"] for agent in sequence["agents"]]
    assert len(set(initial_delays)) == len(initial_delays)
    assert max(initial_delays) - min(initial_delays) > 0.3


class _RecordingVm:
    def __init__(self):
        self.calls = []
        self.api = self
        self.vm = self

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))


class _RejectingVm:
    @property
    def api(self):
        raise AssertionError("baseline mode must not use /agent/runtime")


class _FakeSshResult:
    def __init__(self, stdout):
        self.stdout = stdout


class _RecordingSsh:
    def __init__(self):
        self.commands = []

    def check_output(self, command, timeout=None):
        self.commands.append((command, timeout))
        if "cat /tmp/fast_page_fault_helper.out" in command:
            return _FakeSshResult("2000000\n")
        return _FakeSshResult("")


class _MemoryTouchVm:
    def __init__(self):
        self.ssh = _RecordingSsh()


def test_agent_throughput_baseline_mode_never_calls_agent_runtime():
    vm = _RejectingVm()
    assert _enter_wait(vm, "baseline") == 0.0
    assert _exit_wait(vm, "baseline") == 0.0


def test_agent_throughput_agent_mode_calls_agent_runtime():
    vm = _RecordingVm()
    _enter_wait(vm, "agent")
    _exit_wait(vm, "agent")
    assert vm.calls == [
        ("PATCH", "/agent/runtime", {"state": "LlmWaiting", "pause_on_wait": True}),
        ("PATCH", "/agent/runtime", {"state": "Running"}),
    ]


def test_agent_throughput_memory_touch_waits_for_numeric_helper_output():
    vm = _MemoryTouchVm()

    sandbox_touch_ms, sandbox_total_ms = _trigger_sandbox_memory_touch(
        vm, "123", timeout_sec=1.0
    )

    wait_command = vm.ssh.commands[-1][0]
    assert "grep -Eq '^[0-9]+$' /tmp/fast_page_fault_helper.out" in wait_command
    assert sandbox_touch_ms == 2.0
    assert sandbox_total_ms >= 0.0


def test_agent_throughput_agent_mode_skips_without_swap(monkeypatch):
    monkeypatch.setitem(globals(), "_host_swap_enabled", lambda: False)
    with pytest.raises(pytest.skip.Exception):
        _ensure_mode_preconditions("agent")


def test_agent_throughput_pool_manifest_loads(tmp_path):
    manifest = {
        "schema": "firecracker-agent-throughput-pool-v1",
        "chroot_base": "/srv/agent-throughput-jailer",
        "resources": [
            {"microvm_id": "agtpool-000000", "netns_name": "agtpool-ns-000000"},
            {"microvm_id": "agtpool-000001", "netns_name": "agtpool-ns-000001"},
        ],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    pool = ThroughputPool.load(tmp_path, 2)

    assert pool.chroot_base == Path("/srv/agent-throughput-jailer")
    assert pool.microvm_ids == ("agtpool-000000", "agtpool-000001")
    assert pool.netns_names == ("agtpool-ns-000000", "agtpool-ns-000001")


def test_agent_throughput_result_schema_contains_required_fields():
    config = ThroughputConfig(
        mode="baseline",
        seed=1,
        sweep=(2,),
        running_ratio=0.30,
        duration_sec=1.0,
        run_target_sec=0.20,
        guest_mem_mib=128,
        job_timeout_sec=1.0,
        latency_slo_ms=1000.0,
        failure_threshold=0.0,
        throughput_regression_threshold=0.10,
        sequence_file=None,
        comparison_summary_file=None,
        pool_dir=None,
        rss_sample_interval_sec=1.0,
    )
    result = _build_concurrency_result(
        config=config,
        concurrency=2,
        setup_duration_sec=0.1,
        samples=[
            {
                "job_latency_ms": 100.0,
                "wait_entry_latency_ms": 0.0,
                "resume_latency_ms": 0.0,
                "running_phase_latency_ms": 25.0,
                "sandbox_touch_latency_ms": 20.0,
            }
        ],
        rss_samples=[{"timestamp_monotonic_sec": 1.0, "rss_kib": 1024, "live_vms": 2}],
        cpu_utilization=_build_cpu_utilization(
            cpu_start_sec=1.0,
            cpu_end_sec=1.5,
            start_monotonic=10.0,
            end_monotonic=11.0,
            concurrency=2,
        ),
        failure_counts={},
        failure_details=[],
    )
    _assert_concurrency_result_schema(result)
    assert result["completed_jobs_per_sec"] == 1.0
    assert result["aggregate_rss_kib"]["max_kib"] == 1024
    assert result["aggregate_cpu_utilization"]["average_one_cpu_percent"] == 50.0


def test_agent_throughput_zero_completed_jobs_is_not_usable():
    config = ThroughputConfig(
        mode="agent",
        seed=1,
        sweep=(128,),
        running_ratio=0.30,
        duration_sec=5.0,
        run_target_sec=0.20,
        guest_mem_mib=512,
        job_timeout_sec=30.0,
        latency_slo_ms=10000.0,
        failure_threshold=0.0,
        throughput_regression_threshold=0.10,
        sequence_file=None,
        comparison_summary_file=None,
        pool_dir=None,
        rss_sample_interval_sec=1.0,
    )
    result = _build_concurrency_result(
        config=config,
        concurrency=128,
        setup_duration_sec=0.1,
        samples=[],
        rss_samples=[],
        cpu_utilization=_empty_cpu_utilization(),
        failure_counts={},
        failure_details=[],
    )

    _mark_capacity(config, [result])

    assert not result["usable"]
    assert result["unusable_reasons"] == ["no_completed_jobs"]
