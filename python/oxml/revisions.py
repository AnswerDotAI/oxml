"""Tracked text, paragraph-boundary and property edits in one explicit story."""
from datetime import datetime, timezone
from ._core import Xml
from .build import E
from .model import metadata, _walk
from .text import Story, Range, _W, _CONTAINERS, _name, _content, _token, _run_text, _OPAQUE, _inside

# PowerTools RevisionProcessor.TrackedRevisionsElements, plus SDK custom-XML move/conflict range families.
_REVISIONS = {'ins', 'del', 'delText', 'delInstrText', 'cellIns', 'cellDel', 'cellMerge', 'numberingChange',
              'pPrChange', 'rPrChange', 'sectPrChange', 'tblGridChange', 'tblPrChange', 'tblPrExChange', 'tcPrChange', 'trPrChange',
              'moveFrom', 'moveTo', 'moveFromRangeStart', 'moveFromRangeEnd', 'moveToRangeStart', 'moveToRangeEnd',
              'customXmlInsRangeStart', 'customXmlInsRangeEnd', 'customXmlDelRangeStart', 'customXmlDelRangeEnd',
              'customXmlMoveFromRangeStart', 'customXmlMoveFromRangeEnd', 'customXmlMoveToRangeStart', 'customXmlMoveToRangeEnd'}
_CONFLICTS = {'conflictIns', 'conflictDel', 'customXmlConflictInsRangeStart', 'customXmlConflictInsRangeEnd',
              'customXmlConflictDelRangeStart', 'customXmlConflictDelRangeEnd'}

def _revision_name(element):
    uri, name = element.qname
    if uri == _W and name in _REVISIONS or uri == metadata['namespaces']['w14'] and name in _CONFLICTS: return name

