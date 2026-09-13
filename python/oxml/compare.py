"""Body text/direct-format comparison producing ordinary Word tracked revisions.

This is not Word's layout-aware comparer: changed opaque content and package
dependencies are refused, while untouched content and original metadata remain.
"""
from difflib import SequenceMatcher
from datetime import datetime, timezone
from .build import e
from .document import Document
from .model import Tree, _walk, metadata
from .paragraphs import _property
from .revisions import _revision_attrs, _revision_name
from .text import Story, _W, _XML, _name, _content, _paragraph, _run_text

_METADATA = {'application/vnd.openxmlformats-package.core-properties+xml',
             'application/vnd.openxmlformats-officedocument.extended-properties+xml'}
_META_RELS = {'http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties',
              'http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties'}

def _key(element, omit=(), *, bookkeeping=False, preserve=None):
    """Structural equality, ignoring indentation but retaining opaque namespace meanings."""
    if element is None: return None
    raw, children = element.raw, []
    has_elements = bool(element.children)
    ancestor = element
    while ancestor is not None:
        space = ancestor.attribute(_XML, 'space')
        if space is not None:
            preserve = space == 'preserve'
            break
        if preserve is not None: break
        ancestor = ancestor.parent
    for node in _content(element):
        if node['kind'] == 'element':
            child = element._tree._element(node['id'])
            if _name(child) not in omit: children.append(_key(child, bookkeeping=bookkeeping, preserve=bool(preserve)))
        elif node['kind'] != 'text' or node['text'].strip() or not has_elements or preserve or raw['qname'][0] != _W:
            children.append((node['kind'], node.get('text'), node.get('target')))
    attrs = _attrs(element) if bookkeeping else tuple(sorted(raw['attributes']))
    return raw['qname'], attrs, tuple(sorted((k, v) for k, v in raw['namespaces'] if v)), tuple(children)

def _dependencies(doc):
    package, result = doc.package, {}
    names = [n for n in package.part_names() if n.lower() != '/[content_types].xml' and not n.lower().endswith('.rels')]
    for uri in ['/', *names]:
        if uri != '/':
            content_type = package.content_type(uri)
            if content_type in _METADATA: continue
            if uri != doc.main.uri:
                data = package.read_part(uri)
                if content_type.endswith('xml'):
                    root = Tree(data).root
                    data = _key(root, ('rsids',) if _name(root) == 'settings' else ())
                result[uri] = content_type, data
        relations = []
        for rel in package.relationships(uri):
            if uri == '/' and rel['type'] in _META_RELS: continue
            target = rel['target'] if rel['target_mode'] == 'External' else package.relationship_part(uri, rel['id'])
            relations.append((rel['id'] if uri != '/' else '', rel['type'], target, rel['target_mode']))
        result[uri, 'relationships'] = sorted(relations)
    return result

def _attrs(element):
    # Word bookkeeping is not a reviewable text/format change; keep the original values.
    return tuple(sorted((uri, name, value) for uri, name, value in element.raw['attributes']
        if not (uri == _W and name.startswith('rsid') or uri == metadata['namespaces']['w14'] and name in {'paraId', 'textId'})))

def _props(element, name, omit=()):
    properties = _property(element, name)
    if properties is None: return (), (), ()
    key = _key(properties, omit)
    return key[1], key[3], key[2] if key[1] else ()

