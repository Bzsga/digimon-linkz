"""Offline project checks. Never start adb, an emulator, a game, or the live server.

Tests run against temporary SQLite databases and random loopback ports. The
player database, installed APK candidate and cache revision are fingerprinted
before and after. This is not client/runtime acceptance.
"""
import argparse
import ast
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
from linkz_server import Store
from audit_progression import audit_progression


def fingerprint(path):
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return {'bytes': path.stat().st_size, 'sha256': digest.hexdigest()}


def audit_assets(store, root):
    index = json.loads((root / 'local-index.json').read_text(encoding='utf-8-sig'))
    base = root / 'AB_DATA/ANDROID/2.6.0'
    paths, failures, count = set(), [], 0
    internal_path = ROOT / 'research/apk-internal-icons-latest.json'
    internal = json.loads(internal_path.read_text(encoding='utf-8')) if internal_path.is_file() else {}
    apk = fingerprint(ROOT / 'client-local/linkz-local-cn-hitfix.apk')
    internal_valid = bool(apk and internal.get('apk_sha256') == apk['sha256'] and not internal.get('failures'))
    internal_icons = internal.get('icons', {}) if internal_valid else {}
    icons_from_apk = []
    for entry in index['assetBundleFileInfo']:
        manifest = json.loads((base / entry['name']).read_text(encoding='utf-8-sig'))
        for row in manifest['assetBundleInfoList']:
            count += 1
            bundle = (base / manifest['abPath'] / (row['abName'] + '.unity3d')).resolve()
            if not bundle.is_relative_to(root.resolve()) or not bundle.is_file():
                failures.append({'bundle': row['abName'], 'reason': 'missing or outside asset root'})
            elif bundle.stat().st_size != int(row['size']):
                failures.append({'bundle': row['abName'], 'reason': 'size differs from manifest'})
            paths.update((manifest['abPath'] + p).lower() for p in row['objNameList'])
    # Original AssetDataMng.GetLocalizedPath prepends en/; cn/ bundles
    # also exist in the restored catalog. Never require a nonexistent bare path.
    mission_paths = {language + '/missionbanner/mission_' + group
                     for language in ('en', 'cn') for group in store.progression.GROUPS}
    for mission in store.progression.missions.values():
        for reward in mission['rewards']:
            if reward['assetCategoryId'] == '14':
                mission_paths.add('evolveitemthumbnail/' + reward['assetValue'] + '/thumb')
    for path in sorted(mission_paths):
        if path not in paths:
            failures.append({'reason': 'local mission banner or material icon absent from catalog', 'path': path})
    monsters = {r['monsterId']: r for r in store.masters['990102']['monsterM']}
    groups = {r['monsterGroupId']: r for r in store.masters['990103']['monsterM']}
    dungeons = {r['worldDungeonId']: r for r in store.masters['990905']['worldDungeonM']}
    normal_paths, material_ids = set(), set()
    ordinary_monsters = audit_progression(store)['materialSupportedOrdinaryMonsterIds']
    for mid in ordinary_monsters:
        normal_paths.add('characters/' + groups[monsters[mid]['monsterGroupId']]['modelId'] + '/prefab')
        normal_paths.add('characterthumbnail/' + monsters[mid]['iconId'] + '/thumb')
    version_routes = [r for r in store.masters['990104']['monsterEvolutionM']
                      if r['type'] == '3' and r['effectType'] == '5' and
                      monsters[r['baseMonsterId']]['monsterGroupId'] == monsters[r['nextMonsterId']]['monsterGroupId']]
    mode_routes = [r for r in store.masters['990104']['monsterEvolutionM']
                   if r['type'] == '2' and r['effectType'] == '2']
    for route in mode_routes:
        for mid in (route['baseMonsterId'], route['nextMonsterId']):
            normal_paths.add('characters/' + groups[monsters[mid]['monsterGroupId']]['modelId'] + '/prefab')
            normal_paths.add('characterthumbnail/' + monsters[mid]['iconId'] + '/thumb')
        source, target = monsters[route['baseMonsterId']], monsters[route['nextMonsterId']]
        source_subs = {r['skillGroupSubId'] for r in store.masters['990108']['skillM'] if r['skillGroupId'] == source['skillGroupId']}
        target_subs = {r['skillGroupSubId'] for r in store.masters['990108']['skillM'] if r['skillGroupId'] == target['skillGroupId']}
        if source_subs - target_subs or source['rare'] != target['rare'] or source['maxLevel'] != target['maxLevel']:
            failures.append({'route': route['monsterEvolutionId'], 'reason': 'mode change skill/rarity/level preservation invalid'})
    version_materials = set()
    recipes = {r['monsterEvolutionMaterialId']: r for r in store.masters['990115']['monsterEvolutionMaterialM']}
    for route in version_routes:
        for mid in (route['baseMonsterId'], route['nextMonsterId']):
            normal_paths.add('characters/' + groups[monsters[mid]['monsterGroupId']]['modelId'] + '/prefab')
            normal_paths.add('characterthumbnail/' + monsters[mid]['iconId'] + '/thumb')
        recipe = recipes[route['monsterEvolutionMaterialId']]
        for i in range(1, 8):
            if recipe['materialAssetNum' + str(i)] != '0':
                version_materials.add(recipe['materialAssetValue' + str(i)])
    version_materials.add('4000008')
    normal_paths.update('evolveitemthumbnail/' + sid + '/thumb' for sid in version_materials)
    normal_paths.update('chipthumbnail/' + store.chip_inventory.master[i]['icon'] for i in store.chip_inventory.bonus)
    normal_paths.update(('chipthumbnail/extra_slot_01', 'chipthumbnail/eject_slot_01',
        'farm/builds/farm_build_10/farm_build_10', 'cutscenes/assetbundle/chipgasha/chip_gacha',
        'bgm/bgm_202/sound', 'bgm/bgm_204/sound', 'bgm/bgm_205/sound'))
    for ident, config in store.normal_world.items():
        dungeon = dungeons[ident]
        normal_paths.add('stages/' + dungeon['background'] + '/prefab')
        for key in ('bgm', 'bossBgm', 'exBossBgm'):
            if dungeon[key] not in ('', '0'):
                normal_paths.add('bgm/' + dungeon[key] + '/sound')
        for mid in config['enemyMonsterIds']:
            normal_paths.add('characters/' + groups[monsters[mid]['monsterGroupId']]['modelId'] + '/prefab')
        for drop in store.normal_material_drops(config):
            material_ids.add(str(drop['assetValue']))
            normal_paths.add('evolveitemthumbnail/' + str(drop['assetValue']) + '/thumb')
    for path in sorted(normal_paths):
        if path.lower() not in paths:
            # GUIMonsterIcon first loads CharacterThumbnailInternal from APK
            # Resources, then falls back to CharacterThumbnail bundles.
            key = path.lower().replace('characterthumbnail/', 'characterthumbnailinternal/', 1)
            texture = internal_icons.get(key) if path.startswith('characterthumbnail/') else None
            if texture and texture.get('width', 0) > 0 and texture.get('height', 0) > 0 and texture.get('image_bytes', 0) > 0:
                icons_from_apk.append(key)
            else:
                failures.append({'reason': 'normal dungeon model, environment, audio or icon absent from bundles/APK audit', 'path': path})
    required = set()
    pools = {'growthMonsterId': ('81', '121', '176', '196'), 'childhood1MonsterId': ('1', '6', '16')}
    for field, pool in pools.items():
        for mid in pool:
            route = next(r for r in store.masters['990110']['monsterEvolutionRouteM'] if r[field] == mid)
            required.update(route[k] for k in ('childhood1MonsterId', 'childhood2MonsterId', 'growthMonsterId'))
    # Research maps the saved birth family to any awakening1..5. Audit every
    # original route, its egg model and all three future forms, not only gacha.
    routes = store.masters['990110']['monsterEvolutionRouteM']
    for route in routes:
        required.update(route[k] for k in ('eggMonsterId', 'childhood1MonsterId', 'childhood2MonsterId', 'growthMonsterId'))
    for mid in sorted(required, key=int):
        path = 'characters/' + groups[monsters[mid]['monsterGroupId']]['modelId'] + '/prefab'
        if path.lower() not in paths:
            failures.append({'monsterId': mid, 'reason': 'model path absent from catalog', 'path': path})
    facilities = store.masters['990301']['facilityM']
    for row in facilities:
        ident = row['facilityId']
        path = 'farm/builds/' + row['modelResource']
        if path.lower() not in paths:
            failures.append({'facilityId': ident, 'reason': 'facility model absent from catalog', 'path': path})
        footprint = store.farm_layout['footprints'].get(ident, {})
        if (any(type(footprint.get(k)) is not int for k in
                ('sizeX', 'sizeY', 'baseGridPositionX', 'baseGridPositionY')) or
                not 0 <= footprint.get('baseGridPositionX', -1) < footprint.get('sizeX', 0) or
                not 0 <= footprint.get('baseGridPositionY', -1) < footprint.get('sizeY', 0)):
            failures.append({'facilityId': ident, 'reason': 'missing or invalid footprint'})
    for source in store.farm_layout.get('sources', []):
        bundle = (ROOT / source['path']).resolve()
        if not bundle.is_relative_to(root.resolve()) or not bundle.is_file():
            failures.append({'source': source['path'], 'reason': 'farm layout source missing or outside asset root'})
        elif fingerprint(bundle)['sha256'] != source['sha256']:
            failures.append({'source': source['path'], 'reason': 'farm layout source changed since extraction'})
    return {'catalog_bundles': count, 'catalog_groups': len(index['assetBundleFileInfo']),
            'normal_dungeons_checked': len(store.normal_world),
            'material_supported_ordinary_monsters_checked': len(ordinary_monsters),
            'same_form_version_routes_checked': len(version_routes),
            'mode_change_routes_checked': len(mode_routes),
            'version_material_icon_ids_checked': sorted(version_materials, key=int),
            'ordinary_icons_resolved_in_apk': icons_from_apk,
            'apk_internal_icon_report_matches_apk': internal_valid,
            'normal_enemy_environment_audio_material_paths_checked': sorted(normal_paths),
            'renewable_normal_material_ids_checked': sorted(material_ids, key=int),
            'mission_banner_and_material_paths_checked': sorted(mission_paths),
            'capture_and_garden_monsters_checked': sorted(required, key=int),
            'research_and_garden_routes_checked': len(routes),
            'facility_models_and_footprints_checked': len(facilities),
            'farm_layout_source_hashes_checked': len(store.farm_layout.get('sources', [])),
            'failures': failures, 'scope': 'File presence, declared byte sizes and model aliases only; no Unity rendering.'}


