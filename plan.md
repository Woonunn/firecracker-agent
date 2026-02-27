## Agent Runtime 内存回收改造计划（madvise 版本）

### 背景

当前 `/agent/runtime` 的实现基于 balloon/hinting，只能回收 guest 释放出来的页，无法冻结并回收 guest 正在使用的 RSS。目标是在 VM 进入 LLM 等待态时，将其内存以宿主可回收形式下放，从而提升同机可并发 VM 数。

---

### 目标

1. 保留 `PATCH /agent/runtime` 入口与 `state` 语义（`LlmWaiting | Running`）。
2. 将等待态回收路径改为 `pause + madvise(MADV_PAGEOUT)`。
3. 等待态退出时恢复运行（仅恢复由该流程主动暂停的 VM）。
4. 将旧 balloon 字段保持兼容但废弃：请求可接受、行为忽略。

---

### 前置条件

- 宿主机必须启用 swap（或 zram）。
- 若未启用 swap，进入 `LlmWaiting` 直接返回可解释错误（HTTP 400）。

---

### API 设计

`PATCH /agent/runtime` 请求体：

```json
{
  "state": "LlmWaiting",
  "pause_on_wait": true,
  "target_balloon_mib": 512,
  "acknowledge_on_stop": true
}
```

字段说明：

- `state`：必填；`LlmWaiting` 进入等待态，`Running` 退出等待态。
- `pause_on_wait`：可选，默认 `true`。
- `target_balloon_mib`：**deprecated**，忽略。
- `acknowledge_on_stop`：**deprecated**，忽略。

兼容策略：

- 保留旧字段以避免调用方立即升级失败。
- 若请求中出现旧字段，记录 deprecation message 与 deprecated_api metric。

---

### VMM 行为设计

#### EnterLlmWait

1. 幂等：若已在等待态，直接成功。
2. 校验 swap：
   - 读取 `/proc/swaps`；
   - 无有效 swap 条目则返回 `AgentRuntimeSwapNotAvailable`。
3. 若 `pause_on_wait=true` 且当前 VM 为 Running：
   - 调用 `pause_vm()`；
   - 记录 `paused_by_llm_wait=true`。
4. 遍历 guest memory region，逐段执行：
   - `madvise(addr, len, MADV_PAGEOUT)`。
5. 记录日志：回收耗时、RSS 前后值、是否 pause。
6. 设置 `in_llm_wait=true`。

#### ExitLlmWait

1. 幂等：若不在等待态，直接成功。
2. 若 `paused_by_llm_wait=true` 且当前状态仍为 Paused：
   - 调用 `resume_vm()`。
3. 清理状态：`in_llm_wait=false`、`paused_by_llm_wait=false`。

---

### 数据结构与错误码变更

- `EnterLlmWaitConfig`：改为 `pause_on_wait: Option<bool>`。
- `Vmm` 新状态字段：
  - `in_llm_wait: bool`
  - `paused_by_llm_wait: bool`
- `VmmError` 新增：
  - `AgentRuntimeSwapNotAvailable`
  - `AgentRuntimeSwapCheck(io::Error)`
  - `AgentRuntimeUnsupportedAdvice`
  - `AgentRuntimeMadvise(io::Error)`
- 移除 agent runtime 对 balloon 配置的依赖与恢复逻辑。

---

### 测试计划

#### Rust 单测

1. swap 内容解析函数：有/无 swap 样本。
2. `enter_llm_wait` 在无 swap 输入时失败。
3. `enter_llm_wait` 幂等。
4. `exit_llm_wait` 幂等。
5. `pause_on_wait=false` 时不记录 `paused_by_llm_wait`。

#### API 解析测试

1. 新字段 `pause_on_wait` 解析正确。
2. 旧字段请求可通过，action 正常，且带 deprecation message。
3. 非法请求体仍返回 bad request。

#### 集成测试

1. happy path：enter/exit 后 VM 仍可执行命令。
2. idempotent：重复 enter/exit 不报错。
3. 无 swap 场景：enter 返回可解释错误（若环境有 swap 则 skip）。
4. 并行场景：vm1 回收期间 vm2 保持可用，vm1 退出后恢复可用。

---

### 里程碑

- **M1**：API/配置结构切换到 madvise 语义（含兼容字段）。
- **M2**：VMM 接入 `pause + MADV_PAGEOUT + swap check`。
- **M3**：单测与集成测试改造通过。
- **M4**：文档补齐（本计划 + `docs/agent-runtime.md`）。

---

### 风险与说明

- `MADV_PAGEOUT` 为最佳努力回收，不保证瞬时“100% 归零”。
- 若宿主禁用 swap，回收不可达成，必须失败返回。
- 旧字段兼容仅为过渡，后续版本可正式删除。
