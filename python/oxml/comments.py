"""Classic comment anchors and Word's paragraph-ID based replies/resolution.

New comments use plain text paragraphs. Existing bodies and unrelated modern metadata
stay untouched; rendering, mentions and application-specific identities are not inferred.
"""
from datetime import datetime, timezone
from ._core import Xml
from .build import E
from .model import metadata, _walk, _one, _choose_prefix
from .text import Story, _run_text

_NS = metadata['namespaces']
_W, _W14, _W15, _CID, _CEX, _MC = (_NS[p] for p in ('w', 'w14', 'w15', 'w16cid', 'w16cex', 'mc'))
_PARTS = {'comments': 'WordprocessingCommentsPart', 'extended': 'WordprocessingCommentsExPart',
          'ids': 'WordprocessingCommentsIdsPart', 'extensible': 'WordCommentsExtensiblePart'}

def _last(element):
    paragraphs = [e for e in _walk(element) if e.qname == (_W, 'p')]
    return paragraphs[-1] if paragraphs else None

def _para_key(element):
    paragraph = _last(element)
    return (paragraph.attribute(_W14, 'paraId') or '').upper() if paragraph is not None else ''

def _anchor_id(element):
    try: return int(element.attribute(_W, 'id'))
    except (TypeError, ValueError): return None

def _record(root, uri, key, value):
    if root is None or value is None: return None
    return _one((e for e in root.children if (e.attribute(uri, key) or '').upper() == value.upper()), key)

def _next(values, hexadecimal=False):
    used = {int(v, 16 if hexadecimal else 10) for v in values if v is not None}
    value = 1 if hexadecimal else 0
    while value in used: value += 1
    if hexadecimal and value >= 0x80000000: raise ValueError('No available paragraph/durable ID')
    return f'{value:08X}' if hexadecimal else str(value)

def _attribute(element, prefix, local, value):
    """Set a namespaced attribute without rebinding an existing opaque prefix."""
    uri, bindings = _NS[prefix], dict(element.raw['namespaces'])
    chosen = _choose_prefix(bindings, uri, prefix)
    element._tree.xml.set_attribute(element.node_id, uri, local, value, chosen)
    return chosen

def _para_id(element, value):
    prefix = _attribute(element, 'w14', 'paraId', value)
    ignorable = (element.attribute(_MC, 'Ignorable') or '').split()
    if prefix not in ignorable: _attribute(element, 'mc', 'Ignorable', ' '.join([*ignorable, prefix]))

def _append(root, expression):
    # commentsExtensible permits one trailing extLst; keep opaque extension payloads last.
    xml = root._tree.xml
    index = next((xml.children(root.node_id).index(e.node_id) for e in root.children if e.qname[1] == 'extLst'),
                 xml.child_count(root.node_id))
    return expression.append_to(root, index)

