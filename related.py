# -*- coding: utf-8 -*-
"""相关问题：答完之后，猜学生接下来还想问什么。

三条硬约束：

1. 不编题。每道候选题都要先跑一遍**和正常提问完全相同**的检索链路
   （文书过滤 + 对象过滤 + 弱相关判据），过不了就丢掉。
   点了问不出东西的推荐，比不推荐更伤人——首页那 10 条示例就是一条条试出来的。
2. 题面说人话。用学生自己的问法（"国励需要什么条件"），
   不用文件的条文名（"《…评审办法》第三章资格条件"）。
3. 全部离线。不调大模型、不联网，一次推荐几十毫秒、零成本；
   答案进缓存时推荐也跟着进，缓存命中照样能出。

做法：两张表。
  A.「话题 → 学生问法」，话题必须真的出现在刚看过的文件里才算数。
  B. 每个族（奖助/学籍/考试/纪律/生活/发展）一份常用问法池，A 表不够 3 条时补位。
两张表都要过检索验证，所以补位的也不是凑数题——点了都答得上来。

族的作用是防话题漂移：问转专业条件，文件里出现两次"处分"就推"处分多久能解除"
是典型的漂移。所以能推哪些族，由**问题本身**和**引用文件的标题**一起定，
正文里顺嘴提一句的话题不参与定族。
"""
import re

# 弱相关门槛。这几个数是拿真实短问法标出来的，不是拍的：
# "休学怎么办"首条分数只有 9.2、覆盖率 1.00（命中的就是学籍管理规定），
# 按 server 里那个 15 分去卡会把好题全毙掉——那个门槛是给整句提问用的，
# 题越短 BM25 项越少，分数天然低。
# 所以分两档：分数够 15 就只看覆盖率别太低；分数不到 15 但覆盖率很高也算过。
WEAK_SCORE = 15
MIN_COVER = 0.45
SOFT_SCORE = 8
SOFT_COVER = 0.60

CJK = re.compile(r"[\u4e00-\u9fff]")

F_AWARD, F_STUDY, F_EXAM, F_DISC, F_LIFE, F_GROW = (
    "award", "study", "exam", "disc", "life", "grow")

