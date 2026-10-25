"""Explicit integration intents; retains the original transcript separately."""
import re


def explicit_tool_request(text):
    if not isinstance(text, str) or len(text) > 500:
        return None
    if re.search(r"不要|别|不必|不用|取消|只是举例|只是复述|假如|如果", text):
        return None
    url = re.search(r"https?://[^\s<>\"，。；]+", text)
    if re.search(r"(?:使用|用|通过|让)\s*REA\s*(?:来)?(?:分析|检查|查看)", text, re.I):
        if url:
            return {"tool": "rea_inspect", "arguments": {"url": url[0].rstrip(".,;")}}
    if url and re.match(r"^(?:(?:请|帮我|给我|使用|用|通过|Scrapling|\s))*?(?:读取|抓取|提取)", text, re.I):
        return {"tool": "scrapling_fetch", "arguments": {"url": url[0].rstrip(".,;")}}
    return None


def search_query(text):
    """Use the last explicit search phrase, retaining the object and its qualifiers."""
    matches = list(re.finditer(r"搜索(?:一下)?|搜一下|查询一下", text))
    if not matches:
        return None
    query = text[matches[-1].end():].strip(" ，,。.;；：:")
    if not query or re.search(r"(?:然后|接着|随后|并且)(?:点击|打开|输入|下载|安装|发送)", query):
        return None
    query = re.sub(r"[，,]+", " ", query)
    # Chinese whole-query quotation marks are narration punctuation (ASR may
    # add them). Keep internal quotes and ASCII exact-phrase operators intact.
    if (query.startswith("“") and query.endswith("”")
            and query.count("“") == query.count("”") == 1):
        query = query[1:-1].strip()
    return query if 1 <= len(query) <= 300 else None
