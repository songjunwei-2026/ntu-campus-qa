# -*- coding: utf-8 -*-
"""
LLM 调用封装（OpenAI 兼容接口，标准库实现，无第三方依赖）

配置优先级：环境变量 > config.json
  LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
"""
import os, json, time, urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(BASE, "config.json")

# 直连模型接口，不走系统代理。
# 本机为访问 GitHub 挂着代理，urllib 会默认读取 http_proxy 环境变量，
# 结果连 api.deepseek.com 也被塞进代理隧道，代理返回 502 后整题降级成原文摘录。
# 接口在国内可直连，绕开更稳；确实需要代理时设 LLM_USE_PROXY=1。
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}) if os.environ.get("LLM_USE_PROXY") != "1"
    else urllib.request.ProxyHandler())

# 模型判定"资料答不了"时输出这个标记，服务端据此返回未收录
NO_ANSWER = "【未收录】"

# 最近一次调用的 token 用量，供 /api/status 与成本排查读取
LAST_USAGE = {}

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
   电话、办理入口这类答案），就以资料1为主答，后面的资料只作补充，不要用后面的内容把它顶掉。
10. 回答开头必须明确写出用户所问事项的名称，例如“勤工助学申请需要……”“实验室安全要求包括……”，
    不要用“该项目”“相关活动”“这些要求”等模糊代称替代问题中的核心事项。
11. 问题包含“条件、要求、流程、材料、金额、时间”等多个要素时，按重要性优先回答前三项；
    其余要素归纳为一句话，资料没有写明的要素要明确说“原文未写明”，整体不超过 300 字。
12. 禁止反向推断。不能因为“资料里没看到某条限制”就说“未把 XX 列为排除项”“并未禁止 XX”
    “没有限制 XX”。资料里没有针对该事项的文件时（例如问“国家奖学金申请条件”，而资料只有
    课程考核办法），必须输出【未收录】——你没看到不等于规定里没有，学生按这句话去申请是要出事的。"""


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


def _build_payload(question, context, cfg, web, stream=False):
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT + (WEB_RULES if web else "")},
            {"role": "user", "content": "【参考资料】\n%s\n\n【问题】%s" % (context, question)},
        ],
        "temperature": 0,
        # 新模型默认带推理：思考 token 计入 max_tokens，配额吃满时正文会是空的
        # （实测 max_tokens=700、思考 557 token 时正文只剩 244 字，思考 700 时正文 0 字）。
        # 问答场景不需要思维链，关掉更快也更稳；旧模型会忽略这个字段。
        "thinking": {"type": "disabled"},
        "max_tokens": 600,
    }
    if stream:
        payload["stream"] = True
    return payload


def _request(cfg, payload):
    return urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + cfg["api_key"]},
    )


def chat(question, context, cfg=None, timeout=45, web=False):
    cfg = cfg or load_config()
    if not cfg.get("api_key"):
        raise RuntimeError("未配置 API 密钥（LLM_API_KEY 或 DEEPSEEK_API_KEY）")
    payload = _build_payload(question, context, cfg, web)

    def _post():
        with _OPENER.open(_request(cfg, payload), timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    # 偶发返回空正文（配额被思考吃光或被内容策略截断），重试一次再放弃
    for attempt in (1, 2):
        data = _post()
        choice = data["choices"][0]
        LAST_USAGE.clear()
        LAST_USAGE.update(data.get("usage") or {})
        text = (choice["message"].get("content") or "").strip()
        if text:
            return text
        if attempt == 1:
            time.sleep(0.5)
            continue
        raise RuntimeError("模型返回空内容（finish_reason=%s）" % choice.get("finish_reason"))


# 拒答标记的前几个字。模型要拒答时首字就是「【」，所以把开头这几个字压住不发，
# 确认不是拒答再放出去——已经吐到屏幕上的字没法收回，只能靠"晚一点开口"规避。
HOLD = 5


def chat_stream(question, context, cfg=None, timeout=45, web=False, on_delta=None):
    """边生成边回调。返回完整正文，与 chat() 语义一致，便于复用后处理。

    与 chat() 的差别只有两点：payload 带 stream，以及读 SSE 逐段回调。
    仍然保留"空正文重试一次"的语义——流式下空正文表现为一个 delta 都没有。
    """
    cfg = cfg or load_config()
    if not cfg.get("api_key"):
        raise RuntimeError("未配置 API 密钥（LLM_API_KEY 或 DEEPSEEK_API_KEY）")
    if not on_delta:
        return chat(question, context, cfg, timeout, web)

    payload = _build_payload(question, context, cfg, web, stream=True)
    with _OPENER.open(_request(cfg, payload), timeout=timeout) as r:
        buf, out, released, refused = "", [], False, False
        for raw in r:
            line = raw.decode("utf-8", "ignore").strip()
            if not line or not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                obj = json.loads(chunk)
            except Exception:
                continue
            if obj.get("usage"):
                LAST_USAGE.clear()
                LAST_USAGE.update(obj["usage"])
            for ch in obj.get("choices") or []:
                piece = (ch.get("delta") or {}).get("content") or ""
                if not piece:
                    continue
                out.append(piece)
                buf += piece
                if refused:
                    continue
                if not released:
                    if NO_ANSWER[:2] in buf or "未收录" in buf:
                        refused = True
                        continue
                    if len(buf) < HOLD:
                        continue
                    released = True
                    on_delta(buf)
                else:
                    on_delta(piece)
    text = "".join(out).strip()
    if not text:
        # 一个 delta 都没拿到：跟 chat() 一样重试一次（但这次不再流式，避免二次抖动）
        time.sleep(0.5)
        cfg2 = dict(cfg)
        return chat(question, context, cfg2, timeout, web)
    return text

