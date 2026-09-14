//! General ordered XML storage, preflighted edits and deferred serialization. Schema knowledge lives elsewhere.
use crate::error::{Error, Result};
use pyo3::{prelude::*, types::PyBytes};
use quick_xml::{
    encoding::DecodingReader,
    events::{BytesDecl, BytesPI, BytesStart, BytesText, Event},
    Reader, Writer, XmlVersion,
};
use serde_json::{json, Value};
use std::{collections::HashSet, io::Read, ops::Deref, sync::{Arc, RwLock, RwLockReadGuard}};

const XML_NS: &str = "http://www.w3.org/XML/1998/namespace";
const XMLNS_NS: &str = "http://www.w3.org/2000/xmlns/";
const MAX_BYTES: usize = 32 * 1024 * 1024;
const MAX_NODES: usize = 1_000_000;
const MAX_DEPTH: usize = 256;
const MAX_ATTRIBUTE: usize = 1024 * 1024;
const MAX_CONTEXT: usize = 64 * 1024 * 1024;

fn error(message: impl ToString) -> Error { Error::Invalid(message.to_string()) }
fn stale() -> Error { Error::Stale("XML node or document is no longer live".into()) }
fn space(c: char) -> bool { matches!(c, ' ' | '\t' | '\n' | '\r') }
fn normalize_eols(value: &str) -> String { value.replace("\r\n", "\n").replace('\r', "\n") }
pub(crate) fn xml_char(c: char) -> bool { matches!(c, '\t' | '\n' | '\r' | '\u{20}'..='\u{d7ff}' | '\u{e000}'..='\u{fffd}' | '\u{10000}'..='\u{10ffff}') }
pub(crate) fn check_value(value: &str) -> Result<()> {
    if value.len() > MAX_BYTES { return Err(error("XML exceeds the 32 MiB limit")); }
    if !value.chars().all(xml_char) { return Err(error("Illegal XML character")); }
    Ok(())
}
fn name_start(c: char) -> bool {
    matches!(c, 'A'..='Z' | '_' | 'a'..='z' | '\u{c0}'..='\u{d6}' | '\u{d8}'..='\u{f6}' | '\u{f8}'..='\u{2ff}' |
        '\u{370}'..='\u{37d}' | '\u{37f}'..='\u{1fff}' | '\u{200c}'..='\u{200d}' | '\u{2070}'..='\u{218f}' |
        '\u{2c00}'..='\u{2fef}' | '\u{3001}'..='\u{d7ff}' | '\u{f900}'..='\u{fdcf}' | '\u{fdf0}'..='\u{fffd}' | '\u{10000}'..='\u{effff}')
}
fn valid_name(value: &str, colon: bool) -> bool {
    let mut chars = value.chars();
    chars.next().is_some_and(|c| name_start(c) || colon && c == ':')
        && chars.all(|c| name_start(c) || colon && c == ':' || matches!(c, '-' | '.' | '0'..='9' | '\u{b7}' | '\u{300}'..='\u{36f}' | '\u{203f}'..='\u{2040}'))
}
pub(crate) fn check_local(value: &str) -> Result<()> {
    if !valid_name(value, false) { return Err(error(format!("Invalid XML local name {value:?}"))); }
    Ok(())
}
fn base_namespaces() -> Vec<(String, String)> { vec![("".into(), "".into()), ("xml".into(), XML_NS.into())] }
fn binding<'a>(namespaces: &'a [(String, String)], prefix: &str) -> Option<&'a str> {
    namespaces.iter().find(|(p, _)| p == prefix).map(|(_, uri)| uri.as_str())
}
fn check_binding(prefix: &str, uri: &str) -> Result<()> {
    if !prefix.is_empty() { check_local(prefix)?; }
    check_value(uri)?;
    if escaped(uri, true).len() > MAX_ATTRIBUTE { return Err(error("XML namespace attribute exceeds the 1 MiB limit")); }
    if prefix == "xmlns" || uri == XMLNS_NS || (prefix == "xml") != (uri == XML_NS) || !prefix.is_empty() && uri.is_empty() {
        return Err(error("Invalid reserved or empty namespace binding"));
    }
    Ok(())
}
fn bind(namespaces: &mut Vec<(String, String)>, prefix: &str, uri: &str) -> Result<()> {
    check_binding(prefix, uri)?;
    if let Some((_, old)) = namespaces.iter_mut().find(|(p, _)| p == prefix) { *old = uri.into(); }
    else { namespaces.push((prefix.into(), uri.into())); }
    Ok(())
}
fn escaped(value: &str, attribute: bool) -> String {
    let mut result = String::new();
    for c in value.chars() {
        match c {
            '&' => result.push_str("&amp;"),
            '<' => result.push_str("&lt;"),
            '>' => result.push_str("&gt;"),
            '"' if attribute => result.push_str("&quot;"),
            '\r' => result.push_str("&#13;"),
            '\n' if attribute => result.push_str("&#10;"),
            '\t' if attribute => result.push_str("&#9;"),
            _ => result.push(c),
        }
    }
    result
}

