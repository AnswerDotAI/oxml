# Development

## Commands

```bash
maturin develop && pytest -q
ship-rs-build
```

Rebuild the extension after Rust or embedded-descriptor changes before Python tests. `cargo check` alone does not refresh the installed extension.
`schema/metadata.json` is included in uv's cache keys, so descriptor changes automatically invalidate cached builds; local testing still uses the explicit
`maturin develop` workflow. Do not independently rebuild the shared extension from parallel workers.

## Current foundation

* `src/xml.rs`: internal ordered mutable arena using quick-xml 0.42's decoding reader and events; no separate XML crate or backend abstraction.
  Generic XML well-formedness checks supplement event parsing. Stable node identities survive unrelated edits; deleted/replaced IDs are never reused.
  Ordinary edits preflight local/affected-subtree constraints, mutate live storage and invalidate cached serialization; bytes/save serialize on demand.
  Both `Tree` and package XML parts use this same editor. There is no general rollback framework or per-setter full-part clone.
* `src/package.rs`: ZIP/OPC read/create/edit/save, relationship-based main discovery, scoped content-type/relationship changes and untouched payload copying.
  OPC control XML uses the same XML tree and serializer as document parts; package-specific code checks content-type/relationship rules only.
* `src/schema.rs`: contextual dispatch and partial generic particle/lexical/version/semantic interpretation directly over the live XML tree.
  One immutable index derives contextual child lookups, semantic-rule dispatch and namespace availability from the shared descriptor.
  Content models compile once, including versioned occurrences; the bounded matcher uses contiguous position sets rather than per-position tree allocations.
  Validation resolves types through the effective compatibility view while preserving raw node identities and every stored branch.
  Dependencies are borrowed live XML trees, not serialized/reparsed copies; reference/uniqueness checks share compatibility-aware traversal.
  Reference-value sets and duplicate tracking are local to each validation call, so edits and different targets need no cache invalidation.
* `python/oxml/model.py`: SDK-derived nominal classes/enums and typed properties; contextual lookup follows the requested node's ancestors through indexed declarations.
  A view's successful type check is reused only while the native XML revision is unchanged. Every edit requires a fresh check before that view's next access, with node-level
  immutable raw snapshots produced on demand rather than rebuilding whole-part snapshots after every mutation. Scalar QName/attribute reads use native getters,
  avoiding serialization of a container's child list just to inspect its name or attributes. Calling a live element attaches one detached expression and returns
  its new typed child. Cached schema-order slots distinguish ordered sequences from unordered/repeatable content groups; automatic insertion preserves existing order.
* `python/oxml/build.py`: SDK namespace lookup and parsed-element snapshots for fastcore's namespace-aware XML builder. `E(prefix='', attr_ns=None, ns=None)` configures a factory; `e = E('w', attr_ns='w')` is the WordprocessingML preset. `e.tag(*children, **attrs)` returns a detached `XML` expression. Children accept expressions, parsed `Element` views, strings, ordered collections and omitted `None` values. Parsed children become namespace-complete snapshots at construction, unaffected by later source edits or invalidation. IDs remain unchanged and package dependencies are not copied. Caller-provided raw XML bytes are not accepted as children.
  Factories resolve names at construction and each expression keeps its own bindings. `ns` supplies custom/default and value-only prefix bindings. Keyword attributes use `attr_ns`; `prefix__name` selects an explicit prefix and `attrs_` accepts literal names. Python keywords use a trailing underscore, including `e.del_()`. Attribute values are lexical, not typed setters; booleans serialize as `true`/`false` and `None` is omitted. Name, namespace and character checks run at construction. `bytes()` serializes without formatting whitespace; `parent(expression)` runs the native XML/resource checks and attaches the subtree once. The generic fastcore builder remains schema-free.
* `python/oxml/document.py`: `Document`, `Package` and `Part`; one cached mutable tree per canonical part name, automatic flush at save and invalidation
  after part replacement/removal. Equivalent case spellings resolve to the same state. A per-call relationship walk serves separate story enumeration and
  reachable-part validation, resolving already-read targets without reparsing relationships per ID. Imported part declarations drive package constraints/root checks.
  `_part` shares declared-part discovery/creation among conveniences.
* `python/oxml/text.py`: a read-only story projection and revision-checked ranges over live elements. Run-boundary splitting and replacement-run construction
  are shared by text replacement, comments, links and revisions; there is no second mutable document model or persistent position tracker.
