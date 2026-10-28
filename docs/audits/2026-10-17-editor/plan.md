# 编辑器的基本功能：方案

日期：2026-10-17。来源：你说"编辑器中没有搜索等基本功能"。核对后：

- 编辑器里按 Ctrl-F 会出现 CodeMirror 自带的一行输入框（`vendor/search.js`），但没有按钮，只在 PDF 按钮的提示文字里提了一句；替换（Shift-Ctrl-F）要一个一个弹窗确认；没有计数，没有大小写和整词选项。
- 没有在整个项目里搜索、跳到行、字数统计。
- PDF 的搜索已有（`pdfview.js`），不动。

你选定四项：文件内查找替换、全项目搜索（含替换）、跳到行、字数统计。执行结果写入同目录的 `report.md`。

## 7.1 文件内查找和替换（`findlogic.js` + `app.js`）

- 编辑器右上角浮动一条查找栏，样式与 PDF 的查找栏一致：查找框、计数（"3 / 17"，没有时 "No results"）、上一个、下一个、三个开关（区分大小写 Aa、整词 ab、正则 .*）、展开替换的箭头、关闭。替换行：替换框、"Replace"、"Replace all"。
- 打开：Ctrl-F / ⌘F（编辑器有焦点，或 PDF 不是最后点的那一边时；PDF 那边照旧搜 PDF）；Ctrl-H / ⌘⌥F 打开并展开替换。选中的一行文字自动填入查找框。编辑器头部加一个搜索按钮。
- 键：Enter 下一个，Shift-Enter 上一个，F3 / Shift-F3 也可以；Esc 关闭并把焦点还给编辑器；Alt-C / Alt-W / Alt-R 切换三个开关；替换框里 Enter 替换当前，Ctrl-Alt-Enter 全部替换。
- 所有匹配高亮（overlay），当前匹配另一种颜色。输入时即时查找，从光标处往后找第一个。
- 正则写错时，查找框标红，计数处说"Invalid pattern"，不抛错。
- 替换支持 `$1`…`$9`、`$&`、`$$`（只在正则模式下有意义），与 JavaScript 的 `String.replace` 相同。全部替换是一次操作，Ctrl-Z 一次就撤回。
- 切换标签页时，查找栏保持打开，计数按新文件重算。
- 原来的 `vendor/search.js` 不再加载（它的 Ctrl-F、Ctrl-G 被新的取代）；`searchcursor.js` 继续用。
- 纯逻辑放在新模块 `findlogic.js`（由查找条件生成正则、替换文本的展开、在一段文字里列出全部匹配），在 Node 下检查。

## 7.2 全项目搜索和替换

- 侧栏 Files 标题旁加搜索按钮；Ctrl-Shift-F / ⌘⇧F 打开。打开后侧栏换成搜索面板（Files 和 Outline 暂时隐藏，× 关闭回来）。
- 面板：查找框、三个开关（同 7.1）、可展开的替换框和"Replace all"、结果按文件分组，每个文件可折叠，显示命中行（匹配部分加粗，长行截在匹配附近），文件后面显示命中数；顶部一句"23 results in 5 files"。点一条结果打开该文件，跳到并选中那个匹配。
- 搜索的范围：项目里所有可编辑的文本文件（与文件树相同，`.tex/.bib/.md/.sty/.cls/…`），不含构建目录和被排除的文件夹。
- 内容从哪里来：已打开的标签页用编辑器里的当前内容（包括还没保存的）；其他文件用新的 `POST /api/texts {paths}` 读取，页面按文件树给出的 mtime 缓存，只重读改过的。一个文件超过 2 MB 不读，结果里说一句。查找和替换都在页面里用同一个 JavaScript 正则完成，所以看到的命中和替换掉的一定是同一批。
- 结果数上限 2000 条，超过时说"只显示前 2000 条"。
- 替换全部：先确认"把 5 个文件里的 23 处替换成 …？"。已打开的文件在编辑器里替换（可 Ctrl-Z，随后自动保存）；没打开的文件用现有的保存接口写回，带读到时的 mtime，期间被别人改过的文件不写，结果里说明是哪几个。agent 的 Edit 回合进行中时不替换，说明原因（与保存的规则相同）。
- 文件树刷新（每次 poll）后，面板开着时，结果按新内容重算。

## 7.3 跳到行

- Ctrl-G / ⌘G（Mac 上 VS Code 也是 Ctrl-G，两者都接）打开一个小输入框"Go to line"，可写 `120` 或 `120:5`（行:列）；超出范围时落到最后一行。Enter 跳转并把那一行放到中间，Esc 取消。
- 编辑器底部状态栏的 "Ln 12, Col 5" 点一下也打开它。

## 7.4 状态栏和字数统计

- 编辑器底部一条细状态栏：左边 "Ln 12, Col 5"（有选择时加 "(34 selected)"）；右边字数："1,234 words"（这个文件），文件属于正在编译的文档时再加 "· 8,765 in the document"。悬停给出规则说明。
- 字数规则（与 texcount 的默认相近，不追求一致）：
  - 去掉注释（未转义的 `%` 到行尾），只数 `\begin{document}` 之后（文件里有它时）；
  - 数学不算字：`$…$`、`$$…$$`、`\(…\)`、`\[…\]`，以及 equation、align、gather、multline、eqnarray、math、displaymath 等环境（含 `*`）；
  - 命令名不算；这些命令的参数也不算：`\label \ref \eqref \cref \Cref \autoref \pageref \cite* \input \include \includegraphics \usepackage \documentclass \bibliography \bibliographystyle \begin \end \newcommand \renewcommand \def \url \href 的第一个参数`；其他命令的参数照算（`\section{Intro}`、`\emph{x}`）；
  - 一个"字"是连续的字母或数字（Unicode），可带内部的 `'` 和 `-`；中日韩文字每个字算一个字。
  - Markdown 文件：去掉代码块和数学后数字。
- 有选择时，状态栏也给出选中部分的字数。
- 整篇文档的字数：文档的文件是 tree 给出的 `order`（主文件及其 `\input`/`\include` 链），内容来源同 7.2。在空闲时（输入停下 1 秒后）重算。
- 逻辑在 `findlogic.js` 里的 `wordCount(text, kind)`，Node 检查覆盖上述每条规则。

## 测试

- `tests/frontend_check.js`：`findlogic.js` 的正则生成（三个开关的组合、非法正则）、替换展开、匹配列表（零宽匹配不死循环）、字数的每条规则。
- `tests/test_server.py`：`/api/texts`：只给可编辑文件、项目外和被排除的路径拒绝、过大的文件不给内容、不存在的文件不报错只省略。
- `tests/client_routes.js`：新增的 `Q.texts`。
- 浏览器（新脚本 `tests/browser/editing.mjs`，在 `run.py` 里登记）：Ctrl-F 打开、计数、Enter 换到下一个、全部替换后 Ctrl-Z 一次复原；全项目搜索找到未打开文件里的命中，点结果打开并选中；全项目替换写回未打开的文件；Ctrl-G 跳行；状态栏的行列和字数。

## 顺序

```
7.1（含 findlogic.js）→ 7.2（含 /api/texts）→ 7.3 → 7.4 → README 的编辑器一节
```

每项一个提交；最后跑完整回归（unittest、Node 检查、浏览器），写 report.md。

## 不做

- 拼写检查、多光标（CodeMirror 已有 Ctrl-点击多光标，不另加界面）。
- 在 PDF 里替换；在 agent 的写入视图里查找。
