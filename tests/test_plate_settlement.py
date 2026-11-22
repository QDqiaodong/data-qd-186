import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path

from signature_press.batch import process_batch
from signature_press.settlement import _apportion, process_settlement
from fractions import Fraction

GEOMETRY = {'sheet_width_um': 440000, 'sheet_height_um': 320000, 'leaf_width_um': 200000,
            'leaf_height_um': 280000, 'margin_um': 10000, 'gutter_um': 10000, 'thickness_um': 120}


def edition(eid, sections=4, revisions=None, geometry=None, cap=8, binding='left'):
    revisions = ['r1'] * sections if revisions is None else revisions
    return {'id': eid,
            'source': {'sections': [{'id': 'S', 'pages': sections}],
                       'signature_pages': cap, 'binding': binding,
                       'geometry': geometry or GEOMETRY},
            'revisions': list(revisions)}


def batch(*eids):
    return {'editions': [edition(eid) for eid in eids], 'legacy_lots': []}


def ref(order_id, eid, signature=1, sheet=1, side='front'):
    return {'order_id': order_id, 'edition_id': eid, 'signature': signature,
            'sheet': sheet, 'side': side}


def revise(event_id, *pools):
    return {'event_id': event_id, 'type': 'revise', 'pools': list(pools)}


def pool(pool_id, total_fen, uses):
    return {'pool_id': pool_id, 'total_fen': total_fen, 'uses': list(uses)}


def close(event_id, statement_id, order_id):
    return {'event_id': event_id, 'type': 'close',
            'statement_id': statement_id, 'order_id': order_id}


def settle(orders, events):
    return process_settlement({'orders': orders, 'events': events})


def orders_ab():
    return [{'order_id': 'A', 'proof_batch': batch('ea-a1', 'ea-a2')},
            {'order_id': 'B', 'proof_batch': batch('eb-b1')}]


def pool_by_id(report, pool_id):
    return next(p for p in report['pools'] if p['pool_id'] == pool_id)


def statement_by_id(report, statement_id):
    return next(s for s in report['statements'] if s['statement_id'] == statement_id)


def line(statement, pool_id):
    return next(l for l in statement['lines'] if l['pool_id'] == pool_id)


def outstanding(report, order_id):
    entry = next(o for o in report['outstanding'] if o['order_id'] == order_id)
    return {p['pool_id']: p for p in entry['pools']}


