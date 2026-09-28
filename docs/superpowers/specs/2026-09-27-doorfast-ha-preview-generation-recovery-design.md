# Doorfast HA 预览 generation 会话恢复设计

## 背景

Doorfast 的监视会话按 generation 管理，而 Home Assistant 可以同时存在多个
WebRTC 预览请求。当前协调器只保存一个全局 viewer 计数。1/2 号门口机在首帧
前偶发进入 `stopping`，会让等待中的 WebRTC 请求直接失败；重连期间旧请求、
新请求、抢占事件和异步取消还可能交错，导致旧 viewer 释放新 generation，或在
抢占后错误地重新启动预览。

## 目标

1. 每个成功获取的预览请求都绑定到它实际使用的 generation。
2. 首帧前遇到可恢复的 `stopping`/`stopped` 状态时，同一打开请求可以跟随新的
   generation；并行请求可以共同使用该 generation。
3. `monitor_preempted`、`monitor_failed`、用户关闭和配置卸载是终止信号；它们
   不能被恢复循环跨越，也不能在终止后隐式启动新 generation。
4. 旧 generation 的迟到释放不会减少新 generation 的 viewer 所有权。
5. 取消发生在 Doorfast 已接受启动之后时，仍能清理该 generation 的本地和远端
   所有权。

## 非目标

- 不修改 Doorfast 协议、门口机 UDP 接收逻辑或 go2rtc 配置。
- 不改变 Home Assistant 前端；HA 当前 WebRTC 播放器在已出画面后断流时会清理
  会话，不会自动重新协商，因此“已出画面后停帧”的自动恢复另立任务。
- 不改变 viewer grace period 的默认时长。

## 设计

### 1. Generation-owned viewer lease

`MonitorCoordinator` 不再把 release 解释为“释放当前 generation 的一个 viewer”。
获取操作返回一个不可混淆的 lease（至少包含 generation 和唯一 lease id），调用
方必须用该 lease 释放。协调器维护每个 generation 的 lease 集合；停止、抢占、
失败或 generation 替换时，只撤销属于被终止 generation 的 lease。为保持现有
调用兼容性，内部可以先以 `generation` 作为 lease 的最小字段，但 release 必须
校验 generation 与 lease 所属关系，迟到的旧 lease 只能成为 no-op。

### 2. 可恢复状态与终止 epoch

协调器为每次等待请求记录恢复 epoch。收到当前 generation 的 `stopping` 时，将
该 epoch 标记为可恢复；随后收到同一 generation 的 `stopped`/idle 快照时保留这
个事实，直到恢复请求完成或被终止。任何 `monitor_preempted`、`monitor_failed`、
`async_preempt`、`async_unload` 或调用方取消都会推进终止 epoch，并唤醒等待者。

恢复循环在启动新 generation 前、启动返回后和登记 viewer 前都检查 epoch；若已
终止则抛出取消/终止错误并清理，不得启动或登记新 viewer。并行等待者看到已经由
另一个请求创建的新 generation 时，复用该 generation，而不是再次 stop/start。

### 3. WebRTC provider 生命周期

`DoorfastWebRTCProvider` 保存每个 HA `session_id` 对应的 generation lease。建立
go2rtc WebSocket、answer 超时、producer 未就绪重试和用户关闭都通过同一个清理
路径释放对应 lease。清理必须带 expected lease/session 身份，不能误删后来复用
同一 `session_id` 的新会话。首帧前的 producer 重试只在 lease 仍然有效、协调器未
收到终止 epoch 时继续。

### 4. 事件与状态边界

- 同一 generation 的 `stopping` → `stopped` 是可恢复过渡。
- `monitor_preempted`、`monitor_failed` 和卸载是不可恢复终止。
- 不同 generation 的迟到事件按现有 generation/revision 校验丢弃。
- viewer 登记失败时只清理本 lease 对应的 generation；不影响其他 lease。

## 测试设计

在修改生产代码前，先为以下行为写失败测试：

1. 首帧前 `stopping`，单个请求恢复到新 generation。
2. `stopping` 后紧接 `monitor_stopped`，请求仍能恢复。
3. 两个并行请求共同跟随同一新 generation，viewer 数量正确。
4. `async_wait_ready` 返回后、登记 viewer 前发生 `stopping`，请求重试而不报错。
5. 恢复窗口插入 `monitor_preempted` 或 `monitor_failed`，请求终止且不启动新代。
6. 新代启动进行中取消，已接受的启动会被停止或明确标记为待清理，不留下本地
   viewer 所有权。
7. 旧 session 的迟到关闭/释放不会关闭新 session 的 generation。
8. 现有 go2rtc producer 重试、正常关闭、两台门口机隔离和配置卸载测试继续通过。

## 验收标准

- HA 集成单元测试全部通过，新增并发/取消测试稳定、无依赖真实网络时序。
- `git diff --check` 通过，生产代码没有凭猜测吞掉异常。
- 未经用户明确授权不改实机配置、不重启实机、不发布版本。
