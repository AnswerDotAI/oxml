//! Selected subtree import with per-operation dependency and identifier maps.
use crate::{definitions::{find_id, set_word_attribute, word_attribute}, error::{Error, Result},
    package::{Package, PackageData, Relationship}, package_schema::{declared_uri, story_nodes}, schema::insertion_position,
    text::{self, unique_child, W, W14}, xml::{Document, Xml}};
use pyo3::prelude::*;
use std::collections::{HashMap, HashSet};

const R: &str = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
const WP: &str = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing";
const PIC: &str = "http://schemas.openxmlformats.org/drawingml/2006/picture";
const MC: &str = "http://schemas.openxmlformats.org/markup-compatibility/2006";
const A: &str = "http://schemas.openxmlformats.org/drawingml/2006/main";
const O: &str = "urn:schemas-microsoft-com:office:office";
fn invalid(message: impl Into<String>) -> Error { Error::Invalid(message.into()) }
fn unsupported(message: impl Into<String>) -> Error { Error::Unsupported(message.into()) }
fn integer(value: Option<&str>, radix: u32) -> Result<i64> {
    i64::from_str_radix(value.ok_or_else(|| invalid("Missing imported identifier"))?, radix).map_err(|_| invalid("Invalid imported identifier"))
}

fn fresh(value: &str, used: &mut HashSet<String>, limit: Option<usize>) -> String {
    let mut candidate = value.to_string();
    let mut index = 0;
    while used.contains(&candidate) {
        index += 1;
        let suffix = format!("_imported{index}");
        let base: String = value.chars().take(limit.map_or(usize::MAX, |limit| limit.saturating_sub(suffix.len()))).collect();
        candidate = base + &suffix;
    }
    used.insert(candidate.clone());
    candidate
}

fn inherit_ignorable(source: &Document, id: usize, copy: &mut Document) -> Result<()> {
    let mut namespaces = std::collections::BTreeSet::new();
    let mut ancestor = source.node(id)?.parent;
    while let Some(id) = ancestor {
        let node = source.node(id)?;
        if let Some(e) = node.element() {
            for attr in e.attributes.iter().filter(|a| a.name.uri == MC) {
                if attr.name.local != "Ignorable" { return Err(unsupported("Inherited MC directives other than Ignorable are unsupported")); }
                for prefix in attr.value.split_whitespace() {
                    namespaces.insert(e.namespace(prefix).ok_or_else(|| invalid("Unbound inherited Ignorable prefix"))?.to_string());
                }
            }
        }
        ancestor = node.parent;
    }
    if namespaces.is_empty() { return Ok(()); }
    let mut prefixes = copy.node(copy.root)?.element().unwrap().attribute(MC, "Ignorable").unwrap_or("").split_whitespace().map(str::to_string).collect::<Vec<_>>();
    for uri in namespaces {
        let prefix = copy.ensure_namespace(copy.root, &uri, "imported")?;
        if !prefixes.contains(&prefix) { prefixes.push(prefix); }
    }
    copy.set_attribute_ns(copy.root, MC, "Ignorable", &prefixes.join(" "), "mc")?;
    Ok(())
}

#[derive(Clone, Copy, Hash, Eq, PartialEq)]
enum Kind { Content, Styles, Numbering }
impl Kind {
    fn part(self) -> &'static str { match self { Self::Styles => "StyleDefinitionsPart", Self::Numbering => "NumberingDefinitionsPart", Self::Content => unreachable!() } }
}
struct Entry { kind: Kind, doc: Document }
struct Bookmark { id: String, name: Option<(String, String)>, starts: usize, ends: usize }
struct Link { relationship: Relationship, target: String, refs: Vec<(usize, usize, String, String)> }
struct Image { uri: String, content_type: String, data: Vec<u8> }
struct Import<'a> {
    source: &'a mut PackageData,
    destination: &'a mut PackageData,
    parent: Xml,
    parent_id: usize,
    target_uri: String,
    entries: Vec<Entry>,
    styles: HashMap<String, String>,
    nums: HashMap<i64, String>,
    abstracts: HashMap<i64, String>,
    style_ids: HashSet<String>,
    style_names: HashSet<String>,
    part_names: HashSet<String>,
    ids: HashMap<&'static str, HashSet<i64>>,
    bookmarks: HashMap<(String, i64), Bookmark>,
    bookmark_names: HashSet<String>,
    anchors: Vec<(usize, usize, String)>,
    relationships: HashMap<(String, Kind, String), Link>,
    images: HashMap<String, Image>,
    source_rels: HashMap<String, Vec<Relationship>>,
    source_definitions: HashMap<Kind, (String, Xml)>,
}

