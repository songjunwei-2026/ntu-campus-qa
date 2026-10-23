# CODEX_TASK_6 — T13 补齐页面 head 元信息（favicon / 分享卡片 / 描述）

> 本文件替代 `CODEX_TASK_5.md`。本单**只有一个任务（T13）**，做完就停，不要顺手做别的。

---

## 0. 这一单的性质

项目 `E:\desktop\ntu` 是南通大学校园政策问答网站。现在页面 `<head>` 缺三样东西，导致：

- 发到班级群 / QQ 群，链接只显示一个**没有图标、没有描述**的灰块（`<title>` 只有"通晓"两个字）
- 浏览器标签页没有图标

本单就是补这三样 + 推进版本号。**纯静态改动，不涉及任何 JS 逻辑、不涉及后端。**

**你只需要改 2 个地方、跑 1 条命令、按格式汇报。** 不许重构，不许"优化"其他代码，不许动颜色变量。

---

## 1. 环境（2026-10-23 起可用）

本机 Python 已经装好了：

```
D:\Python\python.exe      # Python 3.13.17，已在 PATH
```

所以 `python xxx.py` 可以直接跑。如果遇到 `CreateProcessAsUserW failed: 2`，改成 `cmd /c "python xxx.py"`。

git commit / push **不要做**：`.git` 目录在你的沙箱里是只读的，必然 `Permission denied`，白花时间。提交由人工完成。

---

## 2. 改动（只有这两处）

文件：`E:\desktop\ntu\web\index.html`

### 改动 1：第 6 行，把 `<title>` 那一行替换成下面 7 行

原文（第 6 行，只有这一行）：

```
<title>通晓</title>
```

替换成（**共 7 行，逐字照抄，一个字符都不许增删**）：

```
<title>通晓 · 南通大学校园政策问答</title>
<meta name="description" content="南通大学校园政策问答：收录校内文件原文，每条回答标出发文部门与日期，点开可核对。查不到会直接说查不到。">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='4' fill='%230B3B72'/%3E%3Crect x='6' y='9' width='20' height='3.4' fill='%23FFFFFF' opacity='.92'/%3E%3Crect x='6' y='16' width='20' height='3.4' fill='%23FFFFFF' opacity='.5'/%3E%3Crect x='19' y='22' width='7' height='6' fill='%23C0111D'/%3E%3C/svg%3E">
<meta property="og:type" content="website">
<meta property="og:site_name" content="通晓">
<meta property="og:title" content="通晓 · 南通大学校园政策问答">
<meta property="og:description" content="收录校内文件原文，每条回答标部门与日期，点开可核对。">
```

第 3 行那个 `data:` URI 是**内联 SVG 图标**（深蓝底 + 两道白杠 + 右下红块），刻意用 `%23` 代替 `#`、用 `%3C` `%3E` 代替 `<` `>`。**不要"美化"它，不要换行，不要改引号**——改成别的写法就会失效。

### 改动 2：footer 里的版本号

搜 `版本 2026-10-23-22`，全文只有 1 处，改成 `版本 2026-10-23-23`。

**只替换这 10 个字符里的 `22`，该行其他内容（包括后面的 `</span>`）一个字都不要动。**

> 这正是上次出事的地方：你把 `版本 2026-10-23-21</span>` 改成了 `版本 2026-10-23-22an>`，`</sp` 被吞掉。这次验收脚本会查标签配对，吞了会直接 FAIL。

---

## 3. 验收（原样跑，贴原始输出）

```
cd E:\desktop\ntu
python tools\check_page.py
```

**这是本单唯一的验收命令。** 它用标准库 `html.parser` 解析整页，检查 22 项：head 元信息、CSS 变量、标签配对、被吞标签残渣。

- 全部 `PASS` + 最后一行 `结果：全部通过` + 退出码 `0` → 通过
- 任何一行 `FAIL` → 按它的提示回去改，改完重跑，直到全 PASS

预期输出（你可以对照）：

```
PASS  HTML 可被解析
PASS  title 非空且含品牌名   实际='通晓 · 南通大学校园政策问答'
PASS  description 恰好 1 条   实际=1
PASS  description 长度 40-120 字   实际=51 字
PASS  存在 og:type   值='website'
PASS  存在 og:title   值='通晓 · 南通大学校园政策问答'
PASS  存在 og:description   值='收录校内文件原文，每条回答标部门与日期，点开可核对。'
PASS  存在 og:site_name   值='通晓'
PASS  icon 恰好 1 条   实际=1
PASS  icon 是 svg data URI   前缀='data:image/svg+xml,%3Csvg '
PASS  icon 序列完整   结尾='/%3E%3C/svg%3E'
PASS  icon 内无裸 # 号
PASS  版本号出现且仅 1 处   实际=['2026-10-23-23']
PASS  CSS 变量 11 个齐全
PASS  标签配对 <html>   1 开 / 1 闭
PASS  标签配对 <head>   1 开 / 1 闭
PASS  标签配对 <body>   1 开 / 1 闭
PASS  标签配对 <div>   62 开 / 62 闭
PASS  标签配对 <span>   30 开 / 30 闭
PASS  标签配对 <footer>   1 开 / 1 闭
PASS  标签配对 <style>   1 开 / 1 闭
PASS  标签配对 <script>   3 开 / 3 闭
PASS  标签配对 <main>   2 开 / 2 闭
PASS  </html> 恰好 1 次   实际=1
PASS  无被吞标签残渣（如 22an>）   命中的=[]

版本号 = 2026-10-23-23
结果：全部通过
```

（上面列了 26 行 PASS，脚本自身按 26 项计，以实际输出为准。）

再跑一条，确认改动范围：

```
git diff --stat
```

**必须正好是：**

```
 web/index.html | 10 ++++++++--
 1 file changed, 8 insertions(+), 2 deletions(-)
```

如果不是 8 增 2 删，说明你动多了（最常见的是在文件末尾多加了空行，或者把某一行拆成了两行）。自己查回来。

最后确认后端没被带坏：

```
python test_rules.py
```

**必须正好是 `28/28 passed`。**

---

## 4. 汇报格式

只写这四段，不要写总结、不要写"已完成优化"之类的话：

```
【1】check_page.py 输出
<把 26 行输出原样贴上来>

【2】git diff --stat 输出
<原样>

【3】test_rules.py 最后一行
<原样>

【4】失败情况
<哪一条没跑成，贴完整报错原文；全部成功就写"无">
```

任何一步没跑成，写 `未执行 + 报错原文`。**不许写"通过"，不许编数字。**

---

## 5. 明确禁止

- 不许改 `web/index.html` 以外的任何文件（除本任务单外）
- 不许改 CSS 变量、示例问题、JS 函数、后端 `server.py`
- 不许动 `corpus/`、`docs/`、`prompts/`
- 不许 `git commit`、`git push`、`git pull`（沙箱写不了 `.git`，必失败）
- 不许重写整个 `index.html`（825 行，重写必丢东西）
- 不许"顺手"把 `--ink-3` 之类的变量改个值

---

## 6. 背景（只读，不需要你处理）

- 项目当前 main = `a28423d`，语料 3180 块 / 612 份文档，`test_rules.py` 28 条用例
- 前端全部逻辑在 `web/index.html` 单文件里，无构建步骤、无框架、无 npm
- 浏览器兼容要求：**禁止 `??`、`?.`、`flatMap`**（旧内核白屏）
