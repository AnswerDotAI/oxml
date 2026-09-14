//! Footnote bodies in the footnotes part and their reference marks in stories.
use crate::{
    definitions::{self, ensure, set_word_attribute},
    error::{Error, Result},
    package::Package,
    package_schema::{declared_uri, story_nodes},
    schema::insertion_position,
    text::{self, Range, Story, View, W},
    xml::{self, Document, Xml},
};
use pyo3::prelude::*;
use std::collections::HashSet;

const PART: &str = "FootnotesPart";
fn invalid(message: &str) -> Error { Error::Invalid(message.into()) }
fn parse(markup: String) -> Result<Document> { xml::parse_bytes(markup.as_bytes()) }
fn reference_run(id: i64) -> Result<Document> {
    parse(format!(r#"<w:r xmlns:w="{W}"><w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr><w:footnoteReference w:id="{id}"/></w:r>"#))
}
fn separator(kind: &str, id: i64) -> Result<Document> {
    parse(format!(r#"<w:footnote xmlns:w="{W}" w:type="{kind}" w:id="{id}"><w:p><w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr><w:r><w:{kind}/></w:r></w:p></w:footnote>"#))
}
fn attribute<'a>(doc: &'a Document, id: usize, local: &str) -> Option<&'a str> { doc.node(id).ok()?.element()?.attribute(W, local) }
fn notes(doc: &Document) -> Result<Vec<usize>> {
    Ok(doc.node(doc.root)?.children.iter().copied().filter(|&id| text::name(doc, id) == Some("footnote")).collect())
}
/// Give `paragraph` the `FootnoteText` style when it has none, and open the first one with Word's mark run and a space.
fn style_paragraph(doc: &mut Document, paragraph: usize, first: bool) -> Result<()> {
    let properties = ensure(doc, paragraph, "pPr")?;
    if text::unique_child(doc, properties, "pStyle")?.is_none() {
        let style = ensure(doc, properties, "pStyle")?;
        set_word_attribute(doc, style, "val", "FootnoteText")?;
    }
    if first {
        let index = doc.position(properties)?.1 + 1;
        let mark = parse(format!(r#"<w:r xmlns:w="{W}"><w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr><w:footnoteRef/></w:r>"#))?;
        let space = parse(format!(r#"<w:r xmlns:w="{W}"><w:t xml:space="preserve"> </w:t></w:r>"#))?;
        text::attach(doc, paragraph, index, &mark)?;
        text::attach(doc, paragraph, index + 1, &space)?;
    }
    Ok(())
}

#[pyclass(name = "NativeFootnotes", module = "oxml._core", from_py_object)]
#[derive(Clone)]
pub struct Footnotes {
    package: Package,
}
impl Footnotes {
    fn ensure_styles(&self) -> Result<()> {
        for (id, display, kind, base, paragraph, run) in [
            ("FootnoteText", "footnote text", "paragraph", "Normal", &[r#"spacing w:after="0" w:line="240" w:lineRule="auto""#][..],
                &[r#"sz w:val="20""#, r#"szCs w:val="20""#][..]),
            ("FootnoteReference", "footnote reference", "character", "DefaultParagraphFont", &[][..], &[r#"vertAlign w:val="superscript""#][..]),
        ] {
            match definitions::style_get(&self.package, id) { Ok(_) => continue, Err(Error::Missing(_)) => (), Err(e) => return Err(e) }
            let base = match definitions::style_get(&self.package, base) { Ok(_) => Some(base), Err(Error::Missing(_)) => None, Err(e) => return Err(e) };
            let fragments = |values: &[&str]| values.iter().map(|s| format!(r#"<w:{s} xmlns:w="{W}"/>"#).into_bytes()).collect();
            definitions::style_add(&self.package, id, Some(display), kind, base, fragments(paragraph), fragments(run))?;
        }
        Ok(())
    }
    /// The footnotes part, created on demand and always holding Word's two separator notes.
    fn part(&self, create: bool) -> Result<Option<Xml>> {
        let mut package = self.package.lock()?;
        let Some(uri) = declared_uri(&mut package, PART, create)? else { return Ok(None) };
        let xml = package.load_xml(&uri)?;
        for (kind, id) in [("separator", -1), ("continuationSeparator", 0)] {
            let present = { let doc = xml.read()?; notes(&doc)?.into_iter().any(|n| attribute(&doc, n, "type") == Some(kind)) };
            if present { continue; }
            let note = separator(kind, id)?;
            xml.edit(|doc| {
                let position = insertion_position(doc, doc.root, W, "footnote")?;
                text::attach(doc, doc.root, position, &note).map(|_| ())
            })?;
        }
        Ok(Some(xml))
    }
    fn main(&self) -> Result<Xml> {
        let mut package = self.package.lock()?;
        let uri = package.main_part().to_string();
        package.load_xml(&uri)
    }
}
#[pymethods]
impl Footnotes {
    #[new]
    pub fn new(package: Package) -> Self { Self { package } }
    pub fn items(&self) -> Result<Vec<Footnote>> {
        let Some(xml) = self.part(false)? else { return Ok(Vec::new()) };
        let elements = {
            let doc = xml.read()?;
            notes(&doc)?.into_iter().filter(|&n| matches!(attribute(&doc, n, "type"), None | Some("normal"))).collect::<Vec<_>>()
        };
        Ok(elements.into_iter().map(|element| Footnote { footnotes: self.clone(), xml: xml.clone(), element }).collect())
    }
    pub fn get(&self, ident: i64) -> Result<Footnote> {
        self.items()?.into_iter().find(|note| note.id().ok() == Some(ident)).ok_or_else(|| Error::Missing(format!("No footnote with id {ident}")))
    }
    /// A new note holding `blocks`, opened by an empty paragraph when the first block is not one.
    pub fn create(&self, blocks: Vec<Vec<u8>>) -> Result<Footnote> {
        let mut sources = blocks.iter().map(|b| xml::parse_bytes(b)).collect::<Result<Vec<_>>>()?;
        self.ensure_styles()?;
        let xml = self.part(true)?.unwrap();
        if !sources.first().is_some_and(|s| text::name(s, s.root) == Some("p")) { sources.insert(0, parse(format!(r#"<w:p xmlns:w="{W}"/>"#))?); }
        let element = xml.edit(|doc| {
            let id = notes(doc)?.into_iter().filter_map(|n| attribute(doc, n, "id")?.parse::<i64>().ok()).max().unwrap_or(0).max(0) + 1;
            let note = parse(format!(r#"<w:footnote xmlns:w="{W}" w:id="{id}"/>"#))?;
            let position = insertion_position(doc, doc.root, W, "footnote")?;
            let note = text::attach(doc, doc.root, position, &note)?;
            for (index, source) in sources.iter().enumerate() {
                let block = doc.import_at(source, source.root, note, index)?;
                if text::name(doc, block) == Some("p") { style_paragraph(doc, block, index == 0)?; }
            }
            Ok(note)
        })?;
        Ok(Footnote { footnotes: self.clone(), xml, element })
    }
    /// A new note whose reference mark follows `span`'s text.
    pub fn add(&self, span: &Range, blocks: Vec<Vec<u8>>) -> Result<Footnote> {
        span.editable()?;
        let main = self.main()?;
        if !main.same_state(&span.story.xml) { return Err(invalid("Footnote references belong in the main document story")); }
        let selected = span.selection(&*main.read()?, false)?;
        let note = self.create(blocks)?;
        let reference = reference_run(note.id()?)?;
        main.edit(|doc| {
            let isolated = text::isolate(doc, selected, false)?;
            let index = isolated.runs.last().map(|&id| doc.position(id).map(|p| p.1 + 1)).transpose()?.unwrap_or(isolated.index);
            text::attach(doc, isolated.paragraph, index, &reference).map(|_| ())
        })?;
        Ok(note)
    }
}

#[pyclass(name = "NativeFootnote", module = "oxml._core", from_py_object)]
#[derive(Clone)]
pub struct Footnote {
    footnotes: Footnotes,
    xml: Xml,
    element: usize,
}
#[pymethods]
impl Footnote {
    #[getter]
    pub fn xml(&self) -> Xml { self.xml.clone() }
    #[getter]
    pub fn element_id(&self) -> usize { self.element }
    #[getter]
    pub fn id(&self) -> Result<i64> {
        self.xml.attribute(self.element, W, "id")?.unwrap_or_default().parse().map_err(|_| invalid("Invalid footnote ID"))
    }
    #[getter]
    pub fn text(&self) -> Result<String> { Story::new_native(self.xml.clone(), self.element, View::Current)?.text_native().map(|text| text.trim_start().to_string()) }
    /// A detached run carrying this note's reference mark.
    pub fn reference(&self) -> Result<Xml> { Ok(Xml::from_document(reference_run(self.id()?)?)) }
    /// Remove the note and every run referencing it; returns the number of references removed.
    pub fn delete(&self) -> Result<usize> {
        let ident = self.id()?.to_string();
        let uris: HashSet<String> = story_nodes(&mut *self.footnotes.package.lock()?)?.into_iter().map(|(uri, _)| uri).collect();
        let mut removed = 0;
        for uri in uris {
            let xml = self.footnotes.package.lock()?.load_xml(&uri)?;
            removed += xml.edit(|doc| {
                let references: Vec<usize> = doc.element_ids().into_iter()
                    .filter(|&n| text::name(doc, n) == Some("footnoteReference") && attribute(doc, n, "id") == Some(ident.as_str())).collect();
                for reference in &references {
                    let run = doc.node(*reference)?.parent.unwrap();
                    let other = doc.node(run)?.children.iter().any(|&c| c != *reference && text::name(doc, c) != Some("rPr"));
                    doc.remove(if other { *reference } else { run })?;
                }
                Ok(references.len())
            })?;
        }
        self.xml.edit(|doc| doc.remove(self.element))?;
        Ok(removed)
    }
}