* `python/oxml/paragraphs.py`: ordinary split/join and multiline replacement within one immediate container. Tracked paragraph edits reuse the same helpers.
* `python/oxml/comments.py`: classic anchors/bodies and supported modern reply/resolution/deletion metadata, with relationship-discovered parts and scoped IDs.
* `python/oxml/revisions.py`, `formatting.py`: text, paragraph-boundary and explicit property history operations. Bulk operations preflight unsupported families;
  author/date/ID generation and revision dispatch are shared rather than implemented separately per operation.
* `python/oxml/styles.py`, `numbering.py`: explicit definitions/references, shared schema-order insertion and numbered-list instances, not a style/counter renderer.
* `python/oxml/tables.py`, `links.py`: bounded rectangular-table edits and bookmark/hyperlink lifecycle using existing elements, ranges and scoped relationships.
* `python/oxml/importing.py`: per-import dependency/collision maps and detached selected-subtree staging; no persistent registry or whole-package merge engine.
* `python/oxml/compare.py`: standard-library text differencing plus existing revision/property operations; one original-document clone becomes the result.
* `tests/test_xml.py`, `test_package.py`, `test_document.py`: parser/editor safety, real archive round trips and integrated open–edit–validate–save.
  `test_probe.py` and `test_holdout.py` retain the difficult imported schema and previously unexamined-feature regressions.

### Editing and preservation contracts

`Tree.xml` is the raw native editor. `root`, `document_children()`, `children(id)` and `parent(id)` expose ordered identities; `node(id)` returns a JSON snapshot
whose `children` contains all child node IDs, including text/comments/PIs. `Element.children` returns only element views. `set_text`, expanded-name
`set_attribute`/`remove_attribute`, `rename`, `declare_namespace`, insertion, `delete`, `replace_node`,
`copy` and `move_node` operate on that state. Element-level Python structural methods use child-content indexes, counting text/comments/PIs as well as elements.
Copies allocate new IDs. Deletion/replacement invalidates subtree IDs; moving preserves them. A contextual typed view rejects access after its type changes.
`parent(expression)` places a new child after others in its schema slot and before later slots. It refuses unknown/wildcard/ambiguous placement and existing
unknown or out-of-order children before mutation; it never repairs order or enforces full content/cardinality/version validity. `parent(expression, index=n)`
bypasses automatic placement and uses the exact raw child-node index, including text/comments/PIs. This is also the route for untyped XML and deliberate invalid fixtures.
`Element.qname` returns the expanded `(namespace_uri, local_name)` pair; it and `Element.attribute` read scalar native values rather than full node snapshots.
`copy_to(parent, index=None)` can import XML across trees through namespace-complete subtree export, including an explicit empty default namespace when needed.
It does not transfer package dependencies or remap relationship/document-wide IDs. Cross-tree movement remains unsupported.

All in-scope namespace bindings are retained, including bindings used only in opaque attribute values. Ordinary name edits cannot silently rebind an existing
prefix; use a fresh prefix or explicit namespace operation. Renaming to an unqualified element necessarily clears its default namespace. Descendant contexts
are retained. Text replacement requires text-only content; structural APIs handle mixed content explicitly. Comments/PI payloads follow XML line-ending
normalization. DTDs/custom entities, XML 1.1 and encodings other than UTF-8/UTF-16 are refused. Edited serialization uses UTF-8 and may change namespace placement,
quote/empty-element style and CDATA boundaries. There is no byte-patching engine or byte-identical edited-subtree promise.

`Package.read_part` returns current edited XML bytes when loaded. `add_part`, `replace_part`, `remove_part`, `set_content_type`, `relationships`,
`relationship_part`, `add_relationship` and `remove_relationship` provide byte-oriented package operations. Use `/` for package relationships and an absolute
part URI for part-scoped relationships. Relationship resolution never fetches external targets. Metadata parts cannot be overwritten through ordinary part APIs.
Removing a part removes its own relationship file, matching content-type overrides and inbound internal relationships, not other payloads. No garbage collection
or normalization runs on untouched parts. New documents use the normal DOCX main content type; DOCM/templates are not edited under a DOCX label.

Unchanged XML and package bytes are returned exactly, including original UTF-16 and ZIP layout. Merely loading/typing/validating XML does not dirty it.
Edited archives copy unchanged compressed payloads, checking their CRC/declared sizes before output. Untouched no-op archives are not eagerly inflated solely
for CRC checking. Corruption is caught when an affected part is read or when saving a changed package. Signed archives are read-only except unchanged saves.
Path saves construct output before writing a temporary file beside the destination, then sync and atomically replace it.

