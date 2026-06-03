# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Performance tests for agent runtime memory reclaim."""

import json
import statistics
import time
from pathlib import Path

import pytest

from framework.artifacts import GUEST_KERNELS_6_1, pin_guest_kernel
from framework.utils import get_resident_memory

NS_IN_MSEC = 1_000_000
GUEST_MEM_MIB = 512
FAST_PAGE_FAULT_HELPER_MIB = 128
ITERATIONS = 10
LLM_WAIT_SECONDS = 2.0


def _patch_agent_runtime(microvm, **kwargs):
    return microvm.api.vm.request("PATCH", "/agent/runtime", **kwargs)


def _host_swap_enabled():
    try:
        with open("/proc/swaps", encoding="utf-8") as file:
            return len([line for line in file.read().splitlines() if line.strip()]) > 1
    except OSError:
        return False


def _rss_kib(microvm):
    return get_resident_memory(microvm.ps)


def _expose_proc_swaps_to_jail(microvm):
    proc_dir = Path(microvm.chroot()) / "proc"
    proc_dir.mkdir(exist_ok=True)
    (proc_dir / "swaps").write_text(
        Path("/proc/swaps").read_text(encoding="utf-8"),
        encoding="utf-8",
    )


def _start_fast_page_fault_helper(microvm):
    microvm.ssh.check_output(
        "rm -f /tmp/fast_page_fault_helper.out; "
        "nohup /usr/local/bin/fast_page_fault_helper >/dev/null 2>&1 </dev/null &"
    )
    time.sleep(1)
    return microvm.ssh.check_output("pidof fast_page_fault_helper").stdout.strip()


def _trigger_sandbox_memory_touch(microvm, pid):
    microvm.ssh.check_output("rm -f /tmp/fast_page_fault_helper.out")
    start_ns = time.perf_counter_ns()
    microvm.ssh.check_output(f"kill -s SIGUSR1 {pid}")
    duration_ns = int(
        microvm.ssh.check_output(
            "while [ ! -f /tmp/fast_page_fault_helper.out ]; do sleep 0.01; done; "
            "cat /tmp/fast_page_fault_helper.out"
        ).stdout.strip()
    )
    total_ns = time.perf_counter_ns() - start_ns
    return duration_ns / NS_IN_MSEC, total_ns / NS_IN_MSEC


def _percentile(values, percentile):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = round((len(ordered) - 1) * percentile / 100)
    return ordered[index]


def _emit_summary_metrics(metrics, name, values, unit):
    metrics.put_metric(f"{name}_mean", statistics.mean(values), unit)
    metrics.put_metric(f"{name}_median", statistics.median(values), unit)
    metrics.put_metric(f"{name}_p95", _percentile(values, 95), unit)


@pytest.mark.nonci
@pin_guest_kernel(GUEST_KERNELS_6_1)
@pytest.mark.parametrize("scenario", ["no_pageout", "pageout"])
def test_agent_runtime_pageout_sandbox_latency(
    microvm_factory,
    guest_kernel,
    rootfs,
    pci_enabled,
    metrics,
    results_dir,
    scenario,
):
    """
    Measure sandbox execution overhead after an LLM wait phase.

    ``no_pageout`` keeps the VM running during the synthetic LLM wait and then
    triggers the sandbox memory-touch workload. ``pageout`` enters
    ``LlmWaiting`` so Firecracker pauses the VM and calls MADV_PAGEOUT, then
    resumes it immediately before the same sandbox workload. The difference in
    sandbox_touch_latency_ms estimates the extra page-in cost paid by the
    sandbox phase because guest memory was unloaded during LLM wait.
    """
    if scenario == "pageout" and not _host_swap_enabled():
        pytest.skip("Host swap is disabled; MADV_PAGEOUT reclaim is unavailable.")

    vm = microvm_factory.build(
        guest_kernel,
        rootfs,
        pci=pci_enabled,
        monitor_memory=False,
    )
    vm.spawn(emit_metrics=False)
    _expose_proc_swaps_to_jail(vm)
    vm.basic_config(vcpu_count=1, mem_size_mib=GUEST_MEM_MIB)
    vm.add_net_iface()
    vm.start()
    vm.pin_threads(0)

    metrics.set_dimensions(
        {
            "performance_test": "test_agent_runtime_pageout_sandbox_latency",
            "scenario": scenario,
            "touched_mib": str(FAST_PAGE_FAULT_HELPER_MIB),
            "llm_wait_seconds": str(LLM_WAIT_SECONDS),
            **vm.dimensions,
        }
    )

    samples = []
    for iteration in range(ITERATIONS):
        pid = _start_fast_page_fault_helper(vm)
        rss_before_wait_kib = _rss_kib(vm)

        enter_wait_ms = 0.0
        resume_ms = 0.0
        if scenario == "pageout":
            start_ns = time.perf_counter_ns()
            _patch_agent_runtime(vm, state="LlmWaiting", pause_on_wait=True)
            enter_wait_ms = (time.perf_counter_ns() - start_ns) / NS_IN_MSEC
            time.sleep(LLM_WAIT_SECONDS)
            rss_during_wait_kib = _rss_kib(vm)

            start_ns = time.perf_counter_ns()
            _patch_agent_runtime(vm, state="Running")
            resume_ms = (time.perf_counter_ns() - start_ns) / NS_IN_MSEC
        else:
            time.sleep(LLM_WAIT_SECONDS)
            rss_during_wait_kib = _rss_kib(vm)

        sandbox_touch_ms, sandbox_total_ms = _trigger_sandbox_memory_touch(vm, pid)
        rss_after_sandbox_kib = _rss_kib(vm)

        sample = {
            "iteration": iteration,
            "scenario": scenario,
            "enter_wait_ms": enter_wait_ms,
            "resume_ms": resume_ms,
            "sandbox_touch_latency_ms": sandbox_touch_ms,
            "sandbox_total_latency_ms": sandbox_total_ms,
            "sandbox_entry_total_ms": resume_ms + sandbox_total_ms,
            "rss_before_wait_kib": rss_before_wait_kib,
            "rss_during_wait_kib": rss_during_wait_kib,
            "rss_after_sandbox_kib": rss_after_sandbox_kib,
            "rss_reclaimed_kib": rss_before_wait_kib - rss_during_wait_kib,
        }
        samples.append(sample)

        for metric_name in (
            "enter_wait_ms",
            "resume_ms",
            "sandbox_touch_latency_ms",
            "sandbox_total_latency_ms",
            "sandbox_entry_total_ms",
        ):
            metrics.put_metric(metric_name, sample[metric_name], "Milliseconds")
        metrics.put_metric("rss_reclaimed", sample["rss_reclaimed_kib"], "Kilobytes")

    _emit_summary_metrics(
        metrics,
        "sandbox_touch_latency_summary",
        [sample["sandbox_touch_latency_ms"] for sample in samples],
        "Milliseconds",
    )
    _emit_summary_metrics(
        metrics,
        "sandbox_total_latency_summary",
        [sample["sandbox_total_latency_ms"] for sample in samples],
        "Milliseconds",
    )
    _emit_summary_metrics(
        metrics,
        "sandbox_entry_total_summary",
        [sample["sandbox_entry_total_ms"] for sample in samples],
        "Milliseconds",
    )

    Path(results_dir / f"{scenario}_samples.json").write_text(
        json.dumps(samples, indent=2),
        encoding="utf-8",
    )
