"""
小工具：把 sample_alert.html 轉成純文字，看職缺資訊的排列方式。
只印文字，不印連結，可以安全地貼出來。

用法：
    python inspect_alert.py
"""

from html.parser import HTMLParser

from read_alerts import JOB_ID_PATTERN, SAMPLE_FILE

SKIP_TAGS = {"style", "script", "head", "title"}


class TextLister(HTMLParser):
    """依照出現順序收集所有看得到的文字，並標出哪些是職缺連結。"""

    def __init__(self):
        super().__init__()
        self.lines = []
        self.in_job_link = False
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip_depth += 1
        if tag == "a" and JOB_ID_PATTERN.search(dict(attrs).get("href", "")):
            self.in_job_link = True

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        if tag == "a":
            self.in_job_link = False

    def handle_data(self, data):
        text = " ".join(data.split())
        if text and not self.skip_depth:
            marker = "[職缺連結] " if self.in_job_link else ""
            self.lines.append(marker + text)


def main():
    with open(SAMPLE_FILE, encoding="utf-8") as f:
        html = f.read()

    lister = TextLister()
    lister.feed(html)

    # 從第一個職缺連結的前幾行開始印，印 40 行
    first = next((i for i, line in enumerate(lister.lines) if line.startswith("[職缺連結]")), 0)
    start = max(first - 3, 0)
    for line in lister.lines[start:start + 40]:
        print(line)


if __name__ == "__main__":
    main()
