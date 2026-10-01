"""Local Linkz protocol reconstruction. Only explicitly implemented APIs succeed.

No third-party dependencies. Run: python server/linkz_server.py
Source contracts are documented in server/README.md.
"""
import argparse
import base64
import copy
import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
import struct
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as BaseThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlsplit
from localization import ChineseLocalization, server_error_message
from progression import Progression
from help_pages import HelpPages
from chip_inventory import ChipInventory

ROOT = Path(__file__).resolve().parents[1]
# OptionSetting.Initialize indexes numeric keys directly (verified in Android log).
DEFAULT_OPTIONS = {'1': '0', '2': '0', '3': '0', '4': '0', '5': '0',
                   '101': '5', '102': '5', '103': '3', '104': '3', '105': '0'}
ABILITY_FIELDS = ('hp', 'attack', 'defense', 'spAttack', 'spDefense', 'speed')
ATTRIBUTE_RESISTANCE_FIELDS = ('none', 'fire', 'water', 'thunder', 'nature', 'light', 'dark')
STATUS_RESISTANCE_FIELDS = ('poison', 'confusion', 'paralysis', 'sleep', 'stun', 'skillLock', 'death')
# Original help25: ordinary battles expire 72 hours after entry.
NORMAL_BATTLE_LIFETIME_SECONDS = 72 * 60 * 60


def float32(value):
    """Round intermediate arithmetic like the client's Unity float values."""
    return struct.unpack('<f', struct.pack('<f', value))[0]


def initialize_player_state(state):
    # Reconstruction defaults, not recovered production-server starting balances.
    # Tutorial rewards must be granted by their corresponding operations later.
    state.setdefault('player', {'point': 0, 'stamina': 20, 'staminaMax': '20',
        'recovery': 0, 'gamemoney': '0', 'friendPoint': '0', 'meatNum': '0',
        'unitLimitMax': '50', 'meatLimitMax': '50', 'friendLimitMax': 50,
        'chipLimitMax': 100, 'gemePlayTime': '0', 'birthday': '', 'titleId': None})
    # TitleDataMng treats null as unequipped; ID 0 is absent from the title master.
    if state['player'].get('titleId') == '0':
        state['player']['titleId'] = None
    state.setdefault('monsters', [])
    for monster in state['monsters']:
        if monster.get('monsterId') == '756' and monster.get('commonSkillId') == '0':
            # Fixed reward 1104's zero is not a runtime skill: the master has no
            # skill 0 and MonsterUserData.GetCommonSkill dereferences it.
            # Use the original MonsterData.CreateMonsterData default locally.
            monster['commonSkillId'] = '473'
    state.setdefault('chips', [])
    state.setdefault('chip_slots', [])
    if state.get('tutorial_capture'):
        captured_id = state['tutorial_capture']['response']['userMonsterList'][0]['userMonsterId']
        for slot in state['chip_slots']:
            manage = slot.get('manage', {})
            if (str(slot['userMonsterId']) == captured_id and
                    manage == {'slotNum': 0, 'extraSlotNum': 0, 'maxSlotNum': 0, 'maxExtraSlotNum': 0}):
                # ChipBaseSelect requires total capacity 5 or 10, even if locked.
                manage['maxSlotNum'] = 5
    state.setdefault('souls', [])
    state.setdefault('facilities', [])
    if not any(r['facilityId'] == 2 for r in state['facilities']):
        state['player']['meatLimitMax'] = '50'
    state.setdefault('stock_facilities', [])
    state.setdefault('last_harvest', [])
    state.setdefault('farm_monsters', [])
    state.setdefault('decks', {'deckCnt': 1, 'selectDeckNum': '1', 'favoriteDeckNum': '1',
                             'deckList': [{'deckNum': '1', 'monsterList': []}]})
    response = state.get('active_battle', {}).get('response', {})
    if response.get('luckDrop') == {'dropBoxType': 0, 'assetCategoryId': '0', 'assetValue': 0, 'assetNum': 0}:
        # DropItemResult treats every non-null luckDrop as an actual item;
        # category 0 has no icon and crashes its result animation callback.
        response['luckDrop'] = None
    refresh_stamina(state)


def refresh_stamina(state, now=None):
    """Client UserStamina expects seconds to FULL, at 180 seconds per point."""
    now = int(time.time()) if now is None else int(now)
    player = state['player']
    maximum = int(player['staminaMax'])
    if int(player['stamina']) >= maximum:
        player['recovery'] = 0
        state.pop('stamina_full_at', None)
        return
    if 'stamina_full_at' not in state:
        # Migrate previous local debits using their known start time.
        start = state.get('active_battle', {}).get('started')
        anchor = int(datetime.strptime(start, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).timestamp()) if start else now
        state['stamina_full_at'] = anchor + (maximum - int(player['stamina'])) * 180
    remaining = max(0, state['stamina_full_at'] - now)
    player['recovery'] = remaining
    player['stamina'] = maximum - (remaining + 179) // 180
    if remaining == 0:
        state.pop('stamina_full_at', None)


