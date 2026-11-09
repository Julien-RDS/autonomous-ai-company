"""Fixed structural validation of a static prototype; never evaluates JavaScript or fetches a URL.

Executed inside the sandbox by the `static-html` profile (python -I -c <this file>).
Every HTML page and stylesheet is checked, not only index.html: any reference to
an external origin, a scheme (javascript:, data:, http:...) or a path outside the
prototype is refused. This is a structural check, not a browser security audit:
JavaScript is never interpreted, so URLs built at runtime by scripts are not seen.
"""
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

URL_ATTRIBUTES = {"src", "href", "action", "formaction", "data", "poster", "background",
                  "cite", "longdesc", "manifest", "xlink:href", "ping"}
CSS_URL = re.compile(r"""url\(\s*(['"]?)(.*?)\1\s*\)|@import\s+(['"])(.*?)\3""", re.I | re.S)


def check_reference(root, origin, value):
    value = value.strip()
    if not value or value.startswith("#"):
        return
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or value.startswith("//"):
        raise ValueError(f"External or executable URL refused in {origin.name}")
    path = unquote(parsed.path)
    if not path:
        return
    if "\x00" in path:
        raise ValueError(f"NUL refused in reference of {origin.name}")
    base = root if path.startswith("/") else origin.parent
    target = (base / path.lstrip("/")).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise ValueError(f"Local resource missing or outside prototype in {origin.name}")


def check_css(root, origin, css):
    for match in CSS_URL.finditer(css):
        check_reference(root, origin, match.group(2) if match.group(2) is not None else match.group(4))


class Document(HTMLParser):
    def __init__(self, root, origin):
        super().__init__(convert_charrefs=True)
        self.root, self.origin, self.tags, self.in_style = root, origin, set(), False

    def handle_starttag(self, tag, attributes):
        self.tags.add(tag)
        if tag == "base":
            raise ValueError(f"<base> refused in {self.origin.name}")
        self.in_style = tag == "style"
        values = {key.lower(): (value or "") for key, value in attributes}
        for key, value in values.items():
            if key in URL_ATTRIBUTES:
                check_reference(self.root, self.origin, value)
            elif key in ("srcset", "imagesrcset"):
                for candidate in value.split(","):
                    if candidate.strip():
                        check_reference(self.root, self.origin, candidate.split()[0])
            elif key == "style":
                check_css(self.root, self.origin, value)
        if tag == "meta" and values.get("http-equiv", "").lower() == "refresh":
            target = re.search(r"url\s*=\s*['\"]?([^'\";]+)", values.get("content", ""), re.I)
            if target:
                check_reference(self.root, self.origin, target.group(1))

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False

    def handle_data(self, data):
        if self.in_style:
            check_css(self.root, self.origin, data)


def validate(root):
    root = Path(root).resolve()
    if not (root / "index.html").is_file():
        raise ValueError("index.html required")
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Symbolic link refused")
        suffix = path.suffix.lower()
        if not path.is_file() or suffix not in (".html", ".htm", ".css"):
            continue
        content = path.read_text(encoding="utf-8")
        if "\x00" in content:
            raise ValueError(f"NUL refused in {path.name}")
        if suffix == ".css":
            check_css(root, path, content)
            continue
        parser = Document(root, path)
        parser.feed(content)
        parser.close()
        if path.name == "index.html" and not {"html", "body", "title"} <= parser.tags:
            raise ValueError("html, body and title elements required")


if __name__ == "__main__":
    validate(Path("/workspace"))
    print("Static HTML structure and local references: passed; JavaScript not executed")
