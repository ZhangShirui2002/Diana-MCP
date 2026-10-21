# DIANA-MCP

> 让支持 Model Context Protocol（MCP）的 AI 助手在人工监督下操作 DIANA FEA、读取求解结果，并从本地手册与经验库中检索知识。

[English](README.en.md) · [快速开始](docs/zh/quickstart.md) · [操作指南](docs/zh/user-guide.md) · [配置参考](docs/zh/configuration.md) · [架构说明](docs/zh/architecture.md)

DIANA-MCP 连接正在运行的 DianaIE 图形界面，也可以调用后台 `diana.exe` 求解器。它支持建模、网格、分析设置、GUI 修改跟踪、结果解析、手册检索和可审批的经验沉淀。项目不包含 DIANA 软件、许可证或用户手册；运行时只读取用户本机合法安装的 DIANA 文件。

> [!IMPORTANT]
> 本项目目前在 DIANA 10.5 上开发和验证，不隶属于 DIANA FEA BV。使用者必须自行取得有效的 DIANA 许可证，并对模型、参数和分析结论负责。

## 主要能力

- 在 DianaIE 的 GUI 主线程中执行 DIANA Python 命令，AI 和用户看到同一个模型。
- 识别用户在 GUI 中的修改、撤销/重做和参数新旧值。
- 在提交调试计算前强制显示完整模型与求解设置，由人工在 DianaIE 中批准。
- 将通过验证的模型认证为基准模型，再对批准范围内的参数变体自动计算。
- 后台运行 `diana.exe`，跟踪增量步、收敛状态、错误和输出文件。
- 读取 `.out`、`.tb` 和 DianaIE 已加载结果；大表可直接导出 CSV。
- 检索本地 DIANA 用户手册、验证算例、SCHEMA 关键字、脚本 API 和经验手册。
- 自动起草建模经验，但只有用户明确批准后才标记为“已验证”。
- 可选 bge-m3 本地语义检索；模型、查询和索引均不上传。

## 快速开始

### 1. 准备环境

- Windows 10/11，或能够运行 DIANA 的 Linux/Wine 环境；
- DIANA 10.5 与有效许可证；
- Python 3.10 或更高版本；
- 支持 MCP stdio 服务的客户端，如 Codex、Claude Code、Claude Desktop 或 Cursor。

### 2. 安装

```powershell
git clone https://github.com/YOUR_ACCOUNT/diana-mcp.git
Set-Location diana-mcp
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
```

如果需要语义检索：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[vector]"
```

### 3. 配置 MCP 客户端

先取得虚拟环境 Python 的绝对路径：

```powershell
(Resolve-Path .\.venv\Scripts\python.exe).Path
```

复制 [.mcp.example.json](.mcp.example.json) 为 `.mcp.json`，把其中的 `command` 改成上述路径。`.mcp.json` 可能包含本机路径，已默认排除在 Git 之外。

### 4. 连接 DianaIE

推荐启用自动连接：

```powershell
.\.venv\Scripts\python.exe -m diana_mcp autostart enable
.\.venv\Scripts\python.exe -m diana_mcp autostart status
```

重新启动 DianaIE 和 MCP 客户端，然后让 AI 依次调用：

```text
diana_status
diana_start
diana_guide(topic="workflow")
```

完整安装、首个模型和故障排查见[快速开始](docs/zh/quickstart.md)。

## 安全的计算工作流

```text
建模与调试
    ↓
模型/单位/网格/荷载/求解设置自检
    ↓
DianaIE 完整展示 + 人工批准
    ↓
一次验证求解
    ↓
正常结束、无错误、全部增量步收敛
    ↓
可信基准模型
    ↓
人工审批参数范围、自检和质量规则
    ↓
AI 自动执行批准范围内的参数分析
```

所有求解入口最终都必须消费一个与 `.dat`、`.dcf` 文件哈希绑定的一次性许可。直接通过 `diana_execute` 调用 `runSolver` 等命令会被阻止。详见[建模与调试阶段](docs/zh/user-guide.md#建模与调试阶段)和[参数分析阶段](docs/zh/user-guide.md#参数分析阶段)。

## 文档

| 文档 | 用途 |
|---|---|
| [快速开始](docs/zh/quickstart.md) | 从克隆仓库到连接 DianaIE 的首次成功流程 |
| [操作指南](docs/zh/user-guide.md) | 日常建模、人工审阅、基准认证、参数分析和结果读取 |
| [配置参考](docs/zh/configuration.md) | MCP 配置、环境变量、单机/远端拆分部署和向量检索 |
| [工具参考](docs/zh/tools.md) | 41 个 MCP 工具的用途和分组 |
| [架构说明](docs/zh/architecture.md) | Bridge、求解作业、知识库、状态目录和安全边界 |
| [安全与隐私](docs/zh/security-and-privacy.md) | 威胁模型、敏感文件和公开仓库检查清单 |
| [维护者发布指南](docs/zh/publishing.md) | 初始化 Git、创建 GitHub 仓库、打标签和发布版本 |
| [DIANA 手册功能复查](docs/manual-audit.md) | TABULA 输出及 DIANA 功能覆盖审计 |

## 知识库与经验手册

知识库默认是用户本地数据，不随源码发布。初始化一个空经验库：

```powershell
.\.venv\Scripts\python.exe -m diana_mcp knowledge init
.\.venv\Scripts\python.exe -m diana_mcp knowledge status
```

历史 `.out` 日志、项目路径和经验条目可能包含敏感工程信息。公开前应单独审查；建议把公开经验库作为独立仓库维护。详见[安全与隐私](docs/zh/security-and-privacy.md)。

## 开发与测试

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -m "not live"
```

真实 DIANA 端到端测试会启动 DianaIE 并占用许可证：

```powershell
$env:DIANA_MCP_LIVE_TESTS = "1"
.\.venv\Scripts\python.exe -m pytest tests\test_live.py
```

贡献前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。

## 许可证

本项目采用 [Apache License 2.0](LICENSE)。该许可证只覆盖本仓库中项目贡献者有权授权的代码和文档，不授予 DIANA 软件、用户手册、商标或客户数据的任何权利。第三方与商标说明见 [NOTICE.md](NOTICE.md)。

## 商标与第三方内容

DIANA 是其权利人的产品和商标。本项目是独立的互操作工具，不包含、修改或再分发 DIANA 可执行文件、许可证、用户手册或其他专有内容。更多说明见 [NOTICE.md](NOTICE.md)。
