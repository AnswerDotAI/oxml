//! SDK descriptor interpreter. Unsupported mechanisms are reported, never treated as successful checks.
use crate::xml::{Document, Node, NodeKind, Xml};
use crate::error::{Error, Result};
use pyo3::prelude::*;
use serde_json::{json, Value};
use std::collections::{BTreeSet, HashMap, HashSet};
use std::sync::OnceLock;

const DATA: &str = include_str!("../schema/metadata.json");
const MC: &str = "http://schemas.openxmlformats.org/markup-compatibility/2006";
const VERSIONS: [&str; 7] = ["Office2007", "Office2010", "Office2013", "Office2016", "Office2019", "Office2021", "Microsoft365"];
static SCHEMA: OnceLock<Value> = OnceLock::new();
pub(crate) fn schema() -> &'static Value { SCHEMA.get_or_init(|| serde_json::from_str(DATA).expect("generated schema JSON")) }
pub(crate) fn s(v: &Value) -> &str { v.as_str().unwrap_or("") }
pub(crate) fn arr(v: &Value) -> &[Value] { v.as_array().map(Vec::as_slice).unwrap_or(&[]) }
fn rank(v: &str) -> Option<u8> { if v.is_empty() { Some(0) } else { VERSIONS.iter().position(|version| *version == v).map(|i| i as u8) } }
pub(crate) fn available(v: &str, target: &str) -> bool { matches!((rank(v), rank(target)), (Some(a), Some(b)) if a <= b) }
pub fn check_target(target: &str) -> Result<()> {
    if VERSIONS.contains(&target) { Ok(()) } else { Err(Error::Invalid("unknown validation target".into())) }
}
pub(crate) fn expanded(q: &str) -> (&str, &str) {
    let (prefix, local) = q.split_once(':').unwrap_or(("", q));
    (s(&schema()["namespaces"][prefix]), local)
}
fn matches(n: &Node, q: &str) -> bool {
    let (uri, local) = expanded(q);
    n.element().is_some_and(|e| e.name.local == local && e.name.uri == uri)
}
fn qname(id: &str) -> &str { id.rsplit('/').next().unwrap_or("") }
fn attribute<'a>(n: &'a Node, q: &str) -> Option<&'a str> {
    let (uri, local) = expanded(q);
    n.element()?.attribute(uri, local)
}
fn ancestors(doc: &Document, id: usize) -> impl Iterator<Item = &Node> {
    std::iter::successors(Some(id), |id| doc.node(*id).ok()?.parent).map(|id| doc.node(id).expect("live ancestor"))
}
fn children(doc: &Document, id: usize) -> Vec<usize> {
    doc.node(id).expect("live parent").children.iter().copied().filter(|id| doc.node(*id).is_ok_and(|n| n.element().is_some())).collect()
}
fn issue(rule: &str, category: &str, node: usize, expected: impl Into<Value>, actual: impl Into<Value>, provenance: &Value) -> Value {
    json!({"rule_id":rule,"category":category,"severity":"error","node":node,"expected":expected.into(),"actual":actual.into(),
        "rule_provenance":provenance})
}

#[pyfunction]
pub fn namespace_bindings() -> HashMap<&'static str, &'static str> {
    schema()["namespaces"].as_object().unwrap().iter().map(|(k, v)| (k.as_str(), s(v))).collect()
}

type FacadeEnum = (&'static str, &'static str, &'static str, Vec<(&'static str, &'static str)>);
type FacadeAttribute = (&'static str, &'static str);
type FacadeType = (&'static str, &'static str, &'static str, bool, Vec<FacadeAttribute>);

#[pyfunction]
pub fn facade_enums() -> Vec<FacadeEnum> {
    schema()["enums"].as_object().unwrap().values().map(|e| {
        let prefix = s(&e["Type"]).split(':').next().unwrap_or("");
        let members = arr(&e["Facets"]).iter().map(|f| {
            (f["Name"].as_str().filter(|n| !n.is_empty()).unwrap_or(s(&f["Value"])), s(&f["Value"]))
        }).collect();
        (s(&e["full_name"]), prefix, s(&e["Name"]), members)
    }).collect()
}

#[pyfunction]
pub fn facade_types() -> Vec<FacadeType> {
    schema()["types"].as_object().unwrap().iter().filter(|(_, t)| !t["is_abstract"].as_bool().unwrap_or(false)).map(|(id, t)| {
        let prefix = qname(id).split(':').next().unwrap_or("");
        let attrs = arr(&t["attributes"]).iter().map(|a| (s(&a["PropertyName"]), s(&a["Type"]))).collect();
        (id.as_str(), prefix, s(&t["class_name"]), t["is_text"].as_bool().unwrap_or(false), attrs)
    }).collect()
}

type QName = (&'static str, &'static str);

#[derive(Default)]
struct SchemaIndex {
    roots: HashMap<QName, Option<&'static str>>,
    children: HashMap<&'static str, HashMap<QName, Option<&'static str>>>,
    semantics: HashMap<QName, Vec<(usize, &'static Value)>>,
    element_versions: HashMap<QName, u8>,
    namespace_versions: HashMap<&'static str, u8>,
    enums: HashMap<&'static str, &'static Value>,
    particles: HashMap<&'static str, Particle>,
    child_order: HashMap<&'static str, HashMap<QName, usize>>,
}

fn schema_index() -> &'static SchemaIndex {
    static INDEX: OnceLock<SchemaIndex> = OnceLock::new();
    INDEX.get_or_init(|| {
        let mut index = SchemaIndex::default();
        for (id, t) in schema()["types"].as_object().unwrap() {
            let name = expanded(qname(id));
            let version = rank(s(&t["version"])).unwrap_or(u8::MAX);
            index.element_versions.entry(name).and_modify(|v| *v = (*v).min(version)).or_insert(version);
            index.namespace_versions.entry(name.0).and_modify(|v| *v = (*v).min(version)).or_insert(version);
            if !t["is_abstract"].as_bool().unwrap_or(false) { index.roots.entry(name).and_modify(|v| *v = None).or_insert(Some(id)); }
            let children = index.children.entry(id).or_default();
            for child in arr(&t["children"]).iter().map(s) { children.entry(expanded(qname(child))).and_modify(|v| *v = None).or_insert(Some(child)); }
            if !t["particle"].is_null() {
                index.particles.insert(id, Particle::compile(&t["particle"]));
                index.child_order.insert(id, child_order(&t["particle"]));
            }
        }
        for (i, rule) in arr(&schema()["semantics"]).iter().enumerate() { index.semantics.entry(expanded(s(&rule["Context"]))).or_default().push((i, rule)); }
        for definition in schema()["enums"].as_object().unwrap().values() { index.enums.insert(s(&definition["full_name"]), definition); }
        index
    })
}

pub fn enum_contains(full_name: &str, value: &str) -> bool {
    schema_index().enums.get(full_name).is_some_and(|e| arr(&e["Facets"]).iter().any(|f| s(&f["Value"]) == value))
}

fn resolve(n: &Node, parent: Option<&str>) -> Option<&'static str> {
    let index = schema_index();
    let choices = match parent { Some(parent) => index.children.get(parent)?, None => &index.roots };
    let name = &n.element()?.name;
    choices.get(&(name.uri.as_str(), name.local.as_str())).copied().flatten()
}

pub fn document_type(doc: &Document, id: usize) -> Result<Option<&'static str>> {
    if doc.node(id)?.element().is_none() { return Err(Error::Invalid("Expected an XML element".into())); }
    let mut path = ancestors(doc, id).collect::<Vec<_>>();
    path.reverse();
    let mut context = None;
    for node in path { if node.element().unwrap().name.uri != MC { context = resolve(node, context.as_deref()); } }
    Ok(if doc.node(id)?.element().unwrap().name.uri == MC { None } else { context })
}

