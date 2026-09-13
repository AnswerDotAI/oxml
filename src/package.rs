use pyo3::exceptions::{PyKeyError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};
use crate::xml::{self, Attribute, Document, Element, Name, NodeKind, check_local, xml_char};
use std::collections::{BTreeMap, HashSet};
use std::io::{Cursor, Read, Write};
use std::path::Path;
use std::sync::Arc;
use zip::write::SimpleFileOptions;
use zip::{CompressionMethod, ZipArchive, ZipWriter};

const TYPES: &str = "/[Content_Types].xml";
const CT_NS: &str = "http://schemas.openxmlformats.org/package/2006/content-types";
const REL_NS: &str = "http://schemas.openxmlformats.org/package/2006/relationships";
const REL_CT: &str = "application/vnd.openxmlformats-package.relationships+xml";
const MAIN_CT: &str = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml";
const OFFICE_REL: &str = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument";
const STRICT_REL: &str = "http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument";
const MAX_ENTRIES: usize = 10_000;
const MAX_PART: u64 = 256 * 1024 * 1024;
const MAX_TOTAL: u64 = 1024 * 1024 * 1024;
const MAX_ARCHIVE: usize = 512 * 1024 * 1024;
const MAX_METADATA: u64 = 8 * 1024 * 1024;

fn invalid(message: impl ToString) -> PyErr { PyValueError::new_err(message.to_string()) }
fn zip_error(error: impl std::fmt::Display) -> PyErr { invalid(format!("Invalid or unsupported ZIP archive: {error}")) }

// This preflight does not read ZIP entries. zip-rs silently deduplicates its index, so
// retain the bounded EOCD entry count before handing parsing to the ZIP implementation.
fn zip_entry_count(data: &[u8]) -> PyResult<usize> {
    if data.len() > MAX_ARCHIVE { return Err(invalid("Archive exceeds the 512 MiB limit")); }
    if data.starts_with(&[0xd0, 0xcf, 0x11, 0xe0]) { return Err(invalid("Encrypted/compound Office packages are unsupported")); }
    let end = (0..data.len().saturating_sub(21)).rev().take(65_536).find(|&i| {
        data[i..].starts_with(b"PK\x05\x06") && i + 22 + u16::from_le_bytes([data[i + 20], data[i + 21]]) as usize == data.len()
    }).ok_or_else(|| invalid("Missing or malformed ZIP end-of-central-directory record"))?;
    let u16_at = |i| u16::from_le_bytes([data[end + i], data[end + i + 1]]);
    if end >= 20 && &data[end - 20..end - 16] == b"PK\x06\x07" || u16_at(10) == u16::MAX
        || data[end + 12..end + 16] == [255; 4] || data[end + 16..end + 20] == [255; 4] {
        return Err(invalid("ZIP64 archives are unsupported"));
    }
    if u16_at(4) != 0 || u16_at(6) != 0 || u16_at(8) != u16_at(10) { return Err(invalid("Multidisk ZIP archives are unsupported")); }
    let count = u16_at(10) as usize;
    if count > MAX_ENTRIES { return Err(invalid("Archive exceeds the 10,000 entry limit")); }
    let u32_at = |i| u32::from_le_bytes(data[end + i..end + i + 4].try_into().unwrap()) as usize;
    let (mut pos, size) = (u32_at(16), u32_at(12));
    if pos.checked_add(size) != Some(end) { return Err(invalid("Invalid ZIP central-directory bounds")); }
    for _ in 0..count {
        let header = data.get(pos..pos.saturating_add(46)).ok_or_else(|| invalid("Truncated ZIP central directory"))?;
        if !header.starts_with(b"PK\x01\x02") { return Err(invalid("Invalid ZIP central-directory entry count")); }
        pos += 46 + [28, 30, 32].iter().map(|&i| u16::from_le_bytes([header[i], header[i + 1]]) as usize).sum::<usize>();
        if pos > end { return Err(invalid("ZIP central-directory entry exceeds its bounds")); }
    }
    if pos != end { return Err(invalid("ZIP central-directory count does not cover every entry")); }
    Ok(count)
}

