# -*- coding: utf-8 -*-
"""联网兜底：本地语料答不上时，去官网实时取证据。

原则：
1. 只采信 ntu.edu.cn 域名下的结果，其他站点一律丢弃；
2. 拿不到正文就退回"搜索摘要"，摘要同样标注来源链接，不编造；
3. 全程只用标准库，不引入依赖，失败一律返回空而不是抛异常。
"""
import gzip
import html as html_mod
import io
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

ALLOWED_SUFFIX = ".ntu.edu.cn"
TIMEOUT = 12
MAX_PAGE_CHARS = 6000


def _open(url, referer=None):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    })
    if referer:
        req.add_header("Referer", referer)
    # 本机装了代理（HTTP_PROXY=127.0.0.1:60007），校内站点不能走代理
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=TIMEOUT) as r:
        raw = r.read()
        enc = (r.headers.get("Content-Encoding") or "").lower()
        if "gzip" in enc:
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
        elif "deflate" in enc:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                pass
        charset = "utf-8"
        m = re.search(r"charset=([\w-]+)", r.headers.get("Content-Type", ""), re.I)
        if m:
            charset = m.group(1)
        else:
            m = re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', raw[:3000], re.I)
            if m:
                charset = m.group(1).decode("ascii", "ignore")
        for cs in (charset, "utf-8", "gb18030"):
            try:
                return raw.decode(cs, "ignore")
            except (LookupError, UnicodeDecodeError):
                continue
        return raw.decode("utf-8", "ignore")


def _clean(s):
    s = re.sub(r"<[^>]+>", "", s or "")
    s = html_mod.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _allowed(url):
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return False
    return host == "ntu.edu.cn" or host.endswith(ALLOWED_SUFFIX)


# ---------- 来源分级 ----------
# 只采信官网还不够：同一站里既有正式文件，也有活动报道和人物特写。
# 回答政策问题必须落到"文件/通知"上，报道只能当线索。
# 判定只看 URL 栏目和标题，不读正文——校内站 SPA 多，正文常常抓不到，
# 用正文判级会让最该采信的文件页反而判不出等级。

# 标题里出现这些，基本可以确定是制度性文件
DOC_TITLE = re.compile(
    r"关于.{0,40}(通知|公告|公示|通告)"
    r"|(通知|公告|公示|通告)\s*$"
    r"|(管理办法|实施办法|实施细则|暂行规定|若干规定|工作条例|规章制度"
    r"|办法|规定|细则|条例|准则|章程|方案|意见|规范)")

# 官网里存放正式文件的栏目
DOC_COL = re.compile(r"/(?:tzgg|gzzd|bszn|zcwj|zdgk|xxgk|xzzf|zcfg)/")

# 宣传报道类：能提供事实（如"现任校长"），但不能当制度依据
NEWS_TITLE = re.compile(
    r"(记者|采访|专访|对话|讲述|事迹|侧记|纪实|剪影|风采|通讯|座谈|交流会"
    r"|调研|走访|仪式|开幕|闭幕|隆重|举行|喜报|捷报|荣获|获奖"
    r"|政协委员|人大代表)")

# 校情介绍页：不是文件，但属于官方权威口径（校区数、办学规模这类事实）
PROFILE_TITLE = re.compile(r"(简介|概况|掠影|现任领导|领导班子|学校沿革)")


def grade(page):
    """来源等级：3=官方文件/通知，2=官网页面（含校情介绍），1=宣传报道。

    返回 (等级, 人话标签)，标签直接显示在引用卡片上。
    """
    title = page.get("title") or ""
    url = page.get("url") or ""
    if DOC_COL.search(url) or DOC_TITLE.search(title):
        return 3, "官方文件/通知"
    if NEWS_TITLE.search(title):
        return 1, "官网报道"
    if PROFILE_TITLE.search(title):
        return 2, "官方校情介绍"
    return 2, "官网页面"


# ---------- 搜索 ----------

