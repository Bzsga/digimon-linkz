"""Restore genuine Unity bundles and client catalogs from the user ZIP.

Only asset cache data is imported. No account, preferences or saves are copied.
Requires workspace .venv (UnityPy). Catalog aliases recover source path casing.
"""
from pathlib import Path, PurePosixPath
import argparse
import hashlib
import json
import re
import struct
import zipfile
import UnityPy

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--categories', help='Comma separated exact category paths; omit for all')
parser.add_argument('--output', type=Path, default=ROOT / 'assets-restored')
parser.add_argument('--keep-existing', action='store_true', help='Keep already repacked bundle files while rebuilding catalogs')
parser.add_argument('--only-missing', action='store_true', help='Extend existing catalogs without rescanning catalogued bundles')
parser.add_argument('--catalog-version', type=int, help='Defaults to the existing catalog version plus one')
args = parser.parse_args()
selected = set(args.categories.split(',')) if args.categories else None
output = args.output.resolve()
if args.catalog_version is None:
    previous_index = output / 'local-index.json'
    args.catalog_version = (int(json.loads(previous_index.read_text(encoding='utf-8'))['version']) + 1
                            if previous_index.exists() else 1)
base = output / 'AB_DATA/ANDROID/2.6.0'
base.mkdir(parents=True, exist_ok=True)
types = {'GO': (0, 'GameObject'), 'TE': (1, 'Texture2D'), 'TX': (2, 'TextAsset'),
         'AC': (3, 'AudioClip'), 'AS': (4, 'MonoBehaviour'), 'AN': (5, 'AnimationClip'),
         'CO': (6, 'AnimatorController'), 'AT': (6, 'AnimatorController'),
         'BY': (7, 'TextAsset'), 'FO': (8, 'Font')}
aliases = {}
with zipfile.ZipFile(ROOT / 'downloads/linksBiu-priv_64ver.apk') as apk:
    metadata = apk.read('assets/bin/Data/Managed/Metadata/global-metadata.dat')
    _, _, offset, size, data, _ = struct.unpack_from('<6I', metadata)
    for pos in range(offset, offset + size, 8):
        length, index = struct.unpack_from('<2I', metadata, pos)
        value = metadata[data + index:data + index + length].decode('utf-8')
        if '/' in value and len(value) < 200 and '\n' not in value:
            aliases.setdefault(value.lower(), set()).add(value)
        if value.startswith('EFF_') and len(value) < 100 and '/' not in value:
            for effect_category in ('HitEffects', 'DeathblowEffects', 'AlwaysEffects'):
                path = effect_category + '/' + value + '/prefab'
                aliases.setdefault(path.lower(), set()).add(path)
        if re.fullmatch(r'(?:\d{4}_|skill)[A-Za-z0-9_]+', value):
            path = 'CameraMotions/' + value + '/prefab'
            aliases.setdefault(path.lower(), set()).add(path)

categories, inventory, errors = {}, [], []
known = set()
if args.only_missing:
    if not args.keep_existing or selected:
        parser.error('--only-missing requires --keep-existing and all categories')
    for manifest_path in sorted(base.glob('*.json')):
        existing = json.loads(manifest_path.read_text(encoding='utf-8'))
        category = existing['abPath'].rstrip('/')
        categories[category] = existing['assetBundleInfoList']
        for row in existing['assetBundleInfoList']:
            path = base / category / (row['abName'] + '.unity3d')
            if not path.is_file():
                raise ValueError('Existing catalog has missing bundle: ' + str(path))
            known.add(row['abName'])
            inventory.append({'bundle': row['abName'], 'category': category,
                              'bytes': path.stat().st_size, 'paths': row['objNameList'],
                              'retained_existing': True})