class AcceptanceScenarioTests(unittest.TestCase):
    """The end-to-end scenario from the contract, including truncated replay."""

    def events(self):
        return [
            revise('ev-1', pool('X', 101, [ref('A', 'ea-a1'), ref('A', 'ea-a2'),
                                           ref('B', 'eb-b1')])),
            close('ev-2', 'st-a1', 'A'),
            revise('ev-3',
                   pool('X', 101, [ref('A', 'ea-a1'), ref('B', 'eb-b1')]),
                   pool('Y', 16, [ref('A', 'ea-a2')])),
            close('ev-4', 'st-a2', 'A'),
            close('ev-5', 'st-b1', 'B'),
        ]

    def test_first_statement_charges_a_67_of_101(self):
        report = settle(orders_ab(), self.events()[:2])
        self.assertEqual(report['schema_version'], 4)
        statement = report['statements'][0]
        self.assertEqual(statement['statement_id'], 'st-a1')
        self.assertEqual(statement['event_id'], 'ev-2')
        self.assertEqual(len(statement['lines']), 1)
        self.assertEqual(line(statement, 'X'),
                         {'pool_id': 'X', 'pool_version': 1,
                          'previously_settled_fen': 0, 'payable_fen': 67,
                          'difference_fen': 67})
        self.assertEqual(statement['signed_total_fen'], 67)
        # 101 over faces 2:1 leaves B with 34.
        x = pool_by_id(report, 'X')
        self.assertEqual([(p['order_id'], p['face_count'], p['payable_fen'])
                          for p in x['participants']],
                         [('A', 2, 67), ('B', 1, 34)])

    def test_correction_statement_lists_refund_and_charge_together(self):
        report = settle(orders_ab(), self.events())
        second = statement_by_id(report, 'st-a2')
        # Not an empty statement: X refund 16 and Y charge 16 on the same sheet.
        self.assertEqual([l['pool_id'] for l in second['lines']], ['X', 'Y'])
        self.assertEqual(line(second, 'X')['difference_fen'], -16)
        self.assertEqual(line(second, 'X')['payable_fen'], 51)
        self.assertEqual(line(second, 'X')['pool_version'], 2)
        self.assertEqual(line(second, 'Y')['difference_fen'], 16)
        self.assertEqual(line(second, 'Y')['payable_fen'], 16)
        self.assertEqual(second['signed_total_fen'], 0)
        # X reapportions to A 51 / B 50.
        x = pool_by_id(report, 'X')
        self.assertEqual([(p['order_id'], p['payable_fen'])
                          for p in x['participants']], [('A', 51), ('B', 50)])
        self.assertEqual(x['version'], 2)
        self.assertEqual(pool_by_id(report, 'Y')['version'], 1)

    def test_unclosed_b_carries_full_50_not_only_the_delta(self):
        report = settle(orders_ab(), self.events()[:4])
        b_open = outstanding(report, 'B')
        self.assertEqual(b_open['X']['payable_fen'], 50)
        self.assertEqual(b_open['X']['settled_fen'], 0)
        self.assertEqual(b_open['X']['open_difference_fen'], 50)
        # Closing B then collects the full 50.
        report2 = settle(orders_ab(), self.events())
        b_statement = statement_by_id(report2, 'st-b1')
        self.assertEqual(line(b_statement, 'X')['difference_fen'], 50)
        self.assertEqual(b_statement['signed_total_fen'], 50)

    def test_truncated_replay_reproduces_first_statement_exactly(self):
        full = settle(orders_ab(), self.events())
        truncated = settle(orders_ab(), self.events()[:2])
        self.assertEqual(truncated['statements'][0], full['statements'][0])

    def test_appending_events_never_rewrites_published_statements(self):
        before = settle(orders_ab(), self.events())
        extended_events = self.events() + [
            revise('ev-6', pool('X', 101, [ref('B', 'eb-b1')])),
            close('ev-7', 'st-a3', 'A'),
        ]
        after = settle(orders_ab(), extended_events)
        self.assertEqual(after['statements'][:3], before['statements'])
        # The new statement reflects the exit: A's X payable dropped to zero.
        third = statement_by_id(after, 'st-a3')
        self.assertEqual(line(third, 'X')['difference_fen'], -51)
        self.assertEqual(line(third, 'X')['previously_settled_fen'], 51)
        self.assertEqual(third['signed_total_fen'], -51)
        # And the exited pool still shows the pending refund before that close.
        pre_exit = settle(orders_ab(), extended_events[:-1])
        a_open = outstanding(pre_exit, 'A')
        self.assertEqual(a_open['X']['payable_fen'], 0)
        self.assertEqual(a_open['X']['settled_fen'], 51)
        self.assertEqual(a_open['X']['open_difference_fen'], -51)


class ApportionmentTests(unittest.TestCase):
    def test_largest_remainder_with_ascii_tie_break(self):
        # 10 over three equal faces: remainders 1/3 each, ASCII order wins.
        split = _apportion(10, {'A': 1, 'B': 1, 'C': 1})
        self.assertEqual(split, {'A': 4, 'B': 3, 'C': 3})
        self.assertEqual(sum(split.values()), 10)

    def test_exact_shares_need_no_leftover(self):
        split = _apportion(100, {'A': 1, 'B': 3})
        self.assertEqual(split, {'A': 25, 'B': 75})

    def test_zero_total_and_face_weight_split(self):
        self.assertEqual(_apportion(0, {'A': 2, 'B': 1}), {'A': 0, 'B': 0})
        split = _apportion(7, {'A': 2, 'B': 1})
        self.assertEqual(split, {'A': 5, 'B': 2})  # remainders 2/3 vs 1/3

    def test_no_floating_point_used(self):
        # A case where binary float rounding would perturb the remainder order.
        total = 10 ** 12
        split = _apportion(total, {'A': 3, 'B': 7})
        self.assertEqual(sum(split.values()), total)
        self.assertEqual(split['A'], Fraction(total * 3, 10).numerator //
                         Fraction(total * 3, 10).denominator)

    def test_tie_break_is_ascii_byte_order_not_input_order(self):
        split = _apportion(5, {'z': 1, 'A': 1, 'm': 1})
        self.assertEqual(split, {'A': 2, 'm': 2, 'z': 1})
        self.assertEqual(sorted(split, key=lambda oid: oid), ['A', 'm', 'z'])