class Comments:
    """Live comments, indexed by w:id (not collection position)."""
    def __init__(self, doc): self.doc = doc

    def _part(self, key, create=False):
        part = self.doc._part(_PARTS[key], create)
        return None if part is None else part.xml.root

    def __iter__(self):
        root = self._part('comments')
        if root is not None:
            for element in root.children:
                if element.qname == (_W, 'comment'): yield Comment(self, element)

    def __getitem__(self, ident):
        comment = _one((c for c in self if c.id == int(ident)), 'comment ID')
        if comment is None: raise KeyError(ident)
        return comment

    def _anchors(self, ident):
        names = ('commentRangeStart', 'commentRangeEnd', 'commentReference')
        nodes = {name: [] for name in names}
        for e in _walk(self.doc.main.xml.root):
            if e.qname[0] == _W and e.qname[1] in nodes and _anchor_id(e) == int(ident):
                nodes[e.qname[1]].append(e)
        if any(len(v) != 1 for v in nodes.values()): raise NotImplementedError('Reply needs one complete anchor in the main story')
        return nodes['commentRangeStart'][0], nodes['commentRangeEnd'][0]

    def _create(self, text, author, initials, date, parent=None):
        if not all(isinstance(v, str) for v in (text, author, initials)): raise TypeError('Comment text, author and initials require str')
        date = datetime.now(timezone.utc) if date is None else date
        if not isinstance(date, datetime) or date.utcoffset() is None: raise ValueError('Comment date requires a timezone-aware datetime')
        stamp = date.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
        roots = {key: self._part(key) for key in _PARTS}
        if roots['extensible'] is not None and roots['ids'] is None: raise ValueError('commentsExtensible is missing its commentsIds linkage')
        comments = list(self)
        reserved = [e.attribute(_W, 'id') for e in _walk(self.doc.main.xml.root)
                    if e.qname[0] == _W and e.qname[1] in {'commentRangeStart', 'commentRangeEnd', 'commentReference'}]
        ident = _next([str(c.id) for c in comments]+reserved)
        values = [value for root in roots.values() if root is not None for e in _walk(root)
                  for uri, local, value in e.raw['attributes'] if uri in {_W14, _W15, _CID} and local in {'paraId', 'paraIdParent'}]
        parent_para, parent_id = None, None
        if parent is not None:
            parent.element.node_id
            parent_para = parent._paragraph()
            parent_id = parent_para.attribute(_W14, 'paraId')
            if parent_id is None:
                if roots['ids'] is not None: raise ValueError('Parent comment has incomplete paragraph/durable-ID linkage')
                parent_id = _next(values, True)
                values.append(parent_id)
            if roots['ids'] is not None:
                record = _record(roots['ids'], _CID, 'paraId', parent_id)
                if record is None: raise ValueError('Parent comment has no durable-ID mapping')
                if roots['extensible'] is not None and _record(roots['extensible'], _CEX, 'durableId', record.attribute(_CID, 'durableId')) is None:
                    raise ValueError('Parent comment has no extensible metadata record')
        modern = parent is not None or any(roots[k] is not None for k in ('extended', 'ids', 'extensible'))
        para_id = _next(values, True) if modern else None
        lines = text.split('\n')
        paragraphs = [E('w:p', E('w:r', E('w:annotationRef')) if i == 0 else None, _run_text(None, line),
                        attrs={'w14:paraId': para_id, 'mc:Ignorable': 'w14'} if para_id is not None and i == len(lines)-1 else {})
                      for i, line in enumerate(lines)]
        body = E('w:comment', *paragraphs, attrs={'w:id': ident, 'w:author': author, 'w:initials': initials, 'w:date': stamp})
        Xml(body.bytes())  # Validate caller text/metadata before changing parts or anchors.
        durable = None
        if roots['ids'] is not None:
            durable = _next([value for root in roots.values() if root is not None for e in _walk(root)
                             for uri, local, value in e.raw['attributes'] if uri in {_CID, _CEX} and local == 'durableId'], True)
        if parent_para is not None and parent_para.attribute(_W14, 'paraId') is None: _para_id(parent_para, parent_id)
        root = roots['comments'] if roots['comments'] is not None else self._part('comments', True)
        element = _append(root, body)
        if parent is not None or roots['extended'] is not None:
            extended = roots['extended'] if roots['extended'] is not None else self._part('extended', True)
            attrs = {'w15:paraId': para_id}
            if parent_id is not None: attrs['w15:paraIdParent'] = parent_id
            _append(extended, E('w15:commentEx', attrs=attrs))
        if durable is not None:
            _append(roots['ids'], E('w16cid:commentId', attrs={'w16cid:paraId': para_id, 'w16cid:durableId': durable}))
            if roots['extensible'] is not None:
                _append(roots['extensible'], E('w16cex:commentExtensible', attrs={'w16cex:durableId': durable, 'w16cex:dateUtc': stamp}))
        return Comment(self, element)

    def add(self, range, text, author, *, initials='', date=None):
        """Anchor plain-text feedback inside one ordinary main-story paragraph."""
        range._preflight()
        if range.story.element._tree is not self.doc.main.xml: raise ValueError('Comment range must belong to this document main story')
        comment = self._create(text, author, initials, date)
        paragraph, runs, index, _ = range._isolate()
        E('w:commentRangeStart', attrs={'w:id': comment.id}).append_to(paragraph, index)
        xml = paragraph._tree.xml
        end = xml.children(paragraph.node_id).index(runs[-1].node_id)+1 if runs else index+1
        E('w:commentRangeEnd', attrs={'w:id': comment.id}).append_to(paragraph, end)
        E('w:r', E('w:commentReference', attrs={'w:id': comment.id})).append_to(paragraph, end+1)
        return comment

