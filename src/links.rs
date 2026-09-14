//! Bookmark markers and hyperlink wrappers over native story ranges.
use crate::{
    error::{Error, Result},
    package::{Package, Relationship},
    text::{self, Range, Story, View, W},
    xml::{self, Document, Xml},
};
use pyo3::prelude::*;
use std::collections::HashSet;

const R: &str = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
const HYPERLINK: &str = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink";
const STRICT_HYPERLINK: &str = "http://purl.oclc.org/ooxml/officeDocument/relationships/hyperlink";
fn invalid(message: &str) -> Error {
    Error::Invalid(message.into())
}
fn editable(story: &Story) -> Result<()> {
    if story.view != View::Current {
        return Err(text::unsupported("Original text view is read-only"));
    }
    Ok(())
}
fn selection(story: &Story, span: &Range, anchor: bool) -> Result<text::Selection> {
    editable(story)?;
    span.editable()?;
    if !story.xml.same_state(&span.story.xml) {
        return Err(invalid("Range belongs to another XML tree"));
    }
    let doc = story.xml.read()?;
    let selected = span.selection(&doc, anchor)?;
    text::inside(&doc, story.element, selected.paragraph)?;
    Ok(selected)
}
fn name_value(name: &str) -> Result<()> {
    if name.is_empty() || name.chars().any(char::is_whitespace) {
        return Err(invalid("Bookmark name requires a nonempty string without whitespace"));
    }
    Ok(())
}
fn integer(value: Option<&str>) -> Result<i64> {
    value.unwrap_or("").parse().map_err(|_| invalid("Invalid bookmark ID"))
}
fn unique(mut values: impl Iterator<Item = usize>, label: &str) -> Result<Option<usize>> {
    let first = values.next();
    if values.next().is_some() {
        return Err(Error::Invalid(format!("Ambiguous {label}")));
    }
    Ok(first)
}
fn marker(local: &str, ident: &str, name: Option<&str>) -> Result<Document> {
    let mut result = Document::from_element(text::word_element(local));
    result.set_attribute(result.root, W, "id", ident, Some("w"))?;
    if let Some(name) = name {
        result.set_attribute(result.root, W, "name", name, Some("w"))?;
    }
    Ok(result)
}
#[pyfunction]
pub fn bookmark_name(text: &str) -> String {
    let prefix = if text.chars().next().is_some_and(char::is_alphabetic) { "" } else { "B" };
    prefix.chars().chain(text.chars().map(|c| if c == '_' || c.is_alphanumeric() { c } else { '_' })).take(40).collect()
}

#[pyfunction]
#[pyo3(signature=(instruction, text, properties=None))]
pub fn field(instruction: &str, text: &str, properties: Option<Vec<u8>>) -> Result<Xml> {
    let mut field = Document::from_element(text::word_element("fldSimple"));
    field.set_attribute(field.root, W, "instr", &format!(" {} ", instruction.trim()), Some("w"))?;
    field.set_attribute(field.root, W, "dirty", "true", Some("w"))?;
    let mut run = text::run_text(&field, None, text)?;
    if let Some(bytes) = properties {
        let props = xml::parse_bytes(&bytes)?;
        if text::name(&props, props.root) != Some("rPr") { return Err(invalid("Field formatting requires w:rPr")); }
        run.import_at(&props, props.root, run.root, 0)?;
    }
    field.import(&run, run.root, Some(field.root))?;
    Ok(Xml::from_document(field))
}

fn ref_field(name: &str, text: &str, switches: &str) -> Result<Xml> {
    if !name.chars().next().is_some_and(|c| c == '_' || c.is_alphabetic()) || !name.chars().all(|c| c == '_' || c.is_alphanumeric()) {
        return Err(invalid("REF construction requires a simple identifier bookmark name"));
    }
    field(&format!("REF {name} {switches}"), text, None)
}

