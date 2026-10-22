# -*- coding: utf-8 -*-
"""官网栏目直采：把各站点"通知公告/规章制度/办事指南"抓下来，补进检索语料。

为什么不走搜索引擎：
  360/搜狗/必应对脚本都上了反爬（访问异常页 / 验证页），
  而校内站点本身是开放的、内容也更权威。抓一次存本地，
  以后检索不联网也能命中——这才是可复用的"学习"。

用法：
  python crawl_sites.py              # 默认种子，每个列表 2 页
  python crawl_sites.py --pages 3    # 更深
  python crawl_sites.py --dry        # 只列出将抓的链接，不写文件
"""
import json
import hashlib
import os
import re
import sys
import time
import urllib.parse

import websearch

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "corpus", "web_chunks.jsonl")
SEEN = os.path.join(BASE, "corpus", "web_seen.json")

# 站点 + 栏目。Sudy 站群规律：列表 list.htm / list2.htm ...，正文 /YYYY/MMDD/cNNNaNNNN/page.htm
SEEDS = [
    ("jwc", "教务处", "https://jwc.ntu.edu.cn/tzgg/list.htm"),
    ("jwc", "教务处", "https://jwc.ntu.edu.cn/gzzd/list.htm"),
    ("jwc", "教务处", "https://jwc.ntu.edu.cn/bszn/list.htm"),
    ("xgc", "学生工作处", "https://xgc.ntu.edu.cn/2613/list.htm"),
    ("xgc", "学生工作处", "https://xgc.ntu.edu.cn/xszz/list.htm"),
    ("hqbzb", "后勤保障部", "https://hqbzb.ntu.edu.cn/2675/list.htm"),
    ("hqbzb", "后勤保障部", "https://hqbzb.ntu.edu.cn/ptl/list.htm"),
    ("bwc", "保卫处", "https://bwc.ntu.edu.cn/tzgg/list.htm"),
    ("yjs", "研究生院", "https://yjs.ntu.edu.cn/xjgl/list.htm"),
    ("www", "学校主页", "https://www.ntu.edu.cn/tzgg/list.htm"),
]

ART = re.compile(r'href="(/[0-9]{4}/[0-9]{4}/c[0-9]+a[0-9]+/page\.htm)"[^>]*>(.*?)</a>', re.S)
# Sudy 列表项：<li class="news n1"> <a href='...' title='...'> ... <span class="news_meta">2026-10-15</span>
ITEM = re.compile(r"<li class=\"news[^\"]*\">(.*?)</li>", re.S)
LINK = re.compile(r"href=['\"]([^'\"]+?page\.htm)['\"]")
TTL = re.compile(r"title=['\"]([^'\"]{6,120})['\"]")
META = re.compile(r"news_meta\">\s*(\d{4}-\d{2}-\d{2})")
DATE = re.compile(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})")
CHUNK = 600
OVERLAP = 120


def _clean(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\s+", " ", s).strip()


def list_page(url):
    """返回 [(文章URL, 标题)]。Sudy 的列表项是服务端渲染的 li.news，直接解析。"""
    try:
        h = websearch._open(url)
    except Exception:
        return []
    out, seen = [], set()

    def add(href, title):
        href = urllib.parse.urljoin(url, href)
        if href in seen or len(title) < 6:
            return
        seen.add(href)
        out.append((href, title))

    for blk in ITEM.findall(h):
        lk = LINK.search(blk)
        if not lk:
            continue
        tt = TTL.search(blk)
        title = tt.group(1) if tt else _clean(blk)
        add(lk.group(1), title)
    if not out:  # 别的模板：退回裸链接匹配
        for m in ART.finditer(h):
            add(m.group(1), _clean(m.group(2)))
    return out


def article(url, title):
    """抓正文。Sudy 站群正文在 vsb_content / wp_articlecontent 里，取不到就退整页。"""
    try:
        h = websearch._open(url)
    except Exception:
        return None
    body = ""
    for pat in (r'<div[^>]+id="vsb_content"[^>]*>(.*?)</div>\s*</div>',
                r'<div[^>]+class="wp_articlecontent"[^>]*>(.*?)</div>',
                r'<div[^>]+id="content"[^>]*>(.*?)</div>'):
        m = re.search(pat, h, re.S)
        if m:
            body = m.group(1)
            break
    if not body:
        m = re.search(r"<body[^>]*>(.*?)</body>", h, re.S)
        body = m.group(1) if m else h
    body = re.sub(r"<(script|style)\b.*?</\1>", " ", body, flags=re.S | re.I)
    text = _clean(body)
    if len(re.findall(r"[\u4e00-\u9fa5]", text)) < 120:
        return None
    # 发布时间：正文里通常有一行"发布时间：2026-10-15"
    d = DATE.search(text[:800]) or DATE.search(h[:4000])
    date = "%s-%02d-%02d" % (d.group(1), int(d.group(2)), int(d.group(3))) if d else ""
    return text, date


def chunks(text):
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + CHUNK])
        if i + CHUNK >= len(text):
            break
        i += CHUNK - OVERLAP
    return out


def main():
    pages = 2
    dry = "--dry" in sys.argv
    for i, a in enumerate(sys.argv):
        if a == "--pages" and i + 1 < len(sys.argv):
            pages = int(sys.argv[i + 1])

    seen = set(json.load(open(SEEN, encoding="utf-8"))) if os.path.exists(SEEN) else set()
    rows, done = [], 0
    for code, dept, seed in SEEDS:
        for p in range(1, pages + 1):
            url = seed if p == 1 else seed.replace("list.htm", "list%d.htm" % p)
            arts = list_page(url)
            if not arts:
                continue
            for art_url, title in arts:
                if art_url in seen:
                    continue
                if dry:
                    print("[dry]", title[:40], art_url)
                    continue
                got = article(art_url, title)
                if not got:
                    continue
                text, date = got
                # 同一篇的所有块必须共用一个 doc_uid，否则跨块取上下文会失效
                uid = "web_" + hashlib.md5(art_url.encode("utf-8")).hexdigest()[:12]
                for n, c in enumerate(chunks(text)):
                    rows.append({
                        "doc_uid": uid,
                        "chunk_id": n,
                        "source": "web",
                        "title": title,
                        "date": date,
                        "year": int(date[:4]) if date else 0,
                        "dept": dept,
                        "tag": "官网页面",
                        "url": art_url,
                        "printed_page": 0,
                        "text": c,
                    })
                seen.add(art_url)
                done += 1
                time.sleep(0.4)
                if done % 20 == 0:
                    print("  已抓 %d 篇…" % done)
    if dry:
        return
    # 增量合并：已有的块保留，新抓的追加，不能覆盖历史
    old = []
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            old = [json.loads(l) for l in f if l.strip()]
    rows = old + rows
    if rows:
        tmp = OUT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, OUT)
        json.dump(sorted(seen), open(SEEN, "w", encoding="utf-8"), ensure_ascii=False)
    print("完成：本次新增页面 %d 篇，总块数 %d -> %s" % (done, len(rows), OUT))


if __name__ == "__main__":
    main()
