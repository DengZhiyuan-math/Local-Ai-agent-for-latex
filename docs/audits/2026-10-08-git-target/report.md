# DeepCode 推送目标诊断

日期：2026-10-08。已提交基线：`9c2f0ef28b2d0827272e746b58c157be7c94441c`。工作区存在其他未提交适配修改，本次没有更改这些文件。与本次目标选择相关的 `gitsync.py`、`hub.py`、`registry.py` 与基线相同。

## 当前能够确定的结论

不能仅凭截图认定“论文被推送到工具仓库”。截图中的 Agent 明确在做工具开发：修改 `backend_codex.py`、`tests/test_agent.py` 和 README，提交标题为增加编辑器编译工具，并将本地提交 rebase 到工具仓库 main。截图列出的是工具仓库的权限查询失败，并提出取得权限后的推送方案；没有展示实际推送命令的执行结果，也没有展示论文内容。

如果原始任务就是开发工具，这个推送目标是合理的。如果原始任务是修改论文，则 Agent 的工作对象已经偏离论文，需查明项目目录或恢复会话为何指向工具。给该账号增加工具仓库写权限，不会解决工作对象错误。

用户已确认后端为 DeepCode。尚缺：是否从 Prism 论文面板启动、原始任务、当时项目目录和实际工具调用日志。因此不能将下面夹具中的原因直接认定为此次事件根因。

## 已确认的程序行为

| 路径 | 代码行为 | 意义 |
| --- | --- | --- |
| 编辑器启动 DeepCode | `cwd=job.root`；root来自当前项目服务 | 没有硬编码把工作目录设为工具代码目录 |
| 编辑器同步 | 存在 upstream 时执行不指定 remote 的 `git push -q` | 最终目标受完整 Git 配置影响 |
| 界面 GitHub 链接 | 读取 `git remote get-url origin` | 不一定等于最终 push URL |
| AI Git 权限 | 公共指令禁止 commit/push，环境限制 Git transport 和 gh登录 | Agent 不应承担论文同步；环境限制不是不可绕过的权限边界 |
| 论文目录位于普通工具仓库内 | GitSync 拒绝自动同步该嵌套目录 | 普通 Git 命令仍会向父目录发现工具仓库 |

程序没有单独读取一个 `prism.json` 论文 URL 来覆盖 Git 目标。项目 Git 目录、remote、分支配置和启动环境是目标选择的实际来源。Home 中设置 GitHub owner 或创建仓库，不会成为每次 push 的强制目标。

源码：`backend_deepcode.py` 的 `_run_cli()`、`backends.py` 的 `agent_env()`、`agent.py` 的 `start()`、`server.py` 的 `github_url()`、`gitsync.py` 的 `check_repo()` 和 `push()`。

## 本地已复现的三种路径

全部测试使用临时夹具。push仅为 dry-run，两个模拟远端均为本机 bare 仓库，结束时确认所有远端 refs仍为空。没有访问 GitHub，也没有请求 DeepCode模型。

### A. 界面显示论文 origin，但同步选择工具仓库

真实调用 `GitSync.push()`，仅将它内部的 push 替换为 dry-run，并移除静默参数以观察目标。其他选择逻辑保持原样。

夹具配置：论文有独立 `.git`；origin 指向 paper.git；upstream 为 origin/main；配置 `branch.main.pushRemote=tool`，tool 指向 latex-ai-agent.git。

结果：

```text
界面 origin: paper.git
upstream: origin/main
程序原始命令: git push -q
dry-run: To <fixture>/latex-ai-agent.git
程序 reported_error: null
```

保持 origin URL不变，改用 `remote.origin.pushurl=latex-ai-agent.git`，同样复现。

**分类：配置差异可以触发程序的目标校验和显示缺口。** 对 Git 本身，这是合法的配置行为；对“界面展示论文仓库、自动保存却去别处”的编辑器契约，这是可复现的程序问题。它不是当前截图已证明的根因。

