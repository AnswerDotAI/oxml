# Development

## Commands

```bash
maturin develop && pytest -q
cargo test
ship-rs-build
```

Run `maturin develop` after Rust or embedded-descriptor changes to refresh the installed extension before Python tests. Coordinate parallel workers around one rebuild of the shared extension. `schema/metadata.json` is included in uv's cache keys and changes invalidate cached builds.

## Architecture

**One native owner of document state, one implementation of document rules, and thin Python bindings.** The Rust core is usable directly without initializing Python:

* `src/xml.rs` stores ordered XML nodes in a mutable arena. It parses with quick-xml 0.42's decoding reader and checks XML well-formedness. Standalone trees, package parts and OPC control XML all use this editor and serializer.
* `src/package.rs` owns ZIP/OPC packages and their loaded XML trees. Cloned handles share native state; reads and saving see edits without Python caches or flushing. Replacement/removal invalidates native part and XML handles. It also manages content types, relationships and custom XML datastores.
* `src/schema.rs` interprets imported SDK types, content models, lexical constraints, versions and semantic rules. It also resolves contextual types, checks typed properties and determines insertion positions.
* `src/package_schema.rs` shares declared-part discovery and relationship traversal between validation, story enumeration and document operations.
* `src/text.rs` provides story projections, Unicode ranges, run splitting and paragraph operations. Plain and tracked replacements share preparation and protection rules, with separate mutation algorithms.
* `src/revisions.rs`, `src/comments.rs` and `src/links.rs` implement tracked edits, comments, bookmarks and hyperlinks. Timestamp validation and namespace handling are shared rather than reimplemented per feature.
* `src/definitions.rs` and `src/tables.rs` implement styles, numbering and rectangular-table edits.
* `src/properties.rs` implements core properties and schema-aware flat settings. `src/footnotes.rs` owns footnote creation, including default styles.
* `src/importing.rs` transfers selected subtrees with per-import dependency and ID maps. `src/compare.rs` uses similar's Myers matcher over Unicode word tokens for paragraph text, maps token ranges to native character positions, and uses the same revision writer as explicit edits.
* `src/error.rs` supplies native errors; conversion to Python exceptions happens at the binding boundary.

The schema layer builds one immutable index for contextual child lookup, semantic-rule dispatch and namespace availability. Content models compile once, including versioned occurrences. Their bounded matcher uses contiguous position sets. Validation borrows live dependency trees and uses compatibility-aware traversal for reference and uniqueness checks. Reference-value sets and duplicate tracking belong to each validation call.

`python/oxml/` contains Python views, argument/result conversion and detached construction conveniences. `model.py` creates nominal classes and enums from compact native descriptors; Python does not load the full schema or implement its rules. `build.py` adds namespace lookup and parsed-element snapshots to fastcore's builder. The other modules delegate document operations to Rust. There is no parallel Python implementation or compatibility layer.

XML/package handles use shared native ownership. A Python `Tree` wrapper is not the tree's identity: separately acquired wrappers can refer to the same native state. Native child/descendant iterators share traversal without allocating full result lists; mutation callers collect node IDs when needed. JSON is used only for explicit raw snapshots and coarse validation reports, not internal node-by-node work. Small subtree snapshots are used where copying or retaining previous formatting requires them; ordinary edits do not clone documents or provide transaction rollback.

## XML construction

`E(prefix='', attr_ns=None, ns=None)` configures a factory. `e = E('w', attr_ns='w')` is the WordprocessingML preset. `e.tag(*children, **attrs)` builds a detached `XML` expression using fastcore's namespace-aware builder.

Children can be expressions, parsed `Element` views, strings or ordered collections. `None` children are omitted. Parsed children capture namespace-complete snapshots at construction that survive later source edits or invalidation. Raw XML bytes must be parsed before use as children.

Calling a detached expression, such as `props(e.b(), e.i())`, appends children and returns that same expression for chaining. It uses the same child handling as construction, including snapshots of live elements taken when they are added. This differs from calling a live parent, which attaches a child in schema order and returns the attached child.

Factories resolve names at construction and each expression retains its own bindings. `ns` supplies custom and default namespace bindings, including prefixes used inside opaque attribute values. Keyword attributes use `attr_ns`. `prefix__name` selects an explicit prefix and `attrs_` accepts literal names. A trailing underscore escapes Python keywords, as in `e.del_()`.

