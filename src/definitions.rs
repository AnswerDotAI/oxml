//! Explicit styles and numbering. Shared property helpers edit the existing native tree.
use crate::{error::{Error, Result}, package::Package, package_schema, schema, text::{self, name, unique_child, W}, xml::{self, Document, NodeKind, Xml}};
use pyo3::prelude::*;
use std::collections::HashSet;

fn invalid(message: impl Into<String>) -> Error { Error::Invalid(message.into()) }
pub fn children(doc: &Document, parent: usize, local: &str) -> Vec<usize> {
    doc.node(parent).map(|n| n.children.iter().copied().filter(|&id| name(doc, id) == Some(local)).collect()).unwrap_or_default()
}
pub fn word_attribute<'a>(doc: &'a Document, id: usize, local: &str) -> Option<&'a str> {
    doc.node(id).ok()?.element()?.attribute(W, local)
}
pub fn append(doc: &mut Document, parent: usize, local: &str) -> Result<usize> {
    doc.add(Some(parent), NodeKind::Element(text::word_element(local)))
}
pub fn ensure(doc: &mut Document, parent: usize, local: &str) -> Result<usize> {
    if let Some(id) = unique_child(doc, parent, local)? { return Ok(id); }
    let position = schema::insertion_position(doc, parent, W, local)?;
    doc.insert_kind(parent, position, NodeKind::Element(text::word_element(local)))
}
pub fn set_word_attribute(doc: &mut Document, id: usize, local: &str, value: &str) -> Result<()> {
    if word_attribute(doc, id, local) == Some(value) { return Ok(()); }
    doc.set_attribute_ns(id, W, local, value, "w")?;
    Ok(())
}
pub fn set_value(doc: &mut Document, parent: usize, local: &str, value: &str) -> Result<usize> {
    let id = ensure(doc, parent, local)?;
    set_word_attribute(doc, id, "val", value)?;
    Ok(id)
}
fn append_value(doc: &mut Document, parent: usize, local: &str, value: &str) -> Result<usize> {
    let id = append(doc, parent, local)?;
    set_word_attribute(doc, id, "val", value)?;
    Ok(id)
}
fn part_xml(package: &Package, name: &str, create: bool) -> Result<Option<Xml>> {
    let mut package = package.lock()?;
    package_schema::declared_uri(&mut package, name, create)?.map(|uri| package.load_xml(&uri)).transpose()
}
fn integer(value: i64, label: &str, maximum: i64) -> Result<()> {
    if !(0..=maximum).contains(&value) { return Err(invalid(format!("{label} must be an integer between 0 and {maximum}"))); }
    Ok(())
}
fn number(doc: &Document, id: usize, attr: &str) -> Result<i64> {
    doc.node(id)?;
    word_attribute(doc, id, attr).ok_or_else(|| invalid(format!("Missing {attr}")))?.trim_matches([' ', '\t', '\r', '\n'])
        .parse().map_err(|_| invalid(format!("Invalid {attr}")))
}
pub fn find_id(doc: &Document, root: usize, local: &str, attr: &str, value: i64) -> Result<Option<usize>> {
    let mut found = None;
    for id in children(doc, root, local) {
        if number(doc, id, attr)? != value { continue; }
        if found.replace(id).is_some() { return Err(invalid(format!("Multiple {local} IDs"))); }
    }
    Ok(found)
}
pub fn next_id(doc: &Document, root: usize, local: &str, attr: &str, mut start: i64) -> Result<i64> {
    let mut used = HashSet::new();
    for id in children(doc, root, local) { if word_attribute(doc, id, attr).is_some() { used.insert(number(doc, id, attr)?); } }
    while used.contains(&start) { start += 1; }
    integer(start, attr, i32::MAX.into())?;
    Ok(start)
}
fn style_kind(doc: &Document, id: usize) -> &str { word_attribute(doc, id, "type").unwrap_or("paragraph") }
fn style_target(kind: &str) -> Result<(&str, &str, &str)> {
    match kind { "paragraph" => Ok(("p", "pPr", "pStyle")), "character" => Ok(("r", "rPr", "rStyle")), "table" => Ok(("tbl", "tblPr", "tblStyle")),
        _ => Err(invalid("Style kind must be paragraph, character or table")) }
}
fn style_lookup(doc: &Document, style_id: &str) -> Result<usize> {
    let found: Vec<_> = children(doc, doc.root, "style").into_iter().filter(|&id| word_attribute(doc, id, "styleId") == Some(style_id)).collect();
    match found.as_slice() { [id] => Ok(*id), [] => Err(Error::Missing(style_id.into())), _ => Err(invalid("Multiple style IDs")) }
}
#[pyclass(module = "oxml._core")]
pub struct Style { package: Package, #[pyo3(get)] pub xml: Xml, #[pyo3(get)] pub node_id: usize }
#[pymethods]
impl Style {
    #[getter]
    pub fn id(&self) -> Result<Option<String>> { let doc = self.xml.read()?; doc.node(self.node_id)?; Ok(word_attribute(&doc, self.node_id, "styleId").map(str::to_owned)) }
    #[getter]
    pub fn kind(&self) -> Result<String> { let doc = self.xml.read()?; doc.node(self.node_id)?; Ok(style_kind(&doc, self.node_id).into()) }
    #[getter]
    pub fn name(&self) -> Result<Option<String>> {
        let doc = self.xml.read()?;
        Ok(unique_child(&doc, self.node_id, "name")?.and_then(|id| word_attribute(&doc, id, "val")).map(str::to_owned))
    }
    pub fn apply(&self, xml: &Xml, id: usize) -> Result<()> {
        self.package.owner(xml)?;
        let (kind, style_id) = (self.kind()?, self.id()?.filter(|s| !s.is_empty()).ok_or_else(|| invalid("Style has no styleId"))?);
        let (target, properties, reference) = style_target(&kind)?;
        xml.edit(|doc| {
            if name(doc, id) != Some(target) { return Err(invalid(format!("{kind} style requires a w:{target}"))); }
            let properties = ensure(doc, id, properties)?;
            set_value(doc, properties, reference, &style_id)?;
            Ok(())
        })
    }
}
#[pyfunction]
pub fn style_items(package: &Package) -> Result<Vec<Style>> {
    let Some(xml) = part_xml(package, "StyleDefinitionsPart", false)? else { return Ok(Vec::new()); };
    let ids = { let doc = xml.read()?; children(&doc, doc.root, "style") };
    Ok(ids.into_iter().map(|node_id| Style { package: package.clone(), xml: xml.clone(), node_id }).collect())
}
#[pyfunction]
pub fn style_get(package: &Package, style_id: &str) -> Result<Style> {
    let xml = part_xml(package, "StyleDefinitionsPart", false)?.ok_or_else(|| Error::Missing(style_id.into()))?;
    let node_id = style_lookup(&*xml.read()?, style_id)?;
    Ok(Style { package: package.clone(), xml, node_id })
}
#[pyfunction]
#[pyo3(signature=(package, display_name, kind=None))]
pub fn style_find(package: &Package, display_name: &str, kind: Option<&str>) -> Result<Option<Style>> {
    let Some(xml) = part_xml(package, "StyleDefinitionsPart", false)? else { return Ok(None); };
    let node_id = {
        let doc = xml.read()?;
        let mut found = None;
        for id in children(&doc, doc.root, "style") {
            let display = unique_child(&doc, id, "name")?.and_then(|id| word_attribute(&doc, id, "val"));
            if display != Some(display_name) || kind.is_some_and(|kind| style_kind(&doc, id) != kind) { continue; }
            if found.replace(id).is_some() { return Err(invalid("Multiple style names")); }
        }
        found
    };
    Ok(node_id.map(|node_id| Style { package: package.clone(), xml, node_id }))
}
#[pyfunction]
#[pyo3(signature=(package, style_id, display_name, kind, based_on, paragraph, run))]
pub fn style_add(package: &Package, style_id: &str, display_name: Option<&str>, kind: &str, based_on: Option<&str>, paragraph: Vec<Vec<u8>>, run: Vec<Vec<u8>>) -> Result<Style> {
    if style_id.is_empty() { return Err(invalid("style_id requires a nonempty string")); }
    style_target(kind)?;
    let existing = part_xml(package, "StyleDefinitionsPart", false)?;
    if let Some(xml) = &existing {
        let doc = xml.read()?;
        if children(&doc, doc.root, "style").iter().any(|&id| word_attribute(&doc, id, "styleId") == Some(style_id)) {
            return Err(invalid("Style ID already exists"));
        }
        if let Some(base) = based_on {
            if style_kind(&doc, style_lookup(&doc, base)?) != kind { return Err(invalid("Base style must have the same kind")); }
        }
    } else if let Some(base) = based_on { return Err(Error::Missing(base.into())); }
    if kind == "character" && !paragraph.is_empty() { return Err(invalid("Character styles cannot have paragraph properties")); }
    let mut source = Document::from_element(text::word_element("style"));
    let root = source.root;
    for (attr, value) in [("type", kind), ("styleId", style_id), ("customStyle", "1")] { set_word_attribute(&mut source, root, attr, value)?; }
    append_value(&mut source, root, "name", display_name.unwrap_or(style_id))?;
    if let Some(base) = based_on { append_value(&mut source, root, "basedOn", base)?; }
    for (local, fragments) in [("pPr", paragraph), ("rPr", run)] {
        if fragments.is_empty() { continue; }
        let parent = append(&mut source, root, local)?;
        for bytes in fragments { let child = xml::parse_bytes(&bytes)?; source.import(&child, child.root, Some(parent))?; }
    }
    let xml = match existing { Some(xml) => xml, None => part_xml(package, "StyleDefinitionsPart", true)?.unwrap() };
    let node_id = xml.edit(|doc| text::attach(doc, doc.root, schema::insertion_position(doc, doc.root, W, "style")?, &source))?;
    Ok(Style { package: package.clone(), xml, node_id })
}

#[derive(Clone)]
#[pyclass(module = "oxml._core", get_all, set_all, from_py_object)]
pub struct Level { pub format: String, pub text: Option<String>, pub start: i64, pub indent: Option<i64>, pub hanging: i64, pub restart: Option<i64> }
#[pymethods]
impl Level {
    #[new]
    #[pyo3(signature=(format="decimal".into(), text=None, start=1, indent=None, hanging=360, restart=None))]
    pub fn new(format: String, text: Option<String>, start: i64, indent: Option<i64>, hanging: i64, restart: Option<i64>) -> Self {
        Self { format, text, start, indent, hanging, restart }
    }
}
impl Level {
    fn append(&self, doc: &mut Document, parent: usize, index: usize) -> Result<()> {
        if !schema::enum_contains("DocumentFormat.OpenXml.Wordprocessing.NumberFormatValues", &self.format) {
            return Err(invalid("Unknown numbering format"));
        }
        let indent = self.indent.unwrap_or(720 * (index as i64 + 1));
        for (label, value) in [("start", self.start), ("indent", indent), ("hanging", self.hanging)] { integer(value, label, i32::MAX.into())?; }
        if let Some(restart) = self.restart { integer(restart, "restart", index as i64)?; }
        let id = append(doc, parent, "lvl")?;
        set_word_attribute(doc, id, "ilvl", &index.to_string())?;
        append_value(doc, id, "start", &self.start.to_string())?;
        append_value(doc, id, "numFmt", &self.format)?;
        if let Some(restart) = self.restart { append_value(doc, id, "lvlRestart", &restart.to_string())?; }
        let label = self.text.clone().unwrap_or_else(|| if self.format == "bullet" { "•".into() } else { format!("%{}.", index + 1) });
        append_value(doc, id, "lvlText", &label)?;
        append_value(doc, id, "lvlJc", "left")?;
        let properties = append(doc, id, "pPr")?;
        let tabs = append(doc, properties, "tabs")?;
        let tab = append_value(doc, tabs, "tab", "num")?;
        set_word_attribute(doc, tab, "pos", &indent.to_string())?;
        let ind = append(doc, properties, "ind")?;
        set_word_attribute(doc, ind, "left", &indent.to_string())?;
        set_word_attribute(doc, ind, "hanging", &self.hanging.to_string())
    }
}
#[pyclass(module = "oxml._core")]
pub struct NumberingInstance { package: Package, #[pyo3(get)] pub xml: Xml, #[pyo3(get)] pub node_id: usize }
impl NumberingInstance {
    fn definition_id(&self, doc: &Document) -> Result<usize> {
        let reference = unique_child(doc, self.node_id, "abstractNumId")?.ok_or_else(|| invalid("Numbering instance has no abstractNumId"))?;
        find_id(doc, doc.root, "abstractNum", "abstractNumId", number(doc, reference, "val")?)?.ok_or_else(|| invalid("Abstract numbering definition does not exist"))
    }
    fn check_level(&self, doc: &Document, level: i64) -> Result<()> {
        integer(level, "level", 8)?;
        if let Some(override_id) = find_id(doc, self.node_id, "lvlOverride", "ilvl", level)? {
            if unique_child(doc, override_id, "lvl")?.is_some() { return Ok(()); }
        }
        let definition = self.definition_id(doc)?;
        if find_id(doc, definition, "lvl", "ilvl", level)?.is_some() { return Ok(()); }
        if unique_child(doc, definition, "numStyleLink")?.is_some() { return Err(Error::Unsupported("Linked numbering styles are not resolved".into())); }
        Err(invalid("Level is not defined by this numbering instance"))
    }
}
#[pymethods]
impl NumberingInstance {
    #[getter]
    pub fn id(&self) -> Result<i64> { number(&*self.xml.read()?, self.node_id, "numId") }
    #[getter]
    pub fn definition(&self) -> Result<usize> { self.definition_id(&*self.xml.read()?) }
    #[pyo3(signature=(xml, id, level=0))]
    pub fn apply(&self, xml: &Xml, id: usize, level: i64) -> Result<()> {
        self.package.owner(xml)?;
        let ident = { let doc = self.xml.read()?; self.check_level(&doc, level)?; number(&doc, self.node_id, "numId")? };
        xml.edit(|doc| {
            if name(doc, id) != Some("p") { return Err(invalid("Numbering requires a paragraph")); }
            if let Some(properties) = unique_child(doc, id, "pPr")? {
                if let Some(properties) = unique_child(doc, properties, "numPr")? {
                    unique_child(doc, properties, "ilvl")?; unique_child(doc, properties, "numId")?;
                }
            }
            let ppr = ensure(doc, id, "pPr")?;
            let properties = ensure(doc, ppr, "numPr")?;
            set_value(doc, properties, "ilvl", &level.to_string())?;
            set_value(doc, properties, "numId", &ident.to_string())?;
            Ok(())
        })
    }
    #[pyo3(signature=(start=1, level=0))]
    pub fn restart(&self, start: i64, level: i64) -> Result<Self> {
        integer(start, "start", i32::MAX.into())?;
        let node_id = self.xml.edit(|doc| {
            self.check_level(doc, level)?;
            if let Some(id) = find_id(doc, self.node_id, "lvlOverride", "ilvl", level)? { unique_child(doc, id, "startOverride")?; }
            let ident = next_id(doc, doc.root, "num", "numId", 1)?;
            let durable = if word_attribute(doc, self.node_id, "durableId").is_some() { Some(next_id(doc, doc.root, "num", "durableId", 1)?) } else { None };
            let position = schema::insertion_position(doc, doc.root, W, "num")?;
            let id = doc.copy(self.node_id, doc.root, position)?;
            set_word_attribute(doc, id, "numId", &ident.to_string())?;
            if let Some(durable) = durable { set_word_attribute(doc, id, "durableId", &durable.to_string())?; }
            let override_id = if let Some(id) = find_id(doc, id, "lvlOverride", "ilvl", level)? { id }
                else {
                    let position = schema::insertion_position(doc, id, W, "lvlOverride")?;
                    let c = doc.insert_kind(id, position, NodeKind::Element(text::word_element("lvlOverride")))?;
                    set_word_attribute(doc, c, "ilvl", &level.to_string())?;
                    c
                };
            set_value(doc, override_id, "startOverride", &start.to_string())?;
            Ok(id)
        })?;
        Ok(Self { package: self.package.clone(), xml: self.xml.clone(), node_id })
    }
}
#[pyfunction]
pub fn numbering_items(package: &Package) -> Result<Vec<NumberingInstance>> {
    let Some(xml) = part_xml(package, "NumberingDefinitionsPart", false)? else { return Ok(Vec::new()); };
    let ids = { let doc = xml.read()?; children(&doc, doc.root, "num") };
    Ok(ids.into_iter().map(|node_id| NumberingInstance { package: package.clone(), xml: xml.clone(), node_id }).collect())
}
#[pyfunction]
pub fn numbering_get(package: &Package, ident: i64) -> Result<NumberingInstance> {
    let xml = part_xml(package, "NumberingDefinitionsPart", false)?.ok_or_else(|| Error::Missing(ident.to_string()))?;
    let node_id = { let doc = xml.read()?; find_id(&doc, doc.root, "num", "numId", ident)?.ok_or_else(|| Error::Missing(ident.to_string()))? };
    Ok(NumberingInstance { package: package.clone(), xml, node_id })
}
#[pyfunction]
pub fn numbering_add(package: &Package, levels: Vec<Level>) -> Result<NumberingInstance> {
    if !(1..=9).contains(&levels.len()) { return Err(invalid("Numbering needs 1–9 Level values")); }
    let mut source = Document::from_element(text::word_element("abstractNum"));
    let root = source.root;
    append_value(&mut source, root, "multiLevelType", if levels.len() == 1 { "singleLevel" } else { "multilevel" })?;
    for (index, level) in levels.iter().enumerate() { level.append(&mut source, root, index)?; }
    let xml = part_xml(package, "NumberingDefinitionsPart", true)?.unwrap();
    let node_id = xml.edit(|doc| {
        let abstract_id = next_id(doc, doc.root, "abstractNum", "abstractNumId", 0)?;
        let ident = next_id(doc, doc.root, "num", "numId", 1)?;
        set_word_attribute(&mut source, root, "abstractNumId", &abstract_id.to_string())?;
        text::attach(doc, doc.root, schema::insertion_position(doc, doc.root, W, "abstractNum")?, &source)?;
        let position = schema::insertion_position(doc, doc.root, W, "num")?;
        let id = doc.insert_kind(doc.root, position, NodeKind::Element(text::word_element("num")))?;
        set_word_attribute(doc, id, "numId", &ident.to_string())?;
        append_value(doc, id, "abstractNumId", &abstract_id.to_string())?;
        Ok(id)
    })?;
    Ok(NumberingInstance { package: package.clone(), xml, node_id })
}