fn part_uri(uri: &str) -> PyResult<()> {
    if uri == TYPES { return Ok(()); }
    if !uri.starts_with('/') || uri.len() < 2 || uri.ends_with('/') { return Err(invalid(format!("Invalid OPC part URI: {uri}"))); }
    for segment in uri[1..].split('/') {
        if segment.is_empty() || segment.ends_with('.') { return Err(invalid(format!("Unsafe OPC part URI: {uri}"))); }
        let bytes = segment.as_bytes();
        let mut i = 0;
        while i < bytes.len() {
            let c = bytes[i];
            if c == b'%' {
                let hex = bytes.get(i + 1..i + 3).and_then(|s| std::str::from_utf8(s).ok()).and_then(|s| u8::from_str_radix(s, 16).ok())
                    .ok_or_else(|| invalid(format!("Invalid percent escape in OPC part URI: {uri}")))?;
                if hex.is_ascii_alphanumeric() || b"-._~/\\".contains(&hex) || hex < 32 || hex == 127 {
                    return Err(invalid(format!("Unsafe/equivalent percent escape in OPC part URI: {uri}")));
                }
                i += 3;
            } else {
                if !c.is_ascii_alphanumeric() && !b"-._~!$&'()*+,;=@".contains(&c) {
                    return Err(invalid(format!("Unsafe character in OPC part URI: {uri}")));
                }
                i += 1;
            }
        }
    }
    Ok(())
}

fn rels_uri(source: &str) -> PyResult<String> {
    if source == "/" { return Ok("/_rels/.rels".into()); }
    part_uri(source)?;
    let (dir, name) = source.rsplit_once('/').unwrap();
    Ok(format!("{dir}/_rels/{name}.rels"))
}

fn rels_source(uri: &str) -> Option<String> {
    if uri.eq_ignore_ascii_case("/_rels/.rels") { return Some("/".into()); }
    let (dir, name) = uri.rsplit_once('/')?;
    if !dir.to_ascii_lowercase().ends_with("/_rels") || !name.to_ascii_lowercase().ends_with(".rels") { return None; }
    Some(format!("{}/{}", &dir[..dir.len() - 6], &name[..name.len() - 5]))
}

fn resolve_target(source: &str, target: &str) -> PyResult<String> {
    let path = target.split('#').next().unwrap();
    if path.contains(['?', '\\']) || path.starts_with("//") || path.split('/').next().unwrap().contains(':') {
        return Err(invalid(format!("Unsafe internal relationship target: {target}")));
    }
    let joined = if path.starts_with('/') { path.to_string() }
        else { format!("{}/{path}", source.rsplit_once('/').unwrap().0) };
    let mut segments = Vec::new();
    for segment in joined.split('/').skip(1) {
        match segment {
            "." => (),
            ".." => { if segments.pop().is_none() { return Err(invalid("Relationship target escapes the package root")); } },
            _ => segments.push(segment),
        }
    }
    let result = format!("/{}", segments.join("/"));
    part_uri(&result)?;
    if result == TYPES || rels_source(&result).is_some() { return Err(invalid("Internal relationships cannot target package metadata")); }
    Ok(result)
}

fn required<'a>(element: &'a Element, name: &str) -> PyResult<&'a str> {
    element.attribute("", name).filter(|s| !s.is_empty())
        .ok_or_else(|| invalid(format!("Missing {name} on OPC {}", element.name.local)))
}

