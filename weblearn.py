# -*- coding: utf-8 -*-
"""网页证据缓存：联网查到的东西留下来，下次同类问题直接复用。

存的是"证据"（官网页面正文），不是"答案"——答案每次重新生成，
这样换了问题措辞也不会拿到答非所问的旧结论；同时每一条都带
URL 和抓取时间，随时能核对是不是过期了。
"""
import json
import os
import re
import time

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, "web_cache.jsonl")
MISSES = os.path.join(BASE, "misses.jsonl")

MAX_ENTRIES = 500


def _norm(s):
    return re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", s or "").lower()


def _grams(s):
    s = _norm(s)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _sim(a, b):
    ga, gb = _grams(a), _grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _read(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def _append(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def save_pages(question, pages):
    """把一次联网查到的页面存下来。pages: [{title,url,snippet,text}]"""
    if not pages:
        return 0
    n = 0
    for p in pages:
        if not (p.get("text") or p.get("snippet")):
            continue
        _append(CACHE, {
            "q": question,
            "title": p.get("title", ""),
            "url": p.get("url", ""),
            "snippet": p.get("snippet", ""),
            "text": p.get("text", ""),
            "ts": time.strftime("%Y-%m-%d %H:%M"),
        })
        n += 1
    _trim()
    return n


def _trim():
    """超过上限就丢掉最老的，避免文件无限长。"""
    rows = _read(CACHE)
    if len(rows) <= MAX_ENTRIES:
        return
    keep = rows[-MAX_ENTRIES:]
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in keep:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, CACHE)


def recall(question, threshold=0.45, limit=3):
    """找历史上问过的高度相似问题，返回当时的页面证据。"""
    rows = _read(CACHE)
    if not rows:
        return []
    best = {}
    for r in rows:
        s = _sim(question, r.get("q", ""))
        if s < threshold:
            continue
        key = r.get("url", "")
        if key not in best or s > best[key][0]:
            best[key] = (s, r)
    hits = sorted(best.values(), key=lambda x: -x[0])[:limit]
    return [r for _, r in hits]


def log_miss(question, reason, score=0.0, coverage=0.0):
    """记下答不上的问题——这是后续补数据的清单。"""
    _append(MISSES, {
        "q": question,
        "reason": reason,
        "score": round(float(score or 0), 2),
        "coverage": round(float(coverage or 0), 3),
        "ts": time.strftime("%Y-%m-%d %H:%M"),
    })


def misses(limit=100):
    """按问题聚合，重复问的排在前面。"""
    rows = _read(MISSES)
    agg = {}
    for r in rows:
        k = r.get("q", "")
        if not k:
            continue
        cur = agg.setdefault(k, {"q": k, "count": 0, "last": "", "reasons": []})
        cur["count"] += 1
        cur["last"] = max(cur["last"], r.get("ts", ""))
        if r.get("reason") and r["reason"] not in cur["reasons"]:
            cur["reasons"].append(r["reason"])
    out = sorted(agg.values(), key=lambda x: (-x["count"], x["last"]))
    return out[:limit]


def stats():
    return {
        "cache": len(_read(CACHE)),
        "misses": len(_read(MISSES)),
        "cache_file": CACHE,
    }