# ---------------------------------------------------------------------------
# A 表：(族, 识别词, [学生问法...])
# 识别词要同时覆盖学生嘴里的简称和文件里的正式说法；
# 第一个问法是主问法，主问法验证不过才会轮到后面的。
# 加条目只加"语料里真有答案"的，写完跑 `python related.py --check`。
# ---------------------------------------------------------------------------
CONCEPTS = [
    # ---- 奖助 ----
    (F_AWARD, ["国家奖学金", "国奖"], ["国家奖学金怎么申请", "国家奖学金一年多少钱"]),
    (F_AWARD, ["国家励志奖学金", "国励"], ["国家励志奖学金怎么申请", "国励需要什么条件"]),
    (F_AWARD, ["国家助学金", "助学金"], ["国家助学金怎么申请", "助学金什么时候发"]),
    (F_AWARD, ["校级奖学金", "奖学金评定", "优秀学生奖学金"], ["校级奖学金怎么评", "奖学金什么时候发"]),
    (F_AWARD, ["综合素质测评", "综测", "综合测评"], ["综测评什么", "综合测评包括哪些方面"]),
    (F_AWARD, ["荣誉称号", "三好学生", "优秀学生干部"], ["三好学生怎么评"]),
    (F_AWARD, ["国家助学贷款", "助学贷款"], ["助学贷款怎么申请", "助学贷款毕业前要不要还"]),
        (F_AWARD, ["家庭经济困难", "困难认定"], ["家庭经济困难认定怎么申请"]),
    (F_AWARD, ["社会奖学金", "社会奖助学金"], ["社会奖学金怎么申请"]),
    # ---- 学籍与学业 ----
    (F_STUDY, ["转专业"], ["转专业需要什么条件", "转专业什么时候申请", "转专业考试考什么"]),
    (F_STUDY, ["休学"], ["休学怎么办", "休学条件是什么"]),
    (F_STUDY, ["复学"], ["复学要办什么手续"]),
    (F_STUDY, ["退学"], ["什么情况会被退学"]),
        (F_STUDY, ["修满", "毕业"], ["毕业要修满多少学分"]),
    (F_STUDY, ["学位"], ["四级没过能拿学位证吗", "毕业证和学位证有什么区别"]),
    (F_STUDY, ["重修"], ["重修怎么报名"]),
    (F_STUDY, ["补考"], ["补考不及格怎么办"]),
    (F_STUDY, ["学分绩", "绩点"], ["绩点怎么算", "绩点低会被学业警告吗"]),
    (F_STUDY, ["学业警告", "学业预警"], ["学业警告会怎么样"]),
    (F_STUDY, ["推免", "推荐免试", "免试攻读"], ["保研需要什么条件", "推免名额怎么分"]),
    (F_STUDY, ["辅修", "第二学士学位"], ["辅修怎么报名"]),
    (F_STUDY, ["转学"], ["转学需要什么条件"]),
    (F_STUDY, ["缓考"], ["缓考怎么申请"]),
    # ---- 考试 ----
    (F_EXAM, ["课程考核", "考核方式"], ["课程考核成绩怎么算", "平时成绩占多少"]),
    (F_EXAM, ["考试纪律", "作弊", "考场"], ["考试作弊会怎么处分"]),
    # ---- 纪律与考勤 ----
    (F_DISC, ["违纪处分", "处分"], ["处分多久能解除", "记过处分有什么影响"]),
    (F_DISC, ["申诉"], ["对处分不服怎么申诉"]),
    (F_DISC, ["考勤", "旷课", "缺课"], ["旷课多少节会被处分"]),
    (F_DISC, ["请假", "病假"], ["请假要办什么手续", "病假要开什么证明"]),
    # ---- 日常事务 ----
    (F_LIFE, ["一卡通", "校园卡"], ["校园卡丢了怎么补办", "校园卡怎么充值"]),
    (F_LIFE, ["学生证"], ["学生证丢了怎么补办", "学生证什么时候盖章"]),
    (F_LIFE, ["校徽"], ["校徽丢了怎么补办"]),
    (F_LIFE, ["网络管理", "校园网"], ["校园网怎么激活"]),
    (F_LIFE, ["电动自行车", "电瓶车", "充电桩"], ["电瓶车能进校园吗", "电动车充电桩在哪"]),
    (F_LIFE, ["公寓", "住宿", "退宿"], ["宿舍能换吗", "毕业生什么时候退宿"]),
    (F_LIFE, ["医疗保险", "医保"], ["大学生医保怎么报销"]),
    (F_LIFE, ["火车票", "优惠卡"], ["火车票优惠卡一年能用几次"]),
        (F_LIFE, ["教室"], ["自习教室在哪"]),
    (F_LIFE, ["图书馆", "借书", "续借"],
     ["图书馆能借几本书", "图书馆开放时间", "图书馆座位怎么预约"]),
    # ---- 发展与活动 ----
    (F_GROW, ["竞赛"], ["学科竞赛怎么报名", "竞赛获奖能加分吗"]),
    (F_GROW, ["创新创业", "大创", "创新创业训练计划"], ["大学生创新创业训练计划怎么申报"]),
    (F_GROW, ["社团"], ["社团怎么加入"]),
    (F_GROW, ["征兵", "入伍"], ["大学生入伍有什么优待"]),
    (F_GROW, ["入党", "党员"], ["入党要什么条件"]),
    (F_GROW, ["志愿"], ["志愿服务时长怎么算"]),
]

