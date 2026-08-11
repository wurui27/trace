# PerfPilot 多用户隔离、原始 SmartPerfetto 报告与中文输出设计

## 目标

本次改造解决四个问题：

1. 当前 Ubuntu 测试服务每次重启后必须永久删除分析数据，不创建备份。
2. 不同登录账号必须隔离 Trace、任务、报告、Agent 和源码工作区。
3. 最终报告必须同时提供 PerfPilot AI 总结和 SmartPerfetto 原始 AI 文档。
4. PerfPilot AI 的自然语言必须使用简体中文，只允许技术标识保留英文。

当前 Ubuntu 部署仍是可信内网测试服务。本设计为测试运行时增加可靠的账号和数据隔离，但不把它声明为公网生产架构。正式生产继续使用现有 PostgreSQL、租户路由和私有对象存储设计。

## 已确认的产品决策

- `ray_wu` 保持平台管理员账号。
- 创建 `user01`、`user02`、`user03`、`user04` 和 `user05` 五个普通账号。
- 普通账号使用独立随机临时密码，首次登录必须修改。
- 用户只能看到自己的分析、上传文件、报告、Agent 和源码工作区。
- 用户自行选择和注册本机源码路径；系统不预置源码目录。
- 管理员管理账号和运行状态，默认不读取普通用户的 Trace、源码或报告。
- 当前测试服务器重启时删除所有用户的分析数据；正式生产环境不自动删除。
- 最终报告新增第四个标签“SmartPerfetto 原始报告”，支持完整查看和独立下载。
- PerfPilot AI 使用简体中文。指标名、技术术语、类名、方法名、文件名、代码和 Unified Diff 可保留英文。

## 运行时数据边界

Ubuntu 测试部署把数据分成持久控制数据和临时分析数据。

### 持久控制数据

持久控制数据放在 `/home/rivotek/perfpilot/state`，服务重启和分析清理不得删除：

- 用户 ID、用户名、密码哈希、角色和首次改密状态；
- 用户与团队的唯一归属；
- Agent ID、凭据摘要、所属用户、状态和公开能力；
- 源码工作区公开标识、所属 Agent、分支、提交 SHA 和验证配置；
- 审计事件，不含 Trace、源码正文、报告正文或绝对源码路径。

文件必须以 `0600` 权限原子写入，目录必须为 `0700`。密码使用现有密码哈希实现；服务不保存明文临时密码。会话重启后可以失效，用户重新登录即可。

### 临时分析数据

临时分析数据放在 `/home/rivotek/perfpilot/data/local-runtime/teams/<team_id>`：

- Trace 和上传文件；
- 分析状态、SmartPerfetto 结果和 Android Memory 结果；
- PerfPilot AI 投影、轮次和最终报告；
- 源码上下文制品和下载文件。

每次测试服务重启前，systemd 必须调用一个受限清理程序。程序只接受固定测试数据根目录，拒绝符号链接、根目录、用户目录、配置目录和持久状态目录。它永久删除临时分析数据，不移动、不压缩、不备份，然后重新创建空的 `0700` 目录。清理失败时 API 和网页不得启动。

## 身份、租户和授权

每个用户拥有一个稳定的 `user_id` 和一个稳定的 `team_id`。测试运行时采用一人一团队；API 路径继续使用现有 `/v1/teams/{team_id}` 结构。

登录成功后，服务端会话确定当前用户和允许的 `team_id`。服务端不得相信浏览器传入的团队归属。每个分析、上传、报告、Agent 和源码工作区操作都执行以下检查：

1. 会话有效；
2. 当前用户属于路径中的团队；
3. 资源记录的团队与路径团队一致；
4. 文件系统路径位于该团队的临时根目录；
5. 跨团队资源统一返回 `404`，避免泄露存在性。

`ray_wu` 的平台管理员权限只覆盖账号创建、禁用、密码重置和运行状态。读取普通用户分析内容需要未来单独设计的显式支持授权，本次不提供管理员旁路。

## 五个普通账号

增加受保护的管理员命令，用于创建普通用户及其团队。命令通过受限文件描述符或交互输入接收密码，不接受命令行密码，不打印密码或哈希。

部署时生成五个随机临时密码，通过当前会话一次性交付给管理员。服务只保存哈希，并把 `must_change_password` 设为 `true`。用户首次登录后只能进入修改密码页面；修改成功前不能访问分析、Agent 或源码 API。

账号名固定为 `user01` 至 `user05`。重复执行部署命令必须幂等，不得覆盖已修改的密码。

## Agent 和源码工作区

“关联源码”当前为空的直接原因是服务器返回 `workspaces: []`，Mac 上也没有运行 PerfPilot Agent。改造后的流程如下：

1. 用户登录自己的账号；
2. 用户生成只属于自己的 Agent 注册码；
3. 用户在 macOS、Windows 或 Linux 上注册并启动 Agent；
4. 用户在本机执行 `perfpilot-agent source add --name ... --path ...`；
5. Agent 心跳只上报公开工作区信息，不上报绝对路径；
6. 源码选择器只列出当前用户团队中在线且 `ready` 的工作区。

同一台开发机可以运行多个用户的 Agent 实例，但每个实例必须使用独立配置、凭据和工作目录。更常见的用法是一台开发机只绑定一个用户。服务器清理分析数据时保留 Agent 注册和工作区归属，因此源码选项不会因重启消失。

## SmartPerfetto 原始 AI 文档

SmartPerfetto 已在每次分析目录生成 `smartperfetto-report.json`。该文件是约四百 KiB 的结构化原始 AI 报告，包含摘要、发现、验证结果和分析过程。系统不得再次调用模型复制它。

SmartPerfetto 原始报告与 PerfPilot 优化报告是两份职责不同的产物，不要求内容或结论结构一致：

