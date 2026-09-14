# oxml

Create and edit Word DOCX files from Python, including text, tables, comments and tracked changes. oxml is a Rust document library with thin Python bindings and types derived from Microsoft's Open XML SDK. It requires neither .NET nor Office.

## Edit a document

```python
from oxml import Document

doc = Document.open('draft.docx')
doc.story.find('fourteen days').replace('twenty-one days')
doc.save('edited.docx')
```

Search works across text runs, so a phrase need not have uniform formatting. Replacement preserves surrounding formatting and uses the first affected run's format for the new text. Find the text again after each edit: ranges refer to a particular version of the XML.

`doc.story` is the main document text. `doc.stories()` also gives access to headers, footers, notes and comments. `Story(element, view='original')` reads the text before tracked changes without accepting or rejecting them.

Replacement can split and join adjacent paragraphs using `\n`, but cannot cross section or table-cell boundaries. Bookmarks and comment anchors survive text edits. A replacement may change a field's cached result or remove a whole field, but cannot cross a field boundary. Content controls and existing revision payloads are protected from ordinary text replacement.

## Build and edit XML

Use `Document.new()` to start a document, or `Tree(xml)` for standalone XML, from bytes or a detached expression.

```python
from oxml import Document, e, w

doc = Document.new()
body = next(doc.main.xml.elements(w.Body))
paragraph = body(e.p(e.r(e.t('New paragraph'))))
paragraph(e.pPr(e.jc(val='center')))
doc.save('new.docx')
```

`e.p(...)` builds a detached XML expression. Calling a live parent, such as `body(...)`, attaches it and returns the new live element. Placement follows the schema: paragraph properties go before runs, and paragraphs go before final section properties. Children already in schema order stay where they are. Siblings out of schema order are first sorted into it, keeping the relative order of children that share a slot, and the attached expression's own descendants are sorted the same way. `element.reorder()` runs that sort on demand, and `reorder(deep=True)` sorts a whole subtree. Use `parent(expression, index=n)` when you need an exact XML child-node position or the schema order is unknown.

The `e` factory supplies the `w` namespace for elements and attributes. For example, `e.tcW(type='dxa', w=2400)` creates a table-cell width. Other attribute prefixes use double underscores, such as `r__id` and `xml__space`. Configure another namespace with `E('a')`, or custom bindings with `E(ns=bindings)`.

You can also work directly with typed elements. For example, `next(doc.main.xml.elements(w.Text)).value = 'Replacement'` changes one text node. Typed attributes check values against SDK rules and refuse constraints they cannot fully check. Raw XML editing remains available for those cases.

Use `tree.count(w.Paragraph)` to count matching elements in Rust without constructing a list of Python views. `tree.count()` counts all elements; both forms include the root when it matches. To require exactly one match, unpack it: `body, = tree.elements(w.Body)`.

The XML editor supports elements, attributes, text, comments and processing instructions, with namespace-aware copying and movement. `str(element)` shows any live element's XML. It reads UTF-8 and UTF-16. Typed views and raw XML edits share the same live tree.