#[pyfunction]
pub fn element_type(xml: &Xml, id: usize) -> Result<Option<&'static str>> { document_type(&*xml.read()?, id) }

pub fn check_type(doc: &Document, id: usize, expected: Option<&str>) -> Result<()> {
    if document_type(doc, id)? != expected { return Err(Error::Stale("XML element type changed; reacquire its typed view".into())); }
    Ok(())
}

#[pyfunction]
pub fn check_element_type(xml: &Xml, id: usize, expected: Option<&str>) -> Result<()> { check_type(&*xml.read()?, id, expected) }

#[pyfunction]
#[pyo3(signature=(xml, type_id=None))]
pub fn elements_of_type(xml: &Xml, type_id: Option<&str>) -> Result<Vec<usize>> {
    let doc = xml.read()?;
    if type_id.is_none() { return Ok(doc.element_ids()); }
    doc.element_ids().into_iter().filter_map(|id| match document_type(&doc, id) {
        Ok(actual) if actual == type_id => Some(Ok(id)),
        Err(error) => Some(Err(error)),
        _ => None,
    }).collect()
}

fn particle_slots(p: &'static Value) -> (Vec<HashSet<QName>>, HashSet<QName>, bool) {
    if let Some(name) = p["Name"].as_str() { return (vec![HashSet::from([expanded(qname(name))])], HashSet::new(), false); }
    let mut slots = Vec::new();
    let mut ambiguous = HashSet::new();
    let mut unknown = false;
    let unordered = matches!(s(&p["Kind"]), "Choice" | "All");
    for item in arr(&p["Items"]) {
        let (groups, names, wildcard) = particle_slots(item);
        ambiguous.extend(names);
        unknown |= wildcard;
        if unordered && groups.len() > 1 { ambiguous.extend(groups.iter().flatten().copied()); }
        slots.extend(groups);
    }
    let names: HashSet<_> = slots.iter().flatten().copied().collect();
    if unordered { slots = vec![names.clone()]; }
    else if !matches!(s(&p["Kind"]), "Sequence" | "Group") { unknown = true; }
    if slots.len() > 1 && arr(&p["Occurs"]).iter().any(|o| o["Max"].as_u64().is_none_or(|max| max == 0 || max > 1)) {
        ambiguous.extend(&names);
        slots = vec![names];
    }
    (slots, ambiguous, unknown)
}

fn child_order(p: &'static Value) -> HashMap<QName, usize> {
    let (slots, mut ambiguous, unknown) = particle_slots(p);
    let mut order = HashMap::new();
    if unknown { return order; }
    for (rank, names) in slots.into_iter().enumerate() {
        for name in names { if order.insert(name, rank).is_some() { ambiguous.insert(name); } }
    }
    order.retain(|name, _| !ambiguous.contains(name));
    order
}

pub fn insertion_position(doc: &Document, parent: usize, uri: &str, local: &str) -> Result<usize> {
    let order = document_type(doc, parent)?.and_then(|id| schema_index().child_order.get(id));
    let Some((order, &rank)) = order.and_then(|order| order.get(&(uri, local)).map(|rank| (order, rank))) else {
        return Err(Error::Invalid(format!("No unambiguous schema position for {{{uri}}}{local}; supply index=")));
    };
    let children = &doc.node(parent)?.children;
    let mut position = children.len();
    let mut previous = None;
    for (index, &id) in children.iter().enumerate() {
        let Some(e) = doc.node(id)?.element() else { continue; };
        let next = order.get(&(e.name.uri.as_str(), e.name.local.as_str())).copied();
        if next.is_none() || previous.is_some_and(|p| next.unwrap() < p) {
            return Err(Error::Invalid("Existing children have unclear schema order; supply index=".into()));
        }
        if next.unwrap() > rank && position == children.len() { position = index; }
        previous = next;
    }
    Ok(position)
}

#[pyfunction]
pub fn child_position(xml: &Xml, parent: usize, uri: &str, local: &str) -> Result<usize> {
    insertion_position(&*xml.read()?, parent, uri, local)
}