class ReplayAndRefundTests(unittest.TestCase):
    def test_revoked_pool_refunds_the_full_settled_amount(self):
        events = [
            revise('ev-1', pool('X', 60, [ref('A', 'ea-a1'), ref('B', 'eb-b1')])),
            close('ev-2', 'st-a1', 'A'),
            revise('ev-3', pool('X', 0, [])),
            close('ev-4', 'st-a2', 'A'),
        ]
        report = settle(orders_ab(), events)
        x = pool_by_id(report, 'X')
        self.assertTrue(x['revoked'])
        self.assertEqual(x['version'], 2)
        self.assertEqual(x['participants'], [])
        second = statement_by_id(report, 'st-a2')
        self.assertEqual(line(second, 'X'),
                         {'pool_id': 'X', 'pool_version': 2,
                          'previously_settled_fen': 30, 'payable_fen': 0,
                          'difference_fen': -30})

    def test_reopened_pool_keeps_identity_and_bumps_version(self):
        events = [
            revise('ev-1', pool('X', 60, [ref('A', 'ea-a1')])),
            revise('ev-2', pool('X', 0, [])),
            revise('ev-3', pool('X', 20, [ref('B', 'eb-b1')])),
        ]
        report = settle(orders_ab(), events)
        x = pool_by_id(report, 'X')
        self.assertFalse(x['revoked'])
        self.assertEqual(x['version'], 3)
        self.assertEqual(x['total_fen'], 20)
        self.assertEqual([p['order_id'] for p in x['participants']], ['B'])

    def test_repeated_close_with_no_change_emits_empty_statement(self):
        events = [
            revise('ev-1', pool('X', 60, [ref('A', 'ea-a1')])),
            close('ev-2', 'st-a1', 'A'),
            close('ev-3', 'st-a2', 'A'),
        ]
        report = settle(orders_ab(), events)
        empty = statement_by_id(report, 'st-a2')
        self.assertEqual(empty['lines'], [])
        self.assertEqual(empty['signed_total_fen'], 0)
        self.assertEqual(len(report['statements']), 2)

    def test_zero_fee_pool_sets_zero_payable_and_no_statement_lines(self):
        events = [
            revise('ev-1', pool('X', 0, [ref('A', 'ea-a1'), ref('B', 'eb-b1')])),
            close('ev-2', 'st-a1', 'A'),
        ]
        report = settle(orders_ab(), events)
        self.assertEqual(pool_by_id(report, 'X')['total_fen'], 0)
        self.assertEqual(statement_by_id(report, 'st-a1')['lines'], [])
        self.assertEqual(outstanding(report, 'A')['X']['open_difference_fen'], 0)

    def test_transfer_and_swap_inside_one_revise_are_order_independent(self):
        base_orders = orders_ab()
        transfer = [
            revise('ev-1', pool('X', 101, [ref('A', 'ea-a1'), ref('A', 'ea-a2'),
                                           ref('B', 'eb-b1')])),
            revise('ev-2',
                   pool('Y', 16, [ref('A', 'ea-a2')]),
                   pool('X', 101, [ref('A', 'ea-a1'), ref('B', 'eb-b1')])),
        ]
        one_order = settle(base_orders, transfer)
        swapped_order = settle(base_orders, [
            transfer[0],
            revise('ev-2',
                   pool('X', 101, [ref('B', 'eb-b1'), ref('A', 'ea-a1')]),
                   pool('Y', 16, [ref('A', 'ea-a2')])),
        ])
        self.assertEqual(json.dumps(one_order, sort_keys=True),
                         json.dumps(swapped_order, sort_keys=True))
        # A direct two-pool swap in one event is also accepted against final state.
        swap = settle(base_orders, [
            revise('ev-1', pool('P', 10, [ref('A', 'ea-a1')]),
                   pool('Q', 20, [ref('A', 'ea-a2')])),
            revise('ev-2', pool('P', 10, [ref('A', 'ea-a2')]),
                   pool('Q', 20, [ref('A', 'ea-a1')])),
        ])
        self.assertEqual(pool_by_id(swap, 'P')['uses'][0]['edition_id'], 'ea-a2')

    def test_duplicate_claim_after_replacement_rejects_even_if_later_fixed(self):
        events = [
            revise('ev-1', pool('X', 10, [ref('A', 'ea-a1')])),
            revise('ev-2', pool('X', 10, [ref('A', 'ea-a1')]),
                   pool('Y', 10, [ref('A', 'ea-a1')])),  # double claim in final state
            revise('ev-3', pool('Y', 10, [ref('A', 'ea-a2')])),  # would fix it later
        ]
        with self.assertRaises(ValueError):
            settle(orders_ab(), events)

    def test_duplicate_face_inside_one_pool_record_is_rejected(self):
        events = [revise('ev-1', pool('X', 10, [ref('A', 'ea-a1'), ref('A', 'ea-a1')]))]
        with self.assertRaises(ValueError):
            settle(orders_ab(), events)

    def test_unmentioned_pools_survive_a_revise_event(self):
        events = [
            revise('ev-1', pool('X', 10, [ref('A', 'ea-a1')]),
                   pool('Y', 20, [ref('A', 'ea-a2')])),
            revise('ev-2', pool('Y', 30, [ref('A', 'ea-a2')])),
        ]
        report = settle(orders_ab(), events)
        self.assertEqual(pool_by_id(report, 'X')['total_fen'], 10)
        self.assertEqual(pool_by_id(report, 'X')['version'], 1)
        self.assertEqual(pool_by_id(report, 'Y')['total_fen'], 30)
        self.assertEqual(pool_by_id(report, 'Y')['version'], 2)


