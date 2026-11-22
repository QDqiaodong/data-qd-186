import copy
import json
import random
import subprocess
import sys
import unittest
from pathlib import Path

from signature_press.batch import process_batch
from signature_press.recall import process_recall

GEOMETRY = {'sheet_width_um': 440000, 'sheet_height_um': 320000, 'leaf_width_um': 200000,
            'leaf_height_um': 280000, 'margin_um': 10000, 'gutter_um': 10000,
            'thickness_um': 120}


def edition(eid, pages=16, cap=8, binding='left', geometry=None):
    return {'id': eid,
            'source': {'sections': [{'id': 'T', 'pages': pages}],
                       'signature_pages': cap, 'binding': binding,
                       'geometry': geometry or GEOMETRY},
            'revisions': ['r1'] * pages}


def proof_batch(*editions, legacy=None):
    return {'editions': list(editions), 'legacy_lots': legacy or []}


def lot(lot_id, eid, signature, count):
    return {'lot_id': lot_id, 'edition_id': eid, 'signature': signature, 'count': count}


def bind(lot_id, first_piece, first_copy, count, step=1):
    return {'lot_id': lot_id, 'first_piece': first_piece, 'first_copy': first_copy,
            'count': count, 'piece_step': step}


def pack(eid, first_copy, copy_step, series, first_carton, carton_count, per_carton):
    return {'edition_id': eid, 'first_copy': first_copy, 'copy_step': copy_step,
            'carton_series': series, 'first_carton': first_carton,
            'carton_count': carton_count, 'books_per_carton': per_carton}


def dispatch(series, first_carton, last_carton, consignee):
    return {'carton_series': series, 'first_carton': first_carton,
            'last_carton': last_carton, 'consignee_id': consignee}


def alert(lot_id, first_piece, last_piece):
    return {'lot_id': lot_id, 'first_piece': first_piece, 'last_piece': last_piece}


def make(batch, copies, lots, binds, packs, dispatches=(), alerts=()):
    return {'proof_batch': batch, 'copies': list(copies), 'print_lots': list(lots),
            'bind_spans': list(binds), 'pack_spans': list(packs),
            'dispatches': list(dispatches), 'alerts': list(alerts)}


# ------------------------------------------------------------- acceptance model

def acceptance_inputs():
    """The exact two-signature, sixteen-copy scenario from the task contract."""
    batch = proof_batch(edition('A'))
    copies = [{'edition_id': 'A', 'count': 16}]
    lots = [lot('L', 'A', 1, 18), lot('M', 'A', 2, 16)]
    binds = [bind('L', 8, 1, 8, -1), bind('L', 9, 9, 8, 1), bind('M', 1, 1, 16, 1)]
    packs = [pack('A', 16, -1, 'C', 100, 4, 4)]
    alerts = [alert('L', 6, 7), alert('L', 15, 18), alert('M', 3, 4)]
    return batch, copies, lots, binds, packs, alerts


def ranges(items, key_first, key_last):
    return [(item[key_first], item[key_last]) for item in items]


def expand(items, key_first, key_last):
    result = set()
    for item in items:
        for value in range(item[key_first], item[key_last] + 1):
            result.add(value)
    return result


def expanded_editions(report):
    return {entry['edition_id']: entry for entry in report['editions']}