// OPC-specific rules and edits over the same XML tree used for document parts.
struct Metadata { doc: Document }
impl Metadata {
    fn read(data: &[u8], relationships: bool) -> PyResult<Self> {
        let doc = xml::parse_bytes_limited(data, MAX_METADATA as usize, 129)?;
        let root = doc.node(doc.root)?.element().unwrap();
        let (namespace, name) = if relationships { (REL_NS, "Relationships") } else { (CT_NS, "Types") };
        if root.name.uri != namespace || root.name.local != name { return Err(invalid("Invalid OPC metadata root")); }
        let result = Self { doc };
        for &id in &result.doc.node(result.doc.root)?.children {
            let node = result.doc.node(id)?;
            match &node.kind {
                NodeKind::Element(e) if e.name.uri == namespace => {
                    if if relationships { e.name.local != "Relationship" } else { !["Default", "Override"].contains(&e.name.local.as_str()) } {
                        return Err(invalid(format!("Unsupported OPC metadata element: {}", e.name.local)));
                    }
                    for &child in &node.children {
                        match &result.doc.node(child)?.kind {
                            NodeKind::Element(_) => return Err(invalid("OPC metadata records cannot contain elements")),
                            NodeKind::Text(t) if !t.trim().is_empty() => return Err(invalid("Unexpected text in OPC metadata")),
                            _ => (),
                        }
                    }
                }
                NodeKind::Text(t) if !t.trim().is_empty() => return Err(invalid("Unexpected text in OPC metadata")),
                _ => (),
            }
        }
        Ok(result)
    }

    fn records(&self) -> impl Iterator<Item = (usize, &Element)> {
        let root = self.doc.node(self.doc.root).unwrap();
        let namespace = &root.element().unwrap().name.uri;
        root.children.iter().filter_map(move |&id| {
            let element = self.doc.node(id).unwrap().element()?;
            (element.name.uri == *namespace).then_some((id, element))
        })
    }

    fn write(mut self, remove: &[usize], append: Option<(&str, &[(&str, &str)])>) -> PyResult<Vec<u8>> {
        for &id in remove { self.doc.remove(id)?; }
        if let Some((local, attrs)) = append {
            let root = self.doc.node(self.doc.root)?.element().unwrap();
            let name = Name { uri: root.name.uri.clone(), prefix: root.name.prefix.clone(), local: local.into() };
            let attributes = attrs.iter().map(|&(local, value)| Attribute {
                name: Name { uri: String::new(), prefix: String::new(), local: local.into() }, value: value.into(),
            }).collect();
            let element = Element { name, attributes, namespaces: root.namespaces.clone() };
            self.doc.add(Some(self.doc.root), NodeKind::Element(element))?;
        }
        let data = self.doc.serialize()?;
        if data.len() as u64 > MAX_METADATA { return Err(invalid("OPC metadata exceeds the 8 MiB limit")); }
        Ok(data)
    }
}

#[derive(Clone)]
struct Relationship { node_id: usize, id: String, kind: String, target: String, mode: String }

#[pyclass]
pub struct Package {
    original: Arc<[u8]>,
    archive: ZipArchive<Cursor<Arc<[u8]>>>,
    names: BTreeMap<String, String>,
    changes: BTreeMap<String, Option<Vec<u8>>>,
    main: String,
    signed: bool,
}

