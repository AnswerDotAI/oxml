//! Comment bodies, anchors and modern reply metadata over the same native package.
use crate::{
    error::{Error, Result},
    package::{Package, PackageData},
    package_schema::{declared_uri, story_nodes},
    revisions::timestamp,
    schema::{insertion_position, schema},
    text::{self, Range, Story, View, W, W14},
    xml::{Document, Element, Name, NodeKind, Xml},
};
use pyo3::prelude::*;
use std::collections::{HashMap, HashSet};

const PARTS: [&str; 4] =
    ["WordprocessingCommentsPart", "WordprocessingCommentsExPart", "WordprocessingCommentsIdsPart", "WordCommentsExtensiblePart"];
fn invalid(message: &str) -> Error {
    Error::Invalid(message.into())
}
fn ns(prefix: &str) -> &'static str {
    schema()["namespaces"][prefix].as_str().unwrap()
}
fn attr<'a>(doc: &'a Document, id: usize, uri: &str, local: &str) -> Option<&'a str> {
    doc.node(id).ok()?.element()?.attribute(uri, local)
}
fn number(value: Option<&str>) -> Result<i64> {
    value.unwrap_or("").parse().map_err(|_| invalid("Invalid comment ID"))
}
fn last(doc: &Document, id: usize) -> Option<usize> {
    text::descendants(doc, id).into_iter().filter(|&n| text::name(doc, n) == Some("p")).last()
}
fn para_key(doc: &Document, id: usize) -> String {
    last(doc, id).and_then(|p| attr(doc, p, W14, "paraId")).unwrap_or("").to_uppercase()
}
fn next_id(values: impl IntoIterator<Item = String>, hex: bool) -> Result<String> {
    let used = values
        .into_iter()
        .map(|v| i64::from_str_radix(&v, if hex { 16 } else { 10 }).map_err(|_| invalid("Invalid comment linkage ID")))
        .collect::<Result<HashSet<_>>>()?;
    let value = (if hex { 1 } else { 0 }..).find(|i| !used.contains(i)).unwrap();
    if hex && value >= 0x80000000 {
        return Err(invalid("No available paragraph/durable ID"));
    }
    Ok(if hex { format!("{value:08X}") } else { value.to_string() })
}
fn element(prefix: &str, local: &str, attributes: &[(&str, &str)]) -> Result<Document> {
    let uri = ns(prefix);
    let mut doc = Document::from_element(Element {
        name: Name { uri: uri.into(), local: local.into(), prefix: prefix.into() },
        attributes: Vec::new(),
        namespaces: vec![("".into(), "".into()), ("xml".into(), text::XML.into()), (prefix.into(), uri.into())],
    });
    for &(local, value) in attributes {
        doc.set_attribute(doc.root, uri, local, value, Some(prefix))?;
    }
    Ok(doc)
}
fn append(xml: &Xml, source: &Document) -> Result<usize> {
    xml.edit(|doc| {
        let name = &source.node(source.root)?.element().unwrap().name;
        let index = insertion_position(doc, doc.root, &name.uri, &name.local)?;
        text::attach(doc, doc.root, index, source)
    })
}
fn paragraph_id(doc: &mut Document, id: usize, value: &str) -> Result<()> {
    let prefix = doc.set_attribute_ns(id, W14, "paraId", value, "w14")?;
    let mut ignorable = attr(doc, id, ns("mc"), "Ignorable").unwrap_or("").split_whitespace().map(str::to_string).collect::<Vec<_>>();
    if !ignorable.contains(&prefix) {
        ignorable.push(prefix);
        doc.set_attribute_ns(id, ns("mc"), "Ignorable", &ignorable.join(" "), "mc")?;
    }
    Ok(())
}
struct Roots([Option<Xml>; 4]);
impl Roots {
    fn load(package: &mut PackageData) -> Result<Self> {
        let mut roots = [None, None, None, None];
        for (i, name) in PARTS.iter().enumerate() {
            if let Some(uri) = declared_uri(package, name, false)? {
                roots[i] = Some(package.load_xml(&uri)?);
            }
        }
        Ok(Self(roots))
    }
    fn values(&self, uris: &[&str], locals: &[&str]) -> Result<Vec<String>> {
        let mut values = Vec::new();
        for xml in self.0.iter().flatten() {
            let doc = xml.read()?;
            for id in doc.element_ids() {
                for a in &doc.node(id)?.element().unwrap().attributes {
                    if uris.contains(&a.name.uri.as_str()) && locals.contains(&a.name.local.as_str()) {
                        values.push(a.value.clone());
                    }
                }
            }
        }
        Ok(values)
    }
    fn para_ids(&self) -> Result<Vec<String>> {
        self.values(&[W14, ns("w15"), ns("w16cid")], &["paraId", "paraIdParent"])
    }
    fn record(&self, index: usize, prefix: &str, key: &str, value: Option<&str>) -> Result<Option<usize>> {
        let (Some(xml), Some(value)) = (&self.0[index], value) else { return Ok(None) };
        if value.is_empty() {
            return Ok(None);
        }
        let doc = xml.read()?;
        let mut found = None;
        for &id in &doc.node(doc.root)?.children {
            if doc.node(id)?.element().is_none() {
                continue;
            }
            if attr(&doc, id, ns(prefix), key).unwrap_or("").eq_ignore_ascii_case(value) {
                if found.is_some() {
                    return Err(Error::Invalid(format!("Ambiguous {key}")));
                }
                found = Some(id);
            }
        }
        Ok(found)
    }
    fn ensure(&mut self, package: &mut PackageData, index: usize) -> Result<Xml> {
        if let Some(xml) = &self.0[index] {
            return Ok(xml.clone());
        }
        let uri = declared_uri(package, PARTS[index], true)?.unwrap();
        let xml = package.load_xml(&uri)?;
        self.0[index] = Some(xml.clone());
        Ok(xml)
    }
}
#[pyclass(name = "NativeComments", module = "oxml._core", from_py_object)]
#[derive(Clone)]
pub struct Comments {
    package: Package,
}
impl Comments {
    fn main(&self) -> Result<Xml> {
        let mut package = self.package.lock()?;
        let uri = package.main_part().to_string();
        package.load_xml(&uri)
    }
    fn roots(&self) -> Result<Roots> {
        Roots::load(&mut *self.package.lock()?)
    }
    fn anchors(&self, ident: i64) -> Result<(Xml, usize, usize)> {
        let xml = self.main()?;
        let mut nodes = [Vec::new(), Vec::new(), Vec::new()];
        {
            let doc = xml.read()?;
            for id in doc.element_ids() {
                if number(attr(&doc, id, W, "id")).ok() != Some(ident) {
                    continue;
                }
                if let Some(index) =
                    ["commentRangeStart", "commentRangeEnd", "commentReference"].iter().position(|&n| text::name(&doc, id) == Some(n))
                {
                    nodes[index].push(id);
                }
            }
        }
        if nodes.iter().any(|n| n.len() != 1) {
            return Err(text::unsupported("Reply needs one complete anchor in the main story"));
        }
        Ok((xml, nodes[0][0], nodes[1][0]))
    }
    fn create(&self, text: &str, author: &str, initials: &str, date: Option<&str>, parent: Option<&Comment>) -> Result<Comment> {
        let stamp = timestamp(date)?;
        let mut roots = self.roots()?;
        if roots.0[3].is_some() && roots.0[2].is_none() {
            return Err(invalid("commentsExtensible is missing its commentsIds linkage"));
        }
        let comments = self.items()?;
        let mut ids = comments.iter().map(|c| c.id().map(|id| id.to_string())).collect::<Result<Vec<_>>>()?;
        {
            let main = self.main()?;
            let doc = main.read()?;
            for id in doc.element_ids() {
                if matches!(text::name(&doc, id), Some("commentRangeStart" | "commentRangeEnd" | "commentReference")) {
                    if let Some(value) = attr(&doc, id, W, "id") {
                        ids.push(value.to_string());
                    }
                }
            }
        }
        let ident = next_id(ids, false)?;
        let mut values = roots.para_ids()?;
        let mut parent_paragraph = None;
        let mut parent_id = None;
        if let Some(parent) = parent {
            let paragraph = parent.paragraph()?;
            parent_paragraph = Some(paragraph);
            let existing = parent.xml.attribute(paragraph, W14, "paraId")?;
            parent_id = Some(if let Some(value) = existing {
                value
            } else {
                if roots.0[2].is_some() {
                    return Err(invalid("Parent comment has incomplete paragraph/durable-ID linkage"));
                }
                let value = next_id(values.clone(), true)?;
                values.push(value.clone());
                value
            });
            if roots.0[2].is_some() {
                let record = roots
                    .record(2, "w16cid", "paraId", parent_id.as_deref())?
                    .ok_or_else(|| invalid("Parent comment has no durable-ID mapping"))?;
                let durable = roots.0[2].as_ref().unwrap().attribute(record, ns("w16cid"), "durableId")?;
                if roots.0[3].is_some() && roots.record(3, "w16cex", "durableId", durable.as_deref())?.is_none() {
                    return Err(invalid("Parent comment has no extensible metadata record"));
                }
            }
        }
        let modern = parent.is_some() || roots.0[1..].iter().any(Option::is_some);
        let para_id = if modern { Some(next_id(values, true)?) } else { None };
        let mut body = element("w", "comment", &[("id", &ident), ("author", author), ("initials", initials), ("date", &stamp)])?;
        let lines = text.split('\n').collect::<Vec<_>>();
        for (i, line) in lines.iter().enumerate() {
            let paragraph = body.add(Some(body.root), NodeKind::Element(text::word_element("p")))?;
            if i == 0 {
                let run = body.add(Some(paragraph), NodeKind::Element(text::word_element("r")))?;
                body.add(Some(run), NodeKind::Element(text::word_element("annotationRef")))?;
            }
            let run = text::run_text(&body, None, line)?;
            body.import(&run, run.root, Some(paragraph))?;
            if i + 1 == lines.len() {
                if let Some(value) = &para_id {
                    paragraph_id(&mut body, paragraph, value)?;
                }
            }
        }
        let durable =
            if roots.0[2].is_some() { Some(next_id(roots.values(&[ns("w16cid"), ns("w16cex")], &["durableId"])?, true)?) } else { None };
        // Validate caller strings and linkages in detached native structures before changing parts.
        let extended = if parent.is_some() || roots.0[1].is_some() {
            let mut record = element("w15", "commentEx", &[("paraId", para_id.as_deref().unwrap())])?;
            if let Some(value) = &parent_id {
                record.set_attribute(record.root, ns("w15"), "paraIdParent", value, Some("w15"))?;
            }
            Some(record)
        } else {
            None
        };
        let mapping = durable
            .as_ref()
            .map(|d| element("w16cid", "commentId", &[("paraId", para_id.as_deref().unwrap()), ("durableId", d)]))
            .transpose()?;
        let extensible = if roots.0[3].is_some() {
            Some(element("w16cex", "commentExtensible", &[("durableId", durable.as_deref().unwrap()), ("dateUtc", &stamp)])?)
        } else {
            None
        };
        if let (Some(parent), Some(paragraph)) = (parent, parent_paragraph) {
            if parent.xml.attribute(paragraph, W14, "paraId")?.is_none() {
                parent.xml.edit(|doc| paragraph_id(doc, paragraph, parent_id.as_deref().unwrap()))?;
            }
        }
        let root = roots.ensure(&mut *self.package.lock()?, 0)?;
        let id = append(&root, &body)?;
        if let Some(record) = extended {
            append(&roots.ensure(&mut *self.package.lock()?, 1)?, &record)?;
        }
        if let Some(record) = mapping {
            append(roots.0[2].as_ref().unwrap(), &record)?;
        }
        if let Some(record) = extensible {
            append(roots.0[3].as_ref().unwrap(), &record)?;
        }
        Ok(Comment { comments: self.clone(), xml: root, element: id })
    }
}
#[pymethods]
impl Comments {
    #[new]
    pub fn new(package: Package) -> Self {
        Self { package }
    }
    pub fn items(&self) -> Result<Vec<Comment>> {
        let mut package = self.package.lock()?;
        let Some(uri) = declared_uri(&mut package, PARTS[0], false)? else { return Ok(Vec::new()) };
        let xml = package.load_xml(&uri)?;
        let elements = {
            let doc = xml.read()?;
            doc.node(doc.root)?.children.iter().copied().filter(|&id| text::name(&doc, id) == Some("comment")).collect::<Vec<_>>()
        };
        Ok(elements.into_iter().map(|element| Comment { comments: self.clone(), xml: xml.clone(), element }).collect())
    }
    pub fn get(&self, ident: i64) -> Result<Comment> {
        let mut found = None;
        for comment in self.items()? {
            if comment.id()? == ident {
                if found.is_some() {
                    return Err(invalid("Ambiguous comment ID"));
                }
                found = Some(comment);
            }
        }
        found.ok_or_else(|| Error::Missing(ident.to_string()))
    }
    #[pyo3(signature=(span,text,author,initials="",date=None))]
    pub fn add(&self, span: &Range, text: &str, author: &str, initials: &str, date: Option<&str>) -> Result<Comment> {
        span.editable()?;
        let main = self.main()?;
        if !main.same_state(&span.story.xml) {
            return Err(invalid("Comment range must belong to this document main story"));
        }
        let selected = span.selection(&*main.read()?)?;
        let comment = self.create(text, author, initials, date, None)?;
        let ident = comment.id()?.to_string();
        let (start, end, reference) = anchors(&ident)?;
        main.edit(|doc| {
            let isolated = text::isolate(doc, selected, false)?;
            text::attach(doc, isolated.paragraph, isolated.index, &start)?;
            let end_index = isolated.runs.last().map(|&id| doc.position(id).map(|p| p.1 + 1)).transpose()?.unwrap_or(isolated.index + 1);
            text::attach(doc, isolated.paragraph, end_index, &end)?;
            text::attach(doc, isolated.paragraph, end_index + 1, &reference)?;
            Ok(())
        })?;
        Ok(comment)
    }
}
fn anchors(ident: &str) -> Result<(Document, Document, Document)> {
    let mut run = Document::from_element(text::word_element("r"));
    let reference = element("w", "commentReference", &[("id", ident)])?;
    run.import(&reference, reference.root, Some(run.root))?;
    Ok((element("w", "commentRangeStart", &[("id", ident)])?, element("w", "commentRangeEnd", &[("id", ident)])?, run))
}
#[pyclass(name = "NativeComment", module = "oxml._core", from_py_object)]
#[derive(Clone)]
pub struct Comment {
    comments: Comments,
    xml: Xml,
    element: usize,
}
impl Comment {
    fn paragraph(&self) -> Result<usize> {
        let (paragraph, key) = {
            let doc = self.xml.read()?;
            doc.node(self.element)?;
            let paragraph = last(&doc, self.element).ok_or_else(|| text::unsupported("Comment has no paragraph for modern linkage"))?;
            (paragraph, attr(&doc, paragraph, W14, "paraId").map(str::to_uppercase))
        };
        if let Some(key) = key {
            let mut found = false;
            for comment in self.comments.items()? {
                let doc = comment.xml.read()?;
                if para_key(&doc, comment.element) == key {
                    if found {
                        return Err(invalid("Ambiguous comment paragraph ID"));
                    }
                    found = true;
                }
            }
        }
        Ok(paragraph)
    }
    fn extended(&self, roots: &Roots) -> Result<Option<usize>> {
        if roots.0[1].is_none() {
            return Ok(None);
        }
        let key = self.xml.attribute(self.paragraph()?, W14, "paraId")?;
        roots.record(1, "w15", "paraId", key.as_deref())
    }
}
#[pymethods]
impl Comment {
    #[getter]
    pub fn xml(&self) -> Xml {
        self.xml.clone()
    }
    #[getter]
    pub fn element_id(&self) -> usize {
        self.element
    }
    #[getter]
    pub fn id(&self) -> Result<i64> {
        number(self.xml.attribute(self.element, W, "id")?.as_deref())
    }
    #[getter]
    pub fn author(&self) -> Result<Option<String>> {
        self.xml.attribute(self.element, W, "author")
    }
    #[getter]
    pub fn text(&self) -> Result<String> {
        Story::new_native(self.xml.clone(), self.element, View::Current)?.text_native()
    }
    #[getter]
    pub fn range(&self) -> Result<Range> {
        let (xml, start, end) = self.comments.anchors(self.id()?)?;
        let root = xml.root()?;
        let story = Story::new_native(xml, root, View::Current)?;
        story.range_native(story.position_native(start)?, story.position_native(end)?)
    }
    #[getter]
    pub fn parent(&self) -> Result<Option<Comment>> {
        let roots = self.comments.roots()?;
        let Some(record) = self.extended(&roots)? else { return Ok(None) };
        let Some(key) = roots.0[1].as_ref().unwrap().attribute(record, ns("w15"), "paraIdParent")? else { return Ok(None) };
        let mut found = None;
        for comment in self.comments.items()? {
            let matches = {
                let doc = comment.xml.read()?;
                para_key(&doc, comment.element).eq_ignore_ascii_case(&key)
            };
            if matches {
                if found.is_some() {
                    return Err(invalid("Ambiguous parent paragraph ID"));
                }
                found = Some(comment);
            }
        }
        found.map(Some).ok_or_else(|| invalid("Comment parent paragraph does not exist"))
    }
    #[getter]
    pub fn replies(&self) -> Result<Vec<Comment>> {
        let key = {
            let doc = self.xml.read()?;
            doc.node(self.element)?;
            para_key(&doc, self.element)
        };
        let roots = self.comments.roots()?;
        let Some(xml) = &roots.0[1] else { return Ok(Vec::new()) };
        if key.is_empty() {
            return Ok(Vec::new());
        }
        let children = {
            let doc = xml.read()?;
            doc.node(doc.root)?
                .children
                .iter()
                .filter_map(|&id| {
                    attr(&doc, id, ns("w15"), "paraIdParent")
                        .filter(|p| p.eq_ignore_ascii_case(&key))
                        .map(|_| attr(&doc, id, ns("w15"), "paraId").unwrap_or("").to_uppercase())
                })
                .collect::<HashSet<_>>()
        };
        self.comments
            .items()?
            .into_iter()
            .filter_map(|c| {
                let matches = c.xml.read().map(|doc| children.contains(&para_key(&doc, c.element)));
                match matches {
                    Ok(true) => Some(Ok(c)),
                    Ok(false) => None,
                    Err(e) => Some(Err(e)),
                }
            })
            .collect()
    }
    #[getter]
    pub fn resolved(&self) -> Result<bool> {
        let roots = self.comments.roots()?;
        let Some(record) = self.extended(&roots)? else { return Ok(false) };
        Ok(matches!(roots.0[1].as_ref().unwrap().attribute(record, ns("w15"), "done")?.as_deref(), Some("1" | "true" | "on")))
    }
    #[pyo3(signature=(value=true))]
    pub fn resolve(&self, value: bool) -> Result<()> {
        let mut roots = self.comments.roots()?;
        let existing = self.extended(&roots)?;
        let resolved = if let Some(id) = existing {
            matches!(roots.0[1].as_ref().unwrap().attribute(id, ns("w15"), "done")?.as_deref(), Some("1" | "true" | "on"))
        } else {
            false
        };
        if value == resolved {
            return Ok(());
        }
        let record = if let Some(record) = existing {
            record
        } else {
            let paragraph = self.paragraph()?;
            let id = if let Some(id) = self.xml.attribute(paragraph, W14, "paraId")? {
                id
            } else {
                if roots.0[2].is_some() {
                    return Err(invalid("Comment has incomplete paragraph/durable-ID linkage"));
                }
                let id = next_id(roots.para_ids()?, true)?;
                self.xml.edit(|doc| paragraph_id(doc, paragraph, &id))?;
                id
            };
            append(&roots.ensure(&mut *self.comments.package.lock()?, 1)?, &element("w15", "commentEx", &[("paraId", &id)])?)?
        };
        roots.0[1].as_ref().unwrap().edit(|doc| {
            doc.set_attribute_ns(record, ns("w15"), "done", if value { "1" } else { "0" }, "w15")?;
            Ok(())
        })
    }
    #[pyo3(signature=(text,author,initials="",date=None))]
    pub fn reply(&self, text: &str, author: &str, initials: &str, date: Option<&str>) -> Result<Comment> {
        let (main, start, end) = self.comments.anchors(self.id()?)?;
        let reply = self.comments.create(text, author, initials, date, Some(self))?;
        let (begin, finish, reference) = anchors(&reply.id()?.to_string())?;
        main.edit(|doc| {
            let (parent, index) = doc.position(start)?;
            text::attach(doc, parent.unwrap(), index + 1, &begin)?;
            let (parent, index) = doc.position(end)?;
            text::attach(doc, parent.unwrap(), index, &finish)?;
            text::attach(doc, parent.unwrap(), index + 1, &reference)?;
            Ok(())
        })?;
        Ok(reply)
    }
    pub fn delete(&self) -> Result<usize> {
        let roots = self.comments.roots()?;
        let comments = self.comments.items()?;
        let mut by_para = HashMap::new();
        for comment in comments {
            let key = {
                let doc = comment.xml.read()?;
                para_key(&doc, comment.element)
            };
            if !key.is_empty() && by_para.insert(key, comment).is_some() {
                return Err(invalid("Ambiguous comment paragraph ID"));
            }
        }
        let mut children: HashMap<String, Vec<String>> = HashMap::new();
        if let Some(xml) = &roots.0[1] {
            let doc = xml.read()?;
            for &id in &doc.node(doc.root)?.children {
                let Some(e) = doc.node(id)?.element() else { continue };
                if e.name.uri != ns("w15") || e.name.local != "commentEx" {
                    continue;
                }
                if let Some(parent) = e.attribute(ns("w15"), "paraIdParent") {
                    children.entry(parent.to_uppercase()).or_default().push(e.attribute(ns("w15"), "paraId").unwrap_or("").to_uppercase());
                }
            }
        }
        let mut selected = vec![self.clone()];
        let mut seen = HashSet::new();
        let mut records = [Vec::new(), Vec::new(), Vec::new(), Vec::new()];
        let mut i = 0;
        while i < selected.len() {
            let comment = selected[i].clone();
            i += 1;
            if !seen.insert(comment.id()?) {
                return Err(invalid("Cyclic or duplicate comment reply linkage"));
            }
            let key = {
                let doc = comment.xml.read()?;
                para_key(&doc, comment.element)
            };
            for child in children.get(&key).into_iter().flatten() {
                selected.push(by_para.get(child).ok_or_else(|| invalid("Reply metadata has no comment body"))?.clone());
            }
            let extended = roots.record(1, "w15", "paraId", Some(&key))?;
            let mapping = roots.record(2, "w16cid", "paraId", Some(&key))?;
            if roots.0[2].is_some() && mapping.is_none() {
                return Err(invalid("Comment has no durable-ID mapping"));
            }
            let durable = if let Some(mapping) = mapping {
                roots.0[2].as_ref().unwrap().attribute(mapping, ns("w16cid"), "durableId")?
            } else {
                None
            };
            if let Some(durable) = &durable {
                roots.record(2, "w16cid", "durableId", Some(durable))?;
            }
            let extensible = roots.record(3, "w16cex", "durableId", durable.as_deref())?;
            if roots.0[3].is_some() && extensible.is_none() {
                return Err(invalid("Comment has no extensible metadata record"));
            }
            for (index, id) in [(1, extended), (2, mapping), (3, extensible)] {
                if let Some(id) = id {
                    records[index].push(id);
                }
            }
        }
        let selected_anchor = |doc: &Document, id: usize| {
            matches!(text::name(doc, id), Some("commentRangeStart" | "commentRangeEnd" | "commentReference"))
                && number(attr(doc, id, W, "id")).ok().is_some_and(|id| seen.contains(&id))
        };
        let main = self.comments.main()?;
        {
            let mut package = self.comments.package.lock()?;
            for (uri, element) in story_nodes(&mut package)? {
                let xml = package.load_xml(&uri)?;
                if xml.same_state(&main) {
                    continue;
                }
                let doc = xml.read()?;
                if text::descendants(&doc, element).into_iter().any(|id| selected_anchor(&doc, id)) {
                    return Err(text::unsupported("Comment anchors outside the main part are unsupported"));
                }
            }
        }
        main.edit(|doc| {
            let anchors = doc.element_ids().into_iter().filter(|&id| selected_anchor(doc, id)).collect::<Vec<_>>();
            for id in anchors {
                let parent = doc.node(id)?.parent;
                let reference = text::name(doc, id) == Some("commentReference");
                doc.remove(id)?;
                if let Some(parent) = parent {
                    if reference
                        && text::name(doc, parent) == Some("r")
                        && doc.node(parent)?.children.is_empty()
                        && doc.node(parent)?.element().unwrap().attributes.is_empty()
                    {
                        doc.remove(parent)?;
                    }
                }
            }
            Ok(())
        })?;
        for (index, ids) in records.into_iter().enumerate() {
            if ids.is_empty() {
                continue;
            }
            roots.0[index].as_ref().unwrap().edit(|doc| {
                for id in ids {
                    doc.remove(id)?;
                }
                Ok(())
            })?;
        }
        self.xml.edit(|doc| {
            for comment in &selected {
                doc.remove(comment.element)?;
            }
            Ok(())
        })?;
        Ok(selected.len())
    }
    pub fn delete_thread(&self) -> Result<usize> {
        let mut comment = self.clone();
        let mut seen = HashSet::new();
        loop {
            if !seen.insert(comment.id()?) {
                return Err(invalid("Cyclic comment reply linkage"));
            }
            match comment.parent()? {
                Some(parent) => comment = parent,
                None => return comment.delete(),
            }
        }
    }
}