fn pattern(pattern: &str, value: &str, gaps: &mut BTreeSet<String>) -> bool {
    static PATTERNS: OnceLock<std::sync::Mutex<HashMap<String, Option<regex::Regex>>>> = OnceLock::new();
    let mut patterns = PATTERNS.get_or_init(Default::default).lock().unwrap();
    // SDK StringValidator anchors .NET regexes. Unsupported syntax is not a successful check.
    let regex = patterns.entry(pattern.into()).or_insert_with(|| {
        if pattern.contains("-[") { return None; } // .NET character-class subtraction differs in Rust.
        regex::Regex::new(&format!(r"\A(?:{pattern})\z")).ok()
    });
    match regex {
        Some(regex) => regex.is_match(value),
        None => {
            gaps.insert(format!("pattern:{pattern}"));
            true
        }
    }
}

fn base64(value: &str) -> Option<Vec<u8>> {
    use base64::{
        engine::general_purpose::{GeneralPurpose, PAD},
        Engine,
    };
    // Convert.FromBase64String accepts XML whitespace and nonzero unused trailing bits.
    let engine = GeneralPurpose::new(&base64::alphabet::STANDARD, PAD.with_decode_allow_trailing_bits(true));
    engine.decode(value.bytes().filter(|c| !matches!(c, b' ' | b'\t' | b'\r' | b'\n')).collect::<Vec<_>>()).ok()
}

fn float(value: &str, single: bool) -> Option<f64> {
    match value {
        "INF" => Some(f64::INFINITY),
        "-INF" => Some(f64::NEG_INFINITY),
        "NaN" => Some(f64::NAN),
        _ if value.bytes().all(|c| c.is_ascii_digit() || b"+-.eE".contains(&c)) => {
            if single { value.parse::<f32>().ok().map(f64::from) } else { value.parse::<f64>().ok() }
        }
        _ => None,
    }
}

fn decimal(value: &str, gaps: &mut BTreeSet<String>) -> bool {
    if !pattern(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)", value, gaps) { return false; }
    let unsigned = value.trim_start_matches(['+', '-']);
    let (whole, fraction) = unsigned.split_once('.').unwrap_or((unsigned, ""));
    let Some(integer) = (if whole.is_empty() { Some(0) } else { whole.parse::<u128>().ok() }) else { return false; };
    const MAX: u128 = (1u128 << 96) - 1; // System.Decimal's magnitude; precision rounding remains unchecked.
    if integer > MAX { return false; }
    let fraction = fraction.trim_end_matches('0');
    if fraction.len() > 28 || format!("{whole}{fraction}").parse::<u128>().map_or(true, |v| v > MAX) { gaps.insert("decimal:precision-rounding".into()); }
    true
}

fn datetime(value: &str, gaps: &mut BTreeSet<String>) -> bool {
    use chrono::{Datelike, Timelike};
    if !value.contains('T') && pattern(r"([0-9]{4}|[0-9:+.Z-]*[-:][0-9:+.Z-]*)", value, gaps) {
        // XmlConvert also accepts legacy XSD date/time fragments, not just full OOXML timestamps.
        gaps.insert("datetime:non-timestamp".into());
        return true;
    }
    if !pattern(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})?", value, gaps) { return false; }
    let parsed =
        chrono::DateTime::parse_from_rfc3339(value).map(|t| t.naive_local()).or_else(|_| chrono::NaiveDateTime::parse_from_str(value, "%Y-%m-%dT%H:%M:%S%.f"));
    parsed.is_ok_and(|t| (1..=9999).contains(&t.year()) && t.nanosecond() < 1_000_000_000)
}

fn boolean(kind: &str, value: &str) -> Option<bool> {
    let value = match kind { "BooleanValue" => value.trim_matches([' ', '\t', '\r', '\n']), "OnOffValue" => value, _ => return None };
    match value {
        "true" | "1" => Some(true),
        "false" | "0" => Some(false),
        "on" if kind == "OnOffValue" => Some(true),
        "off" if kind == "OnOffValue" => Some(false),
        _ => None,
    }
}

fn lexical(kind: &str, value: &str, gaps: &mut BTreeSet<String>) -> bool {
    let number = value.trim_matches([' ', '\t', '\r', '\n']);
    match kind {
        "StringValue" => true,
        "BooleanValue" | "OnOffValue" => boolean(kind, value).is_some(),
        "TrueFalseValue" => ["true", "false", "t", "f"].contains(&value),
        "TrueFalseBlankValue" => ["true", "false", "t", "f", ""].contains(&value),
        "Int16Value" => number.parse::<i16>().is_ok(),
        "Int32Value" => number.parse::<i32>().is_ok(),
        "Int64Value" | "IntegerValue" => number.parse::<i64>().is_ok(),
        "UInt16Value" => number.parse::<u16>().is_ok(),
        "UInt32Value" => number.parse::<u32>().is_ok(),
        "UInt64Value" => number.parse::<u64>().is_ok(),
        "ByteValue" => number.parse::<u8>().is_ok(),
        "SByteValue" => number.parse::<i8>().is_ok(),
        "DoubleValue" | "SingleValue" => float(number, kind == "SingleValue").is_some(),
        "DecimalValue" => decimal(number, gaps),
        "DateTimeValue" => datetime(number, gaps),
        "Base64BinaryValue" => base64(value).is_some(),
        "HexBinaryValue" => value.len() % 2 == 0 && value.bytes().all(|c| c.is_ascii_hexdigit()),
        _ if kind.starts_with("ListValue<") => {
            let item_kind = &kind[10..kind.len() - 1];
            !value.trim().is_empty() && value.split_whitespace().all(|v| lexical(item_kind, v, gaps))
        }
        _ if kind.starts_with("EnumValue<") => {
            let name = kind.trim_start_matches("EnumValue<").trim_end_matches('>');
            let definition = schema_index().enums.get(name);
            match definition {
                Some(e) => {
                    if arr(&e["Facets"]).iter().any(|f| f.get("Version").is_some()) { gaps.insert(format!("enum-facet-version:{name}")); }
                    arr(&e["Facets"]).iter().any(|f| s(&f["Value"]) == value)
                }
                None => {
                    gaps.insert(format!("enum-source:{name}"));
                    true
                }
            }
        }
        _ => {
            gaps.insert(format!("lexical:{kind}"));
            true
        }
    }
}