def _plain(paragraph):
    text, spans, barriers = _paragraph(paragraph)
    if barriers or any(c is None and node['kind'] != 'text' or c is not None and _name(c) not in {'pPr', 'r', 'proofErr'}
                       for (c, _, _), node in zip(spans, _content(paragraph))):
        raise NotImplementedError('Changed paragraphs must contain ordinary runs and direct properties only')
    if len([c for c in paragraph.children if _name(c) == 'pPr']) > 1:
        raise NotImplementedError('Multiple paragraph-property blocks are unsupported')
    runs = [(a, b, r) for r, a, b in spans if r is not None and _name(r) == 'r' and a < b]
    for run in (r for r, _, _ in spans if r is not None and _name(r) == 'r'):
        if len([c for c in run.children if _name(c) == 'rPr']) > 1:
            raise NotImplementedError('Multiple run-property blocks are unsupported')
        if any(_name(c) not in {'rPr', 't', 'tab', 'br', 'cr'} for c in run.children):
            raise NotImplementedError('Changed paragraphs cannot contain annotations or unsupported run payloads')
        for child in run.children:
            name = _name(child)
            if name == 'rPr': continue
            allowed = {'t': {(_XML, 'space')}, 'br': {(_W, 'type')}}.get(name, set())
            if any((uri, local) not in allowed for uri, local, _ in child.raw['attributes']) or name != 't' and _content(child):
                raise NotImplementedError('Changed paragraphs contain unsupported text/break/tab metadata')
    return text, runs

def _fragments(runs, text, start, end):
    return [_run_text(run, text[max(a, start):min(b, end)]) for a, b, run in runs if a < end and b > start]

def _inserted_runs(revision, expressions):
    for run in revision.element.children: run.delete()
    for expression in expressions: revision.element(expression)

def _paragraph_diff(revisions, original, revised, author, date):
    if _key(original, bookkeeping=True) == _key(revised, bookkeeping=True): return
    before, old_runs = _plain(original)
    after, new_runs = _plain(revised)
    if _attrs(original) != _attrs(revised): raise NotImplementedError('Changed paragraph attributes are unsupported')
    for name in ('rPr', 'sectPr'):
        old, new = _property(original, 'pPr'), _property(revised, 'pPr')
        if _key(_property(old, name) if old else None) != _key(_property(new, name) if new else None):
            raise NotImplementedError('Paragraph-mark and section property differences are unsupported')
    opcodes, formatting = SequenceMatcher(None, before, after, autojunk=False).get_opcodes(), []
    for tag, a, b, c, d in opcodes:
        if tag != 'equal': continue
        for left, right, run in old_runs:
            start, end = max(a, left), min(b, right)
            if start >= end: continue
            for x, y, replacement in new_runs:
                low, high = max(c+start-a, x), min(c+end-a, y)
                if low >= high: continue
                if _attrs(run) != _attrs(replacement): raise NotImplementedError('Changed attributes on retained runs are unsupported')
                if _props(run, 'rPr') != _props(replacement, 'rPr'): formatting.append((low, high, replacement))
    story = Story(original)
    for tag, a, b, c, d in reversed(opcodes):
        if tag == 'equal': continue
        created = revisions.replace(story.range(a, b), after[c:d], author=author, date=date)
        for revision in created:
            if revision.kind != 'ins': continue
            _inserted_runs(revision, _fragments(new_runs, after, c, d))
    for start, end, replacement in reversed(formatting):
        _, selected, _, _ = story.range(start, end)._isolate()
        for run in selected: revisions.format(run, _property(replacement, 'rPr') or e.rPr(), author=author, date=date)
    if _props(original, 'pPr', ('rPr', 'sectPr')) != _props(revised, 'pPr', ('rPr', 'sectPr')):
        properties = _property(revised, 'pPr')
        expression = e.pPr() if properties is None else Tree(properties._tree.xml.subtree_bytes(properties.node_id)).root
        if properties is not None:
            for child in expression.children:
                if _name(child) in {'rPr', 'sectPr'}: child.delete()
        revisions.format(original, expression, author=author, date=date)