impl<'a> Import<'a> {
    fn new(source: &'a mut PackageData, destination: &'a mut PackageData, parent: Xml, parent_id: usize, target_uri: String) -> Result<Self> {
        let part_names = destination.part_names().into_iter().map(|name| name.to_lowercase()).collect();
        let mut operation = Self { source, destination, parent, parent_id, target_uri, part_names, entries: Vec::new(),
            styles: HashMap::new(), nums: HashMap::new(), abstracts: HashMap::new(), style_ids: HashSet::new(), style_names: HashSet::new(),
            ids: HashMap::new(), bookmarks: HashMap::new(), bookmark_names: HashSet::new(), anchors: Vec::new(),
            relationships: HashMap::new(), images: HashMap::new(), source_rels: HashMap::new(), source_definitions: HashMap::new() };
        if let Some(uri) = declared_uri(operation.destination, Kind::Styles.part(), false)? {
            let xml = operation.destination.load_xml(&uri)?;
            let doc = xml.read()?;
            for id in doc.node(doc.root)?.children.iter().copied().filter(|&id| text::name(&doc, id) == Some("style")) {
                if let Some(id) = word_attribute(&doc, id, "styleId") { operation.style_ids.insert(id.to_string()); }
                if let Some(name) = unique_child(&doc, id, "name")?.and_then(|id| word_attribute(&doc, id, "val")) { operation.style_names.insert(name.to_string()); }
            }
        }
        for (uri, root) in story_nodes(operation.destination)? {
            let xml = operation.destination.load_xml(&uri)?;
            let doc = xml.read()?;
            for id in doc.descendants(root)? {
                let e = doc.node(id)?.element().unwrap();
                if e.name.uri == W && matches!(e.name.local.as_str(), "bookmarkStart" | "bookmarkEnd") {
                    operation.ids.entry("bookmark").or_default().insert(integer(e.attribute(W, "id"), 10)?);
                    if e.name.local == "bookmarkStart" { if let Some(name) = e.attribute(W, "name") { operation.bookmark_names.insert(name.to_string()); } }
                }
                if matches!((e.name.uri.as_str(), e.name.local.as_str()), (WP, "docPr") | (PIC, "cNvPr")) {
                    operation.ids.entry("drawing").or_default().insert(integer(e.attribute("", "id"), 10)?);
                }
                if let Some(value) = e.attribute(W14, "paraId") { operation.ids.entry("paragraph").or_default().insert(integer(Some(value), 16)?); }
            }
        }
        if let Some(uri) = declared_uri(operation.destination, Kind::Numbering.part(), false)? {
            let xml = operation.destination.load_xml(&uri)?;
            let doc = xml.read()?;
            for id in doc.element_ids() {
                for (local, attr, kind, radix) in [("num", "numId", "num", 10), ("abstractNum", "abstractNumId", "abstract", 10),
                    ("num", "durableId", "durable", 10), ("nsid", "val", "nsid", 16)] {
                    if text::name(&doc, id) == Some(local) { if let Some(value) = word_attribute(&doc, id, attr) { operation.ids.entry(kind).or_default().insert(integer(Some(value), radix)?); } }
                }
            }
        }
        Ok(operation)
    }

