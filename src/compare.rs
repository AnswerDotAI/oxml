//! Conservative body comparison using the same native revision operations as explicit edits.
use crate::{error::{Error, Result}, package::Package, revisions::{self, Metadata}, text::{self, View, W, W14, XML},
    xml::{Attribute, Document, NodeKind}};
use pyo3::prelude::*;
use similar::{capture_diff_slices, Algorithm, DiffOp, DiffTag, TextDiff};
use std::{collections::{BTreeMap, HashMap}, ops::Range};
const R: &str = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";

fn unsupported(message: &str) -> Error { Error::Unsupported(message.into()) }
type Attributes = Vec<(String, String, String)>;
#[derive(PartialEq, Eq, Hash)]
enum Key {
    Element((String, String), Attributes, Vec<(String, String)>, Vec<Key>),
    Text(String), Comment(String), Pi(String, String),
}
fn attrs(doc: &Document, id: usize, bookkeeping: bool, links: Option<&HashMap<String, String>>) -> Result<Attributes> {
    let e = doc.node(id)?.element().ok_or_else(|| Error::Invalid("Expected an XML element".into()))?;
    // A hyperlink's relationship id stands for its target, so links compare by where they point.
    let target = |a: &Attribute| links.filter(|_| e.name.uri == W && e.name.local == "hyperlink" && a.name.uri == R && a.name.local == "id").and_then(|l| l.get(&a.value));
    let mut result = e.attributes.iter().filter(|a| !bookkeeping || !(a.name.uri == W && a.name.local.starts_with("rsid") ||
        a.name.uri == W14 && matches!(a.name.local.as_str(), "paraId" | "textId")))
        .map(|a| (a.name.uri.clone(), a.name.local.clone(), target(a).unwrap_or(&a.value).clone())).collect::<Vec<_>>();
    result.sort();
    Ok(result)
}
fn key(doc: &Document, id: usize, omit: &[&str], bookkeeping: bool, inherited: Option<bool>, links: Option<&HashMap<String, String>>) -> Result<Key> {
    let node = doc.node(id)?;
    let Some(e) = node.element() else { return Ok(match &node.kind {
        NodeKind::Text(s) => Key::Text(s.clone()), NodeKind::Comment(s) => Key::Comment(s.clone()),
        NodeKind::Pi { target, value } => Key::Pi(target.clone(), value.clone()), _ => unreachable!(),
    }); };
    let mut preserve = inherited;
    let mut ancestor = Some(id);
    while let Some(id) = ancestor {
        if let Some(space) = doc.node(id)?.element().and_then(|e| e.attribute(XML, "space")) { preserve = Some(space == "preserve"); break; }
        if preserve.is_some() { break; }
        ancestor = doc.node(id)?.parent;
    }
    let has_elements = node.children.iter().any(|&id| doc.nodes[id].as_ref().unwrap().element().is_some());
    let mut children = Vec::new();
    for &id in &node.children {
        let child = doc.node(id)?;
        if child.element().is_some() {
            if text::name(doc, id).is_some_and(|name| omit.contains(&name)) { continue; }
        } else if matches!(&child.kind, NodeKind::Text(s) if s.trim().is_empty()) && has_elements && preserve != Some(true) && e.name.uri == W { continue; }
        children.push(key(doc, id, &[], bookkeeping, Some(preserve.unwrap_or(false)), links)?);
    }
    let mut ns = e.namespaces.iter().filter(|(_, uri)| !uri.is_empty()).cloned().collect::<Vec<_>>();
    ns.sort();
    Ok(Key::Element((e.name.uri.clone(), e.name.local.clone()), attrs(doc, id, bookkeeping, links)?, ns, children))
}
#[derive(Default, PartialEq, Eq)]
struct Properties { attrs: Attributes, children: Vec<Key>, namespaces: Vec<(String, String)> }
fn props(doc: &Document, id: usize, name: &str, omit: &[&str]) -> Result<Properties> {
    let Some(id) = text::child(doc, id, name) else { return Ok(Properties::default()); };
    let Key::Element(_, attrs, namespaces, children) = key(doc, id, omit, false, None, None)? else { unreachable!() };
    Ok(Properties { namespaces: if attrs.is_empty() { Vec::new() } else { namespaces }, attrs, children })
}
#[derive(PartialEq, Eq)]
enum Payload { Xml(Key), Bytes(Vec<u8>) }
type Relationships = Vec<(String, String, Option<String>, String)>;
#[derive(PartialEq, Eq)]
struct Dependencies { parts: BTreeMap<String, (String, Payload)>, relationships: BTreeMap<String, Relationships> }
fn dependencies(package: &Package) -> Result<Dependencies> {
    let mut package = package.lock()?;
    let mut result = Dependencies { parts: BTreeMap::new(), relationships: BTreeMap::new() };
    let mut names = vec!["/".to_owned()];
    names.extend(package.part_names().into_iter().filter(|name| name.to_lowercase() != "/[content_types].xml" && !name.to_lowercase().ends_with(".rels")));
    for uri in names {
        if uri != "/" {
            let content_type = package.content_type(&uri)?;
            if matches!(content_type.as_str(), "application/vnd.openxmlformats-package.core-properties+xml" |
                "application/vnd.openxmlformats-officedocument.extended-properties+xml") { continue; }
            if uri != package.main_part() {
                let payload = if content_type.ends_with("xml") {
                    let tree = package.load_xml(&uri)?;
                    let doc = tree.read()?;
                    Payload::Xml(key(&doc, doc.root, if text::name(&doc, doc.root) == Some("settings") { &["rsids"] } else { &[] }, false, None, None)?)
                } else { Payload::Bytes(package.data(&uri)?) };
                result.parts.insert(uri.clone(), (content_type, payload));
            }
        }
        let mut relationships = Vec::new();
        for rel in package.relationships(&uri)? {
            if rel.kind.ends_with("/hyperlink") { continue; }
            if uri == "/" && matches!(rel.kind.as_str(), "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" |
                "http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties") { continue; }
            let target = if rel.mode == "External" { Some(rel.target) } else { Some(package.relationship_target(&uri, &rel.target)?) };
            relationships.push((if uri == "/" { String::new() } else { rel.id }, rel.kind, target, rel.mode));
        }
        relationships.sort();
        result.relationships.insert(uri, relationships);
    }
    Ok(result)
}
struct Opcode { equal: bool, old: Range<usize>, new: Range<usize> }
/// Map Unicode word-diff ranges to the character positions used by native text operations.
fn opcodes(before: &str, after: &str) -> Vec<Opcode> {
    let diff = TextDiff::configure().algorithm(Algorithm::Myers).diff_unicode_words(before, after);
    let (mut a, mut c) = (0, 0);
    diff.ops().iter().map(|op| {
        let b = a + op.old_range().map(|i| diff.old_slice(i).unwrap().chars().count()).sum::<usize>();
        let d = c + op.new_range().map(|i| diff.new_slice(i).unwrap().chars().count()).sum::<usize>();
        let result = Opcode { equal: op.tag() == DiffTag::Equal, old: a..b, new: c..d };
        (a, c) = (b, d);
        result
    }).collect()
}
fn properties_document(doc: &Document, parent: usize, name: &str, omit: &[&str]) -> Result<Document> {
    let Some(id) = text::child(doc, parent, name) else { return Ok(Document::from_element(text::word_element(name))); };
    let children = doc.node(id)?.children.iter().copied().filter(|&id| !text::name(doc, id).is_some_and(|name| omit.contains(&name))).collect::<Vec<_>>();
    text::shell(doc, Some(id), &children)
}
/// Set a new paragraph's base properties outright, keeping its mark and section properties.
fn replace_properties(doc: &mut Document, paragraph: usize, properties: &Document) -> Result<()> {
    let old = text::unique_child(doc, paragraph, "pPr")?;
    let index = old.map(|id| doc.position(id).map(|(_, i)| i)).transpose()?.unwrap_or(0);
    let current = doc.import_at(properties, properties.root, paragraph, index)?;
    if let Some(old) = old {
        for child in doc.node(old)?.children.clone() {
            if matches!(text::name(doc, child), Some("rPr" | "sectPr")) { doc.move_node(child, current, doc.node(current)?.children.len())?; }
        }
        doc.remove(old)?;
    }
    Ok(())
}
fn neighbour(doc: &Document, id: usize, forward: bool) -> Result<Option<usize>> {
    let parent = doc.node(id)?.parent.ok_or_else(|| Error::Invalid("Expected a contained block".into()))?;
    let siblings = doc.element_children(parent)?.collect::<Vec<_>>();
    let index = siblings.iter().position(|&s| s == id).unwrap();
    Ok(if forward { siblings.get(index + 1).copied() } else { index.checked_sub(1).map(|i| siblings[i]) })
}
/// Paragraph texts as the story holds them: a paragraph followed by another ends in the `\n` that separates them, so
/// the diff sees paragraph marks as tokens and the tracked replacement splits and joins accordingly.
fn tokens(doc: &Document, paragraphs: &[usize]) -> Result<String> {
    let mut result = String::new();
    for &id in paragraphs {
        result.push_str(&text::paragraph(doc, id, View::Current, false)?.text);
        if neighbour(doc, id, true)?.is_some_and(|next| text::name(doc, next) == Some("p")) { result.push('\n'); }
    }
    Ok(result)
}
#[derive(PartialEq, Eq, Hash)]
enum Block { Paragraph(String), Other(Key) }
/// Paragraphs align by text, so formatting differences are found after pairing; other blocks align structurally.
fn block(doc: &Document, id: usize, links: &HashMap<String, String>) -> Result<Block> {
    Ok(if text::name(doc, id) == Some("p") { Block::Paragraph(text::paragraph(doc, id, View::Current, false)?.text) } else { Block::Other(key(doc, id, &[], true, None, Some(links))?) })
}
/// Aligned paragraph runs of both documents, with the story position where the old run starts.
struct Region { old: Vec<usize>, new: Vec<usize>, position: usize }
/// The revised document, and each side's hyperlink targets by relationship id.
struct Cx<'a> { revised: &'a Document, links: [HashMap<String, String>; 2] }
/// Rewrite every hyperlink under `root` as a HYPERLINK field, so links compare and track like fields.
fn expand_links(doc: &mut Document, root: usize, links: &HashMap<String, String>) -> Result<()> {
    let hyperlinks = doc.descendants(root)?.filter(|&id| text::name(doc, id) == Some("hyperlink")).collect::<Vec<_>>();
    for id in hyperlinks {
        let url = match doc.node(id)?.element().unwrap().attribute(R, "id") {
            Some(rid) => Some(links.get(rid).cloned().ok_or_else(|| Error::Invalid("Hyperlink relationship is missing".into()))?),
            None => None,
        };
        text::expand_link(doc, id, url.as_deref())?;
    }
    Ok(())
}
impl Cx<'_> {
    /// A copy of a revised paragraph with its hyperlinks as fields, ready to be cut and copied into the result.
    fn scratch(&self, paragraph: usize) -> Result<Document> {
        let mut copy = self.revised.subtree(paragraph)?;
        let root = copy.root;
        expand_links(&mut copy, root, &self.links[1])?;
        Ok(copy)
    }
    /// The region paragraph holding new-text offsets `start..end`, with the offsets made local to it.
    fn locate(&self, region: &Region, start: usize, end: usize) -> Result<(usize, usize, usize)> {
        let mut base = 0;
        for (index, &paragraph) in region.new.iter().enumerate() {
            let len = text::paragraph(self.revised, paragraph, View::Current, false)?.len;
            if start >= base && end <= base + len { return Ok((index, start - base, end - base)); }
            base += len + 1;
        }
        Err(Error::Invalid("Inserted text is outside the revised region".into()))
    }
}
fn attribute(doc: &Document, id: usize, local: &str) -> String {
    doc.node(id).ok().and_then(|n| n.element()?.attribute(W, local).map(str::to_string)).unwrap_or_default()
}
/// The content of a scratch paragraph's range, cut at its offsets, as standalone nodes ready to insert, with the bookmarks it wholly encloses.
fn cut(source: &mut Document, start: usize, end: usize, whole: bool) -> Result<Vec<Document>> {
    let projected = text::paragraph(source, source.root, View::Current, false)?;
    let selection = text::preflight(source, source.root, &projected, start, end, false, whole)?;
    let isolated = text::isolate(source, selection, false)?;
    let children = &source.node(source.root)?.children;
    let (mut index, mut stop) = (isolated.index, isolated.stop);
    while index > 0 && text::name(source, children[index - 1]) == Some("bookmarkStart") { index -= 1; }
    while stop < children.len() && text::name(source, children[stop]) == Some("bookmarkEnd") { stop += 1; }
    // A bookmark lying wholly within the range travels with the copy; one straddling it stays with the old text.
    let nodes = &children[index..stop];
    let ids = |kind| nodes.iter().filter(|&&id| text::name(source, id) == Some(kind)).map(|&id| attribute(source, id, "id")).collect::<Vec<_>>();
    let (starts, ends) = (ids("bookmarkStart"), ids("bookmarkEnd"));
    nodes.iter().filter(|&&id| match text::name(source, id) {
        Some("bookmarkStart") => ends.contains(&attribute(source, id, "id")),
        Some("bookmarkEnd") => starts.contains(&attribute(source, id, "id")),
        _ => true,
    }).map(|&id| source.subtree(id)).collect()
}
fn region_diff(doc: &mut Document, scope: usize, region: &Region, cx: &Cx, metadata: &mut Metadata) -> Result<()> {
    for &id in &region.old { expand_links(doc, id, &cx.links[0])?; }
    for (source, ids) in [(&*doc, &region.old), (cx.revised, &region.new)] {
        for &id in ids {
            if text::paragraph(source, id, View::Current, false)?.barriers.iter().any(|b| !b.revision && b.start < b.end) {
                return Err(unsupported("Changed paragraphs with drawings or other opaque content are unsupported"));
            }
        }
    }
    let mut scratches = region.new.iter().map(|&id| cx.scratch(id)).collect::<Result<Vec<_>>>()?;
    let (before, after) = (tokens(doc, &region.old)?, tokens(cx.revised, &region.new)?);
    let chars = after.chars().collect::<Vec<_>>();
    for op in opcodes(&before, &after).iter().rev().filter(|op| !op.equal) {
        let replacement = chars[op.new.clone()].iter().collect::<String>();
        let (mut chunks, mut start) = (Vec::new(), op.new.start);
        for piece in replacement.split('\n') {
            let len = piece.chars().count();
            chunks.push(if len == 0 { Vec::new() } else {
                let (index, local_start, local_end) = cx.locate(region, start, start + len)?;
                cut(&mut scratches[index], local_start, local_end, false)?
            });
            start += len + 1;
        }
        let (parts, multiline) = text::prepare_parts(doc, scope, region.position + op.old.start, region.position + op.old.end, replacement.contains('\n'))?;
        revisions::apply_replacement(doc, scope, parts, chunks, multiline, metadata)?;
    }
    pair_properties(doc, scope, region, &mut scratches, chars.len(), after.ends_with('\n'), metadata)
}
/// Result paragraphs of a story range as `(position, paragraph)`, grouped: paragraphs joined by a deleted mark share one revised paragraph.
fn groups(doc: &Document, scope: usize, start: usize, end: usize, trailing: bool) -> Result<Vec<Vec<(usize, usize)>>> {
    let (mut result, mut group_end): (Vec<Vec<(usize, usize)>>, Option<usize>) = (Vec::new(), None);
    for row in text::paragraphs(doc, scope, View::Current)? {
        let Some(paragraph) = row.paragraph else { continue; };
        if row.position < start || row.position + row.projection.len > end || trailing && row.position == end { continue; }
        match result.last_mut() {
            Some(group) if group_end == Some(row.position) => group.push((row.position, paragraph)),
            _ => result.push(vec![(row.position, paragraph)]),
        }
        group_end = Some(row.position + row.projection.len);
    }
    Ok(result)
}
/// A retained paragraph records a property history; an inserted one takes the revised properties outright, as Word writes them.
fn paragraph_properties(doc: &mut Document, scope: usize, last: usize, revised: &Document, metadata: &mut Metadata) -> Result<()> {
    let right = revised.root;
    let inserted = text::marks(doc, last).iter().any(|&mark| text::name(doc, mark) == Some("ins"));
    if !inserted {
        if attrs(doc, last, true, None)? != attrs(revised, right, true, None)? { return Err(unsupported("Changed paragraph attributes are unsupported")); }
        for name in ["rPr", "sectPr"] {
            let old = text::child(doc, last, "pPr").and_then(|id| text::child(doc, id, name));
            let new = text::child(revised, right, "pPr").and_then(|id| text::child(revised, id, name));
            if old.map(|id| key(doc, id, &[], false, None, None)).transpose()? != new.map(|id| key(revised, id, &[], false, None, None)).transpose()? {
                return Err(unsupported("Paragraph-mark and section property differences are unsupported"));
            }
        }
    }
    let omit = ["rPr", "sectPr"];
    if props(doc, last, "pPr", &omit)? != props(revised, right, "pPr", &omit)? {
        let properties = properties_document(revised, right, "pPr", &omit)?;
        if inserted { replace_properties(doc, last, &properties)?; } else { revisions::format(doc, scope, last, &properties, None, metadata)?; }
    }
    Ok(())
}
/// Pair the group's fields with the revised paragraph's by position; a changed instruction replaces the whole field.
fn field_instructions(doc: &mut Document, scope: usize, group: &[(usize, usize)], revised: &mut Document, metadata: &mut Metadata) -> Result<()> {
    let mut fields = Vec::new();
    for &(position, paragraph) in group {
        let offset = position - group[0].0;
        fields.extend(text::paragraph(doc, paragraph, View::Current, false)?.fields.into_iter().map(|f| (f, paragraph, offset)));
    }
    let expected = text::paragraph(revised, revised.root, View::Current, false)?.fields;
    if fields.len() != expected.len() || fields.iter().zip(&expected).any(|((f, _, offset), e)| (f.start + offset, f.end + offset) != (e.start, e.end)) {
        return Err(unsupported("Changed fields are unsupported"));
    }
    let mut replaced: Vec<(usize, usize)> = Vec::new();
    for ((field, paragraph, _), new) in fields.iter().zip(&expected) {
        if field.instruction == new.instruction || replaced.iter().any(|&(s, e)| s <= new.start && new.end <= e) { continue; }
        let projected = text::paragraph(doc, *paragraph, View::Current, false)?;
        let part = text::preflight(doc, *paragraph, &projected, field.start, field.end, false, true)?;
        let chunk = cut(revised, new.start, new.end, true)?;
        revisions::apply_replacement(doc, scope, vec![part], vec![chunk], false, metadata)?;
        replaced.push((new.start, new.end));
    }
    Ok(())
}
/// Retained runs whose properties differ from the revised run at the same offsets, as `(paragraph, start, end, properties)`.
fn run_formatting(doc: &Document, group: &[(usize, usize)], revised: &Document) -> Result<Vec<(usize, usize, usize, Document)>> {
    let new_runs = text::paragraph(revised, revised.root, View::Current, false)?.spans.into_iter()
        .filter_map(|s| s.element.filter(|&id| text::name(revised, id) == Some("r") && s.start < s.end).map(|id| (s.start, s.end, id))).collect::<Vec<_>>();
    let mut result = Vec::new();
    for &(position, paragraph) in group {
        let offset = position - group[0].0;
        for span in text::paragraph(doc, paragraph, View::Current, false)?.spans {
            let Some(run) = span.element.filter(|&id| text::name(doc, id) == Some("r") && !text::history(doc, id) && span.start < span.end) else { continue; };
            for &(start, end, new) in &new_runs {
                let (low, high) = ((span.start + offset).max(start), (span.end + offset).min(end));
                if low >= high { continue; }
                if attrs(doc, run, true, None)? != attrs(revised, new, true, None)? { return Err(unsupported("Changed attributes on retained runs are unsupported")); }
                if props(doc, run, "rPr", &[])? != props(revised, new, "rPr", &[])? { result.push((paragraph, low - offset, high - offset, properties_document(revised, new, "rPr", &[])?)); }
            }
        }
    }
    Ok(result)
}
/// Compare paragraph and run properties, and field instructions, between the result's current-view paragraphs and the
/// revised paragraphs they now equal in text.
fn pair_properties(doc: &mut Document, scope: usize, region: &Region, scratches: &mut [Document], len: usize, trailing: bool, metadata: &mut Metadata) -> Result<()> {
    if region.new.is_empty() { return Ok(()); }
    let groups = groups(doc, scope, region.position, region.position + len, trailing)?;
    if groups.len() != region.new.len() { return Err(Error::Invalid("Comparison produced an unexpected paragraph count".into())); }
    for (group, revised) in groups.iter().zip(scratches.iter_mut()) {
        paragraph_properties(doc, scope, group.last().unwrap().1, revised, metadata)?;
        field_instructions(doc, scope, group, revised, metadata)?;
        for (paragraph, start, end, properties) in run_formatting(doc, group, revised)? {
            let projected = text::paragraph(doc, paragraph, View::Current, false)?;
            let selection = text::preflight(doc, paragraph, &projected, start, end, false, false)?;
            for run in text::isolate(doc, selection, false)?.runs { revisions::format(doc, scope, run, &properties, None, metadata)?; }
        }
    }
    Ok(())
}
/// A bookmark arriving with inserted text keeps its name under a fresh id, and the copy left in deleted text loses its markers, as in Word.
fn settle_bookmarks(doc: &mut Document, scope: usize, metadata: &mut Metadata) -> Result<()> {
    let markers = doc.descendants(scope)?.filter(|&id| matches!(text::name(doc, id), Some("bookmarkStart" | "bookmarkEnd"))).collect::<Vec<_>>();
    let inserted = |id: usize| std::iter::successors(Some(id), |&id| doc.node(id).ok()?.parent).any(|id| text::name(doc, id) == Some("ins"));
    let (inside, outside): (Vec<_>, Vec<_>) = markers.into_iter().partition(|&id| inserted(id));
    let pairs = inside.iter().copied().filter(|&id| text::name(doc, id) == Some("bookmarkStart"))
        .map(|start| (start, inside.iter().copied().filter(|&id| attribute(doc, id, "id") == attribute(doc, start, "id")).collect::<Vec<_>>())).collect::<Vec<_>>();
    for (start, pair) in pairs {
        let (name, fresh) = (attribute(doc, start, "name"), metadata.fresh().to_string());
        for id in pair { doc.set_attribute(id, W, "id", &fresh, None)?; }
        let stale = outside.iter().copied().filter(|&id| text::name(doc, id) == Some("bookmarkStart") && attribute(doc, id, "name") == name)
            .map(|id| attribute(doc, id, "id")).collect::<Vec<_>>();
        for &id in &outside { if stale.contains(&attribute(doc, id, "id")) { doc.remove(id)?; } }
    }
    Ok(())
}