impl Package {
    fn open(data: &[u8]) -> PyResult<Self> {
        let count = zip_entry_count(data)?;
        let original: Arc<[u8]> = data.into();
        let mut archive = ZipArchive::new(Cursor::new(original.clone())).map_err(zip_error)?;
        if archive.len() != count { return Err(invalid("Duplicate ZIP entry names are forbidden")); }
        if archive.offset() != 0 { return Err(invalid("Prefixed/self-extracting ZIP archives are unsupported")); }
        let mut names = BTreeMap::new();
        let (mut total, mut ranges) = (0_u64, Vec::new());
        let central = archive.central_directory_start();
        for i in 0..archive.len() {
            let entry = archive.by_index_raw(i).map_err(zip_error)?;
            if entry.encrypted() { return Err(invalid("Encrypted ZIP entries are unsupported")); }
            if entry.is_symlink() { return Err(invalid("Symlink ZIP entries are forbidden")); }
            if !matches!(entry.compression(), CompressionMethod::Stored | CompressionMethod::Deflated) { return Err(invalid("Only Stored and Deflated ZIP entries are supported")); }
            let local_start = usize::try_from(entry.header_start()).map_err(invalid)?;
            let central_start = usize::try_from(entry.central_header_start()).map_err(invalid)?;
            let local = data.get(local_start..local_start.saturating_add(30)).ok_or_else(|| invalid("Truncated ZIP local header"))?;
            let central_header = data.get(central_start..central_start.saturating_add(46)).ok_or_else(|| invalid("Truncated ZIP central header"))?;
            let local_name_start = local_start + 30;
            let local_name_end = local_name_start + u16::from_le_bytes([local[26], local[27]]) as usize;
            if !local.starts_with(b"PK\x03\x04") || local[6..10] != central_header[8..12]
                || data.get(local_name_start..local_name_end) != Some(entry.name_raw()) {
                return Err(invalid("ZIP local-header name, flags or compression method disagrees with the central directory"));
            }
            if std::str::from_utf8(entry.name_raw()).ok() != Some(entry.name()) { return Err(invalid("Non-UTF-8 ZIP entry names are unsupported")); }
            let uri = format!("/{}", entry.name().strip_suffix('/').unwrap_or(entry.name()));
            part_uri(&uri)?;
            if names.insert(uri.to_ascii_lowercase(), uri).is_some() { return Err(invalid("Duplicate/equivalent OPC part names are forbidden")); }
            if entry.size() > MAX_PART { return Err(invalid("ZIP entry exceeds the 256 MiB uncompressed limit")); }
            total = total.checked_add(entry.size()).ok_or_else(|| invalid("ZIP size overflow"))?;
            if total > MAX_TOTAL { return Err(invalid("Archive exceeds the 1 GiB uncompressed limit")); }
            let start = entry.data_start().ok_or_else(|| invalid("Missing ZIP payload offset"))?;
            let end = start.checked_add(entry.compressed_size()).ok_or_else(|| invalid("ZIP offset overflow"))?;
            if end > central || entry.header_start() >= start { return Err(invalid("Invalid ZIP entry bounds")); }
            ranges.push((entry.header_start(), end));
        }
        ranges.sort_unstable();
        if ranges.windows(2).any(|pair| pair[0].1 > pair[1].0) { return Err(invalid("Overlapping ZIP entries are forbidden")); }
        // Directories are ZIP records, not OPC parts; preserve them only as raw entries.
        for i in 0..archive.len() {
            let entry = archive.by_index_raw(i).map_err(zip_error)?;
            if entry.is_dir() { names.remove(&format!("/{}", entry.name().trim_end_matches('/')).to_ascii_lowercase()); }
        }
        let mut package = Self { original, archive, names, changes: BTreeMap::new(), main: String::new(), signed: false };
        let types = package.types()?;
        package.signed = types.records().map(|(_, r)| r).any(|r| r.attribute("", "ContentType").is_some_and(|s| s.contains("digital-signature")));
        for uri in package.names.values() {
            if uri.to_ascii_lowercase().starts_with("/_xmlsignatures/") { package.signed = true; }
            if let Some(source) = rels_source(uri) {
                for rel in package.rels(&source)?.1 { if rel.kind.contains("/digital-signature/") { package.signed = true; } }
            }
        }
        let mains: Vec<_> = package.rels("/")?.1.into_iter().filter(|r| r.kind == OFFICE_REL || r.kind == STRICT_REL).collect();
        if mains.len() != 1 || mains[0].mode != "Internal" { return Err(invalid("DOCX needs exactly one internal officeDocument package relationship")); }
        package.main = package.existing(&resolve_target("/", &mains[0].target)?)?;
        if package.content_type(&package.main)? != MAIN_CT { return Err(invalid("The officeDocument part is not a DOCX document main part")); }
        Ok(package)
    }

    fn existing(&self, uri: &str) -> PyResult<String> {
        part_uri(uri)?;
        self.names.get(&uri.to_ascii_lowercase()).filter(|name| self.changes.get(*name) != Some(&None)).cloned()
            .ok_or_else(|| PyKeyError::new_err(format!("No such OPC part: {uri}")))
    }

