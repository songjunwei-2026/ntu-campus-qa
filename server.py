# -*- coding: utf-8 -*-
"""
校园政策问答站（标准库 HTTP 服务，无第三方依赖）

启动:
  python server.py            # http://127.0.0.1:8000
  python server.py 8080       # 指定端口

接口:
  GET  /             问答页面
  GET  /map          校园地图页（同一前端，独立视图）
  GET  /api/status   服务状态（语料规模、是否已接大模型）
  GET  /api/places   全部地点 + 底图范围
  GET  /api/place?q= 按地名/房间号查地点
  POST /api/ask      {"q": "问题"}  ->  回答 + 出处 + 涉及地点
"""
import os, sys, json, re, socket
from collections import OrderedDict
from datetime import date
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, parse_qsl, urlencode

BASE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(BASE, "web")
sys.path.insert(0, BASE)

from rag import Retriever, dept_name, SYNONYMS
import llm
import places as campus_places
import websearch, weblearn
from intent_rules import RULES

WEB_CHUNKS = os.path.join(BASE, "corpus", "web_chunks.jsonl")
# 人工核实的事实层：官网结构上抓不到的东西（SPA、附件、410 栏目）
# 用 add_fact.py 录入，只收有明确出处的内容，宁可空着也不写没核过的
FACT_CHUNKS = os.path.join(BASE, "corpus", "facts_chunks.jsonl")
FEEDBACK_PATH = os.path.join(BASE, "feedback.jsonl")
R = Retriever([os.path.join(BASE, "corpus", "chunks.jsonl"), WEB_CHUNKS, FACT_CHUNKS])
CFG = llm.load_config()
STATS = {}
_p = os.path.join(BASE, "corpus", "stats.json")
if os.path.exists(_p):
    STATS = json.load(open(_p, encoding="utf-8"))

