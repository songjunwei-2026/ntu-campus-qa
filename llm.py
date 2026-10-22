# -*- coding: utf-8 -*-
"""
LLM 调用封装（OpenAI 兼容接口，标准库实现，无第三方依赖）

配置优先级：环境变量 > config.json
  LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
"""
import os, json, urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(BASE, "config.json")

# 模型判定"资料答不了"时输出这个标记，服务端据此返回未收录
NO_ANSWER = "【未收录】"

SYSTEM_PROMPT = """你是南通大学校园事务助手。只能依据【参考资料】回答，禁止用资料以外的常识补充。

规则：
1. 第一句直接给结论（可以/不可以、需要/不需要、能办/不能办），再列条件或步骤。
2. 每条结论后用 [编号] 标注来源，如：申请条件包括……[2]。
3. 只有当资料里确实找不到对应规定时才输出 【未收录】 四个字，输出后不要再写任何内容。
   不要因为资料不完整、措辞与你预期不同而拒答——只要能提取出条件、流程、标准，就必须回答。
   但反过来，资料里出现相关词语不代表回答了问题：例如只写着"校长办公室印发"，
   并不等于知道校长是谁，这种情况要输出 【未收录】。
   【未收录】 和正文互斥：要么只输出标记，要么直接给正文，不能先输出标记再补充说明。
4. 金额、比例、时限、文号必须原样引用，不要换算、不要概括改写。
5. 同一事项有多份文件时，以文号年份较新的为准；旧文件若已写明废止或被替代，要指出来。
6. 用户默认是本科生，涉及本科生与研究生两类规定时，优先答本科生那套。
7. 简体中文，控制在 400 字以内，不要复述问题，不要客套话。
8. 问题问具体数值/时间，而资料只给了定性规定（例如只写"禁止使用大功率电器"、
   没写多少瓦），就把定性规定答出来并注明资料未写明具体数值——这种情况不要输出【未收录】。
9. 资料按与问题的相关度从高到低排列。如果资料1已经直接回答了问题（尤其直接给出地点、
   电话、办理入口这类答案），就以资料1为主答，后面的资料只作补充，不要用后面的内容把它顶掉。"""


def load_config():
    cfg = {
        # LLM_API_KEY 优先；没设就退回通用的 DEEPSEEK_API_KEY，省得再配一遍
        "api_key": os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", ""),
        "base_url": os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1"),
        "model": os.environ.get("LLM_MODEL", "deepseek-chat"),
    }
    if os.path.exists(CONFIG):
        try:
            cfg.update({k: v for k, v in json.load(open(CONFIG, encoding="utf-8")).items() if v})
        except Exception as e:
            print("[警告] config.json 读取失败: %s" % e)
    return cfg


WEB_RULES = """

这次的【参考资料】是从官网网页上实时抓下来的正文，不是学校正式发文，规则补充：
1. 只依据这些正文回答，不要用你自己的常识补。正文里没有的信息一律回答【未收录】。
2. 每条结论后面用 [编号] 标注来自第几段资料。
3. 网页常有导航、栏目标题等噪声，忽略它们，只取正文事实。
4. 资料若明确写的是其他校区（启东校区、钟秀校区等）或其他对象（研究生、自考生），
   必须说明"这是 XX 的规定"，不能当成啬园校区本科生的规定直接照搬。
5. 开头不要写"根据网页"，直接给结论。"""


def chat(question, context, cfg=None, timeout=45, web=False):
    cfg = cfg or load_config()
    if not cfg.get("api_key"):
        raise RuntimeError("未配置 API 密钥（LLM_API_KEY 或 DEEPSEEK_API_KEY）")
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT + (WEB_RULES if web else "")},
            {"role": "user", "content": "【参考资料】\n%s\n\n【问题】%s" % (context, question)},
        ],
        "temperature": 0,
        "max_tokens": 700,
    }
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + cfg["api_key"]},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()