Git 官方说明，不带 remote 的 push可以由 branch.pushRemote改变目标；remote.pushurl可与 fetch URL不同。[git-push](https://git-scm.com/docs/git-push)、[git-config](https://git-scm.com/docs/git-config)

### B. 论文目录没有自己的 Git，位于工具仓库内部

夹具目录：

```text
latex-ai-agent/           ← 工具的 .git 和 origin
└── papers/paper/         ← 论文目录，没有自己的 .git
```

在论文目录运行 Git，会发现上层工具仓库。Agent可读取到工具 origin。编辑器 GitSync.check_repo() 返回 false，不会为该目录自动同步。

**分类：目录/仓库布局错误，伴随 Agent上下文隔离缺口。** 已存在的自动同步保护生效，不能说编辑器自动同步必定会把这种目录推送到工具仓库。[Git 仓库发现规则](https://git-scm.com/docs/git)

### C. 继承的 GIT_DIR改变仓库识别

夹具给论文建立正确的独立 Git和 origin，再将子进程的 `GIT_DIR`、`GIT_WORK_TREE` 指向工具仓库。`agent_env()` 保留这两个变量。

结果：即使 cwd仍是论文目录，子进程查询到的 origin仍是工具仓库。

**分类：启动环境污染可以触发程序的环境隔离缺口。** 已复现只读仓库识别错误，没有尝试突破 Git transport限制进行推送。[Git环境变量规则](https://git-scm.com/docs/git)

## DeepCode会话还需要检查的证据

适配器根据论文 root读取 `~/.deepcode/projects/<project_code>/` 的会话索引，并将前端提供的 session_id传给 `--resume`。源码没有看到显式将论文 session换成工具 session的逻辑。本次未安装真实 DeepCode，也未获取当时的会话文件，不能证明发生了会话串用。

如果截图来自论文面板，应收集：

1. 编辑器 `/api/ping` 返回的 root，以及服务日志启动时的 project。
2. 该次调用的 cwd和 session_id；DeepCode记录的原始用户任务及 bash调用。
3. 论文目录的 Git top、origin fetch/push URL、upstream和 pushRemote/pushDefault配置来源。
4. 启动进程是否带有 GIT_DIR/GIT_WORK_TREE，以及 DeepCode用户/项目设置中的env和外部MCP配置。

DeepCode支持用户及项目配置中的自定义环境变量，因此仅检查 shell环境还不足以排除运行时配置污染。这是检查项，不是已确认的本次原因。[DeepCode配置文档](https://github.com/lessweb/deepcode-cli/blob/main/docs/configuration_en.md)

## 最小修复方向

- 编辑器统一解析“同步remote、目标分支、所有实际push URL”。界面展示相同结果。发现与项目预期目标不同则停止自动推送并给出原因。
- 不指定remote的裸push应改为已解析的明确目标。但只写 `git push origin` 仍不能解决origin.pushurl或URL重写问题，不能作为完整修复。
- 项目服务和AI启动前清理非必要的仓库选择环境变量，重新验证实际Git top。共享仓库和Git worktree须保持支持，不能仅凭 `.git` 是否为目录判定。
- Agent继续只负责论文编辑。Git同步由编辑器完成。恢复会话前验证会话与当前项目的一致性。

不要通过硬编码工具仓库名称来阻止这个问题，也不要为论文任务申请工具仓库写权限。

## 复现命令

```sh
python3 docs/audits/2026-10-08-git-target/probe.py --assert-display-matches-push
```

当前代码终态为exit=1：

```text
AssertionError: branch.main.pushRemote: editor origin is paper, but GitSync push selects tool
```

不加该参数时输出诊断JSON。这个失败断言针对已复现的目标显示差异，不是对截图事件的根因判定。本次仅新增诊断材料，没有修改产品代码或推送任何改动。