def _search_360(q, n):
    """360 搜索结果里的 <a data-mdurl> 就是真实地址，不必解跳转。

    页面里同一结果块内还有"导航""广告"等锚点，所以逐个锚点过滤，
    不能只看块里的第一个。
    """
    url = "https://www.so.com/s?q=" + urllib.parse.quote(q)
    h = _open(url)
    out, seen = [], set()
    for m in re.finditer(r'<a[^>]+data-mdurl="([^"]+)"[^>]*>(.*?)</a>', h, re.S):
        real = html_mod.unescape(m.group(1))
        if not _allowed(real):
            continue
        title = _clean(m.group(2))
        if not title:
            continue
        key = real.split("#")[0].rstrip("/")
        if key in seen:
            continue
        tail = h[m.end():m.end() + 2500]
        snip = re.search(r'data-result-summary[^>]*>(.*?)</span>', tail, re.S)
        snippet = _clean(snip.group(1)) if snip else ""
        if not snippet:
            d = re.search(r'<p class="res-desc">(.*?)</p>', tail, re.S)
            snippet = _clean(d.group(1)) if d else ""
        seen.add(key)
        out.append({"title": title, "url": real, "snippet": snippet, "engine": "360"})
        if len(out) >= n:
            break
    return out


def _search_sogou(q, n):
    url = "https://www.sogou.com/web?query=" + urllib.parse.quote(q)
    h = _open(url)
    out, seen = [], set()
    for m in re.finditer(r'<h3[^>]*class="vr-title"[^>]*>(.*?)</h3>', h, re.S):
        blk = m.group(1)
        a = re.search(r'href="([^"]+)"[^>]*>(.*?)</a>', blk, re.S)
        if not a:
            continue
        href = html_mod.unescape(a.group(1))
        if "/link?" in href:
            continue  # 搜狗的跳转链解不出真实地址，跳过
        if not _allowed(href):
            continue
        if href in seen:
            continue
        seen.add(href)
        out.append({"title": _clean(a.group(2)), "url": href, "snippet": "", "engine": "sogou"})
        if len(out) >= n:
            break
    return out


def search(q, n=4):
    """返回官网候选页，按引擎顺序尝试，全失败返回空列表。

    360 认 site: 且命中准，放第一；搜狗常返验证壳，作备用。
    （试过必应 RSS：能连上，但返回的十条全是百科/新闻站，
    对 ntu.edu.cn 召回为零，留着只会多一次超时，已撤。）
    """
    # 必须带 site: 限定——不加时 360 给的是视频/广告聚合页，且拿不到真实地址
    query = q if "site:" in q else "site:ntu.edu.cn " + q
    for fn in (_search_360, _search_sogou):
        try:
            hits = fn(query, n)
        except (urllib.error.URLError, socket.timeout, OSError, ValueError):
            hits = []
        if hits:
            return hits
    return []


# ---------- 取正文 ----------

DROP = re.compile(r"<(script|style|noscript|nav|footer|header)\b.*?</\1>", re.S | re.I)


def fetch_text(url, limit=MAX_PAGE_CHARS):
    """取页面正文。SPA 站点取不到内容时返回空串，由调用方退回搜索摘要。"""
    try:
        h = _open(url)
    except (urllib.error.URLError, socket.timeout, OSError, ValueError):
        return ""
    h = DROP.sub(" ", h)
    body = re.search(r"<body[^>]*>(.*?)</body>", h, re.S | re.I)
    if body:
        h = body.group(1)
    text = _clean(h)
    # 纯导航页（几乎没有中文正文）直接判空
    if len(re.findall(r"[\u4e00-\u9fa5]", text)) < 80:
        return ""
    return text[:limit]


# ---------- 挑相关句子 ----------

STOP = set("的了和是在有怎么哪里吗呢吧请问一下我你他它这那哪个什么时候可以需要")


def _grams(q):
    q = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", q)
    return {q[i:i + 2] for i in range(len(q) - 1)} - STOP


def pick(text, q, limit=1200):
    """按问题二元组命中的句子优先拼出证据段。"""
    g = _grams(q)
    if not text:
        return ""
    parts = re.split(r"(?<=[。；！？\n])", text)
    scored = []
    for i, s in enumerate(parts):
        s = s.strip()
        if len(s) < 8:
            continue
        hit = sum(1 for x in g if x in s)
        if hit:
            scored.append((hit, i, s))
    if not scored:
        return text[:limit]
    scored.sort(key=lambda x: (-x[0], x[1]))
    keep = sorted(scored[:12], key=lambda x: x[1])
    out = "".join(s for _, _, s in keep)
    return out[:limit]


# ---------- 对外入口 ----------

# 口语问题里的词，换成官网上真正会写的词，召回率差好几倍
EXPAND = {
    "开放时间": "开馆 闭馆 时间 周一",
    "几点": "时间 开始 结束",
    "关门": "闭馆 闭园 时间",
    "开学": "报到 注册 上课 时间",
    "放假": "假期 放假 时间 通知",
    "报销": "医疗 报销 流程 材料 比例",
    "校历": "学年 学期 校历 安排",
    "补办": "补办 挂失 流程 材料",
    "申请": "申请 条件 材料 流程",
    "条件": "条件 要求 规定",
    "怎么办": "流程 办理 材料",
}