    fn data(&self, uri: &str) -> PyResult<Vec<u8>> {
        let name = self.existing(uri)?;
        if let Some(Some(data)) = self.changes.get(&name) { return Ok(data.clone()); }
        let mut archive = self.archive.clone();
        let mut entry = archive.by_name(&name[1..]).map_err(zip_error)?;
        let expected = entry.size();
        if (name == TYPES || rels_source(&name).is_some()) && expected > MAX_METADATA { return Err(invalid("OPC metadata exceeds the 8 MiB limit")); }
        let mut data = Vec::new();
        (&mut entry).take(expected + 1).read_to_end(&mut data).map_err(zip_error)?;
        if data.len() as u64 != expected || data.len() as u64 > MAX_PART { return Err(invalid("ZIP payload size does not match the bounded index")); }
        Ok(data)
    }

    fn editable(&self) -> PyResult<()> {
        if self.signed { return Err(invalid("Signed packages are read-only; only unchanged pass-through saving is supported")); }
        Ok(())
    }

    fn ordinary(&self, uri: &str) -> PyResult<()> {
        self.editable()?;
        part_uri(uri)?;
        if uri.eq_ignore_ascii_case(TYPES) || rels_source(uri).is_some() { return Err(invalid("Use content-type/relationship operations for OPC metadata")); }
        if uri.to_ascii_lowercase().starts_with("/_xmlsignatures/") { return Err(invalid("Creating digital signatures is unsupported")); }
        Ok(())
    }

    fn put(&mut self, uri: &str, data: Vec<u8>) {
        let name = self.names.entry(uri.to_ascii_lowercase()).or_insert_with(|| uri.to_string()).clone();
        self.changes.insert(name, Some(data));
    }

    fn types(&self) -> PyResult<Metadata> {
        let metadata = Metadata::read(&self.data(TYPES)?, false)?;
        let mut keys = HashSet::new();
        for (_, record) in metadata.records() {
            let key = required(record, if record.name.local == "Default" { "Extension" } else { "PartName" })?;
            if record.name.local == "Override" { part_uri(key)?; }
            if !keys.insert((record.name.local.clone(), key.to_ascii_lowercase())) { return Err(invalid("Duplicate content-type record")); }
            required(record, "ContentType")?;
        }
        Ok(metadata)
    }

