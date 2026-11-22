import itertools
import json
import random
import subprocess
import sys
import unittest
from pathlib import Path

from signature_press.batch import process_batch
from signature_press.meter import process_meter

GEOMETRY = {'sheet_width_um': 440000, 'sheet_height_um': 320000, 'leaf_width_um': 200000,
            'leaf_height_um': 280000, 'margin_um': 10000, 'gutter_um': 10000,
            'thickness_um': 120}


def edition(eid, pages=16, cap=8, sections=None):
    return {'id': eid,
            'source': {'sections': sections or [{'id': 'T', 'pages': pages}],
                       'signature_pages': cap, 'binding': 'left',
                       'geometry': GEOMETRY},
            'revisions': ['r1'] * pages}


def proof_batch(*editions, legacy=None):
    return {'editions': list(editions), 'legacy_lots': legacy or []}


def machine(mid, rate=1, modulus=1000, initial=0):
    return {'id': mid, 'max_sheets_per_second': rate, 'modulus': modulus,
            'initial_reading': initial}


def init_package(batch, boundaries, machines):
    return {'proof_batch': batch, 'shift_boundaries': boundaries,
            'machines': machines}


def reading(machine_id, second, value):
    return {'machine': machine_id, 'second': second, 'value': value}


def mounting(machine_id, start, end, edition_id, signature):
    return {'machine': machine_id, 'start': start, 'end': end,
            'edition_id': edition_id, 'signature': signature}


def watermark(machine_id, readings=None, mountings=None):
    row = {'machine': machine_id}
    if readings is not None:
        row['readings'] = readings
    if mountings is not None:
        row['mountings'] = mountings
    return row


def package(state, watermarks=(), readings=(), mountings=()):
    pack = {'state': state}
    if watermarks:
        pack['watermarks'] = list(watermarks)
    if readings:
        pack['readings'] = list(readings)
    if mountings:
        pack['mountings'] = list(mountings)
    return pack


def fresh_signatures(batch):
    order = process_batch(batch)
    return {entry['id']: [s['signature'] for s in entry['signatures']
                          if s['source'] == 'fresh_print']
            for entry in order['editions']}


def roundtrip(state):
    """Simulate archiving: only the JSON state survives between calls."""
    return json.loads(json.dumps(state))


def without_sequence(state):
    state = json.loads(json.dumps(state))
    state.pop('artifact_sequence')
    return state


# ------------------------------------------------------- acceptance: test 1

