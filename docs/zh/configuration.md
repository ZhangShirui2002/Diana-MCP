# 配置参考

## 本机单服务器

默认 `full` 配置同时提供 DIANA 控制、求解和知识库工具。复制 `.mcp.example.json`，只需修改 Python 绝对路径：

```json
{
  "mcpServers": {
    "diana": {
      "type": "stdio",
      "command": "C:\\work\\diana-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "diana_mcp"],
      "env": {
        "DIANA_MCP_DIANA_HOME": "C:\\Program Files\\Diana 10.5"
      }
    }
  }
}
```

`command` 必须使用安装了本项目的 Python，不能使用 DianaIE 内嵌的 Python 3.7。后者只负责 GUI 内部桥接。

## 计算端与知识端拆分

`.mcp.remote.example.json` 展示了两个服务器：

- `diana`：在安装 DIANA 的远程主机上以 `compute` 模式运行；
- `diana-knowledge`：在本机以 `knowledge` 模式运行，保存手册索引、经验库和向量模型。

三个 profile 的工具范围：

| 值 | 用途 | 工具数 |
|---|---|---:|
| `full` | 单机部署，包含全部能力 | 41 |
| `compute` | DianaIE、模型、审阅门禁、求解和结果 | 25 |
| `knowledge` | 手册、API、关键字、术语和经验 | 16 |

远程模板中的主机名、用户名、私钥、Wine 前缀、DISPLAY、工作目录和 Python 路径都必须替换。不要把真实私钥路径或内网地址提交到公共仓库。

## 向量检索与 GPU

安装向量依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[vector]"
```

若使用 NVIDIA GPU，应先按照 PyTorch 官方说明安装与驱动匹配的 CUDA 版本，再安装本项目。环境变量示例：

```json
"env": {
  "DIANA_MCP_VECTOR_MODEL": "sentence-transformers:BAAI/bge-m3",
  "DIANA_MCP_VECTOR_DEVICE": "cuda"
}
```

建立或刷新索引：

```powershell
.\.venv\Scripts\python.exe -m diana_mcp knowledge vector-index --model sentence-transformers:BAAI/bge-m3 --device cuda
.\.venv\Scripts\python.exe -m diana_mcp knowledge vector-status
```

模型权重和索引默认写入源码检出目录的 `models\`；该目录被主仓库忽略。发布包和 GitHub 仓库不应携带数 GB 的模型文件。

`DIANA_MCP_VECTOR_DEVICE` 控制查询时把嵌入模型放在哪个设备上。未设置时默认为 `cpu`，因此“查询设备是 CPU”表示配置选择，并不表示系统未检测到独立显卡。使用 `cuda` 前应确认：

```powershell
.\.venv\Scripts\python.exe -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')"
```

## 自动连接

```powershell
.\.venv\Scripts\python.exe -m diana_mcp autostart enable
.\.venv\Scripts\python.exe -m diana_mcp autostart status
.\.venv\Scripts\python.exe -m diana_mcp autostart disable
```

启用后，新启动的 DianaIE 会自动加载桥接。临时跳过一次可在启动 DianaIE 前设置 `DIANA_MCP_AUTOSTART=0`。桥接只监听本机回环地址，并使用随机会话令牌。

## 环境变量

### 用户配置

| 变量 | 默认值 | 作用 |
|---|---|---|
| `DIANA_MCP_DIANA_HOME` | 自动搜索最新版 | 包含 `bin\DianaIE.exe` 的 DIANA 安装目录 |
| `DIANA_MCP_STATE_DIR` | `%LOCALAPPDATA%\diana-mcp` | 会话、快照、审阅记录、API/手册缓存和作业注册表 |
| `DIANA_MCP_WORKDIR` | `<state>\work` | 临时工作和默认批处理目录 |
| `DIANA_MCP_KNOWLEDGE_DIR` | 源码模式下为 `knowledge\` | 独立经验库 Git 仓库 |
| `DIANA_MCP_MODELS_DIR` | 源码模式下为 `models\` | 嵌入模型缓存和向量索引 |
| `DIANA_MCP_PROFILE` | `full` | `full`、`compute` 或 `knowledge` |
| `DIANA_MCP_VECTOR_MODEL` | 激活配置文件中的模型 | 向量模型 spec；`none`/`off` 禁用 |
| `DIANA_MCP_VECTOR_DEVICE` | `cpu` | 查询设备，例如 `cpu` 或 `cuda` |
| `DIANA_MCP_VECTOR_WEIGHT` | 内置权重 | 向量结果在融合排序中的权重 |
| `DIANA_MCP_DISABLE_RETRIEVERS` | 未设置 | 设置后关闭可插拔检索器 |
| `DIANA_MCP_RETRIEVERS` | Python entry point | 额外检索器工厂列表 |
| `DIANA_MCP_SSH_OPTIONS` | 空 | 远程日志挖掘的 SSH 参数；可含私钥路径 |
| `DIANA_MCP_WINE` | `wine` | Linux/Wine 上使用的 Wine 命令 |
| `DIANA_MCP_AUTOSTART` | `1` | 设为 `0` 时跳过一次 DianaIE 自动桥接 |
| `DIANA_MCP_LIVE_TESTS` | 未设置 | 设为 `1` 才允许运行真机测试 |

### 内部变量

`DIANA_MCP_PORT`、`DIANA_MCP_TOKEN`、`DIANA_MCP_SESSION_DIR` 和 `DIANA_MCP_LAUNCH_ID` 由启动器传给 GUI 桥接，不应手工固定或写入公开配置。`DIANA_MCP_TEST_LOG` 仅供测试夹具使用。

## 数据目录

默认状态目录包含本机敏感信息：

- `sessions\`：活动桥接地址和短期令牌；
- `journals\`：GUI/AI 操作范围；
- `snapshots\`：结构化模型快照；
- `review-gate\`：审阅、基准、参数方案和一次性许可；
- `jobs\`：任务注册表与保留的输入；
- `api\`、`manual\`：从本机安装生成的索引；
- `vector.json`：激活的向量模型和查询设备。

这些内容都不应提交到公共仓库。备份时应按工程保密等级管理。

## 客户端兼容性

DIANA-MCP 使用标准 `stdio` MCP 协议。Codex、Claude Code 以及其他允许配置本地 `stdio` MCP 服务器的客户端原则上都可使用；客户端必须支持工具调用和较长运行任务。某个产品是否可用取决于它是否开放自定义 MCP 配置，不依赖 DIANA-MCP 对品牌做专门适配。