fn validator(v: &Value, value: &str, kind: &str, gaps: &mut BTreeSet<String>) -> bool {
    let name = s(&v["Name"]);
    let override_type = s(&v["Type"]);
    let kind = if override_type.is_empty() { kind } else if let Some(kind) = schema()["simple_types"][override_type].as_str() { kind } else if name == "EnumValidator" {
        if let Some(e) = schema()["enums"].get(override_type) { return arr(&e["Facets"]).iter().any(|f| s(&f["Value"]) == value); }
        gaps.insert(format!("validator-type:{override_type}"));
        return true;
    } else {
        gaps.insert(format!("validator-type:{override_type}"));
        return true;
    };
    if v["IsList"].as_bool().unwrap_or(false) {
        if !lexical(&format!("ListValue<{kind}>"), value, gaps) { return false; }
        if !arr(&v["Arguments"]).is_empty() { gaps.insert(format!("list-facets:{name}")); }
        return true;
    }
    if !lexical(kind, value, gaps) { return false; }
    if !["NumberValidator", "StringValidator"].contains(&name) {
        if !["RequiredValidator", "OfficeVersionValidator"].contains(&name) { gaps.insert(format!("validator:{name}")); }
        return true;
    }
    // SDK NumberValidator.TryGetValue excludes DecimalValue; its lexical check still applies.
    if name == "NumberValidator" && kind == "DecimalValue" { return true; }
    let number = value.trim_matches([' ', '\t', '\r', '\n']);
    if name == "NumberValidator" && !matches!(kind, "DoubleValue" | "SingleValue") && number.parse::<i128>().is_err() { return false; }
    arr(&v["Arguments"]).iter().all(|a| {
        let key = s(&a["Name"]);
        let val = s(&a["Value"]);
        let bound = val.parse::<i128>().ok();
        let compare = |b: i128| {
            if matches!(kind, "DoubleValue" | "SingleValue") { float(number, kind == "SingleValue").and_then(|n| n.partial_cmp(&(b as f64))) } else { number.parse::<i128>().ok().map(|n| n.cmp(&b)) }
        };
        let length = if kind == "HexBinaryValue" { value.len() / 2 } else if kind == "Base64BinaryValue" { base64(value).map_or(0, |b| b.len()) } else { value.encode_utf16().count() } as i128;
        use std::cmp::Ordering::{Equal, Greater, Less};
        match (key, bound) {
            ("MinInclusive", Some(b)) => matches!(compare(b), Some(Equal | Greater)),
            ("MaxInclusive", Some(b)) => matches!(compare(b), Some(Equal | Less)),
            ("MinExclusive", Some(b)) => compare(b) == Some(Greater),
            ("MaxExclusive", Some(b)) => compare(b) == Some(Less),
            ("IsPositive", _) => val.eq_ignore_ascii_case("false") || compare(0) == Some(Greater),
            ("IsNonNegative", _) => val.eq_ignore_ascii_case("false") || matches!(compare(0), Some(Equal | Greater)),
            ("MinLength", Some(b)) => length >= b,
            ("MaxLength", Some(b)) => length <= b,
            ("Length", Some(b)) => length == b,
            ("Pattern", _) => pattern(val, value, gaps),
            ("IsId" | "IsNcName", _) => val.eq_ignore_ascii_case("false") || crate::xml::check_local(value).is_ok() && (key != "IsNcName" || length <= 255),
            ("IsQName", _) => {
                val.eq_ignore_ascii_case("false") || {
                    let names = value.split(':').collect::<Vec<_>>();
                    names.len() <= 2 && names.iter().all(|n| crate::xml::check_local(n).is_ok())
                }
            }
            ("IsToken", _) => {
                val.eq_ignore_ascii_case("false")
                    || !value.starts_with(' ') && !value.ends_with(' ') && !value.contains("  ") && !value.contains(['\t', '\n', '\r'])
            }
            _ => {
                gaps.insert(format!("facet:{name}.{key}"));
                true
            }
        }
    })
}

fn applicable(v: &Value, target: &str) -> bool {
    let version = s(&v["Version"]);
    version.is_empty() || if v["IsInitialVersion"].as_bool().unwrap_or(false) { available(version, target) } else { version == target }
}

fn check_value(kind: &str, validators: &[Value], value: &str, target: &str, gaps: &mut BTreeSet<String>) -> Vec<String> {
    let mut errors = Vec::new();
    if !lexical(kind, value, gaps) { errors.push("lexical".into()); }
    let mut unions: HashMap<i64, Vec<(bool, BTreeSet<String>)>> = HashMap::new();
    for v in validators.iter().filter(|v| applicable(v, target)) {
        let mut local = BTreeSet::new();
        let valid = validator(v, value, kind, &mut local);
        if let Some(id) = v["UnionId"].as_i64() { unions.entry(id).or_default().push((valid, local)); } else {
            gaps.extend(local);
            if !valid { errors.push(format!("facet:{}", s(&v["Name"]))); }
        }
    }
    for branches in unions.values() {
        if branches.iter().any(|(valid, gaps)| *valid && gaps.is_empty()) { continue; }
        if branches.iter().any(|(valid, _)| *valid) { for (_, local) in branches { gaps.extend(local.iter().cloned()); } } else { errors.push("union".into()); }
    }
    errors
}

fn check_attr(a: &Value, value: Option<&str>, target: &str, gaps: &mut BTreeSet<String>) -> Vec<String> {
    let mut validators = arr(&a["Validators"]).iter().filter(|v| applicable(v, target));
    let mut errors = Vec::new();
    if value.is_none() {
        if available(s(&a["Version"]), target)
            && validators.any(|v| {
                s(&v["Name"]) == "RequiredValidator"
                    && !arr(&v["Arguments"]).iter().any(|arg| s(&arg["Name"]) == "IsRequired" && s(&arg["Value"]).eq_ignore_ascii_case("false"))
            })
        { errors.push("required".into()); }
        return errors;
    }
    let value = value.unwrap();
    if !available(s(&a["Version"]), target) { errors.push("version".into()); }
    errors.extend(check_value(s(&a["Type"]), arr(&a["Validators"]), value, target, gaps));
    errors
}

