//! Conservative body comparison using the same native revision operations as explicit edits.
use crate::{error::{Error, Result}, package::Package, revisions::{self, Metadata}, text::{self, View, W, W14, XML},
    xml::{Document, NodeKind}};
use pyo3::prelude::*;
use std::collections::{BTreeMap, HashMap};

fn unsupported(message: &str) -> Error { Error::Unsupported(message.into()) }
type Attributes = Vec<(String, String, String)>;
#[derive(PartialEq, Eq)]
enum Key {
    Element((String, String), Attributes, Vec<(String, String)>, Vec<Key>),
    Text(String), Comment(String), Pi(String, String),
}
fn attrs(doc: &Document, id: usize, bookkeeping: bool) -> Result<Attributes> {
    let e = doc.node(id)?.element().ok_or_else(|| Error::Invalid("Expected an XML element".into()))?;
    let mut result = e.attributes.iter().filter(|a| !bookkeeping || !(a.name.uri == W && a.name.local.starts_with("rsid") ||
        a.name.uri == W14 && matches!(a.name.local.as_str(), "paraId" | "textId")))
        .map(|a| (a.name.uri.clone(), a.name.local.clone(), a.value.clone())).collect::<Vec<_>>();
    result.sort();
    Ok(result)
}
fn key(doc: &Document, id: usize, omit: &[&str], bookkeeping: bool, inherited: Option<bool>) -> Result<Key> {
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
        children.push(key(doc, id, &[], bookkeeping, Some(preserve.unwrap_or(false)))?);
    }
    let mut ns = e.namespaces.iter().filter(|(_, uri)| !uri.is_empty()).cloned().collect::<Vec<_>>();
    ns.sort();
    Ok(Key::Element((e.name.uri.clone(), e.name.local.clone()), attrs(doc, id, bookkeeping)?, ns, children))
}
#[derive(Default, PartialEq, Eq)]
struct Properties { attrs: Attributes, children: Vec<Key>, namespaces: Vec<(String, String)> }
fn props(doc: &Document, id: usize, name: &str, omit: &[&str]) -> Result<Properties> {
    let Some(id) = text::child(doc, id, name) else { return Ok(Properties::default()); };
    let Key::Element(_, attrs, namespaces, children) = key(doc, id, omit, false, None)? else { unreachable!() };
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
                    Payload::Xml(key(&doc, doc.root, if text::name(&doc, doc.root) == Some("settings") { &["rsids"] } else { &[] }, false, None)?)
                } else { Payload::Bytes(package.data(&uri)?) };
                result.parts.insert(uri.clone(), (content_type, payload));
            }
        }
        let mut relationships = Vec::new();
        for rel in package.relationships(&uri)? {
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
#[derive(Clone, Copy)]
struct Run { start: usize, end: usize, id: usize }
fn plain(doc: &Document, paragraph: usize) -> Result<(String, Vec<Run>)> {
    let projected = text::paragraph(doc, paragraph, View::Current, false)?;
    if !projected.barriers.is_empty() || projected.spans.iter().zip(&doc.node(paragraph)?.children).any(|(span, &id)| {
        span.element.map_or(!matches!(doc.nodes[id].as_ref().unwrap().kind, NodeKind::Text(_)),
            |id| !matches!(text::name(doc, id), Some("pPr" | "r" | "proofErr")))
    }) { return Err(unsupported("Changed paragraphs must contain ordinary runs and direct properties only")); }
    if doc.node(paragraph)?.children.iter().filter(|&&id| text::name(doc, id) == Some("pPr")).count() > 1 {
        return Err(unsupported("Multiple paragraph-property blocks are unsupported"));
    }
    let mut runs = Vec::new();
    for span in projected.spans {
        let Some(id) = span.element.filter(|&id| text::name(doc, id) == Some("r")) else { continue; };
        let children = &doc.node(id)?.children;
        if children.iter().filter(|&&id| text::name(doc, id) == Some("rPr")).count() > 1 { return Err(unsupported("Multiple run-property blocks are unsupported")); }
        for &child in children {
            let node = doc.node(child)?;
            let Some(e) = node.element() else { continue; };
            let local = text::name(doc, child).unwrap_or("");
            if local == "rPr" { continue; }
            if !matches!(local, "t" | "tab" | "br" | "cr") { return Err(unsupported("Changed paragraphs cannot contain annotations or unsupported run payloads")); }
            if e.attributes.iter().any(|a| !((local == "t" && a.name.uri == XML && a.name.local == "space") ||
                (local == "br" && a.name.uri == W && a.name.local == "type"))) || local != "t" && !node.children.is_empty() {
                return Err(unsupported("Changed paragraphs contain unsupported text/break/tab metadata"));
            }
        }
        if span.start < span.end { runs.push(Run { start: span.start, end: span.end, id }); }
    }
    Ok((projected.text, runs))
}

#[derive(Clone, Copy)]
struct Opcode { equal: bool, a: usize, b: usize, c: usize, d: usize }
/// Longest contiguous matches, recursively split into gaps (the existing no-junk comparison strategy).
fn opcodes(before: &[char], after: &[char]) -> Vec<Opcode> {
    let mut positions: HashMap<char, Vec<usize>> = HashMap::new();
    for (j, &c) in after.iter().enumerate() { positions.entry(c).or_default().push(j); }
    let mut pending = vec![(0, before.len(), 0, after.len())];
    let mut matches = Vec::new();
    while let Some((alo, ahi, blo, bhi)) = pending.pop() {
        let (mut a, mut b, mut size) = (alo, blo, 0);
        let mut lengths = HashMap::<usize, usize>::new();
        for (i, value) in before.iter().enumerate().take(ahi).skip(alo) {
            let mut next = HashMap::new();
            for &j in positions.get(value).into_iter().flatten().filter(|&&j| blo <= j && j < bhi) {
                let length = if j == 0 { 1 } else { lengths.get(&(j - 1)).copied().unwrap_or(0) + 1 };
                next.insert(j, length);
                if length > size { (a, b, size) = (i + 1 - length, j + 1 - length, length); }
            }
            lengths = next;
        }
        if size == 0 { continue; }
        matches.push((a, b, size));
        if alo < a && blo < b { pending.push((alo, a, blo, b)); }
        if a + size < ahi && b + size < bhi { pending.push((a + size, ahi, b + size, bhi)); }
    }
    matches.sort();
    let mut merged: Vec<(usize, usize, usize)> = Vec::new();
    for (a, b, size) in matches {
        if let Some(last) = merged.last_mut().filter(|last| last.0 + last.2 == a && last.1 + last.2 == b) { last.2 += size; }
        else { merged.push((a, b, size)); }
    }
    let mut matches = merged;
    matches.push((before.len(), after.len(), 0));
    let (mut i, mut j) = (0, 0);
    let mut result = Vec::new();
    for (a, c, size) in matches {
        if i < a || j < c { result.push(Opcode { equal: false, a: i, b: a, c: j, d: c }); }
        if size > 0 { result.push(Opcode { equal: true, a, b: a + size, c, d: c + size }); }
        (i, j) = (a + size, c + size);
    }
    result
}

fn replace_inserted_runs(doc: &mut Document, revision: usize, runs: &[Document]) -> Result<()> {
    for id in doc.node(revision)?.children.clone() { doc.remove(id)?; }
    for run in runs { text::attach(doc, revision, doc.node(revision)?.children.len(), run)?; }
    Ok(())
}
fn properties_document(doc: &Document, parent: usize, name: &str, omit: &[&str]) -> Result<Document> {
    let Some(id) = text::child(doc, parent, name) else { return Ok(Document::from_element(text::word_element(name))); };
    let children = doc.node(id)?.children.iter().copied().filter(|&id| !text::name(doc, id).is_some_and(|name| omit.contains(&name))).collect::<Vec<_>>();
    text::shell(doc, Some(id), &children)
}
fn paragraph_diff(doc: &mut Document, scope: usize, original: usize, revised: &Document, right: usize, metadata: &mut Metadata) -> Result<()> {
    if key(doc, original, &[], true, None)? == key(revised, right, &[], true, None)? { return Ok(()); }
    let (before, old_runs) = plain(doc, original)?;
    let (after, new_runs) = plain(revised, right)?;
    if attrs(doc, original, true)? != attrs(revised, right, true)? { return Err(unsupported("Changed paragraph attributes are unsupported")); }
    for name in ["rPr", "sectPr"] {
        let old = text::child(doc, original, "pPr").and_then(|id| text::child(doc, id, name));
        let new = text::child(revised, right, "pPr").and_then(|id| text::child(revised, id, name));
        if old.map(|id| key(doc, id, &[], false, None)).transpose()? != new.map(|id| key(revised, id, &[], false, None)).transpose()? {
            return Err(unsupported("Paragraph-mark and section property differences are unsupported"));
        }
    }
    let before = before.chars().collect::<Vec<_>>();
    let after = after.chars().collect::<Vec<_>>();
    let codes = opcodes(&before, &after);
    let mut formatting = Vec::new();
    for op in codes.iter().filter(|op| op.equal) {
        for old in &old_runs {
            let (start, end) = (op.a.max(old.start), op.b.min(old.end));
            if start >= end { continue; }
            for new in &new_runs {
                let (low, high) = ((op.c + start - op.a).max(new.start), (op.c + end - op.a).min(new.end));
                if low >= high { continue; }
                if attrs(doc, old.id, true)? != attrs(revised, new.id, true)? { return Err(unsupported("Changed attributes on retained runs are unsupported")); }
                if props(doc, old.id, "rPr", &[])? != props(revised, new.id, "rPr", &[])? { formatting.push((low, high, new.id)); }
            }
        }
    }
    for op in codes.iter().rev().filter(|op| !op.equal) {
        let replacement = after[op.c..op.d].iter().collect::<String>();
        let created = revisions::tracked_replace(doc, scope, original, op.a, op.b, &replacement, metadata)?;
        for id in created {
            if text::name(doc, id) != Some("ins") { continue; }
            let runs = new_runs.iter().filter(|run| run.start < op.d && run.end > op.c).map(|run| {
                text::run_text(revised, Some(run.id), &after[run.start.max(op.c)..run.end.min(op.d)].iter().collect::<String>())
            }).collect::<Result<Vec<_>>>()?;
            replace_inserted_runs(doc, id, &runs)?;
        }
    }
    for (start, end, replacement) in formatting.into_iter().rev() {
        let projected = text::paragraph(doc, original, View::Current, false)?;
        let selection = text::preflight(doc, original, &projected, start, end)?;
        let selected = text::isolate(doc, selection, false)?;
        let properties = properties_document(revised, replacement, "rPr", &[])?;
        for run in selected.runs { revisions::format(doc, scope, run, &properties, metadata)?; }
    }
    if props(doc, original, "pPr", &["rPr", "sectPr"])? != props(revised, right, "pPr", &["rPr", "sectPr"])? {
        let properties = properties_document(revised, right, "pPr", &["rPr", "sectPr"])?;
        revisions::format(doc, scope, original, &properties, metadata)?;
    }
    Ok(())
}
fn multiline(doc: &mut Document, scope: usize, original: &[usize], revised: &Document, new: &[usize], metadata: &mut Metadata) -> Result<()> {
    if original.is_empty() || new.is_empty() { return Err(unsupported("Comparison requires at least one paragraph in each document")); }
    let properties = props(doc, original[0], "pPr", &[])?;
    let mut run_properties = None;
    let mut template = None;
    let mut before = Vec::new();
    let mut after = Vec::new();
    for (source, ids, texts) in [(doc as &Document, original, &mut before), (revised, new, &mut after)] {
        for &id in ids {
            if props(source, id, "pPr", &[])? != properties || !attrs(source, id, true)?.is_empty() {
                return Err(unsupported("Paragraph-count changes require matching uniform paragraph properties"));
            }
            if text::child(source, id, "pPr").is_some_and(|p| source.nodes[p].as_ref().unwrap().children.iter()
                .any(|&id| matches!(text::name(source, id), Some("rPr" | "sectPr")))) {
                return Err(unsupported("Paragraph-count changes with paragraph-mark or section properties are unsupported"));
            }
            let (text, runs) = plain(source, id)?;
            texts.push(text);
            for run in runs {
                let current = props(source, run.id, "rPr", &[])?;
                if !attrs(source, run.id, true)?.is_empty() || run_properties.as_ref().is_some_and(|p| *p != current) {
                    return Err(unsupported("Paragraph-count changes require matching uniform run properties"));
                }
                if template.is_none() {
                    let properties = text::child(source, run.id, "rPr").into_iter().collect::<Vec<_>>();
                    template = Some(text::shell(source, Some(run.id), &properties)?);
                }
                run_properties = Some(current);
            }
        }
    }
    let before = before.join("\n").chars().collect::<Vec<_>>();
    let after = after.join("\n").chars().collect::<Vec<_>>();
    let start = before.iter().zip(&after).take_while(|(a, b)| a == b).count();
    let end = before[start..].iter().rev().zip(after[start..].iter().rev()).take_while(|(a, b)| a == b).count();
    let offset = text::paragraphs(doc, scope, View::Current)?.iter().find(|row| row.paragraph == Some(original[0]))
        .ok_or_else(|| Error::Invalid("Paragraph is outside the comparison story".into()))?.position;
    let created = revisions::tracked_replace(doc, scope, scope, offset + start, offset + before.len() - end,
        &after[start..after.len() - end].iter().collect::<String>(), metadata)?;
    for id in created {
        if text::name(doc, id) == Some("ins") && text::boundary_paragraph(doc, id).is_none() {
            let value = text::paragraph(doc, id, View::Current, false)?.text;
            let (source, run) = template.as_ref().map(|s| (s, Some(s.root))).unwrap_or((doc, None));
            let run = text::run_text(source, run, &value)?;
            replace_inserted_runs(doc, id, &[run])?;
        }
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
        if text::descendants(&doc, doc.root).iter().any(|&id| text::revision_name(&doc, id).is_some()) {
            return Err(unsupported("Resolve existing revisions before comparing documents"));
        }
    }
    let result = Package::from_bytes(&original.to_bytes()?)?;
    let tree = result.xml(&main)?;
    let other = revised.xml(&main)?;
    let revised = other.read()?;
    tree.edit(|doc| {
        let scope = doc.root;
        let mut metadata = Metadata::new(doc, author, date)?;
        if text::name(doc, doc.root) != Some("document") || text::name(&revised, revised.root) != Some("document") ||
            attrs(doc, doc.root, true)? != attrs(&revised, revised.root, true)? {
            return Err(unsupported("Changed document-root attributes or non-Transitional vocabulary are unsupported"));
        }
        let old_body = text::child(doc, doc.root, "body").ok_or_else(|| Error::Invalid("Expected a document body".into()))?;
        let new_body = text::child(&revised, revised.root, "body").ok_or_else(|| Error::Invalid("Expected a document body".into()))?;
        for (source, body) in [(doc as &Document, old_body), (&*revised, new_body)] {
            if source.node(source.root)?.children.iter().any(|&id| source.nodes[id].as_ref().unwrap().element().is_some() && id != body) {
                return Err(unsupported("Extra document-root content is unsupported"));
            }
        }
        if attrs(doc, old_body, true)? != attrs(&revised, new_body, true)? { return Err(unsupported("Changed body attributes are unsupported")); }
        let elements = |source: &Document, id: usize| -> Vec<usize> { source.nodes[id].as_ref().unwrap().children.iter().copied()
            .filter(|&id| source.nodes[id].as_ref().unwrap().element().is_some()).collect() };
        let old = elements(doc, old_body);
        let new = elements(&revised, new_body);
        if old.len() == new.len() {
            for (left, right) in old.into_iter().zip(new) {
                if text::name(doc, left) == Some("p") && text::name(&revised, right) == Some("p") {
                    paragraph_diff(doc, scope, left, &revised, right, &mut metadata)?;
                } else if key(doc, left, &[], true, None)? != key(&revised, right, &[], true, None)? {
                    return Err(unsupported("Changed opaque body content or section properties are unsupported"));
                }
            }
        } else {
            for (source, ids) in [(doc as &Document, &old), (&*revised, &new)] {
                if ids.iter().any(|&id| !matches!(text::name(source, id), Some("p" | "sectPr"))) {
                    return Err(unsupported("Paragraph-count changes around opaque body content are unsupported"));
                }
            }
            let sections = |source: &Document, ids: &[usize]| -> Result<Vec<Key>> { ids.iter().filter(|&&id| text::name(source, id) == Some("sectPr"))
                .map(|&id| key(source, id, &[], false, None)).collect() };
            if sections(doc, &old)? != sections(&revised, &new)? { return Err(unsupported("Changed section properties are unsupported")); }
            multiline(doc, scope, &old.into_iter().filter(|&id| text::name(doc, id) == Some("p")).collect::<Vec<_>>(),
                &revised, &new.into_iter().filter(|&id| text::name(&revised, id) == Some("p")).collect::<Vec<_>>(), &mut metadata)?;
        }
        Ok(())
    })?;
    Ok(result)
}
#[pyfunction]
#[pyo3(signature=(original, revised, author, date=None))]
pub fn compare(py: Python<'_>, original: &Package, revised: &Package, author: &str, date: Option<&str>) -> Result<Package> {
    py.detach(|| compare_packages(original, revised, author, date))
}
