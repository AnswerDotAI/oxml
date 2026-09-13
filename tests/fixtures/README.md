# Borrowed test material

Thank you to the upstream authors and contributors credited in the [project README](../../README.md#license-and-acknowledgements).
These DOCX files and Pandoc `.native` expectations are copied originals, not oxml-generated documents. Their upstream terms remain applicable;
the project's Apache-2.0 license does not replace them. Edited documents are produced only in memory or temporary test output.

Paths below are relative to this directory. Each file keeps its upstream filename. License copies are shared across directories rather than duplicated.

| Project | Local files | Upstream directory | Retained notices |
| --- | --- | --- | --- |
| [Open XML SDK](https://github.com/dotnet/Open-XML-SDK) | `sdk/*.docx` | `test/DocumentFormat.OpenXml.Tests.Assets/assets/TestFiles/` | [MIT](sdk/LICENSE); [source-file copyright](../../python/oxml/SDK-LICENSE) |
| [Pandoc](https://github.com/jgm/pandoc) | `pandoc/*.docx`, `pandoc/*.native`, `body/{char_styles,inline_formatting,lists_level_override,table_header_rowspan}.docx`, `crosspart/notes.docx`, `reviews/pandoc/track_changes_move.docx` | `test/docx/` | [Copyright](pandoc/COPYRIGHT), [GPL](pandoc/COPYING.md) |
| [python-docx](https://github.com/python-openxml/python-docx) | `body/sct-inner-content.docx` | `tests/test_files/` | [MIT / Steve Canny](body/LICENSE.python-docx) |
| [Apache POI](https://github.com/apache/poi) | `crosspart/headerPic.docx` | `test-data/document/` | [Apache-2.0](crosspart/LICENSE.Apache-POI), [NOTICE](crosspart/NOTICE.Apache-POI) |
| [LibreOffice](https://github.com/LibreOffice/core) | `reviews/libreoffice/{CommentReply,CommentDone}.docx`, `crosspart/footer-contain-hyperlink.docx` | `sw/qa/extras/ooxmlexport/data/` | [MPL](reviews/libreoffice/COPYING.MPL), [LGPL](reviews/libreoffice/COPYING.LGPL), [COPYING](reviews/libreoffice/COPYING) |
| [Open XML PowerTools](https://github.com/OpenXmlDev/Open-Xml-PowerTools) | `reviews/powertools/RP024-ParagraphMark-rPr-Change.docx` | `TestFiles/RP/` | [MIT / Microsoft](reviews/powertools/LICENSE) |

SDK validation cases in `tests/test_sdk_*.py` adapt the upstream C# tests into Python; their source comments name the original cases.
The SDK notice applies to that adapted material as well as the imported schema metadata. Review, style, section and cross-part tests also
credit their upstream behavioral examples in comments; oxml's independent assertions do not imply that the upstream suites were run.
