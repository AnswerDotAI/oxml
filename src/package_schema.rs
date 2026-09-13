//! Imported part declarations: discovery, creation, stories and package validation.
use crate::error::{Error, Result};
use crate::package::{Package, PackageData, Part, Relationship};
use crate::schema::{analyze_tree, arr, available, check_target, expanded, s, schema};
use crate::xml::{Document, Element, Name};
use pyo3::prelude::*;
use serde_json::{json, Value};
use std::collections::{BTreeSet, HashMap, HashSet, VecDeque};
use std::sync::OnceLock;

fn part(name: Option<&str>) -> &'static Value { &schema()["parts"][name.unwrap_or("")] }
type PartRules = HashMap<&'static str, (&'static Value, &'static Value)>;
#[derive(Default)]
struct PartIndex {
    roots: HashMap<&'static str, Option<(&'static str, &'static str)>>,
    rules: HashMap<&'static str, PartRules>,
    empty: PartRules,
    paths: HashSet<&'static str>,
}
fn part_index() -> &'static PartIndex {
    static INDEX: OnceLock<PartIndex> = OnceLock::new();
    INDEX.get_or_init(|| {
        let mut index = PartIndex::default();
        let mut roots: HashMap<&str, HashSet<_>> = HashMap::new();
        for t in schema()["types"].as_object().unwrap().values() {
            let name = expanded(s(&t["qname"]));
            roots.entry(name.1).or_default().insert(name);
        }
        for (name, info) in schema()["parts"].as_object().unwrap() {
            let local = s(&info["RootElement"]);
            let root = if let Some(root) = info["Root"].as_str() {
                Some((s(&schema()["namespaces"][root.split(':').next().unwrap_or("")]), local))
            } else { roots.get(local).filter(|names| names.len() == 1).and_then(|names| names.iter().next().copied()) };
            index.roots.insert(name, root);
            index.rules.insert(name, arr(&info["Children"]).iter().filter_map(|rule| {
                let info = part(rule["Name"].as_str());
                info["RelationshipType"].as_str().map(|kind| (kind, (rule, info)))
            }).collect());
        }
        index.paths = arr(&schema()["semantics"]).iter().filter_map(|r| s(&r["Test"]).split_once("document('Part:")?.1.split_once("')").map(|(path, _)| path)).collect();
        index
    })
}
fn rules(name: Option<&str>) -> &'static PartRules { part_index().rules.get(name.unwrap_or("")).unwrap_or(&part_index().empty) }
pub fn part_root(name: &str) -> Result<(&'static str, &'static str)> {
    part_index().roots.get(name).copied().flatten().ok_or_else(|| Error::Unsupported(format!("No unambiguous XML root descriptor for {name}")))
}

pub fn declared_uri(package: &mut PackageData, name: &str, create: bool) -> Result<Option<String>> {
    let info = part(Some(name));
    if info.is_null() { return Err(Error::Invalid(format!("Unknown declared part {name}"))); }
    let main = package.main_part().to_string();
    let mut relations = package.relationships(&main)?.into_iter().filter(|r| r.kind == s(&info["RelationshipType"]));
    let relation = relations.next();
    if relations.next().is_some() { return Err(Error::Invalid(format!("Ambiguous {name} relationship"))); }
    if relation.is_none() && !create { return Ok(None); }
    let expected = part_root(name)?;
    let uri = if let Some(relation) = relation {
        if relation.mode != "Internal" { return Err(Error::Invalid(format!("{name} must be an internal part"))); }
        package.relationship_target(&main, &relation.target)?
    } else {
        let base = format!("{}/{}", main.rsplit_once('/').map_or("", |(dir, _)| dir), s(&info["Target"]));
        let names: HashSet<_> = package.part_names().into_iter().map(|n| n.to_lowercase()).collect();
        let mut uri = format!("{base}.xml");
        let mut number = 0;
        while names.contains(&uri.to_lowercase()) { number += 1; uri = format!("{base}{number}.xml"); }
        let prefix = schema()["namespaces"].as_object().unwrap().iter().find(|(_, value)| s(value) == expected.0).map_or("", |(prefix, _)| prefix);
        let root = Element { name: Name { uri: expected.0.into(), local: expected.1.into(), prefix: prefix.into() },
            attributes: Vec::new(), namespaces: vec![(prefix.into(), expected.0.into())] };
        package.add_part(&uri, s(&info["ContentType"]), &Document::from_element(root).serialize()?)?;
        package.add_relationship(&main, s(&info["RelationshipType"]), &uri, "Internal", None)?;
        uri
    };
    let xml = package.load_xml(&uri)?;
    let doc = xml.read()?;
    let actual = &doc.node(doc.root)?.element().unwrap().name;
    if package.content_type(&uri)? != s(&info["ContentType"]) || (actual.uri.as_str(), actual.local.as_str()) != expected {
        return Err(Error::Invalid(format!("Unexpected {name} part structure")));
    }
    Ok(Some(uri))
}