def _revision_attrs(tree, author, date=None):
    """Yield validated author/date metadata and fresh numeric IDs in the containing XML tree."""
    if not isinstance(author, str) or not author: raise ValueError('A nonempty author is required')
    if date is None: date = datetime.now(timezone.utc)
    if not isinstance(date, datetime) or date.utcoffset() is None: raise ValueError('date requires a timezone-aware datetime')
    attrs = {'w:author': author, 'w:date': date.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')}
    Xml(E('w:ins', attrs=attrs, ns={'w': _W}).bytes())
    used = set()
    for element in tree.elements():
        value = element.attribute(_W, 'id')
        if value is not None:
            try: used.add(int(value))
            except ValueError: pass
    ident = 0
    while True:
        while ident in used: ident += 1
        yield {**attrs, 'w:id': str(ident)}
        ident += 1

def _boundary_paragraph(element):
    parent = element.parent
    if _name(element) not in {'ins', 'del'} or parent is None or _name(parent) != 'rPr': return None
    properties = parent.parent
    if properties is None or _name(properties) != 'pPr': return None
    paragraph = properties.parent
    return paragraph if paragraph is not None and _name(paragraph) == 'p' else None

def _context(paragraph):
    ancestor = paragraph
    while ancestor is not None:
        name = _name(ancestor)
        if name not in _CONTAINERS | {'p', 'footnotes', 'endnotes', 'comments'}:
            raise NotImplementedError('Editing inside fields, content controls or other opaque containers is unsupported')
        properties = {'p': 'pPr', 'tr': 'trPr', 'tc': 'tcPr', 'tbl': 'tblPr'}.get(name)
        if properties:
            for child in ancestor.children:
                if _name(child) != properties: continue
                for element in _walk(child):
                    revision = _revision_name(element)
                    if revision is None: continue
                    if name == 'p' and (revision in {'pPrChange', 'rPrChange'} or _boundary_paragraph(element) is not None): continue
                    raise NotImplementedError('Paragraph or table revision context is unsupported')
        ancestor = ancestor.parent

def _runs(element):
    if _name(element) not in {'ins', 'del'} or element.parent is None or _name(element.parent) != 'p':
        raise NotImplementedError('Only inline insertion/deletion wrappers directly in paragraphs are supported')
    _context(element.parent)
    runs = []
    for node in _content(element):
        if node['kind'] != 'element':
            if node['kind'] == 'text' and node['text'].strip(): raise NotImplementedError('Revision contains non-run text')
            continue
        run = element._tree._element(node['id'])
        if _name(run) != 'r': raise NotImplementedError('Revision contains nested revisions or unsupported non-run content')
        for item in _content(run):
            if _token(item) == _OPAQUE: raise NotImplementedError('Revision contains an unsupported run payload')
            if item['kind'] != 'element': continue
            child = run._tree._element(item['id'])
            if _name(child) in {'t', 'delText'} and _name(child) != ('delText' if _name(element) == 'del' else 't'):
                raise NotImplementedError('Revision contains text with the wrong insertion/deletion form')
            if _name(child) == 'rPr':
                if any(_revision_name(e) for e in _walk(child)): raise NotImplementedError('Run-property revisions are unsupported')
            elif any(n['kind'] != 'text' for n in _content(child)):
                raise NotImplementedError('Revision text contains nested XML')
        runs.append(run)
    return runs

def _rename_text(runs, old, new):
    for run in runs:
        for child in run.children:
            if _name(child) == old: run._tree.xml.rename(child.node_id, _W, new)

def _check_boundary(element, successors=None):
    from .paragraphs import _check_paragraph, _marks
    paragraph = _boundary_paragraph(element)
    if paragraph is None: raise NotImplementedError('Not a paragraph-boundary revision')
    _check_paragraph(paragraph, boundaries=True)
    if successors is not None and paragraph.node_id in successors: following = successors[paragraph.node_id]
    else:
        siblings = paragraph._tree.xml.element_children(paragraph.parent.node_id)
        index = siblings.index(paragraph.node_id)
        following = paragraph._tree._element(siblings[index+1]) if index+1 < len(siblings) else None
        if successors is not None: successors[paragraph.node_id] = following
    if following is None or _name(following) != 'p':
        raise NotImplementedError('Final paragraph marks and paragraph/table boundaries are unsupported')
    _check_paragraph(following, boundaries=True)
    if len(_marks(paragraph)) != 1 or element.children or element.text.strip():
        raise NotImplementedError('Conflicting or nonempty paragraph-boundary markup is unsupported')
    return paragraph, following

def _add_mark(paragraph, expression):
    from .paragraphs import _property
    properties = _property(paragraph, 'pPr')
    if properties is None: properties = E('w:pPr').append_to(paragraph, 0)
    run_properties = _property(properties, 'rPr')
    if run_properties is None:
        index = next((i for i, node in enumerate(_content(properties)) if node.get('qname') in
                      [[_W, 'sectPr'], [_W, 'pPrChange']]), properties._tree.xml.child_count(properties.node_id))
        run_properties = E('w:rPr').append_to(properties, index)
    return expression.append_to(run_properties, 0)

class Revision:
    """One live supported revision. Consuming it invalidates its element handle."""
    def __init__(self, element): self.element = element

    @property
    def kind(self): return _name(self.element)
    @property
    def id(self): return self.element.attribute(_W, 'id')
    @property
    def author(self): return self.element.attribute(_W, 'author')
    @property
    def date(self): return self.element.attribute(_W, 'date')
    @property
    def text(self):
        if self.kind in {'rPrChange', 'pPrChange'}: return ''
        if _boundary_paragraph(self.element) is not None: return '\n'
        return ''.join(_token(node) for run in _runs(self.element) for node in _content(run))

    @property
    def is_boundary(self): return _boundary_paragraph(self.element) is not None

    @property
    def previous(self):
        from .formatting import _preflight_change
        return _preflight_change(self.element)[1]

    @property
    def current(self):
        from .formatting import _preflight_change
        return _preflight_change(self.element)[0]

    def _check(self, successors=None):
        if self.kind in {'rPrChange', 'pPrChange'}:
            from .formatting import _preflight_change
            _preflight_change(self.element)
        elif self.is_boundary: _check_boundary(self.element, successors)
        else: _runs(self.element)

    def _apply(self, accept, successors=None):
        if self.kind in {'rPrChange', 'pPrChange'}:
            from .formatting import _apply_change
            _apply_change(self.element, accept)
            return
        if self.is_boundary:
            from .paragraphs import _join
            paragraph, following = _check_boundary(self.element, successors)
            if (self.kind == 'ins') == accept: self.element.delete()
            else: _join(paragraph, following)
            return
        element, keep = self.element, (self.kind == 'ins') == accept
        if keep:
            if self.kind == 'del': _rename_text(element.children, 'delText', 't')
            parent, xml = element.parent, element._tree.xml
            index = xml.children(parent.node_id).index(element.node_id)
            for identity in xml.children(element.node_id):
                xml.move_node(identity, parent.node_id, index)
                index += 1
        element.delete()

    def accept(self):
        self._check()
        self._apply(True)

    def reject(self):
        self._check()
        self._apply(False)

class Revisions:
    """Inspect and operate on revisions in an explicit Story, not the whole package.

    Iteration and bulk operations refuse unsupported revision families anywhere in this
    scope. Individual handles operate only on their own supported group.
    """
    def __init__(self, story):
        if not isinstance(story, Story): raise TypeError('Revisions requires a Story')
        self.story = story

    def _collect(self, successors=None):
        if successors is None: successors = {}
        revisions = []
        def visit(element):
            name = _revision_name(element)
            if name is not None:
                revision = Revision(element)
                revision._check(successors)
                revisions.append(revision)
                return
            children = element.children
            for index, child in enumerate(children):
                if _name(child) == 'p': successors[child.node_id] = children[index+1] if index+1 < len(children) else None
            for child in children: visit(child)
        visit(self.story.element)
        return revisions

    def __iter__(self): return iter(self._collect())

    def accept_all(self):
        successors = {}
        revisions = self._collect(successors)
        # In document order, boundary joins delete the first paragraph and retain its original successor.
        for revision in revisions: revision._apply(True, successors)
        return len(revisions)

    def reject_all(self):
        successors = {}
        revisions = self._collect(successors)
        for revision in revisions: revision._apply(False, successors)
        return len(revisions)

    def format(self, target, properties, *, author, date=None):
        """Track an explicit run/paragraph property replacement, retaining the previous properties."""
        from .formatting import create
        return Revision(create(self.story, target, properties, author=author, date=date))

    def replace(self, span, text, *, author, date=None):
        """Track a range replacement, deletion (empty text), or insertion (caret).

        Returns the created text and paragraph-boundary revision handles. New text takes the first
        affected run's formatting; rejection retains each original run's formatting.
        date is an aware datetime, defaulting to now in UTC. IDs are unique within this XML tree.
        """
        if not isinstance(span, Range): raise TypeError('Expected a text Range')
        if not isinstance(text, str): raise TypeError('replacement requires str')
        if '\n' in text or not any(p is not None and position <= span.start <= span.end <= position+len(value)
                                   for p, position, value, _, _ in span.story._paragraphs()):
            return self._replace_paragraphs(span, text, author=author, date=date)
        paragraph, _, _, template = span._preflight()
        _inside(self.story, paragraph)
        _context(paragraph)
        attrs = _revision_attrs(paragraph._tree, author, date)
        insertion = _run_text(template, text)
        expressions = []
        for kind, needed in [('del', span.start != span.end), ('ins', bool(text))]:
            if not needed: continue
            expression = E('w:'+kind, insertion if kind == 'ins' else None, attrs=next(attrs), ns={'w': _W})
            Xml(expression.bytes())  # Check the small detached payload and metadata before splitting live runs.
            expressions.append((kind, expression))
        if not expressions: return ()
        paragraph, runs, index, _ = span._isolate(extract_references=True)
        revisions = []
        for kind, expression in expressions:
            element = expression.append_to(paragraph, index)
            if kind == 'del':
                for run in runs: run.move_to(element, element._tree.xml.child_count(element.node_id))
                _rename_text(runs, 't', 'delText')
            revisions.append(Revision(element))
            index += 1
        return tuple(revisions)

    def _replace_paragraphs(self, span, text, *, author, date):
        from .paragraphs import _span_parts, _isolate_parts, _replacement_runs, _split_at, _content_start
        parts = _span_parts(span)
        paragraph = parts[0][0]
        if _name(self.story.element) == 'p':
            raise NotImplementedError('Tracked paragraph creation requires a containing Story, not paragraph-only scope')
        _inside(self.story, paragraph)
        attrs = _revision_attrs(paragraph._tree, author, date)
        def expression(kind, child=None):
            result = E('w:'+kind, child, attrs=next(attrs), ns={'w': _W})
            Xml(result.bytes())
            return result
        chunks = _replacement_runs(parts[0][3], text)
        deletions = [expression('del') if start < end else None for _, start, end, _ in parts]
        old_breaks = [expression('del') for _ in parts[:-1]]
        insertions = [expression('ins', chunk) if chunk is not None else None for chunk in chunks]
        new_breaks = [expression('ins') for _ in chunks[:-1]]
        isolated, created = _isolate_parts(parts), []
        prefix_length = isolated[0][2]-_content_start(isolated[0][0])
        for (paragraph, runs, index, _), deletion in zip(isolated, deletions):
            if deletion is None: continue
            element = deletion.append_to(paragraph, index)
            for run in runs: run.move_to(element, element._tree.xml.child_count(element.node_id))
            _rename_text(runs, 't', 'delText')
            created.append(Revision(element))
        for (paragraph, _, _, _), mark in zip(isolated, old_breaks): created.append(Revision(_add_mark(paragraph, mark)))
        paragraph = isolated[0][0]
        index = _content_start(paragraph)+prefix_length+(deletions[0] is not None)
        for i, insertion in enumerate(insertions):
            if insertion is not None:
                created.append(Revision(insertion.append_to(paragraph, index)))
                index += 1
            if i < len(new_breaks):
                left, paragraph = _split_at(paragraph, index)
                created.append(Revision(_add_mark(left, new_breaks[i])))
                index = _content_start(paragraph)
        return tuple(created)