def source_snapshot():
    paths = {p for folder in ('server', 'tools') for p in (ROOT / folder).glob('*.py')}
    paths.update((ROOT / 'server').rglob('*.json'))
    paths.update((ROOT / 'source-masters').rglob('*.json'))
    paths.update((ROOT / 'source-client').glob('astiob-*/Secure/ConstValue.cs'))
    paths.add(ROOT / 'research/apk-internal-icons-latest.json')
    return {str(p.relative_to(ROOT)): fingerprint(p) for p in sorted(paths)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'research/offline-check-latest.json')
    parser.add_argument('--skip-tests', action='store_true', help='Static audit only; explicitly marked as such')
    args = parser.parse_args()
    args.output = args.output.resolve()
    if not args.output.is_relative_to((ROOT / 'research').resolve()) or args.output.suffix != '.json':
        parser.error('--output must be a JSON report inside the project research directory')
    protected = (ROOT / 'runtime/linkz.sqlite3', ROOT / 'runtime/localization-version.json',
                 ROOT / 'client-local/linkz-local-cn-hitfix.apk')
    before = {str(p.relative_to(ROOT)): fingerprint(p) for p in protected}
    sources_before = source_snapshot()
    errors, checked_python, checked_json = [], 0, 0
    for folder in ('server', 'tools'):
        for path in (ROOT / folder).glob('*.py'):
            try:
                ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
                checked_python += 1
            except (SyntaxError, UnicodeError) as exc:
                errors.append({'path': str(path.relative_to(ROOT)), 'reason': str(exc)})
    for path in (ROOT / 'server').rglob('*.json'):
        try:
            json.loads(path.read_text(encoding='utf-8-sig'))
            checked_json += 1
        except (ValueError, UnicodeError) as exc:
            errors.append({'path': str(path.relative_to(ROOT)), 'reason': str(exc)})
    with tempfile.TemporaryDirectory() as temporary:
        store = Store(Path(temporary) / 'audit.sqlite3')
        assets = audit_assets(store, ROOT / 'assets-restored-all')
        progression = audit_progression(store)
    save = ROOT / 'runtime/linkz.sqlite3'
    save_check = None
    if save.is_file():
        db = sqlite3.connect(save.as_uri() + '?mode=ro', uri=True)
        try:
            save_check = db.execute('PRAGMA quick_check').fetchone()[0]
        finally:
            db.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tests = {'run': False, 'passed': None, 'count': None}
    if not args.skip_tests:
        log = args.output.with_suffix('.tests.log')
        print('Running isolated server tests; no game will be started.', flush=True)
        with log.open('w', encoding='utf-8') as stream:
            completed = subprocess.run([sys.executable, '-X', 'utf8', '-m', 'unittest', 'discover',
                                        '-s', 'server', '-v'], cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
        match = re.search(r'^Ran (\d+) tests?', log.read_text(encoding='utf-8'), re.M)
        tests = {'run': True, 'passed': completed.returncode == 0, 'count': int(match[1]) if match else None,
                 'log': str(log.relative_to(ROOT)) if log.is_relative_to(ROOT) else str(log)}
    after = {str(p.relative_to(ROOT)): fingerprint(p) for p in protected}
    sources_after = source_snapshot()
    preserved = before == after
    sources_unchanged = sources_before == sources_after
    success = not errors and not assets['failures'] and not progression['blockedConfiguredDungeons'] and save_check in (None, 'ok') and preserved and sources_unchanged and tests['passed'] is not False
    report = {'checked_at_utc': datetime.now(timezone.utc).isoformat(), 'passed': success,
              'scope': 'Offline/static and isolated protocol tests; client runtime and player acceptance deferred.',
              'python_files': checked_python, 'json_files': checked_json, 'static_errors': errors,
              'assets': assets, 'progression': progression, 'save_quick_check_read_only': save_check, 'tests': tests,
              'protected_before': before, 'protected_after': after, 'protected_files_unchanged': preserved,
              'source_before': sources_before, 'source_after': sources_after, 'source_files_unchanged': sources_unchanged,
              'game_started': False, 'live_server_restarted': False}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'passed': success, 'static_errors': len(errors), 'asset_errors': len(assets['failures']),
                      'tests': tests, 'protected_files_unchanged': preserved, 'report': str(args.output)}, ensure_ascii=False))
    raise SystemExit(0 if success else 1)


if __name__ == '__main__':
    main()