    fn allocate(&mut self, kind: &'static str, hexadecimal: bool, mut start: i64) -> Result<String> {
        let used = self.ids.entry(kind).or_default();
        while used.contains(&start) { start = start.checked_add(1).ok_or_else(|| invalid("Imported identifier space exhausted"))?; }
        used.insert(start);
        Ok(if hexadecimal { format!("{start:08X}") } else { start.to_string() })
    }

    fn copy(&mut self, source: &Document, id: usize, kind: Kind) -> Result<usize> {
        let mut doc = source.subtree(id)?;
        inherit_ignorable(source, id, &mut doc)?;
        let entry = self.entries.len();
        self.entries.push(Entry { kind, doc });
        Ok(entry)
    }

    fn source_definition(&mut self, kind: Kind) -> Result<(String, Xml)> {
        if !self.source_definitions.contains_key(&kind) {
            let uri = declared_uri(self.source, kind.part(), false)?.ok_or_else(|| invalid(format!("Missing {} part", kind.part())))?;
            let xml = self.source.load_xml(&uri)?;
            self.source_definitions.insert(kind, (uri, xml));
        }
        Ok(self.source_definitions[&kind].clone())
    }

    fn style(&mut self, ident: &str) -> Result<String> {
        if let Some(mapped) = self.styles.get(ident) { return Ok(mapped.clone()); }
        let (uri, xml) = self.source_definition(Kind::Styles)?;
        let doc = xml.read()?;
        let mut matches = doc.node(doc.root)?.children.iter().copied().filter(|&id|
            text::name(&doc, id) == Some("style") && word_attribute(&doc, id, "styleId") == Some(ident));
        let id = matches.next().ok_or_else(|| invalid(format!("Missing style {ident}")))?;
        if matches.next().is_some() { return Err(invalid(format!("Ambiguous style {ident}"))); }
        let mapped = fresh(ident, &mut self.style_ids, None);
        self.styles.insert(ident.to_string(), mapped.clone());
        let entry = self.copy(&doc, id, Kind::Styles)?;
        drop(matches);
        drop(doc);
        let copied = &mut self.entries[entry].doc;
        set_word_attribute(copied, copied.root, "styleId", &mapped)?;
        copied.remove_attribute(copied.root, W, "default")?;
        if let Some(name) = unique_child(copied, copied.root, "name")? {
            let value = word_attribute(copied, name, "val").ok_or_else(|| invalid("Style name has no value"))?;
            let name_value = fresh(value, &mut self.style_names, None);
            set_word_attribute(copied, name, "val", &name_value)?;
        }
        self.scan(entry, &uri)?;
        Ok(mapped)
    }

    fn numbering(&mut self, ident: i64, abstract_definition: bool) -> Result<String> {
        if !abstract_definition && ident == 0 { return Ok("0".into()); }
        if let Some(mapped) = if abstract_definition { self.abstracts.get(&ident) } else { self.nums.get(&ident) } { return Ok(mapped.clone()); }
        let (local, attr, kind, start) = if abstract_definition { ("abstractNum", "abstractNumId", "abstract", 0) } else { ("num", "numId", "num", 1) };
        let (uri, xml) = self.source_definition(Kind::Numbering)?;
        let doc = xml.read()?;
        let id = find_id(&doc, doc.root, local, attr, ident)?.ok_or_else(|| invalid(format!("Missing {local} numbering definition")))?;
        let mapped = self.allocate(kind, false, start)?;
        if abstract_definition { self.abstracts.insert(ident, mapped.clone()); } else { self.nums.insert(ident, mapped.clone()); }
        let entry = self.copy(&doc, id, Kind::Numbering)?;
        drop(doc);
        let root = self.entries[entry].doc.root;
        set_word_attribute(&mut self.entries[entry].doc, root, attr, &mapped)?;
        if word_attribute(&self.entries[entry].doc, root, "durableId").is_some() {
            let value = self.allocate("durable", false, 1)?;
            set_word_attribute(&mut self.entries[entry].doc, root, "durableId", &value)?;
        }
        self.scan(entry, &uri)?;
        Ok(mapped)
    }

