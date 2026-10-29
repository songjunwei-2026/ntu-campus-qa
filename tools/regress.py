# -*- coding: utf-8 -*-
"""20 题回归集：把外部评审出的刁钻问题固化下来，改检索后先跑这个。

分四类，对应 RAG 的四种失效面：
  A 有明确条文但藏例外 —— 应返回相关文件
  B 语料根本没有 —— 应明确拒答，不硬凑
  C 口语化/多意图/错别字 —— 应至少命中一部分，不能整段失效
  D 歧义/跨部门/新旧版本 —— 应返回多个候选版本

判定标准（2026-10-29 修订，外部评审推翻了旧标准）：
  旧版 check_ask 只要接口返回 ok 就算 PASS，20 题里 15 条等于只测了"能不能开口"，
  编答案、答残了都算通过。现在两层各自判内容：
    检索层：want 为空=应拒答；否则首条**文件标题**必须出现 want 中任一词，
            且标题不能是公示/名单/表彰这类没有条款的文书。
    问答层：want 为空=应拒答；否则 ok 必须为真，且**首条引用的文件标题**
            必须出现 want 中任一词。答案正文提到关键词不算——跑偏的答案
            也会提到关键词，只有引用能证明它真的读了那份文件。

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

# 没有条款的文书：获奖名单、审核结果公示、表彰通报。
# 命中关键词只说明"文中出现了这几个字"，不可能回答"能不能办、要什么材料"。
NON_RULE = ("公示", "名单", "表彰", "通报")

# (编号, 问题, 检索层期望, 问答层期望)
# 期望为空元组 = 应当拒答；否则为标题里必须出现的词之一。
CASES = [
    # A 有明确条文，但藏例外/适用范围
    # A1：语料里没有本科生国家奖学金评审办法，能命中的只有获奖名单公示——
    #     返回公示算错，明确说"没有评审办法"才对。
    ("A1", "本科生国家奖学金 必修课不及格还能申请吗", (), ()),
    ("A2", "研究生国家奖学金 课程成绩排名按什么算", ("奖学金",), ("奖学金",)),
    ("A3", "考试违纪 替考和携带小抄分别怎么处分", ("处分", "违纪"), ("处分",)),
    ("A4", "本专科国家助学金 建档立卡家庭还要认定材料吗", ("助学金", "资助"), ("助学金", "资助")),
    # A5：退役复学条款在《学生管理规定》里确有明文（全局 38 块含"退役"），旧判 FAIL 是误判。
    ("A5", "退役复学学生转专业 是否占用名额 是否免笔试", ("管理规定", "学籍", "征兵"), ("管理规定", "学籍", "退役")),
    # B 语料根本没有，应当拒答
    ("B1", "今天下午教务处窗口开到几点", (), ()),
    # B2：全局 0 块含 IPv6。检索层命中"校园网故障排查"尚可理解，
    #     问答层必须拒答——曾编出 300 字开通流程并盖"已核验"。
    ("B2", "校园网IPv6怎么开通 宿舍路由器要换吗", (), ()),
    # B3：语料《自助式电动车充电桩管理办法》里确实写了"1 元不低于 4.5 小时"，
    #     属于能答的题。旧判定要求拒答是错的——等于惩罚"答对了"。
    ("B3", "电动车充电桩怎么收费 充满自动断电吗", ("电动自行车", "充电"), ("充电", "电动自行车")),
    ("B4", "快递驿站晚上几点关门 暑假开吗", (), ()),
    ("B5", "2026年国庆放假调课安排出了吗", ("国庆", "放假"), ("国庆", "放假")),
    # C 口语化、一次多件、错别字
    ("C1", "挂科了还能评优吗顺便问下重修费多少", ("奖学金", "评奖评优", "课程考核"), ("奖学金", "评奖评优", "课程考核")),
    ("C2", "奖学金啥时候发 国励和校奖能一起拿吗", ("奖学金",), ("奖学金",)),
    ("C3", "转专页需要啥条件 绩点要求多少 面试难吗", ("转专业",), ("转专业",)),
    ("C4", "休学一年回来还能住原宿舍吗 学费退不退 医保咋续", ("休学", "学籍", "公寓", "住宿"), ("休学", "学籍")),
    # C5：文件标题写的是"推荐…免试攻读"，全文不含"推免"二字，旧判定误杀。
    ("C5", "补考没过重修咋收费 会影响保研不", ("课程考核", "重修", "免试"), ("课程考核", "重修", "免试")),
    # D 歧义、跨部门、新旧版本
    ("D1", "转专业要什么条件", ("转专业",), ("转专业",)),
    ("D2", "考试作弊处分多久能解除 解除后还能评优吗", ("处分",), ("处分",)),
    # D3：首条只中"研究生"三个字就算过太松，必须落在交流/学籍类文件上。
    ("D3", "研究生出国交流保留学籍怎么办理", ("交流", "学籍"), ("交流", "学籍")),
    # D4：全局 0 块含"实习补贴"，拒答就是正解。
    ("D4", "校外实习住宿补贴谁发 怎么申请", (), ()),
    ("D5", "四级没过能拿学位证吗", ("学位",), ("学位",)),
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
        ok = (not d.get("ok")) and not d.get("files")
        return ok, ("拒答" if not d.get("ok") else "硬答了 " + d["files"][0]["title"][:26])
    if not d.get("ok") or not d.get("files"):
        return False, "未命中(%s)" % (d.get("reason") or "")
    t = d["files"][0]["title"]
    if any(w in t for w in NON_RULE):
        return False, "首条是文书不是规定：%s" % t[:26]
    hit = any(w in t for w in want)
    return hit, ("%s%s" % ("命中 " if hit else "跑偏 ", t[:30]))


def check_ask(q, want):
    d = _post("/api/ask", {"q": q})
    if not want:
        ok = not d.get("ok")
        return ok, ("拒答" if ok else "硬答了 %.26s" % (d.get("answer") or ""))
    if not d.get("ok"):
        return False, "拒答了：%.24s" % (d.get("answer") or "")
    cites = d.get("citations") or []
    if not cites:
        return False, "无引用：%.24s" % (d.get("answer") or "")
    # 看前两条引用，不只看第一条：答案里标 [2] 是常态（首位文件常是"相邻但相关"的），
    # 只要前两条里有一条对上，就说明它确实读了该读的文件。
    titles = [c.get("title") or "" for c in cites[:2]]
    hit = any(w in t for t in titles for w in want)
    return hit, ("引用%s %.28s" % ("对 " if hit else "错 ", titles[0]))


def main():
    use_ask = "--ask" in sys.argv
    fn = check_ask if use_ask else check_keyword
    idx = 3 if use_ask else 2
    stat, rows = {}, []
    for c in CASES:
        cid, q = c[0], c[1]
        want = c[idx]
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
    print("总计：%d/%d  （%s层）" % (total_ok, len(CASES), "问答" if use_ask else "检索"))
    return 0 if total_ok == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(main())