# ---------------------------------------------------------------------------
# B 表：族内常用问法池。A 表凑不满时才用，权重低，永远排在后面。
# 每条带一个话题词：话题词在刚看过的文件里出现过，这条就往前挪一点，
# 不然问宿舍会被推去做校园卡。
# ---------------------------------------------------------------------------
FAMILY_POOL = {
    F_AWARD: [(["国家奖学金"], "国家奖学金怎么申请"),
              (["国家励志奖学金"], "国家励志奖学金怎么申请"),
              (["校级奖学金"], "校级奖学金怎么评"),
              (["综合素质测评", "综合测评"], "综测评什么"),
              (["助学金"], "助学金什么时候发"),
              (["助学贷款"], "助学贷款怎么申请")],
    F_STUDY: [(["转专业"], "转专业需要什么条件"),
              (["休学"], "休学怎么办"),
              (["毕业"], "毕业要修满多少学分"),
              (["重修"], "重修怎么报名"),
              (["补考"], "补考没过怎么办"),
              (["推免"], "保研需要什么条件"),
              (["绩点", "学分绩"], "绩点怎么算")],
    F_EXAM: [(["课程考核"], "课程考核成绩怎么算"),
             (["补考"], "补考没过怎么办"),
             (["作弊", "考试纪律"], "考试作弊会怎么处分"),
             (["缓考"], "缓考怎么申请")],
    F_DISC: [(["处分"], "处分多久能解除"),
             (["申诉"], "对处分不服怎么申诉"),
             (["旷课", "缺课"], "旷课多少节会被处分"),
             (["请假"], "请假要办什么手续")],
    F_LIFE: [(["校园卡", "一卡通"], "校园卡丢了怎么补办"),
             (["学生证"], "学生证丢了怎么补办"),
             (["请假"], "请假要办什么手续"),
             (["公寓", "住宿"], "夜不归宿会怎么处理"),
             (["校园网"], "校园网怎么激活"),
             (["图书馆"], "图书馆能借几本书"),
             (["火车票"], "火车票优惠卡一年能用几次")],
    F_GROW: [(["竞赛"], "学科竞赛怎么报名"),
             (["创新创业", "大创"], "大创项目怎么申报"),
             (["入党", "党员"], "入党要什么条件"),
             (["社团"], "社团怎么加入")],
}

# 推荐来源是这几类文书时降权：通知/公示常常只讲今年这一批怎么办，
# 推一道"多少钱"结果出处是 2018 年的评审通知，看着就不像话。
_LOW_TRUST = re.compile(r"公示|名单|表彰|通报|通知|公告|安排|日程")
# 只写给教职工的文件。问"病假要开什么证明"，推了一道出自
# 《后勤保障部人力资源管理暂行办法》的题——学生在那个文件里没有身份，
# 点进去得到的答复跟他没关系。这类文件一律不推荐。
_STAFF_DOC = re.compile(r"教职工|教师|人力资源|干部|职称|人才引进|岗位聘任|退休|辅导员")
_STAFF_Q = re.compile(r"教职工|教师|老师|职工|辅导员|职称|人才|岗位|工资|编制")
_CUR_YEAR = 2026


def _bigrams(text):
    s = "".join(CJK.findall(text or ""))
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _similar(a, b):
    """两道题是不是在问同一件事。用二字切片的 Jaccard，不做分词——
    "国励""国奖"这类简称能落在这两个二字切片上。"""
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def _doc_index(R):
    """doc_uid → (标题, 全文)。第一次调用时建，之后复用。"""
    cache = getattr(_doc_index, "_cache", None)
    if cache is not None and cache.get("n") == len(R.docs):
        return cache["idx"]
    idx = {}
    for d in R.docs:
        uid = d.get("doc_uid")
        if uid not in idx:
            idx[uid] = [d.get("title") or "", []]
        idx[uid][1].append(d.get("text") or "")
    out = {k: (v[0], "\n".join(v[1])) for k, v in idx.items()}
    _doc_index._cache = {"n": len(R.docs), "idx": out}
    return out


def _matched(title, text):
    """这篇文件能推出哪些话题。

    标题里出现 = 3 分（这份文件就是讲这个的），能定族；
    只在正文出现 = 0.6 + 0.15×次数（封顶 2 分），能推题但不能定族。
    """
    out = []
    for fam, kws, qs in CONCEPTS:
        strong = any(k in title for k in kws)
        n = sum(text.count(k) for k in kws)
        if not strong and n < 2:
            continue
        out.append({"fam": fam, "kw": kws, "qs": qs,
                    "w": 3.0 if strong else min(2.0, 0.6 + 0.15 * n),
                    "title_hit": strong})
    return out


