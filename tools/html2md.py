"""把 Word 文档导出的 HTML 转成可读 Markdown。

用途单一：把 `docs/reference/plan-v0.1.docx` 转成 AI 可读的 `.md`。不是通用转换器。

流程：

1. 用本地 editor_sdk（或 Word「另存为网页」）把 docx 渲染成 HTML
2. ``python tools/html2md.py plan.html docs/reference/plan-v0.1.md``

处理范围：``h1``-``h6`` / ``p`` / ``table`` / ``br``。
含制表符框线（``┌│└─``）的段落自动包成 ` ```text ` 代码块，以保留架构图的等宽对齐。
单元格内含框线时按代码块输出，否则按 Markdown 表格输出；单行单列表格降级为引用块。

设计原则：**不改写原文**。只做版式映射，文字内容逐字保留。
"""


import re
import sys
from html.parser import HTMLParser

BOX_CHARS = set("┌┐└┘│─├┤┬┴┼▼▲◄►╔╗╚╝║═")


def clean_cell(text: str) -> str:
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = text.replace("|", "\\|")
    return text.strip()


class Doc(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []          # (kind, payload)
        self.mode = "text"
        self.kind = None
        self.buf = []
        self.table = None         # list of rows
        self.row = None
        self.cell = None
        self.skip = 0

    # ---------------------------------------------------------------- start
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("style", "script"):
            self.skip += 1
            return
        if self.skip:
            return

        if tag == "table":
            self.mode = "table"
            self.table = []
            self.kind = None
            self.buf = []
            return

        if self.mode == "table":
            if tag == "tr":
                self.row = []
            elif tag in ("td", "th") and self.row is not None:
                self.cell = {"text": [], "colspan": int(a.get("colspan") or 1)}
            elif tag == "br" and self.cell is not None:
                self.cell["text"].append("\n")
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "li"):
            if self.kind is not None:
                self.flush()
            self.kind = tag
            self.buf = []
        elif tag == "br":
            self.buf.append("\n")

    # ------------------------------------------------------------------ end
    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return

        if tag == "table" and self.mode == "table":
            if self.table:
                self.blocks.append(("table", self.table))
            self.table = None
            self.row = None
            self.cell = None
            self.mode = "text"
            return

        if self.mode == "table":
            if tag in ("td", "th") and self.cell is not None and self.row is not None:
                self.row.append(self.cell)
                self.cell = None
            elif tag == "tr" and self.row is not None:
                self.table.append(self.row)
                self.row = None
            elif tag == "p" and self.cell is not None:
                self.cell["text"].append("\n")
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "li") and self.kind is not None:
            self.flush()

    def handle_data(self, data):
        if self.skip:
            return
        if self.mode == "table":
            if self.cell is not None:
                self.cell["text"].append(data)
            return
        if self.kind is not None:
            self.buf.append(data)

    # ---------------------------------------------------------------- flush
    def flush(self):
        raw = "".join(self.buf)
        self.buf = []
        kind = self.kind
        self.kind = None
        if kind is None:
            return
        if kind == "li":
            text = re.sub(r"\s+", " ", raw).strip()
            if text:
                self.blocks.append(("li", text))
            return
        if any(ch in BOX_CHARS for ch in raw):
            self.blocks.append(("box", raw.rstrip()))
            return
        text = re.sub(r"[ \t]*\n[ \t]*", "\n", raw)
        text = re.sub(r"[ \t]+", " ", text).strip()
        if text:
            self.blocks.append((kind, text))


def render(blocks):
    out = []
    prev_kind = None
    for kind, payload in blocks:
        if kind == "box":
            if prev_kind != "box":
                out.append("```text")
            out.append(payload)
            prev_kind = "box"
            continue
        if prev_kind == "box":
            out.append("```")
            out.append("")

        if kind == "h1":
            out.append("## " + payload)
        elif kind == "h2":
            out.append("### " + payload)
        elif kind in ("h3", "h4", "h5", "h6"):
            out.append("#### " + payload)
        elif kind == "li":
            out.append("- " + payload)
        elif kind == "table":
            if len(payload) == 1 and len(payload[0]) == 1:
                raw = "".join(payload[0][0]["text"])
                if any(ch in BOX_CHARS for ch in raw):
                    lines = [ln.rstrip() for ln in raw.split("\n")]
                    while lines and not lines[0].strip():
                        lines.pop(0)
                    while lines and not lines[-1].strip():
                        lines.pop()
                    out.append("```text")
                    out.extend(lines)
                    out.append("```")
                    out.append("")
                    prev_kind = kind
                    continue
                text = re.sub(r"\s*\n\s*", " ", raw)
                text = re.sub(r"[ \t]+", " ", text).strip()
                if text:
                    out.append("> " + text)
                    out.append("")
                prev_kind = kind
                continue
            rows = [[clean_cell("".join(c["text"])) for c in row if c] for row in payload]
            rows = [r for r in rows if any(r)]
            if not rows:
                prev_kind = kind
                continue
            if len(rows) == 1 and len(rows[0]) == 1:
                out.append("> " + rows[0][0])
            else:
                width = max(len(r) for r in rows)
                rows = [r + [""] * (width - len(r)) for r in rows]
                header = rows[0]
                if not any(header):
                    header = ["列 %d" % (i + 1) for i in range(width)]
                    body = rows
                else:
                    body = rows[1:]
                out.append("| " + " | ".join(header) + " |")
                out.append("|" + "|".join(["---"] * width) + "|")
                for r in body:
                    out.append("| " + " | ".join(r) + " |")
        else:
            out.append(payload)
        out.append("")
        prev_kind = kind

    if prev_kind == "box":
        out.append("```")
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


if __name__ == "__main__":
    src, dst = sys.argv[1], sys.argv[2]
    parser = Doc()
    parser.feed(open(src, encoding="utf-8").read())
    result = render(parser.blocks)
    with open(dst, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(result)
    print(f"blocks={len(parser.blocks)} chars={len(result)}")
