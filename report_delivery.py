"""Derive a complete readable Markdown report and build an allowlisted ZIP."""
from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser
import re
import unicodedata
import zipfile


class DeliveryError(Exception):
    pass


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)


class Document(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.root = Node('document')
        self.stack = [self.root]
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in {'br', 'hr', 'img', 'meta', 'link', 'input', 'source', 'wbr'}:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def nodes(node, tag):
    for child in node.children:
        if isinstance(child, Node):
            if tag is None or child.tag == tag:
                yield child
            yield from nodes(child, tag)


def text(node):
    if isinstance(node, str):
        return node
    if node.tag in {'script', 'style', 'head', 'nav'}:
        return ''
    return ''.join(text(c) for c in node.children)


def source_marker(node):
    ids = list(dict.fromkeys(filter(None, re.split(r'[,;\s]+', node.attrs.get('data-source-units', '')))))
    return '[来源：' + ', '.join(ids) + ']' if ids else ''


def inline(node):
    if isinstance(node, str):
        return escape(node, quote=False)
    content = ''.join(inline(c) for c in node.children)
    if node.tag in {'script', 'style', 'nav'}:
        return ''
    if node.tag in {'strong', 'b'}:
        content = '**' + content.strip() + '**'
    elif node.tag in {'em', 'i'}:
        content = '*' + content.strip() + '*'
    elif node.tag == 'a':
        href = node.attrs.get('href', '').replace(')', '%29')
        content = '[' + content.strip() + '](' + href + ')'
    elif node.tag == 'br':
        return '<br>'
    elif node.tag == 'code':
        content = '`' + content + '`'
    marker = source_marker(node)
    if marker:
        content += ' ' + marker
    if node.tag in {'p', 'div'}:
        content += '<br>'
    return content


def render_table(node):
    rows = list(nodes(node, 'tr'))
    if not rows:
        return ''
    row_markers = []
    for row in rows:
        bindings = [row, *list(nodes(row, None))]
        ids = ' '.join(n.attrs.get('data-source-units', '') for n in bindings)
        row_markers.append(source_marker(Node('row', {'data-source-units': ids})))
    has_sources = any(row_markers)
    output = []
    width = 0
    for i, row in enumerate(rows):
        cells = [c for c in row.children if isinstance(c, Node) and c.tag in {'th', 'td'}]
        values = [' '.join(inline(c).split()).replace('|', r'\|') for c in cells]
        if has_sources:
            values.append(('来源 ' + row_markers[i]).strip() if i == 0 else row_markers[i])
        if i == 0:
            width = len(values)
        if len(values) != width or any('rowspan' in c.attrs or 'colspan' in c.attrs for c in cells):
            raise DeliveryError('表格列数或合并单元格需先整理为可对应的 Markdown 表格。')
        output.append('| ' + ' | '.join(values) + ' |')
        if i == 0:
            output.append('| ' + ' | '.join(['---'] * width) + ' |')
    return '\n'.join(output)


def block(node):
    if isinstance(node, str):
        return escape(node.strip(), quote=False)
    tag = node.tag
    binding_node = node
    if tag in {'head', 'style', 'script', 'nav'}:
        return ''
    if tag in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}:
        spans = {c.attrs.get('class'): c for c in node.children if isinstance(c, Node) and c.tag == 'span'}
        if tag == 'h2' and 'section-title' in spans:
            title = text(spans['section-title']).strip()
            if 'num' in spans:
                title = text(spans['num']).strip() + '. ' + title
            if 'section-time' in spans:
                title += '（' + text(spans['section-time']).strip() + '）'
        else:
            title = ' '.join(text(node).split())
        result = '#' * int(tag[1]) + ' ' + escape(title, quote=False)
    elif tag in {'p', 'figcaption', 'footer', 'summary'}:
        result = ''.join(inline(c) for c in node.children).strip()
    elif tag == 'table':
        result = render_table(node)
    elif tag == 'svg':
        title = next(nodes(node, 'title'), None)
        description = text(title).strip() if title else node.attrs.get('aria-label', '')
        if not description:
            raise DeliveryError('SVG 需有说明对象与关系的 title 或 aria-label，才能生成对应 Markdown。')
        result = '**图示：' + escape(description, quote=False) + '**'
        labels = list(dict.fromkeys(' '.join(text(n).split()) for n in nodes(node, 'text')))
        if labels:
            result += '\n\n图中标注：' + '；'.join(escape(v, quote=False) for v in labels) + '。'
        bindings = [node, *list(nodes(node, None))]
        binding_node = Node('svg', {'data-source-units': ' '.join(n.attrs.get('data-source-units', '') for n in bindings)})
    elif tag in {'ul', 'ol'}:
        items = [c for c in node.children if isinstance(c, Node) and c.tag == 'li']
        result = '\n'.join((f'{i}. ' if tag == 'ol' else '- ') + block(c).replace('\n', '\n  ')
                           for i, c in enumerate(items, 1))
    elif tag == 'li':
        parts, pending = [], []
        for child in node.children:
            if isinstance(child, Node) and child.tag in {'p', 'div', 'h3', 'h4', 'ul', 'ol'}:
                if pending:
                    parts.append(''.join(pending).strip())
                    pending = []
                parts.append(block(child))
            else:
                pending.append(inline(child))
        if pending:
            parts.append(''.join(pending).strip())
        result = '\n\n'.join(p for p in parts if p)
    elif tag == 'details':
        result = '\n\n'.join(('## ' + block(c)) if isinstance(c, Node) and c.tag == 'summary' else block(c)
                               for c in node.children if not isinstance(c, str) or c.strip())
    elif tag == 'pre':
        result = '```\n' + text(node).strip() + '\n```'
    else:
        result = '\n\n'.join(block(c) for c in node.children if not isinstance(c, str) or c.strip())
    marker = source_marker(binding_node)
    return result + ('\n\n' + marker if marker else '')