    fn relationship(&mut self, entry: usize, node: usize, source_uri: &str, uri: &str, local: &str, ident: &str) -> Result<()> {
        let key = (source_uri.to_string(), self.entries[entry].kind, ident.to_string());
        if !self.relationships.contains_key(&key) {
            if !self.source_rels.contains_key(source_uri) { self.source_rels.insert(source_uri.to_string(), self.source.relationships(source_uri)?); }
            let rel = self.source_rels[source_uri].iter().find(|r| r.id == ident).ok_or_else(|| invalid(format!("Missing relationship {ident} in {source_uri}")))?.clone();
            if rel.kind != format!("{R}/image") && rel.kind != format!("{R}/hyperlink") { return Err(unsupported(format!("Unsupported imported relationship: {}", rel.kind))); }
            let mut target = rel.target.clone();
            if rel.mode == "Internal" {
                if rel.kind != format!("{R}/image") { return Err(unsupported("Internal hyperlink package targets are unsupported")); }
                let source = self.source.relationship_target(source_uri, &rel.target)?;
                if !self.images.contains_key(&source) {
                    if !self.source.relationships(&source)?.is_empty() { return Err(unsupported("Image has additional package dependencies")); }
                    let base = source.rsplit('/').next().unwrap();
                    let (stem, extension) = base.rsplit_once('.').map_or((base, String::new()), |(stem, extension)| (stem, format!(".{extension}")));
                    let directory = self.destination.main_part().rsplit_once('/').map_or("", |(directory, _)| directory);
                    let stem = format!("{directory}/media/{stem}");
                    let mut name = format!("{stem}{extension}");
                    let mut index = 0;
                    while self.part_names.contains(&name.to_lowercase()) { index += 1; name = format!("{stem}_{index}{extension}"); }
                    self.part_names.insert(name.to_lowercase());
                    self.images.insert(source.clone(), Image { uri: name, content_type: self.source.content_type(&source)?, data: self.source.data(&source)? });
                }
                target = self.images[&source].uri.clone();
            }
            self.relationships.insert(key.clone(), Link { relationship: rel, target, refs: Vec::new() });
        }
        self.relationships.get_mut(&key).unwrap().refs.push((entry, node, uri.into(), local.into()));
        Ok(())
    }

