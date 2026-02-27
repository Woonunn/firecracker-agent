# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Parallel microVM test for /agent/runtime LLM wait transitions."""

import time
from concurrent.futures import ThreadPoolExecutor
from subprocess import TimeoutExpired

import pytest

from framework.guest_stats import MeminfoGuest
from framework.utils import get_stable_rss_mem


def _patch_agent_runtime(microvm, **kwargs):
    return microvm.api.vm.request("PATCH", "/agent/runtime", **kwargs)


def _host_swap_enabled():
    try:
        with open("/proc/swaps", encoding="utf-8") as file:
            return len([line for line in file.read().splitlines() if line.strip()]) > 1
    except OSError:
        return False


def _start_microvm(microvm):
    microvm.time_api_requests = False
    microvm.spawn()
    microvm.basic_config(vcpu_count=1, mem_size_mib=256)
    microvm.add_net_iface()
    microvm.start()


def _dirty_guest_memory(microvm, amount_mib):
    # Keep the helper behavior consistent with existing balloon tests.
    try:
        microvm.ssh.run(f"/usr/local/bin/fillmem {amount_mib}", timeout=1.0)
    except TimeoutExpired:
        pass
    time.sleep(2)


def test_agent_runtime_parallel_llm_wait_reclaim_and_restore(
    microvm_factory, guest_kernel, rootfs, pci_enabled
):
    """
    Run two microVMs in parallel. Put vm1 into LLM wait mode, verify memory reclaim,
    then exit LLM wait and verify memory is returned to vm1 while vm2 keeps running.
    """
    if not _host_swap_enabled():
        pytest.skip("Host swap is disabled; MADV_PAGEOUT reclaim is unavailable.")

    vm1 = microvm_factory.build(guest_kernel, rootfs, pci=pci_enabled)
    vm2 = microvm_factory.build(guest_kernel, rootfs, pci=pci_enabled)

    with ThreadPoolExecutor(max_workers=2) as tpe:
        for future in (
            tpe.submit(_start_microvm, vm1),
            tpe.submit(_start_microvm, vm2),
        ):
            future.result()

    # Keep vm2 active while vm1 enters/exits LLM wait.
    vm2.ssh.check_output("true")

    _dirty_guest_memory(vm1, amount_mib=64)
    rss_before_wait = get_stable_rss_mem(vm1)
    mem_available_before_wait = MeminfoGuest(vm1).get().mem_available.kib()

    _patch_agent_runtime(
        vm1,
        state="LlmWaiting",
        pause_on_wait=True,
    )
    time.sleep(2)

    rss_during_wait = get_stable_rss_mem(vm1)
    mem_available_during_wait = MeminfoGuest(vm1).get().mem_available.kib()
    assert rss_during_wait < rss_before_wait
    assert mem_available_during_wait < mem_available_before_wait

    # vm2 should remain healthy while vm1 is waiting for LLM response.
    vm2.ssh.check_output("true")

    _patch_agent_runtime(vm1, state="Running")
    time.sleep(2)

    mem_available_after_exit = MeminfoGuest(vm1).get().mem_available.kib()
    assert mem_available_after_exit > mem_available_during_wait

    # Both VMs are still responsive after the transition cycle.
    vm1.ssh.check_output("true")
    vm2.ssh.check_output("true")
