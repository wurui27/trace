export type Severity = "critical" | "warning" | "healthy";

export type MetricState = "measured" | "missing" | "failed";

export type ProblemStatus =
  | "已确认问题"
  | "疑似问题"
  | "通过"
  | "数据不足"
  | "本次采集无效";

export type EvidenceStep =
  | "确认症状"
  | "锁定场景窗口"
  | "确认线程状态"
  | "追踪依赖"
  | "排除系统条件";

export interface EvidenceItem {
  readonly step: EvidenceStep;
  readonly interval: string;
  readonly value: string;
  readonly explanation: string;
}

export interface PerformanceProblem {
  readonly id: string;
  readonly title: string;
  readonly area: string;
  readonly severity: Severity;
  readonly status: ProblemStatus;
  readonly impact: string;
  readonly summary: string;
  readonly conclusion: string;
  readonly suggestion: string;
  readonly confidence: number;
  readonly validSamples: number;
  readonly reproducedRuns: number;
  readonly variability: string;
  readonly currentValue: string;
  readonly targetValue: string;
  readonly delta: string;
  readonly sourceLocation: string;
  readonly acceptanceCriteria: string;
  readonly evidence: ReadonlyArray<EvidenceItem>;
}

export interface DashboardData {
  readonly app: {
    readonly name: string;
    readonly packageName: string;
    readonly version: string;
  };
  readonly device: {
    readonly name: string;
    readonly os: string;
    readonly serial: string;
    readonly verified: boolean;
  };
  readonly startup: {
    readonly value: string;
    readonly target: string;
    readonly state: MetricState;
    readonly context: string;
    readonly cold: string;
    readonly warm: string;
    readonly hot: string;
  };
  readonly secondaryMetrics: ReadonlyArray<{
    readonly label: string;
    readonly value: string;
    readonly unit: string;
    readonly state: MetricState;
    readonly context: string;
  }>;
  readonly problems: ReadonlyArray<PerformanceProblem>;
  readonly credibility: {
    readonly runs: number;
    readonly deviceConsistency: string;
    readonly thermalState: string;
    readonly failures: number;
  };
}

function deepFreeze<T>(value: T): T {
  if (value !== null && typeof value === "object") {
    for (const nestedValue of Object.values(value)) {
      deepFreeze(nestedValue);
    }
    Object.freeze(value);
  }

  return value;
}

