# PerfPilot 远程 Agent 真机 Trace 采集设计

## 目标

本次改造让网页端可以把真机分析任务发送给用户账号下的远程 PerfPilot Agent。第一版只自动执行冷启动和滑动两个 Perfetto Trace 场景。Android Memory 内存循环留到下一阶段。

用户选择在线设备并上传 APK 后，平台自动完成 APK 解析、签名任务投递、Agent 采集、断点上传、SmartPerfetto 分析、可选源码关联和中文 PerfPilot 报告。网页继续提供 PerfPilot 优化报告与 SmartPerfetto 原始报告。

## 已确认的产品决策

- 第一版执行冷启动和滑动 Trace，不执行内存循环。
- 网页保留当前设备分析入口和 APK 上传交互。
- 用户不填写包名或启动 Activity；服务端用 `aapt2` 从已校验 APK 中解析。
- 用户可以选择自己账号下的源码工作区；没有源码时只生成通用优化建议。
- 一个场景失败时保留另一个场景的有效结果，并生成部分报告。
- 任务只能发送给当前团队选择的 Agent 和设备。
- 重启继续永久删除 APK、Trace、分析结果和报告，不创建备份；账号、Agent 和源码工作区继续保留。

## 方案选择

### 采用：复用现有签名设备任务协议

服务端复用 `AgentTaskService`、任务快照 JWS、租约续期、取消确认和 Agent 控制路由。Agent 复用 `CaptureTaskRunner`、ADB/Perfetto 采集、APK 校验下载和 `TaskExecutor`。

本地多用户运行时增加文件系统制品适配器，用现有租户和任务协议管理 APK 输入与 Agent 输出。适配器只暴露短期、绑定任务的本地 HTTP 地址，不把服务器文件路径返回给 Agent 或浏览器。

### 不采用：Agent 直接抓取接口

单独增加 `POST /capture` 会绕过签名快照、设备租约、取消和多用户边界。它开发更快，但会形成第二套控制协议。

### 不采用：Agent 手工上传 Trace

手工上传不能实现网页一键真机分析，也无法可靠绑定设备、APK、场景和取消状态。

## 组件边界

### APK 元数据检查器

服务端在浏览器完成 APK 上传后调用现有 `Aapt2LocalApkInspector`。检查器从已校验的私有 APK 文件解析：

- package name；
- version name 和 version code；
- launch activity；
- min SDK 和 target SDK；
- ABI 与 Native library 状态。

解析失败时分析进入稳定失败状态 `apk_metadata_invalid`，不创建 Agent 任务。Ubuntu 已安装 `/home/rivotek/Android/Sdk/build-tools/37.0.0/aapt2`；部署脚本仍通过现有 Android SDK 解析规则发现它。

### 本地设备任务仓库

新增本地运行时适配器，把设备任务定义保存到每个分析的私有 `state.json`，把短期租约保存在进程内。任务定义包含：

- `team_id`、`analysis_id`、`agent_id`、`device_id` 和设备摘要；
- APK 制品 ID、MIME、大小和 SHA-256；
- package name、launch activity 和卸载策略；
- 固定顺序的 `startup` 与 `scroll` 场景；
- 固定版本、recipe hash、时长和滑动次数。

服务器重启会删除全部分析数据，因此未完成设备任务不会恢复。Agent 再次轮询时得到 wait；网页也不显示旧任务。这与当前“每次重启清空分析数据”策略一致。

### 本地 Agent 制品服务

新增只服务本地运行时的 `AgentUploadService` 依赖实现：

- APK 输入读取绑定 `team + analysis + artifact + execution + lease`；
- Agent 输出只允许 `startup_trace`、`scroll_trace` 和 `agent_log`；
- 单个输出最大 512 MiB；
- 上传写入分析私有目录，使用临时文件、原子替换、`0600` 权限和 SHA-256 校验；
- 完成执行前重新校验每个制品的 ID、类型、大小、摘要和场景归属；
- 取消时删除未完成上传；
- 响应和错误不包含绝对路径、Agent token、设备 serial 或租约 token。

本地 HTTP 上传仍实现现有 Agent 客户端期望的 multipart 语义，但每个分片落在同一分析私有根目录。服务端只在 complete 后发布最终制品。

### 设备任务完成协调器

Agent 完成任务时，现有 `/v1/agent/tasks/{execution_id}/complete` 路由先完成以下校验：

1. Agent access token；
2. execution ID、lease version 和租约有效期；
3. 闭合 execution manifest；
4. 场景集合必须恰好为 `startup`、`scroll`；
5. 所有成功场景只引用已完成的同任务制品；
6. 设备任务、制品和分析属于同一团队。

协调器随后把设备恢复为 ready，并把本地分析推进到 SmartPerfetto 阶段。重复提交同一 manifest 幂等；不同 manifest 返回稳定冲突。

## 数据流