_ANSWER_CACHE = OrderedDict()
_CACHE_HITS = 0
_CACHE_STATE = None
_CACHE_MAX = 200
_TOTAL_IN = 0
_TOTAL_CACHED = 0
_TOTAL_OUT = 0
_USAGE_CALLS = 0
_SUGGEST_STATE = None
_SUGGEST_ITEMS = []
def _suggest_norm(s):
    s = str(s or '').strip().lower()
    return ''.join(chr(ord(c)-0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else (' ' if ord(c)==0x3000 else c) for c in s).replace(' ','')
def _suggest_prepare():
    global _SUGGEST_STATE, _SUGGEST_ITEMS
    state = _cache_state_now()
    if state == _SUGGEST_STATE: return
    seen=set(); items=[]
    for d in R.docs:
        title=str(d.get('title') or '').strip()
        if title and title not in seen:
            seen.add(title); items.append({'text':title,'kind':'faq' if d.get('source')=='faq' else 'title'})
    items.sort(key=lambda x: 0 if x['kind']=='faq' else 1); _SUGGEST_ITEMS=items; _SUGGEST_STATE=state
def suggest(q,n=6):
    q=str(q or '').strip(); nq=_suggest_norm(q)
    if len(nq)<2: return {'q':q,'items':[]}
    _suggest_prepare(); out=[]
    # 同义扩展复用检索层的 SYNONYMS，避免"联想给不出、检索却答得上"的错位
    #（如「保研」在标题里写作「推免」「推荐免试」）
    alts=[]
    for k, vs in SYNONYMS.items():
        if _suggest_norm(k) in nq:
            for v in vs:
                nv=_suggest_norm(v)
                if nv and nv != nq and nv not in alts:
                    alts.append(nv)
    for item in _SUGGEST_ITEMS:
        t=_suggest_norm(item['text'])
        score=None
        if nq in t:
            score=100+max(0,50-len(t))
        else:
            best=None
            for alt in alts:
                if alt in t:
                    cand=90+max(0,40-len(t))
                else:
                    # 同义词允许字符覆盖：「推荐免试」在标题里写作
                    #「推荐优秀应届本科毕业生免试攻读」，不是连续子串但意思相同
                    ar=sum(1 for c in set(alt) if c in t)/max(1,len(set(alt)))
                    if ar<.8: continue
                    cand=70+ar*20
                if best is None or cand>best: best=cand
            score=best
        if score is None:
            # 两个字必须全中：只中一个字基本是噪声
            #（「电瓶」只中"电"，会命中「校园网连不上电脑怎么办」，纯误导）
            if len(nq)<=2: continue
            rate=sum(1 for c in set(nq) if c in t)/max(1,len(set(nq)))
            if rate<.6: continue
            score=rate*100
        out.append((score,item))
    # 同分时 faq（同学问过的话）优先，其余保持原顺序
    out.sort(key=lambda x:(-x[0], 0 if x[1]['kind']=='faq' else 1))
    # n<=0 视为未指定，按默认 6 条；上限 10 条避免下拉过长
    return {'q':q,'items':[x[1] for x in out[:max(1,min(int(n) or 6,10))]]}


def _norm_q(q):
    """问题缓存键：半角化、英文小写并折叠连续空白。"""
    out = []
    for ch in str(q or "").strip():
        code = ord(ch)
        if code == 0x3000:
            ch = " "
        elif 0xFF01 <= code <= 0xFF5E:
            ch = chr(code - 0xFEE0)
        out.append(ch)
    return re.sub(r"\s+", " ", "".join(out)).lower()


def _cache_state_now():
    paths = (os.path.join(BASE, "corpus", "chunks.jsonl"), FACT_CHUNKS)
    mtimes = []
    for path in paths:
        try:
            mtimes.append(os.path.getmtime(path))
        except OSError:
            mtimes.append(None)
    return (date.today().isoformat(), tuple(mtimes))


def _cache_prepare():
    global _CACHE_STATE
    state = _cache_state_now()
    if _CACHE_STATE != state:
        _ANSWER_CACHE.clear()
        _CACHE_STATE = state


def _copy_result(result):
    return json.loads(json.dumps(result, ensure_ascii=False))


def _feedback_items(limit=200):
    items = []
    try:
        with open(FEEDBACK_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    items.reverse()
    return items[:limit]


def _feedback_total():
    try:
        with open(FEEDBACK_PATH, encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


def _corpus_meta():
    """语料跨度：发文部门数 + 日期年份区间。给首页「覆盖范围」用。"""
    depts, years = set(), set()
    for d in R.docs:
        n = dept_name(d.get("dept") or "")
        if n:
            depts.add(n)
        y = str(d.get("date") or "")[:4]
        if len(y) == 4 and y.isdigit() and 1900 < int(y) < 2100:
            years.add(y)
    ys = sorted(years)
    return len(depts), (ys[0] if ys else ""), (ys[-1] if ys else "")


def _usage_now():
    usage = llm.LAST_USAGE or {}
    return {
        "in": int(usage.get("prompt_tokens") or 0),
        "cached": int(usage.get("prompt_cache_hit_tokens") or 0),
        "out": int(usage.get("completion_tokens") or 0),
    }

# 模型答案里的数值必须能在原文中找到，找不到就退回原文摘录。
# 这是低成本的事实校验：政策问答错一个百分点的危害，远大于答得不够漂亮。
NUM_UNIT = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|％|个工作日|工作日|天|日|小时|个月|年|次|人|学分|学时|分|元|周|门|项|条|款|号)")

# OCR 把页码和页眉挤进了正文中间，形如 "须上交违规使用的电器，149 :公寓（宿舍）管理人员…"
OCR_PAGE_MARK = re.compile(r"(?<=[，。；、）】」])\s*\d{1,3}\s*[:：]\s*")


def _clean_ocr(text):
    """去掉正文里混入的页码+页眉冒号残留；只删这两个字符，不改证据原文用词。"""
    return OCR_PAGE_MARK.sub("", text)


def _display_title(title):
    """取书名做答句主语：有书名号取第一个《》内；无则剥掉尾部（文号）括号。"""
    m = re.search(r"《([^》]+)》", title)
    if m:
        return m.group(1)
    return re.sub(r"[（(][^（）()]*〔[^〕]*〕[^（）()]*[）)]\s*$", "", title).strip()


def _sentences(text):
    """按中文标点切分，并保留句末标点，供抽取式摘要和前端高亮使用。"""
    parts = re.findall(r"[^。！？!?；;\n]*(?:[。！？!?；;]|\n|$)", text)
    return [p.strip() for p in parts if p.strip()]


def _extract_snippet(question, text, limit=3):
    """从证据块中抽取包含查询词最密集的 2~3 个完整句子，原样返回。"""
    sentences = _sentences(_clean_ocr(text))
    if not sentences:
        return text.strip()

    # 用查询中的中文/英文数字词和相邻二元组计分，不改写证据原文。
    terms = [ch for ch in question if not ch.isspace() and ch not in "的了有什么怎么吗呢？?！!，,。；;：:"]
    unique_terms = list(dict.fromkeys(terms))
    bigrams = list(dict.fromkeys(
        question[i:i + 2] for i in range(len(question) - 1)
        if not question[i].isspace() and not question[i + 1].isspace()
    ))
    scored = []
    for idx, sentence in enumerate(sentences):
        char_hits = sum(sentence.count(term) for term in unique_terms)
        pair_hits = sum(sentence.count(pair) for pair in bigrams)
        priority = 0
        if re.search(r"申请条件|条件|金额|多少钱|比例|多少", question):
            priority += len(NUM_UNIT.findall(sentence)) * 12
        if re.search(r"处分|哪几种|类型", question):
            priority += sum(sentence.count(x) for x in ("警告", "严重警告", "记过", "留校察看", "开除学籍")) * 10
        # 以相邻二元组为主要命中依据，避免短的泛化条款压过真正相关的长句。
        density = char_hits + pair_hits * 3 + priority
        scored.append((density, char_hits + pair_hits, idx))

    positive = [item for item in scored if item[1] > 0]
    chosen = sorted((positive or scored), key=lambda item: (-item[0], -item[1], item[2]))[:limit]
    chosen_idx = sorted(item[2] for item in chosen)
    if re.search(r"申请条件|条件|金额|多少钱|比例|多少", question):
        numeric = []
        for idx, sentence in enumerate(sentences):
            matches = NUM_UNIT.findall(sentence)
            if not matches:
                continue
            values = []
            for value in matches:
                m = re.search(r"\d+(?:\.\d+)?", value)
                if m:
                    values.append(float(m.group()))
            # 金额问题优先保留带“元”的句子；其余问题优先保留数字信息最密的句子。
            money = sum("元" in value for value in matches)
            numeric.append((money if re.search(r"金额|多少钱", question) else 0,
                            max(values or [0]), len(matches), idx))
        if numeric:
            target = max(numeric, key=lambda item: item[:3])[3]
            if target not in chosen_idx:
                chosen_idx = sorted((chosen_idx[:-1] if chosen_idx else []) + [target])
    # 优先给出 2~3 句；极短块则按实际句数返回。
    if len(chosen_idx) == 1 and len(sentences) > 1:
        next_idx = 1 if chosen_idx[0] != 1 else 0
        chosen_idx = sorted({chosen_idx[0], next_idx})
    return "".join(sentences[i] for i in chosen_idx[:limit]).strip()


def _norm(s):
    """比对前先归一：全角转半角、去掉空格。"""
    return re.sub(r"\s+", "", s.translate(str.maketrans("０１２３４５６７８９％", "0123456789%")))


def _unverified(answer, context):
    """返回答案里在参考资料中找不到的数字/比例/文号。"""
    ctx = _norm(context)
    return [m.group() for m in NUM_UNIT.finditer(answer) if _norm(m.group()) not in ctx]


def _group_hits(hits):
    """同一文档的多个命中块合并成一条引用。

    一篇办法里"奖学金类别"和"申请条件"常分在两段，只留一块就必然答偏。
    """
    groups, index = [], {}
    for h in hits:
        uid = h["chunk"]["doc_uid"]
        g = index.get(uid)
        if g is None:
            g = {"best": h, "items": []}
            index[uid] = g
            groups.append(g)
        g["items"].append(h)
    for g in groups:
        g["items"].sort(key=lambda x: x["chunk"]["chunk_id"])
        g["ids"] = [x["chunk"]["chunk_id"] for x in g["items"]]
        g["text"] = "\n".join(_clean_ocr(x["chunk"]["text"]) for x in g["items"])
    groups.sort(key=lambda g: -g["best"]["score"])
    return groups


def _offscope(q, groups):
    """用户没提研究生，就别把研究生规定混进答案——本机用户默认是本科生。

    标题里"研究生"三个字不一定是这么写的：问"学士学位授予"曾带回
    《博士、硕士学位授予点设置调整优化实施细则》——它写的是"硕士"，不是"研究生"，
    只按"研究生"三个字筛等于没筛。研究层次按整套词过滤。
    """
    if re.search(r"研究生|硕士|博士|考研|推免|保研", q):
        return groups
    kept = [g for g in groups
            if not (re.search(r"研究生|硕士|博士", g["best"]["chunk"]["title"])
                    and not re.search(r"本科|学士|专科", g["best"]["chunk"]["title"]))]
    return kept or groups


def _intent_mismatch(q, groups):
    """按外置规则拦截证据不足的时间、班车和费用问题。"""
    if not groups:
        return False
    corpus = "\n".join(
        (g["best"]["chunk"].get("title", "") + "\n" + g.get("text", ""))
        for g in groups[:4]
    )
    for rule in RULES:
        match = re.search(rule["ask"], q)
        if not match:
            continue
        # require：规则自带的附加条件，比如"必须在问时间"，不满足就跳过这条规则。
        # 写成规则字段而不是按 name 特判——按名字判断的话，将来改个名行为就悄悄变了。
        if rule.get("require") and not re.search(rule["require"], q):
            continue
        found = False
        for evidence in rule["evidence"]:
            pattern = evidence
            if "{event}" in pattern:
                pattern = pattern.format(**match.groupdict())
            if "{item}" in pattern:
                # item 从命中文本里取，避免在规则名上挂特例
                pattern = pattern.replace("{item}", "学费" if "学费" in match.group(0) else "住宿费")
            if pattern.startswith("re:"):
                found = bool(re.search(pattern[3:], corpus))
            else:
                found = pattern in corpus
            if found:
                break
        if not found:
            weblearn.log_miss(q, rule["reason"])
            return True
    return False

MAX_DOC_CHUNKS = 8    # 短文档整篇喂，长文档用命中块 ±2 段
CTX_RADIUS = 2        # 关键条款常被切在下一段，邻块给宽一点
HEAD_CHUNKS = 2       # 长文档固定带文件开头：处分种类、期限这类总则条款都在第 1–2 段
CTX_MAX_CHARS = 4200  # 单篇送进上下文的正文上限，按块累加
MAX_GROUPS = 4        # 只把前 4 篇交给模型，控制上下文长度


def _doc_chunk_ids(g):
    """决定一条引用要喂多少原文。

    整篇喂只对短文档做：课程考核办法那种 16 段的长文件整篇塞进去，
    上下文会被稀释，模型反而答不出来，所以长文档退回"命中块 ±2 段"。

    但只给"命中块 ±2 段"会漏掉文件开头的总则条款。问"处分多久能解除"，
    命中第 5 段的处理流程，"记过 10 个月"写在第 1 段的处分种类表里；
    模型看不到就答"原文未写明"，同时又把这份文件列进引用——自相矛盾。
    所以长文档补上开头两块，再按字符预算由近及远补邻块。
    """
    uid = g["best"]["chunk"]["doc_uid"]
    chunks = R.by_doc.get(uid, {})
    if len(chunks) <= MAX_DOC_CHUNKS:
        return sorted(chunks), 0

    used = [cid for cid in sorted(chunks)[:HEAD_CHUNKS]]
    chars = sum(len(chunks[cid].get("text") or "") for cid in used)
    near = []
    for cid in sorted(set(g["ids"])):
        near.append(cid)
        for d in range(1, CTX_RADIUS + 1):
            near.append(cid + d)
            near.append(cid - d)
    seen = set(used)
    for cid in near:
        if cid in seen or cid not in chunks:
            continue
        n = len(chunks[cid].get("text") or "")
        if chars + n > CTX_MAX_CHARS and len(seen) > HEAD_CHUNKS:
            continue
        seen.add(cid)
        chars += n
    return sorted(seen), 0


def _build_context(groups):
    """喂给模型的参考资料：编号与引用编号一致。"""
    blocks = []
    for n, g in enumerate(groups, 1):
        c = g["best"]["chunk"]
        meta = "%s（%s，%s）" % (c["title"], dept_name(c.get("dept", "")),
                                c.get("date", "") or "无日期")
        ids, radius = _doc_chunk_ids(g)
        body = R.expanded_text(c["doc_uid"], ids, radius)
        blocks.append("[%d] %s\n%s" % (n, meta, _clean_ocr(body)))
    return "\n\n".join(blocks)


def _place_block(q):
    """把问题里的地点意图整理成前端地点卡需要的结构。

    答不上来（拒答）也要给地点：问"图书馆在哪"时，政策语料本来就没有答案，
    但用户真正想要的是那栋楼的位置。
    """
    r = campus_places.intent(q)
    if not r.get("wanted"):
        return {"matched": [], "options": [], "room": None, "reason": r.get("reason", "")}
    matched = [campus_places.public(r["place"])] if r.get("place") else []
    return {
        "matched": matched,
        "options": r.get("options", []),
        "room": r.get("room"),
        "reason": r.get("reason", ""),
        "unknown": r.get("unknown"),
        "nearby": campus_places.public_list(campus_places.nearby_for(r.get("unknown")))
                  if r.get("unknown") else [],
    }


def _is_refusal(text):
    """判断模型是不是在拒答。

    模型有时会写成"【未收录】 资料只作了定性规定：……"，标记后面带着完整答案。
    只看"有没有标记"会把这种回答误判成拒答，所以要看标记后面还剩多少内容。
    """
    t = (text or "").strip()
    if not t:
        return True
    i = t.find(llm.NO_ANSWER)
    if i < 0:
        return False
    rest = (t[:i] + t[i + len(llm.NO_ANSWER):]).strip("：:，,。 、")
    return len(rest) < 40


def _drop_marker(text):
    """答案正文里若还带着【未收录】，去掉它，只留实质内容。"""
    return (text or "").replace(llm.NO_ANSWER, "").lstrip("：:，,。 ").strip()


def _truncate_answer(text, limit=400):
    """按完整句子限制答案长度，避免在句中硬切。

    截断后要补 "……"，所以正文只能用 limit-2 个字符——
    否则补完省略号会变成 402 字，超出上限。
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = text[:limit - 2]
    positions = [head.rfind(mark) for mark in ("。", "！", "？", "!", "?", "\n")]
    cut = max(positions)
    if cut >= 0:
        return head[:cut + 1].rstrip() + "……"
    # 没有标点时只保留完整的最后一段，仍不在句中截断。
    paragraph = head.rsplit("\n", 1)[0].strip()
    return (paragraph or head).rstrip() + "……"


def _place_answer(p, room=None, brief=False):
    """纯位置问题的回答文本（政策语料答不了时，地点本身就是答案）。

    brief=True 时只给一句定位，细节交给下面的地点卡，避免同一段话说两遍。
    """
    zone = (p.get("building") or "").strip()
    where = zone if zone.startswith("校园") else ("校园" + zone if zone else "校园内")
    if brief:
        return "%s位于%s。" % (p["name"], where)
    parts = ["%s位于%s。" % (p["name"], where)]
    if p.get("desc"):
        parts.append(p["desc"])
    if p.get("route"):
        parts.append(p["route"])
    return "".join(parts)


def _unknown_place_answer(P):
    """问的是位置、但地点库里没有这个名字。

    不能只回一句"未涉及"——那会让用户以为网站坏了。这里明确说清
    "地图库没收录这个地方"，并列出同类已收录地点供点击定位。
    """
    unknown = P.get("unknown") or "这个地方"
    opts = campus_places.nearby_for(unknown)
    if opts:
        tail = "已收录的同类地点有：%s。也可以点下面的名字直接在地图上定位。" % (
            "、".join(o["short"] or o["name"] for o in opts[:5]))
    else:
        tail = "校园地图目前收录了教学楼、食堂、宿舍园区、校门等主要场所，" \
               "这个地方还没收录。"
    return "校园地图里暂时没有收录「%s」。%s" % (unknown, tail)


def _options_answer(P):
    """只说了类别（"食堂在哪"）：给候选清单，别让模型拿拒答话术搪塞。"""
    opts = P["options"]
    cats = sorted({o["category"] for o in opts})
    return "校内「%s」类共 %d 处：%s。点下面的名字可以直接在地图上定位。" % (
        "、".join(cats), len(opts), "、".join(o["name"] for o in opts[:6]))


# 需要"文件依据"的问法：只拿到报道类页面就不回答，别拿新闻稿当制度
POLICY_ASK = re.compile(
    r"(规定|办法|条件|流程|材料|标准|比例|要求|程序|手续"
    r"|能不能|可不可以|怎么办|如何办理|多少|几(?:点|号|个|天|年|月))")

# 答案里出现这些，等于模型自己承认"这不是你问的那个对象"
EXCLUDE_PAT = re.compile(
    r"(不能乘坐|请勿乘坐|其他[^。，]{0,4}人员请勿|不对外开放|不适用于"
    r"|仅限教职工|本科生(?:不能|不可|不得|请勿))")

# 独立学院等别的办学主体：问到南通大学时不能拿它的标准顶替
OTHER_BODY = ("杏林学院",)


def _scope_conflict(q, text):
    """答案自曝"对象不符"时判为未答。

    模型常把限定条件写进答案里——"这是启东校区的""杏林学院（民办）"
    "本科生不能乘坐"。读起来像解释，实际是在说"我答的不是你问的"。
    后端不接住这个信号，就成了自信地答错，比答"没有"更伤信任。
    """
    if EXCLUDE_PAT.search(text):
        return True
    for k in OTHER_BODY:
        if k in text and k not in q:
            return True
    return False


def _campus_hint(q, text):
    """答案只讲别的校区时补一句提醒（答案本身仍算有效）。"""
    if any(c in q for c in ("啬园", "启秀", "启东")):
        return ""
    if "啬园" in text:  # 已经覆盖啬园，不必提示
        return ""
    hit = [c for c in ("启东校区", "启秀校区") if c in text]
    if hit:
        return "以上为%s的信息；如需啬园校区，请在问题里注明校区。" % hit[0]
    return ""


# 设施/时间类问题：拿别的校区的规定来答，等于答非所问
CAMPUS_FACILITY = re.compile(r"(图书馆|食堂|宿舍|班车|校车|开放|开门|闭馆|借阅|浴室|体育馆|自习)")
OTHER_CAMPUS = ("启东校区", "启秀校区")


def _other_campus_only(q, text):
    """问题没指定校区、问的是设施时间，却只找到别的校区 → 返回该校区名。

    啬园是主校区，问"图书馆几点开"默认就是问它。官网只翻出启东校区
    的通知时，把启东的作息当成答案是误导——宁可说"只找到启东的"。
    """
    if any(c in q for c in ("啬园", "启秀", "启东")):
        return ""
    if not CAMPUS_FACILITY.search(q):
        return ""
    # 首句常是模型复述问题（"啬园校区本科生的图书馆开放时间。"），
    # 不能拿它当"已经覆盖啬园"的证据，只看第二句起的实质内容。
    parts = re.split(r"[。！？]", text)
    body = "。".join(parts[1:]).strip() or text
    if "啬园" in body:
        return ""
    for c in OTHER_CAMPUS:
        if c in body:
            return c
    return ""


def _web_ctx(pages):
    """把官网页面拼成给模型的参考资料，带编号和来源等级，方便它标 [n]。"""
    buf = []
    for i, p in enumerate(pages, 1):
        body = (p.get("text") or "").strip()
        snip = (p.get("snippet") or "").strip()
        if snip and snip[:20] not in body:
            body = (body + "\n搜索摘要：" + snip).strip()
        buf.append("【资料%d｜%s】%s\n链接：%s\n正文：%s" % (
            i, p.get("grade_label") or "官网页面", p.get("title", ""), p.get("url", ""), body))
    return "\n\n".join(buf)


def _web_answer(q, places_block, expanded=None, depth=0):
    """本地语料答不上时，去官网实时取证据。拿不到就返回 None，交给上层拒答。

    depth=1 表示已经试过联网仍不行，不再递归。
    """
    pages, from_cache = [], False
    cached = weblearn.recall(q)
    if cached:
        pages, from_cache = cached, True
    elif depth == 0:
        try:
            pages = websearch.lookup(q, want=3, max_pages=2)
        except Exception:
            pages = []
        if pages:
            weblearn.save_pages(q, pages)
    if not pages:
        return None

    # 只用有正文/摘要的页面做证据
    pages = [p for p in pages if (p.get("text") or p.get("snippet"))][:3]
    if not pages:
        return None

    # 缓存里存的是旧结构，没有等级字段，统一重算；
    # 并把正式文件排到最前——引用顺序直接决定模型先看哪一条。
    for p in pages:
        g, label = websearch.grade(p)
        p["grade"], p["grade_label"] = g, label
    pages.sort(key=lambda p: -(p.get("grade") or 0))
    top_grade = pages[0].get("grade") or 2

    # 政策类问题只拿到报道就不答：拿新闻稿当制度依据，比答"没有"更糟。
    if top_grade <= 1 and POLICY_ASK.search(q):
        return None

    ctx = _web_ctx(pages)
    try:
        text = llm.chat(q, ctx, CFG, web=True)
    except Exception:
        return None
    if _is_refusal(text):
        return None
    if _unverified(text, ctx):
        return None
    if _scope_conflict(q, text):
        return None

    cites = []
    for i, p in enumerate(pages, 1):
        cites.append({
            "no": i,
            "title": p.get("title") or "官网页面",
            "source": "web",
            "dept": "学校官网",
            "grade": p.get("grade") or 2,
            "grade_label": p.get("grade_label") or "官网页面",
            "date": p.get("ts") or "",
            "url": p.get("url", ""),
            "page": None,
            "relevance": round(1.0 - (i - 1) * 0.12, 3),
            "score": 0,
            "bm25": 0,
            "snippet": (p.get("snippet") or p.get("text") or "")[:200],
            "evidence": p.get("text") or p.get("snippet") or "",
        })

    when = next((p.get("ts") for p in pages if p.get("ts")), "")
    src = "此前联网抓取于 %s" % when if from_cache else "刚刚实时检索"
    if top_grade >= 3:
        notice = "本地语料库没有收录，已从官网找到正式文件/通知（%s）。原文未收入语料库，请点击出处核对。" % src
    elif top_grade == 2:
        notice = "本地语料库没有收录，官网只有页面说明、未定位到正式文件（%s）。以下内容请以出处原文为准。" % src
    else:
        notice = "本地语料库没有收录，官网只检索到报道类页面、未见正式文件或通知（%s）。以下内容仅供参考。" % src

    # 找到了，但找的是别的校区：把话说清，不算答上
    other = _other_campus_only(q, text)
    if other:
        weblearn.log_miss(q, "web:other-campus")
        return {
            "ok": False,
            "mode": "web",
            "question": q,
            "answer": _truncate_answer("官网只检索到%s的相关规定，没有啬园校区的对应内容。仅供对照参考：%s" % (
                other, _drop_marker(text))),
            "citations": cites,
            "notice": "以上为%s的信息，不能直接套用到啬园校区。" % other,
            "expanded": expanded or [],
            "places": places_block,
        }

    notice += _campus_hint(q, text)
    return {
        "ok": True,
        "mode": "web",
        "question": q,
        "answer": _truncate_answer(_drop_marker(text)),
        "citations": cites,
        "notice": notice,
        "expanded": expanded or [],
        "places": places_block,
    }


def _answer_uncached(q, emit=None):
    P = _place_block(q)
    # 明确在问"某地怎么走/在哪"时，位置本身就是答案，不必先看政策语料
    if P["matched"] and P.get("reason", "").startswith(("locative", "delivery", "room")):
        p = P["matched"][0]
        return {"ok": True, "mode": "place", "question": q,
                "answer": _place_answer(p, P.get("room"), brief=True),
                "citations": [], "notice": "", "expanded": [], "places": P}

    # 在问位置，但这个地方不在地图库里：说清"没收录"并给同类候选，
    # 而不是让大模型拿政策语料拒答。
    # 例外：语料里可能直接写了答案（"照片采集在哪"→ 答疑表里答"金穗服务中心"），
    # 这种情况先让检索试一次，命中够强就照常作答，别急着说没收录。
    if P.get("reason") == "unknown-place":
        probe = R.search(q, top=1)
        if not (probe and probe[0]["coverage"] >= 0.55):
            return {"ok": False, "mode": "place", "question": q,
                    "answer": _unknown_place_answer(P), "citations": [],
                    "notice": "如确为校内场所，可能是地图库尚未收录，建议以现场指示牌或校方公布信息为准。",
                    "expanded": [], "places": P}

    hits = R.search(q, top=6)
    groups = _offscope(q, _group_hits(hits))[:MAX_GROUPS]
    if _intent_mismatch(q, groups):
        weblearn.log_miss(q, "intent-mismatch")
        return {"ok": False, "mode": "retrieval", "question": q,
                "answer": "本地制度中没有收录与这个问题直接对应的内容。",
                "citations": [],
                "notice": "建议查询学校最新公告或咨询相关职能部门。",
                "expanded": [], "places": P}
    if not groups:
        # 本地语料完全没有相关块：先上网找官网证据（比"未涉及"有用得多）
        web = _web_answer(q, P)
        if web:
            return web
        # 地点卡兜底：问"图书馆几点关门"这类问题时，语料没有也至少把楼指出来。
        # 但 ok 仍为 False —— 开放时间这类问题并没有被回答，不能算作已答。
        if P["matched"]:
            p = P["matched"][0]
            weblearn.log_miss(q, "no-hit:has-place")
            return {"ok": False, "mode": "place", "question": q,
                    "answer": "政策文件里没有查到相关规定；不过 %s" % _place_answer(p, P.get("room")),
                    "citations": [], "notice": "以上位置信息来自校园地图底座，仅供参考。",
                    "expanded": [], "places": P}
        if P["options"]:
            # 只说了类别（"食堂在哪"）：给出候选清单本身就是答案
            return {"ok": True, "mode": "place", "question": q,
                    "answer": _options_answer(P), "citations": [],
                    "notice": "位置信息来自校园地图底座；开放时间、窗口办理等以现场公示为准。",
                    "expanded": [], "places": P}
        weblearn.log_miss(q, "no-hit:no-place")
        return {"ok": False, "mode": "retrieval", "question": q,
                "answer": "本地语料和官网检索都没有找到相关内容。", "citations": [],
                "notice": "", "expanded": [], "places": P}

    top_score = groups[0]["best"]["score"] or 1.0
    citations = []
    for n, g in enumerate(groups, 1):
        c = g["best"]["chunk"]
        snippet = _extract_snippet(q, g["text"])
        loc_chunk, loc_sent = _locate_sent(g, snippet)
        citations.append({
            "no": n,
            "title": c["title"],
            "source": c.get("source", ""),
            "dept": dept_name(c.get("dept", "")),
            "date": c.get("date", "") or "",
            "url": c.get("url", ""),
            "page": c.get("printed_page") or None,
            # 展示用相对匹配度：绝对分数没有可比性，首条恒为 100%
            "relevance": round(g["best"]["score"] / top_score, 3),
            "score": round(g["best"]["score"], 2),
            "bm25": round(g["best"]["bm25"], 2),
            "snippet": snippet,
            "evidence": g["text"],
            # 条款级定位：文档号 + 块号 + 第几句。前端凭这三个把"出处"
            # 从「某文件」压到「第几段第几句」，学生不用打开整篇自己找。
            "doc_uid": c.get("doc_uid") or "",
            "chunk_id": loc_chunk,
            "sent_no": loc_sent,
            "ids": g.get("ids") or [],
        })

    res = {
        "ok": True,
        "mode": "retrieval",
        "question": q,
        "answer": _truncate_answer("根据《%s》：%s [1]" % (
            _display_title(groups[0]["best"]["chunk"]["title"]), citations[0]["snippet"])),
        "citations": citations,
        "notice": "",
        "expanded": groups[0]["best"]["expanded"],
        # 问题写明年份、命中文件却全是别的年份时，前端要在答案上方挂提示
        "year_note": _year_note(q, hits),
        "places": P,
    }

    # 流式：引用和模式先推给前端。用户能立刻看到"在查哪几份文件"，
    # 而不是盯着骨架屏猜系统是不是卡住了。
    if emit:
        # 只带前端渲染要用的字段：evidence 是整段原文（每条 1–2KB），
        # 连同 4 条一起推会把首屏引用列表推迟几百毫秒，得不偿失。
        emit({"type": "meta", "mode": "retrieval", "citations": _slim_cites(citations),
              "year_note": res["year_note"], "expanded": res["expanded"]})

    if not CFG.get("api_key"):
        res["notice"] = "当前为原文摘录模式：未接入大模型，系统无法判断这段原文是否真的回答了问题。"
        return res

    ctx = _build_context(groups)

    # 语料里根本没有的专有名词：模型看不到它，写出来的只可能是编的。
    gaps = _gap_terms(q, ctx)
    if gaps:
        weblearn.log_miss(q, "corpus-gap")
        return {"ok": False, "mode": "gap", "question": q,
                "answer": "资料库里没有关于「%s」的内容，无法给出有出处的答案。" % "、".join(gaps[:2]),
                "citations": [], "notice": "这类信息通常不在校级发文里，建议查学校官网或咨询对应部门。",
                "expanded": res["expanded"], "places": P, "year_note": _year_note(q, hits)}

    try:
        if emit:
            text = llm.chat_stream(q, ctx, CFG,
                                   on_delta=lambda t: emit({"type": "delta", "t": t}))
        else:
            text = llm.chat(q, ctx, CFG)
    except Exception as e:
        # 降级成原文摘录时不能算"已核验"：摘录只是把命中的原文摆出来，
        # 它到底有没有回答问题没人核对过。前端据此不盖核验章。
        res["degraded"] = True
        res["notice"] = "模型调用失败，已回退为原文摘录（%s）。" % e
        return res

    if _is_refusal(text):
        # 真拒答了。不重试本地上下文：命中率高不等于文档真的回答了问题。
        # 但本地没有 ≠ 官网没有，先联网找一轮再说。
        # 拒答时也把年份差异带上：问 2025 年而库里只有 2026 年时，
        # "没有"之外还得让用户知道"有别的年份的"，否则等于什么都没说。
        yn = _year_note(q, hits)
        web = _web_answer(q, P, res["expanded"])
        if web:
            return web
        # 资料答不了就明确拒答，不拿相近的条文凑答案；有地点意图时顺手指路
        if P["matched"]:
            p = P["matched"][0]
            weblearn.log_miss(q, "llm-refused:has-place",
                              groups[0]["best"].get("score", 0),
                              groups[0]["best"].get("coverage", 0))
            return {"ok": False, "mode": "place", "question": q,
                    "answer": "政策文件里没有查到相关规定；不过 %s" % _place_answer(p, P.get("room")),
                    "citations": [], "notice": "以上位置信息来自校园地图底座，仅供参考。",
                    "expanded": res["expanded"], "places": P, "year_note": yn}
        if P["options"]:
            # 只说了类别（"食堂在哪"）：给出候选清单本身就是答案
            return {"ok": True, "mode": "place", "question": q,
                    "answer": _options_answer(P), "citations": [],
                    "notice": "位置信息来自校园地图底座；开放时间、窗口办理等以现场公示为准。",
                    "expanded": res["expanded"], "places": P, "year_note": yn}
        weblearn.log_miss(q, "llm-refused:no-place",
                          groups[0]["best"].get("score", 0),
                          groups[0]["best"].get("coverage", 0))
        return {"ok": False, "mode": "llm", "question": q,
                "answer": "本地语料和官网检索都没有找到相关内容，建议咨询相关职能部门。",
                "citations": [], "notice": "", "expanded": res["expanded"],
                "places": P, "year_note": yn}

    bad = _unverified(text, ctx)
    if bad:
        res["notice"] = "模型给出的 %d 处数值（%s）在原文中核对不上，已回退为原文摘录。" % (
            len(bad), "、".join(bad[:3]))
        return res

    res["mode"] = "llm"
    res["answer"] = _truncate_answer(_drop_marker(text))
    return res


# 关键词检索的弱相关门槛。实测：正常问题首条 score 在 27–62，
# 语料外的问法在 5–13（"快递驿站几点关门"5.5、"教务处窗口几点"13.1）。
# 取 15 留一点余量；这个数是绝对分，换了语料要重新标定。
WEAK_SCORE = 15


def _split_sent(text):
    """按中文句末标点切句，丢掉空白和过短的碎片。"""
    out, buf = [], ""
    for ch in text:
        buf += ch
        if ch in "。！？；\n":
            s = buf.strip()
            if len(s) >= 12:
                out.append(s)
            buf = ""
    s = buf.strip()
    if len(s) >= 12:
        out.append(s)
    return out[:24]


def _year_note(q, hits):
    """问题里写明了年份、但排在首位的文件是别的年份时提醒一句。

    「2026年国庆放假调课」的首条是 2024 年通知：字符全中、年份全错，
    用户拿到的是一份过期安排。这种情况不改排序——因为"调课"这个词
    确实只出现在旧文件里，强行翻转会把正常的长尾查询也带偏——
    改为在答案上方明说，让用户自己判断。
    """
    m = re.search(r"(20\d{2})\s*年", q or "")
    if not m or not hits:
        return ""
    want = int(m.group(1))
    c = hits[0]["chunk"]
    y = c.get("year")
    if not str(y or "").isdigit():
        dt = str(c.get("date") or "")[:4]
        y = int(dt) if dt.isdigit() else 0
    if y and int(y) != want:
        # 不判断谁更权威：可能是库里缺你要的年份，也可能你要的年份本就还没发文。
        # 只把差异摆出来，判断交给用户。
        return "排在最前的是 %d 年的文件，不是你问的 %d 年。请确认这份是否适用于你要办的时间段；" \
               "如需 %d 年的文件，建议查学校官网最新发布。" % (int(y), want, want)
    return ""


# 一句话问好几件事时的切分点。只在首次检索被判弱相关时才用，
# 所以宁可切得粗一点，也不要影响本来问得好好的单意图查询。
SPLIT_PAT = re.compile(r"顺便|另外|还有|以及|同时|再问|想问下|想问|然后|，|,|；|;|、|\s+")


def _split_intents(q):
    """把"挂科了还能评优吗顺便问下重修费多少"拆成两件事。

    整串丢给 BM25 时，每件事的词都在互相稀释，覆盖率上不去，
    最后被弱相关判据一刀切掉——本来是问得很具体的问题，却什么也没得到。
    """
    parts = [p.strip() for p in SPLIT_PAT.split(q or "") if len(p.strip()) >= 3]
    return parts[:3] if len(parts) > 1 else [q]


def _locate_sent(g, snippet):
    """片段落在同一文件的哪一块、第几句。

    引用只到"某文件"时，学生要打开整篇自己找；给出块号加句号，
    才能说"就是这句，你自己看"。句子可能来自命中的任意一块，
    所以在整篇里找，不能拿"命中块拼起来的文本"数句号——
    那样算出来的第 18 句，在原文第 6 块里根本不存在。
    """
    key = (snippet or "").strip()[:16]
    if not key:
        return g["best"]["chunk"].get("chunk_id"), None
    idx = R.by_doc.get(g["best"]["chunk"].get("doc_uid"), {})
    for cid, item in sorted(idx.items()):
        for i, s in enumerate(_split_sent(item.get("text") or ""), 1):
            if key in s:
                return cid, i
    return g["best"]["chunk"].get("chunk_id"), None


# 只在语料里"根本不存在"的专有名词才拦：IPv6、CET-4、APP 名这类。
# 纯中文缺口交给 _weak 与模型拒答判断，避免把口语说法一起拦掉。
_ASCII_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9\.\-]{2,}|\d+[A-Za-z]{2,}")


def _gap_terms(q, ctx):
    """问题里那些模型根本没机会看到的专有名词。

    "校园网IPv6怎么开通"——全库 0 个块含 IPv6，模型却照着"校园网"三个字
    编了 300 字开通流程，四条引用全与 IPv6 无关，前端还盖了"已核验"。
    这类词只可能是用户从别处听来的：语料里没有就是没有，宁可说查不到。
    """
    low = (ctx or "").lower()
    out = []
    for t in _ASCII_TOKEN.findall(q or ""):
        if t.lower() not in low and t not in out:
            out.append(t)
    return out


def _weak(q, hits):
    """首条是不是硬凑来的。判定依据见 _keyword 里的注释。"""
    if not hits:
        return True
    top = hits[0]
    qchars = {ch for ch in q if not ch.isspace()}
    title_hits = len(qchars & set(top["chunk"].get("title") or ""))
    return top["score"] < WEAK_SCORE or (top["coverage"] < 0.55 and title_hits < 2)


def _keyword(q, n_files=3, n_clauses=3):
    """关键词检索：最相关的 N 个文件 + N 个条款（条款=命中块里的原句）。"""
    hits = R.search(q, top=48)

    # 整串检索不成，先看看是不是一次问了好几件事：拆开分头找再合并。
    # 拆开能找到东西就直接放行——整串判弱是因为几件事互相稀释，
    # 不代表每一件都查不到，用原始长查询再判一次等于把刚救回来的又扔掉。
    recovered = False
    if _weak(q, hits):
        parts = _split_intents(q)
        if len(parts) > 1:
            pool = {}
            for p in parts:
                # 太短的碎片（"暑假开吗""面试难吗"）单独拿去检索一定跑偏，
                # 宁可少答一件，也不要把无关文件混进来。
                if len(p.strip()) < 5:
                    continue
                for h in R.search(p, top=12):
                    k = (h["chunk"].get("doc_uid"), h["chunk"].get("chunk_id"))
                    if k not in pool or h["score"] > pool[k]["score"]:
                        pool[k] = h
            if pool:
                hits = sorted(pool.values(), key=lambda x: -x["score"])
                recovered = True

    if not hits:
        return {"ok": False, "files": [], "clauses": [], "reason": "no-hit",
                "suggest": suggest(q, 4)["items"]}

    # 弱相关兜底：宁可说没找到，也不把无关文件端上去。
    # 两个条件都要看，任一个单独都不够准：
    #   score 过低 —— "快递驿站几点关门"首条只有 5.5，正常问题在 27 以上；
    #   标题对不上 —— "退役军人转专业"首条是兵役登记规定，只碰上"役"一个字。
    # 标题看"命中几个字"而不是覆盖率：多意图的长查询（"转专页…绩点…面试…"）
    # 按比例算必然很低，那样会把问对的问题也一起拒掉。
    if _weak(q, hits) and not recovered:
        weblearn.log_miss(q, "keyword-weak", hits[0]["score"], hits[0]["coverage"])
        return {"ok": False, "files": [], "clauses": [], "reason": "low-relevance",
                "suggest": suggest(q, 4)["items"]}

    files, seen = [], set()
    qchars_of = lambda s: sum(1 for ch in q if not ch.isspace() and ch in s)
    for h in hits:
        c = h["chunk"]
        uid = c.get("doc_uid")
        if not uid or uid in seen:
            continue
        seen.add(uid)
        sents = _split_sent(c.get("text") or "")
        # 摘要优先取命中关键词最多的那句，取不到再退回首句
        best = max(sents, key=qchars_of) if sents else ""
        if not best or qchars_of(best) == 0:
            best = sents[0] if sents else (c.get("text") or "")[:120]
        files.append({
            "uid": uid,
            "title": c.get("title") or "(无标题)",
            "dept": dept_name(c.get("dept")),
            "date": c.get("date") or "",
            "url": c.get("url") or "",
            "source": c.get("source") or "",
            "score": h["score"],
            "chunk_id": c.get("chunk_id"),
            # 摘要所在原句在本文中的序号，前端据此定位到条款
            "snippet_no": next((i for i, s in enumerate(sents, 1) if s == best), 1),
            "snippet": best,
        })
        if len(files) >= n_files:
            break

    qchars = [ch for ch in q if not ch.isspace()]
    clauses, used = [], set()
    for h in hits:
        c = h["chunk"]
        sents = _split_sent(c.get("text") or "")
        for si, s in enumerate(sents, 1):
            hit_n = sum(1 for ch in qchars if ch in s)
            if hit_n < 2:
                continue
            key = s[:40]
            if key in used:
                continue
            used.add(key)
            clauses.append({
                "text": s,
                "title": c.get("title") or "(无标题)",
                "dept": dept_name(c.get("dept")),
                "date": c.get("date") or "",
                "url": c.get("url") or "",
                "uid": c.get("doc_uid") or "",
                # 条款级定位：文件内第几块、第几段。有了这两个数，
                # 前端才能把"出处"从文件级往下压到具体那一句。
                "chunk_id": c.get("chunk_id"),
                "sent_no": si,
                "no": len(clauses) + 1,
            })
            if len(clauses) >= n_clauses:
                break
        if len(clauses) >= n_clauses:
            break

    return {"ok": True, "q": q, "files": files, "clauses": clauses,
            "year_note": _year_note(q, hits)}


def _tree_data():
    """文件树：一级=年份，二级=发文部门，三级=文件。"""
    docs, chunks = {}, {}
    for d in R.docs:
        uid = d.get("doc_uid")
        if not uid:
            continue
        chunks[uid] = chunks.get(uid, 0) + 1
        if uid in docs:
            continue
        y = str(d.get("year") or "").strip()
        if not y.isdigit():
            dt = str(d.get("date") or "")
            y = dt[:4] if len(dt) >= 4 and dt[:4].isdigit() else "未标注"
        docs[uid] = {
            "uid": uid,
            "title": d.get("title") or "(无标题)",
            "dept": dept_name(d.get("dept")) or "未标注",
            "date": d.get("date") or "",
            "year": y,
            "url": d.get("url") or "",
            "source": d.get("source") or "",
        }
    tree = {}
    for uid, d in docs.items():
        d["chunks"] = chunks.get(uid, 1)
        node = tree.setdefault(d["year"], {})
        node.setdefault(d["dept"], []).append(d)
    years = []
    order = sorted([k for k in tree if k != "未标注"], reverse=True)
    if "未标注" in tree:
        order.append("未标注")
    for y in order:
        depts = []
        for name in sorted(tree[y].keys(), key=lambda k: (-len(tree[y][k]), k)):
            items = sorted(tree[y][name], key=lambda x: x["date"], reverse=True)
            depts.append({"name": name, "count": len(items), "docs": items})
        years.append({"year": y, "count": sum(d["count"] for d in depts), "depts": depts})
    return {"years": years, "total": len(docs)}


def answer(q, emit=None):
    """带进程内 LRU 缓存的问答入口；拒答和异常结果不缓存。"""
    global _CACHE_HITS, _TOTAL_IN, _TOTAL_CACHED, _TOTAL_OUT, _USAGE_CALLS
    _cache_prepare()
    key = _norm_q(q)
    if key in _ANSWER_CACHE:
        _CACHE_HITS += 1
        cached = _ANSWER_CACHE.pop(key)
        _ANSWER_CACHE[key] = cached
        result = _copy_result(cached)
        result["usage"] = {"in": 0, "cached": 0, "out": 0}
        result["cached"] = True
        return result
    llm.LAST_USAGE.clear()
    result = _answer_uncached(q, emit)
    usage = _usage_now()
    if usage["in"] or usage["cached"] or usage["out"]:
        _TOTAL_IN += usage["in"]
        _TOTAL_CACHED += usage["cached"]
        _TOTAL_OUT += usage["out"]
        _USAGE_CALLS += 1
    result["usage"] = usage
    result["cached"] = False
    if result.get("ok"):
        saved = _copy_result(result)
        saved.pop("usage", None)
        saved.pop("cached", None)
        _ANSWER_CACHE[key] = saved
        _ANSWER_CACHE.move_to_end(key)
        while len(_ANSWER_CACHE) > _CACHE_MAX:
            _ANSWER_CACHE.popitem(last=False)
    return result


MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8", ".webp": "image/webp",
        ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml",
        ".ico": "image/x-icon", ".json": "application/json; charset=utf-8"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 页面与脚本必须每次校验：之前对 HTML 也发 max-age=86400，
        # 手机打开一次后 24 小时内拿到的都是旧版，改了代码也不生效。
        if code != 200:
            cc = "no-cache"
        elif ctype.startswith(("application/json", "text/html", "text/javascript", "application/javascript")):
            cc = "no-store, must-revalidate"
        else:
            cc = "public, max-age=86400"
        self.send_header("Cache-Control", cc)
        self.end_headers()
        self.wfile.write(data)

    def _static(self, rel):
        """只允许访问 web 目录下的文件，挡掉 ../ 穿越"""
        fp = os.path.abspath(os.path.join(WEB, rel.lstrip("/")))
        if not fp.startswith(os.path.abspath(WEB) + os.sep) or not os.path.isfile(fp):
            return False
        ext = os.path.splitext(fp)[1].lower()
        self._send(200, open(fp, "rb").read(), MIME.get(ext, "application/octet-stream"))
        return True

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        if path == "/go":
            # 稳定入口：每次跳到一个带版本戳的地址，绕开手机里的旧缓存。
            # 书签存这个地址即可，改完页面不用再换 URL。
            fp = os.path.join(WEB, "index.html")
            try:
                ver = str(int(os.path.getmtime(fp)))
            except OSError:
                ver = "0"
            self.send_response(302)
            # 除版本戳外，其余参数（如 ?q=问题）原样带过去，语音/二维码入口直接出结果
            rest = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k != "v"]
            loc = "/?" + urlencode([("v", ver)] + rest)
            self.send_header("Location", loc)
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path in ("/", "/index.html"):
            fp = os.path.join(WEB, "app.html")
            if not os.path.exists(fp):
                return self._send(500, "缺少 web/app.html", "text/plain; charset=utf-8")
            return self._send(200, open(fp, "rb").read(), "text/html; charset=utf-8")
        if path in ("/classic", "/classic/", "/map", "/map/"):
            fp = os.path.join(WEB, "index.html")
            if not os.path.exists(fp):
                return self._send(500, "缺少 web/index.html", "text/plain; charset=utf-8")
            return self._send(200, open(fp, "rb").read(), "text/html; charset=utf-8")
        if path == "/api/keyword":
            q = (parse_qs(u.query).get("q") or [""])[0].strip()
            if not q:
                return self._send(200, {"ok": False, "files": [], "clauses": []})
            return self._send(200, _keyword(q))
        if path == "/api/tree":
            return self._send(200, _tree_data())
        if path == "/api/suggest":
            q=(parse_qs(u.query).get('q') or [''])[0]
            try: n=int((parse_qs(u.query).get('n') or ['6'])[0])
            except ValueError: n=6
            return self._send(200,suggest(q,n))
        if path == "/api/status":
            learn = weblearn.stats()
            web_docs = len({d.get("doc_uid") for d in R.docs
                            if str(d.get("doc_uid", "")).startswith("web_")})
            fact_docs = len({d.get("doc_uid") for d in R.docs
                             if d.get("source") in ("fact", "official", "faq")})
            n_depts, y_min, y_max = _corpus_meta()
            return self._send(200, {
                "llm": bool(CFG.get("api_key")),
                "model": CFG.get("model", ""),
                # 语料会持续增长，stats.json 是上次切块时的快照；
                # 以内存里实际的检索库为准，否则页面统计会一直滞后。
                "docs": len({d.get("doc_uid") for d in R.docs}),
                "chunks": len(R.docs),
                "chars": STATS.get("总中文字数", 0),
                "places": len(campus_places.PLACES),
                "depts": n_depts,
                "year_min": y_min,
                "year_max": y_max,
                "web_docs": web_docs,
                "fact_docs": fact_docs,
                "web_cache": learn["cache"],
                "misses": learn["misses"],
                "cache_hits": _CACHE_HITS,
                "cache_size": len(_ANSWER_CACHE),
                "total_in": _TOTAL_IN,
                "total_cached": _TOTAL_CACHED,
                "total_out": _TOTAL_OUT,
                "avg_in": round(_TOTAL_IN / _USAGE_CALLS, 1) if _USAGE_CALLS else 0,
                "avg_out": round(_TOTAL_OUT / _USAGE_CALLS, 1) if _USAGE_CALLS else 0,
                "feedback_total": _feedback_total(),
            })
        if path == "/api/misses":
            return self._send(200, {"items": weblearn.misses(),
                                    "total": weblearn.stats()["misses"]})
        if path == "/api/feedback/list":
            items = _feedback_items()
            return self._send(200, {"items": items, "total": _feedback_total()})
        if path == "/api/places":
            return self._send(200, {
                "campus": campus_places.CAMPUS,
                "places": campus_places.all_places(),
            })
        if path == "/api/place":
            q = (parse_qs(u.query).get("q") or [""])[0]
            found = campus_places.resolve(q)
            return self._send(200, {"q": q, "places": [campus_places.public(p) for p in found[:8]]})
        if path.startswith("/assets/") and self._static(path):
            return
        self._send(404, {"error": "not found"})

    def _stream_ask(self, q):
        """SSE 流式问答。

        没有 Content-Length，靠关闭连接结束响应——前端必须用 fetch 的
        ReadableStream 边读边渲染，不能 await r.json()，那样等于没流式。
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def w(ev):
            try:
                self.wfile.write(("data: %s\n\n" % json.dumps(ev, ensure_ascii=False)).encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        try:
            res = answer(q, emit=w)
            ev = dict(res) if isinstance(res, dict) else {"ok": False}
            ev["citations"] = _slim_cites(ev.get("citations"))
            ev["type"] = "final"
            w(ev)
        except Exception as e:
            w({"type": "error", "msg": "检索失败：%s" % e})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/feedback":
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                return self._send(400, {"error": "请求体不是合法 JSON"})
            if not isinstance(body, dict):
                return self._send(400, {"error": "请求体必须是 JSON 对象"})
            q_value = body.get("q")
            if not isinstance(q_value, str):
                return self._send(400, {"error": "问题必须是字符串"})
            q = q_value.strip()
            if not q or len(q) > 200:
                return self._send(400, {"error": "问题不能为空且不能超过 200 字"})
            if not isinstance(body.get("citation_titles"), list):
                return self._send(400, {"error": "citation_titles 必须是列表"})
            row = {"q": q, "citation_titles": body["citation_titles"],
                   "reason": str(body.get("reason") or "").strip(),
                   "kind": "answer" if body.get("kind") == "answer" else "wrong"}
            with open(FEEDBACK_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            return self._send(200, {"ok": True, "feedback_total": _feedback_total()})
        if path == "/api/ask/stream":
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                return self._send(400, {"error": "请求体不是合法 JSON"})
            q_value = body.get("q") if isinstance(body, dict) else None
            if not isinstance(q_value, str):
                return self._send(400, {"error": "问题必须是字符串"})
            q = q_value.strip()
            if not q:
                return self._send(400, {"error": "问题为空"})
            if len(q) > 200:
                return self._send(400, {"error": "问题过长，请精简到 200 字以内"})
            return self._stream_ask(q)
        if path != "/api/ask":
            return self._send(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return self._send(400, {"error": "请求体不是合法 JSON"})
        if not isinstance(body, dict):
            return self._send(400, {"error": "请求体必须是 JSON 对象"})
        q_value = body.get("q")
        if not isinstance(q_value, str):
            return self._send(400, {"error": "问题必须是字符串"})
        q = q_value.strip()
        if not q:
            return self._send(400, {"error": "问题为空"})
        if len(q) > 200:
            return self._send(400, {"error": "问题过长，请精简到 200 字以内"})
        try:
            self._send(200, answer(q))
        except Exception as e:
            self._send(500, {"error": "检索失败：%s" % e})


def _slim_cites(cites):
    """流式推给前端的引用：去掉 evidence 这类只给模型看的大字段。"""
    keep = ("no", "title", "source", "dept", "date", "url", "page", "relevance",
            "snippet", "doc_uid", "chunk_id", "sent_no")
    return [{k: c.get(k) for k in keep} for c in (cites or [])]


def _lan_ips():
    """列出本机可被手机/其他设备访问的局域网地址。"""
    out = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.5.5.5", 80))          # 不发包，只为让系统选出口网卡
        out.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        host = socket.gethostbyname_ex(socket.gethostname())[2]
        out += [h for h in host if not h.startswith("127.")]
    except Exception:
        pass
    seen, ips = set(), []
    for ip in out:
        if ip not in seen:
            seen.add(ip)
            ips.append(ip)
    return ips


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    # 默认只听本机；传 --lan 或设环境变量 NTU_LAN=1 才开放给局域网（手机同 WiFi 访问）
    lan = "--lan" in sys.argv or os.environ.get("NTU_LAN") == "1"
    host = "0.0.0.0" if lan else "127.0.0.1"
    print("语料 %d 块 | 文档 %d 篇 | 大模型: %s"
          % (len(R.docs), STATS.get("文档数", 0), "已接入 " + CFG.get("model", "") if CFG.get("api_key") else "未配置（检索模式）"))
    print("服务地址: http://127.0.0.1:%d" % port)
    if lan:
        for ip in _lan_ips():
            print("手机访问: http://%s:%d  （需与电脑同一 WiFi）" % (ip, port))
        print("提示: Windows 防火墙首次会弹窗，选「专用网络」允许即可；本机防火墙需放行 %d 端口" % port)
    else:
        print("（局域网访问未开启；需要手机访问请用 server.py %d --lan 启动）" % port)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()