Factory attributes are serialized as XML text values. Booleans become `true` or `false` (`on` or `off` for attributes identified by native enum metadata), and `None` attributes are omitted. Construction checks XML names, namespaces and characters. `.bytes()` serializes the expression for insertion, declaring every binding including an empty default namespace; `str(expression)` is the standalone document form.

`parent(expression)` attaches one expression and returns its new typed child. It places the child after others in its schema slot and before later slots. Siblings out of schema order are sorted first, keeping the relative order within a slot and the positions of text, comments and processing instructions, and the new subtree is sorted the same way once inserted. Copying, placement and sorting share one native edit; a live source element is copied without serialization and reparsing. `element.reorder()` runs that sort alone. `reorder(deep=True)` sorts every descendant whose children can all be ranked and leaves the others untouched, while the element itself must be rankable. Cached slots distinguish ordered sequences from unordered or repeatable content groups. Document helpers share the insertion-position rules; raw XML insertion and `copy_to()` retain explicit order.

Automatic placement requires an unambiguous slot for the new child and for every existing child. Unknown children, wildcard models and ambiguous positions require `parent(expression, index=n)`. The explicit index counts every XML child node, including text, comments and processing instructions. It also supports untyped XML and deliberately invalid fixtures. Attachment runs the native XML and resource checks. Placement and `reorder()` errors name the cause: the element without a schema type, the type without a content model, or the child without a slot. A standalone root whose name belongs to several SDK types takes the one with a content model when exactly one has it, so `Tree(e.style(...))` is a `Style`; otherwise the error lists the candidates.

`element.index` is an element's position in its parent's XML child-node sequence, so `parent(expression, index=sibling.index)` inserts before a sibling and `element.move_to(parent, sibling.index + 1)` moves after one. `element.elements(cls)` yields typed descendant views, like `Tree.elements` scoped to one element. `element.set_attribute(uri, local, value)` declares an unbound namespace with the SDK's prefix for it, and typed attribute setters do the same.

`Tree.count(cls=Element)` uses native traversal and the same contextual type matching as `Tree.elements`, including the root when it matches. It does not materialize matching IDs or Python views, and reflects the current tree after edits. `element.child(cls)` also looks up direct children natively, returning one typed view or `None`, and rejecting duplicates. Use unpacking for exactly-one assertions rather than counting a materialized iterator.

## Editing and preservation contracts

`Tree.xml` is the native mutable editor. `root`, `document_children()`, `children(id)` and `parent(id)` expose ordered node identities. `node(id)` returns a JSON snapshot whose `children` includes IDs for elements, text, comments and processing instructions. `Element.children` filters these to element views.

`set_text`, expanded-name `set_attribute`/`remove_attribute`, `rename`, `declare_namespace`, insertion, `delete`, `replace_node`, `copy` and `move_node` all operate on the same tree. Python structural methods use indexes into the full XML child-node sequence.

Edits preflight the affected nodes, mutate live storage and invalidate cached serialization. Bytes are serialized when requested. The editing API has no transaction rollback.

Node IDs survive unrelated edits and movement. Copies allocate new IDs. Deletion or replacement invalidates subtree IDs permanently. A typed view checks its contextual type natively. Reacquire the view if a move changes that type. `element.raw` produces an immutable node snapshot on demand. `element.bytes()` serializes its subtree, declaring the namespaces it needs, and `str(element)` is the same text. Its repr shows only its type and node ID, without serializing content. `Element.qname` returns the expanded `(namespace_uri, local_name)` pair. It and `Element.attribute` use scalar native getters.

`copy_to(parent, index=None)` copies XML within or across trees. The default index is the parent's raw child count. Cross-tree copies transfer native subtrees with their namespace context, without serialization and reparsing. Both copies and construction snapshots retain relationship and document IDs unchanged. Use `import_content` for supported package-dependency transfer and ID remapping. `move_to(parent, index)` moves an element within one tree.

Edits retain in-scope namespace bindings, including bindings used only in opaque attribute values. Renaming to an unqualified element clears its default namespace while preserving descendant contexts. Use a fresh prefix or an explicit namespace operation when a name edit would conflict with an existing binding.

