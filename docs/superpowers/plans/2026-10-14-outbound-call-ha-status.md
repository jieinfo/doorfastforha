# HA 主动呼叫状态修复实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 subagent-driven-development（推荐）或 executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让 HA 只在 Doorfast 真实主会话进入 `talking` 且身份匹配时显示“通话中”，并正确呈现准备、呼叫、失败、挂断和清理阶段。

**架构：** HA 不自行猜测设备状态；沿用现有 coordinator/status snapshot，把新的 station-call operation 作为主动操作事实，把主 `call` 作为真实通话事实，把 media session 作为视频事实。服务按钮只发送 station/runtime/operation 条件，音频继续要求真实 talking identity。

**技术栈：** Home Assistant Python integration、aiohttp client、现有 MonitorCoordinator、station sensors、pytest/unittest stubs、Node frontend tests。

**规格：** `/Users/shenwenjie/Documents/PVE/doorfast/.worktrees/preview-startup-state/docs/superpowers/specs/2026-10-14-outbound-call-ownership-repair-design.md`（实现者应同时阅读该文件和 Doorfast plan）；现有 `custom_components/doorfast/session_diagnostics.py`、`custom_components/doorfast/station_status.py`、`custom_components/doorfast/monitor.py`、`custom_components/doorfast/client.py`。

## 全局约束

- 不改变来电自动解锁、开锁按钮、preview retry、音频算法、HTTPS/麦克风安全限制和 dashboard。
- `purpose=call`、`ready`、`source_live` 或 HTTP queued 单独不能显示 `in_call`。
- 只接受经过白名单校验的 operation stage/error/timer/identity；不暴露原始错误正文、token、SDP 或音频。
- 保持旧 Doorfast 兼容：没有 station-call 对象时仍可根据真实 `call.session=talking` 展示来电通话。
- 先写失败测试并运行；不把 fake transport 的返回值本身当作行为证明。
- HACS 发布、实机安装和 Actions 状态由用户控制，不在本计划中自动执行。

## 文件职责

- 修改：`custom_components/doorfast/client.py`：发送/校验 station call 与 operation 条件，兼容旧响应。
- 修改：`custom_components/doorfast/monitor.py`：合并权威 call/operation 状态，处理 calling/talking/ending/failed。
- 修改：`custom_components/doorfast/station_status.py`、`custom_components/doorfast/session_diagnostics.py`：纯状态推导和白名单字段。
- 修改：`custom_components/doorfast/translations/en.json`、`custom_components/doorfast/translations/zh-Hans.json`、`custom_components/doorfast/button.py`：状态和错误文案及按钮状态。
- 测试：`tests/test_station_status_entities.py`、`tests/test_session_diagnostics.py`、`tests/test_monitor.py`、`tests/test_client.py`、`tests/test_station_call_entities.py`、`tests/test_audio_client.py`。

---

### 任务 1：锁定 station-call 状态推导矩阵

**文件：**
- 测试：`tests/test_session_diagnostics.py`、`tests/test_station_status_entities.py`
- 修改：`custom_components/doorfast/session_diagnostics.py`、`custom_components/doorfast/station_status.py`

- [ ] **步骤 1：编写失败测试**

  添加真实纯函数输入矩阵：`purpose=call + call.session=idle` 必须是 `calling`/`requesting` 而不是 `in_call`；匹配 station/control generation 的 `talking` 才是 `in_call`；另一 station talking、失配 generation、多个同 station media records、malformed operation 都不能误报。覆盖 `preparing/calling/talking/ending/failed/ended`、timer/remaining/error 白名单。

- [ ] **步骤 2：运行测试确认是预期失败**

  运行：`python -m unittest tests.test_session_diagnostics tests.test_station_status_entities -v`

  预期：当前仅检查 media `purpose=call` 的测试失败，且失败信息明确指出错误 phase。

- [ ] **步骤 3：实现最小状态推导**

  先解析/校验 station-call operation，再解析真实 call owner；只有同 station、正数 generation、`session=talking` 且 operation identity 不冲突时返回 `in_call`。准备阶段返回 calling，终态保留 error/last generation 但不覆盖仍存在的 preview phase。

- [ ] **步骤 4：运行测试确认通过**

  运行同一命令和既有 station status focused tests；预期状态矩阵全部通过。

- [ ] **步骤 5：Commit**

  `git add custom_components/doorfast/session_diagnostics.py custom_components/doorfast/station_status.py tests/test_session_diagnostics.py tests/test_station_status_entities.py && git commit -m "fix: classify station calls from authoritative state"`

### 任务 2：传递 operation_id 并修正服务/协调器生命周期