def battle_is_expired(battle, now=None):
    if battle.get('expired'):
        return True
    if battle.get('kind') != 'normal' or battle.get('settled'):
        return False
    deadline = battle.get('expires_at')
    if deadline is None:
        # Old battles keep their original entry anchor. Do not restart a timer.
        try:
            started = datetime.strptime(battle['started'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        except (KeyError, TypeError, ValueError):
            return False
        deadline = int(started.timestamp()) + NORMAL_BATTLE_LIFETIME_SECONDS
    return (int(time.time()) if now is None else int(now)) >= deadline


def compact_finished_battle(battle):
    """Keep durable settlement/expiry receipts without obsolete combat snapshots.

    Only archived finished encounters use this representation. The current
    encounter keeps its full response for resume and cached start responses.
    """
    if not battle.get('settled'):
        return
    battle['response'] = {key: value for key, value in battle['response'].items()
                          if key not in ('deck', 'dungeonFloor', 'luckDrop')}
    for key in ('chip_snapshot', 'material_drops', 'friendship_rules',
                'friendship_gains', 'link_points', 'staminaCost', 'alive_slot_indices'):
        battle.pop(key, None)


def compact_battle_history(state):
    # Lazily compact older local saves inside the normal account transaction.
    # Keep every receipt: late settlements must never award a second victory.
    for battle in state.get('battle_history', {}).values():
        compact_finished_battle(battle)


def refresh_battle_expiry(state):
    battle = state.get('active_battle')
    if battle and not battle.get('settled') and battle_is_expired(battle):
        # Retain the full encounter for recovery, with no victory/reward receipt.
        battle.update(settled=True, expired=True, expired_at=timestamp())


def filter_owned(rows, requested, field):
    if requested is None:
        return rows
    if not isinstance(requested, list) or any(type(x) is not int or x <= 0 for x in requested):
        raise ProtocolError('Invalid inventory filter')
    ids = set(requested)
    return [row for row in rows if int(row[field]) in ids]


def request_json(raw):
    def object_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ProtocolError('Duplicate activity JSON fields')
            result[key] = value
        return result

    def reject_constant(value):
        raise ProtocolError('Invalid activity JSON number')

    try:
        return json.loads(raw, object_pairs_hook=object_fields, parse_constant=reject_constant)
    except (ValueError, RecursionError):
        raise ProtocolError('Invalid activityList JSON')


def encode(value):
    return quote(str(value), safe='~-._')


def signature(method, url, parameters, consumer_secret, token_secret=''):
    parts = urlsplit(url)
    pairs = list(parameters) + parse_qsl(parts.query, keep_blank_values=True)
    pairs = sorted((encode(k), encode(v)) for k, v in pairs if k not in ('oauth_signature', 'realm'))
    normalized = '&'.join(k + '=' + v for k, v in pairs)
    authority = parts.hostname.lower()
    if parts.port and (parts.scheme, parts.port) not in (('http', 80), ('https', 443)):
        authority += ':' + str(parts.port)
    base_url = parts.scheme.lower() + '://' + authority + (parts.path or '/')
    message = '&'.join(encode(x) for x in (method.upper(), base_url, normalized))
    key = encode(consumer_secret) + '&' + encode(token_secret)
    return base64.b64encode(hmac.new(key.encode(), message.encode(), hashlib.sha1).digest()).decode()


class ProtocolError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code
        self.country = '3'


def timestamp():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def load_consumers():
    source = next((ROOT / 'source-client').glob('astiob-*')) / 'Secure/ConstValue.cs'
    values = dict(re.findall(r'public static string (\w+) = "([^"\n]+)"', source.read_text()))
    return {values[p + 'COUNSUMER_KEY']: values[p + 'COUNSUMER_SECRET']
            for p in ('AND_', 'MULTI_LANGUAGE_')}


class Store:
    def __init__(self, database, master_dir=None, consumers=None, resource_catalog=None):
        self.resource_catalog = resource_catalog
        self.database = str(database)
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        self.consumers = consumers if consumers is not None else load_consumers()
        master_dir = Path(master_dir) if master_dir else next((ROOT / 'source-masters').glob('astiob-*'))
        self.masters = {}
        for path in master_dir.glob('*.json'):
            match = re.search(r' (\d{6}) ', path.name)
            if match:
                self.masters[match[1]] = json.loads(path.read_text(encoding='utf-8-sig'))
        locale_root = ROOT / 'server/localization/cn'
        self.chinese_labels = json.loads((locale_root / '990802.json').read_text(encoding='utf-8'))
        self.chinese_tutorial = json.loads((locale_root / 'tutorial-messages.json').read_text(encoding='utf-8'))
        self.chinese_ui_overrides = json.loads((locale_root / 'ui-overrides.json').read_text(encoding='utf-8'))
        self.localization = ChineseLocalization(self.masters, self.chinese_labels,
                                               self.chinese_tutorial, self.chinese_ui_overrides,
                                               Path(database).parent / 'localization-version.json')
        self.tutorial_battle = json.loads((ROOT / 'server/tutorial-battle.json').read_text(encoding='utf-8'))
        self.normal_world = json.loads((ROOT / 'server/normal-world.json').read_text(encoding='utf-8'))['dungeons']
        self.local_gameplay = json.loads((ROOT / 'server/local-gameplay.json').read_text(encoding='utf-8'))
        self.battle_friendship_rules()
        self.resistance_rules()
        self.chip_inventory = ChipInventory(self, ProtocolError)
        self.progression = Progression(self, ROOT / 'server/local-missions.json', ProtocolError)
        self.farm_layout = json.loads((ROOT / 'server/farm-layout.json').read_text(encoding='utf-8'))
        groups = {r['monsterGroupId']: r['monsterCollectionId'] for r in self.masters['990103']['monsterM']}
        self.collection_ids = set(groups.values()) - {'0'}
        self.monster_collections = {r['monsterId']: groups[r['monsterGroupId']]
                                    for r in self.masters['990102']['monsterM']}
        self.validate_normal_world()
        self.help_pages = HelpPages(self)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS accounts(
                  id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT UNIQUE NOT NULL,
                  token TEXT UNIQUE NOT NULL, secret TEXT NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS retries(
                  token TEXT NOT NULL, api TEXT NOT NULL, request_id INTEGER NOT NULL,
                  input TEXT NOT NULL, output TEXT NOT NULL,
                  PRIMARY KEY(token,api,request_id));
                CREATE TABLE IF NOT EXISTS nonces(
                  consumer TEXT NOT NULL, token TEXT NOT NULL, nonce TEXT NOT NULL,
                  stamp INTEGER NOT NULL, PRIMARY KEY(consumer,token,nonce,stamp));
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def verify(self, db, method, url, form, authorization, auth=False, claimed_uuid=None):
        if not authorization.startswith('OAuth '):
            raise ProtocolError('Missing OAuth authorization')
        pairs = [(unquote(k), unquote(v)) for k, v in re.findall(r'(\w+)="([^"]*)"', authorization[6:])]
        oauth = dict(pairs)
        if len(oauth) != len(pairs):
            raise ProtocolError('Duplicate OAuth fields')
        consumer = oauth.get('oauth_consumer_key', '')
        if consumer not in self.consumers or oauth.get('oauth_signature_method') != 'HMAC-SHA1':
            raise ProtocolError('Unsupported consumer or signature method')
        try:
            stamp = int(oauth['oauth_timestamp'])
        except (KeyError, ValueError):
            raise ProtocolError('Invalid OAuth timestamp')
        if not 0 <= stamp <= 9223372036854775807 or abs(time.time() - stamp) > 300 or not oauth.get('oauth_nonce'):
            raise ProtocolError('Expired timestamp or missing nonce')
        token = oauth.get('oauth_token', '')
        account = None
        if not auth:
            account = db.execute('SELECT * FROM accounts WHERE token=?', (token,)).fetchone()
            if account is None or (claimed_uuid and claimed_uuid != account['uuid']):
                raise ProtocolError('Unknown token or mismatched UUID')
        elif token:
            raise ProtocolError('Initial authentication must not contain a token')
        expected = signature(method, url, list(form.items()) + pairs,
                             self.consumers[consumer], account['secret'] if account else '')
        if not hmac.compare_digest(expected, oauth.get('oauth_signature', '')):
            raise ProtocolError('Invalid OAuth signature')
        db.execute('DELETE FROM nonces WHERE stamp<?', (int(time.time()) - 600,))
        try:
            db.execute('INSERT INTO nonces VALUES(?,?,?,?)', (consumer, token, oauth['oauth_nonce'], stamp))
        except sqlite3.IntegrityError:
            raise ProtocolError('Replayed OAuth nonce')
        return account

    def authenticate(self, db, uuid):
        if not isinstance(uuid, str) or not 1 <= len(uuid) <= 128:
            raise ProtocolError('Invalid x_uuid')
        account = db.execute('SELECT * FROM accounts WHERE uuid=?', (uuid,)).fetchone()
        if account is None:
            # Starting account only; no fabricated farm/monsters or completed tutorial.
            state = {'nickname': 'Tamer', 'description': '', 'options': dict(DEFAULT_OPTIONS), 'policy': 0,
                     'tutorial': {'statusId': '0', 'endFlg': '0'}, 'created': timestamp(),
                     'login_time': '', 'login_count': 0, 'device_token': ''}
            db.execute('INSERT INTO accounts(uuid,token,secret,state) VALUES(?,?,?,?)',
                       (uuid, secrets.token_hex(24), secrets.token_hex(32), json.dumps(state)))
            account = db.execute('SELECT * FROM accounts WHERE uuid=?', (uuid,)).fetchone()
        else:
            # APIUtil request ids restart with wall-clock time on client restart.
            # A new auth session prevents old ids being mistaken for retries.
            db.execute('DELETE FROM retries WHERE token=?', (account['token'],))
            db.execute('UPDATE accounts SET token=?,secret=? WHERE id=?',
                       (secrets.token_hex(24), secrets.token_hex(32), account['id']))
            account = db.execute('SELECT * FROM accounts WHERE uuid=?', (uuid,)).fetchone()
        return {'venus_status': 1, 'token_key': account['token'], 'token_secret': account['secret'],
                'x_user_id': account['id'], 'x_uuid': uuid, 'maintenance': None}

    def normal_material_drops(self, config):
        if 'materialPerWave' in config and 'materialsPerWave' in config:
            raise ProtocolError('Invalid local material drop configuration')
        rows = config.get('materialsPerWave', [config['materialPerWave']] if config.get('materialPerWave') else [])
        souls = {r['soulId'] for r in self.masters['990201']['soulM']}
        if (not isinstance(rows, list) or len(rows) > 8 or any(
                not isinstance(r, dict) or r.get('assetCategoryId') != '14' or
                type(r.get('assetValue')) is not int or str(r['assetValue']) not in souls or
                type(r.get('assetNum')) is not int or not 0 < r['assetNum'] <= 2147483647 or
                type(r.get('dropBoxType')) is not int or r['dropBoxType'] not in (1, 2)
                for r in rows) or len({r['assetValue'] for r in rows}) != len(rows)):
            raise ProtocolError('Invalid local material drop configuration')
        return copy.deepcopy(rows)

    def validate_normal_world(self):
        areas = {r['worldAreaId'] for r in self.masters['990903']['worldAreaM'] if r['worldId'] == '1'}
        stages = {r['worldStageId'] for r in self.masters['990904']['worldStageM'] if r['worldAreaId'] in areas}
        dungeons = {r['worldDungeonId']: r for r in self.masters['990905']['worldDungeonM'] if r['worldStageId'] in stages}
        monsters = {r['monsterId']: r for r in self.masters['990102']['monsterM']}
        for ident, config in self.normal_world.items():
            dungeon = dungeons.get(ident)
            if not dungeon or not isinstance(config, dict):
                raise ValueError('Invalid local normal dungeon: ' + ident)
            enemies, level = config.get('enemyMonsterIds'), config.get('enemyLevel', 1)
            if (not isinstance(enemies, list) or not 1 <= len(enemies) <= 3 or type(level) is not int or
                    any(mid not in monsters or not 1 <= level <= int(monsters[mid]['maxLevel']) for mid in enemies) or
                    any(type(config.get(k)) is not int or not 0 <= config[k] <= 1000000
                        for k in ('fixedExpPerEnemy', 'fixedMoneyPerEnemy'))):
                raise ValueError('Invalid local normal enemy configuration: ' + ident)
            drops = self.normal_material_drops(config)
            if any(r['assetNum'] * int(dungeon['battleNum']) > 2147483647 for r in drops):
                raise ValueError('Invalid local normal drop quantity: ' + ident)

    def normal_world_info(self, state):
        areas = {r['worldAreaId'] for r in self.masters['990903']['worldAreaM'] if r['worldId'] == '1'}
        stages = [r for r in self.masters['990904']['worldStageM'] if r['worldAreaId'] in areas]
        dungeons = self.masters['990905']['worldDungeonM']
        cleared = set(state.get('cleared_dungeons', []))
        cleared_stages = {s['worldStageId'] for s in stages
                          if (rows := [d for d in dungeons if d['worldStageId'] == s['worldStageId']])
                          and all(d['worldDungeonId'] in cleared for d in rows)}
        output = []
        for stage in stages:
            unlocked = stage['prerequisiteWorldStageId'] in ('0', '') or stage['prerequisiteWorldStageId'] in cleared_stages
            rows = []
            for dungeon in dungeons:
                if dungeon['worldStageId'] != stage['worldStageId']:
                    continue
                ident = dungeon['worldDungeonId']
                opened = ident in self.normal_world and unlocked and (dungeon['prerequisiteWorldDungeonId'] in ('0', '') or dungeon['prerequisiteWorldDungeonId'] in cleared)
                config = self.normal_world.get(ident)
                count = len(config['enemyMonsterIds']) * int(dungeon['battleNum']) if config else 0
                rows.append({'worldDungeonId': int(ident), 'status': 4 if ident in cleared else 2 if opened else 1,
                    'isExtraWave': 0, 'exp': count * config['fixedExpPerEnemy'] if config else 0,
                    'money': count * config['fixedMoneyPerEnemy'] if config else 0,
                    'encountEnemies': [], 'dropAssets': [dict(r, assetCategoryId=14) for r in self.normal_material_drops(config)] if config else [], 'playLimit': None,
                    'dungeonTicketId': '0', 'userDungeonTicketId': '0', 'dungeonTicketNum': '0'})
            output.append({'worldStageId': int(stage['worldStageId']), 'isOpen': 1, 'isCounting': 0,
                'updateTime': timestamp(), 'expireTime': stage['closeTime'], 'timeLeft': 0, 'dungeons': rows})
        return {'worldDungeonInfo': output}

    def add_monster_experience(self, monster, amount):
        master = next(m for m in self.masters['990102']['monsterM'] if m['monsterId'] == monster['monsterId'])
        levels = sorted((r for r in self.masters['990106']['monsterExperienceM']
                         if int(r['level']) <= int(master['maxLevel'])), key=lambda r: int(r['level']))
        total = min(int(monster['ex']) + amount, int(levels[-1]['experienceNum']))
        current = max((r for r in levels if int(r['experienceNum']) <= total), key=lambda r: int(r['level']))
        level = int(current['level'])
        following = next((r for r in levels if int(r['level']) == level + 1), None)
        monster.update(ex=str(total), level=str(level), levelEx=str(total - int(current['experienceNum'])),
                       nextLevelEx=str(int(following['experienceNum']) - total) if following else '0')
        for field in ('hp', 'attack', 'defense', 'spAttack', 'spDefense'):
            suffix = field[0].upper() + field[1:]
            low, high = int(master['default' + suffix]), int(master['max' + suffix])
            monster[field] = str(int(low + (high - low) * (level - 1) / max(1, int(master['maxLevel']) - 1) + 0.5))
        monster['speed'] = master['speed']
        # StatusUpAnimation applies talent percentages to level base stats,
        # separately from friendship. Training must preserve those medals.
        for field in ('hp', 'attack', 'defense', 'spAttack', 'spDefense', 'speed'):
            monster[field] = str(int(monster[field]) + int(monster[field]) * int(monster.get(field + 'Ability', '0')) // 100)
        for field, bonus in self.friendship_bonus(monster).items():
            monster[field] = str(int(monster[field]) + bonus)

    def friendship_bonus(self, monster):
        master = next(r for r in self.masters['990102']['monsterM'] if r['monsterId'] == monster['monsterId'])
        step = int(monster.get('friendship', '0')) // int(self.masters['990701']['codeM']['riseConditionFriendShipStatus'])
        # Farm's popup calculates the increase for the crossed threshold rather
        # than rounding a cumulative percentage. Keep each +30 increment exact;
        # otherwise e.g. maxHp=566 would increase by 6 at +60 but display +5.
        return {field: (int(master['speed' if field == 'speed' else 'max' + field[0].upper() + field[1:]]) // 100) * step
                for field in ('hp', 'attack', 'defense', 'spAttack', 'spDefense', 'speed')}

    def battle_friendship_rules(self, rules=None):
        rules = self.local_gameplay['battleFriendship'] if rules is None else rules
        threshold = int(self.masters['990701']['codeM']['riseConditionFriendShipStatus'])
        if (not isinstance(rules, dict) or type(rules.get('probabilityBasisPoints')) is not int or
                not 0 <= rules['probabilityBasisPoints'] <= 10000 or
                type(rules.get('gainOnSuccess')) is not int or not 1 <= rules['gainOnSuccess'] < threshold):
            raise ProtocolError('Invalid battle friendship rules')
        # A single award crosses at most one threshold, matching the existing
        # popup rounding and the saved sum of individual +30 increments.
        return {k: rules[k] for k in ('probabilityBasisPoints', 'gainOnSuccess')}

    def award_battle_friendship(self, battle, state, alive):
        rules = battle.get('friendship_rules')
        if rules is None or battle.get('kind') != 'normal':
            return
        rules = self.battle_friendship_rules(rules)
        chance = rules['probabilityBasisPoints']
        gains = {}
        for ident, survived in zip(battle['response']['deck']['userMonsterIds'], alive):
            if not survived:
                continue
            monster = next(m for m in state['monsters'] if m['userMonsterId'] == ident)
            maximum = int(next(r for r in self.masters['990112']['monsterGrowStepM']
                               if r['monsterGrowStepId'] == self.monster_growth_step(monster))['maxFriendship'])
            gain = min(rules['gainOnSuccess'], max(0, maximum - int(monster.get('friendship', '0'))))
            if gain and chance and (chance == 10000 or secrets.randbelow(10000) < chance):
                before = self.friendship_bonus(monster)
                monster['friendship'] = str(int(monster.get('friendship', '0')) + gain)
                after = self.friendship_bonus(monster)
                for field in after:
                    monster[field] = str(int(monster[field]) + after[field] - before[field])
                gains[ident] = gain
            else:
                gains[ident] = 0
        # Internal audit only; the native client reads the updated monster list.
        battle['friendship_gains'] = gains

    def next_monster_id(self, state):
        # Materials disappear, but their IDs must never identify future captures.
        ident = max([state.get('monster_id_sequence', 0)] +
                    [int(m['userMonsterId']) for m in state['monsters']] +
                    [int(i) for i in state.get('consumed_monsters', {})]) + 1
        if ident > 2147483647:
            raise ProtocolError('Monster identity limit reached')
        state['monster_id_sequence'] = ident
        return ident

    def training_monster(self, state, ident, material=False):
        monster = next((m for m in state['monsters'] if m['userMonsterId'] == ident), None)
        if (not isinstance(ident, str) or not monster or monster['eggFlg'] != '0' or
                monster.get('growEndDate') or self.monster_growth_step(monster) in ('1', '2', '3')):
            raise ProtocolError('Invalid training monster')
        if material:
            if monster.get('statusFlgs', '0') in ('2', '3'):
                raise ProtocolError('Locked monsters cannot be consumed')
            group = next(r for r in self.masters['990103']['monsterM']
                         if r['monsterGroupId'] == next(m for m in self.masters['990102']['monsterM']
                             if m['monsterId'] == monster['monsterId'])['monsterGroupId'])
            if group['monsterType'] == '1':
                raise ProtocolError('Special training monsters cannot be consumed here')
            if (any(r['userMonsterId'] == ident for d in state['decks']['deckList'] for r in d['monsterList']) or
                    ident in {str(r['userMonsterId']) for r in state.get('colosseum_monsters', [])}):
                raise ProtocolError('Remove material monsters from every deck first')
        return monster

    def training_context(self, state):
        if state['tutorial']['endFlg'] != '1':
            raise ProtocolError('Training is available after the tutorial')
        if state.get('active_battle') and not state['active_battle']['settled']:
            raise ProtocolError('Finish the active battle before training')

    def consume_monsters(self, state, ids, operation):
        ids = set(ids)
        # Reserve even old saves' highest ID before removing material rows.
        state['monster_id_sequence'] = max([state.get('monster_id_sequence', 0)] +
                                          [int(m['userMonsterId']) for m in state['monsters']])
        state['monsters'] = [m for m in state['monsters'] if m['userMonsterId'] not in ids]
        state['chip_slots'] = [r for r in state['chip_slots'] if str(r['userMonsterId']) not in ids]
        # Original ChipSlotData resets ownership, keeping the chips in inventory.
        for chip in state['chips']:
            if str(chip.get('userMonsterId', 0)) in ids:
                chip['userMonsterId'] = 0
        state['farm_monsters'] = [i for i in state['farm_monsters'] if str(i) not in ids]
        for ident in ids:
            state.get('friendship_ready_at', {}).pop(ident, None)
            state.setdefault('consumed_monsters', {})[ident] = operation

    def training_receipt(self, state, operation, ident, response_key=None, response_value=None):
        if operation in state.get('training_operations', {}):
            # On reauth return today's owned monster, never a stale pre-evolution
            # snapshot that could visually undo later skill/status changes.
            monster = next((m for m in state['monsters'] if m['userMonsterId'] == ident), None)
            if not monster:
                raise ProtocolError('Training base monster is no longer owned')
            response = {'userMonster': monster}
            if response_key is not None:
                response[response_key] = response_value
            return response

    def talent_values(self, monster):
        values = {}
        for field in ABILITY_FIELDS:
            raw = monster.get(field + 'Ability', '0')
            if not isinstance(raw, str) or not raw.isdecimal():
                raise ProtocolError('Invalid talent medal state')
            value = int(raw)
            flag = '0' if value == 0 else '2' if 5 <= value <= 10 else '1' if 15 <= value <= 20 else None
            if flag is None or monster.get(field + 'AbilityFlg', '0') != flag:
                raise ProtocolError('Invalid talent medal state')
            values[field] = value
        return values

    def talent_cost(self, base_values, material_values):
        # CMD_MedalInherit.CalcCluster rounds float32 at each operation.
        power = float32(math.pow(float32(1.15), max(base_values.values())))
        subtotal = float32(float32(math.floor(float32(power * 10)) / 10) * 500)
        count = sum(value > 0 for value in material_values.values())
        multiplier = float32(1 + float32(float32(0.1) * (count - 1)))
        return math.floor(float32(subtotal * multiplier))

    def talent_plan(self, base, material):
        bv, mv = self.talent_values(base), self.talent_values(material)
        master = next(r for r in self.masters['990102']['monsterM'] if r['monsterId'] == base['monsterId'])
        cap = int(next(r for r in self.masters['990118']['monsterRareM'] if r['monsterRareId'] == master['rare'])['goldMedalMaxNum'])
        held = sum(value >= 15 for value in bv.values())
        if held > cap:
            raise ProtocolError('Invalid talent medal state')
        stage = {'4': 'Growing', '10': 'Growing', '5': 'Ripe', '8': 'Ripe', '11': 'Ripe',
                 '6': 'Perfect', '12': 'Perfect', '7': 'Ultimate', '9': 'Ultimate', '13': 'Ultimate'}[self.monster_growth_step(material)]
        code = self.masters['990701']['codeM']
        multiplier = float32(float(code['abilityUpgradeMulRate' + stage]))
        rates = {(int(r['baseAbility']), int(r['materialAbility'])): int(r['rate'])
                 for r in self.masters['990117']['abilityUpgradeRateM']}
        plan = {}
        for field in ABILITY_FIELDS:
            before, source = bv[field], mv[field]
            rate, after, fallback = 0, before, before
            if source and before < 20:
                percent = (float32(multiplier * float32(rates.get((before, source), 0) / 100))
                           if before else float32(float(code['abilityInheritRate' + stage])))
                rate = min(1000, math.floor(float32(percent * 10)))  # tenths of a percent
                after = source if not before else 15 if before == 10 else before + 1
                if not before and source >= 15 and rate < 1000:
                    fallback = 5
                if before < 15 <= after and held >= cap:
                    rate = 0
            plan[field] = {'rate': rate, 'after': after, 'fallback': fallback,
                           'new_gold': before < 15 <= after}
        return bv, mv, cap - held, plan

    def inherit_talent(self, state, param):
        self.training_context(state)
        ident, material_id = param.get('baseUserMonsterId'), param.get('materialUserMonsterId')
        if not isinstance(ident, str) or not isinstance(material_id, str) or ident == material_id:
            raise ProtocolError('Invalid talent inheritance request')
        operation = json.dumps(['010502', ident, material_id], separators=(',', ':'))
        receipt = self.training_receipt(state, operation, ident)
        if receipt:
            return receipt
        base = self.training_monster(state, ident)
        material = self.training_monster(state, material_id, material=True)
        bv, mv, available, plan = self.talent_plan(base, material)
        if not any(row['rate'] or row['fallback'] > bv[field] for field, row in plan.items()):
            raise ProtocolError('Material cannot improve any talent')
        cost = self.talent_cost(bv, mv)
        if int(state['player']['gamemoney']) < cost:
            raise ProtocolError('Not enough talent inheritance money')
        outcomes, new_gold = {}, []
        for field, row in plan.items():
            success = row['rate'] == 1000 or (row['rate'] > 0 and secrets.randbelow(1000) < row['rate'])
            outcomes[field] = row['after'] if success else row['fallback']
            if success and row['new_gold']:
                new_gold.append(field)
        # Original server's over-cap tie-breaking is unknown. Local rule chooses
        # uniformly among successful new gold medals; overflow keeps fallback.
        while len(new_gold) > available:
            rejected = new_gold.pop(secrets.randbelow(len(new_gold)))
            outcomes[rejected] = plan[rejected]['fallback']
        for field, value in outcomes.items():
            base[field + 'Ability'] = str(value)
            base[field + 'AbilityFlg'] = '1' if value >= 15 else '2' if value else '0'
        self.add_monster_experience(base, 0)
        state['player']['gamemoney'] = str(int(state['player']['gamemoney']) - cost)
        self.consume_monsters(state, [material_id], operation)
        state.setdefault('training_operations', {})[operation] = {'api': '010502', 'cost': cost, 'abilities': outcomes}
        state['normal_talent_inheritance_count'] = state.get('normal_talent_inheritance_count', 0) + 1
        return {'userMonster': base}

    def resistance_rules(self):
        rules = self.local_gameplay.get('resistanceChange')
        if not isinstance(rules, dict) or rules.get('selectionPolicy') != 'uniform_excluding_innate_resistant':
            raise ProtocolError('Invalid resistance rules')
        masters = {r['monsterResistanceId']: r for r in self.masters['990107']['monsterResistanceM']}
        result = {}
        for kind, key, fields in ((1, 'attributeResistanceIds', ATTRIBUTE_RESISTANCE_FIELDS),
                                   (2, 'statusResistanceIds', STATUS_RESISTANCE_FIELDS)):
            ids = rules.get(key)
            if (not isinstance(ids, list) or len(ids) != len(fields) or
                    any(not isinstance(i, str) or i not in masters for i in ids) or len(set(ids)) != len(ids)):
                raise ProtocolError('Invalid resistance rules')
            mapped = []
            for ident in ids:
                row = masters[ident]
                active = [f for f in ATTRIBUTE_RESISTANCE_FIELDS + STATUS_RESISTANCE_FIELDS if row[f] != '0']
                if len(active) != 1 or active[0] not in fields or row[active[0]] != '1':
                    raise ProtocolError('Invalid resistance rules')
                mapped.append((ident, active[0]))
            if {f for _, f in mapped} != set(fields):
                raise ProtocolError('Invalid resistance rules')
            result[kind] = mapped
        return result

    def resistance_candidates(self, master, kind):
        innate = next((r for r in self.masters['990107']['monsterResistanceM']
                       if r['monsterResistanceId'] == master['resistanceId']), None)
        if not innate:
            raise ProtocolError('Resistance master is missing')
        candidates = [ident for ident, field in self.resistance_rules()[kind] if int(innate[field]) <= 0]
        if not candidates:
            raise ProtocolError('No additional resistance is available')
        return candidates

    def change_resistance(self, state, param):
        self.training_context(state)
        ident, material_id, kind = (param.get(k) for k in ('baseUserMonsterId', 'materialUserMonsterId', 'type'))
        if (any(type(i) is not int or not 0 < i <= 2147483647 for i in (ident, material_id)) or
                type(kind) is not int or kind not in (1, 2)):
            raise ProtocolError('Invalid resistance change request')
        ident, material_id = str(ident), str(material_id)
        operation = json.dumps(['010801', ident, material_id, kind], separators=(',', ':'))
        # Ordinary partners are consumed and their unique IDs cannot be reused.
        # Self-material paths are repeatable, paid rerolls; transport request IDs
        # deduplicate them in this session, not a permanent base/base receipt.
        if ident != material_id:
            receipt = self.training_receipt(state, operation, ident)
            if receipt:
                return receipt
        if not any(r['facilityId'] == 4 and r['level'] >= 1 for r in state['facilities']):
            raise ProtocolError('Complete the dojo before resistance changes')
        base = self.training_monster(state, ident)
        master = next(r for r in self.masters['990102']['monsterM'] if r['monsterId'] == base['monsterId'])
        group = next(r for r in self.masters['990103']['monsterM'] if r['monsterGroupId'] == master['monsterGroupId'])
        required = {}
        souls = {r['soulId'] for r in self.masters['990201']['soulM']}
        def require(soul, amount, category='14'):
            if category != '14' or soul not in souls or not isinstance(amount, str) or not amount.isdecimal() or int(amount) <= 0:
                raise ProtocolError('Resistance materials are not implemented')
            required[soul] = required.get(soul, 0) + int(amount)
        consume = False
        if kind == 1:
            fragment = [r for r in self.masters['990113']['monsterTranceM'] if r['monsterId'] == master['monsterId']]
            if fragment:
                if material_id != ident:
                    raise ProtocolError('This resistance change uses materials instead of a partner')
                for row in fragment:
                    require(row['soulId'], row['num'])
            else:
                if material_id == ident or group['growStep'] in ('7', '9', '13'):
                    raise ProtocolError('A same-name partner is required for resistance change')
                material = self.training_monster(state, material_id, material=True)
                mm = next(r for r in self.masters['990102']['monsterM'] if r['monsterId'] == material['monsterId'])
                if mm['monsterGroupId'] != master['monsterGroupId']:
                    raise ProtocolError('A same-name partner is required for resistance change')
                consume = True
            if len(fragment) <= 1:
                tribe = [r for r in self.masters['990114']['monsterTribeTranceM']
                         if r['tribe'] == group['tribe'] and r['growStep'] == group['growStep']]
                if not tribe:
                    raise ProtocolError('Resistance materials are not implemented')
                for row in tribe:
                    require(row['soulId'], row['num'])
        else:
            if int(master['rare']) < 6:
                raise ProtocolError('Status resistance requires a version-up monster')
            row = next((r for r in self.masters['990120']['monsterStatusAilmentM']
                        if r['monsterId'] == master['monsterId']), None)
            recipe = next((r for r in self.masters['990121']['monsterStatusAilmentMaterialM']
                           if row and r['monsterStatusAilmentMaterialId'] == row['monsterStatusAilmentMaterialId']), None)
            if not row or not recipe or material_id != ident:
                raise ProtocolError('Resistance materials are not implemented')
            require(row['assetValue'], row['assetNum'], row['assetCategoryId'])
            for slot in range(1, 5):
                count = recipe['materialAssetNum' + str(slot)]
                if count != '0':
                    require(recipe['materialAssetValue' + str(slot)], count, recipe['materialAssetCategoryId' + str(slot)])
        owned = {r['soulId']: r for r in state['souls']}
        if any(s not in owned or int(owned[s]['num']) < n for s, n in required.items()):
            raise ProtocolError('Not enough resistance materials')
        candidates = self.resistance_candidates(master, kind)
        selected = secrets.choice(candidates)
        for soul, count in required.items():
            owned[soul]['num'] = str(int(owned[soul]['num']) - count)
        field = 'tranceResistance' if kind == 1 else 'tranceStatusAilment'
        base[field] = selected
        if consume:
            self.consume_monsters(state, [material_id], operation)
            state.setdefault('training_operations', {})[operation] = {'api': '010801', 'kind': kind, 'materials': required}
        state['normal_resistance_change_count'] = state.get('normal_resistance_change_count', 0) + 1
        return {'userMonster': base}

    def evolution_resistance_values(self, monster, destination):
        # Original help45 carries additions through evolution and rerolls any
        # collision with the destination's intrinsic resistance. Keep otherwise
        # valid legacy comma-separated values; do not rewrite existing saves.
        masters = {r['monsterResistanceId']: r for r in self.masters['990107']['monsterResistanceM']}
        innate = masters[destination['resistanceId']]
        values = {}
        for kind, key, fields in ((1, 'tranceResistance', ATTRIBUTE_RESISTANCE_FIELDS),
                                  (2, 'tranceStatusAilment', STATUS_RESISTANCE_FIELDS)):
            value = monster.get(key, '')
            if not isinstance(value, str) or any(i not in masters for i in value.split(',') if value):
                raise ProtocolError('Invalid added resistance')
            collision = value and any(int(masters[i][f]) > 0 and int(innate[f]) > 0
                                      for i in value.split(',') for f in fields)
            values[key] = secrets.choice(self.resistance_candidates(destination, kind)) if collision else value
        return values

    def change_monster_mode(self, state, monster, evolution):
        self.training_context(state)
        self.training_monster(state, monster['userMonsterId'])
        if evolution is None:
            # Caller admits this path only for the last target already owned.
            # Return current training/skill data, never a historical snapshot.
            return {'result': 1, 'userMonster': monster, 'reviewStatus': 0}
        masters = {r['monsterId']: r for r in self.masters['990102']['monsterM']}
        source, destination = masters[monster['monsterId']], masters[evolution['nextMonsterId']]
        group = next(r for r in self.masters['990103']['monsterM']
                     if r['monsterGroupId'] == destination['monsterGroupId'])
        step = {'5': '5', '6': '6', '7': '7', '11': '5', '12': '6', '13': '7'}.get(group['growStep'])
        recipe = next(r for r in self.masters['990115']['monsterEvolutionMaterialM']
                      if r['monsterEvolutionMaterialId'] == evolution['monsterEvolutionMaterialId'])
        if (not step or source['rare'] != destination['rare'] or
                source['maxLevel'] != destination['maxLevel'] or
                any(int(recipe['materialAssetNum' + str(i)]) for i in range(1, 8))):
            raise ProtocolError('This evolution form is not implemented')
        code = self.masters['990701']['codeM']
        cost = int(code['modeChangeCoefficientFor' + step]) * int(code['modeChangeCoefficientRare' + destination['rare']])
        if int(state['player']['gamemoney']) < cost:
            raise ProtocolError('Not enough mode change money')
        skill = next((r for r in self.masters['990108']['skillM']
                      if r['skillGroupId'] == destination['skillGroupId'] and
                      r['skillGroupSubId'] == monster['defaultSkillGroupSubId']), None)
        if not skill:
            raise ProtocolError('Evolution skill is missing')
        resistance = self.evolution_resistance_values(monster, destination)
        state['player']['gamemoney'] = str(int(state['player']['gamemoney']) - cost)
        monster.update(monsterId=destination['monsterId'], uniqueSkillId=skill['skillId'])
        monster.update(resistance)
        self.add_monster_experience(monster, 0)
        state.setdefault('mode_change_receipts', {})[monster['userMonsterId']] = {
            'baseMonsterId': source['monsterId'], 'targetMonsterId': destination['monsterId'],
            'routeId': evolution['monsterEvolutionId']}
        state['normal_mode_change_count'] = state.get('normal_mode_change_count', 0) + 1
        return {'result': 1, 'userMonster': monster, 'reviewStatus': 0}


    def fuse_monsters(self, state, param):
        self.training_context(state)
        ident, ids = param.get('baseMonster'), param.get('materialMonster')
        if (not isinstance(ident, str) or not isinstance(ids, list) or not 1 <= len(ids) <= 5 or
                any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids) or ident in ids):
            raise ProtocolError('Invalid fusion material list')
        operation = json.dumps(['010201', ident, sorted(ids)], separators=(',', ':'))
        fusion_type = self.local_gameplay['fusion']['normalFusionType']
        receipt = self.training_receipt(state, operation, ident, 'fusionType', fusion_type)
        if receipt:
            return receipt
        base = self.training_monster(state, ident)
        materials = [self.training_monster(state, i, material=True) for i in ids]
        masters = {r['monsterId']: r for r in self.masters['990102']['monsterM']}
        groups = {r['monsterGroupId']: r for r in self.masters['990103']['monsterM']}
        amount, cost, luck_chance = 0, 0, 0
        tribe = groups[masters[base['monsterId']]['monsterGroupId']]['tribe']
        rates = {'4': 7, '10': 7, '5': 20, '8': 20, '11': 20,
                 '6': 50, '12': 50, '7': 100, '9': 100, '13': 100}
        for material in materials:
            master = masters[material['monsterId']]
            exp = int(master['fusionExp']) + int(master['fusionExpRise']) * (int(material['level']) - 1)
            amount += exp
            cost += (exp + 70) * int(self.masters['990701']['codeM']['reinforcementCoefficient'])
            group = groups[master['monsterGroupId']]
            if group['tribe'] == tribe:
                luck_chance = max(luck_chance, rates.get(group['growStep'], 0))
        if int(state['player']['gamemoney']) < cost:
            raise ProtocolError('Not enough fusion money')
        self.add_monster_experience(base, amount)
        maximum = int(masters[base['monsterId']]['maxLuck'])
        if int(base['luck']) < maximum and luck_chance and secrets.randbelow(100) < luck_chance:
            base['luck'] = str(min(maximum, int(base['luck']) + self.local_gameplay['fusion']['luckGainOnSuccess']))
        state['player']['gamemoney'] = str(int(state['player']['gamemoney']) - cost)
        self.consume_monsters(state, ids, operation)
        state.setdefault('training_operations', {})[operation] = {'api': '010201', 'cost': cost, 'experience': amount}
        state['normal_fusion_count'] = state.get('normal_fusion_count', 0) + 1
        return {'fusionType': fusion_type, 'userMonster': base}

    def inherit_skill(self, state, param):
        self.training_context(state)
        ident, material_id = param.get('baseUserMonsterId'), param.get('materialUserMonsterId')
        target, source = param.get('baseCommonSkillNumber'), param.get('materialCommonSkillNumber')
        if (not isinstance(ident, str) or not isinstance(material_id, str) or ident == material_id or
                any(type(i) is not int or i not in (1, 2) for i in (target, source))):
            raise ProtocolError('Invalid skill inheritance request')
        operation = json.dumps(['010601', ident, material_id, target, source], separators=(',', ':'))
        receipt = self.training_receipt(state, operation, ident, 'result', 1)
        if receipt:
            return receipt
        base = self.training_monster(state, ident)
        material = self.training_monster(state, material_id, material=True)
        masters = {r['monsterId']: r for r in self.masters['990102']['monsterM']}
        bm, mm = masters[base['monsterId']], masters[material['monsterId']]
        if int(material['level']) != int(mm['maxLevel']):
            raise ProtocolError('Inheritance material requires maximum level')
        if (target == 2 and int(bm['rare']) < 6) or (source == 2 and int(mm['rare']) < 6):
            raise ProtocolError('Second inherited skill slot requires version upgrade')
        field = 'commonSkillId' if target == 1 else 'extraCommonSkillId'
        skill_id = material.get('commonSkillId' if source == 1 else 'extraCommonSkillId', '')
        skills = {r['skillId']: r for r in self.masters['990108']['skillM']}
        if skill_id not in skills or skills[skill_id]['type'] not in ('2', '3'):
            raise ProtocolError('Material has no valid inherited skill in that slot')
        if base.get(field) == skill_id:
            raise ProtocolError('Inherited skill is already in the selected slot')
        # CMD_Succession prices the material's FIRST common skill even when the
        # selected source is slot 2. Match its actual confirmation amount.
        first_skill = skills.get(material.get('commonSkillId'))
        if not first_skill:
            raise ProtocolError('Material has no valid inherited skill in that slot')
        code = self.masters['990701']['codeM']
        cost = ((int(bm['rare']) - 1 + int(code['successionBaseCoefficient'])) *
                int(first_skill['inheritancePrice']) *
                (int(mm['rare']) - 1 + int(code['successionPartnerCoefficient'])) + int(code['successionCoefficient']))
        if int(state['player']['gamemoney']) < cost:
            raise ProtocolError('Not enough inheritance money')
        state['player']['gamemoney'] = str(int(state['player']['gamemoney']) - cost)
        base[field] = skill_id
        self.consume_monsters(state, [material_id], operation)
        state.setdefault('training_operations', {})[operation] = {'api': '010601', 'cost': cost, 'skillId': skill_id}
        state['normal_inheritance_count'] = state.get('normal_inheritance_count', 0) + 1
        return {'result': 1, 'userMonster': base}

    def friendship_monster(self, state, ident):
        monster = next((m for m in state['monsters'] if m['userMonsterId'] == ident), None)
        if (not isinstance(ident, str) or not monster or monster['eggFlg'] != '0' or
                monster.get('growEndDate') or self.monster_growth_step(monster) in ('1', '2', '3')):
            raise ProtocolError('Invalid farm friendship monster')
        return monster

    def friendship_remaining(self, state, ident):
        return max(0, int(state.get('friendship_ready_at', {}).get(ident, 0)) - int(time.time()))

    def friendship_times(self, state, param):
        ids = param.get('userMonsterIds')
        if (not isinstance(ids, list) or len(ids) > 3 or any(not isinstance(i, str) for i in ids) or
                len([i for i in ids if i != '0']) != len(set(ids) - {'0'})):
            raise ProtocolError('Invalid friendship monster list')
        owned = {m['userMonsterId'] for m in state['monsters']}
        if any(ident not in owned and ident != '0' for ident in ids):
            raise ProtocolError('Invalid friendship monster list')
        if not ids or state['tutorial']['endFlg'] != '1':
            return {'friendshipTime': None}
        for ident in ids:
            if ident != '0':
                self.friendship_monster(state, ident)
        # FarmDigimonManager always indexes three rows when this is non-null.
        return {'friendshipTime': [{'userMonsterId': ident,
                 'nextTimeSec': self.friendship_remaining(state, ident) if ident != '0' else 0}
                 for ident in ids + ['0'] * (3 - len(ids))]}

    def tap_friendship(self, state, param):
        if state['tutorial']['endFlg'] != '1':
            raise ProtocolError('Farm friendship is available after the tutorial')
        monster = self.friendship_monster(state, param.get('userMonsterId'))
        ident = monster['userMonsterId']
        deck = next((d for d in state['decks']['deckList'] if d['deckNum'] == state['decks']['favoriteDeckNum']), None)
        if not deck or not any(r['userMonsterId'] == ident for r in deck['monsterList']):
            raise ProtocolError('Friendship monster must be in the favorite farm deck')
        if state.get('active_battle') and not state['active_battle']['settled']:
            raise ProtocolError('Finish the active battle before farm friendship')
        remaining = self.friendship_remaining(state, ident)
        maximum = int(next(r for r in self.masters['990112']['monsterGrowStepM']
                           if r['monsterGrowStepId'] == self.monster_growth_step(monster))['maxFriendship'])
        gain = min(self.local_gameplay['friendship']['gainPerTap'], max(0, maximum - int(monster['friendship']))) if not remaining else 0
        up_status = None
        if gain:
            before = self.friendship_bonus(monster)
            monster['friendship'] = str(int(monster['friendship']) + gain)
            after = self.friendship_bonus(monster)
            increases = {k: after[k] - before[k] for k in after}
            threshold = int(self.masters['990701']['codeM']['riseConditionFriendShipStatus'])
            if int(monster['friendship']) // threshold != (int(monster['friendship']) - gain) // threshold:
                up_status = increases
                for field, increment in increases.items():
                    monster[field] = str(int(monster[field]) + increment)
            remaining = self.local_gameplay['friendship']['cooldownSeconds']
            state.setdefault('friendship_ready_at', {})[ident] = int(time.time()) + remaining
            state['farm_friendship_count'] = state.get('farm_friendship_count', 0) + 1
        return {'upFriendship': gain, 'nextTimeSec': remaining, 'upStatus': up_status,
                'userMonster': monster}

    def grant_fixed_monster(self, account, state, reward):
        extra = json.loads(reward['exValue'])
        fixed = next((r for r in self.masters['990116']['monsterFixedValueM']
                      if r['monsterFixedValueId'] == str(extra.get('monsterFixedValueId'))), None)
        if not fixed or fixed['monsterId'] != reward['assetValue']:
            raise ProtocolError('Fixed monster reward is missing or mismatched')
        if len(state['monsters']) >= int(state['player']['unitLimitMax']):
            raise ProtocolError('Monster inventory is full')
        master = next(r for r in self.masters['990102']['monsterM'] if r['monsterId'] == fixed['monsterId'])
        level = int(fixed['level'])
        levels = {r['level']: int(r['experienceNum']) for r in self.masters['990106']['monsterExperienceM']}
        skill = next(r for r in self.masters['990108']['skillM']
                     if r['skillGroupId'] == master['skillGroupId'] and r['skillGroupSubId'] == fixed['defaultSkillGroupSubId'])
        ident = self.next_monster_id(state)
        monster = {k: str(v) for k, v in fixed.items() if k != 'monsterFixedValueId'}
        monster.update(userMonsterId=str(ident), userId=str(account['id']), ex=str(levels[str(level)]),
            levelEx='0', nextLevelEx=str(levels[str(level + 1)] - levels[str(level)]) if level < int(master['maxLevel']) else '0',
            uniqueSkillId=skill['skillId'], extraCommonSkillId='', eggFlg='0', growEndDate='',
            monsterEvolutionRouteId='0', speed=master['speed'], friendship='0', statusFlgs='0',
            tranceResistance='', tranceStatusAilment='', createTimeSec=str(int(time.time())))
        # A fixed infant/growth reward can identify one exact original birth
        # route. Later forms and ambiguous matches keep their unknown origin;
        # never infer an arbitrary family or rewrite an already-owned monster.
        births = [r for r in self.masters['990110']['monsterEvolutionRouteM']
                  if fixed['monsterId'] in (r['childhood1MonsterId'], r['childhood2MonsterId'], r['growthMonsterId'])]
        if len(births) == 1:
            monster['monsterEvolutionRouteId'] = births[0]['monsterEvolutionRouteId']
        if monster['commonSkillId'] == '0':
            monster['commonSkillId'] = '473'
        for field in ('hp', 'attack', 'defense', 'spAttack', 'spDefense'):
            suffix = field[0].upper() + field[1:]
            low, high = int(master['default' + suffix]), int(master['max' + suffix])
            monster[field] = str(int(low + (high - low) * (level - 1) / max(1, int(master['maxLevel']) - 1) + 0.5))
        state['monsters'].append(monster)
        state['chip_slots'].append({'userMonsterId': ident, 'equip': [], 'manage': {
            'slotNum': 0, 'extraSlotNum': 0, 'maxSlotNum': 5, 'maxExtraSlotNum': int(extra.get('maxExtraSlotNum', 0))}})

    def normal_capture(self, account, state, param):
        ident, count, claimed_balance = (param.get(k) for k in ('gachaId', 'playCount', 'itemCount'))
        if (any(type(v) is not int for v in (ident, count, claimed_balance)) or
                ident not in (10001, 10002) or count not in (1, 10) or claimed_balance < 0):
            raise ProtocolError('Invalid local capture price or count')
        if len(state['monsters']) + count > int(state['player']['unitLimitMax']):
            raise ProtocolError('Monster inventory is full')
        key = 'point' if ident == 10001 else 'friendPoint'
        balance = int(state['player'][key])
        # GashaStartButtonEvent sends current inventory, not the price.
        if claimed_balance != balance:
            raise ProtocolError('Capture balance changed; reopen the capture menu')
        cost = (5 if ident == 10001 else 300) * count
        if balance < cost:
            raise ProtocolError('Not enough capture currency')
        # Explicit local equal-weight pools, not recovered production odds.
        pool = ('81', '121', '176', '196') if ident == 10001 else ('1', '6', '16')
        seen = set(state.get('obtained_collections', []))
        result = []
        for _ in range(count):
            mid = secrets.choice(pool)
            route_field = 'growthMonsterId' if ident == 10001 else 'childhood1MonsterId'
            route = next(r for r in self.masters['990110']['monsterEvolutionRouteM'] if r[route_field] == mid)
            master = next(r for r in self.masters['990102']['monsterM'] if r['monsterId'] == mid)
            monster = self.create_capture_monster(account, state, route, master)
            self.roll_capture_talent(monster)
            collection = self.monster_collections[mid]
            result.append(dict(monster, isNew=int(collection not in seen)))
            seen.add(collection)
        state['player'][key] = balance - cost if key == 'point' else str(balance - cost)
        state['normal_capture_count'] = state.get('normal_capture_count', 0) + count
        return {'userMonsterList': result, 'rewards': None}

    def roll_capture_talent(self, monster):
        weights = self.local_gameplay['captureTalents']['probabilityBasisPoints']
        if set(weights) != {'none', 'silver', 'gold'} or any(type(v) is not int or v < 0 for v in weights.values()) or sum(weights.values()) != 10000:
            raise ProtocolError('Invalid local capture talent probabilities')
        roll = secrets.randbelow(10000)
        if roll < weights['none']:
            return
        gold = roll >= weights['none'] + weights['silver']
        field = ABILITY_FIELDS[secrets.randbelow(len(ABILITY_FIELDS))]
        monster[field + 'Ability'] = '15' if gold else '5'
        monster[field + 'AbilityFlg'] = '1' if gold else '2'
        self.add_monster_experience(monster, 0)

    def create_capture_monster(self, account, state, route, master):
        skill = next(r for r in self.masters['990108']['skillM']
                     if r['skillGroupId'] == master['skillGroupId'] and r['skillGroupSubId'] == '1')
        ident = self.next_monster_id(state)
        monster = dict.fromkeys(('hpAbilityFlg hpAbility attackAbilityFlg attackAbility '
            'defenseAbilityFlg defenseAbility spAttackAbilityFlg spAttackAbility '
            'spDefenseAbilityFlg spDefenseAbility speedAbilityFlg speedAbility').split(), '0')
        monster.update(userMonsterId=str(ident), userId=str(account['id']),
            monsterId=master['monsterId'], level='1', ex='0', levelEx='0',
            nextLevelEx=next(r['experienceNum'] for r in self.masters['990106']['monsterExperienceM'] if r['level'] == '2'),
            leaderSkillId='0', uniqueSkillId=skill['skillId'], defaultSkillGroupSubId='1',
            commonSkillId='473', extraCommonSkillId='', eggFlg='0', growEndDate='',
            monsterEvolutionRouteId=route['monsterEvolutionRouteId'], speed=master['speed'],
            luck='0', friendship='0', statusFlgs='0', tranceResistance='',
            tranceStatusAilment='', createTimeSec=str(int(time.time())))
        for field in ('hp', 'attack', 'defense', 'spAttack', 'spDefense'):
            monster[field] = master['default' + field[0].upper() + field[1:]]
        state['monsters'].append(monster)
        state['chip_slots'].append({'userMonsterId': ident, 'equip': [], 'manage': {
            'slotNum': 0, 'extraSlotNum': 0, 'maxSlotNum': 5, 'maxExtraSlotNum': 0}})
        return monster

    def monster_growth_step(self, monster):
        master = next(r for r in self.masters['990102']['monsterM'] if r['monsterId'] == monster['monsterId'])
        group = next(r for r in self.masters['990103']['monsterM'] if r['monsterGroupId'] == master['monsterGroupId'])
        return group['growStep']

    def garden_route(self, monster):
        step = self.monster_growth_step(monster)
        field = {'2': 'childhood1MonsterId', '3': 'childhood2MonsterId'}.get(step)
        route = next((r for r in self.masters['990110']['monsterEvolutionRouteM']
                      if r['monsterEvolutionRouteId'] == monster['monsterEvolutionRouteId']), None)
        if not field or not route or route[field] != monster['monsterId'] or monster['eggFlg'] != '0':
            raise ProtocolError('Only routed childhood monsters can grow in the garden')
        return step, route

    def research_monsters(self, account, state, param):
        self.training_context(state)
        ident, material_id = param.get('baseUserMonsterId'), param.get('materialUserMonsterId')
        if not isinstance(ident, str) or not isinstance(material_id, str) or ident == material_id:
            raise ProtocolError('Invalid research request')
        operation = json.dumps(['010501', ident, material_id], separators=(',', ':'))
        receipt = state.get('training_operations', {}).get(operation)
        if receipt:
            egg = next((m for m in state['monsters'] if m['userMonsterId'] == receipt['newUserMonsterId']), None)
            if not egg:
                raise ProtocolError('Research offspring is no longer owned')
            return {'newUserMonsterId': egg['userMonsterId'], 'eggMonsterId': receipt['eggMonsterId'], 'userMonster': egg}
        # Research consumes BOTH objects; even the base must be out of decks
        # and unlocked, unlike reinforcement or talent inheritance.
        base = self.training_monster(state, ident, material=True)
        material = self.training_monster(state, material_id, material=True)
        if any(self.monster_growth_step(m) not in ('7', '9', '13') for m in (base, material)):
            raise ProtocolError('Research requires two ultimate monsters')
        if sum(bool(m.get('growEndDate')) or m['eggFlg'] != '0' for m in state['monsters']) >= int(self.masters['990701']['codeM']['maxChildMonster']):
            raise ProtocolError('Garden capacity is full')
        masters = {r['monsterId']: r for r in self.masters['990102']['monsterM']}
        bm, mm = masters[base['monsterId']], masters[material['monsterId']]
        route = next((r for r in self.masters['990110']['monsterEvolutionRouteM']
                      if r['monsterEvolutionRouteId'] == base.get('monsterEvolutionRouteId')), None)
        if not route:
            raise ProtocolError('Research requires a known birth route')
        maximum = int(next(r for r in self.masters['990112']['monsterGrowStepM']
                           if r['monsterGrowStepId'] == self.monster_growth_step(base))['maxFriendship'])
        rare = min(5, int(bm['rare']) + (int(base.get('friendship', '0')) >= maximum))
        family_fields = ('childhood1MonsterId', 'childhood2MonsterId', 'growthMonsterId')
        family = tuple(masters[route[field]]['monsterGroupId'] for field in family_fields)
        routes = [r for r in self.masters['990110']['monsterEvolutionRouteM']
                  if tuple(masters[r[field]]['monsterGroupId'] for field in family_fields) == family and
                  masters[r['childhood1MonsterId']]['rare'] == str(rare)]
        if len(routes) != 1:
            raise ProtocolError('Research offspring route is missing or ambiguous')
        route = routes[0]
        talents = self.talent_values(base)
        code = self.masters['990701']['codeM']
        cost = ((int(bm['rare']) - 1 + int(code['laboratoryBasePlusCoefficient'])) * int(code['laboratoryBaseCoefficient']) +
                (int(mm['rare']) - 1 + int(code['laboratoryPartnerPlusCoefficient'])) * int(code['laboratoryPartnerCoefficient']))
        if int(state['player']['gamemoney']) < cost:
            raise ProtocolError('Not enough research money')
        # Egg UI reads rare from monsterId and gets its model from the route.
        # Save the destined childhood-I master, with eggFlg=1, not rare1 egg M.
        egg = self.create_capture_monster(account, state, route, masters[route['childhood1MonsterId']])
        egg.update(eggFlg='1', leaderSkillId=base['leaderSkillId'], commonSkillId=base['commonSkillId'],
                   luck=base['luck'])
        for field, value in talents.items():
            egg[field + 'Ability'] = str(value)
            egg[field + 'AbilityFlg'] = base[field + 'AbilityFlg']
        self.add_monster_experience(egg, 0)
        state['player']['gamemoney'] = str(int(state['player']['gamemoney']) - cost)
        self.consume_monsters(state, [ident, material_id], operation)
        state.setdefault('training_operations', {})[operation] = {'api': '010501', 'cost': cost,
            'newUserMonsterId': egg['userMonsterId'], 'eggMonsterId': route['eggMonsterId']}
        state['normal_research_count'] = state.get('normal_research_count', 0) + 1
        return {'newUserMonsterId': egg['userMonsterId'], 'eggMonsterId': route['eggMonsterId'], 'userMonster': egg}

    def hatch_monster(self, state, param):
        self.training_context(state)
        ident = param.get('userMonsterId')
        monster = next((m for m in state['monsters'] if m['userMonsterId'] == ident), None)
        if not isinstance(ident, str) or not monster:
            raise ProtocolError('Invalid hatching monster')
        if ident in state.get('hatched_monsters', []):
            return {'userMonster': monster}
        route = next((r for r in self.masters['990110']['monsterEvolutionRouteM']
                      if r['monsterEvolutionRouteId'] == monster.get('monsterEvolutionRouteId')), None)
        if monster['eggFlg'] != '1' or not route or route['childhood1MonsterId'] != monster['monsterId']:
            raise ProtocolError('Invalid hatching monster')
        monster['eggFlg'] = '0'
        monster['growEndDate'] = datetime.fromtimestamp(int(time.time()) +
            self.local_gameplay['garden']['growthSeconds']['2'], timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        state.setdefault('hatched_monsters', []).append(ident)
        state['normal_hatching_count'] = state.get('normal_hatching_count', 0) + 1
        return {'userMonster': monster}

    def version_up_monster(self, state, param):
        self.training_context(state)
        ident, target, selected = (param.get(k) for k in ('baseUserMonsterId', 'target', 'material'))
        if (any(type(i) is not int or not 0 < i <= 2147483647 for i in (ident, target)) or
                not isinstance(selected, list) or not 1 <= len(selected) <= 7 or
                any(type(i) is not int or not 0 < i <= 2147483647 for i in selected)):
            raise ProtocolError('Invalid version upgrade request')
        ident = str(ident)
        operation = json.dumps(['010802', ident, target, selected], separators=(',', ':'))
        receipt = state.get('training_operations', {}).get(operation)
        if receipt:
            monster = next((m for m in state['monsters'] if m['userMonsterId'] == receipt['newUserMonsterId']), None)
            if not monster:
                raise ProtocolError('Version upgrade monster is no longer owned')
            return {'result': 1, 'userMonster': monster}
        # The client grays out party members; replacing the old owned ID also
        # requires the same lock/garden/special-material protection as research.
        base = self.training_monster(state, ident, material=True)
        masters = {r['monsterId']: r for r in self.masters['990102']['monsterM']}
        source, destination = masters[base['monsterId']], masters.get(str(target))
        route = next((r for r in self.masters['990104']['monsterEvolutionM']
                      if r['baseMonsterId'] == base['monsterId'] and r['nextMonsterId'] == str(target)
                      and r['type'] == '3' and r['effectType'] == '5'), None)
        if (not route or not destination or source['rare'] != '5' or destination['rare'] != '6' or
                source['monsterGroupId'] != destination['monsterGroupId'] or
                self.monster_growth_step(base) not in ('7', '9', '13')):
            raise ProtocolError('This version upgrade route is not implemented')
        if int(base['level']) != int(source['maxLevel']):
            raise ProtocolError('Version upgrade requires maximum level')
        recipe = next((r for r in self.masters['990115']['monsterEvolutionMaterialM']
                       if r['monsterEvolutionMaterialId'] == route['monsterEvolutionMaterialId']), None)
        if not recipe:
            raise ProtocolError('Version upgrade materials are not implemented')
        souls = {r['soulId']: r for r in self.masters['990201']['soulM']}
        slots = []
        for i in range(1, 8):
            sid, raw = recipe['materialAssetValue' + str(i)], recipe['materialAssetNum' + str(i)]
            if raw == '0':
                continue
            if (recipe['materialAssetCategoryId' + str(i)] != '14' or sid not in souls or
                    not isinstance(raw, str) or not raw.isdecimal() or int(raw) <= 0):
                raise ProtocolError('Version upgrade materials are not implemented')
            slots.append((sid, int(raw)))
        if len(slots) != len(selected):
            raise ProtocolError('Invalid version upgrade material selection')
        required = {}
        for (sid, count), chosen in zip(slots, selected):
            chosen = str(chosen)
            replacement = souls.get(chosen)
            if chosen != sid and not (souls[sid]['soulGroup'] == '3' and souls[sid]['priority'] != '2' and
                    replacement and replacement['soulGroup'] == '3' and replacement['priority'] == '2'):
                raise ProtocolError('Invalid version upgrade material selection')
            required[chosen] = required.get(chosen, 0) + count
        owned = {r['soulId']: r for r in state['souls']}
        if any(s not in owned or int(owned[s]['num']) < n for s, n in required.items()):
            raise ProtocolError('Not enough version upgrade materials')
        cost = 300 * 450  # EvolutionData.CalcClusterForVersionUp, independent of rare/species.
        if int(state['player']['gamemoney']) < cost:
            raise ProtocolError('Not enough version upgrade money')
        self.talent_values(base)
        skill = next((r for r in self.masters['990108']['skillM']
                      if r['skillGroupId'] == destination['skillGroupId'] and
                      r['skillGroupSubId'] == base['defaultSkillGroupSubId']), None)
        if not skill:
            raise ProtocolError('Version upgrade skill is missing')
        resistance = self.evolution_resistance_values(base, destination)
        upgraded = copy.deepcopy(base)
        new_id = self.next_monster_id(state)
        # Local rule preserves earned EXP/level and inherited attributes; the
        # recovered response contract does not specify production carryover.
        upgraded.update(userMonsterId=str(new_id), monsterId=str(target), uniqueSkillId=skill['skillId'],
                        extraCommonSkillId='', createTimeSec=str(int(time.time())))
        upgraded.update(resistance)
        self.add_monster_experience(upgraded, 0)
        ready = state.get('friendship_ready_at', {}).get(ident)
        self.consume_monsters(state, [ident], operation)
        state['monsters'].append(upgraded)
        state['chip_slots'].append({'userMonsterId': new_id, 'equip': [], 'manage': {
            'slotNum': 0, 'extraSlotNum': 0, 'maxSlotNum': 5, 'maxExtraSlotNum': 0}})
        if ready is not None:
            state['friendship_ready_at'][str(new_id)] = ready
        for sid, count in required.items():
            owned[sid]['num'] = str(int(owned[sid]['num']) - count)
        state['player']['gamemoney'] = str(int(state['player']['gamemoney']) - cost)
        state.setdefault('training_operations', {})[operation] = {'api': '010802', 'cost': cost,
            'materials': required, 'newUserMonsterId': str(new_id)}
        state['normal_version_up_count'] = state.get('normal_version_up_count', 0) + 1
        return {'result': 1, 'userMonster': upgraded}

    def garden_remaining(self, monster):
        deadline = datetime.strptime(monster['growEndDate'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        return max(0, int(deadline.timestamp()) - int(time.time()))

    def garden_info(self, state):
        config = self.local_gameplay['garden']
        rows = []
        for monster in state['monsters']:
            if not monster.get('growEndDate') or monster['eggFlg'] != '0':
                continue
            step, _ = self.garden_route(monster)
            remaining = self.garden_remaining(monster)
            unit = config['shorteningHoursPerStone'][step] * 3600
            rows.append({'userMonsterId': int(monster['userMonsterId']), 'growStep': int(step),
                         'remainingTime': remaining, 'stoneNum': (remaining + unit - 1) // unit})
        return {'gardenInfo': {'time1': config['shorteningHoursPerStone']['2'],
                               'time2': config['shorteningHoursPerStone']['3'], 'monster': rows}}

    def move_to_garden(self, state, param):
        if state['tutorial']['endFlg'] != '1':
            raise ProtocolError('Garden is available after the tutorial')
        ident = param.get('userMonsterId')
        monster = next((m for m in state['monsters'] if m['userMonsterId'] == ident), None)
        if not isinstance(ident, str) or not monster:
            raise ProtocolError('Invalid garden monster')
        step, _ = self.garden_route(monster)
        if monster.get('growEndDate'):
            # Reconnection must not restart a timer or consume another slot.
            return {'result': 1, 'userMonster': monster}
        if state.get('active_battle') and not state['active_battle']['settled']:
            raise ProtocolError('Finish the active battle before using the garden')
        if any(r['userMonsterId'] == ident for d in state['decks']['deckList'] for r in d['monsterList']):
            raise ProtocolError('Remove the garden monster from all decks first')
        limit = int(self.masters['990701']['codeM']['maxChildMonster'])
        if sum(bool(m.get('growEndDate')) or m['eggFlg'] != '0' for m in state['monsters']) >= limit:
            raise ProtocolError('Garden capacity is full')
        monster['growEndDate'] = datetime.fromtimestamp(int(time.time()) +
            self.local_gameplay['garden']['growthSeconds'][step], timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        return {'result': 1, 'userMonster': monster}

    def grow_in_garden(self, state, param):
        ident, shorten, stones = (param.get(k) for k in ('userMonsterId', 'shorteningFlg', 'stone'))
        monster = next((m for m in state['monsters'] if m['userMonsterId'] == ident), None)
        if (state['tutorial']['endFlg'] != '1' or not isinstance(ident, str) or not monster or
                not monster.get('growEndDate') or type(shorten) is not int or shorten not in (0, 1) or
                type(stones) is not int or stones < 0):
            raise ProtocolError('Invalid garden growth request')
        if state.get('active_battle') and not state['active_battle']['settled']:
            raise ProtocolError('Finish the active battle before using the garden')
        step, route = self.garden_route(monster)
        remaining = self.garden_remaining(monster)
        if remaining and not shorten:
            raise ProtocolError('Garden growth is not finished')
        unit = self.local_gameplay['garden']['shorteningHoursPerStone'][step] * 3600
        cost = (remaining + unit - 1) // unit
        if cost and stones != state['player']['point']:
            raise ProtocolError('Garden stone balance changed; reopen the garden')
        if cost > state['player']['point']:
            raise ProtocolError('Not enough garden stones')
        target = route['childhood2MonsterId' if step == '2' else 'growthMonsterId']
        destination = next(r for r in self.masters['990102']['monsterM'] if r['monsterId'] == target)
        skill = next((r for r in self.masters['990108']['skillM']
                      if r['skillGroupId'] == destination['skillGroupId'] and
                      r['skillGroupSubId'] == monster['defaultSkillGroupSubId']), None)
        if not skill:
            raise ProtocolError('Garden growth skill is missing')
        state['player']['point'] -= cost
        monster.update(monsterId=target, ex='0', speed=destination['speed'], uniqueSkillId=skill['skillId'])
        self.add_monster_experience(monster, 0)
        monster['growEndDate'] = (datetime.fromtimestamp(int(time.time()) +
            self.local_gameplay['garden']['growthSeconds']['3'], timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            if step == '2' else '')
        state['normal_garden_growth_count'] = state.get('normal_garden_growth_count', 0) + 1
        return {'result': 1, 'useStone': cost, 'userMonster': monster}

    def start_normal_battle(self, param, state):
        refresh_battle_expiry(state)
        ident = param.get('dungeonId')
        if not isinstance(ident, str) or ident not in self.normal_world:
            raise ProtocolError('Normal dungeon encounter data has not been reconstructed yet')
        if param.get('userDungeonTicketId') not in (None, '', '0'):
            raise ProtocolError('Normal dungeon does not use tickets')
        info = next(d for s in self.normal_world_info(state)['worldDungeonInfo'] for d in s['dungeons']
                    if str(d['worldDungeonId']) == ident)
        if info['status'] == 1:
            raise ProtocolError('Dungeon prerequisites have not been cleared')
        deck = next((d for d in state['decks']['deckList'] if d['deckNum'] == param.get('deckNum')), None)
        owned = {m['userMonsterId']: m for m in state['monsters']}
        slots = [r['userMonsterId'] for r in deck['monsterList']] if deck else []
        ids = [ident for ident in slots if ident != '0']
        if not 1 <= len(slots) <= 3 or not ids or len(set(ids)) != len(ids) or any(
                i not in owned or owned[i]['eggFlg'] != '0' or owned[i].get('growEndDate') or
                self.monster_growth_step(owned[i]) in ('1', '2', '3') for i in ids):
            raise ProtocolError('Invalid battle deck')
        old = state.get('active_battle')
        if old and not old['settled']:
            if old['response']['worldDungeonId'] != ident or old['response']['deck']['userMonsterIds'] != ids:
                raise ProtocolError('Finish the active battle before starting another')
            return old['response']
        dungeon = next(d for d in self.masters['990905']['worldDungeonM'] if d['worldDungeonId'] == ident)
        rewards = [r for r in self.masters['990909']['worldDungeonRewardM'] if r['worldDungeonId'] == ident]
        if any(r['assetCategoryId'] not in ('1', '2', '16') for r in rewards):
            raise ProtocolError('This dungeon has first-clear rewards that are not implemented yet')
        monster_count = sum(int(r['assetNum']) for r in rewards if r['assetCategoryId'] == '1'
                            and (r['everyTimeFlg'] != '0' or r['worldDungeonRewardId'] not in state.get('claimed_dungeon_rewards', [])))
        if len(state['monsters']) + monster_count > int(state['player']['unitLimitMax']):
            raise ProtocolError('Make room for the dungeon monster reward before entering')
        cost = int(dungeon['needStamina'])
        if int(state['player']['stamina']) < cost:
            raise ProtocolError('Not enough stamina')
        config = self.normal_world[ident]
        materials = self.normal_material_drops(config)
        friendship = self.battle_friendship_rules()
        floors = []
        for wave in range(1, int(dungeon['battleNum']) + 1):
            enemies = []
            for index, mid in enumerate(config['enemyMonsterIds'], 1):
                master = next(m for m in self.masters['990102']['monsterM'] if m['monsterId'] == mid)
                enemy = {'worldEnemyId': str(int(ident) * 1000 + wave * 10 + index), 'monsterId': mid,
                    'type': 1, 'level': config.get('enemyLevel', 1), 'speed': int(master['speed']), 'resistanceId': master['resistanceId'],
                    'fixedExp': config['fixedExpPerEnemy'], 'fixedMoney': config['fixedMoneyPerEnemy'],
                    'drop': [], 'chipIdList': [], 'ai': [{'type': 1, 'minHpRange': 0, 'maxHpRange': 100,
                    'priority': 1, 'rate': 10000, 'lookTarget': 1, 'lookStatus': 1, 'lookType': 1,
                    'invokeMinRange': 0, 'invokeMaxRange': 100, 'skillId': '571'}]}
                for field in ('hp', 'attack', 'defense', 'spAttack', 'spDefense'):
                    suffix = field[0].upper() + field[1:]
                    low, high = int(master['default' + suffix]), int(master['max' + suffix])
                    enemy[field] = int(low + (high - low) * (enemy['level'] - 1) / max(1, int(master['maxLevel']) - 1) + 0.5)
                if index == 1:
                    enemy['drop'] = [{'dropBoxType': r['dropBoxType']} for r in materials]
                enemies.append(enemy)
            floors.append({'floorId': str(int(ident) * 100 + wave), 'floorNum': wave,
                           'floorType': 1, 'healingRate': 0, 'cameraType': 0, 'enemy': enemies})
        response = {'startId': str(secrets.randbelow(2_000_000_000) + 1), 'worldDungeonId': ident,
                    'luckDrop': None, 'deck': {'userMonsterIds': ids}, 'dungeonFloor': floors}
        if old:
            state.setdefault('battle_history', {})[old['response']['startId']] = old
            compact_finished_battle(old)
        player = state['player']
        previous, maximum, now = int(player['stamina']), int(player['staminaMax']), int(time.time())
        player['stamina'] = previous - cost
        if player['stamina'] >= maximum:
            player['recovery'] = 0
            state.pop('stamina_full_at', None)
        else:
            # Crossing down from banked over-cap stamina starts recovery only
            # for the missing amount. Below-cap debits retain the partial point.
            state['stamina_full_at'] = (now + (maximum - player['stamina']) * 180 if previous >= maximum else
                                       max(now, state.get('stamina_full_at', 0)) + cost * 180)
            player['recovery'] = state['stamina_full_at'] - now
        state['active_battle'] = {'response': response, 'started': timestamp(), 'settled': False,
                                  'kind': 'normal', 'staminaCost': cost,
                                  'alive_slot_indices': [index for index, ident in enumerate(slots) if ident != '0'],
                                  'expires_at': int(time.time()) + NORMAL_BATTLE_LIFETIME_SECONDS,
                                  'friendship_rules': friendship,
                                  'link_points': int(dungeon['linkPoint']),
                                  'material_drops': [dict(r) for _ in floors for r in materials]}
        state['active_battle']['chip_snapshot'] = {
            ident: {'monster': self.chip_inventory.project_monster(state, next(m for m in state['monsters'] if m['userMonsterId'] == ident)),
                    'chips': copy.deepcopy([c for c in state['chips'] if str(c['userMonsterId']) == ident]),
                    'slot': copy.deepcopy(next(r for r in state['chip_slots'] if str(r['userMonsterId']) == ident))}
            for ident in ids}
        return response

    def update_picturebook(self, state):
        seen = {m['monsterId'] for m in state['monsters']}
        evolution = state.get('tutorial_evolution')
        if evolution:
            seen.update((evolution['fromMonsterId'], evolution['toMonsterId']))
        obtained = set(state.get('obtained_collections', []))
        obtained.update(self.monster_collections[m] for m in seen if m in self.monster_collections)
        obtained.discard('0')
        state['obtained_collections'] = sorted(obtained, key=int)

    def check_facility_release(self, state, release_id):
        if release_id == '0':
            return
        conditions = [r for r in self.masters['990308']['facilityConditionM'] if r['releaseId'] == release_id]
        if not conditions or any(r['conditionType'] != '1' or r['operatorType'] != '2' or
                int(state.get('facility_keys', {}).get(r['conditionParam'], 0)) < int(r['conditionValue'])
                for r in conditions):
            raise ProtocolError('Facility is not unlocked')

    def pay_facility_cost(self, state, master, prefix):
        costs = {'2': 0, '4': 0}
        for slot in range(1, 4):
            amount = int(master[prefix + 'AssetNum' + str(slot)])
            category = master[prefix + 'AssetCategoryId' + str(slot)]
            if amount < 0:
                raise ProtocolError('Facility cost type is not implemented')
            if amount:
                if category not in costs or master[prefix + 'AssetValue' + str(slot)] != '1':
                    raise ProtocolError('Facility cost type is not implemented')
                costs[category] += amount
        if int(state['player']['gamemoney']) < costs['4']:
            raise ProtocolError('Not enough money for facility')
        if state['player']['point'] < costs['2']:
            raise ProtocolError('Not enough stones for facility')
        state['player']['gamemoney'] = str(int(state['player']['gamemoney']) - costs['4'])
        state['player']['point'] -= costs['2']

    def refresh_meat_capacity(self, state):
        warehouse = next((r for r in state['facilities'] if r['facilityId'] == 2 and r['level'] > 0), None)
        capacity = int(next(r for r in self.masters['990304']['facilityWarehouseM']
                           if r['level'] == str(warehouse['level']))['limitMeatNum']) if warehouse else 50
        state['player']['meatLimitMax'] = str(capacity)
        return capacity

    def check_facility_placement(self, state, ident, x, y, exclude_owned_id=None):
        field = self.farm_layout['fields'][0]
        def cells(facility_id, anchor_x, anchor_y):
            footprint = self.farm_layout['footprints'].get(str(facility_id))
            if not footprint:
                raise ProtocolError('Facility footprint is not available')
            left = anchor_x - footprint['baseGridPositionX']
            top = anchor_y - footprint['baseGridPositionY']
            return {(column, row) for row in range(top, top + footprint['sizeY'])
                    for column in range(left, left + footprint['sizeX'])}
        placement = cells(ident, x, y)
        invalid = set(field['invalid'])
        if any(not (0 <= column < field['width'] and 0 <= row < field['height']) or
               row * field['width'] + column in invalid for column, row in placement):
            raise ProtocolError('Facility footprint is outside buildable farm tiles')
        for existing in state['facilities']:
            if exclude_owned_id is not None and existing.get('userFacilityId') == exclude_owned_id:
                continue
            occupied = cells(existing['facilityId'], existing['positionX'], existing['positionY'])
            if placement & occupied:
                raise ProtocolError('Facility footprint overlaps an existing facility')

    def next_facility_id(self, state):
        # Sold IDs must never be reused: durable sell receipts refer to them.
        ids = [r['userFacilityId'] for r in state['facilities'] + state['stock_facilities']]
        ids.extend(int(i) for i in state.get('facility_sales', {}))
        ident = max([state.get('facility_id_sequence', 0)] + ids) + 1
        state['facility_id_sequence'] = ident
        return ident

    def facility_master(self, ident):
        return next((r for r in self.masters['990301']['facilityM'] if r['facilityId'] == str(ident)), None)

    def finish_facility(self, state, facility):
        facility['level'] += 1
        facility['completeTime'] = ''
        if facility['facilityId'] == 1:
            ident = facility['userFacilityId']
            state['last_harvest'] = [r for r in state['last_harvest'] if r['userFacilityId'] != ident]
            state['last_harvest'].append({'userFacilityId': ident, 'lastHarvestTime':
                datetime.fromtimestamp(time.time() + 2, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')})
        self.refresh_meat_capacity(state)
        if facility['facilityId'] == 3:
            # Native FarmObject_Restaurant.BuildComplete raises the maximum and
            # recovers up to it. Apply only on this durable level transition,
            # never on a list read/reconnect/replayed completion. Keep over-cap.
            master = next(r for r in self.masters['990306']['facilityRestaurantM']
                          if r['level'] == str(facility['level']))
            player = state['player']
            player['staminaMax'] = master['maxStamina']
            player['stamina'] = max(int(player['stamina']), int(master['maxStamina']))
            player['recovery'] = 0
            state.pop('stamina_full_at', None)
        elif facility['facilityId'] == 7:
            master = next(r for r in self.masters['990307']['facilityHouseM']
                          if r['level'] == str(facility['level']))
            state['player']['unitLimitMax'] = master['maxMonsterNum']
        elif facility['facilityId'] == 25:
            self.chip_inventory.refresh_capacity(state)

    def shorten_facility(self, state, param, upgrade):
        ident = param.get('userFacilityId')
        facility = next((r for r in state['facilities'] if r['userFacilityId'] == ident), None)
        if (state['tutorial']['endFlg'] != '1' or type(ident) is not int or not facility):
            raise ProtocolError('Invalid facility shortening')
        master = self.facility_master(facility['facilityId'])
        if not master or (facility['facilityId'] not in (1, 2, 3, 4, 7) and master['type'] != '2'):
            raise ProtocolError('Invalid facility shortening')
        if not facility['completeTime']:
            # Reauthentication loses the transport retry cache. A completed
            # operation must not charge or raise the level a second time.
            receipt = state.get('facility_shortening', {}).get(str(ident))
            if receipt and receipt['upgrade'] == upgrade and receipt['level'] == facility['level']:
                return {'num': 0}
            raise ProtocolError('Facility has no matching construction to shorten')
        if (facility['level'] > 0) != upgrade:
            raise ProtocolError('Facility has no matching construction to shorten')
        if upgrade:
            # BuildCostLabel deliberately uses the current stored level here.
            master = next((r for r in self.masters['990302']['facilityUpgradeM']
                           if r['facilityId'] == str(facility['facilityId']) and
                           r['level'] == str(facility['level'])), None)
        if not master or master['shorteningAssetCategoryId1'] != '2' or any(
                int(master['shorteningAssetNum' + str(slot)]) for slot in (2, 3)):
            raise ProtocolError('Facility shortening cost type is not implemented')
        deadline = datetime.strptime(facility['completeTime'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).timestamp()
        # Match FarmUtility's integer seconds, charging two stones per started
        # hour. Clamp expired deadlines to zero instead of negative C# division.
        remaining = max(0, int(deadline - time.time()))
        cost = 2 * ((remaining + 3599) // 3600)
        if state['player']['point'] < cost:
            raise ProtocolError('Not enough stones for facility shortening')
        state['player']['point'] -= cost
        self.finish_facility(state, facility)
        state.setdefault('facility_shortening', {})[str(ident)] = {'upgrade': upgrade, 'level': facility['level']}
        return {'num': cost}

    def move_facility(self, state, param):
        ident, x, y = (param.get(k) for k in ('userFacilityId', 'positionX', 'positionY'))
        if (state['tutorial']['endFlg'] != '1' or type(ident) is not int or not 0 < ident <= 2147483647 or
                any(type(v) is not int or not 0 <= v <= 2147483647 for v in (x, y))):
            raise ProtocolError('Invalid facility move')
        facility = next((r for r in state['facilities'] if r['userFacilityId'] == ident), None)
        from_stock = facility is None
        if from_stock and str(ident) in state.get('pending_facility_placements', []):
            facility = next((r for r in state['stock_facilities'] if r['userFacilityId'] == ident), None)
        if not facility:
            raise ProtocolError('Unknown owned facility or placement was not requested')
        master = self.facility_master(facility['facilityId'])
        if not master or master['movingFlg'] != '1':
            raise ProtocolError('Facility cannot be moved')
        self.check_facility_placement(state, facility['facilityId'], x, y, exclude_owned_id=ident)
        facility.update(positionX=x, positionY=y)
        if from_stock:
            state['stock_facilities'].remove(facility)
            state['facilities'].append(facility)
            state['pending_facility_placements'].remove(str(ident))
        return {}

    def arrange_facilities(self, state, param):
        positions = param.get('userFacilityList')
        if state['tutorial']['endFlg'] != '1' or not isinstance(positions, list):
            raise ProtocolError('Invalid farm arrangement')
        owned = {r['userFacilityId']: r for r in state['facilities']}
        seen = set()
        for row in positions:
            if (not isinstance(row, dict) or any(type(row.get(k)) is not int or
                    not 0 <= row[k] <= 2147483647 for k in ('userFacilityId', 'positionX', 'positionY')) or
                    row['userFacilityId'] not in owned or row['userFacilityId'] in seen):
                raise ProtocolError('Invalid farm arrangement')
            seen.add(row['userFacilityId'])
        if seen != set(owned):
            raise ProtocolError('Farm arrangement must include every placed facility exactly once')
        candidate = copy.deepcopy(state['facilities'])
        by_id = {r['userFacilityId']: r for r in candidate}
        for row in positions:
            facility = by_id[row['userFacilityId']]
            master = self.facility_master(facility['facilityId'])
            if not master or (master['movingFlg'] != '1' and
                    (facility['positionX'], facility['positionY']) != (row['positionX'], row['positionY'])):
                raise ProtocolError('Facility cannot be moved')
            facility.update(positionX=row['positionX'], positionY=row['positionY'])
        # Validate the final layout, so swaps don't collide with old positions.
        for facility in candidate:
            self.check_facility_placement({'facilities': candidate}, facility['facilityId'],
                                          facility['positionX'], facility['positionY'], facility['userFacilityId'])
        state['facilities'] = candidate
        return {}

    def stock_facility(self, state, param):
        ident, stock = param.get('userFacilityId'), param.get('stockFlg')
        if (state['tutorial']['endFlg'] != '1' or type(ident) is not int or not 0 < ident <= 2147483647 or
                type(stock) is not int or stock not in (0, 1)):
            raise ProtocolError('Invalid facility stock request')
        active = next((r for r in state['facilities'] if r['userFacilityId'] == ident), None)
        stored = next((r for r in state['stock_facilities'] if r['userFacilityId'] == ident), None)
        facility = active or stored
        if not facility:
            raise ProtocolError('Unknown owned facility')
        master = self.facility_master(facility['facilityId'])
        if not master or master['stockFlg'] != '1':
            raise ProtocolError('Facility cannot be stored')
        if facility['completeTime'] or facility['level'] < 1:
            raise ProtocolError('Finish construction before storing or selling')
        pending = state.setdefault('pending_facility_placements', [])
        if stock == 1:
            if active:
                state['facilities'].remove(active)
                state['stock_facilities'].append(active)
            if str(ident) in pending:
                pending.remove(str(ident))
        elif stored and str(ident) not in pending:
            # The original client sends 140015(0), then 140005 separately.
            # Keep it in stock until valid coordinates are confirmed; reconnects
            # and failed moves must never create a phantom or lose the decoration.
            pending.append(str(ident))
        return {'result': 1}

    def sell_facility(self, state, param):
        ident = param.get('userFacilityId')
        if state['tutorial']['endFlg'] != '1' or type(ident) is not int or not 0 < ident <= 2147483647:
            raise ProtocolError('Invalid facility sale')
        sales = state.get('facility_sales', {})
        if str(ident) in sales:
            return {'sellPrice': sales[str(ident)]['sellPrice']}
        source = state['facilities']
        facility = next((r for r in source if r['userFacilityId'] == ident), None)
        if not facility:
            source = state['stock_facilities']
            facility = next((r for r in source if r['userFacilityId'] == ident), None)
        if not facility:
            raise ProtocolError('Unknown owned facility')
        master = self.facility_master(facility['facilityId'])
        if not master or master['sellFlg'] != '1':
            raise ProtocolError('Facility cannot be sold')
        if facility['completeTime'] or facility['level'] < 1:
            raise ProtocolError('Finish construction before storing or selling')
        balance = int(state['player']['gamemoney'])
        maximum = int(self.masters['990701']['codeM']['maxClusterCount'])
        credited = min(int(master['sellPrice']), max(0, maximum - balance))
        state['player']['gamemoney'] = str(balance + credited)
        source.remove(facility)
        state['pending_facility_placements'] = [i for i in state.get('pending_facility_placements', []) if i != str(ident)]
        state.setdefault('facility_sales', {})[str(ident)] = {'sellPrice': credited, 'sold_at': timestamp()}
        return {'sellPrice': credited}

    def dispatch(self, api, param, account, state):
        # Home reads use local account data, including local mission gifts.
        # No live-service events or colosseum season are configured.
        if api == '110101':
            country = param.get('countryCode')
            if type(country) is not int or country not in (1, 2, 3):
                raise ProtocolError('Invalid mission country code')
            return {'result': self.progression.mission_rows(state)}
        if api == '110102':
            return self.progression.claim_missions(state, param)
        if api == '070001':
            return self.progression.list_gifts(state, param)
        if api == '070002':
            return self.progression.receive_gifts(account, state, param)
        if api == '070003':
            return self.progression.history(state)
        if api == '010201':
            return self.fuse_monsters(state, param)
        if api == '010601':
            return self.inherit_skill(state, param)
        if api == '010502':
            return self.inherit_talent(state, param)
        if api == '010801':
            return self.change_resistance(state, param)
        if api == '010802':
            return self.version_up_monster(state, param)
        if api == '010501':
            return self.research_monsters(account, state, param)
        if api == '010304':
            return self.hatch_monster(state, param)
        if api == '010001':
            ident, locked = param.get('userMonsterId'), param.get('setFlg')
            monster = next((m for m in state['monsters'] if m['userMonsterId'] == ident), None)
            if not isinstance(ident, str) or not monster or locked not in ('0', '1'):
                raise ProtocolError('Invalid monster protection request')
            monster['statusFlgs'] = str((int(monster.get('statusFlgs', '0')) & 1) + (2 if locked == '1' else 0))
            return {'result': 1}
        if api == '200001':
            return {'userTitleList': state.get('titles', [])}
        if api == '020102':
            return self.garden_info(state)
        if api == '010901':
            return self.move_to_garden(state, param)
        if api == '010302':
            return self.grow_in_garden(state, param)
        if api == '140005':
            return self.move_facility(state, param)
        if api == '140008':
            return self.arrange_facilities(state, param)
        if api == '140015':
            return self.stock_facility(state, param)
        if api == '140007':
            return self.sell_facility(state, param)
        if api in ('140002', '140004'):
            return self.shorten_facility(state, param, upgrade=api == '140004')
        if api == '140011':
            keys = state.get('facility_keys', {})
            return {'facilityCondition': [{'key': r['facilityConditionId'],
                    'flg': int(int(keys.get(r['conditionParam'], 0)) >= int(r['conditionValue']))}
                    for r in self.masters['990308']['facilityConditionM'] if r['conditionType'] == '1']}
        if api == '020304':
            return {'userItemData': state.get('items', [])}
        if api == '000007':
            return {'loginBonus': {key: [] for key in ('normal', 'campaign', 'special', 'week')}}
        if api == '000002':
            counts = dict.fromkeys(('friendApplication', 'profileComment', 'collectionReceiveCount',
                'info', 'isPopUpInformaiton', 'newFriend', 'addCost', 'endlessDungeon',
                'missionRewardCount', 'missionNewCount', 'beginnerMissionRewardCount',
                'beginnerMissionNewCount', 'facilityNewCount', 'decorationNewCount'), 0)
            missions = self.progression.mission_rows(state)
            counts['missionRewardCount'] = sum(r['status'] == 1 for g in ('daily', 'total') for r in missions[g])
            counts['beginnerMissionRewardCount'] = sum(r['status'] == 1 for g in ('beginner', 'midrange') for r in missions[g])
            count = sum(not self.progression.expired(g) for g in state.get('prizes', []))
            return {'userNewsCountList': dict(counts, prize=str(count)), 'penaltyUserInfo': None}
        if api == '900001':
            return {'infoList': []}
        if api == '150024':
            # Home polls chat badges even before the user opens the chat panel.
            # [] makes GetPrefsHistoryIdList parse an empty saved string and
            # index a missing ':' field on the next poll. Null clears the cache.
            return {'lastHistoryIds': None, 'lastMockBattleRequestTime': None, 'multiRoomRequestId': None}
        if api == '190001':
            return {'navigationMessageIdList': state.get('pending_guidance', [])}
        if api == '190002':
            ids = param.get('navigationMessageId')
            pending = set(state.get('pending_guidance', []))
            read = set(state.get('read_guidance', []))
            if (not isinstance(ids, list) or not ids or
                    any(type(i) is not int or i not in pending | read for i in ids)):
                raise ProtocolError('Navigation message was not scheduled for this account')
            state['pending_guidance'] = sorted(pending - set(ids))
            state['read_guidance'] = sorted(read | set(ids))
            return {}
        if api == '990601':
            # Archived banners point at expired external promotions.
            return {'bannerM': []}
        if api == '080104':
            if param.get('target') != 'me':
                raise ProtocolError('Only own colosseum deck is implemented')
            return {'resultCode': 1, 'partyMonsters': []}
        if api == '080002':
            return {'resultCode': 0}
        if api == '080001':
            return {'resultCode': 0, 'colosseumId': 0, 'helpPage': '',
                    'extraCost': 0, 'openAllDay': 0, 'eventInfo': None}
        if api == '080116':
            if param.get('act') != '2':
                raise ProtocolError('Colosseum reward collection is not implemented')
            return {'resultCode': 0, 'rewardList': None, 'interimRewardList': None}
        if api == '010702':
            return self.friendship_times(state, param)
        if api == '010701':
            return self.tap_friendship(state, param)
        if api == '090012':
            refresh_battle_expiry(state)
            battle = state.get('active_battle')
            if battle and not battle['settled']:
                return dict(battle['response'], userDungeonTicketId=None)
            return {'startId': None, 'userDungeonTicketId': None}
        if api == '020002':
            if type(param.get('targetUserId')) is not int or param['targetUserId'] != account['id']:
                raise ProtocolError('Only own profile is implemented')
            decks = state['decks']
            favorite = next((d for d in decks['deckList'] if d['deckNum'] == decks['favoriteDeckNum']), None)
            leader_id = favorite['monsterList'][0]['userMonsterId'] if favorite and favorite['monsterList'] else None
            leader = next((m for m in state['monsters'] if m['userMonsterId'] == leader_id), None)
            evolution = state.get('tutorial_evolution')
            history = dict.fromkeys(('fusionCount', 'inheritanceCount', 'combinationCount'), '0')
            history.update(dungeonClearCount=str(int(bool(state.get('tutorial_battle_clear'))) + state.get('normal_clear_count', 0)),
                useMeatNum=str(state.get('tutorial_meal', {}).get('meatNum', 0) + state.get('normal_meat_used', 0)),
                evolutionCount=str(int(bool(evolution)) + state.get('normal_evolution_count', 0)), continueLoginCount='1',
                totalLoginCount=str(state['login_count']))
            return {'userData': {'userId': str(account['id']), 'userCode': f"{account['id']:09}",
                'nickname': state['nickname'], 'description': state['description'],
                'lastLoginTime': state['login_time'], 'loginTime': state['login_time'],
                'loginTimeSort': 0, 'birthday': state['player']['birthday'], 'titleId': state['player']['titleId']},
                'monsterData': {k: v for k, v in leader.items() if k != 'createTimeSec'} if leader else None,
                'collection': {'possessionNum': str(len(state['obtained_collections'])), 'totalNum': str(len(self.collection_ids))},
                'friendStatus': 0, 'playHistory': history}
        if api == '140001':
            if state['tutorial']['endFlg'] == '1':
                ident, x, y = param.get('facilityId'), param.get('positionX'), param.get('positionY')
                master = self.facility_master(ident) if type(ident) is int else None
                if (not master or (ident not in (1, 2, 3, 4, 25) and master['type'] != '2') or
                        any(type(v) is not int or v < 0 or v > 2147483647 for v in (x, y))):
                    raise ProtocolError('Invalid or unsupported facility placement')
                construction = int(master['buildingTime']) > 0 or master['autoBuildingFlg'] == '0'
                if construction and sum(bool(r['completeTime']) for r in state['facilities']) >= 2:
                    raise ProtocolError('Both construction slots are occupied')
                self.check_facility_release(state, master['releaseId'])
                if sum(r['facilityId'] == ident for r in state['facilities'] + state['stock_facilities']) >= int(master['maxNum']):
                    raise ProtocolError('Facility count limit reached')
                self.check_facility_placement(state, ident, x, y)
                self.pay_facility_cost(state, master, 'building')
                owned_id = self.next_facility_id(state)
                deadline = (datetime.fromtimestamp(time.time() + int(master['buildingTime']), timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
                            if construction else '')
                state['facilities'].append({'userFacilityId': owned_id, 'facilityId': ident, 'level': 0 if construction else 1,
                    'positionX': x, 'positionY': y, 'completeTime': deadline})
                return {'userFacilityId': owned_id}
            # FirstTutorial has already displayed/harvested its temporary field.
            # FinishRequest now commits it; return the durable field created830.
            if (state['tutorial']['statusId'] != '1400' or not state.get('tutorial_rare_capture') or
                    type(param.get('facilityId')) is not int or param['facilityId'] != 1 or
                    type(param.get('positionX')) is not int or param['positionX'] != 12 or
                    type(param.get('positionY')) is not int or param['positionY'] not in (11, 12)):
                raise ProtocolError('Only final tutorial field construction is implemented')
            # Archived FinishRequest sends initialX for Y too; keep the real
            # tutorial field at (12,11), avoiding a duplicate or overlapping copy.
            ident = state.get('tutorial_harvest', {}).get('userFacilityId')
            if not any(r['userFacilityId'] == ident and r['facilityId'] == 1 for r in state['facilities']):
                raise ProtocolError('Tutorial field is missing')
            state['tutorial_field_commit'] = {'userFacilityId': ident, 'complete': False} if not state.get('tutorial_field_commit') else state['tutorial_field_commit']
            return {'userFacilityId': ident}
        if api == '140010':
            if state['tutorial']['endFlg'] == '1':
                ident = param.get('userFacilityId')
                facility = next((r for r in state['facilities'] if r['userFacilityId'] == ident), None)
                if (type(ident) is not int or not facility or
                        (facility['facilityId'] not in (1, 2, 3, 4, 7, 25) and self.facility_master(facility['facilityId'])['type'] != '2')):
                    raise ProtocolError('Invalid facility completion')
                if not facility['completeTime']:
                    return {'result': 1}
                deadline = datetime.strptime(facility['completeTime'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).timestamp()
                if time.time() < deadline:
                    raise ProtocolError('Facility construction is not finished')
                self.finish_facility(state, facility)
                return {'result': 1}
            receipt = state.get('tutorial_field_commit')
            if (state['tutorial']['statusId'] != '1400' or not receipt or
                    type(param.get('userFacilityId')) is not int or
                    param['userFacilityId'] != receipt['userFacilityId']):
                raise ProtocolError('Only committed tutorial field completion is implemented')
            receipt['complete'] = True
            return {'result': 1}
        if api == '140003':
            ident = param.get('userFacilityId')
            facility = next((r for r in state['facilities'] if r['userFacilityId'] == ident), None)
            if (state['tutorial']['endFlg'] != '1' or type(ident) is not int or not facility or
                    facility['facilityId'] not in (1, 2, 3, 7) or facility['level'] < 1 or facility['completeTime']):
                raise ProtocolError('Invalid facility upgrade')
            upgrade = next((r for r in self.masters['990302']['facilityUpgradeM']
                            if r['facilityId'] == str(facility['facilityId']) and r['level'] == str(facility['level'] + 1)), None)
            if not upgrade or facility['level'] >= int(self.facility_master(facility['facilityId'])['maxLevel']):
                raise ProtocolError('Facility has reached its maximum level')
            if sum(bool(r['completeTime']) for r in state['facilities']) >= 2:
                raise ProtocolError('Both construction slots are occupied')
            self.check_facility_release(state, upgrade['releaseId'])
            self.pay_facility_cost(state, upgrade, 'upgrade')
            # Even zero-second house upgrades follow native 140003 -> 140010;
            # hold the old level/capacity until explicit completion.
            facility['completeTime'] = datetime.fromtimestamp(time.time() + int(upgrade['upgradeTime']), timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            return {}
        if api == '910002':
            if (state['tutorial']['statusId'] != '1400' or not state.get('tutorial_rare_capture') or
                    not state.get('tutorial_field_commit', {}).get('complete')):
                raise ProtocolError('Tutorial completion prerequisites missing')
            state['tutorial']['endFlg'] = '1'
            return {}
        if api == '910007':
            if state['tutorial']['statusId'] != '900' or not state.get('tutorial_meal'):
                raise ProtocolError('Tutorial battle preparation requires completed meal')
            if state.get('tutorial_party'):
                return {}
            starter_id = state['tutorial_meal']['userMonsterId']
            if not any(m['userMonsterId'] == starter_id for m in state['monsters']):
                raise ProtocolError('Tutorial starter is missing')
            gifts = self.tutorial_battle['companionMonsterIds']
            if len(state['monsters']) + len(gifts) > int(state['player']['unitLimitMax']):
                raise ProtocolError('Monster inventory is full')
            ids = [starter_id]
            for monster_id in gifts:
                master = next(r for r in self.masters['990102']['monsterM'] if r['monsterId'] == monster_id)
                route = next(r for r in self.masters['990110']['monsterEvolutionRouteM'] if r['growthMonsterId'] == monster_id)
                skill = next(r for r in self.masters['990108']['skillM']
                             if r['skillGroupId'] == master['skillGroupId'] and r['skillGroupSubId'] == '1')
                ident = self.next_monster_id(state)
                monster = dict.fromkeys(('hpAbilityFlg hpAbility attackAbilityFlg attackAbility '
                    'defenseAbilityFlg defenseAbility spAttackAbilityFlg spAttackAbility '
                    'spDefenseAbilityFlg spDefenseAbility speedAbilityFlg speedAbility').split(), '0')
                monster.update(userMonsterId=str(ident), userId=str(account['id']), monsterId=monster_id,
                    level='1', ex='0', levelEx='0', nextLevelEx='4', leaderSkillId='0',
                    uniqueSkillId=skill['skillId'], defaultSkillGroupSubId='1', commonSkillId='473',
                    extraCommonSkillId='', eggFlg='0', growEndDate='', monsterEvolutionRouteId=route['monsterEvolutionRouteId'],
                    speed=master['speed'], luck='1', friendship='0', statusFlgs='0', tranceResistance='',
                    tranceStatusAilment='', createTimeSec=str(int(time.time())))
                for field in ('hp', 'attack', 'defense', 'spAttack', 'spDefense'):
                    monster[field] = master['default' + field[0].upper() + field[1:]]
                state['monsters'].append(monster)
                state['chip_slots'].append({'userMonsterId': ident, 'equip': [], 'manage': {
                    'slotNum': 0, 'extraSlotNum': 0, 'maxSlotNum': 5, 'maxExtraSlotNum': 0}})
                ids.append(str(ident))
            deck = next(r for r in state['decks']['deckList'] if r['deckNum'] == '1')
            if deck['monsterList']:
                raise ProtocolError('Tutorial deck already configured')
            deck['monsterList'] = [{'userDeckMonsterId': str(i), 'userId': str(account['id']),
                'deckNum': '1', 'position': str(i), 'userMonsterId': ident} for i, ident in enumerate(ids, 1)]
            state['tutorial_party'] = {'userMonsterIds': ids, 'giftMonsterIds': gifts}
            return {}
        if api == '090009':
            if param.get('worldId') == '1' and state['tutorial']['endFlg'] == '1':
                return self.normal_world_info(state)
            if param.get('worldId') != self.tutorial_battle['worldId'] or not state.get('tutorial_party'):
                raise ProtocolError('Only prepared tutorial world is implemented')
            return {'worldDungeonInfo': [{'worldStageId': 9001, 'isOpen': 1, 'isCounting': 0,
                'updateTime': timestamp(), 'expireTime': '2099-12-31 23:59:59', 'timeLeft': 0,
                'dungeons': [{'worldDungeonId': 9001, 'status': 4 if state.get('tutorial_battle_clear') else 3, 'isExtraWave': 0,
                    'exp': self.tutorial_battle['fixedExpPerEnemy'] * 3,
                    'money': self.tutorial_battle['fixedMoneyPerEnemy'] * 3,
                    'encountEnemies': [], 'dropAssets': [], 'playLimit': None,
                    'dungeonTicketId': '0', 'userDungeonTicketId': '0', 'dungeonTicketNum': '0'}]}]}
        if api in ('090101', '090201'):
            # No event schedule or ticket dungeons configured locally.
            return {'worldDungeonInfo': []}
        if api == '090002':
            if state['tutorial']['endFlg'] == '1':
                return self.start_normal_battle(param, state)
            if (state['tutorial']['statusId'] != '900' or not state.get('tutorial_party') or
                    param.get('dungeonId') != '9001' or param.get('deckNum') != '1' or
                    param.get('userDungeonTicketId') not in (None, '', '0')):
                raise ProtocolError('Only prepared tutorial battle is implemented')
            if state.get('active_battle'):
                return state['active_battle']['response']
            enemies = []
            for i, monster_id in enumerate(self.tutorial_battle['enemyMonsterIds'], 1):
                master = next(r for r in self.masters['990102']['monsterM'] if r['monsterId'] == monster_id)
                enemy = {'worldEnemyId': str(900100 + i), 'monsterId': monster_id, 'type': 1, 'level': 1,
                    'speed': int(master['speed']), 'resistanceId': master['resistanceId'],
                    'fixedExp': self.tutorial_battle['fixedExpPerEnemy'],
                    'fixedMoney': self.tutorial_battle['fixedMoneyPerEnemy'], 'drop': [], 'chipIdList': [],
                    'ai': [{'type': 1, 'minHpRange': 0, 'maxHpRange': 100, 'priority': 1,
                            'rate': 10000, 'lookTarget': 1, 'lookStatus': 1, 'lookType': 1,
                            'invokeMinRange': 0, 'invokeMaxRange': 100, 'skillId': '571'}]}
                for field in ('hp', 'attack', 'defense', 'spAttack', 'spDefense'):
                    suffix = field[0].upper() + field[1:]
                    low, high = int(master['default' + suffix]), int(master['max' + suffix])
                    enemy[field] = int(low + (high - low) * (enemy['level'] - 1) / max(1, int(master['maxLevel']) - 1) + 0.5)
                enemy['speed'] = self.tutorial_battle['enemySpeed']
                if i - 1 == self.tutorial_battle['revivalEnemyIndex']:
                    enemy['attack'] = self.tutorial_battle['revivalEnemyAttack']
                    enemy['ai'][0].update(lookStatus=4, invokeMaxRange=100000)
                    # FirstTutorial requires the middle (highest-defense) Tentomon
                    # to fall after the first two player turns. Subsequent turns
                    # are support-only, leaving time to teach revival and win.
                    enemy['ai'] += [dict(enemy['ai'][0], priority=n, skillId='473',
                                        lookStatus=1, invokeMaxRange=100) for n in range(2, 101)]
                enemies.append(enemy)
            # Continue requests parse this identifier as a signed Int32.
            response = {'startId': str(secrets.randbelow(2_000_000_000) + 1), 'worldDungeonId': '9001',
                'luckDrop': None,
                'deck': {'userMonsterIds': list(state['tutorial_party']['userMonsterIds'])},
                'dungeonFloor': [{'floorId': '900101', 'floorNum': 1, 'floorType': 1,
                                  'healingRate': 0, 'cameraType': 0, 'enemy': enemies}]}
            state['active_battle'] = {'response': response, 'started': timestamp(), 'settled': False}
            return response
        if api == '090003':
            if (not isinstance(param.get('startId'), str) or
                    not isinstance(param.get('dungeonId'), str)):
                raise ProtocolError('Invalid battle identifiers')
            battle = state.get('active_battle')
            if battle and param.get('startId') != battle['response']['startId']:
                battle = state.get('battle_history', {}).get(param.get('startId'))
            if (not battle or param.get('startId') != battle['response']['startId'] or
                    param.get('dungeonId') != battle['response']['worldDungeonId']):
                raise ProtocolError('Unknown owned battle')
            result_input = {k: param.get(k) for k in
                            ('startId', 'dungeonId', 'clear', 'clearRound', 'aliveInfo', 'enemyAliveInfo')}
            if battle_is_expired(battle):
                raise ProtocolError('Battle has expired')
            if battle['settled']:
                if result_input != battle['result_input']:
                    raise ProtocolError('Battle already settled with a different result')
                return battle['result']
            alive, enemy_alive = param.get('aliveInfo'), param.get('enemyAliveInfo')
            deck = battle['response']['deck']['userMonsterIds']
            floors = battle['response']['dungeonFloor']
            def flags(value, count):
                return (isinstance(value, list) and len(value) == count and
                        all(type(v) is int and v in (0, 1) for v in value))
            # DataMng.SetAliveFlag always sends three positions. Keep the
            # original slot mapping on entry so a sparse deck's final member
            # cannot be mistaken for its empty middle slot. Existing compact
            # callers and older full-deck encounters retain their contract.
            player_alive = alive if flags(alive, len(deck)) else None
            indices = battle.get('alive_slot_indices')
            if player_alive is None and flags(alive, 3):
                if (isinstance(indices, list) and len(indices) == len(deck) and
                        all(type(i) is int and 0 <= i < 3 for i in indices) and
                        len(set(indices)) == len(indices) and
                        all(alive[i] == 0 for i in set(range(3)) - set(indices))):
                    player_alive = [alive[i] for i in indices]
                elif type(param.get('clear')) is int and param['clear'] in (0, 2):
                    # Native recovery cancellation uses three zero flags even
                    # for a legacy compact deck with no saved slot mapping.
                    # Defeat/timeout never awards any survivor EXP or friendship.
                    player_alive = [0] * len(deck)
            if (type(param.get('clear')) is not int or param['clear'] not in (0, 1, 2) or
                    type(param.get('clearRound')) is not int or
                    not (1 if param['clear'] == 1 else 0) <= param['clearRound'] <= 9999 or
                    player_alive is None or not isinstance(enemy_alive, list) or
                    len(enemy_alive) != len(floors) or
                    any(not flags(row, len(floor['enemy'])) for row, floor in zip(enemy_alive, floors))):
                raise ProtocolError('Invalid battle result shape')
            won = param['clear'] == 1
            if won and (not any(player_alive) or any(any(row) for row in enemy_alive)):
                raise ProtocolError('Victory requires a survivor and defeated enemies')
            exp = sum(e['fixedExp'] for f in floors for e in f['enemy']) if won else 0
            money = sum(e['fixedMoney'] for f in floors for e in f['enemy']) if won else 0
            dungeon_rewards = []
            if won and battle.get('kind') == 'normal':
                claimed = set(state.get('claimed_dungeon_rewards', []))
                for reward in self.masters['990909']['worldDungeonRewardM']:
                    rid = reward['worldDungeonRewardId']
                    if reward['worldDungeonId'] != param['dungeonId'] or (rid in claimed and reward['everyTimeFlg'] == '0'):
                        continue
                    category = reward['assetCategoryId']
                    if category == '2':
                        state['player']['point'] += int(reward['assetNum'])
                    elif category == '16':
                        keys = state.setdefault('facility_keys', {})
                        keys[reward['assetValue']] = int(keys.get(reward['assetValue'], 0)) + int(reward['assetNum'])
                    elif category == '1':
                        for _ in range(int(reward['assetNum'])):
                            self.grant_fixed_monster(account, state, reward)
                    else:
                        raise ProtocolError('Dungeon reward category is not implemented')
                    dungeon_rewards.append({k: int(v) if k in ('assetValue', 'assetNum', 'everyTimeFlg') else v
                                            for k, v in reward.items() if k != 'worldDungeonId'})
                    claimed.add(rid)
                state['claimed_dungeon_rewards'] = sorted(claimed, key=int)
            if won:
                for ident, survived in zip(deck, player_alive):
                    if not survived:
                        continue
                    monster = next((m for m in state['monsters'] if m['userMonsterId'] == ident), None)
                    if monster is None:
                        raise ProtocolError('Battle monster is missing')
                    self.add_monster_experience(monster, exp)
                self.award_battle_friendship(battle, state, player_alive)
                state['player']['gamemoney'] = str(int(state['player']['gamemoney']) + money)
            material_drops = battle.get('material_drops', []) if won else []
            for drop in material_drops:
                soul = next((r for r in state['souls'] if r['soulId'] == str(drop['assetValue'])), None)
                if soul is None:
                    soul = {'soulId': str(drop['assetValue']), 'num': '0'}
                    state['souls'].append(soul)
                soul['num'] = str(int(soul['num']) + drop['assetNum'])
            drops = list(material_drops)
            if won and battle.get('kind') == 'normal':
                maximum = int(self.masters['990701']['codeM']['maxLinkPointCount'])
                balance = int(state['player']['friendPoint'])
                # Snapshot the master reward on entry; existing battles from older
                # builds retain their old reward contract rather than gaining extras.
                link_points = min(max(0, maximum - balance), battle.get('link_points', 0))
                if link_points:
                    state['player']['friendPoint'] = str(balance + link_points)
                    drops.append({'assetCategoryId': '3', 'assetValue': 0,
                                  'assetNum': link_points, 'dropBoxType': 0})
            result = {'worldDungeonId': battle['response']['worldDungeonId'], 'clearType': 1 if won else 0,
                      'totalExp': exp, 'totalMoney': money, 'dungeonReward': dungeon_rewards, 'dropReward': drops,
                      'optionDrop': [], 'eventChipReward': []}
            battle.update(settled=True, result_input=result_input, result=result, settled_at=timestamp())
            if won and battle.get('kind') == 'normal':
                state['cleared_dungeons'] = sorted(set(state.get('cleared_dungeons', [])) | {param['dungeonId']}, key=int)
                state['normal_clear_count'] = state.get('normal_clear_count', 0) + 1
            elif won:
                state['tutorial_battle_clear'] = {'startId': param['startId'], 'totalExp': exp, 'totalMoney': money}
            return result
        if api == '050002':
            if state['tutorial']['endFlg'] == '1':
                return self.normal_capture(account, state, param)
            rare = state['tutorial']['statusId'] == '1200'
            if state['tutorial']['statusId'] not in ('600', '1200'):
                raise ProtocolError('Capture requires tutorial checkpoint 600 or 1200')
            if rare and not state.get('tutorial_battle_clear'):
                raise ProtocolError('Rare tutorial capture requires battle victory')
            receipt_key = 'tutorial_rare_capture' if rare else 'tutorial_capture'
            if any(type(param.get(k)) is not int or param[k] != v
                   for k, v in {'gachaId': 10001 if rare else 10002, 'playCount': 1,
                                'itemCount': 20 if rare else 300}.items()):
                raise ProtocolError('Only the current single tutorial capture is implemented')
            # Persist the receipt across auth sessions and changed request IDs.
            if receipt_key in state:
                return state[receipt_key]['response']
            if len(state['monsters']) >= int(state['player']['unitLimitMax']):
                raise ProtocolError('Monster inventory is full')
            # Local starter choice: Botamon -> Koromon -> Agumon, master route 121.
            route = next(r for r in self.masters['990110']['monsterEvolutionRouteM']
                         if r['monsterEvolutionRouteId'] == '121')
            if rare:
                # Local fixed reward, not a recovered production gacha pool.
                route = next(r for r in self.masters['990110']['monsterEvolutionRouteM']
                             if r['growthMonsterId'] == '121')
            master = next(r for r in self.masters['990102']['monsterM']
                          if r['monsterId'] == route['growthMonsterId' if rare else 'childhood1MonsterId'])
            monster = self.create_capture_monster(account, state, route, master)
            # Non-null, even empty, opens CMD_CaptureBonus in the real client.
            response = {'userMonsterList': [dict(monster, isNew=1)], 'rewards': None}
            # The script presents a 300-point gift; grant and spend it atomically.
            # Net balance is unchanged; an existing balance is never overwritten.
            state[receipt_key] = ({'grantedStones': 20, 'spentStones': 20, 'response': response} if rare else
                                 {'grantedLinkPoints': 300, 'spentLinkPoints': 300, 'response': response})
            return response
        if api == '050001':
            tutorial = param.get('isTutorial')
            normal = tutorial == 0 and state['tutorial']['endFlg'] == '1'
            if type(tutorial) is not int or not (normal or tutorial == 1 and state['tutorial']['statusId'] in ('600', '1200')):
                raise ProtocolError('Capture is unavailable at this checkpoint')
            # Local reconstruction: rare/link tabs match the script's TAB2_RIGHT.
            # Actual capture remains a separate, transactional operation.
            talents = self.local_gameplay['captureTalents']['probabilityBasisPoints']
            talent_text = f"才能：银Ⅰ{talents['silver'] / 100:g}% / 金Ⅰ{talents['gold'] / 100:g}%" if normal else ''
            result = []
            for ident, name, category, cost, banner in (
                    (10001, '本地成长捕捉' if normal else '稀有捕捉', '2', 5 if normal else 20, 'normal' if normal else 'rare'),
                    (10002, 'Link捕捉', '3', 300, 'link')):
                result.append({'gachaId': str(ident), 'gachaName': name,
                    'mainImagePath': 'local_' + banner + '.png',
                    'subImagePath': ['local_' + banner + '.png'],
                    'startTime': '2020-01-01 00:00:00', 'endTime': '2099-12-31 23:59:59',
                    'prize': '1', 'priceType': {'category': category, 'value': '0'},
                    'totalPlayLimitCount': '0', 'totalPlayCount': '0', 'dispNum': str(ident),
                    'details': [{'count': str(count), 'price': str(cost * count),
                        'priceFirst': str(cost * count), 'dailyResetFirst': '0',
                        'appealText': talent_text, 'appealTextDisplayType': '1' if normal else '0',
                        'isFirst': 0, 'isTodayFirst': 0} for count in (1, 10)]})
            if normal and any(r['facilityId'] == 25 and r['level'] > 0 and not r['completeTime'] for r in state['facilities']):
                chip = self.local_gameplay['chips']
                chip_pools = [(chip, '本地基础芯片捕捉', '六种G级基础芯片等概率；本地规则', chip['gachaId'])]
                if self.chip_inventory.advanced_available(state):
                    advanced = self.chip_inventory.advanced
                    chip_pools.append((advanced, '本地进阶芯片捕捉', 'F至S基础芯片；等级概率见详情；本地规则', advanced['dispNum']))
                for chip, name, appeal, disp_num in chip_pools:
                    result.append({'gachaId': str(chip['gachaId']), 'gachaName': name,
                    'mainImagePath': 'local_link.png', 'subImagePath': ['local_link.png'],
                    'startTime': '2020-01-01 00:00:00', 'endTime': '2099-12-31 23:59:59',
                    'prize': '17', 'priceType': {'category': '3', 'value': '0'},
                    'totalPlayLimitCount': '0', 'totalPlayCount': '0', 'dispNum': str(disp_num),
                    'details': [{'count': str(count), 'price': str(chip['linkCostPerChip'] * count),
                        'priceFirst': str(chip['linkCostPerChip'] * count), 'dailyResetFirst': '0',
                        'appealText': appeal, 'appealTextDisplayType': '1',
                        'isFirst': 0, 'isTodayFirst': 0} for count in (1, 10)]})
            return {'result': result}
        if api == '050012':
            return self.chip_inventory.gacha(state, param)
        if api == '140014':
            # UserDataMng explicitly sends 0 to request the authenticated user's stock.
            if type(param.get('userId')) is not int or param['userId'] not in (0, account['id']):
                raise ProtocolError('Only own facility stock is implemented')
            return {'userFacilityList': state['stock_facilities'], 'currentTime': timestamp(),
                    'lastHarvestTime': [], 'monsterIdsInFarm': []}
        if api == '020001':
            info = dict(state['player'], nickname=state['nickname'], description=state['description'],
                        lastLoginTime=state['login_time'], loginTime=state['login_time'],
                        loginTimeSort=0, loginCount=state['login_count'],
                        countryCode=str(state.get('country_code', state['options']['103'])))
            return {'playerInfo': info, 'leaderMonster': None}
        if api == '020101':
            return {'userMonsterList': filter_owned(state['monsters'], param.get('userMonsterIds'), 'userMonsterId')}
        if api == '010101':
            if param.get('targetUserId') != str(account['id']):
                raise ProtocolError('Only own picturebook is implemented')
            obtained = state['obtained_collections']
            return {'possessionNum': len(obtained), 'totalNum': str(len(self.collection_ids)),
                    'userCollectionList': [{'monsterCollectionId': ident, 'collectionStatus': '1'}
                                           for ident in obtained]}
        if api == '010401':
            return state['decks']
        if api == '010301':
            ident, target = param.get('userMonsterId'), param.get('monsterId')
            monster = next((m for m in state['monsters'] if m['userMonsterId'] == ident), None)
            if not isinstance(ident, str) or type(target) is not int or not monster or monster['eggFlg'] != '0':
                raise ProtocolError('Invalid evolution monster')
            evolution = next((r for r in self.masters['990104']['monsterEvolutionM']
                              if r['baseMonsterId'] == monster['monsterId'] and r['nextMonsterId'] == str(target)), None)
            mode_receipt = state.get('mode_change_receipts', {}).get(ident, {})
            if ((evolution and evolution['type'] == '2' and evolution['effectType'] == '2') or
                    (evolution is None and monster['monsterId'] == str(target) and
                     mode_receipt.get('targetMonsterId') == str(target))):
                return self.change_monster_mode(state, monster, evolution)
            if not evolution or evolution['type'] != '1' or evolution['effectType'] != '1':
                raise ProtocolError('This evolution form is not implemented')
            masters = {m['monsterId']: m for m in self.masters['990102']['monsterM']}
            source, destination = masters[monster['monsterId']], masters[str(target)]
            if int(monster['level']) != int(source['maxLevel']):
                raise ProtocolError('Evolution requires maximum level')
            if state.get('active_battle') and not state['active_battle']['settled']:
                raise ProtocolError('Finish the active battle before evolving')
            group = next(r for r in self.masters['990103']['monsterM'] if r['monsterGroupId'] == destination['monsterGroupId'])
            step = {'5': '5', '6': '6', '7': '7', '11': '5', '12': '6', '13': '7'}.get(group['growStep'])
            if not step:
                raise ProtocolError('Unsupported evolution growth stage')
            code = self.masters['990701']['codeM']
            cost = int(code['evolveCoefficientFor' + step]) * int(code['evolveCoefficientRare' + destination['rare']])
            if int(state['player']['gamemoney']) < cost:
                raise ProtocolError('Not enough evolution money')
            material = next(r for r in self.masters['990115']['monsterEvolutionMaterialM']
                            if r['monsterEvolutionMaterialId'] == evolution['monsterEvolutionMaterialId'])
            required = {}
            for i in range(1, 8):
                count = int(material['materialAssetNum' + str(i)])
                if not count:
                    continue
                if material['materialAssetCategoryId' + str(i)] != '14':
                    raise ProtocolError('Unsupported evolution material category')
                soul = material['materialAssetValue' + str(i)]
                required[soul] = required.get(soul, 0) + count
            owned = {r['soulId']: r for r in state['souls']}
            if any(soul not in owned or int(owned[soul]['num']) < count for soul, count in required.items()):
                raise ProtocolError('Not enough evolution materials')
            skill = next((r for r in self.masters['990108']['skillM']
                          if r['skillGroupId'] == destination['skillGroupId'] and
                          r['skillGroupSubId'] == monster['defaultSkillGroupSubId']), None)
            if not skill:
                raise ProtocolError('Evolution skill is missing')
            resistance = self.evolution_resistance_values(monster, destination)
            for soul, count in required.items():
                owned[soul]['num'] = str(int(owned[soul]['num']) - count)
            state['player']['gamemoney'] = str(int(state['player']['gamemoney']) - cost)
            monster.update(monsterId=str(target), ex='0', speed=destination['speed'], uniqueSkillId=skill['skillId'])
            monster.update(resistance)
            self.add_monster_experience(monster, 0)
            state['normal_evolution_count'] = state.get('normal_evolution_count', 0) + 1
            return {'result': 1, 'userMonster': monster,
                    'reviewStatus': 1 if state['normal_evolution_count'] == 1 else 0}
        if api == '010202':
            ident, count = param.get('mealMonster'), param.get('meatNum')
            monster = next((m for m in state['monsters'] if m['userMonsterId'] == ident), None)
            if (not isinstance(ident, str) or not monster or monster['eggFlg'] != '0' or
                    type(count) is not int or count <= 0 or count > int(state['player']['meatNum'])):
                raise ProtocolError('Invalid meal monster or meat quantity')
            if monster.get('growEndDate') or self.monster_growth_step(monster) in ('1', '2', '3'):
                raise ProtocolError('Garden monsters cannot be fed')
            master = next(m for m in self.masters['990102']['monsterM'] if m['monsterId'] == monster['monsterId'])
            if int(monster['level']) >= int(master['maxLevel']):
                raise ProtocolError('Monster is already at maximum level')
            if state.get('active_battle') and not state['active_battle']['settled']:
                raise ProtocolError('Finish the active battle before feeding')
            self.add_monster_experience(monster, count * int(self.masters['990701']['codeM']['expOfOneMeat']))
            state['player']['meatNum'] = str(int(state['player']['meatNum']) - count)
            state['normal_meat_used'] = state.get('normal_meat_used', 0) + count
            return {'mealType': 1, 'userMonster': monster}
        if api == '140009':
            field_id, warehouse_id = param.get('userFacilityId1'), param.get('userFacilityId2')
            if type(field_id) is not int or type(warehouse_id) is not int:
                raise ProtocolError('Invalid harvest facility IDs')
            field = next((r for r in state['facilities'] if r['userFacilityId'] == field_id and r['facilityId'] == 1), None)
            warehouse = next((r for r in state['facilities'] if r['facilityId'] == 2), None)
            if (not field or field['completeTime'] or
                    warehouse_id != (warehouse['userFacilityId'] if warehouse else 0)):
                raise ProtocolError('Invalid harvest field or warehouse')
            production = next(r for r in self.masters['990303']['facilityMeatFieldM'] if r['level'] == str(field['level']))
            capacity = self.refresh_meat_capacity(state)
            harvest = next((r for r in state['last_harvest'] if r['userFacilityId'] == field_id), None)
            if not harvest:
                raise ProtocolError('Field production start time is missing')
            now = int(time.time())
            previous = int(datetime.strptime(harvest['lastHarvestTime'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).timestamp())
            amount = min(int(production['maxMeatNum']), max(0, now - previous) // int(production['perMeatTime']))
            if int(state['player']['meatNum']) + amount > capacity:
                # FarmObject_MeatFarm releases its input lock for this code.
                raise ProtocolError('Meat storage is full', code='E-FA20')
            if amount:
                state['player']['meatNum'] = str(int(state['player']['meatNum']) + amount)
                # Client resets production to receipt time + 2s for the harvest animation.
                harvest['lastHarvestTime'] = datetime.fromtimestamp(now + 2, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            state['player']['meatLimitMax'] = str(capacity)
            return {'harvestNum': amount}
        if api == '010402':
            # PartyEditPartyMember sends all existing decks in numeric order;
            # GetUserMonsterIdList refuses incomplete three-member parties.
            decks = state['decks']
            data = param.get('deckData')
            selected, favorite = param.get('selectDeckNum'), param.get('favoriteDeckNum')
            count = decks['deckCnt']
            if (not isinstance(data, list) or len(data) != count or
                    type(selected) is not int or type(favorite) is not int or
                    not 1 <= selected <= count or not 1 <= favorite <= count):
                raise ProtocolError('Invalid deck selection')
            owned = {int(m['userMonsterId']): m for m in state['monsters']}
            for party in data:
                if (not isinstance(party, list) or len(party) != 3 or
                        any(type(i) is not int or i not in owned or owned[i]['eggFlg'] != '0' or
                            owned[i].get('growEndDate') or self.monster_growth_step(owned[i]) in ('1', '2', '3') for i in party) or
                        len(set(party)) != 3):
                    raise ProtocolError('Deck requires three distinct owned non-egg monsters')
            if state.get('active_battle') and not state['active_battle']['settled']:
                raise ProtocolError('Finish the active battle before editing decks')
            decks.update(selectDeckNum=str(selected), favoriteDeckNum=str(favorite),
                deckList=[{'deckNum': str(d), 'monsterList': [
                    {'userDeckMonsterId': str((d - 1) * 3 + pos), 'userId': str(account['id']),
                     'deckNum': str(d), 'position': str(pos), 'userMonsterId': str(ident)}
                    for pos, ident in enumerate(party, 1)]} for d, party in enumerate(data, 1)])
            return {'result': 1}
        if api == '180001':
            chips = filter_owned(state['chips'], param.get('userChipId'), 'userChipId')
            return {'count': len(chips), 'resultCode': 1, 'userChipList': chips}
        if api == '180006':
            self.chip_inventory.refresh_slots(state)
            return {'resultCode': 1, 'slotInfo': filter_owned(state['chip_slots'], param.get('userMonsterId'), 'userMonsterId')}
        if api == '180002':
            return self.chip_inventory.equip(state, param)
        if api == '180003':
            return self.chip_inventory.fuse(state, param)
        if api == '180004':
            return self.chip_inventory.expand(state, param)
        if api == '180005':
            return self.chip_inventory.sell(state, param)
        if api == '020303':
            return {'userSoulData': state['souls']}
        if api == '140006':
            if type(param.get('userId')) is not int or param['userId'] != account['id']:
                raise ProtocolError('Only own farm is implemented')
            if int(state['tutorial']['statusId']) >= 500 and not state.get('initial_facilities_created'):
                # Fixed starter buildings, from facility master initial positions.
                # Garden (6) shares the lab mesh and is filtered out by UserDataMng.
                existing = {row['facilityId'] for row in state['facilities']}
                for row in self.masters['990301']['facilityM']:
                    ident = int(row['facilityId'])
                    if ident in (5, 7, 21) and ident not in existing:
                        state['facilities'].append({'userFacilityId': self.next_facility_id(state), 'facilityId': ident,
                            'level': 1, 'positionX': int(row['initialX']),
                            'positionY': int(row['initialY']), 'completeTime': ''})
                state['initial_facilities_created'] = True
            facilities, harvests = state['facilities'], state['last_harvest']
            if state['tutorial']['endFlg'] == '0' and state.get('tutorial_harvest'):
                # TutorialRestart rebuilds this dummy from #build even after
                # save830. Sending its persisted counterpart creates two field
                # objects and FacilityBuildComplete.SingleOrDefault throws.
                dummy_id = state['tutorial_harvest']['userFacilityId']
                facilities = [r for r in facilities if r['userFacilityId'] != dummy_id]
                harvests = [r for r in harvests if r['userFacilityId'] != dummy_id]
            return {'userFacilityList': facilities, 'currentTime': timestamp(),
                    'lastHarvestTime': harvests, 'monsterIdsInFarm': state['farm_monsters']}
        if api == '000005':
            if self.resource_catalog is None:
                raise ProtocolError('Missing AssetBundle catalog: configure --asset-root')
            return self.resource_catalog
        if api == '000004':
            return self.localization.versions(self.masters[api])
        country = str(param.get('countryCode', state.get('country_code', state['options']['103'])))
        if country == '3' and api in self.localization.masters:
            return self.localization.masters[api]
        if api in self.masters:
            return self.masters[api]
        if api == '000001':
            state['login_time'] = timestamp()
            state['login_count'] += 1
            return {'playerInfo': {'userId': str(account['id']), 'userCode': f"{account['id']:09}",
                                   'compassCarrierId': 0, 'osType': str(param.get('osType', '2'))},
                    'tutorialStatus': state['tutorial'], 'optionList': state['options'],
                    'adminUser': None, 'penaltyUserInfo': None, 'isWarning': 0, 'warningData': [],
                    'nowDateTime': timestamp(), 'isPolicyConfirm': state['policy'], 'state': 1}
        if api == '000501':
            return {'nowDateTime': timestamp()}
        if api == '000601':
            if 'inquiry_code' not in state:
                state['inquiry_code'] = secrets.token_hex(8).upper()
            return {'result': '1', 'inquiryCode': state['inquiry_code']}
        if api == '000201':
            state['device_token'] = str(param.get('deviceToken', ''))
            return {'tokenStatus': 1}
        if api == '020305':
            country = param.get('countryCode')
            if type(country) is not int or country not in (1, 2, 3, 4):
                raise ProtocolError('Invalid countryCode')
            state['country_code'] = country
            state['options']['103'] = str(country)
            return {}
        if api == '910001':
            # CMD_AgreementConsent saves checkpoint 10 after policy acceptance.
            # Later checkpoints require their own gameplay state transitions.
            checkpoint = param.get('statusId')
            if type(checkpoint) is not int or checkpoint not in (10, 100, 200, 300, 400, 500, 600, 700, 800, 830, 860, 900, 1000, 1100, 1200, 1300, 1400):
                raise ProtocolError('Tutorial checkpoint not implemented: ' + str(checkpoint))
            if state['policy'] != 1:
                raise ProtocolError('Policy acceptance required')
            previous = state['tutorial']['statusId']
            predecessor = {10: '0', 100: '10', 200: '100', 300: '200', 400: '300', 500: '400', 600: '500', 700: '600', 800: '700', 830: '800', 860: '830', 900: '860', 1000: '900', 1100: '1000', 1200: '1100', 1300: '1200', 1400: '1300'}
            allowed = (predecessor[checkpoint], str(checkpoint))
            if previous not in allowed:
                raise ProtocolError('Invalid tutorial transition')
            if checkpoint == 200 and not state['nickname'].strip():
                raise ProtocolError('Tutorial name is required')
            if checkpoint == 700 and 'tutorial_capture' not in state:
                raise ProtocolError('Tutorial capture is required')
            if checkpoint == 900 and not state.get('tutorial_meal'):
                raise ProtocolError('Tutorial meal is required')
            if checkpoint >= 1000 and not state.get('tutorial_battle_clear'):
                raise ProtocolError('Tutorial battle victory is required')
            if checkpoint >= 1300 and not state.get('tutorial_rare_capture'):
                raise ProtocolError('Rare tutorial capture is required')
            if checkpoint == 860 and previous == '830':
                # FirstTutorial guides one MEAL_PLUS then MEAL_GIVE. DontExec
                # suppresses the normal meal API, so commit at its save point.
                evolution = state.get('tutorial_evolution', {})
                monster = next((r for r in state['monsters']
                                if r['userMonsterId'] == evolution.get('userMonsterId')), None)
                if (not state.get('tutorial_harvest') or monster is None or
                        monster['monsterId'] != evolution.get('toMonsterId') or
                        monster['ex'] != '0' or monster['level'] != '1' or
                        int(state['player']['meatNum']) < 1):
                    raise ProtocolError('Tutorial meal prerequisites missing')
                master = next(r for r in self.masters['990102']['monsterM']
                              if r['monsterId'] == monster['monsterId'])
                exp = int(self.masters['990701']['codeM']['expOfOneMeat'])
                levels = sorted(self.masters['990106']['monsterExperienceM'], key=lambda r: int(r['level']))
                current = max((r for r in levels if int(r['experienceNum']) <= exp), key=lambda r: int(r['level']))
                level = int(current['level'])
                following = next(r for r in levels if int(r['level']) == level + 1)
                monster.update(ex=str(exp), level=str(level), levelEx=str(exp - int(current['experienceNum'])),
                               nextLevelEx=str(int(following['experienceNum']) - exp))
                for field in ('hp', 'attack', 'defense', 'spAttack', 'spDefense'):
                    suffix = field[0].upper() + field[1:]
                    low, high = int(master['default' + suffix]), int(master['max' + suffix])
                    # Positive status interpolation, rounded away from zero,
                    # as MonsterStatusData.CalcLevelStatusUpValue does.
                    monster[field] = str(int(low + (high - low) * (level - 1) / (int(master['maxLevel']) - 1) + 0.5))
                state['player']['meatNum'] = str(int(state['player']['meatNum']) - 1)
                state['tutorial_meal'] = {'userMonsterId': monster['userMonsterId'], 'meatNum': 1,
                                          'experience': exp, 'level': level}
            if checkpoint == 830 and previous == '800':
                if not state.get('tutorial_evolution'):
                    raise ProtocolError('Tutorial evolution is required')
                field = next((r for r in state['facilities'] if r['facilityId'] == 1), None)
                if field is None:
                    master = next(r for r in self.masters['990301']['facilityM'] if r['facilityId'] == '1')
                    field = {'userFacilityId': max((r['userFacilityId'] for r in state['facilities']), default=0) + 1,
                             'facilityId': 1, 'level': 1, 'positionX': int(master['initialX']),
                             'positionY': int(master['initialY']), 'completeTime': ''}
                    state['facilities'].append(field)
                harvested_at = timestamp()
                state['last_harvest'] = [r for r in state['last_harvest'] if r['userFacilityId'] != field['userFacilityId']]
                state['last_harvest'].append({'userFacilityId': field['userFacilityId'], 'lastHarvestTime': harvested_at})
                # FirstTutorial's #wait harvest 1 produces exactly one meat locally.
                state['player']['meatNum'] = str(int(state['player']['meatNum']) + 1)
                state['tutorial_harvest'] = {'meatNum': 1, 'userFacilityId': field['userFacilityId'], 'time': harvested_at}
            if checkpoint == 800 and previous == '700':
                # OfflineGrow_Step2 performs the tutorial evolution locally; save800
                # is its first server acknowledgement. Keep the same owned identity.
                receipt = state.get('tutorial_capture')
                if not receipt:
                    raise ProtocolError('Tutorial capture is required')
                captured = receipt['response']['userMonsterList'][0]
                monster = next((r for r in state['monsters']
                                if r['userMonsterId'] == captured['userMonsterId']), None)
                if monster is None or monster['monsterId'] != captured['monsterId']:
                    raise ProtocolError('Tutorial starter is missing or already changed')
                route = next(r for r in self.masters['990110']['monsterEvolutionRouteM']
                             if r['monsterEvolutionRouteId'] == monster['monsterEvolutionRouteId'])
                master = next(r for r in self.masters['990102']['monsterM']
                              if r['monsterId'] == route['growthMonsterId'])
                skill = next(r for r in self.masters['990108']['skillM']
                             if r['skillGroupId'] == master['skillGroupId'] and r['skillGroupSubId'] == '1')
                monster.update(monsterId=master['monsterId'], level='1', ex='0', levelEx='0',
                               nextLevelEx='4', uniqueSkillId=skill['skillId'],
                               defaultSkillGroupSubId='1', speed=master['speed'], luck='1',
                               friendship='0', growEndDate='')
                for field in ('hp', 'attack', 'defense', 'spAttack', 'spDefense'):
                    monster[field] = master['default' + field[0].upper() + field[1:]]
                state['player']['point'] += 1
                state['tutorial_evolution'] = {'userMonsterId': monster['userMonsterId'],
                    'fromMonsterId': captured['monsterId'], 'toMonsterId': master['monsterId'],
                    'grantedStones': 25, 'spentStones': 24}
            # APK FirstTutorial begins with download/fade/save100 before its prologue.
            # 400->500 contains dialogue/download only; farm creation follows save500.
            # 500->600 shows a client-side dummy farm and dialogue; capture follows 600.
            state['tutorial']['statusId'] = str(checkpoint)
            return {}
        if api == '160001':
            # No timed campaigns are configured on this local server.
            # Meal screens still request this list before applying base EXP.
            campaign_id = param.get('campaignId', 0)
            if type(campaign_id) is not int or campaign_id < 0:
                raise ProtocolError('Invalid campaignId')
            return {'campaignInfo': []}
        if api in ('020003', '020004'):
            field = 'nickname' if api == '020003' else 'description'
            value = param.get(field)
            if not isinstance(value, str) or len(value) > 256 or (field == 'nickname' and not value.strip()):
                raise ProtocolError('Invalid ' + field)
            state[field] = value
            return {}
        if api == '020010':
            state['policy'] = 1
            return {'resultStatus': 1}
        if api == '020015':
            # Local reconstruction has no external consent documents to display.
            if param.get('functionId', 1) != 1:
                raise ProtocolError('Unsupported GDPR info function')
            return {'gdprList': []}
        if api == '020301':
            options = param.get('optionList')
            if not isinstance(options, dict) or any(not isinstance(v, int) or isinstance(v, bool) for v in options.values()):
                raise ProtocolError('Invalid optionList')
            state['options'].update({k: str(v) for k, v in options.items()})
            return {}
        raise ProtocolError('Not implemented: API ' + api)

    def activities(self, db, account, activities):
        if not isinstance(activities, dict) or not activities or len(activities) > 100:
            raise ProtocolError('activityList must be a nonempty object with at most 100 entries')
        state = json.loads(account['state'])
        initialize_player_state(state)
        refresh_battle_expiry(state)
        compact_battle_history(state)
        self.update_picturebook(state)
        self.refresh_meat_capacity(state)
        self.chip_inventory.refresh_capacity(state)
        self.chip_inventory.refresh_slots(state)
        self.progression.refresh_day(state)
        state['options'] = dict(DEFAULT_OPTIONS, **state['options'])
        results = {}
        for api, param in activities.items():
            if not re.fullmatch(r'\d{6}', api) or not isinstance(param, dict):
                raise ProtocolError('Invalid activity')
            request_id = param.get('uniqueRequestId')
            if type(request_id) is not int:
                raise ProtocolError('Missing uniqueRequestId')
            if not -2147483648 <= request_id <= 2147483647:
                raise ProtocolError('Invalid uniqueRequestId range')
            normalized = json.dumps({k: v for k, v in param.items() if k != 'requestStatus'}, sort_keys=True)
            cached = db.execute('SELECT * FROM retries WHERE token=? AND api=? AND request_id=?',
                                (account['token'], api, request_id)).fetchone()
            if cached:
                if cached['input'] != normalized:
                    raise ProtocolError('Conflicting repeated uniqueRequestId')
                result = json.loads(cached['output'])
                if api == '090012':
                    # Resume is a query of the current encounter, not an award
                    # receipt. An old cached response must not revive an expiry.
                    result = copy.deepcopy(self.dispatch(api, param, account, state))
                    db.execute('UPDATE retries SET output=? WHERE token=? AND api=? AND request_id=?',
                               (json.dumps(result), account['token'], api, request_id))
                elif api == '090002':
                    started = state.get('active_battle')
                    if started and started['response']['startId'] != result['startId']:
                        started = state.get('battle_history', {}).get(result['startId'])
                    if started and battle_is_expired(started):
                        raise ProtocolError('Battle has expired')
                results[api] = result
            else:
                # Each activity's response is the state at that point in the batch.
                # Later activities can mutate the same monster/deck/list in place.
                raw = self.dispatch(api, param, account, state)
                self.chip_inventory.refresh_slots(state)
                result = self.chip_inventory.project_response(state, raw)
                self.update_picturebook(state)
                results[api] = result
                db.execute('INSERT INTO retries VALUES(?,?,?,?,?)',
                           (account['token'], api, request_id, normalized, json.dumps(result)))
        db.execute('UPDATE accounts SET state=? WHERE id=?', (json.dumps(state), account['id']))
        return {'venus_status': 1, 'osType': '2', 'compassCarrierId': 0, 'resData': results}

    def post(self, path, url, form, authorization, uuid=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if path == '/appApi/AuthUserByUuid':
                self.verify(db, 'POST', url, form, authorization, auth=True)
                return self.authenticate(db, form.get('x_uuid'))
            if path != '/app/ActiveController':
                raise ProtocolError('Unknown endpoint')
            account = self.verify(db, 'POST', url, form, authorization, claimed_uuid=uuid)
            try:
                activities = request_json(form['activityList'])
            except KeyError:
                raise ProtocolError('Invalid activityList JSON')
            try:
                return self.activities(db, account, activities)
            except ProtocolError as exc:
                state = json.loads(account['state'])
                exc.country = str(state.get('country_code', state.get('options', DEFAULT_OPTIONS).get('103', '3')))
                raise


class ThreadingHTTPServer(BaseThreadingHTTPServer):
    # Unity requests category catalogs in bursts; the default backlog of five
    # can time out valid local connections during startup.
    request_queue_size = 128


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            # Unity closes idle keep-alive sockets and cancels asset downloads.
            pass

    def reply(self, body, status=200):
        # The original GetBody blindly reads four characters of each API body.
        # Preserve empty-object semantics but pad it to avoid Substring(0, 4).
        encoder = json.JSONEncoder(ensure_ascii=False, separators=(',', ':'))
        raw = ''.join('{  }' if chunk == '{}' else chunk
                      for chunk in encoder.iterencode(body)).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        if self.close_connection:
            self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        # This APK's Version.txt retains a UTF-8 BOM in its runtime version string.
        path = path.replace('/asset/AB_DATA/ANDROID/\ufeff', '/asset/AB_DATA/ANDROID/', 1)
        if path in ('/help', '/help/', '/help/index.html', '/help/materials', '/help/HelpView', '/help/HelpView/', '/gacha/GachaChipList'):
            status, raw = self.server.store.help_pages.render(path, urlsplit(self.path).query)
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(raw)
        elif path == '/health':
            self.reply({'service': 'linkz-local', 'stage': 'local-reconstruction',
                        'configured_dungeons': len(self.server.store.normal_world),
                        'assets_ready': self.server.asset_root is not None})
        elif path == '/asset/local-index.json' and self.server.probe_assets:
            # Diagnostic only: no external bundles are present. Never invent files.
            self.reply({'version': 1, 'assetBundleFileInfo': []})
        elif path.startswith('/asset/') and self.server.asset_root:
            root = self.server.asset_root.resolve()
            target = (root / path.removeprefix('/asset/')).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                self.reply({'error': 'Asset file missing'}, 404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(target.stat().st_size))
            self.end_headers()
            with target.open('rb') as source:
                while chunk := source.read(1024 * 1024):
                    self.wfile.write(chunk)
        else:
            self.reply({'error': 'Not implemented'}, 404)

    def do_POST(self):
        body_consumed = False
        try:
            if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
                raise ProtocolError('Invalid content length')
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 2_000_000:
                raise ProtocolError('Invalid content length')
            if self.headers.get_content_type() != 'application/x-www-form-urlencoded':
                raise ProtocolError('Expected application/x-www-form-urlencoded')
            body = self.rfile.read(length)
            if len(body) != length:
                raise ProtocolError('Incomplete request body')
            body_consumed = True
            pairs = parse_qsl(body.decode('utf-8'), keep_blank_values=True)
            form = dict(pairs)
            if len(form) != len(pairs):
                raise ProtocolError('Duplicate form fields')
            url = self.server.public_base + self.path
            result = self.server.store.post(urlsplit(self.path).path, url, form,
                                           self.headers.get('Authorization', ''), self.headers.get('x-uuid'))
            self.reply(result)
        except (ProtocolError, ValueError, UnicodeError) as exc:
            # Do not let unread rejected bytes become the next keep-alive request.
            if not body_consumed:
                self.close_connection = True
                # Closing a Windows TCP socket with unread request bytes can
                # reset it before the caller receives this JSON error. Discard
                # only a bounded amount already arriving, never parse it as the
                # next request. A short timeout keeps malformed lengths from
                # blocking a worker while waiting for the declared huge body.
                previous_timeout = self.connection.gettimeout()
                try:
                    self.connection.settimeout(0.05)
                    remaining = 65536
                    while remaining:
                        chunk = self.rfile.read1(min(8192, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                except OSError:
                    pass
                finally:
                    self.connection.settimeout(previous_timeout)
            self.log_message('Protocol error: %s', str(exc))
            chinese = getattr(exc, 'country', '3') == '3'
            code = getattr(exc, 'code', None)
            message = server_error_message(str(exc)) if chinese else str(exc)
            if code:
                data = self.server.store.localization.masters if chinese else self.server.store.masters
                row = next(r for r in data['990801']['messageM'] if r['messageCode'] == code)
                message = row['messageText']
            self.reply({'venus_status': 2, 'message': message,
                        'subject': code or ('本地服务' if chinese else 'Local server'),
                        'e': code or str(exc), 'display': '1'})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=3000)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--public-base', help='Exact scheme and host used by the client for OAuth signatures')
    parser.add_argument('--database', type=Path, default=ROOT / 'runtime/linkz.sqlite3')
    parser.add_argument('--asset-root', type=Path, help='Asset directory containing catalog.json (000005 response) and files')
    parser.add_argument('--probe-assets', action='store_true', help='Diagnostic only: expose an empty external-bundle index')
    args = parser.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.public_base = (args.public_base or f'http://{args.host}:{args.port}').rstrip('/')
    httpd.asset_root = args.asset_root
    httpd.probe_assets = args.probe_assets
    catalog = None
    if args.asset_root:
        catalog = json.loads((args.asset_root / 'catalog.json').read_text(encoding='utf-8-sig'))
    elif args.probe_assets:
        catalog = {'assetBundleVersionList': [{'assetbundlePath': '', 'assetbundleGroup': '1',
                    'osType': '2', 'version': '1', 'accessPath': httpd.public_base + '/asset/local-index.json'}],
                   'versionManagerAsset': {'versionManagerId': '1', 'versionManagerType': '2',
                    'osType': '2', 'version': '1'}}
        print('DIAGNOSTIC MODE: external asset inventory is empty; gameplay is not available', flush=True)
    httpd.store = Store(args.database, resource_catalog=catalog)
    print(f'Linkz protocol foundation listening on {args.host}:{args.port}; gameplay incomplete', flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == '__main__':
    main()
