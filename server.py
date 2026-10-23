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

from rag import Retriever, dept_name
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


def _feedback_total():
    try:
        with open(FEEDBACK_PATH, encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


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
    """用户没提研究生，就别把研究生规定混进答案——本机用户默认是本科生。"""
    if re.search(r"研究生|硕士|博士|考研|推免|保研", q):
        return groups
    kept = [g for g in groups
            if not ("研究生" in g["best"]["chunk"]["title"]
                    and "本科" not in g["best"]["chunk"]["title"])]
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
MAX_GROUPS = 4        # 只把前 4 篇交给模型，控制上下文长度


def _doc_chunk_ids(g):
    """决定一条引用要喂多少原文。

    整篇喂只对短文档做：课程考核办法那种 16 段的长文件整篇塞进去，
    上下文会被稀释，模型反而答不出来，所以长文档退回"命中块 ±2 段"。
    """
    uid = g["best"]["chunk"]["doc_uid"]
    chunks = R.by_doc.get(uid, {})
    if len(chunks) <= MAX_DOC_CHUNKS:
        return sorted(chunks), 0
    return g["ids"], CTX_RADIUS


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


def _answer_uncached(q):
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
            "snippet": _extract_snippet(q, g["text"]),
            "evidence": g["text"],
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
        "places": P,
    }

    if not CFG.get("api_key"):
        res["notice"] = "当前为原文摘录模式：未接入大模型，系统无法判断这段原文是否真的回答了问题。"
        return res

    ctx = _build_context(groups)
    try:
        text = llm.chat(q, ctx, CFG)
    except Exception as e:
        res["notice"] = "模型调用失败，已回退为原文摘录（%s）。" % e
        return res

    if _is_refusal(text):
        # 真拒答了。不重试本地上下文：命中率高不等于文档真的回答了问题。
        # 但本地没有 ≠ 官网没有，先联网找一轮再说。
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
                    "expanded": res["expanded"], "places": P}
        if P["options"]:
            # 只说了类别（"食堂在哪"）：给出候选清单本身就是答案
            return {"ok": True, "mode": "place", "question": q,
                    "answer": _options_answer(P), "citations": [],
                    "notice": "位置信息来自校园地图底座；开放时间、窗口办理等以现场公示为准。",
                    "expanded": res["expanded"], "places": P}
        weblearn.log_miss(q, "llm-refused:no-place",
                          groups[0]["best"].get("score", 0),
                          groups[0]["best"].get("coverage", 0))
        return {"ok": False, "mode": "llm", "question": q,
                "answer": "本地语料和官网检索都没有找到相关内容，建议咨询相关职能部门。",
                "citations": [], "notice": "", "expanded": res["expanded"],
                "places": P}

    bad = _unverified(text, ctx)
    if bad:
        res["notice"] = "模型给出的 %d 处数值（%s）在原文中核对不上，已回退为原文摘录。" % (
            len(bad), "、".join(bad[:3]))
        return res

    res["mode"] = "llm"
    res["answer"] = _truncate_answer(_drop_marker(text))
    return res


def answer(q):
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
    result = _answer_uncached(q)
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
        if path in ("/", "/index.html", "/map", "/map/"):
            fp = os.path.join(WEB, "index.html")
            if not os.path.exists(fp):
                return self._send(500, "缺少 web/index.html", "text/plain; charset=utf-8")
            return self._send(200, open(fp, "rb").read(), "text/html; charset=utf-8")
        if path == "/api/status":
            learn = weblearn.stats()
            web_docs = len({d.get("doc_uid") for d in R.docs
                            if str(d.get("doc_uid", "")).startswith("web_")})
            fact_docs = len({d.get("doc_uid") for d in R.docs
                             if d.get("source") in ("fact", "official", "faq")})
            return self._send(200, {
                "llm": bool(CFG.get("api_key")),
                "model": CFG.get("model", ""),
                # 语料会持续增长，stats.json 是上次切块时的快照；
                # 以内存里实际的检索库为准，否则页面统计会一直滞后。
                "docs": len({d.get("doc_uid") for d in R.docs}),
                "chunks": len(R.docs),
                "chars": STATS.get("总中文字数", 0),
                "places": len(campus_places.PLACES),
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

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/feedback":
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                return self._send(400, {"error": "请求体不是合法 JSON"})
            q = (body.get("q") or "").strip()
            if not q or len(q) > 200:
                return self._send(400, {"error": "问题不能为空且不能超过 200 字"})
            if not isinstance(body.get("citation_titles"), list):
                return self._send(400, {"error": "citation_titles 必须是列表"})
            row = {"q": q, "citation_titles": body["citation_titles"],
                   "reason": str(body.get("reason") or "").strip()}
            with open(FEEDBACK_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            return self._send(200, {"ok": True, "feedback_total": _feedback_total()})
        if path != "/api/ask":
            return self._send(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return self._send(400, {"error": "请求体不是合法 JSON"})
        q = (body.get("q") or "").strip()
        if not q:
            return self._send(400, {"error": "问题为空"})
        if len(q) > 200:
            return self._send(400, {"error": "问题过长，请精简到 200 字以内"})
        try:
            self._send(200, answer(q))
        except Exception as e:
            self._send(500, {"error": "检索失败：%s" % e})


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


