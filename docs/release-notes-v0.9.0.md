# DIANA-MCP v0.9.0

DIANA-MCP v0.9.0 是首个计划公开发布的版本。它把 DianaIE 实时控制、后台求解、人工审阅门禁和本地自生长知识库整合为一套面向 DIANA 10.5 的 MCP 工具。

## 重点功能

- 通过本机桥接让 MCP 客户端操作正在运行的 DianaIE GUI；
- 结构化模型快照、差异比较和 GUI/AI 修改来源追踪；
- 开发阶段模型必须在 DianaIE 中完整展示并由人批准；
- 人工审阅 + 验证求解 + 质量检查后建立不可变基准模型；
- 一次性审批参数范围、自检与质量规则，随后自动运行参数变体；
- 所有求解入口使用与输入哈希绑定、只能消费一次的 permit；
- 后台运行 `diana.exe` 并解析 `.out`、`.tb`、`.dnb`；
- TABULA 默认使用 `LINPAG 0` 和 `COLLIN 1000`，避免分页与窄行换行；
- 本地检索 DIANA 手册、SCHEMA 关键字、DianaIE API、术语和经验；
- 经验自动起草、人工批准、可执行验证、反馈排序和手册导出；
- 可选 bge-m3 语义检索，支持 CPU 或 CUDA；
- `full`、`compute`、`knowledge` 三种部署 profile。

## 安全模型

- GUI 人工审批不能由 MCP 工具自行完成；
- `diana_execute` 中的已知直接求解 API 会被拦截；
- 审批后发生模型或分析命令变化会使授权失效；
- `diana_run_solver` 只能处理可信基准或批准参数方案中的变体；
- 自动发现和自动验证的经验不会自动成为“已验证”；
- 项目不包含 DIANA、许可证、用户手册、客户模型或嵌入模型权重。

## 安装

```powershell
git clone https://github.com/ZhangShirui2002/Diana-MCP.git
Set-Location diana-mcp
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

语义检索需要：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[vector]"
```

请从[快速上手](zh/quickstart.md)开始，并在首次建模前阅读[操作指南](zh/user-guide.md)。

## 兼容性

- 已验证：DIANA 10.5；
- 主 MCP 服务器：Python 3.10+；
- DianaIE 桥接：内嵌 Python 3.7；
- 主要开发平台：Windows；
- 支持实验性 Linux/Wine 与 SSH 拆分部署。

## 已知限制

- 尚未承诺其他 DIANA 版本兼容性；
- 部分纯 GUI 交互无法通过公开 DianaIE API 完整表达；
- 用户子程序编译仍依赖用户安装的 Intel Fortran 工具链；
- `.dna` 文本结果未解析，优先使用 `.dnb`/DianaIE 结果 API；
- 语义模型需要单独下载，不随仓库发布；
- 真机测试需要 DIANA 许可证，不在普通公共 CI 中运行；
- 用户仍需对有限元模型、参数和结论承担工程责任。

## 升级说明

从早期开发版本升级后应：

1. 重新连接 MCP 服务器以加载 41 个工具；
2. 重新检查 `diana_autostart` 状态；
3. 确认向量配置中的模型和 `DIANA_MCP_VECTOR_DEVICE`；
4. 按新门禁流程重新建立可信 `baseline_id`，旧的非门禁作业不能自动取得基准资格；
5. 保留本机 `knowledge\` 和状态目录，不要把它们提交到公共仓库。

## 完整变更记录

见 [CHANGELOG.md](../CHANGELOG.md)。
