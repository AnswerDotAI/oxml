//! Core properties and flat document settings over the native package and schema.
use crate::{error::{Error, Result}, package::Package, package_schema, schema::{self, s}, text::{self, W},
    xml::{Document, Element, Name, Xml}};
use pyo3::prelude::*;

const CORE_REL: &str = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties";
const CORE_CT: &str = "application/vnd.openxmlformats-package.core-properties+xml";
const XSI: &str = "http://www.w3.org/2001/XMLSchema-instance";

fn core_prefix(name: &str) -> Result<&'static str> {
    match name {
        "title" | "subject" | "creator" | "description" | "identifier" | "language" => Ok("dc"),
        "keywords" | "category" | "contentStatus" | "contentType" | "lastModifiedBy" | "lastPrinted" | "revision" | "version" => Ok("cp"),
        "created" | "modified" => Ok("dcterms"),
        _ => Err(Error::Missing(format!("Unknown core property {name}"))),
    }
}
fn element(prefix: &str, local: &str) -> Element {
    let uri = s(&schema::schema()["namespaces"][prefix]);
    Element { name: Name { uri: uri.into(), local: local.into(), prefix: prefix.into() }, attributes: Vec::new(),
        namespaces: vec![("".into(), "".into()), ("xml".into(), text::XML.into()), (prefix.into(), uri.into())] }
}
fn find(doc: &Document, uri: &str, local: &str) -> Result<Option<usize>> {
    let mut found = None;
    for id in doc.element_children(doc.root)? {
        let name = &doc.node(id)?.element().unwrap().name;
        if name.uri != uri || name.local != local { continue; }
        if found.replace(id).is_some() { return Err(Error::Invalid(format!("Multiple {local} values"))); }
    }
    Ok(found)
}

#[pyclass(name = "NativeProperties", module = "oxml._core")]
pub struct Properties { package: Package }
impl Properties {
    fn xml(&self, create: bool) -> Result<Option<Xml>> {
        let mut package = self.package.lock()?;
        let mut relationships = package.relationships("/")?.into_iter().filter(|r| r.kind == CORE_REL);
        let relation = relationships.next();
        if relationships.next().is_some() { return Err(Error::Invalid("Multiple core-properties relationships".into())); }
        let uri = if let Some(relation) = relation {
            if relation.mode != "Internal" { return Err(Error::Invalid("Core properties must be an internal part".into())); }
            package.relationship_target("/", &relation.target)?
        } else {
            if !create { return Ok(None); }
            let uri = (0..).map(|i| if i == 0 { "/docProps/core.xml".into() } else { format!("/docProps/core{i}.xml") })
                .find(|uri| package.existing(uri).is_err()).unwrap();
            let root = Document::from_element(element("cp", "coreProperties"));
            package.add_part(&uri, CORE_CT, &root.serialize()?)?;
            package.add_relationship("/", CORE_REL, &uri, "Internal", None)?;
            uri
        };
        let xml = package.load_xml(&uri)?;
        let expected = element("cp", "coreProperties");
        let doc = xml.read()?;
        let actual = &doc.node(doc.root)?.element().unwrap().name;
        if package.content_type(&uri)? != CORE_CT || (actual.uri.as_str(), actual.local.as_str()) != (expected.name.uri.as_str(), "coreProperties") {
            return Err(Error::Invalid("Unexpected core-properties part structure".into()));
        }
        drop(doc);
        Ok(Some(xml))
    }
}
#[pymethods]
impl Properties {
    #[new]
    pub fn new(package: Package) -> Self { Self { package } }
    pub fn get(&self, name: &str) -> Result<String> {
        let expected = element(core_prefix(name)?, name);
        let xml = self.xml(false)?.ok_or_else(|| Error::Missing(name.into()))?;
        let id = { let doc = xml.read()?; find(&doc, &expected.name.uri, name)?.ok_or_else(|| Error::Missing(name.into()))? };
        xml.text(id)
    }
    pub fn set(&self, name: &str, value: &str) -> Result<()> {
        let prefix = core_prefix(name)?;
        let mut source = Document::from_element(element(prefix, name));
        source.set_text(source.root, value)?;
        if prefix == "dcterms" { source.set_attribute_ns(source.root, XSI, "type", "dcterms:W3CDTF", "xsi")?; }
        let xml = self.xml(true)?.unwrap();
        xml.edit(|doc| {
            let uri = &source.node(source.root)?.element().unwrap().name.uri;
            if let Some(id) = find(doc, uri, name)? {
                doc.set_text(id, value)?;
                if prefix == "dcterms" {
                    doc.declare_namespace(id, "dcterms", uri)?;
                    doc.set_attribute_ns(id, XSI, "type", "dcterms:W3CDTF", "xsi")?;
                }
            } else { doc.import_at(&source, source.root, doc.root, doc.node(doc.root)?.children.len())?; }
            Ok(())
        })
    }
    pub fn remove(&self, name: &str) -> Result<()> {
        let expected = element(core_prefix(name)?, name);
        let xml = self.xml(false)?.ok_or_else(|| Error::Missing(name.into()))?;
        xml.edit(|doc| {
            let id = find(doc, &expected.name.uri, name)?.ok_or_else(|| Error::Missing(name.into()))?;
            doc.remove(id)
        })
    }
    pub fn keys(&self) -> Result<Vec<String>> {
        let Some(xml) = self.xml(false)? else { return Ok(Vec::new()); };
        let doc = xml.read()?;
        let keys = doc.element_children(doc.root)?.filter_map(|id| {
            let name = &doc.node(id).ok()?.element()?.name;
            let prefix = core_prefix(&name.local).ok()?;
            (name.uri == s(&schema::schema()["namespaces"][prefix])).then(|| name.local.clone())
        }).collect();
        Ok(keys)
    }
}