with zipfile.ZipFile(ROOT / 'org.is_saved.zip') as archive:
    prefix = 'org.is_saved.digimon_links_ww/files/'
    records = json.loads(archive.read(prefix + 'ab_all_record_VER_0_1.txt').decode('utf-8-sig'))['DLRecordList']
    cache = {n.split('/')[-3]: n for n in archive.namelist() if n.endswith('/__data')}
    for index, (record_path, record) in enumerate(records.items()):
        category, bundle = record_path.rsplit('/_', 1)
        if bundle in known:
            continue
        if selected and category not in selected:
            continue
        suffix = bundle.rsplit('_', 1)[-1]
        if suffix not in types:
            errors.append({'bundle': bundle, 'reason': 'unknown type ' + suffix})
            continue
        raw = archive.read(cache[bundle])
        env = UnityPy.load(raw)
        type_id, expected_type = types[suffix]
        paths = set()
        object_ids = set()
        for asset_path, pointer in env.container.items():
            resource_prefix = next((p for p in ('assets/resources/', 'assets/assetbundles/resources/')
                                    if asset_path.startswith(p)), None)
            if resource_prefix is None:
                continue
            if pointer.type.name != expected_type:
                continue
            obj = pointer.read()
            if type(obj).__name__ != expected_type:
                continue
            path = str(PurePosixPath(asset_path[len(resource_prefix):]).with_suffix(''))
            if not path.lower().startswith(category.lower() + '/'):
                continue
            if type_id == 0 and not asset_path.endswith(('.prefab', '.fbx')):
                continue
            object_ids.add(pointer.path_id)
            relative = path[len(category) + 1:]
            paths.add(relative)
            original_name = getattr(obj, 'm_Name', '')
            if original_name and '/' not in original_name:
                parent = relative.rsplit('/', 1)[0] + '/' if '/' in relative else ''
                paths.add(parent + original_name)
            for alias in aliases.get(path.lower(), ()):
                if alias.startswith(category + '/'):
                    paths.add(alias[len(category) + 1:])
        if not paths:
            errors.append({'bundle': bundle, 'reason': 'no matching resource objects'})
            continue
        target = (base / category / (bundle + '.unity3d')).resolve()
        if not target.is_relative_to(base.resolve()):
            raise ValueError('Unsafe archive path')
        target.parent.mkdir(parents=True, exist_ok=True)
        if not args.keep_existing or not target.exists():
            target.write_bytes(raw)
        row = {'type': type_id, 'startName': min(paths), 'endName': max(paths),
               'abName': bundle, 'objNameList': sorted(paths), 'realCT': len(object_ids),
               'level': 'FONT' if category == 'Font' else 'TUTO1',
               'crc': record['crc'], 'size': str(target.stat().st_size)}
        categories.setdefault(category, []).append(row)
        inventory.append({'bundle': bundle, 'category': category, 'bytes': len(raw),
                          'sha256': hashlib.sha256(raw).hexdigest(), 'paths': sorted(paths)})
        if len(inventory) % 100 == 0:
            print('Restored', len(inventory), 'bundles', flush=True)

names = []
# Stage paths are built at runtime from master data, so APK string literals
# cannot supply their case-sensitive aliases. Apply to retained catalogs too.
master_dir = next((ROOT / 'source-masters').glob('astiob-*'))
stage_aliases = {}
for dungeon in json.loads(next(master_dir.glob('*990905*')).read_text(encoding='utf-8-sig'))['worldDungeonM']:
    stage_path = dungeon['background'] + '/prefab'
    stage_aliases.setdefault(stage_path.lower(), set()).add(stage_path)
for row in categories.get('Stages', []):
    paths = set(row['objNameList'])
    for path in list(paths):
        paths.update(stage_aliases.get(path.lower(), ()))
    row.update(objNameList=sorted(paths), startName=min(paths), endName=max(paths))
# FarmUtility builds the construction path with a capital C at runtime.
# Without this key, IsConstruction stays false and the completion tap is ignored.
for row in categories.get('Farm/Builds', []):
    paths = set(row['objNameList'])
    for path in list(paths):
        if path.startswith('construction/'):
            paths.add('Construction/' + path[len('construction/'):])
    row.update(objNameList=sorted(paths), startName=min(paths), endName=max(paths))
