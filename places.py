"""校园地点库：加载、模糊匹配、从问题里判断"问的是不是地方"。

设计原则：宁可少答，不要乱标。只有问题确实在问方位/位置/怎么去时，
才返回地点；问"教室里能不能吃东西"这种规定类问题不返回地点卡。
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "places.json")

# 方位/位置类疑问词——出现这些词才认为用户想找地方
LOCATIVE = (
    "在哪", "位置", "怎么走", "怎么去", "如何去", "到哪里", "哪儿", "哪里", "哪个楼",
    "几楼", "几层", "几号", "哪一栋", "哪栋", "远不远", "附近", "朝哪", "方向",
    "带我去", "导航", "地图", "找一下", "怎么找", "门在哪", "怎么进",
)

# 这些词出现，说明问的是"规定"而不是"位置"
RULE_HINT = ("能不能", "可以吗", "是否允许", "规定", "办法", "条例", "处罚", "标准",
             "条件", "流程", "申请", "几点", "多少", "怎么办理", "需要什么",
             "吗", "可以", "允许", "禁止", "能否", "有没有")

# "交材料/送到/提交至" 属于跑腿动作，反而说明用户要找那间屋子
DELIVERY = ("交到", "交至", "送到", "送至", "提交", "报送", "盖章", "办理")

# 只说了类别、没点名具体地点时，给候选列表
CATEGORY_WORDS = (
    ("食堂", "餐饮"), ("餐厅", "餐饮"), ("饭堂", "餐饮"),
    ("宿舍", "生活"), ("公寓", "生活"), ("宿舍区", "生活"), ("学生公寓", "生活"),
    ("学生社区", "社区"), ("社区", "社区"), ("服务站", "社区"),
    ("教学楼", "教学"), ("教室", "教学"),
    ("操场", "运动"), ("球场", "运动"), ("足球", "运动"), ("篮球", "运动"),
    ("学院", "学院"), ("校门", "出入口"), ("大门", "出入口"), ("门", "出入口"),
    ("艺术馆", "场馆"), ("图书馆", "学习"),
    ("事务大厅", "办事"), ("办事大厅", "办事"), ("服务中心", "办事"),
    ("校医院", "医疗"), ("医院", "医疗"),
)

ROOM_RE = re.compile(r"([A-Z]{0,2}\d{3,4})\s*室")

# 从问句里把"没收录的地名"抠出来，用于兜底提示。
# 只取"在/到 + 地名"结构，或去掉方位词后剩下的名词片段，
# 避免把"哪里""怎么"之类也当成地名。
_STOP = {"哪里", "哪儿", "在哪", "位置", "怎么", "如何", "多少", "几点",
         "是什么", "有没有", "能不能", "可以", "吗", "呢", "的", "了", "我",
         "请问", "老师", "同学", "校园", "学校", "现在", "今天"}
_LOC_TAIL = re.compile(
    r"(?:在哪里|在哪儿|在哪|在哪里呢|的位置|怎么走|怎么去|如何去|"
    r"哪儿|哪里|在哪裏)\s*$")


def _guess_unknown(q):
    """猜问题里那个"库里没有"的地名，猜不出就返回 None。"""
    t = (q or "").strip()
    if not t:
        return None
    # "在XX哪里" / "XX在哪里" / "去XX怎么走"
    for pat in (r"(.+?)(?:在哪里|在哪儿|在哪|怎么走|怎么去|如何去|的位置)",
                r"(?:在|去|到|往)(.+?)(?:哪里|哪儿|哪|怎么走|怎么去)"):
        m = re.search(pat, t)
        if m:
            cand = _LOC_TAIL.sub("", m.group(1)).strip("的地得了吗呢")
            cand = re.sub(r"^(请问|我想问|问一下|想问一下|老师|同学)+", "", cand)
            if 2 <= len(cand) <= 12 and cand not in _STOP:
                return cand
    return None

CAT_ORDER = ["办事", "餐饮", "教学", "学习", "医疗", "生活", "社区", "运动",
             "出入口", "学院", "场馆", "文化"]


def _load():
    with open(DATA, encoding="utf-8") as f:
        raw = json.load(f)
    campus = raw["campus"]
    places = raw["places"]
    # 预计算：别名按长度降序，长别名优先，避免"教学楼"抢走"逸夫教学楼"
    index = []
    for p in places:
        names = [p["name"], p.get("short", "")] + p.get("aliases", [])
        for n in names:
            if n:
                index.append((n, p))
    index.sort(key=lambda x: -len(x[0]))
    return campus, places, index


CAMPUS, PLACES, ALIAS_INDEX = _load()
BY_ID = {p["id"]: p for p in PLACES}


def public(p):
    """给前端的精简结构（去掉降级用的内部字段）"""
    return {
        "id": p["id"], "name": p["name"], "short": p["short"],
        "lat": p["lat"], "lon": p["lon"], "category": p["category"],
        "building": p["building"], "floor": p.get("floor", ""),
        "rooms": p.get("rooms", ""), "hours": p.get("hours", ""),
        "desc": p.get("desc", ""), "route": p.get("route", ""),
    }


def all_places():
    return [public(p) for p in PLACES]


def public_list(ps):
    return [public(p) for p in ps]


# 未知地名的字面提示 -> 分类。用于兜底时推荐同类已收录地点。
_HINT_CAT = (
    ("楼", "教学"), ("馆", "场馆"), ("堂", "办事"), ("厅", "办事"),
    ("中心", "办事"), ("服务", "办事"), ("社", "社区"), ("园", "社区"),
    ("园", "生活"), ("舍", "生活"), ("宿", "生活"), ("公寓", "生活"),
    ("操", "运动"), ("场", "运动"), ("球", "运动"), ("跑", "运动"),
    ("院", "学院"), ("门", "出入口"), ("口", "出入口"), ("校", None),
    ("医", "医疗"), ("图", "学习"), ("书", "学习"), ("教", "教学"),
)


def nearby_for(word):
    """给一个没收录的地名，返回最可能相关的已收录地点。"""
    if not word:
        return []
    # 命中字面提示就按分类找
    for ch, cat in _HINT_CAT:
        if ch in word:
            if cat:
                out = [x for x in PLACES if x["category"] == cat]
                if out:
                    return out
            break
    # 退一步：找别名/描述里共享 2 字片段的
    head = word[:2]
    if len(head) >= 2:
        out = [x for x in PLACES
               if head in x["name"] or any(head in a for a in x.get("aliases", []))]
        if out:
            return out
    # 最后给几个覆盖面最广的（办事、食堂、教学），总比空列表有用
    out = [x for x in PLACES if x["category"] in ("办事", "餐饮", "教学", "出入口")]
    return out[:6]


def has_locative(q):
    return any(w in q for w in LOCATIVE)


def has_rule_hint(q):
    return any(w in q for w in RULE_HINT)


def match_place(q, allow_fuzzy=False):
    """从问题里找出被提到的地点，返回 (place, 命中的别名) 或 (None, None)。

    策略：最长别名优先。'逸夫教学楼在哪' 同时含 '教学楼' 和 '逸夫教学楼'，
    按长度排序后先命中后者，不会被公共教学楼抢走。
    """
    if not q:
        return None, None
    best = None
    for alias, p in ALIAS_INDEX:
        if alias in q:
            if best is None or len(alias) > len(best[1]):
                best = (p, alias)
            # 已按长度降序，第一个命中的就是最长的
            break
    if best:
        return best
    if allow_fuzzy:
        # 兜底：两字以上片段重合度
        for alias, p in ALIAS_INDEX:
            if len(alias) < 3:
                continue
            hit = sum(1 for i in range(len(alias) - 1) if alias[i:i + 2] in q)
            if hit >= len(alias) - 2:
                return p, alias
    return None, None


def intent(q):
    """判断问题是否在问地点。

    返回 dict：
      place    命中的地点（或 None）
      options  同类别候选（说了"食堂"但没说是哪个食堂时）
      wanted   是否建议出地点卡
      reason   判定依据，便于调试
    """
    p, alias = match_place(q)
    loc = has_locative(q)
    rule = has_rule_hint(q)
    room = ROOM_RE.search(q)
    delivery = any(w in q for w in DELIVERY)

    # 只说了类别（"食堂在哪"），给候选而不是硬塞一个；但问的是规定就不给
    if not rule:
        for word, cat in CATEGORY_WORDS:
            if word in q and not p:
                opts = [x for x in PLACES if x["category"] == cat]
                if opts and (loc or len(q) <= 8):
                    return {"place": None, "options": [public(x) for x in opts],
                            "wanted": True, "reason": "category:%s" % cat}

    if not p:
        # 只提到房间号（"337室在哪"）→ 反查哪个地点的 rooms 里有它
        if room:
            num = room.group(1)
            for x in PLACES:
                if num in x.get("rooms", ""):
                    return {"place": x, "options": [], "wanted": True,
                            "reason": "room-lookup:%s" % num, "room": room.group(0)}
        # 明显在问位置、但地点库里没有这个名字：不能装作没听懂，
        # 把地名抠出来交给前端提示"未收录"，并给同类候选。
        # 但"食堂几点关门"这类其实是问作息，不是问位置，仍走原拒答路径。
        if loc and not rule:
            return {"place": None, "options": [], "wanted": True,
                    "reason": "unknown-place", "unknown": _guess_unknown(q)}
        return {"place": None, "options": [], "wanted": False, "reason": "no-place"}

    # 带房间号（"综合楼337室"）→ 直接指楼，房间号本身就是最强信号
    if room:
        return {"place": p, "options": [], "wanted": True,
                "reason": "room:%s" % room.group(1), "room": room.group(0)}
    # 命中地点 + 有方位词 → 明确要导航
    if loc:
        return {"place": p, "options": [], "wanted": True, "reason": "locative+alias:%s" % alias}
    # 命中地点 + 跑腿动作 → 要找那栋楼
    if delivery:
        return {"place": p, "options": [], "wanted": True, "reason": "delivery+alias:%s" % alias}
    # 命中地点 + 规定类措辞 → 不导航（问的是规定）
    if rule:
        return {"place": p, "options": [], "wanted": False, "reason": "rule-hint+alias:%s" % alias}
    # 命中地点 + 短问句 → 大概率是光杆地名
    if len(q) <= 10:
        return {"place": p, "options": [], "wanted": True, "reason": "short-query+alias:%s" % alias}
    return {"place": p, "options": [], "wanted": False, "reason": "weak+alias:%s" % alias}


def resolve(q):
    """直接按地名查（地图页搜索框用），支持分类、编号、拼音外的子串"""
    q = (q or "").strip()
    if not q:
        return []
    out, seen = [], set()
    for alias, p in ALIAS_INDEX:
        if q in alias or alias in q:
            if p["id"] not in seen:
                seen.add(p["id"])
                out.append(p)
    for p in PLACES:
        if q in p.get("desc", "") or q in p.get("rooms", ""):
            if p["id"] not in seen:
                seen.add(p["id"])
                out.append(p)
    return out


def catalog():
    """按分类分组，供地图页左栏渲染"""
    groups = {}
    for p in PLACES:
        groups.setdefault(p["category"], []).append(public(p))
    return [{"category": c, "places": groups[c]}
            for c in CAT_ORDER if c in groups]


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    for q in ["教学楼在哪", "一食堂怎么走", "逸夫楼在哪", "综合楼337室交材料",
              "教室里能吃东西吗", "食堂", "食堂在哪", "图书馆开放时间", "宿舍怎么走"]:
        r = intent(q)
        name = r["place"]["name"] if r["place"] else ("候选 %d 个" % len(r.get("options", [])))
        print("%-20s -> 出卡=%-5s %-16s %s" % (q, r["wanted"], name, r["reason"]))