#[pyclass(name = "NativeBookmarks", module = "oxml._core", from_py_object)]
#[derive(Clone)]
pub struct Bookmarks {
    story: Story,
}
#[pymethods]
impl Bookmarks {
    #[new]
    pub fn new(story: Story) -> Self {
        Self { story }
    }
    pub fn items(&self) -> Result<Vec<Bookmark>> {
        let doc = self.story.xml.read()?;
        let nodes = doc.descendants(self.story.element)?;
        Ok(nodes
            .filter(|&id| text::name(&doc, id) == Some("bookmarkStart"))
            .map(|element| Bookmark { bookmarks: self.clone(), element })
            .collect())
    }
    pub fn find(&self, name: &str) -> Result<Option<Bookmark>> {
        let mut found = None;
        for bookmark in self.items()? {
            if bookmark.name()?.as_deref() != Some(name) {
                continue;
            }
            if found.is_some() {
                return Err(invalid("Ambiguous bookmark name"));
            }
            found = Some(bookmark);
        }
        Ok(found)
    }
    pub fn add(&self, span: &Range, name: &str) -> Result<Bookmark> {
        let selected = selection(&self.story, span, true)?;
        name_value(name)?;
        let ident = {
            let doc = self.story.xml.read()?;
            let mut used = HashSet::new();
            for id in doc.element_ids() {
                if !matches!(text::name(&doc, id), Some("bookmarkStart" | "bookmarkEnd")) {
                    continue;
                }
                let e = doc.node(id)?.element().unwrap();
                if e.attribute(W, "name") == Some(name) {
                    return Err(invalid("Bookmark name already exists"));
                }
                used.insert(integer(e.attribute(W, "id"))?);
            }
            (0..).find(|id| !used.contains(id)).unwrap().to_string()
        };
        let (start, end) = (marker("bookmarkStart", &ident, Some(name))?, marker("bookmarkEnd", &ident, None)?);
        let element = self.story.xml.edit(|doc| {
            let isolated = text::isolate(doc, selected, false)?;
            text::attach(doc, isolated.paragraph, isolated.stop, &end)?;
            text::attach(doc, isolated.paragraph, isolated.index, &start)
        })?;
        Ok(Bookmark { bookmarks: self.clone(), element })
    }
    #[pyo3(signature=(name, text=None, switches=""))]
    pub fn reference(&self, name: &str, text: Option<&str>, switches: &str) -> Result<Xml> {
        let text = match text {
            Some(s) => s.to_string(),
            None => self.find(name)?.ok_or_else(|| Error::Missing(format!("No bookmark named {name}")))?.text()?,
        };
        ref_field(name, &text, switches)
    }
}
#[pyclass(name = "NativeBookmark", module = "oxml._core", from_py_object)]
#[derive(Clone)]
pub struct Bookmark {
    bookmarks: Bookmarks,
    element: usize,
}
impl Bookmark {
    fn text(&self) -> Result<String> {
        let range = self.range()?;
        Ok(range.story.text_native()?.chars().skip(range.start).take(range.end - range.start).collect())
    }
    fn end(&self) -> Result<usize> {
        let doc = self.bookmarks.story.xml.read()?;
        let ident = integer(doc.node(self.element)?.element().unwrap().attribute(W, "id"))?;
        let mut starts = Vec::new();
        let mut ends = Vec::new();
        for id in doc.descendants(self.bookmarks.story.element)? {
            if !matches!(text::name(&doc, id), Some("bookmarkStart" | "bookmarkEnd")) {
                continue;
            }
            if integer(doc.node(id)?.element().unwrap().attribute(W, "id"))? != ident {
                continue;
            }
            if text::name(&doc, id) == Some("bookmarkStart") {
                starts.push(id);
            } else {
                ends.push(id);
            }
        }
        if starts.len() != 1 {
            return Err(invalid("Ambiguous bookmark ID"));
        }
        unique(ends.into_iter(), "bookmark end")?.ok_or_else(|| invalid("Bookmark end is missing from this story"))
    }
}
#[pymethods]
impl Bookmark {
    #[getter]
    pub fn xml(&self) -> Xml {
        self.bookmarks.story.xml.clone()
    }
    #[getter]
    pub fn element_id(&self) -> usize {
        self.element
    }
    #[getter]
    pub fn name(&self) -> Result<Option<String>> {
        self.xml().attribute(self.element, W, "name")
    }
    #[getter]
    pub fn id(&self) -> Result<i64> {
        integer(self.xml().attribute(self.element, W, "id")?.as_deref())
    }
    #[getter]
    pub fn range(&self) -> Result<Range> {
        let story = &self.bookmarks.story;
        story.range_native(story.position_native(self.element)?, story.position_native(self.end()?)?)
    }
    pub fn remove(&self) -> Result<()> {
        editable(&self.bookmarks.story)?;
        let end = self.end()?;
        self.range()?;
        self.xml().edit(|doc| {
            doc.remove(end)?;
            doc.remove(self.element)
        })
    }
    #[pyo3(signature=(text=None, switches=""))]
    pub fn reference(&self, text: Option<&str>, switches: &str) -> Result<Xml> {
        let name = self.name()?.ok_or_else(|| invalid("Bookmark name is missing"))?;
        let text = match text {
            Some(s) => s.to_string(),
            None => self.text()?,
        };
        ref_field(&name, &text, switches)
    }
}