class AcceptanceTests(unittest.TestCase):
    def base_report(self, dispatches):
        batch, copies, lots, binds, packs, alerts = acceptance_inputs()
        return process_recall(make(batch, copies, lots, binds, packs, dispatches, alerts))

    def test_dispatch_only_to_d(self):
        report = self.base_report([dispatch('C', 103, 103, 'D')])
        entry = expanded_editions(report)['A']
        self.assertEqual(report['schema_version'], 6)
        self.assertEqual(ranges(entry['affected_copies'], 'first', 'last'),
                         [(2, 4), (15, 16)])
        self.assertEqual(expand(entry['affected_copies'], 'first', 'last'),
                         {2, 3, 4, 15, 16})
        # Copy 3 is affected by two signatures, so both provenance rows survive
        # while the copy itself is counted once.
        evidence = entry['incoming_evidence']
        self.assertEqual([(e['signature'], e['copy_first'], e['copy_last'],
                           e['piece_first'], e['piece_last'], e['piece_step'])
                          for e in evidence],
                         [(1, 2, 3, 6, 7, -1), (1, 15, 16, 15, 16, 1),
                          (2, 3, 4, 3, 4, 1)])
        # Unbound affected pieces L17-L18 remain in the stack.
        self.assertEqual(report['unbound_pieces'],
                         [{'lot_id': 'L', 'edition_id': 'A', 'signature': 1,
                           'pieces': [{'first_piece': 17, 'last_piece': 18}]}])
        # C100 is the only factory carton holding an affected book (copy 16).
        self.assertEqual(report['held_cartons'],
                         [{'carton_series': 'C',
                           'ranges': [{'first_carton': 100, 'last_carton': 100}]}])
        self.assertEqual(ranges(entry['held_good_copies'], 'first', 'last'),
                         [(13, 14)])
        self.assertEqual(entry['in_factory_affected'],
                         [{'first': 15, 'last': 16}])
        # D locates C103 and recalls only copies 2-4.
        self.assertEqual(report['consignees'],
                         [{'consignee_id': 'D',
                           'cartons': [{'carton_series': 'C',
                                        'ranges': [{'first_carton': 103,
                                                    'last_carton': 103}]}],
                           'editions': [{'edition_id': 'A',
                                         'recall_copies': [{'first': 2,
                                                            'last': 4}]}]}])
        self.assertEqual(report['summary'], {
            'affected_copies': 5, 'in_factory_affected_copies': 2,
            'dispatched_affected_copies': 3, 'held_good_copies': 2,
            'held_cartons': 1, 'recalled_copies': 3, 'unbound_pieces': 2,
            'consignees': 1})
        self.assertEqual(report['summary']['affected_copies'],
                         report['summary']['in_factory_affected_copies'] +
                         report['summary']['dispatched_affected_copies'])

    def test_removing_dispatch_keeps_provenance_but_holds_c100_and_c103(self):
        with_dispatch = self.base_report([dispatch('C', 103, 103, 'D')])
        without = self.base_report([])
        entry = expanded_editions(without)['A']
        self.assertEqual(entry['affected_copies'],
                         expanded_editions(with_dispatch)['A']['affected_copies'])
        self.assertEqual(entry['incoming_evidence'],
                         expanded_editions(with_dispatch)['A']['incoming_evidence'])
        self.assertEqual(without['unbound_pieces'],
                         with_dispatch['unbound_pieces'])
        self.assertEqual(ranges(without['held_cartons'][0]['ranges'],
                                'first_carton', 'last_carton'),
                         [(100, 100), (103, 103)])
        # Copy 1 now shares held carton C103, so it is a held good copy too.
        self.assertEqual(ranges(entry['held_good_copies'], 'first', 'last'),
                         [(1, 1), (13, 14)])
        self.assertEqual(entry['in_factory_affected'],
                         [{'first': 2, 'last': 4}, {'first': 15, 'last': 16}])
        self.assertEqual(without['consignees'], [])
        self.assertEqual(without['summary']['dispatched_affected_copies'], 0)
        self.assertEqual(without['summary']['recalled_copies'], 0)


# ------------------------------------------------------------ ledger validation

