# oxml

Rust/Python Office Open XML client with a **DOCX package and XML-editing foundation**. Python is the primary API; no .NET runtime is required.

## Open, edit and save

```python
from oxml import Document, E, w

doc = Document.open('draft.docx')
tree = doc.main.xml
text = next(tree.elements(w.Text))
text.value = 'A targeted replacement'
report = doc.validate()
print(report['issues'], report['coverage']['gaps'])
doc.save('edited.docx')
```

`Document.new()` creates a minimal DOCX. `Tree(xml_bytes)` opens standalone XML. The pinned SDK descriptors expose 4,119 nominal element types and
582 enums, with contextual typing, typed attributes and partial structural/semantic validation. A name such as `w:del` resolves using its parent context.

Typed views and `tree.xml` raw editing use **one mutable XML state**. Ordinary edits preserve element handles; deleting/replacing a node invalidates its
views. Moving a node can change its contextual type, requiring a new typed view but retaining its raw `node_id`. `element.raw` is an immutable snapshot.

```python
body = next(tree.elements(w.Body))
paragraph = E('w:p', E('w:r', E('w:t', 'New paragraph'))).append_to(body)
paragraph.copy_to(body)
```

`E` builds detached subtrees and attaches them in one operation. It accepts parsed `Element` children alongside nested expressions and text, capturing their
XML when the expression is constructed; later source edits do not affect it. These snapshots and cross-tree `copy_to` preserve XML namespace context, but
do not copy package dependencies or remap relationship/document IDs.

The internal quick-xml editor supports ordered elements, attributes, text, comments and processing instructions; namespace-aware insertion, deletion,
replacement, copying and movement; and UTF-8/UTF-16 input. Ordinary edits use preflight checks and in-place mutation, not whole-part cloning or serialization.
Serialization is deferred until bytes/save are requested; aggregate output limits can fail then. No-op XML keeps its original bytes; edited XML is serialized as
UTF-8, preserving unknown content and namespace meaning rather than original formatting or CDATA boundaries. Mixed-content text replacement is refused.

`doc.package` exposes parts, content types and scoped relationships. The main part is discovered through the package relationship, not a fixed filename.
Untouched package saves are byte-identical, and changed saves retain untouched payloads without garbage collection. Saving to a path uses atomic replacement.
Opaque and embedded payloads remain bytes; they do not require dedicated editing APIs.

## Text and reviews

`doc.story` exposes current main-story text, literal search across runs, and Unicode character ranges. `Story(element, view='original')` provides a read-only
original view without accepting/rejecting stored revisions. `doc.stories()` enumerates separate body, header/footer, note and comment stories, each carrying
its live element and owning `part_uri`. `span.replace(text)` preserves unaffected formatting; new text takes the first affected run's format.
Any XML edit makes existing ranges stale, so find/select again before the next edit.

```python
doc = Document.open('draft.docx')
comment = doc.comments.add(doc.story.find('fourteen days'), 'Please extend this period.', 'Reviewer')
comment.reply('Agreed.', 'Drafter')
comment.resolve()
doc.save('commented.docx')
```

Comments are indexed by ID (`doc.comments[id]`). Replies and resolution maintain supported modern paragraph/durable-ID linkage and preserve other metadata.
`comment.range` finds its anchored text; `comment.delete()` removes its reply subtree, while `delete_thread()` removes the whole thread. Deletion removes
linked metadata and anchors, not unrelated parts. Main-part anchors are supported; deletion refuses matching anchors in other stored stories.
An independent tracked-edit workflow:

```python
doc = Document.open('draft.docx')
doc.revisions.replace(doc.story.find('fourteen days'), 'twenty-one days', author='Drafter')
doc.save('redlined.docx')
```

Iterating `doc.revisions` exposes inline insertions/deletions, paragraph-boundary changes and run/paragraph property histories; each supports `accept()`/`reject()`.
Bulk `accept_all()`/`reject_all()` preflight the story and refuse unsupported families rather than silently skipping them. `Revisions(story)` handles another
explicit story. `doc.revisions.format(run, E('w:rPr', E('w:b')), author='Drafter')` tracks a direct formatting change; `.previous`/`.current` expose its properties.

Ordinary text beside existing revisions is editable; editing inside/across a revision requires explicit acceptance/rejection first. `\n` replacement supports
paragraph splits/joins among adjacent sibling paragraphs, but not section or table-container boundaries. Joins retain the last paragraph's properties.
Bookmarks/comment anchors are zero-width and survive text edits: enclosing anchors retain replacement text, and interior anchors collapse to its end.
Fields, content controls, moves and opaque payloads remain protected; unsupported text structures appear as U+FFFC. These are stored-text views, not rendering.

