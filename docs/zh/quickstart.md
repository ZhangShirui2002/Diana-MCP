# 快速上手

本教程在 Windows 上安装 DIANA-MCP，连接一个 DIANA 10.5 GUI 会话，并按强制人工审阅流程完成第一次计算。完成后，你将拥有一个可供 MCP 客户端调用的本地 DIANA 工具服务器。

## 1. 前置条件

- 已合法安装并可正常启动 DIANA 10.5；
- Python 3.10 或更高版本；
- Git；
- 支持本地 `stdio` MCP 服务器的客户端，例如 Codex 或 Claude Code。

DIANA-MCP 不包含 DIANA、许可证、用户手册或求解器。使用者负责遵守 DIANA 的许可条款。

## 2. 安装

下面的 `YOUR_ACCOUNT` 是占位符，请替换为发布仓库所属的 GitHub 用户名或组织名。

```powershell
git clone https://github.com/YOUR_ACCOUNT/diana-mcp.git
Set-Location diana-mcp
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev,vector]"
```

确认安装成功：

```powershell
.\.venv\Scripts\python.exe -c "import diana_mcp; print(diana_mcp.__version__)"
.\.venv\Scripts\python.exe -c "from diana_mcp.config import default_install; print(default_install().home)"
```

如果第二条命令没有找到 DIANA，请在 MCP 配置中设置：

```json
"env": {
  "DIANA_MCP_DIANA_HOME": "C:\\Program Files\\Diana 10.5"
}
```

## 3. 配置 MCP 客户端

复制仓库根目录的 `.mcp.example.json` 为客户端使用的配置文件，并将 `command` 改为本机虚拟环境 Python 的绝对路径：

```json
{
  "mcpServers": {
    "diana": {
      "type": "stdio",
      "command": "C:\\work\\diana-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "diana_mcp"]
    }
  }
}
```

不同 MCP 客户端的配置入口不同，但服务器启动方式相同：标准输入输出、命令为虚拟环境 Python、参数为 `-m diana_mcp`。修改配置后重启或重新连接 MCP 服务器。

## 4. 启用 DianaIE 自动连接

```powershell
.\.venv\Scripts\python.exe -m diana_mcp autostart enable
.\.venv\Scripts\python.exe -m diana_mcp autostart status
```

然后完全退出并重新启动 DianaIE。自动连接只安装一个 Python 启动钩子；它不会自动打开工程或提交计算。

如果不希望永久启用，可在 DianaIE 的 Python Console 中按 `diana_start` 返回的命令手动加载桥接脚本。

## 5. 第一次连接

让 MCP 客户端依次调用：

1. `diana_status`：检查 DIANA 安装、桥接会话、知识库和向量索引；
2. `diana_start`：如果尚未连接，则启动或附加 DianaIE；
3. `diana_guide(topic="workflow")`：读取当前版本的工作流规则；
4. `diana_model_summary`：检查当前工程、分析、网格和校验问题。

成功连接时，`diana_status` 会报告一个可用会话。DIANA GUI 仍由人控制；MCP 只通过本机回环地址与当前 DianaIE 会话通信。

## 6. 第一次受控计算

DIANA-MCP 不允许未经授权直接提交开发阶段模型。调用：

```text
diana_solve(analysis="Analysis 1")
```

此时不会启动求解器，而会：

1. 在 DianaIE 中完整显示并适配模型；
2. 导出当前模型快照和完整分析命令；
3. 打开由 DianaIE 持有的审阅面板；
4. 等待人点击“仅批准本次验证”或“批准为基准模型”。

关闭窗口不等于批准。完成审阅后，先调用 `diana_review_status`，再把批准记录的 `review_id` 传给 `diana_solve`：

```text
diana_solve(analysis="Analysis 1", review_id="review-...")
```

若选择了“批准为基准模型”，它仍只是候选基准；只有验证计算成功并通过求解后质量检查，才成为可信基准。

## 7. 初始化本地经验库

```powershell
.\.venv\Scripts\python.exe -m diana_mcp knowledge init
.\.venv\Scripts\python.exe -m diana_mcp knowledge status
```

经验库默认位于源码检出目录的 `knowledge\`，它是独立 Git 仓库且被主仓库忽略。自动发现的经验始终先成为草稿，必须经人工确认才能标记为“已验证”。

## 8. 运行测试

```powershell
.\.venv\Scripts\python.exe -m pytest -m "not live"
```

真机测试会启动真实 DIANA，不适合作为普通 CI：

```powershell
$env:DIANA_MCP_LIVE_TESTS = "1"
.\.venv\Scripts\python.exe -m pytest -m live
```

下一步请阅读[操作指南](user-guide.md)。
