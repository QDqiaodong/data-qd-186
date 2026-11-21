import json
import subprocess
import sys
import unittest
from pathlib import Path

from signature_press.batch import process_batch
from signature_press.service import process

GEOMETRY = {'sheet_width_um': 440000, 'sheet_height_um': 320000, 'leaf_width_um': 200000,
            'leaf_height_um': 280000, 'margin_um': 10000, 'gutter_um': 10000, 'thickness_um': 120}
CREEP_GEOMETRY = dict(GEOMETRY, gutter_um=10000, thickness_um=6000)


def source(sections, cap=12, binding='left', geometry=None):
    return {'sections': sections, 'signature_pages': cap, 'binding': binding,
            'geometry': geometry or GEOMETRY}


def edition(eid, sections, revisions, cap=12, binding='left', geometry=None):
    return {'id': eid, 'source': source(sections, cap, binding, geometry),
            'revisions': list(revisions)}


def lot(lid, sections, revisions, retained, cap=12, binding='left', geometry=None):
    return {'id': lid, 'source': source(sections, cap, binding, geometry),
            'revisions': list(revisions), 'retained': retained}


def stock(stock_id, signature):
    return {'stock_id': stock_id, 'signature': signature}


def plan(batch):
    return process_batch(batch)


class SharedLegacyProofTests(unittest.TestCase):
    def scenario_one(self, mutate_yi=False):
        batch = {
            'editions': [
                edition('jia', [{'id': 'P', 'pages': 1}, {'id': 'A', 'pages': 8},
                                {'id': 'Q', 'pages': 3}], ['r1'] * 12),
                edition('yi', [{'id': 'A', 'pages': 8}], ['r1'] * 8),
            ],
            'legacy_lots': [
                lot('lot-a', [{'id': 'A', 'pages': 8}], ['r1'] * 8, [stock('s-a', 1)]),
            ],
        }
        if mutate_yi:
            batch['editions'][1]['revisions'][3] = 'rX'
        return batch

    def test_batch_optimum_prefers_joint_saving_over_per_edition_saving(self):
        result = plan(self.scenario_one())
        self.assertEqual(result['schema_version'], 2)
        self.assertEqual(result['summary'],
                         {'fresh_sheets': 3, 'blank_pages': 0, 'signatures': 2})
        jia, yi = result['editions']
        # Jia prints the whole 12-page book; Yi consumes the single retained piece.
        self.assertEqual([(s['source'], s.get('stock_id')) for s in jia['signatures']],
                         [('fresh_print', None)])
        self.assertEqual(jia['signatures'][0]['logical_start'], 1)
        self.assertEqual(jia['signatures'][0]['logical_end'], 12)
        self.assertEqual([(s['source'], s.get('stock_id')) for s in yi['signatures']],
                         [('retained', 's-a')])
        self.assertTrue(all(e['collation_verified'] for e in result['editions']))
        self.assertTrue(result['optimization_verified'])

    def test_revision_mismatch_blocks_reuse_and_recuts_other_edition(self):
        result = plan(self.scenario_one(mutate_yi=True))
        # Jia recuts into 1 / 8 / 3 and reuses the middle signature; Yi prints fresh.
        self.assertEqual(result['summary']['fresh_sheets'], 4)
        jia, yi = result['editions']
        self.assertEqual([(s['logical_start'], s['logical_end'], s['source'], s.get('stock_id'))
                          for s in jia['signatures']],
                         [(1, 1, 'fresh_print', None),
                          (2, 9, 'retained', 's-a'),
                          (10, 12, 'fresh_print', None)])
        self.assertEqual([(s['source'], s.get('stock_id')) for s in yi['signatures']],
                         [('fresh_print', None)])
        # The reused signature carries the original folio layout of the legacy piece.
        reused = jia['signatures'][1]
        self.assertEqual(reused['lot_id'], 'lot-a')
        self.assertEqual(reused['legacy_signature'], 1)
        self.assertTrue(all(e['collation_verified'] for e in result['editions']))

    def test_creep_conflict_splits_eight_pages_into_two_four_page_signatures(self):
        ed = edition('creep', [{'id': 'B', 'pages': 8}], ['r'] * 8, cap=8,
                     binding='right', geometry=CREEP_GEOMETRY)
        result = plan({'editions': [ed], 'legacy_lots': []})
        signatures = result['editions'][0]['signatures']
        self.assertEqual([(s['logical_start'], s['logical_end'], s['folio_count'])
                          for s in signatures], [(1, 4, 4), (5, 8, 4)])
        self.assertEqual(result['summary']['fresh_sheets'], 2)
        # Every depth must satisfy the creep bound 2*depth*thickness <= gutter.
        for entry in signatures:
            for sheet in entry['sheets']:
                self.assertLessEqual(2 * sheet['creep_um'], CREEP_GEOMETRY['gutter_um'])

    def test_opposite_binding_legacy_piece_is_physically_non_equivalent(self):
        # A geometrically valid 4-page left-bound piece cannot feed a right-bound book.
        legacy = lot('left-lot', [{'id': 'B', 'pages': 4}], ['r'] * 4, [stock('left-s', 1)],
                     cap=8, binding='left', geometry=CREEP_GEOMETRY)
        ed = edition('right-book', [{'id': 'B', 'pages': 4}], ['r'] * 4, cap=8,
                     binding='right', geometry=CREEP_GEOMETRY)
        result = plan({'editions': [ed], 'legacy_lots': [legacy]})
        self.assertEqual([(s['source'], s.get('stock_id'))
                          for s in result['editions'][0]['signatures']],
                         [('fresh_print', None)])
        self.assertEqual(result['summary']['fresh_sheets'], 1)

    def test_reused_piece_keeps_traceability_and_revision_collation(self):
        legacy = lot('trace-lot', [{'id': 'X', 'pages': 4}], ['rev-1', 'rev-2', 'rev-3', 'rev-4'],
                     [stock('trace-stock', 1)], cap=4)
        ed = edition('trace-book', [{'id': 'X', 'pages': 4}],
                     ['rev-1', 'rev-2', 'rev-3', 'rev-4'], cap=4)
        result = plan({'editions': [ed], 'legacy_lots': [legacy]})
        entry = result['editions'][0]['signatures'][0]
        self.assertEqual(entry['source'], 'retained')
        self.assertEqual(entry['stock_id'], 'trace-stock')
        self.assertEqual(entry['lot_id'], 'trace-lot')
        self.assertEqual(entry['legacy_signature'], 1)
        self.assertEqual(result['editions'][0]['collation'],
                         [{'section_id': 'X', 'page': p, 'revision': f'rev-{p}'}
                          for p in range(1, 5)])
        # Sheets expose full double-sided slots with folio and placement.
        sheet = entry['sheets'][0]
        self.assertEqual([slot['folio'] for slot in sheet['sides']['front']], [4, 1])
        self.assertTrue(all('placement' in slot for side in sheet['sides'].values()
                            for slot in side))

    def test_middle_signature_blank_padding_does_not_pollute_collation(self):
        # Capacity four, eleven pages. The deterministic tie rule cuts earliest,
        # producing a padded 3-page signature whose blank slot stays internal.
        ed = edition('padded', [{'id': 'X', 'pages': 11}], ['r'] * 11, cap=4)
        result = plan({'editions': [ed], 'legacy_lots': []})
        signatures = result['editions'][0]['signatures']
        self.assertEqual([(s['logical_start'], s['logical_end']) for s in signatures],
                         [(1, 3), (4, 7), (8, 11)])
        self.assertEqual([s['blank_count'] for s in signatures], [1, 0, 0])
        self.assertEqual(result['summary']['blank_pages'], 1)
        self.assertEqual(len(result['editions'][0]['collation']), 11)
        self.assertEqual(result['editions'][0]['collation'],
                         [{'section_id': 'X', 'page': p, 'revision': 'r'} for p in range(1, 12)])
        # The blank physically sits inside the first signature only.
        nulls = [slot for sheet in signatures[0]['sheets'] for side in sheet['sides'].values()
                 for slot in side if slot['logical_page'] is None]
        self.assertEqual(len(nulls), 1)
        self.assertTrue(all(slot['logical_page'] is not None
                            for s in signatures[1:] for sheet in s['sheets']
                            for side in sheet['sides'].values() for slot in side))

    def test_equivalent_pieces_contend_with_stable_stock_id_decision(self):
        ed = edition('contended', [{'id': 'X', 'pages': 8}], ['r'] * 8, cap=4)
        lots = [
            lot('LA', [{'id': 'X', 'pages': 4}], ['r'] * 4, [stock('stkA', 1)], cap=4),
            lot('LZ', [{'id': 'X', 'pages': 4}], ['r'] * 4, [stock('stkZ', 1)], cap=4),
            lot('LB', [{'id': 'X', 'pages': 8}], ['r'] * 8, [stock('stkB', 2)], cap=4),
        ]
        chosen = [(s['source'], s.get('stock_id'))
                  for s in plan({'editions': [ed], 'legacy_lots': lots})['editions'][0]['signatures']]
        reversed_lots = [(s['source'], s.get('stock_id'))
                         for s in plan({'editions': [ed],
                                        'legacy_lots': list(reversed(lots))})['editions'][0]['signatures']]
        self.assertEqual(chosen, [('retained', 'stkA'), ('retained', 'stkB')])
        self.assertEqual(reversed_lots, chosen)

    def test_no_inventory_is_allowed_and_always_feasible(self):
        ed = edition('solo', [{'id': 'X', 'pages': 5}], ['r'] * 5, cap=4)
        result = plan({'editions': [ed], 'legacy_lots': []})
        self.assertEqual(result['summary']['fresh_sheets'], 2)
        self.assertTrue(all(s['source'] == 'fresh_print'
                            for s in result['editions'][0]['signatures']))

    def test_two_editions_cannot_double_issue_one_piece(self):
        # Both editions match the only piece; the second must print fresh.
        legacy = lot('shared', [{'id': 'X', 'pages': 4}], ['r'] * 4, [stock('only', 1)], cap=4)
        batch = {'editions': [edition('one', [{'id': 'X', 'pages': 4}], ['r'] * 4, cap=4),
                              edition('two', [{'id': 'X', 'pages': 4}], ['r'] * 4, cap=4)],
                 'legacy_lots': [legacy]}
        result = plan(batch)
        sources = [[(s['source'], s.get('stock_id')) for s in e['signatures']]
                   for e in result['editions']]
        self.assertEqual(sources, [[('retained', 'only')], [('fresh_print', None)]])
        self.assertEqual(result['summary']['fresh_sheets'], 1)

    def test_legal_but_non_matching_pieces_are_ignored_not_rejected(self):
        # Different content, different size and a printable-ASCII revision with
        # punctuation are all legal; the pieces simply never get issued.
        legal_mismatch = lot('lm', [{'id': 'Z', 'pages': 8}], ['rev#1'] * 8,
                             [stock('big-piece', 1)], cap=12)
        ed = edition('book', [{'id': 'X', 'pages': 4}], ['r'] * 4, cap=4)
        result = plan({'editions': [ed], 'legacy_lots': [legal_mismatch]})
        self.assertTrue(all(s['source'] == 'fresh_print'
                            for s in result['editions'][0]['signatures']))
        # A long, punctuation-rich revision identifier is accepted.
        long_rev = 'a' * 200 + '#-:_~'
        ed2 = edition('book2', [{'id': 'X', 'pages': 1}], [long_rev], cap=4)
        result2 = plan({'editions': [ed2], 'legacy_lots': []})
        self.assertEqual(result2['editions'][0]['collation'][0]['revision'], long_rev)

    def test_pieces_cannot_be_split_or_partially_consumed(self):
        # An 8-page physical signature cannot feed a 4-page segment.
        piece = lot('whole', [{'id': 'X', 'pages': 8}], ['r'] * 8, [stock('eight', 1)], cap=12)
        ed = edition('half', [{'id': 'X', 'pages': 4}], ['r'] * 4, cap=4)
        result = plan({'editions': [ed], 'legacy_lots': [piece]})
        self.assertEqual([s['source'] for s in result['editions'][0]['signatures']],
                         ['fresh_print'])

    def test_single_segment_picks_lexicographically_smallest_stock_id(self):
        ed = edition('one-seg', [{'id': 'X', 'pages': 4}], ['r'] * 4, cap=4)
        lots = [
            lot('L2', [{'id': 'X', 'pages': 4}], ['r'] * 4, [stock('zzz', 1)], cap=4),
            lot('L1', [{'id': 'X', 'pages': 4}], ['r'] * 4, [stock('aaa', 1)], cap=4),
        ]
        result = plan({'editions': [ed], 'legacy_lots': lots})
        self.assertEqual(result['editions'][0]['signatures'][0]['stock_id'], 'aaa')

    def test_minimum_one_page_book_pads_to_four_folios(self):
        ed = edition('tiny', [{'id': 'X', 'pages': 1}], ['r'], cap=4)
        result = plan({'editions': [ed], 'legacy_lots': []})
        sig = result['editions'][0]['signatures'][0]
        self.assertEqual((sig['logical_start'], sig['logical_end'], sig['folio_count'],
                          sig['blank_count']), (1, 1, 4, 3))
        self.assertEqual(result['summary'],
                         {'fresh_sheets': 1, 'blank_pages': 3, 'signatures': 1})

    def test_invalid_inventory_references_are_input_errors(self):
        good = edition('book', [{'id': 'X', 'pages': 4}], ['r'] * 4, cap=4)
        cases = [
            # retained signature number absent from the reconstructed legacy book
            {'editions': [good],
             'legacy_lots': [lot('L', [{'id': 'X', 'pages': 4}], ['r'] * 4,
                             [stock('s', 9)], cap=4)]},
            # duplicate stock id across lots
            {'editions': [good],
             'legacy_lots': [lot('L1', [{'id': 'X', 'pages': 4}], ['r'] * 4, [stock('dup', 1)], cap=4),
                             lot('L2', [{'id': 'X', 'pages': 4}], ['r'] * 4, [stock('dup', 1)], cap=4)]},
            # duplicate signature number within one lot
            {'editions': [good],
             'legacy_lots': [lot('L', [{'id': 'X', 'pages': 8}], ['r'] * 8,
                             [stock('a', 1), stock('b', 1)], cap=8)]},
            # revision list length does not match the expanded pages
            {'editions': [edition('bad', [{'id': 'X', 'pages': 4}], ['r'] * 3, cap=4)],
             'legacy_lots': []},
            # empty revision identifier
            {'editions': [edition('bad', [{'id': 'X', 'pages': 1}], [''], cap=4)],
             'legacy_lots': []},
            # edition exceeds 512 logical pages
            {'editions': [edition('big', [{'id': 'X', 'pages': 513}], ['r'] * 513)],
             'legacy_lots': []},
            # geometrically invalid legacy ticket is rejected even if never reusable
            {'editions': [edition('ok', [{'id': 'Y', 'pages': 4}], ['q'] * 4, cap=8,
                                  binding='right', geometry=CREEP_GEOMETRY)],
             'legacy_lots': [lot('broken', [{'id': 'B', 'pages': 8}], ['r'] * 8,
                             [stock('s', 1)], cap=8, binding='left', geometry=CREEP_GEOMETRY)]},
        ]
        for case in cases:
            with self.assertRaises(ValueError):
                plan(case)

    def test_illegal_structure_and_identity_rules(self):
        good = edition('book', [{'id': 'X', 'pages': 4}], ['r'] * 4, cap=4)
        cases = [
            {'editions': [], 'legacy_lots': []},
            {'editions': [good, good, good], 'legacy_lots': []},
            {'editions': [good], 'legacy_lots': [], 'unexpected': 1},
        ]
        for case in cases:
            with self.assertRaises(ValueError):
                plan(case)
        duplicate_id = {'editions': [good, dict(good)], 'legacy_lots': []}
        with self.assertRaises(ValueError):
            plan(duplicate_id)


class CliAtomicityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__file__).resolve().parent / '_tmp_cli'
        self.tmp.mkdir(exist_ok=True)

    def tearDown(self):
        for child in self.tmp.iterdir():
            child.unlink()
        self.tmp.rmdir()

    def _run(self, source_path, output_path, proof=False):
        args = [sys.executable, '-m', 'signature_press.cli', '--source', str(source_path),
                '--output', str(output_path)]
        if proof:
            args.append('--proof-batch')
        return subprocess.run(args, capture_output=True, text=True)

    def test_v1_cli_still_works_and_refuses_existing_output(self):
        source = self.tmp / 'v1.json'
        output = self.tmp / 'v1-out.json'
        source.write_text(json.dumps({
            'sections': [{'id': 'intro', 'pages': 3}, {'id': 'body', 'pages': 7}],
            'signature_pages': 8, 'binding': 'left', 'geometry': GEOMETRY}))
        first = self._run(source, output)
        self.assertEqual(first.returncode, 0, first.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report['schema_version'], 1)
        self.assertEqual(report['summary']['physical_sheets'], 3)
        # Re-running to the same path must fail with exit 2 and leave bytes untouched.
        before = output.read_bytes()
        second = self._run(source, output)
        self.assertEqual(second.returncode, 2)
        self.assertEqual(output.read_bytes(), before)

    def test_proof_batch_cli_publishes_v2_and_invalid_input_leaves_nothing(self):
        batch = {
            'editions': [edition('jia', [{'id': 'P', 'pages': 1}, {'id': 'A', 'pages': 8},
                                         {'id': 'Q', 'pages': 3}], ['r1'] * 12),
                         edition('yi', [{'id': 'A', 'pages': 8}], ['r1'] * 8)],
            'legacy_lots': [lot('lot-a', [{'id': 'A', 'pages': 8}], ['r1'] * 8,
                            [stock('s-a', 1)])],
        }
        source = self.tmp / 'batch.json'
        output = self.tmp / 'batch-out.json'
        source.write_text(json.dumps(batch))
        ok = self._run(source, output, proof=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report['schema_version'], 2)
        self.assertEqual(report['summary']['fresh_sheets'], 3)
        # Existing target: failure with no overwrite and no half-written sibling.
        before = output.read_bytes()
        siblings = sorted(p.name for p in self.tmp.iterdir())
        again = self._run(source, output, proof=True)
        self.assertEqual(again.returncode, 2)
        self.assertEqual(output.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), siblings)
        # Invalid batch: exit 2, no new file, no temp residue.
        bad_source = self.tmp / 'bad.json'
        bad_output = self.tmp / 'bad-out.json'
        broken = json.loads(json.dumps(batch))
        broken['legacy_lots'][0]['retained'][0]['signature'] = 99
        bad_source.write_text(json.dumps(broken))
        failed = self._run(bad_source, bad_output, proof=True)
        self.assertEqual(failed.returncode, 2)
        self.assertFalse(bad_output.exists())
        self.assertFalse(any(p.name.startswith('.report-') for p in self.tmp.iterdir()))


if __name__ == '__main__':
    unittest.main()
