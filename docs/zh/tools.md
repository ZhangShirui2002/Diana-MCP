# 工具参考

当前版本在 `full` profile 下提供 46 个工具。工具描述和参数模式以 MCP 的 `tools/list` 返回为最终依据；本页用于快速定位。

## 会话、模型与追踪（13）

| 工具 | 类型 | 用途 |
|---|---|---|
| `diana_status` | 只读 | DIANA 安装、桥接会话、自动连接和缓存状态 |
| `diana_start` | 会话 | 启动 DianaIE 或附加现有桥接 |
| `diana_execute` | 写入/高级 | 在 GUI 主线程执行 DianaIE Python；禁止直接调用求解器入口 |
| `diana_screenshot` | 只读 | 截取当前 DianaIE 视图 |
| `diana_model_summary` | 只读 | 模型、单位、网格、分析和校验问题摘要 |
| `diana_parameters` | 只读 | 查询材料、几何、荷载等对象的参数和值 |
| `diana_analysis_commands` | 只读 | 查看分析命令树和导出的 `.dcf` |
| `diana_command_log` | 只读 | 查看 DianaIE 调用历史 |
| `diana_changes` | 只读 | 识别用户与 AI 的增量操作并处理撤销/重做 |
| `diana_model_snapshot` | 写入元数据 | 保存当前或已保存工程的结构化快照 |
| `diana_model_diff` | 只读 | 比较两个快照或快照与当前模型 |
| `diana_autostart` | 配置 | 查询、启用或禁用 DianaIE 自动桥接 |
| `diana_stop` | 破坏性 | 断开桥接，可选保存并关闭 DianaIE |

`diana_execute` 能修改模型，属于专家接口。应优先查询 API 文档、检查返回值并用快照验证结果。服务器会拦截已知的直接求解 API，所有求解必须经过审阅门禁。

## 人工审阅、求解与结果（12）

| 工具 | 类型 | 用途 |
|---|---|---|
| `diana_review_prepare` | 门禁 | 完整展示模型和 `.dcf`，在 GUI 打开人工审阅面板 |
| `diana_review_status` | 状态 | 查询审阅、基准和参数方案；不能代替人审批 |
| `diana_parameter_study_prepare` | 门禁 | 创建参数包络、自检和质量规则，并请求一次性人工批准 |
| `diana_solve` | 求解 | 从当前 GUI 模型提交验证、基准或批准的参数变体 |
| `diana_run_solver` | 求解 | 对可信基准或批准变体的 `.dat/.dcf` 快速批处理 |
| `diana_job_status` | 只读 | 后台作业、进度、收敛、输出和学习建议 |
| `diana_job_cancel` | 破坏性 | 终止 `diana.exe` 及其子进程 |
| `diana_load_results` | GUI 写入 | 把 `.dnb` 加载到当前分析 |
| `diana_plot_result` | 只读视图 | 选择并显示结果，可返回截图 |
| `diana_results_table` | 只读 | 通过 DianaIE 读取高精度结果表，可筛选和导出 CSV |
| `diana_read_out` | 只读 | 解析 `.out` 的步骤、收敛、错误、模态和安全系数 |
| `diana_read_tb` | 只读 | 索引/筛选 `.tb`，处理无分页、组合、极值、坐标和状态文本 |

授权规则：

- `diana_solve` 不带 ID 时只准备审阅，不求解；
- 开发阶段使用 GUI 批准的 `review_id`；
- 可信模型可使用 `baseline_id`；
- 参数变体使用已批准的 `campaign_id` 和完整 `case_parameters`；
- `diana_run_solver` 必须且只能提供 `baseline_id` 或 `campaign_id`；
- 每次真正启动进程前都会生成与输入绑定的一次性 permit。

## 知识与文档（7）

| 工具 | 类型 | 用途 |
|---|---|---|
| `diana_guide` | 只读 | 当前服务器的工作流、脚本规则、追踪和知识库指南 |
| `diana_keywords` | 只读 | 查询 DIANA 数据文件关键字与 schema |
| `diana_glossary` | 只读 | 中英文界面术语查询 |
| `diana_api_search` | 只读 | 搜索 DianaIE Python API |
| `diana_api_doc` | 只读 | 读取某个 API 的签名和文档 |
| `diana_manual_search` | 只读 | 在本机 DIANA 用户手册索引中检索 |
| `diana_manual_read` | 只读 | 读取命中的手册页片段 |

手册内容只在用户机器上建立缓存，不作为项目源码发布。

## 经验库与融合检索（9）

| 工具 | 类型 | 用途 |
|---|---|---|
| `diana_knowledge_search` | 只读 | 融合手册、关键字、API、经验和向量召回 |
| `diana_knowledge_feedback` | 写入元数据 | 记录命中是否有帮助，改进经验排序 |
| `diana_experience_add` | 写入知识库 | 新建草稿；近重复默认拒绝 |
| `diana_experience_update` | 写入知识库 | 编辑、加证据或在人工批准后改状态 |
| `diana_experience_list` | 只读 | 按状态、类型等列经验和使用情况 |
| `diana_experience_mine_logs` | 写入知识库 | 从本地或 `ssh://` 的 `.out` 提取未验证草稿 |
| `diana_experience_verify` | 执行检查 | 在批处理 DianaIE 中运行条目验证脚本 |
| `diana_experience_review` | 只读 | 查找重复、冲突、待复验和待批准条目 |
| `diana_experience_export` | 写文件 | 导出 HTML/Markdown 经验手册 |

自动验证通过只会写入检查记录，不会自动把草稿改成“已验证”。只有用户明确批准并留下审批说明，才能改变该状态。

## 社区知识网络（5）

| 工具 | 类型 | 用途 |
|---|---|---|
| `diana_community_status` | 只读 | 查看缓存 commit、可信度统计和可脱敏的本地候选；不联网 |
| `diana_community_sync` | 网络/本地写入 | 显式克隆或更新公共社区仓库 |
| `diana_community_prepare` | 本地写入 | 生成独立的脱敏候选批次，不上传、不修改私有经验 |
| `diana_community_preview` | 只读 | 显示完整公开候选、替换统计和语义泄露提醒 |
| `diana_community_contribute` | 公开外部写入 | 仅在确认短语等于 `UPLOAD <batch-id>` 时推送贡献分支 |

社区同步和贡献都不是后台隐式操作。社区条目保留仓库、commit、状态和可信度；`candidate` 只能作为线索，不能冒充本地已验证经验。

## Profile 与工具归属

`knowledge` profile 包含本页“知识与文档”“经验库与融合检索”和“社区知识网络”的 21 个工具。其余 25 个属于 `compute` profile。`full` 同时注册两组。