#[derive(Clone, Debug)]
pub struct Name { pub uri: String, pub local: String, pub prefix: String }
impl Name {
    pub(crate) fn lexical(&self) -> String { if self.prefix.is_empty() { self.local.clone() } else { format!("{}:{}", self.prefix, self.local) } }
    fn resolve(lexical: &str, namespaces: &[(String, String)], attribute: bool) -> Result<Self> {
        let (prefix, local) = lexical.split_once(':').unwrap_or(("", lexical));
        check_local(local)?;
        if !prefix.is_empty() { check_local(prefix)?; }
        if lexical.starts_with(':') || prefix == "xmlns" { return Err(error("Invalid XML qualified name")); }
        let uri =
            if attribute && prefix.is_empty() { "" } else { binding(namespaces, prefix).ok_or_else(|| error(format!("Unbound XML prefix {prefix:?}")))? };
        Ok(Self { uri: uri.into(), local: local.into(), prefix: prefix.into() })
    }
}
#[derive(Clone, Debug)]
pub struct Attribute { pub name: Name, pub value: String }
#[derive(Clone, Debug)]
pub struct Element {
    pub name: Name,
    pub attributes: Vec<Attribute>,
    /// Full context, not just prefixes found in names: opaque values may use any binding.
    pub namespaces: Vec<(String, String)>,
}
impl Element {
    pub fn attribute(&self, uri: &str, local: &str) -> Option<&str> {
        self.attributes.iter().find(|a| a.name.uri == uri && a.name.local == local).map(|a| a.value.as_str())
    }
    pub fn namespace(&self, prefix: &str) -> Option<&str> { binding(&self.namespaces, prefix) }
    fn edit_name(&self, uri: &str, local: &str, prefix: Option<&str>, attribute: bool) -> Result<Name> {
        check_local(local)?;
        check_value(uri)?;
        if uri == XMLNS_NS || attribute && uri.is_empty() && local == "xmlns" { return Err(error("Use declare_namespace for namespace declarations")); }
        let prefix = match prefix {
            Some(p) => p.to_owned(),
            None if uri.is_empty() => String::new(),
            None => self
                .namespaces
                .iter()
                .find(|(p, u)| u == uri && (!attribute || !p.is_empty()))
                .map(|(p, _)| p.clone())
                .ok_or_else(|| error("New namespace requires an explicit prefix"))?,
        };
        if !prefix.is_empty() { check_local(&prefix)?; }
        if attribute && prefix.is_empty() && !uri.is_empty() { return Err(error("Namespaced attributes require a nonempty prefix")); }
        if !prefix.is_empty() && uri.is_empty() { return Err(error("An unqualified name cannot have a prefix")); }
        if !(attribute && prefix.is_empty()) && self.namespace(&prefix) != Some(uri) {
            if self.namespace(&prefix).is_some_and(|old| !old.is_empty()) && !(prefix.is_empty() && uri.is_empty() && !attribute) {
                return Err(error("Namespace binding already exists; use a fresh prefix or explicit declare_namespace"));
            }
            check_binding(&prefix, uri)?;
        }
        Ok(Name { uri: uri.into(), local: local.into(), prefix })
    }
}
#[derive(Clone, Debug)]
pub enum NodeKind {
    Element(Element),
    Text(String),
    Comment(String),
    Pi { target: String, value: String },
}
#[derive(Clone, Debug)]
pub struct Node {
    pub id: usize,
    pub parent: Option<usize>,
    pub kind: NodeKind,
    pub children: Vec<usize>,
}
impl Node {
    pub fn element(&self) -> Option<&Element> { if let NodeKind::Element(e) = &self.kind { Some(e) } else { None } }
    pub fn text(&self) -> Option<&str> { match &self.kind { NodeKind::Text(s) | NodeKind::Comment(s) => Some(s), _ => None } }
}
#[derive(Clone, Debug)]
struct Declaration { standalone: Option<String> }
#[derive(Clone, Debug)]
pub struct Document {
    pub nodes: Vec<Option<Node>>,
    pub root: usize,
    pub children: Vec<usize>,
    declaration: Option<Declaration>,
    mutation: u64,
}
impl Document {
    pub fn from_element(element: Element) -> Self {
        let root = Node { id: 0, parent: None, kind: NodeKind::Element(element), children: Vec::new() };
        Self { nodes: vec![Some(root)], root: 0, children: vec![0], declaration: None, mutation: 0 }
    }
    pub fn set_text(&mut self, id: usize, value: &str) -> Result<()> {
        check_value(value)?;
        match &self.node(id)?.kind {
            NodeKind::Element(_) => {
                let children = self.node(id)?.children.clone();
                if children.iter().any(|id| !matches!(self.nodes[*id].as_ref().unwrap().kind, NodeKind::Text(_))) {
                    return Err(error("set_text requires text-only content; mixed content is not replaced"));
                }
                if value.is_empty() {
                    for child in children { self.drop_subtree(child); }
                    self.node_mut(id)?.children.clear();
                } else if let Some(first) = children.first().copied() {
                    self.node_mut(first)?.kind = NodeKind::Text(value.into());
                    for child in &children[1..] { self.drop_subtree(*child); }
                    self.node_mut(id)?.children.truncate(1);
                } else { self.add(Some(id), NodeKind::Text(value.into()))?; }
            }
            NodeKind::Text(_) => {
                if self.node(id)?.parent.is_none() && !value.chars().all(space) { return Err(error("Character data outside the document element")); }
                self.node_mut(id)?.kind = NodeKind::Text(value.into());
            }
            NodeKind::Comment(_) => {
                check_comment(value)?;
                self.node_mut(id)?.kind = NodeKind::Comment(normalize_eols(value));
            }
            NodeKind::Pi { .. } => return Err(error("Use replace_node to replace a processing instruction")),
        }
        Ok(())
    }
    pub fn set_attribute(&mut self, id: usize, uri: &str, local: &str, value: &str, prefix: Option<&str>) -> Result<()> {
        check_value(value)?;
        if escaped(value, true).len() > MAX_ATTRIBUTE { return Err(error("XML attribute exceeds the 1 MiB limit")); }
        let e = self.node(id)?.element().ok_or_else(|| error("Expected an XML element"))?;
        let found = e.attributes.iter().position(|a| a.name.uri == uri && a.name.local == local);
        let name = match found {
            Some(i) if prefix.is_none() => e.attributes[i].name.clone(),
            _ => e.edit_name(uri, local, prefix, true)?,
        };
        let e = self.element_mut(id)?;
        let old_context = e.namespaces.len();
        if !name.prefix.is_empty() { bind(&mut e.namespaces, &name.prefix, uri)?; }
        if let Some(i) = found { e.attributes[i] = Attribute { name, value: value.into() }; }
        else { e.attributes.push(Attribute { name, value: value.into() }); }
        if e.namespaces.len() != old_context { self.inherit_namespaces(id); }
        Ok(())
    }
    pub fn remove_attribute(&mut self, id: usize, uri: &str, local: &str) -> Result<()> {
        self.element_mut(id)?.attributes.retain(|a| a.name.uri != uri || a.name.local != local);
        Ok(())
    }
    pub fn ensure_namespace(&mut self, id: usize, uri: &str, preferred: &str) -> Result<String> {
        let e = self.node(id)?.element().ok_or_else(|| error("Expected an XML element"))?;
        if let Some((prefix, _)) = e.namespaces.iter().find(|(p, u)| !p.is_empty() && u == uri) { return Ok(prefix.clone()); }
        let preferred = if preferred.is_empty() { "ns" } else { preferred };
        let mut prefix = preferred.to_string();
        let mut n = 1;
        while e.namespace(&prefix).is_some() { prefix = format!("{preferred}{n}"); n += 1; }
        // A prefix unbound here is unbound along the whole path from the root, so one root declaration serves the document.
        let root = self.root;
        self.declare_namespace(root, &prefix, uri)?;
        Ok(prefix)
    }
    pub fn set_attribute_ns(&mut self, id: usize, uri: &str, local: &str, value: &str, preferred: &str) -> Result<String> {
        check_local(local)?; check_value(value)?;
        let prefix = self.ensure_namespace(id, uri, preferred)?;
        self.set_attribute(id, uri, local, value, Some(&prefix))?;
        Ok(prefix)
    }
    pub fn rename(&mut self, id: usize, uri: &str, local: &str, prefix: Option<&str>) -> Result<()> {
        let name = self.node(id)?.element().ok_or_else(|| error("Expected an XML element"))?.edit_name(uri, local, prefix, false)?;
        let e = self.element_mut(id)?;
        let old_context = e.namespaces.len();
        bind(&mut e.namespaces, &name.prefix, uri)?;
        e.name = name;
        if e.namespaces.len() != old_context { self.inherit_namespaces(id); }
        Ok(())
    }
    pub fn declare_namespace(&mut self, id: usize, prefix: &str, uri: &str) -> Result<()> {
        check_binding(prefix, uri)?;
        let e = self.node(id)?.element().ok_or_else(|| error("Expected an XML element"))?;
        if e.name.prefix == prefix && e.name.uri != uri
            || e.attributes.iter().any(|a| !a.name.prefix.is_empty() && a.name.prefix == prefix && a.name.uri != uri)
        { return Err(error("Namespace binding conflicts with an existing name")); }
        let e = self.element_mut(id)?;
        let old_context = e.namespaces.len();
        bind(&mut e.namespaces, prefix, uri)?;
        if e.namespaces.len() != old_context { self.inherit_namespaces(id); }
        Ok(())
    }
    pub fn copy(&mut self, id: usize, parent: usize, index: usize) -> Result<usize> {
        if index > self.sequence(Some(parent))?.len() { return Err(Error::Index("XML content index out of range".into())); }
        self.capacity(self.subtree_size(id, self.depth(Some(parent))?)?)?;
        // Stage only the copied subtree, including when the destination is inside it.
        let source = self.subtree(id)?;
        self.import_at(&source, source.root, parent, index)
    }
    pub fn subtree(&self, id: usize) -> Result<Document> {
        let mut result = Document { nodes: Vec::new(), root: 0, children: Vec::new(), declaration: None, mutation: 0 };
        result.root = result.import(self, id, None)?;
        Ok(result)
    }
    pub fn import_at(&mut self, source: &Document, id: usize, parent: usize, index: usize) -> Result<usize> {
        if index > self.sequence(Some(parent))?.len() { return Err(Error::Index("XML content index out of range".into())); }
        self.capacity(source.subtree_size(id, self.depth(Some(parent))?)?)?;
        let new = self.import(source, id, Some(parent))?;
        let sequence = self.sequence_mut(Some(parent))?;
        sequence.pop();
        sequence.insert(index, new);
        self.inherit_namespaces(new);
        Ok(new)
    }
    pub fn move_node(&mut self, id: usize, parent: usize, index: usize) -> Result<()> {
        if index > self.sequence(Some(parent))?.len() { return Err(Error::Index("XML content index out of range".into())); }
        let mut ancestor = Some(parent);
        while let Some(p) = ancestor {
            if p == id { return Err(error("Cannot move a node into itself or its descendant")); }
            ancestor = self.node(p)?.parent;
        }
        self.subtree_size(id, self.depth(Some(parent))?)?;
        let (old_parent, old_index) = self.position(id)?;
        self.sequence_mut(old_parent)?.remove(old_index);
        let index = index - usize::from(old_parent == Some(parent) && old_index < index);
        self.sequence_mut(Some(parent))?.insert(index, id);
        self.node_mut(id)?.parent = Some(parent);
        self.inherit_namespaces(id);
        Ok(())
    }
    pub fn node(&self, id: usize) -> Result<&Node> { self.nodes.get(id).and_then(Option::as_ref).ok_or_else(stale) }
    pub(crate) fn node_mut(&mut self, id: usize) -> Result<&mut Node> {
        let node = self.nodes.get_mut(id).and_then(Option::as_mut).ok_or_else(stale)?;
        self.mutation = self.mutation.wrapping_add(1);
        Ok(node)
    }
    pub(crate) fn element_mut(&mut self, id: usize) -> Result<&mut Element> {
        if self.node(id)?.element().is_none() { return Err(error("Expected an XML element")); }
        let NodeKind::Element(e) = &mut self.node_mut(id)?.kind else { unreachable!() };
        Ok(e)
    }
    pub fn element_children(&self, id: usize) -> Result<impl DoubleEndedIterator<Item = usize> + '_> {
        Ok(self.node(id)?.children.iter().copied().filter(|&id| self.nodes[id].as_ref().unwrap().element().is_some()))
    }
    /// Elements in document order, including the supplied root when it is an element.
    pub fn descendants(&self, id: usize) -> Result<impl Iterator<Item = usize> + '_> {
        self.node(id)?;
        let mut stack = vec![id];
        Ok(std::iter::from_fn(move || {
            while let Some(id) = stack.pop() {
                let node = self.nodes[id].as_ref().unwrap();
                stack.extend(node.children.iter().rev().copied());
                if node.element().is_some() { return Some(id); }
            }
            None
        }))
    }
    pub fn element_ids(&self) -> Vec<usize> { self.descendants(self.root).expect("Document has a live root").collect() }
    pub(crate) fn sequence(&self, parent: Option<usize>) -> Result<&Vec<usize>> {
        match parent {
            Some(id) => {
                let n = self.node(id)?;
                if n.element().is_none() { return Err(error("Only elements have child content")); }
                Ok(&n.children)
            }
            None => Ok(&self.children),
        }
    }
    pub(crate) fn sequence_mut(&mut self, parent: Option<usize>) -> Result<&mut Vec<usize>> {
        self.mutation = self.mutation.wrapping_add(1);
        match parent { Some(id) => Ok(&mut self.node_mut(id)?.children), None => Ok(&mut self.children) }
    }
    pub(crate) fn add(&mut self, parent: Option<usize>, kind: NodeKind) -> Result<usize> {
        if self.nodes.len() >= MAX_NODES { return Err(error("XML exceeds the one-million allocated-node limit")); }
        let id = self.nodes.len();
        self.nodes.push(Some(Node { id, parent, kind, children: Vec::new() }));
        self.sequence_mut(parent)?.push(id);
        Ok(id)
    }
    fn append_text(&mut self, parent: Option<usize>, value: String) -> Result<()> {
        if let Some(id) = self.sequence(parent)?.last().copied() {
            if let NodeKind::Text(old) = &mut self.node_mut(id)?.kind {
                old.push_str(&value);
                return Ok(());
            }
        }
        self.add(parent, NodeKind::Text(value))?;
        Ok(())
    }
    pub(crate) fn drop_subtree(&mut self, id: usize) {
        let mut stack = vec![id];
        while let Some(id) = stack.pop() { if let Some(node) = self.nodes[id].take() { self.mutation = self.mutation.wrapping_add(1); stack.extend(node.children); } }
    }
    pub(crate) fn position(&self, id: usize) -> Result<(Option<usize>, usize)> {
        let parent = self.node(id)?.parent;
        Ok((parent, self.sequence(parent)?.iter().position(|i| *i == id).unwrap()))
    }
    pub(crate) fn remove(&mut self, id: usize) -> Result<()> {
        if id == self.root { return Err(error("Cannot delete the document element")); }
        let (parent, index) = self.position(id)?;
        self.sequence_mut(parent)?.remove(index);
        self.drop_subtree(id);
        Ok(())
    }
    pub(crate) fn insert_kind(&mut self, parent: usize, index: usize, kind: NodeKind) -> Result<usize> {
        if index > self.sequence(Some(parent))?.len() { return Err(Error::Index("XML content index out of range".into())); }
        let id = self.add(Some(parent), kind)?;
        let sequence = self.sequence_mut(Some(parent))?;
        sequence.pop();
        sequence.insert(index, id);
        Ok(id)
    }
    pub(crate) fn import(&mut self, source: &Document, id: usize, parent: Option<usize>) -> Result<usize> {
        let node = source.node(id)?;
        let new = self.add(parent, node.kind.clone())?;
        for child in &node.children { self.import(source, *child, Some(new))?; }
        Ok(new)
    }
    pub(crate) fn capacity(&self, additional: usize) -> Result<()> {
        if self.nodes.len() + additional > MAX_NODES { return Err(error("XML exceeds the one-million allocated-node limit")); }
        Ok(())
    }
    pub(crate) fn depth(&self, mut parent: Option<usize>) -> Result<usize> {
        let mut depth = 0;
        while let Some(id) = parent {
            let node = self.node(id)?;
            depth += usize::from(node.element().is_some());
            parent = node.parent;
        }
        Ok(depth)
    }
    pub(crate) fn subtree_size(&self, id: usize, base_depth: usize) -> Result<usize> {
        let mut count = 0;
        let mut stack = vec![(id, base_depth)];
        while let Some((id, depth)) = stack.pop() {
            let node = self.node(id)?;
            let depth = depth + usize::from(node.element().is_some());
            if depth > MAX_DEPTH { return Err(error("XML exceeds the depth limit of 256")); }
            count += 1;
            stack.extend(node.children.iter().map(|id| (*id, depth)));
        }
        Ok(count)
    }
    // Existing contexts win: names and opaque QName values retain their meaning after moves.
    pub(crate) fn inherit_namespaces(&mut self, id: usize) {
        let mut stack = vec![id];
        while let Some(id) = stack.pop() {
            let parent = self.nodes[id].as_ref().unwrap().parent;
            let inherited = parent.and_then(|p| self.nodes[p].as_ref().unwrap().element()).map(|e| e.namespaces.clone());
            let node = self.nodes[id].as_mut().unwrap();
            if let (Some(inherited), NodeKind::Element(e)) = (inherited, &mut node.kind) {
                for (prefix, uri) in inherited { if e.namespace(&prefix).is_none() { e.namespaces.push((prefix, uri)); } }
            }
            stack.extend(node.children.iter().copied());
        }
    }
    fn set_root(&mut self) -> Result<()> {
        let roots = self.children.iter().filter(|id| self.nodes[**id].as_ref().unwrap().element().is_some()).copied().collect::<Vec<_>>();
        if roots.len() != 1 { return Err(error("XML requires exactly one document element")); }
        self.root = roots[0];
        Ok(())
    }
    fn write_node(&self, writer: &mut Writer<Vec<u8>>, id: usize, inherited: &[(String, String)]) -> Result<()> {
        let node = self.node(id)?;
        match &node.kind {
            NodeKind::Element(e) => {
                let name = e.name.lexical();
                let mut content = name.clone();
                for (prefix, uri) in &e.namespaces {
                    if binding(inherited, prefix) == Some(uri.as_str()) { continue; }
                    let key = if prefix.is_empty() { "xmlns".into() } else { format!("xmlns:{prefix}") };
                    content.push_str(&format!(" {key}=\"{}\"", escaped(uri, true)));
                }
                for a in &e.attributes { content.push_str(&format!(" {}=\"{}\"", a.name.lexical(), escaped(&a.value, true))); }
                let start = BytesStart::from_content(content, name.len());
                if node.children.is_empty() { writer.write_event(Event::Empty(start)).map_err(error)?; } else {
                    writer.write_event(Event::Start(start.borrow())).map_err(error)?;
                    for child in &node.children { self.write_node(writer, *child, &e.namespaces)?; }
                    writer.write_event(Event::End(start.to_end())).map_err(error)?;
                }
            }
            NodeKind::Text(s) => writer.write_event(Event::Text(BytesText::from_escaped(escaped(s, false)))).map_err(error)?,
            NodeKind::Comment(s) => writer.write_event(Event::Comment(BytesText::from_escaped(s))).map_err(error)?,
            NodeKind::Pi { target, value } => {
                let content = if value.is_empty() { target.clone() } else { format!("{target} {value}") };
                writer.write_event(Event::PI(BytesPI::new(content))).map_err(error)?;
            }
        }
        if writer.get_ref().len() > MAX_BYTES { return Err(error("XML exceeds the 32 MiB serialized limit")); }
        Ok(())
    }
    pub(crate) fn serialize(&self) -> Result<Vec<u8>> {
        let context_bytes: usize = self.nodes.iter().flatten().filter_map(Node::element).flat_map(|e| &e.namespaces).map(|(p, uri)| p.len() + uri.len()).sum();
        if context_bytes > MAX_CONTEXT { return Err(error("XML exceeds the 64 MiB namespace-context limit")); }
        let mut writer = Writer::new(Vec::new());
        if let Some(d) = &self.declaration { writer.write_event(Event::Decl(BytesDecl::new("1.0", Some("UTF-8"), d.standalone.as_deref()))).map_err(error)?; }
        for id in &self.children { self.write_node(&mut writer, *id, &base_namespaces())?; }
        Ok(writer.into_inner())
    }
    fn row(&self, id: usize) -> Result<Value> {
        let node = self.node(id)?;
        let kind = match node.kind {
            NodeKind::Element(_) => "element",
            NodeKind::Text(_) => "text",
            NodeKind::Comment(_) => "comment",
            NodeKind::Pi { .. } => "pi",
        };
        let mut row = json!({"id":node.id,"parent":node.parent,"kind":kind,"children":node.children});
        if let Some(e) = node.element() {
            let content = node
                .children
                .iter()
                .map(|id| {
                    let child = self.nodes[*id].as_ref().unwrap();
                    match &child.kind {
                        NodeKind::Element(_) => json!(["element", id]),
                        NodeKind::Text(s) => json!(["text", s]),
                        NodeKind::Comment(s) => json!(["comment", s]),
                        NodeKind::Pi { target, value } => json!(["pi", [target, value]]),
                    }
                })
                .collect::<Vec<_>>();
            row["qname"] = json!([e.name.uri, e.name.local]);
            row["prefix"] = json!(e.name.prefix);
            row["attributes"] = json!(e.attributes.iter().map(|a| [&a.name.uri, &a.name.local, &a.value]).collect::<Vec<_>>());
            row["attribute_prefixes"] = json!(e.attributes.iter().map(|a| &a.name.prefix).collect::<Vec<_>>());
            row["namespaces"] = json!(e.namespaces);
            row["content"] = json!(content);
            row["text"] = json!(node
                .children
                .iter()
                .filter_map(|id| match &self.nodes[*id].as_ref().unwrap().kind {
                    NodeKind::Text(s) => Some(s.as_str()),
                    _ => None,
                })
                .collect::<String>());
        }
        else if let NodeKind::Pi { target, value } = &node.kind {
            row["target"] = json!(target);
            row["text"] = json!(value);
        }
        else { row["text"] = json!(node.text()); }
        Ok(row)
    }
}