class SideValidityTests(unittest.TestCase):
    def test_different_revision_sides_cannot_share_a_pool(self):
        # A second edition identical except the revision of page 1, which sits
        # on the front side (folios 4,1): its front therefore differs.
        other_edition = edition('ec-c1', revisions=['r2', 'r1', 'r1', 'r1'])
        orders = [
            {'order_id': 'A', 'proof_batch': batch('ea-a1')},
            {'order_id': 'C',
             'proof_batch': {'editions': [other_edition], 'legacy_lots': []}},
        ]
        events = [revise('ev-1', pool('X', 10, [ref('A', 'ea-a1'),
                                                ref('C', 'ec-c1')]))]
        with self.assertRaises(ValueError):
            settle(orders, events)
        # But each side is billable on its own: separate pools are fine.
        ok = settle(orders, [
            revise('ev-1', pool('X', 10, [ref('A', 'ea-a1')]),
                   pool('Y', 8, [ref('C', 'ec-c1')])),
        ])
        self.assertEqual([p['pool_id'] for p in ok['pools']], ['X', 'Y'])

    def test_different_paper_size_blocks_a_shared_pool(self):
        big = dict(GEOMETRY, sheet_width_um=500000)
        orders = [
            {'order_id': 'A', 'proof_batch': batch('ea-a1')},
            {'order_id': 'C', 'proof_batch': {
                'editions': [edition('ec-c1', geometry=big)], 'legacy_lots': []}},
        ]
        events = [revise('ev-1', pool('X', 10, [ref('A', 'ea-a1'),
                                                ref('C', 'ec-c1')]))]
        with self.assertRaises(ValueError):
            settle(orders, events)

    def test_placement_frame_difference_blocks_a_shared_pool(self):
        wide_gutter = dict(GEOMETRY, gutter_um=12000)
        orders = [
            {'order_id': 'A', 'proof_batch': batch('ea-a1')},
            {'order_id': 'C', 'proof_batch': {
                'editions': [edition('ec-c1', geometry=wide_gutter)],
                'legacy_lots': []}},
        ]
        events = [revise('ev-1', pool('X', 10, [ref('A', 'ea-a1'),
                                                ref('C', 'ec-c1')]))]
        with self.assertRaises(ValueError):
            settle(orders, events)

    def test_identical_sides_from_different_orders_share_one_plate(self):
        # Same paper and identical slot-wise pages/frames under independent
        # order and edition identities: the supplier may pool them, and the
        # fee splits across the two orders. Edition names and work-order
        # provenance do not participate in the same-plate comparison.
        orders = [
            {'order_id': 'A', 'proof_batch': batch('ea-a1')},
            {'order_id': 'C', 'proof_batch': batch('ec-c1')},
        ]
        report = settle(orders, [
            revise('ev-1', pool('X', 10, [ref('A', 'ea-a1'), ref('C', 'ec-c1')])),
        ])
        self.assertEqual([(p['order_id'], p['face_count'], p['payable_fen'])
                          for p in pool_by_id(report, 'X')['participants']],
                         [('A', 1, 5), ('C', 1, 5)])

    def test_identical_sides_without_a_shared_pool_are_never_merged(self):
        # The planner must not invent a shared charge merely because the two
        # sides look identical: separate pool records stay separate.
        orders = [
            {'order_id': 'A', 'proof_batch': batch('ea-a1')},
            {'order_id': 'C', 'proof_batch': batch('ec-c1')},
        ]
        report = settle(orders, [
            revise('ev-1', pool('X', 7, [ref('A', 'ea-a1')]),
                   pool('Y', 3, [ref('C', 'ec-c1')])),
        ])
        self.assertEqual([p['pool_id'] for p in report['pools']], ['X', 'Y'])
        self.assertEqual(pool_by_id(report, 'X')['total_fen'], 7)
        self.assertEqual(pool_by_id(report, 'Y')['total_fen'], 3)

    def test_retained_signature_references_are_input_errors(self):
        # An order whose v2 plan consumes a retained legacy signature.
        legacy = {
            'editions': [edition('ed-d1')],
            'legacy_lots': [{
                'id': 'lot-1',
                'source': {'sections': [{'id': 'S', 'pages': 4}],
                           'signature_pages': 8, 'binding': 'left',
                           'geometry': GEOMETRY},
                'revisions': ['r1'] * 4,
                'retained': [{'stock_id': 'stock-1', 'signature': 1}],
            }],
        }
        orders = [{'order_id': 'D', 'proof_batch': legacy}]
        planned = process_batch(legacy)
        self.assertEqual(planned['editions'][0]['signatures'][0]['source'], 'retained')
        events = [revise('ev-1', pool('X', 10, [ref('D', 'ed-d1')]))]
        with self.assertRaises(ValueError):
            settle(orders, events)

    def test_all_blank_side_references_are_input_errors(self):
        # One page padded to four folios: the back side holds pages 2,3 -> both
        # blank, while the front still carries one real page and stays billable.
        orders = [{'order_id': 'E',
                   'proof_batch': {'editions': [edition('ee-e1', sections=1)],
                                   'legacy_lots': []}}]
        with self.assertRaises(ValueError):
            settle(orders, [revise('ev-1', pool('X', 10,
                                                [ref('E', 'ee-e1', side='back')]))])
        ok = settle(orders, [revise('ev-1', pool('X', 10,
                                                 [ref('E', 'ee-e1', side='front')]))])
        self.assertEqual(ok['pools'][0]['uses'][0]['side'], 'front')

    def test_unknown_and_malformed_references_are_rejected(self):
        good_orders = orders_ab()
        bad_refs = [
            {'order_id': 'ZZZ', 'edition_id': 'ea-a1', 'signature': 1,
             'sheet': 1, 'side': 'front'},
            {'order_id': 'A', 'edition_id': 'nope', 'signature': 1,
             'sheet': 1, 'side': 'front'},
            {'order_id': 'A', 'edition_id': 'ea-a1', 'signature': 0,
             'sheet': 1, 'side': 'front'},
            {'order_id': 'A', 'edition_id': 'ea-a1', 'signature': 1,
             'sheet': 1, 'side': 'spine'},
            {'order_id': 'A', 'edition_id': 'ea-a1', 'signature': '1',
             'sheet': 1, 'side': 'front'},
        ]
        for bad in bad_refs:
            with self.assertRaises(ValueError):
                settle(good_orders, [revise('ev-1', pool('X', 10, [bad]))])
        with self.assertRaises(ValueError):
            settle(good_orders, [close('ev-1', 'st-1', 'ZZZ')])


