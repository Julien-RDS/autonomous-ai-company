"""Fixed structural validation; never evaluates JavaScript or fetches a URL."""
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit, unquote


def validate(root: Path):
    class Document(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.tags=set()
        def handle_starttag(self, tag, attributes):
            self.tags.add(tag)
            for key,value in attributes:
                if key not in ('src','href') or not value or value.startswith('#'):
                    continue
                parsed=urlsplit(value)
                if parsed.scheme or parsed.netloc:
                    raise ValueError('External or executable URL refused')
                target=(root/unquote(parsed.path)).resolve()
                if not target.is_relative_to(root.resolve()) or not target.is_file():
                    raise ValueError('Local resource missing or outside prototype')
    entry=root/'index.html'
    if not entry.is_file():
        raise ValueError('index.html required')
    parser=Document()
    content=entry.read_text(encoding='utf-8')
    if '\x00' in content:
        raise ValueError('NUL in HTML')
    parser.feed(content); parser.close()
    if not {'html','body','title'} <= parser.tags:
        raise ValueError('html, body and title elements required')


if __name__=='__main__':
    validate(Path('/workspace'))
    print('Static HTML structure and local references: passed; JavaScript not executed')