See [Editing and preservation contracts](DEV.md#editing-and-preservation-contracts) for copying, namespaces and raw XML operations.

## Comments and tracked changes

Add a comment to a text range:

```python
doc = Document.open('draft.docx')
comment = doc.comments.add(doc.story.find('fourteen days'),
    'Please extend this period.', 'Reviewer')
comment.reply('Agreed.', 'Drafter')
comment.resolve()
doc.save('commented.docx')
```

Comments support plain-text bodies, replies, resolution and deletion of individual reply subtrees or whole threads. Comment anchors are currently supported in the main document. Modern reply and resolution metadata is maintained alongside the comment text.

Record a replacement as a tracked change:

```python
doc = Document.open('draft.docx')
doc.revisions.replace(doc.story.find('fourteen days'),
    'twenty-one days', author='Drafter')
doc.save('redlined.docx')
```

Tracked changes cover text insertions and deletions, paragraph splits and joins, and direct run/paragraph formatting. Iterate over `doc.revisions` to accept or reject individual changes. `accept_all()` and `reject_all()` handle a whole story. Use `doc.revisions.format(...)` to track formatting changes.

Editing inside an existing revision requires accepting or rejecting it first. Table, move and nested revision histories are not supported. Bulk operations refuse unsupported revision types rather than silently skipping them. See [Text/review scope](DEV.md#textreview-scope) for the detailed rules.

## Styles, lists, tables and links

* `doc.styles` finds, creates and applies paragraph, character and table styles without replacing direct formatting.
* `doc.numbering` creates multilevel lists and controls continuation or restart.
* `Table.add(...)` creates rectangular tables. `Table(element)` provides row and column edits. Structural edits do not support merged, offset or revised grids.
* `doc.bookmarks` creates, finds and removes bookmarks and builds REF fields with optional switches.
* `doc.hyperlinks` adds and removes internal or external links, and retargets them through `link.target` while retaining the text's formatting. Linked text is protected from range edits until the link is removed. `doc.hyperlinks.link(url, *runs)` builds a detached link while constructing content.
* `part.add_image(bytes)` embeds a picture and returns detached drawing markup for a run, sized from the image itself unless given EMU extents.
* `doc.footnotes` creates footnotes from text or block expressions, marks them after live text or through a detached reference run, and removes them with their references.

These helpers edit the document's XML. They do not calculate layout, inherited formatting, displayed list numbers or field results. Usage details are in [Document helpers](DEV.md#document-helpers).

`doc.set_custom_xml(item_id, xml_bytes, schema_uri=...)` creates or replaces a custom XML datastore by GUID and returns its `Part`. It manages the property part and relationships while preserving unrelated stores. Content controls can refer to the GUID through `w:storeItemID`.

`doc.properties` is a mapping over the core properties part (`title`, `creator`, `lastModifiedBy`, `created` and so on), created on first write. `doc.settings` is a native schema-aware mapping for flat settings: `updateFields` reads as a boolean, while values such as `defaultTabStop` read as strings. Use `doc.settings.root` for complex settings. `doc.add_part('HeaderPart')` creates another declared part related to the main document, and `doc.package.relationship_id(source, target)` finds the relationship id that a section or reference needs.

## Compare and import documents

Compare two documents to produce a new document with tracked text and direct-formatting changes:

```python
from oxml import Document, compare

redline = compare(Document.open('original.docx'),
    Document.open('revised.docx'), author='Reviewer')
redline.save('comparison.docx')
```

The originals stay unchanged, and equal tables and other opaque blocks are retained. Comparison supports body-text and direct-formatting changes. Paragraphs align by text, and each changed region is compared by Unicode words across run and paragraph boundaries, retaining whitespace, punctuation and each side's formatting, so paragraph splits, joins, insertions and deletions appear as paragraph-mark changes. Tables with the same grid compare cell by cell. Field results compare as text, and a changed field instruction replaces the whole field. Hyperlinks in changed paragraphs become HYPERLINK fields, as in Word, so a changed target replaces the whole link. A bookmark that arrives with inserted text keeps its name, and the copy left in deleted text loses it. Changed table structure, sections and other dependencies are refused, as are documents with unresolved revisions.

`import_content(...)` copies selected paragraphs and tables between documents, including their style, numbering, image and hyperlink dependencies. It preserves complete bookmark ranges and remaps conflicting IDs without overwriting destination definitions. Destination themes and document defaults still apply. Content with reviews, fields, sections or unsupported package dependencies is refused. See [Import and compare](DEV.md#import-and-compare) for the detailed rules.

## Preservation and limits

Saving an unchanged document returns the original bytes. Edited saves retain untouched package payloads. XML edits preserve namespace meaning and unknown content. Edited XML is serialized as UTF-8 without retaining its original formatting. `doc.package` gives access to parts, content types and relationships.

`doc.validate()` checks XML structure, attribute values, supported semantic rules and package relationships, including headers, footers and other reachable parts. Errors and unchecked regions are reported separately. Validation is incomplete: a report without errors does not establish that a document is valid.

Current document support focuses on DOCX. XLSX editing and conversion of Strict XML namespaces to the typed vocabulary are not implemented. Signed documents can pass through unchanged but cannot be edited. Encrypted, macro-enabled and template documents, ZIP64 and multidisk archives are refused. See [DEV.md](DEV.md) for resource limits, validation gaps and development commands.

## License and acknowledgements

oxml's own code is [Apache-2.0 licensed](LICENSE). Imported material retains its upstream notices and terms.

* **[Open XML SDK](https://github.com/dotnet/Open-XML-SDK)** — Microsoft, the .NET Foundation and contributors. Its schema metadata, validator behavior, implementation ideas and tests are the foundation for our typed model and validation. The [SDK copyright and MIT notice](python/oxml/SDK-LICENSE) ships with the Python package.
* **[Open XML PowerTools](https://github.com/OpenXmlDev/Open-Xml-PowerTools)** — Microsoft, Eric White and contributors, for revision-processing and document-comparison reference behavior, tests and fixtures.
* **[Pandoc](https://github.com/jgm/pandoc)** — John MacFarlane, Jesse Rosenthal and contributors, for DOCX fixtures and independent reader/review expectations.
* **[LibreOffice](https://github.com/LibreOffice/core)** contributors and The Document Foundation, for regression documents and tests covering modern comments and cross-part content.
* **[python-docx](https://github.com/python-openxml/python-docx)** — Steve Canny and contributors, for the section fixture and API/test examples; **[Apache POI](https://github.com/apache/poi)** — the Apache Software Foundation and contributors, for the header-image fixture and relationship tests.

The [fixture source table](tests/fixtures/README.md) maps borrowed files to their upstream locations and retained licenses. Adapted tests identify their upstream cases in source comments. Thanks also to the developers of our runtime dependencies, especially PyO3 and quick-xml.