1. 浏览器提交当前团队、所选 `device_id`、APK 摘要和可选源码绑定。
2. API 校验会话、团队、在线设备和源码绑定，创建 device 分析及 APK 上传槽。
3. 浏览器上传并 finalize APK。
4. 服务端校验 APK 字节，调用 `aapt2`，保存公开元数据。
5. 服务端创建绑定所选 Agent/设备的任务定义并安排租约。
6. Agent 长轮询取得签名任务，验证 JWS、Agent ID、设备摘要和有效期。
7. Agent 下载并校验 APK，安装应用，依次执行 startup 和 scroll Trace。
8. 每个场景独立返回 completed 或 failed。有效 Trace 和 Agent log 经断点上传完成。
9. Agent 提交 execution manifest；服务端校验并接受。
10. 本地运行时分别把有效 startup/scroll Trace 提交给 SmartPerfetto。
11. 服务端合并两个 SmartPerfetto 结果。失败场景生成稳定 limitation，不覆盖成功场景。
12. 若用户选择源码，平台创建现有 source context 任务并等待最多 120 秒。
13. PerfPilot 进行一次中文 AI 总结，发布 AnalysisReport 1.2 和 SmartPerfetto 原始报告。

## 状态和界面

设备分析继续返回三个场景，以保持现有 1.0/1.1 响应兼容：

- `cold_start`：映射 Agent `startup`；
- `scroll`：映射 Agent `scroll`；
- `memory_cycle`：第一版固定显示未执行，文案为“内存分析暂未执行”。

分析主状态按以下顺序推进：

```text
created -> uploading -> queued -> scheduled -> running -> analyzing -> completed
```

任务卡显示“等待 Agent”“正在抓取冷启动 Trace”“正在抓取滑动 Trace”“正在分析 Trace”和“正在生成报告”等阶段。用户提交后弹窗立即关闭，任务在主界面后台运行。

## 失败、取消和部分成功

- 设备在创建前离线：返回 `remote_device_capture_unavailable`，不创建分析。
- APK 元数据无效：分析失败为 `apk_metadata_invalid`，不投递 Agent。
- Agent 在领取前离线：分析保持 queued；设备重新上线后可继续领取。
- 租约过期：旧 token 不能续租、上传或完成；分析显示稳定可重试失败。
- 用户取消：服务端标记 cancel requested，Agent 停止 ADB/Perfetto，删除工作目录，确认取消；服务器删除未完成上传。
- 一个 Trace 成功、另一个失败：进入 `partially_completed`，SmartPerfetto 和 PerfPilot 使用成功证据并说明缺失场景。
- 两个 Trace 均失败：分析失败，不调用 PerfPilot AI；保留脱敏 Agent 诊断码。
- SmartPerfetto 某场景失败：保留另一场景结果，并生成部分报告。
- 源码 Agent 超时：Trace 报告继续生成，源码状态为 unavailable。

## 安全和隔离

- 浏览器只能选择当前会话团队的设备和源码工作区。
- 任务定义中的 Agent、设备和团队在创建、领取、上传与完成时重复校验。
- 设备 serial 不写分析状态、API 响应、日志或报告；任务只使用 HMAC device digest。
- APK、Trace、Agent log 和报告全部位于 `/home/rivotek/perfpilot/data/local-runtime/teams/<team_id>/analyses/<analysis_id>`。
- Agent access token、refresh token、lease token、签名 URL 和对象内部标识不写入分析文件或日志。
- APK 下载和 Trace 上传授权只能用于当前 execution、lease version 和制品。
- 每次统一重启先停服务，再永久删除整个分析数据根，不生成 archive、backup 或 ZIP。

## 测试与验收

自动化测试必须覆盖：

1. user01 只能把任务发送给 user01 的 Agent/设备；user02 无法读取或完成该任务；
2. 设备离线、忙碌或归属错误时不创建任务；
3. APK finalize 后才解析元数据和创建任务，失败时零任务；
4. 签名快照只含 startup/scroll，绑定正确 Agent、device digest 和 APK；
5. Agent 使用真实 capture runner 完成 APK 下载、两个 Trace 和日志上传；
6. 上传支持断点续传，错 lease、错团队、错大小或错 SHA-256 均拒绝；
7. 一个场景失败时另一个 Trace 仍进入 SmartPerfetto，并生成部分报告；
8. 两个场景成功时 SmartPerfetto 接收两份 Trace，PerfPilot 只调用一次 AI；
9. 选择 strong 源码后最终报告包含相对路径、symbol、Unified Diff 和复测方案；
10. weak/none 或没有源码时不显示路径、行号或 Diff；
11. SmartPerfetto 原始报告和 PerfPilot 源码报告保持两份独立文档；
12. 自然语言为简体中文，技术术语、代码和 Diff 保持原文；
13. 取消能停止 Agent 并清理未完成上传；
14. 服务重启删除 APK、Trace 和报告，不备份，保留账户与 Agent；
15. macOS、Windows 和 Linux Agent 单元测试继续通过；
16. Ubuntu 实机验收中，网页、API、SmartPerfetto 和 Agent 控制链健康。

## 非目标

本次不实现 Android Memory 远程循环、不自动修改源码、不自动提交 Gerrit、不实现公网部署，也不改变当前测试服务器“重启清空全部分析数据”的策略。
