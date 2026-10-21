# GitHub 发布指南

本指南供项目维护者把当前工作目录发布为新的公共 GitHub 仓库。它刻意把“准备”“审计”“创建远程仓库”和“正式发布”分开，任何一步发现敏感数据都应停下处理。

## 0. 发布前决定

在首个公开 commit/tag 前必须完成：

- 确认已选择的 Apache-2.0 `LICENSE` 和 `NOTICE.md` 内容正确；
- 确认项目代码权属允许公开；
- 决定 GitHub 用户/组织和仓库名；
- 确认本机 `knowledge\` 不随主仓库公开；
- 确认没有客户模型、DIANA 专有内容或真实基础设施信息。

许可证决策记录见[许可证选择](license-selection.md)。

## 1. 做本地备份

根目录当前尚未依赖 Git 历史保护，初始化前先把整个项目目录复制到仅自己可访问的位置。特别保留：

- 本机 `.mcp.json`；
- 独立的 `knowledge\.git`；
- `models\` 中已下载的向量模型；
- 状态目录中的审阅和基准记录。

这些文件用于本机恢复，不应进入公共仓库。

## 2. 运行质量检查

```powershell
# 将此路径替换为你的实际项目根目录
Set-Location C:\path\to\diana-mcp
.\.venv\Scripts\python.exe -m pytest -m "not live"
.\.venv\Scripts\python.exe -m diana_mcp knowledge validate
```

第二条检查的是本机私有经验库；它不会把经验库加入主仓库。若准备发布公开经验库，应另做一次逐条隐私审查。

检查文档链接和占位符：

```powershell
rg -n "YOUR_ACCOUNT|C:\\path\\to|user@diana-compute-host|/path/to" README* docs .mcp*.json
```

这些值在模板和发布指南中可以保留，但正式 GitHub URL 一旦确定，应把 README 的 `YOUR_ACCOUNT` 替换为真实账号。

## 3. 初始化主 Git 仓库

不要进入 `knowledge\` 执行下面的命令；它是另一个仓库。

```powershell
# 将此路径替换为你的实际项目根目录
Set-Location C:\path\to\diana-mcp
git init -b main
git add .
git status --short
```

确认本机内容确实被忽略：

```powershell
git check-ignore -v .mcp.json .codex .scratch .add-staging knowledge models
```

如果任何敏感目录没有输出对应的 `.gitignore` 规则，不要继续提交。

## 4. 审查暂存区

查看将被公开的完整文件列表：

```powershell
git diff --cached --name-only
git diff --cached --stat
git diff --cached --check
```

搜索常见秘密和内部地址：

```powershell
git grep --cached -n -I -E "BEGIN (RSA|OPENSSH|EC|DSA) PRIVATE KEY|password|secret|api[_-]?key"
git grep --cached -n -I -E "192\.168\.|10\.[0-9]+\.|172\.(1[6-9]|2[0-9]|3[01])\."
```

检查大文件：

```powershell
git diff --cached --name-only | ForEach-Object {
  $item = Get-Item -LiteralPath $_ -ErrorAction SilentlyContinue
  if ($item -and $item.Length -gt 10MB) { $item | Select-Object FullName, Length }
}
```

逐项确认以下目录没有出现在暂存区：

- `.mcp.json`、`.codex\`、`.scratch\`、`.add-staging\`；
- `knowledge\`、`models\`；
- 生成的 `.dpf/.dat/.dcf/.out/.tb/.dnb/.ff`；
- 与项目无关的私人研究笔记。

## 5. 首次提交

许可证和审计都完成后：

```powershell
git commit -m "Initial open-source release"
```

提交后再次确认：

```powershell
git status
git ls-files
```

由于这是第一段 Git 历史，如果发现秘密，最稳妥的方式是删除本地 `.git` 并从清洁文件重新初始化，而不是创建一个后续删除 commit。执行此类操作前应确认备份和目标路径。

## 6. 创建 GitHub 仓库

先安装并登录 GitHub CLI：

```powershell
gh auth status
```

设置变量。下面两项都是示例，必须替换：

```powershell
$GitHubOwner = "your-account-or-organization"
$RepositoryName = "diana-mcp"
```

建议先创建 private 仓库完成一次远程预演：

```powershell
gh repo create "$GitHubOwner/$RepositoryName" --private --source . --remote origin
git push -u origin main
```

在 GitHub 网页复核文件列表、语言统计、README、许可证识别和安全设置。确认无误后再把仓库改为 public：

```powershell
gh repo edit "$GitHubOwner/$RepositoryName" --visibility public --accept-visibility-change-consequences
```

如果项目所有者明确希望直接公开，可把 `--private` 换成 `--public`，但会失去远程预演缓冲。

## 7. GitHub 仓库设置

建议配置：

- About：`MCP server for human-supervised DIANA FEA workflows`；
- Topics：`mcp`、`finite-element-analysis`、`diana-fea`、`python`、`engineering`；
- 默认分支 `main`；
- 合并前要求 CI 通过；
- 至少一个 pull request 审核；
- 禁止强制推送到 `main`；
- 启用 Dependabot alerts、Secret scanning 和 Private vulnerability reporting；
- Issues 与 Discussions 是否开放由维护能力决定。

`SECURITY.md` 中的占位说明应在启用私密漏洞报告后更新为实际入口。

## 8. 创建 v0.9.0 Release

先检查版本一致：

```powershell
rg -n "0\.9\.0" pyproject.toml src\diana_mcp\__init__.py CHANGELOG.md docs\release-notes-v0.9.0.md
```

创建带注释 tag 并推送：

```powershell
git tag -a v0.9.0 -m "DIANA-MCP v0.9.0"
git push origin v0.9.0
```

用已准备的发布说明创建 GitHub Release：

```powershell
gh release create v0.9.0 --title "DIANA-MCP v0.9.0" --notes-file docs\release-notes-v0.9.0.md
```

本项目尚未配置 PyPI 发布流程。GitHub Release 和源码安装足以完成首发；不要在没有包签名、构建验证和许可证元数据时匆忙上传 PyPI。

## 9. 发布后验证

在一个全新目录做用户视角验证：

```powershell
git clone "https://github.com/$GitHubOwner/$RepositoryName.git" diana-mcp-clean
Set-Location diana-mcp-clean
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -m "not live"
```

再确认：

- 文档中所有相对链接可打开；
- `.mcp.example.json` 不含私人信息；
- 无 DIANA 安装文件或手册内容；
- 新用户能在不拥有你的状态目录、经验库或模型缓存时导入包；
- 有 DIANA 10.5 的隔离机器能连接并通过最小 live 测试。

## 10. 后续版本

每次发布都应：

1. 更新 `CHANGELOG.md`；
2. 增加对应 release notes；
3. 跑非真机测试和有针对性的 live 测试；
4. 重复暂存区秘密/大文件检查；
5. 先合并再打不可变 tag；
6. 不修改已经发布的 tag。
