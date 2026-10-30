"""Builds sayproof/static/index.html from web/index.src.html: inlines the fonts and web/app.js.

Run `uv run python web/build.py` after editing either source. The fonts are OFL-licensed and embedded,
so the page makes no requests besides the app's own API.
"""

import base64
import pathlib
import re

WEB = pathlib.Path(__file__).parent
OUT = WEB.parent / "sayproof" / "static" / "index.html"


def font(m: re.Match) -> str:
    data = base64.b64encode((WEB / m.group(1)).read_bytes()).decode()
    return f"data:font/woff2;base64,{data}"


html = re.sub(r"\{\{FONT:([^}]+)\}\}", font, (WEB / "index.src.html").read_text())
html = html.replace("<!--APP-SCRIPT-->", "<script>\n" + (WEB / "app.js").read_text() + "</script>")
assert "{{FONT:" not in html and "<!--APP-SCRIPT-->" not in html
OUT.write_text(html)
print(f"{OUT.relative_to(WEB.parent)}: {len(html) // 1024} KB")