Text replacement requires text-only content. Use structural operations for mixed content. Comment and processing-instruction payloads follow XML line-ending normalization. Edited serialization uses UTF-8 and can change namespace placement, quote style, empty-element syntax and CDATA boundaries.

### Packages and saving

`Package.read_part` returns current edited XML bytes for a loaded part. `add_part`, `replace_part`, `remove_part` and `set_content_type` manage parts. `relationships`, `relationship_part`, `add_relationship` and `remove_relationship` manage links between parts. Use `/` for package relationships and an absolute part URI for part-scoped relationships. External relationship targets are retained as references and are never fetched.

Use package-specific APIs to change OPC metadata. Ordinary part APIs reject overwrites of metadata parts. Removing a part also removes its relationship file, matching content-type overrides and inbound internal relationships. All other payloads remain untouched. `Document.new()` creates a DOCX package with the normal main-document content type.

Loading, typing and validating XML leave its original bytes unchanged. An unchanged save returns the original XML or package bytes, including UTF-16 encoding and ZIP layout. Changed archives retain untouched compressed payloads. CRC and declared-size checks run when reading affected parts or writing a changed archive. An unchanged save passes through the archive without inflating its entries. Signed packages support unchanged saves and reject edits.

Path saves construct the output, write a temporary file beside the destination, sync it and atomically replace the destination.

## Resource and archive boundaries

| Input/operation | Explicit limits or refusals |
| --- | --- |
| General XML | 32 MiB input/decoded/serialized bytes; depth 256; 1 million allocated nodes including tombstones; 1 MiB attribute; 64 MiB aggregate namespace contexts |
| OPC control XML | 8 MiB raw/decoded bytes and bounded nesting, plus the generic XML checks |
| ZIP | 512 MiB archive; 10,000 entries; 256 MiB per payload; 1 GiB total uncompressed |
| Archive formats | Stored/Deflated only; no encryption, ZIP64, multidisk, prefixed executables, symlinks, overlapping entries or equivalent/unsafe part names |

XML input is limited to XML 1.0 in UTF-8 or UTF-16. DTDs and custom entities are refused. Macro-enabled and template packages are refused.

Malformed input and failed structural preconditions are rejected before mutation. Aggregate namespace-context and serialized-byte limits are checked during serialization. An accepted edit can therefore cause bytes/save to fail. The live tree remains available for correction.

Ordinary setters operate on the affected nodes. Namespace changes and structural operations can inspect affected descendants. The package writer constructs output in memory. Shared-document concurrent editing is unsupported. The limits above have not been established as sufficient by fuzzing or an adversarial security audit.

## Text/review scope

### Text ranges

`doc.story.find(text)` searches literal text across runs. `range.paragraph` is the paragraph element containing a range; a range spanning paragraphs or an opaque structure has none and raises. `doc.stories(view=...)` yields each reachable main, header, footer, individual note and comment story, including note separators. Each story carries its owning `part_uri`.

Stories use Python Unicode positions, `\n` between visible paragraph boundaries, `\t` for tabs, `\v` for line breaks and U+FFFC for unsupported structures. Bookmarks, comment anchors/references and annotation labels have zero width. Current and original views select insertion/deletion text and paragraph boundaries while leaving stored XML unchanged. `Story(element, view='original')` provides a read-only original view. Every edit to a story's XML tree invalidates its ranges.

Ordinary text beside revisions remains editable. Range editing protects revision payloads, property-history runs and hyperlinks. Fields in either form read alike: markers are zero width and the cached result is text, a field without one reads as U+FFFC, and comment, footnote and endnote marks are zero width too. A range may lie inside a result or contain a whole field, never cross its boundary, and an edit touching a simple field first rewrites it in the complex form, as Word does. A field that continues in a later paragraph keeps its tail opaque. Content controls, textboxes, moves and revised table containers are outside its supported scope. Replacement text takes the first affected run's formatting. A caret insertion uses an ordinary neighboring run's formatting.

Run isolation splits boundaries and retains references unless the caller explicitly requests their extraction. Text replacement retains enclosing anchors and collapses interior anchors to the replacement's end. Hyperlink insertion moves the selected XML slice together, preserving interior marker positions.

Multiline edits require consecutive sibling paragraphs in one container. Section breaks and table-container crossings are refused. Joins retain the last paragraph's properties and identity. Splits create a new left paragraph and remove its copied `paraId` and `textId` attributes.

