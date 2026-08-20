# Finding 中心 Trace 分析工作台优化设计

**日期：** 2026-08-20

**状态：** 已确认

**范围：** `AnalysisReport 1.3`、`Synthesis 2.1`、结构化 Finding、证据定位、源码建议、复测计划和桌面端报告工作台

**输入：** 《PerfPilot-Trace分析专项设计报告》、现有分析可靠性设计、当前 Trace 上传分析主链和真实报告验收结果

## 1. 背景

PerfPilot 已经能够完成 Trace 上传、SmartPerfetto 分析、源码关联、AI 中文总结和最终报告发布。现有主链的主要问题不再是“没有分析结果”，而是结果仍然以长篇文本为中心：

- 概览、AI 总结和报告正文容易重复；
- “分析完成”“部分完成”和内部阶段状态可能同时出现；
- Finding、Evidence、Recommendation 仍缺少稳定、可比较的结构；
- 证据 ID 不能总是直接定位到 Trace 时间窗口；
- 主要问题默认只显示少量内容时，其余问题容易被理解为没有生成；
- 源码结论、Trace 结论和 AI 文案之间的可信边界不够直观；
- 复测仍以重新阅读两份报告为主，无法稳定比较同一个问题。

本设计把 PerfPilot 从“AI 报告阅读器”升级为“Finding 中心的 Trace 分析工作台”。默认页面仍然面向不熟悉性能分析的用户和管理人员，但所有结论都可以继续展开到证据、源码、修改建议和复测标准。

## 2. 设计原则

1. **先证据，后结论。** 正式 Finding 必须引用经过服务端验证的 Evidence。
2. **AI 不制造事实。** AI 只解释结构化指标、Finding、Evidence 和已验证源码上下文。
3. **默认易读，细节可展开。** 默认展示三条主要问题，其余内容完整保留。
4. **Trace 结论与源码结论分层。** 源码不可读或不匹配时，Trace 分析继续，但不得伪造源码根因或 Diff。
5. **原始报告保持原始。** SmartPerfetto 原始 HTML 独立保存和展示，不被 PerfPilot 改写。
6. **状态来自同一权威。** 页面状态、阶段状态和报告完整性不能相互矛盾。
7. **稳定结构优先于稳定文案。** Finding ID、指标和证据绑定必须可重复，AI 措辞允许变化。
8. **修改建议仅供参考。** 源码建议必须显示风险、适用范围和复测方法。

## 3. 目标

本次优化必须达到以下结果：

1. 用户能在概览中快速理解本次分析是否完成、数据是否完整、最重要的问题是什么。
2. 每条正式 Finding 都能解释问题、影响、机制、根因、证据、建议和复测方法。
3. Evidence 可以定位到 Trace 时间范围、进程、线程、Slice、Track 或 SQL。
4. 默认展示三条主要问题，其余问题可展开、筛选和排序，不丢失。
5. 源码真实匹配时提供根因、多个参考 Diff、风险和预期收益；不匹配时明确降级。
6. SmartPerfetto、源码和 AI 分别显示真实能力状态，不互相替代成功。
7. AI 输出不合规时仍能生成确定性中文报告，而不是让整个报告不可用。
8. 同一问题在复测报告中保持稳定身份，可以判断已解决、改善、无变化、恶化或新出现。

## 4. 非目标

本设计不包含：

- 账号注册、账号审核、Agent 审核或密码找回；
- 真机采集流程修改；
- 管理后台或操作记录；
- 修改 SmartPerfetto 原始 HTML 内容；
- 新建“技术附录”页面；
- 一次性实现多项目、长期趋势和 CI 性能门禁；
- 一次性删除或重写现有 1.2 报告；
- 让 AI 自主运行新的 Trace SQL 或访问未授权源码。

多报告比较、性能预算和回归门禁属于后续 P2 能力，本次只建立稳定 Finding 和 RetestPlan 基础。

## 5. 总体信息架构

新桌面端报告使用六个区域：

