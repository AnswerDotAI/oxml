//! Word text projections and edits over the native XML tree. Offsets count Unicode code points.
use crate::{error::{Error, Result}, xml::{self, Document, Element, Name, NodeKind, Xml}};
use pyo3::prelude::*;
use std::collections::HashMap;

pub const W: &str = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
pub const W14: &str = "http://schemas.microsoft.com/office/word/2010/wordml";
pub const XML: &str = "http://www.w3.org/XML/1998/namespace";
pub const OPAQUE: &str = "\u{fffc}";
fn invalid(message: &str) -> Error { Error::Invalid(message.into()) }
pub(crate) fn unsupported(message: &str) -> Error { Error::Unsupported(message.into()) }
pub fn name(doc: &Document, id: usize) -> Option<&str> {
    doc.node(id).ok()?.element().filter(|e| e.name.uri == W).map(|e| e.name.local.as_str())
}
pub fn container(name: &str) -> bool { matches!(name, "document" | "body" | "hdr" | "ftr" | "footnote" | "endnote" | "comment" | "tbl" | "tr" | "tc") }
fn marker(name: &str) -> bool { matches!(name, "bookmarkStart" | "bookmarkEnd" | "commentRangeStart" | "commentRangeEnd" | "annotationRef" | "commentReference") }
pub fn child(doc: &Document, id: usize, local: &str) -> Option<usize> {
    doc.node(id).ok()?.children.iter().copied().find(|&id| name(doc, id) == Some(local))
}
pub fn unique_child(doc: &Document, parent: usize, local: &str) -> Result<Option<usize>> {
    let mut children = doc.node(parent)?.children.iter().copied().filter(|&id| name(doc, id) == Some(local));
    let result = children.next();
    if children.next().is_some() { return Err(Error::Invalid(format!("Multiple {local} elements"))); }
    Ok(result)
}
pub fn descendants(doc: &Document, id: usize) -> Vec<usize> {
    let mut result = Vec::new();
    let mut stack = vec![id];
    while let Some(id) = stack.pop() {
        if let Ok(node) = doc.node(id) {
            if node.element().is_some() { result.push(id); }
            stack.extend(node.children.iter().rev().copied());
        }
    }
    result
}
pub fn inside(doc: &Document, root: usize, mut id: usize) -> Result<()> {
    loop {
        if id == root { return Ok(()); }
        id = doc.node(id)?.parent.ok_or_else(|| invalid("Target is outside this story"))?;
    }
}
pub fn revision_name(doc: &Document, id: usize) -> Option<&str> {
    let e = doc.node(id).ok()?.element()?;
    let local = e.name.local.as_str();
    let known = if e.name.uri == W {
        matches!(local, "ins" | "del" | "delText" | "delInstrText" | "cellIns" | "cellDel" | "cellMerge" | "numberingChange" |
            "pPrChange" | "rPrChange" | "sectPrChange" | "tblGridChange" | "tblPrChange" | "tblPrExChange" | "tcPrChange" | "trPrChange" |
            "moveFrom" | "moveTo" | "moveFromRangeStart" | "moveFromRangeEnd" | "moveToRangeStart" | "moveToRangeEnd" |
            "customXmlInsRangeStart" | "customXmlInsRangeEnd" | "customXmlDelRangeStart" | "customXmlDelRangeEnd" |
            "customXmlMoveFromRangeStart" | "customXmlMoveFromRangeEnd" | "customXmlMoveToRangeStart" | "customXmlMoveToRangeEnd")
    } else { e.name.uri == W14 && matches!(local, "conflictIns" | "conflictDel" | "customXmlConflictInsRangeStart" |
        "customXmlConflictInsRangeEnd" | "customXmlConflictDelRangeStart" | "customXmlConflictDelRangeEnd") };
    known.then_some(local)
}
pub fn boundary_paragraph(doc: &Document, id: usize) -> Option<usize> {
    if !matches!(name(doc, id), Some("ins" | "del")) { return None; }
    let run = doc.node(id).ok()?.parent?;
    let properties = doc.node(run).ok()?.parent?;
    let paragraph = doc.node(properties).ok()?.parent?;
    (name(doc, run) == Some("rPr") && name(doc, properties) == Some("pPr") && name(doc, paragraph) == Some("p")).then_some(paragraph)
}
pub fn context(doc: &Document, id: usize) -> Result<()> {
    let mut ancestor = Some(id);
    while let Some(id) = ancestor {
        let local = name(doc, id).unwrap_or("");
        if !container(local) && !matches!(local, "p" | "footnotes" | "endnotes" | "comments") {
            return Err(unsupported("Editing inside fields, content controls or other opaque containers is unsupported"));
        }
        let properties = match local { "p" => "pPr", "tr" => "trPr", "tc" => "tcPr", "tbl" => "tblPr", _ => "" };
        for &properties in doc.node(id)?.children.iter().filter(|&&id| name(doc, id) == Some(properties)) {
            for element in descendants(doc, properties) {
                if let Some(revision) = revision_name(doc, element) {
                    if local == "p" && (matches!(revision, "pPrChange" | "rPrChange") || boundary_paragraph(doc, element).is_some()) { continue; }
                    return Err(unsupported("Paragraph or table revision context is unsupported"));
                }
            }
        }
        ancestor = doc.node(id)?.parent;
    }
    Ok(())
}
pub fn history(doc: &Document, id: usize) -> bool { descendants(doc, id).iter().any(|&id| name(doc, id) == Some("rPrChange")) }
pub fn unsafe_content(doc: &Document, id: usize) -> bool {
    match name(doc, id) {
        Some("ins" | "del" | "rPr" | "pPr") => false,
        Some("fldChar" | "instrText" | "fldSimple" | "sdt" | "txbxContent" | "moveFrom" | "moveTo" | "delText") => true,
        _ => doc.node(id).is_ok_and(|n| n.children.iter().any(|&id| unsafe_content(doc, id))),
    }
}
pub fn token(doc: &Document, id: usize) -> Result<String> {
    let node = doc.node(id)?;
    let Some(e) = node.element() else {
        return Ok(if matches!(&node.kind, NodeKind::Text(s) if !s.trim().is_empty()) { OPAQUE.into() } else { String::new() });
    };
    Ok(match name(doc, id) {
        Some("rPr" | "annotationRef" | "commentReference") => String::new(),
        Some("t" | "delText") => node.children.iter().filter_map(|id| match &doc.nodes[*id].as_ref()?.kind {
            NodeKind::Text(s) => Some(s.as_str()), _ => None,
        }).collect(),
        Some("tab") => "\t".into(),
        Some("br" | "cr") if e.attribute(W, "type").unwrap_or("textWrapping") == "textWrapping" => "\u{b}".into(),
        _ => OPAQUE.into(),
    })
}
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum View { Current, Original }
impl View {
    fn parse(value: &str) -> Result<Self> { match value { "current" => Ok(Self::Current), "original" => Ok(Self::Original), _ => Err(invalid("view must be current or original")) } }
    fn label(self) -> &'static str { if self == Self::Current { "current" } else { "original" } }
}
#[derive(Clone, Copy, Debug)]
pub struct Span { pub element: Option<usize>, pub start: usize, pub end: usize }
#[derive(Clone, Copy, Debug)]
pub struct Barrier { pub start: usize, pub end: usize, pub revision: bool }
#[derive(Default, Debug)]
pub struct Projection {
    pub text: String, pub len: usize, pub spans: Vec<Span>, pub barriers: Vec<Barrier>, pub positions: HashMap<usize, usize>,
}
impl Projection {
    fn push(&mut self, value: &str) { self.text.push_str(value); self.len += value.chars().count(); }
    fn block(&mut self, start: usize, end: usize, revision: bool) { self.barriers.push(Barrier { start, end, revision }); }
}
pub fn paragraph(doc: &Document, element: usize, view: View, positions: bool) -> Result<Projection> {
    fn visit(doc: &Document, root: usize, parent: usize, visible: bool, view: View, positions: bool, out: &mut Projection) -> Result<()> {
        for &id in &doc.node(parent)?.children {
            if positions { out.positions.insert(id, out.len); }
            let node = doc.node(id)?;
            let start = out.len;
            match name(doc, id) {
                Some("r") => {
                    for &item in &node.children {
                        if positions { out.positions.insert(item, out.len); }
                        if !visible { continue; }
                        let value = token(doc, item)?;
                        let text = matches!(name(doc, item), Some("t" | "delText"));
                        if value == OPAQUE && !text { out.block(out.len, out.len + 1, false); }
                        if matches!(doc.node(item)?.kind, NodeKind::Comment(_) | NodeKind::Pi { .. }) || name(doc, item) == Some("annotationRef") {
                            out.block(out.len, out.len, false);
                        }
                        if text && doc.node(item)?.children.iter().any(|&id| !matches!(doc.nodes[id].as_ref().unwrap().kind, NodeKind::Text(_))) {
                            out.block(out.len, out.len + value.chars().count().max(1), false);
                        }
                        out.push(&value);
                    }
                    if visible && history(doc, id) { out.block(start, out.len, true); }
                }
                Some(local @ ("ins" | "del" | "hyperlink")) => {
                    visit(doc, root, id, visible && (local == "hyperlink" || (local == "ins") == (view == View::Current)), view, positions, out)?;
                    out.block(start, out.len, true);
                }
                Some("bookmarkStart" | "bookmarkEnd" | "commentRangeStart" | "commentRangeEnd" | "pPr" | "proofErr") => (),
                _ if visible => {
                    if matches!(node.kind, NodeKind::Comment(_) | NodeKind::Pi { .. }) { out.block(out.len, out.len, false); }
                    if node.element().is_some() || matches!(&node.kind, NodeKind::Text(s) if !s.trim().is_empty()) {
                        out.block(out.len, out.len + 1, false);
                        out.push(OPAQUE);
                    }
                }
                _ => (),
            }
            if parent == root { out.spans.push(Span { element: node.element().map(|_| id), start, end: out.len }); }
        }
        Ok(())
    }
    let mut result = Projection::default();
    visit(doc, element, element, true, view, positions, &mut result)?;
    Ok(result)
}
pub fn marks(doc: &Document, paragraph: usize) -> Vec<usize> {
    child(doc, paragraph, "pPr").and_then(|p| child(doc, p, "rPr")).map(|p| doc.nodes[p].as_ref().unwrap().children.iter()
        .copied().filter(|&id| matches!(name(doc, id), Some("ins" | "del"))).collect()).unwrap_or_default()
}
pub fn adjacent(doc: &Document, first: usize, second: usize) -> Result<bool> {
    let parent = doc.node(first)?.parent;
    if parent.is_none() || parent != doc.node(second)?.parent { return Ok(false); }
    let mut children = doc.sequence(parent)?.iter().copied().filter(|&id| doc.nodes[id].as_ref().unwrap().element().is_some());
    Ok(children.find(|&id| id == first).is_some() && children.next() == Some(second))
}
#[derive(Debug)]
pub struct Row { pub paragraph: Option<usize>, pub position: usize, pub projection: Projection }
pub fn paragraphs(doc: &Document, root: usize, view: View) -> Result<Vec<Row>> {
    fn walk(doc: &Document, id: usize, adjacent: bool, out: &mut Vec<(Option<usize>, bool)>) -> Result<()> {
        if name(doc, id) == Some("p") { out.push((Some(id), adjacent)); return Ok(()); }
        let mut previous = None;
        for &id in &doc.node(id)?.children {
            if doc.node(id)?.element().is_none() { continue; }
            let local = name(doc, id);
            if local.is_some_and(|n| container(n) || n == "p") { walk(doc, id, local == Some("p") && previous == Some("p"), out)?; }
            else if !matches!(local, Some("pPr" | "tblPr" | "tblGrid" | "trPr" | "tcPr" | "sectPr")) { out.push((None, false)); }
            previous = local;
        }
        Ok(())
    }
    let mut physical = Vec::new();
    walk(doc, root, false, &mut physical)?;
    let mut result: Vec<Row> = Vec::new();
    let mut position = 0;
    for (id, adjacent) in physical {
        if let Some(previous) = result.last() {
            let marks = previous.paragraph.map(|p| marks(doc, p)).unwrap_or_default();
            let hidden = adjacent && marks.len() == 1 && name(doc, marks[0]) == Some(if view == View::Current { "del" } else { "ins" });
            if !hidden { position += 1; }
        }
        let projection = match id { Some(id) => paragraph(doc, id, view, false)?, None => {
            let mut projection = Projection::default(); projection.push(OPAQUE); projection.block(0, 1, false); projection
        }};
        let len = projection.len;
        result.push(Row { paragraph: id, position, projection });
        position += len;
    }
    Ok(result)
}
fn projected_text(rows: &[Row]) -> String {
    let mut result = String::new();
    let mut end = 0;
    for row in rows {
        for _ in end..row.position { result.push('\n'); }
        result.push_str(&row.projection.text);
        end = row.position + row.projection.len;
    }
    result
}
fn slice(value: &str, start: usize, end: usize) -> String { value.chars().skip(start).take(end - start).collect() }

