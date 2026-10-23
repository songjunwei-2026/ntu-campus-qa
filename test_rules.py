# -*- coding: utf-8 -*-
"""_intent_mismatch 回归测试：使用真实语料检索，不 mock groups。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server


CASES = [
    # 钟点类：语料没有明确时刻表，应拦截
    ("食堂几点开门", True),
    ("图书馆开放时间", True),
    ("校车时刻表", True),
    ("食堂什么时候关门", True),
    ("校车几点发车", True),
    # 钟点类：语料有作息/上课证据，应放行
    ("第一节课几点上课", False),
    ("作息时间表", False),
    ("每天几点上课", False),
    # 周期类：语料有对应学期/评审/毕业设计内容，应放行
    ("转专业什么时候可以申请", False),
    ("奖学金什么时候评", False),
    ("毕业设计什么时候开题", False),
    ("学期什么时候结束", False),
    # 周期类：语料无校历/军训时间依据，应拦截
    ("2026年校历什么时候开学", True),
    ("军训什么时候开始", True),
    ("什么时候放寒假", True),
    ("新生什么时候报到", True),
    # 学费类：本地没有对应的具体收费金额，应拦截
    ("学费一年多少钱", True),
    ("住宿费多少钱", True),
    ("学费标准是多少", True),
    # 其他金额问题有制度依据，不应被学费规则误拦
    ("奖学金金额是多少", False),
    # 无关干扰，应放行
    ("考试作弊怎么处理", False),
    ("学分绩点怎么计算", False),
    ("实验室安全有哪些要求", False),
    ("电动车通行证怎么办理", False),
    ("转专业需要什么条件", False),
    # T6 外置规则新增覆盖：事件词非时间问法应放行，费用项要按同一费用匹配
    ("寒假留校申请流程", False),
    ("暑假什么时候结束", True),
    ("住宿费标准是多少", True),
]


def main():
    failed = []
    for q, expected in CASES:
        hits = server.R.search(q, top=6)
        groups = server._offscope(q, server._group_hits(hits))[:server.MAX_GROUPS]
        actual = server._intent_mismatch(q, groups)
        ok = actual == expected
        print("%s %-24s expected=%s actual=%s groups=%d" %
              ("PASS" if ok else "FAIL", q, expected, actual, len(groups)))
        if not ok:
            failed.append(q)
    print("%d/%d passed" % (len(CASES) - len(failed), len(CASES)))
    if failed:
        print("failed:", "、".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
