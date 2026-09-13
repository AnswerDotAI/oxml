use std::{fmt, io};

#[derive(Debug)]
pub enum Error {
    Invalid(String),
    Unsupported(String),
    Missing(String),
    Stale(String),
    Index(String),
    Io(io::Error),
}
pub type Result<T> = std::result::Result<T, Error>;

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Invalid(s) | Self::Unsupported(s) | Self::Missing(s) | Self::Stale(s) | Self::Index(s) => f.write_str(s),
            Self::Io(e) => e.fmt(f),
        }
    }
}
impl std::error::Error for Error {}
impl From<io::Error> for Error { fn from(error: io::Error) -> Self { Self::Io(error) } }

impl From<Error> for pyo3::PyErr {
    fn from(error: Error) -> Self {
        use pyo3::exceptions::{PyIndexError, PyKeyError, PyNotImplementedError, PyReferenceError, PyValueError};
        match error {
            Error::Invalid(s) => PyValueError::new_err(s),
            Error::Unsupported(s) => PyNotImplementedError::new_err(s),
            Error::Missing(s) => PyKeyError::new_err(s),
            Error::Stale(s) => PyReferenceError::new_err(s),
            Error::Index(s) => PyIndexError::new_err(s),
            Error::Io(e) => e.into(),
        }
    }
}