def _fams_of(q):
    """问题本身点了哪些族。全取，不取最长。

    因为一句话里常同时点两个族——"考试作弊会怎么处分"既是考试（作弊）
    又是纪律（处分），只取最长词会只剩考试，于是推"缓考怎么申请"而不是
    "处分多久能解除"。两个都留着，再靠"问题里点名的概念加权重"排先后。
    """
    fams = set()
    for fam, kws, _qs in CONCEPTS:
        if any(k in q for k in kws):
            fams.add(fam)
    return fams


def suggest(q, seed_uids, R, pipeline=None, limit=4):
    """返回 [{"q": 问题, "hint": 出处文件简称}]，最多 limit 条。

    seed_uids：刚才回答里引用到的文件，按相关度从高到低。
    pipeline：把一句话变成命中列表的函数，必须是正常提问链路
      （文书过滤 + 对象过滤），不传就退回裸检索——只用于自测。
    """
    docs = _doc_index(R)
    seeds = [u for u in (seed_uids or []) if u in docs][:3]

    fam_q = _fams_of(q)
    fam_seed = set()
    matched = []
    for rank, uid in enumerate(seeds):
        title, text = docs[uid]
        ms = _matched(title, text)
        matched.append((rank, uid, title, ms))
        if rank == 0:
            strong = [m for m in ms if m["title_hit"]]
            fam_seed = {m["fam"] for m in (strong or ms[:1])}

    # 能推的族：问题自己点的族 + 首条引用标题里的族。两边都没有就不推。
    fams = set(fam_seed) | set(fam_q)
    if not fams:
        return []

    cands = []
    for rank, uid, title, ms in matched:
        for m in ms:
            if m["fam"] not in fams:
                continue
            named = any(k in q for k in m["kw"])
            for i, cq in enumerate(m["qs"]):
                score = m["w"] * (1.0 if rank == 0 else 0.7) - 0.4 * i
                if m["fam"] in fam_q:
                    score += 0.6      # 问题点到的族优先，其次才是文件里带的族
                if named:
                    score += 1.8      # 问题里直接点名的概念，优先
                cands.append({"q": cq, "kw": m["kw"], "score": score,
                              "from": title, "named": named})
    # B 表补位：权重固定最低，只用来把条数凑到 3–4 条。
    # 话题词在首条引用里出现过的往前挪，问宿舍就不会被推去做校园卡。
    top_text = docs[seeds[0]][1] if seeds else ""
    for fam in fams:
        for kws, cq in FAMILY_POOL.get(fam, []):
            score = 0.2
            if any(k in top_text for k in kws):
                score = 0.9
            cands.append({"q": cq, "kw": kws, "score": score, "from": "", "named": False})

    # 和刚问过的问题太像的不要：答完"国家奖学金怎么申请"再推同一句，等于没推
    cands = [c for c in cands if _similar(c["q"], q) < 0.55]
    cands.sort(key=lambda c: -c["score"])

    ok, seen_q, tried = [], [], 0
    for c in cands:
        if len(ok) >= limit or tried >= 16:
            break
        if any(_similar(c["q"], s) >= 0.7 for s in seen_q):
            continue                       # 与已入选的题重复
        tried += 1
        r = _answerable(c, R, pipeline)
        if not r:
            continue
        seen_q.append(c["q"])
        ok.append({"c": c, "r": r})
    if not ok:
        return []

    # 一个概念最多两条：四条全说奖学金等于只推了一条；
    # 但同一份文件换个问法（"转专业什么时候申请" / "转专业考试考什么"）
    # 是真的两个问题，不能按概念一刀切掉。
    ok.sort(key=lambda x: -(x["c"]["score"] + x["r"]["bonus"]))
    out, per, pad = [], {}, 0
    for x in ok:
        key = x["c"]["kw"][0]
        if per.get(key, 0) >= (2 if x["c"].get("named") else 1):
            continue
        if x["c"]["score"] <= 0.2:      # B 表的通用补位题，最多带一条
            if pad >= 1:
                continue
            pad += 1
        per[key] = per.get(key, 0) + 1
        out.append({"q": x["c"]["q"], "hint": _short(x["r"]["title"] or x["c"]["from"])})
        if len(out) >= limit:
            break
    return out


