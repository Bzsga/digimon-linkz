"""Redirect a COPY of the verified community APK to Android emulator host.

Updates IL2CPP literal lengths without relocating metadata tables. Output must be
aligned and signed before installation. Native code and game assets are unchanged.
"""
from pathlib import Path
import argparse
import hashlib
import json
import struct
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_SHA = 'f1ad4bb1b0dff47f51a9a1566d90fb80b1ab4e7b3604b1268355de40ad78b884'
META = 'assets/bin/Data/Managed/Metadata/global-metadata.dat'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='10.0.2.2')
    parser.add_argument('--port', type=int, default=3000)
    args = parser.parse_args()
    source = ROOT / 'downloads/linksBiu-priv_64ver.apk'
    if hashlib.sha256(source.read_bytes()).hexdigest() != ORIGINAL_SHA:
        raise ValueError('Input APK does not match the inspected baseline')
    replacements = {'http://static.123.62.161.5.clients.your-server.de:3000': f'http://{args.host}:{args.port}',
                    '5.161.62.123': args.host}
    output = ROOT / 'client-local/linkz-local-unsigned.apk'
    output.parent.mkdir(exist_ok=True)
    changes = []
    with zipfile.ZipFile(source) as archive:
        metadata = bytearray(archive.read(META))
        magic, version, offset, size, data_offset, data_size = struct.unpack_from('<6I', metadata)
        assert magic == 0xFAB11BAF and version == 24
        for entry in range(offset, offset + size, 8):
            length, index = struct.unpack_from('<2I', metadata, entry)
            begin = data_offset + index
            value = metadata[begin:begin + length].decode('utf-8')
            if value in replacements:
                new = replacements[value].encode('utf-8')
                if len(new) > length:
                    raise ValueError('Replacement is too long for in-place literal patch')
                metadata[begin:begin + length] = new + b'\0' * (length - len(new))
                struct.pack_into('<I', metadata, entry, len(new))
                changes.append({'old': value, 'new': replacements[value], 'literal_entry': entry})
        if {c['old'] for c in changes} != set(replacements):
            raise ValueError('Expected endpoints were not found')
        with zipfile.ZipFile(output, 'w') as result:
            for info in archive.infolist():
                if info.filename.upper().startswith('META-INF/'):
                    continue
                result.writestr(info, metadata if info.filename == META else archive.read(info.filename))
    with zipfile.ZipFile(source) as before, zipfile.ZipFile(output) as after:
        assert after.testzip() is None
        for name in after.namelist():
            if name != META:
                assert after.getinfo(name).CRC == before.getinfo(name).CRC
    report = {'baseline_sha256': ORIGINAL_SHA, 'unsigned_sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
              'changes': changes, 'signed': False, 'installed': False, 'runtime_verified': False}
    (output.parent / 'patch-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
