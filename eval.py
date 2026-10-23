# -*- coding: utf-8 -*-
"""
问答质量评测

旧版只看"top1 文本里有没有出现关键词"，一句"国家奖学金"就能把答错的答案判成对。
这版分两层判定：
  检索层  首条命中的文件对不对（按标题判，不看正文，防止正文顺带提到关键词蒙混）
  答案层  答案里有没有出现该题必须给出的要素（数字、比例、处分种类等）
用法:
  python eval.py          只跑检索层（快，不花钱）
  python eval.py --llm    连大模型一起跑（每题约 2 秒）
"""
import os, sys, time, re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

# (问题, 标题关键词, 答案必需要素, 是否可答)
CASES = [
    ("国家奖学金的申请条件", ["奖学金评定条例"], ["10%", "10％", "8000"], 1),
    ("二等奖学金的金额是多少", ["奖学金评定条例"], ["1500", "800"], 1),
    ("电瓶车通行证怎么办理", ["电动自行车", "交通管理"], ["通行证"], 1),
    ("校园卡丢了怎么补办", ["校园卡"], ["补卡"], 1),
    ("宿舍能用多大功率的电器", ["用电器", "安全用电"], ["电器"], 1),
    ("考试作弊怎么处理", ["课程考核", "违纪处分", "纪律处分"], ["记过"], 1),
    ("请假超过三天找谁批", ["考勤"], ["请假", "考勤"], 1),
    ("转专业需要什么条件", ["转专业"], ["转专业"], 1),
    ("勤工助学怎么申请", ["勤工助学"], ["勤工助学"], 1),
    ("学生申诉的流程是什么", ["申诉"], ["申诉"], 1),
    ("学士学位授予条件", ["学位授予"], ["学位"], 1),
    ("学分绩点怎么计算", ["学籍管理", "学分绩"], ["绩点"], 1),
    ("实验室安全有哪些要求", ["实验室安全"], ["实验室"], 1),
    ("校园网账号怎么开通", ["校园网"], ["校园网"], 1),
    ("国家助学贷款怎么申请", ["助学贷款"], ["助学贷款"], 1),
    ("学生违纪处分有哪几种", ["违纪处分", "纪律处分"], ["警告", "记过"], 1),
    ("校徽可以借给别人吗", ["校徽"], ["校徽"], 1),
    # —— 官网固定页已入库（fetch_fixed.py），走本地检索 ——
    # 注意：校长是王建浦，党委书记是杨宇民。2026 年已换任，
    # 网上大量旧稿仍写"校长杨宇民"，这条必须按官网「现任领导」页为准。
    ("南通大学校长是谁", ["现任领导"], ["王建浦"], 1),
    ("学校有几个校区", ["学校简介"], ["3个", "三个", "3 个"], 1),
    ("第一节课几点上课", ["作息时间"], ["7:50"], 1),
    # —— 本地库里确实没有，正确行为是拒答 ——
    ("食堂几点开门", [], [], 0),
    ("图书馆开放时间", [], [], 0),
    ("学费一年多少钱", [], [], 0),
    ("校车时刻表", [], [], 0),
    ("2026年校历什么时候开学", [], [], 0),
]


