# 安全与隐私

DIANA-MCP 能修改有限元模型、执行 Python、启动求解器并读取工程结果，权限接近操作 DIANA 的本地用户。部署和公开仓库都应采用最小权限与人工审阅。

## 信任边界

- MCP 客户端能请求工具，但不能在 DianaIE 审阅面板中替人批准；
- GUI 桥接仅监听 `127.0.0.1`，请求必须携带随机令牌；
- 直接求解 API 被拦截，后台求解必须消费一次性 permit；
- 基准输入使用 SHA-256 绑定，参数变体受 campaign 包络约束；
- 知识条目的自动验证不能替代人工批准；
- DIANA 许可证和操作系统权限仍由 DIANA 与用户环境负责。

桥接令牌用于本机进程间鉴别，不应当作跨网络长期凭据。不要把 bridge 端口直接暴露到局域网或互联网。

## 哪些数据可能敏感

| 位置/文件 | 可能包含 |
|---|---|
| `.mcp.json`、客户端配置 | 用户名、主机名、内网地址、SSH 私钥路径 |
| 状态目录 `sessions\` | 活动端口和短期随机令牌 |
| `journals\`、DianaIE history | 工程路径、API 参数和建模操作 |
| `snapshots\`、`review-gate\` | 模型对象、材料、荷载、分析设置和审批记录 |
| 作业目录 | `.dat/.dcf/.out/.tb/.dnb/.ff` 和完整工程结果 |
| `knowledge\` | 真实故障、证据路径、项目名称和审批人说明 |
| `models\` | 数 GB 第三方模型权重与向量索引 |

主仓库的 `.gitignore` 默认排除上述本机目录，但忽略规则不能替代发布前检查。

## 公开仓库不得包含

- DIANA 安装文件、可执行文件、DLL、许可证或许可证服务器信息；
- DIANA 用户手册原文、帮助数据库或未经授权的专有示例；
- 客户模型、坐标、材料数据、结果和截图；
- 真实内网 IP、远程主机、用户名和私钥路径；
- bridge 会话 JSON、令牌、DianaIE 历史和调试日志；
- 未经逐条审查的经验库；
- 下载的 bge-m3/PyTorch 模型或生成的向量索引。

手册检索代码可以公开，但手册内容应在最终用户机器上从其合法 DIANA 安装中建立索引。

## 发布前检查

在 `git add` 后检查的对象必须是“将被提交的文件”，而不只是工作目录：

```powershell
git status --short
git diff --cached --check
git diff --cached --name-only
git grep --cached -n -I -E "BEGIN (RSA|OPENSSH|EC|DSA) PRIVATE KEY|password|token|secret"
git grep --cached -n -I -E "192\.168\.|10\.[0-9]+\.|172\.(1[6-9]|2[0-9]|3[01])\."
```

命中 `token` 等普通术语不一定是秘密，应逐项判断；任何真实凭据都必须先从文件和 Git 历史中移除，再轮换凭据。不要只提交一个“删除秘密”的后续 commit，因为秘密仍会留在历史里。

检查大文件：

```powershell
git ls-files | ForEach-Object {
  $item = Get-Item -LiteralPath $_ -ErrorAction SilentlyContinue
  if ($item -and $item.Length -gt 10MB) { $item | Select-Object FullName, Length }
}
```

创建 GitHub 仓库后应启用：

- Private vulnerability reporting；
- Secret scanning（账户/仓库方案支持时）；
- Dependabot alerts；
- 分支保护和 pull request 审核。

## MCP 客户端和提示注入

DIANA 文件、手册片段、日志和经验条目都是数据，不是可信指令。客户端应避免执行这些内容中出现的命令。DIANA-MCP 自身只把它们作为检索结果返回；贡献者不得把项目文件中的文字直接拼成系统级授权。

## `diana_execute`

该工具执行任意 DianaIE Python，是最强权限接口：

- 只执行为当前建模任务所必需的代码；
- 不读取无关文件或环境变量；
- 不调用外部网络；
- 不使用会阻塞 GUI 的对话框；
- 保存、关闭、覆盖工程前必须确认；
- 修改后使用返回值、快照和模型校验交叉确认。

## 远程部署

- 使用专用低权限 SSH 账户；
- 私钥保留在系统密钥目录，不复制进项目；
- 使用 `BatchMode=yes`、`IdentitiesOnly=yes` 和主机密钥校验；
- 不把 bridge TCP 端口做远程转发；
- 为作业设置专用目录，不让 MCP 写入整个主目录；
- 明确远程 DIANA 许可证的使用范围。

## 经验库公开策略

建议把公开经验库做成单独仓库，只导入满足以下条件的条目：

1. 已去除工程名、单位名、人员名和绝对路径；
2. 不含客户输入和专有结果；
3. 证据可公开或改为可复现的最小示例；
4. 状态和验证范围描述准确；
5. 由人逐条审核。

当前本机 `knowledge\` 是用户数据仓库，不应因主代码开源而自动公开。

## 漏洞报告

不要在公开 Issue 中发布可利用细节、令牌或工程文件。仓库创建后，维护者应按 [SECURITY.md](../../SECURITY.md) 启用 GitHub Private vulnerability reporting，并在该文件中补充实际联系方式。