class LedgerValidationTests(unittest.TestCase):
    def valid(self):
        batch, copies, lots, binds, packs, alerts = acceptance_inputs()
        return make(batch, copies, lots, binds, packs,
                    [dispatch('C', 103, 103, 'D')], alerts)

    def test_empty_alert_still_validates_the_whole_ledger(self):
        raw = self.valid()
        raw['alerts'] = []
        report = process_recall(raw)
        self.assertEqual(report['summary']['affected_copies'], 0)
        self.assertEqual(report['held_cartons'], [])
        self.assertEqual(report['consignees'], [])
        # Duplicated physical use is an error even with no alert at all.
        broken = copy.deepcopy(raw)
        broken['bind_spans'].append(bind('L', 8, 15, 1, -1))  # reuses L8
        with self.assertRaises(ValueError):
            process_recall(broken)
        # Missing supply is an error even with no alert at all.
        broken = copy.deepcopy(raw)
        broken['bind_spans'][-1]['count'] = 15  # copy 16 gets no M piece
        with self.assertRaises(ValueError):
            process_recall(broken)
        # A dispatch range crossing a missing carton is an error even with no alert.
        broken = copy.deepcopy(raw)
        broken['dispatches'] = [dispatch('C', 103, 104, 'D')]  # C104 does not exist
        with self.assertRaises(ValueError):
            process_recall(broken)

    def test_rejections(self):
        cases = []
        # unknown lot in a bind span
        raw = self.valid(); raw['bind_spans'][0]['lot_id'] = 'nope'; cases.append(raw)
        # same lot piece issued twice
        raw = self.valid(); raw['bind_spans'].append(
            bind('L', 8, 9, 1, -1)); cases.append(raw)
        # missing copy coverage on fresh signature
        raw = self.valid(); raw['bind_spans'] = raw['bind_spans'][:-1]; cases.append(raw)
        # lot names a signature that is not fresh (retained edition)
        batch = proof_batch(edition('A', pages=16))
        raw = make(batch, [{'edition_id': 'A', 'count': 16}],
                   [lot('L', 'A', 9, 8)],
                   [bind('L', 1, 1, 16)], [pack('A', 1, 1, 'C', 1, 4, 4)],
                   [], [])
        cases.append(raw)
        # bind span exceeds the lot piece count
        raw = self.valid(); raw['bind_spans'][0]['first_piece'] = 30; cases.append(raw)
        # dispatch to an undefined series
        raw = self.valid(); raw['dispatches'] = [dispatch('Z', 100, 100, 'D')]
        cases.append(raw)
        # carton series defined twice
        raw = self.valid(); raw['pack_spans'].append(pack('A', 1, 1, 'C', 100, 1, 1))
        cases.append(raw)
        # edition not packed exactly once
        raw = self.valid(); raw['pack_spans'] = [pack('A', 1, 1, 'C', 100, 2, 4)]
        cases.append(raw)
        # invalid step
        raw = self.valid(); raw['bind_spans'][0]['piece_step'] = 2; cases.append(raw)
        # alert beyond the lot
        raw = self.valid(); raw['alerts'] = [alert('L', 1, 99)]; cases.append(raw)
        # inverted alert
        raw = self.valid(); raw['alerts'] = [alert('L', 5, 4)]; cases.append(raw)
        # duplicate copies row
        raw = self.valid(); raw['copies'].append({'edition_id': 'A', 'count': 16})
        cases.append(raw)
        # duplicate lot id
        raw = self.valid(); raw['print_lots'].append(lot('L', 'A', 2, 16)); cases.append(raw)
        # unexpected top-level field
        raw = self.valid(); raw['extra'] = 1; cases.append(raw)
        for case in cases:
            with self.assertRaises(ValueError):
                process_recall(case)

    def test_two_lots_can_supply_the_same_signature(self):
        batch = proof_batch(edition('A', pages=8))
        raw = make(batch, [{'edition_id': 'A', 'count': 8}],
                   [lot('P', 'A', 1, 4), lot('Q', 'A', 1, 4)],
                   [bind('P', 1, 1, 4), bind('Q', 1, 5, 4)],
                   [pack('A', 1, 1, 'C', 1, 2, 4)], [], [])
        report = process_recall(raw)
        self.assertEqual(report['summary']['affected_copies'], 0)

    def test_retained_edition_has_exactly_one_copy(self):
        legacy = {'id': 'legacy', 'source': {
            'sections': [{'id': 'B', 'pages': 8}], 'signature_pages': 12,
            'binding': 'left', 'geometry': GEOMETRY},
            'revisions': ['r1'] * 8,
            'retained': [{'stock_id': 's1', 'signature': 1}]}
        batch = {'editions': [
            {'id': 'A', 'source': {'sections': [{'id': 'P', 'pages': 1},
                                                {'id': 'B', 'pages': 8},
                                                {'id': 'Q', 'pages': 3}],
                                   'signature_pages': 12, 'binding': 'left',
                                   'geometry': GEOMETRY},
             'revisions': ['r1'] * 12}],
            'legacy_lots': [legacy]}
        order = process_batch(batch)
        sources = [s['source'] for s in order['editions'][0]['signatures']]
        self.assertIn('retained', sources)
        fresh = [s['signature'] for s in order['editions'][0]['signatures']
                 if s['source'] == 'fresh_print']
        lots = [lot('L%d' % sig, 'A', sig, 1) for sig in fresh]
        binds = [bind('L%d' % sig, 1, 1, 1) for sig in fresh]
        raw = make(batch, [{'edition_id': 'A', 'count': 1}], lots, binds,
                   [pack('A', 1, 1, 'C', 1, 1, 1)], [], [])
        report = process_recall(raw)
        self.assertEqual(report['summary']['affected_copies'], 0)
        # More than one copy is rejected because a retained piece is bound in.
        raw['copies'] = [{'edition_id': 'A', 'count': 2}]
        with self.assertRaises(ValueError):
            process_recall(raw)