struct RelatedPart { relationship: Relationship, uri: Option<String>, name: Option<&'static str> }
impl RelatedPart {
    fn value(&self) -> Value {
        let r = &self.relationship;
        json!({"id":r.id,"type":r.kind,"target":r.target,"target_mode":r.mode,"part_uri":self.uri,"part_name":self.name})
    }
}
struct Entry { uri: String, name: Option<&'static str>, relationships: Vec<RelatedPart>, errors: Vec<Value> }

fn related_parts(package: &PackageData) -> Result<Vec<Entry>> {
    let mut entries = Vec::new();
    let mut visited = HashSet::new();
    let mut pending = VecDeque::from([("/".to_string(), Some("WordprocessingDocument"))]);
    while let Some((uri, name)) = pending.pop_front() {
        if !visited.insert(uri.clone()) { continue; }
        let mut entry = Entry { uri, name, relationships: Vec::new(), errors: Vec::new() };
        let relationships = match package.relationships(&entry.uri) {
            Ok(relationships) => relationships,
            Err(error) => {
                entry.errors.push(json!({"part_uri":entry.uri,"error":error.to_string()}));
                entries.push(entry);
                continue;
            }
        };
        let rules = rules(name);
        for mut relationship in relationships {
            const STRICT: &str = "http://purl.oclc.org/ooxml/officeDocument/relationships/";
            if let Some(suffix) = relationship.kind.strip_prefix(STRICT) {
                relationship.kind = format!("http://schemas.openxmlformats.org/officeDocument/2006/relationships/{suffix}");
            }
            let mut rel = RelatedPart { name: rules.get(relationship.kind.as_str()).and_then(|(_, info)| info["Name"].as_str()), relationship, uri: None };
            if rel.relationship.mode != "External" {
                match package.relationship_target(&entry.uri, &rel.relationship.target) {
                    Ok(uri) => {
                        if uri == package.main_part() { rel.name = Some("MainDocumentPart"); }
                        if rel.name.is_none() {
                            let content_type = package.content_type(&uri)?;
                            let mut candidates = schema()["parts"].as_object().unwrap().iter().filter(|(_, info)|
                                s(&info["RelationshipType"]) == rel.relationship.kind && s(&info["ContentType"]) == content_type);
                            let candidate = candidates.next();
                            if candidates.next().is_none() { rel.name = candidate.map(|(name, _)| name.as_str()); }
                        }
                        pending.push_back((uri.clone(), rel.name));
                        rel.uri = Some(uri);
                    }
                    Err(error) => entry.errors.push(json!({"part_uri":entry.uri,"relationship_id":rel.relationship.id,
                        "target":rel.relationship.target,"error":error.to_string()})),
                }
            }
            entry.relationships.push(rel);
        }
        entries.push(entry);
    }
    Ok(entries)
}

fn story_container(name: Option<&str>) -> Option<Option<&'static str>> {
    match name? {
        "MainDocumentPart" | "HeaderPart" | "FooterPart" => Some(None),
        "FootnotesPart" => Some(Some("footnote")), "EndnotesPart" => Some(Some("endnote")),
        "WordprocessingCommentsPart" => Some(Some("comment")), _ => None,
    }
}

pub fn story_nodes(package: &mut PackageData) -> Result<Vec<(String, usize)>> {
    let mut stories = Vec::new();
    for entry in related_parts(package)? {
        if entry.errors.iter().any(|e| e["relationship_id"].is_null()) {
            return Err(Error::Invalid(format!("Cannot read relationships from {}", entry.uri)));
        }
        if let Some(rel) = entry.relationships.iter().find(|r| story_container(r.name).is_some() && r.uri.is_none()) {
            return Err(Error::Invalid(format!("Cannot read story target {} from {}", rel.relationship.target, entry.uri)));
        }
        let Some(container) = story_container(entry.name) else { continue; };
        let xml = package.load_xml(&entry.uri)?;
        let doc = xml.read()?;
        if let Some(local) = container {
            let word = s(&schema()["namespaces"]["w"]);
            for &id in &doc.node(doc.root)?.children {
                if doc.node(id)?.element().is_some_and(|e| e.name.uri == word && e.name.local == local) { stories.push((entry.uri.clone(), id)); }
            }
        } else { stories.push((entry.uri, doc.root)); }
    }
    Ok(stories)
}

fn package_issue(entry: &Entry, target: &str, rule: &str, expected: impl Into<Value>, actual: impl Into<Value>) -> Value {
    json!({"rule_id":rule,"category":"package","severity":"error","node":null,"part_uri":entry.uri,"target":target,
        "expected":expected.into(),"actual":actual.into(),"rule_provenance":{"path":format!("data/parts/{}.json", entry.name.unwrap_or("None"))}})
}

fn dependency<'a>(entries: &'a [Entry], mut uri: &'a str, path: &str) -> Option<&'a str> {
    if path == "." { return Some(uri); }
    if path == ".." {
        return entries.iter().find(|e| e.uri != "/" && e.relationships.iter().any(|r| r.uri.as_deref() == Some(uri))).map(|e| e.uri.as_str());
    }
    if path.starts_with('/') { uri = "/"; }
    for name in path.trim_matches('/').split('/') {
        uri = entries.iter().find(|e| e.uri == uri)?.relationships.iter().find(|r| r.name == Some(name))?.uri.as_deref()?;
    }
    Some(uri)
}

pub fn validate(package: &mut PackageData, target: &str) -> Result<Value> {
    check_target(target)?;
    let entries = related_parts(package)?;
    let mut issues = Vec::new();
    let mut incomplete = Vec::new();
    let mut gaps = BTreeSet::new();
    let mut invalid_parts = HashSet::new();
    for entry in &entries {
        let rules = rules(entry.name);
        let mut counts: HashMap<&str, usize> = HashMap::new();
        for error in &entry.errors {
            incomplete.push(error.clone());
            issues.push(package_issue(entry, target, if error["target"].is_null() { "relationship-xml" } else { "relationship-target" }, "readable internal relationships", error.clone()));
        }
        for rel in &entry.relationships {
            let Some(uri) = &rel.uri else { continue; };
            let kind = rel.relationship.kind.as_str();
            let Some(&(rule, info)) = rules.get(kind) else {
                if entry.name.is_some() && schema()["parts"].as_object().unwrap().values().any(|i| s(&i["RelationshipType"]) == kind && available(s(&i["Version"]), target)) {
                    issues.push(package_issue(entry, target, "part-not-allowed", format!("a permitted {} relationship", entry.name.unwrap()), rel.value()));
                }
                continue;
            };
            if !available(s(&info["Version"]), target) { continue; }
            *counts.entry(kind).or_default() += 1;
            let actual = package.content_type(uri)?;
            if rule["HasFixedContent"] == true && info["ContentType"] != actual {
                issues.push(package_issue(entry, target, "part-content-type", info["ContentType"].clone(), json!({"part_uri":uri,"content_type":actual})));
                invalid_parts.insert(uri.as_str());
            }
        }
        for (kind, (rule, info)) in rules {
            if !available(s(&info["Version"]), target) { continue; }
            let count = counts.get(kind).copied().unwrap_or(0);
            if count > 1 && rule["MaxOccursGreatThanOne"] != true { issues.push(package_issue(entry, target, "part-cardinality", format!("at most one {kind}"), count)); }
            if count == 0 && rule["MinOccursIsNonZero"] == true { issues.push(package_issue(entry, target, "part-required", format!("at least one {kind}"), count)); }
        }
    }
    let mut trees = HashMap::new();
    let mut scope = Vec::new();
    for entry in &entries {
        if entry.uri == "/" { continue; }
        let info = part(entry.name);
        if !available(s(&info["Version"]), target) || invalid_parts.contains(entry.uri.as_str()) {
            gaps.insert(format!("unchecked-part:{}", entry.uri));
            continue;
        }
        if info["Root"].is_null() && info["RootElement"].is_null() {
            if info.is_null() { gaps.insert(format!("opaque-part:{}", entry.uri)); }
            continue;
        }
        match package.load_xml(&entry.uri) {
            Ok(xml) => {
                let doc = xml.read()?;
                let actual = &doc.node(doc.root)?.element().unwrap().name;
                match part_root(entry.name.unwrap()) {
                    Ok(_) if actual.uri.starts_with("http://purl.oclc.org/ooxml/") => { gaps.insert(format!("strict-part-root:{}", entry.uri)); }
                    Ok(expected) if (actual.uri.as_str(), actual.local.as_str()) != expected => {
                        issues.push(package_issue(entry, target, "part-root", json!([expected.0, expected.1]), json!([actual.uri, actual.local])));
                    }
                    Err(_) => { gaps.insert(format!("unknown-part-root:{}", entry.uri)); }
                    _ => (),
                }
                drop(doc);
                scope.push(entry.uri.clone());
                trees.insert(entry.uri.as_str(), xml);
            }
            Err(error) => {
                incomplete.push(json!({"part_uri":entry.uri,"error":error.to_string()}));
                issues.push(package_issue(entry, target, "part-xml", "well-formed XML", error.to_string()));
            }
        }
    }
    let (mut checked, mut semantic_checks, mut skipped) = (0_u64, 0_u64, Vec::new());
    for entry in &entries {
        let Some(xml) = trees.get(entry.uri.as_str()) else { continue; };
        let dependencies = part_index().paths.iter().filter_map(|&path| Some((path.to_string(), trees.get(dependency(&entries, &entry.uri, path)?)?.clone()))).collect();
        let relationships = entry.relationships.iter().map(|r| (r.relationship.id.clone(), r.relationship.kind.clone())).collect();
        let mut report = analyze_tree(xml, target, &dependencies, Some(&relationships), incomplete.is_empty() && invalid_parts.is_empty())?;
        for mut issue in report["issues"].as_array_mut().unwrap().drain(..) {
            issue["part_uri"] = entry.uri.clone().into();
            issue["target"] = target.into();
            issues.push(issue);
        }
        checked += report["coverage"]["schema_nodes_checked"].as_u64().unwrap();
        semantic_checks += report["coverage"]["semantic_checks"].as_u64().unwrap();
        for mut region in report["coverage"]["skipped_regions"].as_array_mut().unwrap().drain(..) { region["part_uri"] = entry.uri.clone().into(); skipped.push(region); }
        gaps.extend(arr(&report["coverage"]["gaps"]).iter().map(|g| s(g).to_string()));
    }
    Ok(json!({"issues":issues,"target":target,"source":schema()["source"],"scope":{"part_uris":scope},
        "coverage":{"schema_nodes_checked":checked,"semantic_checks":semantic_checks,"skipped_regions":skipped,"complete":false,
            "gaps":gaps,"incomplete_dependencies":incomplete}}))
}

#[pyfunction]
#[pyo3(signature=(package, name, create=false))]
pub fn declared_part(package: &Package, name: &str, create: bool) -> Result<Option<Part>> {
    let uri = declared_uri(&mut *package.lock()?, name, create)?;
    uri.map(|uri| package.part(&uri)).transpose()
}

#[pyfunction]
pub fn package_stories(package: &Package) -> Result<Vec<(Part, usize)>> {
    let nodes = story_nodes(&mut *package.lock()?)?;
    nodes.into_iter().map(|(uri, id)| Ok((package.part(&uri)?, id))).collect()
}

#[pyfunction]
#[pyo3(signature=(package, target="Microsoft365"))]
pub fn validate_package(py: Python<'_>, package: &Package, target: &str) -> Result<String> {
    py.detach(|| validate(&mut *package.lock()?, target).map(|report| report.to_string()))
}