def _multiline(revisions, original, revised, author, date):
    """One paragraph-count hunk; uniform properties avoid ambiguous boundary/property history."""
    if not original or not revised: raise NotImplementedError('Comparison requires at least one paragraph in each document')
    old, new = [_plain(p) for p in original], [_plain(p) for p in revised]
    properties = _props(original[0], 'pPr')
    if any(_props(p, 'pPr') != properties or _attrs(p) for p in [*original, *revised]):
        raise NotImplementedError('Paragraph-count changes require matching uniform paragraph properties')
    if any(_name(e) in {'rPr', 'sectPr'} for p in [*original, *revised] for c in p.children if _name(c) == 'pPr' for e in c.children):
        raise NotImplementedError('Paragraph-count changes with paragraph-mark or section properties are unsupported')
    all_runs = [r for _, runs in old+new for _, _, r in runs]
    if all_runs and any(_props(r, 'rPr') != _props(all_runs[0], 'rPr') or _attrs(r) for r in all_runs):
        raise NotImplementedError('Paragraph-count changes require matching uniform run properties')
    before, after = '\n'.join(t for t, _ in old), '\n'.join(t for t, _ in new)
    start = 0
    while start < min(len(before), len(after)) and before[start] == after[start]: start += 1
    end = 0
    while end < min(len(before), len(after))-start and before[-end-1] == after[-end-1]: end += 1
    offset = next(pos for p, pos, _, _, _ in revisions.story._paragraphs() if p is not None and p.node_id == original[0].node_id)
    created = revisions.replace(revisions.story.range(offset+start, offset+len(before)-end),
                                after[start:len(after)-end], author=author, date=date)
    for revision in created:
        if revision.kind == 'ins' and not revision.is_boundary:
            _inserted_runs(revision, [_run_text(all_runs[0] if all_runs else None, revision.text)])

def compare(original, revised, *, author, date=None):
    """Return a new document with tracked body-text/direct-format differences.

    Equal opaque body blocks survive untouched. Changed tables, fields, relationships,
    dependencies, sections and unresolved revisions are refused. Core/extended package
    metadata changes are ignored. Paragraph-count changes currently require matching,
    uniform direct properties and an otherwise ordinary paragraph-only body.
    """
    if not isinstance(original, Document) or not isinstance(revised, Document): raise TypeError('compare requires two Documents')
    if original.main.uri != revised.main.uri or _dependencies(original) != _dependencies(revised):
        raise NotImplementedError('Changed content or formatting dependencies are unsupported')
    for doc in (original, revised):
        if any(_revision_name(e) for e in _walk(doc.main.xml.root)):
            raise NotImplementedError('Resolve existing revisions before comparing documents')
    # Share one timestamp across all changes, including the default-now case.
    if date is None: date = datetime.now(timezone.utc)
    next(_revision_attrs(original.main.xml, author, date))
    result = Document.from_bytes(original.bytes())
    roots = result.main.xml.root, revised.main.xml.root
    if any(_name(root) != 'document' for root in roots) or _attrs(roots[0]) != _attrs(roots[1]):
        raise NotImplementedError('Changed document-root attributes or non-Transitional vocabulary are unsupported')
    bodies = [_property(root, 'body') for root in roots]
    if any(b is None for b in bodies): raise ValueError('Expected a document body')
    for root, body in zip(roots, bodies):
        if any(c.node_id != body.node_id for c in root.children): raise NotImplementedError('Extra document-root content is unsupported')
    if _attrs(bodies[0]) != _attrs(bodies[1]): raise NotImplementedError('Changed body attributes are unsupported')
    old, new = (b.children for b in bodies)
    if len(old) == len(new):
        for left, right in zip(old, new):
            if _name(left) == _name(right) == 'p': _paragraph_diff(result.revisions, left, right, author, date)
            elif _key(left, bookkeeping=True) != _key(right, bookkeeping=True):
                raise NotImplementedError('Changed opaque body content or section properties are unsupported')
    else:
        if any(_name(c) not in {'p', 'sectPr'} for c in old+new):
            raise NotImplementedError('Paragraph-count changes around opaque body content are unsupported')
        if [_key(c) for c in old if _name(c) == 'sectPr'] != [_key(c) for c in new if _name(c) == 'sectPr']:
            raise NotImplementedError('Changed section properties are unsupported')
        _multiline(result.revisions, [c for c in old if _name(c) == 'p'], [c for c in new if _name(c) == 'p'], author, date)
    return result