/// A detached XML shell used only to snapshot the formatting that an edit will reuse.
pub fn shell(doc: &Document, template: Option<usize>, properties: &[usize]) -> Result<Document> {
    let element = match template {
        Some(id) => doc.node(id)?.element().ok_or_else(|| invalid("Expected an XML element"))?.clone(),
        None => word_element("r"),
    };
    let mut result = Document::from_element(element);
    for &id in properties { result.import(doc, id, Some(result.root))?; }
    Ok(result)
}
pub fn word_element(local: &str) -> Element {
    Element { name: Name { uri: W.into(), local: local.into(), prefix: "w".into() }, attributes: Vec::new(),
        namespaces: vec![(String::new(), String::new()), ("xml".into(), XML.into()), ("w".into(), W.into())] }
}
pub fn attach(doc: &mut Document, parent: usize, index: usize, source: &Document) -> Result<usize> {
    doc.import_at(source, source.root, parent, index)
}
pub fn run_text(doc: &Document, template: Option<usize>, text: &str) -> Result<Document> {
    if text.contains(['\n', '\r']) { return Err(unsupported("Paragraph insertion/joining is unsupported; use \\v for a line break")); }
    if !text.chars().all(|c| c == '\u{b}' || xml::xml_char(c)) { return Err(invalid("Illegal XML character")); }
    let properties = template.map(|id| doc.nodes[id].as_ref().unwrap().children.iter().copied()
        .filter(|&id| name(doc, id) == Some("rPr")).collect::<Vec<_>>()).unwrap_or_default();
    let mut result = shell(doc, template, &properties)?;
    let mut start = 0;
    let append = |result: &mut Document, local: &str, value: &str| -> Result<()> {
        let id = result.add(Some(result.root), NodeKind::Element(word_element(local)))?;
        if local == "t" { result.set_attribute(id, XML, "space", "preserve", None)?; result.set_text(id, value)?; }
        Ok(())
    };
    for (index, c) in text.char_indices() {
        if c != '\t' && c != '\u{b}' { continue; }
        if start < index { append(&mut result, "t", &text[start..index])?; }
        append(&mut result, if c == '\t' { "tab" } else { "br" }, "")?;
        start = index + c.len_utf8();
    }
    if start < text.len() { append(&mut result, "t", &text[start..])?; }
    Ok(result)
}
pub fn split_run(doc: &mut Document, run: usize, offset: usize) -> Result<usize> {
    let (parent, index) = doc.position(run)?;
    let parent = parent.ok_or_else(|| invalid("Run has no paragraph"))?;
    let nodes = doc.node(run)?.children.clone();
    let right = doc.copy(run, parent, index + 1)?;
    let copied = doc.node(right)?.children.clone();
    let mut position = 0;
    for (id, other) in nodes.into_iter().zip(copied) {
        if name(doc, id) == Some("rPr") { continue; }
        let value = token(doc, id)?;
        let end = position + value.chars().count();
        if position < offset && offset < end {
            for (id, value) in [(id, slice(&value, 0, offset - position)), (other, slice(&value, offset - position, end - position))] {
                doc.set_text(id, &value)?; doc.set_attribute(id, XML, "space", "preserve", None)?;
            }
        } else if end <= offset && (!value.is_empty() || position < offset) { doc.remove(other)?; }
        else { doc.remove(id)?; }
        position = end;
    }
    Ok(index + 1)
}
pub fn boundary(doc: &mut Document, paragraph_id: usize, offset: usize) -> Result<usize> {
    let projected = paragraph(doc, paragraph_id, View::Current, false)?;
    for (index, span) in projected.spans.iter().enumerate() {
        if span.start == offset && offset < span.end { return Ok(index); }
        if span.start < offset && offset < span.end {
            let run = span.element.filter(|&id| name(doc, id) == Some("r") && !history(doc, id))
                .ok_or_else(|| unsupported("Boundary is inside protected XML"))?;
            return split_run(doc, run, offset - span.start);
        }
    }
    if offset == projected.len { return Ok(doc.node(paragraph_id)?.children.len()); }
    Err(unsupported("Cannot place a boundary inside opaque XML"))
}
#[derive(Clone, Copy, Debug)]
pub struct Selection { pub paragraph: usize, pub start: usize, pub end: usize, pub template: Option<usize> }
pub fn preflight(doc: &Document, paragraph_id: usize, projected: &Projection, start: usize, end: usize) -> Result<Selection> {
    context(doc, paragraph_id)?;
    if unsafe_content(doc, paragraph_id) { return Err(unsupported("Fields, content controls, textboxes and moves are unsupported")); }
    for &Barrier { start: a, end: b, revision } in &projected.barriers {
        let blocked = if revision {
            if a == b { start < a && a < end } else if start == end { a < start && start < b } else { start < b && end > a }
        } else { start < b && end > a || start == end && a <= start && start < b || a == b && start <= a && a <= end };
        if blocked { return Err(unsupported("Range touches a revision, hyperlink or opaque XML boundary")); }
    }
    let ordinary = projected.spans.iter().filter(|s| s.element.is_some_and(|id| name(doc, id) == Some("r") && !history(doc, id)));
    let template = ordinary.clone().find(|s| s.start <= start && start < s.end || s.start >= start).or_else(|| ordinary.last()).and_then(|s| s.element);
    Ok(Selection { paragraph: paragraph_id, start, end, template })
}
#[derive(Debug)]
pub struct Isolated { pub paragraph: usize, pub runs: Vec<usize>, pub index: usize, pub template: Option<usize> }
pub fn isolate(doc: &mut Document, selection: Selection, extract_references: bool) -> Result<Isolated> {
    let Selection { paragraph: p, start, end, template } = selection;
    boundary(doc, p, end)?;
    let index = boundary(doc, p, start)?;
    let runs = paragraph(doc, p, View::Current, false)?.spans.iter().filter(|s| s.start < end && s.end > start)
        .filter_map(|s| s.element.filter(|&id| name(doc, id) == Some("r"))).collect::<Vec<_>>();
    if extract_references {
        for &run in &runs {
            let references = doc.node(run)?.children.iter().copied().filter(|&id| name(doc, id) == Some("commentReference")).collect::<Vec<_>>();
            if references.is_empty() { continue; }
            let props = doc.node(run)?.children.iter().copied().filter(|&id| name(doc, id) == Some("rPr")).collect::<Vec<_>>();
            let snapshot = shell(doc, Some(run), &props)?;
            let marker_run = attach(doc, p, doc.position(run)?.1 + 1, &snapshot)?;
            for id in references { doc.move_node(id, marker_run, doc.node(marker_run)?.children.len())?; }
        }
    }
    Ok(Isolated { paragraph: p, runs, index, template })
}
pub fn check_paragraph(doc: &Document, id: usize, boundaries: bool) -> Result<()> {
    if name(doc, id) != Some("p") || doc.node(id)?.parent.is_none() { return Err(invalid("Expected a paragraph in a Word container")); }
    if !matches!(name(doc, doc.node(id)?.parent.unwrap()), Some("body" | "hdr" | "ftr" | "tc" | "footnote" | "endnote" | "comment")) {
        return Err(unsupported("Paragraph edits require one ordinary immediate container"));
    }
    context(doc, id)?;
    if let Some(properties) = unique_child(doc, id, "pPr")? {
        unique_child(doc, properties, "rPr")?;
        for element in descendants(doc, properties) {
            if name(doc, element) == Some("sectPr") { return Err(unsupported("Editing section-break paragraphs is unsupported")); }
            if revision_name(doc, element).is_some() && !(boundaries && boundary_paragraph(doc, element).is_some()) {
                return Err(unsupported("Paragraph edits cannot discard existing property/boundary revisions"));
            }
        }
    }
    if marks(doc, id).len() > 1 { return Err(unsupported("Conflicting paragraph-boundary revisions are unsupported")); }
    Ok(())
}
pub fn split_at(doc: &mut Document, paragraph: usize, index: usize, snapshot: Option<&Document>) -> Result<(usize, usize)> {
    let (parent, location) = doc.position(paragraph)?;
    let parent = parent.ok_or_else(|| invalid("Paragraph has no parent"))?;
    let properties = unique_child(doc, paragraph, "pPr")?;
    let prefix = doc.node(paragraph)?.children[..index].iter().copied().filter(|id| Some(*id) != properties).collect::<Vec<_>>();
    let own_shell;
    let snapshot = match snapshot { Some(snapshot) => snapshot, None => {
        own_shell = shell(doc, Some(paragraph), &properties.into_iter().collect::<Vec<_>>())?; &own_shell
    }};
    let left = attach(doc, parent, location, snapshot)?;
    for local in ["paraId", "textId"] { doc.remove_attribute(left, W14, local)?; }
    for mark in marks(doc, left) { doc.remove(mark)?; }
    for id in prefix { doc.move_node(id, left, doc.node(left)?.children.len())?; }
    Ok((left, paragraph))
}
pub fn content_start(doc: &Document, paragraph: usize) -> Result<usize> {
    match unique_child(doc, paragraph, "pPr")? { Some(id) => Ok(doc.position(id)?.1 + 1), None => Ok(0) }
}
pub fn join(doc: &mut Document, first: usize, second: usize) -> Result<usize> {
    // PowerTools RP052: the final paragraph owns the surviving mark and properties.
    let mut index = content_start(doc, second)?;
    let properties = unique_child(doc, first, "pPr")?;
    for id in doc.node(first)?.children.clone() {
        if Some(id) == properties { continue; }
        doc.move_node(id, second, index)?;
        index += 1;
    }
    doc.remove(first)?;
    Ok(second)
}
pub fn split_paragraph(doc: &mut Document, id: usize, offset: usize) -> Result<(usize, usize)> {
    check_paragraph(doc, id, false)?;
    let projected = paragraph(doc, id, View::Current, false)?;
    if offset > projected.len { return Err(invalid("Range positions are outside the story")); }
    preflight(doc, id, &projected, offset, offset)?;
    let index = boundary(doc, id, offset)?;
    split_at(doc, id, index, None)
}
pub fn join_paragraphs(doc: &mut Document, first: usize, second: usize) -> Result<usize> {
    check_paragraph(doc, first, false)?;
    check_paragraph(doc, second, false)?;
    if !adjacent(doc, first, second)? { return Err(unsupported("Only adjacent paragraphs in the same container can be joined")); }
    join(doc, first, second)
}
pub fn span_parts(doc: &Document, rows: &[Row], start: usize, end: usize) -> Result<Vec<Selection>> {
    let index = |offset| rows.iter().position(|row| row.position <= offset && offset <= row.position + row.projection.len);
    let first = index(start).ok_or_else(|| unsupported("Range touches an opaque paragraph boundary"))?;
    let last = index(end).ok_or_else(|| unsupported("Range touches an opaque paragraph boundary"))?;
    let mut result: Vec<Selection> = Vec::new();
    for row in &rows[first..=last] {
        let id = row.paragraph.ok_or_else(|| unsupported("Range crosses opaque content"))?;
        check_paragraph(doc, id, false)?;
        if let Some(previous) = result.last() {
            if !adjacent(doc, previous.paragraph, id)? { return Err(unsupported("Range crosses a table/container boundary")); }
        }
        result.push(preflight(doc, id, &row.projection, start.saturating_sub(row.position), (end - row.position).min(row.projection.len))?);
    }
    Ok(result)
}
pub fn replacement_runs(doc: &Document, template: Option<usize>, text: &str) -> Result<Vec<Option<Document>>> {
    if text.contains('\r') { return Err(invalid("Use \\n for paragraph boundaries")); }
    text.split('\n').map(|chunk| if chunk.is_empty() { Ok(None) } else { run_text(doc, template, chunk).map(Some) }).collect()
}

