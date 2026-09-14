use oxml::{definitions, error::Result, footnotes::Footnotes, package::Package, properties::{Properties, Settings, SettingValue},
    revisions, text::{self, Story, View}};

#[test]
fn native_edit_review_save_and_invalidate_share_one_document() -> Result<()> {
    let package = Package::new()?;
    let main = package.main_part()?;
    let part = package.part(&main)?;
    let xml = part.xml()?;
    let other_handle = package.xml(&main)?;
    assert!(xml.same_state(&other_handle));
    let body = { let doc = xml.read()?; text::child(&doc, doc.root, "body").unwrap() };
    xml.edit(|doc| {
        let paragraph = definitions::append(doc, body, "p")?;
        let run = definitions::append(doc, paragraph, "r")?;
        let text = definitions::append(doc, run, "t")?;
        doc.set_text(text, "old clause")
    })?;
    let story = Story::new_native(xml.clone(), body, View::Current)?;
    let selection = story.range_native(0, 3)?;
    xml.edit(|doc| {
        let mut metadata = revisions::Metadata::new(doc, "Jeremy", Some("2026-09-13T12:00:00Z"))?;
        revisions::tracked_replace(doc, body, body, selection.start, selection.end, "new", &mut metadata)
    })?;
    assert_eq!(Story::new_native(other_handle.clone(), body, View::Current)?.text_native()?, "new clause");
    assert_eq!(Story::new_native(other_handle, body, View::Original)?.text_native()?, "old clause");
    assert_eq!(xml.edit(|doc| revisions::apply_all(doc, body, true))?, 2);
    let report = oxml::schema::analyze_tree(&xml, "Microsoft365", &Default::default(), None, false)?;
    assert_eq!(report["issues"].as_array().unwrap().len(), 0);
    assert_eq!(report["coverage"]["xsd_roots_checked"], 1);

    let note = Footnotes::new(package.clone()).create(vec![])?;
    let paragraph = { let doc = xml.read()?; text::child(&doc, body, "p").unwrap() };
    xml.attach_document(paragraph, &*note.reference()?.read()?, None)?;
    definitions::style_get(&package, "FootnoteText")?;
    definitions::style_get(&package, "FootnoteReference")?;
    Properties::new(package.clone()).set("title", "Agreement")?;
    Settings::new(package.clone()).set("defaultTabStop", SettingValue::Text("1".into()))?;

    let bytes = package.to_bytes()?;
    let reopened = Package::from_bytes(&bytes)?;
    assert_eq!(Properties::new(reopened.clone()).get("title")?, "Agreement");
    assert_eq!(Settings::new(reopened.clone()).get("defaultTabStop")?, SettingValue::Text("1".into()));
    let reopened_xml = reopened.xml(&main)?;
    let reopened_body = { let doc = reopened_xml.read()?; text::child(&doc, doc.root, "body").unwrap() };
    assert_eq!(Story::new_native(reopened_xml, reopened_body, View::Current)?.text_native()?, "new clause");
    package.replace_part(&main, &xml.to_bytes()?)?;
    assert!(part.xml().is_err());
    assert!(xml.read().is_err());
    assert_eq!(package.xml(&main)?.to_bytes()?, reopened.xml(&main)?.to_bytes()?);
    Ok(())
}