### Comments

Comments support plain-text bodies, main-story anchors, replies and per-comment resolution. Replies link through the last comment paragraph's `paraId`. Existing `commentsIds` and `commentsExtensible` records receive matching durable-ID and UTC entries. Classic documents are not automatically upgraded to every modern metadata part. Unknown metadata is retained. Ambiguous or incomplete required linkage is refused. Mention and application-identity generation is unsupported.

`doc.comments[id]` looks up a comment by numeric ID, accepting padded lexical forms. `comment.range` returns its anchored current text. `.delete()` removes the comment's reply subtree, linked records and anchors. `.delete_thread()` starts at the thread root. Empty parts and unrelated package content remain. Matching anchors outside the main XML cause deletion to fail during preflight.

### Tracked changes

`doc.revisions` exposes inline insertions/deletions, paragraph-boundary changes and run/paragraph property histories, and lists them without checking whether they can be applied. Each revision supports `accept()` and `reject()`. Applying either operation invalidates the revision handle. Bulk `accept_all()` and `reject_all()` preflight the whole story before changing anything and refuse unsupported families. Use `Revisions(story)` for another explicit story.

Tracked text edits emit `w:ins`, `w:del` and `w:delText`. Paragraph-boundary marks go in `pPr/rPr`. `doc.revisions.format(run, e.rPr(e.b()), author='Drafter')` records a direct formatting change with `rPrChange` or `pPrChange` snapshots. Its `.previous` and `.current` expose the properties. Paragraph history retains independent paragraph-mark and section properties.

Author/date metadata is explicit. Dates default to UTC and supplied dates must be timezone-aware. New revision IDs avoid existing values in the containing XML tree. Table, move, conflict and nested histories, and final paragraph marks, remain unsupported.

## Document helpers

`doc.styles` finds and creates styles, then applies references while retaining direct formatting.

`doc.properties` reads and writes core properties by local name (`title`, `creator`, `lastModifiedBy`, `created`, ...), creating the core-properties part and its package relationship on first write without overwriting an occupied filename. Python converts datetimes to strings; Rust owns property names, namespaces and date-type markup. `doc.settings` maps flat values using native schema descriptors: on/off settings read as booleans (a bare element means `True`), while numeric and other settings retain their lexical strings. Setters validate before editing existing elements in place. Complex settings are excluded from the mapping and available through the live `doc.settings.root`. `doc.add_part(name)` creates a fresh declared part of that SDK kind, where `doc.part(name, create=True)` finds or creates the single one; `doc.package.relationship_id(source, target)` is the inverse of `relationship_part`.

Field, REF, detached hyperlink, drawing and footnote-reference factories return native-backed `Element` views. Calling a live parent with one copies its native subtree directly. Embedding one in an `E` expression takes the usual construction-time XML snapshot.

`doc.numbering.add([Level(), Level(format='lowerLetter')])` creates a multilevel list. Reuse the returned instance with `.apply(paragraph, level=...)` to continue it. `.restart(start=...)` creates a separate instance and leaves the original list unchanged.

`Table.add(body, [['Clause', 'Response']], widths=[4000, 4000])` creates a rectangular table. `Table(element)` exposes rows, cells and row/column edits. Use `Story(cell)` for cell text. Structural operations refuse merged, offset or revised grids. Removal refuses cells containing bookmarks or comments.

`doc.bookmarks.add(span, 'clause')` creates a bookmark with `.range` and `.remove()`. `doc.bookmarks.ref(name, text=None, switches='')` creates detached REF markup with a cached result, attachable with `paragraph(doc.bookmarks.ref('cap', '5.1', switches=r'\w \h'))`; the bookmark need not exist yet when `text` is given. `bookmark.ref(...)` is the same for an existing bookmark, defaulting `text` to its text. `field(instr, text, rpr=None)` builds any other simple field the same way, such as `SEQ` or `MERGEFIELD`, with a dirty flag so Word refreshes it. `bookmark_name(text)` turns any text into a Word-legal bookmark name. Bookmarks may enclose fields and other opaque content, since anchors have no width.