/// Queue a paragraph region; an insertion with no old paragraph goes before `following`, which must be a paragraph.
fn queue(doc: &Document, rows: &[text::Row], olds: &[usize], news: &[usize], following: Option<usize>, regions: &mut Vec<Region>) -> Result<()> {
    if olds.is_empty() && news.is_empty() { return Ok(()); }
    let anchor = olds.first().copied().or(following).filter(|&id| text::name(doc, id) == Some("p"))
        .ok_or_else(|| unsupported("Paragraph changes beside tables need a neighbouring paragraph"))?;
    let position = rows.iter().find(|row| row.paragraph == Some(anchor)).map(|row| row.position)
        .ok_or_else(|| Error::Invalid("Paragraph is outside the comparison story".into()))?;
    regions.push(Region { old: olds.to_vec(), new: news.to_vec(), position });
    Ok(())
}
/// Split a table or row into its content children and the structural keys of everything else.
fn table_parts(source: &Document, id: usize, content: &str, properties: &[&str]) -> Result<(Vec<Key>, Vec<usize>)> {
    let (mut keys, mut children) = (Vec::new(), Vec::new());
    for child in source.element_children(id)? {
        match text::name(source, child) {
            Some(local) if local == content => children.push(child),
            Some(local) if properties.contains(&local) => keys.push(key(source, child, &[], true, None, None)?),
            _ => return Err(unsupported("Tables with content other than rows and cells are unsupported")),
        }
    }
    Ok((keys, children))
}
/// Paired tables with the same grid compare cell by cell; any structural difference is refused.
fn plan_table(doc: &Document, rows: &[text::Row], old: usize, cx: &Cx, new: usize, regions: &mut Vec<Region>) -> Result<()> {
    let (old_keys, old_rows) = table_parts(doc, old, "tr", &["tblPr", "tblGrid"])?;
    let (new_keys, new_rows) = table_parts(cx.revised, new, "tr", &["tblPr", "tblGrid"])?;
    if old_keys != new_keys || old_rows.len() != new_rows.len() { return Err(unsupported("Changed table structure or row count is unsupported")); }
    for (&left, &right) in old_rows.iter().zip(&new_rows) {
        let (old_keys, old_cells) = table_parts(doc, left, "tc", &["trPr"])?;
        let (new_keys, new_cells) = table_parts(cx.revised, right, "tc", &["trPr"])?;
        if old_keys != new_keys || old_cells.len() != new_cells.len() { return Err(unsupported("Changed table row structure is unsupported")); }
        for (&left, &right) in old_cells.iter().zip(&new_cells) {
            if props(doc, left, "tcPr", &[])? != props(cx.revised, right, "tcPr", &[])? { return Err(unsupported("Changed table cell properties are unsupported")); }
            plan(doc, rows, left, cx, right, regions)?;
        }
    }
    Ok(())
}
/// Align the block children of paired containers and queue the paragraph regions to diff, descending into paired tables.
fn plan(doc: &Document, rows: &[text::Row], old_parent: usize, cx: &Cx, new_parent: usize, regions: &mut Vec<Region>) -> Result<()> {
    let revised = cx.revised;
    let blocks = |source: &Document, parent: usize| source.element_children(parent).map(|ids| ids.filter(|&id| text::name(source, id) != Some("tcPr")).collect::<Vec<_>>());
    let (old, new) = (blocks(doc, old_parent)?, blocks(revised, new_parent)?);
    let old_keys = old.iter().map(|&id| block(doc, id, &cx.links[0])).collect::<Result<Vec<_>>>()?;
    let new_keys = new.iter().map(|&id| block(revised, id, &cx.links[1])).collect::<Result<Vec<_>>>()?;
    let mut segments: Vec<(bool, Vec<usize>, Vec<usize>)> = Vec::new();
    for op in capture_diff_slices(Algorithm::Myers, &old_keys, &new_keys) {
        match op {
            DiffOp::Equal { old_index, new_index, len } => segments.extend((0..len).map(|i| (true, vec![old[old_index + i]], vec![new[new_index + i]]))),
            DiffOp::Delete { old_index, old_len, .. } => segments.push((false, old[old_index..old_index + old_len].to_vec(), Vec::new())),
            DiffOp::Insert { new_index, new_len, .. } => segments.push((false, Vec::new(), new[new_index..new_index + new_len].to_vec())),
            DiffOp::Replace { old_index, old_len, new_index, new_len } =>
                segments.push((false, old[old_index..old_index + old_len].to_vec(), new[new_index..new_index + new_len].to_vec())),
        }
    }
    // A change absorbs the equal paragraph before it when that paragraph's trailing separator changed: the mark before
    // a table or the end of the container is never inserted or deleted, so the change is carried by that paragraph's text.
    let mut merged: Vec<(bool, Vec<usize>, Vec<usize>)> = Vec::new();
    for (equal, olds, news) in segments {
        match merged.last() {
            Some((true, previous_old, previous_new)) if !equal && text::name(doc, previous_old[0]) == Some("p") && tokens(doc, previous_old)? != tokens(revised, previous_new)? => {
                let (_, mut old_ids, mut new_ids) = merged.pop().unwrap();
                old_ids.extend(olds);
                new_ids.extend(news);
                merged.push((false, old_ids, new_ids));
            }
            _ => merged.push((equal, olds, news)),
        }
    }
    let table = |source: &Document, id: usize| text::name(source, id) == Some("tbl");
    for (i, (equal, olds, news)) in merged.iter().enumerate() {
        let following = merged[i + 1..].iter().find_map(|(_, following, _)| following.first().copied());
        if *equal {
            if text::name(doc, olds[0]) == Some("p") && key(doc, olds[0], &[], true, None, Some(&cx.links[0]))? != key(revised, news[0], &[], true, None, Some(&cx.links[1]))? {
                queue(doc, rows, olds, news, following, regions)?;
            }
            continue;
        }
        // A change mixing paragraphs and tables splits at the tables, which pair in order.
        let old_tables = olds.iter().copied().filter(|&id| table(doc, id)).collect::<Vec<_>>();
        let new_tables = news.iter().copied().filter(|&id| table(revised, id)).collect::<Vec<_>>();
        if old_tables.len() != new_tables.len() || olds.iter().any(|&id| !table(doc, id) && text::name(doc, id) != Some("p")) ||
            news.iter().any(|&id| !table(revised, id) && text::name(revised, id) != Some("p")) {
            return Err(unsupported("Changed opaque body content or section properties are unsupported"));
        }
        let (mut old_groups, mut new_groups) = (olds.split(|&id| table(doc, id)), news.split(|&id| table(revised, id)));
        for (&left, &right) in old_tables.iter().zip(&new_tables) {
            queue(doc, rows, old_groups.next().unwrap(), new_groups.next().unwrap(), Some(left), regions)?;
            plan_table(doc, rows, left, cx, right, regions)?;
        }
        queue(doc, rows, old_groups.next().unwrap(), new_groups.next().unwrap(), following, regions)?;
    }
    Ok(())
}
pub fn compare_packages(original: &Package, revised: &Package, author: &str, date: Option<&str>) -> Result<Package> {
    let main = original.main_part()?;
    if main != revised.main_part()? || dependencies(original)? != dependencies(revised)? {
        return Err(unsupported("Changed content or formatting dependencies are unsupported"));
    }
    for package in [original, revised] {
        let tree = package.xml(&main)?;
        let doc = tree.read()?;
        if doc.descendants(doc.root)?.any(|id| text::revision_name(&doc, id).is_some()) {
            return Err(unsupported("Resolve existing revisions before comparing documents"));
        }
    }
    let hyperlinks = |package: &Package| -> Result<HashMap<String, String>> {
        Ok(package.lock()?.relationships(&main)?.into_iter().filter(|rel| rel.kind.ends_with("/hyperlink")).map(|rel| (rel.id, rel.target)).collect())
    };
    let links = [hyperlinks(original)?, hyperlinks(revised)?];
    let result = Package::from_bytes(&original.to_bytes()?)?;
    let tree = result.xml(&main)?;
    let other = revised.xml(&main)?;
    let revised = other.read()?;
    tree.edit(|doc| {
        let scope = doc.root;
        if text::name(doc, doc.root) != Some("document") || text::name(&revised, revised.root) != Some("document") ||
            attrs(doc, doc.root, true, None)? != attrs(&revised, revised.root, true, None)? {
            return Err(unsupported("Changed document-root attributes or non-Transitional vocabulary are unsupported"));
        }
        let old_body = text::child(doc, doc.root, "body").ok_or_else(|| Error::Invalid("Expected a document body".into()))?;
        let new_body = text::child(&revised, revised.root, "body").ok_or_else(|| Error::Invalid("Expected a document body".into()))?;
        for (source, body) in [(&*doc, old_body), (&*revised, new_body)] {
            if source.element_children(source.root)?.any(|id| id != body) { return Err(unsupported("Extra document-root content is unsupported")); }
        }
        if attrs(doc, old_body, true, None)? != attrs(&revised, new_body, true, None)? { return Err(unsupported("Changed body attributes are unsupported")); }
        let cx = Cx { revised: &revised, links };
        let rows = text::paragraphs(doc, scope, View::Current)?;
        let mut regions = Vec::new();
        plan(doc, &rows, old_body, &cx, new_body, &mut regions)?;
        let mut metadata = Metadata::new(doc, author, date)?;
        for region in regions.iter().rev() { region_diff(doc, scope, region, &cx, &mut metadata)?; }
        settle_bookmarks(doc, scope, &mut metadata)
    })?;
    Ok(result)
}
#[pyfunction]
#[pyo3(signature=(original, revised, author, date=None))]
pub fn compare(py: Python<'_>, original: &Package, revised: &Package, author: &str, date: Option<&str>) -> Result<Package> {
    py.detach(|| compare_packages(original, revised, author, date))
}
