# -*- coding: utf-8 -*-
"""
校园政策问答 CLI

用法:
  python ask.py "国家奖学金的申请条件"
  python ask.py "电瓶车通行证怎么办理" -n 3
  python ask.py "宿舍用电" --no-llm      只检索，不调模型

LLM 配置见 llm.py（环境变量或 config.json）
"""
import sys, os, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rag import Retriever, dept_name
import llm
import server as app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="+")
    ap.add_argument("-n", "--top", type=int, default=5)
    ap.add_argument("--no-llm", action="store_true")
    a = ap.parse_args()
    q = " ".join(a.question)

    if not a.no_llm:
        res = app.answer(q)
        print("=" * 70)
        print("问题:", q)
        print("模式:", res.get("mode", "retrieval"))
        print("答案:", res.get("answer", ""))
        if res.get("notice"):
            print("提示:", res["notice"])
        for c in res.get("citations", []):
            print("[%d] 依据度 %.0f%% | %s" %
                  (c["no"], float(c.get("relevance", 0)) * 100, c.get("title", "")))
        return 0

    r = Retriever()
    hits = r.search(q, a.top)
    print("=" * 70)
    print("问题:", q)
    if not hits:
        print("结果: 现有文件中未涉及这个问题。")
        return 0
    print("命中: %d 份 | 首条依据度 %.0f%%%s"
          % (len(hits), hits[0]["coverage"] * 100,
             "" if hits[0]["coverage"] >= 0.45 else "（依据可能不足）"))
    print("=" * 70)
    for i, h in enumerate(hits, 1):
        c = h["chunk"]
        print("[%d] 综合分%.1f  依据度%.0f%%  %s" % (i, h["score"], h["coverage"] * 100, c["title"][:50]))
        # 手册源的 date 是编印年，不是发布年，不展示；网页源才显示日期
        dept = dept_name(c.get("dept", ""))
        if c.get("source") == "handbook":
            page = c.get("printed_page")
            tail = "新生手册 P%s" % page if page else "新生手册"
            row = [tail] if dept == "新生手册" else [dept, tail]
        else:
            row = [dept, c.get("date", ""), c.get("url", "")]
        print("     %s" % " | ".join(x for x in row if x))
    if hits[0]["expanded"]:
        print("同义词扩展:", "、".join(hits[0]["expanded"]))
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