class AcceptanceChainTests(unittest.TestCase):
    """The two-fresh-signature, two-shift chain from the task contract."""

    def setUp(self):
        self.batch = proof_batch(edition('E', pages=16, cap=8))
        self.assertEqual(fresh_signatures(self.batch), {'E': [1, 2]})
        self.init = init_package(self.batch, [0, 10, 20],
                                 [machine('M1', rate=1, modulus=1000, initial=100)])
        self.mounts = [mounting('M1', 0, 8, 'E', 1),
                       mounting('M1', 10, 20, 'E', 2)]

    def first_package(self):
        return {'init': self.init,
                'watermarks': [watermark('M1', readings=5, mountings=20)],
                'readings': [reading('M1', 15, 111)],
                'mountings': self.mounts}

    def test_first_package_cannot_close_yet(self):
        artifact = process_meter(self.first_package())
        self.assertEqual(artifact['schema_version'], 7)
        self.assertEqual(artifact['artifact_sequence'], 1)
        self.assertEqual(artifact['new_slips'], [])
        diagnosis = artifact['diagnosis']
        self.assertEqual(diagnosis['kind'], 'incomplete')
        self.assertEqual((diagnosis['shift_index'], diagnosis['shift_start'],
                          diagnosis['shift_end']), (0, 0, 10))
        self.assertEqual(diagnosis['missing'],
                         [{'machine': 'M1', 'lane': 'readings',
                           'reason': 'readings_not_sealed', 'segment_right': 15,
                           'reading_watermark': 5}])
        self.assertEqual(artifact['summary']['diagnosis'], 'incomplete')

    def test_watermark_only_advance_is_ambiguous_6_to_8(self):
        first = process_meter(self.first_package())
        artifact = process_meter(package(roundtrip(first['continuation']),
                                         watermarks=[watermark('M1', readings=16)]))
        self.assertEqual(artifact['new_slips'], [])
        diagnosis = artifact['diagnosis']
        self.assertEqual(diagnosis['kind'], 'ambiguous')
        self.assertEqual(diagnosis['keys'],
                         [{'machine': 'M1', 'edition_id': 'E', 'signature': 1,
                           'min_sheets': 6, 'max_sheets': 8}])
        self.assertEqual(diagnosis['causes'],
                         [{'machine': 'M1', 'left_reading': [0, 100],
                           'right_reading': [15, 111],
                           'loose_keys': [{'edition_id': 'E', 'signature': 1,
                                           'min_sheets': 6, 'max_sheets': 8}]}])

    def test_backfill_before_readings_watermark_is_rejected(self):
        first = process_meter(self.first_package())
        sealed = process_meter(package(roundtrip(first['continuation']),
                                       watermarks=[watermark('M1', readings=16)]))
        # The state whose readings watermark is already 16 must not accept (5, 103).
        with self.assertRaises(ValueError):
            process_meter(package(roundtrip(sealed['continuation']),
                                  readings=[reading('M1', 5, 103)]))

    def test_both_alternative_continuations_from_one_artifact(self):
        first = process_meter(self.first_package())
        snapshot = json.dumps(first)
        # Path one: only advance the readings watermark -> ambiguous 6..8.
        only_watermark = process_meter(package(
            roundtrip(first['continuation']),
            watermarks=[watermark('M1', readings=16)]))
        self.assertEqual(only_watermark['diagnosis']['kind'], 'ambiguous')
        # Path two (from the same artifact, not from path one's state):
        # also backfill (5, 103) -> exact 6.
        with_backfill = process_meter(package(
            roundtrip(first['continuation']),
            watermarks=[watermark('M1', readings=16)],
            readings=[reading('M1', 5, 103)]))
        self.assertEqual(with_backfill['new_slips'][0]['machines'][0]['keys'],
                         [{'edition_id': 'E', 'signature': 1, 'sheets': 6}])
        # The shared first artifact was not mutated by either continuation.
        self.assertEqual(json.dumps(first), snapshot)

    def test_cross_shift_segment_chain(self):
        first = process_meter(self.first_package())
        # Alternative continuation: the same package both seals to 16 and
        # backfills (5, 103), which is still at the previous watermark.
        second = process_meter(package(roundtrip(first['continuation']),
                                       watermarks=[watermark('M1', readings=16)],
                                       readings=[reading('M1', 5, 103)]))
        # Shift 0 closes; shift 1 becomes the first pending shift and still
        # lacks a reading at or after its end.
        self.assertEqual(second['diagnosis'],
                         {'kind': 'incomplete', 'shift_index': 1,
                          'shift_start': 10, 'shift_end': 20,
                          'missing': [{'machine': 'M1', 'lane': 'readings',
                                       'reason': 'missing_right_reading',
                                       'shift_end': 20, 'latest_reading': 15}]})
        self.assertEqual(len(second['new_slips']), 1)
        slip = second['new_slips'][0]
        self.assertEqual((slip['shift_index'], slip['shift_start'],
                          slip['shift_end']), (0, 0, 10))
        entry, = slip['machines']
        self.assertEqual(entry['keys'],
                         [{'edition_id': 'E', 'signature': 1, 'sheets': 6}])
        self.assertEqual(entry['machine_total'], 6)
        self.assertEqual(slip['total_sheets'], 6)
        # Three of the six sheets come from the cross-shift segment [5, 15).
        segments = entry['segments']
        self.assertEqual([(s['left_reading'], s['right_reading'])
                          for s in segments],
                         [([0, 100], [5, 103]), ([5, 103], [15, 111])])
        self.assertEqual(segments[0]['contributions'],
                         [{'edition_id': 'E', 'signature': 1, 'sheets': 3}])
        self.assertEqual(segments[1]['contributions'],
                         [{'edition_id': 'E', 'signature': 1, 'sheets': 3}])
        # The cross-shift segment keeps exactly five sheets for B later.
        self.assertEqual(segments[1]['carried_forward'],
                         {'min_sheets': 5, 'max_sheets': 5})
        self.assertEqual(segments[1]['archived_before'], 0)

        # Discard the old packages, the old states and the first slip: only
        # the newest continuation state is carried forward.
        third = process_meter(package(roundtrip(second['continuation']),
                                      watermarks=[watermark('M1', readings=21)],
                                      readings=[reading('M1', 20, 114)]))
        self.assertEqual(third['diagnosis'], None)
        # The already issued first shift does not reappear.
        self.assertEqual(len(third['new_slips']), 1)
        slip2 = third['new_slips'][0]
        self.assertEqual((slip2['shift_index'], slip2['shift_start'],
                          slip2['shift_end']), (1, 10, 20))
        entry2, = slip2['machines']
        # B is exactly 8: neither just the new 3 nor the whole cross-shift 8.
        self.assertEqual(entry2['keys'],
                         [{'edition_id': 'E', 'signature': 2, 'sheets': 8}])
        self.assertEqual(slip2['total_sheets'], 8)
        seg_cross, seg_new = entry2['segments']
        self.assertEqual((seg_cross['left_reading'], seg_cross['right_reading']),
                         ([5, 103], [15, 111]))
        self.assertEqual(seg_cross['archived_before'], 3)
        self.assertEqual(seg_cross['contributions'],
                         [{'edition_id': 'E', 'signature': 2, 'sheets': 5}])
        self.assertEqual(seg_new['contributions'],
                         [{'edition_id': 'E', 'signature': 2, 'sheets': 3}])
        # Everything is closed now; no diagnosis remains.
        self.assertEqual(third['summary']['issued_shifts_total'], 2)

        # Recomputing from the complete evidence in one shot yields the
        # same two slips and the same final state (modulo the artifact
        # sequence, which counts the packages of its own chain).
        one_shot = process_meter({
            'init': self.init,
            'watermarks': [watermark('M1', readings=21, mountings=20)],
            'readings': [reading('M1', 5, 103), reading('M1', 15, 111),
                         reading('M1', 20, 114)],
            'mountings': self.mounts})
        self.assertEqual(one_shot['new_slips'], [slip, slip2])
        self.assertEqual(without_sequence(one_shot['continuation']),
                         without_sequence(third['continuation']))


# ------------------------------------------------------- acceptance: test 2

