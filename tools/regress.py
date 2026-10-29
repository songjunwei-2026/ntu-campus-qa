# -*- coding: utf-8 -*-
"""20 题回归集：把外部评审出的刁钻问题固化下来，改检索后先跑这个。

分四类，对应 RAG 的四种失效面：
  A 有明确条文但藏例外 —— 应返回相关文件
  B 语料根本没有 —— 应明确拒答，不硬凑
  C 口语化/多意图/错别字 —— 应至少命中一部分，不能整段失效
  D 歧义/跨部门/新旧版本 —— 应返回多个候选版本

判定标准写在每条的 want 里：want 为空表示"应当拒答"，
否则是"首条标题里必须出现的关键词之一"。

用法：python tools/regress.py [--ask]
  不带 --ask 只跑检索层（快，秒级）
  带 --ask 逐个跑问答接口（慢，每题一次模型调用）
"""
import json
import os
import sys
import urllib.parse
import urllib.request

BASE = os.environ.get("NTU_BASE", "http://127.0.0.1:8001")

# 本机 requests 别走系统代理：环境里配了 HTTP_PROXY 时，
# urllib 会把 127.0.0.1 也发给代理，直接回 502。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# (编号, 类别, 问题, 期望——空元组=应拒答，否则首条标题应含其中任一词)
CASES = [
    # A 有明确条文，但藏例外/适用范围
    ("A1", "本科生国家奖学金 必修课不及格还能申请吗", ("奖学金",)),
    ("A2", "研究生国家奖学金 课程成绩排名按什么算", ("奖学金",)),
    ("A3", "考试违纪 替考和携带小抄分别怎么处分", ("处分", "违纪")),
    ("A4", "本专科国家助学金 建档立卡家庭还要认定材料吗", ("助学金", "资助")),
    ("A5", "退役复学学生转专业 是否占用名额 是否免笔试", ()),
    # B 语料根本没有，应当拒答
    ("B1", "今天下午教务处窗口开到几点", ()),
    ("B2", "校园网IPv6怎么开通 宿舍路由器要换吗", ()),
    ("B3", "电动车充电桩怎么收费 充满自动断电吗", ()),
    ("B4", "快递驿站晚上几点关门 暑假开吗", ()),
    ("B5", "2026年国庆放假调课安排出了吗", ("国庆", "放假")),
    # C 口语化、一次多件、错别字
    ("C1", "挂科了还能评优吗顺便问下重修费多少", ("评奖评优", "奖学金", "课程考核")),
    ("C2", "奖学金啥时候发 国励和校奖能一起拿吗", ("奖学金",)),
    ("C3", "转专页需要啥条件 绩点要求多少 面试难吗", ("转专业",)),
    ("C4", "休学一年回来还能住原宿舍吗 学费退不退 医保咋续", ("休学", "学籍", "公寓", "住宿")),
    ("C5", "补考没过重修咋收费 会影响保研不", ("课程考核", "重修", "推免")),
    # D 歧义、跨部门、新旧版本
    ("D1", "转专业要什么条件", ("转专业",)),
    ("D2", "考试作弊处分多久能解除 解除后还能评优吗", ("处分",)),
    ("D3", "研究生出国交流保留学籍怎么办理", ("交流", "学籍", "研究生")),
    ("D4", "校外实习住宿补贴谁发 怎么申请", ("实习", "实践")),
    ("D5", "四级没过能拿学位证吗", ("学位",)),
]


def _get(path):
    with _OPENER.open(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(path, payload):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with _OPENER.open(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def check_keyword(q, want):
    d = _get("/api/keyword?q=" + urllib.parse.quote(q))
    if not want:
        # 应拒答：ok 必须是 False，且不能返回文件
        ok = (not d.get("ok")) and not d.get("files")
        return ok, ("拒答" if not d.get("ok") else "硬答了 " + d["files"][0]["title"][:26])
    if not d.get("ok") or not d.get("files"):
        return False, "未命中(%s)" % (d.get("reason") or "")
    t = d["files"][0]["title"]
    hit = any(w in t for w in want)
    return hit, ("%s%s" % ("命中 " if hit else "跑偏 ", t[:30]))


def check_ask(q, want):
    d = _post("/api/ask", {"q": q})
    if not want:
        ok = not d.get("ok")
        return ok, ("拒答" if ok else "硬答了 %.26s" % (d.get("answer") or ""))
    ok = bool(d.get("ok"))
    return ok, ("已答 %.26s" % (d.get("answer") or "")) if ok else "拒答了"


def main():
    use_ask = "--ask" in sys.argv
    fn = check_ask if use_ask else check_keyword
    stat = {}
    rows = []
    for cid, q, want in CASES:
        try:
            ok, note = fn(q, want)
        except Exception as e:
            ok, note = False, "请求失败 %s" % e
        cls = cid[0]
        stat.setdefault(cls, [0, 0])
        stat[cls][0] += 1
        stat[cls][1] += 1 if ok else 0
        rows.append((cid, ok, q, note))

    for cid, ok, q, note in rows:
        print("%s %s %-34s %s" % (cid, "PASS" if ok else "FAIL", q[:34], note))
    print()
    total_ok = sum(v[1] for v in stat.values())
    for cls in sorted(stat):
        n, k = stat[cls]
        print("  类别%s：%d/%d" % (cls, k, n))
    print("总计：%d/%d" % (total_ok, len(CASES)))
    return 0 if total_ok == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(main())
