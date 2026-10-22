# -*- coding: utf-8 -*-
"""
校园政策检索核心（BM25 + 文种/时效/同义词/口径加权）

为什么不用纯 TF-IDF：
  1. TF-IDF 里长文档吃亏不明显，但对"旧通知压过新条例"这类排序错误无解；
  2. 校园问答的查询很短（5-15 字），需要给"条例/办法"这类规范性文件加权；
  3. 需要把"电瓶车"这类口语词映射到规范性表述"电动自行车"。

对外接口：
  r = Retriever()
  hits = r.search("电瓶车通行证怎么办理", top=5)
  ctx  = r.build_context(hits)
"""
import os, re, json, math, difflib
from collections import Counter, defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
CHUNKS = os.path.join(BASE, "corpus", "chunks.jsonl")

CJK = re.compile(r"[\u4e00-\u9fff]")
CUR_YEAR = 2026

# 语料里的 dept 是爬虫目录代码，对外展示必须换成中文名
DEPT_NAMES = {
    "xxgk": "信息公开网",
    "jwc": "教务处",
    "yjs": "研究生院",
    "hr": "人事处",
    "bwc": "保卫处",
    "hqbzb": "后勤保障部",
    "met": "现代教育技术中心",
    "jsfzzx": "教师发展中心",
    "xgc": "学生工作处",
    "新生手册": "新生手册",
}


def dept_name(code):
    """部门代码转中文名；未知代码原样返回，不猜。"""
    return DEPT_NAMES.get(code, code or "")

# 纯疑问词，检索前剥离（不动"办理/申请"这类有信息量的词）
QUESTION_WORDS = ["请问", "怎么样", "怎么", "怎么办", "如何", "是什么", "什么",
                  "哪些", "多少", "是否", "能不能", "可不可以", "吗", "呢", "呀", "啊", "我在"]

# 口语 → 规范性表述
SYNONYMS = {
    "电瓶车": ["电动自行车"], "电动车": ["电动自行车"], "小电驴": ["电动自行车"],
    "校园卡": ["一卡通"], "饭卡": ["一卡通"],
    "保研": ["推免", "推荐免试"], "考研": ["研究生招生"],
    "挂科": ["不及格", "补考"], "绩点": ["学分绩", "平均学分绩点"],
    "作弊": ["违纪", "考试纪律"], "请假": ["考勤", "外出"],
    "宿舍": ["公寓"], "退宿": ["公寓", "住宿"],
    "学位证": ["学士学位"], "毕业证": ["毕业"],
    "医保": ["医疗保险"], "助学贷款": ["国家助学贷款"],
    "打工": ["勤工助学"], "家教": ["勤工助学"],
    "评奖学金": ["奖学金", "综合测评"], "入党": ["党员", "党组织"],
    "转专业": ["转专业", "专业分流"],
    "缺勤": ["旷课", "缺课"], "旷课": ["缺课"], "缺课": ["旷课"],
    "迟到": ["考勤"], "早退": ["考勤"],
    "退学": ["退学处理", "学业警告"],
}

# 规范性文件 vs 过程性文件
T_STRONG = re.compile(r"条例|办法|规定|细则|规则|规程|标准|准则|方案|制度|章程")
T_WEAK = re.compile(r"通知|公告|公示|名单|结果|动态|新闻|报道|简报|议程|招聘|招标|申报")

STOP_BIGRAM = set("的了和是在有为对与及等这那之其所以并且但或如果因为一个我们你们他们".split())

# 覆盖率只统计实义字符；疑问/虚词不计，否则"我的""是不"会给所有文档抬分
COVER_STOP = set("的了和是在有为对与及等这那之其所以并且但或如果因为一个我们你们他们"
                 "吗呢吧啊呀怎什么多少是否请问如何哪些我你他她它需要找谁几")

# 来源权重：制度原文 1.0，正式页（official）也是 1.0；
# faq 是窗口值班人员的口头答复，比正式文件低一档，遇到同一问法时让制度原文优先；
# web 是联网临时抓的页面，最低。
SRC_W = {"faq": 0.92, "web": 0.90}


def tokens(text):
    s = "".join(CJK.findall(text))
    out = []
    for i in range(len(s) - 1):
        g = s[i:i + 2]
        if g in STOP_BIGRAM:
            continue
        out.append(g)
    return out


def strip_question(q):
    for w in QUESTION_WORDS:
        q = q.replace(w, " ")
    return q


