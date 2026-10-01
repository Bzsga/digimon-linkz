import copy
import json
import re
import tempfile
import unittest
from pathlib import Path

from linkz_server import Store
from localization import ChineseLocalization, TEXT_FIELDS, compatible, parameters
from skill_translation import translate_description


class LocalizationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.store = Store(Path(cls.temp.name) / 'test.sqlite3')

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_ui_and_alerts_have_no_japanese_and_keep_parameters(self):
        for api, table in (('990802', 'messageStringM'), ('990801', 'messageM')):
            source = {r['messageCode']: r for r in self.store.masters[api][table]}
            rows = self.store.localization.masters[api][table]
            self.assertEqual(len(rows), len({r['messageCode'] for r in rows}))
            for row in rows:
                for field in ('messageTitle', 'messageText'):
                    if field not in row:
                        continue
                    value = row[field]
                    self.assertIsNone(re.search('[\u3040-\u30ff]', value), row['messageCode'])
                    original = source.get(row['messageCode'], {}).get(field, '')
                    if original:
                        self.assertEqual(parameters(value), parameters(original), row['messageCode'])
            self.assertTrue(set(source).issubset(r['messageCode'] for r in rows))
        labels = {r['messageCode']: r['messageText'] for r in self.store.localization.masters['990802']['messageStringM']}
        self.assertIn('{1:D2}', labels['CountDownHM'])
        self.assertEqual(labels['CharaStatus-19'], '固有技能')
        self.assertEqual(labels['QuestDetailsSoloButton'], '单人')
        self.assertEqual(labels['PartyBattleStart'], '开始战斗')
        self.assertEqual(labels['SystemNavigator'], '阳菜')

    def test_translations_only_change_text_preserve_gameplay(self):
        def without_text(value):
            if isinstance(value, dict):
                return {k: without_text(v) for k, v in value.items()
                        if k not in TEXT_FIELDS | {'messageTitle', 'messageText', 'message'}}
            if isinstance(value, list):
                return [without_text(v) for v in value]
            return value
        for api, translated in self.store.localization.masters.items():
            if api == '990802':  # UI includes later bundled keys.
                continue
            self.assertEqual(without_text(translated), without_text(self.store.masters[api]), api)
        source = self.store.masters['990103']['monsterM']
        self.assertTrue(any(r['monsterName'] == 'アグモン' for r in source))

    def test_cache_revision_changes_with_translation_content(self):
        store = self.store
        ui = dict(store.chinese_ui_overrides, StaminaFull='体力已恢复满')
        updated = ChineseLocalization(store.masters, store.chinese_labels, store.chinese_tutorial, ui)
        self.assertNotEqual(updated.revision, store.localization.revision)
        source = copy.deepcopy(store.masters['000004'])
        versions = updated.versions(source)
        self.assertEqual(source, store.masters['000004'])
        actual = {r['tableName'] for r in source['dataVersionList']}
        self.assertTrue(updated.tables.issubset(actual))
        for before, after in zip(source['dataVersionList'], versions['dataVersionList']):
            for field in ('version', 'cacheVersion'):
                delta = updated.revision if before['tableName'] in updated.tables else 0
                self.assertEqual(int(after[field]), int(before[field]) + delta)
                self.assertLess(int(after[field]), 2**31)

    def test_incomplete_bundled_formats_are_rejected(self):
        self.assertFalse(compatible('剩余{0}小时{1:D2}分', '剩余'))
        self.assertFalse(compatible('要领取吗？', '要使用{0}张'))
        self.assertFalse(compatible('剩余{0}', '剩余{0'))
        self.assertTrue(compatible('{0}×{1}', '{1}个{0}'))

    def test_cache_revision_is_monotonic_across_updates_and_rollbacks(self):
        store = self.store
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'version.json'
            def build(ui):
                return ChineseLocalization(store.masters, store.chinese_labels,
                    store.chinese_tutorial, ui, path).revision
            first = build(store.chinese_ui_overrides)
            self.assertGreater(first, 268535455)  # Maximum former hash revision.
            self.assertEqual(build(store.chinese_ui_overrides), first)
            updated = dict(store.chinese_ui_overrides, StaminaFull='体力已恢复满')
            self.assertEqual(build(updated), first + 1)
            self.assertEqual(build(updated), first + 1)
            self.assertEqual(build(store.chinese_ui_overrides), first + 2)

    def test_ui_edit_only_invalidates_its_table_and_migrates_legacy_state(self):
        store = self.store
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'version.json'
            def build(ui):
                return ChineseLocalization(store.masters, store.chinese_labels,
                    store.chinese_tutorial, ui, path)
            first = build(store.chinese_ui_overrides)
            legacy = json.loads(path.read_text(encoding='utf-8'))
            legacy.pop('tables')
            path.write_text(json.dumps(legacy), encoding='utf-8')
            migrated = build(store.chinese_ui_overrides)
            self.assertEqual(migrated.revision, first.revision)
            self.assertEqual(migrated.table_revisions, first.table_revisions)
            updated = build(dict(store.chinese_ui_overrides, StaminaFull='体力已恢复满'))
            self.assertEqual(updated.table_revisions['message_string_m'], first.revision + 1)
            for table in updated.tables - {'message_string_m'}:
                self.assertEqual(updated.table_revisions[table], first.table_revisions[table], table)

    def test_skill_translation_preserves_targets_conditions_and_numbers(self):
        examples = {
            '火属性で敵単体に複数回物理大ダメージ': '对单个敌人多次造成大量火属性物理伤害',
            '3ラウンドの間、味方全体の魔法攻撃力が極小アップ': '3回合内，所有我方的魔法攻击力微量提升',
            '25%の確率で敵全体を眠りにする': '25%概率使所有敌人睡眠',
            '火属性スキル攻撃を受ける時、バトル中に2回まで被ダメージ-30%': '受到火属性技能攻击时，承受伤害减少30%，每场战斗最多2次',
            'バトル中に3回まで敵を戦闘不能にすると2ターン与ダメ20%アップ': '击倒敌人后，2回合内造成伤害提升20%，每场战斗最多3次',
        }
        for original, expected in examples.items():
            self.assertEqual(translate_description(original), expected)
        self.assertEqual(translate_description('未知の効果、確率で毒にする'), '未知の効果、確率で毒にする')
        original = self.store.masters['990108']['skillM']
        translated = self.store.localization.masters['990108']['skillM']
        for before, after in zip(original, translated):
            self.assertEqual(sorted(re.findall(r'\d+(?:\.\d+)?', before['description'])),
                             sorted(re.findall(r'\d+(?:\.\d+)?', after['description'])), before['skillId'])
            self.assertIsNone(re.search('[\u3040-\u30ff]', after['description']), before['skillId'])

    def test_materials_facilities_skill_names_and_tips_are_translated(self):
        for api, table, fields in (
            ('990103', 'monsterM', ('monsterName',)),
            ('990108', 'skillM', ('name', 'description')),
            ('990201', 'soulM', ('soulName', 'description')),
            ('990301', 'facilityM', ('facilityName', 'description')),
            ('990308', 'facilityConditionM', ('description',)),
            ('990312', 'facilityKeyM', ('facilityKeyName',)),
            ('991001', 'helpCategoryM', ('helpCategoryName',)),
        ):
            before = self.store.masters[api][table]
            after = self.store.localization.masters[api][table]
            for original, translated in zip(before, after):
                for field in fields:
                    self.assertIsNone(re.search('[\u3040-\u30ff]', translated[field]), (api, field, translated[field]))
                    self.assertEqual(parameters(original[field]), parameters(translated[field]))
                    self.assertEqual(sorted(re.findall(r'\d+(?:\.\d+)?', original[field])),
                                     sorted(re.findall(r'\d+(?:\.\d+)?', translated[field])), (api, original[field]))
        tips = self.store.localization.masters['990501']['tipsM']['tips']
        self.assertEqual(len(tips), 60)
        self.assertIn('{0}', next(r['message'] for r in tips if r['tipsId'] == '45'))
        self.assertTrue(all(not re.search('[\u3040-\u30ff]', r['message']) for r in tips))
        tutorial = {r['tutorialNaviId']: r for r in self.store.localization.masters['999101']['tutorialNaviM']}
        self.assertEqual(tutorial['2021']['message'], tutorial['1107']['message'])
        self.assertIn('进化碎片', tutorial['2021']['message'])
        self.assertNotRegex(tutorial['2021']['message'], '[\u3040-\u30ff]')


if __name__ == '__main__':
    unittest.main()