def render_markdown(html):
    doc = Document(html)
    body = next(nodes(doc.root, 'body'), None)
    if body is None:
        raise DeliveryError('HTML 缺少 body。')
    result = re.sub(r'\n[ \t]+\n', '\n\n', block(body))
    return re.sub(r'\n{3,}', '\n\n', result).strip() + '\n'


def markdown_source_ids(markdown):
    return {value for group in re.findall(r'\[来源：([^\]]+)\]', markdown)
            for value in re.split(r'[,;\s]+', group) if value}


def report_paths(folder, state):
    files = state.get('report_files', {'html': 'report.html', 'markdown': 'report.md'})
    paths = []
    for key, suffix in (('html', '.html'), ('markdown', '.md')):
        name = files.get(key)
        if (not isinstance(name, str) or not name.endswith(suffix) or '/' in name or '\\' in name
                or (folder / name).is_symlink()):
            raise DeliveryError('报告文件名无效或为符号链接。')
        paths.append(folder / name)
    return paths


def content_stem(html, source, title=None, mode='standard'):
    doc = Document(html)
    heading = next(nodes(doc.root, 'h1'), None)
    candidates = [title] if title is not None else [text(heading) if heading else '', source.get('video_title', '')]
    for candidate in candidates:
        name = unicodedata.normalize('NFKC', str(candidate or ''))
        name = ''.join(c for c in name if not unicodedata.category(c).startswith('C'))
        name = re.sub(r'[<>:"/\\|?*]+', '-', name)
        name = re.sub(r'\s+', ' ', name).strip(' .-')
        while len(name.encode('utf-8')) > 160:
            name = name[:-1]
        name = name.rstrip(' .-')
        if name and name.casefold() not in {'report', '报告', '完整报告', '视频报告', '视频总结', '总结', '阅读报告'}:
            return name + ('_速览' if mode == 'brief' else '_精读')
    raise DeliveryError('请根据正文拟定简洁主题标题，使用 deliver --title 内容标题。')


REQUIRED_FILES = ('transcript.srt', 'transcript.txt', 'transcript.md',
                  'input.json', 'source-units.json', 'state.json', 'asr-task.json', 'asr-raw.json', 'report-check.json')


def write_archive(folder, name, report_files):
    required = (*REQUIRED_FILES, *report_files.values())
    if any(not (folder / item).is_file() or (folder / item).is_symlink() for item in required):
        raise DeliveryError('下载包缺少正式报告或必需的原始资料。')
    names = list(dict.fromkeys(required)) + [n for n in ('review.json',) if (folder / n).is_file()]
    if any((folder / item).is_symlink() for item in names):
        raise DeliveryError('下载包不能包含符号链接。')
    destination = folder / name
    temp = destination.with_suffix('.zip.tmp')
    with zipfile.ZipFile(temp, 'w', zipfile.ZIP_DEFLATED) as archive:
        for item in names:
            archive.write(folder / item, item)
    with zipfile.ZipFile(temp) as archive:
        if archive.testzip() or any(archive.read(item) != (folder / item).read_bytes() for item in names):
            raise DeliveryError('下载包完整性检查失败。')
    temp.replace(destination)
    return destination