`doc.hyperlinks.add(span, '#clause')` links existing formatted text to a bookmark. A URL instead creates an external link. Assigning `.target` retargets a link to another URL or `#bookmark`, releasing an unused relationship. `.remove()` unwraps the text and removes unused relationships in the owning part. Hyperlink text remains visible in the story but protected from range edits while wrapped. `doc.hyperlinks.link(target, *children, part_uri=None)` builds a detached hyperlink for content under construction, allocating one relationship per URL and part, and `doc.hyperlinks.external_id(url)` returns just that id.

`part.add_image(data, width=None, height=None, description='', content_type=None)` stores the bytes as a media part beside the main document, relates it from `part`, and returns detached `w:drawing` markup for a run. PNG, JPEG and GIF headers supply the content type, pixel size and resolution; other formats need `content_type` and both extents. One extent keeps the aspect ratio. `wp:docPr` ids count up from the highest in the package's stories and stay unique for the package's lifetime, so detached drawings can be attached later in any order.

`doc.footnotes.add(span, content)` inserts a footnote reference after `span` and creates the note. `doc.footnotes.create(content)` creates the note alone, and `note.reference()` is the detached reference run to attach where the mark belongs. `content` is a string or block expressions. A note whose first block is not a paragraph gets an empty one first, every top-level paragraph without a style gets `FootnoteText`, and the first paragraph opens with Word's mark run and a space. The footnotes part is created on first use and always carries Word's two separator notes, and the `FootnoteText` and `FootnoteReference` styles are added when missing. `note.delete()` removes the note and every run referencing it. Separator notes are not listed by `doc.footnotes`, and `note.text` starts after the mark and its space.

Style inheritance, displayed list counters, field evaluation and layout are left to the application reading the document.

`doc.set_custom_xml(item_id, data, schema_uri=None)` writes XML bytes to a main-document custom XML datastore, matching its GUID case-insensitively, and returns its `Part`. A matching store retains its property part unchanged; `schema_uri` only supplies the schema reference for a new store. Creation allocates non-colliding item and property part names and links both parts. Unrelated stores remain untouched. This does not create content controls or interpret the payload.

## Import and compare

`import_content` copies selected paragraphs and tables with their explicit style, numbering, image and hyperlink dependencies:

```python
from oxml import import_content, w

blocks = list(source.main.xml.elements(w.Paragraph))[:2]
body = next(destination.main.xml.elements(w.Body))
import_content(source, blocks, destination, body)
```

Import remaps conflicting identifiers and preserves complete bookmark ranges. Destination definitions, themes and document defaults remain in effect. Unsupported review, field, section and dependency contexts are refused, including revised containing cells and rows. Inherited `mc:Ignorable` is carried automatically. Other inherited MC directives require explicit handling.

`compare` matches stored text and direct run/paragraph properties, then returns a new document containing tracked differences. It preserves both originals and leaves equal opaque blocks untouched. Original metadata and revision-session bookkeeping are retained.

Paragraphs align by text within each container, descending into paired tables whose properties, grids, rows and cell properties match. Each changed region is diffed by Unicode words with paragraph marks as tokens, so splits, joins, insertions and deletions come out of the same tracked replacement as ordinary edits. Inserted text is copied from the revised paragraph, fields included. A new paragraph takes its properties outright, and a retained one records a property history. Hyperlinks in changed paragraphs are rewritten as HYPERLINK fields first, so link targets compare like field instructions, and a changed instruction replaces the whole field. A bookmark copied with inserted text keeps its name and the older copy loses its markers. Changed table structure, sections, dependencies and unresolved revisions are refused. Move detection and comparison of rendered appearance are unsupported.

## Validation scope

Run `doc.validate()` after editing. It walks reachable relationships, checks declared package constraints and part roots, and validates each recognized XML part once with its dependency and relationship context. Issues and skipped regions identify their part URI. `scope.part_uris` lists the XML parts checked. Missing targets and malformed secondary XML are reported as errors. Unknown extension payloads remain opaque.

Reports are plain dicts whose repr leads with the issue count and tallies gaps by family. They mark validation as incomplete. Gaps include some regex syntax and list facets, versioned enum values, decimal precision rounding, legacy date/time fragments, particle filtering and semantic-rule families. Inspect the reported gaps alongside errors when deciding whether a document meets your requirements.

Standalone `Tree.validate(dependencies=...)` accepts a mapping from part names to live `Tree` objects. Malformed dependency XML fails at tree construction. `Document.validate()` reports malformed parts explicitly.

