"""
解析模型输出之前，先去掉 Markdown 记号。

提示词要的是「数据库: erp_db」「结论：一致」「解析结果：a，b」这样的纯文本标签，解析也是
按标签找。GPT 系的模型很爱加格式：`**数据库**：erp_db`、`- 结论：一致`、`### 步骤1`、
把字段名包进反引号。标签和冒号之间夹着 `**`，按标签就找不到了——规划解析成零步，
自检的「不一致」读成没有结论，抽词把整段带格式的回答当成一个关键词。

在每个正则里都容忍一遍这些写法，迟早漏掉一处；解析之前统一去掉更可靠。
"""
from __future__ import annotations

import re

_CODE = re.compile(r"`+")
_EMPHASIS = re.compile(r"\*\*|__")
_HEADING = re.compile(r"^[ \t]*#{1,6}[ \t]+", re.M)
# 行首的列表记号：- * • + 和「1.」「2)」。小数（0.9）后面不跟空白，不会被当成编号。
_BULLET = re.compile(r"^[ \t]*(?:[-*•+]|\d{1,2}[.)])[ \t]+", re.M)


def plain_text(text: str) -> str:
    """去掉代码记号、加粗、标题和行首列表记号，其余原样保留。"""
    text = _CODE.sub("", text or "")
    text = _EMPHASIS.sub("", text)
    text = _HEADING.sub("", text)
    return _BULLET.sub("", text)