- SmartPerfetto 原始报告完整呈现内核自己的分析过程、证据和结论，不被 PerfPilot 改写；
- PerfPilot 优化报告以源码关联为主要价值，把 Trace 问题映射到具体文件、类和方法，直接给出修改动作、代码方案和复测方法；
- 只有服务端验证为 strong 的源码证据才能生成文件位置和 Unified Diff；weak/none 或没有源码时只给不带源码坐标的通用建议；
- PerfPilot 不复制 SmartPerfetto 长篇指标，也不为了“看起来一致”改写原始报告。

服务在 SmartPerfetto 完成后保存原始文档的私有制品绑定：

- `team_id`；
- `analysis_id`；
- `artifact_id`；
- MIME `application/json`；
- 文件大小；
- SHA-256；
- 不可变版本标识。

最终 `AnalysisReport` 只保存该私有制品的公开状态和摘要，不嵌入四百 KiB 原文。新增授权接口：

```text
GET /v1/teams/{team_id}/analyses/{analysis_id}/smartperfetto-original
```

接口要求同团队会话，按绑定版本和校验和读取，限制响应大小，设置 `Cache-Control: private, no-store` 和 `X-Content-Type-Options: nosniff`。跨团队和未知分析返回 `404`；缺失或校验失败返回稳定错误，不暴露存储路径。

网页为 1.2 报告增加第四个标签“SmartPerfetto 原始报告”。标签首次打开时按需加载，避免拖慢结论页。页面优先渲染原始摘要、发现和验证结果，并提供“查看完整 JSON”和“下载原始报告”。下载文件名固定为 `smartperfetto-{analysis_id}.json`。打印或 PDF 包含原始报告摘要，不展开完整 JSON。

## PerfPilot AI 中文输出

英文输出的根因是 `perfpilot-report-v3` 使用英文指令，却没有规定叙述语言。修复在生成边界完成，而不是仅在前端翻译。

Prompt 增加以下硬约束：

- 所有面向用户的叙述字段使用简体中文；
- 保留 Android、Perfetto、Jank、PSS、TTID 等标准术语；
- 类名、方法名、包名、文件名、指标名、规则 ID、代码和 Diff 保持原文；
- 不翻译引用标识，不改写数值或单位；
- 不输出中英双语重复段落。

服务端增加确定性语言检查。检查只覆盖自然语言字段，例如结论、用户影响、建议标题、建议动作、预期效果、复测步骤和限制说明。代码块、路径、symbol、指标名、ID 和 Diff 不参与中文比例计算。

当自然语言包含明显的大段英文且缺少中文时，validator 拒绝候选，并在同一逻辑报告轮次重试一次。第二次仍不合格时，系统发布 SmartPerfetto 核心报告和原始文档，把 PerfPilot AI 标记为失败，绝不把不合格英文报告伪装成成功结果。

## 重启清理

新增独立的 `perfpilot-reset-analysis-data` 程序和 systemd oneshot 服务。三个运行服务依赖该 oneshot：

```text
perfpilot-reset-analysis-data.service
  -> perfpilot-smartperfetto.service
  -> perfpilot-api.service
  -> perfpilot-web.service
```

管理员执行统一重启命令时，先停止三个运行服务，再启动 reset oneshot，最后启动三个运行服务。任何直接重启 API/Web/SmartPerfetto 的操作也必须通过统一包装命令或 systemd target，避免绕过清理。

生产环境配置 `PERFPILOT_RESET_ANALYSIS_ON_RESTART=false`，并禁止启用 reset oneshot。测试服务器配置为 `true`。

## 错误处理

- 登录失败返回统一错误，不区分用户不存在和密码错误。
- 首次改密未完成返回稳定的 `password_change_required`。
- 用户访问其他团队返回 `404`。
- Agent 离线时源码选择器显示当前用户的空态和注册说明。
- 原始 SmartPerfetto 制品缺失时，我们的结论仍可读取；原始报告标签显示明确失败状态。
- PerfPilot AI 中文检查失败时保留 SmartPerfetto 原始报告和确定性 Trace 事实。
- 重启清理失败时服务保持停止，避免旧数据与新会话混用。

## 测试与验收

自动化测试必须覆盖：

1. 五个普通账号创建幂等，密码不进入日志、命令行或 Git；
2. 首次登录强制改密；
3. 用户 A 无法列出、读取、取消或下载用户 B 的资源；
4. 用户 A 的浏览器不能看到用户 B 的分析卡片和报告入口；
5. 用户 A 只能选择自己的 Agent 和源码工作区；
6. Agent 绝对源码路径不进入 API、数据库、日志或报告；
7. 重启永久删除所有团队的临时分析数据，不生成 archive 或 backup；
8. 重启保留用户、密码哈希、团队、Agent 注册和源码工作区归属；
9. SmartPerfetto 原始报告只允许所属用户查看和下载；
10. 原始报告校验和或版本不匹配时拒绝下载；
11. PerfPilot AI 中文候选通过，英文叙述候选触发一次重试；
12. 第二次仍为英文时发布降级报告和 SmartPerfetto 原始文档；
13. 报告四个标签、旧报告兼容、SSR、打印和生产构建通过；
14. Ubuntu 重启后网页、API 和 SmartPerfetto 健康，分析列表为空，五个用户仍可登录。
15. SmartPerfetto 标签完整忠实展示原始文档；PerfPilot 标签独立输出源码定位、直接优化动作、strong-only Diff 和复测方案，两份报告不会被合并成同一份内容。

## 非目标

本次不实现管理员查看普通用户报告、不实现用户间共享、不实现团队多人协作、不实现公网 HTTPS、不实现计费，也不实现已暂缓的自动补丁执行。正式生产迁移和数据保留策略另行验收。
