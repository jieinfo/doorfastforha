# 任务 2 报告：首帧前状态恢复与终止 epoch

## RED

先新增了四个由 `asyncio.Event` 控制的测试：单请求 `stopping` 恢复、
`stopping -> monitor_stopped` 恢复、两个并行 acquire 跟随同一新 generation、
以及 wait 成功后登记前进入 `stopping`。运行：

```text
$ python3 -m unittest tests.test_monitor
...
ERROR: test_acquire_recovers_after_stopping_then_monitor_stopped
RuntimeError: monitor generation is no longer ready
ERROR: test_acquire_recovers_if_stopping_arrives_before_viewer_registration
RuntimeError: monitor generation is no longer ready
ERROR: test_acquire_recovers_same_generation_after_stopping
RuntimeError: monitor generation is no longer ready
ERROR: test_parallel_acquires_follow_one_recovered_generation
RuntimeError: monitor generation is no longer ready
Ran 26 tests ...
FAILED (errors=4)
```

失败原因对应缺失的 recoverable stopping generation 和并发跟随逻辑，而不是测试构造错误。

## 实现

- `custom_components/doorfast/monitor.py`
  - 增加 `_lifecycle_lock`，串行化 start/stop/viewer 生命周期 HTTP；等待远端时不持有 `_lock`。
  - 增加 `_recoverable_generation` 和 `_terminal_epoch`。
  - `stopping` 与同代 `monitor_stopped` 保留恢复事实；恢复前后核对 epoch，并行 acquire 复用已经启动的新代。
  - 终止事件、`async_preempt`、`async_stop`、unload/close 在首次 await 前推进 epoch；终止后不启动新代。
  - HTTP start 返回后若 epoch 已变化，停止本次刚接受的 generation，不采用其状态。
  - 新增 `lease_active(lease)`，校验 lease 登记、generation、epoch 和 ready 状态。
- `tests/test_monitor.py`
  - 使用 `asyncio.Event` 增加恢复、并发、登记窗口和 stop HTTP 阻塞期间终止交错测试，以及 `lease_active` 测试。

按任务边界未修改 WebRTC provider 的 retry gate；该调用方改动属于任务 4。

## GREEN

实现后运行：

```text
$ python3 -m unittest tests.test_monitor
Ran 30 tests in 0.058s
OK

$ python3 -m unittest tests.test_monitor tests.test_webrtc
Ran 43 tests in 1.129s
OK

$ python3 -m unittest discover -s tests
Ran 175 tests in 3.261s
OK

$ python3 -m compileall -q custom_components/doorfast
$ git diff --check
```

## 提交

```text
6c910df fix(monitor): recover stopping generations without crossing preemption
```

## 自审

- 本地状态读写均通过 `_lock`；start/stop/viewer HTTP 在 `_lifecycle_lock` 内串行但不持有 `_lock`。
- 终止事件入口先校验 runtime/station、generation 和 revision，再在第一次 await 前推进 epoch；持锁应用时再次检查 revision/generation。
- 迟到旧 generation lease 仍由精确 lease 值校验，不能释放新 generation。
- 新增测试覆盖恢复后两个 lease 使用同一 generation 但不同 lease id，及终止交错不启动 generation 8。

## 疑虑

- 任务 3 的 start 取消后远端清理和任务 4 的 WebRTC retry gate 尚未实现，留待后续任务；本提交只提供任务 2 的协调器边界和 `lease_active` API。
- 终止 epoch 是协程调度内的同步标记，假设所有状态入口都在同一 asyncio 事件循环中运行；当前 Home Assistant 调用模型满足这一假设。

## 第 1 轮审查修复

审查新增三个回归测试：显式 preempt 后 marker 必须清除；pending start 收到
`monitor_failed` 后不得登记返回的 generation；start HTTP 返回 queued/gen7 前已收到
同代 `stopping` 时不得覆盖该状态。

### RED

```text
$ python3 -m unittest tests.test_monitor.MonitorCoordinatorTest.test_explicit_preempt_does_not_leave_a_recoverable_generation tests.test_monitor.MonitorCoordinatorTest.test_failed_event_during_pending_start_terminates_accepted_generation tests.test_monitor.MonitorCoordinatorTest.test_pending_start_response_does_not_overwrite_stopping_status
FFE
FAILED (failures=2, errors=1)
AssertionError: 7 is not None
AssertionError: RuntimeError not raised
TimeoutError
```

### 修复

- 增加 `_start_inflight`，无本地 generation 时收到属于 pending start 的终止事件也推进 epoch；start 返回后按 epoch 停止已接受 generation 并失败。
- start response 若发现同代已进入 `stopping`，保留事件状态，交由恢复循环停止旧代并启动新代。
- 显式 terminal stop 不再把 stop response 的 `stopping` 写回 `_recoverable_generation`。

### GREEN

```text
$ python3 -m unittest tests.test_monitor.MonitorCoordinatorTest.test_explicit_preempt_does_not_leave_a_recoverable_generation tests.test_monitor.MonitorCoordinatorTest.test_failed_event_during_pending_start_terminates_accepted_generation tests.test_monitor.MonitorCoordinatorTest.test_pending_start_response_does_not_overwrite_stopping_status
Ran 3 tests in 0.004s
OK

$ python3 -m unittest tests.test_monitor
Ran 33 tests in 0.061s
OK

$ python3 -m unittest discover -s tests
Ran 178 tests in 3.265s
OK
```

第 1 轮修复仍未修改任务 3/4 的取消清理或 provider retry gate。提交修订为
`fix(monitor): recover stopping generations without crossing preemption`，最终 SHA 在提交后更新。
