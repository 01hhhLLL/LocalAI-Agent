"""Split explicit registered application boundaries without rewriting speech."""
import re
from launch_intent import affirmative_open, blocked_context
from task_entry import _post_launch_steps, _validated_transcript


def split_plan(transcript, resolve):
    text = _validated_transcript(transcript)
    boundaries = [0]
    for match in re.finditer(r"(?:[;；。]\s*|(?:然后|随后|接着|再)\s*)(?=(?:(?:请|麻烦|帮我|现在|再)\s*)*(?:打开|启动|运行))", text):
        start = match.end()
        if resolve(text[start:]).get("status") == "confirmed_alias":
            boundaries.append(start)
    if len(boundaries) == 1:
        return [text]
    if len(boundaries) > 3:
        raise ValueError("a command can contain at most three application tasks")
    segments = [text[start:end].strip(" \n\t，,;；。") for start, end in
                zip(boundaries, boundaries[1:] + [len(text)])]
    for segment in segments:
        resolution = resolve(segment)
        if (not affirmative_open(segment) or blocked_context(segment)
                or resolution.get("status") != "confirmed_alias"
                or re.search(r"卸载|删除|格式化|输入|发送|保存|下载|安装|写入|关闭", segment)
                or len(_post_launch_steps(segment, resolution.get("matched_aliases", []))) > 3):
            raise ValueError("every application task must pass preflight before dispatch")
    return segments
