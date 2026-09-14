use std::{env, fs, path::PathBuf};

fn main() {
    println!("cargo:rerun-if-changed=schema/xsd");
    let directory = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap()).join("schema/xsd");
    let mut paths: Vec<_> = fs::read_dir(directory).unwrap().map(|e| e.unwrap().path()).filter(|p| p.extension().is_some_and(|e| e == "xsd")).collect();
    paths.sort();
    let mut source = String::from("const ASSETS: &[(&str, &[u8])] = &[\n");
    for path in paths { source.push_str(&format!("({:?}, include_bytes!({:?})),\n", path.file_name().unwrap().to_str().unwrap(), path)); }
    source.push_str("];\n");
    fs::write(PathBuf::from(env::var_os("OUT_DIR").unwrap()).join("xsd_assets.rs"), source).unwrap();
}
