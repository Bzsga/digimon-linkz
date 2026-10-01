"""Read-only normal-world/material coverage; never open the player database."""
import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
from linkz_server import Store


def audit_progression(store):
    areas = {r['worldAreaId'] for r in store.masters['990903']['worldAreaM'] if r['worldId'] == '1'}
    stages = {r['worldStageId']: r for r in store.masters['990904']['worldStageM'] if r['worldAreaId'] in areas}
    dungeons = {r['worldDungeonId']: r for r in store.masters['990905']['worldDungeonM'] if r['worldStageId'] in stages}
    reached = set()
    while True:
        previous = len(reached)
        for ident in store.normal_world:
            row = dungeons[ident]
            stage_parent = stages[row['worldStageId']]['prerequisiteWorldStageId']
            prior_stage = {i for i, d in dungeons.items() if d['worldStageId'] == stage_parent}
            if ((row['prerequisiteWorldDungeonId'] in ('', '0') or row['prerequisiteWorldDungeonId'] in reached) and
                    (stage_parent in ('', '0') or prior_stage and prior_stage <= reached)):
                reached.add(ident)
        if len(reached) == previous:
            break
    names = {r['soulId']: r['soulName'] for r in store.localization.masters['990201']['soulM']}
    sources = {}
    for ident, config in store.normal_world.items():
        dungeon = dungeons[ident]
        for drop in store.normal_material_drops(config):
            sid = str(drop['assetValue'])
            item = sources.setdefault(sid, {'name': names[sid], 'sources': []})
            item['sources'].append({'dungeonId': ident, 'stageId': dungeon['worldStageId'],
                'perWave': drop['assetNum'], 'perWin': drop['assetNum'] * int(dungeon['battleNum']),
                'boxType': drop['dropBoxType'], 'prerequisitesReachable': ident in reached})
    # Track ordinary branches from actual local capture families. This checks
    # materials and supported forms only, not combat difficulty or UI behavior.
    available = {sid for sid, row in sources.items() if any(s['prerequisitesReachable'] for s in row['sources'])}
    capture = {'81', '121', '176', '196'}
    for mid in ('1', '6', '16'):
        route = next(r for r in store.masters['990110']['monsterEvolutionRouteM'] if r['childhood1MonsterId'] == mid)
        capture.add(route['growthMonsterId'])
    monsters = {r['monsterId']: r for r in store.masters['990102']['monsterM']}
    groups = {r['monsterGroupId']: r for r in store.masters['990103']['monsterM']}
    materials = {r['monsterEvolutionMaterialId']: r for r in store.masters['990115']['monsterEvolutionMaterialM']}
    evolutions = [r for r in store.masters['990104']['monsterEvolutionM']
                  if (r['type'], r['effectType']) in (('1', '1'), ('2', '2')) and
                  groups[monsters[r['nextMonsterId']]['monsterGroupId']]['growStep'] in ('5', '6', '7', '11', '12', '13')]
    obtainable = set(capture)
    while True:
        previous = len(obtainable)
        for route in evolutions:
            recipe = materials[route['monsterEvolutionMaterialId']]
            needs = [(recipe['materialAssetCategoryId' + str(i)], recipe['materialAssetValue' + str(i)])
                     for i in range(1, 8) if int(recipe['materialAssetNum' + str(i)]) > 0]
            if route['baseMonsterId'] in obtainable and all(c == '14' and sid in available for c, sid in needs):
                obtainable.add(route['nextMonsterId'])
        if len(obtainable) == previous:
            break
    gaps = []
    for route in evolutions:
        if route['baseMonsterId'] not in obtainable:
            continue
        recipe = materials[route['monsterEvolutionMaterialId']]
        missing = [{'category': recipe['materialAssetCategoryId' + str(i)], 'id': recipe['materialAssetValue' + str(i)],
                    'quantity': recipe['materialAssetNum' + str(i)]}
                   for i in range(1, 8) if int(recipe['materialAssetNum' + str(i)]) > 0 and
                   (recipe['materialAssetCategoryId' + str(i)] != '14' or recipe['materialAssetValue' + str(i)] not in available)]
        if missing:
            gaps.append({'evolutionId': route['monsterEvolutionId'], 'baseMonsterId': route['baseMonsterId'],
                         'nextMonsterId': route['nextMonsterId'], 'missingMaterials': missing})
    blocked = sorted(set(store.normal_world) - reached, key=int)
    version_coverage = []
    for route in store.masters['990104']['monsterEvolutionM']:
        source, destination = monsters[route['baseMonsterId']], monsters[route['nextMonsterId']]
        if (route['type'] != '3' or route['effectType'] != '5' or
                source['monsterGroupId'] != destination['monsterGroupId'] or
                groups[source['monsterGroupId']]['monsterType'] != '0'):
            continue
        recipe = materials[route['monsterEvolutionMaterialId']]
        required = {recipe['materialAssetValue' + str(i)] for i in range(1, 8)
                    if int(recipe['materialAssetNum' + str(i)]) > 0}
        missing = sorted(required - available, key=int)
        version_coverage.append({'baseMonsterId': route['baseMonsterId'], 'nextMonsterId': route['nextMonsterId'],
            'allIngredientsRenewable': not missing, 'missingMaterialIds': missing,
            'scope': 'Ingredients only; naturally rearing a maximum-level +4 base is not proved.'})
    status_materials = {r['materialAssetValue' + str(i)]
                       for r in store.masters['990121']['monsterStatusAilmentMaterialM'] for i in range(1, 5)
                       if int(r['materialAssetNum' + str(i)]) > 0}
    status_materials.update(r['assetValue'] for r in store.masters['990120']['monsterStatusAilmentM'])
    return {'scope': 'Static prerequisite and renewable material coverage only; EXP, money, skills, combat, menus and runtime not proved.',
            'configuredNormalDungeons': len(store.normal_world), 'originalNormalDungeons': len(dungeons),
            'reachableNormalDungeons': len(reached), 'blockedConfiguredDungeons': blocked,
            'unconfiguredNormalDungeons': sorted(set(dungeons) - set(store.normal_world), key=int),
            'renewableMaterialCount': len(available), 'materials': dict(sorted(sources.items(), key=lambda item: int(item[0]))),
            'captureSpecies': sorted(capture, key=int), 'materialSupportedOrdinaryMonsterIds': sorted(obtainable, key=int),
            'ordinarySameFormVersionMaterialCoverage': version_coverage,
            'statusResistanceMissingMaterialIds': sorted(status_materials - available, key=int),
            'ordinaryBranchMaterialGaps': gaps}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'research/material-routes-latest.json')
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / 'research').resolve()) or output.suffix != '.json':
        parser.error('--output must be a JSON report inside project research')
    with tempfile.TemporaryDirectory() as temporary:
        report = audit_progression(Store(Path(temporary) / 'audit.sqlite3'))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('configuredNormalDungeons', 'reachableNormalDungeons',
        'renewableMaterialCount', 'blockedConfiguredDungeons')}, ensure_ascii=False))
    print('Material-supported ordinary monsters:', len(report['materialSupportedOrdinaryMonsterIds']),
          'remaining branches with material gaps:', len(report['ordinaryBranchMaterialGaps']))


if __name__ == '__main__':
    main()