## Document conveniences

* `doc.styles` finds/creates styles and applies references without replacing direct formatting.
* `doc.numbering.add([Level(), Level(format='lowerLetter')])` creates a multilevel list. Reuse the returned instance with `.apply(paragraph, level=...)`
  to continue it; `.restart(start=...)` creates a separate instance without changing the original list.
* `Table.add(body, [['Clause', 'Response']], widths=[4000, 4000])` creates a rectangular table. `Table(element)` exposes rows/cells and row/column edits;
  use `Story(cell)` for cell text. Structural operations refuse merged/offset/revised grids, and removal refuses cells containing bookmarks/comments.
* `doc.bookmarks.add(span, 'clause')` creates a bookmark with `.range`, `.remove()` and `.ref(text)` for detached REF markup with a cached result.
* `doc.hyperlinks.add(span, '#clause')` or `.add(span, 'https://example.com/')` links existing formatted text. `.remove()` unwraps it and removes unused
  relationships in the owning part. Hyperlink text is visible but protected from range edits while wrapped.

These APIs manipulate explicit OOXML; they do not compute style inheritance, displayed list counters, field results or layout.

## Import and compare

```python
from oxml import import_content, compare, w

blocks = list(source.main.xml.elements(w.Paragraph))[:2]
body = next(destination.main.xml.elements(w.Body))
import_content(source, blocks, destination, body)

redline = compare(original, revised, author='Reviewer')
redline.save('comparison.docx')
```

`import_content` copies selected paragraphs/tables with explicit style/numbering dependencies, images and hyperlinks. It remaps conflicting identifiers,
keeps complete bookmark ranges and carries inherited `mc:Ignorable` namespace meaning. It never overwrites destination definitions; destination themes and
document defaults still apply. Review/field/section/unsupported package dependencies are refused, rather than silently dropped. This is not whole-package merging.

`compare` returns a new document with tracked body-text and direct run/paragraph formatting differences, preserving the originals. Equal opaque blocks remain
untouched. Paragraph-count changes currently require uniform matching direct properties and a paragraph-only body (apart from final section properties).
Changed tables, sections, dependencies and unresolved revisions are refused. Routine metadata/revision-session bookkeeping is retained from the original.
Comparison uses stored text/properties, not rendered appearance, move detection or Word's complete comparison semantics.

## Current boundaries

* Validation is **incomplete**, with errors and unchecked regions reported separately. No reported errors does not establish validity.
  `Document.validate()` checks reachable package relationships and validates declared XML parts with their actual dependency context, including secondary parts.
  Issues identify their part URI; malformed XML and opaque/unchecked regions remain explicit. Standalone validation accepts live `Tree` dependencies.
  This is not complete SDK validation.
  Compatibility processing selects a validation view without removing stored branches.
* Typed setters refuse invalid or incompletely checked constraints. Raw lexical edits remain available for unsupported cases.
  Numeric, pattern, list and union checks share the imported rules; remaining dialect and semantic gaps are explicit.
  BooleanValue getters and validation accept surrounding XML whitespace; OnOffValue requires exact tokens, matching the SDK.
* Vocabulary coverage, validation coverage and operation support are separate. Strict DOCX package discovery works, but Strict XML namespaces are not
  converted to Transitional typed vocabulary. Table/move revision operations, XLSX and rendering remain unimplemented.
* Signed packages permit unchanged pass-through only. Encrypted, macro-enabled/template, ZIP64, multidisk and unsafe archives are refused.
  Shared-document concurrent editing, a stable Python ABI and current Word interoperability are not established. CI is configured to install/test the actual
  Linux/macOS CPython 3.10–3.13 wheels before publication; local source tests alone do not establish that platform matrix.

The source policy targets SDK parity using pinned SDK JSON, relevant C# information and small explicit supplements—not an independent standards audit.
`scripts/import_sdk.py` regenerates the shared descriptor; ordinary builds and installed packages do not need an SDK checkout.
The curated corpus contains 18 original DOCX files, with targeted editing tests for body structures, review metadata and secondary parts. Independent XML
comparisons and exact untouched-payload checks provide preservation evidence, not application interoperability claims.
See [DEV.md](DEV.md) for implementation boundaries, resource limits and reproducible verification.

## Development

```bash
maturin develop && pytest -q
```

In the shared `aai-ws` workspace, run `ws-add oxml` after the first GitHub push to register the project.

## Build

```bash
ship-rs-build
```

## Release

```bash
ship-release
```

`ship-release` tags the Cargo version, leaves wheel publication to GitHub Actions, then bumps the project.