# ---------------------------------------------------------- structural scenarios

class StructuralScenarioTests(unittest.TestCase):
    def test_dispatch_spanning_adjacent_pack_rows_and_consignee_isolation(self):
        batch = proof_batch(edition('A', pages=8))
        raw = make(
            batch, [{'edition_id': 'A', 'count': 8}], [lot('L', 'A', 1, 8)],
            [bind('L', 8, 1, 8, -1)],
            [pack('A', 1, 1, 'C', 10, 2, 2),   # copies 1-4 -> C10, C11
             pack('A', 5, 1, 'C', 12, 1, 2),   # copies 5-6 -> C12
             pack('A', 7, 1, 'C', 14, 1, 2)],  # copies 7-8 -> C14 (C13 absent)
            [dispatch('C', 11, 12, 'D'), dispatch('C', 14, 14, 'E')],
            [alert('L', 3, 4)])  # pieces 3-4 -> copies 6, 5 -> carton C12
        report = process_recall(raw)
        # C12 is reached through the D range 11-12; E never receives it.
        self.assertEqual(report['consignees'],
                         [{'consignee_id': 'D',
                           'cartons': [{'carton_series': 'C',
                                        'ranges': [{'first_carton': 12,
                                                    'last_carton': 12}]}],
                           'editions': [{'edition_id': 'A',
                                         'recall_copies': [{'first': 5,
                                                            'last': 6}]}]}])
        self.assertEqual(report['held_cartons'], [])
        # The same affected copies with the D dispatch removed are in the factory.
        raw['dispatches'] = [dispatch('C', 14, 14, 'E')]
        report = process_recall(raw)
        self.assertEqual(report['held_cartons'],
                         [{'carton_series': 'C',
                           'ranges': [{'first_carton': 12, 'last_carton': 12}]}])
        self.assertEqual(expanded_editions(report)['A']['in_factory_affected'],
                         [{'first': 5, 'last': 6}])
        self.assertEqual(expanded_editions(report)['A']['held_good_copies'], [])
        self.assertEqual(report['consignees'], [])

    def test_same_copy_numbers_across_editions_stay_separate(self):
        batch = proof_batch(edition('A', pages=8), edition('B', pages=8))
        raw = make(
            batch, [{'edition_id': 'A', 'count': 8}, {'edition_id': 'B', 'count': 8}],
            [lot('LA', 'A', 1, 8), lot('LB', 'B', 1, 8)],
            [bind('LA', 8, 1, 8, -1), bind('LB', 1, 1, 8, 1)],
            [pack('A', 1, 1, 'C', 1, 1, 8), pack('B', 1, 1, 'D', 1, 1, 8)],
            [dispatch('C', 1, 1, 'X'), dispatch('D', 1, 1, 'X')],
            [alert('LA', 1, 8), alert('LB', 1, 8)])
        report = process_recall(raw)
        self.assertEqual(report['summary']['affected_copies'], 16)
        # One consignee holds a C carton and a D carton; per-edition recall rows
        # never merge across editions even though the copy numbers coincide.
        self.assertEqual(report['consignees'][0]['consignee_id'], 'X')
        self.assertEqual(report['consignees'][0]['editions'],
                         [{'edition_id': 'A',
                           'recall_copies': [{'first': 1, 'last': 8}]},
                          {'edition_id': 'B',
                           'recall_copies': [{'first': 1, 'last': 8}]}])

    def test_direction_flip_reports_ascending_piece_intervals(self):
        batch = proof_batch(edition('A', pages=8))
        raw = make(
            batch, [{'edition_id': 'A', 'count': 8}], [lot('L', 'A', 1, 8)],
            [bind('L', 4, 1, 4, -1), bind('L', 8, 5, 4, -1)],
            [pack('A', 1, 1, 'C', 1, 2, 4)], [],
            [alert('L', 2, 3), alert('L', 5, 6)])
        report = process_recall(raw)
        self.assertEqual(
            [(e['copy_first'], e['copy_last'], e['piece_first'], e['piece_last'],
              e['piece_step']) for e in
             expanded_editions(report)['A']['incoming_evidence']],
            [(2, 3, 2, 3, -1), (7, 8, 5, 6, -1)])

    def test_unused_lot_with_alert_reports_only_unbound_pieces(self):
        batch = proof_batch(edition('A', pages=8))
        raw = make(
            batch, [{'edition_id': 'A', 'count': 8}],
            [lot('L', 'A', 1, 8), lot('U', 'A', 1, 5)],
            [bind('L', 1, 1, 8)], [pack('A', 1, 1, 'C', 1, 2, 4)], [],
            [alert('U', 2, 4)])
        report = process_recall(raw)
        self.assertEqual(report['summary']['affected_copies'], 0)
        self.assertEqual(report['unbound_pieces'],
                         [{'lot_id': 'U', 'edition_id': 'A', 'signature': 1,
                           'pieces': [{'first_piece': 2, 'last_piece': 4}]}])
        self.assertEqual(report['summary']['unbound_pieces'], 3)

    def test_two_lots_may_each_use_physical_code_one(self):
        batch = proof_batch(edition('A', pages=8))
        raw = make(
            batch, [{'edition_id': 'A', 'count': 8}],
            [lot('P', 'A', 1, 4), lot('Q', 'A', 1, 4)],
            [bind('P', 1, 1, 4), bind('Q', 1, 5, 4)],
            [pack('A', 1, 1, 'C', 1, 2, 4)], [],
            [alert('P', 1, 1), alert('Q', 1, 1)])
        report = process_recall(raw)
        # Separate lot numbering namespaces: both alerts hit, on different copies.
        self.assertEqual(expanded_editions(report)['A']['affected_copies'],
                         [{'first': 1, 'last': 1}, {'first': 5, 'last': 5}])

    def test_overlapping_alert_on_bound_and_unbound_pieces(self):
        batch = proof_batch(edition('A', pages=8))
        raw = make(
            batch, [{'edition_id': 'A', 'count': 8}],
            [lot('L', 'A', 1, 12)],
            [bind('L', 8, 1, 8, -1)],
            [pack('A', 1, 1, 'C', 1, 2, 4)], [],
            [alert('L', 8, 12), alert('L', 5, 10), alert('L', 9, 9)])
        report = process_recall(raw)
        # Copies 1..4 come from pieces 8,7,6,5; pieces 9-12 were never bound.
        self.assertEqual(expanded_editions(report)['A']['affected_copies'],
                         [{'first': 1, 'last': 4}])
        self.assertEqual(report['unbound_pieces'],
                         [{'lot_id': 'L', 'edition_id': 'A', 'signature': 1,
                           'pieces': [{'first_piece': 9, 'last_piece': 12}]}])
        self.assertEqual(report['summary']['affected_copies'], 4)
        self.assertEqual(report['summary']['unbound_pieces'], 4)


    def test_unaffected_edition_is_reported_with_empty_sets(self):
        batch = proof_batch(edition('A', pages=8), edition('B', pages=8))
        raw = make(
            batch, [{'edition_id': 'A', 'count': 8}, {'edition_id': 'B', 'count': 8}],
            [lot('LA', 'A', 1, 8), lot('LB', 'B', 1, 8)],
            [bind('LA', 8, 1, 8, -1), bind('LB', 1, 1, 8, 1)],
            [pack('A', 1, 1, 'C', 1, 1, 8), pack('B', 1, 1, 'D', 1, 1, 8)],
            [], [alert('LA', 1, 1)])
        report = process_recall(raw)
        by_id = expanded_editions(report)
        self.assertEqual(by_id['B'], {
            'edition_id': 'B', 'copy_count': 8, 'affected_copies': [],
            'in_factory_affected': [], 'dispatched_affected': [],
            'held_good_copies': [], 'incoming_evidence': []})
        # B's carton D holds no affected book, so it is not frozen even though
        # A's held carton freezes good copies of A.
        self.assertEqual(by_id['A']['held_good_copies'],
                         [{'first': 1, 'last': 7}])
        self.assertEqual(report['held_cartons'],
                         [{'carton_series': 'C',
                           'ranges': [{'first_carton': 1, 'last_carton': 1}]}])
        self.assertEqual(report['consignees'], [])