#[pyclass(name = "NativeHyperlinks", module = "oxml._core", from_py_object)]
#[derive(Clone)]
pub struct Hyperlinks {
    package: Package,
    story: Story,
    claimed_uri: Option<String>,
}
impl Hyperlinks {
    fn shell(&self, target: &str, part_uri: Option<&str>) -> Result<Document> {
        if target.is_empty() { return Err(invalid("Hyperlink target requires a nonempty string")); }
        let mut link = Document::from_element(text::word_element("hyperlink"));
        if let Some(anchor) = target.strip_prefix('#') {
            name_value(anchor)?;
            link.set_attribute(link.root, W, "anchor", anchor, Some("w"))?;
        } else {
            let id = self.external_id(target, part_uri)?;
            link.set_attribute(link.root, R, "id", &id, Some("r"))?;
        }
        Ok(link)
    }
    fn owner(&self) -> Result<String> {
        let uri = self.package.owner(&self.story.xml)?;
        if let Some(claimed) = &self.claimed_uri {
            if self.package.resolve_part(claimed)? != uri {
                return Err(invalid("Story does not belong to the claimed package part"));
            }
        }
        Ok(uri)
    }
    fn find_external(&self, uri: &str, target: &str) -> Result<Option<String>> {
        let found = self.package.lock()?.relationships(uri)?.into_iter()
            .find(|r| matches!(r.kind.as_str(), HYPERLINK | STRICT_HYPERLINK) && r.mode == "External" && r.target == target);
        Ok(found.map(|r| r.id))
    }
    fn external_relationship(&self, uri: &str, target: &str) -> Result<String> {
        if let Some(id) = self.find_external(uri, target)? { return Ok(id); }
        self.package.lock()?.add_relationship(uri, HYPERLINK, target, "External", None)
    }
    fn references(&self, ident: &str) -> Result<usize> {
        let doc = self.story.xml.read()?;
        Ok(doc.element_ids().into_iter().filter(|&id| doc.node(id).unwrap().element().unwrap().attributes.iter().any(|a| a.value == ident)).count())
    }
    fn release(&self, uri: &str, relation: Option<Relationship>) -> Result<()> {
        let Some(rel) = relation.filter(|r| matches!(r.kind.as_str(), HYPERLINK | STRICT_HYPERLINK)) else { return Ok(()) };
        if self.references(&rel.id)? == 0 {
            self.package.remove_relationship(uri, &rel.id)?;
        }
        Ok(())
    }
}
#[pymethods]
impl Hyperlinks {
    #[pyo3(signature=(target, children, part_uri=None))]
    pub fn build(&self, target: &str, children: Vec<Vec<u8>>, part_uri: Option<&str>) -> Result<Xml> {
        let children = children.iter().map(|bytes| xml::parse_bytes(bytes)).collect::<Result<Vec<_>>>()?;
        let mut link = self.shell(target, part_uri)?;
        for child in children { link.import(&child, child.root, Some(link.root))?; }
        Ok(Xml::from_document(link))
    }
    #[pyo3(signature=(target, part_uri=None))]
    pub fn external_id(&self, target: &str, part_uri: Option<&str>) -> Result<String> {
        if target.is_empty() { return Err(invalid("Hyperlink target requires a nonempty string")); }
        let uri = match part_uri { Some(uri) => self.package.resolve_part(uri)?, None => self.owner()? };
        self.external_relationship(&uri, target)
    }
    #[new]
    #[pyo3(signature=(package,story,part_uri=None))]
    pub fn new(package: Package, story: Story, part_uri: Option<String>) -> Self {
        Self { package, story, claimed_uri: part_uri }
    }
    pub fn items(&self) -> Result<Vec<Hyperlink>> {
        self.owner()?;
        let doc = self.story.xml.read()?;
        let nodes = doc.descendants(self.story.element)?;
        Ok(nodes
            .filter(|&id| text::name(&doc, id) == Some("hyperlink"))
            .map(|element| Hyperlink { hyperlinks: self.clone(), element })
            .collect())
    }
    pub fn add(&self, span: &Range, target: &str) -> Result<Hyperlink> {
        let selected = selection(&self.story, span, false)?;
        if span.start == span.end {
            return Err(invalid("A hyperlink requires a nonempty text range"));
        }
        self.owner()?;
        let link = self.shell(target, None)?;
        let element = self.story.xml.edit(|doc| {
            let isolated = text::isolate(doc, selected, false)?;
            let stop = doc.position(*isolated.runs.last().unwrap())?.1 + 1;
            let selected = doc.node(isolated.paragraph)?.children[isolated.index..stop].to_vec();
            let element = text::attach(doc, isolated.paragraph, isolated.index, &link)?;
            for id in selected {
                doc.move_node(id, element, doc.node(element)?.children.len())?;
            }
            Ok(element)
        })?;
        Ok(Hyperlink { hyperlinks: self.clone(), element })
    }
}
#[pyclass(name = "NativeHyperlink", module = "oxml._core", from_py_object)]
#[derive(Clone)]
pub struct Hyperlink {
    hyperlinks: Hyperlinks,
    element: usize,
}
impl Hyperlink {
    fn relationship(&self) -> Result<Option<Relationship>> {
        let ident = self.xml().attribute(self.element, R, "id")?;
        let Some(ident) = ident else { return Ok(None) };
        let uri = self.hyperlinks.owner()?;
        Ok(self.hyperlinks.package.lock()?.relationships(&uri)?.into_iter().find(|r| r.id == ident))
    }
}
#[pymethods]
impl Hyperlink {
    #[getter]
    pub fn xml(&self) -> Xml {
        self.hyperlinks.story.xml.clone()
    }
    #[getter]
    pub fn element_id(&self) -> usize {
        self.element
    }
    #[getter]
    pub fn anchor(&self) -> Result<Option<String>> {
        self.xml().attribute(self.element, W, "anchor")
    }
    #[getter]
    pub fn text(&self) -> Result<String> {
        Ok(text::paragraph(&*self.xml().read()?, self.element, self.hyperlinks.story.view, false)?.text)
    }
    #[getter]
    pub fn target(&self) -> Result<Option<String>> {
        let relationship = self.relationship()?;
        let Some(rel) = relationship else {
            if self.xml().attribute(self.element, R, "id")?.is_some() {
                return Err(Error::Missing("Hyperlink relationship is missing".into()));
            }
            return Ok(self.anchor()?.map(|a| format!("#{a}")));
        };
        if !matches!(rel.kind.as_str(), HYPERLINK | STRICT_HYPERLINK) {
            return Err(invalid("Hyperlink ID references a different relationship type"));
        }
        if rel.mode == "External" {
            return Ok(Some(rel.target));
        }
        let target = self.hyperlinks.package.relationship_target(&self.hyperlinks.owner()?, &rel.target)?;
        Ok(Some(match rel.target.split_once('#') {
            Some((_, fragment)) => format!("{target}#{fragment}"),
            None => target,
        }))
    }
    #[setter]
    pub fn set_target(&self, target: &str) -> Result<()> {
        editable(&self.hyperlinks.story)?;
        if target.is_empty() {
            return Err(invalid("Hyperlink target requires a nonempty string"));
        }
        let uri = self.hyperlinks.owner()?;
        let previous = self.relationship()?;
        let xml = self.xml();
        if let Some(anchor) = target.strip_prefix('#') {
            name_value(anchor)?;
            xml.edit(|doc| {
                doc.remove_attribute(self.element, R, "id")?;
                doc.set_attribute(self.element, W, "anchor", anchor, Some("w"))
            })?;
        } else {
            let ident = match self.hyperlinks.find_external(&uri, target)? {
                Some(id) => id,
                None => match &previous {
                    Some(rel) if matches!(rel.kind.as_str(), HYPERLINK | STRICT_HYPERLINK) && rel.mode == "External" && self.hyperlinks.references(&rel.id)? == 1 => {
                        let mut package = self.hyperlinks.package.lock()?;
                        package.remove_relationship(&uri, &rel.id)?;
                        package.add_relationship(&uri, HYPERLINK, target, "External", Some(&rel.id))?
                    }
                    _ => self.hyperlinks.external_relationship(&uri, target)?,
                },
            };
            xml.edit(|doc| {
                doc.remove_attribute(self.element, W, "anchor")?;
                doc.set_attribute(self.element, R, "id", &ident, Some("r"))
            })?;
        }
        self.hyperlinks.release(&uri, previous)
    }
    pub fn remove(&self) -> Result<()> {
        editable(&self.hyperlinks.story)?;
        let uri = self.hyperlinks.owner()?;
        let relation = self.relationship()?;
        let xml = self.xml();
        let (parent, index) = {
            let doc = xml.read()?;
            text::inside(&doc, self.hyperlinks.story.element, self.element)?;
            let (parent, index) = doc.position(self.element)?;
            let parent = parent
                .filter(|&p| text::name(&doc, p) == Some("p"))
                .ok_or_else(|| text::unsupported("Only direct paragraph hyperlinks can be unwrapped"))?;
            text::context(&doc, parent)?;
            (parent, index)
        };
        xml.edit(|doc| {
            let children = doc.node(self.element)?.children.clone();
            for (offset, id) in children.into_iter().enumerate() {
                doc.move_node(id, parent, index + offset)?;
            }
            doc.remove(self.element)
        })?;
        self.hyperlinks.release(&uri, relation)?;
        Ok(())
    }
}
