import copy
import json
from pathlib import Path
import unittest
from signature_press.service import process

def sample():
    return json.loads((Path(__file__).resolve().parents[1] / 'fixtures/input.json').read_text())

class PressTests(unittest.TestCase):
    def test_physical_order_and_conservation(self):
        r = process(sample())
        self.assertEqual(r['summary'], {'logical_pages': 10, 'signatures': 2, 'physical_sheets': 3, 'blank_pages': 2, 'collation_verified': True})
        sheets = r['signatures'][0]['sheets']
        self.assertEqual([[s['folio'] for s in x['sides']['front']] for x in sheets], [[8,1],[6,3]])
        self.assertEqual([[s['folio'] for s in x['sides']['back']] for x in sheets], [[2,7],[4,5]])
        self.assertEqual(r['collation'][3], {'section_id':'body', 'page':1})
        self.assertEqual(r['signatures'][1]['sheets'][0]['sides']['front'][0]['logical_page'], None)

    def test_right_binding_mirrors_each_side(self):
        d = sample(); d['binding'] = 'right'
        r = process(d)
        self.assertEqual([s['folio'] for s in r['signatures'][0]['sheets'][0]['sides']['front']], [1,8])
        self.assertEqual(r['collation'], process(sample())['collation'])

    def test_creep_moves_toward_fold(self):
        r = process(sample())
        outer, inner = r['signatures'][0]['sheets']
        self.assertEqual(inner['creep_um'], 120)
        self.assertEqual([s['placement']['x_um'] for s in inner['sides']['front']], [10120,219880])
        self.assertEqual([s['placement']['x_um'] for s in outer['sides']['front']], [10000,220000])

    def test_geometry_and_invalid_identity(self):
        for key, value in [('sheet_width_um',10),('sheet_height_um',20),('thickness_um',6000)]:
            d = sample(); d['geometry'][key] = value
            with self.assertRaises(ValueError): process(d)
        d = sample(); d['sections'][1]['id'] = 'intro'
        with self.assertRaises(ValueError): process(d)

    def test_strict_capacity_and_fields(self):
        for value in [True, 6, 0, '8']:
            d = sample(); d['signature_pages'] = value
            with self.assertRaises(ValueError): process(d)
        d = sample(); d['unexpected'] = 1
        with self.assertRaises(ValueError): process(d)

    def test_one_page_minimum_sheet(self):
        d=sample();d['sections']=[{'id':'one','pages':1}]
        self.assertEqual(process(d)['summary']['blank_pages'],3)

if __name__ == '__main__': unittest.main()
