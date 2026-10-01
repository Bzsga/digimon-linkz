"""Convert cached UnityFS v8 containers to v6 without changing embedded bytes."""
from pathlib import Path
import argparse
import hashlib
import json
import zlib
import UnityPy
from UnityPy.streams import EndianBinaryReader

parser = argparse.ArgumentParser()
parser.add_argument('root', type=Path)
args = parser.parse_args()
base = args.root / 'AB_DATA/ANDROID/2.6.0'
report = []
audit_path = args.root / 'repack-audit.json'
verified = {row['bundle']: row for row in json.loads(audit_path.read_text(encoding='utf-8'))} if audit_path.exists() else {}
for manifest_file in sorted(base.glob('*.json')):
    manifest = json.loads(manifest_file.read_text(encoding='utf-8'))
    for row in manifest['assetBundleInfoList']:
        path = base / manifest['abPath'] / (row['abName'] + '.unity3d')
        source = path.read_bytes()
        prior = verified.get(row['abName'])
        if (prior and prior.get('embedded_bytes_identical') is True
                and prior['crc'] == row['crc']
                and hashlib.sha256(source).hexdigest() == prior['after_sha256']):
            row['size'] = str(len(source))
            report.append(prior)
            continue
        bundle = next(iter(UnityPy.load(source).files.values()))
        crc = 0
        embedded = {}
        for name, file in list(bundle.files.items()):
            raw = file.bytes if isinstance(file, EndianBinaryReader) else file.reader.bytes
            crc = zlib.crc32(raw, crc)
            embedded[name] = raw
            reader = EndianBinaryReader(raw)
            reader.flags = file.flags
            bundle.files[name] = reader
        if crc != row['crc']:
            raise ValueError(f'CRC mismatch: {path}: {crc} != {row["crc"]}')
        bundle.version = 6
        bundle.version_engine = '2018.4.11f1'
        bundle._uses_block_alignment = False
        output = bundle.save(packer='lz4')
        restored = next(iter(UnityPy.load(output).files.values()))
        for name, file in restored.files.items():
            raw = file.bytes if isinstance(file, EndianBinaryReader) else file.reader.bytes
            assert raw == embedded[name], f'Embedded payload changed: {path}:{name}'
        path.write_bytes(output)
        row['size'] = str(len(output))
        report.append({'bundle': row['abName'], 'crc': crc, 'embedded_bytes_identical': True,
                       'before_sha256': hashlib.sha256(source).hexdigest(),
                       'after_sha256': hashlib.sha256(output).hexdigest()})
        if len(report) % 100 == 0:
            print('Repacked and verified', len(report), flush=True)
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
(args.root / 'repack-audit.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print('Repacked and byte-verified', len(report), 'bundles')
