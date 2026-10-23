# -*- coding: utf-8 -*-
"""页面静态校验：只用标准库，改完 web/index.html 跑一遍再提交。

用法：
    python tools/check_page.py                # 校验 web/index.html
    python tools/check_page.py 某路径.html     # 校验指定文件

存在意义：Codex 的"自检通过"只数过目标字符串。2026-10-23 它把 footer 的
`版本 2026-10-23-21</span>` 替换成 `版本 2026-10-23-22an>`，`</sp` 被吞掉，
而三条字符串计数全部通过。本脚本补的正是这一类"结构被改坏但目标值对"的检查。
"""
import sys, io, os, re
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "web", "index.html")

if not os.path.exists(PATH):
    print("FAIL  文件不存在: " + PATH)
    sys.exit(2)

src = io.open(PATH, encoding="utf-8").read()

ok = True
def chk(name, cond, extra=""):
    global ok
    print(("PASS  " if cond else "FAIL  ") + name + (("   " + extra) if extra else ""))
    if not cond:
        ok = False

class P(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.title = ""
        self.intitle = False
        self.meta = []
        self.links = []
    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "title":
            self.intitle = True
        elif tag == "meta":
            self.meta.append(d)
        elif tag == "link":
            self.links.append(d)
    def handle_endtag(self, tag):
        if tag == "title":
            self.intitle = False
    def handle_data(self, data):
        if self.intitle:
            self.title += data

p = P()
try:
    p.feed(src)
    p.close()
    chk("HTML 可被解析", True)
except Exception as e:
    chk("HTML 可被解析", False, repr(e))

# ---- head 元信息 ----
t = p.title.strip()
chk("title 非空且含品牌名", ("通晓" in t) and 4 <= len(t) <= 40, "实际=" + repr(t))

desc = [m for m in p.meta if m.get("name") == "description"]
chk("description 恰好 1 条", len(desc) == 1, "实际=%d" % len(desc))
if desc:
    v = desc[0].get("content") or ""
    chk("description 长度 40-120 字", 40 <= len(v) <= 120, "实际=%d 字" % len(v))

og = dict((m.get("property"), m.get("content")) for m in p.meta if (m.get("property") or "").startswith("og:"))
for k in ("og:type", "og:title", "og:description", "og:site_name"):
    chk("存在 " + k, k in og, ("值=" + repr(og[k]) if k in og else "缺失"))

icons = [l for l in p.links if l.get("rel") == "icon"]
chk("icon 恰好 1 条", len(icons) == 1, "实际=%d" % len(icons))
if icons:
    href = icons[0].get("href") or ""
    chk("icon 是 svg data URI", href.startswith("data:image/svg+xml,"), "前缀=" + repr(href[:26]))
    chk("icon 序列完整", href.endswith("%3C/svg%3E"), "结尾=" + repr(href[-14:]))
    chk("icon 内无裸 # 号", "#" not in href)

# ---- 版本号（只校验格式，值由任务单单独核对）----
vers = re.findall(r"版本 (\d{4}-\d{2}-\d{2}-\d{2})", src)
chk("版本号出现且仅 1 处", len(vers) == 1, "实际=%s" % vers)

# ---- CSS 设计变量完整（防止配色被改坏）----
need = ["--navy", "--navy-deep", "--red", "--bg", "--card", "--line",
        "--ink", "--ink-2", "--ink-3", "--serif", "--sans"]
missing = [v for v in need if (v + ":") not in src]
chk("CSS 变量 11 个齐全", not missing, "缺失=" + ",".join(missing) if missing else "")

# ---- 结构完整性（核心）----
for tag in ("html", "head", "body", "div", "span", "footer", "style", "script", "main"):
    o = len(re.findall(r"<" + tag + r"[\s>]", src))
    c = len(re.findall(r"</" + tag + r">", src))
    chk("标签配对 <%s>" % tag, o == c, "%d 开 / %d 闭" % (o, c))

chk("</html> 恰好 1 次", src.count("</html>") == 1, "实际=%d" % src.count("</html>"))
bad = re.findall(r"\d{2}an>", src)
chk("无被吞标签残渣（如 22an>）", not bad, "命中的=" + repr(bad[:3]))

print("")
print("版本号 = " + (vers[0] if vers else "未找到"))
print("结果：" + ("全部通过" if ok else "存在失败项"))
sys.exit(0 if ok else 1)