class Comment:
    def __init__(self, comments, element): self.comments, self.element = comments, element

    @property
    def id(self): return int(self.element.attribute(_W, 'id'))
    @property
    def author(self): return self.element.attribute(_W, 'author')
    @property
    def text(self): return Story(self.element).text

    @property
    def range(self):
        """The current-view main-story range between this comment's anchors."""
        start, end = self.comments._anchors(self.id)
        story = self.comments.doc.story
        return story.range(story._position(start), story._position(end))

    def _paragraph(self):
        paragraph = _last(self.element)
        if paragraph is None: raise NotImplementedError('Comment has no paragraph for modern linkage')
        ident = paragraph.attribute(_W14, 'paraId')
        if ident is not None:
            _one((c for c in self.comments if _para_key(c.element) == ident.upper()), 'comment paragraph ID')
        return paragraph

    def _extended(self):
        root = self.comments._part('extended')
        return None if root is None else _record(root, _W15, 'paraId', self._paragraph().attribute(_W14, 'paraId'))

    @property
    def parent(self):
        record = self._extended()
        ident = record.attribute(_W15, 'paraIdParent') if record is not None else None
        if ident is None: return None
        parent = _one((c for c in self.comments if _para_key(c.element) == ident.upper()), 'parent paragraph ID')
        if parent is None: raise ValueError('Comment parent paragraph does not exist')
        return parent

    @property
    def replies(self):
        ident = _para_key(self.element)
        root = self.comments._part('extended')
        if not ident or root is None: return []
        children = {(e.attribute(_W15, 'paraId') or '').upper() for e in root.children
                    if (e.attribute(_W15, 'paraIdParent') or '').upper() == ident.upper()}
        return [c for c in self.comments if _para_key(c.element) in children]
    @property
    def resolved(self):
        record = self._extended()
        return record is not None and record.attribute(_W15, 'done') in {'1', 'true', 'on'}

    def resolve(self, value=True):
        """Set this comment's w15:done flag, preserving its other metadata."""
        if not isinstance(value, bool): raise TypeError('Resolution requires bool')
        record = self._extended()
        if (record is not None and record.attribute(_W15, 'done') in {'1', 'true', 'on'}) == value: return self
        if record is None:
            paragraph = self._paragraph()
            roots = [self.comments._part(key) for key in _PARTS]
            ident = paragraph.attribute(_W14, 'paraId')
            if ident is None:
                if self.comments._part('ids') is not None: raise ValueError('Comment has incomplete paragraph/durable-ID linkage')
                ident = _next([v for root in roots if root is not None for e in _walk(root)
                               for ns, name, v in e.raw['attributes'] if ns in {_W14, _W15, _CID} and name in {'paraId', 'paraIdParent'}], True)
                _para_id(paragraph, ident)
            record = _append(self.comments._part('extended', True), E('w15:commentEx', attrs={'w15:paraId': ident}))
        _attribute(record, 'w15', 'done', '1' if value else '0')
        return self

    def reply(self, text, author, *, initials='', date=None):
        """Reply to this comment, reusing its existing main-story anchor boundaries."""
        start, end = self.comments._anchors(self.id)
        reply = self.comments._create(text, author, initials, date, self)
        index = start._tree.xml.children(start.parent.node_id).index(start.node_id)+1
        E('w:commentRangeStart', attrs={'w:id': reply.id}).append_to(start.parent, index)
        index = end._tree.xml.children(end.parent.node_id).index(end.node_id)
        E('w:commentRangeEnd', attrs={'w:id': reply.id}).append_to(end.parent, index)
        E('w:r', E('w:commentReference', attrs={'w:id': reply.id})).append_to(end.parent, index+1)
        return reply

    def delete(self):
        """Delete this comment and its descendants, anchors and linked modern records.

        Other comments and opaque metadata remain; empty metadata parts are retained.
        Deleting a reply leaves its parent. Use delete_thread() to remove the whole thread.
        """
        roots = {key: self.comments._part(key) for key in ('extended', 'ids', 'extensible')}
        children, by_para = {}, {}
        for comment in self.comments:
            key = _para_key(comment.element)
            if not key: continue
            if key in by_para: raise ValueError('Ambiguous comment paragraph ID')
            by_para[key] = comment
        if roots['extended'] is not None:
            for record in roots['extended'].children:
                if record.qname != (_W15, 'commentEx'): continue
                parent, para = record.attribute(_W15, 'paraIdParent'), record.attribute(_W15, 'paraId')
                if parent is not None: children.setdefault(parent.upper(), []).append((para or '').upper())
        selected, seen, records = [self], set(), []
        for comment in selected:
            if comment.id in seen: raise ValueError('Cyclic or duplicate comment reply linkage')
            seen.add(comment.id)
            para = _para_key(comment.element)
            for child in children.get(para, []):
                if child not in by_para: raise ValueError('Reply metadata has no comment body')
                selected.append(by_para[child])
            extended = _record(roots['extended'], _W15, 'paraId', para)
            mapping = _record(roots['ids'], _CID, 'paraId', para)
            if roots['ids'] is not None and mapping is None: raise ValueError('Comment has no durable-ID mapping')
            durable = mapping.attribute(_CID, 'durableId') if mapping is not None else None
            if durable is not None:
                _one((r for r in roots['ids'].children if (r.attribute(_CID, 'durableId') or '').upper() == durable.upper()),
                     'comment durable ID')
            extensible = _record(roots['extensible'], _CEX, 'durableId', durable)
            if roots['extensible'] is not None and extensible is None: raise ValueError('Comment has no extensible metadata record')
            records.extend(e for e in (extended, mapping, extensible) if e is not None)
        def selected_anchor(e):
            return e.qname[0] == _W and e.qname[1] in {'commentRangeStart', 'commentRangeEnd', 'commentReference'} \
                   and _anchor_id(e) in seen
        main = self.comments.doc.main.xml
        for story in self.comments.doc.stories():
            if story.element._tree is not main and any(selected_anchor(e) for e in _walk(story.element)):
                raise NotImplementedError('Comment anchors outside the main part are unsupported')
        anchors = [e for e in _walk(main.root) if selected_anchor(e)]
        # Preflight the complete thread before removing anything. No package-wide garbage collection.
        for anchor in anchors:
            parent, reference = anchor.parent, anchor.qname[1] == 'commentReference'
            anchor.delete()
            if reference and parent.qname == (_W, 'r') and not parent._tree.xml.children(parent.node_id) and not parent.raw['attributes']:
                parent.delete()
        for record in records: record.delete()
        for comment in selected: comment.element.delete()
        return len(selected)

    def delete_thread(self):
        """Delete this thread from its root, including all replies."""
        comment, seen = self, set()
        while True:
            if comment.id in seen: raise ValueError('Cyclic comment reply linkage')
            seen.add(comment.id)
            parent = comment.parent
            if parent is None: return comment.delete()
            comment = parent
