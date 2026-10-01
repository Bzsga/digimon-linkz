"""Chinese text overlays. Gameplay identifiers and numeric fields stay original."""
import copy
import hashlib
import json
import re
from html import escape
from pathlib import Path
from skill_translation import translate_description
from master_translation import translate as translate_master

LOCALE_ROOT = Path(__file__).resolve().parent / 'localization/cn'
TEXT_FIELDS = frozenset(('assetTitle', 'monsterName', 'simpleDescription', 'description',
    'name', 'detail', 'facilityName', 'facilityKeyName', 'monsterGrowStepName',
    'soulName', 'helpCategoryName', 'helpTitle', 'title'))
ERROR_PREFIX = re.compile(r'^【[EC]-[A-Z]+\d+\s*】')
SERVER_ERRORS = json.loads((LOCALE_ROOT / 'server-errors.json').read_text(encoding='utf-8'))


def server_error_message(message):
    if message in SERVER_ERRORS:
        return SERVER_ERRORS[message]
    if message.startswith(('Not implemented:', 'Only ', 'Unsupported ', 'Colosseum reward collection')):
        return '当前本地版本尚未开放此功能。'
    if message.startswith(('Invalid ', 'Missing ')):
        return '请求信息不完整或无效，请重新打开菜单后重试。'
    return '操作未能完成，请重新连接后重试。'


def parameters(text):
    # Includes the alert master's positional {} replacements and .NET formats.
    return sorted(re.findall(r'\{[^{}]*\}|%[sd]|\$[A-Z_]+\$', text))


def compatible(source, translated):
    return (bool(translated) and source.count('{') == source.count('}') and
            translated.count('{') == translated.count('}') and
            (not source or parameters(source) == parameters(translated)))


