# HA 预览 generation 恢复实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 subagent-driven-development（推荐）或 executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让 HA 首帧前的 Doorfast `stopping` 预览安全地恢复，同时保证抢占、失败、取消和旧 viewer 释放不会错误地启动或关闭新 generation。

**架构：** 每个 WebRTC 请求持有唯一的 generation lease，协调器只按 lease 所属 generation 计算 viewer。协调器以独立的生命周期锁串行化 Doorfast start/stop HTTP 调用，但事件状态更新不等待该锁；终止 epoch 阻止在抢占、失败或取消后重新启动。WebRTC provider 按 session 身份释放 lease，不把旧 session 的清理施加到新 session。

**技术栈：** Python 3、`asyncio`、Home Assistant Camera WebRTC provider、现有 `unittest.IsolatedAsyncioTestCase`。

**规格：** `docs/superpowers/specs/2026-09-27-doorfast-ha-preview-generation-recovery-design.md`

## 全局约束

- 实现前在当前独立 worktree 从 `main` 创建 `codex/ha-preview-generation-recovery`；不要重置或清理其他工作树。
- 先写测试、确认针对目标行为失败，再写最小实现；每个任务结束运行对应测试并审查 diff。
- 不修改 Doorfast 协议、门口机 UDP 接收逻辑、go2rtc 配置或 Home Assistant 前端。
- 不改变 viewer grace period 的默认 15 秒。
- `monitor_preempted`、`monitor_failed`、`async_preempt`、`async_unload`、HA 关闭是终止信号，不能被预览恢复循环越过。
- 未经用户明确授权不改实机配置、不重启实机、不发布版本。

## 文件结构

- `custom_components/doorfast/monitor.py`：唯一拥有 Doorfast monitor generation、lease、事件 epoch 和 start/stop 序列化的模块。
- `custom_components/doorfast/webrtc.py`：将 HA session id 绑定到 `MonitorLease`，在所有退出路径精确释放。
- `tests/test_monitor.py`：协调器的 generation、并行请求、事件与取消确定性时序测试。
- `tests/test_webrtc.py`：HA provider 的 session 复用、producer 重试、关闭与 lease 释放测试。
- `docs/superpowers/specs/2026-09-27-doorfast-ha-preview-generation-recovery-design.md`：已提交的设计依据，不在实现任务中改写。

---

### 任务 1：Generation lease 与精确释放

**文件：**
- 修改：`custom_components/doorfast/monitor.py:20-55,174-260`
- 修改：`custom_components/doorfast/webrtc.py:50-65,150-220,285-320`
- 测试：`tests/test_monitor.py:225-255,335-365`
- 测试：`tests/test_webrtc.py:125-145,195-480`

- [ ] **步骤 1：写失败测试。** 将现有测试的无参 release 改为传入 acquire 返回的 lease，并新增旧代迟到释放测试。关键断言：

```python
old = await coordinator.async_acquire_viewer()
assert old.generation == 7
await coordinator.async_apply_status(
    {"event": "monitor_stopped", "generation": 7, "status_revision": 1}
)
client.start_result = {"state": "publishing", "generation": 8}
new = await coordinator.async_acquire_viewer()
await coordinator.async_release_viewer(old)
assert coordinator.viewer_count == 1
assert coordinator.generation == 8
await coordinator.async_release_viewer(new)
assert coordinator.viewer_count == 0
```

- [ ] **步骤 2：确认红灯。** 运行 `python3 -m unittest tests.test_monitor`；预期在 `old.generation` 或 `async_release_viewer(old)` 处失败，而不是测试配置错误。
- [ ] **步骤 3：实现 lease 并同步调用方。** 在 `monitor.py` 定义不可变 `MonitorLease(generation: int, lease_id: int, epoch: int)`；协调器维护 `_next_lease_id` 与 `_leases: dict[int, MonitorLease]`，初始 `_terminal_epoch = 0`。`viewer_count` 改为从当前 generation 的 lease 集合计算，删除可漂移的全局 `_viewer_count` 计数。`async_acquire_viewer()` 登记并返回 lease，`async_release_viewer(lease)` 仅移除仍在 `_leases` 中且值相同的 lease；仅当前 generation 的最后一个 lease 触发 viewer false 与 grace stop。终止旧代时删除其所有 lease。同步让 `webrtc.py` 的 `_Session` 保存 lease，FakeCoordinator 使用相同接口；每个原有 release 路径传回对应 lease。此任务只迁移接口，不改变协商重试的时序。

```python
@dataclass(frozen=True)
class MonitorLease:
    generation: int
    lease_id: int
    epoch: int

# 在持有 _lock 时：
self._next_lease_id += 1
lease = MonitorLease(generation, self._next_lease_id, self._terminal_epoch)
self._leases[lease.lease_id] = lease
return lease

# webrtc.py：取得 lease 后只用它的 generation 参与现有 ready 校验。
lease = await coordinator.async_acquire_viewer()
await coordinator.async_wait_ready(
    lease.generation, timeout=MONITOR_READY_TIMEOUT
)
```