class WraparoundAmbiguityTests(unittest.TestCase):
    def setUp(self):
        self.batch = proof_batch(edition('E', pages=8, cap=8))
        self.assertEqual(fresh_signatures(self.batch), {'E': [1]})

    def test_single_wrap_ambiguity_then_unique_with_intermediate_reading(self):
        init = init_package(self.batch, [0, 11],
                            [machine('M1', rate=1, modulus=10, initial=9)])
        mounts = [mounting('M1', 0, 11, 'E', 1)]
        # Complete evidence: consumption is 1 or 11, so no slip is issued.
        artifact = process_meter({
            'init': init,
            'watermarks': [watermark('M1', readings=12, mountings=11)],
            'readings': [reading('M1', 11, 0)],
            'mountings': mounts})
        self.assertEqual(artifact['new_slips'], [])
        self.assertEqual(artifact['diagnosis']['kind'], 'ambiguous')
        self.assertEqual(artifact['diagnosis']['keys'],
                         [{'machine': 'M1', 'edition_id': 'E', 'signature': 1,
                           'min_sheets': 1, 'max_sheets': 11}])
        # An intermediate reading (2, 0) pinned down before sealing makes it 1.
        artifact = process_meter({
            'init': init,
            'watermarks': [watermark('M1', readings=12, mountings=11)],
            'readings': [reading('M1', 2, 0), reading('M1', 11, 0)],
            'mountings': mounts})
        self.assertEqual(len(artifact['new_slips']), 1)
        slip = artifact['new_slips'][0]
        self.assertEqual(slip['machines'][0]['keys'],
                         [{'edition_id': 'E', 'signature': 1, 'sheets': 1}])
        self.assertEqual(artifact['diagnosis'], None)

    def test_same_key_two_intervals_with_idle_gap_still_closes(self):
        init = init_package(self.batch, [0, 8],
                            [machine('M1', rate=1, modulus=100, initial=0)])
        # The same aggregation key holds two mounting intervals with an idle
        # gap; each interval's own sheets are uncertain but the shift total
        # across both is certain, so the slip is issued.
        artifact = process_meter({
            'init': init,
            'watermarks': [watermark('M1', readings=9, mountings=8)],
            'readings': [reading('M1', 8, 4)],
            'mountings': [mounting('M1', 0, 3, 'E', 1),
                          mounting('M1', 5, 8, 'E', 1)]})
        self.assertEqual(artifact['diagnosis'], None)
        slip, = artifact['new_slips']
        self.assertEqual(slip['machines'][0]['keys'],
                         [{'edition_id': 'E', 'signature': 1, 'sheets': 4}])
        self.assertEqual(slip['total_sheets'], 4)

    def test_two_machines_no_half_issued_slip(self):
        batch = proof_batch(edition('E', pages=16, cap=8))
        init = init_package(batch, [0, 5],
                            [machine('M1', rate=1, modulus=100, initial=0),
                             machine('M2', rate=1, modulus=100, initial=0)])
        artifact = process_meter({
            'init': init,
            'watermarks': [watermark('M1', readings=6, mountings=5),
                           watermark('M2', readings=3, mountings=5)],
            'readings': [reading('M1', 5, 1), reading('M2', 5, 2)],
            'mountings': [mounting('M1', 0, 5, 'E', 1),
                          mounting('M2', 0, 5, 'E', 2)]})
        # M1 is fully sealed but M2 is not: the shift is not issued at all.
        self.assertEqual(artifact['new_slips'], [])
        diagnosis = artifact['diagnosis']
        self.assertEqual(diagnosis['kind'], 'incomplete')
        self.assertEqual(diagnosis['missing'],
                         [{'machine': 'M2', 'lane': 'readings',
                           'reason': 'readings_not_sealed', 'segment_right': 5,
                           'reading_watermark': 3}])
        # Once M2 seals, the shift closes with both machines on one slip.
        second = process_meter(package(roundtrip(artifact['continuation']),
                                       watermarks=[watermark('M2', readings=6)]))
        slip, = second['new_slips']
        self.assertEqual(len(slip['machines']), 2)
        self.assertEqual(slip['machines'][0]['keys'],
                         [{'edition_id': 'E', 'signature': 1, 'sheets': 1}])
        self.assertEqual(slip['machines'][1]['keys'],
                         [{'edition_id': 'E', 'signature': 2, 'sheets': 2}])
        self.assertEqual(slip['total_sheets'], 3)


# ------------------------------------------------------- acceptance: test 3