    fn scan(&mut self, entry: usize, source_uri: &str) -> Result<()> {
        let ids = self.entries[entry].doc.element_ids();
        for id in ids {
            let doc = &self.entries[entry].doc;
            let e = doc.node(id)?.element().unwrap();
            let (uri, local) = (e.name.uri.clone(), e.name.local.clone());
            let attributes = e.attributes.clone();
            if text::revision_name(doc, id).is_some() || uri == W && matches!(local.as_str(), "commentRangeStart" | "commentRangeEnd" | "commentReference" |
                "annotationRef" | "footnoteReference" | "endnoteReference" | "sectPr" | "altChunk" | "object" | "sdt" | "fldSimple" | "fldChar" |
                "instrText" | "dataBinding" | "lvlPicBulletId" | "permStart" | "permEnd") {
                return Err(unsupported(format!("Unsupported imported structure: {local}")));
            }
            if uri == "urn:schemas-microsoft-com:vml" && local == "shape" { return Err(unsupported("VML shape identity/dependencies are unsupported")); }
            if uri == A && matches!(local.as_str(), "stCxn" | "endCxn") { return Err(unsupported("Drawing connector identities are unsupported")); }
            if uri == W {
                let value = word_attribute(doc, id, "val").map(str::to_owned);
                let mapped = match local.as_str() {
                    "pStyle" | "rStyle" | "tblStyle" | "basedOn" | "next" | "link" | "styleLink" | "numStyleLink" => Some(self.style(value.as_deref().ok_or_else(|| invalid("Style reference has no value"))?)?),
                    "numId" => Some(self.numbering(integer(value.as_deref(), 10)?, false)?),
                    "abstractNumId" => Some(self.numbering(integer(value.as_deref(), 10)?, true)?),
                    "nsid" => Some(self.allocate("nsid", true, 1)?),
                    _ => None,
                };
                if let Some(value) = mapped { set_word_attribute(&mut self.entries[entry].doc, id, "val", &value)?; }
                if matches!(local.as_str(), "bookmarkStart" | "bookmarkEnd") {
                    let doc = &self.entries[entry].doc;
                    let key = (source_uri.to_string(), integer(word_attribute(doc, id, "id"), 10)?);
                    let name = word_attribute(doc, id, "name").map(str::to_string);
                    if !self.bookmarks.contains_key(&key) {
                        let id = self.allocate("bookmark", false, 1)?;
                        self.bookmarks.insert(key.clone(), Bookmark { id, name: None, starts: 0, ends: 0 });
                    }
                    let bookmark = self.bookmarks.get_mut(&key).unwrap();
                    set_word_attribute(&mut self.entries[entry].doc, id, "id", &bookmark.id)?;
                    if local == "bookmarkStart" {
                        bookmark.starts += 1;
                        let name = name.filter(|n| !n.is_empty()).ok_or_else(|| invalid("Bookmark has no name"))?;
                        let new_name = fresh(&name, &mut self.bookmark_names, Some(40));
                        set_word_attribute(&mut self.entries[entry].doc, id, "name", &new_name)?;
                        bookmark.name = Some((name, new_name));
                    } else { bookmark.ends += 1; }
                } else if local == "hyperlink" {
                    let e = self.entries[entry].doc.node(id)?.element().unwrap();
                    if e.attribute(R, "id").is_none() { if let Some(name) = e.attribute(W, "anchor") { self.anchors.push((entry, id, name.to_string())); } }
                }
            }
            if matches!((uri.as_str(), local.as_str()), (WP, "docPr") | (PIC, "cNvPr")) {
                let value = self.allocate("drawing", false, 1)?;
                self.entries[entry].doc.set_attribute(id, "", "id", &value, None)?;
            }
            if self.entries[entry].doc.node(id)?.element().unwrap().attribute(W14, "paraId").is_some() {
                let value = self.allocate("paragraph", true, 1)?;
                self.entries[entry].doc.set_attribute(id, W14, "paraId", &value, None)?;
            }
            for attr in attributes {
                if attr.name.uri == R || attr.name.uri == O && attr.name.local == "relid" {
                    self.relationship(entry, id, source_uri, &attr.name.uri, &attr.name.local, &attr.value)?;
                }
            }
        }
        Ok(())
    }

    fn finish(mut self, mut index: usize) -> Result<Vec<usize>> {
        let mut names = HashMap::new();
        for bookmark in self.bookmarks.values() {
            if bookmark.starts != 1 || bookmark.ends != 1 { return Err(unsupported("Import must include complete, unique bookmark ranges")); }
            let (old, new) = bookmark.name.as_ref().unwrap();
            if names.insert(old, new).is_some() { return Err(invalid("Ambiguous imported bookmark name")); }
        }
        for (entry, id, name) in &self.anchors {
            let mapped = names.get(name).ok_or_else(|| unsupported("Internal hyperlink target is outside the imported content"))?;
            set_word_attribute(&mut self.entries[*entry].doc, *id, "anchor", mapped)?;
        }
        // Dependencies and unsupported structures were checked on selected-subtree copies, not live destination content.
        let mut targets = HashMap::from([(Kind::Content, (self.target_uri.clone(), self.parent.clone(), self.parent_id))]);
        for kind in [Kind::Styles, Kind::Numbering] {
            if self.entries.iter().any(|entry| entry.kind == kind) {
                let uri = declared_uri(self.destination, kind.part(), true)?.unwrap();
                let xml = self.destination.load_xml(&uri)?;
                let root = xml.root()?;
                targets.insert(kind, (uri, xml, root));
            }
        }
        for image in self.images.values() { self.destination.add_part(&image.uri, &image.content_type, &image.data)?; }
        for ((_, kind, _), link) in &self.relationships {
            let ident = self.destination.add_relationship(&targets[kind].0, &link.relationship.kind, &link.target, &link.relationship.mode, None)?;
            for (entry, id, uri, local) in &link.refs { self.entries[*entry].doc.set_attribute(*id, uri, local, &ident, None)?; }
        }
        let mut result = Vec::new();
        for entry in self.entries {
            let (_, xml, parent) = &targets[&entry.kind];
            let inserted = xml.edit(|doc| {
                let position = if entry.kind == Kind::Content { index } else {
                    let name = &entry.doc.node(entry.doc.root)?.element().unwrap().name;
                    insertion_position(doc, *parent, &name.uri, &name.local)?
                };
                text::attach(doc, *parent, position, &entry.doc)
            })?;
            if entry.kind == Kind::Content { result.push(inserted); index += 1; }
        }
        Ok(result)
    }
}