- [ ] **步骤 4：确认绿灯。** 运行 `python3 -m unittest tests.test_monitor tests.test_webrtc`；两个测试模块都必须通过。
- [ ] **步骤 5：检查与提交。** 运行 `git diff --check`、`git diff -- custom_components/doorfast/monitor.py custom_components/doorfast/webrtc.py tests/test_monitor.py tests/test_webrtc.py`，提交 `fix(monitor): bind viewers to generation leases`。

### 任务 2：首帧前状态恢复与终止 epoch

**文件：**
- 修改：`custom_components/doorfast/monitor.py:40-55,150-230,300-390`
- 测试：`tests/test_monitor.py:260-430`

- [ ] **步骤 1：写失败测试。** 用 `asyncio.Event` 控制 `start_monitor`/`stop_monitor` 返回时刻，覆盖：单请求 `stopping`、`stopping → monitor_stopped`、两个并行 acquire、wait 成功后登记前转为 `stopping`。每种情形都断言仅启动一个新 generation，两个请求拿到不同 lease id 但同为 generation 8。测试替身在返回 queued generation 7 时设置 `started` 事件，测试等待此事件后才注入状态。

```python
class GatedClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()

    async def start_monitor(self, runtime_id, station_id):
        response = await super().start_monitor(runtime_id, station_id)
        self.started.set()
        return response

first = asyncio.create_task(coordinator.async_acquire_viewer())
second = asyncio.create_task(coordinator.async_acquire_viewer())
await client.started.wait()
client.start_result = {"state": "publishing", "generation": 8}
await coordinator.async_apply_status(
    {"generation": 7, "state": "stopping", "status_revision": 1}
)
left, right = await asyncio.gather(first, second)
assert (left.generation, right.generation) == (8, 8)
assert left.lease_id != right.lease_id
assert coordinator.viewer_count == 2
```

- [ ] **步骤 2：确认红灯。** 运行 `python3 -m unittest tests.test_monitor`；预期上述时序由 `RuntimeError("monitor generation is no longer ready")` 或重复 start 导致失败。
- [ ] **步骤 3：实现状态边界。** 使用 `_lifecycle_lock` 串行化 HTTP start/stop，保留 `_lock` 只保护本地状态；不得在等待 HTTP 时持有 `_lock`。记录 `_recoverable_generation` 与 `_terminal_epoch`。同代 `stopping` 建立可恢复标记；同代 `monitor_stopped` 保留标记；抢占、失败、显式 preempt、unload 清除标记并推进 epoch。恢复循环启动新代前后都核对 epoch；发现别的请求已经启动新代时直接跟随。HTTP 返回后若 epoch 已变，停止本请求刚启动的代，不采用其状态。

```python
async with self._lock:
    request_epoch = self._terminal_epoch
    old_generation = self._generation or 0
async with self._lifecycle_lock:
    async with self._lock:
        if self._unloaded or request_epoch != self._terminal_epoch:
            raise RuntimeError("monitor request was terminated")
        follow_generation = (
            self._generation
            if self._generation is not None and self._generation > old_generation
            else None
        )
    if follow_generation is None:
        response = await self._client.start_monitor(
            self.runtime_id, self.station_id
        )
        async with self._lock:
            accepted_generation = self._response_generation(response)
            terminated = request_epoch != self._terminal_epoch
        if terminated:
            await self._client.stop_monitor(
                self.runtime_id, self.station_id, accepted_generation
            )
            raise RuntimeError("monitor request was terminated")
```

- [ ] **步骤 4：写终止交错测试并确认红灯。** 在旧代 stop HTTP 阻塞期间分别注入 `monitor_preempted`、`monitor_failed` 和 `async_preempt()`；解除阻塞后断言请求失败、没有 `start(8)`、viewer 数为零。事件入口在等待 `_lock` 前同步标记终止意图，标记前必须校验 runtime/station、generation、revision；迟到旧代事件不能终止新代。
- [ ] **步骤 5：实现终止交错保护。** `async_apply_status` 与 `async_preempt` 在第一次 `await` 前，对已验证属于当前代的终止事件推进 epoch；持 `_lock` 应用状态时重新校验 revision。恢复循环不捕获并重试终止错误。新增 `lease_active(lease)`，仅当 lease 仍登记、generation 相同、`lease.epoch == _terminal_epoch` 且 ready 时为真；WebRTC 的 producer 重试在每次尝试前检查此方法。

```python
def lease_active(self, lease: MonitorLease) -> bool:
    return (
        self._leases.get(lease.lease_id) == lease
        and self._generation == lease.generation
        and lease.epoch == self._terminal_epoch
        and self._ready
    )
```
- [ ] **步骤 6：确认绿灯与提交。** 运行 `python3 -m unittest tests.test_monitor`、`git diff --check`，审查 `monitor.py` diff，提交 `fix(monitor): recover stopping generations without crossing preemption`。

### 任务 3：取消 start 的远端清理

**文件：**
- 修改：`custom_components/doorfast/monitor.py:130-230,260-325`
- 测试：`tests/test_monitor.py:260-330`