**文件：**
- 测试：`tests/test_client.py`、`tests/test_monitor.py`、`tests/test_station_call_entities.py`
- 修改：`custom_components/doorfast/client.py`、`custom_components/doorfast/monitor.py`、`custom_components/doorfast/__init__.py`

- [ ] **步骤 1：编写失败测试**

  断言 call station 记录 queued response 中的 station、operation_id 和 generation；station hangup 携带同一 operation_id；异步状态从 calling 到 talking 再到 ending/ended 时不被旧 revision 回退；重复 call 不制造新的本地 lease 或再次请求。

- [ ] **步骤 2：运行测试确认是预期失败**

  运行：`python -m unittest tests.test_client tests.test_monitor tests.test_station_call_entities -v`

  预期：client 当前只发送 station/runtime，coordinator 当前把 queued 状态直接当作返回 state，新增 operation 断言失败。

- [ ] **步骤 3：实现最小服务生命周期**

  client 对新增字段做类型/identity 白名单校验，保留旧响应兼容；MonitorCoordinator 保存 operation_id、call/media/control generation、stage/error/timer，并让 async_call 只进入 calling，async_hangup 使用匹配 operation 条件。服务处理不主动挂断或解锁，也不释放不属于该 station 的 audio listener。

- [ ] **步骤 4：运行测试确认通过**

  运行同一命令和 `python -m unittest tests.test_generation tests.test_polling -v`；预期旧服务和新 operation 行为都通过。

- [ ] **步骤 5：Commit**

  `git add custom_components/doorfast/client.py custom_components/doorfast/monitor.py custom_components/doorfast/__init__.py tests/test_client.py tests/test_monitor.py tests/test_station_call_entities.py && git commit -m "feat: track station call operations in HA"`

### 任务 3：补齐翻译、实体状态和音频身份边界

**文件：**
- 测试：`tests/test_station_call_entities.py`、`tests/test_audio_client.py`、`tests/test_audio_contract.py`、`tests/test_call_labels.py`
- 修改：`custom_components/doorfast/translations/en.json`、`custom_components/doorfast/translations/zh-Hans.json`、`custom_components/doorfast/button.py`

- [ ] **步骤 1：编写失败测试**

  验证每台 station 的 Call/Hang up button 文案和状态选项包含 calling/talking/ending/failed；验证 audio identity 仍拒绝 call idle、purpose-only call、失配 control generation 和 malformed operation。

- [ ] **步骤 2：运行测试确认是预期失败**

  运行：`python -m unittest tests.test_station_call_entities tests.test_audio_client tests.test_audio_contract tests.test_call_labels -v`

  预期：新状态和文案缺失或旧 purpose-only fixture 暴露误判，测试失败。

- [ ] **步骤 3：实现最小 UI/音频边界**

  增加英文/简体中文稳定文案；实体从 coordinator snapshot 读取 operation stage；音频继续只绑定 `call.session=talking` 的 `AudioIdentity`，不因为 calling/媒体 ready 放宽。

- [ ] **步骤 4：运行测试确认通过**

  运行同一命令、`python -m unittest tests.test_audio_listener tests.test_pcm_websocket -v`。

- [ ] **步骤 5：Commit**

  `git add custom_components/doorfast/translations custom_components/doorfast/strings.json custom_components/doorfast/entities.py tests/test_station_call_entities.py tests/test_audio_client.py tests/test_audio_contract.py tests/test_call_labels.py && git commit -m "fix: align HA call labels and audio admission"`

### 任务 4：HA 完整验证与本地交付检查

**文件：**
- 测试：现有 `tests/` Python 和 `tests/js/` Node suites
- 文档记录：忽略目录 `.superpowers/sdd/2026-10-14-outbound-call-ownership-repair/`

- [ ] **步骤 1：运行完整验证**

  运行：`python -m unittest discover -s tests -p 'test_*.py'`、`node --test tests/js/*.mjs`、`python -m compileall -q custom_components tests`，再验证 translations/manifest JSON。

- [ ] **步骤 2：执行变异检查**

  临时验证测试能捕获：把 `purpose=call` 直接映射成 in_call；忽略 operation station；让旧 revision 覆盖 talking；AudioIdentity 只检查 media purpose；hangup 丢弃 operation_id。恢复源码后重跑 focused suites。

- [ ] **步骤 3：检查最终 diff 和工作树**

  运行：`git diff --check`、`git diff --stat origin/codex/integrate-webrtc-camera...HEAD`、`git status --short --branch`；保留用户现有 `.DS_Store`，不修改发布版本号或 HACS 元数据，除非后续明确要求发布。

- [ ] **步骤 4：Commit**

  `git add docs/superpowers/plans/2026-10-14-outbound-call-ha-status.md && git commit -m "docs: plan HA outbound call status repair"`

