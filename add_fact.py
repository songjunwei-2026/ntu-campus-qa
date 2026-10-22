# -*- coding: utf-8 -*-
"""录入一条人工核实的事实。

用途：官网结构上抓不到、但又确实存在的信息。典型是这三类——
  · 站点是前端渲染（SPA），静态抓下来只有空壳；
  · 内容在附件 zip / doc 里，正文页只有一行"详见附件"；
  · 栏目返回 410，页面还在但取不到。

这些靠爬虫永远补不齐，人工抄一条最省事。原则是**宁缺勿假**：
没有明确出处的不要录，出处写不出 URL 的就写在 --dept 里（如"图书馆公众号 2026-09-01"）。

用法:
  python add_fact.py --title "啬园校区图书馆开放时间" \
      --text "啬园校区图书馆：周一至周日 8:00-22:00，考试周延长至 23:00。" \
      --dept "图书馆" --url "https://lib.ntu.edu.cn/..." --date "2026-10-21"

  python add_fact.py --list          # 看已录入的事实
  python add_fact.py --del 关键词    # 按标题关键词删除
"""
import hashlib
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

BASE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(BASE, "corpus", "facts_chunks.jsonl")


def _read():
    if not os.path.exists(PATH):
        return []
    out = []
    with open(PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _write(rows):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    with open(PATH, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _uid(title):
    return "fact_" + hashlib.md5(title.encode("utf-8")).hexdigest()[:12]


def add(title, text, dept, url, date):
    text = (text or "").strip()
    if not title or not text:
        print("× 标题和正文都不能为空")
        return 1
    if len(text) < 15:
        print("× 正文太短（至少 15 字），检索不会命中，请写完整一些")
        return 1

    rows = [r for r in _read() if r.get("title") != title]
    rows.append({
        "doc_uid": _uid(title),
        "chunk_id": 0,
        "source": "fact",
        "title": title,
        "date": date or "",
        "year": int(date[:4]) if date and date[:4].isdigit() else 0,
        "dept": dept or "人工核实",
        "tag": "人工核实",
        "url": url or "",
        "printed_page": 0,
        "text": text,
    })
    _write(rows)
    print("√ 已录入：%s" % title)
    print("  出处：%s%s" % (dept or "人工核实", (" " + url) if url else ""))
    print("  当前事实条数：%d" % len(rows))
    print("  （重启 server.py 后生效）")
    return 0


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if args[0] == "--list":
        rows = _read()
        if not rows:
            print("还没有录入任何事实（这是正常的：没核证过的不要写进来）")
            return 0
        print("共 %d 条：" % len(rows))
        for r in rows:
            print("  · %s\n    出处：%s %s\n    %s" % (
                r["title"], r.get("dept", ""), r.get("url", ""), r["text"][:60]))
        return 0
    if args[0] == "--del":
        if len(args) < 2:
            print("× 需要关键词，如 --del 图书馆")
            return 1
        kw = args[1]
        rows = _read()
        keep = [r for r in rows if kw not in r.get("title", "")]
        _write(keep)
        print("√ 删除 %d 条，剩 %d 条" % (len(rows) - len(keep), len(keep)))
        return 0

    # --key value 形式
    kv = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            k = a[2:]
            v = args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("--") else ""
            kv[k] = v
            i += 2 if v else 1
        else:
            i += 1
    return add(kv.get("title", ""), kv.get("text", ""),
               kv.get("dept", ""), kv.get("url", ""), kv.get("date", ""))


if __name__ == "__main__":
    sys.exit(main())