fn typed_descriptor(type_id: &str, property: &str) -> Result<&'static Value> {
    let attrs = arr(&schema()["types"][type_id]["attributes"]);
    attrs.iter().find(|a| s(&a["PropertyName"]) == property).ok_or_else(|| Error::Invalid("unknown typed attribute".into()))
}

fn checked_attribute(a: &Value, property: &str, value: &str, writing: bool) -> Result<()> {
    let mut gaps = BTreeSet::new();
    let errors = check_attr(a, Some(value), "Microsoft365", &mut gaps);
    if !errors.is_empty() { return Err(Error::Invalid(format!("{property}: {value:?}: {}", errors.join(", ")))); }
    if writing && !gaps.is_empty() { return Err(Error::Unsupported(format!("Unchecked setter constraints: {}", gaps.into_iter().collect::<Vec<_>>().join(", ")))); }
    Ok(())
}

#[pyfunction]
pub fn typed_attribute(xml: &Xml, id: usize, type_id: &str, property: &str) -> Result<Option<String>> {
    let doc = xml.read()?;
    check_type(&doc, id, Some(type_id))?;
    let a = typed_descriptor(type_id, property)?;
    let Some(value) = attribute(doc.node(id)?, s(&a["QName"])) else { return Ok(None); };
    checked_attribute(a, property, value, false)?;
    Ok(Some(match boolean(s(&a["Type"]), value) { Some(true) => "true", Some(false) => "false", None => value }.into()))
}

#[pyfunction]
pub fn set_typed_attribute(xml: &Xml, id: usize, type_id: &str, property: &str, value: &str) -> Result<()> {
    let a = typed_descriptor(type_id, property)?;
    checked_attribute(a, property, value, true)?;
    let (uri, local) = expanded(s(&a["QName"]));
    xml.edit(|doc| {
        check_type(doc, id, Some(type_id))?;
        doc.set_attribute(id, uri, local, value, None)
    })
}

fn particle_occurs(p: &Value, target: &str) -> Option<(usize, usize)> {
    let version = |o: &Value| if o["IncludeVersion"].as_bool().unwrap_or(false) { rank(s(&o["Version"])) } else { rank(s(&p["InitialVersion"])) };
    let occurs = arr(&p["Occurs"]);
    if occurs.is_empty() { return available(s(&p["InitialVersion"]), target).then_some((1, 1)); }
    let target_rank = rank(target)?;
    occurs
        .iter()
        .filter(|o| version(o).is_some_and(|v| v <= target_rank))
        .max_by_key(|o| version(o))
        .map(|o| (o["Min"].as_u64().unwrap_or(0) as usize, o["Max"].as_u64().unwrap_or(0) as usize))
}