class LongShiftTests(unittest.TestCase):
    HORIZON = 10 ** 9

    def setUp(self):
        self.batch = proof_batch(edition('E', pages=4, cap=4))
        self.init = {'proof_batch': self.batch,
                     'shift_boundaries': [0, self.HORIZON],
                     'machines': [{'id': 'M1', 'max_sheets_per_second': 1,
                                   'modulus': self.HORIZON, 'initial_reading': 0}]}
        self.mounts = [mounting('M1', 0, self.HORIZON, 'E', 1)]

    def test_billion_second_shift_closes_as_one(self):
        artifact = process_meter({
            'init': self.init,
            'watermarks': [watermark('M1', readings=self.HORIZON + 1,
                                     mountings=self.HORIZON)],
            'readings': [reading('M1', self.HORIZON, 1)],
            'mountings': self.mounts})
        slip, = artifact['new_slips']
        self.assertEqual(slip['machines'][0]['keys'],
                         [{'edition_id': 'E', 'signature': 1, 'sheets': 1}])
        # Nothing expands per second, sheet or counter value: the artifact
        # and the continuation state stay a few kilobytes.
        self.assertLess(len(json.dumps(artifact)), 6000)
        self.assertLess(len(json.dumps(artifact['continuation'])), 2000)

    def test_modulus_two_is_ambiguous_over_the_whole_range(self):
        init = json.loads(json.dumps(self.init))
        init['machines'][0]['modulus'] = 2
        artifact = process_meter({
            'init': init,
            'watermarks': [watermark('M1', readings=self.HORIZON + 1,
                                     mountings=self.HORIZON)],
            'readings': [reading('M1', self.HORIZON, 1)],
            'mountings': self.mounts})
        self.assertEqual(artifact['new_slips'], [])
        self.assertEqual(artifact['diagnosis']['kind'], 'ambiguous')
        self.assertEqual(artifact['diagnosis']['keys'],
                         [{'machine': 'M1', 'edition_id': 'E', 'signature': 1,
                           'min_sheets': 1, 'max_sheets': 999_999_999}])

    def test_contradiction_is_an_error_not_a_pending_shift(self):
        init = init_package(self.batch, [0, 5],
                            [machine('M1', rate=1, modulus=100, initial=0)])
        # 50 sheets modulo 100 can never fit into five seconds at 1 sheet/s.
        with self.assertRaises(ValueError):
            process_meter({
                'init': init,
                'watermarks': [watermark('M1', readings=6, mountings=5)],
                'readings': [reading('M1', 5, 50)],
                'mountings': [mounting('M1', 0, 5, 'E', 1)]})

    def test_long_shift_cli_under_memory_limit(self):
        tmp = Path(__file__).resolve().parent / '_tmp_meter_long'
        tmp.mkdir(exist_ok=True)
        try:
            source = tmp / 'long.json'
            output = tmp / 'long-out.json'
            source.write_text(json.dumps({
                'init': self.init,
                'watermarks': [watermark('M1', readings=self.HORIZON + 1,
                                         mountings=self.HORIZON)],
                'readings': [reading('M1', self.HORIZON, 1)],
                'mountings': self.mounts}))
            args = [sys.executable, '-m', 'signature_press.cli', '--press-meter',
                    '--source', str(source), '--output', str(output)]
            # Constrain a fresh shell's address space, not this test process.
            result = subprocess.run(['sh', '-c',
                                     'ulimit -v 262144; exec "$@"', 'sh'] + args,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            artifact = json.loads(output.read_text())
            self.assertEqual(artifact['new_slips'][0]['machines'][0]['keys'],
                             [{'edition_id': 'E', 'signature': 1, 'sheets': 1}])
        finally:
            for child in tmp.iterdir():
                child.unlink()
            tmp.rmdir()

    def test_contradiction_checked_even_outside_the_pending_shift(self):
        # Two shifts; the contradictory segment sits in the second one while
        # the first shift could otherwise close.
        init = init_package(self.batch, [0, 5, 10],
                            [machine('M1', rate=1, modulus=100, initial=0)])
        with self.assertRaises(ValueError):
            process_meter({
                'init': init,
                'watermarks': [watermark('M1', readings=11, mountings=10)],
                'readings': [reading('M1', 5, 1), reading('M1', 10, 60)],
                'mountings': [mounting('M1', 0, 10, 'E', 1)]})

    def test_idle_machine_with_nonzero_residue_is_contradictory(self):
        init = init_package(self.batch, [0, 5],
                            [machine('M1', rate=1, modulus=100, initial=0)])
        with self.assertRaises(ValueError):
            process_meter({
                'init': init,
                'watermarks': [watermark('M1', readings=6, mountings=5)],
                'readings': [reading('M1', 5, 1)],
                'mountings': []})


# ----------------------------------------------------- continuation behaviour

class MultiShiftFrontierTests(unittest.TestCase):
    """One reading segment straddling two shift boundaries."""

    def setUp(self):
        self.batch = proof_batch(edition('E', pages=16, cap=8))
        self.init = init_package(self.batch, [0, 10, 20, 30],
                                 [machine('M1', rate=1, modulus=1000, initial=0)])
        self.mounts = [mounting('M1', 0, 10, 'E', 1),
                       mounting('M1', 10, 30, 'E', 2)]
        self.rows = [reading('M1', 5, 2), reading('M1', 25, 22),
                     reading('M1', 30, 25)]

    def test_segment_carrying_two_cutoffs(self):
        artifact = process_meter({
            'init': self.init,
            'watermarks': [watermark('M1', readings=31, mountings=30)],
            'readings': self.rows, 'mountings': self.mounts})
        self.assertEqual(artifact['diagnosis'], None)
        self.assertEqual(len(artifact['new_slips']), 3)
        first, second, third = artifact['new_slips']
        # Shift [0,10): A = 2 + 5 = 7; segment [5,25) archives 5 of its 20.
        self.assertEqual(first['machines'][0]['keys'],
                         [{'edition_id': 'E', 'signature': 1, 'sheets': 7}])
        cross = first['machines'][0]['segments'][1]
        self.assertEqual((cross['left_reading'], cross['right_reading']),
                         ([5, 2], [25, 22]))
        self.assertEqual(cross['carried_forward'],
                         {'min_sheets': 15, 'max_sheets': 15})
        # Shift [10,20): B gets exactly 10 of the remaining 15.
        self.assertEqual(second['machines'][0]['keys'],
                         [{'edition_id': 'E', 'signature': 2, 'sheets': 10}])
        cross2 = second['machines'][0]['segments'][0]
        self.assertEqual((cross2['left_reading'], cross2['right_reading']),
                         ([5, 2], [25, 22]))
        self.assertEqual(cross2['archived_before'], 5)
        self.assertEqual(cross2['in_shift_sheets'], 10)
        # Shift [20,30): B gets the last 5 plus the 3 from [25,30).
        self.assertEqual(third['machines'][0]['keys'],
                         [{'edition_id': 'E', 'signature': 2, 'sheets': 8}])
        self.assertEqual(third['machines'][0]['segments'][0]['archived_before'],
                         15)
        # Conservation across the three slips: 7 + 10 + 8 = 2 + 20 + 3.
        self.assertEqual(sum(s['total_sheets'] for s in artifact['new_slips']),
                         25)

    def test_same_chain_across_packages(self):
        first = process_meter({
            'init': self.init,
            'watermarks': [watermark('M1', readings=6, mountings=30)],
            'readings': self.rows, 'mountings': self.mounts})
        self.assertEqual(first['new_slips'], [])
        second = process_meter(package(roundtrip(first['continuation']),
                                       watermarks=[watermark('M1', readings=26)]))
        self.assertEqual([s['shift_index'] for s in second['new_slips']], [0, 1])
        third = process_meter(package(roundtrip(second['continuation']),
                                      watermarks=[watermark('M1', readings=31)]))
        self.assertEqual([s['shift_index'] for s in third['new_slips']], [2])
        one_shot = process_meter({
            'init': self.init,
            'watermarks': [watermark('M1', readings=31, mountings=30)],
            'readings': self.rows, 'mountings': self.mounts})
        self.assertEqual(second['new_slips'] + third['new_slips'],
                         one_shot['new_slips'])
        self.assertEqual(without_sequence(third['continuation']),
                         without_sequence(one_shot['continuation']))


class BoundaryAndKeyShapeTests(unittest.TestCase):
    def setUp(self):
        self.batch = proof_batch(edition('E', pages=16, cap=8))

    def test_reading_exactly_on_the_boundary_leaves_no_frontier(self):
        artifact = process_meter({
            'init': init_package(self.batch, [0, 10, 20],
                                 [machine('M1', rate=1, modulus=100, initial=0)]),
            'watermarks': [watermark('M1', readings=21, mountings=20)],
            'readings': [reading('M1', 10, 3), reading('M1', 20, 8)],
            'mountings': [mounting('M1', 0, 20, 'E', 1)]})
        self.assertEqual([s['total_sheets'] for s in artifact['new_slips']],
                         [3, 5])
        state = artifact['continuation']
        self.assertIsNone(state['machines'][0]['frontier'])
        self.assertEqual(state['machines'][0]['anchor'],
                         {'second': 20, 'value': 8})

    def test_mountings_not_sealed_diagnosis(self):
        artifact = process_meter({
            'init': init_package(self.batch, [0, 10],
                                 [machine('M1', rate=1, modulus=1000,
                                          initial=100)]),
            'watermarks': [watermark('M1', readings=11, mountings=5)],
            'readings': [reading('M1', 10, 111)],
            'mountings': [mounting('M1', 0, 8, 'E', 1)]})
        self.assertEqual(artifact['diagnosis'],
                         {'kind': 'incomplete', 'shift_index': 0,
                          'shift_start': 0, 'shift_end': 10,
                          'missing': [{'machine': 'M1', 'lane': 'mountings',
                                       'reason': 'mountings_not_sealed',
                                       'segment_right': 10,
                                       'mounting_watermark': 5}]})

    def test_same_signature_on_two_machines_at_once(self):
        artifact = process_meter({
            'init': init_package(self.batch, [0, 5],
                                 [machine('M1', rate=1, modulus=100, initial=0),
                                  machine('M2', rate=2, modulus=100, initial=0)]),
            'watermarks': [watermark('M1', readings=6, mountings=5),
                           watermark('M2', readings=6, mountings=5)],
            'readings': [reading('M1', 5, 2), reading('M2', 5, 7)],
            'mountings': [mounting('M1', 0, 5, 'E', 1),
                          mounting('M2', 0, 5, 'E', 1)]})
        slip, = artifact['new_slips']
        self.assertEqual(slip['machines'][0]['keys'],
                         [{'edition_id': 'E', 'signature': 1, 'sheets': 2}])
        self.assertEqual(slip['machines'][1]['keys'],
                         [{'edition_id': 'E', 'signature': 1, 'sheets': 7}])
        self.assertEqual(slip['total_sheets'], 9)

    def test_mounted_key_with_zero_sheets_is_listed(self):
        artifact = process_meter({
            'init': init_package(self.batch, [0, 5],
                                 [machine('M1', rate=1, modulus=100, initial=0)]),
            'watermarks': [watermark('M1', readings=6, mountings=5)],
            'readings': [reading('M1', 5, 0)],
            'mountings': [mounting('M1', 0, 5, 'E', 1)]})
        slip, = artifact['new_slips']
        self.assertEqual(slip['machines'][0]['keys'],
                         [{'edition_id': 'E', 'signature': 1, 'sheets': 0}])
        self.assertEqual(slip['total_sheets'], 0)


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.batch = proof_batch(edition('E', pages=16, cap=8))
        self.init = init_package(self.batch, [0, 10, 20],
                                 [machine('M1', rate=1, modulus=1000, initial=100)])
        self.mounts = [mounting('M1', 0, 8, 'E', 1),
                       mounting('M1', 10, 20, 'E', 2)]

    def test_package_without_new_shift_still_continues(self):
        first = process_meter({'init': self.init,
                               'watermarks': [watermark('M1', readings=5,
                                                        mountings=20)],
                               'readings': [reading('M1', 15, 111)],
                               'mountings': self.mounts})
        # An empty package: no new rows, no watermark moves, no new slip.
        artifact = process_meter(package(roundtrip(first['continuation'])))
        self.assertEqual(artifact['new_slips'], [])
        self.assertEqual(artifact['diagnosis'], first['diagnosis'])
        self.assertEqual(artifact['artifact_sequence'], 2)
        self.assertEqual(artifact['continuation']['artifact_sequence'], 2)

    def test_row_order_inside_a_package_does_not_matter(self):
        rows_a = [reading('M1', 5, 103), reading('M1', 15, 111),
                  reading('M1', 20, 114)]
        rows_b = [reading('M1', 20, 114), reading('M1', 5, 103),
                  reading('M1', 15, 111)]
        mounts = [mounting('M1', 10, 20, 'E', 2), mounting('M1', 0, 8, 'E', 1)]
        base = {'init': self.init,
                'watermarks': [watermark('M1', readings=21, mountings=20)]}
        one = process_meter(dict(base, readings=rows_a,
                                 mountings=self.mounts))
        two = process_meter(dict(base, readings=rows_b,
                                 mountings=list(reversed(mounts))))
        self.assertEqual(one, two)

    def test_illegal_retained_signature_is_rejected(self):
        legacy = {'id': 'legacy', 'source': {
            'sections': [{'id': 'B', 'pages': 8}], 'signature_pages': 12,
            'binding': 'left', 'geometry': GEOMETRY},
            'revisions': ['r1'] * 8,
            'retained': [{'stock_id': 's1', 'signature': 1}]}
        batch = {'editions': [
            {'id': 'E', 'source': {'sections': [{'id': 'P', 'pages': 1},
                                                {'id': 'B', 'pages': 8},
                                                {'id': 'Q', 'pages': 3}],
                                   'signature_pages': 12, 'binding': 'left',
                                   'geometry': GEOMETRY},
             'revisions': ['r1'] * 12}],
            'legacy_lots': [legacy]}
        order = process_batch(batch)
        sources = {s['signature']: s['source'] for s in order['editions'][0]['signatures']}
        self.assertEqual(sources[2], 'retained')
        init = init_package(batch, [0, 5],
                            [machine('M1', rate=1, modulus=100, initial=0)])
        # Signature 2 is a retained legacy piece, not a fresh-print signature.
        with self.assertRaises(ValueError):
            process_meter({'init': init,
                           'mountings': [mounting('M1', 0, 5, 'E', 2)]})
        # A signature number that does not exist in the work order at all.
        with self.assertRaises(ValueError):
            process_meter({'init': init,
                           'mountings': [mounting('M1', 0, 5, 'E', 99)]})
        # An edition that does not exist in the work order.
        with self.assertRaises(ValueError):
            process_meter({'init': init,
                           'mountings': [mounting('M1', 0, 5, 'nope', 1)]})

    def test_state_alone_is_enough_after_archiving(self):
        # The continuation state validates as a fresh input even after a
        # JSON round trip with every other artifact deleted.
        first = process_meter({'init': self.init,
                               'watermarks': [watermark('M1', readings=5,
                                                        mountings=20)],
                               'readings': [reading('M1', 15, 111)],
                               'mountings': self.mounts})
        state = roundtrip(first['continuation'])
        again = process_meter(package(state))
        self.assertEqual(again['artifact_sequence'], 2)
        self.assertEqual(again['diagnosis'], first['diagnosis'])


# ------------------------------------------------------------------ validation

class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.batch = proof_batch(edition('E', pages=8, cap=8))
        self.init = init_package(self.batch, [0, 10],
                                 [machine('M1', rate=1, modulus=100, initial=0)])

    def test_init_and_state_are_exclusive_and_required(self):
        with self.assertRaises(ValueError):
            process_meter({})
        with self.assertRaises(ValueError):
            process_meter({'init': self.init, 'state': {}})
        with self.assertRaises(ValueError):
            process_meter({'init': self.init, 'unexpected': 1})

    def test_init_validation(self):
        # boundaries must start at 0 and strictly increase
        bad = json.loads(json.dumps(self.init))
        bad['shift_boundaries'] = [1, 10]
        with self.assertRaises(ValueError):
            process_meter({'init': bad})
        bad = json.loads(json.dumps(self.init))
        bad['shift_boundaries'] = [0, 10, 10]
        with self.assertRaises(ValueError):
            process_meter({'init': bad})
        # too many shifts
        bad = json.loads(json.dumps(self.init))
        bad['shift_boundaries'] = list(range(66))
        with self.assertRaises(ValueError):
            process_meter({'init': bad})
        # machine limits
        for key, value in (('max_sheets_per_second', 0),
                           ('max_sheets_per_second', 10001),
                           ('modulus', 1), ('modulus', 10 ** 9 + 1),
                           ('initial_reading', 100)):
            bad = json.loads(json.dumps(self.init))
            bad['machines'][0][key] = value
            with self.assertRaises(ValueError, msg=f'{key}={value}'):
                process_meter({'init': bad})
        # duplicate machine id
        bad = json.loads(json.dumps(self.init))
        bad['machines'].append(dict(bad['machines'][0]))
        with self.assertRaises(ValueError):
            process_meter({'init': bad})
        # booleans are not integers
        bad = json.loads(json.dumps(self.init))
        bad['machines'][0]['modulus'] = True
        with self.assertRaises(ValueError):
            process_meter({'init': bad})
        # a broken proof batch is an input error
        with self.assertRaises(ValueError):
            process_meter({'init': {'proof_batch': {'editions': [], 'legacy_lots': []},
                                    'shift_boundaries': [0, 10],
                                    'machines': [machine('M1')]}})

    def test_row_validation(self):
        def run(**overrides):
            pack = {'init': json.loads(json.dumps(self.init))}
            pack.update(overrides)
            return process_meter(pack)

        with self.assertRaises(ValueError):  # unknown machine
            run(readings=[reading('M9', 5, 1)])
        with self.assertRaises(ValueError):  # second out of range
            run(readings=[reading('M1', 0, 1)])
        with self.assertRaises(ValueError):
            run(readings=[reading('M1', 11, 1)])
        with self.assertRaises(ValueError):  # value out of modulus
            run(readings=[reading('M1', 5, 100)])
        with self.assertRaises(ValueError):  # boolean is not an integer
            run(readings=[{'machine': 'M1', 'second': 5, 'value': True}])
        with self.assertRaises(ValueError):  # duplicate reading in one package
            run(readings=[reading('M1', 5, 1), reading('M1', 5, 2)])
        with self.assertRaises(ValueError):  # duplicate of the initial reading
            run(readings=[reading('M1', 0, 0)])
        with self.assertRaises(ValueError):  # empty mounting interval
            run(mountings=[mounting('M1', 3, 3, 'E', 1)])
        with self.assertRaises(ValueError):  # mounting beyond the horizon
            run(mountings=[mounting('M1', 3, 11, 'E', 1)])
        with self.assertRaises(ValueError):  # overlapping mountings
            run(mountings=[mounting('M1', 0, 5, 'E', 1),
                           mounting('M1', 4, 8, 'E', 1)])
        # adjacent mountings are fine
        artifact = run(mountings=[mounting('M1', 0, 5, 'E', 1),
                                  mounting('M1', 5, 8, 'E', 1)])
        self.assertEqual(artifact['summary']['diagnosis'], 'incomplete')
        with self.assertRaises(ValueError):  # unknown machine watermark
            run(watermarks=[watermark('M9', readings=1)])
        with self.assertRaises(ValueError):  # watermark above the horizon
            run(watermarks=[watermark('M1', mountings=11)])
        with self.assertRaises(ValueError):  # readings watermark above T+1
            run(watermarks=[watermark('M1', readings=12)])
        with self.assertRaises(ValueError):  # empty watermark row
            run(watermarks=[{'machine': 'M1'}])
        with self.assertRaises(ValueError):  # duplicate watermark row
            run(watermarks=[watermark('M1', readings=1),
                            watermark('M1', mountings=1)])

    def test_watermarks_never_decrease_and_rows_never_backfill(self):
        first = process_meter({'init': self.init,
                               'watermarks': [watermark('M1', readings=6,
                                                        mountings=4)]})
        state = roundtrip(first['continuation'])
        with self.assertRaises(ValueError):
            process_meter(package(state, watermarks=[watermark('M1', readings=5)]))
        with self.assertRaises(ValueError):
            process_meter(package(state, watermarks=[watermark('M1', mountings=3)]))
        with self.assertRaises(ValueError):
            process_meter(package(state, readings=[reading('M1', 5, 1)]))
        with self.assertRaises(ValueError):
            process_meter(package(state,
                                  mountings=[mounting('M1', 3, 5, 'E', 1)]))

    def test_duplicate_against_kept_rows_and_overlap_with_kept(self):
        first = process_meter({'init': self.init,
                               'readings': [reading('M1', 8, 1)],
                               'mountings': [mounting('M1', 6, 9, 'E', 1)]})
        state = roundtrip(first['continuation'])
        with self.assertRaises(ValueError):
            process_meter(package(state, readings=[reading('M1', 8, 2)]))
        with self.assertRaises(ValueError):
            process_meter(package(state,
                                  mountings=[mounting('M1', 7, 10, 'E', 1)]))

    def test_package_and_chain_row_limits(self):
        rows = [reading('M1', second, 0) for second in range(1, 2002)]
        with self.assertRaises(ValueError):
            process_meter({'init': self.init, 'readings': rows})
        # A hand-built but valid state near the chain row limit.
        state = {'schema_version': 7, 'artifact_sequence': 1,
                 'chain': {'shift_boundaries': [0, 10],
                           'machines': [machine('M1')],
                           'fresh_signatures': [{'edition_id': 'E',
                                                 'signatures': [1]}]},
                 'next_shift_index': 0, 'total_rows': 19_999,
                 'machines': [{'id': 'M1', 'reading_watermark': 0,
                               'mounting_watermark': 0,
                               'anchor': {'second': 0, 'value': 0},
                               'frontier': None, 'readings': [],
                               'mountings': []}]}
        artifact = process_meter(package(json.loads(json.dumps(state)),
                                         readings=[reading('M1', 5, 1)]))
        self.assertEqual(artifact['continuation']['total_rows'], 20_000)
        with self.assertRaises(ValueError):
            process_meter(package(json.loads(json.dumps(state)),
                                  readings=[reading('M1', 5, 1),
                                            reading('M1', 6, 1)]))

    def test_corrupt_state_is_rejected(self):
        first = process_meter({'init': self.init,
                               'readings': [reading('M1', 8, 1)]})
        good = roundtrip(first['continuation'])
        with self.assertRaises(ValueError):  # wrong schema
            bad = json.loads(json.dumps(good)); bad['schema_version'] = 6
            process_meter(package(bad))
        with self.assertRaises(ValueError):  # anchor off the cutoff
            bad = json.loads(json.dumps(good))
            bad['machines'][0]['anchor']['second'] = 1
            process_meter(package(bad))
        with self.assertRaises(ValueError):  # watermark below the cutoff
            bad = json.loads(json.dumps(good))
            bad['next_shift_index'] = 1
            process_meter(package(bad))
        with self.assertRaises(ValueError):  # unknown fresh signature in state
            bad = json.loads(json.dumps(good))
            bad['machines'][0]['mountings'] = [
                mounting('M1', 0, 5, 'E', 7)]
            process_meter(package(bad))
        with self.assertRaises(ValueError):  # overlapping state mountings
            bad = json.loads(json.dumps(good))
            bad['machines'][0]['mountings'] = [mounting('M1', 0, 5, 'E', 1),
                                               mounting('M1', 4, 8, 'E', 1)]
            process_meter(package(bad))
        with self.assertRaises(ValueError):  # duplicate state readings
            bad = json.loads(json.dumps(good))
            bad['machines'][0]['readings'] = [{'second': 8, 'value': 1},
                                              {'second': 8, 'value': 2}]
            process_meter(package(bad))


# ------------------------------------------------------- randomized cross-check

def reference_bounds(modulus, rate, segments, mountings, start, end):
    """Brute-force per-key shift bounds by enumerating per-second sheets."""
    totals = {}
    for left, right, residue in segments:
        keys = []
        for second in range(left, right):
            key = None
            for record in mountings:
                if record['start'] <= second < record['end']:
                    key = (record['edition_id'], record['signature'])
                    break
            keys.append(key)
        choices = [range(rate + 1) if key is not None else (0,) for key in keys]
        best = None
        for assignment in itertools.product(*choices):
            if sum(assignment) % modulus != residue:
                continue
            inside = {}
            for offset, amount in enumerate(assignment):
                key = keys[offset]
                if key is not None and start <= left + offset < end:
                    inside[key] = inside.get(key, 0) + amount
            if best is None:
                best = {}
            for key in {key for key in keys if key is not None}:
                value = inside.get(key, 0)
                low, high = best.get(key, (value, value))
                best[key] = (min(low, value), max(high, value))
        if best is None:
            return None  # infeasible segment
        for key, (low, high) in best.items():
            bounds = totals.setdefault(key, [0, 0])
            bounds[0] += low
            bounds[1] += high
    return totals


class RandomizedCrossCheckTests(unittest.TestCase):
    def scenario(self, rng):
        horizon = rng.randint(4, 7)
        modulus = rng.choice((2, 3, 5, 10))
        rate = rng.choice((1, 2))
        initial = rng.randrange(modulus)
        mountings = []
        position = 0
        while position < horizon and rng.random() < 0.8:
            length = rng.randint(1, horizon - position)
            if rng.random() < 0.8:
                mountings.append(mounting('M1', position, position + length,
                                          'E', rng.randint(1, 2)))
            position += length + rng.randint(0, 1)
        # Draw a true per-second consumption and derive the counter values,
        # so every scenario is feasible by construction (wraps included).
        mounted_at = {}
        for record in mountings:
            for second in range(record['start'], record['end']):
                mounted_at[second] = record
        seconds = sorted(rng.sample(range(1, horizon + 1),
                                    rng.randint(1, min(3, horizon))))
        if seconds[-1] != horizon:
            seconds.append(horizon)
        values = {}
        cumulative = initial
        previous = 0
        for second in seconds:
            for moment in range(previous, second):
                if moment in mounted_at:
                    cumulative += rng.randint(0, rate)
            values[second] = cumulative % modulus
            previous = second
        batch = proof_batch(edition('E', pages=16, cap=8))
        init = init_package(batch, [0, horizon],
                            [machine('M1', rate=rate, modulus=modulus,
                                     initial=initial)])
        pack = {'init': init,
                'watermarks': [watermark('M1', readings=horizon + 1,
                                         mountings=horizon)],
                'readings': [reading('M1', second, values[second])
                             for second in seconds],
                'mountings': mountings}
        segments = []
        previous = (0, initial)
        for second in seconds:
            segments.append((previous[0], second,
                             (values[second] - previous[1]) % modulus))
            previous = (second, values[second])
        return pack, segments, mountings, modulus, rate, horizon

    def test_against_brute_force(self):
        rng = random.Random(20261122)
        checked = slips = ambiguous = 0
        for _trial in range(300):
            pack, segments, mountings, modulus, rate, horizon = self.scenario(rng)
            expected = reference_bounds(modulus, rate, segments, mountings,
                                        0, horizon)
            if expected is None:
                continue  # infeasible input would be a contradiction error
            shift_keys = {key for key, seconds in
                          reference_key_seconds(mountings, 0, horizon).items()
                          if seconds > 0}
            expected = {key: bounds for key, bounds in expected.items()
                        if key in shift_keys}
            try:
                artifact = process_meter(pack)
            except ValueError:
                continue  # contradictory input is rejected, not compared
            checked += 1
            if artifact['new_slips']:
                slips += 1
                self.assertTrue(all(low == high for low, high in
                                    (expected.get(key, [0, 0])
                                     for key in expected)))
                slip = artifact['new_slips'][0]
                got = {(row['edition_id'], row['signature']): row['sheets']
                       for row in slip['machines'][0]['keys']}
                want = {key: low for key, (low, _high) in expected.items()}
                self.assertEqual(got, want)
            else:
                ambiguous += 1
                self.assertEqual(artifact['diagnosis']['kind'], 'ambiguous')
                got = {(row['edition_id'], row['signature']):
                       (row['min_sheets'], row['max_sheets'])
                       for row in artifact['diagnosis']['keys']}
                want = {key: (low, high)
                        for key, (low, high) in expected.items()}
                self.assertEqual(got, want)
        self.assertGreater(checked, 150)
        self.assertGreater(slips, 20)
        self.assertGreater(ambiguous, 20)


def reference_key_seconds(mountings, start, end):
    seconds = {}
    for record in mountings:
        low = max(record['start'], start)
        high = min(record['end'], end)
        if low < high:
            key = (record['edition_id'], record['signature'])
            seconds[key] = seconds.get(key, 0) + high - low
    return seconds


class RandomizedContinuationTests(unittest.TestCase):
    """Two-package continuations reproduce the one-shot result exactly."""

    def test_concatenated_slips_equal_one_shot(self):
        rng = random.Random(20261123)
        batch = proof_batch(edition('E', pages=16, cap=8))
        compared = 0
        for _trial in range(200):
            horizon = rng.randint(6, 10)
            middle = rng.randint(2, horizon - 2)
            modulus = rng.choice((3, 5, 10, 100))
            initial = rng.randrange(modulus)
            mountings = []
            position = 0
            while position < horizon and rng.random() < 0.85:
                length = rng.randint(1, horizon - position)
                if rng.random() < 0.8:
                    mountings.append(mounting('M1', position, position + length,
                                              'E', rng.randint(1, 2)))
                position += length + rng.randint(0, 1)
            mounted_at = set()
            for record in mountings:
                mounted_at.update(range(record['start'], record['end']))
            seconds = sorted(rng.sample(range(1, horizon + 1),
                                        rng.randint(2, min(4, horizon))))
            if seconds[-1] != horizon:
                seconds.append(horizon)
            values = {}
            cumulative = initial
            previous = 0
            for second in seconds:
                cumulative += sum(1 for moment in range(previous, second)
                                  if moment in mounted_at and rng.random() < 0.7)
                values[second] = cumulative % modulus
                previous = second
            init = init_package(batch, [0, middle, horizon],
                                [machine('M1', rate=1, modulus=modulus,
                                         initial=initial)])
            rows = [reading('M1', second, values[second]) for second in seconds]
            one_shot = process_meter({
                'init': init,
                'watermarks': [watermark('M1', readings=horizon + 1,
                                         mountings=horizon)],
                'readings': rows, 'mountings': mountings})
            right = next(second for second in seconds if second >= middle)
            first = process_meter({
                'init': init,
                'watermarks': [watermark('M1', readings=right + 1,
                                         mountings=right)],
                'readings': rows, 'mountings': mountings})
            second_artifact = process_meter(package(
                roundtrip(first['continuation']),
                watermarks=[watermark('M1', readings=horizon + 1,
                                      mountings=horizon)]))
            self.assertEqual(first['new_slips'] + second_artifact['new_slips'],
                             one_shot['new_slips'])
            self.assertEqual(second_artifact['diagnosis'], one_shot['diagnosis'])
            self.assertEqual(without_sequence(second_artifact['continuation']),
                             without_sequence(one_shot['continuation']))
            compared += 1
        self.assertGreater(compared, 100)


# ------------------------------------------------------------------------ CLI

class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__file__).resolve().parent / '_tmp_meter_cli'
        self.tmp.mkdir(exist_ok=True)

    def tearDown(self):
        for child in self.tmp.iterdir():
            child.unlink()
        self.tmp.rmdir()

    def _run(self, source, output):
        return subprocess.run(
            [sys.executable, '-m', 'signature_press.cli', '--press-meter',
             '--source', str(source), '--output', str(output)],
            capture_output=True, text=True)

    def test_fixture_then_continuation_and_no_overwrite(self):
        fixture = Path(__file__).resolve().parents[1] / 'fixtures' / 'press-meter.json'
        first_out = self.tmp / 'meter-1.json'
        first = self._run(fixture, first_out)
        self.assertEqual(first.returncode, 0, first.stderr)
        artifact = json.loads(first_out.read_text())
        self.assertEqual(artifact['schema_version'], 7)
        self.assertEqual(artifact['diagnosis']['kind'], 'incomplete')
        self.assertEqual(artifact['new_slips'], [])
        summary = json.loads(first.stdout)
        self.assertEqual(summary['diagnosis'], 'incomplete')

        # Continuation package: only the previous artifact's state plus new data.
        continuation = artifact['continuation']
        second_pack = self.tmp / 'pkg-2.json'
        second_pack.write_text(json.dumps(
            package(continuation,
                    watermarks=[watermark('press-1', readings=16)],
                    readings=[reading('press-1', 5, 103)])))
        second_out = self.tmp / 'meter-2.json'
        second = self._run(second_pack, second_out)
        self.assertEqual(second.returncode, 0, second.stderr)
        artifact2 = json.loads(second_out.read_text())
        self.assertEqual(len(artifact2['new_slips']), 1)
        self.assertEqual(artifact2['new_slips'][0]['machines'][0]['keys'],
                         [{'edition_id': 'edition-ji', 'signature': 1,
                           'sheets': 6}])

        # Existing outputs are never overwritten.
        before = second_out.read_bytes()
        again = self._run(second_pack, second_out)
        self.assertEqual(again.returncode, 2)
        self.assertEqual(second_out.read_bytes(), before)

    def test_invalid_input_exits_two_and_publishes_nothing(self):
        source = self.tmp / 'bad.json'
        output = self.tmp / 'bad-out.json'
        source.write_text(json.dumps({'init': {'proof_batch': {},
                                               'shift_boundaries': [0, 10],
                                               'machines': []}}))
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
            [sys.executable, '-m', 'signature_press.cli', '--press-meter',
             '--proof-batch', '--source', str(source), '--output', str(output)],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