1. **概览**
   - 绿色“分析完成”主状态；
   - Trace、SmartPerfetto、源码和 AI 的能力完整性；
   - 关键路径瀑布；
   - 三条主要 Finding；
   - 其余 Finding 数量和展开入口。
2. **问题清单**
   - 全部 Finding；
   - 按优先级、影响、证据等级、归因可信度和状态筛选；
   - 稳定排序和去重。
3. **证据与指标**
   - 指标定义、数值、单位和来源；
   - Evidence 时间范围、进程、线程、Slice、Track、SQL；
   - 打开对应 Trace 时间窗口的受控 deep link。
4. **源码与优化**
   - 只在源码 strong match 时显示相对路径、符号和 Diff；
   - 展示源码根因、建议、预期收益、改动成本、风险和适用范围；
   - weak、none、unavailable 和 mismatch 不显示路径、符号或 Diff。
5. **SmartPerfetto 原始报告**
   - 独立加载原始 HTML；
   - 保持原始字节和原始结构；
   - 不参与 PerfPilot 文案重写。
6. **复测计划**
   - 测试类型、包名、时长、设备和构建环境；
   - 需要验证的 Finding；
   - 指标和通过标准；
   - 与原报告的环境可比性说明。

“数据完整性”不单独占用页签。它作为概览能力卡片和每个 Finding 的可信度信息展示，避免页面继续膨胀。

## 6. Finding 数据模型

### 6.1 Finding

每条 Finding 至少包含：

```text
finding_id
scenario_type
title
problem
impact
mechanism
root_cause
critical_path_contribution
priority
priority_score
evidence_ids[]
metric_ids[]
source_ref_ids[]
confidence
status
confirmed_items[]
unconfirmed_items[]
recommendations[]
retest_plan
```

字段规则：

- `finding_id` 不能由 AI 文案直接生成；
- `problem` 回答“发生了什么”；
- `mechanism` 回答“为什么产生这个性能现象”；
- `root_cause` 回答“能够被证据支持的根因是什么”；
- `source_ref_ids` 只允许引用 strong source context；
- `confirmed_items` 和 `unconfirmed_items` 必须分开；
- `status` 至少支持 `confirmed|hypothesis|resolved|improved|unchanged|regressed|new`；
- `hypothesis` 不进入正式主要问题数量，但可以在展开区域展示。

### 6.2 Evidence

每条 Evidence 至少包含：

```text
evidence_id
kind
scenario_type
metric_ids[]
time_range
process
thread
track
slice
sql
summary
deep_link
source
```

约束：

- `time_range` 使用 Trace 内相对时间，不公开服务器临时路径；
- `process`、`thread`、`track`、`slice` 和 `sql` 均为可选，但必须至少有一种可定位方式；
- `deep_link` 只能使用服务端生成的内部受控参数；
- 正式 Finding 至少引用一条 Evidence；
- AI 不得创建新的 Evidence ID。

### 6.3 Metric

Metric 使用结构化值，而不是只保留文本：

```text
metric_id
name
value
unit
aggregation
scenario_type
source
evidence_ids[]
quality
```

缺失值必须表示为 `unavailable` 或 `not_collected`，不能写成 `0`。

### 6.4 Recommendation

Recommendation 至少包含：

```text
recommendation_id
finding_id
summary
steps[]
expected_impact
effort
risk
applicability
source_fix_ids[]
verification_metric_ids[]
```

其中：

- `expected_impact` 使用范围或定性等级，不能伪造精确收益；
- `source_fix_ids` 可以有多项，但必须各自绑定 strong source ref；
- 没有 strong source 时，允许输出不包含文件位置的手动解决方案；
- 所有建议统一显示“修改仅供参考”。

### 6.5 RetestPlan

RetestPlan 至少包含：

```text
retest_plan_id
finding_id
scenario_type
package_name
duration_seconds
environment_fingerprint
metrics[]
pass_criteria[]
notes
```

复测结果必须先校验环境可比性，再比较 Finding。

