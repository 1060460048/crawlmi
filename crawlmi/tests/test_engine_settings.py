from twisted.trial import unittest

from crawlmi.http import Request, Response
from crawlmi.settings.engine_settings import EngineSettings


class EngineSettingsTest(unittest.TestCase):
    default_settings = {
        'a': 1, 'b': 1, 'c': 1, 'd': 1,
        'bool': True, 'int': '10', 'float': '2.3', 'list': '1,2,3'}
    module_settings = {'a': 2, 'b': 2, 'c': 2}
    spider_settings = {'a': 3, 'b': 3}
    custom_settings = {'a': 4}

    def setUp(self):
        self.settings = EngineSettings(
            default_settings=self.default_settings,
            module_settings=self.module_settings,
            spider_settings=self.spider_settings,
            custom_settings=self.custom_settings)

    def test_basic(self):
        self.assertIn('a', self.settings)
        self.assertNotIn('x', self.settings)
        self.assertEqual(self.settings['d'], 1)
        self.assertRaises(KeyError, self.settings.__getitem__, 'x')
        self.assertEqual(self.settings.get('d'), 1)
        self.assertIsNone(self.settings.get('x'))

    def test_req_or_resp(self):
        req = Request('http://github.com/', meta={'a': 10, 'x': 'y'})
        self.assertEqual(self.settings.get('a', req_or_resp=req), 10)
        self.assertEqual(self.settings.get('x', req_or_resp=req), 'y')
        resp = Response('', request=req)
        self.assertEqual(self.settings.get('a', req_or_resp=resp), 10)
        self.assertEqual(self.settings.get('x', req_or_resp=resp), 'y')

    def test_priority(self):
        self.assertEqual(self.settings.get('a'), 4)
        self.assertEqual(self.settings.get('b'), 3)
        self.assertEqual(self.settings.get('c'), 2)
        self.assertEqual(self.settings.get('d'), 1)

    def test_getters(self):
        self.assertEqual(self.settings.get_bool('bool'), True)
        self.assertEqual(self.settings.get_int('int'), 10)
        self.assertEqual(self.settings.get_float('float'), 2.3)
        self.assertListEqual(self.settings.get_list('list'), ['1', '2', '3'])

    def test_keys(self):
        keys = self.settings.keys()
        self.assertIsInstance(keys, list)
        keys2 = set(self.default_settings.keys() +
                    self.module_settings.keys() +
                    self.spider_settings.keys() +
                    self.custom_settings.keys())
        self.assertEqual(len(keys), len(set(keys)))
        self.assertSetEqual(set(keys), keys2)