const dashboardData = deepFreeze<DashboardData>({
  app: {
    name: "Acme Gallery",
    packageName: "com.acme.gallery",
    version: "4.8.0 (480)",
  },
  device: {
    name: "Pixel 8",
    os: "Android 15",
    serial: "3A221JEHN02762",
    verified: true,
  },
  startup: {
    value: "1.42 s",
    target: "< 1.20 s",
    state: "measured",
    context: "5 轮冷启动中位数",
    cold: "1.42 s",
    warm: "684 ms",
    hot: "418 ms",
  },
  secondaryMetrics: [
    {
      label: "页面流畅度",
      value: "8.6",
      unit: "%",
      state: "measured",
      context: "相册网格连续滚动 · 2 个异常页面",
    },
    {
      label: "主线程响应",
      value: "186",
      unit: "ms",
      state: "measured",
      context: "启动场景 · 最长连续阻塞",
    },
    {
      label: "内存稳定性",
      value: "+18",
      unit: "MB",
      state: "measured",
      context: "详情页进出 10 轮后未回落",
    },
    {
      label: "CPU 与调度",
      value: "72",
      unit: "%",
      state: "measured",
      context: "启动峰值 · 无温控降频",
    },
  ],
  problems: [
    {
      id: "startup-main-thread",
      area: "启动体验",
      title: "首页启动慢",
      status: "已确认问题",
      severity: "critical",
      impact: "用户平均要多等待 217 ms 才看到首页",
      summary: "主线程在首帧前同步等待 PackageManager 186 ms",
      conclusion:
        "首页启动的主要延迟来自 Application 初始化阶段的同步包管理查询；跨进程等待链已闭合。",
      suggestion: "将包信息查询移出首帧关键路径，并缓存稳定结果。",
      confidence: 92,
      validSamples: 5,
      reproducedRuns: 4,
      variability: "CV 4.8%",
      currentValue: "1.42 s",
      targetValue: "< 1.20 s",
      delta: "+217 ms",
      sourceLocation:
        "未提供源码；当前定位到 Application 初始化中的包管理查询调用链",
      acceptanceCriteria:
        "Pixel 8、Android 15、相同构建与数据集下运行 5 轮；冷启动中位数 < 1.20 s，问题签名最多出现 1 轮。",
      evidence: [
        {
          step: "确认症状",
          interval: "0–1.42 s",
          value: "冷启动中位数 1.42 s",
          explanation: "5 轮有效样本均超过 1.20 s 目标",
        },
        {
          step: "锁定场景窗口",
          interval: "startup_id=42",
          value: "首帧前关键路径",
          explanation: "只分析点击桌面图标到首帧呈现的区间",
        },
        {
          step: "确认线程状态",
          interval: "482–668 ms",
          value: "主线程 Sleeping 186 ms",
          explanation: "这段墙钟时间主要在等待，不是 App CPU 执行",
        },
        {
          step: "追踪依赖",
          interval: "binder_txn 0x7ab2",
          value: "PackageManager 响应 161 ms",
          explanation: "同步调用的客户端与服务端证据已跨进程闭合",
        },
        {
          step: "排除系统条件",
          interval: "完整采集窗口",
          value: "温度正常 · 无 CPU 降频",
          explanation: "设备竞争与温控不是主要原因",
        },
      ],
    },
    {
      id: "gallery-grid-jank",
      area: "页面流畅度",
      title: "相册网格卡顿",
      status: "疑似问题",
      severity: "warning",
      impact: "相册网格连续滚动时，每 12 帧约有 1 帧明显迟到",
      summary: "图片解码与网格重组在主线程重叠",
      conclusion:
        "卡顿与图片解码批次重叠，但缺少完整工作线程唤醒链，当前只标记为疑似问题。",
      suggestion: "把大图解码下沉到后台，并限制同一帧提交的缩略图数量。",
      confidence: 84,
      validSamples: 5,
      reproducedRuns: 4,
      variability: "CV 7.2%",
      currentValue: "8.6%",
      targetValue: "< 5%",
      delta: "+3.6 pp",
      sourceLocation:
        "未提供源码；当前定位到相册网格的图片加载与布局阶段",
      acceptanceCriteria:
        "相同设备与滚动脚本运行 5 轮；慢帧率 < 5%，P95 超时 < 8 ms，问题签名最多出现 1 轮。",
      evidence: [
        {
          step: "确认症状",
          interval: "12.4–42.4 s",
          value: "慢帧率 8.6%",
          explanation: "1,824 帧中有 157 帧错过预期呈现时间",
        },
        {
          step: "锁定场景窗口",
          interval: "scenario=grid-scroll-03",
          value: "连续滚动 30 s",
          explanation: "只包含脚本执行的网格滚动区间",
        },
        {
          step: "确认线程状态",
          interval: "18.21–18.27 s",
          value: "主线程 Running 63 ms",
          explanation: "代表性慢帧主要消耗在 App CPU 工作",
        },
        {
          step: "追踪依赖",
          interval: "frame_cluster=img-11",
          value: "11/14 慢帧与图片解码重叠",
          explanation: "相关性较强，但工作线程唤醒链仍不完整",
        },
        {
          step: "排除系统条件",
          interval: "完整采集窗口",
          value: "温度正常 · SurfaceFlinger 无连续超时",
          explanation: "显示服务与温控不是主要原因",
        },
      ],
    },
    {
      id: "detail-response",
      area: "主线程响应",
      title: "详情页响应慢",
      status: "疑似问题",
      severity: "warning",
      impact: "打开详情页时，最慢一次触摸响应达到 286 ms",
      summary: "主线程在页面创建阶段连续执行布局和资源读取",
      conclusion:
        "响应延迟与布局、资源读取重叠；没有内核阻塞栈，暂不能断言为磁盘 I/O 根因。",
      suggestion: "拆分首屏布局，延后非首屏资源读取，并补采 I/O 数据源复测。",
      confidence: 78,
      validSamples: 4,
      reproducedRuns: 3,
      variability: "CV 9.1%",
      currentValue: "286 ms",
      targetValue: "< 100 ms",
      delta: "+186 ms",
      sourceLocation:
        "未提供源码；当前定位到详情页创建与首屏资源加载阶段",
      acceptanceCriteria:
        "相同设备与点击脚本运行 5 轮；P95 输入响应 < 100 ms，且不再出现连续主线程长任务。",
      evidence: [
        {
          step: "确认症状",
          interval: "54.81–55.10 s",
          value: "输入响应 286 ms",
          explanation: "最慢一次点击超过 100 ms 目标",
        },
        {
          step: "锁定场景窗口",
          interval: "scenario=detail-open-02",
          value: "点击到首屏稳定",
          explanation: "分析已排除后台空闲时间",
        },
        {
          step: "确认线程状态",
          interval: "54.86–55.05 s",
          value: "主线程 Running 173 ms",
          explanation: "布局和资源工作占据主要响应窗口",
        },
        {
          step: "追踪依赖",
          interval: "input_event=0x91f",
          value: "缺少内核阻塞栈",
          explanation: "Trace 无法闭合 I/O 依赖，因此仍标记为疑似问题",
        },
        {
          step: "排除系统条件",
          interval: "完整采集窗口",
          value: "CPU 可用 · 无 GC 暂停",
          explanation: "调度竞争与 GC 不是主要原因",
        },
      ],
    },
  ],
  credibility: {
    runs: 5,
    deviceConsistency: "同一台已验证真机",
    thermalState: "温度正常",
    failures: 0,
  },
});

export function getDashboardData(): DashboardData {
  return dashboardData;
}

export function getProblem(id: string): PerformanceProblem | undefined {
  return dashboardData.problems.find((problem) => problem.id === id);
}