# TutorialSecondPart/HelpPart and GuidancePart assemble these paths at runtime.
# Bundle names are lowercase; FindObject uses the case-sensitive catalog key.
for row in categories.get('AdventureScene/Text', []):
    paths = set(row['objNameList'])
    for path in list(paths):
        for prefix in ('Tutorial', 'Guidance'):
            if path.startswith(prefix.lower() + '/'):
                paths.add(prefix + path[len(prefix):])
    row.update(objNameList=sorted(paths), startName=min(paths), endName=max(paths))
cache_source = ROOT / 'source-client/astiob-digimon-linkz-client-250d17b/Assembly-CSharp/AssetDataCacheData.cs'
tutorial_preload = {p.lower() for p in re.findall(r'"([^"\n]+/[^"\n]+)"', cache_source.read_text(encoding='utf-8-sig'))}
tutorial_preload.update({'characters/4/prefab', 'characters/6/prefab',
                         'characters/17/prefab', 'characters/74/prefab', 'characters/152/prefab',
                         'characters/25/prefab', 'characters/40/prefab', 'characters/31/prefab', 'characters/36/prefab',
                         'deathbloweffects/17/prefab', 'deathbloweffects/25/prefab', 'deathbloweffects/40/prefab', 'deathbloweffects/36/prefab',
                         'deathbloweffects/none/prefab', 'deathbloweffects/eff_com_skill/prefab',
                         'inheritancetechniqueeffects/none/prefab',
                         'stages/bg_battle_base_03/prefab'})
for category, rows in categories.items():
    # Keep tutorial preloads small. Other bundles remain available via LoadObject.
    level = 'FONT' if category == 'Font' else ('TUTO1' if category in ('BGM', 'Navi', 'SE', 'HitEffects', 'CameraMotions', 'cn/Tutorial_info') or category.startswith('Farm/') else 'ON_DEMAND')
    for row in rows:
        paths = set(row['objNameList'])
        for path in list(paths):
            for alias in aliases.get((category + '/' + path).lower(), ()):
                if alias.startswith(category + '/'):
                    paths.add(alias[len(category) + 1:])
        row.update(objNameList=sorted(paths), startName=min(paths), endName=max(paths))
        row['level'] = level
        if any((category + '/' + p).lower() in tutorial_preload for p in row['objNameList']):
            row['level'] = 'TUTO1'
    name = category.replace('/', '_') + '.json'
    manifest = {'ver': 1, 'time': '2026-09-29T00:00:00', 'name': category,
                'abPath': category + '/', 'assetBundleInfoList': rows}
    (base / name).write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    names.append({'name': name})
(output / 'local-index.json').write_text(json.dumps({'version': args.catalog_version, 'assetBundleFileInfo': names}), encoding='utf-8')
catalog = {'assetBundleVersionList': [{'assetbundlePath': '', 'assetbundleGroup': '1',
           'osType': '2', 'version': str(args.catalog_version), 'accessPath': 'http://10.0.2.2:3000/asset/local-index.json'}],
           'versionManagerAsset': {'versionManagerId': '1', 'versionManagerType': '2', 'osType': '2', 'version': str(args.catalog_version)}}
(output / 'catalog.json').write_text(json.dumps(catalog), encoding='utf-8')
report = {'bundle_count': len(inventory), 'categories': list(categories), 'errors': errors,
          'inventory': inventory, 'note': 'Reconstructed groups: Font=FONT; BGM/Navi/Farm/battle-common/starter-monsters=TUTO1; others=ON_DEMAND. Tutorial dependency coverage requires runtime validation.'}
(ROOT / 'research/restored-assets.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print('Complete:', len(inventory), 'bundles;', len(errors), 'unresolved;', len(categories), 'categories')