## 7. 四维可信度

每条 Finding 分别记录四个维度，不能合并为一个模糊的“置信度”：

1. **数据完整性**
   - Trace 是否完整；
   - 必需轨道和事件是否存在；
   - SmartPerfetto 是否完成；
   - 源码是否请求、可读、匹配。
2. **证据等级**
   - `E0`：没有直接证据；
   - `E1`：只有相关指标或弱关联现象；
   - `E2`：有明确时间窗口和相关线程/事件；
   - `E3`：机制链由多条一致证据支持；
   - `E4`：机制、责任域和源码证据形成闭环。
3. **归因可信度**
   - `low|medium|high`；
   - 反映从现象到根因的可信程度；
   - 不因存在源码文件而自动提高。
4. **统计可信度**
   - 单次样本默认不能标记为高；
   - 多次样本需要记录数量、波动和聚合方式；
   - 本次只建立字段和单样本规则，不实现完整统计平台。

正式 Finding 至少需要 `E2`。`E0` 和 `E1` 只能作为假设或数据不足提示。

## 8. 优先级与去重

### 8.1 优先级评分

优先级由服务端确定性计算：

- 性能和业务影响：40%；
- 证据等级和归因可信度：25%；
- 对关键路径的贡献：20%；
- 是否稳定复现：15%。

AI 可以解释排序原因，但不能覆盖分数。

概览选择 P0/P1 中得分最高的三条 Finding。分数相同时使用稳定 `finding_id` 排序，保证同一报告重复构建时顺序一致。

### 8.2 稳定身份

`finding_id` 由以下稳定字段确定性生成：

```text
scenario_type
normalized_mechanism
root_cause_domain
responsible_component
```

AI 标题、中文措辞和证据展示顺序不能参与 ID。

### 8.3 合并规则

- 同一场景、同一机制、同一根因责任域合并为一条 Finding；
- 合并后保留全部 evidence、metric、source ref 和 recommendation；
- 不同根因即使表现相似，也保持独立；
- 重复 source fix tuple 必须被合同、服务端和前端同时拒绝；
- 合并后的优先级重新确定性计算。

## 9. 分析链路

```text
Trace 上传与完整性校验
  -> SmartPerfetto 原始 HTML + 确定性指标/证据
  -> 规则引擎形成候选 Finding
  -> 可选源码工程识别与受限读取
  -> Trace 与源码证据合并
  -> AI 中文解释和组织
  -> 合同、语义、隐私与一致性校验
  -> AnalysisReport 1.3 原子发布
```

### 9.1 SmartPerfetto

- SmartPerfetto 是 PerfPilot Finding 的底层分析依据；
- 原始 HTML 与结构化 normalized result 分开保存；
- SmartPerfetto 失败时不能生成没有依据的 PerfPilot Finding；
- SmartPerfetto 持续产生有效进度时，不设置简单的总时长截止。

### 9.2 源码

- Agent 必须实际读取 `AndroidManifest.xml`、Gradle 配置和源码文件；
- 服务端校验包名、工程指纹、可读源码数量和 source context hash；
- 匹配失败时继续 Trace 分析，并明确显示源码不匹配；
- 不匹配、不可读、Agent 离线或 weak match 时禁止发布路径、符号和 Diff；
- 源码读取持续产生有效进度时，不设置简单的总时长截止。

### 9.3 AI 中文总结

- 输入只包含经过验证的结构化数据；
- 固定组织为“问题点、为什么、源码根因、修改建议”；
- 用户可见叙述以中文为主，专业术语可以保留英文；
- 不合规输出最多自动重试一次；
- 第二次仍失败时，服务端使用确定性中文模板生成报告；
- AI 持续产生有效进度时，不设置简单的总时长截止。

### 9.4 最终校验

最终校验必须覆盖：

