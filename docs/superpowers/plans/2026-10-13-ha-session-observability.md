# HA 会话可观测性实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 subagent-driven-development（推荐）或 executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 将 Doorfast 新增的会话/媒体诊断字段安全归一化为每台门口机和控制器可读的状态、属性与中英文文案。

**架构：** DoorfastClient 保留原始状态并提供严格的兼容归一化；station status 和 controller sensor 只展示已知字段。事件推送和定时轮询共用同一属性映射，旧 bridge 缺字段时显示 unknown。

**技术栈：** Home Assistant Python integration、aiohttp JSON、HA enum sensors、unittest。

**规格：** `../doorfast/.worktrees/preview-startup-state/docs/superpowers/specs/2026-10-13-session-observability-design.md`

## 全局约束

- 不改变预览租约、请求频率、重试、冷却、音频或控制动作。
- 不从前端时间推断 Doorfast deadline；只使用 bridge 提供的 `remaining_ms`。
- 缺失字段和未知枚举显示 `unknown`，不把缺失当作 live、success 或 zero。
- 不把确认超时展示为会话超时；不把 `ready=true` 展示为真实视频在线。
- 不把 token、密码、SDP、地址材料或原始媒体字段复制到实体属性。

## 文件结构

- 修改：`custom_components/doorfast/client.py` —— 校验/归一化 call 与 media diagnostic 字段。
- 修改：`custom_components/doorfast/station_status.py` —— station 预览属性映射。
- 修改：`custom_components/doorfast/sensor.py` —— controller 属性映射。
- 修改：`custom_components/doorfast/events.py` —— 事件 status 归一化和旧字段兼容。
- 修改：`custom_components/doorfast/translations/en.json`, `custom_components/doorfast/translations/zh-Hans.json` —— 稳定枚举文案。
- 修改：`README.md`, `docs/ha-e2e-acceptance-runner.md` —— 用户可见字段和验收边界。
- 测试：`tests/test_station_status_entities.py`, `tests/test_events.py`, `tests/test_client.py`, `tests/test_station_call_entities.py`。

### 任务 1：客户端状态字段归一化

- [ ] **步骤 1：编写失败测试**：新旧 status fixture 分别验证 call/media 的 timer、remaining、end reason、fault；非法类型、负数、未知枚举降级为 unknown。
- [ ] **步骤 2：运行测试验证失败**：`python3 -m unittest tests.test_client -v`；预期归一化 helper 未定义而失败。
- [ ] **步骤 3：实现最小校验**：在 `client.py` 增加私有安全读取函数，保留原始未知字段之外只输出白名单；`remaining_ms` 限制为非负整数。
- [ ] **步骤 4：运行测试验证通过**：`python3 -m unittest tests.test_client -v`。
- [ ] **步骤 5：Commit**：`git add custom_components/doorfast/client.py tests/test_client.py && git commit -m "feat: normalize session diagnostic fields"`

### 任务 2：station 预览属性和 controller 属性

- [ ] **步骤 1：编写失败测试**：覆盖 requesting/awaiting/live/reconnecting/in_call/failed、first-frame timeout、preemption、source stall；断言 station 和 controller 属性名称、unknown 行为及 generation 归属。
- [ ] **步骤 2：运行测试验证失败**：`python3 -m unittest tests.test_station_status_entities tests.test_station_call_entities -v`。
- [ ] **步骤 3：实现最小映射**：在 `station_status.py` 返回 `monitor_timer`、`monitor_remaining_ms`、`monitor_end_reason`、`monitor_failure_reason`、`monitor_last_generation`；在 `sensor.py` 增加 call 对应属性。
- [ ] **步骤 4：运行测试验证通过**：`python3 -m unittest tests.test_station_status_entities tests.test_station_call_entities -v`。
- [ ] **步骤 5：Commit**：`git add custom_components/doorfast/station_status.py custom_components/doorfast/sensor.py tests/test_station_status_entities.py tests/test_station_call_entities.py && git commit -m "feat: expose station session diagnostics"`

### 任务 3：事件与翻译文案

- [ ] **步骤 1：编写失败测试**：验证事件更新和 5 秒轮询使用同一字段；旧 generation、旧 revision、重复事件不能覆盖当前诊断；英文/简体中文枚举均有稳定文案。
- [ ] **步骤 2：运行测试验证失败**：`python3 -m unittest tests.test_events -v`。
- [ ] **步骤 3：实现最小改动**：在 `events.py` 复用白名单归一化，更新两份翻译；不改变事件去重规则。
- [ ] **步骤 4：运行测试验证通过**：`python3 -m unittest tests.test_events -v`。
- [ ] **步骤 5：Commit**：`git add custom_components/doorfast/events.py custom_components/doorfast/translations/en.json custom_components/doorfast/translations/zh-Hans.json tests/test_events.py && git commit -m "feat: translate session failure diagnostics"`

### 任务 4：文档、全量验证和发布准备

- [ ] **步骤 1：更新文档**：README 和验收文档说明字段含义、确认超时与会话超时区别、旧 bridge 的 unknown 行为；不声称已完成浏览器听音或全双工现场验收。
- [ ] **步骤 2：运行全量验证**：`python3 -m unittest discover -s tests`、`node --test tests/js/*.mjs`、`python3 -m compileall -q custom_components/doorfast`、JSON 校验、`git diff --check`。
- [ ] **步骤 3：检查实体清单**：确认新增属性不产生未经请求的按钮/实体，不暴露敏感字段。
- [ ] **步骤 4：Commit**：`git add README.md docs/ha-e2e-acceptance-runner.md && git commit -m "docs: describe session observability"`

## 完成门槛

- Python、Node、compileall、JSON 和 diff 检查全部通过。
- HA 旧 bridge fixture 不回归；三台 station 的状态属性都能按 generation 正确归属。
- 通过 Actions 后再由用户更新 HACS；不自动修改用户仪表盘。
