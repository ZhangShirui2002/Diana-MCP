# 操作指南

## 核心原则

DIANA-MCP 把有限元工作分为两个阶段：

1. **模型建立与调试**：模型必须在 DianaIE GUI 中完整展示，由人核对几何、材料、边界、荷载、网格和求解器设置；
2. **参数分析**：只有从已通过人工校审和验证计算的基准模型出发，AI 才能在批准的参数范围内自动生成和求解变体。

人工审阅建立的是“模型是否符合人的工程意图”的信任；自动自检只能检查一致性、范围和已定义规则，不能替代工程判断。

```mermaid
flowchart LR
    A[GUI 建模与调试] --> B[完整展示和人工审阅]
    B --> C[验证计算]
    C --> D{质量检查通过?}
    D -- 否 --> A
    D -- 是 --> E[可信基准模型]
    E --> F[一次性批准参数方案]
    F --> G[自动生成变体]
    G --> H[范围检查和自检]
    H --> I[批量求解与质量检查]
```

## 日常启动

1. 启动 DianaIE，打开或新建工程；
2. 让客户端调用 `diana_status` 和 `diana_model_summary`；
3. 对不熟悉的命令先调用 `diana_manual_search`、`diana_keywords` 或 `diana_api_search`；
4. 开始修改前调用 `diana_model_snapshot` 留下基线；
5. 修改后调用 `diana_changes` 或 `diana_model_diff` 检查人和 AI 分别改了什么。

`diana_changes` 读取 DianaIE 命令日志和桥接日志，可区分 GUI/用户操作与 MCP 操作，并考虑撤销/重做。`diana_model_diff` 比较结构化快照，适合确认最终状态；两者应配合使用。

## 建模与调试阶段

### 1. 先检查模型

调用 `diana_model_summary` 查看：

- 工程和分析名称；
- 几何、材料、截面、荷载和支承；
- 网格节点/单元数量；
- 单位制；
- `validateModel('ALL')` 返回的问题；
- 与当前问题相关的手册或经验条目。

修改参数时，应先用 `diana_parameters` 获取实际可用路径。`setParameter()` 的可写路径是模型目录以下的路径，例如 `LINEAR/ELASTI/YOUNG`，不是 `availableParameters()` 返回的完整根路径。

### 2. 追踪修改

- 即时操作记录：`diana_changes`；
- 保存当前状态：`diana_model_snapshot(label="before-mesh")`；
- 比较状态：`diana_model_diff(before="latest", after="current")`；
- 查看原始 DianaIE 调用：`diana_command_log`。

视图旋转、选择和显示方式等纯视图操作默认不会污染结构化模型差异。

### 3. 提交人工审阅

可直接调用 `diana_solve(analysis="...")`，也可以显式调用 `diana_review_prepare`。两者在没有授权 ID 时都不会启动求解器。

审阅面板应至少核对：

- 几何尺寸、坐标系和单位；
- 材料、截面、单元类型与指派；
- 荷载工况、组合、边界条件和连接；
- 网格尺寸、局部细化和异常单元；
- 分析类型、步长、收敛准则和求解器；
- 本轮相对上次审阅的变化；
- 完整 `.dcf` 分析命令；
- 模型校验警告及其处理理由。

人可以拒绝、仅批准一次验证计算，或批准为基准候选。决定由 DianaIE 原生窗口写入，MCP 无法替人点击批准。

### 4. 验证并建立基准

审批后：

1. 调用 `diana_review_status(id="review-...")` 确认决定；
2. 使用相同 `review_id` 调用 `diana_solve`；
3. 用 `diana_job_status` 跟踪进度；
4. 用 `diana_read_out` 检查收敛、错误和终止状态；
5. 用 `diana_read_tb` 或 `diana_results_table` 检查关键结果；
6. 需要可视化时用 `diana_load_results` 和 `diana_plot_result`；
7. 只有计算成功且质量检查通过，基准候选才会晋升为可信 `baseline_id`。

模型在审批后若发生变化，状态指纹和输入文件哈希会失效，旧授权不能用于新模型。

## 参数分析阶段

### 1. 定义参数方案

使用 `diana_parameter_study_prepare`。每个参数需要唯一名称和模型路径，并指定连续范围或离散值：

```json
{
  "baseline_id": "baseline-...",
  "name": "mesh-and-young-modulus",
  "parameters": [
    {"name": "mesh_size", "path": "MESH/ELEMENT_SIZE", "values": [0.05, 0.075, 0.1]},
    {"name": "young", "path": "MATERIAL/Concrete/LINEAR/ELASTI/YOUNG", "min": 25000, "max": 35000}
  ],
  "combination": "full-factorial",
  "self_checks": ["模型校验无错误", "网格非空", "荷载和支承集合存在"],
  "quality_checks": ["正常终止", "所有增量收敛", "关键响应在工程允许范围内"]
}
```

路径名称必须与实际快照和审阅面板中显示的一致。示例只是格式说明，不代表任何工程的正确参数。

### 2. 一次性人工批准方案

参数范围、组合方式、自检和质量规则会显示在 DianaIE 审阅面板中。批准后取得 `campaign_id`。批准的是明确的参数包络，不是无限制自动修改权。

### 3. 自动运行变体

对 GUI 中生成的变体调用 `diana_solve`，并传入：

- `campaign_id`；
- 完整 `case_parameters`；
- 当前分析名称。

每个变体仍需通过：参数范围检查、相对基准的变化检查、提交前自检和求解后质量检查，但不再逐案弹出人工审阅窗口。

对于已生成的 `.dat/.dcf`，可以调用 `diana_run_solver` 快速求解。它只接受：

- 与归档基准逐字节一致的输入和 `baseline_id`；或
- 属于已批准参数方案的输入、`campaign_id`、完整参数值和语义 `change_manifest`。

因此，`diana_run_solver` 是可信模型的批量求解通道，不是绕过 GUI 审阅的建模通道。

## 结果处理

- `diana_job_status`：后台任务、步骤进度、收敛和输出文件；
- `diana_read_out`：解析 `.out` 的错误、警告、步、模态和安全系数；
- `diana_read_tb`：按结果、步、工况、模态和 ID 筛选 `.tb`，也可导出 CSV；
- `diana_results_table`：从 DianaIE 的 `.dnb` 结果读取高精度表格；
- `diana_plot_result`：在 GUI 中显示结果并返回截图；
- `diana_load_results`：把后台作业的 `.dnb` 加载到当前分析。

批量作业默认向副本 `.run.dcf` 注入 `LINPAG 0` 和 `COLLIN 1000`，避免 `.tb` 分页和窄行换行；不会修改原始 `.dcf`。

## 经验库的闭环

1. 检索：`diana_knowledge_search` 融合用户手册、关键字、API、经验和可选向量检索；
2. 反馈：用 `diana_knowledge_feedback` 标记有帮助或无帮助；
3. 起草：失败后成功、API 参数纠正或历史日志挖掘可自动形成草稿；
4. 复核：`diana_experience_review` 找重复、冲突和待批准项；
5. 验证：`diana_experience_verify` 运行条目附带的检查脚本；
6. 批准：只有人给出审批说明后，条目才能成为“已验证”；自动检查通过不等于人工批准；
7. 导出：`diana_experience_export` 生成 Markdown/HTML 经验手册。

真实项目路径、模型名称、公司信息和结果数据在加入公开知识库前必须匿名化。

## 结束会话

`diana_stop(save=true, close_diana=false)` 断开桥接但保留 DianaIE；只有明确希望关闭 GUI 时才设置 `close_diana=true`。任何可能丢失未保存工作的操作都应先由人确认。
