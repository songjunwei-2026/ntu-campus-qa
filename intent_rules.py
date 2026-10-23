# -*- coding: utf-8 -*-
"""意图拦截规则。

evidence 中普通字符串按“包含”匹配；以 ``re:`` 开头的字符串按正则匹配。
规则中的 ``{event}`` 会替换为 ask 正则捕获的事件词。
"""

RULES = [
    {
        "name": "clock_detail",
        "ask": r"几点|时刻表|开门|关门|开放时间|发车|班次",
        "evidence": [
            r"re:\d{1,2}\s*[:：]\s*\d{2}",
            "时刻表", "发车", "班次", "作息时间", "开放时间", "开门", "关门", "上课时间",
        ],
        "reason": "intent-mismatch:clock",
    },
    {
        "name": "calendar_detail",
        "ask": r"什么时候|什么时间|何时|校历|开学",
        "evidence": ["第1学期", "第一学期", "第2学期", "第二学期", "第3学期", "第三学期",
                     "第4学期", "第四学期", "每学年", "每学期", "校历", "开学", "日期", "受理", "报名",
                     r"re:\d{1,2}\s*月"],
        "reason": "intent-mismatch:calendar",
    },
    {
        "name": "bus_detail",
        "ask": r"校车",
        "evidence": ["校车", "班车", "发车", "班次"],
        "reason": "intent-mismatch:bus",
    },
    {
        "name": "event_date",
        "ask": r"(?P<event>军训|寒假|暑假|报到)",
        "evidence": [r"re:{event}.{{0,25}}(\d{{1,2}}\s*月\s*\d{{1,2}}\s*日|\d{{1,2}}\s*[:：]\s*\d{{2}})"],
        "reason": "intent-mismatch:event-date",
    },
    {
        "name": "calendar_relation",
        "ask": r"校历",
        "evidence": [r"re:校历.{0,30}(开学|上课)|开学.{0,30}校历"],
        "reason": "intent-mismatch:calendar-relation",
    },
    {
        "name": "fee_amount",
        "ask": r"(?:学费|住宿费).*(?:多少|多少钱|标准)|(?:多少|多少钱|标准).*?(?:学费|住宿费)",
        "evidence": [r"re:{item}.{0,20}\d+(?:\.\d+)?\s*元"],
        "reason": "intent-mismatch:fee",
    },
]
