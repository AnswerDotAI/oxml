//! Tracked text, paragraph marks and direct-property histories, sharing the native text editor.
use crate::{error::{Error, Result}, text::{self, *}, xml::{Document, NodeKind, Xml}};
use chrono::{DateTime, SecondsFormat, Utc};
use pyo3::prelude::*;
use std::{collections::{HashMap, HashSet}, time::SystemTime};

pub fn timestamp(date: Option<&str>) -> Result<String> {
    let date = match date {
        Some(date) => DateTime::parse_from_rfc3339(date).map_err(|_| Error::Invalid("date requires a timezone-aware datetime".into()))?.with_timezone(&Utc),
        None => DateTime::<Utc>::from(SystemTime::now()),
    };
    Ok(date.to_rfc3339_opts(if date.timestamp_subsec_nanos() == 0 { SecondsFormat::Secs } else { SecondsFormat::Micros }, true))
}
pub struct Metadata { author: String, date: String, used: HashSet<u64>, next: u64 }
impl Metadata {
    pub fn new(doc: &Document, author: &str, date: Option<&str>) -> Result<Self> {
        if author.is_empty() { return Err(Error::Invalid("A nonempty author is required".into())); }
        let date = timestamp(date)?;
        crate::xml::check_value(author)?;
        let used = doc.nodes.iter().flatten().filter_map(|n| n.element()?.attribute(W, "id")?.trim().parse().ok()).collect();
        Ok(Self { author: author.into(), date, used, next: 0 })
    }
    /// The next revision or bookmark id unused in the tree.
    pub fn fresh(&mut self) -> u64 {
        while self.used.contains(&self.next) { self.next += 1; }
        self.next += 1;
        self.next - 1
    }
    pub fn element(&mut self, kind: &str, content: Option<&Document>) -> Result<Document> {
        let mut doc = Document::from_element(word_element(kind));
        let ident = self.fresh().to_string();
        for (key, value) in [("author", self.author.as_str()), ("date", self.date.as_str()), ("id", &ident)] {
            doc.set_attribute(doc.root, W, key, value, None)?;
        }
        if let Some(content) = content { doc.import(content, content.root, Some(doc.root))?; }
        Ok(doc)
    }
}
pub fn runs(doc: &Document, id: usize) -> Result<Vec<usize>> {
    if !matches!(name(doc, id), Some("ins" | "del")) || doc.node(id)?.parent.is_none_or(|p| name(doc, p) != Some("p")) {
        return Err(unsupported("Only inline insertion/deletion wrappers directly in paragraphs are supported"));
    }
    context(doc, doc.node(id)?.parent.unwrap())?;
    let mut result = Vec::new();
    for &run in &doc.node(id)?.children {
        let node = doc.node(run)?;
        if node.element().is_none() {
            if matches!(&node.kind, NodeKind::Text(s) if !s.trim().is_empty()) { return Err(unsupported("Revision contains non-run text")); }
            continue;
        }
        if name(doc, run) != Some("r") {
            if name(doc, run).is_some_and(crate::text::marker) { continue; }
            return Err(unsupported("Revision contains nested revisions or unsupported non-run content"));
        }
        for &item in &node.children {
            if token(doc, item)? == OPAQUE { return Err(unsupported("Revision contains an unsupported run payload")); }
            if doc.node(item)?.element().is_none() { continue; }
            let local = name(doc, item);
            if matches!(local, Some("t" | "delText" | "instrText" | "delInstrText")) && matches!(local, Some("t" | "instrText")) == (name(doc, id) == Some("del")) {
                return Err(unsupported("Revision contains text with the wrong insertion/deletion form"));
            }
            if local == Some("rPr") {
                if doc.descendants(item)?.any(|id| revision_name(doc, id).is_some()) { return Err(unsupported("Run-property revisions are unsupported")); }
            } else if doc.node(item)?.children.iter().any(|&id| !matches!(doc.nodes[id].as_ref().unwrap().kind, NodeKind::Text(_))) {
                return Err(unsupported("Revision text contains nested XML"));
            }
        }
        result.push(run);
    }
    Ok(result)
}
pub fn rename_text(doc: &mut Document, runs: &[usize], deleted: bool) -> Result<()> {
    for &run in runs {
        for id in doc.node(run)?.children.clone() {
            for (text, deleted_text) in [("t", "delText"), ("instrText", "delInstrText")] {
                let (old, new) = if deleted { (text, deleted_text) } else { (deleted_text, text) };
                if name(doc, id) == Some(old) { doc.rename(id, W, new, None)?; }
            }
        }
    }
    Ok(())
}
fn check_boundary(doc: &Document, id: usize, following: Option<usize>) -> Result<(usize, usize)> {
    let paragraph = boundary_paragraph(doc, id).ok_or_else(|| unsupported("Not a paragraph-boundary revision"))?;
    check_paragraph(doc, paragraph, true, false)?;
    let following = if let Some(following) = following { Some(following) } else {
        doc.element_children(doc.node(paragraph)?.parent.unwrap())?.skip_while(|&p| p != paragraph).nth(1)
    }.filter(|&p| name(doc, p) == Some("p")).ok_or_else(|| unsupported("Final paragraph marks and paragraph/table boundaries are unsupported"))?;
    check_paragraph(doc, following, true, true)?;
    if marks(doc, paragraph).len() != 1 || doc.node(id)?.children.iter().any(|&id| {
        let node = doc.nodes[id].as_ref().unwrap(); node.element().is_some() || matches!(&node.kind, NodeKind::Text(s) if !s.trim().is_empty())
    }) { return Err(unsupported("Conflicting or nonempty paragraph-boundary markup is unsupported")); }
    Ok((paragraph, following))
}
pub fn add_mark(doc: &mut Document, paragraph: usize, snapshot: &Document) -> Result<usize> {
    let properties = match unique_child(doc, paragraph, "pPr")? { Some(id) => id, None => doc.insert_kind(paragraph, 0, NodeKind::Element(word_element("pPr")))? };
    let properties = match unique_child(doc, properties, "rPr")? { Some(id) => id, None => {
        let index = crate::schema::insertion_position(doc, properties, W, "rPr")?;
        doc.insert_kind(properties, index, NodeKind::Element(word_element("rPr")))?
    }};
    let index = crate::schema::insertion_position(doc, properties, W, name(snapshot, snapshot.root).unwrap())?;
    attach(doc, properties, index, snapshot)
}
fn property_owner(doc: &Document, properties: usize) -> Result<usize> {
    let owner = doc.node(properties)?.parent.ok_or_else(|| unsupported("Unsupported property revision location"))?;
    let valid = if name(doc, properties) == Some("pPr") { name(doc, owner) == Some("p") } else { matches!(name(doc, owner), Some("r" | "pPr")) };
    if !valid { return Err(unsupported("Unsupported property revision location")); }
    let paragraph = if matches!(name(doc, owner), Some("r" | "pPr")) { doc.node(owner)?.parent.unwrap_or(owner) } else { owner };
    if name(doc, paragraph) != Some("p") { return Err(unsupported("Expected properties of an ordinary run or paragraph")); }
    context(doc, paragraph)?;
    Ok(paragraph)
}
pub fn property_change(doc: &Document, id: usize) -> Result<(usize, usize)> {
    let kind = name(doc, id).unwrap_or("");
    let current = doc.node(id)?.parent.ok_or_else(|| unsupported("Expected a run or paragraph property change"))?;
    if !matches!(kind, "rPrChange" | "pPrChange") || name(doc, current) != kind.strip_suffix("Change") {
        return Err(unsupported("Expected a run or paragraph property change"));
    }
    property_owner(doc, current)?;
    unique_child(doc, doc.node(current)?.parent.unwrap(), name(doc, current).unwrap())?;
    let mut children = doc.element_children(id)?;
    let previous = match (children.next(), children.next()) {
        (Some(previous), None) if name(doc, previous) == name(doc, current) => previous,
        _ => return Err(unsupported("Property change requires exactly one previous-properties snapshot")),
    };
    if doc.descendants(current)?.any(|e| e != id && revision_name(doc, e).is_some()) {
        return Err(unsupported("Nested or conflicting property revision history is unsupported"));
    }
    if kind == "pPrChange" && doc.element_children(previous)?.any(|id| matches!(name(doc, id), Some("rPr" | "sectPr"))) {
        return Err(unsupported("Paragraph history cannot replace paragraph-mark or section properties"));
    }
    Ok((current, previous))
}
fn check(doc: &Document, id: usize, following: Option<usize>) -> Result<()> {
    if matches!(name(doc, id), Some("rPrChange" | "pPrChange")) { property_change(doc, id)?; }
    else if boundary_paragraph(doc, id).is_some() { check_boundary(doc, id, following)?; }
    else { runs(doc, id)?; }
    Ok(())
}
fn checked_apply(doc: &mut Document, id: usize, accept: bool, following: Option<usize>) -> Result<()> {
    if matches!(name(doc, id), Some("rPrChange" | "pPrChange")) {
        let (current, previous) = property_change(doc, id)?;
        if accept { return doc.remove(id); }
        let (parent, index) = doc.position(current)?;
        let restored = doc.copy(previous, parent.unwrap(), index)?;
        if name(doc, current) == Some("pPr") {
            for child in doc.node(current)?.children.clone() {
                if matches!(name(doc, child), Some("rPr" | "sectPr")) { doc.move_node(child, restored, doc.node(restored)?.children.len())?; }
            }
        }
        return doc.remove(current);
    }
    if boundary_paragraph(doc, id).is_some() {
        let (paragraph, following) = check_boundary(doc, id, following)?;
        if (name(doc, id) == Some("ins")) == accept { doc.remove(id)?; } else { text::join(doc, paragraph, following)?; }
        return Ok(());
    }
    if (name(doc, id) == Some("ins")) == accept {
        if name(doc, id) == Some("del") {
            let runs = doc.element_children(id)?.collect::<Vec<_>>();
            rename_text(doc, &runs, false)?;
        }
        let (parent, mut index) = doc.position(id)?;
        for child in doc.node(id)?.children.clone() { doc.move_node(child, parent.unwrap(), index)?; index += 1; }
    }
    doc.remove(id)
}
pub fn apply(doc: &mut Document, id: usize, accept: bool) -> Result<()> { check(doc, id, None)?; checked_apply(doc, id, accept, None) }
fn collect_with_successors(doc: &Document, scope: usize) -> Result<(Vec<usize>, HashMap<usize, usize>)> {
    fn visit(doc: &Document, id: usize, revisions: &mut Vec<usize>, successors: &mut HashMap<usize, usize>) -> Result<()> {
        if revision_name(doc, id).is_some() {
            revisions.push(id); return Ok(());
        }
        let mut children = doc.element_children(id)?.peekable();
        while let Some(child) = children.next() {
            if name(doc, child) == Some("p") { if let Some(&next) = children.peek() { successors.insert(child, next); } }
            visit(doc, child, revisions, successors)?;
        }
        Ok(())
    }
    let (mut revisions, mut successors) = (Vec::new(), HashMap::new());
    visit(doc, scope, &mut revisions, &mut successors)?;
    Ok((revisions, successors))
}
pub fn collect(doc: &Document, scope: usize) -> Result<Vec<usize>> { collect_with_successors(doc, scope).map(|v| v.0) }
pub fn apply_all(doc: &mut Document, scope: usize, accept: bool) -> Result<usize> {
    let (ids, successors) = collect_with_successors(doc, scope)?;
    let following = |doc: &Document, id| boundary_paragraph(doc, id).and_then(|p| successors.get(&p).copied());
    for &id in &ids { check(doc, id, following(doc, id))?; }
    for &id in &ids { checked_apply(doc, id, accept, following(doc, id))?; }
    Ok(ids.len())
}
pub fn revision_text(doc: &Document, id: usize) -> Result<String> {
    doc.node(id)?;
    if matches!(name(doc, id), Some("rPrChange" | "pPrChange")) { return Ok(String::new()); }
    if boundary_paragraph(doc, id).is_some() { return Ok("\n".into()); }
    let mut result = String::new();
    for run in runs(doc, id)? { for &item in &doc.node(run)?.children { result.push_str(&token(doc, item)?); } }
    Ok(result)
}
pub fn format(doc: &mut Document, scope: usize, target: usize, properties: &Document, previous: Option<Document>, attrs: &mut Metadata) -> Result<usize> {
    let expected = match name(doc, target) { Some("r") => "rPr", Some("p") => "pPr", _ => return Err(Error::Invalid("Formatting target requires a run or paragraph".into())) };
    inside(doc, scope, target)?;
    let paragraph = if expected == "pPr" { Some(target) } else { doc.node(target)?.parent };
    let paragraph = paragraph.filter(|&id| name(doc, id) == Some("p")).ok_or_else(|| unsupported("Formatting requires an ordinary paragraph"))?;
    context(doc, paragraph)?;
    if name(properties, properties.root) != Some(expected) { return Err(Error::Invalid(format!("Expected w:{expected} properties"))); }
    if expected == "pPr" && properties.element_children(properties.root)?.any(|id| matches!(name(properties, id), Some("rPr" | "sectPr"))) {
        return Err(Error::Invalid("Supply paragraph base properties only; paragraph-mark and section properties are retained".into()));
    }
    let old = unique_child(doc, target, expected)?;
    if properties.descendants(properties.root)?.any(|id| revision_name(properties, id).is_some()) ||
        old.is_some_and(|id| doc.descendants(id).unwrap().any(|id| revision_name(doc, id).is_some())) {
        return Err(unsupported("Nested or conflicting property revision history is unsupported"));
    }
    let mut previous = match (previous, old) {
        (Some(previous), _) => previous,
        (None, Some(id)) => shell(doc, Some(id), &doc.node(id)?.children)?,
        (None, None) => Document::from_element(word_element(expected)),
    };
    // Property elements need their ordinary run/paragraph context for schema ordering.
    let mut replacement = shell(doc, Some(target), &[])?;
    let replacement_root = replacement.import(properties, properties.root, Some(replacement.root))?;
    if expected == "pPr" {
        for id in previous.node(previous.root)?.children.clone() { if matches!(name(&previous, id), Some("rPr" | "sectPr")) { previous.remove(id)?; } }
        if let Some(old) = old {
            for &id in &doc.node(old)?.children {
                if matches!(name(doc, id), Some("rPr" | "sectPr")) { replacement.import(doc, id, Some(replacement_root))?; }
            }
        }
    }
    let kind = format!("{expected}Change");
    let history = attrs.element(&kind, Some(&previous))?;
    let index = crate::schema::insertion_position(&mut replacement, replacement_root, W, &kind)?;
    attach(&mut replacement, replacement_root, index, &history)?;
    let index = old.map(|id| doc.position(id).map(|(_, i)| i)).transpose()?.unwrap_or(0);
    let current = doc.import_at(&replacement, replacement_root, target, index)?;
    if let Some(old) = old { doc.remove(old)?; }
    Ok(child(doc, current, &kind).unwrap())
}
pub fn tracked_replace(doc: &mut Document, scope: usize, range_root: usize, start: usize, end: usize, text: &str, attrs: &mut Metadata) -> Result<Vec<usize>> {
    let (parts, chunks, multiline) = prepare_replacement(doc, range_root, start, end, text)?;
    apply_replacement(doc, scope, parts, chunks, multiline, attrs)
}
/// Record prepared selections as deletions and each chunk's nodes as an insertion, splitting and joining paragraphs between chunks.
pub fn apply_replacement(doc: &mut Document, scope: usize, parts: Vec<Selection>, chunks: Vec<Vec<Document>>, multiline: bool, attrs: &mut Metadata) -> Result<Vec<usize>> {
    inside(doc, scope, parts[0].paragraph)?;
    if multiline && name(doc, scope) == Some("p") { return Err(unsupported("Tracked paragraph creation requires a containing Story, not paragraph-only scope")); }
    let deletions = parts.iter().map(|p| if p.start < p.end { attrs.element("del", None).map(Some) } else { Ok(None) }).collect::<Result<Vec<_>>>()?;
    let old_breaks = (1..parts.len()).map(|_| attrs.element("del", None)).collect::<Result<Vec<_>>>()?;
    let insertions = chunks.iter().map(|chunk| if chunk.is_empty() { Ok(None) } else { attrs.element("ins", None).map(Some) }).collect::<Result<Vec<_>>>()?;
    let new_breaks = (1..chunks.len()).map(|_| attrs.element("ins", None)).collect::<Result<Vec<_>>>()?;
    if !multiline && parts[0].start == parts[0].end && chunks[0].is_empty() { return Ok(Vec::new()); }
    let isolated = parts.into_iter().map(|p| isolate(doc, p, true)).collect::<Result<Vec<_>>>()?;
    let mut created = Vec::new();
    let prefix = if multiline { isolated[0].index - content_start(doc, isolated[0].paragraph)? } else { 0 };
    for (part, deletion) in isolated.iter().zip(&deletions) {
        if let Some(deletion) = deletion {
            let element = attach(doc, part.paragraph, part.index, deletion)?;
            for &run in &part.runs { doc.move_node(run, element, doc.node(element)?.children.len())?; }
            rename_text(doc, &part.runs, true)?;
            created.push(element);
        }
    }
    for (part, mark) in isolated.iter().zip(old_breaks) { created.push(add_mark(doc, part.paragraph, &mark)?); }
    let mut paragraph = isolated[0].paragraph;
    let mut index = if multiline { content_start(doc, paragraph)? + prefix } else { isolated[0].index } + usize::from(deletions[0].is_some());
    for (i, insertion) in insertions.iter().enumerate() {
        if let Some(insertion) = insertion {
            let element = attach(doc, paragraph, index, insertion)?;
            for (at, node) in chunks[i].iter().enumerate() { attach(doc, element, at, node)?; }
            created.push(element);
            index += 1;
        }
        if i < new_breaks.len() {
            let left;
            (left, paragraph) = split_at(doc, paragraph, index, None)?;
            created.push(add_mark(doc, left, &new_breaks[i])?);
            index = content_start(doc, paragraph)?;
        }
    }
    Ok(created)
}

