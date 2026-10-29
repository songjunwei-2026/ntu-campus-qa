# -*- coding: utf-8 -*-
"""自测脚本：真实学生问题的延迟 + 回答质量原始数据。

不判对错（判对错要人看原文），只负责把延迟、命中、引用数、是否降级dump出来，
判断由人做——自评打分的依据必须是这些原始数。
"""
import json
import statistics
import sys
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8001"
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))

QS = [
    "转专业要什么条件",
    "国家奖学金怎么申请",
    "助学贷款怎么办",
    "推免需要什么条件",
    "补考怎么报名",
    "请假要提前几天",
    "学位证需要什么条件",
    "宿舍可以晚归吗",
    "考试作弊会怎么处理",
    "毕业要修满多少学分",
]


def get(path):
    with OP.open(BASE + path, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path, payload):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with OP.open(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    sys.stdout.reconfigure(encoding="utf-8")

    # 静态接口
    for p in ("/api/status", "/api/tree", "/api/places"):
        t = time.time()
        try:
            get(p)
            print("GET %-14s %5.0f ms" % (p, (time.time() - t) * 1000))
        except Exception as e:
            print("GET %-14s FAIL %s" % (p, e))

    kw_t, ask_t = [], []
    print("\n%-30s %6s %6s %5s %4s %s" % ("问题", "kw(ms)", "ask(s)", "ok", "引用", "摘要"))
    for q in QS:
        t = time.time()
        try:
            k = get("/api/keyword?q=" + urllib.parse.quote(q))
            kt = (time.time() - t) * 1000
        except Exception:
            k, kt = {}, -1
        t = time.time()
        try:
            a = post("/api/ask", {"q": q})
            at = time.time() - t
        except Exception as e:
            a, at = {"answer": "ERR %s" % e}, -1
        kw_t.append(kt)
        ask_t.append(at)
        print("%-30s %6.0f %6.1f %5s %4d %s" % (
            q, kt, at, a.get("ok"),
            len(a.get("citations") or []),
            (a.get("answer") or "")[:46].replace("\n", " ")))

    print("\nkeyword: n=%d 中位 %.0fms 最大 %.0fms" % (
        len(kw_t), statistics.median(kw_t), max(kw_t)))
    ask_ok = [x for x in ask_t if x > 0]
    print("ask    : n=%d 中位 %.1fs 最大 %.1fs 最小 %.1fs" % (
        len(ask_ok), statistics.median(ask_ok), max(ask_ok), min(ask_ok)))


if __name__ == "__main__":
    main()
