# -*- coding: utf-8 -*-
"""抓官网的固定栏目页，写进事实层。

为什么单独做这个：这几页的 URL 是稳定的（学校概况、现任领导、教学作息时间），
但内容常年不变、搜索引擎又不一定收，靠联网搜索碰运气太浪费。
直接抓一次存下来，之后离线就能答，而且来源就是官网原文。

只处理**文字页**。校历是张 JPG（/_upload/...jpg），抓下来是二进制，
这里会明确跳过并提示——要它得人工录或用 OCR，不能假装抓到了。

用法:
  python fetch_fixed.py            # 抓取并写入
  python fetch_fixed.py --dry      # 只看抓到什么，不写入
"""
import hashlib
import json
import os
import re
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import websearch

OUT = os.path.join(BASE, "corpus", "facts_chunks.jsonl")

# (标题, URL, 正文起始锚点, 正文结束锚点, 说明)
# 起始/结束锚点用来剥掉页面顶部那一整块栏目导航——每页都有，
# 不剥掉的话模型会拿"校园邮箱 办公系统 远程接入"当正文。
PAGES = [
    (
        "南通大学现任领导（党委书记、校长、副校长）",
        "https://www.ntu.edu.cn/xrld_8080/list.htm",
        r"党委书记",
        r"啬园校区地址",
        "学校领导班子职务与姓名。问到校长、党委书记、副校长是谁时以此页为准。",
    ),
    (
        "南通大学教学作息时间表（各节课上课时间）",
        "https://www.ntu.edu.cn/2018/0228/c763a35845/page.htm",
        r"教学作息时间表（5月",
        r"校园黄页|啬园校区地址",
        "各节课上下课时间，分 5-9 月与 10-4 月两套作息。",
    ),
    (
        "南通大学学校简介（校区、学院、办学规模）",
        "https://www.ntu.edu.cn/xxjj/list.htm",
        r"南通大学（Nantong University）",
        r"啬园校区地址",
        "学校沿革、校区设置、学院数量、独立学院（杏林学院）等基本情况。",
    ),
]


def _slice(text, start_pat, end_pat):
    m = re.search(start_pat, text)
    if not m:
        return ""
    tail = text[m.start():]
    e = re.search(end_pat, tail)
    return (tail[:e.start()] if e else tail).strip()


def _split(text, size=520):
    """按句子切块，单块不超过 size 字。"""
    parts = re.split(r"(?<=[。；！？])", text)
    out, buf = [], ""
    for p in parts:
        if len(buf) + len(p) > size and buf:
            out.append(buf.strip())
            buf = p
        else:
            buf += p
    if buf.strip():
        out.append(buf.strip())
    return [x for x in out if len(x) > 30]


def _uid(url):
    return "off_" + hashlib.md5(url.encode("utf-8")).hexdigest()[:12]


def _existing():
    """读已有事实，保留人工录入的（source=fact），官方页每次重抓覆盖。"""
    if not os.path.exists(OUT):
        return []
    keep = []
    with open(OUT, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("source") == "fact":
                keep.append(r)
    return keep


def main():
    dry = "--dry" in sys.argv
    today = time.strftime("%Y-%m-%d")
    rows = _existing()
    ok = fail = 0

    for title, url, sp, ep, desc in PAGES:
        try:
            raw = websearch._open(url)
        except Exception as e:
            print("✗ %s\n  抓取失败：%s" % (title, type(e).__name__))
            fail += 1
            continue
        text = websearch._clean(raw)
        body = _slice(text, sp, ep)
        # 现任领导页的姓名是图片，alt 残留成"王建浦-->王建浦""陆俊杰-->吉明明"，先洗掉
        body = re.sub(r"([\u4e00-\u9fa5]{2,4})\s*-->\s*\1", r"\1", body)
        body = re.sub(r"\s*-->\s*", "、", body)
        body = re.sub(r"、{2,}", "、", body).strip()
        if len(body) < 40:
            print("✗ %s\n  正文没抓到（页面可能改版，锚点失效）。抓到的前 80 字：%s"
                  % (title, text[:80]))
            fail += 1
            continue
        body = "%s\n%s" % (body, desc)
        chunks = _split(body)
        print("√ %s\n  %d 字 / %d 块  %s" % (title, len(body), len(chunks), url))
        if dry:
            print("  预览：%s" % chunks[0][:120].replace("\n", " "))
            ok += 1
            continue
        for i, c in enumerate(chunks):
            rows.append({
                "doc_uid": _uid(url),
                "chunk_id": i,
                "source": "official",
                "title": title,
                "date": today,
                "year": int(today[:4]),
                "dept": "学校官网",
                "tag": "官网固定页",
                "url": url,
                "printed_page": 0,
                "text": c,
            })
        ok += 1
        time.sleep(0.4)

    print()
    print("说明：校级校历在官网是 JPG 图片"
          "（/_upload/article/images/4d/43/...jpg），本脚本抓不到文字，需要人工录入：")
    print('  python add_fact.py --title "2026-2027学年校历" --text "..." --dept "教务处"')

    if dry:
        print("\n（--dry 模式，未写入）成功 %d 页，失败 %d 页" % (ok, fail))
        return 0 if fail == 0 else 1
    if ok:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print("\n已写入 %s：共 %d 块（含人工录入 %d 条）"
              % (OUT, len(rows), len([r for r in rows if r.get("source") == "fact"])))
        print("重启 server.py 后生效。")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
