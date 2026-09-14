# Schema provenance

The `*.xsd` files except `xml.xsd` are the wordprocessing import closure (12 of 26 files) of the ECMA-376 Part 4 5th edition transitional schema set, from `OfficeOpenXML-XMLSchema-Transitional.zip` inside https://ecma-international.org/wp-content/uploads/ECMA-376-4_5th_edition_december_2016.zip (sml/pml and their closures omitted: spreadsheets and presentations are out of scope).

`xml.xsd` is the W3C schema for the XML namespace, from https://www.w3.org/2001/xml.xsd.

Local modification, needed because lxml cannot resolve a namespace-only import: in `wml.xsd` and `shared-math.xsd`, the `<xsd:import namespace="http://www.w3.org/XML/1998/namespace"/>` element gained `schemaLocation="xml.xsd"`. No other edits.

These files were originally curated in Answer.AI's mdhtml2docx and moved to oxml. December 2016 is the current edition of the Transitional document schemas, not an Office support cutoff.

## Microsoft Word extensions

`w14.xsd` through `w16cei.xsd` are the ten schemas in [MS-DOCX Appendix A](https://learn.microsoft.com/en-us/openspecs/office_standards/ms-docx/d0a2e301-0ff7-4e9e-9bb7-ff47070dce0a), retrieved 2026-09-14. They include the 2026 comment-entity extension. The exact source page for each file is recorded in `scripts/import_xsd.py`, which refreshes them. Microsoft publishes these schemas for implementation under the Open Specifications documentation permissions.

The importer makes two mechanical adaptations, without adding or changing constraints:

* Microsoft import filenames (`word12.xsd`, `oartbasetypes.xsd`, `oartsplineproperties.xsd`, `orel.xsd`, `word16.xsd`) refer to their corresponding local ECMA/Word files.
* Four simple types referenced in the earlier Word namespace (`ST_OnOff`, `ST_String`, `ST_HexColorRGB`, `ST_UnsignedDecimalNumber`) now reside in ECMA's shared-commonSimpleTypes namespace. References are redirected to those existing declarations and the shared import added. The importer discovers the relocated names from the two schema files, not a separate type definition table.

The schema set is a complementary check, not complete modern Office coverage. Modern extension roots with global declarations are checked separately; contextual placement and cross-namespace extension attributes remain SDK checks and are reported as XSD coverage gaps. Unknown/undeclared roots are reported, not silently accepted. No base content models are patched. Markup-compatibility processing selects a temporary validation view and never changes stored XML.