class StructuralValidationTests(unittest.TestCase):
    def test_top_level_and_order_bounds(self):
        good = orders_ab()
        for broken in [
            {'events': []},
            {'orders': [], 'events': []},
            {'orders': good + [{'order_id': 'A',
                                'proof_batch': batch('dup')}], 'events': []},
            {'orders': good, 'events': [], 'extra': 1},
        ]:
            with self.assertRaises(ValueError):
                process_settlement(broken)
        with self.assertRaises(ValueError):
            process_settlement({'orders': [
                {'order_id': 'A', 'proof_batch': batch('x')}
                for _ in range(33)], 'events': []})

    def test_event_id_and_statement_id_uniqueness(self):
        base = [revise('ev-1', pool('X', 10, [ref('A', 'ea-a1')]))]
        with self.assertRaises(ValueError):
            settle(orders_ab(), base + [close('ev-1', 'st-1', 'A')])
        with self.assertRaises(ValueError):
            settle(orders_ab(), base + [close('ev-2', 'st-1', 'A'),
                                       close('ev-3', 'st-1', 'A')])

    def test_revise_structure_rules(self):
        cases = [
            [{'event_id': 'ev-1', 'type': 'revise', 'pools': []}],
            [{'event_id': 'ev-1', 'type': 'revise',
              'pools': [pool('X', 5, [])]}],                       # empty new pool
            [{'event_id': 'ev-1', 'type': 'revise',
              'pools': [pool('X', 5, []), pool('X', 5, [])]}],     # duplicate pool in event
            [revise('ev-1', pool('X', -1, [ref('A', 'ea-a1')]))],
            [revise('ev-1', pool('X', 10 ** 12 + 1,
                                 [ref('A', 'ea-a1')]))],
            [{'event_id': 'ev-1', 'type': 'frobnicate'}],
        ]
        for events in cases:
            with self.assertRaises(ValueError):
                settle(orders_ab(), events)

    def test_midstream_error_is_not_masked_by_later_events(self):
        # The close at ev-3 references an unknown order; ev-4 would be legal.
        events = [
            revise('ev-1', pool('X', 10, [ref('A', 'ea-a1')])),
            close('ev-2', 'st-a1', 'A'),
            close('ev-3', 'st-bad', 'ZZZ'),
            close('ev-4', 'st-a2', 'A'),
        ]
        with self.assertRaises(ValueError):
            settle(orders_ab(), events)

    def test_events_must_be_an_array_within_bounds(self):
        with self.assertRaises(ValueError):
            settle(orders_ab(), {'event_id': 'ev-1', 'type': 'revise', 'pools': []})

    def test_fen_bounds_and_non_list_uses_are_rejected(self):
        good_ref = ref('A', 'ea-a1')
        for total in [-1, 10 ** 12 + 1, '10', True]:
            with self.assertRaises(ValueError):
                settle(orders_ab(), [revise('ev-1', pool('X', total, [good_ref]))])
        with self.assertRaises(ValueError):
            settle(orders_ab(), [
                {'event_id': 'ev-1', 'type': 'revise',
                 'pools': [{'pool_id': 'X', 'total_fen': 5, 'uses': 'not-a-list'}]}])
        # The upper bound itself is accepted.
        report = settle(orders_ab(), [
            revise('ev-1', pool('X', 10 ** 12, [good_ref]))])
        self.assertEqual(pool_by_id(report, 'X')['total_fen'], 10 ** 12)

    def test_close_rejects_extra_or_missing_fields(self):
        good = revise('ev-1', pool('X', 10, [ref('A', 'ea-a1')]))
        malformed = [
            {'event_id': 'ev-2', 'type': 'close', 'statement_id': 'st-1',
             'order_id': 'A', 'pools': []},
            {'event_id': 'ev-3', 'type': 'close', 'statement_id': 'st-2'},
        ]
        for event in malformed:
            with self.assertRaises(ValueError):
                settle(orders_ab(), [good, event])