- schema exact keys；
- Finding、Evidence、Metric、Recommendation 引用闭合；
- 所有数值可追溯；
- source strong/weak/none 不变量；
- Finding ID 和排序稳定性；
- 重复问题和重复 source fix；
- 绝对路径、令牌、远端地址、命令和私有字段；
- SmartPerfetto normalized evidence 与 AI 叙述一致性；
- 主状态、阶段状态和报告状态一致性。

只有最终校验通过，才能原子发布报告并把公开主状态切换为绿色“分析完成”。

## 10. 失败、取消与恢复

### 10.1 阶段失败

- SmartPerfetto 失败：分析失败，不生成无依据的 PerfPilot Finding；
- 源码失败或不匹配：Trace 分析继续，源码能力单独降级；
- AI 输出无效：一次重试后使用确定性中文模板；
- Evidence 不闭合或隐私校验失败：拒绝发布，保留稳定错误码和可重试入口；
- 原始 HTML 可用但结构化结果失败：允许查看原始 HTML，但不能声称 PerfPilot 分析完成。

### 10.2 取消

用户在任意阶段点击取消时：

1. 先持久化 `cancel_requested`；
2. 停止创建新的 SmartPerfetto、source 和 AI 工作；
3. 请求上游取消；
4. 拒绝迟到 completion、报告和源码结果恢复任务；
5. 最终进入 `canceled`。

### 10.3 重启恢复

- 每个阶段使用持久化 generation、input binding、artifact binding 和 checkpoint；
- 已完成步骤不重复上传或重复分析；
- 未完成步骤从最近安全提交点恢复；
- 报告发布与主状态切换使用同一原子边界；
- 重启后仍然遵守取消状态和 source capability 状态。

## 11. 状态展示

公开主状态只表达分析是否仍在运行、是否成功生成可用报告：

- `queued`；
- `running`；
- `completed`；
- `failed`；
- `canceled`。

当最终报告可用且一致性校验通过时，页面显示绿色“分析完成”。源码未匹配、统计样本不足或部分可选能力不可用，不再把主标题改成容易误解的“部分结论”，而是在能力完整性和 Finding 可信度中明确说明。

内部仍可保留更细阶段状态，但前端不能根据报告是否存在自行推断主状态。

## 12. 合同和版本

### 12.1 新版本

- `AnalysisReport 1.3`：Finding 工作台最终文档；
- `Synthesis 2.1`：AI 结构化解释；
- Evidence、Metric、Recommendation 和 RetestPlan 作为 1.3 闭合子结构；
- 新分析只生成新版本。

### 12.2 兼容策略

- 1.0、1.1 和 1.2 报告继续精确读取；
- 1.2 使用当前旧报告组件；
- 1.3 分派到 Finding 工作台；
- 不批量重写已有持久化报告；
- 不向旧版本偷偷增加新字段；
- schema、examples、Python models、semantic validators、TypeScript validators 和 UI fixtures 必须在同一任务内更新。

### 12.3 SmartPerfetto HTML

- HTML 保持独立私有 artifact；
- 1.3 只记录受控公开 binding 和可用状态；
- HTML 查看和下载仍校验 team、analysis、artifact、size 和 SHA-256；
- 不把完整 HTML 塞入报告 JSON。

## 13. 前端行为

### 13.1 概览

- 状态与能力卡片先于 AI 文案；
- 关键路径瀑布使用确定性指标；
- 三条主要 Finding 显示影响、证据等级、归因可信度和优先级；
- “展开其余 N 项”必须显示真实数量。

### 13.2 Finding 详情

详情固定展示：

```text
问题点
  -> 为什么出现
  -> 结合源码判断的根因
  -> 修改建议
```

没有 strong source 时，“结合源码判断的根因”显示明确的源码不可用状态，不使用模型猜测填充。

### 13.3 Evidence 定位

- 点击证据打开对应 Trace 时间范围；
- 若当前没有内嵌 Trace viewer，则先生成受控 deep link 和可复制定位参数；
- Evidence 详情显示来源和验证等级；
- 不显示服务端文件路径。

### 13.4 打印和导出

