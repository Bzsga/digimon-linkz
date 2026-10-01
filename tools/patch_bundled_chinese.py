"""Patch Chinese ScriptableObjects in a copy of the existing local APK.

Use the project's UnityPy environment. Align/sign the candidate with the same
keystore before adb install -r. Original APK, key and application data stay intact.
"""
import hashlib
import json
import struct
import sys
import tempfile
import zipfile
from pathlib import Path

import UnityPy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
from linkz_server import Store

BASELINE_SHA = 'd8d281804c56149cb1037cebd330524b7aa6738022b17a298ac9096dbe6394e6'
DATA = 'assets/bin/Data/'


def decode(raw, expected_name, fields):
    position = 28
    def string():
        nonlocal position
        length, = struct.unpack_from('<i', raw, position)
        position += 4
        if not 0 <= length <= len(raw) - position:
            raise ValueError('Invalid Unity string length')
        value = raw[position:position + length].decode('utf-8')
        position = (position + length + 3) & ~3
        return value
    if string() != expected_name:
        raise ValueError('Unexpected ScriptableObject name')
    count, = struct.unpack_from('<i', raw, position)
    position += 4
    if not 0 < count < 10000:
        raise ValueError('Unexpected table size')
    rows = [{field: string() for field in fields} for _ in range(count)]
    if position != len(raw) or len({r['messageCode'] for r in rows}) != count:
        raise ValueError('Unexpected table layout or duplicate keys')
    return rows


def encode(base, name, rows, fields):
    def string(value):
        value = value.encode('utf-8')
        return struct.pack('<i', len(value)) + value + bytes((-len(value)) % 4)
    return (base[:28] + string(name) + struct.pack('<i', len(rows)) +
            b''.join(string(row[field]) for row in rows for field in fields))


def main():
    source = ROOT / 'client-local/linkz-local.apk'
    if hashlib.sha256(source.read_bytes()).hexdigest() != BASELINE_SHA:
        raise ValueError('Existing local APK differs from the preserved baseline')
    with tempfile.TemporaryDirectory() as temporary:
        store = Store(Path(temporary) / 'audit.sqlite3')
        localized = store.localization.masters
    changed_files = {}
    reports = []
    with zipfile.ZipFile(source) as archive:
        manager_env = UnityPy.load(archive.read(DATA + 'globalgamemanagers'))
        manager = next(o for o in manager_env.objects if o.type.name == 'ResourceManager')
        pointers = dict(manager.read_typetree()['m_Container'])
        files = {}
        for name, api, table, fields in (
            ('message_string_m3', '990802', 'messageStringM', ('messageCode', 'messageText')),
            ('message_m3', '990801', 'messageM', ('messageCode', 'messageTitle', 'messageText', 'actionType', 'actionValue', 'isResources'))):
            pointer = pointers['master/' + name]
            filename = DATA + manager.assets_file.externals[pointer['m_FileID'] - 1].path
            if filename not in files:
                env = UnityPy.load(archive.read(filename))
                files[filename] = (env, {o.path_id: o.get_raw_data() for o in env.objects}, {})
            env, before, modified = files[filename]
            obj = next(o for o in env.objects if o.path_id == pointer['m_PathID'])
            raw = obj.get_raw_data()
            original = decode(raw, name, fields)
            translations = {r['messageCode']: r for r in localized[api][table]}
            if api == '990802':
                rows = list(translations.values())
            else:
                rows = [dict(row, **{field: translations[row['messageCode']][field]
                        for field in ('messageTitle', 'messageText')})
                        if row['messageCode'] in translations else row for row in original]
            new_raw = encode(raw, name, rows, fields)
            assert decode(new_raw, name, fields) == rows
            obj.set_raw_data(new_raw)
            modified[obj.path_id] = new_raw
            reports.append({'table': name, 'previous_rows': len(original), 'rows': len(rows),
                            'sha256': hashlib.sha256(new_raw).hexdigest()})
        for filename, (env, before, modified) in files.items():
            data = next(iter(env.files.values())).save()
            restored = UnityPy.load(data)
            assert {o.path_id for o in restored.objects} == set(before)
            for obj in restored.objects:
                if obj.path_id not in modified:
                    assert obj.get_raw_data() == before[obj.path_id], obj.path_id
                else:
                    assert obj.get_raw_data() == modified[obj.path_id]
            changed_files[filename] = data
        output = ROOT / 'client-local/linkz-local-cn-unsigned.apk'
        with zipfile.ZipFile(output, 'w') as result:
            for info in archive.infolist():
                if not info.filename.upper().startswith('META-INF/'):
                    result.writestr(info, changed_files.get(info.filename, archive.read(info.filename)))
    with zipfile.ZipFile(source) as before, zipfile.ZipFile(output) as after:
        assert after.testzip() is None
        for name in after.namelist():
            if name not in changed_files:
                assert after.getinfo(name).CRC == before.getinfo(name).CRC, name
    report = {'baseline_sha256': BASELINE_SHA,
              'candidate_unsigned_sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
              'changed_files': list(changed_files), 'tables': reports,
              'unrelated_objects_preserved': True, 'signed': False, 'installed': False}
    (ROOT / 'research/bundled-chinese-patch.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
