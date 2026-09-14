//! Structural edits of rectangular tables. Formatting snapshots copy only the affected cells.
use crate::{definitions::{self, children}, error::{Error, Result}, schema, text::{self, name, W, W14}, xml::{Document, Xml}};
use pyo3::prelude::*;

fn invalid(message: &str) -> Error { Error::Invalid(message.into()) }
fn unsupported(message: &str) -> Error { Error::Unsupported(message.into()) }
fn indexed(items: &[usize], index: isize) -> Result<usize> {
    let position = if index < 0 { items.len() as isize + index } else { index };
    items.get(position as usize).copied().ok_or_else(|| Error::Index(index.to_string()))
}
fn cell(doc: &Document, template: Option<usize>, value: &str) -> Result<Document> {
    let mut result = Document::from_element(text::word_element("tc"));
    let paragraph = template.and_then(|id| text::child(doc, id, "p"));
    let run = paragraph.and_then(|id| text::child(doc, id, "r"));
    if let Some(template) = template {
        for id in children(doc, template, "tcPr")? { result.import(doc, id, Some(result.root))?; }
    }
    for line in value.split('\n') {
        let root = result.root;
        let p = definitions::append(&mut result, root, "p")?;
        if let Some(paragraph) = paragraph { for id in children(doc, paragraph, "pPr")? { result.import(doc, id, Some(p))?; } }
        let source = text::run_text(doc, run, line)?;
        let position = result.node(p)?.children.len();
        text::attach(&mut result, p, position, &source)?;
    }
    Ok(result)
}
fn grid(doc: &Document, table: usize) -> Result<(usize, Vec<usize>)> {
    doc.node(table)?;
    let grids: Vec<_> = children(doc, table, "tblGrid")?.collect();
    let rows: Vec<_> = children(doc, table, "tr")?.collect();
    if grids.len() != 1 || rows.is_empty() { return Err(unsupported("Table needs one grid and at least one row")); }
    let columns: Vec<_> = doc.element_children(grids[0])?.collect();
    if columns.is_empty() || columns.iter().any(|&id| name(doc, id) != Some("gridCol")) { return Err(unsupported("Unsupported table grid")); }
    if doc.element_children(table)?.any(|id| !matches!(name(doc, id), Some("tblPr" | "tblGrid" | "tr"))) {
        return Err(unsupported("Table contains unsupported structural children"));
    }
    for &row in &rows {
        let cells: Vec<_> = children(doc, row, "tc")?.collect();
        if cells.len() != columns.len() || doc.element_children(row)?.any(|id| !matches!(name(doc, id), Some("trPr" | "tc"))) {
            return Err(unsupported("Only rectangular tables are supported"));
        }
        let mut properties: Vec<_> = children(doc, row, "trPr")?.collect();
        for cell in cells { properties.extend(children(doc, cell, "tcPr")?); }
        for property in properties {
            if doc.element_children(property)?.any(|id| matches!(name(doc, id), Some("gridSpan" | "vMerge" | "hMerge" | "gridBefore" | "gridAfter"))) {
                return Err(unsupported("Merged or offset cells require explicit grid editing"));
            }
        }
    }
    if doc.descendants(table)?.any(|id| text::revision_name(doc, id).is_some()) {
        return Err(unsupported("Revised tables require explicit acceptance/rejection first"));
    }
    Ok((grids[0], rows))
}
fn check_removal(doc: &Document, ids: &[usize]) -> Result<()> {
    for &id in ids {
        if doc.descendants(id)?.any(|id|
            matches!(name(doc, id), Some("bookmarkStart" | "bookmarkEnd" | "commentRangeStart" | "commentRangeEnd" | "commentReference"))) {
            return Err(unsupported("Remove or relocate bookmarks/comments before deleting their table cells"));
        }
    }
    Ok(())
}
#[pyclass(module = "oxml._core")]
pub struct Table { #[pyo3(get)] pub xml: Xml, #[pyo3(get)] pub node_id: usize }
#[pymethods]
impl Table {
    #[new]
    pub fn new(xml: &Xml, node_id: usize) -> Result<Self> {
        if name(&*xml.read()?, node_id) != Some("tbl") { return Err(invalid("Table requires a live w:tbl Element")); }
        Ok(Self { xml: xml.clone(), node_id })
    }
    #[staticmethod]
    #[pyo3(signature=(xml, parent, values, widths, index=None))]
    pub fn add(xml: &Xml, parent: usize, values: Vec<Vec<String>>, widths: Vec<i64>, index: Option<usize>) -> Result<Self> {
        if widths.is_empty() || widths.iter().any(|&w| w <= 0) { return Err(invalid("Widths require positive integer twips")); }
        if values.is_empty() || values.iter().any(|row| row.len() != widths.len()) { return Err(invalid("Rows must match the nonempty column grid")); }
        let mut source = Document::from_element(text::word_element("tbl"));
        let root = source.root;
        definitions::append(&mut source, root, "tblPr")?;
        let grid = definitions::append(&mut source, root, "tblGrid")?;
        for width in widths {
            let column = definitions::append(&mut source, grid, "gridCol")?;
            definitions::set_word_attribute(&mut source, column, "w", &width.to_string())?;
        }
        for values in values {
            let row = definitions::append(&mut source, root, "tr")?;
            for value in values {
                let new_cell = cell(&source, None, &value)?;
                let index = source.node(row)?.children.len();
                text::attach(&mut source, row, index, &new_cell)?;
            }
        }
        let node_id = xml.edit(|doc| {
            if !matches!(name(doc, parent), Some("body" | "hdr" | "ftr" | "footnote" | "endnote" | "comment" | "tc")) {
                return Err(invalid("Expected a block-content container"));
            }
            let position = match index {
                Some(i) => i,
                None if name(doc, parent) == Some("tc") && doc.element_children(parent)?.next_back().is_some_and(|id| name(doc, id) == Some("p")) => {
                    doc.position(doc.element_children(parent)?.next_back().unwrap())?.1
                }
                None => schema::insertion_position(doc, parent, W, "tbl")?,
            };
            text::attach(doc, parent, position, &source)
        })?;
        Ok(Self { xml: xml.clone(), node_id })
    }
    #[getter]
    pub fn rows(&self) -> Result<Vec<usize>> {
        let doc = self.xml.read()?;
        let rows = children(&doc, self.node_id, "tr")?;
        Ok(rows.collect())
    }
    pub fn cells(&self, row: isize) -> Result<Vec<usize>> {
        let doc = self.xml.read()?;
        let row = indexed(&children(&doc, self.node_id, "tr")?.collect::<Vec<_>>(), row)?;
        let cells = children(&doc, row, "tc")?;
        Ok(cells.collect())
    }
    #[pyo3(signature=(index, values=None))]
    pub fn insert_row(&self, index: usize, values: Option<Vec<String>>) -> Result<usize> {
        self.xml.edit(|doc| {
            let (_, rows) = grid(doc, self.node_id)?;
            if index > rows.len() { return Err(Error::Index(index.to_string())); }
            let template = rows[index.min(rows.len() - 1)];
            let cells: Vec<_> = children(doc, template, "tc")?.collect();
            let values = values.unwrap_or_else(|| vec![String::new(); cells.len()]);
            if values.len() != cells.len() { return Err(invalid("Row values must match the column grid")); }
            let mut source = text::shell(doc, Some(template), &children(doc, template, "trPr")?.collect::<Vec<_>>())?;
            for (template, value) in cells.into_iter().zip(values) {
                let new_cell = cell(doc, Some(template), &value)?;
                let (root, index) = (source.root, source.node(source.root)?.children.len());
                text::attach(&mut source, root, index, &new_cell)?;
            }
            for local in ["paraId", "textId"] { source.remove_attribute(source.root, W14, local)?; }
            let position = doc.position(template)?.1 + usize::from(index == rows.len());
            text::attach(doc, self.node_id, position, &source)
        })
    }
    pub fn delete_row(&self, index: isize) -> Result<()> {
        self.xml.edit(|doc| {
            let (_, rows) = grid(doc, self.node_id)?;
            if rows.len() == 1 { return Err(invalid("Delete the table element to remove its last row")); }
            let row = indexed(&rows, index)?;
            check_removal(doc, &[row])?;
            doc.remove(row)
        })
    }
    pub fn insert_column(&self, index: usize) -> Result<Vec<usize>> {
        self.xml.edit(|doc| {
            let (grid, rows) = grid(doc, self.node_id)?;
            let columns: Vec<_> = doc.element_children(grid)?.collect();
            if index > columns.len() { return Err(Error::Index(index.to_string())); }
            let adjacent = index.min(columns.len() - 1);
            let after = usize::from(index == columns.len());
            let cells = rows.iter().map(|&row| Ok(children(doc, row, "tc")?.nth(adjacent).unwrap())).collect::<Result<Vec<_>>>()?;
            let sources = cells.iter().map(|&id| cell(doc, Some(id), "")).collect::<Result<Vec<_>>>()?;
            let position = doc.position(columns[adjacent])?.1 + after;
            doc.copy(columns[adjacent], grid, position)?;
            rows.into_iter().zip(cells).zip(sources).map(|((row, cell), source)| text::attach(doc, row, doc.position(cell)?.1 + after, &source)).collect()
        })
    }
    pub fn delete_column(&self, index: isize) -> Result<()> {
        self.xml.edit(|doc| {
            let (grid, rows) = grid(doc, self.node_id)?;
            let columns: Vec<_> = doc.element_children(grid)?.collect();
            if columns.len() == 1 { return Err(invalid("Delete the table element to remove its last column")); }
            let column = indexed(&columns, index)?;
            let cells = rows.iter().map(|&row| indexed(&children(doc, row, "tc")?.collect::<Vec<_>>(), index)).collect::<Result<Vec<_>>>()?;
            check_removal(doc, &cells)?;
            for cell in cells { doc.remove(cell)?; }
            doc.remove(column)
        })
    }
}
