# Agent Experiment Experience

Notes from running the agent runtime and throughput performance experiments.

1. Run performance tests inside the Firecracker test container/devtool, not as host root.
   - `test_agent_runtime.py` and `test_agent_throughput.py` need the Firecracker integration-test environment, root privileges, networking setup, cgroups, and `/dev` access.
   - Prefer `tools/devtool` or the same privileged container path used by `tools/test.sh`; do not try to run the full integration benchmark directly on the host with `sudo pytest`.
   - Host-side pytest is only useful for fast helper/unit checks, for example:
     ```bash
     PYTHONPATH=tests pytest -q --confcutdir=tests/integration_tests/performance \
       tests/integration_tests/performance/test_agent_throughput.py -m 'not nonci'
     ```

2. Result directories may be owned by the container user.
   - Docker writes result files as the user inside the container, commonly `nobody:nogroup`.
   - After a benchmark run, chown the result directory back to the host user so later agents can edit, archive, or remove it:
     ```bash
     docker run --rm \
       --volume /home/wlh/firecracker-agent:/firecracker:z \
       --workdir /firecracker \
       public.ecr.aws/firecracker/fcuvm:v90 \
       chown -R 1003:1003 test_results/<result-dir>
     ```

3. Check the benchmark JSON, not only pytest output.
   - `test_agent_throughput.py` can pass pytest while a concurrency point is marked unusable in the JSON result.
   - Always inspect `agent_throughput_<mode>_n<N>.json` and `agent_throughput_summary_<mode>.json`.
   - Confirm at least:
     - `completed_jobs > 0`
     - `failure_rate == 0`
     - `usable == true`
     - `failure_counts.vm_start_failure == 0`

4. Use a long enough measurement window for high concurrency.
   - A 5 second window at `N=128` was too short and produced `completed_jobs: 0`.
   - For `N=128`, use at least:
     ```bash
     AGENT_THROUGHPUT_DURATION_SEC=30
     ```
   - The setup time can still be around 4-5 minutes even with a pre-created pool.

5. For 128 concurrency, use the pre-created jailer/netns pool.
   - Jailer setup and netns setup are the main startup bottlenecks.
   - Prepare the pool before running the benchmark:
     ```bash
     ./tools/prepare_agent_throughput_pool.sh \
       --count 128 \
       --pool-dir /srv/agent-throughput-pool \
       --chroot-base /srv/agent-throughput-jailer
     ```
   - Then run with:
     ```bash
     AGENT_THROUGHPUT_POOL_DIR=/srv/agent-throughput-pool
     ```

6. The helper output file can exist before it contains the duration.
   - At high concurrency, `fast_page_fault_helper.out` was sometimes created but empty.
   - Waiting only for file existence caused:
     ```text
     invalid literal for int() with base 10: ''
     ```
   - The throughput test should wait until the file contains numeric output before parsing.

7. In `test_agent_throughput.py`, Agent mode requires host swap or zram.
   - `firecracker-agent` uses the wait-state memory reclaim path, so the test skips agent mode when `/proc/swaps` has no active swap entries.
   - Baseline mode does not call `/agent/runtime` and does not need this precondition.

8. Keep the same sequence file for A/B comparisons.
    - For agent vs baseline, reuse `agent_throughput_sequence.json` with:
      ```bash
      AGENT_THROUGHPUT_SEQ_FILE=<path-to-agent_throughput_sequence.json>
      ```
    - This keeps wait/run timing identical across both binaries.

9. `Microvm.ssh_iface` has a default `lru_cache` size of 128.
   - Throughput runs above 128 VMs can evict the earliest VM SSH connections from the global cache.
   - If the benchmark later calls `microvm.ssh` again, those older VMs can fail with a blank `AssertionError()` from SSH control-master liveness, while only the newest 128 agents make progress.
   - Cache the SSH connection on each VM instance after `vm.start()` or otherwise avoid repeated `microvm.ssh` property lookups in high-concurrency workers.
