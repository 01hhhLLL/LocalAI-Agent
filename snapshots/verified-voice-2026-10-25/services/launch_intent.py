"""Conservative affirmative simple-open gate; ambiguous speech remains reviewable."""
import re
from application_registry import normalized, primary_launch_clause, requested_application_name, resolve_exact_name


def blocked_context(text):
    verb = re.search(r"打开|启动|运行", text)
    prefix = text[:verb.end()] if verb else text
    return (any(term in prefix for term in ("不要", "不用", "不必", "别", "取消", "禁止",
                                            "假如", "如果", "是否", "能否", "怎么", "如何", "可能", "也许", "还是"))
            or any(term in text for term in ("只是举例", "只是复述", "刚才说的")))


def affirmative_open(text):
    if blocked_context(text):
        return False
    return bool(re.match(r"^(?:(?:请|麻烦|帮我|给我|现在|直接|立刻|我要|我想|一下))*"
                         r"(?:打开|启动|运行).+", text.strip()))


def simple_open(text, app_id, registry_db, *, reviewed=False):
    verb = re.search(r"打开|启动|运行", text)
    if (not affirmative_open(text) or verb is None or
            normalized(primary_launch_clause(text)) != normalized(text[verb.start():])):
        return False
    target = requested_application_name(text)
    target = re.sub(r"(?:一下|就行|即可|就好|吧)$", "", target).strip()
    if re.search(r"选择|点击|双击|输入|发送|下载|安装|关闭|切换|登录", target):
        return False
    return reviewed or resolve_exact_name(target, registry_db).get("app_id") == app_id