def expand_query(q):
    q2 = strip_question(q)
    add = []
    for k, vs in SYNONYMS.items():
        if k in q:
            add.extend(vs)
    return q2 + (" " + " ".join(add) if add else ""), add


class Retriever:
    def __init__(self, path=CHUNKS, k1=1.5, b=0.75):
        # 支持多份语料：政策原文 chunks.jsonl + 官网栏目直采 web_chunks.jsonl
        paths = [path] if isinstance(path, str) else list(path)
        self.docs = []
        for p in paths:
            if not os.path.exists(p):
                continue
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self.docs.append(json.loads(line))
        self.k1, self.b = k1, b
        self.N = len(self.docs)
        self.tf, self.dl, self.df = [], [], defaultdict(int)
        for d in self.docs:
            c = Counter(tokens(d["text"]))
            self.tf.append(c)
            self.dl.append(max(sum(c.values()), 1))
            for g in c:
                self.df[g] += 1
        self.avgdl = sum(self.dl) / max(self.N, 1)
        self.idf = {g: math.log(1 + (self.N - v + 0.5) / (v + 0.5)) for g, v in self.df.items()}
        # 文档 → 块号 → 块，供跨块取上下文
        self.by_doc = defaultdict(dict)
        for d in self.docs:
            self.by_doc[d["doc_uid"]][d["chunk_id"]] = d
        # 字符级 IDF：二元切分会造出"业需""要条"这类跨词边界的假词，
        # 用它们算覆盖率会把正确文档误判成不相关，所以覆盖率按字符算。
        cdf = defaultdict(int)
        for d in self.docs:
            for ch in set(CJK.findall(d["text"])):
                cdf[ch] += 1
        self.cidf = {ch: math.log(1 + (self.N - v + 0.5) / (v + 0.5)) for ch, v in cdf.items()}

    def _bm25(self, qterms, i):
        tf, dl = self.tf[i], self.dl[i]
        s = 0.0
        for g in qterms:
            f = tf.get(g)
            if not f:
                continue
            s += self.idf[g] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
        return s

    @staticmethod
    def _type_weight(title):
        if T_STRONG.search(title):
            return 1.10
        if T_WEAK.search(title):
            return 0.80
        return 1.0

    @staticmethod
    def _age_weight(year):
        if not year:
            return 0.95
        return max(0.75, 1 - 0.022 * (CUR_YEAR - year))

    @staticmethod
    def _scope_weight(q, title):
        if re.search(r"本科|本科生", q) and "研究生" in title and "本科" not in title:
            return 0.65
        if "研究生" in q and "本科" in title and "研究生" not in title:
            return 0.65
        # 默认用户是本科生：没写"研究生"就别让研究生文件抢前排
        if "研究生" not in q and "研究生" in title:
            return 0.80
        return 1.0

    @staticmethod
    def _title_boost(qterms, title):
        """查询词命中标题的越多，越可能是"专门讲这件事"的文件"""
        if not qterms:
            return 1.0
        tt = set(tokens(title))
        hit = sum(1 for g in qterms if g in tt)
        return 1.0 + 1.3 * hit / len(qterms)

    @staticmethod
    def _ask_weight(query, title):
        """问"怎么处理/什么条件"时要的是规定，不是公告。

        公告里常常只写"违者按《XX规定》处理"，是索引不是条文；
        命中率高但答不了问题，所以这类问法下把通知公告再压一档。
        """
        if re.search(r"怎么处理|如何处理|处分|处罚|规定|标准|条件|流程|办法|要求", query) \
                and T_WEAK.search(title) and not T_STRONG.search(title):
            return 0.62
        return 1.0

    def search(self, query, top=5):
        q_exp, added = expand_query(query)
        qterms = Counter(tokens(q_exp))
        if not qterms:
            return []
        qchars = list(dict.fromkeys(ch for ch in CJK.findall(q_exp) if ch not in COVER_STOP))
        scored = []
        for i in range(self.N):
            raw = self._bm25(qterms, i)
            if raw <= 0:
                continue
            d = self.docs[i]
            s = raw * self._type_weight(d["title"]) \
                    * self._age_weight(d.get("year", 0)) \
                    * self._scope_weight(query, d["title"]) \
                    * self._title_boost(qterms, d["title"]) \
                    * self._ask_weight(query, d["title"]) \
                    * SRC_W.get(d.get("source"), 1.0)
            if d.get("source") == "faq":
                s *= 0.40 + 0.60 * self._title_cover(qchars, i)
            scored.append((s, raw, i))
        scored.sort(reverse=True)
        if not scored:
            return []

        cut = scored[0][0] * 0.45  # 相对截断：与首条差距过大的直接判为噪声

        # 同一文档最多保留 2 块：一篇办法里"类别"和"条件"常分在不同段，
        # 以前每篇只留 1 块，答案所在的那段永远进不了结果。
        PER_DOC = 2
        FLOOR = 3   # 至少给 3 条：top1 判错时，后面正确的还有机会被看到

        out, doc_n, kept_titles = [], defaultdict(int), []
        for s, raw, i in scored:
            if s < cut and len(out) >= FLOOR:
                break
            d = self.docs[i]
            if doc_n[d["doc_uid"]] >= PER_DOC:
                continue
            cov = self._coverage(qchars, i)
            if cov < 0.30:
                continue
            # 近似标题只留排名靠前的一条（如"纪律处分规定"/"违纪处分规定"），不删数据只去重展示
            if doc_n[d["doc_uid"]] == 0 and \
                    any(difflib.SequenceMatcher(None, d["title"], t).ratio() >= 0.90 for t in kept_titles):
                continue
            doc_n[d["doc_uid"]] += 1
            if doc_n[d["doc_uid"]] == 1:
                kept_titles.append(d["title"])
            out.append({"score": round(s, 4), "bm25": round(raw, 4),
                        "chunk": d, "coverage": cov, "expanded": added})
            if len(out) >= top:
                break
        return out

    def _coverage(self, qchars, i):
        """字符级 IDF 加权覆盖率：查询的实义字符有多少出现在该块里。

        不用二元组：切分会造出"业需""要条"这类语料里根本没有的假词，
        把它们算成"没命中"会让正确文档的覆盖率掉到 0.2 以下，被门槛误杀。
        这是相关度信号，不是拒答判据——"校长"在每篇印发行里都有，
        命中不代表答得上，拒答交给大模型判断。"""
        text = self.docs[i]["text"]
        tot = hit = 0.0
        for ch in qchars:
            w = self.cidf.get(ch)
            if not w:
                continue
            tot += w
            if ch in text:
                hit += w
        return round(hit / tot, 3) if tot else 0.0

    def _title_cover(self, qchars, i):
        """问题实义字符落在标题上的比例。

        faq 条目的标题就是标准问法，只有这一层能判断"问的是不是同一件事"；
        正文里的共享动词（"借""怎么"）会让两条毫不相干的问答互相顶替
        ——"校徽可以借给别人吗"曾被"可以借桌子椅子吗"抢走首条。
        """
        if not qchars:
            return 0.0
        title = self.docs[i]["title"]
        hit = sum(1 for ch in qchars if ch in title)
        return hit / len(qchars)

    def context_of(self, hit, radius=1):
        """取同文件相邻块，补足被切断的上下文"""
        c = hit["chunk"]
        idx = self.by_doc.get(c["doc_uid"], {})
        parts = []
        for k in range(c["chunk_id"] - radius, c["chunk_id"] + radius + 1):
            if k in idx:
                parts.append(idx[k]["text"])
        return "\n".join(parts)

    def expanded_text(self, doc_uid, chunk_ids, radius=1):
        """给定同一文件的若干块号，返回它们连同邻块拼起来的正文（按块号排序、去重）。"""
        idx = self.by_doc.get(doc_uid, {})
        want = sorted({k for cid in chunk_ids
                       for k in range(cid - radius, cid + radius + 1) if k in idx})
        return "\n".join(idx[k]["text"] for k in want)

    def build_context(self, hits, radius=2):
        blocks = []
        for i, h in enumerate(hits, 1):
            c = h["chunk"]
            meta = "%s（%s，%s）" % (c["title"], c.get("dept", ""), c.get("date", "") or "无日期")
            blocks.append("[%d] %s\n%s" % (i, meta, self.context_of(h, radius)))
        return "\n\n".join(blocks)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    r = Retriever()
    q = " ".join(sys.argv[1:]) or "国家奖学金的申请条件"
    print("语料 %d 块 | 查询: %s\n" % (r.N, q))
    for i, h in enumerate(r.search(q, 5), 1):
        c = h["chunk"]
        print("%d. %.3f (bm25 %.2f, 覆盖 %.2f) %s" % (i, h["score"], h["bm25"], h["coverage"], c["title"][:46]))
        print("     %s | %s" % (c.get("dept", ""), c.get("date", "")))
