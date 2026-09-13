use pyo3::prelude::*;
mod package;
mod schema;
mod xml;

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<xml::Xml>()?;
    m.add_class::<package::Package>()?;
    m.add_function(wrap_pyfunction!(schema::metadata_json, m)?)?;
    m.add_function(wrap_pyfunction!(schema::analyze, m)?)?;
    m.add_function(wrap_pyfunction!(schema::check_attribute, m)?)?;
    m.add_function(wrap_pyfunction!(schema::element_type, m)?)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