// The event reader is deliberately supplemented: events are not a well-formedness guarantee.
fn attributes(start: &BytesStart<'_>) -> Result<Vec<(String, String)>> {
    let raw = start.attributes_raw();
    let mut quote = None;
    for (i, c) in raw.char_indices() {
        if let Some(q) = quote {
            if c == '<' { return Err(error("Literal '<' in XML attribute")); }
            if c == q {
                quote = None;
                if raw[i + 1..].chars().next().is_some_and(|c| !space(c)) { return Err(error("XML attributes require separating whitespace")); }
            }
        } else if matches!(c, '\'' | '"') { quote = Some(c); }
    }
    start
        .attributes()
        .map(|a| {
            let a = a.map_err(error)?;
            if a.value.len() > MAX_ATTRIBUTE { return Err(error("XML attribute exceeds the 1 MiB limit")); }
            let value = a.normalized_value(XmlVersion::Implicit1_0).map_err(error)?.into_owned();
            check_value(&value)?;
            Ok((a.key.as_ref().into(), value))
        })
        .collect()
}
fn declaration(event: &BytesDecl<'_>, encoding: &str) -> Result<Declaration> {
    let start = BytesStart::from_content(event.as_ref(), 3);
    let attrs = attributes(&start)?;
    let names = attrs.iter().map(|(n, _)| n.as_str()).collect::<Vec<_>>();
    if !matches!(names.as_slice(), ["version"] | ["version", "encoding"] | ["version", "standalone"] | ["version", "encoding", "standalone"])
        || attrs[0].1 != "1.0"
    { return Err(error("Invalid or unsupported XML declaration")); }
    // Declaration pseudo-attributes are literal grammar, never entity references.
    if event.as_ref().contains('&') { return Err(error("Entity reference in XML declaration")); }
    let declared = attrs.iter().find(|(n, _)| n == "encoding").map(|(_, v)| v.to_ascii_uppercase());
    if declared.as_deref().is_some_and(|d| d != encoding && !(d == "UTF-16" && encoding.starts_with("UTF-16"))) {
        return Err(error("XML encoding declaration does not match supported UTF-8/UTF-16 input"));
    }
    let standalone = attrs.iter().find(|(n, _)| n == "standalone").map(|(_, v)| v.clone());
    if standalone.as_deref().is_some_and(|v| !matches!(v, "yes" | "no")) { return Err(error("Invalid XML standalone declaration")); }
    Ok(Declaration { standalone })
}
fn check_pi(target: &str, value: &str) -> Result<()> {
    check_value(value)?;
    if !valid_name(target, true) || target.eq_ignore_ascii_case("xml") { return Err(error("Invalid or reserved XML processing-instruction target")); }
    if value.contains("?>") { return Err(error("Processing instruction contains '?>'")); }
    Ok(())
}
fn check_comment(value: &str) -> Result<()> {
    check_value(value)?;
    if value.contains("--") || value.ends_with('-') { return Err(error("Invalid XML comment")); }
    Ok(())
}
fn parse_encoded(source: &str, encoding: &str, max_depth: usize) -> Result<Document> {
    check_value(source)?;
    let mut reader = Reader::from_str(source);
    reader.config_mut().check_comments = true;
    let mut doc = Document { nodes: Vec::new(), root: 0, children: Vec::new(), declaration: None, mutation: 0 };
    let mut stack = Vec::new();
    let mut context_bytes = 0;
    loop {
        let at = reader.buffer_position();
        let event = reader.read_event().map_err(error)?;
        let parent = stack.last().copied();
        match event {
            Event::Start(ref start) | Event::Empty(ref start) => {
                if stack.len() >= max_depth { return Err(error("XML exceeds the depth limit of 256")); }
                let mut namespaces = match parent { Some(id) => doc.node(id)?.element().unwrap().namespaces.clone(), None => base_namespaces() };
                let attrs = attributes(start)?;
                for (key, value) in &attrs {
                    if key == "xmlns" { bind(&mut namespaces, "", value)?; } else if let Some(prefix) = key.strip_prefix("xmlns:") {
                        check_local(prefix)?;
                        bind(&mut namespaces, prefix, value)?;
                    }
                }
                context_bytes += namespaces.iter().map(|(p, uri)| p.len() + uri.len()).sum::<usize>();
                if context_bytes > MAX_CONTEXT { return Err(error("XML exceeds the 64 MiB namespace-context limit")); }
                let name = Name::resolve(start.name().as_ref(), &namespaces, false)?;
                let mut attributes = Vec::new();
                let mut expanded = HashSet::new();
                for (key, value) in attrs {
                    if key == "xmlns" || key.starts_with("xmlns:") { continue; }
                    let name = Name::resolve(&key, &namespaces, true)?;
                    if !expanded.insert((name.uri.clone(), name.local.clone())) { return Err(error("Duplicate expanded XML attribute")); }
                    attributes.push(Attribute { name, value });
                }
                let id = doc.add(parent, NodeKind::Element(Element { name, attributes, namespaces }))?;
                if matches!(event, Event::Start(_)) { stack.push(id); }
            }
            Event::End(_) => {
                stack.pop().ok_or_else(|| error("Unmatched XML end tag"))?;
            }
            Event::Text(text) => {
                if text.as_ref().contains("]]>") { return Err(error("Literal ']]>' in XML text")); }
                let value = text.xml10_content().into_owned();
                if parent.is_none() && !value.chars().all(space) { return Err(error("Character data outside the document element")); }
                doc.append_text(parent, value)?;
            }
            Event::CData(text) => {
                if parent.is_none() { return Err(error("CDATA outside the document element")); }
                doc.append_text(parent, text.xml10_content().into_owned())?;
            }
            Event::GeneralRef(reference) => {
                if parent.is_none() { return Err(error("Entity reference outside the document element")); }
                let value = if let Some(c) = reference.resolve_char_ref().map_err(error)? { c.to_string() } else {
                    quick_xml::escape::resolve_predefined_entity(reference.as_ref()).ok_or_else(|| error("DTD and custom entities are disabled"))?.into()
                };
                check_value(&value)?;
                doc.append_text(parent, value)?;
            }
            Event::Comment(text) => {
                let value = text.xml10_content().into_owned();
                check_comment(&value)?;
                doc.add(parent, NodeKind::Comment(value))?;
            }
            Event::PI(pi) => {
                if !pi.content().is_empty() && !pi.content().starts_with(space) { return Err(error("XML processing-instruction data requires whitespace")); }
                let value = normalize_eols(pi.content()).get(1..).unwrap_or("").to_owned();
                check_pi(pi.target(), &value)?;
                doc.add(parent, NodeKind::Pi { target: pi.target().into(), value })?;
            }
            Event::Decl(d) => {
                if at != 0 || doc.declaration.is_some() { return Err(error("XML declaration must occur at the start")); }
                doc.declaration = Some(declaration(&d, encoding)?);
            }
            Event::DocType(_) => return Err(error("DTD processing is disabled")),
            Event::Eof => break,
        }
    }
    if !stack.is_empty() { return Err(error("Unclosed XML element")); }
    doc.set_root()?;
    Ok(doc)
}
fn decode(data: &[u8]) -> Result<(String, String)> {
    if data.len() > MAX_BYTES { return Err(error("XML exceeds the 32 MiB input limit")); }
    let mut decoder = DecodingReader::new(data);
    let mut source = String::new();
    decoder.by_ref().take((MAX_BYTES + 1) as u64).read_to_string(&mut source).map_err(error)?;
    check_value(&source)?;
    Ok((source, decoder.encoding().name().into()))
}
pub(crate) fn parse_bytes(data: &[u8]) -> Result<Document> { parse_bytes_limited(data, MAX_BYTES, MAX_DEPTH) }
pub(crate) fn parse_bytes_limited(data: &[u8], max_bytes: usize, max_depth: usize) -> Result<Document> {
    if data.len() > max_bytes { return Err(error("XML exceeds the input byte limit")); }
    let (source, encoding) = decode(data)?;
    if source.len() > max_bytes { return Err(error("XML exceeds the decoded byte limit")); }
    parse_encoded(&source, &encoding, max_depth)
}
fn fragment(data: &[u8], namespaces: &[(String, String)]) -> Result<Document> {
    let (source, _) = decode(data)?;
    let mut opening = String::from("<fragment");
    for (prefix, uri) in namespaces {
        let name = if prefix.is_empty() { "xmlns".into() } else { format!("xmlns:{prefix}") };
        opening.push_str(&format!(" {name}=\"{}\"", escaped(uri, true)));
    }
    parse_encoded(&format!("{opening}>{source}</fragment>"), "UTF-8", MAX_DEPTH + 1)
}

