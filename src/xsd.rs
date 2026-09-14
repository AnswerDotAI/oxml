//! Complementary XSD checks. SDK rules retain responsibility for extension placement and attributes.
use crate::{error::{Error, Result}, schema::{effective, ignorable, namespace_available, MC}, xml::{self, Document, NodeKind}};
use libxml::{bindings, error::StructuredError, parser::{Parser, ParserOptions}, schemas::{SchemaParserContext, SchemaValidationContext}};
use serde_json::{json, Value};
use std::{cell::RefCell, collections::{BTreeSet, HashMap, HashSet}, ffi::c_void, sync::OnceLock};

include!(concat!(env!("OUT_DIR"), "/xsd_assets.rs"));
const XSD: &str = "http://www.w3.org/2001/XMLSchema";

struct Sources {
    roots: HashSet<(String, String)>,
    base: HashSet<String>,
    imports: String,
}

fn sources() -> Result<&'static Sources> {
    static SOURCES: OnceLock<std::result::Result<Sources, String>> = OnceLock::new();
    SOURCES.get_or_init(|| {
        let (mut roots, mut base) = (HashSet::new(), HashSet::new());
        let mut imports = String::new();
        for &(name, bytes) in ASSETS {
            let doc = xml::parse_bytes(bytes).map_err(|e| e.to_string())?;
            let uri = doc.node(doc.root).unwrap().element().unwrap().attribute("", "targetNamespace").unwrap();
            if !uri.starts_with("http://schemas.microsoft.com/") { base.insert(uri.to_owned()); }
            for id in doc.element_children(doc.root).unwrap() {
                let e = doc.node(id).unwrap().element().unwrap();
                if e.name.uri == XSD && e.name.local == "element" {
                    if let Some(local) = e.attribute("", "name") { roots.insert((uri.to_owned(), local.to_owned())); }
                }
                if e.name.uri == XSD && ["import", "include"].contains(&e.name.local.as_str()) {
                    if !e.attribute("", "schemaLocation").is_some_and(|p| ASSETS.iter().any(|(n, _)| *n == p)) {
                        return Err(format!("XSD import is not a bundled local file: {name}"));
                    }
                }
            }
            imports.push_str(&format!("<xs:import namespace=\"{uri}\" schemaLocation=\"{name}\"/>"));
        }
        Ok(Sources { roots, base, imports })
    }).as_ref().map_err(|e| Error::Invalid(format!("Bundled XSD setup failed: {e}")))
}

thread_local! { static VALIDATOR: RefCell<Option<SchemaValidationContext>> = const { RefCell::new(None) }; }

// The wrapper omits xmlError.node. Retain its address solely to map diagnostics back to our live node IDs.
unsafe extern "C" fn capture(context: *mut c_void, error: *const bindings::xmlError) {
    if context.is_null() || error.is_null() { return; }
    let errors = &mut *(context as *mut Vec<(usize, StructuredError)>);
    errors.push(((*error).node as usize, StructuredError::from_raw(error)));
}

struct Projection<'a> {
    source: &'a Document,
    target: &'a str,
    sources: &'a Sources,
    ids: Vec<usize>,
    affected: HashSet<usize>,
    extensions: Vec<usize>,
    gaps: &'a mut BTreeSet<String>,
}

impl Projection<'_> {
    fn append(&mut self, id: usize, parent: Option<usize>, output: &mut Document) -> Result<usize> {
        let mut node = self.source.node(id)?.clone();
        let local_id = output.nodes.len();
        node.id = local_id;
        node.parent = parent;
        node.children.clear();
        if let NodeKind::Element(e) = &mut node.kind {
            self.ids.push(id);
            e.attributes.retain(|a| {
                if a.name.uri == MC { return false; }
                if !a.name.uri.is_empty() && a.name.uri != e.name.uri && !self.sources.base.contains(&a.name.uri)
                    && (namespace_available(&a.name.uri, self.target) || ignorable(self.source, id, &a.name.uri)) {
                    self.gaps.insert(format!("xsd:extension-attribute:{{{}}}{}", a.name.uri, a.name.local));
                    return false;
                }
                true
            });
        }
        output.nodes.push(Some(node));
        let source = self.source.node(id)?;
        let base_parent = source.element().is_some_and(|e| self.sources.base.contains(&e.name.uri));
        // Text nodes keep their exact content; effective() supplies only compatibility-selected element children.
        let selected = effective(self.source, id, self.target, &mut Vec::new(), &mut Vec::new());
        let mut child_ids = Vec::new();
        if selected == self.source.element_children(id)?.collect::<Vec<_>>() { child_ids.extend(&source.children); }
        else {
            // OOXML MC wrappers appear in element-only content. Keep surrounding text and unwrap selected branches in place.
            for &child in &source.children {
                if self.source.node(child)?.element().is_none() { child_ids.push(child); }
                else if selected.contains(&child) { child_ids.push(child); }
                else {
                    child_ids.extend(selected.iter().copied().filter(|&candidate| {
                        std::iter::successors(Some(candidate), |&n| self.source.node(n).ok()?.parent).any(|n| n == child)
                    }));
                }
            }
        }
        for child in child_ids {
            if let Some(e) = self.source.node(child)?.element() {
                if base_parent && !self.sources.base.contains(&e.name.uri) && namespace_available(&e.name.uri, self.target) {
                    self.affected.insert(id);
                    self.extensions.push(child);
                    self.gaps.insert(format!("xsd:extension-placement:{{{}}}{}", e.name.uri, e.name.local));
                    continue;
                }
            }
            let inserted = self.append(child, Some(local_id), output)?;
            output.nodes[local_id].as_mut().unwrap().children.push(inserted);
        }
        Ok(local_id)
    }
}

