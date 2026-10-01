import copy
import hashlib
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

from help_pages import paragraphs
from linkz_server import Handler, Store, ThreadingHTTPServer
from localization import ChineseLocalization


class HelpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.database = Path(cls.temp.name) / 'test.sqlite3'
        cls.store = Store(cls.database)
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.httpd.store = cls.store
        cls.httpd.asset_root = None
        cls.httpd.probe_assets = False
        cls.base = f'http://127.0.0.1:{cls.httpd.server_port}'
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def get(self, path):
        try:
            response = urlopen(self.base + path, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            data = response.read()
            self.assertEqual(len(data), int(response.headers['Content-Length']))
            self.assertEqual(response.headers['Content-Type'], 'text/html; charset=utf-8')
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            return response.status, data.decode('utf-8')

    def test_all_original_ids_available_with_native_query_and_no_db_writes(self):
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        cache = self.database.parent / 'localization-version.json'
        revision = cache.read_bytes()
        for ident, row in self.store.help_pages.chinese.items():
            status, text = self.get('/help/HelpView/?helpId=' + ident + '&disabledVC=0')
            self.assertEqual(status, 200, ident)
            self.assertIn(row['helpTitle'], text)
            self.assertIn('本地版本说明', text)
            self.assertIn('原版说明', text)
            self.assertNotIn('<img', text)
            self.assertNotIn('<script', text)
            self.assertNotRegex(text, '[\u3040-\u30ff]')
        self.assertEqual(len(self.store.help_pages.chinese), 99)
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before)
        self.assertEqual(cache.read_bytes(), revision)
        self.assertEqual(self.get('/help/HelpView?helpId=13&disabledVC=0&legacy=1')[0], 200)

    def test_help_query_errors_and_unknown_id(self):
        for query in ('', 'helpId=', 'helpId=0', 'helpId=-1', 'helpId=01', 'helpId=1.0',
                      'helpId=1&helpId=2', 'helpId=1&countryCode=9',
                      'helpId=1&countryCode=3&countryCode=3', 'helpId=%FF',
                      'helpId=1&q=' + 'x' * 201, 'helpId=1&' + '&'.join(f'x{i}=1' for i in range(33)),
                      'helpId=1&extra=' + 'x' * 4096):
            self.assertEqual(self.get('/help/HelpView/?' + query)[0], 400, query[:80])
        self.assertEqual(self.get('/help/HelpView/?helpId=49')[0], 404)
        self.assertEqual(self.get('/help/HelpView/?helpId=999999')[0], 404)

    def test_index_search_orphan_category_and_material_identity(self):
        for path in ('/help', '/help/', '/help/index.html'):
            status, text = self.get(path)
            self.assertEqual(status, 200)
            self.assertEqual(len(re.findall(r'helpId=\d+', text)), 99)
            self.assertIn('历史数据迁移 / 其他', text)
            self.assertIn('helpId=71', text)
        status, text = self.get('/help/?' + urlencode({'q': '领取礼物'}))
        self.assertEqual(status, 200)
        self.assertIn('helpId=13', text)
        self.assertNotIn('helpId=71', text)
        status, text = self.get('/help/materials')
        self.assertEqual(status, 200)
        self.assertEqual(text.count('<tr><td>'), 171)
        for sid in ('1070177', '1076018'):
            self.assertIn('材料编号' + sid, text)
            filtered = self.get('/help/materials?' + urlencode({'q': sid}))[1]
            self.assertEqual(filtered.count('<tr><td>'), 1)
        fire = self.get('/help/materials?' + urlencode({'q': '火插件ver3.0'}))[1]
        for ident in (10, 38, 66):
            self.assertIn(f'第{ident}关 ×6', fire)
        self.assertEqual(len(self.store.help_pages.categories), 14)

    def test_escaped_search_and_historical_html_without_active_content(self):
        attack = '\"><script>alert(1)</script>&'
        text = self.get('/help/?' + urlencode({'q': attack}))[1]
        self.assertNotIn('<script', text)
        self.assertIn('&lt;script&gt;', text)
        self.assertNotIn('value="">', text)
        self.assertEqual(paragraphs('<div>safe<br>next<img src="https://bad/x"><script>evil</script>'
                                    '<style>evil</style>&lt;script&gt;</div>'),
                         '<p>safe</p><p>next&lt;script&gt;</p>')
        for country in ('1', '2'):
            status, text = self.get('/help/HelpView/?helpId=1&countryCode=' + country)
            self.assertEqual(status, 200)
            self.assertIn(self.store.masters['991002']['helpM'][0]['helpTitle'], text)
            self.assertIn('原始日文', text)
            self.assertNotIn('<img', text)

    def test_local_notes_match_configuration_and_unavailable_features(self):
        for ident, fragments in {
            '13': ('30天', '原版帮助', '每页20条', 'UTC+8', '先进入礼物箱'),
            '48': ('1小时', '3小时', '最多3只', '立即可免费孵化'),
            '56': ('甲虫兽', '5数码石', '300点', '90%', '9%', '1%', '没有额外保底'),
            '5': ('24小时', '+1友情', '30%概率增加1点友情', '不占用农场互动冷却'),
            '25': ('保留72小时', '已消耗体力不返还'),
            '37': ('最多两处同时施工',),
            '38': ('数码石而非金币', '完工确认后才增加容量', '999只'),
            '42': ('第16关', '1000金币', '重试和重连不会再次补满'),
            '43': ('第9关', '2000金币', '需要先建好道场'),
            '45': ('同名伙伴', '等概率是本地规则', '进化保留附加耐性', '状态耐性变化仅适用于版本对象', '再次消耗材料'),
            '44': ('本地技能继承', '满级素材'),
            '54': ('原48条', '不要求满级', '7200/12000/24000/36000/60000/108000',
                   '固有技能按原子编号', '互动冷却、队伍和芯片', '正常双向变化逐次收费',
                   '模式变化不计入本地普通进化任务', '特殊进化与core/hull版本转换仍未实现'),
            '66': ('不能用于此版本', '不要照历史步骤卸载'),
            '71': ('不能用于此版本',),
            '72': ('竞技场', '尚未开放'),
            '100': ('第70–76关', '第83关', '原事件材料来源尚未重建', '135000金币', '万能更新材料只能替代更新材料', '第二继承技能槽', '结转方式为本地规则'),
        }.items():
            text = self.get('/help/HelpView/?helpId=' + ident)[1]
            for fragment in fragments:
                self.assertIn(fragment, text, ident)
        self.assertNotIn('当前尚未开放或未完整恢复', self.get('/help/HelpView/?helpId=44')[1])
        self.assertNotIn('当前尚未开放或未完整恢复', self.get('/help/HelpView/?helpId=22')[1])

    def test_native_chip_lineup_and_help_current_scope(self):
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        status, text = self.get('/gacha/GachaChipList?gachaId=10003')
        self.assertEqual(status, 200)
        self.assertIn('100 Link点', text)
        self.assertIn('各占1/6', text)
        self.assertEqual(text.count('：1/6</li>'), 6)
        self.assertIn('原服务器概率未恢复', text)
        self.assertNotRegex(text, '[\u3040-\u30ff]')
        self.assertNotIn('<script', text)
        for query in ('gachaId=10001', 'gachaId=10003&gachaId=10003', 'gachaId=true', ''):
            self.assertEqual(self.get('/gacha/GachaChipList?' + query)[0], 404)
        for ident in ('90', '91', '92', '94'):
            text = self.get('/help/HelpView/?helpId=' + ident)[1]
            self.assertIn('48种', text)
            self.assertIn('当前没有开放工厂升级', text)
            self.assertIn('5个额外槽', text)
            self.assertIn('出售按等级×100金币', text)
            self.assertIn('即使等级不同，也不能同时装备', text)
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before)

    def test_advanced_chip_lineup_exact_odds_and_readonly_help(self):
        from fractions import Fraction
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        status, text = self.get('/gacha/GachaChipList?gachaId=10004&disabledVC=0')
        self.assertEqual(status, 200)
        self.assertIn('1000 Link点', text)
        self.assertIn('第83关', text)
        self.assertIn('确认完工', text)
        self.assertIn('十连没有额外保底', text)
        self.assertIn('原服务器进阶池未恢复', text)
        self.assertEqual(text.count('（单枚总概率）</li>'), 42)
        self.assertEqual(text.count('：1/30（单枚总概率）</li>'), 18)
        self.assertEqual(text.count('：1/40（单枚总概率）</li>'), 12)
        self.assertEqual(text.count('：3/200（单枚总概率）</li>'), 6)
        self.assertEqual(text.count('：1/600（单枚总概率）</li>'), 6)
        self.assertEqual(sum(Fraction(value) for value in re.findall(r'：(\d+/\d+)（单枚总概率）', text)), 1)
        self.assertNotRegex(text, '[\u3040-\u30ff]')
        self.assertNotIn('<script', text)
        for ident in ('90', '91', '92', '94'):
            notes = self.get('/help/HelpView/?helpId=' + ident)[1]
            self.assertIn('F级20%、E级20%、D级20%、C级15%、B级15%、A级9%、S级1%', notes)
            self.assertIn('本地进阶芯片捕捉', notes)
        self.assertEqual(self.get('/gacha/GachaChipList?gachaId=10004&gachaId=10004')[0], 404)
        self.assertEqual(self.get('/gacha/GachaChipList?gachaId=10004&x=%FF')[0], 400)
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before)

    def test_chinese_help_preserves_fields_and_invalidates_only_changed_help_table(self):
        store = self.store
        for api, table, fields in (('991001', 'helpCategoryM', {'helpCategoryName'}),
                                   ('991002', 'helpM', {'helpTitle', 'detail'})):
            source, translated = store.masters[api][table], store.localization.masters[api][table]
            self.assertEqual(len(source), len(translated))
            for before, after in zip(source, translated):
                self.assertEqual({k: v for k, v in before.items() if k not in fields},
                                 {k: v for k, v in after.items() if k not in fields})
                self.assertNotRegex(after[next(iter(fields))], '[\u3040-\u30ff]')
        raw = copy.deepcopy(store.masters['991002'])
        translated_help = copy.deepcopy(store.localization.help_texts)
        translated_help['articles']['13']['paragraphs'].append('测试修改礼物说明。')
        original_read = Path.read_text
        def read(path, *args, **kwargs):
            if path.name == 'help-texts.json':
                return json.dumps(translated_help, ensure_ascii=False)
            return original_read(path, *args, **kwargs)
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / 'version.json'
            before = ChineseLocalization(store.masters, store.chinese_labels,
                store.chinese_tutorial, store.chinese_ui_overrides, state)
            with patch.object(Path, 'read_text', read):
                after = ChineseLocalization(store.masters, store.chinese_labels,
                    store.chinese_tutorial, store.chinese_ui_overrides, state)
            self.assertEqual(after.revision, before.revision + 1)
            self.assertEqual(after.table_revisions['help_m'], after.revision)
            for table in before.tables - {'help_m'}:
                self.assertEqual(before.table_revisions[table], after.table_revisions[table], table)
        self.assertEqual(store.masters['991002'], raw)
