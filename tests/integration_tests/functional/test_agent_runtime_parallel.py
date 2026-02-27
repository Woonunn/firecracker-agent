# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Parallel microVM test for /agent/runtime LLM wait transitions."""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest


def _patch_agent_runtime(microvm, **kwargs):
    return microvm.api.vm.request("PATCH", "/agent/runtime", **kwargs)


def _put_agent_runtime_response(microvm, **kwargs):
    return microvm.api.vm.request("PUT", "/agent/runtime/response", **kwargs)


def _host_swap_enabled():
    try:
        with open("/proc/swaps", encoding="utf-8") as file:
            return len([line for line in file.read().splitlines() if line.strip()]) > 1
    except OSError:
        return False


def _start_microvm_with_vsock(microvm, guest_cid):
    microvm.time_api_requests = False
    microvm.spawn()
    microvm.basic_config(vcpu_count=1, mem_size_mib=256)
    microvm.add_net_iface()
    microvm.api.vsock.put(vsock_id="vsock0", guest_cid=guest_cid, uds_path="/v.sock")
    microvm.start()


def test_agent_runtime_parallel_wait_handoff_and_resume(
    microvm_factory, guest_kernel, rootfs, pci_enabled
):
    """
    Simulate host-side LLM handoff:
    vm1 enters LLM waiting, host submits response via /agent/runtime/response,
    vm1 resumes and receives response over vsock, while vm2 stays healthy.
    """
    if not _host_swap_enabled():
        pytest.skip("Host swap is disabled; MADV_PAGEOUT reclaim is unavailable.")

    vm1 = microvm_factory.build(guest_kernel, rootfs, pci=pci_enabled)
    vm2 = microvm_factory.build(guest_kernel, rootfs, pci=pci_enabled)

    with ThreadPoolExecutor(max_workers=2) as tpe:
        for future in (
            tpe.submit(_start_microvm_with_vsock, vm1, 3),
            tpe.submit(_start_microvm_with_vsock, vm2, 4),
        ):
            future.result()

    vm2.ssh.check_output("true")

    vm1.ssh.check_output("rm -f /tmp/llm_response.out")
    vm1.ssh.check_output(
        'nohup sh -c "socat -u VSOCK-LISTEN:11000,reuseaddr,fork '
        'OPEN:/tmp/llm_response.out,creat,append >/tmp/llm_listener.log 2>&1" &'
    )
    time.sleep(1)

    _patch_agent_runtime(vm1, state="LlmWaiting", pause_on_wait=True)

    _put_agent_runtime_response(
        vm1,
        request_id="req-1",
        vsock_port=11000,
        response='{"content":"hello"}',
        resume_vm=True,
    )

    response_seen = False
    for _ in range(20):
        output = vm1.ssh.check_output("cat /tmp/llm_response.out 2>/dev/null || true")
        if "req-1" in output and "hello" in output:
            response_seen = True
            break
        time.sleep(0.2)

    assert response_seen
    vm1.ssh.check_output("true")
    vm2.ssh.check_output("true")
