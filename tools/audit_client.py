"""Read-only inventory of the downloaded client and its protocol definitions."""
from pathlib import Path
import collections
import hashlib
import json
import re
import struct
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    client = next((ROOT / 'source-client').glob('astiob-*'))
    apis = []
    for path in (client / 'Assembly-CSharp').rglob('*.cs'):
        text = path.read_text(encoding='utf-8-sig')
        pattern = r'class\s+(\w+)\s*:\s*RequestTypeBase<([^\n]+)>\s*\{\s*public\s+\w+\(\)\s*\{\s*this\.apiId\s*=\s*"(\d+)"'
        for match in re.finditer(pattern, text):
            apis.append(dict(name=match[1], types=match[2], api_id=match[3],
                             source=str(path.relative_to(ROOT)),
                             line=text[:match.start()].count('\n') + 1))
    apk = ROOT / 'downloads/linksBiu-priv_64ver.apk'
    with zipfile.ZipFile(apk) as z:
        metadata = z.read('assets/bin/Data/Managed/Metadata/global-metadata.dat')
        magic, version, offset, size, data_offset, data_size = struct.unpack_from('<6I', metadata)
        assert magic == 0xFAB11BAF and size % 8 == 0
        assert offset + size <= len(metadata) and data_offset + data_size <= len(metadata)
        literals = []
        for i in range(offset, offset + size, 8):
            length, index = struct.unpack_from('<2I', metadata, i)
            assert index + length <= data_size
            literals.append(metadata[data_offset + index:data_offset + index + length].decode('utf-8'))
        # Only report endpoints and paths, never dump all embedded strings/credentials.
        endpoints = sorted(set(s for s in literals if len(s) < 200 and
                               (s.startswith(('/app/', '/appApi/', '/asset/')) or
                                'your-server.de' in s or s == '5.161.62.123')))
        result = dict(apk_sha256=hashlib.sha256(apk.read_bytes()).hexdigest(),
                      metadata_version=version, metadata_literal_count=len(literals),
                      endpoint_literals=endpoints,
                      builds={n: z.read(n).decode('utf-8-sig') for n in z.namelist()
                              if n.startswith('assets/BuildNumber/')},
                      native_libraries=[n for n in z.namelist() if n.startswith('lib/')],
                      unity_files=[{'path': n, 'bytes': z.getinfo(n).file_size} for n in z.namelist()
                                   if n.startswith('assets/bin/Data/') and '/Managed/' not in n],
                      source_api_count=len(apis),
                      source_api_literal_overlap=sum(a['api_id'] in literals for a in apis),
                      source_api_literal_missing=[a['api_id'] for a in apis if a['api_id'] not in literals],
                      note='Matching API literals do not prove matching protocol schemas or runtime compatibility.')
    out = ROOT / 'research'
    out.mkdir(exist_ok=True)
    (out / 'api-index.json').write_text(json.dumps(apis, ensure_ascii=False, indent=2), encoding='utf-8')
    (out / 'client-audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k != 'unity_files'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
