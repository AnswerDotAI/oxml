//! Bookmark markers and hyperlink wrappers over native story ranges.
use crate::{
    error::{Error, Result},
    package::{Package, Relationship},
    text::{self, Range, Story, View, W},
    xml::{Document, Xml},
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
fn selection(story: &Story, span: &Range) -> Result<text::Selection> {
    editable(story)?;
    span.editable()?;
    if !story.xml.same_state(&span.story.xml) {
        return Err(invalid("Range belongs to another XML tree"));
    }
    let doc = story.xml.read()?;
    let selected = span.selection(&doc)?;
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
        doc.node(self.story.element)?;
        Ok(text::descendants(&doc, self.story.element)
            .into_iter()
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
        let selected = selection(&self.story, span)?;
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
            let stop = isolated.runs.last().map(|&id| doc.position(id).map(|p| p.1 + 1)).transpose()?.unwrap_or(isolated.index);
            text::attach(doc, isolated.paragraph, stop, &end)?;
            text::attach(doc, isolated.paragraph, isolated.index, &start)
        })?;
        Ok(Bookmark { bookmarks: self.clone(), element })
    }
}
#[pyclass(name = "NativeBookmark", module = "oxml._core", from_py_object)]
#[derive(Clone)]
pub struct Bookmark {
    bookmarks: Bookmarks,
    element: usize,
}
impl Bookmark {
    fn end(&self) -> Result<usize> {
        let doc = self.bookmarks.story.xml.read()?;
        let ident = integer(doc.node(self.element)?.element().unwrap().attribute(W, "id"))?;
        let mut starts = Vec::new();
        let mut ends = Vec::new();
        for id in text::descendants(&doc, self.bookmarks.story.element) {
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
    #[pyo3(signature=(text=None))]
    pub fn reference(&self, text: Option<&str>) -> Result<Xml> {
        let name = self.name()?.ok_or_else(|| invalid("Bookmark name is missing"))?;
        if !name.chars().next().is_some_and(|c| c == '_' || c.is_alphabetic()) || !name.chars().all(|c| c == '_' || c.is_alphanumeric()) {
            return Err(invalid("REF construction requires a simple identifier bookmark name"));
        }
        let text = match text {
            Some(s) => s.to_string(),
            None => {
                let range = self.range()?;
                range.story.text_native()?.chars().skip(range.start).take(range.end - range.start).collect()
            }
        };
        let mut field = Document::from_element(text::word_element("fldSimple"));
        field.set_attribute(field.root, W, "instr", &format!(" REF {name} "), Some("w"))?;
        field.set_attribute(field.root, W, "dirty", "true", Some("w"))?;
        let run = text::run_text(&field, None, &text)?;
        field.import(&run, run.root, Some(field.root))?;
        Ok(Xml::from_document(field))
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
    fn owner(&self) -> Result<String> {
        let uri = self.package.owner(&self.story.xml)?;
        if let Some(claimed) = &self.claimed_uri {
            if self.package.resolve_part(claimed)? != uri {
                return Err(invalid("Story does not belong to the claimed package part"));
            }
        }
        Ok(uri)
    }
}
#[pymethods]
impl Hyperlinks {
    #[new]
    #[pyo3(signature=(package,story,part_uri=None))]
    pub fn new(package: Package, story: Story, part_uri: Option<String>) -> Self {
        Self { package, story, claimed_uri: part_uri }
    }
    pub fn items(&self) -> Result<Vec<Hyperlink>> {
        self.owner()?;
        let doc = self.story.xml.read()?;
        doc.node(self.story.element)?;
        Ok(text::descendants(&doc, self.story.element)
            .into_iter()
            .filter(|&id| text::name(&doc, id) == Some("hyperlink"))
            .map(|element| Hyperlink { hyperlinks: self.clone(), element })
            .collect())
    }
    pub fn add(&self, span: &Range, target: &str) -> Result<Hyperlink> {
        let selected = selection(&self.story, span)?;
        if span.start == span.end {
            return Err(invalid("A hyperlink requires a nonempty text range"));
        }
        let uri = self.owner()?;
        if target.is_empty() {
            return Err(invalid("Hyperlink target requires a nonempty string"));
        }
        let mut link = Document::from_element(text::word_element("hyperlink"));
        if let Some(anchor) = target.strip_prefix('#') {
            name_value(anchor)?;
            link.set_attribute(link.root, W, "anchor", anchor, Some("w"))?;
        } else {
            let mut package = self.package.lock()?;
            let existing = package
                .relationships(&uri)?
                .into_iter()
                .find(|r| matches!(r.kind.as_str(), HYPERLINK | STRICT_HYPERLINK) && r.mode == "External" && r.target == target);
            let ident = match existing {
                Some(r) => r.id,
                None => package.add_relationship(&uri, HYPERLINK, target, "External", None)?,
            };
            link.set_attribute(link.root, R, "id", &ident, Some("r"))?;
        }
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
        if let Some(rel) = relation.filter(|r| matches!(r.kind.as_str(), HYPERLINK | STRICT_HYPERLINK)) {
            let used = {
                let doc = xml.read()?;
                doc.element_ids()
                    .into_iter()
                    .any(|id| doc.node(id).unwrap().element().unwrap().attributes.iter().any(|a| a.value == rel.id))
            };
            if !used {
                self.hyperlinks.package.remove_relationship(&uri, &rel.id)?;
            }
        }
        Ok(())
    }
}
