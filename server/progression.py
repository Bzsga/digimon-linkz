"""Local mission -> gift -> inventory flow; original thresholds not recovered."""
import copy
import json
import time
from datetime import datetime, timedelta, timezone


class Progression:
    GROUPS = {'daily': '1', 'total': '2', 'beginner': '3', 'midrange': '4'}

    def __init__(self, store, path, error):
        self.store, self.error = store, error
        self.config = json.loads(path.read_text(encoding='utf-8'))
        self.missions = {r['id']: r for r in self.config['missions']}
        if len(self.missions) != len(self.config['missions']):
            raise ValueError('Duplicate local mission IDs')
        for row in self.missions.values():
            if (type(row['id']) is not int or not 0 < row['id'] <= 2147483647 or
                    row['group'] not in self.GROUPS or type(row['target']) is not int or row['target'] <= 0 or
                    row['category'] not in (1, 2, 3, 4, 5, 6, 7, 8) or not row['rewards']):
                raise ValueError('Invalid local mission definition')
            for reward in row['rewards']:
                self.asset_spec(reward)
        if not 1 <= self.config['giftPageSize'] <= 100 or not 1 <= self.config['giftExpiryDays'] <= 365:
            raise ValueError('Invalid local gift pagination or expiry')

    def day_key(self):
        return datetime.fromtimestamp(time.time(), timezone.utc).astimezone(
            timezone(timedelta(hours=self.config['dailyUtcOffsetHours']))).strftime('%Y-%m-%d')

    def refresh_day(self, state):
        day = self.day_key()
        if state.get('mission_daily', {}).get('day') != day:
            counters = {r['counter'] for r in self.missions.values() if r['group'] == 'daily'}
            state['mission_daily'] = {'day': day, 'baseline': {k: state.get(k, 0) for k in counters}}

    def claim_key(self, row, state):
        return str(row['id']) + ('@' + state['mission_daily']['day'] if row['group'] == 'daily' else '')

    def mission_rows(self, state):
        # activities() snapshots the day once per atomic request. Re-reading
        # the clock here could discard progress when a batch crosses midnight.
        if 'mission_daily' not in state:
            self.refresh_day(state)
        result = {group: [] for group in self.GROUPS}
        if state['tutorial']['endFlg'] != '1':
            return result
        for row in self.missions.values():
            ident = str(row['id'])
            progress = int(state.get(row['counter'], 0))
            if row['group'] == 'daily':
                progress -= state['mission_daily']['baseline'].get(row['counter'], 0)
            progress = min(row['target'], max(0, progress))
            claimed = self.claim_key(row, state) in state.get('mission_claims', {})
            rewards = [dict(r, viewFlg='1', missionRewardListId=ident,
                            missionRewardId=ident + str(i)) for i, r in enumerate(row['rewards'])]
            result[row['group']].append({'missionId': ident, 'missionType': self.GROUPS[row['group']],
                'missionCategoryId': str(row['category']), 'lastStepFlg': '0', 'missionName': row['name'],
                'status': 2 if claimed else 1 if progress >= row['target'] else 0, 'nowValue': progress,
                'detail': {'missionDetailId': ident, 'missionId': ident, 'missionDetail': row['detail'],
                    'missionRank': '1', 'missionValue': str(row['target']), 'missionRewardListId': ident},
                'reward': rewards, 'informationTime': '2020-01-01 00:00:00', 'displayGroup': self.GROUPS[row['group']]})
        return result

    def claim_missions(self, state, param):
        ids = param.get('missionId')
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 100 or
                any(type(i) is not int for i in ids) or len(set(ids)) != len(ids)):
            raise self.error('Invalid mission reward list')
        rows = {int(r['missionId']): r for group in self.mission_rows(state).values() for r in group}
        if any(i not in rows or rows[i]['status'] == 0 for i in ids):
            raise self.error('Mission is missing or not complete')
        rewards = []
        for ident in ids:
            row = self.missions[ident]
            key = self.claim_key(row, state)
            if key not in state.get('mission_claims', {}):
                gifts = [self.add_gift(state, r, row['name'], str(ident)) for r in row['rewards']]
                state.setdefault('mission_claims', {})[key] = gifts
            rewards.extend({**r, 'viewFlg': '1'} for r in row['rewards'])
        return {'result': rewards}

    def add_gift(self, state, reward, message, source):
        self.asset_spec(reward)
        ident = max([state.get('prize_id_sequence', 0)] +
                    [int(r['receiveId']) for r in state.get('prizes', [])] +
                    [int(r['userPrizeReceiveId']) for r in state.get('prize_history', [])]) + 1
        if ident > 2147483647:
            raise self.error('Prize identity limit reached')
        now = datetime.fromtimestamp(time.time(), timezone.utc)
        state['prize_id_sequence'] = ident
        state.setdefault('prizes', []).append(dict(reward, receiveId=str(ident), fromType='2', fromId=source,
            message=message, receiveLimitTime=(now + timedelta(days=self.config['giftExpiryDays'])).strftime('%Y-%m-%d %H:%M:%S'),
            receiveStatus='0', updateTime=now.strftime('%Y-%m-%d %H:%M:%S')))
        return str(ident)

    def asset_spec(self, reward):
        category, value, amount = (reward.get(k) for k in ('assetCategoryId', 'assetValue', 'assetNum'))
        if (any(not isinstance(v, str) or not v.isdecimal() for v in (category, value, amount)) or
                not 0 < int(amount) <= 2147483647):
            raise self.error('Invalid prize asset')
        value, amount = int(value), int(amount)
        code = self.store.masters['990701']['codeM']
        if category in ('2', '3', '4', '13'):
            if value != 0:
                raise self.error('Invalid prize asset')
            field, maximum = {'2': ('point', int(code['maxDigistoneCount'])),
                '3': ('friendPoint', int(code['maxLinkPointCount'])), '4': ('gamemoney', int(code['maxClusterCount'])),
                '13': ('meatNum', None)}[category]
            return category, value, amount, field, maximum
        if category == '14' and any(r['soulId'] == str(value) for r in self.store.masters['990201']['soulM']):
            return category, value, amount, None, 2147483647
        if category == '16' and any(r['facilityKeyId'] == str(value) for r in self.store.masters['990312']['facilityKeyM']):
            return category, value, amount, None, 2147483647
        if category == '6' and value in (6, 7):
            return category, value, amount, None, 2147483647
        if category == '17' and value in self.store.chip_inventory.bonus:
            return category, value, amount, None, None
        if category == '1':
            try:
                extra = json.loads(reward.get('exValue', '{}'))
            except (ValueError, TypeError):
                raise self.error('Invalid prize asset')
            if not isinstance(extra, dict):
                raise self.error('Invalid prize asset')
            fixed = next((r for r in self.store.masters['990116']['monsterFixedValueM']
                          if r['monsterFixedValueId'] == str(extra.get('monsterFixedValueId'))), None)
            master = next((r for r in self.store.masters['990102']['monsterM'] if r['monsterId'] == str(value)), None)
            if (fixed and master and fixed['monsterId'] == str(value) and
                    1 <= int(fixed['level']) <= int(master['maxLevel'])):
                return category, value, amount, None, None
        raise self.error('This prize asset type is not implemented')

    def has_room(self, state, reward):
        category, value, amount, field, maximum = self.asset_spec(reward)
        if field:
            maximum = int(state['player']['meatLimitMax']) if maximum is None else maximum
            return int(state['player'][field]) + amount <= maximum
        if category == '14':
            owned = next((r for r in state['souls'] if r['soulId'] == str(value)), {'num': '0'})
            return int(owned['num']) + amount <= maximum
        if category == '16':
            return int(state.get('facility_keys', {}).get(str(value), 0)) + amount <= maximum
        if category == '6':
            return self.store.chip_inventory.item_count(state, value) + amount <= maximum
        if category == '17':
            sequence = max([state.get('chip_id_sequence', 0)] + [int(c['userChipId']) for c in state['chips']])
            return (len(state['chips']) + amount <= int(state['player']['chipLimitMax']) and
                    sequence + amount <= 2147483647)
        return len(state['monsters']) + amount <= int(state['player']['unitLimitMax'])

    @staticmethod
    def expired(gift):
        deadline = datetime.strptime(gift['receiveLimitTime'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        return deadline.timestamp() <= time.time()

    def list_gifts(self, state, param):
        page = param.get('page')
        if type(page) is not int or not 1 <= page <= 2147483647:
            raise self.error('Invalid prize page')
        pending = [r for r in state.get('prizes', []) if not self.expired(r)]
        size = self.config['giftPageSize']
        rows = []
        for gift in pending[(page - 1) * size:page * size]:
            rows.append({k: v for k, v in gift.items() if k not in ('fromId', 'exValue')})
            rows[-1]['isAssetNumLimitOver'] = int(not self.has_room(state, gift))
        return {'prizeData': rows, 'prizeTotalCount': str(len(pending))}

    def receive_gifts(self, account, state, param):
        empty = {'prizeReceiveIds': [], 'isWarning': 0, 'warningData': [], 'resultCode': 1}
        if type(param.get('receiveType')) is not int or param['receiveType'] != 2:
            return dict(empty, resultCode=92)
        ids = param.get('receiveIds')
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 100 or
                any(not isinstance(i, str) or not i.isdecimal() for i in ids) or len(set(ids)) != len(ids)):
            raise self.error('Invalid prize receive list')
        pending = {r['receiveId']: r for r in state.get('prizes', [])}
        received = {r['userPrizeReceiveId'] for r in state.get('prize_history', [])}
        if any(i not in pending and i not in received for i in ids):
            return dict(empty, resultCode=91)
        if any(self.expired(pending[i]) for i in ids if i in pending):
            return dict(empty, resultCode=90)
        if state.get('active_battle') and not state['active_battle']['settled']:
            raise self.error('Finish the active battle before receiving gifts')
        result = copy.deepcopy(empty)
        for ident in ids:
            if ident in received:
                result['prizeReceiveIds'].append(ident)
                continue
            gift = pending[ident]
            if not self.has_room(state, gift):
                result['warningData'].append(ident)
                continue
            category, value, amount, field, _ = self.asset_spec(gift)
            if field:
                current = state['player'][field]
                total = int(current) + amount
                state['player'][field] = total if isinstance(current, int) else str(total)
            elif category == '14':
                owned = next((r for r in state['souls'] if r['soulId'] == str(value)), None)
                if owned is None:
                    owned = {'soulId': str(value), 'num': '0'}
                    state['souls'].append(owned)
                owned['num'] = str(int(owned['num']) + amount)
            elif category == '16':
                keys = state.setdefault('facility_keys', {})
                keys[str(value)] = int(keys.get(str(value), 0)) + amount
            elif category == '6':
                self.store.chip_inventory.add_item(state, value, amount)
            elif category == '17':
                self.store.chip_inventory.grant(state, value, amount)
            else:
                for _ in range(amount):
                    self.store.grant_fixed_monster(account, state, gift)
                    self.store.add_monster_experience(state['monsters'][-1], 0)
            state['prizes'].remove(gift)
            state.setdefault('prize_history', []).append(dict(gift, userPrizeReceiveId=ident,
                userId=str(account['id']), receiveStatus='1', exValue=gift.get('exValue', ''),
                updateTime=datetime.fromtimestamp(time.time(), timezone.utc).strftime('%Y-%m-%d %H:%M:%S')))
            result['prizeReceiveIds'].append(ident)
        result['isWarning'] = int(bool(result['warningData']))
        return result

    def history(self, state):
        return {'prizeReceiveHistory': [{k: v for k, v in r.items() if k != 'receiveId'}
                                       for r in reversed(state.get('prize_history', []))]}
