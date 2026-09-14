"Download Microsoft's published Word extension schemas, retaining declarations and localizing imports."
from html import unescape
from pathlib import Path
import re
from urllib.request import urlopen
from xml.etree import ElementTree as ET

DEST = Path(__file__).resolve().parents[1]/'schema/xsd'
BASE = 'https://learn.microsoft.com/en-us/openspecs/office_standards/ms-docx/'
SOURCES = {
    'w14': '9704b59f-bc49-4618-ac66-41beb82a0d7f',
    'w15': 'd416013d-c112-44fa-8bef-7819b8898117',
    'w16se': '372b619f-f0f4-463f-9d59-02ebb1c8956d',
    'w16cid': '8e395dbe-ff70-43c3-b9fe-e758c8f69683',
    'w16': '6970e332-57ff-4ab5-a9a4-3b8834bbc477',
    'w16cex': '0df7c115-b22a-4e09-bab7-4f24fbb8e6f5',
    'w16sdtdh': 'c865ba38-51df-45bc-91ae-f6beaffeb264',
    'w16du': 'e5d0aa0c-4ecc-40d9-a0e0-aac8655a8316',
    'w16sdtfl': '14bc8126-7c6e-41a8-b042-c199036e2339',
    'w16cei': '4c5c0c9a-50e6-4d50-a387-9e5aceecd2f9',
}
IMPORTS = {'word12.xsd': 'wml.xsd', 'oartbasetypes.xsd': 'dml-main.xsd', 'oartsplineproperties.xsd': 'dml-main.xsd',
           'orel.xsd': 'shared-relationshipReference.xsd', 'word16.xsd': 'w16.xsd'}

def main():
    shared = ET.parse(DEST/'shared-commonSimpleTypes.xsd').getroot()
    word = ET.parse(DEST/'wml.xsd').getroot()
    moved = {e.get('name') for e in shared} - {e.get('name') for e in word}
    uri = shared.get('targetNamespace')
    for name, page in SOURCES.items():
        with urlopen(BASE+page, timeout=45) as response: html = response.read().decode()
        block, = [unescape(re.sub('<[^>]*>', '', b)) for b in re.findall(r'<pre[^>]*>(.*?)</pre>', html, re.S)
                  if '&lt;xsd:schema' in b]
        block = '\n'.join(line.replace('\xa0', ' ').rstrip() for line in block.strip().splitlines())+'\n'
        for source, dest in IMPORTS.items(): block = block.replace(f'schemaLocation="{source}"', f'schemaLocation="{dest}"')
        # MS-DOCX uses the earlier word12.xsd organization; ECMA moved these existing definitions to shared types.
        block = re.sub(r'type="(?:w|w12):([^\"]+)"',
            lambda m: f'type="s:{m[1]}"' if m[1] in moved else m[0], block)
        if 'type="s:' in block:
            block = block.replace('<xsd:schema ', f'<xsd:schema xmlns:s="{uri}" ', 1)
            opening = block.index('>')+1
            block = block[:opening] + f'\n   <xsd:import namespace="{uri}" schemaLocation="shared-commonSimpleTypes.xsd"/>' + block[opening:]
        (DEST/f'{name}.xsd').write_text(block)
        print(name)

if __name__ == '__main__': main()