def _variants(q):
    """按"更具体 → 更宽"生成查询。别加校区词，加了反而召回一堆无关通知。"""
    extra = " ".join(v for k, v in EXPAND.items() if k in q)
    vs = []
    if extra:
        vs.append("%s %s" % (q, extra))
    vs.append(q)
    return vs


def _useful(text, q):
    """判断抓回来的正文到底有没有料：中文太少或完全不含问题词就不算。"""
    if not text:
        return False
    if len(re.findall(r"[\u4e00-\u9fa5]", text)) < 120:
        return False
    g = _grams(q)
    return any(x in text for x in g)


LEAD = re.compile(r"^首页.{0,300}?(?:更多|通知公告)\s*")


def _window(raw, q, size=1400):
    """从页面正文里截"问题词最密"的一段。

    校内站页面开头一整屏是栏目导航（首页 机构设置 工作动态 …），
    整段丢给模型只会让它抓错重点，所以按命中密度取窗口。
    """
    if not raw:
        return ""
    g = _grams(q)
    if not g:
        return raw[:size]
    bins = {}
    for x in g:
        start = 0
        while True:
            i = raw.find(x, start)
            if i < 0:
                break
            bins[i // 200] = bins.get(i // 200, 0) + 1
            start = i + len(x)
    if not bins:
        return ""
    # 导航栏也会命中"工作""学生"这类词，但通篇没有句号；
    # 用句号数加权，能稳定把正文段顶上去。
    for b in list(bins):
        chunk = raw[b * 200:(b + 1) * 200]
        bins[b] += 3 * chunk.count("。")
    best = max(bins, key=lambda k: (bins[k], -k))
    lo = max(0, best * 200 - 300)
    cut = raw[lo:lo + size]
    # 掐掉开头残留的栏目名（连续出现、每段都很短且不带标点）
    parts = cut.split(" ")
    while parts and len(parts[0]) <= 8 and not re.search(r"[。：，；]", parts[0]):
        parts.pop(0)
    cut = " ".join(parts)
    return LEAD.sub("", cut)


def _rel(q, h):
    """候选相关度：标题权重高，摘要次之。摘要相关才说明这一页真是讲这事的。"""
    g = _grams(q)
    if not g:
        return 0
    title = h.get("title") or ""
    snip = h.get("snippet") or ""
    return sum(2 for x in g if x in title) + sum(1 for x in g if x in snip)


def lookup(question, want=3, max_pages=2):
    """返回 [{'title','url','snippet','text'}]。

    以搜索摘要为主证据（引擎自己截的相关段），正文只用来补充。
    校内站点慢、SPA 多，所以正文最多抓 max_pages 个。
    """
    seen, cands = set(), []
    for v in _variants(question):
        try:
            hits = search(v, n=6)
        except (urllib.error.URLError, socket.timeout, OSError, ValueError):
            hits = []
        for h in hits:
            u = urllib.parse.urlparse(h["url"])
            k = (u.hostname or "", u.path or "/")
            if k in seen:
                continue
            seen.add(k)
            cands.append(h)
        if len(cands) >= 8:
            break

    # 相关度 × 来源等级：同等相关时正式文件排前面，报道只当线索。
    # 权重不能悬殊太大，否则"问校长是谁"这种只能靠报道回答的问题会全被压到 0。
    GRADE_W = {3: 1.0, 2: 0.85, 1: 0.45}
    ranked = []
    for h in cands:
        base = _rel(question, h)
        if base <= 0:  # 一点都不沾边的不送进模型
            continue
        g, label = grade(h)
        h["grade"], h["grade_label"] = g, label
        ranked.append((base * GRADE_W.get(g, 0.8), h))
    ranked.sort(key=lambda x: -x[0])
    if not ranked:
        return []

    picked, fetched = [], 0
    for _, h in ranked:
        text = ""
        if fetched < max_pages:
            fetched += 1
            raw = fetch_text(h["url"])
            if _useful(raw, question):
                text = _window(raw, question)
                # 全是"一、二、三"清单或栏目名的窗口没有句子，不如直接用搜索摘要
                if text.count("。") < 2:
                    text = ""
            time.sleep(0.3)
        item = dict(h)
        item["text"] = text
        picked.append(item)
        if len(picked) >= want:
            break
    return picked