pub(crate) fn validate(doc: &Document, target: &str, issues: &mut Vec<Value>, gaps: &mut BTreeSet<String>) -> Result<usize> {
    let sources = sources()?;
    VALIDATOR.with(|cache| {
        let mut cached = cache.borrow_mut();
        if cached.is_none() {
            libxml::init_parser(); // Once-only library initialization; schema primitive initialization is also guarded by the wrapper.
            // Imports compile eagerly. The private files disappear immediately after compilation, not at process exit.
            let directory = tempfile::tempdir()?;
            for &(name, bytes) in ASSETS { std::fs::write(directory.path().join(name), bytes)?; }
            std::fs::write(directory.path().join("all.xsd"), format!("<xs:schema xmlns:xs=\"{XSD}\">{}</xs:schema>", sources.imports))?;
            let mut parser = SchemaParserContext::from_file(directory.path().join("all.xsd").to_str().unwrap());
            *cached = Some(SchemaValidationContext::from_parser(&mut parser).map_err(|e| Error::Invalid(format!("Bundled XSD compilation failed: {e:?}")))?);
        }
        let validator = cached.as_mut().unwrap();
        let mut pending = vec![doc.root];
        let mut checked = 0;
        while let Some(root) = pending.pop() {
            let e = doc.node(root)?.element().unwrap();
            if !sources.roots.contains(&(e.name.uri.clone(), e.name.local.clone())) {
                gaps.insert(format!("xsd:no-root-declaration:{{{}}}{}", e.name.uri, e.name.local));
                continue;
            }
            let mut projection = Projection { source: doc, target, sources, ids: Vec::new(), affected: HashSet::new(), extensions: Vec::new(), gaps };
            let mut view = Document::from_element(e.clone());
            view.nodes.clear();
            projection.append(root, None, &mut view)?;
            pending.extend(&projection.extensions);
            let parsed = Parser::default().parse_string_with_options(view.serialize()?, ParserOptions { recover: false, no_net: true, ..Default::default() })
                .map_err(|e| Error::Invalid(format!("Cannot parse XSD validation view: {e:?}")))?;
            let mut pointers = HashMap::new();
            let mut nodes = vec![parsed.get_root_element().unwrap()];
            for source_id in &projection.ids {
                let node = nodes.pop().unwrap();
                pointers.insert(node.node_ptr() as usize, *source_id);
                nodes.extend(node.get_child_elements().into_iter().rev());
            }
            let mut errors: Vec<(usize, StructuredError)> = Vec::new();
            // Callback and buffer live for this synchronous call only. No global error handler or document mutation.
            let result = unsafe {
                // libxml2 2.12 added const to the error pointer; the callback ABI is unchanged.
                let callback = std::mem::transmute::<unsafe extern "C" fn(*mut c_void, *const bindings::xmlError), _>(capture);
                bindings::xmlSchemaSetValidStructuredErrors(validator.as_ptr(), Some(callback), &mut errors as *mut _ as *mut c_void);
                let result = bindings::xmlSchemaValidateDoc(validator.as_ptr(), parsed.doc_ptr());
                bindings::xmlSchemaSetValidStructuredErrors(validator.as_ptr(), None, std::ptr::null_mut());
                result
            };
            if result < 0 { return Err(Error::Invalid("libxml2 XSD validation failed internally".into())); }
            checked += 1;
            for (pointer, error) in errors {
                let id = pointers.get(&pointer).copied().unwrap_or(root);
                let parent = doc.node(id)?.parent;
                let structural = matches!(error.code as u32, bindings::xmlParserErrors_XML_SCHEMAV_ELEMENT_CONTENT | bindings::xmlParserErrors_XML_SCHEMAV_CVC_COMPLEX_TYPE_2_4);
                if structural && (projection.affected.contains(&id) || parent.is_some_and(|p| projection.affected.contains(&p))) {
                    projection.gaps.insert(format!("xsd:projected-content-model:{id}"));
                    continue;
                }
                issues.push(json!({"rule_id":format!("xsd-{}",error.code),"category":"xsd","severity":"error","node":id,
                    "expected":"bundled XSD constraint","actual":error.message.unwrap_or_default().trim(),
                    "rule_provenance":{"source":"ECMA-376 / MS-DOCX XSD","engine":"libxml2"}}));
            }
        }
        Ok(checked)
    })
}