class ChineseLocalization:
    def __init__(self, masters, bundled_labels, tutorial, ui, version_state=None):
        def read(name):
            return json.loads((LOCALE_ROOT / name).read_text(encoding='utf-8'))
        completion = read('ui-completion.json')
        repairs = read('ui-repairs.json')
        ui_terms = read('ui-dictionary.json')
        phrases = {**read('monster-names.json'), **read('master-texts.json')}
        errors = read('error-templates.json')
        tips = read('tips-messages.json')
        self.help_texts = read('help-texts.json')
        for api, table, section, ident in (
                ('991001', 'helpCategoryM', 'categories', 'helpCategoryId'),
                ('991002', 'helpM', 'articles', 'helpId')):
            if set(self.help_texts[section]) != {r[ident] for r in masters[api][table]}:
                raise ValueError('Chinese help IDs do not match original masters: ' + api)
        tutorial = {**tutorial, **read('tutorial-navigation.json')}
        simplified = str.maketrans(read('simplified-characters.json'))
        self.masters = {}
        self.tables = {'message_string_m', 'message_m', 'tutorial_navi_m'}
        for api, data in masters.items():
            if api in ('990802', '990801', '999101', '000004'):
                continue
            localized = copy.deepcopy(data)
            def translate(value):
                if isinstance(value, dict):
                    for key, item in value.items():
                        if key in TEXT_FIELDS and isinstance(item, str):
                            if api == '991001' and key == 'helpCategoryName':
                                value[key] = self.help_texts['categories'][value['helpCategoryId']]
                            elif api == '991002' and key in ('helpTitle', 'detail'):
                                article = self.help_texts['articles'][value['helpId']]
                                value[key] = (article['title'] if key == 'helpTitle' else
                                    '<article id="help">' + ''.join('<p>' + escape(p) + '</p>'
                                    for p in article['paragraphs']) + '</article>')
                            elif item in phrases:
                                value[key] = phrases[item]
                            elif api == '990108' and key == 'description':
                                value[key] = translate_description(item)
                            else:
                                value[key] = translate_master(api, key, item, phrases)
                            if not compatible(item, value[key]) and item:
                                raise ValueError('Chinese master parameter mismatch: ' + api + '.' + key)
                        elif api == '990501' and key == 'message':
                            value[key] = tips.get(value['tipsId'], item)
                            if not compatible(item, value[key]):
                                raise ValueError('Chinese tip parameter mismatch: ' + value['tipsId'])
                        else:
                            translate(item)
                elif isinstance(value, list):
                    for item in value:
                        translate(item)
            translate(localized)
            if localized != data:
                self.masters[api] = localized
                self.tables.update(TABLES.get(api, ()))
        source_labels = {r['messageCode']: r['messageText'] for r in masters['990802']['messageStringM']}
        labels = {r['messageCode']: dict(r) for r in masters['990802']['messageStringM']}
        for row in bundled_labels['messageStringM']:
            key = row['messageCode']
            source = labels.get(key, {}).get('messageText', '')
            if compatible(source, row['messageText']):
                labels[key] = dict(row)
        for key, row in labels.items():
            source = source_labels.get(key, '')
            if source in ui_terms:
                if not compatible(source, ui_terms[source]):
                    raise ValueError('Chinese UI dictionary parameter mismatch: ' + key)
                row['messageText'] = ui_terms[source]
        for key, value in {**completion, **repairs, **ui}.items():
            source = source_labels.get(key, '')
            if not compatible(source, value):
                raise ValueError('Chinese UI parameter mismatch: ' + key)
            labels[key] = {'messageCode': key, 'messageText': value}
        self.masters['990802'] = {'messageStringM': list(labels.values())}
        for row in labels.values():
            row['messageText'] = row['messageText'].translate(simplified)
        rows = masters['990801']['messageM']
        by_code = {r['messageCode']: r for r in rows}
        templates = {ERROR_PREFIX.sub('', by_code[key]['messageText']): text
                     for key, text in errors['texts'].items()}
        translated_errors = []
        for row in rows:
            result = dict(row)
            source = row['messageText']
            text = templates.get(ERROR_PREFIX.sub('', source))
            if text is None or row['messageTitle'] not in errors['titles']:
                raise ValueError('Missing Chinese alert: ' + row['messageCode'])
            prefix = ERROR_PREFIX.match(source)
            result['messageText'] = (prefix.group() if prefix else '') + text
            result['messageTitle'] = errors['titles'][row['messageTitle']]
            if not compatible(source, result['messageText']):
                raise ValueError('Chinese alert parameter mismatch: ' + row['messageCode'])
            translated_errors.append(result)
        self.masters['990801'] = {'messageM': translated_errors}
        tutorial_phrases = {}
        for row in masters['999101']['tutorialNaviM']:
            if row['tutorialNaviId'] in tutorial:
                tutorial_phrases.setdefault(row['message'], tutorial[row['tutorialNaviId']])
        # Anytime guidance uses different IDs for identical dialogue. Match the
        # original full sentence, retaining each row's ID and face animation.
        self.masters['999101'] = {'tutorialNaviM': [dict(r,
            message=tutorial.get(r['tutorialNaviId'], tutorial_phrases.get(r['message'], r['message'])))
            for r in masters['999101']['tutorialNaviM']]}
        for before, after in zip(masters['999101']['tutorialNaviM'], self.masters['999101']['tutorialNaviM']):
            if not compatible(before['message'], after['message']):
                raise ValueError('Chinese tutorial parameter mismatch: ' + before['tutorialNaviId'])
        # Unity checks local < server, so hashes alone cannot be cache versions.
        # Start above every revision used by the earlier hash-only implementation.
        digest = hashlib.sha256(json.dumps(self.masters, ensure_ascii=False,
            sort_keys=True).encode('utf-8')).hexdigest()
        self.revision = 300000000 + int(digest[:7], 16)
        table_content = {table: {} for table in self.tables}
        for api, data in self.masters.items():
            for table in TABLES.get(api, ()):
                table_content[table][api] = data
        for api, table in (('990802', 'message_string_m'), ('990801', 'message_m'), ('999101', 'tutorial_navi_m')):
            table_content[table][api] = self.masters[api]
        table_hashes = {table: hashlib.sha256(json.dumps(data, ensure_ascii=False,
                       sort_keys=True).encode('utf-8')).hexdigest() for table, data in table_content.items()}
        self.table_revisions = {table: self.revision for table in self.tables}
        if version_state is not None:
            path = Path(version_state)
            state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
            if state is not None:
                self.revision = int(state['revision']) + (state['sha256'] != digest)
            if not 300000000 <= self.revision < 2000000000:
                raise ValueError('Chinese master revision outside supported range')
            previous_tables = state.get('tables', {}) if state else {}
            new_tables = {}
            for table, table_digest in table_hashes.items():
                previous = previous_tables.get(table)
                # Legacy states bumped every translated table together.
                revision = self.revision
                if previous and previous['sha256'] == table_digest:
                    revision = int(previous['revision'])
                if not 300000000 <= revision <= self.revision:
                    raise ValueError('Invalid Chinese table revision: ' + table)
                self.table_revisions[table] = revision
                new_tables[table] = {'revision': revision, 'sha256': table_digest}
            updated_state = {'revision': self.revision, 'sha256': digest, 'tables': new_tables}
            if state != updated_state:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix('.tmp')
                temporary.write_text(json.dumps(updated_state, indent=2), encoding='utf-8')
                temporary.replace(path)

    def versions(self, source):
        result = copy.deepcopy(source)
        for row in result['dataVersionList']:
            if row['tableName'] in self.tables:
                for field in ('version', 'cacheVersion'):
                    row[field] = str(int(row[field]) + self.table_revisions[row['tableName']])
        return result


# Original GetTableName contracts (monster names share monster_m).
TABLES = {
    '990003': ('asset_category_m',), '990103': ('monster_m',),
    '990107': ('monster_resistance_m',), '990108': ('skill_m',),
    '990112': ('monster_grow_step_m',), '990201': ('soul_m',),
    '990301': ('facility_m',), '990308': ('facility_condition_m',),
    '990312': ('facility_key_m',), '990501': ('tips_m',),
    '990902': ('world_area_m',), '990903': ('world_area_m',),
    '990904': ('world_stage_m',), '990905': ('world_dungeon_m',),
    '991001': ('help_category_m',), '991002': ('help_m',),
    '991101': ('item_m',), '991301': ('chip_m',),
    '991501': ('dungeon_ticket_m',), '999103': ('title_m',),
    '991205': ('colosseum_rank_m',), '991402': ('event_point_bonus_m',)
}
