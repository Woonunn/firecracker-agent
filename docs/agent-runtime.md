# Agent Runtime memory reclaim

This document describes the `/agent/runtime` workflow used to reclaim memory
when a microVM is waiting for external LLM/network responses.

## API

`PATCH /agent/runtime`

Request body:

```json
{
  "state": "LlmWaiting",
  "pause_on_wait": true
}
```

- `state` (required): `LlmWaiting` or `Running`.
- `pause_on_wait` (optional, default `true`): pause vCPUs before reclaim.
- `target_balloon_mib` (deprecated): accepted for compatibility, ignored.
- `acknowledge_on_stop` (deprecated): accepted for compatibility, ignored.

`PUT /agent/runtime/response`

Request body:

```json
{
  "request_id": "req-1",
  "vsock_port": 11000,
  "response": "{\"ok\":true}",
  "resume_vm": true
}
```

- `request_id`: caller correlation id.
- `vsock_port`: guest vsock listening port for response delivery.
- `response`: serialized response payload.
- `resume_vm` (optional, default `true`): if set, leave `LlmWaiting` before delivery.

## Runtime behavior

### Enter `LlmWaiting`

1. Verify host swap is enabled (`/proc/swaps` has at least one entry).
2. If `pause_on_wait=true` and VM is running, pause vCPUs.
3. Iterate all guest memory regions and call:
   - `madvise(MADV_PAGEOUT)` on each region.
4. Mark VM as in LLM wait mode.

### Exit to `Running`

1. If this flow paused the VM, resume vCPUs.
2. Clear LLM wait mode state.

Both transitions are idempotent.

### Host-submitted response handoff

1. Host-side proxy receives external LLM/network response.
2. Proxy calls `PUT /agent/runtime/response`.
3. Firecracker optionally resumes the VM, then forwards:
   - `{"request_id":"...","response":"..."}` plus trailing newline
   - to host vsock socket path `<uds_path>_<vsock_port>`.
4. Guest service reads payload on the requested vsock port and continues execution.

## Operational notes

- Reclaim is best-effort; it does not guarantee instant 100% RSS eviction.
- Swap (or zram-backed swap) is required for `MADV_PAGEOUT`-based reclaim.
- If swap is unavailable, entering `LlmWaiting` fails with a clear error.