struct XmlState {
    document: Document,
    cached_bytes: Option<Vec<u8>>,
    revision: u64,
    live: bool,
    read_only: bool,
}

/// A handle to one XML tree, shared by standalone views and the containing package.
#[derive(Clone)]
#[pyclass(module = "oxml._core", from_py_object)]
pub struct Xml { state: Arc<RwLock<XmlState>> }
pub struct XmlRead<'a>(RwLockReadGuard<'a, XmlState>);
impl Deref for XmlRead<'_> {
    type Target = Document;
    fn deref(&self) -> &Document { &self.0.document }
}
impl Xml {
    pub fn from_document(document: Document) -> Self {
        Self { state: Arc::new(RwLock::new(XmlState { document, cached_bytes: None, revision: 0, live: true, read_only: false })) }
    }
    pub fn read(&self) -> Result<XmlRead<'_>> {
        let state = self.state.read().map_err(|_| error("XML state lock was poisoned"))?;
        if !state.live { return Err(stale()); }
        Ok(XmlRead(state))
    }
    pub fn edit<T>(&self, operation: impl FnOnce(&mut Document) -> Result<T>) -> Result<T> {
        let mut state = self.state.write().map_err(|_| error("XML state lock was poisoned"))?;
        if !state.live { return Err(stale()); }
        if state.read_only { return Err(error("Signed packages are read-only")); }
        let before = state.document.mutation;
        let result = operation(&mut state.document);
        // Includes partial edits before a failure, without dirtying rejected preconditions.
        if state.document.mutation != before {
            state.cached_bytes = None;
            state.revision += 1;
        }
        result
    }
    pub fn to_bytes(&self) -> Result<Vec<u8>> {
        let mut state = self.state.write().map_err(|_| error("XML state lock was poisoned"))?;
        if !state.live { return Err(stale()); }
        if state.cached_bytes.is_none() { state.cached_bytes = Some(state.document.serialize()?); }
        Ok(state.cached_bytes.as_ref().unwrap().clone())
    }
    pub fn subtree(&self, id: usize) -> Result<Vec<u8>> {
        let doc = self.read()?;
        if doc.node(id)?.element().is_none() { return Err(error("Expected an XML element")); }
        let mut writer = Writer::new(Vec::new());
        // Include an empty default binding: the destination may have a nonempty default.
        doc.write_node(&mut writer, id, &[("xml".into(), XML_NS.into())])?;
        Ok(writer.into_inner())
    }
    pub fn attach_document(&self, parent: usize, source: &Document, index: Option<usize>) -> Result<usize> {
        let name = &source.node(source.root)?.element().ok_or_else(|| error("Expected an XML element"))?.name;
        self.edit(|doc| {
            let index = match index { Some(i) => i, None => crate::schema::insertion_position(doc, parent, &name.uri, &name.local)? };
            let id = doc.import_at(source, source.root, parent, index)?;
            crate::schema::reorder(doc, id, true, false)?;
            Ok(id)
        })
    }
    pub(crate) fn set_read_only(&self, value: bool) { self.state.write().unwrap().read_only = value; }
    pub(crate) fn insert_fragment(doc: &mut Document, parent: Option<usize>, index: usize, data: &[u8], replacing: Option<usize>) -> Result<Vec<usize>> {
        if index > doc.sequence(parent)?.len() { return Err(Error::Index("XML content index out of range".into())); }
        let namespaces = match parent { Some(id) => doc.node(id)?.element().unwrap().namespaces.clone(), None => base_namespaces() };
        let source = fragment(data, &namespaces)?;
        let roots = &source.node(source.root)?.children;
        let depth = doc.depth(parent)?;
        let count = roots.iter().map(|id| source.subtree_size(*id, depth)).collect::<Result<Vec<_>>>()?.iter().sum();
        doc.capacity(count)?;
        if parent.is_none() {
            let remaining_roots = doc.children.iter().filter(|id| Some(**id) != replacing && doc.nodes[**id].as_ref().unwrap().element().is_some()).count();
            let new_roots = roots.iter().filter(|id| source.nodes[**id].as_ref().unwrap().element().is_some()).count();
            if remaining_roots + new_roots != 1 { return Err(error("XML requires exactly one document element")); }
            if roots.iter().any(|id| matches!(&source.nodes[*id].as_ref().unwrap().kind, NodeKind::Text(s) if !s.chars().all(space))) {
                return Err(error("Character data outside the document element"));
            }
        }
        if let Some(id) = replacing {
            doc.sequence_mut(parent)?.remove(index);
            doc.drop_subtree(id);
        }
        let mut ids = Vec::new();
        for id in roots { ids.push(doc.import(&source, *id, parent)?); }
        let sequence = doc.sequence_mut(parent)?;
        sequence.truncate(sequence.len() - ids.len());
        sequence.splice(index..index, ids.iter().copied());
        if parent.is_none() { doc.root = *doc.children.iter().find(|id| doc.nodes[**id].as_ref().unwrap().element().is_some()).unwrap(); }
        Ok(ids)
    }
}
#[pymethods]
impl Xml {
    #[new]
    pub fn new(data: &[u8]) -> Result<Self> {
        let document = parse_bytes(data)?;
        Ok(Self { state: Arc::new(RwLock::new(XmlState {
            document, cached_bytes: Some(data.to_vec()), revision: 0, live: true, read_only: false,
        })) })
    }
    #[getter]
    pub fn root(&self) -> Result<usize> { Ok(self.read()?.root) }
    #[getter]
    pub fn revision(&self) -> u64 { self.state.read().unwrap().revision }
    pub fn same_state(&self, other: &Xml) -> bool { Arc::ptr_eq(&self.state, &other.state) }
    fn bytes<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> { Ok(PyBytes::new(py, &py.detach(|| self.to_bytes())?)) }
    fn subtree_bytes<'py>(&self, py: Python<'py>, id: usize) -> PyResult<Bound<'py, PyBytes>> {
        Ok(PyBytes::new(py, &py.detach(|| self.subtree(id))?))
    }
    #[staticmethod]
    pub fn check_qname(name: &str) -> Result<()> {
        if let Some((prefix, local)) = name.split_once(':') {
            check_local(prefix)?;
            check_local(local)
        } else { check_local(name) }
    }
    pub fn document_text(&self) -> Result<String> { String::from_utf8(self.read()?.serialize()?).map_err(error) }
    pub fn node(&self, id: usize) -> Result<String> { Ok(self.read()?.row(id)?.to_string()) }
    pub fn is_element(&self, id: usize) -> Result<bool> { Ok(self.read()?.node(id)?.element().is_some()) }
    pub fn text(&self, id: usize) -> Result<String> {
        let doc = self.read()?;
        let node = doc.node(id)?;
        Ok(if node.element().is_some() {
            node.children.iter().filter_map(|id| match &doc.nodes[*id].as_ref().unwrap().kind {
                NodeKind::Text(t) => Some(t.as_str()), _ => None,
            }).collect()
        } else { node.text().unwrap_or("").to_string() })
    }
    pub fn qname(&self, id: usize) -> Result<(String, String)> {
        let doc = self.read()?;
        let element = doc.node(id)?.element().ok_or_else(|| error("Expected an XML element"))?;
        Ok((element.name.uri.clone(), element.name.local.clone()))
    }
    pub fn attribute(&self, id: usize, uri: &str, local: &str) -> Result<Option<String>> {
        let doc = self.read()?;
        let element = doc.node(id)?.element().ok_or_else(|| error("Expected an XML element"))?;
        Ok(element.attributes.iter().find(|a| a.name.uri == uri && a.name.local == local).map(|a| a.value.clone()))
    }
    pub fn element_ids(&self) -> Result<Vec<usize>> { Ok(self.read()?.element_ids()) }
    pub fn document_children(&self) -> Result<Vec<usize>> { Ok(self.read()?.children.clone()) }
    pub fn children(&self, id: usize) -> Result<Vec<usize>> { Ok(self.read()?.node(id)?.children.clone()) }
    pub fn child_count(&self, id: usize) -> Result<usize> { Ok(self.read()?.node(id)?.children.len()) }
    pub fn element_children(&self, id: usize) -> Result<Vec<usize>> {
        let doc = self.read()?;
        let children = doc.element_children(id)?;
        Ok(children.collect())
    }
    pub fn parent(&self, id: usize) -> Result<Option<usize>> { Ok(self.read()?.node(id)?.parent) }
    pub fn position(&self, id: usize) -> Result<(Option<usize>, usize)> { self.read()?.position(id) }
    pub fn set_text(&self, id: usize, value: &str) -> Result<()> { self.edit(|doc| doc.set_text(id, value)) }
    #[pyo3(signature=(id, uri, local, value, prefix=None))]
    pub fn set_attribute(&self, id: usize, uri: &str, local: &str, value: &str, prefix: Option<&str>) -> Result<()> { self.edit(|doc| doc.set_attribute(id, uri, local, value, prefix)) }
    pub fn set_attribute_ns(&self, id: usize, uri: &str, local: &str, value: &str, preferred: &str) -> Result<String> { self.edit(|doc| doc.set_attribute_ns(id, uri, local, value, preferred)) }
    pub fn remove_attribute(&self, id: usize, uri: &str, local: &str) -> Result<()> { self.edit(|doc| doc.remove_attribute(id, uri, local)) }
    #[pyo3(signature=(id, uri, local, prefix=None))]
    pub fn rename(&self, id: usize, uri: &str, local: &str, prefix: Option<&str>) -> Result<()> { self.edit(|doc| doc.rename(id, uri, local, prefix)) }
    pub fn declare_namespace(&self, id: usize, prefix: &str, uri: &str) -> Result<()> { self.edit(|doc| doc.declare_namespace(id, prefix, uri)) }
    pub fn insert_xml(&self, parent: usize, index: usize, data: &[u8]) -> Result<Vec<usize>> {
        self.edit(|doc| Self::insert_fragment(doc, Some(parent), index, data, None))
    }
    #[pyo3(signature=(parent, data, index=None))]
    pub fn attach_xml(&self, parent: usize, data: &[u8], index: Option<usize>) -> Result<usize> {
        self.attach_document(parent, &parse_bytes(data)?, index)
    }
    #[pyo3(signature=(parent, source, id, index=None))]
    pub fn attach_element(&self, parent: usize, source: &Xml, id: usize, index: Option<usize>) -> Result<usize> {
        let source = source.read()?.subtree(id)?;
        self.attach_document(parent, &source, index)
    }
    pub fn insert_text(&self, parent: usize, index: usize, value: &str) -> Result<usize> {
            self.edit(|doc| {
                check_value(value)?;
                doc.insert_kind(parent, index, NodeKind::Text(value.into()))
            })
    }
    pub fn insert_comment(&self, parent: usize, index: usize, value: &str) -> Result<usize> {
            self.edit(|doc| {
                check_comment(value)?;
                doc.insert_kind(parent, index, NodeKind::Comment(normalize_eols(value)))
            })
    }
    #[pyo3(signature=(parent, index, target, value=""))]
    pub fn insert_pi(&self, parent: usize, index: usize, target: &str, value: &str) -> Result<usize> {
            self.edit(|doc| {
                check_pi(target, value)?;
                doc.insert_kind(parent, index, NodeKind::Pi { target: target.into(), value: normalize_eols(value) })
            })
    }
    pub fn delete(&self, id: usize) -> Result<()> { self.edit(|doc| doc.remove(id)) }
    pub fn replace_node(&self, id: usize, data: &[u8]) -> Result<Vec<usize>> {
            self.edit(|doc| {
                let (parent, index) = doc.position(id)?;
                Self::insert_fragment(doc, parent, index, data, Some(id))
            })
    }
    pub fn copy(&self, id: usize, parent: usize, index: usize) -> Result<usize> { self.edit(|doc| doc.copy(id, parent, index)) }
    pub fn copy_to(&self, id: usize, destination: &Xml, parent: usize, index: usize) -> Result<usize> {
        if self.same_state(destination) { return self.copy(id, parent, index); }
        let source = self.read()?.subtree(id)?;
        destination.edit(|doc| doc.import_at(&source, source.root, parent, index))
    }
    pub fn move_to(&self, id: usize, destination: &Xml, parent: usize, index: usize) -> Result<()> {
        if !self.same_state(destination) { return Err(error("Cross-part movement is not supported")); }
        self.move_node(id, parent, index)
    }
    pub fn move_node(&self, id: usize, parent: usize, index: usize) -> Result<()> { self.edit(|doc| doc.move_node(id, parent, index)) }
    pub fn replace(&self, data: &[u8]) -> Result<()> {
        let parsed = parse_bytes(data)?;
        self.edit(|doc| {
            let mut candidate = Document {
                nodes: vec![None; doc.nodes.len()], root: 0, children: Vec::new(), declaration: parsed.declaration.clone(), mutation: doc.mutation.wrapping_add(1),
            };
            for id in &parsed.children { candidate.import(&parsed, *id, None)?; }
            candidate.set_root()?;
            *doc = candidate;
            Ok(())
        })?;
        self.state.write().unwrap().cached_bytes = Some(data.to_vec());
        Ok(())
    }
    pub fn invalidate(&self) {
        let mut state = self.state.write().unwrap();
        state.live = false;
        state.document.nodes.clear();
        state.document.children.clear();
        state.cached_bytes = None;
        state.revision += 1;
    }
}