Attribute and element-text constraints share one interpreter. Integer comparisons are exact. Floating-point values use floating-point lexical and bound checks. Decimal handling follows SDK behavior. Required attributes respect availability, `IsInitialVersion` and `IsRequired=False`. `BooleanValue` validation and Python getters share native parsing, including surrounding XML whitespace. `OnOffValue` follows the SDK's exact-token rules. Expanded names distinguish qualified and unqualified attributes.

Typed setters refuse invalid or incompletely checked constraints. Raw lexical edits remain available for unsupported cases. Structural edits permit incomplete or schema-invalid content while a document is being assembled.

Compatibility directives select the effective validation view while preserving stored XML nodes and branches. `MustUnderstand` checks declared prefixes. Application-loading enforcement for unsupported namespaces is unimplemented.

Strict DOCX package discovery is supported. Strict XML namespaces remain available as raw XML and have no conversion to the Transitional typed vocabulary. The imported vocabulary includes shared and non-DOCX schemas. Validation and editing support are limited to the operations documented here. XLSX editing is unimplemented.

## SDK import

`scripts/import_sdk.py` reads pinned SDK JSON and C# primitive definitions from a reference checkout. It checks for unknown fields or mechanisms and unresolved child references before writing `schema/metadata.json`. The reference checkout stays unchanged. The importer runs without .NET.

The descriptor is compiled into the native extension and supplies both validation rules and Python type generation. SDK notices are retained in `python/oxml/SDK-LICENSE`.

```bash
python scripts/import_sdk.py
maturin develop
pytest -q
```

The default SDK revision is `431ab05cf160248cc3885a4a766026d4f8243792`. `--sdk`, `--revision` and `--output` override the source and destination. Regeneration requires the reference checkout. Ordinary builds use the checked-in descriptor.

The source policy targets parity with the pinned SDK. Independent standards conformance has not been audited.

## Tests

`test_xml.py`, `test_package.py` and `test_document.py` cover parser/editor safety, archive round trips and open-edit-validate-save workflows. `test_probe.py` and `test_holdout.py` cover difficult imported-schema cases and holdout regressions.

`cargo test` runs `tests/native.rs`, an ownership, tracked-edit and save/reopen workflow that uses the Rust API without Python initialization.

`test_sdk_attributes.py`, `test_sdk_particles.py`, `test_sdk_mc.py` and `test_sdk_semantics.py` adapt SDK inputs and expected behavior into self-contained Python tests. Source comments identify the upstream cases. They run without reference checkouts or .NET. Tests for unsupported cases must retain the SDK's expected outcome, with validation gaps reported separately.

`test_text.py`, `test_comments.py` and `test_revisions.py` exercise public APIs with real DOCX inputs, independent XML and endpoint expectations, and untouched-part checks.

`tests/fixtures/` contains unchanged original DOCX files and upstream notices. Package no-op tests cover every original recursively, including ZIPs with directory entries. Package part enumeration excludes those directory entries. Keep the source and license mappings in `tests/fixtures/README.md` current when borrowing fixtures.

`test_corpus_body.py`, `test_corpus_reviews.py` and `test_corpus_crosspart.py` independently edit expected minidom trees and compare expanded names, attributes, effective namespace bindings and ordered content. Untouched payloads must remain byte-identical. Derived documents stay in test memory or temporary output files. These tests check preservation of stored structures.

CI installs and tests the built Linux/macOS CPython 3.10-3.13 wheels before publishing. The test suite runs without launching Word. Current Word interoperability, a stable Python ABI and free-threading support remain unverified.

Keep historical feasibility notes in `meta/` and exclude them from Git.

## Versioning

The canonical version lives in `Cargo.toml`. `pyproject.toml` gets the Python package version from Cargo via `dynamic = ["version"]`.

## Build profiles

uv builds and `maturin develop --release` use the incremental `release` profile for fast local iteration. CI builds distributed wheels with `dist`, which enables full LTO and one codegen unit, disables incremental compilation, and strips the result.

## Release

1. Confirm the release version in `Cargo.toml` (`[package].version`).
2. Ensure all changes are committed and pushed and the working tree is clean.
3. Run `ship-release`.

Fastship pushes the version tag for GitHub Actions, then bumps and pushes `Cargo.toml`. CI builds and publishes the distributions and generates GitHub release notes.
