//! Inline picture markup, image sniffing and the owning part's image relationship.
use crate::{error::{Error, Result}, package::PackageData, package_schema::story_nodes, xml::{self, Xml}};
use std::collections::HashSet;

const WP: &str = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing";
const DRAWING: &[u8] = br#"<w:drawing xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<wp:inline distT="0" distB="0" distL="0" distR="0">
<wp:extent/><wp:effectExtent l="0" t="0" r="0" b="0"/>
<wp:docPr/><wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>
<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
<pic:pic><pic:nvPicPr><pic:cNvPr/><pic:cNvPicPr/></pic:nvPicPr>
<pic:blipFill><a:blip/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>
<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext/></a:xfrm>
<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>
</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing>"#;

/// Pixel size, resolution and content type read from an image header.
pub struct Sniffed { pub width: u32, pub height: u32, pub dpi_x: u32, pub dpi_y: u32, pub content_type: &'static str }

fn be32(d: &[u8], i: usize) -> Option<u32> { d.get(i..i + 4).map(|b| u32::from_be_bytes([b[0], b[1], b[2], b[3]])) }
fn be16(d: &[u8], i: usize) -> Option<u32> { d.get(i..i + 2).map(|b| u16::from_be_bytes([b[0], b[1]]) as u32) }
fn le16(d: &[u8], i: usize) -> Option<u32> { d.get(i..i + 2).map(|b| u16::from_le_bytes([b[0], b[1]]) as u32) }
fn dpi(value: u32) -> u32 { if value == 0 { 96 } else { value } }

/// PNG, JPEG or GIF header facts; 96 dpi stands in when the file states no resolution.
pub fn sniff(data: &[u8]) -> Option<Sniffed> {
    if data.starts_with(b"\x89PNG\r\n\x1a\n") {
        let (width, height, mut dpi_x, mut dpi_y) = (be32(data, 16)?, be32(data, 20)?, 96, 96);
        let mut i = 8;
        while let (Some(len), Some(kind)) = (be32(data, i), data.get(i + 4..i + 8)) {
            if kind == b"pHYs" {
                if data.get(i + 16) == Some(&1) {
                    dpi_x = dpi((be32(data, i + 8)? as f64 * 0.0254).round() as u32);
                    dpi_y = dpi((be32(data, i + 12)? as f64 * 0.0254).round() as u32);
                }
                break;
            }
            i += len as usize + 12;
        }
        return Some(Sniffed { width, height, dpi_x, dpi_y, content_type: "image/png" });
    }
    if data.starts_with(b"\xff\xd8") {
        let (mut dpi_x, mut dpi_y, mut i) = (96, 96, 2);
        while data.get(i) == Some(&0xFF) {
            let (marker, len) = (*data.get(i + 1)?, be16(data, i + 2)? as usize);
            if marker == 0xE0 && data.get(i + 4..i + 9) == Some(b"JFIF\0") {
                let (units, xd, yd) = (*data.get(i + 11)?, be16(data, i + 12)?, be16(data, i + 14)?);
                if units == 1 { (dpi_x, dpi_y) = (dpi(xd), dpi(yd)); }
                if units == 2 { (dpi_x, dpi_y) = (dpi((xd as f64 * 2.54).round() as u32), dpi((yd as f64 * 2.54).round() as u32)); }
            } else if matches!(marker, 0xC0..=0xC3 | 0xC5..=0xC7 | 0xC9..=0xCB | 0xCD..=0xCF) {
                return Some(Sniffed { width: be16(data, i + 7)?, height: be16(data, i + 5)?, dpi_x, dpi_y, content_type: "image/jpeg" });
            }
            i += 2 + len;
        }
        return None;
    }
    if data.starts_with(b"GIF87a") || data.starts_with(b"GIF89a") {
        return Some(Sniffed { width: le16(data, 6)?, height: le16(data, 8)?, dpi_x: 96, dpi_y: 96, content_type: "image/gif" });
    }
    None
}

fn emu(pixels: u32, dpi: u32) -> i64 { (pixels as i64 * 914400 + dpi as i64 / 2) / dpi as i64 }

/// The next unused `wp:docPr` id: one above every id in the package's stories, then counting up for the package's lifetime.
fn next_drawing_id(package: &mut PackageData) -> Result<u32> {
    if package.next_drawing_id == 0 {
        let mut max = 0;
        let uris: HashSet<String> = story_nodes(package)?.into_iter().map(|(uri, _)| uri).collect();
        for uri in uris {
            let xml = package.load_xml(&uri)?;
            let doc = xml.read()?;
            for id in doc.element_ids() {
                let element = doc.node(id)?.element().unwrap();
                if element.name.uri == WP && element.name.local == "docPr" {
                    max = max.max(element.attribute("", "id").and_then(|v| v.parse().ok()).unwrap_or(0));
                }
            }
        }
        package.next_drawing_id = max + 1;
    }
    let id = package.next_drawing_id;
    package.next_drawing_id += 1;
    Ok(id)
}

/// Add image bytes and return detached drawing XML. EMU sizes default to the image's own size at its resolution, and one given size keeps the aspect ratio.
pub fn add(package: &mut PackageData, source: &str, data: &[u8], content_type: Option<&str>, width: Option<i64>, height: Option<i64>, description: &str) -> Result<Xml> {
    let sniffed = sniff(data);
    let Some(content_type) = content_type.or(sniffed.as_ref().map(|s| s.content_type)) else {
        return Err(Error::Invalid("Unrecognised image format; supply content_type, width and height".into()));
    };
    let natural = sniffed.as_ref().filter(|s| s.width > 0 && s.height > 0).map(|s| (emu(s.width, s.dpi_x), emu(s.height, s.dpi_y)));
    let (width, height) = match (width, height, natural) {
        (Some(width), Some(height), _) => (width, height),
        (Some(width), None, Some((nw, nh))) => (width, width * nh / nw),
        (None, Some(height), Some((nw, nh))) => (height * nw / nh, height),
        (None, None, Some(size)) => size,
        _ => return Err(Error::Invalid("Image extents are required when the format is not recognised".into())),
    };
    if width <= 0 || height <= 0 { return Err(Error::Invalid("Image extents must be positive EMUs".into())); }
    let drawing_id = next_drawing_id(package)?;
    let extension = match content_type {
        "image/png" => "png", "image/jpeg" => "jpeg", "image/gif" => "gif", "image/tiff" => "tiff",
        "image/svg+xml" => "svg", "image/bmp" => "bmp", _ => "bin",
    };
    let mut doc = xml::parse_bytes(DRAWING)?;
    let mut blip = doc.root;
    for id in doc.element_ids() {
        match doc.node(id)?.element().unwrap().name.local.as_str() {
            "extent" | "ext" => {
                doc.set_attribute(id, "", "cx", &width.to_string(), None)?;
                doc.set_attribute(id, "", "cy", &height.to_string(), None)?;
            }
            "docPr" | "cNvPr" => {
                doc.set_attribute(id, "", "id", &drawing_id.to_string(), None)?;
                doc.set_attribute(id, "", "name", &format!("Picture {drawing_id}"), None)?;
                doc.set_attribute(id, "", "descr", description, None)?;
            }
            "blip" => blip = id,
            _ => (),
        }
    }
    let directory = package.main_part().rsplit_once('/').map_or("", |(dir, _)| dir);
    let names: HashSet<_> = package.part_names().into_iter().map(|name| name.to_lowercase()).collect();
    let mut index = 1;
    let mut uri = format!("{directory}/media/image{index}.{extension}");
    while names.contains(&uri.to_lowercase()) { index += 1; uri = format!("{directory}/media/image{index}.{extension}"); }
    package.add_part(&uri, content_type, data)?;
    let relationship = package.add_relationship(source, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image", &uri, "Internal", None)?;
    doc.set_attribute_ns(blip, "http://schemas.openxmlformats.org/officeDocument/2006/relationships", "embed", &relationship, "r")?;
    Ok(Xml::from_document(doc))
}
