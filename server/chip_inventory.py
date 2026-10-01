"""Native basic-chip contracts. Stored monster stats exclude chip additions.

Only the six original unconditional stat families are enabled; special/event
chips need their own server rules and acquisition evidence before use.
"""
import copy
import json
import secrets


class ChipInventory:
    STAT_FIELDS = {'21': 'attack', '22': 'defense', '23': 'hp', '24': 'speed',
                   '25': 'spAttack', '26': 'spDefense'}

    def __init__(self, store, error):
        self.store, self.error = store, error
        self.master = {int(r['chipId']): r for r in store.masters['991301']['chipM']}
        self.bonus = {}
        effects = store.masters['991302']['chipEffectM']
        for ident, row in self.master.items():
            if row['chipGroupId'] not in ('1', '2', '3', '4', '5', '6'):
                continue
            rows = [e for e in effects if e['chipId'] == str(ident)]
            if len(rows) != 1:
                raise ValueError('Basic chip must have exactly one original effect')
            effect = rows[0]
            if (any(effect[k] != '0' for k in ('targetType', 'targetSubType', 'targetValue',
                    'targetValue2', 'effectTrigger', 'effectTrigger2')) or
                    effect['effectSubType'] != '3' or effect['lot'] != '10000' or
                    effect['effectType'] not in self.STAT_FIELDS):
                raise ValueError('Unsupported basic chip effect')
            self.bonus[ident] = (self.STAT_FIELDS[effect['effectType']], int(effect['effectValue']))
        config = store.local_gameplay['chips']
        self.pool = config['gachaChipIds']
        if (type(config['gachaId']) is not int or type(config['linkCostPerChip']) is not int or
                config['linkCostPerChip'] <= 0 or not isinstance(self.pool, list) or
                len(self.pool) != 6 or len(set(self.pool)) != 6 or any(
                    type(i) is not int or i not in self.bonus or self.master[i]['rank'] != '1'
                    for i in self.pool)):
            raise ValueError('Invalid local basic chip pool')
        self.advanced = config.get('advancedGacha')
        if self.advanced is not None:
            advanced = self.advanced
            if not isinstance(advanced, dict):
                raise ValueError('Invalid local advanced chip pool')
            weights, pools = advanced.get('rankWeightsBasisPoints'), advanced.get('chipIdsByRank')
            if (not self.positive(advanced.get('gachaId')) or advanced['gachaId'] in
                    (10001, 10002, config['gachaId']) or not self.positive(advanced.get('linkCostPerChip')) or
                    not self.positive(advanced.get('unlockDungeonId')) or
                    str(advanced['unlockDungeonId']) not in store.normal_world or
                    not self.positive(advanced.get('dispNum')) or not isinstance(weights, dict) or
                    not isinstance(pools, dict) or set(weights) != set(map(str, range(2, 9))) or
                    set(pools) != set(weights) or any(type(w) is not int or w <= 0 for w in weights.values()) or
                    sum(weights.values()) != 10000):
                raise ValueError('Invalid local advanced chip pool')
            for rank, ids in pools.items():
                if (not isinstance(ids, list) or len(ids) != 6 or
                        any(type(i) is not int or i not in self.bonus or self.master[i]['rank'] != rank for i in ids) or
                        len(set(ids)) != 6 or
                        {self.master[i]['chipGroupId'] for i in ids} != set(map(str, range(1, 7)))):
                    raise ValueError('Invalid local advanced chip rank pool')

    @staticmethod
    def positive(value):
        return type(value) is int and 0 < value <= 2147483647

    def ids(self, values):
        if (not isinstance(values, list) or not 1 <= len(values) <= 300 or
                any(not self.positive(i) for i in values) or len(set(values)) != len(values)):
            raise self.error('Invalid chip list')
        return values

    def grant(self, state, ident, amount):
        if ident not in self.bonus or not self.positive(amount):
            raise self.error('Unsupported chip type')
        if len(state['chips']) + amount > int(state['player']['chipLimitMax']):
            raise self.error('Chip inventory is full')
        sequence = max([state.get('chip_id_sequence', 0)] +
                       [int(c['userChipId']) for c in state['chips']])
        if sequence + amount > 2147483647:
            raise self.error('Chip identity limit reached')
        result = [{'chipId': ident, 'userChipId': sequence + i + 1, 'userMonsterId': 0}
                  for i in range(amount)]
        state['chip_id_sequence'] = sequence + amount
        state['chips'].extend(result)
        state['obtained_chip_ids'] = sorted(set(state.get('obtained_chip_ids', [])) | {ident})
        return result

    def refresh_capacity(self, state):
        factory = next((r for r in state['facilities'] if r['facilityId'] == 25 and r['level'] > 0), None)
        if factory:
            row = next(r for r in self.store.masters['990311']['facilityChipM']
                       if r['level'] == str(factory['level']))
            state['player']['chipLimitMax'] = int(row['maxChipNum'])

    def free_slots(self, monster):
        step = self.store.monster_growth_step(monster)
        master = next(r for r in self.store.masters['990102']['monsterM'] if r['monsterId'] == monster['monsterId'])
        arousal = int(master['rare']) - 1
        return (step in ('5', '6', '7', '8', '9', '11', '12', '13'),
                step in ('6', '7', '9', '12', '13'), step in ('7', '9', '13'),
                arousal >= 2, arousal >= 4)

    def refresh_slots(self, state):
        # Replaces prior placeholder counts, preserving equipped chips and all
        # paid extra slots. Free awakening slots can be non-contiguous.
        slots = {str(r['userMonsterId']): r for r in state['chip_slots']}
        for monster in state['monsters']:
            ident = monster['userMonsterId']
            slot = slots.get(ident)
            if slot is None:
                slot = {'userMonsterId': int(ident), 'equip': [], 'manage': {'extraSlotNum': 0}}
                state['chip_slots'].append(slot)
            slot['userMonsterId'] = int(ident)
            slot['manage'].update(slotNum=sum(self.free_slots(monster)), maxSlotNum=5, maxExtraSlotNum=5)

    def monster_slot(self, state, ident):
        if not self.positive(ident):
            raise self.error('Invalid chip monster')
        monster = self.store.training_monster(state, str(ident))
        self.refresh_slots(state)
        slot = next(r for r in state['chip_slots'] if r['userMonsterId'] == ident)
        return monster, slot

    @staticmethod
    def item_count(state, ident):
        return next((r['userItemNum'] for r in state.get('items', []) if r['itemId'] == ident), 0)

    @staticmethod
    def add_item(state, ident, amount):
        rows = state.setdefault('items', [])
        row = next((r for r in rows if r['itemId'] == ident), None)
        if row is None:
            row = {'itemId': ident, 'userItemNum': 0}
            rows.append(row)
        row['userItemNum'] += amount

    def project_monster(self, state, monster):
        result = copy.deepcopy(monster)
        slot = next((r for r in state['chip_slots'] if str(r['userMonsterId']) == monster['userMonsterId']), None)
        if slot:
            owned = {int(c['userChipId']): c for c in state['chips']}
            for equip in slot['equip']:
                # Old isolated fixtures may lack slot type/position. They are
                # not native equipment and cannot imply actual stat additions.
                chip = owned.get(equip['userChipId'])
                if chip and 'type' in equip and 'dispNum' in equip and int(chip.get('chipId', 0)) in self.bonus:
                    field, bonus = self.bonus[int(chip['chipId'])]
                    result[field] = str(int(result[field]) + bonus)
        return result

    def project_response(self, state, value):
        # Project after each activity before freezing its transport receipt.
        # Database stats stay plain so feeding/evolution/friendship cannot
        # accumulate or erase permanent chips; Usually effects skip client stats.
        if isinstance(value, dict):
            if 'userMonsterId' in value and 'monsterId' in value and all(k in value for k in self.STAT_FIELDS.values()):
                return self.project_monster(state, value)
            return {k: self.project_response(state, v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.project_response(state, v) for v in value]
        return value

    def equip(self, state, param):
        self.store.training_context(state)
        act, kind, disp, ident = (param.get(k) for k in ('act', 'type', 'dispNum', 'userChipId'))
        if (type(act) is not int or act not in (0, 1) or type(kind) is not int or kind not in (0, 1) or
                type(disp) is not int or not 1 <= disp <= 5 or not self.positive(ident)):
            raise self.error('Invalid chip equipment')
        monster, slot = self.monster_slot(state, param.get('userMonsterId'))
        if (kind == 0 and not self.free_slots(monster)[disp - 1] or
                kind == 1 and disp > slot['manage']['extraSlotNum']):
            raise self.error('Chip slot is locked')
        chip = next((c for c in state['chips'] if c['userChipId'] == ident), None)
        if not chip or int(chip['chipId']) not in self.bonus:
            raise self.error('Unsupported chip type')
        entry = {'type': kind, 'dispNum': disp, 'userChipId': ident}
        current = next((r for r in slot['equip'] if r['type'] == kind and r['dispNum'] == disp), None)
        signature = [act, kind, disp, ident, slot['userMonsterId']]
        if act == 1:
            if current == entry and chip['userMonsterId'] == slot['userMonsterId']:
                return {'resultCode': 1, 'equip': slot['equip'], 'userMonster': monster}
            if chip['userMonsterId'] != 0:
                raise self.error('Chip is already equipped')
            others = [r for r in slot['equip'] if r != current]
            owned = {c['userChipId']: c for c in state['chips']}
            group = self.master[int(chip['chipId'])]['chipGroupId']
            # Native ChipIcon dims every equipped family, including other ranks.
            if any(self.master[int(owned[r['userChipId']]['chipId'])]['chipGroupId'] == group for r in others):
                raise self.error('Duplicate chip effect')
            if current:
                owned[current['userChipId']]['userMonsterId'] = 0
                slot['equip'].remove(current)
            slot['equip'].append(entry)
            chip['userMonsterId'] = slot['userMonsterId']
        else:
            if current != entry or chip['userMonsterId'] != slot['userMonsterId']:
                if chip['userMonsterId'] == 0 and state.get('chip_last_equipment', {}).get(str(ident)) == signature:
                    return {'resultCode': 1, 'equip': slot['equip'], 'userMonster': monster}
                raise self.error('Chip is not in the requested slot')
            if self.item_count(state, 7) < 1:
                raise self.error('Not enough chip removal patches')
            self.add_item(state, 7, -1)
            chip['userMonsterId'] = 0
            slot['equip'].remove(current)
        state.setdefault('chip_last_equipment', {})[str(ident)] = signature
        return {'resultCode': 1, 'equip': slot['equip'], 'userMonster': monster}

    def expand(self, state, param):
        self.store.training_context(state)
        disp = param.get('dispNum')
        if type(disp) is not int or not 1 <= disp <= 5:
            raise self.error('Invalid extra chip slot')
        _, slot = self.monster_slot(state, param.get('userMonsterId'))
        count = slot['manage']['extraSlotNum']
        if disp <= count:
            return {'resultCode': 1}
        if disp != count + 1:
            raise self.error('Unlock the preceding chip slot first')
        cost = int(self.store.masters['990701']['codeM']['chipExtendSlotNeeds' + str(disp)])
        if self.item_count(state, 6) < cost:
            raise self.error('Not enough chip expansion patches')
        self.add_item(state, 6, -cost)
        slot['manage']['extraSlotNum'] = disp
        return {'resultCode': 1}

    def fuse(self, state, param):
        self.store.training_context(state)
        ident, ids = param.get('baseChip'), self.ids(param.get('materialChip'))
        if not self.positive(ident) or ident in ids:
            raise self.error('Invalid chip fusion base')
        owned = {c['userChipId']: c for c in state['chips']}
        base = owned.get(ident)
        if not base or int(base['chipId']) not in self.bonus:
            raise self.error('Unsupported chip type')
        key = json.dumps([ident, sorted(ids)], separators=(',', ':'))
        if key in state.get('chip_fusions', {}):
            return {'resultCode': 1, 'userChip': {'chipId': int(base['chipId'])}}
        row = self.master[int(base['chipId'])]
        destinations = [r for r in self.master.values() if r['chipGroupId'] == row['chipGroupId'] and
                        int(r['rank']) > int(row['rank']) and int(r['chipId']) in self.bonus]
        if not destinations:
            raise self.error('Chip has reached maximum rank')
        if len(ids) != int(row['needChip']) or any(i not in owned or
                owned[i]['userMonsterId'] != 0 or int(owned[i]['chipId']) != int(base['chipId']) for i in ids):
            raise self.error('Invalid chip fusion materials')
        if base['userMonsterId']:
            self.monster_slot(state, base['userMonsterId'])
        destination = min(destinations, key=lambda r: int(r['rank']))
        if base['userMonsterId']:
            _, slot = self.monster_slot(state, base['userMonsterId'])
            if any(owned[r['userChipId']]['userChipId'] != ident and
                   int(owned[r['userChipId']]['chipId']) == int(destination['chipId']) for r in slot['equip']):
                raise self.error('Duplicate chip effect')
        state['chip_id_sequence'] = max([state.get('chip_id_sequence', 0)] + list(owned))
        state['chips'] = [c for c in state['chips'] if c['userChipId'] not in ids]
        base['chipId'] = int(destination['chipId'])
        state.setdefault('chip_fusions', {})[key] = base['chipId']
        return {'resultCode': 1, 'userChip': {'chipId': base['chipId']}}

    def sell(self, state, param):
        self.store.training_context(state)
        ids = self.ids(param.get('materialChip'))
        key = json.dumps(sorted(ids), separators=(',', ':'))
        if key in state.get('chip_sales', {}):
            return {'resultCode': 1, 'itemRecovered': 0}
        owned = {c['userChipId']: c for c in state['chips']}
        if any(i not in owned or owned[i]['userMonsterId'] != 0 or int(owned[i]['chipId']) not in self.bonus for i in ids):
            raise self.error('Invalid chip sale materials')
        code = self.store.masters['990701']['codeM']
        cost = sum(int(self.master[int(owned[i]['chipId'])]['rank']) * int(code['baseChipSellPrice']) for i in ids)
        if int(state['player']['gamemoney']) + cost > int(code['maxClusterCount']):
            raise self.error('Chip sale would exceed coin capacity')
        state['player']['gamemoney'] = str(int(state['player']['gamemoney']) + cost)
        state['chip_id_sequence'] = max([state.get('chip_id_sequence', 0)] + list(owned))
        state['chips'] = [c for c in state['chips'] if c['userChipId'] not in ids]
        state.setdefault('chip_sales', {})[key] = cost
        # Original patch recovery odds/quantity unknown; no fabricated bonus.
        return {'resultCode': 1, 'itemRecovered': 0}

    def gacha(self, state, param):
        self.store.training_context(state)
        basic = self.store.local_gameplay['chips']
        is_advanced = self.advanced is not None and param.get('gachaId') == self.advanced['gachaId']
        if (type(param.get('gachaId')) is not int or
                (param['gachaId'] != basic['gachaId'] and not is_advanced) or
                type(param.get('playCount')) is not int or param['playCount'] not in (1, 10)):
            raise self.error('Invalid local chip capture')
        if is_advanced and not self.advanced_available(state):
            raise self.error('Advanced chip capture is locked')
        if not any(r['facilityId'] == 25 and r['level'] > 0 and not r['completeTime'] for r in state['facilities']):
            raise self.error('Build the chip factory first')
        config = self.advanced if is_advanced else basic
        count, cost = param['playCount'], config['linkCostPerChip'] * param['playCount']
        if int(state['player']['friendPoint']) < cost:
            raise self.error('Not enough Link points for chips')
        if len(state['chips']) + count > int(state['player']['chipLimitMax']):
            raise self.error('Chip inventory is full')
        assets, obtained = [], set(state.get('obtained_chip_ids', []))
        for _ in range(count):
            pool = self.pool
            if is_advanced:
                roll = secrets.randbelow(10000)
                for rank in sorted(config['rankWeightsBasisPoints'], key=int):
                    roll -= config['rankWeightsBasisPoints'][rank]
                    if roll < 0:
                        pool = config['chipIdsByRank'][rank]
                        break
            ident = pool[secrets.randbelow(len(pool))]
            chip = self.grant(state, ident, 1)[0]
            assets.append({'assetCategoryId': '17', 'assetValue': str(ident), 'assetNum': '1',
                'exValue': '', 'userAssetId': str(chip['userChipId']), 'effectType': '1',
                'isNew': int(ident not in obtained)})
            obtained.add(ident)
        state['obtained_chip_ids'] = sorted(obtained)
        state['player']['friendPoint'] = str(int(state['player']['friendPoint']) - cost)
        return {'userAssetList': assets, 'rewards': []}

    def advanced_available(self, state):
        return self.advanced is not None and str(self.advanced['unlockDungeonId']) in state.get('cleared_dungeons', [])
