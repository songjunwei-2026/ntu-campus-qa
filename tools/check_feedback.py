#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T14 校验：反馈机制补「我知道，我告诉你」双模式 + /api/feedback/list 只读接口。

用标准库，不依赖第三方。任何一项 FAIL 都说明没改对。
用法：python tools/check_feedback.py
"""
import io
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(BASE, "server.py")
INDEX = os.path.join(BASE, "web", "index.html")

fails = []
passes = []


def chk(name, cond, detail=""):
    if cond:
        passes.append(name)
        print("PASS  " + name)
    else:
        fails.append((name, detail))
        print("FAIL  " + name + (("    <- " + detail) if detail else ""))


def read(p):
    with io.open(p, encoding="utf-8") as f:
        return f.read()


def main():
    if not os.path.exists(SERVER) or not os.path.exists(INDEX):
        print("找不到 server.py 或 web/index.html，请在项目根目录下运行")
        return 2

    s = read(SERVER)
    h = read(INDEX)

    # ---------- 后端 ----------
    chk("server.py 有 /api/feedback/list 路由",
        '"/api/feedback/list"' in s,
        "没找到这个路由字符串")

    chk("server.py 有读反馈文件的函数 _feedback_items",
        "_feedback_items" in s,
        "没有这个函数")

    chk("server.py 反馈写入带 kind 白名单",
        '"kind": "answer" if body.get("kind") == "answer" else "wrong"' in s,
        "写入行里没有 kind 字段或写法不符")

    chk("server.py 没有对 /api/feedback 的 GET 误解处理",
        s.count('"/api/feedback"') == 1,
        '"/api/feedback" 出现 %d 次（应为 1，多出来多半是加了 GET 分支）' % s.count('"/api/feedback"'))

    # ---------- 前端 ----------
    i = h.find("function bindFeedback")
    j = h.find("async function ask")
    chk("web/index.html 里有 bindFeedback 函数", i >= 0 and j > i)
    if i >= 0 and j > i:
        fn = h[i:j]
        chk("bindFeedback 里有 d.ok 判断",
            "if(!d.ok)" in fn, "没找到 if(!d.ok)")
        chk("查不到时按钮文案是「我知道，我告诉你」",
            '"我知道，我告诉你"' in fn, "没找到该文案")
        chk("查不到时 placeholder 改过",
            "请写下正确答案" in fn, "没有替换 placeholder")
        chk("提交时带 kind 字段",
            'kind:d.ok?"wrong":"answer"' in fn, "没找到 kind 三元")
        chk("查不到时提交成功文案是「已收到，谢谢补充」",
            '"已收到，谢谢补充"' in fn, "没找到该文案")
        chk("bindFeedback 花括号平衡",
            fn.count("{") == fn.count("}"),
            "开 %d 个闭 %d 个" % (fn.count("{"), fn.count("}")))

    # ---------- 兼容性：禁用特性 ----------
    chk("没有 ?? 空值合并运算符", "??" not in h, "旧内核会白屏")
    chk("没有 ?. 可选链", "?." not in h, "旧内核会白屏")
    chk("没有 flatMap", "flatMap" not in h, "旧内核不支持")

    # ---------- 结构完整性 ----------
    chk("span 开闭配对",
        h.count("<span") == h.count("</span>"),
        "开 %d 闭 %d" % (h.count("<span"), h.count("</span>")))
    chk("div 开闭配对",
        h.count("<div") == h.count("</div>"),
        "开 %d 闭 %d" % (h.count("<div"), h.count("</div>")))
    # 先把所有合法 span 标签剥掉，剩下的 an> 就是被吞的残渣（上次事故：</span> → 22an>）
    rest = re.sub(r"</?span[^>]*>", "", h)
    bad_tag = re.findall(r".{0,10}an>", rest)
    chk("没有标签被吞的残渣", not bad_tag,
        "发现 %d 处孤立 an>：%s" % (len(bad_tag), " / ".join(bad_tag[:3])))

    # ---------- server.py 语法 ----------
    try:
        compile(s, SERVER, "exec")
        chk("server.py 语法正确（compile 通过）", True)
    except SyntaxError as e:
        chk("server.py 语法正确（compile 通过）", False, "第 %s 行：%s" % (e.lineno, e.msg))

    # ---------- 版本号 ----------
    m = re.search(r"版本 (\d{4}-\d{2}-\d{2}-\d+)", h)
    chk("footer 版本号读到", m is not None, "没找到版本号")
    if m:
        print("      版本号 = " + m.group(1))

    print("")
    print("结果：%d 项 PASS，%d 项 FAIL" % (len(passes), len(fails)))
    if fails:
        print("未通过：")
        for n, d in fails:
            print("  - " + n + (("  (" + d + ")") if d else ""))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