# ----------------------------------------------------------------- determinism

class DeterminismTests(unittest.TestCase):
    def test_row_order_and_equivalent_alerts_do_not_change_the_report(self):
        batch, copies, lots, binds, packs, alerts = acceptance_inputs()
        base = process_recall(make(batch, copies, lots, binds, packs,
                                   [dispatch('C', 103, 103, 'D')], alerts))
        # Reverse every ledger array; the proof batch order is unchanged.
        shuffled = process_recall(make(
            batch, list(reversed(copies)), list(reversed(lots)),
            list(reversed(binds)), list(reversed(packs)),
            [dispatch('C', 103, 103, 'D')], list(reversed(alerts))))
        self.assertEqual(base, shuffled)
        # Equivalent overlapping alert intervals merge to the same sets.
        overlapping = [alert('L', 6, 7), alert('L', 7, 7), alert('L', 15, 16),
                       alert('L', 16, 18), alert('L', 6, 18), alert('M', 3, 4),
                       alert('M', 4, 4)]
        # L6-18 also touches pieces 8-14, which are bound but not "unbound";
        # this is a genuinely different alert set, so compare only the merged
        # representation of the original alerts plus their sub-intervals.
        equivalent = [alert('L', 6, 7), alert('L', 15, 18), alert('M', 3, 4),
                      alert('L', 6, 6), alert('M', 4, 4), alert('L', 17, 18)]
        self.assertEqual(
            process_recall(make(batch, copies, lots, binds, packs,
                                [dispatch('C', 103, 103, 'D')], equivalent)),
            base)