- 打印包含概览、全部 Finding、证据摘要、源码建议和复测计划；
- 默认折叠内容在打印前展开；
- SmartPerfetto 原始 HTML 保持独立，不嵌入 PerfPilot PDF；
- weak/none/mismatch 在打印路径上继续隐藏源码位置和 Diff。

## 14. 验收标准

### 14.1 合同与确定性

1. `AnalysisReport 1.3` 和 `Synthesis 2.1` 使用闭合 schema。
2. 同一 normalized input 重建时 Metric、Finding 和 Evidence ID 稳定。
3. 所有引用闭合，未知字段和重复绑定被拒绝。
4. 缺失数据不能被当成零或“没有问题”。
5. 同一根因的重复 Finding 被确定性合并。

### 14.2 用户体验

1. 完整报告顶部显示绿色“分析完成”。
2. 默认显示三条主要问题，其他问题可展开。
3. 每条 Finding 都能看到问题、原因、根因、建议和可信度。
4. 每条正式 Finding 至少可以定位一条 Trace Evidence。
5. 不新增“技术附录”。
6. 页面保持蓝白色桌面端风格。

### 14.3 源码

1. strong match 可以显示相对路径、符号和多个参考 Diff。
2. weak、none、unavailable 和 mismatch 不显示路径、符号或 Diff。
3. 源码不匹配时 Trace 报告仍然完成，并明确显示不匹配。
4. AI 不得通过叙述字段绕过源码隐私限制。

### 14.4 生命周期

1. SmartPerfetto、源码和 AI 只要持续产生进度就继续运行。
2. AI 连续输出无效后使用确定性中文模板完成报告。
3. 用户在任意阶段取消后没有迟到报告。
4. 服务重启后不重复已完成步骤。
5. 报告可见与主状态完成在同一提交边界。

### 14.5 真实验收

使用真实上传 Trace 完成一次端到端验收：

1. 上传有效 Trace；
2. SmartPerfetto 生成原始 HTML 和结构化结果；
3. 生成稳定 Finding、Evidence 和 Metric；
4. 若选择源码工作区，验证工程指纹和包名；
5. AI 生成合规中文解释，或触发确定性中文降级；
6. 发布 `AnalysisReport 1.3`；
7. 页面显示三条主要问题和可展开的其余问题；
8. Evidence 可以定位到 Trace 时间范围；
9. SmartPerfetto 原始 HTML 可独立查看和下载；
10. 同一输入重建时 Finding ID 和指标保持稳定。

## 15. 实施顺序约束

后续实施计划必须按依赖顺序拆分：

1. 合同和稳定 ID；
2. 服务端 Finding/Evidence/Recommendation 构建与校验；
3. AI 2.1 输入、输出和确定性中文降级；
4. 报告 writer 和持久化；
5. 前端 strict parser；
6. Finding 工作台 UI；
7. Evidence deep link；
8. RetestPlan；
9. 兼容、重启、取消、隐私和真实 Trace 验收。

每一步先建立精确 RED，再完成最小 GREEN。不得先放宽前端或 schema，再依赖后续任务补安全闭包。

## 16. 已确认决策

1. 采用 Finding 工作台，而不是只增加长文本折叠。
2. 默认展示三条主要问题，其余问题完整折叠保留。
3. 页面使用六个区域，不新增技术附录。
4. 问题详情固定使用“问题点、为什么、源码根因、修改建议”。
5. SmartPerfetto 原始 HTML 独立保存，但 PerfPilot Finding 必须依赖其结构化分析结果。
6. 源码不匹配时继续 Trace 分析，不生成源码根因和 Diff。
7. 有可用且已验证报告时统一显示绿色“分析完成”。
8. SmartPerfetto、源码读取和 AI 中文总结采用进度驱动，不设置简单总截止时间。
9. 新报告采用 1.3，新 synthesis 采用 2.1，旧报告只读兼容。
10. 本次建立 RetestPlan，不实施完整多报告比较和 CI 门禁。