def main():
    use_llm = "--llm" in sys.argv
    import server

    n_doc = sum(1 for c in CASES if c[3] and c[1])          # 本地语料能检索到
    n_web = sum(1 for c in CASES if c[3] and not c[1])      # 只能靠官网
    n_ans = n_doc + n_web if use_llm else 0
    n_ref = sum(1 for c in CASES if not c[3]) if use_llm else 0
    doc_ok = ans_ok = ref_ok = src_ok = 0
    total = 0.0
    usage_total = {"in": 0, "cached": 0, "out": 0}

    def take_usage(res):
        usage = res.get("usage") or {}
        values = {k: int(usage.get(k) or 0) for k in usage_total}
        for key, value in values.items():
            usage_total[key] += value
        return values

    def usage_text(values):
        return "in=%d/cached=%d/out=%d" % (values["in"], values["cached"], values["out"])

    print("%-24s %-6s %-6s %-6s" % ("问题", "文件", "答案", "耗时"))
    print("-" * 56)
    for q, titles, must, answerable in CASES:
        elapsed = 0.0
        t0 = time.time()
        hits = server.R.search(q, top=1)
        top_title = hits[0]["chunk"]["title"] if hits else ""
        is_web = bool(answerable) and not titles
        if not use_llm:
            elapsed = time.time() - t0

        if not answerable:
            if not use_llm:
                print("%-24s %-6s %-6s %-6s" % (q[:22], "-", "跳过", "-"))
                continue
            t0 = time.time()
            res = server.answer(q)
            values = take_usage(res)
            ok = not res["ok"]
            elapsed = time.time() - t0
            ref_ok += ok
            total += elapsed
            print("%-24s %-6s %-6s %-6s %s" % (q[:22], "-", "-", "%.1fs" % elapsed,
                  ("✓ 正确拒答" if ok else "✗ 未拒答 ← " + top_title[:24]) +
                  " " + usage_text(values)))
            continue

        if is_web:
            # 只靠官网的题：本地首条没有意义，判"答案要素 + 来源等级"
            if not use_llm:
                print("%-24s %-6s %-6s %-6s" % (q[:22], "-", "-", "-"))
                continue
            t0 = time.time()
            res = server.answer(q)
            values = take_usage(res)
            elapsed = time.time() - t0
            ans = res["answer"] if res["ok"] else ""
            hit_ans = bool(ans) and any(m in ans for m in must)
            cites = res.get("citations") or []
            grade = (cites[0].get("grade") or 0) if cites else 0
            hit_src = grade >= 2
            ans_ok += hit_ans and hit_src
            src_ok += hit_src
            total += elapsed
            print("%-24s %-6s %-6s %-6s %s" % (q[:22], "官网" if hit_src else "✗源",
                  "✓" if hit_ans else "✗", "%.1fs" % elapsed, usage_text(values)))
            if not hit_src:
                print("      来源等级不足：%s" % ((cites[0].get("grade_label") if cites else "无引用")))
            elif not hit_ans:
                print("      答案缺要素：%s" % ans[:46])
            continue

        hit_doc = any(t in top_title for t in titles)
        doc_ok += hit_doc
        if use_llm:
            t0 = time.time()
            res = server.answer(q)
            values = take_usage(res)
            elapsed = time.time() - t0
            ans = res["answer"] if res["ok"] else ""
            hit_ans = bool(ans) and any(m in ans for m in must)
        else:
            print("%-24s %-6s %-6s %-6s" % (q[:22], "✓" if hit_doc else "✗", "跳过", "-"))
            continue
        ans_ok += hit_ans
        total += elapsed
        extra = usage_text(values) if use_llm else ""
        print("%-24s %-6s %-6s %-6s %s" % (q[:22], "✓" if hit_doc else "✗",
              "✓" if hit_ans else "✗", "%.1fs" % elapsed if use_llm else "-", extra))
        if not hit_doc:
            print("      首条文件：%s" % top_title[:46])

    print("-" * 56)
    print("检索层 首条文件正确 %d/%d (%.0f%%)" % (doc_ok, n_doc, 100 * doc_ok / max(n_doc, 1)))
    if use_llm:
        print("答案层 要素齐全   %d/%d (%.0f%%)" % (ans_ok, n_ans, 100 * ans_ok / max(n_ans, 1)))
        if n_web:
            print("来源层 官网等级达标 %d/%d (%.0f%%)" % (src_ok, n_web, 100 * src_ok / n_web))
        print("拒答层 正确拒答   %d/%d (%.0f%%)" % (ref_ok, n_ref, 100 * ref_ok / n_ref))
    else:
        print("答案层 跳过（加 --llm 才测试）")
        print("拒答层 跳过（加 --llm 才测试）")
    if use_llm:
        print("总耗时 %.1fs | 单题平均 %.1fs" % (total, total / len(CASES)))
        uncached = max(usage_total["in"] - usage_total["cached"], 0)
        cost = (usage_total["cached"] * 0.02 + uncached * 1.0 + usage_total["out"] * 4.0) / 1000000
        print("Token 合计 in=%d cached=%d out=%d | 估算成本 %.6f 元" % (
            usage_total["in"], usage_total["cached"], usage_total["out"], cost))


if __name__ == "__main__":
    main()