# ------------------------------------------------------------------ large case

class LargeCaseTests(unittest.TestCase):
    """The 10^9-copy acceptance case, asserted entirely by intervals."""

    STACK = 10 ** 9

    def raw(self):
        batch = proof_batch(edition('A', pages=4, cap=4))
        return make(
            batch,
            [{'edition_id': 'A', 'count': self.STACK}],
            [lot('L', 'A', 1, self.STACK)],
            [bind('L', self.STACK, 1, self.STACK, -1)],
            [pack('A', 1, 1, 'C', 1, self.STACK // 10, 10)],
            [dispatch('C', 75_000_001, 100_000_000, 'D')],
            [alert('L', 1, 500_000_001)])

    def test_large_case_intervals(self):
        report = process_recall(self.raw())
        entry = report['editions'][0]
        self.assertEqual(entry['affected_copies'],
                         [{'first': 500_000_000, 'last': 1_000_000_000}])
        self.assertEqual(report['held_cartons'],
                         [{'carton_series': 'C',
                           'ranges': [{'first_carton': 50_000_000,
                                       'last_carton': 75_000_000}]}])
        self.assertEqual(entry['held_good_copies'],
                         [{'first': 499_999_991, 'last': 499_999_999}])
        self.assertEqual(report['consignees'],
                         [{'consignee_id': 'D',
                           'cartons': [{'carton_series': 'C',
                                        'ranges': [{'first_carton': 75_000_001,
                                                    'last_carton': 100_000_000}]}],
                           'editions': [{'edition_id': 'A',
                                         'recall_copies': [
                                             {'first': 750_000_001,
                                              'last': 1_000_000_000}]}]}])
        self.assertEqual(entry['in_factory_affected'],
                         [{'first': 500_000_000, 'last': 750_000_000}])
        summary = report['summary']
        self.assertEqual(summary['affected_copies'], 500_000_001)
        self.assertEqual(summary['in_factory_affected_copies'], 250_000_001)
        self.assertEqual(summary['dispatched_affected_copies'], 250_000_000)
        self.assertEqual(summary['held_good_copies'], 9)
        self.assertEqual(summary['held_cartons'], 25_000_001)
        self.assertEqual(summary['recalled_copies'], 250_000_000)
        self.assertEqual(summary['unbound_pieces'], 0)
        # No per-copy or per-carton expansion: the report is a few kilobytes.
        self.assertLess(len(json.dumps(report)), 5000)

    def test_large_case_cli_under_memory_limit(self):
        tmp = Path(__file__).resolve().parent / '_tmp_large'
        tmp.mkdir(exist_ok=True)
        try:
            source = tmp / 'large.json'
            output = tmp / 'large-out.json'
            source.write_text(json.dumps(self.raw()))
            args = [sys.executable, '-m', 'signature_press.cli', '--lot-recall',
                    '--source', str(source), '--output', str(output)]
            # Constrain a fresh shell's address space, not this test process.
            script = 'ulimit -v 262144; exec "$@"'
            result = subprocess.run(['sh', '-c', script, 'sh'] + args,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report['summary']['affected_copies'], 500_000_001)
            self.assertEqual(report['held_cartons'][0]['ranges'][0]['first_carton'],
                             50_000_000)
        finally:
            for child in tmp.iterdir():
                child.unlink()
            tmp.rmdir()


# ------------------------------------------------------------------ randomized

class RandomizedCrossCheckTests(unittest.TestCase):
    """Small randomized ledgers compared with a per-copy/page-1 expansion."""

    def reference(self, raw):
        alerts = {}
        for row in raw['alerts']:
            alerts.setdefault(row['lot_id'], set()).update(
                range(row['first_piece'], row['last_piece'] + 1))
        lots = {row['lot_id']: row for row in raw['print_lots']}
        affected = {}
        for span in raw['bind_spans']:
            info = lots[span['lot_id']]
            for index in range(span['count']):
                piece = span['first_piece'] + index * span['piece_step']
                copy_number = span['first_copy'] + index
                if piece in alerts.get(span['lot_id'], set()):
                    affected.setdefault(info['edition_id'], set()).add(copy_number)
        copy_carton = {}
        carton_copies = {}
        for pack_row in raw['pack_spans']:
            index = 0
            for carton in range(pack_row['carton_count']):
                number = pack_row['first_carton'] + carton
                for _ in range(pack_row['books_per_carton']):
                    copy_number = pack_row['first_copy'] + index * pack_row['copy_step']
                    index += 1
                    copy_carton[(pack_row['edition_id'], copy_number)] = (
                        pack_row['carton_series'], number)
                    carton_copies.setdefault(
                        (pack_row['carton_series'], number), set()).add(
                        (pack_row['edition_id'], copy_number))
        dispatched_cartons = set()
        for row in raw['dispatches']:
            for number in range(row['first_carton'], row['last_carton'] + 1):
                dispatched_cartons.add((row['carton_series'], number))
        affected_cartons = {copy_carton[(eid, copy)]
                            for eid, copies in affected.items() for copy in copies}
        held = affected_cartons - dispatched_cartons
        held_good = set()
        for key in held:
            for eid, copy in carton_copies[key]:
                if copy not in affected.get(eid, set()):
                    held_good.add((eid, copy))
        in_factory = {(eid, copy) for eid, copies in affected.items()
                      for copy in copies if copy_carton[(eid, copy)] in held}
        dispatched = set()
        for row in raw['dispatches']:
            for number in range(row['first_carton'], row['last_carton'] + 1):
                for eid, copy in carton_copies.get((row['carton_series'], number), ()):
                    if copy in affected.get(eid, set()):
                        dispatched.add((row['consignee_id'], eid, copy))
        return affected, held, held_good, in_factory, dispatched

    def expand_editions(self, report, field):
        result = set()
        for entry in report['editions']:
            for row in entry[field]:
                for copy in range(row['first'], row['last'] + 1):
                    result.add((entry['edition_id'], copy))
        return result

    def test_random_ledgers(self):
        rng = random.Random(20261122)
        batch = proof_batch(edition('A', pages=16))
        signatures = [s['signature'] for s in
                      process_batch(batch)['editions'][0]['signatures']]
        self.assertEqual(signatures, [1, 2])
        checked = 0
        for trial in range(400):
            size = rng.randint(2, 12)
            binds = []
            ok = True
            for signature in signatures:
                lot_id = 'lot%d' % signature
                used = set()
                copy = 1
                while copy <= size:
                    count = rng.randint(1, size - copy + 1)
                    step = rng.choice((1, -1))
                    for _ in range(400):
                        first = rng.randint(1, 60)
                        pieces = {first + i * step for i in range(count)}
                        if all(1 <= value <= 60 for value in pieces) and \
                                not pieces & used:
                            break
                    else:
                        ok = False
                        break
                    used |= pieces
                    binds.append(bind(lot_id, first, copy, count, step))
                    copy += count
                if not ok:
                    break
            if not ok:
                continue
            # Disjoint copy intervals in random directions form the packing.
            packs = []
            taken = set()
            number = 1
            while len(taken) < size:
                start = rng.randint(1, size)
                step = rng.choice((1, -1))
                length = rng.randint(1, size)
                block = {start + i * step for i in range(length)}
                if any(value < 1 or value > size for value in block) or block & taken:
                    if len(taken) == size:
                        break
                    # Fall back to the first unpacked copy as a singleton.
                    block = {next(value for value in range(1, size + 1)
                                  if value not in taken)}
                    step = 1
                    start = next(iter(block))
                    length = 1
                remaining = length
                position = start
                while remaining > 0:
                    per_carton = rng.randint(1, min(4, remaining))
                    packs.append(pack('A', position, step, 'C', number, 1,
                                      per_carton))
                    position += per_carton * step
                    remaining -= per_carton
                    number += 1
                taken |= block
            total_cartons = number - 1
            dispatches = []
            if total_cartons >= 1 and rng.random() < 0.8:
                first = rng.randint(1, total_cartons)
                last = rng.randint(first, total_cartons)
                dispatches = [dispatch('C', first, last,
                                       rng.choice(('D', 'E')))]
            alerts = []
            for signature in signatures:
                if rng.random() < 0.85:
                    first = rng.randint(1, 60)
                    last = rng.randint(first, 60)
                    alerts.append(alert('lot%d' % signature, first, last))
            raw = make(batch, [{'edition_id': 'A', 'count': size}],
                       [lot('lot%d' % s, 'A', s, 60) for s in signatures],
                       binds, packs, dispatches, alerts)
            report = process_recall(raw)
            checked += 1
            affected, held, held_good, in_factory, dispatched = self.reference(raw)
            self.assertEqual(
                self.expand_editions(report, 'affected_copies'),
                {(eid, copy) for eid, copies in affected.items() for copy in copies})
            held_cartons = set()
            for row in report['held_cartons']:
                for rg in row['ranges']:
                    for value in range(rg['first_carton'], rg['last_carton'] + 1):
                        held_cartons.add((row['carton_series'], value))
            self.assertEqual(held_cartons, held)
            self.assertEqual(self.expand_editions(report, 'held_good_copies'),
                             held_good)
            self.assertEqual(self.expand_editions(report, 'in_factory_affected'),
                             in_factory)
            recalled = set()
            for consignee in report['consignees']:
                for entry in consignee['editions']:
                    for rg in entry['recall_copies']:
                        for value in range(rg['first'], rg['last'] + 1):
                            recalled.add((consignee['consignee_id'],
                                          entry['edition_id'], value))
            self.assertEqual(recalled, dispatched)
        self.assertGreater(checked, 250)


# ------------------------------------------------------------------- CLI tests

class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__file__).resolve().parent / '_tmp_recall_cli'
        self.tmp.mkdir(exist_ok=True)

    def tearDown(self):
        for child in self.tmp.iterdir():
            child.unlink()
        self.tmp.rmdir()

    def _run(self, source, output):
        return subprocess.run(
            [sys.executable, '-m', 'signature_press.cli', '--lot-recall',
             '--source', str(source), '--output', str(output)],
            capture_output=True, text=True)

    def test_fixture_runs_and_refuses_to_overwrite(self):
        fixture = Path(__file__).resolve().parents[1] / 'fixtures' / 'lot-recall.json'
        output = self.tmp / 'recall.json'
        first = self._run(fixture, output)
        self.assertEqual(first.returncode, 0, first.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report['schema_version'], 6)
        self.assertEqual(report['summary']['affected_copies'], 5)
        before = output.read_bytes()
        siblings = sorted(path.name for path in self.tmp.iterdir())
        again = self._run(fixture, output)
        self.assertEqual(again.returncode, 2)
        self.assertEqual(output.read_bytes(), before)
        self.assertEqual(sorted(path.name for path in self.tmp.iterdir()), siblings)

    def test_invalid_input_exits_two_and_publishes_nothing(self):
        batch, copies, lots, binds, packs, alerts = acceptance_inputs()
        raw = make(batch, copies, lots, binds, packs, [], alerts)
        raw['bind_spans'][0]['lot_id'] = 'missing'
        source = self.tmp / 'bad.json'
        output = self.tmp / 'bad-out.json'
        source.write_text(json.dumps(raw))
        result = self._run(source, output)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(output.exists())
        self.assertFalse(any(path.name.startswith('.report-')
                             for path in self.tmp.iterdir()))

    def test_modes_are_mutually_exclusive(self):
        source = self.tmp / 'any.json'
        output = self.tmp / 'any-out.json'
        source.write_text('{}')
        result = subprocess.run(
            [sys.executable, '-m', 'signature_press.cli', '--lot-recall',
             '--proof-batch', '--source', str(source), '--output', str(output)],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
