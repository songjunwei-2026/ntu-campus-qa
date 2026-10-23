# Codex 任务单（2026-10-23 第四批：T6 / T7 / T8）

> 上一批 T3、T4、T5 已验收通过（`b8c98f8` / `0903c0f` / `d52ce06`），我另外修了一处
> 截断溢出（`537f3bd`）。本文件为当前批次唯一依据。

## 0. 开工前必做：先证明你能验证

上一批你汇报「eval.py --llm 连跑 3 轮均 20/20、20/20、5/5」，同时又说
「当前本机未配置可用模型调用，实际评测 token 为 0/0/0」。这两句话是矛盾的——
没有模型调用不可能产生答案层结果。我在本机重跑，token 是 in=94811 / out≈3000，
说明代码本身没问题，是你的环境没跑起来，但你把没跑的结果报成了通过。

**本批开始，先跑这三条，把原始输出贴进汇报里，一条都不许省略：**

```bash
# 1. 配置里到底有没有可用模型
python -c "import json;c=json.load(open('config.json',encoding='utf-8'));print(c.get('model'), (c.get('api_key') or 'MISSING')[:8])"

# 2. 真的能调通一次
python -c "import server,llm;r=server.answer('勤工助学怎么申请');print(r.get('mode'), r.get('usage'), len(r.get('answer') or ''))"

# 3. 基线没被改坏
python test_rules.py | tail -2
```

第 2 条如果打印出 `usage={'in': 0, 'cached': 0, 'out': 0}` 或报错，
**说明你环境里调不到模型**。这种情况下：
- 可以继续改代码
- 但凡涉及 `eval.py --llm` 的验收项，一律写「未验证（环境无法调用模型）」，
  **不许自己编数字**，也不许把「代码里看着对」当成通过

## 1. 两个已知现象，不要去修

- **偶发 10 秒耗时**：沙箱网络抖动，同一个问题单独复跑就回到 1.5s，与代码无关。
- **`prompt_cache_hit_tokens` 只有 384**：那是系统提示的固定前缀命中长度，正常。
  同一问题连问才会命中到 4000+。

## 2. T6 拦截规则外置

`server.py` 的 `_intent_mismatch` 现在有 5 组规则，全硬编码。抽成独立模块。

- 新建 `intent_rules.py`，导出 `RULES`：列表，每项是 dict：
  - `name`：英文标识，用于测试报错时定位
  - `ask`：正则字符串，命中表示"用户可能问的是这类"
  - `evidence`：列表，字符串按**包含**匹配，`re:` 前缀按**正则**匹配
  - `reason`：拦截时写进 `weblearn.log_miss` 的原因
- 文件顶部注释写明 `evidence` 两种匹配语义的区别
- `_intent_mismatch` 只负责遍历 `RULES`，`corpus` 拼装逻辑保留在原处
- **必须先备份当前行为**：动代码前先跑 `python test_rules.py` 记录 25/25，
  重构后再跑一次，必须还是 25/25

**验收**：`test_rules.py` 25/25；`eval.py` 纯检索 20/20；
额外写 3 条新用例加进 `test_rules.py`（只许新增，不许改已有的）。

## 3. T7 答案反馈闭环

现在只收「没答上」（`misses.jsonl`），收不到「答了但答错」，后者危害更大。

后端：
- `POST /api/feedback`，收 `{"q": str, "citation_titles": [str], "reason": str}`
- 追加写 `feedback.jsonl`（一行一条，UTF-8，`ensure_ascii=False`）
- 校验：`q` 为空或超长（>200 字）返回 400，`citation_titles` 不是列表返回 400
- `/api/status` 加 `feedback_total` 计数
- `feedback.jsonl` 加进 `.gitignore`

前端（`web/index.html`）：
- 答案卡片底部加一个「这条不准」按钮，点击后弹一个简单的多选或文本框收原因，
  提交后按钮变成「已反馈」，不要用 alert 阻塞
- 提交失败要有可见提示，不要静默失败
- 版本号往后加一位

**验收**：
1. `curl` 发一次，`feedback.jsonl` 多一行且是合法 JSON
2. 空 `q` 返回 400
3. `/api/status` 的 `feedback_total` +1
4. 页面上点一次按钮，网络面板能看到 200，按钮状态变化

## 4. T8 `add_fact.py --edit`

- 加 `--edit "标题关键词" --text "新内容"`
- 唯一匹配才更新；匹配到多条时报错并**列出全部候选标题**；匹配不到报「未找到」
- 更新时保留原有的 `doc_uid` / `source` / `dept` / `tag`，只改 `text`
- 更新后打印改前改后的字数
- `--list` 要能显示更新后的内容

**验收**：`--edit` 改一条 → `--list` 显示新内容；用不存在的标题 → 明确报错且不写文件；
用一个能匹配多条的模糊词 → 报错并列出候选。

## 5. 硬约束（违反即失败）

- 不许改 `eval.py` 的 `CASES` 列表
- `test_rules.py` 只许**新增**用例，不许删改已有用例，不许放宽断言
- 不许改 `corpus/` 下任何文件的内容
- 不许新增第三方依赖，只用标准库
- 不许用 `git add -f` 绕过 `.gitignore`
- 不许重开 thinking 模式
- 不许为了省 token 减少资料块数量
- 不许新增无条件拒答规则
- 不许为 10 秒抖动改任何超时参数

## 6. 不许回退的地方

- `server.py` 的 `_truncate_answer`：正文只能用 `limit-2` 字符，给 `……` 留位。
  上一批你写成 `text[:limit]`，补完省略号变成 402 字，超出上限，已由我修正。
- `_intent_mismatch` 里军训/寒暑假/报到的规则：必须同时满足「问时间」+「语料缺完整月日」，
  不许改回无条件拦截。
- `llm.py` 的 `"thinking": {"type": "disabled"}` 与空正文重试。

## 7. 汇报格式

1. 第 0 节三条命令的**原始输出**（不许改写、不许只说"正常"）
2. 三项各自的验收数据，逐条对应上面的「验收」
3. `git log --oneline -5`
4. 三个 commit 分别改了哪些文件
5. 有没有碰到硬约束，碰到就直说
6. 哪些验收项是「未验证」，单独列出来，不要混在通过项里

## 8. 不要做的事

- 不要动 `_intent_mismatch` 里已改好的规则语义
- 不要给 `llm.py` 再加 prompt 规则（已有 11 条，再加挤占资料空间）
- 不要给缓存引入 redis / sqlite / 文件持久化
- 不要顺手重构 `server.py` 其他部分
- 不要为了让测试通过而放宽断言或加硬编码特判
