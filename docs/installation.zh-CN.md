# 中文快速安装

[README](../README.md) · [完整安装说明（英文）](installation.md) · [页面演示](demos/README.md)

Prism 在本机浏览器中提供 LaTeX 编辑器、PDF 预览、项目管理和 AI 助手。
论文保存在自己选择的普通文件夹里。可以先完成编辑和编译，再配置 GitHub 或 AI。

## 1. 安装并打开 Prism

**Windows：**从开始菜单打开 **PowerShell**，粘贴下面的命令并回车：

```powershell
irm https://raw.githubusercontent.com/DengZhiyuan-math/Local-Ai-agent-for-latex/main/install.ps1 | iex
```

**macOS / Linux：**打开**终端**，运行：

```sh
curl -fsSL https://raw.githubusercontent.com/DengZhiyuan-math/Local-Ai-agent-for-latex/main/install.sh | sh
```

命令会下载并执行仓库中的安装脚本。也可以先查看
[Windows 脚本](../install.ps1)、[macOS/Linux 脚本](../install.sh)，或按
[手动安装说明](installation.md#manual-installation)操作。

安装器检查 Python 3.9+ 和 git、下载 Prism、创建启动入口并打开项目首页。
Windows 会尝试安装缺少的工具；macOS 如弹出开发者工具安装窗口，按提示完成。
Linux 如果提示缺少依赖，先执行它打印的系统安装命令，再重新运行上面的命令。
Prism 本身不需要运行 `pip install` 或 `npm install`。

## 2. 准备 LaTeX

在首页找到 **Getting started（开始使用）**，或打开 **Settings → Programs（设置 → 程序）**。

- 已检测到 MiKTeX、MacTeX 或 TeX Live：直接使用现有安装。
- **Quick start（快速开始）**：下载 Tectonic，首次编译会联网下载所需宏包。
- **Full LaTeX（完整 LaTeX）**：适合需要常规 TeX 发行版的用户；Windows 可安装
  MiKTeX，其他平台按页面提示安装。下载可能达到数 GB。

运行编辑器需要 Python；生成 PDF 还需要 LaTeX。Git 用于本地版本历史。
GitHub 登录、GitHub CLI 和 AI 服务都是后续可选配置。

## 3. 编译第一篇文档

1. 点击首页的 **+ New project（新建项目）**，填写文件夹名，例如 `my-first-paper`，
   并选择论文保存位置。建议将论文保存在 Prism 安装目录以外。
2. 选择 **Article（文章）** 或 **Math paper（数学论文）** 模板。
   暂时不勾选 **Create a private GitHub repository（创建私有 GitHub 仓库）**，点击 **Create**。
3. 在编辑器中打开 `main.tex`，修改标题或正文，点击 **Compile（编译）**。
   快捷键：Windows/Linux 为 Ctrl+Enter，macOS 为 Cmd+Enter。
4. 右侧应出现 PDF。双击 PDF 中的文字可以跳到源文件；在编辑器里按 Ctrl+J / Cmd+J 可以反向定位。

已有论文：点击 **Add existing…（添加已有项目）**，选择论文所在文件夹。
想先试用示例：将 [`examples/minimal`](../examples/minimal) 复制到自己的论文目录，再添加副本。

中文文档还需要相应的中文宏包和字体。使用 `ctex`、`xeCJK`、`fontspec` 时，Prism
通常会自动选择 XeLaTeX；也可以在 `prism.json` 中明确指定 `"engine": "xelatex"`。
Debian/Ubuntu 的 TeX Live 中文宏包为 `texlive-lang-chinese`。

## 4. 下次打开、更新和排查问题

| 操作 | 方法 |
| --- | --- |
| Windows 打开 | 开始菜单或桌面的 **Prism** 快捷方式 |
| macOS 打开 | Applications 或 Spotlight 中的 **prism-local** |
| Linux 打开 | 应用菜单中的 **Prism** |
| 更新 | 重新运行安装命令，然后关闭并重新打开 Prism |
| 编译失败 | 查看编辑器底部的 **Problems（问题）** 和 **Output（输出）** |
| 首页打不开 | 按[手动启动说明](installation.md#3-start-the-home-page)运行，并打开终端打印的网址 |
| 端口不可用 | 手动启动时加 `--port 0`，打开终端打印的新地址 |

保留安装目录和 Python，快捷方式需要它们。编辑器和已准备好宏包的 TeX 可以离线使用；
在线 AI、GitHub 同步和下载依赖需要网络。更完整的问题排查见
[Installation and troubleshooting](installation.md#troubleshooting)。
