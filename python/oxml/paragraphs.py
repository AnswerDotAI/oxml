"""Paragraph boundaries in one immediate Word container, without section/table-boundary guessing."""
from ._core import Xml
from .model import _walk, metadata
from .styles import _child as _property
from .text import Story, _name, _shell, _run_text, _boundary

def _marks(paragraph):
    properties = _property(paragraph, 'pPr')
    run_properties = None if properties is None else _property(properties, 'rPr')
    return [] if run_properties is None else [c for c in run_properties.children if _name(c) in {'ins', 'del'}]

def _adjacent(first, second):
    if first is None or second is None or first._tree is not second._tree: return False
    parent = first.parent
    if parent is None or second.parent is None or parent.node_id != second.parent.node_id: return False
    siblings = parent._tree.xml.element_children(parent.node_id)
    index = siblings.index(first.node_id)
    return index+1 < len(siblings) and siblings[index+1] == second.node_id

def _separator(first, view, adjacent):
    if not adjacent: return '\n'
    marks = _marks(first)
    if len(marks) == 1 and _name(marks[0]) == ('del' if view == 'current' else 'ins'): return ''
    return '\n'

def _check_paragraph(paragraph, boundaries=False):
    from .revisions import _context, _revision_name, _boundary_paragraph
    if _name(paragraph) != 'p' or paragraph.parent is None: raise ValueError('Expected a paragraph in a Word container')
    if _name(paragraph.parent) not in {'body', 'hdr', 'ftr', 'tc', 'footnote', 'endnote', 'comment'}:
        raise NotImplementedError('Paragraph edits require one ordinary immediate container')
    _context(paragraph)
    properties = _property(paragraph, 'pPr')
    if properties is not None:
        for element in _walk(properties):
            if _name(element) == 'sectPr': raise NotImplementedError('Editing section-break paragraphs is unsupported')
            if _revision_name(element) and not (boundaries and _boundary_paragraph(element) is not None):
                raise NotImplementedError('Paragraph edits cannot discard existing property/boundary revisions')
    if len(_marks(paragraph)) > 1: raise NotImplementedError('Conflicting paragraph-boundary revisions are unsupported')

def _split_at(paragraph, index, shell=None):
    """Split at a preflighted raw content boundary; the original paragraph/mark remains on the right."""
    parent, xml = paragraph.parent, paragraph._tree.xml
    properties = _property(paragraph, 'pPr')
    prefix = [i for i in xml.children(paragraph.node_id)[:index] if properties is None or i != properties.node_id]
    location = xml.children(parent.node_id).index(paragraph.node_id)
    left = (_shell(paragraph, properties) if shell is None else shell).append_to(parent, location)
    for local in ('paraId', 'textId'): left.remove_attribute(metadata['namespaces']['w14'], local)
    for mark in _marks(left): mark.delete()
    for identity in prefix: xml.move_node(identity, left.node_id, xml.child_count(left.node_id))
    return left, paragraph

def split_paragraph(paragraph, offset):
    """Split an ordinary paragraph at a Unicode text offset; return (new left, original right)."""
    _check_paragraph(paragraph)
    Story(paragraph).range(offset, offset)._preflight()
    return _split_at(paragraph, _boundary(paragraph, offset))

def _content_start(paragraph):
    properties = _property(paragraph, 'pPr')
    return 0 if properties is None else paragraph._tree.xml.children(paragraph.node_id).index(properties.node_id)+1

def _join(first, second):
    # PowerTools AcceptDeletedAndMoveFromParagraphMarksTransform/RP052: keep the last paragraph's properties.
    xml, index = first._tree.xml, _content_start(second)
    properties = _property(first, 'pPr')
    for identity in xml.children(first.node_id):
        if properties is not None and identity == properties.node_id: continue
        xml.move_node(identity, second.node_id, index)
        index += 1
    first.delete()
    return second

def join_paragraphs(first, second):
    """Remove a boundary between adjacent sibling paragraphs, keeping the second paragraph and its properties."""
    _check_paragraph(first)
    _check_paragraph(second)
    if not _adjacent(first, second): raise NotImplementedError('Only adjacent paragraphs in the same container can be joined')
    return _join(first, second)

def _span_parts(span):
    """Preflight an editable span as physical paragraph-local slices before any mutation."""
    span._check()
    rows = list(span.story._paragraphs())
    def index(position):
        return next((i for i, (_, start, text, _, _) in enumerate(rows) if start <= position <= start+len(text)), None)
    first, last = index(span.start), index(span.end)
    if first is None or last is None: raise NotImplementedError('Range touches an opaque paragraph boundary')
    result = []
    for i in range(first, last+1):
        paragraph, position, text, _, _ = rows[i]
        if paragraph is None: raise NotImplementedError('Range crosses opaque content')
        _check_paragraph(paragraph)
        if not result:
            siblings = paragraph._tree.xml.element_children(paragraph.parent.node_id)
            sibling_index = siblings.index(paragraph.node_id)
        if sibling_index >= len(siblings) or siblings[sibling_index] != paragraph.node_id:
            raise NotImplementedError('Range crosses a table/container boundary')
        sibling_index += 1
        start, end = max(0, span.start-position), min(len(text), span.end-position)
        _, _, _, template = Story(paragraph, view=span.story.view).range(start, end)._preflight()
        result.append((paragraph, start, end, template))
    return result

def _isolate_parts(parts):
    return [Story(paragraph).range(start, end)._isolate(extract_references=True) for paragraph, start, end, _ in parts]

def _replacement_runs(template, text):
    if not isinstance(text, str): raise TypeError('replacement requires str')
    if '\r' in text: raise ValueError('Use \n for paragraph boundaries')
    expressions = [_run_text(template, chunk) if chunk else None for chunk in text.split('\n')]
    for expression in expressions:
        if expression is not None: Xml(expression.bytes())
    return expressions

def _replace_range(span, text):
    parts = _span_parts(span)
    first = parts[0][0]
    story, start = span.story, span.start
    if _name(story.element) == 'p':
        story = Story(first.parent)
        start = next(position for p, position, _, _, _ in story._paragraphs() if p is not None and p.node_id == first.node_id)+parts[0][1]
    expressions = _replacement_runs(parts[0][3], text)
    left_shell = _shell(first, _property(first, 'pPr'))
    isolated = _isolate_parts(parts)
    paragraph, _, index, _ = isolated[0]
    prefix_length = index-_content_start(paragraph)
    for _, runs, _, _ in isolated:
        for run in runs: run.delete()
    for following, _, _, _ in isolated[1:]: paragraph = _join(paragraph, following)
    index = _content_start(paragraph)+prefix_length
    for i, expression in enumerate(expressions):
        if expression is not None:
            expression.append_to(paragraph, index)
            index += 1
        if i < len(expressions)-1:
            _, paragraph = _split_at(paragraph, index, left_shell)
            index = _content_start(paragraph)
    return story.range(start, start+len(text))