### Resource and archive boundaries

| Input/operation | Explicit limits or refusals |
| --- | --- |
| General XML | 32 MiB input/decoded/serialized bytes; depth 256; 1 million allocated nodes including tombstones; 1 MiB attribute; 64 MiB aggregate namespace contexts |
| OPC control XML | 8 MiB raw/decoded bytes and bounded nesting, plus the generic XML checks |
| ZIP | 512 MiB archive; 10,000 entries; 256 MiB per payload; 1 GiB total uncompressed |
| Archive formats | Stored/Deflated only; no encryption, ZIP64, multidisk, prefixed executables, symlinks, overlapping entries or equivalent/unsafe part names |

Malformed input and structural preconditions are rejected before mutation. Aggregate namespace-context and serialized-byte limits are checked when output
is serialized, so an accepted edit can cause bytes/save to fail; the live tree remains available for correction. Namespace additions and structural operations
may inspect affected descendants, but ordinary setters do not traverse/copy/serialize the entire part. These are bounded foundation safeguards, not a fuzzing
or adversarial security certification. Shared-document concurrent editing, constant-time mutations and a streaming package writer are not promised.

### Text/review scope

`Document.story`, `.comments`, `.revisions`, `.styles`, `.numbering`, `.bookmarks` and `.hyperlinks` are live entry points, not alternate document models.
`doc.stories(view=...)` yields each reachable main/header/footer/individual note/comment story, including note separators, with an owning `part_uri`.
Stories use Python Unicode positions, `\n` between visible paragraph boundaries, `\t` for tabs, `\v` for line breaks, and U+FFFC for unsupported structures.
Current/original views select insertion/deletion text and paragraph boundaries without changing stored XML; original ranges are read-only.
Bookmarks, comment anchors/references and annotation labels contribute no text. Every XML mutation invalidates prior ranges; consuming a revision invalidates its handle.

Ordinary neighboring text can be edited while other revisions remain in the paragraph. Revision payloads/property-history runs and hyperlink wrappers are protected;
fields, content controls, textboxes, moves and revised table containers remain outside range editing. Replacement formatting comes from the first affected run, with an ordinary neighboring
run used for carets. Run isolation only splits boundaries unless a destructive caller explicitly requests reference extraction. Text replacement retains enclosing
anchors and collapses interior anchors to replacement end; adding hyperlinks instead moves the entire selected XML slice, preserving interior marker positions.
Multiline edits require consecutive sibling paragraphs in one container, no section breaks or table-container crossings. Joins keep the last paragraph's properties
and identity; splits create a new left paragraph without duplicating paragraph IDs. No generic rollback/transaction layer is introduced.

Comments support plain-text bodies, main-story anchors, reply links through the last comment paragraph's `paraId`, and per-comment resolution. Existing
commentsIds/commentsExtensible records receive corresponding durable-ID/UTC entries; classic documents are not automatically upgraded to every modern part.
Unknown metadata is retained and ambiguous/incomplete required linkage is refused. `.range` locates anchored current text; `.delete()` removes the comment/reply
subtree and linked records, `.delete_thread()` starts at the thread root. Empty parts remain. Matching anchors outside the main XML cause a preflight refusal.
Numeric comment IDs are compared numerically, including padded lexical forms. Mentions/application identities are not inferred.

Tracked edits emit actual `w:ins`/`w:del` and `w:delText`, with paragraph-boundary marks in `pPr/rPr`. Direct formatting records use `rPrChange`/`pPrChange` snapshots;
paragraph history retains independent paragraph-mark/section properties. Author/date metadata is explicit; dates default to UTC and supplied dates must be aware.
IDs avoid existing values in the containing XML tree. Table/move/conflict/nested histories and final paragraph marks remain unsupported.

Import remaps explicit dependencies, complete bookmark ranges and known drawing/paragraph/list IDs without overwriting destination definitions. Destination theme
and document defaults remain authoritative. Unsupported review, field, section and dependency contexts are refused, including revised containing cells/rows.
Only inherited `mc:Ignorable` is carried automatically; other inherited MC directives require explicit handling. Comparison uses existing edit operations, retains
original metadata and rejects unsupported dependency/content differences. Its paragraph-count path requires uniform direct properties; it is not Word comparison parity.

### Validation scope