- [ ] **步骤 1：写失败测试。** 让 fake `start_monitor` 在服务端已生成 generation 8 后等待测试事件；取消 acquire，然后放行 HTTP 返回。断言取消传播、远端收到 `("stop", "runtime-a", "gate_main", 8)`、本地没有 lease，且不会因为旧代迟到 release 停止其他 generation。

```python
acquire = asyncio.create_task(coordinator.async_acquire_viewer())
await client.start_accepted.wait()
acquire.cancel()
client.finish_start.set()
with self.assertRaises(asyncio.CancelledError):
    await acquire
self.assertIn(("stop", "runtime-a", "gate_main", 8), client.calls)
assert coordinator.viewer_count == 0
```

- [ ] **步骤 2：确认红灯。** 运行 `python3 -m unittest tests.test_monitor`；预期缺少 `stop(8)`。
- [ ] **步骤 3：实现确定性清理。** 为 start HTTP 建立 task 并使用 `asyncio.shield`，取消外层 acquire 时等待已发出的 start 得到 generation，再在 `_lifecycle_lock` 下停止该 generation；若 HTTP 本身报错，则不猜测 generation、不停止其他代，将原错误传出。更新本地状态前再次核对终止 epoch，避免取消后登记 lease。

```python
start_task = asyncio.create_task(
    self._client.start_monitor(self.runtime_id, self.station_id)
)
try:
    response = await asyncio.shield(start_task)
except asyncio.CancelledError:
    response = await asyncio.shield(start_task)
    generation = self._response_generation(response)
    await self._client.stop_monitor(self.runtime_id, self.station_id, generation)
    raise
```

- [ ] **步骤 4：确认绿灯与提交。** 运行 `python3 -m unittest tests.test_monitor`、`git diff --check`，审查取消/异常分支，提交 `fix(monitor): clean up accepted start after cancellation`。

### 任务 4：WebRTC session 复用与精确清理

**文件：**
- 修改：`custom_components/doorfast/webrtc.py:50-65,130-225,275-335`
- 测试：`tests/test_webrtc.py:125-145,195-480`

- [ ] **步骤 1：写失败测试。** fake coordinator 第一次返回 `MonitorLease(9, 1, 0)`，第二次返回 `MonitorLease(10, 2, 0)`；开一个 session、关闭后复用同一 session id，迟到的旧清理只释放 lease 1，不释放 lease 2。另覆盖初始 offer 等待期间 HA 关闭，producer 未就绪循环在终止 epoch 后退出。

```python
await self.open_offer(provider, old_ws, "gate_main", "reused")
provider.async_close_session("reused")
await asyncio.gather(*provider._hass.tasks)
await self.open_offer(provider, new_ws, "gate_main", "reused")
assert registry.monitors["gate_main"].released == [MonitorLease(9, 1, 0)]
assert provider._sessions["reused"].lease == MonitorLease(10, 2, 0)
```

- [ ] **步骤 2：确认红灯。** 运行 `python3 -m unittest tests.test_webrtc`；预期旧的无参 release 或 session 竞态导致失败。
- [ ] **步骤 3：实现 provider 生命周期收口。** 沿用任务 1 的 `_Session.lease`；`async_handle_async_webrtc_offer()` 在取得 lease 前登记 `_negotiations[session_id]`，确保关闭可取消初始 acquire；外层 `try/finally` 覆盖 acquire、wait 和 WebSocket 阶段。`_cleanup_session(session_id, expected=state)` 保持 identity 检查；所有 cleanup 都调用 `coordinator.async_release_viewer(state.lease)`，先将 `state.released = True` 以防并行清理重复释放。acquire 之后、创建 `_Session` 之前的失败由外层 finally 释放 lease；成功创建后将所有权交给 `_Session`。producer 重试循环每轮先检查 `coordinator.lease_active(lease)`，失效时退出，不再无限连接 go2rtc。

```python
state = self._sessions.get(session_id)
if state is None or (expected is not None and state is not expected):
    return
self._sessions.pop(session_id, None)
if not state.released:
    state.released = True
    await state.coordinator.async_release_viewer(state.lease)
```

- [ ] **步骤 4：确认绿灯与提交。** 运行 `python3 -m unittest tests.test_webrtc tests.test_monitor`、`git diff --check`，审查没有 double release，提交 `fix(webrtc): release the exact monitor lease`。

### 任务 5：整体回归与审查

**文件：**
- 测试：`tests/test_monitor.py`、`tests/test_webrtc.py`、`tests/test_setup_lifecycle.py`

- [ ] **步骤 1：运行完整测试。** `python3 -m unittest discover -s tests`；要求 0 failures、0 errors。
- [ ] **步骤 2：静态与 diff 检查。** `python3 -m compileall -q custom_components/doorfast`、`git diff --check`、`git status --short --branch`、`git log -5 --oneline`。
- [ ] **步骤 3：做独立代码审查。** 对比 `main...HEAD`，重点核对终止 epoch、锁顺序、取消期间远端资源清理、双重释放、旧代事件与状态 revision。任何 Critical/Important 问题用新增失败测试修复后重跑完整测试。
- [ ] **步骤 4：汇报边界。** 明确说明本计划只改善首帧前恢复，不声称已修复 answer 后停帧；没有实机授权时不部署。
