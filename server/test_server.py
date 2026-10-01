import copy
import json
import secrets
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from linkz_server import DEFAULT_OPTIONS, Handler, Store, ThreadingHTTPServer, encode, signature, refresh_stamina, initialize_player_state


class ServerTest(unittest.TestCase):
    def test_native_zero_round_cancel_and_timeout_receipts_release_battle(self):
        from unittest.mock import patch
        self.prepare_friendship()
        now = int(time.time())
        with patch('linkz_server.time.time', return_value=now):
            for offset, clear in enumerate((0, 2), 200):
                start = self.api({'090002': {'uniqueRequestId': offset, 'dungeonId': '1', 'deckNum': '1'}})['resData']['090002']
                before = self.talent_state()
                request = self.battle_result_request(start, offset, clear)
                # BattleDataStore.RequestWorldStartDataDelete has default round0,
                # aliveInfo[0,0,0] and zeroed arrays for every returned floor.
                # BattleStateTimeOver uses round0 and DataMng.ClearFlag.Excess2.
                request.update(clearRound=0, aliveInfo=[0, 0, 0] if clear == 0 else [1, 0, 1],
                               enemyAliveInfo=[[0] * len(f['enemy']) for f in start['dungeonFloor']])
                self.assertEqual(self.api({'090003': request, '999999': {'uniqueRequestId': offset}})['venus_status'], 2)
                self.assertEqual(self.talent_state(), before)
                result = self.api({'090003': request})
                self.assertEqual(result['venus_status'], 1, result)
                row = result['resData']['090003']
                self.assertEqual((row['clearType'], row['totalExp'], row['totalMoney']), (0, 0, 0))
                self.assertEqual((row['dungeonReward'], row['dropReward']), ([], []))
                after = self.talent_state()
                for key in ('player', 'monsters', 'souls', 'decks'):
                    self.assertEqual(after[key], before[key])
                self.assertEqual(after.get('cleared_dungeons', []), before.get('cleared_dungeons', []))
                self.assertNotIn('friendship_gains', after['active_battle'])
                self.assertTrue(after['active_battle']['settled'])
                self.assertEqual(self.api({'090012': {'uniqueRequestId': offset}})['resData']['090012'],
                                 {'startId': None, 'userDungeonTicketId': None})
                self.assertEqual(self.api({'090003': request}), result)
                self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
                self.auth()
                self.assertEqual(self.api({'090003': dict(request, uniqueRequestId=offset + 400)})['resData']['090003'], row)
                self.assertEqual(self.api({'090003': dict(request, uniqueRequestId=offset + 500, clear=1)})['venus_status'], 2)
            # A fresh start archives the completed encounter. The older cancel
            # receipt still works after compaction and another authentication.
            self.api({'090002': {'uniqueRequestId': 900, 'dungeonId': '1', 'deckNum': '1'}})
            first = next(b for b in self.talent_state()['battle_history'].values() if b['result_input']['clear'] == 0)
            self.assertNotIn('alive_slot_indices', first)
            self.auth()
            self.assertEqual(self.api({'090003': dict(first['result_input'], uniqueRequestId=901)})['resData']['090003'], first['result'])

    def test_zero_round_victory_and_invalid_rounds_do_not_settle(self):
        from unittest.mock import patch
        self.prepare_friendship()
        with patch('linkz_server.time.time', return_value=int(time.time())):
            start = self.api({'090002': {'uniqueRequestId': 200, 'dungeonId': '1', 'deckNum': '1'}})['resData']['090002']
            before = self.talent_state()
            base = self.battle_result_request(start, 201)
            base['aliveInfo'] = [1, 0, 1]
            for index, invalid in enumerate((-1, True, 0.0, '0', 10000, None), 201):
                result = self.api({'090003': dict(base, uniqueRequestId=index, clear=0, clearRound=invalid)})
                self.assertEqual(result['venus_status'], 2, result)
                self.assertEqual(self.talent_state(), before)
            result = self.api({'090003': dict(base, uniqueRequestId=220, clearRound=0)})
            self.assertEqual(result['venus_status'], 2, result)
            self.assertEqual(self.talent_state(), before)
            self.assertEqual(self.api({'090003': dict(base, uniqueRequestId=221)})['venus_status'], 1)

    def test_native_three_slot_alive_projection_preserves_sparse_deck_members(self):
        from unittest.mock import patch
        self.prepare_friendship()
        state = self.talent_state()
        state['decks']['deckList'][0]['monsterList'] = [
            {'userMonsterId': '1'}, {'userMonsterId': '0'}, {'userMonsterId': '3'}]
        self.save_talent_state(state)
        random_draw = secrets.randbelow
        with patch('linkz_server.time.time', return_value=int(time.time())), patch(
                'linkz_server.secrets.randbelow', side_effect=lambda bound: 0 if bound == 10000 else random_draw(bound)):
            start = self.api({'090002': {'uniqueRequestId': 200, 'dungeonId': '1', 'deckNum': '1'}})['resData']['090002']
            self.assertEqual(start['deck']['userMonsterIds'], ['1', '3'])
            before = self.talent_state()
            request = self.battle_result_request(start, 201)
            request['aliveInfo'] = [1, 0, 1]
            illegal = self.api({'090003': dict(request, aliveInfo=[1, 1, 1])})
            self.assertEqual(illegal['venus_status'], 2, illegal)
            self.assertEqual(self.talent_state(), before)
            result = self.api({'090003': request})
            self.assertEqual(result['venus_status'], 1, result)
            monsters = {m['userMonsterId']: m for m in self.talent_state()['monsters']}
            original = {m['userMonsterId']: m for m in before['monsters']}
            for ident in ('1', '3'):
                self.assertGreater(int(monsters[ident]['ex']), int(original[ident]['ex']))
                self.assertEqual(monsters[ident]['friendship'], '1')
            self.assertEqual(monsters['2'], original['2'])
            self.assertEqual(self.talent_state()['active_battle']['friendship_gains'], {'1': 1, '3': 1})
            self.assertEqual(self.api({'090003': request}), result)
            # Compact protocol callers retain the existing two-member contract.
            again = self.api({'090002': {'uniqueRequestId': 202, 'dungeonId': '1', 'deckNum': '1'}})['resData']['090002']
            compact = self.battle_result_request(again, 203)
            compact['aliveInfo'] = [0, 1]
            self.assertEqual(self.api({'090003': compact})['venus_status'], 1)
            after = {m['userMonsterId']: m for m in self.talent_state()['monsters']}
            self.assertEqual(after['1'], monsters['1'])
            self.assertEqual(after['3']['friendship'], '2')

    def test_native_cancel_three_zero_slots_preserves_legacy_partial_encounter(self):
        from unittest.mock import patch
        self.prepare_normal_battle()
        with patch('linkz_server.time.time', return_value=int(time.time())):
            start = self.api({'090002': {'uniqueRequestId': 200, 'dungeonId': '1', 'deckNum': '1'}})['resData']['090002']
            state = self.talent_state()
            state['active_battle'].pop('alive_slot_indices', None)
            self.save_talent_state(state)
            before = self.talent_state()
            request = self.battle_result_request(start, 201, 0)
            request.update(clearRound=0, aliveInfo=[0, 0, 0])
            result = self.api({'090003': request})
            self.assertEqual(result['venus_status'], 1, result)
            after = self.talent_state()
            self.assertEqual(after['monsters'], before['monsters'])
            self.assertEqual(after['player'], before['player'])
            self.assertTrue(after['active_battle']['settled'])

    def test_fixed_reward_unique_birth_route_ambiguity_rollback_and_reauth(self):
        self.prepare_training()
        store = self.httpd.store
        original = next(r for r in store.masters['990909']['worldDungeonRewardM']
                        if r['worldDungeonRewardId'] == '8238')
        # Original event reward, queued only as a temporary gift fixture. The
        # event itself is not implemented and has not been cleared in a client.
        ordinary = {'assetCategoryId': '1', 'assetValue': '756', 'assetNum': '1',
                    'exValue': '{"monsterFixedValueId":1104}'}
        ids = self.inject_gifts([original, ordinary])
        request = {'uniqueRequestId': 2001, 'receiveType': 2, 'receiveIds': ids}
        before = self.talent_state()
        self.assertEqual(self.api({'070002': request, '999999': {'uniqueRequestId': 2001}})['venus_status'], 2)
        self.assertEqual(self.talent_state(), before)
        claimed = self.api({'070002': request})
        self.assertEqual(claimed['venus_status'], 1, claimed)
        self.assertEqual(claimed['resData']['070002']['prizeReceiveIds'], ids)
        state = self.talent_state()
        special, unknown = state['monsters'][-2:]
        self.assertEqual((special['monsterId'], special['monsterEvolutionRouteId'], special['level']), ('10021', '2001', '1'))
        self.assertEqual((special['commonSkillId'], special['defaultSkillGroupSubId']), ('572', '1'))
        self.assertEqual(unknown['monsterEvolutionRouteId'], '0')
        self.assertEqual(state['chip_slots'][-2]['manage']['maxExtraSlotNum'], 5)
        self.assertEqual(self.api({'070002': request}), claimed)
        # Never choose the first route when the source table is ambiguous.
        routes = store.masters['990110']['monsterEvolutionRouteM']
        duplicate = dict(next(r for r in routes if r['monsterEvolutionRouteId'] == '2001'),
                         monsterEvolutionRouteId='temporary-ambiguous-birth')
        routes.append(duplicate)
        try:
            ambiguous = self.inject_gifts([original])
            result = self.api({'070002': {'uniqueRequestId': 2002, 'receiveType': 2, 'receiveIds': ambiguous}})
            self.assertEqual(result['venus_status'], 1, result)
            self.assertEqual(self.talent_state()['monsters'][-1]['monsterEvolutionRouteId'], '0')
        finally:
            routes.remove(duplicate)
        expected = self.talent_state()
        self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
        self.auth()
        replay = self.api({'070002': dict(request, uniqueRequestId=2003)})
        self.assertEqual(replay['resData']['070002']['prizeReceiveIds'], ids)
        after = self.talent_state()
        self.assertEqual(after['monsters'], expected['monsters'])
        self.assertEqual(after['chip_slots'], expected['chip_slots'])
        self.assertEqual(after['player']['gamemoney'], expected['player']['gamemoney'])
        self.assertEqual(after['monsters'][-1]['monsterEvolutionRouteId'], '0')

    def test_mode_version_second_skill_research_birth_chips_and_reauthentication(self):
        from datetime import datetime, timezone
        from unittest.mock import patch
        store = self.httpd.store
        masters = {r['monsterId']: r for r in store.masters['990102']['monsterM']}
        experience = next(r['experienceNum'] for r in store.masters['990106']['monsterExperienceM'] if r['level'] == '80')
        self.prepare_mode(mid='1000', experience=int(experience))
        state = self.talent_state()
        # All parent species/EXP/coins/materials are isolated fixtures. This
        # checks lawful API continuity; it does not prove natural +4 acquisition.
        state['monsters'][0]['monsterEvolutionRouteId'] = '110'  # original111 family +4
        for deck in state['decks']['deckList']:
            deck['monsterList'] = [{'userMonsterId': '0'} for _ in range(3)]
        donor = next(m for m in state['monsters'] if m['userMonsterId'] == '2')
        donor['ex'] = next(r['experienceNum'] for r in store.masters['990106']['monsterExperienceM']
                          if r['level'] == masters[donor['monsterId']]['maxLevel'])
        store.add_monster_experience(donor, 0)
        partner = next(m for m in state['monsters'] if m['userMonsterId'] == '3')
        partner.update(monsterId='866', ex='0')
        store.add_monster_experience(partner, 0)
        version_route = next(r for r in store.masters['990104']['monsterEvolutionM']
                             if r['baseMonsterId'] == '1175' and r['type'] == '3')
        recipe = next(r for r in store.masters['990115']['monsterEvolutionMaterialM']
                      if r['monsterEvolutionMaterialId'] == version_route['monsterEvolutionMaterialId'])
        selected, counts = [], {}
        for slot in range(1, 8):
            sid, amount = recipe['materialAssetValue' + str(slot)], int(recipe['materialAssetNum' + str(slot)])
            if amount:
                selected.append(int(sid))
                counts[sid] = counts.get(sid, 0) + amount
        state['souls'] = [{'soulId': sid, 'num': str(amount)} for sid, amount in counts.items()]
        self.save_talent_state(state)
        sequence = 1000
        def call(api, **params):
            nonlocal sequence
            sequence += 1
            return self.chip_call(api, sequence, **params)
        before = self.talent_state()
        changed = call('010301', userMonsterId='1', monsterId=1175)['userMonster']
        self.assertEqual((changed['monsterId'], changed['level'], changed['monsterEvolutionRouteId']), ('1175', '80', '110'))
        self.assertEqual(self.talent_state()['player']['gamemoney'], '940000')
        self.assertEqual(self.talent_state()['chips'], before['chips'])
        call('010001', userMonsterId='1', setFlg='0')
        version_request = {'uniqueRequestId': sequence + 1, 'baseUserMonsterId': 1,
                           'target': int(version_route['nextMonsterId']), 'material': selected}
        version = self.api({'010802': version_request})
        sequence += 1
        self.assertEqual(version['venus_status'], 1, version)
        upgraded = version['resData']['010802']['userMonster']
        ident = upgraded['userMonsterId']
        self.assertEqual(upgraded['monsterId'], '20341')
        self.assertNotEqual(ident, '1')
        self.assertEqual(upgraded['monsterEvolutionRouteId'], '110')
        self.assertEqual(self.talent_state()['player']['gamemoney'], '805000')
        self.assertTrue(all(r['num'] == '0' for r in self.talent_state()['souls']))
        self.assertEqual(self.talent_state()['chips'][0]['userMonsterId'], 0)
        self.assertNotIn('1', self.talent_state()['friendship_ready_at'])
        self.assertEqual(self.talent_state()['friendship_ready_at'][ident], before['friendship_ready_at']['1'])
        inherited = call('010601', baseUserMonsterId=ident, materialUserMonsterId='2',
                         baseCommonSkillNumber=2, materialCommonSkillNumber=1)['userMonster']
        self.assertEqual(inherited['extraCommonSkillId'], donor['commonSkillId'])
        call('180004', userMonsterId=int(ident), dispNum=1)
        chip_id = self.talent_state()['chips'][0]['userChipId']
        call('180002', act=1, type=1, dispNum=1, userChipId=chip_id, userMonsterId=int(ident))
        before_mode = self.talent_state()
        version_changed = call('010301', userMonsterId=ident, monsterId=20166)['userMonster']
        current = next(m for m in self.talent_state()['monsters'] if m['userMonsterId'] == ident)
        self.assertEqual(current['extraCommonSkillId'], donor['commonSkillId'])
        self.assertEqual(current['level'], '80')
        self.assertEqual(current['ex'], upgraded['ex'])
        self.assertEqual(current['monsterEvolutionRouteId'], '110')
        self.assertEqual(self.talent_state()['chips'], before_mode['chips'])
        self.assertEqual(self.talent_state()['chip_slots'], before_mode['chip_slots'])
        self.assertEqual(int(before_mode['player']['gamemoney']) - int(self.talent_state()['player']['gamemoney']), 108000)
        self.assertEqual(int(version_changed['attack']), int(current['attack']) + 100)
        self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
        self.auth()
        before_replay = self.talent_state()
        replay_version = self.api({'010802': dict(version_request, uniqueRequestId=sequence + 1)})
        sequence += 1
        self.assertEqual(replay_version['venus_status'], 1, replay_version)
        self.assertEqual(replay_version['resData']['010802']['userMonster']['monsterId'], '20166')
        replay_mode = call('010301', userMonsterId=ident, monsterId=20166)
        self.assertEqual(replay_mode['userMonster']['extraCommonSkillId'], donor['commonSkillId'])
        self.assertEqual(self.talent_state()['player']['gamemoney'], before_replay['player']['gamemoney'])
        self.assertEqual(self.talent_state()['souls'], before_replay['souls'])
        research_request = {'uniqueRequestId': sequence + 1, 'baseUserMonsterId': ident, 'materialUserMonsterId': '3'}
        failed = self.api({'010501': research_request, '999999': {'uniqueRequestId': sequence + 1}})
        self.assertEqual(failed['venus_status'], 2)
        self.assertEqual(self.talent_state(), before_replay)
        research = self.api({'010501': research_request})
        sequence += 1
        self.assertEqual(research['venus_status'], 1, research)
        egg = research['resData']['010501']['userMonster']
        offspring = egg['userMonsterId']
        self.assertEqual((egg['monsterId'], egg['monsterEvolutionRouteId'], egg['eggFlg']), ('20', '110', '1'))
        self.assertEqual((egg['friendship'], egg['extraCommonSkillId'], egg['hpAbility']), ('0', '', '5'))
        after_research = self.talent_state()
        self.assertEqual(int(before_replay['player']['gamemoney']) - int(after_research['player']['gamemoney']), 14500)
        self.assertEqual(after_research['chips'][0]['userMonsterId'], 0)
        self.assertNotIn(ident, after_research['friendship_ready_at'])
        self.assertNotIn(ident, [m['userMonsterId'] for m in after_research['monsters']])
        call('010304', userMonsterId=offspring)
        for _ in range(2):
            child = next(m for m in self.talent_state()['monsters'] if m['userMonsterId'] == offspring)
            deadline = int(datetime.strptime(child['growEndDate'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).timestamp())
            with patch('linkz_server.time.time', return_value=deadline + 1):
                growth = call('010302', userMonsterId=offspring, shorteningFlg=0, stone=0)
            self.assertEqual(growth['useStone'], 0)
        child = next(m for m in self.talent_state()['monsters'] if m['userMonsterId'] == offspring)
        self.assertEqual((child['monsterId'], child['monsterEvolutionRouteId']), ('115', '110'))
        self.assertEqual((child['hpAbility'], child['extraCommonSkillId'], child['friendship']), ('5', '', '0'))
        # Keep the synthetic clock forward during reconnect; returning to real
        # time after crossing UTC+8 midnight would legitimately reset the day.
        with patch('linkz_server.time.time', return_value=deadline + 1):
            self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
            self.auth()
            before_final = self.talent_state()
            replay = self.api({'010501': dict(research_request, uniqueRequestId=sequence + 1)})
            sequence += 1
            self.assertEqual(replay['resData']['010501']['userMonster']['monsterId'], '115')
            for api, params in (('010301', {'userMonsterId': ident, 'monsterId': 20166}),
                                ('010802', dict(version_request))):
                params['uniqueRequestId'] = sequence + 1
                sequence += 1
                self.assertEqual(self.api({api: params})['venus_status'], 2)
            self.assertEqual(self.talent_state(), before_final)

    def prepare_mode(self, mid='996', sub='2', experience=230):
        self.prepare_chips(amount=1)
        store = self.httpd.store
        state = self.talent_state()
        monster = state['monsters'][0]
        master = next(r for r in store.masters['990102']['monsterM'] if r['monsterId'] == mid)
        skill = next(r for r in store.masters['990108']['skillM']
                     if r['skillGroupId'] == master['skillGroupId'] and r['skillGroupSubId'] == sub)
        monster.update(monsterId=mid, ex=str(experience), defaultSkillGroupSubId=sub,
                       uniqueSkillId=skill['skillId'], friendship='60', luck='7',
                       hpAbility='5', hpAbilityFlg='2', statusFlgs='3')
        store.add_monster_experience(monster, 0)
        state['player']['gamemoney'] = '1000000'
        state['friendship_ready_at'] = {'1': 2000000000}
        state['souls'] = []
        store.chip_inventory.refresh_slots(state)
        slot = next(s for s in state['chip_slots'] if s['userMonsterId'] == 1)
        slot['manage']['extraSlotNum'] = 1
        slot['equip'] = [{'type': 1, 'dispNum': 1, 'userChipId': state['chips'][0]['userChipId']}]
        state['chips'][0]['userMonsterId'] = 1
        self.save_talent_state(state)
        return state


    def test_mode_all_48_routes_subskills_experience_and_cost(self):
        original = self.prepare_mode()
        store = self.httpd.store
        masters = {r['monsterId']: r for r in store.masters['990102']['monsterM']}
        levels = {r['level']: int(r['experienceNum']) for r in store.masters['990106']['monsterExperienceM']}
        routes = [r for r in store.masters['990104']['monsterEvolutionM'] if (r['type'], r['effectType']) == ('2', '2')]
        self.assertEqual(len(routes), 48)
        for route in routes:
            for sub in ('1', '2'):
                for level in ('1', '10', masters[route['baseMonsterId']]['maxLevel']):
                    with self.subTest(route=route['monsterEvolutionId'], sub=sub, level=level):
                        state = copy.deepcopy(original)
                        monster = state['monsters'][0]
                        monster.update(monsterId=route['baseMonsterId'], defaultSkillGroupSubId=sub,
                                       ex=str(levels[level] + (1 if level == '10' else 0)))
                        store.add_monster_experience(monster, 0)
                        before = copy.deepcopy(monster)
                        result = store.dispatch('010301', {'userMonsterId': '1', 'monsterId': int(route['nextMonsterId'])}, {'id': 1}, state)
                        for field in ('userMonsterId', 'level', 'ex', 'levelEx', 'nextLevelEx', 'friendship', 'luck',
                                      'hpAbility', 'hpAbilityFlg', 'monsterEvolutionRouteId', 'commonSkillId', 'leaderSkillId'):
                            self.assertEqual(monster[field], before[field], field)
                        destination = masters[route['nextMonsterId']]
                        expected_skill = next(s for s in store.masters['990108']['skillM']
                            if s['skillGroupId'] == destination['skillGroupId'] and s['skillGroupSubId'] == sub)
                        self.assertEqual(monster['uniqueSkillId'], expected_skill['skillId'])
                        self.assertEqual(state['player']['gamemoney'], str(1000000 - 240 * int(store.masters['990701']['codeM']['modeChangeCoefficientRare' + destination['rare']])))
                        self.assertEqual(result['reviewStatus'], 0)
                        self.assertEqual(state['souls'], [])
                        self.assertEqual(state['chips'], original['chips'])
                        self.assertEqual(state['chip_slots'], original['chip_slots'])
                        self.assertEqual(state['decks'], original['decks'])
                        self.assertEqual(state['friendship_ready_at'], original['friendship_ready_at'])
                        self.assertEqual(state.get('normal_evolution_count', 0), original.get('normal_evolution_count', 0))


    def test_mode_protocol_slots_replay_reverse_and_reauth(self):
        original = self.prepare_mode()
        request = {'uniqueRequestId': 201, 'userMonsterId': '1', 'monsterId': 1171}
        batch = {'010301': request, '180006': {'uniqueRequestId': 201, 'userMonsterId': [1]}}
        first = self.api(batch)
        self.assertEqual(first['venus_status'], 1, first)
        self.assertEqual(first['resData']['010301']['userMonster']['monsterId'], '1171')
        # Original 1171 level10 stats, 5% HP medal, friendship60 and +100 ATK chip.
        # Stored stats exclude chips; protocol projections add their effect once.
        changed = self.talent_state()['monsters'][0]
        self.assertEqual((changed['hp'], changed['attack'], changed['speed']), ('901', '1741', '178'))
        self.assertEqual(first['resData']['010301']['userMonster']['attack'], '1841')
        self.assertEqual(first['resData']['180006']['slotInfo'][0]['equip'], original['chip_slots'][0]['equip'])
        self.assertEqual(self.api(batch), first)
        state = self.talent_state()
        self.assertEqual(state['player']['gamemoney'], '992800')
        self.assertEqual(state['normal_mode_change_count'], 1)
        self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
        self.auth()
        replay = self.api({'010301': dict(request, uniqueRequestId=202)})
        self.assertEqual(replay['venus_status'], 1, replay)
        self.assertEqual(self.talent_state()['player']['gamemoney'], '992800')
        # A later level/skill update must not be overwritten by a mode receipt.
        self.chip_call('010202', 203, mealMonster='1', meatNum=1)
        current = self.talent_state()['monsters'][0]
        replay = self.chip_call('010301', 204, userMonsterId='1', monsterId=1171)
        self.assertEqual(replay['userMonster']['ex'], current['ex'])
        self.assertEqual(replay['userMonster']['commonSkillId'], current['commonSkillId'])
        self.assertEqual(replay['userMonster']['attack'], str(int(current['attack']) + 100))
        self.assertEqual(self.talent_state()['monsters'][0], current)
        reverse = self.chip_call('010301', 205, userMonsterId='1', monsterId=996)
        self.assertEqual(reverse['userMonster']['monsterId'], '996')
        again = self.chip_call('010301', 206, userMonsterId='1', monsterId=1171)
        self.assertEqual(again['userMonster']['monsterId'], '1171')
        self.assertEqual(self.talent_state()['normal_mode_change_count'], 3)
        self.assertEqual(self.talent_state()['player']['gamemoney'], '978400')


    def test_mode_invalid_funds_battle_skill_and_batch_rollback(self):
        original = self.prepare_mode()
        request = {'uniqueRequestId': 301, 'userMonsterId': '1', 'monsterId': 1171}
        for changes in ({'userMonsterId': 1}, {'monsterId': True}, {'monsterId': '1171'}, {'monsterId': 851}):
            self.assertEqual(self.api({'010301': dict(request, **changes)})['venus_status'], 2)
            self.assertEqual(self.talent_state(), original)
        self.assertEqual(self.api({'010301': request, '999999': {'uniqueRequestId': 301}})['venus_status'], 2)
        self.assertEqual(self.talent_state(), original)

        for kind in ('funds', 'battle', 'skill', 'egg', 'tutorial'):
            state = copy.deepcopy(original)
            if kind == 'funds': state['player']['gamemoney'] = '7199'
            if kind == 'battle': state['active_battle'] = {'settled': False}
            if kind == 'skill': state['monsters'][0]['defaultSkillGroupSubId'] = '999'
            if kind == 'egg': state['monsters'][0]['eggFlg'] = '1'
            if kind == 'tutorial': state['tutorial']['endFlg'] = '0'
            self.save_talent_state(state)
            failure = self.api({'010301': request})
            self.assertEqual(failure['venus_status'], 2, kind)
            if kind == 'funds':
                self.assertEqual(failure['message'], '模式变化所需的金币不足。')
            if kind == 'skill':
                self.assertEqual(failure['message'], '目标形态的固有技能资料不完整，暂不能进行进化或模式变化。')
            self.assertEqual(self.talent_state(), state, kind)
        self.save_talent_state(original)
        # Already being in a form without its receipt is not a free mode change.
        self.assertEqual(self.api({'010301': dict(request, monsterId=996)})['venus_status'], 2)
        self.assertEqual(self.talent_state(), original)


    def test_mode_added_resistance_preservation_collision_and_transaction(self):
        from unittest.mock import patch
        from linkz_server import ATTRIBUTE_RESISTANCE_FIELDS, STATUS_RESISTANCE_FIELDS
        original = self.prepare_mode()
        store = self.httpd.store
        masters = {r['monsterId']: r for r in store.masters['990102']['monsterM']}
        resistances = {r['monsterResistanceId']: r for r in store.masters['990107']['monsterResistanceM']}
        routes = [r for r in store.masters['990104']['monsterEvolutionM'] if (r['type'], r['effectType']) == ('2', '2')]
        for kind, field, fields in ((1, 'tranceResistance', ATTRIBUTE_RESISTANCE_FIELDS),
                                    (2, 'tranceStatusAilment', STATUS_RESISTANCE_FIELDS)):
            route = next(r for r in routes if any(int(resistances[masters[r['nextMonsterId']]['resistanceId']][f]) > 0 for f in fields))
            destination = masters[route['nextMonsterId']]
            candidates = store.resistance_candidates(destination, kind)
            for collision in (False, True):
                state = copy.deepcopy(original)
                state['monsters'][0].update(monsterId=route['baseMonsterId'], ex='230',
                    tranceResistance='', tranceStatusAilment='')
                state['monsters'][0][field] = destination['resistanceId'] if collision else candidates[0]
                store.add_monster_experience(state['monsters'][0], 0)
                self.save_talent_state(state)
                request = {'uniqueRequestId': 401, 'userMonsterId': '1', 'monsterId': int(route['nextMonsterId'])}
                with patch('linkz_server.secrets.choice', return_value=candidates[0]) as draw:
                    self.assertEqual(self.api({'010301': request, '999999': {'uniqueRequestId': 401}})['venus_status'], 2)
                    self.assertEqual(draw.call_count, int(collision))
                self.assertEqual(self.talent_state(), state)
                with patch('linkz_server.secrets.choice', return_value=candidates[0]) as draw:
                    response = self.api({'010301': request})
                    self.assertEqual(response['venus_status'], 1, response)
                    self.assertEqual(draw.call_count, int(collision))
                self.assertEqual(response['resData']['010301']['userMonster'][field], candidates[0])
                with patch('linkz_server.secrets.choice', side_effect=AssertionError('mode replay resampled resistance')):
                    self.assertEqual(self.api({'010301': request}), response)
                    self.auth()
                    replay = self.chip_call('010301', 402, userMonsterId='1', monsterId=int(route['nextMonsterId']))
                self.assertEqual(replay['userMonster'][field], candidates[0])
                # Restore authentication session before another source fixture.
                self.auth()


    def test_mode_concurrent_retry_owner_and_consumed_monster(self):
        from concurrent.futures import ThreadPoolExecutor
        self.prepare_mode()
        request = {'uniqueRequestId': 501, 'userMonsterId': '1', 'monsterId': 1171}
        with ThreadPoolExecutor(max_workers=3) as workers:
            responses = list(workers.map(lambda _: self.api({'010301': request}), range(3)))
        self.assertEqual(responses[0]['venus_status'], 1, responses[0])
        self.assertTrue(all(r == responses[0] for r in responses))
        self.assertEqual(self.talent_state()['player']['gamemoney'], '992800')
        self.assertEqual(self.talent_state()['normal_mode_change_count'], 1)
        original_owner = self.talent_state()
        self.auth('other-mode-owner')
        self.assertEqual(self.api({'010301': dict(request, uniqueRequestId=502)})['venus_status'], 2)
        self.assertEqual(self.talent_state(), original_owner)
        self.auth()
        state = self.talent_state()
        # Simulate a later lawful consumption. No receipt may recreate that ID.
        for deck in state['decks']['deckList']:
            for row in deck['monsterList']:
                if row['userMonsterId'] == '1': row['userMonsterId'] = '0'
        self.httpd.store.consume_monsters(state, ['1'], 'later-mode-fixture-consumption')
        self.save_talent_state(state)
        self.auth()
        before = self.talent_state()
        self.assertEqual(self.api({'010301': dict(request, uniqueRequestId=503)})['venus_status'], 2)
        self.assertEqual(self.talent_state(), before)
        self.assertTrue(all(m['userMonsterId'] != '1' for m in before['monsters']))


    def prepare_chips(self, amount=12, chip_id=1):
        self.prepare_training()
        state = self.talent_state()
        # Isolated protocol fixture; separate test earns these through APIs.
        state['items'] = [{'itemId': 6, 'userItemNum': 76}, {'itemId': 7, 'userItemNum': 10}]
        self.httpd.store.chip_inventory.grant(state, chip_id, amount) if amount else None
        self.save_talent_state(state)

    def chip_call(self, api, request_id, **param):
        result = self.api({api: dict(param, uniqueRequestId=request_id)})
        self.assertEqual(result['venus_status'], 1, result)
        return result['resData'][api]

    def test_advanced_chips_api_earned_factory_unlock_payment_and_replay(self):
        from unittest.mock import patch
        self.prepare_training()
        state = self.talent_state()
        # Tutorial/one partner/stamina/predecessor clears are explicit fixtures.
        # Key, factory coins, final clear83, Link and chips are earned via APIs.
        state['player'].update(gamemoney='0', point=0, friendPoint='0', stamina=10000, staminaMax='10000')
        state['decks']['deckList'][0]['monsterList'] = [{'userMonsterId': '1'}]
        state['cleared_dungeons'] = [str(i) for i in range(1, 83) if i != 12]
        state['chips'] = []; state['items'] = []; state['facility_keys'] = {}
        self.save_talent_state(state)
        sequence = 100
        def call(api, **param):
            nonlocal sequence
            sequence += 1
            return self.chip_call(api, sequence, **param)
        def win(dungeon):
            start = call('090002', dungeonId=str(dungeon), deckNum='1')
            request = self.battle_result_request(start, sequence + 1)
            request.pop('uniqueRequestId')
            return call('090003', **request)
        win(12)
        facility = call('140001', facilityId=25, positionX=10, positionY=10)['userFacilityId']
        call('140010', userFacilityId=facility)
        self.assertNotIn('10004', [r['gachaId'] for r in call('050001', isTutorial=0)['result']])
        win(83)
        menu = {r['gachaId']: r for r in call('050001', isTutorial=0)['result']}
        self.assertEqual(set(menu), {'10001', '10002', '10003', '10004'})
        self.assertEqual(menu['10004']['prize'], '17')
        self.assertEqual(menu['10004']['priceType']['category'], '3')
        self.assertEqual([r['price'] for r in menu['10004']['details']], ['1000', '10000'])
        self.assertLess(int(menu['10004']['dispNum']), min(int(menu[i]['dispNum']) for i in ('10001', '10002', '10003')))
        for _ in range(200):
            if int(self.talent_state()['player']['friendPoint']) >= 2000:
                break
            win(83)
        before = int(self.talent_state()['player']['friendPoint'])
        self.assertGreaterEqual(before, 2000)
        request = dict(uniqueRequestId=sequence + 1, gachaId=10004, playCount=1)
        with patch('chip_inventory.secrets.randbelow', side_effect=[9900, 5]):
            first = self.api({'050012': request})
        self.assertEqual(first['venus_status'], 1, first)
        self.assertEqual(first['resData']['050012']['userAssetList'][0]['assetValue'], '58')
        self.assertEqual(self.api({'050012': request}), first)
        self.assertEqual(int(self.talent_state()['player']['friendPoint']), before - 1000)
        # Ordinary capture has session receipts. A new capture after auth is paid.
        self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
        self.auth()
        with patch('chip_inventory.secrets.randbelow', side_effect=[9000, 0]):
            second = self.chip_call('050012', sequence + 2, gachaId=10004, playCount=1)
        self.assertEqual(second['userAssetList'][0]['assetValue'], '7')
        self.assertEqual(int(self.talent_state()['player']['friendPoint']), before - 2000)
        self.assertEqual([c['chipId'] for c in self.talent_state()['chips']], [58, 7])

    def test_advanced_chips_rank_thresholds_all_families_and_ten_pull(self):
        from unittest.mock import patch
        self.prepare_chips(amount=0)
        state = self.talent_state()
        state['facilities'].append(dict(userFacilityId=99, facilityId=25, level=1,
                                       positionX=10, positionY=10, completeTime=''))
        state['cleared_dungeons'] = ['83']
        state['player']['friendPoint'] = '100000'
        self.save_talent_state(state)
        bounds = [(2, 0, 1999), (3, 2000, 3999), (4, 4000, 5999), (5, 6000, 7499),
                  (6, 7500, 8999), (7, 9000, 9899), (8, 9900, 9999)]
        request_id = 20
        for rank, low, high in bounds:
            for roll in (low, high):
                for family in range(6):
                    request_id += 1
                    with patch('chip_inventory.secrets.randbelow', side_effect=[roll, family]) as draw:
                        result = self.chip_call('050012', request_id, gachaId=10004, playCount=1)
                    self.assertEqual([c.args[0] for c in draw.call_args_list], [10000, 6])
                    self.assertEqual(result['userAssetList'][0]['assetValue'], str(rank + family * 10))
        with patch('chip_inventory.secrets.randbelow', side_effect=[0, 0] * 10):
            result = self.chip_call('050012', request_id + 1, gachaId=10004, playCount=10)
        self.assertEqual([c['assetValue'] for c in result['userAssetList']], ['2'] * 10)
        self.assertEqual([c['isNew'] for c in result['userAssetList']], [0] * 10)
        self.assertEqual(self.talent_state()['player']['friendPoint'], '6000')
        self.assertEqual(len(self.talent_state()['chips']), 94)
        self.assertEqual(len(set(c['chipId'] for c in self.talent_state()['chips'])), 42)

    def test_advanced_chips_gate_capacity_identity_and_batch_rollback(self):
        from unittest.mock import patch
        import copy
        self.prepare_chips(amount=0)
        original = self.talent_state()
        valid = copy.deepcopy(original)
        valid['cleared_dungeons'] = ['83']
        valid['facilities'].append(dict(userFacilityId=99, facilityId=25, level=1,
                                       positionX=10, positionY=10, completeTime=''))
        self.httpd.store.chip_inventory.refresh_capacity(valid)
        valid['player']['friendPoint'] = '10000'
        request = dict(uniqueRequestId=20, gachaId=10004, playCount=10)
        fixtures = [copy.deepcopy(valid) for _ in range(5)]
        fixtures[0]['cleared_dungeons'] = []
        fixtures[1]['facilities'][-1]['completeTime'] = '2099-01-01 00:00:00'
        fixtures[2]['player']['friendPoint'] = '9999'
        self.httpd.store.chip_inventory.grant(fixtures[3], 1, 291)
        fixtures[4]['chip_id_sequence'] = 2147483640
        for fixture in fixtures:
            self.save_talent_state(fixture)
            before = self.talent_state()
            self.assertEqual(self.api({'050012': request})['venus_status'], 2)
            self.assertEqual(self.talent_state(), before)
        self.save_talent_state(valid)
        before = self.talent_state()
        with patch('chip_inventory.secrets.randbelow', side_effect=[9999, 5] * 10):
            self.assertEqual(self.api({'050012': request, '999999': {'uniqueRequestId': 20}})['venus_status'], 2)
        self.assertEqual(self.talent_state(), before)
        for field, value in (('gachaId', True), ('gachaId', '10004'), ('playCount', True), ('playCount', 0), ('playCount', 11)):
            self.assertEqual(self.api({'050012': dict(request, **{field: value})})['venus_status'], 2)
            self.assertEqual(self.talent_state(), before)
        with patch('chip_inventory.secrets.randbelow', side_effect=[9999, 5] * 10):
            first = self.api({'050012': request})
        self.assertEqual(first['venus_status'], 1, first)
        self.assertEqual(self.api({'050012': request}), first)
        self.assertEqual(self.talent_state()['player']['friendPoint'], '0')
        self.assertEqual([c['chipId'] for c in self.talent_state()['chips']], [58] * 10)

    def test_advanced_chips_invalid_configuration_rejected(self):
        import copy
        from chip_inventory import ChipInventory
        store = self.httpd.store
        original = copy.deepcopy(store.local_gameplay['chips']['advancedGacha'])
        invalid = [True, [], 'invalid']
        for key, value in (('gachaId', True), ('gachaId', 10003), ('linkCostPerChip', 0),
                           ('unlockDungeonId', 84), ('dispNum', 0), ('rankWeightsBasisPoints', {}),
                           ('chipIdsByRank', {})):
            invalid.append(dict(original, **{key: value}))
        wrong_weights = copy.deepcopy(original); wrong_weights['rankWeightsBasisPoints']['8'] = 99
        invalid.append(wrong_weights)
        wrong_rank = copy.deepcopy(original); wrong_rank['chipIdsByRank']['8'][0] = 7
        invalid.append(wrong_rank)
        duplicate = copy.deepcopy(original); duplicate['chipIdsByRank']['8'][-1] = 8
        invalid.append(duplicate)
        malformed = copy.deepcopy(original); malformed['chipIdsByRank']['8'][0] = {}
        invalid.append(malformed)
        try:
            for config in invalid:
                store.local_gameplay['chips']['advancedGacha'] = config
                with self.assertRaises(ValueError):
                    ChipInventory(store, ValueError)
        finally:
            store.local_gameplay['chips']['advancedGacha'] = original

    def test_chips_same_family_across_ranks_rejected_with_replace_and_reauth(self):
        from copy import deepcopy
        from linkz_server import ProtocolError
        self.prepare_chips(amount=0)
        chip = self.httpd.store.chip_inventory
        template = self.talent_state()
        # Two paid slots isolate equip contracts; no formal account is touched.
        template['chip_slots'][0]['manage']['extraSlotNum'] = 2
        for family in range(6):
            for rank in range(2, 9):
                fixture = deepcopy(template)
                basic, advanced, other = [chip.grant(fixture, ident, 1)[0]['userChipId']
                    for ident in (family * 10 + 1, family * 10 + rank, ((family + 1) % 6) * 10 + rank)]
                first = dict(act=1, type=1, dispNum=1, userChipId=basic, userMonsterId=1)
                chip.equip(fixture, first)
                before = deepcopy(fixture)
                with self.assertRaisesRegex(ProtocolError, 'Duplicate chip effect'):
                    chip.equip(fixture, dict(first, dispNum=2, userChipId=advanced))
                self.assertEqual(fixture, before)
                chip.equip(fixture, dict(first, dispNum=2, userChipId=other))
                chip.equip(fixture, dict(first, userChipId=advanced))
                self.assertEqual([c['userMonsterId'] for c in fixture['chips']], [0, 1, 1])
                self.assertEqual(len(fixture['chip_slots'][0]['equip']), 2)
        fixture = deepcopy(template)
        for ident in (1, 8, 11):
            chip.grant(fixture, ident, 1)
        self.save_talent_state(fixture)
        first = dict(act=1, type=1, dispNum=1, userChipId=1, userMonsterId=1)
        self.chip_call('180002', 20, **first)
        before = self.talent_state()
        duplicate = dict(first, dispNum=2, userChipId=2)
        rejected = self.api({'180002': dict(duplicate, uniqueRequestId=21)})
        self.assertEqual(rejected['venus_status'], 2)
        self.assertEqual(rejected['message'], '伙伴已装配同一属性类别的芯片，等级不同也不能同时装备。')
        self.assertEqual(self.talent_state(), before)
        self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
        self.auth()
        reauthenticated = self.api({'180002': dict(duplicate, uniqueRequestId=22)})
        self.assertEqual(reauthenticated['venus_status'], 2)
        self.assertEqual(reauthenticated['message'], rejected['message'])
        self.chip_call('180002', 23, **dict(first, userChipId=2))
        self.chip_call('180002', 24, **dict(first, dispNum=2, userChipId=3))
        self.assertEqual([c['userMonsterId'] for c in self.talent_state()['chips']], [0, 1, 1])

    def test_chips_natural_factory_link_gifts_fusion_and_reconnect(self):
        from unittest.mock import patch
        self.prepare_training()
        state = self.talent_state()
        # Partner/team/tutorial/predecessor clears/stamina are explicit fixtures.
        # Coins, Link, chips, patches and factory key below are native API earned.
        state['player'].update(gamemoney='0', friendPoint='0', stamina=1000, staminaMax='1000')
        state['decks']['deckList'][0]['monsterList'] = [{'userMonsterId': '1'}]
        state['cleared_dungeons'] = [str(i) for i in range(1, 12)]
        state['chips'] = []; state['items'] = []; state['facility_keys'] = {}
        self.save_talent_state(state)
        self.assertEqual(len(self.chip_call('050001', 10, isTutorial=0)['result']), 2)
        self.assertEqual(self.api({'050012': dict(uniqueRequestId=10, gachaId=10003, playCount=1)})['venus_status'], 2)
        sequence = 100
        def call(api, **param):
            nonlocal sequence
            sequence += 1
            return self.chip_call(api, sequence, **param)
        def win(dungeon):
            start = call('090002', dungeonId=str(dungeon), deckNum='1')
            request = self.battle_result_request(start, sequence + 1)
            request.pop('uniqueRequestId')
            return call('090003', **request)
        call('110101', countryCode=3)
        win(12)
        self.assertEqual(self.talent_state()['facility_keys']['11'], 1)
        facility = call('140001', facilityId=25, positionX=10, positionY=10)['userFacilityId']
        self.assertEqual(self.talent_state()['player']['chipLimitMax'], 100)
        call('140010', userFacilityId=facility)
        self.assertEqual(self.talent_state()['player']['chipLimitMax'], 300)
        self.assertEqual(call('050001', isTutorial=0)['result'][-1]['prize'], '17')
        for attempt in range(60):
            if int(self.talent_state()['player']['friendPoint']) >= 1100:
                break
            before = int(self.talent_state()['player']['friendPoint'])
            win(1)
            self.assertGreater(int(self.talent_state()['player']['friendPoint']), before)
        self.assertGreaterEqual(int(self.talent_state()['player']['friendPoint']), 1100)
        call('110102', missionId=[10006, 10007, 10008])
        gifts = call('070001', page=1)['prizeData']
        call('070002', receiveType=2, receiveIds=[r['receiveId'] for r in gifts])
        self.assertEqual(call('020304')['userItemData'], [{'itemId': 7, 'userItemNum': 1}, {'itemId': 6, 'userItemNum': 5}])
        with patch('chip_inventory.secrets.randbelow', return_value=0):
            ten = call('050012', gachaId=10003, playCount=10)
            one = call('050012', gachaId=10003, playCount=1)
        self.assertEqual(len(ten['userAssetList']), 10)
        self.assertEqual(one['userAssetList'][0]['isNew'], 0)
        chips = call('180001')['userChipList']
        self.assertEqual(len(chips), 12)
        call('180004', userMonsterId=1, dispNum=1)
        equipment = dict(type=1, dispNum=1, userChipId=chips[0]['userChipId'], userMonsterId=1)
        equipped = call('180002', act=1, **equipment)
        self.assertEqual(int(equipped['userMonster']['attack']), int(self.talent_state()['monsters'][0]['attack']) + 100)
        param = dict(baseChip=chips[0]['userChipId'], materialChip=[c['userChipId'] for c in chips[1:11]])
        self.assertEqual(call('180003', **param)['userChip']['chipId'], 2)
        self.assertEqual(len(call('180001')['userChipList']), 2)
        call('180002', act=0, **equipment)
        after = self.talent_state()
        self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
        self.auth()
        self.assertEqual(call('180003', **param)['userChip']['chipId'], 2)
        call('180004', userMonsterId=1, dispNum=1)
        call('180002', act=0, **equipment)
        self.assertEqual(self.talent_state()['items'], after['items'])
        self.assertEqual(self.talent_state()['player']['friendPoint'], after['player']['friendPoint'])
        self.assertEqual(call('180005', materialChip=[chips[0]['userChipId']])['itemRecovered'], 0)
        self.assertEqual(self.api({'180003': dict(param, uniqueRequestId=sequence + 10)})['venus_status'], 2)

    def test_chips_slot_scopes_all_basic_ranks_and_noncontiguous_awakenings(self):
        self.prepare_chips(amount=0)
        store, state = self.httpd.store, self.talent_state()
        chip = store.chip_inventory
        self.assertEqual(len(chip.bonus), 48)
        groups = {r['monsterGroupId']: r for r in store.masters['990103']['monsterM']}
        for master in store.masters['990102']['monsterM']:
            monster = dict(state['monsters'][0], monsterId=master['monsterId'])
            step, awake = groups[master['monsterGroupId']]['growStep'], int(master['rare']) - 1
            expected = (step in ('5', '6', '7', '8', '9', '11', '12', '13'), step in ('6', '7', '9', '12', '13'),
                        step in ('7', '9', '13'), awake >= 2, awake >= 4)
            self.assertEqual(chip.free_slots(monster), expected, master['monsterId'])
        for ident, (field, bonus) in chip.bonus.items():
            fixture = json.loads(json.dumps(state))
            store.add_monster_experience(fixture['monsters'][0], 0)
            plain = json.loads(json.dumps(fixture['monsters'][0]))
            owned = chip.grant(fixture, ident, 1)[0]
            fixture['chip_slots'][0]['equip'] = [dict(type=1, dispNum=1, userChipId=owned['userChipId'])]
            projected = chip.project_monster(fixture, plain)
            self.assertEqual(int(projected[field]), int(plain[field]) + bonus)
            self.assertEqual(fixture['monsters'][0], plain)

    def test_chips_equipment_replace_remove_and_exact_prices(self):
        self.prepare_chips()
        state = self.talent_state(); ids = [c['userChipId'] for c in state['chips']]
        plain = state['monsters'][0]
        attach = dict(act=1, type=1, dispNum=1, userChipId=ids[0], userMonsterId=1)
        self.assertEqual(self.api({'180002': dict(attach, type=0, uniqueRequestId=20)})['venus_status'], 2)
        for index, cost in enumerate((1, 5, 10, 20, 40), 1):
            before = self.httpd.store.chip_inventory.item_count(self.talent_state(), 6)
            self.chip_call('180004', 30 + index, userMonsterId=1, dispNum=index)
            self.assertEqual(self.httpd.store.chip_inventory.item_count(self.talent_state(), 6), before - cost)
        slots = self.chip_call('180006', 40, userMonsterId=[1])['slotInfo'][0]['manage']
        self.assertEqual(slots['extraSlotNum'], 5); self.assertEqual(slots['maxExtraSlotNum'], 5)
        first = self.chip_call('180002', 41, **attach)
        self.assertEqual(int(first['userMonster']['attack']), int(plain['attack']) + 100)
        self.assertEqual(self.talent_state()['monsters'][0], plain)
        self.chip_call('180002', 42, **attach)
        self.assertEqual(self.api({'180002': dict(attach, uniqueRequestId=43, dispNum=2, userChipId=ids[1])})['venus_status'], 2)
        self.chip_call('180002', 44, **dict(attach, userChipId=ids[1]))
        self.assertEqual(self.talent_state()['chips'][0]['userMonsterId'], 0)
        self.assertEqual(self.httpd.store.chip_inventory.item_count(self.talent_state(), 7), 10)
        remove = dict(attach, act=0, userChipId=ids[1])
        self.chip_call('180002', 45, **remove); self.auth(); self.chip_call('180002', 46, **remove)
        self.assertEqual(self.httpd.store.chip_inventory.item_count(self.talent_state(), 7), 9)
        self.chip_call('180002', 47, **dict(attach, userChipId=ids[1]))
        self.chip_call('180002', 48, **remove)
        self.assertEqual(self.httpd.store.chip_inventory.item_count(self.talent_state(), 7), 8)

    def test_chips_fusion_sell_concurrency_receipts_and_identity(self):
        from concurrent.futures import ThreadPoolExecutor
        self.prepare_chips(amount=21)
        ids = [c['userChipId'] for c in self.talent_state()['chips']]
        param = dict(baseChip=ids[0], materialChip=ids[1:11])
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda n: self.api({'180003': dict(param, uniqueRequestId=n)}), (20, 21)))
        self.assertTrue(all(r['venus_status'] == 1 for r in responses), responses)
        self.assertEqual(len(self.talent_state()['chips']), 11)
        self.auth(); self.assertEqual(self.chip_call('180003', 22, **param)['userChip']['chipId'], 2)
        sale = dict(materialChip=[ids[0]])
        before = int(self.talent_state()['player']['gamemoney'])
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda n: self.api({'180005': dict(sale, uniqueRequestId=n)}), (23, 24)))
        self.assertTrue(all(r['venus_status'] == 1 for r in responses), responses)
        self.assertEqual(int(self.talent_state()['player']['gamemoney']), before + 200)
        self.auth(); self.chip_call('180005', 25, **sale)
        self.assertEqual(int(self.talent_state()['player']['gamemoney']), before + 200)
        self.assertEqual(self.api({'180003': dict(param, uniqueRequestId=26)})['venus_status'], 2)
        self.assertEqual(self.api({'180005': dict(uniqueRequestId=27, materialChip=[ids[0], ids[-1]])})['venus_status'], 2)
        state = self.talent_state(); new = self.httpd.store.chip_inventory.grant(state, 1, 1)[0]
        self.assertGreater(new['userChipId'], max(ids))

    def test_chips_invalid_inputs_rollback_and_overflow(self):
        self.prepare_chips()
        state = self.talent_state(); state['chips'][0]['chipId'] = 8
        state['player']['gamemoney'] = self.httpd.store.masters['990701']['codeM']['maxClusterCount']
        self.save_talent_state(state)
        cases = [('180004', dict(userMonsterId=1, dispNum=2)), ('180004', dict(userMonsterId=True, dispNum=1)),
            ('180004', dict(userMonsterId=1, dispNum=6)),
            ('180002', dict(act=True, type=1, dispNum=1, userChipId=1, userMonsterId=1)),
            ('180003', dict(baseChip=1, materialChip=[2, 3, 4])),
            ('180003', dict(baseChip=2, materialChip=[3] * 10)), ('180003', dict(baseChip=2, materialChip=[3])),
            ('180005', dict(materialChip=[2])), ('180005', dict(materialChip=[True]))]
        for n, (api, param) in enumerate(cases, 20):
            before = self.talent_state()
            self.assertEqual(self.api({api: dict(param, uniqueRequestId=n)})['venus_status'], 2, (api, param))
            self.assertEqual(self.talent_state(), before)
        self.assertEqual(self.api({'180004': dict(uniqueRequestId=50, userMonsterId=1, dispNum=1),
                                  '999999': dict(uniqueRequestId=50)})['venus_status'], 2)
        self.assertEqual(self.talent_state(), state)

    def test_chips_gift_capacity_whole_item_overflow_and_reauth(self):
        self.prepare_chips(amount=99)
        state = self.talent_state(); store = self.httpd.store
        gift = store.progression.add_gift(state, dict(assetCategoryId='17', assetValue='1', assetNum='2'), 'capacity fixture', 'test')
        patch = store.progression.add_gift(state, dict(assetCategoryId='6', assetValue='6', assetNum='1'), 'capacity fixture', 'test')
        state['items'][0]['userItemNum'] = 2147483647
        self.save_talent_state(state)
        warning = self.chip_call('070002', 20, receiveType=2, receiveIds=[gift, patch])
        self.assertEqual(warning['warningData'], [gift, patch]); self.assertEqual(len(self.talent_state()['chips']), 99)
        self.chip_call('180005', 21, materialChip=[1])
        state = self.talent_state(); state['items'][0]['userItemNum'] -= 1; self.save_talent_state(state)
        first = self.chip_call('070002', 22, receiveType=2, receiveIds=[gift, patch])
        self.assertEqual(first['isWarning'], 0); self.assertEqual(len(self.talent_state()['chips']), 100)
        after = self.talent_state(); self.auth(); self.chip_call('070002', 23, receiveType=2, receiveIds=[gift, patch])
        self.assertEqual(self.talent_state()['chips'], after['chips']); self.assertEqual(self.talent_state()['items'], after['items'])
        self.assertEqual(self.talent_state()['items'][0]['userItemNum'], 2147483647)

    def test_chips_battle_snapshot_and_mutations_protected(self):
        self.prepare_chips()
        state = self.talent_state(); state['decks']['deckList'][0]['monsterList'] = [{'userMonsterId': '1'}]
        self.save_talent_state(state)
        self.chip_call('180004', 20, userMonsterId=1, dispNum=1)
        self.chip_call('180002', 21, act=1, type=1, dispNum=1, userChipId=1, userMonsterId=1)
        start = self.chip_call('090002', 22, dungeonId='1', deckNum='1')
        snapshot = self.talent_state()['active_battle']['chip_snapshot']['1']
        self.assertEqual(snapshot['chips'][0]['chipId'], 1)
        self.assertEqual(int(snapshot['monster']['attack']), int(self.talent_state()['monsters'][0]['attack']) + 100)
        for n, (api, param) in enumerate([('180004', dict(userMonsterId=1, dispNum=2)),
                ('180002', dict(act=0, type=1, dispNum=1, userChipId=1, userMonsterId=1)),
                ('180003', dict(baseChip=1, materialChip=list(range(2, 12)))), ('180005', dict(materialChip=[12]))], 30):
            before = self.talent_state()
            self.assertEqual(self.api({api: dict(param, uniqueRequestId=n)})['venus_status'], 2)
            self.assertEqual(self.talent_state(), before)
        request = self.battle_result_request(start, 40); request.pop('uniqueRequestId')
        self.chip_call('090003', 40, **request)
        monster = self.chip_call('020101', 41, userMonsterIds=[1])['userMonsterList'][0]
        self.assertEqual(int(monster['attack']), int(self.talent_state()['monsters'][0]['attack']) + 100)

    def test_chips_capture_balance_capacity_retries_and_rollback(self):
        from unittest.mock import patch
        self.prepare_chips(amount=0)
        state = self.talent_state()
        state['facilities'].append(dict(userFacilityId=99, facilityId=25, level=1,
                                       positionX=10, positionY=10, completeTime=''))
        state['player']['friendPoint'] = '1100'
        self.save_talent_state(state)
        request = dict(uniqueRequestId=20, gachaId=10003, playCount=10)
        before = self.talent_state()
        with patch('chip_inventory.secrets.randbelow', return_value=5):
            self.assertEqual(self.api({'050012': request, '999999': dict(uniqueRequestId=20)})['venus_status'], 2)
            self.assertEqual(self.talent_state(), before)
            first = self.api({'050012': request})
        self.assertEqual(first['venus_status'], 1, first)
        self.assertEqual([c['assetValue'] for c in first['resData']['050012']['userAssetList']], ['51'] * 10)
        self.assertEqual([c['isNew'] for c in first['resData']['050012']['userAssetList']], [1] + [0] * 9)
        self.assertEqual(self.api({'050012': request}), first)
        self.assertEqual(self.talent_state()['player']['friendPoint'], '100')
        self.chip_call('050012', 21, gachaId=10003, playCount=1)
        self.assertEqual(self.talent_state()['player']['friendPoint'], '0')
        self.assertEqual(self.api({'050012': dict(request, uniqueRequestId=22)})['venus_status'], 2)
        state = self.talent_state(); state['player']['friendPoint'] = '1000'
        self.httpd.store.chip_inventory.grant(state, 1, 288)
        self.save_talent_state(state)
        before = self.talent_state()
        self.assertEqual(self.api({'050012': dict(request, uniqueRequestId=23)})['venus_status'], 2)
        self.assertEqual(self.talent_state(), before)
        state['chips'] = []; state['chip_id_sequence'] = 2147483647
        self.save_talent_state(state)
        before = self.talent_state()
        self.assertEqual(self.api({'050012': dict(request, uniqueRequestId=24)})['venus_status'], 2)
        self.assertEqual(self.talent_state(), before)

    def test_chips_training_keeps_plain_stats_and_missing_patch_protection(self):
        self.prepare_chips()
        self.chip_call('180004', 20, userMonsterId=1, dispNum=1)
        self.chip_call('180002', 21, act=1, type=1, dispNum=1, userChipId=1, userMonsterId=1)
        state = self.talent_state(); state['items'] = []
        self.save_talent_state(state)
        before = self.talent_state()
        for n, (api, param) in enumerate([('180004', dict(userMonsterId=1, dispNum=2)),
                ('180002', dict(act=0, type=1, dispNum=1, userChipId=1, userMonsterId=1)),
                ('180005', dict(materialChip=[1])), ('180003', dict(baseChip=2, materialChip=list(range(1, 11))))], 30):
            self.assertEqual(self.api({api: dict(param, uniqueRequestId=n)})['venus_status'], 2)
            self.assertEqual(self.talent_state(), before)
        # Real training operation recalculates base stats while equipment survives.
        fused = self.chip_call('010201', 40, baseMonster='1', materialMonster=['6'])['userMonster']
        plain = self.talent_state()['monsters'][0]
        self.assertEqual(int(fused['attack']), int(plain['attack']) + 100)
        for n in (41, 42):
            current = self.chip_call('020101', n, userMonsterIds=[1])['userMonsterList'][0]
            self.assertEqual(current['attack'], fused['attack'])
            self.assertEqual(self.talent_state()['monsters'][0], plain)

    def prepare_advanced_material_battle(self):
        self.prepare_normal_battle()
        state = self.talent_state()
        # Focused late-stage protocol fixture; does not prove natural progress.
        state['cleared_dungeons'] = [str(i) for i in range(1,84)]
        state['player'].update(stamina=3000,staminaMax='3000')
        self.save_talent_state(state)

    def test_advanced_material_sources_native_preview_rewards_and_retries(self):
        self.prepare_advanced_material_battle()
        for index, dungeon in enumerate([*range(70,77),83],1):
            expected = [4000000+index,5000000+index] if index<8 else [4000008]
            world = self.api({'090009':dict(uniqueRequestId=100+index,worldId='1')})['resData']['090009']
            row = next(d for s in world['worldDungeonInfo'] for d in s['dungeons'] if d['worldDungeonId']==dungeon)
            for sid in expected:
                self.assertIn(dict(assetCategoryId=14,assetValue=sid,assetNum=1,dropBoxType=2),row['dropAssets'])
            start = self.api({'090002':dict(uniqueRequestId=index,dungeonId=str(dungeon),deckNum='1')})['resData']['090002']
            request = self.battle_result_request(start,index)
            before = self.talent_state()
            result = self.api({'090003':request})
            self.assertEqual(result['venus_status'],1,result)
            for sid in expected:
                drops = [r for r in result['resData']['090003']['dropReward'] if r['assetCategoryId']=='14' and r['assetValue']==sid]
                self.assertEqual(len(drops),3);self.assertEqual(sum(r['assetNum'] for r in drops),3)
                self.assertEqual(next(r['num'] for r in self.talent_state()['souls'] if r['soulId']==str(sid)),'3')
            after = self.talent_state()
            self.assertEqual(self.api({'090003':request})['resData']['090003'],result['resData']['090003'])
            self.assertEqual(self.talent_state()['souls'],after['souls'])
            self.assertGreater(int(after['player']['gamemoney']),int(before['player']['gamemoney']))
        self.auth()
        self.assertEqual(self.api({'090003':dict(request,uniqueRequestId=99)})['venus_status'],1)
        self.assertEqual(self.talent_state()['souls'],after['souls'])
        self.assertTrue(all(r['soulId'] not in ('4000009','4000010') for r in after['souls']))

    def test_advanced_material_snapshot_rollback_defeat_and_locked_stage(self):
        self.prepare_advanced_material_battle()
        state = self.talent_state();state['cleared_dungeons']=['1'];self.save_talent_state(state)
        self.assertEqual(self.api({'090002':dict(uniqueRequestId=2,dungeonId='70',deckNum='1')})['venus_status'],2)
        state['cleared_dungeons']=[str(i) for i in range(1,84)];self.save_talent_state(state)
        start = self.api({'090002':dict(uniqueRequestId=3,dungeonId='70',deckNum='1')})['resData']['090002']
        request = self.battle_result_request(start,4)
        self.assertEqual(self.api({'090003':request,'999999':dict(uniqueRequestId=4)})['venus_status'],2)
        self.assertEqual(self.talent_state()['souls'],state['souls'])
        store = self.httpd.store;rows = store.normal_world['70']['materialsPerWave']
        store.normal_world['70']['materialsPerWave']=[dict(r,assetNum=9) for r in rows]
        try:result=self.api({'090003':request})
        finally:store.normal_world['70']['materialsPerWave']=rows
        self.assertEqual(result['venus_status'],1,result)
        self.assertEqual(next(r['num'] for r in self.talent_state()['souls'] if r['soulId']=='4000001'),'3')
        start = self.api({'090002':dict(uniqueRequestId=5,dungeonId='71',deckNum='1')})['resData']['090002']
        before = self.talent_state()['souls']
        self.assertEqual(self.api({'090003':self.battle_result_request(start,6,clear=0)})['venus_status'],1)
        self.assertEqual(self.talent_state()['souls'],before)

    def test_advanced_material_api_earned_money_version_and_status_payments(self):
        param = self.prepare_version_up()
        state = self.talent_state()
        # +4/maximum-level base, combatant, dojo, late-stage unlock and stamina
        # are explicit isolated fixtures. All payment assets/coins below are
        # earned through repeated native battle entry/settlement, never seeded.
        state['souls']=[];state['player'].update(gamemoney='0',stamina=3000,staminaMax='3000')
        state['decks']['deckList'][0]['monsterList']=[{'userMonsterId':'2'}]
        state['cleared_dungeons']=[str(i) for i in range(1,84)]
        state['facilities']=[dict(userFacilityId=1,facilityId=4,level=1,positionX=10,positionY=10,completeTime='')]
        self.save_talent_state(state)
        sequence=100
        def win(dungeon):
            nonlocal sequence
            sequence+=1
            entry=self.api({'090002':dict(uniqueRequestId=sequence,dungeonId=str(dungeon),deckNum='1')})
            self.assertEqual(entry['venus_status'],1,entry)
            result=self.api({'090003':self.battle_result_request(entry['resData']['090002'],sequence)})
            self.assertEqual(result['venus_status'],1,result)
        # Full original version recipe plus the original status reroll recipe.
        needs={'2010003':130,'2010004':105,'1070174':50,'4000002':3,'5000002':3,'2010002':40}
        sources={'2010003':10,'2010004':11,'1070174':29,'4000002':71,'5000002':71,'2010002':4}
        for sid,count in needs.items():
            held=lambda:int(next((r['num'] for r in self.talent_state()['souls'] if r['soulId']==sid),'0'))
            for attempt in range(count+1):
                before_count=held()
                if before_count>=count:break
                win(sources[sid])
                self.assertGreater(held(),before_count,'Wrong material source: '+sid)
            self.assertGreaterEqual(held(),count)
        for attempt in range(100):
            before_money=int(self.talent_state()['player']['gamemoney'])
            if before_money>=135000:break
            win(83)
            self.assertGreater(int(self.talent_state()['player']['gamemoney']),before_money)
        self.assertGreaterEqual(int(self.talent_state()['player']['gamemoney']),135000)
        before=self.talent_state()
        result=self.api({'010802':dict(param,uniqueRequestId=sequence+1)})
        self.assertEqual(result['venus_status'],1,result)
        monster=result['resData']['010802']['userMonster'];ident=int(monster['userMonsterId'])
        self.assertEqual(int(self.talent_state()['player']['gamemoney']),int(before['player']['gamemoney'])-135000)
        changed=self.api({'010801':dict(uniqueRequestId=sequence+2,baseUserMonsterId=ident,materialUserMonsterId=ident,type=2)})
        self.assertEqual(changed['venus_status'],1,changed)
        self.assertNotEqual(changed['resData']['010801']['userMonster']['tranceStatusAilment'],'')
        after=self.talent_state();self.auth()
        self.assertEqual(self.api({'010802':dict(param,uniqueRequestId=sequence+3)})['venus_status'],1)
        self.assertEqual(self.talent_state()['souls'],after['souls'])
        self.assertEqual(self.talent_state()['player']['gamemoney'],after['player']['gamemoney'])

    def prepare_version_up(self, monster_id='870'):
        self.prepare_training()
        store, state = self.httpd.store, self.talent_state()
        master = next(r for r in store.masters['990102']['monsterM'] if r['monsterId'] == monster_id)
        route = next(r for r in store.masters['990104']['monsterEvolutionM'] if r['baseMonsterId'] == monster_id and r['type'] == '3')
        recipe = next(r for r in store.masters['990115']['monsterEvolutionMaterialM']
                      if r['monsterEvolutionMaterialId'] == route['monsterEvolutionMaterialId'])
        # Isolated +4/maximum-level/material fixtures, not natural acquisition.
        base = state['monsters'][0]
        base.update(monsterId=monster_id, ex=next(r['experienceNum'] for r in store.masters['990106']['monsterExperienceM']
                    if r['level'] == master['maxLevel']), friendship='150', luck='12', hpAbility='5', hpAbilityFlg='2')
        if monster_id == '870':
            base['monsterEvolutionRouteId'] = '125'  # original Agumon+4 birth route
        base['uniqueSkillId'] = next(r['skillId'] for r in store.masters['990108']['skillM']
            if r['skillGroupId'] == master['skillGroupId'] and r['skillGroupSubId'] == base['defaultSkillGroupSubId'])
        store.add_monster_experience(base, 0)
        state['decks']['deckList'][0]['monsterList'] = [{'userMonsterId': '0'}] * 3
        state['player'].update(gamemoney='300000', meatNum='100', unitLimitMax=str(len(state['monsters'])))
        state['friendship_ready_at'] = {'1': int(time.time()) + 3600}
        state['chips'] = [{'userChipId': 22, 'userMonsterId': 1}]
        state['chip_slots'][0]['equip'] = [{'userChipId': 22}]
        material, counts = [], {}
        for i in range(1, 8):
            count = int(recipe['materialAssetNum' + str(i)])
            if count:
                sid = recipe['materialAssetValue' + str(i)]
                material.append(int(sid))
                counts[sid] = counts.get(sid, 0) + count
        state['souls'] = [{'soulId': s, 'num': str(n * 4)} for s, n in counts.items()]
        state['souls'].append({'soulId': '4000008', 'num': '100'})
        self.save_talent_state(state)
        return {'uniqueRequestId': 2, 'baseUserMonsterId': 1, 'target': int(route['nextMonsterId']), 'material': material}

    def test_version_up_replaces_identity_preserves_training_chips_and_reauth(self):
        from copy import deepcopy
        param = self.prepare_version_up()
        before = self.talent_state()
        result = self.api({'010802': param})
        self.assertEqual(result['venus_status'], 1, result)
        response = result['resData']['010802']
        self.assertEqual(set(response), {'result', 'userMonster'})
        monster = response['userMonster']; ident = monster['userMonsterId']
        self.assertNotEqual(ident, '1')
        for key in ('ex', 'level', 'friendship', 'luck', 'hpAbility', 'hpAbilityFlg', 'commonSkillId', 'leaderSkillId',
                    'monsterEvolutionRouteId', 'defaultSkillGroupSubId', 'statusFlgs'):
            self.assertEqual(monster[key], before['monsters'][0][key], key)
        self.assertEqual(monster['monsterId'], '20036')
        self.assertEqual(monster['level'], '80'); self.assertGreater(int(monster['nextLevelEx']), 0)
        after = self.talent_state()
        self.assertEqual(len(after['monsters']), len(before['monsters']))  # full inventory replacement allowed
        self.assertNotIn('1', [m['userMonsterId'] for m in after['monsters']])
        self.assertEqual(after['player']['gamemoney'], '165000')
        self.assertEqual(after['chips'], [{'userChipId': 22, 'userMonsterId': 0}])
        self.assertNotIn(1, [s['userMonsterId'] for s in after['chip_slots']])
        self.assertEqual(after['friendship_ready_at'][ident], before['friendship_ready_at']['1'])
        self.assertNotIn('1', after['friendship_ready_at'])
        self.assertEqual(self.api({'010802': param})['resData']['010802'], response)
        trained = self.api({'010202': {'uniqueRequestId': 3, 'mealMonster': ident, 'meatNum': 1}})
        self.assertEqual(trained['venus_status'], 1, trained)
        expected = deepcopy(self.talent_state())
        self.auth()
        param['uniqueRequestId'] = 4
        replay = self.api({'010802': param})
        self.assertEqual(replay['venus_status'], 1, replay)
        self.assertEqual(replay['resData']['010802']['userMonster'], next(m for m in expected['monsters'] if m['userMonsterId'] == ident))
        self.assertEqual(self.talent_state()['player'], expected['player'])
        self.assertEqual(self.talent_state()['souls'], expected['souls'])
        # Consumed upgraded objects must not be resurrected by old receipts.
        expected['monsters'] = [m for m in expected['monsters'] if m['userMonsterId'] != ident]
        self.save_talent_state(expected)
        param['uniqueRequestId'] = 5
        self.assertEqual(self.api({'010802': param})['venus_status'], 2)

    def test_version_up_almighty_multiple_slots_aggregate_and_reject_wrong_substitutes(self):
        from copy import deepcopy
        param = self.prepare_version_up('875')  # two different updater slots, 6 + 3
        state = self.talent_state()
        for i, sid in enumerate(param['material']):
            if sid in (4000002, 4000004): param['material'][i] = 4000008
        state['souls'][-1]['num'] = '8'
        self.save_talent_state(state)
        self.assertEqual(self.api({'010802': param})['venus_status'], 2)
        self.assertEqual(self.talent_state()['souls'], state['souls'])
        wrong = deepcopy(param); wrong['material'][0] = 4000008
        self.assertEqual(self.api({'010802': wrong})['venus_status'], 2)  # plugin is never replaceable
        state['souls'][-1]['num'] = '9'; self.save_talent_state(state)
        result = self.api({'010802': param})
        self.assertEqual(result['venus_status'], 1, result)
        self.assertEqual(self.talent_state()['souls'][-1]['num'], '0')

    def test_version_up_invalid_inputs_protection_and_funds_leave_state_unchanged(self):
        from copy import deepcopy
        param = self.prepare_version_up(); before = self.talent_state()
        cases = []
        for key, values in [('baseUserMonsterId', [True, '1', 0, -1, 2147483648]),
                            ('target', [True, '20036', 0, 756, 20041]),
                            ('material', [None, [], ['2010003'], [True], [0], param['material'] + [4000002], list(reversed(param['material']))])]:
            for value in values:
                bad = deepcopy(param); bad[key] = value; cases.append((bad, deepcopy(before)))
        for key, value in [('statusFlgs','2'), ('eggFlg','1'), ('growEndDate','2099-01-01 00:00:00'), ('level','79'), ('monsterId','866')]:
            badstate = deepcopy(before); badstate['monsters'][0][key] = value; cases.append((deepcopy(param), badstate))
        for balance in ('0', '134999'):
            s = deepcopy(before); s['player']['gamemoney'] = balance; cases.append((deepcopy(param), s))
        s = deepcopy(before); s['souls'][0]['num'] = '99'; cases.append((deepcopy(param), s))
        s = deepcopy(before); s['decks']['deckList'].append({'deckNum':'2','monsterList':[{'userMonsterId':'1'}]}); cases.append((deepcopy(param), s))
        s = deepcopy(before); s['tutorial']['endFlg'] = '0'; cases.append((deepcopy(param), s))
        s = deepcopy(before); s['active_battle'] = {'settled':False}; cases.append((deepcopy(param), s))
        for index, (bad, state) in enumerate(cases, 30):
            with self.subTest(index=index):
                bad['uniqueRequestId'] = index
                self.save_talent_state(state)
                self.assertEqual(self.api({'010802': bad})['venus_status'], 2)
                current = self.talent_state()
                for key in ('monsters','souls','chips','chip_slots','friendship_ready_at'):
                    self.assertEqual(current[key], state[key], key)
                self.assertEqual(current['player']['gamemoney'], state['player']['gamemoney'])

    def test_version_up_batch_rollback_parallel_retries_and_store_restart(self):
        from concurrent.futures import ThreadPoolExecutor
        param = self.prepare_version_up(); before = self.talent_state()
        rolled = self.api({'010802': param, '999999': {'uniqueRequestId':2}})
        self.assertEqual(rolled['venus_status'], 2)
        for key in ('monsters','souls','chips','chip_slots'):
            self.assertEqual(self.talent_state()[key], before[key])
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda i: self.api({'010802':dict(param,uniqueRequestId=i)}), range(10,14)))
        self.assertTrue(all(r['venus_status']==1 for r in results), results)
        self.assertEqual(len({r['resData']['010802']['userMonster']['userMonsterId'] for r in results}), 1)
        self.assertEqual(self.talent_state()['normal_version_up_count'], 1)
        self.httpd.store = Store(self.database, consumers={'test-client':'test-secret'})
        self.auth()
        self.assertEqual(self.api({'010802':dict(param,uniqueRequestId=20)})['venus_status'], 1)
        self.assertEqual(self.talent_state()['player']['gamemoney'], '165000')

    def test_version_up_to_second_skill_status_resistance_and_research_reset(self):
        from unittest.mock import patch
        param = self.prepare_version_up()
        response = self.api({'010802': param})
        self.assertEqual(response['venus_status'], 1, response)
        ident = response['resData']['010802']['userMonster']['userMonsterId']
        store, state = self.httpd.store, self.talent_state()
        material = next(m for m in state['monsters'] if m['userMonsterId'] == '2')
        material['ex'] = next(r['experienceNum'] for r in store.masters['990106']['monsterExperienceM'] if r['level'] == '10')
        store.add_monster_experience(material, 0)
        partner = next(m for m in state['monsters'] if m['userMonsterId'] == '3')
        partner.update(monsterId='866', ex='0'); store.add_monster_experience(partner, 0)
        row = next(r for r in store.masters['990120']['monsterStatusAilmentM'] if r['monsterId'] == '20036')
        recipe = next(r for r in store.masters['990121']['monsterStatusAilmentMaterialM']
                      if r['monsterStatusAilmentMaterialId'] == row['monsterStatusAilmentMaterialId'])
        needed = {row['assetValue']: int(row['assetNum'])}
        for i in range(1,5):
            if recipe['materialAssetNum'+str(i)] != '0':
                sid = recipe['materialAssetValue'+str(i)]
                needed[sid] = needed.get(sid,0) + int(recipe['materialAssetNum'+str(i)])
        for sid, count in needed.items():
            held = next((r for r in state['souls'] if r['soulId']==sid),None)
            if held: held['num'] = str(int(held['num'])+count)
            else: state['souls'].append({'soulId':sid,'num':str(count)})
        state['facilities'] = [{'userFacilityId':1,'facilityId':4,'level':1,'positionX':10,'positionY':10,'completeTime':''}]
        self.save_talent_state(state)
        inherited = self.api({'010601':dict(uniqueRequestId=3,baseUserMonsterId=ident,materialUserMonsterId='2',
                                           baseCommonSkillNumber=2,materialCommonSkillNumber=1)})
        self.assertEqual(inherited['venus_status'],1,inherited)
        self.assertEqual(inherited['resData']['010601']['userMonster']['extraCommonSkillId'], material['commonSkillId'])
        with patch('linkz_server.secrets.choice', side_effect=lambda choices: choices[0]):
            changed = self.api({'010801':dict(uniqueRequestId=4,baseUserMonsterId=int(ident),materialUserMonsterId=int(ident),type=2)})
        self.assertEqual(changed['venus_status'],1,changed)
        self.assertNotEqual(changed['resData']['010801']['userMonster']['tranceStatusAilment'],'')
        result = self.api({'010501':dict(uniqueRequestId=5,baseUserMonsterId=ident,materialUserMonsterId='3')})
        self.assertEqual(result['venus_status'],1,result)
        egg = result['resData']['010501']['userMonster']
        self.assertEqual(egg['monsterId'],'20')  # +4, never Ver.1 egg
        self.assertEqual(egg['extraCommonSkillId'],'')
        self.assertEqual(egg['tranceStatusAilment'],'')
        self.assertEqual(egg['friendship'],'0')
        self.assertEqual(egg['hpAbility'],'5')
        self.assertEqual(self.api({'010802':dict(param,uniqueRequestId=6)})['venus_status'],2)

    def test_version_up_all_original_same_form_recipes_and_special_boundary(self):
        from copy import deepcopy
        from linkz_server import ProtocolError
        self.prepare_version_up()
        store, template = self.httpd.store, self.talent_state()
        masters = {r['monsterId']:r for r in store.masters['990102']['monsterM']}
        groups = {r['monsterGroupId']:r for r in store.masters['990103']['monsterM']}
        recipes = {r['monsterEvolutionMaterialId']:r for r in store.masters['990115']['monsterEvolutionMaterialM']}
        successful, protected = 0, 0
        for route in store.masters['990104']['monsterEvolutionM']:
            if route['type'] != '3': continue
            master, destination = masters[route['baseMonsterId']], masters[route['nextMonsterId']]
            if master['monsterGroupId'] != destination['monsterGroupId']: continue
            with self.subTest(base=master['monsterId'],target=destination['monsterId']):
                state = deepcopy(template)
                base = state['monsters'][0]
                base.update(monsterId=master['monsterId'],ex=next(r['experienceNum'] for r in store.masters['990106']['monsterExperienceM'] if r['level']==master['maxLevel']))
                store.add_monster_experience(base,0)
                recipe = recipes[route['monsterEvolutionMaterialId']]
                selected, amounts = [], {}
                for i in range(1,8):
                    count = int(recipe['materialAssetNum'+str(i)])
                    if count:
                        sid = recipe['materialAssetValue'+str(i)]
                        selected.append(int(sid)); amounts[sid]=amounts.get(sid,0)+count
                state['souls'] = [{'soulId':s,'num':str(n)} for s,n in amounts.items()]
                param = dict(baseUserMonsterId=1,target=int(destination['monsterId']),material=selected)
                if groups[master['monsterGroupId']]['monsterType'] == '1':
                    with self.assertRaises(ProtocolError): store.version_up_monster(state,param)
                    protected += 1
                    continue
                result = store.version_up_monster(state,param)
                self.assertEqual(result['result'],1)
                self.assertEqual(result['userMonster']['monsterId'], destination['monsterId'])
                self.assertEqual(result['userMonster']['level'],'80')
                self.assertEqual(state['player']['gamemoney'],'165000')
                self.assertTrue(all(r['num']=='0' for r in state['souls']))
                self.assertNotIn('1',[r['userMonsterId'] for r in state['monsters']])
                successful += 1
        self.assertEqual((successful,protected),(127,1))

    def prepare_resistance(self, monster_id='81', kind=1):
        self.prepare_training()
        store = self.httpd.store
        state = self.talent_state()
        state['facilities'] = [{'userFacilityId': 1, 'facilityId': 4, 'level': 1,
            'positionX': 10, 'positionY': 10, 'completeTime': ''}]
        master = next(r for r in store.masters['990102']['monsterM'] if r['monsterId'] == monster_id)
        group = next(r for r in store.masters['990103']['monsterM'] if r['monsterGroupId'] == master['monsterGroupId'])
        for m in state['monsters'][:3]:
            m.update(monsterId=monster_id, ex='0')
            store.add_monster_experience(m, 0)
        fragment = [r for r in store.masters['990113']['monsterTranceM'] if r['monsterId'] == monster_id]
        costs = {}
        def seed(soul, amount):
            costs[soul] = costs.get(soul, 0) + int(amount)
        if kind == 1:
            for r in fragment:
                seed(r['soulId'], r['num'])
            if len(fragment) <= 1:
                for r in store.masters['990114']['monsterTribeTranceM']:
                    if r['tribe'] == group['tribe'] and r['growStep'] == group['growStep']:
                        seed(r['soulId'], r['num'])
        else:
            row = next(r for r in store.masters['990120']['monsterStatusAilmentM'] if r['monsterId'] == monster_id)
            seed(row['assetValue'], row['assetNum'])
            recipe = next(r for r in store.masters['990121']['monsterStatusAilmentMaterialM']
                          if r['monsterStatusAilmentMaterialId'] == row['monsterStatusAilmentMaterialId'])
            for slot in range(1, 5):
                if recipe['materialAssetNum' + str(slot)] != '0':
                    seed(recipe['materialAssetValue' + str(slot)], recipe['materialAssetNum' + str(slot)])
        # Explicit isolated resource/monster fixtures. No player-save injection
        # or claim of naturally acquiring special/version monsters or materials.
        state['souls'] = [{'soulId': s, 'num': str(n * 3)} for s, n in costs.items()]
        # This fixture changes species directly. Normalize current native slots
        # before asserting that resistance changes preserve the same equipment.
        store.chip_inventory.refresh_slots(state)
        self.save_talent_state(state)
        return {'uniqueRequestId': 2, 'baseUserMonsterId': 1,
                'materialUserMonsterId': 1 if fragment or kind == 2 else 2, 'type': kind}

    def test_resistance_ordinary_materials_replacement_rollback_and_reauth(self):
        from unittest.mock import patch
        request = self.prepare_resistance()
        state = self.talent_state()
        state['monsters'][0].update(friendship='29', hpAbility='5', hpAbilityFlg='2', tranceStatusAilment='40001')
        state['monsters'][1]['monsterId'] = '82'  # Same name, different awakening.
        state['chips'] = [{'userChipId': 10, 'userMonsterId': 2}]
        state['farm_monsters'] = [1, 2, 3]
        state['friendship_ready_at'] = {'1': 2000000000, '2': 2000000000}
        self.save_talent_state(state)
        with patch('linkz_server.secrets.choice', return_value='917'):
            failed = self.api({'010801': request, '999999': {'uniqueRequestId': 2}})
        self.assertEqual(failed['venus_status'], 2)
        after = self.talent_state()
        for key in ('monsters', 'souls', 'chips', 'farm_monsters', 'friendship_ready_at'):
            self.assertEqual(after[key], state[key], key)
        with patch('linkz_server.secrets.choice', return_value='917') as roll:
            first = self.api({'010801': request})
        self.assertEqual(first['venus_status'], 1, first)
        self.assertNotIn('916', roll.call_args.args[0])  # Agumon intrinsically resists fire.
        monster = first['resData']['010801']['userMonster']
        expected = dict(state['monsters'][0], tranceResistance='917')
        self.assertEqual(monster, expected)
        after = self.talent_state()
        self.assertEqual({r['soulId']: r['num'] for r in after['souls']}, {'2010001': '20', '2010002': '10'})
        self.assertEqual(after['player']['gamemoney'], state['player']['gamemoney'])
        self.assertEqual(after['player']['point'], state['player']['point'])
        self.assertEqual(after['chips'][0]['userMonsterId'], 0)
        self.assertEqual(after['farm_monsters'], [1, 3])
        self.assertEqual(after['friendship_ready_at'], {'1': 2000000000})
        self.assertEqual(len(after['monsters']), 5)
        with patch('linkz_server.secrets.choice', side_effect=AssertionError('receipt must not reroll')):
            self.assertEqual(self.api({'010801': request}), first)
            self.assertEqual(self.api({'010801': dict(request, uniqueRequestId=3)}), first)
            self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
            self.auth()
            self.assertEqual(self.api({'010801': dict(request, uniqueRequestId=1)}), first)
        # Another real partner produces a deliberate replacement, not stacking.
        with patch('linkz_server.secrets.choice', return_value='919'):
            second = self.api({'010801': dict(request, materialUserMonsterId=3, uniqueRequestId=4)})
        self.assertEqual(second['resData']['010801']['userMonster']['tranceResistance'], '919')
        self.auth()
        with patch('linkz_server.secrets.choice', side_effect=AssertionError('old receipt returns current base')):
            replay = self.api({'010801': dict(request, uniqueRequestId=1)})
        self.assertEqual(replay['resData']['010801']['userMonster']['tranceResistance'], '919')
        self.assertEqual(self.talent_state()['normal_resistance_change_count'], 2)

    def test_resistance_ordinary_concurrent_different_ids_consumes_once(self):
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        request = self.prepare_resistance()
        with patch('linkz_server.secrets.choice', return_value='915') as roll:
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda n: self.api({'010801': dict(request, uniqueRequestId=n)}), (2, 3)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0]['venus_status'], 1, results)
        self.assertEqual(roll.call_count, 1)
        after = self.talent_state()
        self.assertEqual(after['normal_resistance_change_count'], 1)
        self.assertEqual(len(after['monsters']), 5)

    def test_resistance_guards_material_shortage_and_invalid_rules_atomic(self):
        from copy import deepcopy
        request = self.prepare_resistance()
        original = self.talent_state()
        for key, changes in (('facilities', []), ('facilities', [dict(original['facilities'][0], level=0, completeTime='2099-01-01 00:00:00')]),
                             ('active_battle', {'settled': False}), ('tutorial', {'endFlg': '0', 'statusId': '0'})):
            state = deepcopy(original);state[key] = changes;self.save_talent_state(state)
            self.assertEqual(self.api({'010801': request})['venus_status'], 2)
            self.assertEqual(self.talent_state()['souls'], original['souls'])
        for changes in ({'monsterId': '86'}, {'statusFlgs': '2'}, {'eggFlg': '1'}, {'growEndDate': '2099-01-01 00:00:00'}):
            state = deepcopy(original);state['monsters'][1].update(changes);self.save_talent_state(state)
            self.assertEqual(self.api({'010801': request})['venus_status'], 2)
            self.assertEqual(len(self.talent_state()['monsters']), 6)
        state = deepcopy(original);state['decks']['deckList'][0]['monsterList'].append({'userMonsterId': '2'});self.save_talent_state(state)
        self.assertEqual(self.api({'010801': request})['venus_status'], 2)
        state = deepcopy(original);state['souls'][0]['num'] = '9';self.save_talent_state(state)
        self.assertEqual(self.api({'010801': request})['venus_status'], 2)
        self.assertEqual(self.talent_state()['souls'], state['souls'])
        self.save_talent_state(original)
        for changes in ({'baseUserMonsterId': True}, {'materialUserMonsterId': '2'}, {'type': True}, {'type': 0},
                        {'baseUserMonsterId': 99}, {'materialUserMonsterId': 1}, {'materialUserMonsterId': 2147483648}, {'type': 2}):
            self.assertEqual(self.api({'010801': dict(request, **changes)})['venus_status'], 2)
        store = self.httpd.store;rules = deepcopy(store.local_gameplay['resistanceChange'])
        for ids in (['915'] * 7, ['1', '916', '917', '918', '919', '920', '921'], [True] * 7, ['missing'] * 7):
            store.local_gameplay['resistanceChange']['attributeResistanceIds'] = ids
            self.assertEqual(self.api({'010801': request})['venus_status'], 2)
            self.assertEqual(self.talent_state()['souls'], original['souls'])
        store.local_gameplay['resistanceChange'] = rules

    def test_resistance_ultimate_fragment_repeat_and_session_receipt(self):
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        request = self.prepare_resistance('831')
        before = self.talent_state()
        with patch('linkz_server.secrets.choice', return_value='916') as roll:
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = list(executor.map(lambda _: self.api({'010801': request}), (0, 1)))
        self.assertEqual(first[0], first[1])
        self.assertEqual(first[0]['venus_status'], 1, first)
        self.assertEqual(roll.call_count, 1)
        after = self.talent_state()
        spent = {s['soulId']: int(s['num']) - int(next(x for x in after['souls'] if x['soulId'] == s['soulId'])['num']) for s in before['souls']}
        self.assertEqual(spent, {'1070167': 2, '2050001': 25, '2050002': 20, '2050003': 20, '2050004': 10})
        self.assertEqual(len(after['monsters']), 6)
        self.assertEqual(after['chip_slots'], before['chip_slots'])
        self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
        # Store recreation preserves both state and same-token transport receipt.
        with patch('linkz_server.secrets.choice', side_effect=AssertionError('same ID must not reroll')):
            self.assertEqual(self.api({'010801': request}), first[0])
        self.auth()
        current = self.api({'020101': {'uniqueRequestId': 1, 'userMonsterIds': [1]}})['resData']['020101']['userMonsterList'][0]
        self.assertEqual(current['tranceResistance'], '916')
        # Native self-ID arguments do not identify paid operation intent across
        # auth; a new request is explicitly a new paid change, not a replay.
        with patch('linkz_server.secrets.choice', return_value='917'):
            second = self.api({'010801': dict(request, uniqueRequestId=2)})
        self.assertEqual(second['resData']['010801']['userMonster']['tranceResistance'], '917')
        self.assertEqual(self.talent_state()['normal_resistance_change_count'], 2)
        self.assertEqual(self.talent_state()['player']['gamemoney'], before['player']['gamemoney'])

    def test_resistance_special_multiple_materials_do_not_add_tribe_recipe(self):
        from unittest.mock import patch
        request = self.prepare_resistance('10021')
        before = self.talent_state()
        self.assertEqual(len(before['souls']), 5)
        with patch('linkz_server.secrets.choice', return_value='917'):
            response = self.api({'010801': request})
        self.assertEqual(response['venus_status'], 1, response)
        after = self.talent_state()
        spent = {s['soulId']: int(s['num']) - int(next(x for x in after['souls'] if x['soulId'] == s['soulId'])['num']) for s in before['souls']}
        self.assertEqual(spent, {'3000001': 10, '3000002': 5, '3000003': 4, '2010003': 10, '2010004': 10})
        self.assertEqual(len(after['monsters']), 6)

    def test_resistance_status_version_materials_independent_and_rollback(self):
        from unittest.mock import patch
        request = self.prepare_resistance('20001', 2)
        before = self.talent_state();before['monsters'][0]['tranceResistance'] = '916';self.save_talent_state(before)
        with patch('linkz_server.secrets.choice', return_value='40005'):
            self.assertEqual(self.api({'010801': request, '999999': {'uniqueRequestId': 2}})['venus_status'], 2)
        self.assertEqual(self.talent_state()['souls'], before['souls'])
        with patch('linkz_server.secrets.choice', return_value='40005') as roll:
            response = self.api({'010801': request})
        self.assertEqual(response['venus_status'], 1, response)
        monster = response['resData']['010801']['userMonster']
        self.assertEqual((monster['tranceResistance'], monster['tranceStatusAilment']), ('916', '40005'))
        innate = next(r for r in self.httpd.store.masters['990107']['monsterResistanceM'] if r['monsterResistanceId'] == '19')
        for row in self.httpd.store.masters['990107']['monsterResistanceM']:
            if row['monsterResistanceId'] in roll.call_args.args[0]:
                self.assertTrue(all(int(innate[f]) <= 0 for f in ('poison', 'confusion', 'paralysis', 'sleep', 'stun', 'skillLock', 'death') if row[f] == '1'))
        after = self.talent_state()
        spent = {s['soulId']: int(s['num']) - int(next(x for x in after['souls'] if x['soulId'] == s['soulId'])['num']) for s in before['souls']}
        self.assertEqual(spent, {'1070167': 15, '5000006': 3, '2050002': 40, '2050003': 30, '2050004': 20})
        self.assertEqual(len(after['monsters']), 6)
        self.assertEqual(after['player']['gamemoney'], before['player']['gamemoney'])
        self.assertEqual(self.api({'010801': request}), response)

    def test_resistance_carry_and_intrinsic_collision_on_evolution(self):
        from unittest.mock import patch
        request = self.prepare_resistance('121')
        # 121 intrinsically resists fire; 476 intrinsically resists water.
        with patch('linkz_server.secrets.choice', return_value='917'):
            self.assertEqual(self.api({'010801': request})['venus_status'], 1)
        store = self.httpd.store;state = self.talent_state();store.add_monster_experience(state['monsters'][0], 100000)
        evo = next(r for r in store.masters['990104']['monsterEvolutionM'] if r['baseMonsterId'] == '121' and r['nextMonsterId'] == '476')
        recipe = next(r for r in store.masters['990115']['monsterEvolutionMaterialM'] if r['monsterEvolutionMaterialId'] == evo['monsterEvolutionMaterialId'])
        for slot in range(1, 8):
            if int(recipe['materialAssetNum' + str(slot)]):
                state['souls'].append({'soulId': recipe['materialAssetValue' + str(slot)], 'num': '1000'})
        # Avoid duplicate inventory rows; fixture counts cover the full recipe.
        state['souls'] = list({r['soulId']: r for r in state['souls']}.values());self.save_talent_state(state)
        evolve = {'uniqueRequestId': 3, 'userMonsterId': '1', 'monsterId': 476}
        with patch('linkz_server.secrets.choice', return_value='915'):
            self.assertEqual(self.api({'010301': evolve, '999999': {'uniqueRequestId': 3}})['venus_status'], 2)
        self.assertEqual(self.talent_state()['monsters'][0]['tranceResistance'], '917')
        with patch('linkz_server.secrets.choice', return_value='915') as roll:
            result = self.api({'010301': evolve})
        self.assertEqual(result['venus_status'], 1, result)
        self.assertNotIn('917', roll.call_args.args[0])
        self.assertEqual(result['resData']['010301']['userMonster']['tranceResistance'], '915')
        # Existing noncolliding additions (including legacy comma separation)
        # survive; malformed/unknown IDs fail before a paid evolution.
        destination = next(r for r in store.masters['990102']['monsterM'] if r['monsterId'] == '476')
        with patch('linkz_server.secrets.choice', side_effect=AssertionError('no collision')):
            self.assertEqual(store.evolution_resistance_values({'tranceResistance': '915,916'}, destination)['tranceResistance'], '915,916')
        with self.assertRaisesRegex(Exception, 'Invalid added resistance'):
            store.evolution_resistance_values({'tranceResistance': 'missing'}, destination)

    def test_battle_final_friendship_to_research_awakening_native_api_chain(self):
        from unittest.mock import patch
        request = self.prepare_research(friendship=299)
        store = self.httpd.store;state = self.talent_state()
        # Ultimate pair / 299 friendship / funds are explicit temporary boundary
        # fixtures. The final +1 is earned by an ordinary battle settlement API.
        state['decks']['deckList'][0]['monsterList'] = [{'userMonsterId': '1'}, {'userMonsterId': '0'}, {'userMonsterId': '0'}]
        state['player'].update(stamina=40, staminaMax='40')
        state['friendship_ready_at'] = {'1': int(time.time()) + 86400}
        self.save_talent_state(state)
        start = self.api({'090002': {'uniqueRequestId': 10, 'dungeonId': '1', 'deckNum': '1'}})['resData']['090002']
        self.assertEqual(self.api({'010501': request})['venus_status'], 2)
        result = self.battle_result_request(start, 10)
        with patch('linkz_server.secrets.randbelow', return_value=0):
            win = self.api({'090003': result, '020101': {'uniqueRequestId': 11, 'userMonsterIds': [1, 2]}})
        self.assertEqual(win['venus_status'], 1, win)
        partners = win['resData']['020101']['userMonsterList']
        self.assertEqual([m['friendship'] for m in partners], ['300', '299'])
        self.assertEqual(self.talent_state()['friendship_ready_at'], state['friendship_ready_at'])
        self.assertEqual(self.api({'010501': dict(request, uniqueRequestId=12)})['venus_status'], 2)
        # Native deck save requires three partners; replace the base with the
        # three other owned partners rather than submitting an empty deck.
        remove = self.api({'010402': {'uniqueRequestId': 12, 'deckData': [[3, 4, 5]], 'selectDeckNum': 1, 'favoriteDeckNum': 1}})
        self.assertEqual(remove['venus_status'], 1, remove)
        before = self.talent_state()
        self.assertEqual(self.api({'010501': dict(request, uniqueRequestId=13), '999999': {'uniqueRequestId': 13}})['venus_status'], 2)
        self.assertEqual(self.talent_state()['monsters'], before['monsters'])
        researched = self.api({'010501': dict(request, uniqueRequestId=13)})
        self.assertEqual(researched['venus_status'], 1, researched)
        egg = researched['resData']['010501']['userMonster']
        self.assertEqual((egg['monsterId'], egg['monsterEvolutionRouteId'], egg['eggFlg'], egg['friendship']), ('17', '122', '1', '0'))
        self.assertEqual(self.talent_state()['normal_research_count'], 1)
        self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'});self.auth()
        self.assertEqual(self.api({'010501': dict(request, uniqueRequestId=1)}), researched)
        self.assertEqual(self.talent_state()['normal_research_count'], 1)

    def test_restaurant_build_upgrade_completion_once_and_recovery(self):
        from concurrent.futures import ThreadPoolExecutor
        self.prepare_farm_edit()
        state = self.talent_state()
        state['player'].update(gamemoney='10000', point=10, stamina=5)
        self.save_talent_state(state)
        build = {'uniqueRequestId': 2, 'facilityId': 3, 'positionX': 10, 'positionY': 10}
        self.assertEqual(self.api({'140001': build})['venus_status'], 2)
        state = self.talent_state()
        state['facility_keys'] = {'3': 1}
        self.save_talent_state(state)
        self.assertEqual(self.api({'140001': build, '999999': {'uniqueRequestId': 2}})['venus_status'], 2)
        self.assertEqual(self.talent_state()['player']['gamemoney'], '10000')
        first = self.api({'140001': build})
        self.assertEqual(first['venus_status'], 1, first)
        self.assertEqual(self.api({'140001': build}), first)
        ident = first['resData']['140001']['userFacilityId']
        complete = {'uniqueRequestId': 3, 'userFacilityId': ident}
        self.assertEqual(self.api({'140010': complete})['venus_status'], 2)
        self.assertEqual(self.talent_state()['player']['staminaMax'], '20')
        self.assertEqual(self.api({'140002': complete})['resData']['140002']['num'], 2)
        state = self.talent_state()
        self.assertEqual((state['player']['stamina'], state['player']['staminaMax']), (40, '40'))
        self.assertEqual(state['player']['recovery'], 0)
        self.assertNotIn('stamina_full_at', state)
        self.assertEqual(state['player']['gamemoney'], '9000')
        # Model a later debit only in the temporary DB. Duplicate completion,
        # shortening and reconnect must preserve its timer rather than refill.
        state['player']['stamina'] = 31
        deadline = int(time.time()) + 9 * 180
        state['stamina_full_at'] = deadline
        self.save_talent_state(state)
        self.assertEqual(self.api({'140010': complete})['venus_status'], 1)
        self.assertEqual(self.api({'140002': dict(complete, uniqueRequestId=4)})['resData']['140002']['num'], 0)
        self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
        self.auth()
        self.assertEqual(self.api({'140010': dict(complete, uniqueRequestId=1)})['venus_status'], 1)
        state = self.talent_state()
        self.assertEqual((state['player']['stamina'], state['stamina_full_at']), (31, deadline))
        upgrade = dict(complete, uniqueRequestId=5)
        before = self.talent_state()
        self.assertEqual(self.api({'140003': upgrade, '999999': {'uniqueRequestId': 5}})['venus_status'], 2)
        self.assertEqual(self.talent_state()['facilities'], before['facilities'])
        self.assertEqual(self.talent_state()['player']['gamemoney'], '9000')
        self.assertEqual(self.api({'140003': upgrade})['venus_status'], 1)
        self.assertEqual(self.api({'140003': upgrade})['venus_status'], 1)
        self.assertEqual(self.talent_state()['player']['gamemoney'], '8000')
        self.assertEqual(self.talent_state()['player']['staminaMax'], '40')
        state = self.talent_state()
        state['facilities'][0]['completeTime'] = '2000-01-01 00:00:00'
        self.save_talent_state(state)
        before = self.talent_state()
        self.assertEqual(self.api({'140010': dict(complete, uniqueRequestId=6), '999999': {'uniqueRequestId': 6}})['venus_status'], 2)
        self.assertEqual(self.talent_state()['facilities'], before['facilities'])
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda n: self.api({'140010': dict(complete, uniqueRequestId=n)}), (6, 7)))
        self.assertTrue(all(r['venus_status'] == 1 for r in results), results)
        state = self.talent_state()
        self.assertEqual(state['facilities'][0]['level'], 2)
        self.assertEqual((state['player']['stamina'], state['player']['staminaMax']), (45, '45'))
        self.assertNotIn('stamina_full_at', state)
        state['player']['stamina'] = 32
        state['stamina_full_at'] = int(time.time()) + 13 * 180
        self.save_talent_state(state)
        self.auth()
        self.assertEqual(self.api({'140010': dict(complete, uniqueRequestId=1)})['venus_status'], 1)
        self.assertEqual(self.api({'020001': {'uniqueRequestId': 2}, '140006': {'uniqueRequestId': 2, 'userId': 1}})['resData']['020001']['playerInfo']['stamina'], 32)

    def test_restaurant_all_levels_and_overcap_preserved(self):
        self.prepare_farm_edit()
        state = self.talent_state()
        state['player'].update(gamemoney='100000', point=1000, stamina=123)
        state['facility_keys'] = {'3': 1}
        self.save_talent_state(state)
        built = self.api({'140001': {'uniqueRequestId': 2, 'facilityId': 3, 'positionX': 10, 'positionY': 10}})
        ident = built['resData']['140001']['userFacilityId']
        for level, maximum in enumerate((40, 45, 50, 55, 60, 65, 70, 75, 80, 100), 1):
            request = {'uniqueRequestId': 10 + level, 'userFacilityId': ident}
            if level > 1:
                self.assertEqual(self.api({'140003': request})['venus_status'], 1)
            shortened = self.api({'140002' if level == 1 else '140004': request})
            self.assertEqual(shortened['venus_status'], 1, shortened)
            player = self.talent_state()['player']
            self.assertEqual((player['stamina'], player['staminaMax'], player['recovery']), (123, str(maximum), 0))
        before = self.talent_state()
        self.assertEqual(self.api({'140003': {'uniqueRequestId': 30, 'userFacilityId': ident}})['venus_status'], 2)
        self.assertEqual(self.talent_state()['player'], before['player'])
        for api in ('140015', '140007'):
            self.assertEqual(self.api({api: {'uniqueRequestId': 31, 'userFacilityId': ident, 'stockFlg': 1}})['venus_status'], 2)

    def test_house_zero_time_upgrade_stones_all_levels_capacity_and_reauth(self):
        from concurrent.futures import ThreadPoolExecutor
        self.prepare_farm_edit()
        state = self.talent_state()
        state['initial_facilities_created'] = False
        state['player'].update(point=2000, gamemoney='1000')
        self.save_talent_state(state)
        farm = self.api({'140006': {'uniqueRequestId': 2, 'userId': 1}})['resData']['140006']
        house = next(r for r in farm['userFacilityList'] if r['facilityId'] == 7)
        ident = house['userFacilityId']
        self.assertEqual((house['level'], house['positionX'], house['positionY']), (1, 17, 11))
        caps = (50, 70, 90, 110, 130, 150, 180, 210, 240, 300, 370, 410, 450, 500, 550, 600, 670, 740, 810, 999)
        spent = 0
        for level in range(2, 21):
            request = {'uniqueRequestId': 10 + level, 'userFacilityId': ident}
            before = self.talent_state()
            if level == 2:
                self.assertEqual(self.api({'140003': request, '999999': {'uniqueRequestId': 12}})['venus_status'], 2)
                self.assertEqual(self.talent_state()['player']['point'], 2000)
            upgraded = self.api({'140003': request})
            self.assertEqual(upgraded['venus_status'], 1, upgraded)
            self.assertEqual(self.api({'140003': request}), upgraded)
            fee = 5 if level <= 10 else 20 if level <= 13 else 50 if level <= 16 else 100 if level <= 19 else 200
            spent += fee
            current = self.talent_state()
            row = next(r for r in current['facilities'] if r['facilityId'] == 7)
            self.assertEqual((row['level'], current['player']['unitLimitMax']), (level - 1, str(caps[level - 2])))
            self.assertTrue(row['completeTime'])
            self.assertEqual(current['player']['point'], 2000 - spent)
            self.assertEqual(current['player']['gamemoney'], '1000')
            self.assertEqual(current['monsters'], before['monsters'])
            self.assertEqual(self.api({'140003': dict(request, uniqueRequestId=100 + level)})['venus_status'], 2)
            if level == 2:
                self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
                self.auth()
            if level == 20:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(lambda n: self.api({'140010': dict(request, uniqueRequestId=n)}), (200, 201)))
                self.assertTrue(all(r['venus_status'] == 1 for r in results), results)
            else:
                # Expired zero-second shortening costs zero and completes once.
                result = self.api({'140004': request})
                self.assertEqual(result['resData']['140004']['num'], 0, result)
                self.assertEqual(self.api({'140010': dict(request, uniqueRequestId=200 + level)})['venus_status'], 1)
            current = self.talent_state()
            self.assertEqual(current['player']['unitLimitMax'], str(caps[level - 1]))
            self.assertEqual(current['player']['point'], 2000 - spent)
        self.auth()
        self.assertEqual(self.api({'140003': {'uniqueRequestId': 1, 'userFacilityId': ident}})['venus_status'], 2)
        data = self.api({'020001': {'uniqueRequestId': 2}, '140006': {'uniqueRequestId': 2, 'userId': 1}})['resData']
        self.assertEqual(data['020001']['playerInfo']['unitLimitMax'], '999')
        self.assertEqual(len([r for r in data['140006']['userFacilityList'] if r['facilityId'] == 7]), 1)
        for api in ('140015', '140007'):
            self.assertEqual(self.api({api: {'uniqueRequestId': 3, 'userFacilityId': ident, 'stockFlg': 1}})['venus_status'], 2)

    def test_facility_currency_aggregate_validation_before_debit(self):
        from copy import deepcopy
        store = self.httpd.store
        master = deepcopy(next(r for r in store.masters['990302']['facilityUpgradeM'] if r['facilityId'] == '7' and r['level'] == '2'))
        master.update(upgradeAssetCategoryId2='4', upgradeAssetValue2='1', upgradeAssetNum2='10',
                      upgradeAssetCategoryId3='2', upgradeAssetValue3='1', upgradeAssetNum3='3')
        for money, stones, error in ((9, 8, 'money'), (10, 7, 'stones')):
            state = {'player': {'gamemoney': str(money), 'point': stones}}
            before = deepcopy(state)
            with self.assertRaisesRegex(Exception, error):
                store.pay_facility_cost(state, master, 'upgrade')
            self.assertEqual(state, before)
        for key, value in (('upgradeAssetCategoryId3', '16'), ('upgradeAssetValue3', '9'), ('upgradeAssetNum3', '-1')):
            invalid = dict(master, **{key: value})
            state = {'player': {'gamemoney': '10', 'point': 8}}
            with self.assertRaisesRegex(Exception, 'cost type'):
                store.pay_facility_cost(state, invalid, 'upgrade')
            self.assertEqual(state['player'], {'gamemoney': '10', 'point': 8})
        state = {'player': {'gamemoney': '10', 'point': 8}}
        store.pay_facility_cost(state, master, 'upgrade')
        self.assertEqual(state['player'], {'gamemoney': '0', 'point': 0})

    def test_dojo_unlock_construction_retry_completion_and_limits(self):
        self.prepare_farm_edit()
        state = self.talent_state()
        state['player'].update(gamemoney='5000', point=10)
        state['facility_keys'] = {'3': 1}  # Restaurant key does not unlock dojo.
        self.save_talent_state(state)
        request = {'uniqueRequestId': 2, 'facilityId': 4, 'positionX': 10, 'positionY': 10}
        self.assertEqual(self.api({'140001': request})['venus_status'], 2)
        state = self.talent_state()
        state['facility_keys']['2'] = 1
        self.save_talent_state(state)
        built = self.api({'140001': request})
        self.assertEqual(built['venus_status'], 1, built)
        self.assertEqual(self.api({'140001': request}), built)
        ident = built['resData']['140001']['userFacilityId']
        complete = {'uniqueRequestId': 3, 'userFacilityId': ident}
        self.assertEqual(self.api({'140010': complete})['venus_status'], 2)
        result = self.api({'140002': complete})
        self.assertEqual(result['resData']['140002']['num'], 2)
        self.assertEqual(self.api({'140002': complete}), result)
        self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
        self.auth()
        self.assertEqual(self.api({'140002': dict(complete, uniqueRequestId=1)})['resData']['140002']['num'], 0)
        state = self.talent_state()
        self.assertEqual((state['facilities'][0]['level'], state['player']['gamemoney'], state['player']['point']), (1, '3000', 8))
        for api, params in (('140003', complete), ('140001', dict(request, uniqueRequestId=4)),
                            ('140015', dict(complete, stockFlg=1)), ('140007', complete)):
            self.assertEqual(self.api({api: dict(params, uniqueRequestId=5)})['venus_status'], 2)

    def test_house_upgrade_insufficient_stones_slots_and_invalid_owner(self):
        self.prepare_farm_edit()
        state = self.talent_state()
        state['player'].update(point=4, gamemoney='10000')
        state['facilities'] = [{'userFacilityId': 1, 'facilityId': 7, 'level': 1,
            'positionX': 17, 'positionY': 11, 'completeTime': ''}]
        self.save_talent_state(state)
        request = {'uniqueRequestId': 2, 'userFacilityId': 1}
        result = self.api({'140003': request})
        self.assertEqual(result['venus_status'], 2)
        self.assertIn('数码石', result['message'])
        self.assertEqual(self.talent_state()['player']['point'], 4)
        self.assertEqual(self.talent_state()['player']['gamemoney'], '10000')
        for ident in (True, None, '1', 0, 99):
            self.assertEqual(self.api({'140003': dict(request, userFacilityId=ident)})['venus_status'], 2)
        state = self.talent_state()
        state['player']['point'] = 10
        state['facilities'] += [{'userFacilityId': i, 'facilityId': 1, 'level': 0,
            'positionX': i + 10, 'positionY': 10, 'completeTime': '2099-01-01 00:00:00'} for i in (2, 3)]
        self.save_talent_state(state)
        self.assertEqual(self.api({'140003': request})['venus_status'], 2)
        self.assertEqual(self.talent_state()['player']['point'], 10)
        self.assertEqual(self.talent_state()['facilities'], state['facilities'])

    def test_house_expansion_unlocks_real_capture_capacity_only_after_completion(self):
        from copy import deepcopy
        self.prepare_farm_edit()
        state = self.talent_state()
        state['player']['point'] = 500
        state['facilities'] = [{'userFacilityId': 1, 'facilityId': 7, 'level': 1,
            'positionX': 17, 'positionY': 11, 'completeTime': ''}]
        self.save_talent_state(state)
        request = {'uniqueRequestId': 2, 'gachaId': 10001, 'playCount': 1, 'itemCount': 500}
        first = self.api({'050002': request})
        self.assertEqual(first['venus_status'], 1, first)
        # Temporary capacity fixture, not proof of naturally earning 50 partners.
        monster = first['resData']['050002']['userMonsterList'][0]
        state = self.talent_state()
        state['monsters'] = [dict(deepcopy(monster), userMonsterId=str(i)) for i in range(1, 51)]
        self.save_talent_state(state)
        request.update(uniqueRequestId=3, itemCount=495)
        self.assertEqual(self.api({'050002': request})['venus_status'], 2)
        self.assertEqual(self.api({'140003': {'uniqueRequestId': 4, 'userFacilityId': 1}})['venus_status'], 1)
        request['itemCount'] = 490
        self.assertEqual(self.api({'050002': request})['venus_status'], 2)
        self.assertEqual(len(self.talent_state()['monsters']), 50)
        self.assertEqual(self.api({'140010': {'uniqueRequestId': 5, 'userFacilityId': 1}})['venus_status'], 1)
        for ident, balance in ((6, 490), (7, 440)):
            response = self.api({'050002': dict(request, uniqueRequestId=ident, playCount=10, itemCount=balance)})
            self.assertEqual(response['venus_status'], 1, response)
            self.assertEqual(len(response['resData']['050002']['userMonsterList']), 10)
        state = self.talent_state()
        self.assertEqual((len(state['monsters']), state['player']['point'], state['player']['unitLimitMax']), (70, 390, '70'))
        self.assertEqual(len({m['userMonsterId'] for m in state['monsters']}), 70)
        self.assertEqual(self.api({'050002': dict(request, uniqueRequestId=8, itemCount=390)})['venus_status'], 2)
        self.assertEqual(self.talent_state()['player']['point'], 390)

    def test_house_research_egg_whole_gift_capacity_and_native_completion_chain(self):
        from copy import deepcopy
        from unittest.mock import patch
        from datetime import datetime, timezone
        request = self.prepare_research()
        state = self.talent_state()
        # Capacity/partners/coins/stones/gift are isolated fixtures, not a claim
        # that 50 partners were naturally captured. Research/upgrade/claim/hatch
        # and growth below use the actual transactional API contracts.
        template = state['monsters'][-1]
        state['monsters'].extend(dict(deepcopy(template), userMonsterId=str(i)) for i in range(7, 51))
        state['player'].update(unitLimitMax='50', point=10)
        state['facilities'].append(dict(userFacilityId=99, facilityId=7, level=1,
                                       positionX=17, positionY=11, completeTime=''))
        self.save_talent_state(state)
        gift = self.inject_gifts([{'assetCategoryId': '1', 'assetValue': '756', 'assetNum': '2',
                                  'exValue': '{"monsterFixedValueId":1104}'}])[0]
        receive = dict(receiveType=2, receiveIds=[gift])
        self.assertEqual(self.chip_call('070002', 10, **receive)['warningData'], [gift])
        self.chip_call('140003', 11, userFacilityId=99)
        self.assertEqual((self.talent_state()['player']['unitLimitMax'], self.talent_state()['player']['point']), ('50', 5))
        self.assertEqual(self.chip_call('070002', 12, **receive)['warningData'], [gift])
        egg = self.chip_call('010501', 13, baseUserMonsterId=request['baseUserMonsterId'],
                             materialUserMonsterId=request['materialUserMonsterId'])['userMonster']
        self.assertEqual((egg['userMonsterId'], egg['eggFlg'], len(self.talent_state()['monsters'])), ('51', '1', 49))
        # A research egg counts as one owned partner; the two-partner gift stays
        # whole at49/50 rather than granting one and silently losing the other.
        self.assertEqual(self.chip_call('070001', 14, page=1)['prizeData'][0]['isAssetNumLimitOver'], 1)
        self.assertEqual(self.chip_call('070002', 15, **receive)['warningData'], [gift])
        self.chip_call('010304', 16, userMonsterId='51')
        before = self.talent_state()
        rolled = self.api({'140010': dict(uniqueRequestId=17, userFacilityId=99),
                           '070002': dict(receive, uniqueRequestId=17), '999999': {'uniqueRequestId': 17}})
        self.assertEqual(rolled['venus_status'], 2)
        self.assertEqual(self.talent_state(), before)
        done = self.api({'140010': dict(uniqueRequestId=18, userFacilityId=99),
                         '070002': dict(receive, uniqueRequestId=18)})
        self.assertEqual(done['venus_status'], 1, done)
        self.assertEqual(done['resData']['070002']['prizeReceiveIds'], [gift])
        state = self.talent_state()
        self.assertEqual((state['player']['unitLimitMax'], len(state['monsters'])), ('70', 51))
        self.assertEqual([m['userMonsterId'] for m in state['monsters'][-2:]], ['52', '53'])
        self.assertEqual(self.api({'140010': dict(uniqueRequestId=18, userFacilityId=99),
                                  '070002': dict(receive, uniqueRequestId=18)}), done)
        for expected in ('27', '82'):
            baby = next(m for m in self.talent_state()['monsters'] if m['userMonsterId'] == '51')
            end = int(datetime.strptime(baby['growEndDate'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).timestamp())
            with patch('linkz_server.time.time', return_value=end):
                grown = self.chip_call('010302', int(expected) + 100, userMonsterId='51', shorteningFlg=0, stone=0)
            self.assertEqual(grown['userMonster']['monsterId'], expected)
            self.assertEqual(len(self.talent_state()['monsters']), 51)
        self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
        self.auth()
        self.assertEqual(self.chip_call('070002', 200, **receive)['prizeReceiveIds'], [gift])
        replay = self.chip_call('010501', 201, baseUserMonsterId='1', materialUserMonsterId='2')['userMonster']
        self.assertEqual((replay['userMonsterId'], replay['monsterId']), ('51', '82'))
        state = self.talent_state()
        self.assertEqual(len(state['monsters']), 51)
        self.assertEqual(len({m['userMonsterId'] for m in state['monsters']}), 51)
        self.assertTrue({'1', '2'}.isdisjoint(m['userMonsterId'] for m in state['monsters']))
        self.assertEqual((state['normal_research_count'], state['normal_hatching_count']), (1, 1))

    def test_restaurant_completion_at_natural_recovery_boundary_no_refill_on_replay(self):
        from unittest.mock import patch
        from datetime import datetime, timezone
        self.prepare_normal_battle()
        now = int(time.time())
        state = self.talent_state()
        # Existing40 cap with one point due exactly when the temporary pending
        # level2 restaurant completes. Advance only the test process clock.
        state['player'].update(stamina=39, staminaMax='40', recovery=10)
        state['stamina_full_at'] = now + 10
        state['facilities'].append(dict(userFacilityId=99, facilityId=3, level=1,
            positionX=10, positionY=10, completeTime=datetime.fromtimestamp(now + 10, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')))
        self.save_talent_state(state)
        complete = dict(uniqueRequestId=10, userFacilityId=99)
        with patch('linkz_server.time.time', return_value=now + 9):
            self.assertEqual(self.api({'140010': complete})['venus_status'], 2)
            self.assertEqual(self.talent_state(), state)
        with patch('linkz_server.time.time', return_value=now + 10):
            self.assertEqual(self.api({'140010': complete, '999999': {'uniqueRequestId': 10}})['venus_status'], 2)
            self.assertEqual(self.talent_state(), state)
            self.chip_call('140010', 10, userFacilityId=99)
            player = self.chip_call('020001', 11)['playerInfo']
            self.assertEqual((player['stamina'], player['staminaMax'], player['recovery']), (45, '45', 0))
            self.assertNotIn('stamina_full_at', self.talent_state())
        with patch('linkz_server.time.time', return_value=now + 11):
            self.chip_call('090002', 12, dungeonId='1', deckNum='1')
            player = self.chip_call('020001', 13)['playerInfo']
            self.assertEqual((player['stamina'], player['recovery']), (43, 360))
            self.chip_call('140010', 10, userFacilityId=99)
        with patch('linkz_server.time.time', return_value=now + 191):
            self.chip_call('140010', 14, userFacilityId=99)
            self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
            self.auth()
            self.chip_call('140010', 15, userFacilityId=99)
            player = self.chip_call('020001', 16)['playerInfo']
            self.assertEqual((player['stamina'], player['staminaMax'], player['recovery']), (44, '45', 180))
            self.assertEqual(self.talent_state()['stamina_full_at'], now + 371)

    def test_battle_friendship_probability_snapshot_cap_and_native_monster_refresh(self):
        from unittest.mock import patch
        self.prepare_friendship()
        store = self.httpd.store
        state = self.talent_state()
        for monster, friendship in zip(state['monsters'], ('29', '59', '90')):
            monster['friendship'] = friendship
            store.add_monster_experience(monster, 0)
        state['friendship_ready_at'] = {'1': int(time.time()) + 86400}
        state['player']['meatNum'] = '1'
        self.save_talent_state(state)
        start = self.api({'090002': {'uniqueRequestId': 10, 'dungeonId': '1', 'deckNum': '1'}})['resData']['090002']
        # The settled award uses entry rules, even after a local config edit.
        store.local_gameplay['battleFriendship'].update(probabilityBasisPoints=0, gainOnSuccess=29)
        request = self.battle_result_request(start, 10)
        request['aliveInfo'] = [1, 1, 1]
        control = json.loads(json.dumps(state['monsters'][0]))
        exp = sum(e['fixedExp'] for f in start['dungeonFloor'] for e in f['enemy'])
        store.add_monster_experience(control, exp)
        with patch('linkz_server.secrets.randbelow', side_effect=[2999, 3000]) as roll:
            response = self.api({'090003': request, '020101': {'uniqueRequestId': 20, 'userMonsterIds': [1, 2, 3]}})
        self.assertEqual(response['venus_status'], 1, response)
        self.assertEqual(roll.call_args_list, [unittest.mock.call(10000)] * 2)
        monsters = response['resData']['020101']['userMonsterList']
        self.assertEqual([m['friendship'] for m in monsters], ['30', '59', '90'])
        master = next(m for m in store.masters['990102']['monsterM'] if m['monsterId'] == control['monsterId'])
        for field in ('hp', 'attack', 'defense', 'spAttack', 'spDefense', 'speed'):
            maximum = int(master['speed' if field == 'speed' else 'max' + field[0].upper() + field[1:]])
            self.assertEqual(int(monsters[0][field]), int(control[field]) + maximum // 100, field)
        after = self.talent_state()
        self.assertEqual(after['friendship_ready_at'], state['friendship_ready_at'])
        self.assertEqual(after['active_battle']['friendship_gains'], {'1': 1, '2': 0, '3': 0})
        self.assertNotIn('friendship_gains', response['resData']['090003'])
        # A later meal recalculates level/talent stats but retains this threshold.
        meal = self.api({'010202': {'uniqueRequestId': 21, 'mealMonster': '1', 'meatNum': 1}})
        self.assertEqual(meal['venus_status'], 1, meal)
        monster = meal['resData']['010202']['userMonster']
        self.assertEqual(monster['friendship'], '30')
        self.assertEqual(monster['hpAbility'], monsters[0]['hpAbility'])
        self.assertGreater(int(monster['hp']), int(control['hp']))

    def test_battle_friendship_rollback_replay_reauth_and_historical_receipt(self):
        from unittest.mock import patch
        self.prepare_friendship()
        start = self.api({'090002': {'uniqueRequestId': 10, 'dungeonId': '1', 'deckNum': '1'}})['resData']['090002']
        request = self.battle_result_request(start, 10)
        request['aliveInfo'] = [1, 0, 1]
        before = self.talent_state()
        with patch('linkz_server.secrets.randbelow', return_value=0):
            failed = self.api({'090003': request, '999999': {'uniqueRequestId': 1}})
        self.assertEqual(failed['venus_status'], 2)
        after = self.talent_state()
        for key in ('monsters', 'souls', 'player', 'active_battle'):
            self.assertEqual(after[key], before[key], key)
        with patch('linkz_server.secrets.randbelow', return_value=0) as roll:
            result = self.api({'090003': request})
        self.assertEqual(result['venus_status'], 1, result)
        self.assertEqual(roll.call_count, 2)
        self.assertEqual([m['friendship'] for m in self.talent_state()['monsters']], ['1', '0', '1'])
        with patch('linkz_server.secrets.randbelow', side_effect=AssertionError('receipt must not reroll')):
            self.assertEqual(self.api({'090003': request}), result)
            self.assertEqual(self.api({'090003': dict(request, uniqueRequestId=11)}), result)
            self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
            self.auth()
            self.assertEqual(self.api({'090003': dict(request, uniqueRequestId=1)}), result)
        next_start = self.api({'090002': {'uniqueRequestId': 12, 'dungeonId': '1', 'deckNum': '1'}})['resData']['090002']
        self.assertNotEqual(next_start['startId'], start['startId'])
        snapshot = self.talent_state()
        with patch('linkz_server.secrets.randbelow', side_effect=AssertionError('history must not reroll')):
            self.assertEqual(self.api({'090003': dict(request, uniqueRequestId=2)}), result)
        self.assertEqual(self.talent_state()['monsters'], snapshot['monsters'])
        # Real time can reduce the recovery countdown between requests.
        # A historical receipt must preserve awards and paid stamina instead.
        for key in ('gamemoney', 'point', 'friendPoint', 'stamina'):
            self.assertEqual(self.talent_state()['player'][key], snapshot['player'][key])
        from concurrent.futures import ThreadPoolExecutor
        next_request = self.battle_result_request(next_start, 3)
        next_request['aliveInfo'] = [1, 0, 1]
        with patch('linkz_server.secrets.randbelow', return_value=0) as roll:
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda n: self.api({'090003': dict(next_request, uniqueRequestId=n)}), (3, 4)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0]['venus_status'], 1, results)
        self.assertEqual(roll.call_count, 2)
        self.assertEqual([m['friendship'] for m in self.talent_state()['monsters']], ['2', '0', '2'])
        self.assertEqual(self.talent_state()['normal_clear_count'], 2)

    def test_battle_friendship_loss_withdraw_and_legacy_encounter_no_awards(self):
        from unittest.mock import patch
        self.prepare_friendship()
        for count, clear in enumerate((0, 2, 1), 10):
            start = self.api({'090002': {'uniqueRequestId': count, 'dungeonId': '1', 'deckNum': '1'}})['resData']['090002']
            if clear == 1:
                state = self.talent_state()
                state['active_battle'].pop('friendship_rules')
                self.save_talent_state(state)
            request = self.battle_result_request(start, count, clear)
            request['aliveInfo'] = [1, 0, 1] if clear != 0 else [0, 0, 0]
            with patch('linkz_server.secrets.randbelow', side_effect=AssertionError('not eligible to roll')):
                response = self.api({'090003': request})
            self.assertEqual(response['venus_status'], 1, response)
            self.assertEqual([m['friendship'] for m in self.talent_state()['monsters']], ['0', '0', '0'])
            self.assertNotIn('friendship_gains', self.talent_state()['active_battle'])

    def test_battle_friendship_zero_and_guaranteed_rules_with_near_cap(self):
        from unittest.mock import patch
        self.prepare_friendship()
        for count, chance in enumerate((0, 10000), 10):
            self.httpd.store.local_gameplay['battleFriendship'].update(probabilityBasisPoints=chance, gainOnSuccess=29)
            state = self.talent_state()
            for monster in state['monsters']:
                monster['friendship'] = '89'
                self.httpd.store.add_monster_experience(monster, 0)
            self.save_talent_state(state)
            start = self.api({'090002': {'uniqueRequestId': count, 'dungeonId': '1', 'deckNum': '1'}})['resData']['090002']
            request = self.battle_result_request(start, count)
            request['aliveInfo'] = [1, 1, 1]
            with patch('linkz_server.secrets.randbelow', side_effect=AssertionError('0/100% does not need a roll')):
                response = self.api({'090003': request})
            self.assertEqual(response['venus_status'], 1, response)
            self.assertEqual([m['friendship'] for m in self.talent_state()['monsters']], ['90' if chance else '89'] * 3)
            self.assertEqual(self.talent_state()['active_battle']['friendship_gains'], dict.fromkeys(('1', '2', '3'), int(bool(chance))))

    def test_invalid_battle_friendship_configuration_prevents_entry_without_debit(self):
        self.prepare_normal_battle()
        state = self.talent_state()
        for count, config in enumerate(({}, None, {'probabilityBasisPoints': True, 'gainOnSuccess': 1},
                {'probabilityBasisPoints': -1, 'gainOnSuccess': 1}, {'probabilityBasisPoints': 10001, 'gainOnSuccess': 1},
                {'probabilityBasisPoints': 3000, 'gainOnSuccess': True}, {'probabilityBasisPoints': 3000, 'gainOnSuccess': 0},
                {'probabilityBasisPoints': 3000, 'gainOnSuccess': 30}), 10):
            self.httpd.store.local_gameplay['battleFriendship'] = config
            result = self.api({'090002': {'uniqueRequestId': count, 'dungeonId': '1', 'deckNum': '1'}})
            self.assertEqual(result['venus_status'], 2, config)
            after = self.talent_state()
            self.assertEqual(after['player']['stamina'], state['player']['stamina'])
            self.assertNotIn('active_battle', after)

    def test_normal_battle_expiry_boundary_fresh_resume_and_no_reward(self):
        from unittest.mock import patch
        from linkz_server import NORMAL_BATTLE_LIFETIME_SECONDS, refresh_battle_expiry
        self.prepare_normal_battle()
        now = int(time.time())
        start_request = {'uniqueRequestId': 10, 'dungeonId': '1', 'deckNum': '1'}
        resume = {'uniqueRequestId': 10}
        with patch('linkz_server.time.time', return_value=now):
            start = self.api({'090002': start_request})['resData']['090002']
            self.assertEqual(self.api({'090012': resume})['resData']['090012']['startId'], start['startId'])
        before = self.talent_state()
        deadline = now + NORMAL_BATTLE_LIFETIME_SECONDS
        self.assertEqual(before['active_battle']['expires_at'], deadline)
        with patch('linkz_server.time.time', return_value=deadline - 1):
            self.assertEqual(self.api({'090012': resume})['resData']['090012']['startId'], start['startId'])
        # Pure expiration changes only encounter lifecycle, not paid stamina.
        control = json.loads(json.dumps(before))
        with patch('linkz_server.time.time', return_value=deadline):
            refresh_battle_expiry(control)
            self.assertEqual(control['player'], before['player'])
            result = self.api({'090012': resume})
        self.assertEqual(result['resData']['090012'], {'startId': None, 'userDungeonTicketId': None})
        after = self.talent_state()
        self.assertTrue(after['active_battle']['expired'])
        self.assertTrue(after['active_battle']['settled'])
        self.assertEqual(after['active_battle']['response'], start)
        self.assertNotIn('result', after['active_battle'])
        with patch('linkz_server.time.time', return_value=deadline), patch('linkz_server.secrets.randbelow', side_effect=AssertionError('expired cannot roll')):
            self.assertEqual(self.api({'090003': self.battle_result_request(start, 20)})['venus_status'], 2)
            self.assertEqual(self.api({'090002': start_request})['venus_status'], 2)
        after = self.talent_state()
        self.assertEqual(after['monsters'], before['monsters'])
        self.assertEqual(after['souls'], before['souls'])
        self.assertEqual(after['player']['gamemoney'], before['player']['gamemoney'])
        self.assertNotIn('claimed_dungeon_rewards', after)
        with patch('linkz_server.time.time', return_value=deadline):
            new = self.api({'090002': dict(start_request, uniqueRequestId=11)})['resData']['090002']
        self.assertNotEqual(new['startId'], start['startId'])
        self.assertTrue(self.talent_state()['battle_history'][start['startId']]['expired'])

    def test_normal_battle_expiry_legacy_anchor_rollback_and_release_training(self):
        from datetime import datetime, timezone
        from unittest.mock import patch
        from linkz_server import NORMAL_BATTLE_LIFETIME_SECONDS
        self.prepare_friendship()
        start = self.api({'090002': {'uniqueRequestId': 10, 'dungeonId': '1', 'deckNum': '1'}})['resData']['090002']
        state = self.talent_state()
        now = int(time.time())
        state['active_battle'].pop('expires_at')
        state['active_battle'].pop('friendship_rules')
        state['active_battle']['started'] = datetime.fromtimestamp(now - NORMAL_BATTLE_LIFETIME_SECONDS, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        self.save_talent_state(state)
        with patch('linkz_server.time.time', return_value=now):
            self.assertEqual(self.api({'090012': {'uniqueRequestId': 11}, '999999': {'uniqueRequestId': 1}})['venus_status'], 2)
            self.assertFalse(self.talent_state()['active_battle']['settled'])
            result = self.api({'010701': {'uniqueRequestId': 11, 'userMonsterId': '1'}})
        self.assertEqual(result['venus_status'], 1, result)
        self.assertEqual(result['resData']['010701']['upFriendship'], 1)
        self.assertTrue(self.talent_state()['active_battle']['expired'])
        self.assertEqual(self.talent_state()['active_battle']['started'], state['active_battle']['started'])
        self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
        self.auth()
        self.assertIsNone(self.api({'090012': {'uniqueRequestId': 1}})['resData']['090012']['startId'])
        self.assertEqual(self.api({'090003': self.battle_result_request(start, 1)})['venus_status'], 2)

    def test_archived_battle_compaction_keeps_reauth_receipts_expiry_and_rollback(self):
        import copy
        import statistics
        from unittest.mock import patch
        self.prepare_normal_battle()
        start = self.api({'090002': dict(uniqueRequestId=10, dungeonId='1', deckNum='1')})['resData']['090002']
        request = self.battle_result_request(start, 10)
        result = self.api({'090003': request})
        self.assertEqual(result['venus_status'], 1)
        state = self.talent_state()
        template = copy.deepcopy(state['active_battle'])
        # Synthetic legacy history for migration/size/time measurements only.
        # No extra rewards, currencies, progress or player data are granted.
        history = {}
        for ident in range(1000000, 1000128):
            record = copy.deepcopy(template)
            record['response']['startId'] = str(ident)
            record['result_input']['startId'] = str(ident)
            history[str(ident)] = record
        expired = copy.deepcopy(template)
        expired.update(expired=True)
        expired.pop('result'); expired.pop('result_input')
        expired['response']['startId'] = '2000000'
        history['2000000'] = expired
        unfinished = copy.deepcopy(template)
        unfinished.update(settled=False)
        unfinished.pop('result'); unfinished.pop('result_input')
        unfinished['response']['startId'] = '3000000'
        history['3000000'] = unfinished
        state['battle_history'] = history
        self.save_talent_state(state)
        before = self.talent_state()
        rolled = self.api({'020001': dict(uniqueRequestId=20), '999999': dict(uniqueRequestId=20)})
        self.assertEqual(rolled['venus_status'], 2)
        self.assertEqual(self.talent_state(), before)
        read_before, read_after = [], []
        # Compare actual loopback reads with the same legacy history, disabling
        # only the new migration during the baseline. This is not live latency.
        with patch('linkz_server.compact_battle_history', return_value=None):
            for ident in range(30, 37):
                began = time.perf_counter()
                self.assertEqual(self.api({'020001': dict(uniqueRequestId=ident)})['venus_status'], 1)
                read_before.append(time.perf_counter() - began)
        self.assertEqual(self.api({'020001': dict(uniqueRequestId=21)})['venus_status'], 1)
        after = self.talent_state()
        for ident in range(40, 47):
            began = time.perf_counter()
            self.assertEqual(self.api({'020001': dict(uniqueRequestId=ident)})['venus_status'], 1)
            read_after.append(time.perf_counter() - began)
        self.assertEqual(after['active_battle'], before['active_battle'])
        self.assertEqual(after['battle_history']['3000000'], unfinished)
        self.assertTrue(after['battle_history']['2000000']['expired'])
        for ident in map(str, range(1000000, 1000128)):
            self.assertEqual(after['battle_history'][ident]['result_input'], history[ident]['result_input'])
            self.assertEqual(after['battle_history'][ident]['result'], history[ident]['result'])
            self.assertNotIn('dungeonFloor', after['battle_history'][ident]['response'])
            self.assertNotIn('chip_snapshot', after['battle_history'][ident])
        for key in ('monsters', 'souls', 'normal_clear_count', 'claimed_dungeon_rewards'):
            self.assertEqual(after[key], before[key])
        before_json = json.dumps(before['battle_history'])
        after_json = json.dumps(after['battle_history'])
        self.assertLess(len(after_json), len(before_json) * 0.4)
        times = []
        for text in (before_json, after_json):
            samples = []
            for _ in range(7):
                began = time.perf_counter()
                json.dumps(json.loads(text))
                samples.append(time.perf_counter() - began)
            times.append(statistics.median(samples))
        # Starting another encounter archives the previous one compactly too.
        new = self.api({'090002': dict(uniqueRequestId=22, dungeonId='1', deckNum='1')})['resData']['090002']
        self.assertNotEqual(new['startId'], start['startId'])
        self.assertNotIn('dungeonFloor', self.talent_state()['battle_history'][start['startId']]['response'])
        self.assertEqual(self.api({'090003': dict(request, uniqueRequestId=23)}), result)
        self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
        self.auth()
        before_retry = self.talent_state()
        self.assertEqual(self.api({'090003': dict(request, uniqueRequestId=1)}), result)
        synthetic_retry = dict(request, startId='1000000', uniqueRequestId=2)
        self.assertEqual(self.api({'090003': synthetic_retry}), result)
        self.assertEqual(self.api({'090003': dict(request, startId='2000000', uniqueRequestId=3)})['venus_status'], 2)
        final = self.talent_state()
        for key in ('gamemoney', 'point', 'friendPoint'):
            self.assertEqual(final['player'][key], before_retry['player'][key])
        self.assertEqual(final['monsters'], before_retry['monsters'])
        self.assertEqual(final['souls'], before_retry['souls'])
        print('BATTLE_HISTORY_COMPACTION ' + json.dumps(dict(
            fixtureArchivedReceipts=128, beforeBytes=len(before_json.encode('utf-8')),
            afterBytes=len(after_json.encode('utf-8')), jsonRoundtripMedianSecondsBefore=times[0],
            jsonRoundtripMedianSecondsAfter=times[1], nativeReadMedianSecondsBefore=statistics.median(read_before),
            nativeReadMedianSecondsAfter=statistics.median(read_after),
            scope='synthetic128-history JSON/native loopback read timing; receipt/re-auth/expiry/rollback checks'),
            ensure_ascii=False), flush=True)

    def test_successful_battle_receipts_survive_expiry_and_resume_changes_after_settlement(self):
        from unittest.mock import patch
        from linkz_server import NORMAL_BATTLE_LIFETIME_SECONDS
        self.prepare_normal_battle()
        now = int(time.time())
        with patch('linkz_server.time.time', return_value=now):
            start = self.api({'090002': {'uniqueRequestId': 10, 'dungeonId': '1', 'deckNum': '1'}})['resData']['090002']
            self.api({'090012': {'uniqueRequestId': 10}})
            request = self.battle_result_request(start, 10)
            result = self.api({'090003': request})
            self.assertEqual(result['venus_status'], 1, result)
            self.assertIsNone(self.api({'090012': {'uniqueRequestId': 10}})['resData']['090012']['startId'])
        before = self.talent_state()
        with patch('linkz_server.time.time', return_value=now + NORMAL_BATTLE_LIFETIME_SECONDS + 1), patch('linkz_server.secrets.randbelow', side_effect=AssertionError('receipt must not roll')):
            self.assertEqual(self.api({'090003': request}), result)
            self.assertEqual(self.api({'090003': dict(request, uniqueRequestId=11)}), result)
        after = self.talent_state()
        self.assertFalse(after['active_battle'].get('expired', False))
        self.assertEqual(after['monsters'], before['monsters'])
        for key in ('gamemoney', 'point', 'friendPoint'):
            self.assertEqual(after['player'][key], before['player'][key])

    def test_help_masters_chinese_and_original_language_contract(self):
        self.auth()
        translated = self.api({api: {'uniqueRequestId': 1, 'countryCode': 3}
                               for api in ('991001', '991002')})
        self.assertEqual(translated['venus_status'], 1, translated)
        rows = translated['resData']['991002']['helpM']
        self.assertEqual(len(rows), 99)
        gift = next(r for r in rows if r['helpId'] == '13')
        self.assertEqual(gift['helpTitle'], '领取礼物')
        self.assertIn('30天', gift['detail'])
        self.assertNotIn('<img', gift['detail'])
        self.assertEqual(len(translated['resData']['991001']['helpCategoryM']), 14)
        for country in (1, 2):
            result = self.api({api: {'uniqueRequestId': country + 1, 'countryCode': country}
                               for api in ('991001', '991002')})
            self.assertEqual(result['venus_status'], 1, result)
            for api in ('991001', '991002'):
                self.assertEqual(result['resData'][api], self.httpd.store.masters[api])

    def mission_info(self, request_id):
        response = self.api({'110101': {'uniqueRequestId': request_id, 'countryCode': 2}})
        self.assertEqual(response['venus_status'], 1, response)
        return {int(row['missionId']): row for group in response['resData']['110101']['result'].values() for row in group}

    def inject_gifts(self, rewards):
        state = self.talent_state()
        ids = [self.httpd.store.progression.add_gift(state, reward, '临时库测试礼物', 'test') for reward in rewards]
        self.save_talent_state(state)
        return ids

    def test_missions_battle_progress_gift_claim_inventory_and_reauth(self):
        self.prepare_normal_battle()
        first = self.mission_info(2)
        self.assertEqual(len(first), 16)
        self.assertTrue({10006, 10007, 10008} <= set(first))
        self.assertTrue(all(r['status'] == 0 for r in first.values()))
        for number in range(5):
            start = self.api({'090002': {'uniqueRequestId': number + 10, 'dungeonId': '1', 'deckNum': '1'}})['resData']['090002']
            result = self.api({'090003': self.battle_result_request(start, number + 10)})
            self.assertEqual(result['venus_status'], 1, result)
        rows = self.mission_info(20)
        claim_ids = [10001, 10002, 10005, 30001]
        self.assertTrue(all(rows[i]['status'] == 1 for i in claim_ids))
        self.assertEqual(rows[10005]['detail']['missionValue'], '5')
        before = self.talent_state()
        claim = {'uniqueRequestId': 21, 'missionId': claim_ids}
        response = self.api({'110102': claim})
        self.assertEqual(response['venus_status'], 1, response)
        self.assertEqual(len(response['resData']['110102']['result']), 4)
        for field in ('point', 'friendPoint', 'gamemoney', 'meatNum'):
            self.assertEqual(self.talent_state()['player'][field], before['player'][field])  # rewards only in gift box
        self.assertEqual(self.api({'110102': claim}), response)
        listed = self.api({'070001': {'uniqueRequestId': 22, 'page': 1}, '000002': {'uniqueRequestId': 22}})['resData']
        self.assertEqual(listed['070001']['prizeTotalCount'], '4')
        self.assertEqual(listed['000002']['userNewsCountList']['prize'], '4')
        ids = [r['receiveId'] for r in listed['070001']['prizeData']]
        receive = {'uniqueRequestId': 23, 'receiveType': 2, 'receiveIds': ids}
        received = self.api({'070002': receive})
        self.assertEqual(received['resData']['070002'], {'prizeReceiveIds': ids, 'isWarning': 0, 'warningData': [], 'resultCode': 1})
        after = self.talent_state()
        self.assertEqual(int(after['player']['gamemoney']) - int(before['player']['gamemoney']), 1500)
        self.assertEqual(after['player']['point'] - before['player']['point'], 1)
        self.assertEqual(next(r['num'] for r in after['souls'] if r['soulId'] == '2010003'), '5')
        self.assertEqual(after['prizes'], [])
        self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
        self.auth()
        self.assertEqual(self.api({'110102': dict(claim, uniqueRequestId=24)})['venus_status'], 1)
        self.assertEqual(self.api({'070002': dict(receive, uniqueRequestId=25)})['resData']['070002'], received['resData']['070002'])
        for field in ('point', 'friendPoint', 'gamemoney', 'meatNum'):
            self.assertEqual(self.talent_state()['player'][field], after['player'][field])
        history = self.api({'070003': {'uniqueRequestId': 26}})['resData']['070003']['prizeReceiveHistory']
        self.assertEqual(len(history), 4)
        self.assertTrue(all(r['receiveStatus'] == '1' and r['userId'] == '1' for r in history))
        self.assertTrue(all(self.mission_info(27)[i]['status'] == 2 for i in claim_ids))

    def test_mission_daily_rollover_baselines_and_lifetime_claims(self):
        from unittest.mock import patch
        self.prepare_training()
        state = self.talent_state()
        state['normal_clear_count'] = 5
        self.save_talent_state(state)
        with patch.object(self.httpd.store.progression, 'day_key', return_value='2026-10-01'):
            self.assertEqual(self.mission_info(2)[10001]['nowValue'], 1)
            response = self.api({'110102': {'uniqueRequestId': 3, 'missionId': [10001, 30001]}})
            self.assertEqual(response['venus_status'], 1, response)
            self.assertEqual(len(self.talent_state()['prizes']), 2)
        with patch.object(self.httpd.store.progression, 'day_key', return_value='2026-10-02'):
            rows = self.mission_info(4)
            self.assertEqual((rows[10001]['status'], rows[10001]['nowValue'], rows[30001]['status']), (0, 0, 2))
            self.assertEqual(self.api({'110102': {'uniqueRequestId': 5, 'missionId': [10001]}})['venus_status'], 2)
            state = self.talent_state()
            state['normal_clear_count'] = 6
            self.save_talent_state(state)
            self.assertEqual(self.mission_info(6)[10001]['status'], 1)
            self.assertEqual(self.api({'110102': {'uniqueRequestId': 7, 'missionId': [10001]}})['venus_status'], 1)
            self.assertEqual(len(self.talent_state()['prizes']), 3)
        state = self.talent_state()
        self.assertEqual(set(state['mission_claims']), {'10001@2026-10-01', '10001@2026-10-02', '30001'})
        # Upgraded old save starts today's daily baseline at existing count.
        state.pop('mission_daily')
        self.save_talent_state(state)
        self.assertEqual(self.mission_info(8)[10001]['nowValue'], 0)

    def test_mission_batch_crossing_midnight_keeps_one_daily_snapshot(self):
        from unittest.mock import patch
        self.prepare_normal_battle()
        start = self.api({'090002': {'uniqueRequestId': 2, 'dungeonId': '1', 'deckNum': '1'}})['resData']['090002']
        progression = self.httpd.store.progression
        with patch.object(progression, 'day_key', side_effect=['2026-10-01', '2026-10-02']) as clock:
            response = self.api({'090003': self.battle_result_request(start, 3),
                '110101': {'uniqueRequestId': 3, 'countryCode': 2},
                '110102': {'uniqueRequestId': 3, 'missionId': [10001]},
                '000002': {'uniqueRequestId': 3}})
            self.assertEqual(response['venus_status'], 1, response)
            self.assertEqual(clock.call_count, 1)
            self.assertEqual(response['resData']['110101']['result']['daily'][0]['nowValue'], 1)
            self.assertEqual(set(self.talent_state()['mission_claims']), {'10001@2026-10-01'})
            next_day = self.mission_info(4)
            self.assertEqual((next_day[10001]['status'], next_day[10001]['nowValue']), (0, 0))
            self.assertEqual(clock.call_count, 2)
        self.assertEqual(len(self.talent_state()['prizes']), 1)

    def test_mission_incomplete_invalid_atomic_claims_and_no_client_clear(self):
        self.prepare_training()
        for ids in ([], [10001, 10001], [True], [99999], ['10001'], None):
            self.assertEqual(self.api({'110102': {'uniqueRequestId': 2, 'missionId': ids}})['venus_status'], 2)
        state = self.talent_state()
        state['normal_capture_count'] = 1
        self.save_talent_state(state)
        self.assertEqual(self.api({'110102': {'uniqueRequestId': 3, 'missionId': [10004, 10005]}})['venus_status'], 2)
        self.assertNotIn('prizes', self.talent_state())
        self.assertEqual(self.api({'110102': {'uniqueRequestId': 4, 'missionId': [10004]}, '999999': {'uniqueRequestId': 4}})['venus_status'], 2)
        self.assertNotIn('mission_claims', self.talent_state())
        self.assertEqual(self.api({'110103': {'uniqueRequestId': 5, 'missionCategoryId': 1}})['venus_status'], 2)
        self.auth('other-mission-owner')
        self.assertEqual(self.api({'110102': {'uniqueRequestId': 6, 'missionId': [10004]}})['venus_status'], 2)

    def test_gift_partial_capacity_limits_and_later_receive(self):
        self.prepare_training()
        state = self.talent_state()
        state['player'].update(gamemoney='9999994', point=9998, friendPoint='999998', meatNum='49')
        self.save_talent_state(state)
        ids = self.inject_gifts([{'assetCategoryId': category, 'assetValue': '0', 'assetNum': amount}
            for category, amount in (('4', '10'), ('2', '2'), ('3', '2'), ('13', '2'), ('4', '3'))])
        rows = self.api({'070001': {'uniqueRequestId': 2, 'page': 1}})['resData']['070001']['prizeData']
        self.assertEqual([r['isAssetNumLimitOver'] for r in rows], [1, 1, 1, 1, 0])
        request = {'uniqueRequestId': 3, 'receiveType': 2, 'receiveIds': ids}
        response = self.api({'070002': request})['resData']['070002']
        self.assertEqual((response['prizeReceiveIds'], response['warningData'], response['isWarning']), ([ids[-1]], ids[:-1], 1))
        state = self.talent_state()
        self.assertEqual((len(state['prizes']), state['player']['gamemoney']), (4, '9999997'))
        state['player'].update(gamemoney='100', point=10, friendPoint='0', meatNum='0')
        self.save_talent_state(state)
        received = self.api({'070002': dict(request, uniqueRequestId=4)})['resData']['070002']
        self.assertEqual(received['prizeReceiveIds'], ids)
        state = self.talent_state()
        self.assertEqual((state['player']['gamemoney'], state['player']['point'], state['player']['friendPoint'], state['player']['meatNum']), ('110', 12, '2', '2'))
        self.assertEqual(len(state['prize_history']), 5)

    def test_gift_expired_missing_and_malformed_ids_are_nonmutating(self):
        self.prepare_training()
        ids = self.inject_gifts([{'assetCategoryId': '4', 'assetValue': '0', 'assetNum': '10'}] * 2)
        state = self.talent_state()
        state['prizes'][0]['receiveLimitTime'] = '2000-01-01 00:00:00'
        self.save_talent_state(state)
        request = {'uniqueRequestId': 2, 'receiveType': 2, 'receiveIds': ids}
        for number, (changes, expected) in enumerate((({}, 90), ({'receiveIds': ['999']}, 91), ({'receiveType': True}, 92), ({'receiveType': 1}, 92)), 30):
            response = self.api({'070002': dict(request, **changes, uniqueRequestId=number)})
            self.assertEqual(response['resData']['070002']['resultCode'], expected)
        for invalid in ([], [ids[1], ids[1]], [2], ['-1'], None):
            self.assertEqual(self.api({'070002': dict(request, receiveIds=invalid)})['venus_status'], 2)
        after = self.talent_state()
        self.assertEqual((after['player']['gamemoney'], len(after['prizes'])), ('100000', 2))
        self.assertEqual(self.api({'070001': {'uniqueRequestId': 3, 'page': 1}})['resData']['070001']['prizeTotalCount'], '1')
        for page in (0, -1, True, '1'):
            self.assertEqual(self.api({'070001': {'uniqueRequestId': 4, 'page': page}})['venus_status'], 2)

    def test_gift_pagination_history_monotonic_ids_and_owner_isolation(self):
        self.prepare_training()
        ids = self.inject_gifts([{'assetCategoryId': '14', 'assetValue': '2010003', 'assetNum': '1'}] * 23)
        first = self.api({'070001': {'uniqueRequestId': 2, 'page': 1}})['resData']['070001']
        second = self.api({'070001': {'uniqueRequestId': 3, 'page': 2}})['resData']['070001']
        self.assertEqual((len(first['prizeData']), len(second['prizeData']), first['prizeTotalCount']), (20, 3, '23'))
        self.api({'070002': {'uniqueRequestId': 4, 'receiveType': 2, 'receiveIds': ids}})
        self.assertEqual(self.inject_gifts([{'assetCategoryId': '4', 'assetValue': '0', 'assetNum': '1'}]), ['24'])
        self.auth('other-prize-owner')
        self.assertEqual(self.api({'070001': {'uniqueRequestId': 5, 'page': 1}})['resData']['070001']['prizeTotalCount'], '0')
        self.assertEqual(self.api({'070002': {'uniqueRequestId': 6, 'receiveType': 2, 'receiveIds': ids}})['resData']['070002']['resultCode'], 91)
        self.assertEqual(self.api({'070003': {'uniqueRequestId': 7}})['resData']['070003']['prizeReceiveHistory'], [])

    def test_gift_monsters_materials_keys_chip_slots_and_capacity(self):
        self.prepare_training()
        state = self.talent_state()
        state['player']['unitLimitMax'] = '7'
        self.save_talent_state(state)
        rewards = [ {'assetCategoryId': '1', 'assetValue': '756', 'assetNum': '2', 'exValue': '{"monsterFixedValueId":1104}'},
                    {'assetCategoryId': '14', 'assetValue': '2010004', 'assetNum': '5'},
                    {'assetCategoryId': '16', 'assetValue': '1', 'assetNum': '1'}]
        ids = self.inject_gifts(rewards)
        receive = {'uniqueRequestId': 2, 'receiveType': 2, 'receiveIds': ids}
        response = self.api({'070002': receive})['resData']['070002']
        self.assertEqual((response['prizeReceiveIds'], response['warningData']), (ids[1:], ids[:1]))
        state = self.talent_state()
        self.assertEqual(state['facility_keys']['1'], 1)
        self.assertEqual(next(r['num'] for r in state['souls'] if r['soulId'] == '2010004'), '5')
        state['player']['unitLimitMax'] = '8'
        self.save_talent_state(state)
        self.assertEqual(self.api({'070002': dict(receive, uniqueRequestId=3)})['resData']['070002']['prizeReceiveIds'], ids)
        state = self.talent_state()
        self.assertEqual([m['userMonsterId'] for m in state['monsters'][-2:]], ['7', '8'])
        self.assertEqual([r['userMonsterId'] for r in state['chip_slots'][-2:]], [7, 8])
        self.assertTrue(all(m['commonSkillId'] == '473' for m in state['monsters'][-2:]))
        with self.assertRaisesRegex(Exception, 'not implemented'):
            self.httpd.store.progression.add_gift(state, dict(rewards[0], exValue='{"monsterFixedValueId":1036}'), 'invalid boss', 'test')

    def test_concurrent_mission_and_gift_receipts_and_batch_rollback(self):
        from concurrent.futures import ThreadPoolExecutor
        self.prepare_training()
        state = self.talent_state()
        state['normal_capture_count'] = 1
        self.save_talent_state(state)
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda n: self.api({'110102': {'uniqueRequestId': n, 'missionId': [10004]}}), (2, 3)))
        self.assertTrue(all(r['venus_status'] == 1 for r in responses), responses)
        state = self.talent_state()
        self.assertEqual(len(state['prizes']), 1)
        ident = state['prizes'][0]['receiveId']
        request = {'uniqueRequestId': 4, 'receiveType': 2, 'receiveIds': [ident]}
        self.assertEqual(self.api({'070002': request, '999999': {'uniqueRequestId': 4}})['venus_status'], 2)
        self.assertEqual(len(self.talent_state()['prizes']), 1)
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda n: self.api({'070002': dict(request, uniqueRequestId=n)}), (4, 5)))
        self.assertTrue(all(r['venus_status'] == 1 for r in responses), responses)
        state = self.talent_state()
        self.assertEqual((state['player']['friendPoint'], len(state['prize_history'])), ('60', 1))

    def test_capture_talent_probability_boundaries_all_stats_and_no_accumulation(self):
        import copy
        from unittest.mock import patch
        from linkz_server import ABILITY_FIELDS
        self.prepare_training()
        template = self.talent_state()['monsters'][0]
        for roll, value, flag in ((8999, '0', '0'), (9000, '5', '2'), (9899, '5', '2'), (9900, '15', '1'), (9999, '15', '1')):
            for index, field in enumerate(ABILITY_FIELDS):
                monster = copy.deepcopy(template)
                with patch('linkz_server.secrets.randbelow', side_effect=[roll, index]) as random:
                    self.httpd.store.roll_capture_talent(monster)
                self.assertEqual((monster[field + 'Ability'], monster[field + 'AbilityFlg']), (value, flag))
                self.assertEqual(sum(monster[f + 'Ability'] != '0' for f in ABILITY_FIELDS), int(value != '0'))
                self.assertEqual(random.call_count, 1 if value == '0' else 2)
                plain = copy.deepcopy(monster)
                plain[field + 'Ability'] = '0'
                self.httpd.store.add_monster_experience(plain, 0)
                self.assertEqual(int(monster[field]), int(plain[field]) + int(plain[field]) * int(value) // 100)
                self.httpd.store.add_monster_experience(monster, 0)
                self.assertEqual(int(monster[field]), int(plain[field]) + int(plain[field]) * int(value) // 100)

    def test_capture_talents_real_protocol_acquisition_garden_and_inheritance(self):
        from unittest.mock import patch
        self.prepare_training()
        state = self.talent_state()
        state['player']['friendPoint'] = '300'
        self.save_talent_state(state)
        menu = self.api({'050001': {'uniqueRequestId': 2, 'isTutorial': 0}})['resData']['050001']['result']
        for tab in menu:
            self.assertEqual(tab['details'][0]['appealText'], '才能：银Ⅰ9% / 金Ⅰ1%')
            self.assertEqual(tab['details'][0]['appealTextDisplayType'], '1')
        capture = {'uniqueRequestId': 3, 'gachaId': 10001, 'playCount': 1, 'itemCount': 10}
        with patch('linkz_server.secrets.choice', return_value='81'), patch('linkz_server.secrets.randbelow', side_effect=[9000, 0]):
            acquired = self.api({'050002': capture})
        self.assertEqual(acquired['venus_status'], 1, acquired)
        silver = acquired['resData']['050002']['userMonsterList'][0]
        self.assertEqual((silver['userMonsterId'], silver['hpAbility'], silver['hpAbilityFlg']), ('7', '5', '2'))
        with patch('linkz_server.secrets.randbelow', side_effect=AssertionError('same capture must not reroll')):
            self.assertEqual(self.api({'050002': capture}), acquired)
        with patch('linkz_server.secrets.randbelow', return_value=0):
            trained = self.api({'010502': {'uniqueRequestId': 4, 'baseUserMonsterId': '1', 'materialUserMonsterId': '7'}})
        self.assertEqual(trained['resData']['010502']['userMonster']['hpAbility'], '5')
        with patch('linkz_server.secrets.choice', return_value='16'), patch('linkz_server.secrets.randbelow', side_effect=[9900, 1]):
            acquired = self.api({'050002': dict(capture, uniqueRequestId=5, gachaId=10002, itemCount=300)})
        gold = acquired['resData']['050002']['userMonsterList'][0]
        self.assertEqual((gold['userMonsterId'], gold['attackAbility'], gold['attackAbilityFlg']), ('8', '15', '1'))
        route = next(r for r in self.httpd.store.masters['990110']['monsterEvolutionRouteM']
                     if r['monsterEvolutionRouteId'] == gold['monsterEvolutionRouteId'])
        self.assertEqual(self.api({'010901': {'uniqueRequestId': 6, 'userMonsterId': '8'}})['venus_status'], 1)
        for number, expected in ((7, route['childhood2MonsterId']), (8, route['growthMonsterId'])):
            state = self.talent_state()
            state['monsters'][-1]['growEndDate'] = '2000-01-01 00:00:00'
            self.save_talent_state(state)
            response = self.api({'010302': {'uniqueRequestId': number, 'userMonsterId': '8', 'shorteningFlg': 0, 'stone': 0}})
            self.assertEqual(response['resData']['010302']['userMonster']['monsterId'], expected)
            self.assertEqual(response['resData']['010302']['userMonster']['attackAbility'], '15')
        with patch('linkz_server.secrets.randbelow', return_value=100):
            fallback = self.api({'010502': {'uniqueRequestId': 9, 'baseUserMonsterId': '1', 'materialUserMonsterId': '8'}})
        self.assertEqual(fallback['resData']['010502']['userMonster']['attackAbility'], '5')
        self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
        self.auth()
        state = self.talent_state()
        self.assertEqual((state['player']['point'], state['player']['friendPoint'], state['player']['gamemoney']), (5, '0', '98500'))
        self.assertEqual((len(state['monsters']), state['normal_talent_inheritance_count']), (6, 2))

    def test_link_capture_pool_has_three_distinct_unawakened_families(self):
        from unittest.mock import patch
        self.prepare_training()
        state = self.talent_state()
        state['player']['friendPoint'] = '900'
        self.save_talent_state(state)
        captured = []
        for number, mid in enumerate(('1', '6', '16')):
            with patch('linkz_server.secrets.choice', return_value=mid), patch('linkz_server.secrets.randbelow', return_value=8999):
                response = self.api({'050002': {'uniqueRequestId': number + 2, 'gachaId': 10002,
                    'playCount': 1, 'itemCount': 900 - number * 300}})
            self.assertEqual(response['venus_status'], 1, response)
            captured.append(response['resData']['050002']['userMonsterList'][0])
        masters = {r['monsterId']: r for r in self.httpd.store.masters['990102']['monsterM']}
        self.assertEqual(len({masters[m['monsterId']]['monsterGroupId'] for m in captured}), 3)
        self.assertTrue(all(masters[m['monsterId']]['rare'] == '1' for m in captured))
        self.assertEqual(self.talent_state()['player']['friendPoint'], '0')

    def test_capture_talent_batch_failure_rolls_back_and_retry_keeps_result(self):
        from unittest.mock import patch
        self.prepare_training()
        request = {'uniqueRequestId': 2, 'gachaId': 10001, 'playCount': 1, 'itemCount': 10}
        with patch('linkz_server.secrets.randbelow', side_effect=[9900, 5]):
            failed = self.api({'050002': request, '999999': {'uniqueRequestId': 2}})
        self.assertEqual(failed['venus_status'], 2)
        state = self.talent_state()
        self.assertEqual((state['player']['point'], len(state['monsters'])), (10, 6))
        with patch('linkz_server.secrets.randbelow', side_effect=[9900, 5]):
            first = self.api({'050002': request})
        self.assertEqual(first['resData']['050002']['userMonsterList'][0]['speedAbility'], '15')
        with patch('linkz_server.secrets.randbelow', side_effect=AssertionError('receipt must not reroll')):
            self.assertEqual(self.api({'050002': request}), first)

    def prepare_research(self, base_rare=1, friendship=300, material_rare=1, birth_route='121'):
        self.prepare_training()
        state = self.talent_state()
        state['decks']['deckList'][0]['monsterList'] = [{'userMonsterId': '3'}, {'userMonsterId': '0'}, {'userMonsterId': '0'}]
        masters = self.httpd.store.masters['990102']['monsterM']
        for monster, rare, route in zip(state['monsters'][:2], (base_rare, material_rare), (birth_route, '31')):
            master = next(r for r in masters if r['monsterGroupId'] == '167' and r['rare'] == str(rare))
            monster.update(monsterId=master['monsterId'], monsterEvolutionRouteId=route, friendship=str(friendship))
            self.httpd.store.add_monster_experience(monster, 0)
        self.save_talent_state(state)
        return {'uniqueRequestId': 2, 'baseUserMonsterId': '1', 'materialUserMonsterId': '2'}

    def test_research_egg_inheritance_chips_hatch_growth_and_receipts(self):
        from unittest.mock import patch
        request = self.prepare_research()
        state = self.talent_state()
        state['monsters'][0].update(leaderSkillId='455', commonSkillId='500', extraCommonSkillId='400',
                                   hpAbility='9', hpAbilityFlg='2', attackAbility='18', attackAbilityFlg='1',
                                   luck='3', tranceResistance='1', tranceStatusAilment='2')
        state['chips'] = [{'userChipId': 10, 'userMonsterId': 1}, {'userChipId': 11, 'userMonsterId': 2}]
        state['farm_monsters'] = [1, 2, 3]
        state['friendship_ready_at'] = {'1': 2000000000, '2': 2000000000, '3': 2000000000}
        self.save_talent_state(state)
        first = self.api({'010501': request})
        self.assertEqual(first['venus_status'], 1, first)
        result = first['resData']['010501']
        self.assertEqual((result['newUserMonsterId'], result['eggMonsterId']), ('7', '10002'))
        egg = result['userMonster']
        self.assertEqual((egg['monsterId'], egg['monsterEvolutionRouteId'], egg['eggFlg'], egg['growEndDate']), ('17', '122', '1', ''))
        self.assertEqual((egg['commonSkillId'], egg['leaderSkillId'], egg['extraCommonSkillId'], egg['luck']), ('500', '455', '', '3'))
        self.assertEqual((egg['hpAbility'], egg['attackAbility'], egg['friendship'], egg['tranceResistance'], egg['tranceStatusAilment']), ('9', '18', '0', '', ''))
        after = self.talent_state()
        self.assertEqual((len(after['monsters']), after['player']['gamemoney']), (5, '97000'))
        self.assertEqual([c['userMonsterId'] for c in after['chips']], [0, 0])
        self.assertEqual(after['farm_monsters'], [3])
        self.assertEqual(after['friendship_ready_at'], {'3': 2000000000})
        self.assertEqual(after['chip_slots'][-1]['userMonsterId'], 7)
        self.assertEqual(self.api({'010501': request}), first)
        self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
        self.auth()
        self.assertEqual(self.api({'010501': dict(request, uniqueRequestId=3)})['resData']['010501'], result)
        hatch_request = {'uniqueRequestId': 4, 'userMonsterId': '7'}
        hatched = self.api({'010304': hatch_request})
        self.assertEqual(hatched['venus_status'], 1, hatched)
        childhood = hatched['resData']['010304']['userMonster']
        self.assertEqual((childhood['monsterId'], childhood['eggFlg']), ('17', '0'))
        self.assertNotEqual(childhood['growEndDate'], '')
        self.assertEqual(self.api({'010304': hatch_request}), hatched)
        for expected in ('27', '82'):
            state = self.talent_state()
            state['monsters'][-1]['growEndDate'] = '2000-01-01 00:00:00'
            self.save_talent_state(state)
            growth = self.api({'010302': {'uniqueRequestId': int(expected), 'userMonsterId': '7', 'shorteningFlg': 0, 'stone': 0}})
            self.assertEqual(growth['venus_status'], 1, growth)
            self.assertEqual(growth['resData']['010302']['userMonster']['monsterId'], expected)
        self.auth()
        with patch('linkz_server.secrets.randbelow', side_effect=AssertionError('replay must not create or roll')):
            replay = self.api({'010501': dict(request, uniqueRequestId=90), '010304': dict(hatch_request, uniqueRequestId=90)})
        self.assertEqual(replay['resData']['010501']['userMonster']['monsterId'], '82')
        self.assertEqual(replay['resData']['010304']['userMonster']['growEndDate'], '')
        final = self.talent_state()
        self.assertEqual((final['normal_research_count'], final['normal_hatching_count'], final['player']['gamemoney']), (1, 1, '97000'))
        self.assertEqual((final['monsters'][-1]['hpAbility'], final['monsters'][-1]['commonSkillId']), ('9', '500'))

    def test_research_awakening_boundaries_and_version_reset_cost(self):
        request = self.prepare_research(base_rare=6, material_rare=2)
        state = self.talent_state()
        state['monsters'][0]['extraCommonSkillId'] = '400'
        self.save_talent_state(state)
        response = self.api({'010501': request})
        self.assertEqual(response['venus_status'], 1, response)
        egg = response['resData']['010501']['userMonster']
        self.assertEqual((egg['monsterId'], egg['monsterEvolutionRouteId'], egg['extraCommonSkillId']), ('20', '125', ''))
        self.assertEqual(self.talent_state()['player']['gamemoney'], '84800')
        # Further pairs exercise full-friendship+4 cap and nonfull+2 unchanged.
        state = self.talent_state()
        for index, mid, friendship in ((0, '835', '300'), (1, '831', '0'), (2, '833', '299'), (3, '831', '0')):
            state['monsters'][index].update(monsterId=mid, monsterEvolutionRouteId='121', friendship=friendship)
        state['decks']['deckList'][0]['monsterList'] = [{'userMonsterId': '0'}] * 3
        self.save_talent_state(state)
        for base_id, material_id, child in (('3', '4', '20'), ('5', '6', '18')):
            result = self.api({'010501': dict(request, baseUserMonsterId=base_id, materialUserMonsterId=material_id, uniqueRequestId=int(base_id) + 10)})
            self.assertEqual(result['venus_status'], 1, result)
            self.assertEqual(result['resData']['010501']['userMonster']['monsterId'], child)

    def test_research_birth_family_preserves_growing_branch(self):
        request = self.prepare_research(birth_route='126')  # same baby as121, different growing species
        response = self.api({'010501': request})
        self.assertEqual(response['venus_status'], 1, response)
        self.assertEqual(response['resData']['010501']['userMonster']['monsterEvolutionRouteId'], '127')
        route = next(r for r in self.httpd.store.masters['990110']['monsterEvolutionRouteM'] if r['monsterEvolutionRouteId'] == '127')
        self.assertEqual(route['growthMonsterId'], '182')

    def test_research_both_consumed_monster_guards_capacity_and_no_money(self):
        request = self.prepare_research()
        before = self.talent_state()
        for index in (0, 1):
            for changes in ({'statusFlgs': '2'}, {'eggFlg': '1'}, {'growEndDate': '2099-01-01 00:00:00'}, {'monsterId': '81'}):
                state = json.loads(json.dumps(before))
                state['monsters'][index].update(changes)
                self.save_talent_state(state)
                self.assertEqual(self.api({'010501': request})['venus_status'], 2)
            state = json.loads(json.dumps(before))
            state['decks']['deckList'][0]['monsterList'][1]['userMonsterId'] = str(index + 1)
            self.save_talent_state(state)
            self.assertEqual(self.api({'010501': request})['venus_status'], 2)
        state = json.loads(json.dumps(before))
        for monster in state['monsters'][2:5]:
            monster.update(monsterId='16', monsterEvolutionRouteId='121', eggFlg='1')
        self.save_talent_state(state)
        self.assertEqual(self.api({'010501': request})['venus_status'], 2)
        state = json.loads(json.dumps(before))
        state['player']['gamemoney'] = '2999'
        self.save_talent_state(state)
        self.assertEqual(self.api({'010501': request})['venus_status'], 2)
        state = json.loads(json.dumps(before))
        state['monsters'][0]['monsterEvolutionRouteId'] = '0'
        self.save_talent_state(state)
        self.assertEqual(self.api({'010501': request})['venus_status'], 2)
        after = self.talent_state()
        self.assertEqual((len(after['monsters']), after['player']['gamemoney']), (6, '100000'))
        self.assertNotIn('consumed_monsters', after)

    def test_research_atomic_batch_rollback_battle_invalid_owner_and_ids(self):
        request = self.prepare_research()
        for changes in ({'materialUserMonsterId': '1'}, {'baseUserMonsterId': 1}, {'materialUserMonsterId': '999'}):
            self.assertEqual(self.api({'010501': dict(request, **changes)})['venus_status'], 2)
        self.assertEqual(self.api({'010501': request, '999999': {'uniqueRequestId': 2}})['venus_status'], 2)
        state = self.talent_state()
        self.assertEqual((len(state['monsters']), state['player']['gamemoney']), (6, '100000'))
        self.assertNotIn('normal_research_count', state)
        state['active_battle'] = {'settled': False}
        self.save_talent_state(state)
        self.assertEqual(self.api({'010501': request})['venus_status'], 2)
        self.auth('other-research-owner')
        self.assertEqual(self.api({'010501': request})['venus_status'], 2)

    def test_concurrent_research_receipt_creates_one_egg(self):
        from concurrent.futures import ThreadPoolExecutor
        request = self.prepare_research(friendship=299)
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda n: self.api({'010501': dict(request, uniqueRequestId=n)}), (2, 3)))
        self.assertTrue(all(r['venus_status'] == 1 for r in responses), responses)
        self.assertEqual([r['resData']['010501']['newUserMonsterId'] for r in responses], ['7', '7'])
        state = self.talent_state()
        self.assertEqual((state['normal_research_count'], state['player']['gamemoney'], state['monsters'][-1]['monsterId']), (1, '97000', '16'))
        self.assertEqual(len(state['monsters']), 5)

    def test_hatching_guards_batch_rollback_and_concurrent_timer(self):
        from concurrent.futures import ThreadPoolExecutor
        request = self.prepare_research()
        self.api({'010501': request})
        hatch = {'uniqueRequestId': 3, 'userMonsterId': '7'}
        for ident in ('3', '999', 7, None):
            self.assertEqual(self.api({'010304': dict(hatch, userMonsterId=ident)})['venus_status'], 2)
        self.assertEqual(self.api({'010304': hatch, '999999': {'uniqueRequestId': 3}})['venus_status'], 2)
        self.assertEqual(self.talent_state()['monsters'][-1]['eggFlg'], '1')
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda n: self.api({'010304': dict(hatch, uniqueRequestId=n)}), (3, 4)))
        self.assertTrue(all(r['venus_status'] == 1 for r in responses), responses)
        self.assertEqual(responses[0]['resData']['010304'], responses[1]['resData']['010304'])
        state = self.talent_state()
        self.assertEqual((state['normal_hatching_count'], state['player']['gamemoney'], len(state['monsters'])), (1, '97000', 5))
        self.assertEqual(state['hatched_monsters'], ['7'])

    def talent_state(self):
        with self.httpd.store.connect() as db:
            return json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])

    def save_talent_state(self, state):
        with self.httpd.store.connect() as db:
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))

    def prepare_talent(self, base=None, material=None):
        self.prepare_training()
        state = self.talent_state()
        for monster, changes in zip(state['monsters'][:2], (base or {}, material or {'hp': 5})):
            for field, value in changes.items():
                monster[field + 'Ability'] = str(value)
                monster[field + 'AbilityFlg'] = '1' if value >= 15 else '2' if value else '0'
            self.httpd.store.add_monster_experience(monster, 0)
        self.save_talent_state(state)
        return {'uniqueRequestId': 2, 'baseUserMonsterId': '1', 'materialUserMonsterId': '2'}

    def test_talent_fee_float32_and_rate_growth_boundaries(self):
        from linkz_server import ABILITY_FIELDS
        store = self.httpd.store
        # Unity float intermediates yield 8149, not decimal arithmetic's 8150.
        expected = {0: [500, 550, 600, 650, 700, 750], 5: [1000, 1100, 1200, 1300, 1400, 1500],
                    10: [2000, 2200, 2400, 2600, 2800, 3000], 15: [4050, 4455, 4860, 5265, 5670, 6075],
                    20: [8149, 8965, 9780, 10594, 11409, 12224]}
        for maximum, fees in expected.items():
            for count, fee in enumerate(fees, 1):
                self.assertEqual(store.talent_cost(dict.fromkeys(ABILITY_FIELDS, maximum),
                    dict(zip(ABILITY_FIELDS, [5] * count + [0] * (6 - count)))), fee)
        self.prepare_talent(base={'hp': 5}, material={'hp': 5})
        state = self.talent_state()
        groups = {r['monsterGroupId']: r for r in store.masters['990103']['monsterM']}
        for stage, rate, empty_rate in (('4', 200, 100), ('5', 240, 200), ('6', 300, 350), ('7', 400, 500)):
            master = next(r for r in store.masters['990102']['monsterM']
                          if groups[r['monsterGroupId']]['growStep'] == stage and groups[r['monsterGroupId']]['monsterType'] == '0')
            state['monsters'][1]['monsterId'] = master['monsterId']
            plan = store.talent_plan(*state['monsters'][:2])[3]['hp']
            self.assertEqual((plan['rate'], plan['after']), (rate, 6))
            state['monsters'][0].update(hpAbility='0', hpAbilityFlg='0')
            self.assertEqual(store.talent_plan(*state['monsters'][:2])[3]['hp']['rate'], empty_rate)
            state['monsters'][0].update(hpAbility='5', hpAbilityFlg='2')

    def test_talent_success_cost_stats_chips_receipt_and_identity(self):
        from unittest.mock import patch
        request = self.prepare_talent(material={'hp': 9})
        state = self.talent_state()
        state['monsters'][0].update(friendship='30', statusFlgs='3')  # locked base may train
        state['chips'] = [{'userChipId': 10, 'userMonsterId': 2}, {'userChipId': 11, 'userMonsterId': 1}]
        state['chip_slots'][1]['equip'] = [{'userChipId': 10}]
        state['farm_monsters'] = [1, 2]
        state['friendship_ready_at'] = {'1': 2000000000, '2': 2000000000}
        self.save_talent_state(state)
        with patch('linkz_server.secrets.randbelow', return_value=99) as roll:
            first = self.api({'010502': request})
            self.assertEqual(first['venus_status'], 1, first)
            self.assertEqual(roll.call_count, 1)
        monster = first['resData']['010502']['userMonster']
        self.assertEqual(set(first['resData']['010502']), {'userMonster'})
        self.assertEqual((monster['hpAbility'], monster['hpAbilityFlg']), ('9', '2'))
        self.assertEqual((monster['commonSkillId'], monster['ex'], monster['friendship']), ('473', '0', '30'))
        plain = dict(monster, hpAbility='0', friendship='0')
        self.httpd.store.add_monster_experience(plain, 0)
        self.assertEqual(int(monster['hp']), int(plain['hp']) + int(plain['hp']) * 9 // 100 + 5)
        self.assertEqual(self.api({'010502': request}), first)
        after = self.talent_state()
        self.assertEqual(after['player']['gamemoney'], '99500')
        self.assertEqual([c['userMonsterId'] for c in after['chips']], [0, 1])
        self.assertEqual(after['farm_monsters'], [1])
        self.assertEqual(after['friendship_ready_at'], {'1': 2000000000})
        self.assertNotIn(2, [s['userMonsterId'] for s in after['chip_slots']])
        after['monsters'][0]['commonSkillId'] = '500'
        self.save_talent_state(after)
        self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
        self.auth()
        with patch('linkz_server.secrets.randbelow', side_effect=AssertionError('receipt must not reroll')):
            replay = self.api({'010502': dict(request, uniqueRequestId=3)})
        self.assertEqual(replay['resData']['010502']['userMonster']['commonSkillId'], '500')
        captured = self.api({'050002': {'uniqueRequestId': 4, 'gachaId': 10001, 'playCount': 1, 'itemCount': 10}})
        self.assertEqual(captured['resData']['050002']['userMonsterList'][0]['userMonsterId'], '7')
        self.assertEqual(self.api({'010502': dict(request, baseUserMonsterId='3', uniqueRequestId=5)})['venus_status'], 2)

    def test_talent_failure_consumes_once_and_gold_silver_guarantee(self):
        from unittest.mock import patch
        request = self.prepare_talent(material={'hp': 5, 'attack': 20})
        with patch('linkz_server.secrets.randbelow', return_value=100):  # exactly 10% fails
            first = self.api({'010502': request})
        monster = first['resData']['010502']['userMonster']
        self.assertEqual((monster['hpAbility'], monster['attackAbility'], monster['attackAbilityFlg']), ('0', '5', '2'))
        self.auth()
        with patch('linkz_server.secrets.randbelow', side_effect=AssertionError('loss must not reroll')):
            replay = self.api({'010502': dict(request, uniqueRequestId=3)})
        self.assertEqual(replay['venus_status'], 1, replay)
        after = self.talent_state()
        self.assertEqual(after['player']['gamemoney'], '99450')
        self.assertEqual(after['normal_talent_inheritance_count'], 1)
        self.assertEqual(len(after['monsters']), 5)

    def test_talent_silver_to_gold_max20_and_gold_cap_fallback(self):
        from unittest.mock import patch
        request = self.prepare_talent(base={'hp': 10, 'attack': 15, 'defense': 20},
                                      material={'hp': 20, 'attack': 20, 'defense': 20, 'speed': 20})
        with patch('linkz_server.secrets.randbelow', return_value=0):
            response = self.api({'010502': request})
        monster = response['resData']['010502']['userMonster']
        self.assertEqual([monster[f + 'Ability'] for f in ('hp', 'attack', 'defense', 'speed')], ['10', '16', '20', '5'])
        self.assertEqual(sum(monster[f + 'AbilityFlg'] == '1' for f in ('hp', 'attack', 'defense', 'spAttack', 'spDefense', 'speed')), 2)
        # One free gold slot: silver10 becomes gold15, existing gold upgrades.
        state = self.talent_state()
        state['monsters'][0].update(defenseAbility='0', defenseAbilityFlg='0')
        state['monsters'][1].update(hpAbility='20', hpAbilityFlg='1')  # owned ID3
        self.save_talent_state(state)
        with patch('linkz_server.secrets.randbelow', return_value=0):
            next_result = self.api({'010502': dict(request, materialUserMonsterId='3', uniqueRequestId=3)})
        self.assertEqual(next_result['resData']['010502']['userMonster']['hpAbility'], '15')

    def test_talent_multiple_new_gold_uniform_overflow_and_fallback(self):
        from unittest.mock import patch
        request = self.prepare_talent(base={'hp': 10}, material={'hp': 20, 'attack': 20, 'defense': 20, 'speed': 20})
        with patch('linkz_server.secrets.randbelow', return_value=0):
            response = self.api({'010502': request})
        monster = response['resData']['010502']['userMonster']
        self.assertEqual([monster[f + 'Ability'] for f in ('hp', 'attack', 'defense', 'speed')], ['10', '5', '20', '20'])
        self.assertEqual(self.talent_state()['player']['gamemoney'], '97400')

    def test_talent_noop_invalid_medals_and_money_atomic_rollback(self):
        request = self.prepare_talent(base={'hp': 20}, material={'hp': 20})
        before = self.talent_state()
        for changes in ({}, {'materialUserMonsterId': '1'}, {'materialUserMonsterId': 2}, {'baseUserMonsterId': None}):
            self.assertEqual(self.api({'010502': dict(request, **changes)})['venus_status'], 2)
        self.assertEqual(self.talent_state()['player']['gamemoney'], before['player']['gamemoney'])
        for changes in ({'hpAbility': '12'}, {'hpAbility': '5', 'hpAbilityFlg': '1'}, {'hpAbility': True}):
            state = json.loads(json.dumps(before))
            state['monsters'][1].update(changes)
            self.save_talent_state(state)
            self.assertEqual(self.api({'010502': request})['venus_status'], 2)
        state = before
        state['monsters'][0].update(hpAbility='0', hpAbilityFlg='0')
        state['player']['gamemoney'] = '499'
        self.save_talent_state(state)
        self.assertEqual(self.api({'020003': {'uniqueRequestId': 2, 'nickname': 'rollback'}, '010502': request})['venus_status'], 2)
        after = self.talent_state()
        self.assertEqual((after['nickname'], after['player']['gamemoney'], len(after['monsters'])), ('Tamer', '499', 6))
        self.assertNotIn('consumed_monsters', after)

    def test_talent_material_protection_battle_and_batch_rollback(self):
        from unittest.mock import patch
        request = self.prepare_talent(material={'hp': 20})
        before = self.talent_state()
        for changes in ({'statusFlgs': '2'}, {'statusFlgs': '3'}, {'eggFlg': '1'}, {'growEndDate': '2099-01-01 00:00:00'}):
            state = json.loads(json.dumps(before))
            state['monsters'][1].update(changes)
            self.save_talent_state(state)
            self.assertEqual(self.api({'010502': request})['venus_status'], 2)
        for key, value in (('active_battle', {'settled': False}), ('tutorial', {'endFlg': '0', 'statusId': '0'})):
            state = json.loads(json.dumps(before))
            state[key] = value
            self.save_talent_state(state)
            self.assertEqual(self.api({'010502': request})['venus_status'], 2)
        state = json.loads(json.dumps(before))
        state['decks']['deckList'][0]['monsterList'][1]['userMonsterId'] = '2'
        self.save_talent_state(state)
        self.assertEqual(self.api({'010502': request})['venus_status'], 2)
        self.save_talent_state(before)
        with patch('linkz_server.secrets.randbelow', return_value=0):
            self.assertEqual(self.api({'010502': request, '999999': {'uniqueRequestId': 2}})['venus_status'], 2)
        after = self.talent_state()
        self.assertEqual((len(after['monsters']), after['player']['gamemoney'], after['monsters'][0]['hpAbility']), (6, '100000', '0'))
        self.auth('other-talent-owner')
        self.assertEqual(self.api({'010502': request})['venus_status'], 2)

    def test_concurrent_talent_receipt_spends_and_rolls_once(self):
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        request = self.prepare_talent()
        with patch('linkz_server.secrets.randbelow', return_value=100) as roll:
            with ThreadPoolExecutor(max_workers=2) as executor:
                responses = list(executor.map(lambda n: self.api({'010502': dict(request, uniqueRequestId=n)}), (2, 3)))
        self.assertTrue(all(r['venus_status'] == 1 for r in responses), responses)
        self.assertEqual(roll.call_count, 1)
        after = self.talent_state()
        self.assertEqual((after['player']['gamemoney'], after['normal_talent_inheritance_count'], len(after['monsters'])), ('99500', 1, 5))

    def prepare_training(self):
        self.prepare_friendship()
        store = self.httpd.store
        with store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            state['player']['gamemoney'] = '100000'
            state['decks']['deckList'][0]['monsterList'] = [{'userMonsterId': '1'}, {'userMonsterId': '0'}, {'userMonsterId': '0'}]
            route = next(r for r in store.masters['990110']['monsterEvolutionRouteM'] if r['growthMonsterId'] == '81')
            master = next(r for r in store.masters['990102']['monsterM'] if r['monsterId'] == '81')
            for _ in range(3):
                store.create_capture_monster({'id': 1}, state, route, master)
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))

    def test_fusion_cost_experience_replay_chips_and_identity(self):
        from unittest.mock import patch
        self.prepare_training()
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            state['chips'] = [{'userChipId': 10, 'userMonsterId': 6, 'chipId': '1'}]
            state['chip_slots'][-1]['equip'] = [{'userChipId': 10}]
            state['friendship_ready_at'] = {'6': 2000000000}
            state['monsters'][0].update(friendship='30', hpAbilityFlg='1', hpAbility='10')
            self.httpd.store.add_monster_experience(state['monsters'][0], 0)
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        request = {'uniqueRequestId': 2, 'baseMonster': '1', 'materialMonster': ['2', '6']}
        self.assertEqual(self.api({'010201': request, '999999': {'uniqueRequestId': 2}})['venus_status'], 2)
        with patch('linkz_server.secrets.randbelow', return_value=99):
            first = self.api({'010201': request})
        self.assertEqual(first['venus_status'], 1, first)
        monster = first['resData']['010201']['userMonster']
        self.assertEqual(monster['ex'], '20')
        self.assertEqual(monster['friendship'], '30')
        plain = dict(monster, friendship='0', hpAbility='0')
        self.httpd.store.add_monster_experience(plain, 0)
        self.assertEqual(int(monster['hp']), int(plain['hp']) + int(plain['hp']) // 10 + 5)
        self.assertEqual(self.api({'010201': request}), first)
        self.auth()
        replay = self.api({'010201': dict(request, uniqueRequestId=3)})
        self.assertEqual(replay['resData']['010201']['userMonster']['ex'], '20')
        data = self.api({'020001': {'uniqueRequestId': 4}, '020101': {'uniqueRequestId': 4}})['resData']
        self.assertEqual(data['020001']['playerInfo']['gamemoney'], '99520')
        self.assertEqual([m['userMonsterId'] for m in data['020101']['userMonsterList']], ['1', '3', '4', '5'])
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            self.assertEqual(state['chips'][0]['userMonsterId'], 0)
            self.assertNotIn('6', state['friendship_ready_at'])
            self.assertNotIn(6, [s['userMonsterId'] for s in state['chip_slots']])
            self.assertEqual(state['normal_fusion_count'], 1)
            self.assertEqual(state['monster_id_sequence'], 6)
        captured = self.api({'050002': {'uniqueRequestId': 5, 'gachaId': 10001, 'playCount': 1, 'itemCount': 10}})['resData']['050002']['userMonsterList'][0]
        self.assertEqual(captured['userMonsterId'], '7')
        self.assertEqual(self.api({'010201': dict(request, uniqueRequestId=6)})['venus_status'], 1)
        self.assertEqual(self.api({'010201': dict(request, baseMonster='3', uniqueRequestId=7)})['venus_status'], 2)

    def test_fusion_material_guards_and_insufficient_money_rollback(self):
        self.prepare_training()
        base = {'uniqueRequestId': 2, 'baseMonster': '1', 'materialMonster': ['2']}
        for ids in (None, '2', [], ['2'] * 6, ['2', '2'], ['1'], [True], [2], ['999']):
            self.assertEqual(self.api({'010201': dict(base, materialMonster=ids)})['venus_status'], 2)
        for changes in ({'statusFlgs': '2'}, {'statusFlgs': '3'}, {'eggFlg': '1'}, {'growEndDate': '2099-01-01 00:00:00'}):
            with self.httpd.store.connect() as db:
                state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
                original = dict(state['monsters'][1])
                state['monsters'][1].update(changes)
                db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
            self.assertEqual(self.api({'010201': base})['venus_status'], 2)
            with self.httpd.store.connect() as db:
                state['monsters'][1] = original
                db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        with self.httpd.store.connect() as db:
            state['decks']['deckList'][0]['monsterList'][1]['userMonsterId'] = '2'
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        self.assertEqual(self.api({'010201': base})['venus_status'], 2)
        with self.httpd.store.connect() as db:
            state['decks']['deckList'][0]['monsterList'][1]['userMonsterId'] = '0'
            state['player']['gamemoney'] = '239'
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        self.assertEqual(self.api({'020003': {'uniqueRequestId': 2, 'nickname': 'rollback'}, '010201': base})['venus_status'], 2)
        with self.httpd.store.connect() as db:
            after = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            self.assertEqual(len(after['monsters']), 6)
            self.assertEqual(after['player']['gamemoney'], '239')
            self.assertEqual(after['nickname'], 'Tamer')
            self.assertNotIn('consumed_monsters', after)
        self.auth('other-training-owner')
        self.assertEqual(self.api({'010201': base})['venus_status'], 2)

    def test_fusion_max_level_luck_probability_and_cap(self):
        from unittest.mock import patch
        self.prepare_training()
        store = self.httpd.store
        with store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            base = state['monsters'][0]
            store.add_monster_experience(base, 100000)
            base['luck'] = '49'
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        request = {'uniqueRequestId': 2, 'baseMonster': '1', 'materialMonster': ['2']}
        with patch('linkz_server.secrets.randbelow', return_value=6):
            first = self.api({'010201': request})
        self.assertEqual(first['venus_status'], 1, first)
        self.assertEqual((first['resData']['010201']['userMonster']['level'], first['resData']['010201']['userMonster']['luck']), ('10', '50'))
        with patch('linkz_server.secrets.randbelow', side_effect=AssertionError('max luck must not roll')):
            capped = self.api({'010201': dict(request, materialMonster=['3'], uniqueRequestId=3)})
        self.assertEqual(capped['resData']['010201']['userMonster']['luck'], '50')
        with store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            state['monsters'][0]['luck'] = '0'
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        with patch('linkz_server.secrets.randbelow', return_value=7):
            missed = self.api({'010201': dict(request, materialMonster=['4'], uniqueRequestId=4)})
        self.assertEqual(missed['resData']['010201']['userMonster']['luck'], '0')

    def test_skill_inheritance_cost_both_skill_types_replay_and_rollback(self):
        self.prepare_training()
        store = self.httpd.store
        with store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            store.add_monster_experience(state['monsters'][1], 100000)
            state['monsters'][1]['commonSkillId'] = '400'
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        request = {'uniqueRequestId': 2, 'baseUserMonsterId': '1', 'materialUserMonsterId': '2',
                   'baseCommonSkillNumber': 1, 'materialCommonSkillNumber': 1}
        self.assertEqual(self.api({'010601': request, '999999': {'uniqueRequestId': 2}})['venus_status'], 2)
        first = self.api({'010601': request})
        self.assertEqual(first['venus_status'], 1, first)
        self.assertEqual(first['resData']['010601']['userMonster']['commonSkillId'], '400')
        self.assertEqual(first['resData']['010601']['userMonster']['ex'], '0')
        self.assertEqual(self.api({'010601': request}), first)
        self.auth()
        self.assertEqual(self.api({'010601': dict(request, uniqueRequestId=3)}), first)
        with store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            self.assertEqual(state['player']['gamemoney'], '94800')
            self.assertNotIn('2', [m['userMonsterId'] for m in state['monsters']])
            self.assertEqual(state['normal_inheritance_count'], 1)
            state['monsters'][0]['commonSkillId'] = '500'
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        self.assertEqual(self.api({'010601': dict(request, uniqueRequestId=4)})['resData']['010601']['userMonster']['commonSkillId'], '500')

    def test_fusion_ultimate_odds_and_special_material_protection(self):
        from unittest.mock import patch
        self.prepare_training()
        store = self.httpd.store
        groups = {r['monsterGroupId']: r for r in store.masters['990103']['monsterM']}
        master = next(r for r in store.masters['990102']['monsterM'] if r['monsterId'] == '81')
        tribe = groups[master['monsterGroupId']]['tribe']
        ultimate = next(r for r in store.masters['990102']['monsterM']
                        if groups[r['monsterGroupId']]['tribe'] == tribe and
                        groups[r['monsterGroupId']]['growStep'] == '7' and groups[r['monsterGroupId']]['monsterType'] == '0')
        special = next(r for r in store.masters['990102']['monsterM'] if groups[r['monsterGroupId']]['monsterType'] == '1')
        with store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            state['monsters'][1]['monsterId'] = ultimate['monsterId']
            state['monsters'][2]['monsterId'] = special['monsterId']
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        with patch('linkz_server.secrets.randbelow', return_value=99):
            response = self.api({'010201': {'uniqueRequestId': 2, 'baseMonster': '1', 'materialMonster': ['2']}})
        self.assertEqual(response['venus_status'], 1, response)
        self.assertEqual(response['resData']['010201']['userMonster']['luck'], '1')
        self.assertEqual(self.api({'010201': {'uniqueRequestId': 3, 'baseMonster': '1', 'materialMonster': ['3']}})['venus_status'], 2)
        with store.connect() as db:
            after = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            self.assertEqual(after['normal_fusion_count'], 1)
            self.assertIn('3', [m['userMonsterId'] for m in after['monsters']])

    def test_concurrent_fusion_material_receipt_spends_once(self):
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        self.prepare_training()
        with patch('linkz_server.secrets.randbelow', return_value=99) as luck_roll:
            with ThreadPoolExecutor(max_workers=2) as executor:
                responses = list(executor.map(lambda n: self.api({'010201': {'uniqueRequestId': n,
                    'baseMonster': '1', 'materialMonster': ['6']}}), (2, 3)))
        self.assertTrue(all(r['venus_status'] == 1 for r in responses), responses)
        self.assertEqual(luck_roll.call_count, 1)
        with self.httpd.store.connect() as db:
            after = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            self.assertEqual(after['player']['gamemoney'], '99760')
            self.assertEqual(after['monsters'][0]['ex'], '10')
            self.assertEqual(after['normal_fusion_count'], 1)
            self.assertEqual(len(after['monsters']), 5)

    def test_skill_inheritance_second_slots_use_first_skill_price(self):
        self.prepare_training()
        store = self.httpd.store
        version = next(r for r in store.masters['990102']['monsterM'] if r['rare'] == '6')
        with store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            for monster in state['monsters'][:2]:
                monster['monsterId'] = version['monsterId']
            state['monsters'][0]['extraCommonSkillId'] = ''
            state['monsters'][1]['extraCommonSkillId'] = '400'
            store.add_monster_experience(state['monsters'][1], 100000000)
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        response = self.api({'010601': {'uniqueRequestId': 2, 'baseUserMonsterId': '1', 'materialUserMonsterId': '2',
                                        'baseCommonSkillNumber': 2, 'materialCommonSkillNumber': 2}})
        self.assertEqual(response['venus_status'], 1, response)
        monster = response['resData']['010601']['userMonster']
        self.assertEqual((monster['commonSkillId'], monster['extraCommonSkillId']), ('473', '400'))
        self.assertEqual(self.api({'020001': {'uniqueRequestId': 3}})['resData']['020001']['playerInfo']['gamemoney'], '96870')

    def test_skill_inheritance_guards_and_monster_lock(self):
        self.prepare_training()
        store = self.httpd.store
        request = {'uniqueRequestId': 2, 'baseUserMonsterId': '1', 'materialUserMonsterId': '2',
                   'baseCommonSkillNumber': 1, 'materialCommonSkillNumber': 1}
        self.assertEqual(self.api({'010601': request})['venus_status'], 2)
        with store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            store.add_monster_experience(state['monsters'][1], 100000)
            state['monsters'][1]['commonSkillId'] = '474'
            state['monsters'][1]['statusFlgs'] = '1'
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        lock = {'uniqueRequestId': 3, 'userMonsterId': '2', 'setFlg': '1'}
        self.assertEqual(self.api({'010001': lock})['venus_status'], 1)
        self.auth()
        self.assertEqual(self.api({'010601': request})['venus_status'], 2)
        with store.connect() as db:
            self.assertEqual(json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])['monsters'][1]['statusFlgs'], '3')
        self.assertEqual(self.api({'010001': dict(lock, uniqueRequestId=4, setFlg='0')})['venus_status'], 1)
        with store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            state['monsters'][1]['commonSkillId'] = '473'
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        self.assertEqual(self.api({'010601': request})['venus_status'], 2)
        with store.connect() as db:
            state['monsters'][1]['commonSkillId'] = '474'
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        for changes in ({'baseCommonSkillNumber': 2}, {'materialCommonSkillNumber': 2}, {'baseCommonSkillNumber': True},
                        {'materialCommonSkillNumber': 0}, {'materialUserMonsterId': '1'}, {'materialUserMonsterId': '999'}):
            self.assertEqual(self.api({'010601': dict(request, **changes)})['venus_status'], 2)
        with store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            self.assertEqual(state['monsters'][1]['statusFlgs'], '1')
            state['player']['gamemoney'] = '879'
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        self.assertEqual(self.api({'010601': request})['venus_status'], 2)
        with store.connect() as db:
            after = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            self.assertEqual(len(after['monsters']), 6)
            self.assertEqual(after['player']['gamemoney'], '879')
        for value in (True, 1, '2', None):
            self.assertEqual(self.api({'010001': dict(lock, setFlg=value, uniqueRequestId=5)})['venus_status'], 2)

    def prepare_friendship(self):
        self.prepare_garden(count=0)
        store = self.httpd.store
        with store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            route = next(r for r in store.masters['990110']['monsterEvolutionRouteM'] if r['growthMonsterId'] == '81')
            master = next(r for r in store.masters['990102']['monsterM'] if r['monsterId'] == '81')
            for _ in range(3):
                store.create_capture_monster({'id': 1}, state, route, master)
            state['decks']['deckList'][0]['monsterList'] = [{'userMonsterId': str(i)} for i in range(1, 4)]
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))

    def test_friendship_daily_cooldown_persistence_and_cap(self):
        self.prepare_friendship()
        request = {'uniqueRequestId': 2, 'userMonsterId': '1'}
        self.assertEqual(self.api({'010701': request, '999999': {'uniqueRequestId': 2}})['venus_status'], 2)
        first = self.api({'010701': request})
        self.assertEqual(first['venus_status'], 1, first)
        result = first['resData']['010701']
        self.assertEqual((result['upFriendship'], result['nextTimeSec'], result['userMonster']['friendship']), (1, 86400, '1'))
        self.assertIsNone(result['upStatus'])
        self.assertEqual(self.api({'010701': request}), first)
        self.assertEqual(self.api({'010701': dict(request, uniqueRequestId=3)})['resData']['010701']['upFriendship'], 0)
        self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
        self.auth()
        times = self.api({'010702': {'uniqueRequestId': 4, 'userMonsterIds': ['1', '0', '2']}})['resData']['010702']['friendshipTime']
        self.assertEqual([r['userMonsterId'] for r in times], ['1', '0', '2'])
        self.assertTrue(86300 <= times[0]['nextTimeSec'] <= 86400)
        self.assertEqual([r['nextTimeSec'] for r in times[1:]], [0, 0])
        self.assertEqual(self.api({'010701': dict(request, uniqueRequestId=5)})['resData']['010701']['upFriendship'], 0)
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            state['friendship_ready_at']['1'] = int(time.time()) - 1
            state['monsters'][0]['friendship'] = '89'
            self.httpd.store.add_monster_experience(state['monsters'][0], 0)
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        maximum = self.api({'010701': dict(request, uniqueRequestId=6)})['resData']['010701']
        self.assertEqual((maximum['upFriendship'], maximum['userMonster']['friendship']), (1, '90'))
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            state['friendship_ready_at']['1'] = 0
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        capped = self.api({'010701': dict(request, uniqueRequestId=7)})['resData']['010701']
        self.assertEqual((capped['upFriendship'], capped['userMonster']['friendship']), (0, '90'))
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            self.assertEqual(state['farm_friendship_count'], 2)
            self.assertEqual(state['friendship_ready_at']['1'], 0)

    def test_friendship_bonus_survives_feeding_and_evolution(self):
        self.prepare_friendship()
        store = self.httpd.store
        with store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            monster = state['monsters'][0]
            monster['friendship'] = '29'
            store.add_monster_experience(monster, 0)
            before = dict(monster)
            state['player'].update(meatNum='10', gamemoney='1000')
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        result = self.api({'010701': {'uniqueRequestId': 2, 'userMonsterId': '1'}})['resData']['010701']
        master = next(r for r in store.masters['990102']['monsterM'] if r['monsterId'] == '81')
        expected = {f: int(master['speed' if f == 'speed' else 'max' + f[0].upper() + f[1:]]) // 100
                    for f in ('hp', 'attack', 'defense', 'spAttack', 'spDefense', 'speed')}
        self.assertEqual(result['upStatus'], expected)
        for field, bonus in expected.items():
            self.assertEqual(int(result['userMonster'][field]) - int(before[field]), bonus)
        fed = self.api({'010202': {'uniqueRequestId': 3, 'mealMonster': '1', 'meatNum': 1}})['resData']['010202']['userMonster']
        plain = dict(fed, friendship='0')
        store.add_monster_experience(plain, 0)
        for field, bonus in expected.items():
            self.assertEqual(int(fed[field]) - int(plain[field]), bonus)
        with store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            state['friendship_ready_at']['1'] = 0
            state['monsters'][0]['friendship'] = '59'
            store.add_monster_experience(state['monsters'][0], 0)
            before_second = dict(state['monsters'][0])
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        second = self.api({'010701': {'uniqueRequestId': 30, 'userMonsterId': '1'}})['resData']['010701']
        self.assertEqual(second['upStatus'], expected)
        for field, bonus in expected.items():
            self.assertEqual(int(second['userMonster'][field]) - int(before_second[field]), bonus)
        with store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            store.add_monster_experience(state['monsters'][0], 100000)
            state['souls'] = [{'soulId': '2010001', 'num': '8'}, {'soulId': '2010002', 'num': '3'}]
            deadline = state['friendship_ready_at']['1']
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        evolved = self.api({'010301': {'uniqueRequestId': 4, 'userMonsterId': '1', 'monsterId': 366}})['resData']['010301']['userMonster']
        self.assertEqual((evolved['friendship'], evolved['userMonsterId']), ('60', '1'))
        plain = dict(evolved, friendship='0')
        store.add_monster_experience(plain, 0)
        for field, bonus in store.friendship_bonus(evolved).items():
            self.assertEqual(int(evolved[field]) - int(plain[field]), bonus)
        with store.connect() as db:
            self.assertEqual(json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])['friendship_ready_at']['1'], deadline)

    def test_friendship_validation_slots_and_battle_protection(self):
        self.prepare_friendship()
        for ids in (None, '1', ['1', '1'], ['1'] * 4, [True], [1], ['999']):
            self.assertEqual(self.api({'010702': {'uniqueRequestId': 2, 'userMonsterIds': ids}})['venus_status'], 2)
        times = self.api({'010702': {'uniqueRequestId': 2, 'userMonsterIds': ['1']}})['resData']['010702']['friendshipTime']
        self.assertEqual([r['userMonsterId'] for r in times], ['1', '0', '0'])
        for ident in (None, 1, True, '0', '999'):
            self.assertEqual(self.api({'010701': {'uniqueRequestId': 3, 'userMonsterId': ident}})['venus_status'], 2)
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            state['decks']['deckList'][0]['monsterList'][2]['userMonsterId'] = '0'
            state['active_battle'] = {'settled': False}
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        self.assertEqual(self.api({'010701': {'uniqueRequestId': 3, 'userMonsterId': '3'}})['venus_status'], 2)
        self.assertEqual(self.api({'010701': {'uniqueRequestId': 3, 'userMonsterId': '1'}})['venus_status'], 2)
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            self.assertNotIn('friendship_ready_at', state)
            self.assertEqual(state['monsters'][0]['friendship'], '0')
            state['active_battle'] = {'settled': True}
            state['monsters'][0]['growEndDate'] = '2099-01-01 00:00:00'
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        self.assertEqual(self.api({'010701': {'uniqueRequestId': 3, 'userMonsterId': '1'}})['venus_status'], 2)
        self.auth('other-friendship-owner')
        self.assertEqual(self.api({'010701': {'uniqueRequestId': 1, 'userMonsterId': '1'}})['venus_status'], 2)

    def test_concurrent_friendship_taps_grant_once(self):
        from concurrent.futures import ThreadPoolExecutor
        self.prepare_friendship()
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda n: self.api({'010701': {'uniqueRequestId': n, 'userMonsterId': '1'}}), (2, 3)))
        self.assertTrue(all(r['venus_status'] == 1 for r in responses), responses)
        self.assertEqual(sum(r['resData']['010701']['upFriendship'] for r in responses), 1)
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            self.assertEqual(state['monsters'][0]['friendship'], '1')
            self.assertEqual(state['farm_friendship_count'], 1)

    def test_friendship_exact_cooldown_boundary_and_tutorial(self):
        from unittest.mock import patch
        self.prepare_friendship()
        store = self.httpd.store
        with store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
        state['tutorial']['endFlg'] = '0'
        self.assertIsNone(store.friendship_times(state, {'userMonsterIds': ['1', '0', '2']})['friendshipTime'])
        with self.assertRaisesRegex(Exception, 'after the tutorial'):
            store.tap_friendship(state, {'userMonsterId': '1'})
        state['tutorial']['endFlg'] = '1'
        state['friendship_ready_at'] = {'1': 1000}
        with patch('linkz_server.time.time', return_value=999):
            result = store.tap_friendship(state, {'userMonsterId': '1'})
            self.assertEqual((result['upFriendship'], result['nextTimeSec']), (0, 1))
        with patch('linkz_server.time.time', return_value=1000):
            result = store.tap_friendship(state, {'userMonsterId': '1'})
            self.assertEqual((result['upFriendship'], result['nextTimeSec']), (1, 86400))
            self.assertEqual(state['friendship_ready_at']['1'], 87400)
            self.assertEqual(store.friendship_times(state, {'userMonsterIds': ['1']})['friendshipTime'][0]['nextTimeSec'], 86400)

    def test_facility_shortening_build_upgrade_retry_and_rollback(self):
        self.prepare_farm_edit()
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            state['player'].update(gamemoney='10000', point=10)
            state['facility_keys'] = {'1': 1}
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        ident = self.api({'140001': {'uniqueRequestId': 2, 'facilityId': 2, 'positionX': 10, 'positionY': 10}})['resData']['140001']['userFacilityId']
        request = {'uniqueRequestId': 3, 'userFacilityId': ident}
        self.assertEqual(self.api({'140004': request})['venus_status'], 2)
        self.assertEqual(self.api({'140002': request, '999999': {'uniqueRequestId': 3}})['venus_status'], 2)
        first = self.api({'140002': request})
        self.assertEqual(first['venus_status'], 1, first)
        self.assertEqual(first['resData']['140002']['num'], 2)
        self.assertEqual(self.api({'140002': request}), first)
        self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
        self.auth()
        self.assertEqual(self.api({'140002': dict(request, uniqueRequestId=4)})['resData']['140002']['num'], 0)
        self.assertEqual(self.api({'140010': dict(request, uniqueRequestId=5)})['venus_status'], 1)
        self.assertEqual(self.api({'140003': dict(request, uniqueRequestId=6)})['venus_status'], 1)
        self.assertEqual(self.api({'140002': dict(request, uniqueRequestId=7)})['venus_status'], 2)
        upgraded = self.api({'140004': dict(request, uniqueRequestId=7)})
        self.assertEqual(upgraded['resData']['140004']['num'], 2)
        self.assertEqual(self.api({'140004': dict(request, uniqueRequestId=7)}), upgraded)
        self.auth()
        self.assertEqual(self.api({'140004': dict(request, uniqueRequestId=8)})['resData']['140004']['num'], 0)
        data = self.api({'020001': {'uniqueRequestId': 9}, '140006': {'uniqueRequestId': 9, 'userId': 1}})['resData']
        self.assertEqual(data['020001']['playerInfo']['point'], 6)
        self.assertEqual(data['020001']['playerInfo']['meatLimitMax'], '150')
        self.assertEqual(data['140006']['userFacilityList'][0]['level'], 2)
        self.assertFalse(data['140006']['userFacilityList'][0]['completeTime'])

    def test_facility_shortening_fee_boundaries_and_expired_field(self):
        from unittest.mock import patch
        from datetime import datetime, timezone
        store = self.httpd.store
        now = int(time.time())
        for seconds, fee in ((0, 0), (-3601, 0), (1, 2), (3599, 2), (3600, 2), (3601, 4), (7200, 4)):
            state = {'tutorial': {'endFlg': '1'}, 'player': {'point': 10}, 'last_harvest': [],
                     'facilities': [{'userFacilityId': 1, 'facilityId': 1, 'level': 0,
                                     'completeTime': datetime.fromtimestamp(now + seconds, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}]}
            with patch('linkz_server.time.time', return_value=now):
                result = store.shorten_facility(state, {'userFacilityId': 1}, False)
            self.assertEqual(result, {'num': fee})
            self.assertEqual(state['player']['point'], 10 - fee)
            self.assertEqual(state['facilities'][0]['level'], 1)
            self.assertEqual(state['last_harvest'], [{'userFacilityId': 1, 'lastHarvestTime':
                datetime.fromtimestamp(now + 2, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}])
            self.assertEqual(store.shorten_facility(state, {'userFacilityId': 1}, False), {'num': 0})
        # Walkable decor still has exclusive placement in FarmObject.CheckPlaceable.
        for ident in (12, 13, 14, 20):
            with self.assertRaisesRegex(Exception, 'overlaps'):
                store.check_facility_placement({'facilities': [{'facilityId': ident, 'positionX': 10, 'positionY': 10}]}, ident, 10, 10)

    def test_facility_shortening_insufficient_funds_and_ownership(self):
        self.prepare_farm_edit()
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            state['player']['point'] = 1
            state['facilities'] = [{'userFacilityId': 1, 'facilityId': 1, 'level': 0, 'positionX': 10,
                                    'positionY': 10, 'completeTime': '2099-01-01 00:00:00'}]
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        for ident in (None, True, '1', 0, 2, 2**31):
            self.assertEqual(self.api({'140002': {'uniqueRequestId': 2, 'userFacilityId': ident}})['venus_status'], 2)
        failed = self.api({'020003': {'uniqueRequestId': 2, 'nickname': 'rollback'},
                           '140002': {'uniqueRequestId': 2, 'userFacilityId': 1}})
        self.assertEqual(failed['venus_status'], 2)
        data = self.api({'020001': {'uniqueRequestId': 3}, '140006': {'uniqueRequestId': 3, 'userId': 1}})['resData']
        self.assertEqual(data['020001']['playerInfo']['point'], 1)
        self.assertEqual(data['020001']['playerInfo']['nickname'], 'Tamer')
        self.assertEqual(data['140006']['userFacilityList'][0]['level'], 0)
        self.assertEqual(data['140006']['userFacilityList'][0]['completeTime'], '2099-01-01 00:00:00')
        self.auth('other-facility-shortening-owner')
        self.assertEqual(self.api({'140002': {'uniqueRequestId': 1, 'userFacilityId': 1}})['venus_status'], 2)

    def prepare_farm_edit(self):
        self.auth()
        self.api({'020001': {'uniqueRequestId': 1}})
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            state['tutorial'] = {'statusId': '1400', 'endFlg': '1'}
            state['initial_facilities_created'] = True
            state['player']['gamemoney'] = '1000'
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))

    def test_decoration_build_move_and_atomic_layout_swap(self):
        self.prepare_farm_edit()
        requests = []
        for index, x in enumerate((10, 11), 2):
            request = {'uniqueRequestId': index, 'facilityId': 11, 'positionX': x, 'positionY': 10}
            response = self.api({'140001': request})
            self.assertEqual(response['venus_status'], 1, response)
            self.assertEqual(self.api({'140001': request}), response)
            requests.append(response['resData']['140001']['userFacilityId'])
        positions = [{'userFacilityId': requests[0], 'positionX': 11, 'positionY': 10},
                     {'userFacilityId': requests[1], 'positionX': 10, 'positionY': 10}]
        move = {'uniqueRequestId': 4, **positions[0]}
        self.assertEqual(self.api({'140005': move})['venus_status'], 2)
        arranged = self.api({'140008': {'uniqueRequestId': 5, 'userFacilityList': positions}})
        self.assertEqual(arranged['venus_status'], 1, arranged)
        self.assertEqual(self.api({'140008': {'uniqueRequestId': 5, 'userFacilityList': positions}}), arranged)
        self.assertEqual(self.api({'140005': dict(move, uniqueRequestId=6)})['venus_status'], 1)
        self.auth()
        data = self.api({'140006': {'uniqueRequestId': 7, 'userId': 1}, '020001': {'uniqueRequestId': 7}})['resData']
        rows = data['140006']['userFacilityList']
        self.assertEqual([(r['positionX'], r['positionY']) for r in rows], [(11, 10), (10, 10)])
        self.assertTrue(all(r['level'] == 1 and not r['completeTime'] for r in rows))
        self.assertEqual(data['020001']['playerInfo']['gamemoney'], '960')

    def test_invalid_arrangements_rollback_and_account_isolation(self):
        self.prepare_farm_edit()
        ident = self.api({'140001': {'uniqueRequestId': 2, 'facilityId': 11, 'positionX': 10, 'positionY': 10}})['resData']['140001']['userFacilityId']
        good = {'userFacilityId': ident, 'positionX': 10, 'positionY': 10}
        for rows in (None, [], [good, good], [dict(good, userFacilityId=999)],
                     [dict(good, userFacilityId=True)], [dict(good, positionX=True)],
                     [dict(good, positionX=-1)], [dict(good, positionX=2**31)], [dict(good, positionX=0, positionY=0)]):
            response = self.api({'020003': {'uniqueRequestId': 3, 'nickname': 'rollback'},
                                 '140008': {'uniqueRequestId': 3, 'userFacilityList': rows}})
            self.assertEqual(response['venus_status'], 2, response)
        data = self.api({'140006': {'uniqueRequestId': 4, 'userId': 1}, '020001': {'uniqueRequestId': 4}})['resData']
        self.assertEqual(data['020001']['playerInfo']['nickname'], 'Tamer')
        self.assertEqual(data['140006']['userFacilityList'][0]['positionX'], 10)
        self.auth('other-farm-edit-owner')
        self.assertEqual(self.api({'140005': {'uniqueRequestId': 1, **good}})['venus_status'], 2)
        self.assertEqual(self.api({'140015': {'uniqueRequestId': 2, 'userFacilityId': ident, 'stockFlg': 1}})['venus_status'], 2)

    def test_stock_restore_two_request_disconnect_and_collision(self):
        self.prepare_farm_edit()
        ident = self.api({'140001': {'uniqueRequestId': 2, 'facilityId': 11, 'positionX': 10, 'positionY': 10}})['resData']['140001']['userFacilityId']
        request = {'uniqueRequestId': 3, 'userFacilityId': ident, 'stockFlg': 1}
        response = self.api({'140015': request})
        self.assertEqual(self.api({'140015': request}), response)
        self.assertEqual(self.api({'140015': dict(request, uniqueRequestId=4)})['venus_status'], 1)
        replacement = self.api({'140001': {'uniqueRequestId': 5, 'facilityId': 11, 'positionX': 10, 'positionY': 10}})['resData']['140001']['userFacilityId']
        self.assertNotEqual(ident, replacement)
        self.assertEqual(self.api({'140005': {'uniqueRequestId': 6, 'userFacilityId': ident, 'positionX': 12, 'positionY': 10}})['venus_status'], 2)
        self.assertEqual(self.api({'140015': dict(request, stockFlg=0, uniqueRequestId=7)})['venus_status'], 1)
        self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
        self.auth()
        data = self.api({'140006': {'uniqueRequestId': 8, 'userId': 1}, '140014': {'uniqueRequestId': 8, 'userId': 0}})['resData']
        self.assertEqual([r['userFacilityId'] for r in data['140006']['userFacilityList']], [replacement])
        self.assertEqual([r['userFacilityId'] for r in data['140014']['userFacilityList']], [ident])
        move = {'uniqueRequestId': 9, 'userFacilityId': ident, 'positionX': 10, 'positionY': 10}
        self.assertEqual(self.api({'140005': move})['venus_status'], 2)
        self.assertEqual(self.api({'140005': dict(move, positionX=12), '999999': {'uniqueRequestId': 9}})['venus_status'], 2)
        self.assertEqual(self.api({'140005': dict(move, positionX=12)})['venus_status'], 1)
        self.assertEqual(self.api({'140014': {'uniqueRequestId': 10, 'userId': 0}})['resData']['140014']['userFacilityList'], [])
        self.assertEqual(len(self.api({'140006': {'uniqueRequestId': 10, 'userId': 1}})['resData']['140006']['userFacilityList']), 2)

    def test_decoration_sale_persistence_no_id_reuse_and_cap(self):
        self.prepare_farm_edit()
        build = {'uniqueRequestId': 2, 'facilityId': 11, 'positionX': 10, 'positionY': 10}
        ident = self.api({'140001': build})['resData']['140001']['userFacilityId']
        sale = {'uniqueRequestId': 3, 'userFacilityId': ident}
        self.assertEqual(self.api({'140007': sale, '999999': {'uniqueRequestId': 3}})['venus_status'], 2)
        first = self.api({'140007': sale})
        self.assertEqual(first['resData']['140007']['sellPrice'], 10)
        self.assertEqual(self.api({'140007': sale}), first)
        self.auth()
        self.assertEqual(self.api({'140007': dict(sale, uniqueRequestId=4)}), first)
        next_id = self.api({'140001': dict(build, uniqueRequestId=5)})['resData']['140001']['userFacilityId']
        self.assertGreater(next_id, ident)
        self.assertEqual(self.api({'140015': {'uniqueRequestId': 6, 'userFacilityId': next_id, 'stockFlg': 1}})['venus_status'], 1)
        self.assertEqual(self.api({'140007': dict(sale, userFacilityId=next_id, uniqueRequestId=7)})['resData']['140007']['sellPrice'], 10)
        self.assertEqual(self.api({'020001': {'uniqueRequestId': 8}})['resData']['020001']['playerInfo']['gamemoney'], '980')
        capped_id = self.api({'140001': dict(build, uniqueRequestId=9)})['resData']['140001']['userFacilityId']
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            state['player']['gamemoney'] = '9999994'
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        self.assertEqual(self.api({'140007': dict(sale, userFacilityId=capped_id, uniqueRequestId=10)})['resData']['140007']['sellPrice'], 5)
        self.assertEqual(self.api({'020001': {'uniqueRequestId': 11}})['resData']['020001']['playerInfo']['gamemoney'], '9999999')

    def test_core_facility_stock_sale_protection_and_timer_move(self):
        self.prepare_farm_edit()
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            state['facilities'] = [{'userFacilityId': 1, 'facilityId': 2, 'level': 1,
                                   'positionX': 10, 'positionY': 10, 'completeTime': '2099-01-01 00:00:00'}]
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        self.assertEqual(self.api({'140015': {'uniqueRequestId': 2, 'userFacilityId': 1, 'stockFlg': 1}})['venus_status'], 2)
        self.assertEqual(self.api({'140007': {'uniqueRequestId': 3, 'userFacilityId': 1}})['venus_status'], 2)
        self.assertEqual(self.api({'140005': {'uniqueRequestId': 4, 'userFacilityId': 1, 'positionX': 14, 'positionY': 16}})['venus_status'], 1)
        row = self.api({'140006': {'uniqueRequestId': 5, 'userId': 1}})['resData']['140006']['userFacilityList'][0]
        self.assertEqual(row['completeTime'], '2099-01-01 00:00:00')
        self.assertEqual(row['level'], 1)
        self.assertEqual(self.api({'020001': {'uniqueRequestId': 6}})['resData']['020001']['playerInfo']['meatLimitMax'], '100')

    def test_ambiguous_json_and_oversized_ids_rollback(self):
        self.auth()
        bad_json = (
            '{"020003":{"uniqueRequestId":2,"nickname":"a","nickname":"b"}}',
            '{"020003":{"uniqueRequestId":2,"nickname":"a"},"020003":{"uniqueRequestId":3,"nickname":"b"}}',
            '{"020003":{"uniqueRequestId":2,"nickname":"a"},"020301":{"uniqueRequestId":2,"optionList":{"1":NaN}}}',
            '{"020301":{"uniqueRequestId":2,"optionList":{"1":Infinity}}}',
            '{"020301":{"uniqueRequestId":2,"optionList":{"1":-Infinity}}}',
        )
        for raw in bad_json:
            self.assertEqual(self.request('/app/ActiveController', {'activityList': raw})['venus_status'], 2)
        for ident in (2**31, -(2**31)-1, 10**100, True, '2'):
            response = self.api({'020003': {'uniqueRequestId': 2, 'nickname': 'must rollback'},
                                 '020001': {'uniqueRequestId': ident}})
            self.assertEqual(response['venus_status'], 2)
        with self.httpd.store.connect() as db:
            self.assertEqual(json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])['nickname'], 'Tamer')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM retries').fetchone()[0], 0)
        self.assertEqual(self.api({'020001': {'uniqueRequestId': 2}})['venus_status'], 1)

    def test_rejected_http_body_closes_connection_cleanly(self):
        from contextlib import closing
        from http.client import HTTPConnection
        for headers in ({'Content-Length': '2000001', 'Content-Type': 'application/x-www-form-urlencoded'},
                        {'Content-Length': 'bad', 'Content-Type': 'application/x-www-form-urlencoded'},
                        {'Content-Length': '3', 'Content-Type': 'application/json'},
                        {'Content-Length': '3', 'Transfer-Encoding': 'chunked', 'Content-Type': 'application/x-www-form-urlencoded'}) * 5:
            with closing(HTTPConnection('127.0.0.1', self.httpd.server_port, timeout=3)) as connection:
                connection.request('POST', '/app/ActiveController', body=b'xxx', headers=headers)
                response = connection.getresponse()
                self.assertEqual(response.getheader('Connection'), 'close')
                self.assertEqual(json.loads(response.read())['venus_status'], 2)
                connection.request('GET', '/health')
                self.assertEqual(json.loads(connection.getresponse().read())['service'], 'linkz-local')

    def test_concurrent_capture_retry_spends_once(self):
        from concurrent.futures import ThreadPoolExecutor
        self.prepare_garden(count=0)
        request = {'050002': {'uniqueRequestId': 2, 'gachaId': 10001, 'playCount': 1, 'itemCount': 10}}
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda _: self.api(request), range(2)))
        self.assertEqual(responses[0]['venus_status'], 1, responses)
        self.assertEqual(responses[0], responses[1])
        data = self.api({'020001': {'uniqueRequestId': 3}, '020101': {'uniqueRequestId': 3}})['resData']
        self.assertEqual(data['020001']['playerInfo']['point'], 5)
        self.assertEqual(len(data['020101']['userMonsterList']), 1)

    def prepare_garden(self, count=4):
        self.auth()
        self.api({'020001': {'uniqueRequestId': 1}})
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            state['tutorial'] = {'statusId': '1400', 'endFlg': '1'}
            state['player'].update(point=10, meatNum='5')
            route = next(r for r in self.httpd.store.masters['990110']['monsterEvolutionRouteM']
                         if r['monsterEvolutionRouteId'] == '121')
            master = next(r for r in self.httpd.store.masters['990102']['monsterM']
                          if r['monsterId'] == route['childhood1MonsterId'])
            for _ in range(count):
                self.httpd.store.create_capture_monster({'id': 1}, state, route, master)
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        return route

    def test_batch_response_snapshot_matches_retry(self):
        self.prepare_garden(count=1)
        batch = {'020101': {'uniqueRequestId': 10},
                 '010901': {'uniqueRequestId': 10, 'userMonsterId': '1'}}
        first = self.api(batch)
        self.assertEqual(first['resData']['020101']['userMonsterList'][0]['growEndDate'], '')
        self.assertTrue(first['resData']['010901']['userMonster']['growEndDate'])
        self.assertEqual(self.api(batch), first)
        # The captured first-stage result must not change after the next activity.
        batch = {'010302': {'uniqueRequestId': 11, 'userMonsterId': '1', 'shorteningFlg': 1, 'stone': 10},
                 '020101': {'uniqueRequestId': 11}}
        response = self.api(batch)
        self.assertEqual(response['venus_status'], 1, response)
        self.assertEqual(self.api(batch), response)

    def test_garden_capacity_timer_rollback_and_relogin(self):
        self.prepare_garden()
        request = {'uniqueRequestId': 2, 'userMonsterId': '1'}
        for bad in (None, True, 1, [], '999'):
            self.assertEqual(self.api({'010901': dict(request, userMonsterId=bad)})['venus_status'], 2)
        self.assertEqual(self.api({'010901': request, '999999': {'uniqueRequestId': 2}})['venus_status'], 2)
        first = self.api({'010901': request})
        deadline = first['resData']['010901']['userMonster']['growEndDate']
        self.assertEqual(self.api({'010901': request}), first)
        for ident in ('2', '3'):
            self.assertEqual(self.api({'010901': dict(request, userMonsterId=ident, uniqueRequestId=int(ident)+2)})['venus_status'], 1)
        self.assertEqual(self.api({'010901': dict(request, userMonsterId='4', uniqueRequestId=6)})['venus_status'], 2)
        self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
        self.auth()
        resumed = self.api({'010901': dict(request, uniqueRequestId=7)})['resData']['010901']
        self.assertEqual(resumed['userMonster']['growEndDate'], deadline)
        info = self.api({'020102': {'uniqueRequestId': 8}})['resData']['020102']['gardenInfo']
        self.assertEqual(len(info['monster']), 3)
        self.assertEqual((info['time1'], info['time2']), (1, 1))
        self.assertTrue(all(0 < row['remainingTime'] <= 3600 and row['stoneNum'] == 1 for row in info['monster']))
        self.auth('another-garden-owner')
        self.assertEqual(self.api({'020102': {'uniqueRequestId': 1}})['resData']['020102']['gardenInfo']['monster'], [])
        self.assertEqual(self.api({'010901': request})['venus_status'], 2)

    def test_garden_free_growth_route_identity_and_two_stages(self):
        route = self.prepare_garden(count=1)
        self.api({'010901': {'uniqueRequestId': 2, 'userMonsterId': '1'}})
        request = {'uniqueRequestId': 3, 'userMonsterId': '1', 'shorteningFlg': 0, 'stone': 10}
        self.assertEqual(self.api({'010302': request})['venus_status'], 2)
        for stage, target in enumerate((route['childhood2MonsterId'], route['growthMonsterId']), 3):
            with self.httpd.store.connect() as db:
                state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
                state['monsters'][0]['growEndDate'] = '2000-01-01 00:00:00'
                db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
            response = self.api({'010302': dict(request, uniqueRequestId=stage)})
            self.assertEqual(response['venus_status'], 1, response)
            result = response['resData']['010302']
            self.assertEqual(result['useStone'], 0)
            self.assertEqual((result['userMonster']['userMonsterId'], result['userMonster']['monsterId']), ('1', target))
            self.assertEqual((result['userMonster']['level'], result['userMonster']['ex']), ('1', '0'))
            self.assertEqual(bool(result['userMonster']['growEndDate']), stage == 3)
            self.assertEqual(self.api({'010302': dict(request, uniqueRequestId=stage)}), response)
            if stage == 3:
                self.assertEqual(self.api({'010302': dict(request, uniqueRequestId=30)})['venus_status'], 2)
        self.auth()
        data = self.api({'020001': {'uniqueRequestId': 5}, '020101': {'uniqueRequestId': 5},
                         '020102': {'uniqueRequestId': 5}, '010101': {'uniqueRequestId': 5, 'targetUserId': '1'}})['resData']
        self.assertEqual(data['020001']['playerInfo']['point'], 10)
        self.assertEqual(data['020101']['userMonsterList'][0]['monsterId'], route['growthMonsterId'])
        self.assertEqual(data['020102']['gardenInfo']['monster'], [])
        self.assertEqual(data['010101']['possessionNum'], 3)

    def test_garden_shortening_payment_validation_and_rollback(self):
        route = self.prepare_garden(count=1)
        self.api({'010901': {'uniqueRequestId': 2, 'userMonsterId': '1'}})
        request = {'uniqueRequestId': 3, 'userMonsterId': '1', 'shorteningFlg': 1, 'stone': 10}
        for override in ({'stone': 9}, {'stone': True}, {'shorteningFlg': True}, {'shorteningFlg': 2}):
            self.assertEqual(self.api({'010302': dict(request, **override)})['venus_status'], 2)
        self.assertEqual(self.api({'010302': request, '999999': {'uniqueRequestId': 3}})['venus_status'], 2)
        response = self.api({'010302': request})
        self.assertEqual(response['resData']['010302']['useStone'], 1)
        self.assertEqual(self.api({'010302': request}), response)
        self.auth()
        self.assertEqual(self.api({'010302': dict(request, uniqueRequestId=4)})['venus_status'], 2)
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            state['player']['point'] = 2
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        self.assertEqual(self.api({'010302': dict(request, stone=2, uniqueRequestId=5)})['venus_status'], 2)
        data = self.api({'020001': {'uniqueRequestId': 6}, '020101': {'uniqueRequestId': 6}})['resData']
        self.assertEqual(data['020001']['playerInfo']['point'], 2)
        self.assertEqual(data['020101']['userMonsterList'][0]['monsterId'], route['childhood2MonsterId'])

    def test_database_context_closes_on_commit_and_rollback(self):
        with self.httpd.store.connect() as db:
            db.execute('CREATE TABLE context_probe(value TEXT)')
            db.execute('INSERT INTO context_probe VALUES(?)', ('committed',))
        import sqlite3
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute('SELECT 1')
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with self.httpd.store.connect() as failed_db:
                failed_db.execute('INSERT INTO context_probe VALUES(?)', ('must rollback',))
                raise RuntimeError('rollback')
        with self.assertRaises(sqlite3.ProgrammingError):
            failed_db.execute('SELECT 1')
        with self.httpd.store.connect() as reopened:
            self.assertEqual([r[0] for r in reopened.execute('SELECT value FROM context_probe')], ['committed'])

    def prepare_normal_battle(self, friend_points='0'):
        self.auth()
        self.api({'020001': {'uniqueRequestId': 1}})
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            state['tutorial'] = {'statusId': '1400', 'endFlg': '1'}
            state['player'].update(stamina=40, staminaMax='40', friendPoint=friend_points)
            state['monsters'] = [{'userMonsterId': '1', 'monsterId': '756', 'eggFlg': '0', 'level': '40', 'ex': '4536'}]
            state['decks']['deckList'][0]['monsterList'] = [{'userMonsterId': '1'}]
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))

    def battle_result_request(self, start, request_id, clear=1):
        return {'uniqueRequestId': request_id, 'startId': start['startId'], 'dungeonId': start['worldDungeonId'],
                'clear': clear, 'clearRound': 1, 'aliveInfo': [1] if clear == 1 else [0],
                'enemyAliveInfo': [[0 if clear == 1 else 1] * len(f['enemy']) for f in start['dungeonFloor']]}

    def test_link_rewards_capture_and_garden_loop(self):
        self.prepare_normal_battle()
        for run in range(15):
            ident = run + 10
            start = self.api({'090002': {'uniqueRequestId': ident, 'dungeonId': '1', 'deckNum': '1'}})['resData']['090002']
            request = self.battle_result_request(start, ident)
            response = self.api({'090003': request})
            self.assertEqual(response['venus_status'], 1, response)
            self.assertEqual(next(r['assetNum'] for r in response['resData']['090003']['dropReward']
                                  if r['assetCategoryId'] == '3'), 20)
            self.assertEqual(self.api({'090003': request}), response)
        self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
        self.auth()
        player = self.api({'020001': {'uniqueRequestId': 1}})['resData']['020001']['playerInfo']
        self.assertEqual(player['friendPoint'], '300')
        self.assertEqual(player['stamina'], 10)
        request = {'uniqueRequestId': 2, 'gachaId': 10002, 'playCount': 1, 'itemCount': 300}
        response = self.api({'050002': request})
        self.assertEqual(response['venus_status'], 1, response)
        self.assertEqual(self.api({'050002': request}), response)
        monster = response['resData']['050002']['userMonsterList'][0]
        self.assertEqual(self.api({'020001': {'uniqueRequestId': 3}})['resData']['020001']['playerInfo']['friendPoint'], '0')
        placed = self.api({'010901': {'uniqueRequestId': 4, 'userMonsterId': monster['userMonsterId']}})
        self.assertEqual(placed['venus_status'], 1, placed)
        self.assertTrue(placed['resData']['010901']['userMonster']['growEndDate'])

    def test_link_reward_cap_loss_rollback_and_history_retries(self):
        self.prepare_normal_battle(friend_points='999990')
        start = self.api({'090002': {'uniqueRequestId': 2, 'dungeonId': '1', 'deckNum': '1'}})['resData']['090002']
        request = self.battle_result_request(start, 3)
        self.assertEqual(self.api({'090003': request, '999999': {'uniqueRequestId': 3}})['venus_status'], 2)
        self.assertEqual(self.api({'020001': {'uniqueRequestId': 4}})['resData']['020001']['playerInfo']['friendPoint'], '999990')
        response = self.api({'090003': request})
        link = next(r for r in response['resData']['090003']['dropReward'] if r['assetCategoryId'] == '3')
        self.assertEqual(link['assetNum'], 9)
        next_start = self.api({'090002': {'uniqueRequestId': 5, 'dungeonId': '1', 'deckNum': '1'}})['resData']['090002']
        self.auth()
        self.assertEqual(self.api({'090003': dict(request, uniqueRequestId=6)}), response)
        lost = self.api({'090003': self.battle_result_request(next_start, 7, clear=0)})
        self.assertEqual(lost['resData']['090003']['dropReward'], [])
        player = self.api({'020001': {'uniqueRequestId': 8}})['resData']['020001']['playerInfo']
        self.assertEqual(player['friendPoint'], '999999')

    def test_garden_monsters_cannot_feed_or_enter_battle_decks(self):
        self.prepare_garden(count=3)
        self.assertEqual(self.api({'010202': {'uniqueRequestId': 2, 'mealMonster': '1', 'meatNum': 1}})['venus_status'], 2)
        self.assertEqual(self.api({'010402': {'uniqueRequestId': 3, 'deckData': [[1, 2, 3]],
                                            'selectDeckNum': 1, 'favoriteDeckNum': 1}})['venus_status'], 2)
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            state['decks']['deckList'][0]['monsterList'] = [{'userMonsterId': '1'}]
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        self.assertEqual(self.api({'010901': {'uniqueRequestId': 4, 'userMonsterId': '1'}})['venus_status'], 2)
        self.assertEqual(self.api({'090002': {'uniqueRequestId': 5, 'dungeonId': '1', 'deckNum': '1'}})['venus_status'], 2)
        self.assertEqual(self.api({'020001': {'uniqueRequestId': 6}})['resData']['020001']['playerInfo']['meatNum'], '5')

    def test_normal_capture_payment_retry_persistence_and_rollback(self):
        self.auth()
        self.api({'020001': {'uniqueRequestId': 1}})
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            state['tutorial'] = {'statusId': '1400', 'endFlg': '1'}
            state['player']['point'] = 55
            state['player']['friendPoint'] = '300'
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        rows = self.api({'050001': {'uniqueRequestId': 2, 'isTutorial': 0}})['resData']['050001']['result']
        self.assertEqual(rows[0]['details'][0]['price'], '5')
        request = {'uniqueRequestId': 3, 'gachaId': 10001, 'playCount': 1, 'itemCount': 55}
        for override in ({'itemCount': 0}, {'playCount': True}, {'gachaId': 99}):
            self.assertEqual(self.api({'050002': dict(request, **override)})['venus_status'], 2)
        self.assertEqual(self.api({'050002': request, '999999': {'uniqueRequestId': 3}})['venus_status'], 2)
        response = self.api({'050002': request})
        self.assertEqual(response['venus_status'], 1)
        self.assertEqual(self.api({'050002': request}), response)
        monster = response['resData']['050002']['userMonsterList'][0]
        self.assertIn(monster['monsterId'], ('81', '121', '176', '196'))
        self.assertEqual(monster['level'], '1')
        slots = self.api({'180006': {'uniqueRequestId': 4, 'userMonsterId': [1]}})['resData']['180006']['slotInfo']
        self.assertEqual(slots[0]['manage']['maxSlotNum'], 5)
        ten = dict(request, uniqueRequestId=5, playCount=10, itemCount=50)
        self.assertEqual(len(self.api({'050002': ten})['resData']['050002']['userMonsterList']), 10)
        self.assertEqual(self.api({'050002': dict(request, uniqueRequestId=6, itemCount=0)})['venus_status'], 2)
        link = dict(request, uniqueRequestId=6, gachaId=10002, itemCount=300)
        self.assertEqual(self.api({'050002': link})['venus_status'], 1)
        self.assertEqual(self.api({'050002': dict(link, uniqueRequestId=7, itemCount=0)})['venus_status'], 2)
        self.auth()
        data = self.api({'020101': {'uniqueRequestId': 8}, '020001': {'uniqueRequestId': 8}})['resData']
        self.assertEqual(len(data['020101']['userMonsterList']), 12)
        self.assertEqual(data['020001']['playerInfo']['point'], 0)
        self.assertEqual(data['020001']['playerInfo']['friendPoint'], '0')
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            state['player']['point'] = 5
            state['player']['unitLimitMax'] = '12'
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        self.assertEqual(self.api({'050002': dict(request, uniqueRequestId=9, itemCount=5)})['venus_status'], 2)
        self.assertEqual(self.api({'020001': {'uniqueRequestId': 9}})['resData']['020001']['playerInfo']['point'], 5)

    def test_extended_dungeon_prerequisite_and_reward_loop(self):
        self.auth()
        self.api({'020001': {'uniqueRequestId': 1}})
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            state['tutorial'] = {'statusId': '1400', 'endFlg': '1'}
            state['monsters'] = [{'userMonsterId': '1', 'monsterId': '756', 'eggFlg': '0', 'level': '40', 'ex': '4536'}]
            state['decks']['deckList'][0]['monsterList'] = [{'userMonsterId': '1'}]
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        request = {'uniqueRequestId': 2, 'dungeonId': '4', 'deckNum': '1'}
        self.assertEqual(self.api({'090002': request})['venus_status'], 2)
        with self.httpd.store.connect() as db:
            state['cleared_dungeons'] = ['1', '2', '3']
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        for dungeon in range(4, 10):
            start = self.api({'090002': dict(request, uniqueRequestId=dungeon, dungeonId=str(dungeon))})['resData']['090002']
            self.assertEqual(len(start['dungeonFloor']), 3)
            result = {'uniqueRequestId': dungeon, 'startId': start['startId'], 'dungeonId': str(dungeon),
                      'clear': 1, 'clearRound': 1, 'aliveInfo': [1],
                      'enemyAliveInfo': [[0] * len(f['enemy']) for f in start['dungeonFloor']]}
            settled = self.api({'090003': result})
            self.assertEqual(settled['venus_status'], 1)
            self.assertEqual(self.api({'090003': result}), settled)
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            self.assertEqual(state['player']['point'], 12)
            self.assertEqual(state['facility_keys'], {'2': 1})
            self.assertEqual(state['cleared_dungeons'], [str(i) for i in range(1, 10)])
        world = self.api({'090009': {'uniqueRequestId': 10, 'worldId': '1'}})['resData']['090009']
        self.assertEqual(next(d for st in world['worldDungeonInfo'] for d in st['dungeons'] if d['worldDungeonId'] == 10)['status'], 2)

    def test_full_normal_world_earned_materials_evolution_research_chain(self):
        from unittest.mock import patch
        self.prepare_normal_battle()
        state = self.talent_state()
        # Combat/stamina fixture only. Every coin, stone, material and extra
        # monster below is acquired through APIs, not written into inventory.
        state['player'].update(stamina=1000, staminaMax='1000')
        self.save_talent_state(state)
        sequence = 100

        def call(api, **params):
            nonlocal sequence
            sequence += 1
            response = self.api({api: dict(params, uniqueRequestId=sequence)})
            self.assertEqual(response['venus_status'], 1, response)
            return response['resData'][api]

        def win(ident):
            start = call('090002', dungeonId=str(ident), deckNum='1')
            result = call('090003', startId=start['startId'], dungeonId=str(ident), clear=1,
                clearRound=1, aliveInfo=[1] * len(start['deck']['userMonsterIds']),
                enemyAliveInfo=[[0] * len(f['enemy']) for f in start['dungeonFloor']])
            self.assertEqual(len(start['dungeonFloor']), 3)
            return result

        def capture(mid):
            with patch('linkz_server.secrets.choice', return_value=mid):
                result = call('050002', gachaId=10001, playCount=1, itemCount=self.talent_state()['player']['point'])
            return int(result['userMonsterList'][0]['userMonsterId'])

        targets = []
        for dungeon in range(1, 84):
            win(dungeon)
            if dungeon == 5:
                targets = [capture('81'), capture('81')]
                call('010402', deckData=[[1, *targets]], selectDeckNum=1, favoriteDeckNum=1)
        earned = self.talent_state()
        self.assertEqual(earned['cleared_dungeons'], [str(i) for i in range(1, 84)])
        self.assertEqual(earned['facility_keys'], {'1': 1, '2': 1, '11': 1, '3': 1})
        expected_materials = {str(r['assetValue']) for c in self.httpd.store.normal_world.values()
                              for r in self.httpd.store.normal_material_drops(c)}
        self.assertEqual({r['soulId'] for r in earned['souls']}, expected_materials)
        self.assertEqual(len(expected_materials), 171)
        world = call('090009', worldId='1')['worldDungeonInfo']
        self.assertTrue(all(d['status'] == 4 for st in world for d in st['dungeons']))
        # Renewable drops close both extra Agumon -> WarGreymon recipes.
        required = {}
        for base, destination in (('81', '366'), ('366', '756'), ('756', '866')):
            evolution = next(r for r in self.httpd.store.masters['990104']['monsterEvolutionM']
                             if r['baseMonsterId'] == base and r['nextMonsterId'] == destination)
            material = next(r for r in self.httpd.store.masters['990115']['monsterEvolutionMaterialM']
                            if r['monsterEvolutionMaterialId'] == evolution['monsterEvolutionMaterialId'])
            for i in range(1, 8):
                sid, amount = material['materialAssetValue' + str(i)], int(material['materialAssetNum' + str(i)])
                if amount:
                    required[sid] = required.get(sid, 0) + 2 * amount
        # Original866 attribute-change recipe: reserve these in addition to
        # both evolution chains. Fresh ultimate evolution does not make the
        # remaining plugin balance sufficient for another paid operation.
        resistance_required = {'1070174': 2, '2010001': 25, '2010002': 20, '2010003': 20, '2010004': 10}
        for sid, amount in resistance_required.items():
            required[sid] = required.get(sid, 0) + amount
        for sid, amount in required.items():
            route = next(ident for ident, c in self.httpd.store.normal_world.items()
                         if any(str(r['assetValue']) == sid for r in self.httpd.store.normal_material_drops(c)))
            for _ in range(50):
                owned = next(r for r in self.talent_state()['souls'] if r['soulId'] == sid)
                if int(owned['num']) >= amount:
                    break
                win(route)
            else:
                self.fail('Renewable material did not meet recipe: ' + sid)
        for destination in (366, 756, 866):
            for _ in range(40):
                current = {int(r['userMonsterId']): r for r in self.talent_state()['monsters']}
                if all(current[i]['level'] == next(m['maxLevel'] for m in self.httpd.store.masters['990102']['monsterM']
                           if m['monsterId'] == current[i]['monsterId']) for i in targets):
                    break
                win(83)
            else:
                self.fail('Earned battle EXP did not reach evolution level')
            for ident in targets:
                call('010301', userMonsterId=str(ident), monsterId=destination)
        # Spend only API-earned coins/stones/materials to open the dojo and
        # change a freshly evolved extra ultimate's resistance. The initial
        # combat/stamina fixture remains the only non-natural starting aid.
        dojo = call('140001', facilityId=4, positionX=10, positionY=10)['userFacilityId']
        self.assertEqual(call('140002', userFacilityId=dojo)['num'], 2)
        before_change = self.talent_state()
        with patch('linkz_server.secrets.choice', return_value='915'):
            changed = call('010801', baseUserMonsterId=targets[0], materialUserMonsterId=targets[0], type=1)['userMonster']
        self.assertEqual(changed['tranceResistance'], '915')
        self.assertEqual(len(self.talent_state()['monsters']), len(before_change['monsters']))
        self.assertEqual(self.talent_state()['player']['gamemoney'], before_change['player']['gamemoney'])
        after_materials = {r['soulId']: int(r['num']) for r in self.talent_state()['souls']}
        spent = {r['soulId']: int(r['num']) - after_materials[r['soulId']] for r in before_change['souls']}
        self.assertEqual({s: n for s, n in spent.items() if n}, resistance_required)
        self.assertEqual(call('020101', userMonsterIds=[targets[0]])['userMonsterList'][0]['tranceResistance'], '915')
        extra = capture('176')
        fixed = next(int(m['userMonsterId']) for m in self.talent_state()['monsters'] if m['userMonsterId'] != '1' and m['monsterId'] == '756')
        call('010402', deckData=[[1, fixed, extra]], selectDeckNum=1, favoriteDeckNum=1)
        research = call('010501', baseUserMonsterId=str(targets[0]), materialUserMonsterId=str(targets[1]))
        egg = research['userMonster']
        self.assertEqual(egg['eggFlg'], '1')
        self.assertEqual(egg['tranceResistance'], '')
        self.assertTrue(all(m['userMonsterId'] not in {str(i) for i in targets} for m in self.talent_state()['monsters']))
        hatched = call('010304', userMonsterId=egg['userMonsterId'])['userMonster']
        self.assertEqual(hatched['eggFlg'], '0')
        self.assertTrue(hatched['growEndDate'])
        self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
        self.auth()
        after = self.talent_state()
        self.assertEqual(after['normal_research_count'], 1)
        self.assertEqual(after['normal_evolution_count'], 6)
        self.assertEqual(after['normal_resistance_change_count'], 1)
        self.assertEqual(after['cleared_dungeons'], earned['cleared_dungeons'])
        self.assertEqual(next(m for m in after['monsters'] if m['userMonsterId'] == hatched['userMonsterId']), hatched)

    def test_api_earned_four_generation_awakening_maxlevel_version_and_chip_carryover(self):
        from datetime import datetime, timezone
        from unittest.mock import patch
        self.prepare_normal_battle()
        state = self.talent_state()
        # Only tutorial, one combat partner and ample stamina are fixtures.
        # Every new partner, currency, material, chip and patch is API earned.
        state['player'].update(gamemoney='0', point=0, friendPoint='0', meatNum='0',
                               stamina=50000, staminaMax='50000')
        state.update(souls=[], chips=[], items=[], facility_keys={}, cleared_dungeons=[],
                     claimed_dungeon_rewards=[])
        self.save_talent_state(state)
        sequence, wins = 100, 0
        store = self.httpd.store
        masters = {r['monsterId']: r for r in store.masters['990102']['monsterM']}
        recipes = {str(r['monsterEvolutionMaterialId']): r for r in
                   store.masters['990115']['monsterEvolutionMaterialM']}

        def call(api, **params):
            nonlocal sequence
            sequence += 1
            response = self.api({api: dict(params, uniqueRequestId=sequence)})
            self.assertEqual(response['venus_status'], 1, (api, response))
            return response['resData'][api]

        def owned(ident):
            return next(m for m in self.talent_state()['monsters'] if m['userMonsterId'] == str(ident))

        def team(*ids, exclude=()):
            selected = [1, *map(int, ids)]
            excluded = set(map(int, exclude))
            for monster in self.talent_state()['monsters']:
                ident = int(monster['userMonsterId'])
                if (len(selected) < 3 and ident not in selected and ident not in excluded and
                        monster['eggFlg'] == '0' and not monster.get('growEndDate') and
                        store.monster_growth_step(monster) not in ('1', '2', '3')):
                    selected.append(ident)
            self.assertEqual(len(selected), 3)
            call('010402', deckData=[selected], selectDeckNum=1, favoriteDeckNum=1)

        def win(dungeon=83):
            nonlocal wins
            start = call('090002', dungeonId=str(dungeon), deckNum='1')
            # A successful original 30% friendship roll is deterministic here.
            # Enemy defeat/survival flags are protocol inputs, not played combat.
            with patch('linkz_server.secrets.randbelow', return_value=0):
                result = call('090003', startId=start['startId'], dungeonId=str(dungeon),
                    clear=1, clearRound=1, aliveInfo=[1] * len(start['deck']['userMonsterIds']),
                    enemyAliveInfo=[[0] * len(f['enemy']) for f in start['dungeonFloor']])
            wins += 1
            return result

        def capture(mid, gacha):
            def choose(pool):
                self.assertIn(mid, pool)  # Never force an unoffered master ID.
                return mid
            with patch('linkz_server.secrets.choice', side_effect=choose):
                balance_key = 'point' if gacha == 10001 else 'friendPoint'
                result = call('050002', gachaId=gacha, playCount=1,
                              itemCount=int(self.talent_state()['player'][balance_key]))
            return int(result['userMonsterList'][0]['userMonsterId'])

        def grow(ident, hatch=False):
            call('010304' if hatch else '010901', userMonsterId=str(ident))
            for _ in range(2):
                # Advance only the test process clock to the actual API timer.
                deadline = datetime.strptime(owned(ident)['growEndDate'], '%Y-%m-%d %H:%M:%S')
                deadline = int(deadline.replace(tzinfo=timezone.utc).timestamp())
                with patch('linkz_server.time.time', return_value=deadline + 1):
                    result = call('010302', userMonsterId=str(ident), shorteningFlg=0, stone=0)
                self.assertEqual(result['useStone'], 0)
            self.assertEqual(owned(ident)['growEndDate'], '')

        def earn_recipe(recipe, multiplier=1):
            for slot in range(1, 8):
                sid, amount = str(recipe['materialAssetValue' + str(slot)]), int(recipe['materialAssetNum' + str(slot)])
                if not amount:
                    continue
                self.assertEqual(recipe['materialAssetCategoryId' + str(slot)], '14')
                dungeon = next(d for d, c in store.normal_world.items()
                    if any(str(r['assetValue']) == sid for r in store.normal_material_drops(c)))
                for _ in range(250):
                    balance = next((int(r['num']) for r in self.talent_state()['souls'] if r['soulId'] == sid), 0)
                    if balance >= amount * multiplier:
                        break
                    win(dungeon)
                else:
                    self.fail('API-earned recipe could not be paid: ' + sid)

        def evolve(ident):
            rare = int(masters[owned(ident)['monsterId']]['rare'])
            for target in (str(365 + rare), str(755 + rare), str(865 + rare)):
                for _ in range(150):
                    current = owned(ident)
                    if current['level'] == masters[current['monsterId']]['maxLevel']:
                        break
                    win()
                else:
                    self.fail('Battle EXP did not reach evolution level')
                route = next(r for r in store.masters['990104']['monsterEvolutionM']
                    if r['baseMonsterId'] == owned(ident)['monsterId'] and r['nextMonsterId'] == target)
                earn_recipe(recipes[str(route['monsterEvolutionMaterialId'])])
                call('010301', userMonsterId=str(ident), monsterId=int(target))

        call('110101', countryCode=3)
        for dungeon in range(1, 84):
            win(dungeon)
        base = capture('81', 10001)
        capture('81', 10001)  # Earn a spare to keep a legal three-member team during research.
        self.assertEqual(owned(base)['monsterId'], '81')
        factory = call('140001', facilityId=25, positionX=10, positionY=10)['userFacilityId']
        call('140010', userFacilityId=factory)
        call('110102', missionId=[10006, 10007, 10008])
        gifts = call('070001', page=1)['prizeData']
        call('070002', receiveType=2, receiveIds=[r['receiveId'] for r in gifts])
        with patch('chip_inventory.secrets.randbelow', return_value=0):
            call('050012', gachaId=10003, playCount=1)
        chip_id = call('180001')['userChipList'][0]['userChipId']
        generations = []
        for awakening in range(4):
            material = capture('81', 10001)
            team(base, material)
            for ident in (base, material):
                evolve(ident)
            call('180004', userMonsterId=base, dispNum=1)
            call('180002', act=1, type=1, dispNum=1, userChipId=chip_id, userMonsterId=base)
            for _ in range(301):
                current = owned(base)
                if current['friendship'] == '300':
                    break
                win()
            else:
                self.fail('API-earned friendship did not reach awakening threshold')
            team(exclude=(base, material))
            before = self.talent_state()
            egg = call('010501', baseUserMonsterId=str(base), materialUserMonsterId=str(material))['userMonster']
            base = int(egg['userMonsterId'])
            self.assertEqual((egg['monsterId'], egg['monsterEvolutionRouteId']),
                             (str(17 + awakening), str(122 + awakening)))
            self.assertEqual(egg['friendship'], '0')
            after = self.talent_state()
            self.assertLess(int(after['player']['gamemoney']), int(before['player']['gamemoney']))
            self.assertEqual(next(c for c in after['chips'] if c['userChipId'] == chip_id)['userMonsterId'], 0)
            self.assertEqual(next(s for s in after['chip_slots'] if s['userMonsterId'] == base)['manage']['extraSlotNum'], 0)
            grow(base, hatch=True)
            generations.append(owned(base)['monsterId'])
            self.assertEqual(generations[-1], str(82 + awakening))
        team(base)
        evolve(base)
        call('180004', userMonsterId=base, dispNum=1)
        call('180002', act=1, type=1, dispNum=1, userChipId=chip_id, userMonsterId=base)
        for _ in range(150):
            if owned(base)['level'] == masters['870']['maxLevel']:
                break
            win()
        self.assertEqual((owned(base)['monsterId'], owned(base)['level']), ('870', masters['870']['maxLevel']))
        route = next(r for r in store.masters['990104']['monsterEvolutionM']
                     if r['baseMonsterId'] == '870' and r['type'] == '3' and r['effectType'] == '5')
        recipe = recipes[str(route['monsterEvolutionMaterialId'])]
        earn_recipe(recipe)
        while int(self.talent_state()['player']['gamemoney']) < 135000:
            self.assertLess(wins, 2500)
            win()
        team(exclude=(base,))
        request = dict(baseUserMonsterId=base, target=int(route['nextMonsterId']),
                       material=[int(recipe['materialAssetValue' + str(i)]) for i in range(1, 8)
                                 if int(recipe['materialAssetNum' + str(i)])])
        upgraded = call('010802', **request)['userMonster']
        self.assertEqual(upgraded['monsterId'], route['nextMonsterId'])
        self.assertNotEqual(upgraded['userMonsterId'], str(base))
        self.assertEqual(next(c for c in self.talent_state()['chips'] if c['userChipId'] == chip_id)['userMonsterId'], 0)
        self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
        self.auth()
        self.assertEqual(call('010802', **request)['userMonster'], upgraded)
        self.assertEqual(self.talent_state()['normal_research_count'], 4)
        self.assertEqual(self.talent_state()['normal_evolution_count'], 27)
        self.assertEqual(self.talent_state()['normal_hatching_count'], 4)
        print('FOUR_GENERATION_API_CHAIN ' + json.dumps(dict(wins=wins, generations=generations,
            finalMonsterId=upgraded['monsterId'], finalLevel=upgraded['level'],
            fixtures='tutorial/fighter/stamina; forced offered captures and friendship rolls; test garden clock',
            currenciesAndPartnersEarned=True), ensure_ascii=False), flush=True)

    def test_multi_material_battle_preview_snapshot_defeat_and_rollback(self):
        self.prepare_normal_battle()
        state = self.talent_state()
        state['cleared_dungeons'] = [str(i) for i in range(1, 29)]
        self.save_talent_state(state)
        store = self.httpd.store
        materials = store.normal_material_drops(store.normal_world['29'])
        world = self.api({'090009': {'uniqueRequestId': 2, 'worldId': '1'}})['resData']['090009']
        row = next(d for st in world['worldDungeonInfo'] for d in st['dungeons'] if d['worldDungeonId'] == 29)
        self.assertEqual(row['dropAssets'], [dict(r, assetCategoryId=14) for r in materials])
        start = self.api({'090002': {'uniqueRequestId': 3, 'dungeonId': '29', 'deckNum': '1'}})['resData']['090002']
        self.assertTrue(all(f['enemy'][0]['drop'] == [{'dropBoxType': r['dropBoxType']} for r in materials] for f in start['dungeonFloor']))
        before = self.talent_state()
        request = self.battle_result_request(start, 4)
        self.assertEqual(self.api({'090003': request, '999999': {'uniqueRequestId': 4}})['venus_status'], 2)
        self.assertEqual(self.talent_state()['souls'], before['souls'])
        original = store.normal_world['29']['materialsPerWave']
        store.normal_world['29']['materialsPerWave'] = [dict(r, assetNum=99) for r in materials]
        try:
            result = self.api({'090003': request})['resData']['090003']
        finally:
            store.normal_world['29']['materialsPerWave'] = original
        self.assertEqual(result['dropReward'][:-1], materials * 3)
        after = self.talent_state()
        self.auth()
        self.assertEqual(self.api({'090003': dict(request, uniqueRequestId=5)})['resData']['090003'], result)
        self.assertEqual(self.talent_state()['souls'], after['souls'])
        repeat = self.api({'090002': {'uniqueRequestId': 6, 'dungeonId': '29', 'deckNum': '1'}})['resData']['090002']
        lost = self.api({'090003': self.battle_result_request(repeat, 7, clear=0)})['resData']['090003']
        self.assertEqual(lost['dropReward'], [])
        self.assertEqual(self.talent_state()['souls'], after['souls'])

    def test_malformed_multi_material_config_does_not_start_or_charge(self):
        self.prepare_normal_battle()
        state = self.talent_state()
        state['cleared_dungeons'] = [str(i) for i in range(1, 10)]
        self.save_talent_state(state)
        config = self.httpd.store.normal_world['10']
        original = config['materialsPerWave']
        valid = original[0]
        invalid = ([dict(valid, assetNum=True)], [dict(valid, assetValue=999)],
                   [dict(valid, dropBoxType=0)], [valid, valid], None)
        try:
            for drops in invalid:
                config['materialsPerWave'] = drops
                response = self.api({'090002': {'uniqueRequestId': 2, 'dungeonId': '10', 'deckNum': '1'}})
                self.assertEqual(response['venus_status'], 2, response)
                self.assertEqual(self.talent_state()['player']['stamina'], 40)
                self.assertNotIn('active_battle', self.talent_state())
        finally:
            config['materialsPerWave'] = original

    def test_warehouse_build_upgrade_completion_and_retry(self):
        self.auth()
        self.api({'020001': {'uniqueRequestId': 1}})
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            state['tutorial'] = {'statusId': '1400', 'endFlg': '1'}
            state['player']['gamemoney'] = '10000'
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        db.close()
        build = {'facilityId': 2, 'positionX': 10, 'positionY': 10, 'uniqueRequestId': 2}
        self.assertEqual(self.api({'140001': build})['venus_status'], 2)
        with self.httpd.store.connect() as db:
            state['facility_keys'] = {'1': 1}
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        db.close()
        for position in (-1, True, 2147483648, 0, 24):
            self.assertEqual(self.api({'140001': dict(build, positionX=position)})['venus_status'], 2)
        self.assertEqual(self.api({'140001': build, '999999': {'uniqueRequestId': 2}})['venus_status'], 2)
        first = self.api({'140001': build})
        self.assertEqual(first['venus_status'], 1)
        self.assertEqual(self.api({'140001': build}), first)
        ident = first['resData']['140001']['userFacilityId']
        self.assertEqual(self.api({'140001': dict(build, uniqueRequestId=3)})['venus_status'], 2)
        complete = {'userFacilityId': ident, 'uniqueRequestId': 3}
        self.assertEqual(self.api({'140010': complete})['venus_status'], 2)
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            self.assertEqual(state['player']['gamemoney'], '9000')
            self.assertEqual(state['player']['meatLimitMax'], '50')
            self.assertEqual(state['facility_keys'], {'1': 1})
            state['facilities'][0]['completeTime'] = '2000-01-01 00:00:00'
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        db.close()
        self.assertEqual(self.api({'140010': complete})['venus_status'], 1)
        self.assertEqual(self.api({'140010': dict(complete, uniqueRequestId=4)})['venus_status'], 1)
        upgrade = {'userFacilityId': ident, 'uniqueRequestId': 5}
        self.assertEqual(self.api({'140003': upgrade, '999999': {'uniqueRequestId': 5}})['venus_status'], 2)
        self.assertEqual(self.api({'140003': upgrade})['venus_status'], 1)
        self.assertEqual(self.api({'140003': upgrade})['venus_status'], 1)
        self.assertEqual(self.api({'140003': dict(upgrade, uniqueRequestId=6)})['venus_status'], 2)
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            self.assertEqual(state['facilities'][0]['level'], 1)
            self.assertEqual(state['player']['meatLimitMax'], '100')
            cost = int(next(r for r in self.httpd.store.masters['990302']['facilityUpgradeM']
                            if r['facilityId'] == '2' and r['level'] == '2')['upgradeAssetNum1'])
            self.assertEqual(state['player']['gamemoney'], str(9000 - cost))
            state['facilities'][0]['completeTime'] = '2000-01-01 00:00:00'
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        db.close()
        self.assertEqual(self.api({'140010': dict(complete, uniqueRequestId=7)})['venus_status'], 1)
        self.auth()
        player = self.api({'020001': {'uniqueRequestId': 8}})['resData']['020001']['playerInfo']
        self.assertEqual(player['meatLimitMax'], '150')

    def test_facility_footprint_overlap_and_invalid_tiles(self):
        store = self.httpd.store
        store.check_facility_placement({'facilities': []}, 2, 10, 10)
        state = {'facilities': [{'facilityId': 1, 'positionX': 12, 'positionY': 11}]}
        with self.assertRaisesRegex(Exception, 'overlaps'):
            store.check_facility_placement(state, 2, 11, 10)
        for x, y in ((0, 0), (23, 31), (2, 2)):
            with self.assertRaisesRegex(Exception, 'outside buildable'):
                store.check_facility_placement({'facilities': []}, 2, x, y)

    def test_normal_evolution_cost_materials_identity_and_rollback(self):
        self.auth()
        self.api({'020001': {'uniqueRequestId': 1}})
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            state['player']['gamemoney'] = '1000'
            state['monsters'] = [{'userMonsterId': '1', 'monsterId': '81', 'eggFlg': '0', 'level': '10',
                                  'ex': '229', 'defaultSkillGroupSubId': '1', 'commonSkillId': '473', 'luck': '2'}]
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        db.close()
        before_book = self.api({'010101': {'targetUserId': '1', 'uniqueRequestId': 1}})['resData']['010101']
        self.assertEqual(before_book['possessionNum'], 1)
        self.assertEqual(self.api({'010101': {'targetUserId': '2', 'uniqueRequestId': 2}})['venus_status'], 2)
        request = {'userMonsterId': '1', 'monsterId': 366, 'uniqueRequestId': 2}
        self.assertEqual(self.api({'010301': request})['venus_status'], 2)
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            self.assertEqual(state['player']['gamemoney'], '1000')
            state['souls'] = [{'soulId': '2010001', 'num': '8'}, {'soulId': '2010002', 'num': '3'}]
            state['monsters'][0]['level'] = '9'
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        db.close()
        self.assertEqual(self.api({'010301': request})['venus_status'], 2)
        with self.httpd.store.connect() as db:
            state['monsters'][0]['level'] = '10'
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        db.close()
        self.assertEqual(self.api({'010301': dict(request, monsterId=756)})['venus_status'], 2)
        self.assertEqual(self.api({'010301': request, '999999': {'uniqueRequestId': 2}})['venus_status'], 2)
        first = self.api({'010301': request})
        monster = first['resData']['010301']['userMonster']
        self.assertEqual((monster['userMonsterId'], monster['monsterId'], monster['level'], monster['ex']), ('1', '366', '1', '0'))
        self.assertEqual((monster['hp'], monster['attack'], monster['commonSkillId'], monster['luck']), ('385', '756', '473', '2'))
        self.assertEqual(self.api({'010301': request}), first)
        self.auth()
        data = self.api({'020001': {'uniqueRequestId': 3}, '020303': {'uniqueRequestId': 3}})['resData']
        self.assertEqual(data['020001']['playerInfo']['gamemoney'], '400')
        self.assertTrue(all(r['num'] == '0' for r in data['020303']['userSoulData']))
        after_book = self.api({'010101': {'targetUserId': '1', 'uniqueRequestId': 4}})['resData']['010101']
        self.assertEqual(after_book['possessionNum'], 2)
        self.assertTrue(all(r in after_book['userCollectionList'] for r in before_book['userCollectionList']))
        self.assertTrue(all(r['collectionStatus'] == '1' for r in after_book['userCollectionList']))
        profile = self.api({'020002': {'targetUserId': 1, 'uniqueRequestId': 4}})['resData']['020002']
        self.assertEqual(profile['collection']['possessionNum'], '2')
        self.assertEqual(profile['playHistory']['evolutionCount'], '1')

    def test_normal_meal_level_cap_retry_and_rollback(self):
        self.auth()
        self.api({'020001': {'uniqueRequestId': 1}})
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            state['player']['meatNum'] = '40'
            state['monsters'] = [{'userMonsterId': '1', 'monsterId': '121', 'eggFlg': '0', 'level': '1', 'ex': '0'}]
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        db.close()
        request = {'mealMonster': '1', 'meatNum': 1, 'uniqueRequestId': 2}
        for invalid in ({'mealMonster': '999'}, {'meatNum': 0}, {'meatNum': -1}, {'meatNum': True}, {'meatNum': 41}):
            self.assertEqual(self.api({'010202': dict(request, **invalid)})['venus_status'], 2)
        self.assertEqual(self.api({'010202': request, '999999': {'uniqueRequestId': 2}})['venus_status'], 2)
        first = self.api({'010202': request})
        monster = first['resData']['010202']['userMonster']
        self.assertEqual((monster['ex'], monster['level']), ('10', '2'))
        self.assertEqual(self.api({'010202': request}), first)
        capped = self.api({'010202': dict(request, meatNum=23, uniqueRequestId=3)})['resData']['010202']['userMonster']
        self.assertEqual((capped['ex'], capped['level'], capped['nextLevelEx']), ('229', '10', '0'))
        self.assertEqual(self.api({'010202': dict(request, uniqueRequestId=4)})['venus_status'], 2)
        self.auth()
        player = self.api({'020001': {'uniqueRequestId': 5}})['resData']['020001']['playerInfo']
        self.assertEqual(player['meatNum'], '16')
        profile = self.api({'020002': {'targetUserId': 1, 'uniqueRequestId': 6}})['resData']['020002']
        self.assertEqual(profile['playHistory']['useMeatNum'], '24')

    def test_normal_harvest_cap_retry_capacity_and_batch_rollback(self):
        self.auth()
        self.api({'020001': {'uniqueRequestId': 1}})
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            state['facilities'] = [{'userFacilityId': 4, 'facilityId': 1, 'level': 1, 'completeTime': ''}]
            state['last_harvest'] = [{'userFacilityId': 4, 'lastHarvestTime': '2020-01-01 00:00:00'}]
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        request = {'userFacilityId1': 4, 'userFacilityId2': 0, 'uniqueRequestId': 2}
        self.assertEqual(self.api({'140009': dict(request, userFacilityId1=99)})['venus_status'], 2)
        self.assertEqual(self.api({'140009': dict(request, userFacilityId2=99)})['venus_status'], 2)
        self.assertEqual(self.api({'140009': request, '999999': {'uniqueRequestId': 2}})['venus_status'], 2)
        first = self.api({'140009': request})
        self.assertEqual(first['resData']['140009'], {'harvestNum': 20})
        self.assertEqual(self.api({'140009': request}), first)
        self.assertEqual(self.api({'140009': dict(request, uniqueRequestId=3)})['resData']['140009'], {'harvestNum': 0})
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            self.assertEqual(state['player']['meatNum'], '20')
            state['player']['meatNum'] = '40'
            state['last_harvest'][0]['lastHarvestTime'] = '2020-01-01 00:00:00'
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        full = self.api({'140009': dict(request, uniqueRequestId=4)})
        self.assertEqual(full['venus_status'], 2)
        self.assertEqual(full['subject'], 'E-FA20')
        self.assertEqual(full['e'], 'E-FA20')
        self.assertIn('上限', full['message'])
        self.assertNotRegex(full['message'], '[\u3040-\u30ff]')
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            self.assertEqual(state['player']['meatNum'], '40')
            self.assertEqual(state['last_harvest'][0]['lastHarvestTime'], '2020-01-01 00:00:00')

    def test_fixed_gift_legacy_skill_migration(self):
        state = {'monsters': [{'monsterId': '756', 'commonSkillId': '0'},
                              {'monsterId': '756', 'commonSkillId': '500'}]}
        initialize_player_state(state)
        self.assertEqual([m['commonSkillId'] for m in state['monsters']], ['473', '500'])
        initialize_player_state(state)
        self.assertEqual([m['commonSkillId'] for m in state['monsters']], ['473', '500'])

    def test_stamina_elapsed_time_and_full_cap(self):
        state = {'player': {'stamina': 18, 'staminaMax': '20', 'recovery': 360}, 'stamina_full_at': 1360}
        refresh_stamina(state, 1179)
        self.assertEqual((state['player']['stamina'], state['player']['recovery']), (18, 181))
        refresh_stamina(state, 1180)
        self.assertEqual((state['player']['stamina'], state['player']['recovery']), (19, 180))
        refresh_stamina(state, 5000)
        self.assertEqual((state['player']['stamina'], state['player']['recovery']), (20, 0))
        self.assertNotIn('stamina_full_at', state)
        state['player']['stamina'] = 25
        refresh_stamina(state, 6000)
        self.assertEqual(state['player']['stamina'], 25)

    def test_battle_overcap_stamina_debit_recovery_reauth_and_rollback(self):
        from copy import deepcopy
        from unittest.mock import patch
        self.prepare_normal_battle()
        baseline = self.talent_state()
        now = int(time.time())
        # Explicit temporary balance/clock fixtures; original dungeon1 costs2.
        cases = [(41, 39, 180, None), (43, 41, 0, None), (42, 40, 0, None),
                 (40, 38, 360, None), (39, 37, 539, now + 179)]
        with patch('linkz_server.time.time', return_value=now):
            for index, (initial, expected, recovery, deadline) in enumerate(cases, 100):
                state = deepcopy(baseline)
                state['player'].update(stamina=initial, staminaMax='40', recovery=0)
                state.pop('stamina_full_at', None)
                if deadline:
                    state['stamina_full_at'] = deadline
                self.save_talent_state(state)
                request = dict(uniqueRequestId=index, dungeonId='1', deckNum='1')
                before = self.talent_state()
                self.assertEqual(self.api({'090002': request, '999999': {'uniqueRequestId': index}})['venus_status'], 2)
                self.assertEqual(self.talent_state(), before)
                entered = self.api({'090002': request, '020001': {'uniqueRequestId': index}})
                self.assertEqual(entered['venus_status'], 1, entered)
                player = entered['resData']['020001']['playerInfo']
                self.assertEqual(player['stamina'], expected)
                # A separate read must not recalculate away previously banked stamina.
                read = self.chip_call('020001', index + 100)['playerInfo']
                self.assertEqual((read['stamina'], read['recovery']), (expected, recovery))
                self.assertEqual(self.api({'090002': request})['resData']['090002'], entered['resData']['090002'])
                self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
                self.auth()
                read = self.chip_call('020001', index + 200)['playerInfo']
                self.assertEqual((read['stamina'], read['recovery']), (expected, recovery))
                if recovery:
                    self.assertEqual(self.talent_state()['stamina_full_at'], now + recovery)
                else:
                    self.assertNotIn('stamina_full_at', self.talent_state())
            # Keep the partially elapsed point when spending below the cap.
            with patch('linkz_server.time.time', return_value=now + 179):
                read = self.chip_call('020001', 500)['playerInfo']
                self.assertEqual((read['stamina'], read['recovery']), (38, 360))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / 'save.sqlite3'
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.base = f'http://127.0.0.1:{self.httpd.server_port}'
        self.httpd.public_base = self.base
        self.httpd.probe_assets = False
        self.httpd.asset_root = None
        self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.token = self.secret = ''

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, form, token=None, secret=None):
        oauth = {'oauth_consumer_key': 'test-client', 'oauth_signature_method': 'HMAC-SHA1',
                 'oauth_timestamp': str(int(time.time())), 'oauth_nonce': secrets.token_hex(12),
                 'oauth_version': '1.0'}
        token = self.token if token is None else token
        secret = self.secret if secret is None else secret
        if token:
            oauth['oauth_token'] = token
        oauth['oauth_signature'] = signature('POST', self.base + path,
                                            list(form.items()) + list(oauth.items()), 'test-secret', secret)
        header = 'OAuth ' + ', '.join(f'{encode(k)}="{encode(v)}"' for k, v in oauth.items())
        req = Request(self.base + path, urlencode(form).encode(),
                      {'Authorization': header, 'Content-Type': 'application/x-www-form-urlencoded'})
        with urlopen(req, timeout=5) as r:
            return json.load(r)

    def auth(self, uuid='local-test-device'):
        result = self.request('/appApi/AuthUserByUuid', {'x_uuid': uuid}, token='', secret='')
        self.assertEqual(result['venus_status'], 1)
        self.token, self.secret = result['token_key'], result['token_secret']
        return result

    def api(self, activities):
        return self.request('/app/ActiveController', {'activityList': json.dumps(activities),
                            'appVersion': '2.6.0', 'assetVersion': '0', 'disabledVC': '1'})

    def test_campaign_list_without_scheduled_events(self):
        self.auth()
        for campaign_id in (0, 10003):
            result = self.api({'160001': {'uniqueRequestId': campaign_id, 'campaignId': campaign_id}})
            self.assertEqual(result['resData']['160001'], {'campaignInfo': []})
        self.assertEqual(self.api({'160001': {'uniqueRequestId': 44, 'campaignId': '10003'}})['venus_status'], 2)

    def test_server_errors_follow_player_language(self):
        self.auth()
        chinese = self.api({'999999': {'uniqueRequestId': 1}})
        self.assertEqual(chinese['venus_status'], 2)
        self.assertEqual(chinese['message'], '当前本地版本尚未开放此功能。')
        self.assertEqual(chinese['subject'], '本地服务')
        self.api({'020305': {'uniqueRequestId': 1, 'countryCode': 2}})
        english = self.api({'999999': {'uniqueRequestId': 1}})
        self.assertIn('Not implemented', english['message'])
        self.assertEqual(english['subject'], 'Local server')

    def test_home_contracts_and_account_boundary(self):
        first = self.auth()
        params = {api: {} for api in ('200001', '020304', '000007', '000002', '900001',
                                      '990601', '080002', '080001', '090012', '190001', '150024', '090101', '090201')}
        params.update({'080104': {'target': 'me'}, '080116': {'act': '2'},
                       '010702': {'userMonsterIds': []}, '020002': {'targetUserId': first['x_user_id']}})
        result = self.api({api: dict(p, uniqueRequestId=80) for api, p in params.items()})
        self.assertEqual(result['venus_status'], 1, result)
        home = result['resData']
        self.assertEqual(home['020002']['collection']['possessionNum'], '0')
        self.assertIsNone(home['020002']['monsterData'])
        self.assertEqual(home['080002']['resultCode'], 0)
        self.assertEqual(home['080116']['resultCode'], 0)
        self.assertIsNone(home['080116']['rewardList'])
        self.assertIsNone(home['010702']['friendshipTime'])
        self.assertEqual(home['000007']['loginBonus']['normal'], [])
        self.assertEqual(home['000002']['userNewsCountList']['prize'], '0')
        self.assertEqual(home['990601']['bannerM'], [])
        self.assertEqual(home['190001']['navigationMessageIdList'], [])
        self.assertIsNone(home['150024']['lastHistoryIds'])
        self.assertIsNone(home['150024']['lastMockBattleRequestTime'])
        for api, bad in [('020002', {'targetUserId': first['x_user_id'] + 1}),
                         ('080104', {'target': 'other'}), ('080116', {'act': '1'}),
                         ('010702', {'userMonsterIds': ['999']}),
                         ('190002', {'navigationMessageId': [1]})]:
            self.assertEqual(self.api({api: dict(bad, uniqueRequestId=81)})['venus_status'], 2)
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            state['pending_guidance'] = [1, 2]
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        self.assertEqual(self.api({'190002': {'uniqueRequestId': 82, 'navigationMessageId': [1]}})['venus_status'], 1)
        self.assertEqual(self.api({'190002': {'uniqueRequestId': 83, 'navigationMessageId': [1]}})['venus_status'], 1)
        self.assertEqual(self.api({'190001': {'uniqueRequestId': 84}})['resData']['190001']['navigationMessageIdList'], [2])
        self.auth('another-home-account')
        self.assertEqual(self.api({'020002': {'uniqueRequestId': 1, 'targetUserId': first['x_user_id']}})['venus_status'], 2)

    def test_oauth_published_reference_vector(self):
        # OAuth 1.0 photos.example.net published example; independent expected value.
        params = [('oauth_consumer_key', 'dpf43f3p2l4k3l03'), ('oauth_token', 'nnch734d00sl2jdk'),
                  ('oauth_nonce', 'kllo9940pd9333jh'), ('oauth_timestamp', '1191242096'),
                  ('oauth_signature_method', 'HMAC-SHA1'), ('oauth_version', '1.0')]
        actual = signature('GET', 'http://photos.example.net/photos?file=vacation.jpg&size=original',
                           params, 'kd94hf93k423kf44', 'pfkkdhi9sl3r4s00')
        self.assertEqual(actual, 'tR3+Ty81lMeYAr/Fid0kMTYa/WM=')

    def test_deck_edit_ownership_validation_rollback_and_relogin(self):
        self.auth()
        self.api({'010401': {'uniqueRequestId': 1}})
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            state['monsters'] = [{'userMonsterId': str(i), 'monsterId': '81',
                                  'eggFlg': '1' if i == 4 else '0'} for i in range(1, 5)]
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        edit = {'deckData': [[3, 1, 2]], 'selectDeckNum': 1, 'favoriteDeckNum': 1}
        self.assertEqual(self.api({'010402': dict(edit, uniqueRequestId=2)})['resData']['010402'], {'result': 1})
        initial = self.api({'010401': {'uniqueRequestId': 2}})['resData']['010401']
        self.assertEqual([m['userMonsterId'] for m in initial['deckList'][0]['monsterList']], ['3', '1', '2'])
        for number, party in enumerate(([1, 1, 2], [1, 2, 99], [1, 2, 4], [True, 2, 3], [1, 2]), 3):
            self.assertEqual(self.api({'010402': dict(edit, deckData=[party], uniqueRequestId=number)})['venus_status'], 2)
        self.assertEqual(self.api({'010402': dict(edit, selectDeckNum=2, uniqueRequestId=8)})['venus_status'], 2)
        self.assertEqual(self.api({'010402': dict(edit, deckData=[[1, 2, 3]], uniqueRequestId=9),
                                  '999999': {'uniqueRequestId': 9}})['venus_status'], 2)
        self.auth()
        self.assertEqual(self.api({'010401': {'uniqueRequestId': 10}})['resData']['010401'], initial)

    def test_resource_catalog_and_file_boundary(self):
        self.auth()
        response = self.api({'000005': {'uniqueRequestId': 1, 'downloadType': 0}})
        self.assertEqual(response['venus_status'], 2)
        self.assertIn('游戏资源', response['message'])
        self.assertIn('Missing AssetBundle', response['e'])
        catalog = {'assetBundleVersionList': [], 'versionManagerAsset': None}
        self.httpd.store.resource_catalog = catalog
        self.assertEqual(self.api({'000005': {'uniqueRequestId': 2}})['resData']['000005'], catalog)
        root = Path(self.temp.name) / 'assets'
        root.mkdir()
        (root / 'sample.bytes').write_bytes(b'\x00\xffasset')
        version_dir = root / 'AB_DATA/ANDROID/2.6.0'
        version_dir.mkdir(parents=True)
        (version_dir / 'Font.json').write_bytes(b'{"ver":1}')
        (root.parent / 'private.txt').write_text('private')
        self.httpd.asset_root = root
        with urlopen(self.base + '/asset/sample.bytes?1') as response:
            self.assertEqual(response.read(), b'\x00\xffasset')
        with urlopen(self.base + '/asset/AB_DATA/ANDROID/%EF%BB%BF2.6.0/Font.json?2') as response:
            self.assertEqual(json.load(response), {'ver': 1})
        for path in ('/asset/%2e%2e/private.txt', '/asset/missing.unity3d'):
            with self.assertRaises(HTTPError) as error:
                urlopen(self.base + path)
            self.assertEqual(error.exception.code, 404)

    def test_language_persistence_and_validation(self):
        self.auth()
        self.assertEqual(self.api({'020305': {'uniqueRequestId': 1, 'countryCode': 3}})['venus_status'], 1)
        self.assertEqual(self.api({'020305': {'uniqueRequestId': 2, 'countryCode': 99}})['venus_status'], 2)
        reopened = Store(self.database, consumers={'test-client': 'test-secret'})
        with reopened.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts').fetchone()[0])
        self.assertEqual(state['country_code'], 3)
        self.assertEqual(state['options']['103'], '3')

    def test_tutorial_agreement_checkpoint(self):
        self.auth()
        self.assertEqual(self.api({'020015': {'uniqueRequestId': 9, 'functionId': 1}})['resData']['020015'], {'gdprList': []})
        self.assertEqual(self.api({'910001': {'uniqueRequestId': 1, 'statusId': 10}})['venus_status'], 2)
        response = self.api({'020010': {'uniqueRequestId': 2},
                             '910001': {'uniqueRequestId': 3, 'statusId': 10}})
        self.assertEqual(response['venus_status'], 1)
        self.assertEqual(self.api({'910001': {'uniqueRequestId': 4, 'statusId': 999}})['venus_status'], 2)
        self.auth()
        tutorial = self.api({'000001': {'uniqueRequestId': 1}})['resData']['000001']['tutorialStatus']
        self.assertEqual(tutorial, {'statusId': '10', 'endFlg': '0'})
        self.assertEqual(self.api({'910001': {'uniqueRequestId': 2, 'statusId': 100}})['venus_status'], 1)
        self.assertEqual(self.api({'910001': {'uniqueRequestId': 3, 'statusId': 10}})['venus_status'], 2)
        self.assertEqual(self.api({'910001': {'uniqueRequestId': 4, 'statusId': 400}})['venus_status'], 2)
        self.assertEqual(self.api({'910001': {'uniqueRequestId': 5, 'statusId': 600}})['venus_status'], 2)
        for step in (200, 300, 400, 500, 600):
            self.assertEqual(self.api({'910001': {'uniqueRequestId': step, 'statusId': step}})['venus_status'], 1)
        self.assertEqual(self.api({'910001': {'uniqueRequestId': 601, 'statusId': 600}})['venus_status'], 1)
        self.assertEqual(self.api({'910001': {'uniqueRequestId': 700, 'statusId': 700}})['venus_status'], 2)
        self.auth()
        self.assertEqual(self.api({'000001': {'uniqueRequestId': 1}})['resData']['000001']['tutorialStatus'],
                         {'statusId': '600', 'endFlg': '0'})

    def test_capture_catalog_and_facility_stock(self):
        self.auth()
        self.assertEqual(self.api({'050001': {'uniqueRequestId': 1, 'isTutorial': 1}})['venus_status'], 2)
        self.api({'020010': {'uniqueRequestId': 2}})
        for step in (10, 100, 200, 300, 400, 500, 600):
            self.assertEqual(self.api({'910001': {'uniqueRequestId': step, 'statusId': step}})['venus_status'], 1)
        rows = self.api({'050001': {'uniqueRequestId': 2, 'isTutorial': 1, 'countryCode': 3}})['resData']['050001']['result']
        self.assertEqual([x['priceType']['category'] for x in rows], ['2', '3'])
        self.assertEqual(rows[1]['details'][0]['count'], '1')
        self.assertEqual(self.api({'140014': {'uniqueRequestId': 3, 'userId': 0}})['resData']['140014']['userFacilityList'], [])
        self.assertEqual(self.api({'140014': {'uniqueRequestId': 4, 'userId': 999}})['venus_status'], 2)
        self.assertEqual(self.api({'050001': {'uniqueRequestId': 5, 'isTutorial': 0}})['venus_status'], 2)
        self.assertEqual(self.api({'020101': {'uniqueRequestId': 6}})['resData']['020101']['userMonsterList'], [])
        self.auth('other-capture-account')
        self.assertEqual(self.api({'050001': {'uniqueRequestId': 1, 'isTutorial': 1}})['venus_status'], 2)

    def test_capture_transaction_and_durable_receipt(self):
        self.auth()
        self.api({'020010': {'uniqueRequestId': 1}})
        for step in (10, 100, 200, 300, 400, 500, 600):
            self.api({'910001': {'uniqueRequestId': step, 'statusId': step}})
        capture = {'uniqueRequestId': 1, 'gachaId': 10002, 'playCount': 1, 'itemCount': 300}
        self.assertEqual(self.api({'050002': dict(capture, playCount=10)})['venus_status'], 2)
        # Later failure rolls back both the grant and its receipt.
        self.assertEqual(self.api({'050002': capture, '999999': {'uniqueRequestId': 1}})['venus_status'], 2)
        self.assertEqual(self.api({'020101': {'uniqueRequestId': 1}})['resData']['020101']['userMonsterList'], [])
        response = self.api({'050002': capture})['resData']['050002']
        monster = response['userMonsterList'][0]
        self.assertEqual((monster['monsterId'], monster['hp'], monster['monsterEvolutionRouteId']), ('16', '53', '121'))
        self.assertEqual(self.api({'050002': capture})['resData']['050002'], response)
        self.auth()
        self.assertEqual(self.api({'050002': dict(capture, uniqueRequestId=42)})['resData']['050002'], response)
        inventory = self.api({'020101': {'uniqueRequestId': 2},
                              '180006': {'uniqueRequestId': 3, 'userMonsterId': [int(monster['userMonsterId'])]}})['resData']
        self.assertEqual(len(inventory['020101']['userMonsterList']), 1)
        self.assertEqual(len(inventory['180006']['slotInfo']), 1)
        self.assertEqual(inventory['180006']['slotInfo'][0]['manage']['maxSlotNum'], 5)
        self.assertEqual(self.api({'910001': {'uniqueRequestId': 700, 'statusId': 700}})['venus_status'], 1)
        self.assertEqual(self.api({'910001': {'uniqueRequestId': 800, 'statusId': 800}})['venus_status'], 1)
        self.assertEqual(self.api({'910001': {'uniqueRequestId': 801, 'statusId': 800}})['venus_status'], 1)
        self.auth()
        saved = self.api({'020101': {'uniqueRequestId': 1}, '020001': {'uniqueRequestId': 2}})['resData']
        evolved = saved['020101']['userMonsterList'][0]
        self.assertEqual((evolved['monsterId'], evolved['hp'], evolved['uniqueSkillId']), ('81', '350', '1'))
        self.assertEqual(evolved['userMonsterId'], monster['userMonsterId'])
        self.assertEqual(saved['020001']['playerInfo']['point'], 1)
        self.assertEqual(self.api({'910001': {'uniqueRequestId': 830, 'statusId': 830}})['venus_status'], 1)
        self.assertEqual(self.api({'910001': {'uniqueRequestId': 831, 'statusId': 830}})['venus_status'], 1)
        self.auth()
        self.assertEqual(self.api({'020001': {'uniqueRequestId': 2}})['resData']['020001']['playerInfo']['meatNum'], '1')
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts').fetchone()[0])
        fields = [r for r in state['facilities'] if r['facilityId'] == 1]
        self.assertEqual(len(fields), 1)
        self.assertEqual((fields[0]['positionX'], fields[0]['positionY']), (12, 11))
        self.assertEqual(len(state['last_harvest']), 1)
        farm = self.api({'140006': {'uniqueRequestId': 90, 'userId': 1}})['resData']['140006']
        self.assertFalse(any(r['facilityId'] == 1 for r in farm['userFacilityList']))
        self.assertEqual(farm['lastHarvestTime'], [])
        meal = {'uniqueRequestId': 860, 'statusId': 860}
        self.assertEqual(self.api({'910001': meal, '999999': {'uniqueRequestId': 1}})['venus_status'], 2)
        rolled_back = self.api({'020101': {'uniqueRequestId': 4}, '020001': {'uniqueRequestId': 5}})['resData']
        self.assertEqual(rolled_back['020101']['userMonsterList'][0]['ex'], '0')
        self.assertEqual(rolled_back['020001']['playerInfo']['meatNum'], '1')
        self.assertEqual(self.api({'910001': meal})['venus_status'], 1)
        self.assertEqual(self.api({'910001': dict(meal, uniqueRequestId=861)})['venus_status'], 1)
        self.auth()
        fed = self.api({'020101': {'uniqueRequestId': 1}, '020001': {'uniqueRequestId': 2}})['resData']
        trained = fed['020101']['userMonsterList'][0]
        self.assertEqual((trained['level'], trained['ex'], trained['levelEx'], trained['nextLevelEx']), ('2', '10', '6', '2'))
        self.assertEqual([trained[k] for k in ('hp', 'attack', 'defense', 'spAttack', 'spDefense')],
                         ['374', '570', '505', '428', '428'])
        self.assertEqual(fed['020001']['playerInfo']['meatNum'], '0')
        self.assertEqual(trained['userMonsterId'], monster['userMonsterId'])
        self.assertEqual(self.api({'910007': {'uniqueRequestId': 1}})['venus_status'], 2)
        self.assertEqual(self.api({'910001': {'uniqueRequestId': 900, 'statusId': 900}})['venus_status'], 1)
        self.assertEqual(self.api({'910007': {'uniqueRequestId': 2}, '999999': {'uniqueRequestId': 1}})['venus_status'], 2)
        self.assertEqual(len(self.api({'020101': {'uniqueRequestId': 3}})['resData']['020101']['userMonsterList']), 1)
        prepared = self.api({'910007': {'uniqueRequestId': 3}, '020101': {'uniqueRequestId': 4},
                             '010401': {'uniqueRequestId': 5}, '090009': {'uniqueRequestId': 6, 'worldId': '4'}})['resData']
        party = prepared['020101']['userMonsterList']
        self.assertEqual([r['monsterId'] for r in party], ['81', '176', '196'])
        self.assertEqual((party[0]['level'], party[0]['ex']), ('2', '10'))
        self.assertEqual([r['userMonsterId'] for r in prepared['010401']['deckList'][0]['monsterList']],
                         [r['userMonsterId'] for r in party])
        self.assertEqual(prepared['090009']['worldDungeonInfo'][0]['dungeons'][0]['status'], 3)
        start = {'uniqueRequestId': 1, 'dungeonId': '9001', 'deckNum': '1'}
        self.assertEqual(self.api({'090002': dict(start, deckNum='2')})['venus_status'], 2)
        started = self.api({'090002': start})['resData']['090002']
        self.assertTrue(0 < int(started['startId']) < 2_147_483_647)
        self.assertIsNone(started['luckDrop'])
        self.assertEqual(started['deck']['userMonsterIds'], [r['userMonsterId'] for r in party])
        self.assertEqual(len(started['dungeonFloor'][0]['enemy']), 3)
        self.auth()
        self.assertEqual(self.api({'910007': {'uniqueRequestId': 1}})['venus_status'], 1)
        self.assertEqual(self.api({'020101': {'uniqueRequestId': 2}})['resData']['020101']['userMonsterList'], party)
        self.assertEqual(self.api({'090002': start})['resData']['090002'], started)
        # Starting/retrying a battle must not award its advertised rewards.
        player = self.api({'020001': {'uniqueRequestId': 3}})['resData']['020001']['playerInfo']
        self.assertEqual((player['gamemoney'], player['point']), ('0', 1))
        self.assertEqual(self.api({'910001': {'uniqueRequestId': 1000, 'statusId': 1000}})['venus_status'], 2)
        result = {'uniqueRequestId': 1, 'startId': started['startId'], 'dungeonId': '9001',
                  'clear': 1, 'clearRound': 4, 'aliveInfo': [1, 0, 1], 'enemyAliveInfo': [[0, 0, 0]]}
        for invalid in (dict(result, startId='not-owned'), dict(result, aliveInfo=[0, 0, 0]),
                        dict(result, enemyAliveInfo=[[0, 1, 0]]), dict(result, aliveInfo=[True, 0, 1]),
                        dict(result, enemyAliveInfo=[])):
            self.assertEqual(self.api({'090003': invalid})['venus_status'], 2)
        self.assertEqual(self.api({'090003': result, '999999': {'uniqueRequestId': 1}})['venus_status'], 2)
        self.assertEqual(self.api({'020101': {'uniqueRequestId': 33}})['resData']['020101']['userMonsterList'], party)
        settled = self.api({'090003': result})['resData']['090003']
        self.assertEqual((settled['totalExp'], settled['totalMoney'], settled['clearType']), (30, 300, 1))
        self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
        self.auth()
        self.assertEqual(self.api({'090003': dict(result, uniqueRequestId=2)})['resData']['090003'], settled)
        self.assertEqual(self.api({'090003': dict(result, uniqueRequestId=3, clearRound=5)})['venus_status'], 2)
        saved = self.api({'020101': {'uniqueRequestId': 4}, '020001': {'uniqueRequestId': 5},
                          '090009': {'uniqueRequestId': 6, 'worldId': '4'}})['resData']
        self.assertEqual([r['ex'] for r in saved['020101']['userMonsterList']], ['40', '0', '30'])
        self.assertEqual(saved['020001']['playerInfo']['gamemoney'], '300')
        self.assertEqual(saved['090009']['worldDungeonInfo'][0]['dungeons'][0]['status'], 4)
        self.assertEqual(self.api({'910001': {'uniqueRequestId': 1000, 'statusId': 1000}})['venus_status'], 1)
        for step in (1100, 1200):
            self.assertEqual(self.api({'910001': {'uniqueRequestId': step, 'statusId': step}})['venus_status'], 1)
        self.assertEqual(self.api({'910001': {'uniqueRequestId': 1300, 'statusId': 1300}})['venus_status'], 2)
        rare = {'uniqueRequestId': 10001, 'gachaId': 10001, 'playCount': 1, 'itemCount': 20}
        self.assertEqual(self.api({'050002': dict(rare, itemCount=0)})['venus_status'], 2)
        self.assertEqual(self.api({'050002': rare, '999999': {'uniqueRequestId': 1}})['venus_status'], 2)
        rare_result = self.api({'050002': rare})['resData']['050002']
        self.assertEqual(rare_result['userMonsterList'][0]['monsterId'], '121')
        self.assertEqual(rare_result['userMonsterList'][0]['userMonsterId'], '4')
        self.auth()
        self.assertEqual(self.api({'050002': dict(rare, uniqueRequestId=1)})['resData']['050002'], rare_result)
        after_rare = self.api({'020101': {'uniqueRequestId': 1}, '020001': {'uniqueRequestId': 2}})['resData']
        self.assertEqual(len(after_rare['020101']['userMonsterList']), 4)
        self.assertEqual(after_rare['020001']['playerInfo']['point'], 1)
        for step in (1300, 1400):
            self.assertEqual(self.api({'910001': {'uniqueRequestId': step, 'statusId': step}})['venus_status'], 1)
        self.assertEqual(self.api({'910002': {'uniqueRequestId': 1}})['venus_status'], 2)
        finish_field = {'uniqueRequestId': 1, 'facilityId': 1, 'positionX': 12, 'positionY': 12}
        field_id = self.api({'140001': finish_field})['resData']['140001']['userFacilityId']
        self.assertEqual(field_id, fields[0]['userFacilityId'])
        self.assertEqual(self.api({'140010': {'uniqueRequestId': 1, 'userFacilityId': 999}})['venus_status'], 2)
        self.assertEqual(self.api({'140010': {'uniqueRequestId': 2, 'userFacilityId': field_id},
                                  '910002': {'uniqueRequestId': 2}})['venus_status'], 1)
        self.auth()
        self.assertEqual(self.api({'910002': {'uniqueRequestId': 1}})['venus_status'], 1)
        completed_farm = self.api({'140006': {'uniqueRequestId': 1, 'userId': 1}})['resData']['140006']
        self.assertEqual(len([r for r in completed_farm['userFacilityList'] if r['facilityId'] == 1]), 1)
        self.assertEqual(completed_farm['lastHarvestTime'][0]['userFacilityId'], field_id)
        profile = self.api({'020002': {'uniqueRequestId': 90, 'targetUserId': 1}})['resData']['020002']
        self.assertEqual(profile['monsterData']['userMonsterId'], '1')
        self.assertNotIn('createTimeSec', profile['monsterData'])
        self.assertEqual(profile['collection']['possessionNum'], '5')
        self.assertEqual(profile['playHistory']['dungeonClearCount'], '1')
        self.assertEqual(profile['playHistory']['useMeatNum'], '1')
        self.assertEqual(profile['playHistory']['evolutionCount'], '1')
        self.assertIsNone(self.api({'090012': {'uniqueRequestId': 90}})['resData']['090012']['startId'])
        world = self.api({'090009': {'uniqueRequestId': 91, 'worldId': '1'}})['resData']['090009']
        first_stage = next(s for s in world['worldDungeonInfo'] if s['worldStageId'] == 1)
        self.assertEqual([d['status'] for d in first_stage['dungeons']], [2, 1, 1])
        normal = {'uniqueRequestId': 91, 'dungeonId': '1', 'deckNum': '1'}
        self.assertEqual(self.api({'090002': dict(normal, dungeonId='2')})['venus_status'], 2)
        self.assertEqual(self.api({'090002': normal, '999999': {'uniqueRequestId': 91}})['venus_status'], 2)
        start = self.api({'090002': normal})['resData']['090002']
        self.assertEqual(len(start['dungeonFloor']), 3)
        self.assertTrue(all(f['enemy'][0]['drop'] == [{'dropBoxType': 1}] for f in start['dungeonFloor']))
        self.assertEqual(self.api({'020001': {'uniqueRequestId': 91}})['resData']['020001']['playerInfo']['stamina'], 18)
        self.auth()
        self.assertEqual(self.api({'090002': normal})['resData']['090002'], start)
        self.assertEqual(self.api({'090012': {'uniqueRequestId': 92}})['resData']['090012']['startId'], start['startId'])
        result_normal = {'uniqueRequestId': 91, 'startId': start['startId'], 'dungeonId': '1',
                         'clear': 1, 'clearRound': 9, 'aliveInfo': [1, 1, 1], 'enemyAliveInfo': [[0]] * 3}
        self.assertEqual(self.api({'090003': result_normal, '999999': {'uniqueRequestId': 91}})['venus_status'], 2)
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            self.assertEqual(state['souls'], [])
            self.assertFalse(state['active_battle']['settled'])
        # An in-flight battle retains its drops even if the configuration changes.
        material = self.httpd.store.normal_world['1']['materialPerWave']
        original_amount = material['assetNum']
        material['assetNum'] = 99
        settled_normal = self.api({'090003': result_normal})['resData']['090003']
        material['assetNum'] = original_amount
        self.assertEqual(settled_normal['totalExp'], 30)
        self.assertEqual(settled_normal['totalMoney'], 90)
        self.assertEqual(settled_normal['dungeonReward'][0]['assetNum'], 2)
        self.assertEqual(settled_normal['dropReward'], [
            {'assetCategoryId': '14', 'assetValue': 2010001, 'assetNum': 1, 'dropBoxType': 1}] * 3 + [
            {'assetCategoryId': '3', 'assetValue': 0, 'assetNum': 20, 'dropBoxType': 0}])
        next_world = self.api({'090009': {'uniqueRequestId': 92, 'worldId': '1'}})['resData']['090009']
        first_stage = next(s for s in next_world['worldDungeonInfo'] if s['worldStageId'] == 1)
        self.assertEqual([d['status'] for d in first_stage['dungeons']], [4, 2, 1])
        repeat = self.api({'090002': dict(normal, uniqueRequestId=92)})['resData']['090002']
        self.assertNotEqual(repeat['startId'], start['startId'])
        self.auth()
        self.assertEqual(self.api({'090003': result_normal})['resData']['090003'], settled_normal)
        self.assertEqual(self.api({'020303': {'uniqueRequestId': 92}})['resData']['020303']['userSoulData'],
                         [{'soulId': '2010001', 'num': '3'}])
        resources = self.api({'020001': {'uniqueRequestId': 93}})['resData']['020001']['playerInfo']
        self.assertEqual(resources['stamina'], 16)
        self.assertEqual(resources['gamemoney'], '390')
        self.assertEqual(resources['point'], 3)
        defeated = self.api({'090003': dict(result_normal, uniqueRequestId=94, startId=repeat['startId'],
            clear=0, aliveInfo=[0, 0, 0], enemyAliveInfo=[[0], [1], [1]])})['resData']['090003']
        self.assertEqual(defeated['dropReward'], [])
        self.assertEqual(self.api({'020303': {'uniqueRequestId': 94}})['resData']['020303']['userSoulData'],
                         [{'soulId': '2010001', 'num': '3'}])
        second = self.api({'090002': dict(normal, uniqueRequestId=95, dungeonId='2')})['resData']['090002']
        self.assertEqual(self.api({'090003': dict(result_normal, uniqueRequestId=95, dungeonId='2',
            startId=second['startId'], enemyAliveInfo=[[0, 0]] * 3)})['venus_status'], 1)
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            state['player']['unitLimitMax'] = '4'
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        self.assertEqual(self.api({'090002': dict(normal, uniqueRequestId=96, dungeonId='3')})['venus_status'], 2)
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            self.assertEqual(state['player']['stamina'], 14)
            state['player']['unitLimitMax'] = '50'
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        third = self.api({'090002': dict(normal, uniqueRequestId=96, dungeonId='3')})['resData']['090002']
        third_result = dict(result_normal, uniqueRequestId=96, dungeonId='3', startId=third['startId'], enemyAliveInfo=[[0, 0, 0]] * 3)
        self.assertEqual(self.api({'090003': third_result, '999999': {'uniqueRequestId': 96}})['venus_status'], 2)
        before = self.api({'020101': {'uniqueRequestId': 96}, '140011': {'uniqueRequestId': 96}})['resData']
        self.assertEqual(len(before['020101']['userMonsterList']), 4)
        self.assertEqual(before['140011']['facilityCondition'][0]['flg'], 0)
        third_reward = self.api({'090003': third_result})['resData']['090003']
        self.assertEqual({r['assetCategoryId'] for r in third_reward['dungeonReward']}, {'1', '2', '16'})
        self.auth()
        self.assertEqual(self.api({'090003': third_result})['resData']['090003'], third_reward)
        after = self.api({'020101': {'uniqueRequestId': 97}, '140011': {'uniqueRequestId': 97},
                          '180006': {'uniqueRequestId': 97, 'userMonsterId': [5]}})['resData']
        self.assertEqual(len(after['020101']['userMonsterList']), 5)
        gift = after['020101']['userMonsterList'][-1]
        self.assertEqual((gift['userMonsterId'], gift['monsterId'], gift['level'], gift['ex'], gift['luck']), ('5', '756', '40', '4536', '1'))
        self.assertNotIn('monsterFixedValueId', gift)
        self.assertEqual(gift['commonSkillId'], '473')
        self.assertTrue(any(r['skillId'] == gift['commonSkillId'] for r in self.httpd.store.masters['990108']['skillM']))
        self.assertEqual(after['140011']['facilityCondition'][0]['flg'], 1)
        self.assertEqual(after['180006']['slotInfo'][0]['manage']['maxSlotNum'], 5)
        world = self.api({'090009': {'uniqueRequestId': 97, 'worldId': '1'}})['resData']['090009']
        self.assertGreater(next(s for s in world['worldDungeonInfo'] if s['worldStageId'] == 2)['dungeons'][0]['status'], 1)
        self.assertEqual(self.api({'050002': capture})['venus_status'], 2)
        self.auth('other-capture-owner')
        self.assertEqual(self.api({'020101': {'uniqueRequestId': 1}})['resData']['020101']['userMonsterList'], [])
        self.assertEqual(self.api({'050002': capture})['venus_status'], 2)

    def test_initial_player_batch_and_saved_inventory(self):
        self.auth()
        batch = {api: {'uniqueRequestId': i} for i, api in enumerate(
                 ['020001', '020101', '010401', '180001', '180006'], 1)}
        result = self.api(batch)
        self.assertEqual(result['venus_status'], 1)
        self.assertEqual(result['resData']['020101']['userMonsterList'], [])
        self.assertEqual(result['resData']['180001']['count'], 0)
        self.assertEqual(result['resData']['010401']['selectDeckNum'], '1')
        self.assertIsNone(result['resData']['020001']['playerInfo']['titleId'])
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts').fetchone()[0])
            state['player']['gamemoney'] = '123'
            state['player']['titleId'] = '0'  # Legacy reconstruction default.
            state['chips'] = [{'chipId': 1, 'userChipId': 45, 'userMonsterId': 0}]
            db.execute('UPDATE accounts SET state=?', (json.dumps(state),))
        self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
        self.assertEqual(self.api({'020001': {'uniqueRequestId': 10}})['resData']['020001']['playerInfo']['gamemoney'], '123')
        self.assertIsNone(self.api({'020001': {'uniqueRequestId': 13}})['resData']['020001']['playerInfo']['titleId'])
        self.assertEqual(self.api({'180001': {'uniqueRequestId': 11, 'userChipId': [45]}})['resData']['180001']['count'], 1)
        self.assertEqual(self.api({'180001': {'uniqueRequestId': 12, 'userChipId': [46]}})['resData']['180001']['count'], 0)
        self.auth('second-empty-player')
        self.assertEqual(self.api({'180001': {'uniqueRequestId': 1}})['resData']['180001']['count'], 0)

    def test_chinese_texts_and_cache_versions(self):
        self.auth()
        response = self.api({'990802': {'uniqueRequestId': 1, 'countryCode': 3},
                             '999101': {'uniqueRequestId': 2, 'countryCode': 3},
                             '000004': {'uniqueRequestId': 3}})['resData']
        labels = {r['messageCode']: r['messageText'] for r in response['990802']['messageStringM']}
        source = self.httpd.store.chinese_labels['messageStringM']
        import localization
        overlays = set(self.httpd.store.chinese_ui_overrides)
        for filename in ('ui-completion.json', 'ui-repairs.json'):
            overlays.update(json.loads((localization.LOCALE_ROOT / filename).read_text(encoding='utf-8')))
        originals = {r['messageCode']: r['messageText'] for r in self.httpd.store.masters['990802']['messageStringM']}
        dictionary = json.loads((localization.LOCALE_ROOT / 'ui-dictionary.json').read_text(encoding='utf-8'))
        overlays.update(key for key, text in originals.items() if text in dictionary)
        for row in source:
            if (localization.compatible(originals.get(row['messageCode'], ''), row['messageText'])
                    and row['messageCode'] not in overlays):
                simplified = str.maketrans(json.loads((localization.LOCALE_ROOT / 'simplified-characters.json').read_text(encoding='utf-8')))
                self.assertEqual(labels[row['messageCode']], row['messageText'].translate(simplified))
        self.assertEqual(labels['StaminaFull'], '体力已满')
        tutorial = {r['tutorialNaviId']: r['message'] for r in response['999101']['tutorialNaviM']}
        self.assertEqual(tutorial['26'], '呼……总算成功备份了农场……')
        self.assertIn('$USER_INPUT_NAME$', tutorial['24'])
        original = self.api({'999101': {'uniqueRequestId': 4, 'countryCode': 1}})['resData']['999101']
        self.assertEqual(original, self.httpd.store.masters['999101'])
        versions = {r['tableName']: r['version'] for r in response['000004']['dataVersionList']}
        self.assertGreater(int(versions['tutorial_navi_m']), 100000)

    def test_tutorial_farm_reads_persist_and_isolate(self):
        first = self.auth()
        uid = first['x_user_id']
        result = self.api({'020303': {'uniqueRequestId': 1},
                           '140006': {'uniqueRequestId': 2, 'userId': uid}})
        self.assertEqual(result['venus_status'], 1)
        self.assertEqual(result['resData']['020303']['userSoulData'], [])
        self.assertEqual(result['resData']['140006']['userFacilityList'], [])
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=?', (uid,)).fetchone()[0])
            state['souls'] = [{'soulId': '1', 'num': '7'}]
            state['facilities'] = [{'userFacilityId': 1, 'facilityId': 1}]
            db.execute('UPDATE accounts SET state=? WHERE id=?', (json.dumps(state), uid))
        self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
        result = self.api({'020303': {'uniqueRequestId': 3},
                           '140006': {'uniqueRequestId': 4, 'userId': uid}})
        self.assertEqual(result['resData']['020303']['userSoulData'][0]['num'], '7')
        self.assertEqual(len(result['resData']['140006']['userFacilityList']), 1)
        second = self.auth('farm-second-account')
        self.assertEqual(self.api({'140006': {'uniqueRequestId': 1, 'userId': uid}})['venus_status'], 2)
        result = self.api({'140006': {'uniqueRequestId': 2, 'userId': second['x_user_id']}})
        self.assertEqual(result['resData']['140006']['userFacilityList'], [])

    def test_initial_farm_buildings_preserve_existing(self):
        uid = self.auth()['x_user_id']
        self.api({'020010': {'uniqueRequestId': 1}})
        for step in (10, 100, 200, 300, 400, 500):
            self.api({'910001': {'uniqueRequestId': step, 'statusId': step}})
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=?', (uid,)).fetchone()[0])
            lab = {'userFacilityId': 8, 'facilityId': 5, 'level': 2,
                   'positionX': 14, 'positionY': 16, 'completeTime': ''}
            state['facilities'] = [lab]
            db.execute('UPDATE accounts SET state=? WHERE id=?', (json.dumps(state), uid))
        response = self.api({'140006': {'uniqueRequestId': 1, 'userId': uid}})['resData']['140006']
        rows = response['userFacilityList']
        self.assertEqual({r['facilityId'] for r in rows}, {5, 7, 21})
        self.assertEqual(rows[0], lab)
        self.assertEqual(len({r['userFacilityId'] for r in rows}), 3)
        self.auth()
        self.assertEqual(self.api({'140006': {'uniqueRequestId': 1, 'userId': uid}})['resData']['140006']['userFacilityList'], rows)

    def test_account_persistence_and_retry(self):
        first = self.auth()
        login = {'000001': {'uniqueRequestId': 1, 'requestStatus': 0, 'osType': '2'}}
        response = self.api(login)
        # GUIScreenTitle.TitleStete.defaultState == 1; zero ignores start clicks.
        self.assertEqual(response['resData']['000001']['state'], 1)
        self.assertEqual(response['resData']['000001']['playerInfo']['userId'], str(first['x_user_id']))
        login['000001']['requestStatus'] = 1
        self.assertEqual(self.api(login), response)
        self.assertEqual(self.api({'020301': {'uniqueRequestId': 2, 'optionList': {'bgm': 0}}})['venus_status'], 1)
        self.assertEqual(self.api({'020003': {'uniqueRequestId': 3, 'nickname': '本地测试员'}})['venus_status'], 1)
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts').fetchone()[0])
        self.assertEqual(state['login_count'], 1)
        # Re-open from the persisted file; do not reuse an in-memory state object.
        self.httpd.store = Store(self.database, consumers={'test-client': 'test-secret'})
        second = self.auth()
        self.assertEqual(second['x_user_id'], first['x_user_id'])
        self.assertNotEqual(second['token_key'], first['token_key'])
        resumed = self.api({'000001': {'uniqueRequestId': 1}})['resData']['000001']
        self.assertEqual(resumed['optionList'], dict(DEFAULT_OPTIONS, bgm='0'))
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts').fetchone()[0])
        self.assertEqual(state['nickname'], '本地测试员')
        self.assertEqual(state['login_count'], 2)

    def test_atomic_batch_rollback_and_id_conflict(self):
        self.auth()
        result = self.api({'020003': {'uniqueRequestId': 10, 'nickname': 'Must roll back'},
                           '999999': {'uniqueRequestId': 11}})
        self.assertEqual(result['venus_status'], 2)
        self.assertIn('尚未开放', result['message'])
        self.assertIn('Not implemented', result['e'])
        with self.httpd.store.connect() as db:
            self.assertEqual(json.loads(db.execute('SELECT state FROM accounts').fetchone()[0])['nickname'], 'Tamer')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM retries').fetchone()[0], 0)
        self.assertEqual(self.api({'020003': {'uniqueRequestId': 10, 'nickname': 'Saved'}})['venus_status'], 1)
        self.assertEqual(self.api({'020003': {'uniqueRequestId': 10, 'nickname': 'Changed'}})['venus_status'], 2)

    def test_all_archived_masters_and_invalid_auth(self):
        self.auth()
        batch = {api: {'uniqueRequestId': i} for i, api in enumerate(self.httpd.store.masters)}
        response = self.api(batch)
        self.assertEqual(response['venus_status'], 1)
        self.assertEqual(len(response['resData']), 72)
        self.assertIn('monsterM', response['resData']['990102'])
        self.secret = 'wrong-token-secret'
        self.assertEqual(self.api({'000001': {'uniqueRequestId': 99}})['venus_status'], 2)

    def test_account_isolation(self):
        first = self.auth('first-device')
        self.api({'020301': {'uniqueRequestId': 1, 'optionList': {'bgm': 0}}})
        second = self.auth('second-device')
        self.assertNotEqual(first['x_user_id'], second['x_user_id'])
        result = self.api({'000001': {'uniqueRequestId': 1}})
        self.assertEqual(result['resData']['000001']['optionList'], DEFAULT_OPTIONS)

    def test_error_shape_and_inquiry_persistence(self):
        self.auth()
        first = self.api({'000601': {'uniqueRequestId': 1}})['resData']['000601']['inquiryCode']
        self.auth()
        second = self.api({'000601': {'uniqueRequestId': 1}})['resData']['000601']['inquiryCode']
        self.assertEqual(first, second)
        error = self.api({'000005': {'uniqueRequestId': 2}})
        self.assertEqual(error['venus_status'], 2)
        # The real client rejects fields absent from WebAPI.ResponseDataErr.
        self.assertNotIn('resData', error)

    def test_malformed_battle_ids_return_error_and_rollback(self):
        self.auth()
        self.api({'020001': {'uniqueRequestId': 1}})
        with self.httpd.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
            state['active_battle'] = {'response': {'startId': '123'}, 'settled': False}
            db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
        for bad in ([], {}, 123, None):
            result = self.api({'020003': {'uniqueRequestId': 2, 'nickname': 'Must rollback'},
                               '090003': {'uniqueRequestId': 2, 'startId': bad, 'dungeonId': '1'}})
            self.assertEqual(result['venus_status'], 2)
        with self.httpd.store.connect() as db:
            state_after = json.loads(db.execute('SELECT state FROM accounts WHERE id=1').fetchone()[0])
        self.assertEqual(state_after, state)
        self.assertEqual(self.api({'020001': {'uniqueRequestId': 3}})['venus_status'], 1)

    def test_chinese_master_texts_and_language_isolation(self):
        self.auth()
        requests = {api: {'uniqueRequestId': 1, 'countryCode': 3}
                    for api in ('990003', '990103', '990108', '990301', '990905', '990801')}
        response = self.api(requests)['resData']
        self.assertEqual(response['990003']['assetCategoryM'][0]['assetTitle'], '数码兽')
        self.assertTrue(any(r['monsterName'] == '亚古兽' for r in response['990103']['monsterM']))
        self.assertEqual(response['990905']['worldDungeonM'][0]['name'], '开始探索吧')
        for api in requests:
            original = self.api({api: {'uniqueRequestId': 2, 'countryCode': 1}})['resData'][api]
            self.assertEqual(original, self.httpd.store.masters[api])


if __name__ == '__main__':
    unittest.main(verbosity=2)