Validation reports completeness false; this is not a full SDK validator port. Remaining gaps include unsupported regex syntax/list facets, versioned enum
values, decimal precision rounding, legacy date/time fragments, particle filtering and unsupported semantic-rule families. `Document.validate()` walks reachable
relationships, checks declared package constraints and part roots, then validates each recognized XML part once with actual dependency/relationship context.
Issues and skipped regions identify their part URI; `scope.part_uris` lists validated XML. Missing targets and malformed secondary XML are errors as well as
incomplete checking. Unknown extension payloads remain opaque. Standalone `Tree.validate(dependencies=...)` accepts part-name → live `Tree` mappings.
Malformed dependency XML fails when constructing its tree; `Document.validate()` reports malformed parts explicitly. Structural editing does not enforce
schema validity; validate after edits. Strict namespaces remain raw/unknown rather than being silently converted.

Integers retain exact comparisons; floating-point values use floating-point lexical/bound checks, and decimal handling follows the SDK's validation behavior.
Attribute and element-text constraints share one interpreter. Required attributes respect availability, `IsInitialVersion` and `IsRequired=False`.
BooleanValue validation and Python getters share native parsing, including surrounding XML whitespace; OnOffValue retains the SDK's exact-token rules.
Compatibility directives control the effective validation view, not stored XML. Like SDK standalone validation, `MustUnderstand` checks declared prefixes;
unsupported-namespace enforcement during application loading is not claimed. Exact expanded names distinguish qualified from unqualified attributes.

Local tests do not establish current Word interoperability, stable ABI or free-threading support. CI installs and tests the built Linux/macOS CPython 3.10–3.13
wheels before publishing. There is no separate, duplicative editable-install CI test job.

## SDK import

`scripts/import_sdk.py` reads the pinned SDK JSON and C# primitive table without modifying reference checkouts or requiring .NET. It rejects unknown
fields/mechanisms and unresolved child references before writing `schema/metadata.json`, which is compiled into the native extension.
Native validation and Python facade generation consume this one descriptor. SDK notices are retained in `python/oxml/SDK-LICENSE`.

```bash
python scripts/import_sdk.py
maturin develop
pytest -q
```

The default SDK revision is `431ab05cf160248cc3885a4a766026d4f8243792`; `--sdk`, `--revision` and `--output` override the source and destination.
Only regeneration requires the reference clone. Ordinary builds use the checked-in descriptor. The imported vocabulary includes shared/non-DOCX schemas,
not an XLSX support declaration. The source target remains SDK parity, without a second schema-source or historical-audit pipeline.

## Tests

`test_sdk_attributes.py`, `test_sdk_particles.py`, `test_sdk_mc.py` and `test_sdk_semantics.py` adapt upstream SDK inputs and expected behavior into
self-contained Python tests. Source comments identify the original tests; running these cases requires neither reference clones nor .NET.
The initially expected failures now pass. Future unsupported cases must retain their actual expected behavior rather than asserting that reporting a gap is validation.
`test_text.py`, `test_comments.py` and `test_revisions.py` exercise the public APIs using real DOCX inputs, independent XML/endpoint expectations and untouched-part
checks. These are structural/preservation tests; they do not launch Word or claim application interoperability.

`tests/fixtures/` contains unchanged original DOCX files and upstream notices. Generic package no-op tests cover every original recursively, including ZIPs
with directory entries, which are not parts. `tests/fixtures/README.md` maps files to their upstream sources and shared notices; keep it current when borrowing fixtures.
The 16 tests in `test_corpus_body.py`, `test_corpus_reviews.py` and `test_corpus_crosspart.py` independently mutate expected minidom trees and compare expanded
names, attributes, effective namespace bindings and ordered content; every untouched payload must remain byte-identical. Derivatives live only in test memory
and temporary output files. These tests establish preservation of stored review structures, not review operations, schema validity, rendering or Word behavior.
Historical feasibility notes stay in `meta/`, which must never be added to Git.

## Versioning

The canonical version lives in `Cargo.toml`. `pyproject.toml` gets the Python package version from Cargo via `dynamic = ["version"]`.

## Build profiles

uv builds and `maturin develop --release` use the incremental `release` profile for fast local iteration. CI builds distributed wheels with `dist`, which enables full LTO and one codegen unit, disables incremental compilation, and strips the result.

## Release

1. Confirm the release version in `Cargo.toml` (`[package].version`).
2. Ensure all changes are committed and pushed and the working tree is clean.
3. Run `ship-release`.

Fastship pushes the version tag for GitHub Actions, then bumps and pushes `Cargo.toml`. CI builds and publishes the distributions and generates GitHub release notes; there is no local changelog step.