pub fn import_blocks(source: &Package, elements: &[(Xml, usize)], destination: &Package, parent: &Xml, parent_id: usize, index: Option<usize>) -> Result<Vec<usize>> {
    if source.same_state(destination) { return Err(invalid("Use copy_to for copies within one document")); }
    if elements.is_empty() { return Ok(Vec::new()); }
    let target_uri = destination.owner(parent)?;
    let mut selections = Vec::new();
    for (xml, id) in elements {
        let uri = source.owner(xml)?;
        let doc = xml.read()?;
        if !matches!(text::name(&doc, *id), Some("p" | "tbl")) { return Err(invalid("Import accepts paragraph/table blocks")); }
        text::context(&doc, *id)?;
        if selections.iter().any(|(selected_uri, selected_id)| selected_uri == &uri && selected_id == id) { return Err(invalid("Import selection contains duplicate blocks")); }
        selections.push((uri, *id));
    }
    for ((xml, id), (uri, _)) in elements.iter().zip(&selections) {
        let doc = xml.read()?;
        let mut ancestor = doc.node(*id)?.parent;
        while let Some(id) = ancestor {
            if selections.iter().any(|(selected_uri, selected_id)| selected_uri == uri && *selected_id == id) { return Err(invalid("Import selection contains nested blocks")); }
            ancestor = doc.node(id)?.parent;
        }
    }
    let position = {
        let doc = parent.read()?;
        if !matches!(text::name(&doc, parent_id), Some("body" | "hdr" | "ftr" | "tc")) { return Err(invalid("Import destination must be a body, header, footer or table cell")); }
        text::context(&doc, parent_id)?;
        let children = &doc.node(parent_id)?.children;
        let position = index.unwrap_or_else(|| children.iter().position(|&id| text::name(&doc, id) == Some("sectPr")).unwrap_or(children.len()));
        if position > children.len() { return Err(invalid("Invalid import index")); }
        if text::name(&doc, parent_id) == Some("tc") && position == children.len() {
            let (xml, id) = elements.last().unwrap();
            if text::name(&*xml.read()?, *id) != Some("p") { return Err(invalid("A table cell must end in a paragraph; insert before its final paragraph")); }
        }
        position
    };
    let mut source_data = source.lock()?;
    let mut destination_data = destination.lock()?;
    let mut operation = Import::new(&mut source_data, &mut destination_data, parent.clone(), parent_id, target_uri)?;
    for ((xml, id), (uri, _)) in elements.iter().zip(&selections) {
        let entry = operation.copy(&*xml.read()?, *id, Kind::Content)?;
        operation.scan(entry, uri)?;
    }
    operation.finish(position)
}

#[pyfunction]
#[pyo3(signature=(source, elements, destination, parent, parent_id, index=None))]
pub fn import_content(py: Python<'_>, source: &Package, elements: Vec<(Xml, usize)>, destination: &Package, parent: &Xml, parent_id: usize, index: Option<usize>) -> Result<Vec<usize>> {
    py.detach(|| import_blocks(source, &elements, destination, parent, parent_id, index))
}