    fn rels(&self, source: &str) -> PyResult<(Metadata, Vec<Relationship>)> {
        let uri = rels_uri(source)?;
        let data = if self.existing(&uri).is_ok() { self.data(&uri)? }
            else { format!(r#"<Relationships xmlns="{REL_NS}"/>"#).into_bytes() };
        let metadata = Metadata::read(&data, true)?;
        let mut ids = HashSet::new();
        let mut result = Vec::new();
        for (node_id, record) in metadata.records() {
            let id = required(record, "Id")?.to_string();
            if !ids.insert(id.clone()) { return Err(invalid("Duplicate relationship Id within one source part")); }
            let mode = record.attribute("", "TargetMode").unwrap_or("Internal");
            if !["Internal", "External"].contains(&mode) { return Err(invalid("Invalid relationship TargetMode")); }
            let target = required(record, "Target")?;
            if mode == "Internal" { resolve_target(source, target)?; }
            result.push(Relationship { node_id, id, kind: required(record, "Type")?.into(), target: target.into(), mode: mode.into() });
        }
        Ok((metadata, result))
    }

    fn set_type(&mut self, uri: &str, content_type: &str) -> PyResult<()> {
        if !content_type.contains('/') || content_type.chars().any(|c| !xml_char(c) || c.is_control() || c.is_whitespace()) { return Err(invalid("Invalid content type")); }
        let metadata = self.types()?;
        let remove: Vec<_> = metadata.records().filter(|(_, r)| r.name.local == "Override" && r.attribute("", "PartName").is_some_and(|p| p.eq_ignore_ascii_case(uri))).map(|(i, _)| i).collect();
        self.put(TYPES, metadata.write(&remove, Some(("Override", &[("PartName", uri), ("ContentType", content_type)])))?);
        Ok(())
    }

    fn output(&self) -> PyResult<Vec<u8>> {
        if self.changes.is_empty() { return Ok(self.original.to_vec()); }
        self.editable()?;
        let mut archive = self.archive.clone();
        let (mut total, mut count) = (0_u64, 0);
        for i in 0..archive.len() {
            let entry = archive.by_index_raw(i).map_err(zip_error)?;
            let size = match self.changes.get(&format!("/{}", entry.name())) {
                Some(Some(data)) => data.len() as u64,
                Some(None) => continue,
                None => entry.size(),
            };
            total += size;
            count += 1;
        }
        for (name, change) in &self.changes {
            if archive.index_for_name(&name[1..]).is_none() { if let Some(data) = change { total += data.len() as u64; count += 1; } }
        }
        if count > MAX_ENTRIES || total > MAX_TOTAL { return Err(invalid("Saved package exceeds the 10,000 entry or 1 GiB uncompressed limit")); }
        let mut writer = ZipWriter::new(Cursor::new(Vec::new()));
        writer.set_raw_comment(archive.comment().into()).map_err(zip_error)?;
        let mut written = HashSet::new();
        for i in 0..archive.len() {
            let entry = archive.by_index_raw(i).map_err(zip_error)?;
            let name = format!("/{}", entry.name());
            if let Some(change) = self.changes.get(&name) {
                if let Some(data) = change {
                    let options = entry.options();
                    writer.start_file(&name[1..], options).map_err(zip_error)?;
                    writer.write_all(data).map_err(zip_error)?;
                }
            } else {
                // raw_copy_file trusts the CRC. Verify decompression before accepting it.
                if !entry.is_dir() { self.data(&name)?; }
                writer.raw_copy_file(entry).map_err(zip_error)?;
            }
            written.insert(name);
        }
        for (name, change) in &self.changes {
            if written.contains(name) { continue; }
            if let Some(data) = change {
                writer.start_file(&name[1..], SimpleFileOptions::default().compression_method(CompressionMethod::Deflated)).map_err(zip_error)?;
                writer.write_all(data).map_err(zip_error)?;
            }
        }
        let result = writer.finish().map_err(zip_error)?.into_inner();
        if result.len() > MAX_ARCHIVE { return Err(invalid("Saved archive exceeds the 512 MiB limit")); }
        Ok(result)
    }
}

#[pymethods]
impl Package {
    #[new]
    fn py_new(data: &[u8]) -> PyResult<Self> { Self::open(data) }

    #[staticmethod]
    fn new() -> PyResult<Self> {
        let mut writer = ZipWriter::new(Cursor::new(Vec::new()));
        for (name, data) in [
            ("[Content_Types].xml", format!(r#"<Types xmlns="{CT_NS}"><Default Extension="rels" ContentType="{REL_CT}"/><Override PartName="/word/document.xml" ContentType="{MAIN_CT}"/></Types>"#)),
            ("_rels/.rels", format!(r#"<Relationships xmlns="{REL_NS}"><Relationship Id="rId1" Type="{OFFICE_REL}" Target="word/document.xml"/></Relationships>"#)),
            ("word/document.xml", r#"<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>"#.into()),
        ] {
            writer.start_file(name, SimpleFileOptions::default().compression_method(CompressionMethod::Deflated)).map_err(zip_error)?;
            writer.write_all(data.as_bytes()).map_err(zip_error)?;
        }
        Self::open(&writer.finish().map_err(zip_error)?.into_inner())
    }

    #[getter]
    fn main_part(&self) -> &str { &self.main }

    fn resolve_part(&self, uri: &str) -> PyResult<String> { self.existing(uri) }

    fn part_names(&self) -> Vec<String> { self.names.values().filter(|n| self.changes.get(*n) != Some(&None)).cloned().collect() }

    fn read_part<'py>(&self, py: Python<'py>, uri: &str) -> PyResult<Bound<'py, PyBytes>> { Ok(PyBytes::new(py, &self.data(uri)?)) }

    fn content_type(&self, uri: &str) -> PyResult<String> {
        let name = self.existing(uri)?;
        if name == TYPES { return Err(invalid("[Content_Types].xml is package metadata, not a typed OPC part")); }
        let metadata = self.types()?;
        let extension = name.rsplit_once('.').map_or("", |(_, e)| e);
        let mut default = None;
        for (_, record) in metadata.records() {
            if record.name.local == "Override" && required(record, "PartName")?.eq_ignore_ascii_case(&name) { return Ok(required(record, "ContentType")?.into()); }
            if record.name.local == "Default" && required(record, "Extension")?.eq_ignore_ascii_case(extension) { default = Some(required(record, "ContentType")?.to_string()); }
        }
        default.ok_or_else(|| invalid(format!("No content type for OPC part {name}")))
    }

    fn add_part(&mut self, uri: &str, content_type: &str, data: &[u8]) -> PyResult<()> {
        self.ordinary(uri)?;
        if self.existing(uri).is_ok() { return Err(invalid("OPC part already exists")); }
        if content_type.contains("digital-signature") { return Err(invalid("Creating digital signatures is unsupported")); }
        if data.len() as u64 > MAX_PART { return Err(invalid("Part exceeds the 256 MiB limit")); }
        self.set_type(uri, content_type)?;
        self.put(uri, data.to_vec());
        Ok(())
    }

    fn set_content_type(&mut self, uri: &str, content_type: &str) -> PyResult<()> {
        let name = self.existing(uri)?;
        if self.content_type(&name).is_ok_and(|s| s == content_type) { return Ok(()); }
        self.ordinary(&name)?;
        if name == self.main && content_type != MAIN_CT { return Err(invalid("The DOCX main part content type cannot be changed")); }
        if content_type.contains("digital-signature") { return Err(invalid("Creating digital signatures is unsupported")); }
        self.set_type(&name, content_type)
    }

    fn replace_part(&mut self, uri: &str, data: &[u8]) -> PyResult<()> {
        let name = self.existing(uri)?;
        if self.data(&name)? == data { return Ok(()); }
        self.ordinary(&name)?;
        if data.len() as u64 > MAX_PART { return Err(invalid("Part exceeds the 256 MiB limit")); }
        self.put(&name, data.to_vec());
        Ok(())
    }

    fn remove_part(&mut self, uri: &str) -> PyResult<()> {
        let name = self.existing(uri)?;
        self.ordinary(&name)?;
        if name == self.main { return Err(invalid("The DOCX main part cannot be removed")); }
        let mut updates = Vec::new();
        let types = self.types()?;
        let own_rels = rels_uri(&name)?;
        let remove: Vec<_> = types.records().filter(|(_, r)| r.name.local == "Override" && r.attribute("", "PartName").is_some_and(|p| p.eq_ignore_ascii_case(&name) || p.eq_ignore_ascii_case(&own_rels))).map(|(i, _)| i).collect();
        if !remove.is_empty() { updates.push((TYPES.to_string(), types.write(&remove, None)?)); }
        for rel_uri in self.part_names() {
            let Some(source) = rels_source(&rel_uri) else { continue };
            if rel_uri.eq_ignore_ascii_case(&own_rels) { continue; }
            let (metadata, rels) = self.rels(&source)?;
            let remove: Vec<_> = rels.iter().filter(|r| r.mode == "Internal" && resolve_target(&source, &r.target).is_ok_and(|p| p.eq_ignore_ascii_case(&name))).map(|r| r.node_id).collect();
            if !remove.is_empty() {
                updates.push((rel_uri.clone(), metadata.write(&remove, None)?));
            }
        }
        for (uri, data) in updates { self.put(&uri, data); }
        self.changes.insert(name, None);
        if let Ok(uri) = self.existing(&own_rels) { self.changes.insert(uri, None); }
        Ok(())
    }

    fn relationships<'py>(&self, py: Python<'py>, source_uri: &str) -> PyResult<Bound<'py, PyList>> {
        if source_uri != "/" { self.existing(source_uri)?; }
        let result = PyList::empty(py);
        for rel in self.rels(source_uri)?.1 {
            let item = PyDict::new(py);
            item.set_item("id", rel.id)?;
            item.set_item("type", rel.kind)?;
            item.set_item("target", rel.target)?;
            item.set_item("target_mode", rel.mode)?;
            result.append(item)?;
        }
        Ok(result)
    }

    fn relationship_part(&self, source_uri: &str, relationship_id: &str) -> PyResult<Option<String>> {
        let source = if source_uri == "/" { "/".into() } else { self.existing(source_uri)? };
        let rel = self.rels(&source)?.1.into_iter().find(|r| r.id == relationship_id).ok_or_else(|| PyKeyError::new_err("No such relationship in this scope"))?;
        if rel.mode == "External" { return Ok(None); }
        Ok(Some(self.relationship_target(&source, &rel.target)?))
    }

    // Resolve an already-read internal relationship without reparsing its .rels file.
    fn relationship_target(&self, source_uri: &str, target: &str) -> PyResult<String> {
        let source = if source_uri == "/" { "/".into() } else { self.existing(source_uri)? };
        self.existing(&resolve_target(&source, target)?)
    }

    #[pyo3(signature = (source_uri, relationship_type, target, target_mode="Internal", relationship_id=None))]
    fn add_relationship(&mut self, source_uri: &str, relationship_type: &str, target: &str, target_mode: &str, relationship_id: Option<&str>) -> PyResult<String> {
        self.editable()?;
        let source = if source_uri == "/" { "/".into() } else { self.existing(source_uri)? };
        if source != "/" { self.ordinary(&source)?; }
        if !["Internal", "External"].contains(&target_mode) { return Err(invalid("TargetMode must be Internal or External")); }
        if !relationship_type.contains(':') || relationship_type.chars().any(|c| c.is_whitespace() || !xml_char(c)) || target.is_empty() || target.chars().any(|c| c.is_whitespace() || !xml_char(c)) { return Err(invalid("Invalid relationship type or target")); }
        if relationship_type.contains("/digital-signature/") { return Err(invalid("Creating digital signatures is unsupported")); }
        if source == "/" && [OFFICE_REL, STRICT_REL].contains(&relationship_type) { return Err(invalid("The DOCX already has its officeDocument relationship")); }
        if target_mode == "Internal" { self.existing(&resolve_target(&source, target)?)?; }
        let (metadata, rels) = self.rels(&source)?;
        let id = relationship_id.map(str::to_string).unwrap_or_else(|| (1..).map(|n| format!("rId{n}")).find(|id| rels.iter().all(|r| &r.id != id)).unwrap());
        check_local(&id)?;
        if rels.iter().any(|r| r.id == id) { return Err(invalid("Relationship Id already exists in this scope")); }
        let uri = rels_uri(&source)?;
        let data = metadata.write(&[], Some(("Relationship", &[("Id", &id), ("Type", relationship_type), ("Target", target), ("TargetMode", target_mode)])))?;
        if self.existing(&uri).is_err() { self.set_type(&uri, REL_CT)?; }
        self.put(&uri, data);
        Ok(id)
    }

    fn remove_relationship(&mut self, source_uri: &str, relationship_id: &str) -> PyResult<()> {
        self.editable()?;
        let source = if source_uri == "/" { "/".into() } else { self.existing(source_uri)? };
        let (metadata, rels) = self.rels(&source)?;
        let rel = rels.iter().find(|r| r.id == relationship_id).ok_or_else(|| PyKeyError::new_err("No such relationship in this scope"))?;
        if source == "/" && [OFFICE_REL, STRICT_REL].contains(&rel.kind.as_str()) { return Err(invalid("The main officeDocument relationship cannot be removed")); }
        let uri = rels_uri(&source)?;
        let data = metadata.write(&[rel.node_id], None)?;
        self.put(&uri, data);
        Ok(())
    }

    fn bytes<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> { Ok(PyBytes::new(py, &py.detach(|| self.output())?)) }

    fn save(&self, py: Python<'_>, path: &str) -> PyResult<()> {
        py.detach(|| {
            let data = self.output()?;
            let path = Path::new(path);
            let parent = path.parent().filter(|p| !p.as_os_str().is_empty()).unwrap_or_else(|| Path::new("."));
            let mut temporary = tempfile::NamedTempFile::new_in(parent)?;
            temporary.write_all(&data)?;
            if let Ok(metadata) = std::fs::metadata(path) { temporary.as_file().set_permissions(metadata.permissions())?; }
            temporary.as_file().sync_all()?;
            temporary.persist(path).map_err(|error| PyErr::from(error.error))?;
            Ok(())
        })
    }
}