enum Term {
    Element(&'static str),
    Sequence(Vec<Particle>),
    Choice(Vec<Particle>),
    Any(&'static str),
    All(Vec<Particle>),
    Unsupported(&'static str),
}

struct Particle { occurs: [Option<(usize, usize)>; 7], require_filter: bool, term: Term }

fn positions(mut values: Vec<usize>) -> Vec<usize> {
    values.sort_unstable();
    values.dedup();
    values
}

impl Particle {
    fn compile(p: &'static Value) -> Self {
        let items = || arr(&p["Items"]).iter().map(Self::compile).collect();
        let term = if let Some(name) = p["Name"].as_str() { Term::Element(name) } else {
            match s(&p["Kind"]) {
                "Sequence" | "Group" => Term::Sequence(items()),
                "Choice" => Term::Choice(items()),
                "All" => Term::All(items()),
                "Any" => Term::Any(s(&p["Namespace"])),
                other => Term::Unsupported(other),
            }
        };
        Self { occurs: std::array::from_fn(|i| particle_occurs(p, VERSIONS[i])), require_filter: p["RequireFilter"].as_bool().unwrap_or(false), term }
    }

    // Same bounded matcher, over immutable compiled descriptors and contiguous position sets.
    fn matches(&self, children: &[(&str, &str)], start: usize, parent_uri: &str, target: usize, gaps: &mut BTreeSet<String>, budget: &mut usize) -> Vec<usize> {
        if *budget == 0 {
            gaps.insert("particle:budget".into());
            return Vec::new();
        }
        *budget -= 1;
        let Some((min, max)) = self.occurs[target] else { return vec![start]; };
        if self.require_filter { gaps.insert("particle:RequireFilter".into()); }
        let max = if max == 0 { children.len() + 1 } else { max.min(children.len() + min + 1) };
        let mut results = Vec::new();
        let mut current = vec![start];
        if min == 0 { results.push(start); }
        for count in 1..=max {
            let mut next = Vec::new();
            for &pos in &current {
                match &self.term {
                    Term::Element(name) => {
                        if children.get(pos).is_some_and(|(id, _)| id == name) { next.push(pos + 1); }
                    }
                    Term::Sequence(items) => {
                        let mut matched = vec![pos];
                        for item in items {
                            matched = positions(matched.iter().flat_map(|i| item.matches(children, *i, parent_uri, target, gaps, budget)).collect());
                        }
                        next.extend(matched);
                    }
                    Term::Choice(items) => {
                        for item in items { next.extend(item.matches(children, pos, parent_uri, target, gaps, budget)); }
                    }
                    Term::Any(namespace) => {
                        if let Some((_, uri)) = children.get(pos) {
                            let matches = match *namespace {
                                "" | "##any" => true,
                                "##local" => uri.is_empty(),
                                "##other" => uri.is_empty() || *uri != parent_uri,
                                "##targetNamespace" => *uri == parent_uri,
                                other => {
                                    gaps.insert(format!("particle:namespace:{other}"));
                                    false
                                }
                            };
                            if matches { next.push(pos + 1); }
                        }
                    }
                    Term::All(items) => {
                        let mut members = HashMap::new();
                        for item in items {
                            let Some((min, max)) = item.occurs[target] else { continue; };
                            if let Term::Element(name) = item.term {
                                if min <= 1 && max == 1 {
                                    members.insert(name, min);
                                    continue;
                                }
                            }
                            gaps.insert("particle:all-member".into());
                        }
                        let mut seen = HashSet::new();
                        if members.values().all(|min| *min == 0) { next.push(pos); }
                        for (offset, (id, _)) in children[pos..].iter().enumerate() {
                            if *budget == 0 {
                                gaps.insert("particle:budget".into());
                                break;
                            }
                            *budget -= 1;
                            if !members.contains_key(id) || !seen.insert(*id) { break; }
                            if members.iter().all(|(name, min)| *min == 0 || seen.contains(name)) { next.push(pos + offset + 1); }
                        }
                    }
                    Term::Unsupported(kind) => {
                        gaps.insert(format!("particle:{kind}"));
                    }
                }
            }
            let next = positions(next);
            if count >= min { results.extend(&next); }
            if next == current || next.is_empty() { break; }
            current = next;
        }
        positions(results)
    }
}

fn ignorable(doc: &Document, id: usize, uri: &str) -> bool {
    if uri.is_empty() { return false; }
    ancestors(doc, id)
        .filter_map(Node::element)
        .any(|a| a.attribute(MC, "Ignorable").is_some_and(|v| v.split_whitespace().any(|prefix| a.namespace(prefix) == Some(uri))))
}
fn namespace_available(uri: &str, target: &str) -> bool {
    uri == MC
        || uri == "http://www.w3.org/XML/1998/namespace"
        || schema_index().namespace_versions.get(uri).is_some_and(|version| Some(*version) <= rank(target))
}
fn process_content(doc: &Document, id: usize) -> bool {
    let name = &doc.node(id).unwrap().element().unwrap().name;
    ancestors(doc, id).filter_map(Node::element).any(|a| {
        a.attribute(MC, "ProcessContent").is_some_and(|v| {
            v.split_whitespace().any(|q| {
                q.split_once(':').is_some_and(|(prefix, local)| a.namespace(prefix) == Some(name.uri.as_str()) && (local == "*" || local == name.local))
            })
        })
    })
}

// SDK CompatibilityRuleAttributesValidator and AlternateContentValidator. Validation never rewrites the stored XML.
fn mc_validate(doc: &Document, id: usize, issues: &mut Vec<Value>) {
    let e = doc.node(id).unwrap().element().unwrap();
    let provenance = json!({"source":"SDK CompatibilityRuleAttributesValidator / AlternateContentValidator"});
    let defined = |prefix: &str| e.namespace(prefix).filter(|uri| !uri.is_empty());
    for name in ["Ignorable", "MustUnderstand"] {
        if let Some(value) = e.attribute(MC, name) {
            if value.split_whitespace().any(|prefix| defined(prefix).is_none()) {
                issues.push(issue(&format!("mc-{name}"), "markup-compatibility", id, "declared namespace prefixes", value, &provenance));
            }
        }
    }
    let local_ignorable: Vec<_> = e.attribute(MC, "Ignorable").unwrap_or("").split_whitespace().filter_map(defined).collect();
    for name in ["PreserveElements", "PreserveAttributes", "ProcessContent"] {
        if let Some(value) = e.attribute(MC, name).filter(|v| !v.is_empty()) {
            let valid = !local_ignorable.is_empty()
                && value.split_whitespace().all(|q| {
                    q.split_once(':').is_some_and(|(prefix, local)| !local.contains(':') && defined(prefix).is_some_and(|uri| local_ignorable.contains(&uri)))
                });
            if !valid {
                issues.push(issue(&format!("mc-{name}"), "markup-compatibility", id, "QNames in this element's Ignorable namespaces", value, &provenance));
            }
        }
    }
    let wrapper = e.name.uri == MC && ["AlternateContent", "Choice", "Fallback"].contains(&e.name.local.as_str());
    for a in &e.attributes {
        let xml_space = a.name.uri == "http://www.w3.org/XML/1998/namespace" && ["lang", "space"].contains(&a.name.local.as_str());
        if xml_space && (wrapper || e.attribute(MC, "ProcessContent").is_some_and(|v| !v.is_empty())) {
            issues.push(issue(
                "mc-xml-attribute",
                "markup-compatibility",
                id,
                "no xml:lang/xml:space on MC or ProcessContent host",
                a.name.local.as_str(),
                &provenance,
            ));
        }
        if wrapper && a.name.uri.is_empty() && !(e.name.local == "Choice" && a.name.local == "Requires") {
            issues.push(issue("mc-unprefixed-attribute", "markup-compatibility", id, "prefixed MC attributes", a.name.local.as_str(), &provenance));
        }
    }
    if e.name.uri != MC { return; }
    if e.name.local == "Choice" {
        if !e.attribute("", "Requires").is_some_and(|v| v.split_whitespace().all(|prefix| defined(prefix).is_some())) {
            issues.push(issue("mc-requires", "markup-compatibility", id, "Requires with declared prefixes", json!(e.attribute("", "Requires")), &provenance));
        }
    } else if e.name.local == "AlternateContent" {
        let branches = children(doc, id);
        let mut choices = 0;
        let mut fallback = false;
        let mut valid = !branches.is_empty();
        for branch in branches {
            let b = doc.node(branch).unwrap().element().unwrap();
            match (b.name.uri.as_str(), b.name.local.as_str()) {
                (MC, "Choice") => {
                    valid &= !fallback;
                    choices += 1;
                    mc_validate(doc, branch, issues);
                }
                (MC, "Fallback") => {
                    valid &= choices > 0 && !fallback;
                    fallback = true;
                    mc_validate(doc, branch, issues);
                }
                _ => valid = false,
            }
        }
        if !valid || choices == 0 {
            issues.push(issue(
                "mc-content",
                "markup-compatibility",
                id,
                "one or more Choices followed by at most one Fallback",
                "invalid branch sequence",
                &provenance,
            ));
        }
    }
}

fn effective(doc: &Document, id: usize, target: &str, skipped: &mut Vec<Value>, issues: &mut Vec<Value>) -> Vec<usize> {
    let mut result = Vec::new();
    for child in children(doc, id) {
        let n = doc.node(child).unwrap();
        let e = n.element().unwrap();
        if e.name.uri == MC && e.name.local == "AlternateContent" {
            mc_validate(doc, child, issues);
            let branches = children(doc, child);
            let selected = branches
                .iter()
                .copied()
                .find(|branch| {
                    let b = doc.node(*branch).unwrap().element().unwrap();
                    b.name.uri == MC
                        && b.name.local == "Choice"
                        && b.attribute("", "Requires")
                            .is_some_and(|v| v.split_whitespace().all(|prefix| b.namespace(prefix).is_some_and(|uri| namespace_available(uri, target))))
                })
                .or_else(|| {
                    branches.into_iter().find(|branch| {
                        let b = doc.node(*branch).unwrap().element().unwrap();
                        b.name.uri == MC && b.name.local == "Fallback"
                    })
                });
            skipped.push(json!({"reason":"unselected-mc-branches","node":child,
                "selected":selected.map(|branch| &doc.node(branch).unwrap().element().unwrap().name.local)}));
            if let Some(branch) = selected { result.extend(effective(doc, branch, target, skipped, issues)); }
        } else if ignorable(doc, child, &e.name.uri)
            && !schema_index().element_versions.get(&(e.name.uri.as_str(), e.name.local.as_str())).is_some_and(|version| Some(*version) <= rank(target))
        {
            if process_content(doc, child) { result.extend(effective(doc, child, target, skipped, issues)); } else { skipped.push(json!({"reason":"ignorable-unknown","node":child})); }
        } else { result.push(child); }
    }
    result
}

fn path_nodes(doc: &Document, path: &str, target: &str) -> Vec<usize> {
    let names: Vec<&str> = path.split('/').filter(|p| !p.is_empty()).collect();
    let mut result = Vec::new();
    let mut pending = vec![(doc.root, Vec::new())];
    while let Some((id, mut lineage)) = pending.pop() {
        lineage.push(id);
        if lineage.len() >= names.len() && lineage.iter().rev().zip(names.iter().rev()).all(|(id, name)| matches(doc.node(*id).unwrap(), name)) {
            result.push(id);
        }
        if lineage.len() >= names.len() { lineage.remove(0); }
        for child in effective(doc, id, target, &mut Vec::new(), &mut Vec::new()) { pending.push((child, lineage.clone())); }
    }
    result
}

struct SemanticContext<'a> {
    doc: &'a Document,
    dependencies: &'a HashMap<String, &'a Document>,
    relationships: Option<&'a HashMap<String, String>>,
    complete_dependencies: bool,
    target: &'a str,
    duplicates: HashMap<String, BTreeSet<String>>,
    references: HashMap<&'static str, HashSet<String>>,
}

fn semantic(rule: &'static Value, n: &Node, context: &mut SemanticContext<'_>, gaps: &mut BTreeSet<String>) -> Option<bool> {
    let test = s(&rule["Test"]);
    if !available(s(&rule["Version"]), context.target) { return Some(true); }
    if let Some(rest) = test.strip_prefix("count(distinct-values(") {
        let (query, repeated) = rest.split_once(")) = count(")?;
        if repeated.strip_suffix(')') != Some(query) || !query.chars().all(|c| c.is_ascii_alphanumeric() || ":/_-@.".contains(c)) { return None; }
        let (path, attr) = query.rsplit_once("/@")?;
        let names: Vec<_> = path.trim_start_matches('/').split('/').collect();
        // Contextual element types and ancestor-scoped constraints need their own SDK interpretation.
        if names.len() == 1 && resolve(n, None).is_none() || names.len() > 1 && !matches(context.doc.node(context.doc.root).unwrap(), names[0]) { return None; }
        let Some(value) = attribute(n, attr).filter(|v| !v.is_empty()) else { return Some(true); };
        let duplicates = context.duplicates.entry(test.to_owned()).or_insert_with(|| {
            let mut seen = BTreeSet::new();
            let mut duplicates = BTreeSet::new();
            for id in path_nodes(context.doc, path, context.target) {
                if let Some(value) = attribute(context.doc.node(id).unwrap(), attr).filter(|v| !v.is_empty()) {
                    if !seen.insert(value.to_owned()) { duplicates.insert(value.to_owned()); }
                }
            }
            duplicates
        });
        return Some(!duplicates.remove(value)); // Report once per repeated value, as the SDK does.
    }
    if let Some(rest) = test.strip_prefix("document(rels)//r:Relationship[@Id = current()/@") {
        let (attr, expected) = rest.split_once("]/@Type = '")?;
        let expected = expected.strip_suffix('\'')?;
        let Some(value) = attribute(n, attr).filter(|v| !v.is_empty()) else { return Some(true); };
        let Some(relationships) = context.relationships else {
            gaps.insert("relationships".into());
            return None;
        };
        return Some(relationships.get(value).is_some_and(|actual| actual == expected));
    }
    // Recognize the imported cross-part lookup grammar, not individual Word reference IDs.
    let rest = test.strip_prefix("Index-of(document('Part:")?;
    let Some((part, rest)) = rest.split_once("')//") else { return None; };
    let Some((path, rest)) = rest.split_once("/@") else { return None; };
    let Some((dest, source)) = rest.trim_end_matches(')').split_once(", @") else { return None; };
    let Some(value) = attribute(n, source).filter(|v| !v.is_empty()) else { return Some(true); };
    let Some(doc) = context.dependencies.get(part) else {
        if context.complete_dependencies { return Some(false); }
        gaps.insert(format!("dependency:{part}"));
        return None;
    };
    let values = context.references.entry(test).or_insert_with(|| {
        path_nodes(doc, path, context.target)
            .into_iter()
            .filter_map(|id| attribute(doc.node(id).unwrap(), dest).filter(|v| !v.is_empty()).map(str::to_owned))
            .collect()
    });
    Some(values.contains(value))
}

pub fn analyze_document(
    doc: &Document,
    target: &str,
    dependencies: &HashMap<String, &Document>,
    relationships: Option<&HashMap<String, String>>,
    complete_dependencies: bool,
) -> Result<Value> {
    check_target(target)?;
    let mut issues = Vec::new();
    let mut gaps = BTreeSet::new();
    let mut skipped = Vec::new();
    let mut semantic_context = SemanticContext {
        doc,
        dependencies,
        relationships,
        complete_dependencies,
        target,
        duplicates: HashMap::new(),
        references: HashMap::new(),
    };
    let mut active = vec![(doc.root, resolve(doc.node(doc.root)?, None))];
    let mut checked = 0;
    let mut semantic_checked = 0;
    while let Some((index, id)) = active.pop() {
        let n = doc.node(index)?;
        let e = n.element().unwrap();
        mc_validate(doc, index, &mut issues);
        let children = effective(doc, index, target, &mut skipped, &mut issues);
        let child_types: Vec<_> = children.iter().map(|c| resolve(doc.node(*c).unwrap(), id.as_deref())).collect();
        active.extend(children.iter().copied().zip(child_types.iter().cloned()));
        let Some(id) = id else {
            gaps.insert(format!("untyped:{{{}}}{}", e.name.uri, e.name.local));
            continue;
        };
        let t = &schema()["types"][&id];
        checked += 1;
        if !t["unresolved_base"].is_null() { gaps.insert(format!("source-base:{id}:{}", s(&t["unresolved_base"]))); }
        if !available(s(&t["version"]), target) { issues.push(issue("element-version", "schema", index, s(&t["version"]), target, &t["source"])); }
        for a in arr(&t["attributes"]) {
            if !available(s(&a["Version"]), target) && ignorable(doc, index, expanded(s(&a["QName"])).0) { continue; }
            let value = attribute(n, s(&a["QName"]));
            for error in check_attr(a, value, target, &mut gaps) {
                issues.push(issue(&format!("attribute-{error}"), "schema", index, a.clone(), json!(value), &t["source"]));
            }
        }
        for a in &e.attributes {
            if a.name.uri == MC {
                if !["Ignorable", "MustUnderstand", "PreserveElements", "PreserveAttributes", "ProcessContent"].contains(&a.name.local.as_str()) {
                    gaps.insert(format!("mc:{}", a.name.local));
                }
                continue;
            }
            if a.name.uri == "http://www.w3.org/XML/1998/namespace" || ignorable(doc, index, &a.name.uri) { continue; }
            if !arr(&t["attributes"]).iter().any(|v| expanded(s(&v["QName"])) == (a.name.uri.as_str(), a.name.local.as_str())) {
                if !t["unresolved_base"].is_null() { gaps.insert(format!("undeclared-attribute:{id}:{{{}}}{}", a.name.uri, a.name.local)); } else { issues.push(issue("undeclared-attribute", "schema", index, "declared attribute", json!([a.name.uri, a.name.local]), &t["source"])); }
            }
        }
        if let Some(p) = schema_index().particles.get(id) {
            let mut local = BTreeSet::new();
            let mut budget = 100_000;
            let matching_children: Vec<_> = children
                .iter()
                .zip(&child_types)
                .map(|(c, t)| (t.as_deref().unwrap_or(""), doc.node(*c).unwrap().element().unwrap().name.uri.as_str()))
                .collect();
            let accepted = p.matches(&matching_children, 0, &e.name.uri, rank(target).unwrap() as usize, &mut local, &mut budget).contains(&children.len());
            if !accepted && local.is_empty() { issues.push(issue("child-particle", "schema", index, t["particle"].clone(), json!(child_types), &t["source"])); }
            gaps.extend(local);
        }
        else if !children.is_empty() {
            if t["is_leaf"].as_bool().unwrap_or(false) && t["unresolved_base"].is_null() {
                issues.push(issue("leaf-child", "schema", index, "no element children", json!(children), &t["source"]));
            } else { gaps.insert(format!("missing-content-model:{id}")); }
        }
        let text: String = n
            .children
            .iter()
            .filter_map(|id| match &doc.node(*id).unwrap().kind { NodeKind::Text(t) => Some(t.as_str()), _ => None })
            .collect();
        for error in check_value("StringValue", arr(&t["validators"]), &text, target, &mut gaps) {
            issues.push(issue(&format!("element-{error}"), "schema", index, t["validators"].clone(), text.as_str(), &t["source"]));
        }
        if !t["is_text"].as_bool().unwrap_or(false) && !text.trim().is_empty() {
            issues.push(issue("element-only-content", "schema", index, "no non-whitespace character data", text, &t["source"]));
        }
        for &(i, rule) in schema_index().semantics.get(&(e.name.uri.as_str(), e.name.local.as_str())).into_iter().flatten() {
            match semantic(rule, n, &mut semantic_context, &mut gaps) {
                Some(valid) => {
                    semantic_checked += 1;
                    if !valid { issues.push(issue(&format!("sdk-schematron-{i}"), "semantic", index, s(&rule["Test"]), "constraint not satisfied", rule)); }
                }
                None => {
                    gaps.insert(format!("semantic:sdk-schematron-{i}"));
                }
            }
        }
    }
    Ok(json!({"issues":issues,"target":target,"source":schema()["source"],"coverage":{"schema_nodes_checked":checked,
        "semantic_checks":semantic_checked,"gaps":gaps,"skipped_regions":skipped,"complete":false}}))
}

pub fn analyze_tree(
    xml: &Xml, target: &str, dependencies: &HashMap<String, Xml>, relationships: Option<&HashMap<String, String>>, complete_dependencies: bool,
) -> Result<Value> {
    let doc = xml.read()?;
    let guards = dependencies.iter().map(|(name, xml)| Ok((name, xml.read()?))).collect::<Result<Vec<_>>>()?;
    let deps = guards.iter().map(|(name, doc)| ((*name).clone(), &**doc)).collect();
    analyze_document(&doc, target, &deps, relationships, complete_dependencies)
}

#[pyfunction]
#[pyo3(signature=(xml, target="Microsoft365", dependencies=None, relationships=None, complete_dependencies=false))]
pub fn analyze(
    py: Python<'_>, xml: &Xml, target: &str, dependencies: Option<HashMap<String, PyRef<'_, Xml>>>,
    relationships: Option<HashMap<String, String>>, complete_dependencies: bool,
) -> Result<String> {
    let deps = dependencies.unwrap_or_default().into_iter().map(|(name, xml)| (name, (*xml).clone())).collect();
    py.detach(|| analyze_tree(xml, target, &deps, relationships.as_ref(), complete_dependencies).map(|report| report.to_string()))
}