def _answerable(c, R, pipeline=None):
    """这题点了之后，语料里真的答得上来吗。

    判据与正常链路一致：过文书/对象过滤后，首条覆盖率和分数够，
    且命中的前两段里真的提到了这个话题——否则是别的文件顺嘴带过。
    返回 None 表示不过；过了返回命中的标题和一个小加成（越新、越像制度文件越高）。
    """
    try:
        hits = (pipeline or (lambda s: R.search(s, top=3)))(c["q"])
    except Exception:
        return None
    if not hits:
        return None
    # 只写给教职工的文件先剔掉（问题本身就在问教职工时除外）
    if not _STAFF_Q.search(c["q"]):
        hits = [h for h in hits if not _STAFF_DOC.search(h["chunk"].get("title") or "")]
    hits = hits[:3]
    if not hits:
        return None
    top = hits[0]
    score, cov = top["score"], top["coverage"]
    if not ((score >= WEAK_SCORE and cov >= MIN_COVER) or
            (score >= SOFT_SCORE and cov >= SOFT_COVER)):
        return None
    # 话题必须出现在首条里，且不能在标题之外只出现一次：
    # 问"勤工助学怎么申请"，首条是《助学贷款实施办法》，全文只在资助体系那段
    # 顺嘴提了一句"勤工助学"——这题推出去，点开答的全是贷款，不是勤工助学。
    hit_text = None
    text = top["chunk"].get("text") or ""
    if any(k in text for k in c["kw"]):
        title = top["chunk"].get("title") or ""
        if any(k in title for k in c["kw"]) or sum(text.count(k) for k in c["kw"]) >= 2:
            hit_text = top
    if hit_text is None:
        return None
    title = hit_text["chunk"].get("title") or ""
    year = hit_text["chunk"].get("year") or 0
    if not isinstance(year, int):
        year = 0
    bonus = 0.0
    if _LOW_TRUST.search(title):
        bonus -= 0.35
    if year and _CUR_YEAR - year <= 3:
        bonus += 0.15
    return {"title": title, "bonus": bonus}


def _short(title):
    """出处文件简称：去掉栏目前缀、书名号、文号、修订标注，只留认得出的部分。"""
    t = re.sub(r"^【[^】]*】", "", title or "")
    t = re.sub(r"（[^）]{0,40}）|\([^)]{0,40}\)", "", t)
    t = t.replace("《", "").replace("》", "")
    t = re.sub(r"^(南通大学|江苏省|中华人民共和国)", "", t)
    t = re.sub(r"^关于(印发|组织|做好|公布|开展)", "", t)
    return t.strip()[:22]


if __name__ == "__main__":
    import sys, os
    sys.stdout.reconfigure(encoding="utf-8")
    from server import R as SRV_R, _drop_non_rule, _offscope_hits

    R = SRV_R

    def pipe(s):
        return _offscope_hits(s, _drop_non_rule(s, R.search(s, top=12)))[:3]

    if "--check" in sys.argv:
        bad, tot = [], 0
        for fam, kws, qs in CONCEPTS:
            for cq in qs:
                tot += 1
                if not _answerable({"q": cq, "kw": kws}, R, pipe):
                    bad.append((cq, kws))
        for cq, kws in bad:
            print("×  %-26s %s" % (cq, " ".join(kws)))
        print("\n不通过 %d / 共 %d" % (len(bad), tot))
        sys.exit(0)

    qs = [a for a in sys.argv[1:] if not a.startswith("--")] or \
         ["国家奖学金怎么申请", "转专业需要什么条件", "请假要办什么手续",
          "考试作弊会怎么处分", "助学贷款怎么申请", "毕业要修满多少学分",
          "必修课不及格怎么办", "四级没过能拿学位证吗", "宿舍能换吗"]
    for q in qs:
        hits = _drop_non_rule(q, R.search(q, top=12))
        uids = [h["chunk"]["doc_uid"] for h in _offscope_hits(q, hits)][:6]
        print("问：%s" % q)
        print("  引用：" + " | ".join([_short(h["chunk"]["title"]) for h in _offscope_hits(q, hits)[:2]] or ["无"]))
        for i, r in enumerate(suggest(q, uids, R, pipe), 1):
            print("   %d. %s   〔%s〕" % (i, r["q"], r["hint"]))
        print()
