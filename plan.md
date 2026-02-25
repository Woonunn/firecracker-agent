## 为 agent 基于 firecracker 定制化一个 vmm

### 背景

agent 运行时涉及到 LLM 的调用，当使用一个 vm 执行 agent 时，若这个 agent 进入到 LLM 调用的阶段，则这个 vm 所占用的内存会空占资源。理想方法是：vmm 可以监测哪些 vm 处于 LLM 状态，得知这些 vm 使用了哪些内存，将内存设置为 inactive 并卸载到磁盘；IO 调用（LLM 或网络请求）由 vmm 代管，收到响应后再唤醒内存并交还给 vm 继续执行。

---

### MVP 目标（最小可落地）

先实现一个“可运行版本”，满足：

1. guest/agent 能显式通知 Firecracker 进入/退出 `LLM waiting` 状态；
2. 进入等待态后，触发主机侧可用的内存回收路径（balloon/hinting）；
3. 退出等待态后，恢复运行态；
4. IO 托管先不内嵌到 Firecracker 内核逻辑，先通过现有 `vsock + 宿主代理进程` 完成。

> 注意：MVP 不做“页级精确识别 LLM 使用内存”，也不做“Firecracker 内部直接发起 LLM HTTP 请求”。

---

### MVP 范围与边界

- **包含**：API、VMM action、运行态状态机、balloon/hinting 触发、基础测试；
- **不包含**：
  - VMM 内部完整网络协议栈代管；
  - 页级工作集精确跟踪与按页冷热管理；
  - 多 VM 全局调度（应由外部 controller 负责）。

---

### MVP 最小改动清单（按源码模块）

#### 1) API 定义与路由

- 在 `src/firecracker/swagger/firecracker.yaml` 新增运行态接口（建议：`PATCH /agent/runtime`）；
- 新增请求解析文件：`src/firecracker/src/api_server/request/agent_runtime.rs`；
- 在 `src/firecracker/src/api_server/request/mod.rs` 注册模块；
- 在 `src/firecracker/src/api_server/parsed_request.rs` 增加路由分发。

建议请求体（示例）：

```json
{
  "state": "LlmWaiting",
  "target_balloon_mib": 512,
  "acknowledge_on_stop": true
}
```

#### 2) VMM 动作与执行入口

- 在 `src/vmm/src/rpc_interface.rs` 新增：
  - `VmmAction::EnterLlmWait(EnterLlmWaitConfig)`
  - `VmmAction::ExitLlmWait`
- 在 RuntimeApiController 中接入对应 match 分支；
- 返回值先使用 `VmmData::Empty` 即可。

#### 3) VMM 运行态状态

- 在 `src/vmm/src/lib.rs` 的 `Vmm` 结构中新增轻量状态（建议）：
  - `in_llm_wait: bool`
  - `prev_balloon_mib: Option<u32>`

用于避免重复进入/退出、并支持恢复到进入前配置。

#### 4) 进入等待态逻辑（MVP）

复用现有能力，不新增复杂内存机制：

- 调用 `update_balloon_config()` 将 balloon 增大到目标值；
- 调用 `start_balloon_hinting()` 启动 free page hinting；
- 记录 `in_llm_wait=true` 和 `prev_balloon_mib`。

> 默认先不自动 `pause_vm()`，避免影响 guest 等待外部响应路径；若后续验证需要可加开关。

#### 5) 退出等待态逻辑（MVP）

- 调用 `stop_balloon_hinting()`；
- balloon 恢复到 `prev_balloon_mib`（没有则回到 0）；
- 更新 `in_llm_wait=false`。

#### 6) 前置校验

- 若未配置 balloon 设备，`EnterLlmWait` 返回可解释错误（HTTP 400）；
- 若重复进入/退出，保持幂等（重复请求不报错，直接返回成功）。

#### 7) IO 托管通道（MVP 方案）

- 复用现有 `vsock` 通道与宿主代理，不修改 net/tap 数据路径；
- guest agent 在 LLM 调用阶段通过 vsock 发起请求给宿主代理；
- 宿主代理完成真实网络请求并回传结果；
- guest 收到结果后调用 `ExitLlmWait`。

---

### 测试最小闭环

在 `tests/integration_tests/functional/test_api.py` 增加 1 条主流程用例：

1. 启动带 balloon 的 microVM；
2. 调 `PATCH /agent/runtime` 进入等待态；
3. 验证 API 成功、balloon 配置/统计有变化；
4. 调 `PATCH /agent/runtime` 退出等待态；
5. 验证 VM 仍可继续执行（如 ssh 命令）。

另补 2 条负例：

- 未配置 balloon 时进入等待态失败；
- 重复进入/退出请求幂等。

---

### 里程碑（MVP）

- **M1：API 与 Action 打通**（路由->VmmAction->空实现）
- **M2：等待态内存回收联动**（balloon/hinting）
- **M3：vsock 代理联调 + 用例通过**

---

### 下一阶段（MVP 后）