#[pyfunction]
pub fn revision_ids(py: Python<'_>, story: &Story) -> Result<Vec<usize>> { py.detach(|| collect(&*story.xml.read()?, story.element)) }
#[pyfunction]
pub fn revision_info(xml: &Xml, id: usize) -> Result<(String, bool)> {
    let doc = xml.read()?;
    Ok((revision_text(&doc, id)?, boundary_paragraph(&doc, id).is_some()))
}
#[pyfunction]
pub fn revision_properties(xml: &Xml, id: usize) -> Result<(usize, usize)> { property_change(&*xml.read()?, id) }
#[pyfunction]
pub fn revision_apply(py: Python<'_>, xml: &Xml, id: usize, accept: bool) -> Result<()> { py.detach(|| xml.edit(|doc| apply(doc, id, accept))) }
#[pyfunction]
pub fn revisions_apply(py: Python<'_>, story: &Story, accept: bool) -> Result<usize> { py.detach(|| story.xml.edit(|doc| apply_all(doc, story.element, accept))) }
#[pyfunction]
#[pyo3(signature=(story, span, text, author, date=None))]
pub fn revisions_replace(py: Python<'_>, story: &Story, span: &text::Range, text: &str, author: &str, date: Option<&str>) -> Result<Vec<usize>> {
    span.editable()?;
    if !story.xml.same_state(&span.story.xml) { return Err(Error::Invalid("Target is outside this story".into())); }
    py.detach(|| story.xml.edit(|doc| {
        let mut attrs = Metadata::new(doc, author, date)?;
        tracked_replace(doc, story.element, span.story.element, span.start, span.end, text, &mut attrs)
    }))
}
#[pyfunction]
#[pyo3(signature=(story, target, properties, author, date=None))]
pub fn revisions_format(py: Python<'_>, story: &Story, target: usize, properties: &[u8], author: &str, date: Option<&str>) -> Result<usize> {
    let properties = crate::xml::parse_bytes(properties)?;
    py.detach(|| story.xml.edit(|doc| {
        let mut attrs = Metadata::new(doc, author, date)?;
        format(doc, story.element, target, &properties, None, &mut attrs)
    }))
}
