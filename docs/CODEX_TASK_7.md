# CODEX_TASK_7 — T14：反馈机制双模式 + 反馈查看接口

> 生成时间：2026-10-23 23:20
> 执行者：Codex
> 提交与推送：**不由 Codex 执行**（.git 只读，必失败，别再试）

---

## 0. 现状与自检命令

项目根目录：`E:\desktop\ntu`

开始前先跑一遍，确认基线：

```powershell
cd E:\desktop\ntu
python tools\check_feedback.py
```

**期望：`结果：11 项 PASS，8 项 FAIL`**

那 8 项 FAIL 就是你要修的：3 项在 `server.py`，5 项在 `web/index.html`。

如果 python 报 `CreateProcessAsUserW failed: 2`，改成 `cmd /c "python tools\check_feedback.py"`。

---

## 1. 这次要解决什么

现在只有「这条不准」一种反馈。但实测问「图书馆开放时间」时，页面回答「本地制度中没有收录」，再让用户点「这条不准」就不对了——答案并没有不准，它只是承认查不到。

而这类问题累计已经被问了 72 次，用户明明知道答案，页面却只让他「去咨询相关职能部门」，等于把信息丢掉。

所以补两件事：

1. **查不到时，按钮换成「我知道，我告诉你」**，让用户把正确答案写进来（用户补充的答案是补语料的直接来源）
2. **加一个只读接口** `/api/feedback/list`，能在浏览器里看别人反馈了什么

---

## 2. 四处改动（逐字照做，不许"优化"写法）

### 改动 1 —— `server.py`：新增读反馈的函数

在 `server.py` 里搜索这段（全文唯一）：

```python
def _feedback_total():
    try:
        with open(FEEDBACK_PATH, encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0
```

**整段替换为**（新函数插在前面，原 `_feedback_total` 一字不改）：

```python
def _feedback_items(limit=200):
    items = []
    try:
        with open(FEEDBACK_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    items.reverse()
    return items[:limit]


def _feedback_total():
    try:
        with open(FEEDBACK_PATH, encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0
```

函数名**必须**是 `_feedback_items`，校验脚本按这个名字找。

---

### 改动 2 —— `server.py`：新增只读路由

搜索这段（全文唯一）：

```python
        if path == "/api/misses":
            return self._send(200, {"items": weblearn.misses(),
                                    "total": weblearn.stats()["misses"]})
```

**替换为**（在原两行后面追加三行，注意 `if path == "/api/misses":` 的缩进是 8 个空格）：

```python
        if path == "/api/misses":
            return self._send(200, {"items": weblearn.misses(),
                                    "total": weblearn.stats()["misses"]})
        if path == "/api/feedback/list":
            items = _feedback_items()
            return self._send(200, {"items": items, "total": _feedback_total()})
```

放在这里是因为它在 `do_GET` 里、`if path != "/api/ask"` 那个兜底之前。

---

### 改动 3 —— `server.py`：写入时带上 kind

在 `do_POST` 里搜索这段（全文唯一，注意 `"reason"` 后面紧跟 `}`）：

```python
            row = {"q": q, "citation_titles": body["citation_titles"],
                   "reason": str(body.get("reason") or "").strip()}
```

**替换为**（只加一行，并把原来的 `}` 移到新行末）：

```python
            row = {"q": q, "citation_titles": body["citation_titles"],
                   "reason": str(body.get("reason") or "").strip(),
                   "kind": "answer" if body.get("kind") == "answer" else "wrong"}
```

白名单写法是刻意的：只有明确传 `"answer"` 才记成补充答案，其他任何值（包括不传、传 `"hack"`）一律降级为 `"wrong"`。不要改成 `body.get("kind")` 直接写入。

---

### 改动 4 —— `web/index.html`：`bindFeedback` 双模式

**只改 `bindFeedback` 这一个函数，不许碰 `render` 那一行（第 265 行，超长单行，最容易改坏）。**

在 `web/index.html` 里搜索这段（全文唯一）：

```javascript
if(!btn)return;btn.onclick=()=>{box.hidden=false;btn.disabled=true};
```

**替换为**（一行，不要换行、不要缩进）：

```javascript
if(!btn)return;if(!d.ok){btn.textContent="我知道，我告诉你";$("#feedbackReason").placeholder="请写下正确答案，或告诉我该查哪个文件（可选）"}btn.onclick=()=>{box.hidden=false;btn.disabled=true};
```

再搜索这段（全文唯一）：

```javascript
reason:$("#feedbackReason").value.trim()})});if(!r.ok)throw Error();statusEl.textContent="已反馈";send.hidden=true}
```

**替换为**（一行，不要换行）：

```javascript
reason:$("#feedbackReason").value.trim(),kind:d.ok?"wrong":"answer"})});if(!r.ok)throw Error();statusEl.textContent=d.ok?"已反馈":"已收到，谢谢补充";send.hidden=true}
```

**为什么不在 `render` 里改按钮文案**：`render` 是第 265 行一整条超长单行（HTML 模板全挤在一行里），改它极易把标签吞掉——2026-10-23 就发生过一次，把 `</span>` 改成了 `22an>`，而当时三条字符串计数检查全部通过、没人发现。这次在 `bindFeedback` 里用 `d.ok` 判断覆盖文案，效果完全一样，风险小一个数量级。

---

## 3. 验收（三条，原样跑，贴原始输出）

```powershell
cd E:\desktop\ntu
python tools\check_feedback.py
```

**期望：`结果：19 项 PASS，0 项 FAIL` + `全部通过`**，退出码 0。

```powershell
python tools\check_page.py
```

**期望：`结果：全部通过`**。这条查 HTML 标签配对、CSS 变量、有没有被吞的标签残渣。任何 FAIL 说明你改坏了结构。

```powershell
python test_rules.py
```

**期望：`28/28 passed`**。

补充检查（第三条，不算验收但要说）：

```powershell
git status --short
```

**期望：只有 `M server.py` 和 `M web/index.html` 两行**。如果出现第三个文件，说明你顺手改了别的，回退掉。

**不要核对 `git diff --stat` 的增删行数**。这次会因为写法差异（缩进、空行）对不上，以 `check_feedback.py` 的结果为准。

---

## 4. 禁止

- **不许 commit、不许 push、不许 pull。** 你的沙箱对 `.git` 只读，`git commit` 必然 `Permission denied`，这个结论已经在你机器上实测过三次，别再试。
- 不许改 `web/index.html` 的 `render` 函数（第 265 行整条超长单行）。
- 不许改版本号、不许动 footer。
- 不许改 CSS 变量、示例问题、`server.py` 里的检索逻辑。
- 不许让 `tools/check_feedback.py` 或 `tools/check_page.py` 通过而修改这两个脚本。它们是验收门槛，改它们等于作弊。
- 不许重写整个文件。`server.py` 1042 行、`web/index.html` 831 行，重写必丢东西。

---

## 5. 汇报格式

只写这四行，不加解释、不加总结：

```
1. check_feedback.py 最后两行
2. check_page.py 最后一行
3. test_rules.py 最后一行
4. git status --short 原样
```

没跑成的写 `未执行 + 报错原文`。不许写"通过"，不许编数字。