1. 引入页级工作集跟踪（更精细内存回收）；
2. 支持策略化触发（按时延、内存水位、请求类型）；
3. 设计多 VM 外部调度器与统一观测指标。

---

### 按提交顺序的实施任务清单（建议）

> 目标：每个提交都保持“可编译、可回归、可验证”。

#### Commit 1：新增 API 协议定义（不接逻辑）

**改动内容**

- 在 `src/firecracker/swagger/firecracker.yaml` 增加 `PATCH /agent/runtime`；
- 新增请求/响应 schema（最小字段）：
  - `state`：`LlmWaiting | Running`
  - `target_balloon_mib`：可选
  - `acknowledge_on_stop`：可选

**验收标准**

- Swagger 静态校验通过；
- 仅文档变化，无代码行为变更。

---

#### Commit 2：新增 API 解析模块与路由（先返回 NotSupported）

**改动内容**

- 新增 `src/firecracker/src/api_server/request/agent_runtime.rs`：
  - 解析 body -> `VmmAction::EnterLlmWait(...)` / `VmmAction::ExitLlmWait`；
- 修改 `src/firecracker/src/api_server/request/mod.rs` 注册模块；
- 修改 `src/firecracker/src/api_server/parsed_request.rs` 增加路由分发；
- 在 `src/vmm/src/rpc_interface.rs` 先加 action 枚举与 `NotSupported` 返回。

**验收标准**

- 新接口能被识别，不再是 invalid path；
- 返回“暂不支持”类错误（预期行为）。

---

#### Commit 3：接入 RuntimeApiController 基础流程（空实现成功）

**改动内容**

- 在 `src/vmm/src/rpc_interface.rs` Runtime 分支接入：
  - `EnterLlmWait` -> 调用 VMM 新方法
  - `ExitLlmWait` -> 调用 VMM 新方法
- 在 `src/vmm/src/lib.rs` 增加空方法：
  - `enter_llm_wait(...) -> Result<(), VmmError>`
  - `exit_llm_wait() -> Result<(), VmmError>`

**验收标准**

- API 调用成功返回 204（先不实际回收内存）；
- 不影响现有 `/vm`、`/snapshot/*`、`/balloon*`。

---

#### Commit 4：VMM 状态机与幂等保护

**改动内容**

- 在 `src/vmm/src/lib.rs` 的 `Vmm` 结构新增：
  - `in_llm_wait: bool`
  - `prev_balloon_mib: Option<u32>`
- 在 `enter_llm_wait/exit_llm_wait` 增加幂等逻辑：
  - 重复 enter：直接成功；
  - 重复 exit：直接成功；
- 增加必要日志与错误信息。

**验收标准**

- 连续调用 enter/enter 与 exit/exit 均不报错；
- 状态切换符合预期。

---

#### Commit 5：接入 balloon/hinting（MVP 内存回收主路径）

**改动内容**

- `enter_llm_wait`：
  - 读取并记录当前 balloon 目标值；
  - `update_balloon_config(target_balloon_mib)`；
  - `start_balloon_hinting(...)`；
- `exit_llm_wait`：
  - `stop_balloon_hinting()`；
  - 恢复 balloon 到 `prev_balloon_mib`（无则 0）；
- 未配置 balloon 时返回可解释错误（HTTP 400）。

**验收标准**

- 进入等待态后 balloon 可观测变化；
- 退出后回退到进入前值；
- 无 balloon 配置时接口失败且报错清晰。

---

#### Commit 6：API 单测（解析与路由）

**改动内容**

- 在 `src/firecracker/src/api_server/request/agent_runtime.rs` 增加：
  - 正常解析、缺失字段、非法字段测试；
- 在 `src/firecracker/src/api_server/parsed_request.rs` 相关测试中补新路由覆盖。

**验收标准**

- 新增测试全部通过；
- 不影响现有 actions/balloon/snapshot 解析测试。

---

#### Commit 7：功能测试（最小闭环）

**改动内容**

- 在 `tests/integration_tests/functional/test_api.py` 增加 3 条：
  1. `test_api_agent_runtime_happy_path`（enter -> exit）
  2. `test_api_agent_runtime_without_balloon`
  3. `test_api_agent_runtime_idempotent`

**验收标准**

- happy path 可通过并验证 VM 继续工作；
- 两条负例行为稳定。

---

#### Commit 8：文档补充与落地说明

**改动内容**

- 在 `docs/` 下新增简要说明（建议 `docs/agent-runtime.md`）：
  - 接口定义
  - 时序图（agent -> firecracker -> balloon/hinting -> agent）
  - MVP 限制与后续计划
- 在 `plan.md` 回填“实现状态/已完成提交”。

**验收标准**

- 研发和测试可按文档复现；
- 新功能边界清晰，不与后续阶段混淆。

---

### 提交执行建议

- 每个 commit 后都执行最小验证（至少相关单测）；
- 提交信息建议前缀统一，例如：
  - `mvp(agent-runtime): ...`
  - `test(agent-runtime): ...`
  - `docs(agent-runtime): ...`
