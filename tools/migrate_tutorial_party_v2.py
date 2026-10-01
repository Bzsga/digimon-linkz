"""Repair only the original local, unsettled tutorial party; keep an SQLite backup.

Run while the local server/client are stopped. Does not import another account.
"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
from linkz_server import Store


def main():
    store = Store(ROOT / 'runtime/linkz.sqlite3')
    with store.connect() as db:
        candidates = []
        for row in db.execute('SELECT id,state FROM accounts'):
            state = json.loads(row['state'])
            party = state.get('tutorial_party', {})
            if party.get('giftMonsterIds') != ['121', '196']:
                continue
            battle = state.get('active_battle', {})
            assert state['tutorial']['statusId'] == '900' and not battle.get('settled')
            ident = party['userMonsterIds'][1]
            monster = next(m for m in state['monsters'] if m['userMonsterId'] == ident)
            assert (monster['monsterId'], monster['level'], monster['ex']) == ('121', '1', '0')
            assert not state.get('tutorial_battle_clear')
            master = next(m for m in store.masters['990102']['monsterM'] if m['monsterId'] == '176')
            route = next(r for r in store.masters['990110']['monsterEvolutionRouteM'] if r['growthMonsterId'] == '176')
            skill = next(r for r in store.masters['990108']['skillM']
                         if r['skillGroupId'] == master['skillGroupId'] and r['skillGroupSubId'] == '1')
            state['tutorial_party_v1_archive'] = {'party': dict(party), 'monster': dict(monster), 'battle': battle}
            monster.update(monsterId='176', uniqueSkillId=skill['skillId'], speed=master['speed'],
                           monsterEvolutionRouteId=route['monsterEvolutionRouteId'])
            for field in ('hp', 'attack', 'defense', 'spAttack', 'spDefense'):
                monster[field] = master['default' + field[0].upper() + field[1:]]
            party['giftMonsterIds'] = ['176', '196']
            state.pop('active_battle', None)
            candidates.append((json.dumps(state), row['id']))
        if not candidates:
            print('No matching unsettled v1 tutorial party; no changes.')
            return
        backup = ROOT / 'runtime/backups' / ('before-tutorial-party-v2-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.sqlite3')
        backup.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(backup) as target:
            db.backup(target)
        db.executemany('UPDATE accounts SET state=? WHERE id=?', candidates)
        db.executemany('DELETE FROM retries WHERE token=(SELECT token FROM accounts WHERE id=?)',
                       [(ident,) for _, ident in candidates])
        print(f'Repaired {len(candidates)} unsettled tutorial party; backup: {backup}')


if __name__ == '__main__':
    main()