class DeterminismTests(unittest.TestCase):
    def test_permutation_of_orders_pools_and_faces_keeps_results_identical(self):
        events = [
            revise('ev-1', pool('X', 101, [ref('A', 'ea-a1'), ref('A', 'ea-a2'),
                                           ref('B', 'eb-b1')]),
                   pool('Y', 16, [ref('B', 'eb-b1', side='back')])),
            close('ev-2', 'st-a1', 'A'),
            close('ev-3', 'st-b1', 'B'),
        ]
        orders = orders_ab()
        baseline = settle(orders, events)
        shuffled_orders = list(reversed(copy.deepcopy(orders)))
        shuffled_events = copy.deepcopy(events)
        shuffled_events[0]['pools'].reverse()
        for item in shuffled_events[0]['pools']:
            item['uses'].reverse()
        permuted = settle(shuffled_orders, shuffled_events)

        def canonical(report):
            normalized = copy.deepcopy(report)
            normalized['orders'] = sorted(normalized['orders'])
            normalized['outstanding'] = sorted(normalized['outstanding'],
                                               key=lambda entry: entry['order_id'])
            return json.dumps(normalized, sort_keys=True)

        self.assertEqual(canonical(baseline), canonical(permuted))
        # Pool and statement sections are key-sorted / event-ordered themselves,
        # so they are byte-identical without any normalization.
        self.assertEqual(json.dumps(baseline['pools'], sort_keys=True),
                         json.dumps(permuted['pools'], sort_keys=True))
        self.assertEqual(json.dumps(baseline['statements'], sort_keys=True),
                         json.dumps(permuted['statements'], sort_keys=True))

    def test_conservation_invariants_hold_on_a_long_mixed_log(self):
        events = [
            revise('ev-1', pool('X', 101, [ref('A', 'ea-a1'), ref('A', 'ea-a2'),
                                           ref('B', 'eb-b1')])),
            close('ev-2', 'st-a1', 'A'),
            revise('ev-3', pool('X', 101, [ref('A', 'ea-a1'), ref('B', 'eb-b1')]),
                   pool('Y', 16, [ref('A', 'ea-a2')])),
            close('ev-4', 'st-a2', 'A'),
            close('ev-5', 'st-b1', 'B'),
            revise('ev-6', pool('X', 101, [ref('B', 'eb-b1')])),
            close('ev-7', 'st-a3', 'A'),
            revise('ev-8', pool('X', 0, [])),
            close('ev-9', 'st-b2', 'B'),
            revise('ev-10', pool('X', 9, [ref('B', 'eb-b1', side='back')])),
            close('ev-11', 'st-b3', 'B'),
        ]
        report = settle(orders_ab(), events)
        for entry in report['pools']:
            self.assertEqual(sum(p['payable_fen'] for p in entry['participants']),
                             entry['total_fen'])
        for order_entry in report['outstanding']:
            for item in order_entry['pools']:
                self.assertEqual(item['settled_fen'] + item['open_difference_fen'],
                                 item['payable_fen'])


class CliSettlementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__file__).resolve().parent / '_tmp_settlement'
        self.tmp.mkdir(exist_ok=True)

    def tearDown(self):
        for child in self.tmp.iterdir():
            child.unlink()
        self.tmp.rmdir()

    def _run(self, source_path, output_path, *flags):
        args = [sys.executable, '-m', 'signature_press.cli', '--source', str(source_path),
                '--output', str(output_path), *flags]
        return subprocess.run(args, capture_output=True, text=True)

    def test_cli_runs_fixture_and_never_overwrites(self):
        fixture = Path(__file__).resolve().parents[1] / 'fixtures' / 'plate-settlement.json'
        output = self.tmp / 'settlement-out.json'
        first = self._run(fixture, output, '--plate-settlement')
        self.assertEqual(first.returncode, 0, first.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report['schema_version'], 4)
        self.assertEqual(report['summary']['statements'], 8)
        before = output.read_bytes()
        siblings = sorted(p.name for p in self.tmp.iterdir())
        again = self._run(fixture, output, '--plate-settlement')
        self.assertEqual(again.returncode, 2)
        self.assertEqual(output.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), siblings)

    def test_invalid_input_exits_two_and_publishes_nothing(self):
        source = self.tmp / 'bad.json'
        output = self.tmp / 'bad-out.json'
        source.write_text(json.dumps({'orders': [], 'events': []}))
        failed = self._run(source, output, '--plate-settlement')
        self.assertEqual(failed.returncode, 2)
        self.assertFalse(output.exists())
        self.assertFalse(any(p.name.startswith('.report-') for p in self.tmp.iterdir()))

    def test_mode_flags_are_mutually_exclusive(self):
        source = self.tmp / 'any.json'
        output = self.tmp / 'any-out.json'
        source.write_text(json.dumps({'orders': orders_ab(), 'events': []}))
        both = self._run(source, output, '--plate-settlement', '--proof-batch')
        self.assertEqual(both.returncode, 2)
        self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