#[pyclass(name = "NativeStory", module = "oxml._core", from_py_object)]
#[derive(Clone)]
pub struct Story { pub xml: Xml, pub element: usize, pub view: View }
impl Story {
    pub fn new_native(xml: Xml, element: usize, view: View) -> Result<Self> {
        {
            let doc = xml.read()?;
            doc.node(element)?;
            if !name(&doc, element).is_some_and(|n| container(n) || n == "p") { return Err(invalid("Expected a Word story container or paragraph")); }
        }
        Ok(Self { xml, element, view })
    }
    pub fn text_native(&self) -> Result<String> { Ok(projected_text(&paragraphs(&*self.xml.read()?, self.element, self.view)?)) }
    pub fn range_native(&self, start: usize, end: usize) -> Result<Range> {
        let revision = self.xml.revision();
        let doc = self.xml.read()?;
        let rows = paragraphs(&doc, self.element, self.view)?;
        let length = rows.last().map(|r| r.position + r.projection.len).unwrap_or(0);
        if start > end || end > length { return Err(invalid("Range positions are outside the story")); }
        Ok(Range { story: self.clone(), start, end, revision })
    }
    pub fn position_native(&self, marker_id: usize) -> Result<usize> {
        let doc = self.xml.read()?;
        if !name(&doc, marker_id).is_some_and(marker) { return Err(invalid("Expected a zero-width Word marker in this story")); }
        for row in paragraphs(&doc, self.element, self.view)? {
            if let Some(id) = row.paragraph {
                if let Some(position) = paragraph(&doc, id, self.view, true)?.positions.get(&marker_id) { return Ok(row.position + position); }
            }
        }
        Err(invalid("Marker is outside the projected story"))
    }
}
#[pymethods]
impl Story {
    #[new]
    #[pyo3(signature=(xml, element, view="current"))]
    fn new(xml: Xml, element: usize, view: &str) -> Result<Self> { Self::new_native(xml, element, View::parse(view)?) }
    #[getter]
    fn xml(&self) -> Xml { self.xml.clone() }
    #[getter]
    fn element_id(&self) -> usize { self.element }
    #[getter]
    fn view(&self) -> &'static str { self.view.label() }
    #[getter]
    fn text(&self, py: Python<'_>) -> Result<String> { py.detach(|| self.text_native()) }
    fn range(&self, start: usize, end: usize) -> Result<Range> { self.range_native(start, end) }
    #[pyo3(signature=(literal, start=0))]
    fn find(&self, py: Python<'_>, literal: &str, start: isize) -> Result<Option<Range>> {
        if literal.is_empty() { return Err(invalid("find requires a nonempty literal string")); }
        py.detach(|| {
            let text = self.text_native()?;
            let length = text.chars().count();
            let start = if start < 0 { length.saturating_sub(start.unsigned_abs()) } else { start as usize };
            if start > length { return Ok(None); }
            let byte = text.char_indices().nth(start).map(|(i, _)| i).unwrap_or(text.len());
            Ok(text[byte..].find(literal).map(|offset| {
                let start = start + text[byte..byte + offset].chars().count();
                Range { story: self.clone(), start, end: start + literal.chars().count(), revision: self.xml.revision() }
            }))
        })
    }
    fn position(&self, marker_id: usize) -> Result<usize> { self.position_native(marker_id) }
}
#[pyclass(name = "NativeRange", module = "oxml._core", from_py_object)]
#[derive(Clone)]
pub struct Range { pub story: Story, pub start: usize, pub end: usize, pub revision: u64 }
impl Range {
    pub fn check(&self) -> Result<()> {
        self.story.xml.read()?.node(self.story.element)?;
        if self.revision != self.story.xml.revision() { return Err(Error::Stale("Text range is stale; find or select it again".into())); }
        Ok(())
    }
    pub fn editable(&self) -> Result<()> {
        self.check()?;
        if self.story.view != View::Current { return Err(unsupported("Original text view is read-only")); }
        Ok(())
    }
    pub fn selection(&self, doc: &Document) -> Result<Selection> {
        for row in paragraphs(doc, self.story.element, self.story.view)? {
            if row.position <= self.start && self.end <= row.position + row.projection.len {
                let id = row.paragraph.ok_or_else(|| unsupported("Range contains an opaque story structure"))?;
                return preflight(doc, id, &row.projection, self.start - row.position, self.end - row.position);
            }
        }
        Err(unsupported("Cross-paragraph replacement is unsupported"))
    }
    pub fn replace_native(&self, text: &str) -> Result<Range> {
        self.editable()?;
        let (mut story, mut start) = (self.story.clone(), self.start);
        let (parts, expressions, left_shell, multiline) = {
            let doc = self.story.xml.read()?;
            let rows = paragraphs(&doc, self.story.element, self.story.view)?;
            let multiline = text.contains('\n') || !rows.iter().any(|r| r.paragraph.is_some() && r.position <= self.start && self.end <= r.position + r.projection.len);
            let parts = if multiline { span_parts(&doc, &rows, self.start, self.end)? } else { vec![self.selection(&doc)?] };
            let first = parts[0].paragraph;
            let expressions = replacement_runs(&doc, parts[0].template, text)?;
            let left_shell = if multiline { Some(shell(&doc, Some(first), &child(&doc, first, "pPr").into_iter().collect::<Vec<_>>())?) } else { None };
            if multiline && name(&doc, self.story.element) == Some("p") {
                story.element = doc.node(first)?.parent.ok_or_else(|| invalid("Paragraph has no parent"))?;
                start = paragraphs(&doc, story.element, View::Current)?.iter().find(|r| r.paragraph == Some(first)).unwrap().position + parts[0].start;
            }
            (parts, expressions, left_shell, multiline)
        };
        if self.start == self.end && text.is_empty() { return Ok(self.clone()); }
        self.story.xml.edit(|doc| {
            let isolated = parts.into_iter().map(|part| isolate(doc, part, true)).collect::<Result<Vec<_>>>()?;
            let mut paragraph = isolated[0].paragraph;
            let prefix = if multiline { isolated[0].index - content_start(doc, paragraph)? } else { 0 };
            for part in &isolated { for &run in &part.runs { doc.remove(run)?; } }
            for part in &isolated[1..] { paragraph = join(doc, paragraph, part.paragraph)?; }
            let mut index = if multiline { content_start(doc, paragraph)? + prefix } else { isolated[0].index };
            for (i, expression) in expressions.iter().enumerate() {
                if let Some(expression) = expression { attach(doc, paragraph, index, expression)?; index += 1; }
                if i + 1 < expressions.len() {
                    (_, paragraph) = split_at(doc, paragraph, index, left_shell.as_ref())?;
                    index = content_start(doc, paragraph)?;
                }
            }
            Ok(())
        })?;
        Ok(Range { revision: story.xml.revision(), story, start, end: start + text.chars().count() })
    }
}
#[pymethods]
impl Range {
    #[new]
    fn new(story: Story, start: usize, end: usize) -> Result<Self> { story.range_native(start, end) }
    #[getter]
    fn story(&self) -> Story { self.story.clone() }
    #[getter]
    fn start(&self) -> usize { self.start }
    #[getter]
    fn end(&self) -> usize { self.end }
    #[getter]
    fn text(&self, py: Python<'_>) -> Result<String> { self.check()?; py.detach(|| self.story.text_native().map(|s| slice(&s, self.start, self.end))) }
    fn replace(&self, py: Python<'_>, text: &str) -> Result<Self> { py.detach(|| self.replace_native(text)) }
}
#[pyfunction]
pub fn split(xml: &Xml, paragraph: usize, offset: usize) -> Result<(usize, usize)> { xml.edit(|doc| split_paragraph(doc, paragraph, offset)) }
#[pyfunction]
pub fn join_pair(xml: &Xml, first: usize, second: usize) -> Result<usize> { xml.edit(|doc| join_paragraphs(doc, first, second)) }