#[derive(Debug, PartialEq, FromPyObject, IntoPyObject)]
pub enum SettingValue {
    #[pyo3(transparent)]
    Bool(bool),
    #[pyo3(transparent)]
    Text(String),
}

#[pyclass(name = "NativeSettings", module = "oxml._core")]
pub struct Settings { package: Package }
impl Settings {
    fn xml(&self, create: bool) -> Result<Option<Xml>> {
        let mut package = self.package.lock()?;
        package_schema::declared_uri(&mut package, "DocumentSettingsPart", create)?.map(|uri| package.load_xml(&uri)).transpose()
    }
}
#[pymethods]
impl Settings {
    #[new]
    pub fn new(package: Package) -> Self { Self { package } }
    #[getter]
    pub fn root(&self) -> Result<Xml> { Ok(self.xml(true)?.unwrap()) }
    pub fn get(&self, name: &str) -> Result<SettingValue> {
        let attribute = schema::setting_attribute(name)?;
        let xml = self.xml(false)?.ok_or_else(|| Error::Missing(name.into()))?;
        let doc = xml.read()?;
        let id = find(&doc, W, name)?.ok_or_else(|| Error::Missing(name.into()))?;
        let value = doc.node(id)?.element().unwrap().attribute(W, "val");
        let kind = s(&attribute["Type"]);
        if value.is_none() && kind == "OnOffValue" { return Ok(SettingValue::Bool(true)); }
        let value = value.ok_or_else(|| Error::Invalid(format!("Missing {name}/@w:val")))?;
        schema::checked_attribute(attribute, name, value, false)?;
        Ok(match schema::boolean(kind, value) { Some(b) => SettingValue::Bool(b), None => SettingValue::Text(value.into()) })
    }
    pub fn set(&self, name: &str, value: SettingValue) -> Result<()> {
        let attribute = schema::setting_attribute(name)?;
        let kind = s(&attribute["Type"]);
        let bare = matches!(value, SettingValue::Bool(true)) && kind == "OnOffValue";
        let value = match value {
            SettingValue::Bool(b) if matches!(kind, "OnOffValue" | "BooleanValue") => b.to_string(),
            SettingValue::Bool(_) => return Err(Error::Invalid(format!("{name} is not an on/off setting"))),
            SettingValue::Text(s) => s,
        };
        schema::checked_attribute(attribute, name, &value, true)?;
        let xml = self.xml(true)?.unwrap();
        xml.edit(|doc| {
            let id = crate::definitions::ensure(doc, doc.root, name)?;
            if bare { doc.remove_attribute(id, W, "val") } else { crate::definitions::set_word_attribute(doc, id, "val", &value) }
        })
    }
    pub fn remove(&self, name: &str) -> Result<()> {
        schema::setting_attribute(name)?;
        let xml = self.xml(false)?.ok_or_else(|| Error::Missing(name.into()))?;
        xml.edit(|doc| { let id = find(doc, W, name)?.ok_or_else(|| Error::Missing(name.into()))?; doc.remove(id) })
    }
    pub fn keys(&self) -> Result<Vec<String>> {
        let Some(xml) = self.xml(false)? else { return Ok(Vec::new()); };
        let doc = xml.read()?;
        let keys = doc.element_children(doc.root)?.filter_map(|id| {
            let name = &doc.node(id).ok()?.element()?.name;
            (name.uri == W && schema::setting_attribute(&name.local).is_ok()).then(|| name.local.clone())
        }).collect();
        Ok(keys)
    }
}
