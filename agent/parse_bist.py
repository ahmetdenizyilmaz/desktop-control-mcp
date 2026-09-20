import re, io, sys

path = r"C:\Users\ady\Desktop\claude_folder\desktop controller\agent\bist30page.html"
html = io.open(path, encoding="utf-8", errors="ignore").read()

rows = html.split("<tr")
out = []
for row in rows:
    m = re.search(r'data-rowkey="BIST:([A-Z0-9]+)"', row)
    if not m:
        continue
    ticker = m.group(1)
    # strip tags, get visible cell text
    cells = re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S)
    texts = []
    for c in cells:
        t = re.sub(r"<[^>]+>", "|", c)
        t = re.sub(r"\|+", "|", t).strip("|")
        texts.append(t)
    out.append(ticker + " => " + " ;; ".join(texts))

print(len(out), "rows")
for line in out:
    print(line[:400])
