import itertools
import json
import random
import subprocess
import sys
import unittest
from pathlib import Path

from signature_press.audit import (_allowed_edges, _deficiency, _invert,
                                   _max_matching, _strong_components,
                                   process_audit)
from signature_press.batch import process_batch

GEOMETRY = {'sheet_width_um': 440000, 'sheet_height_um': 320000, 'leaf_width_um': 200000,
            'leaf_height_um': 280000, 'margin_um': 10000, 'gutter_um': 10000, 'thickness_um': 120}


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


def ref(eid, signature, sheet):
    return {'edition_id': eid, 'signature': signature, 'sheet': sheet}


def sheet(pid, targets, front, back=None):
    return {'physical_id': pid, 'possible_targets': list(targets),
            'front_from': front, 'back_from': back if back is not None else front}


def audit(batch, observed):
    return process_audit({'proof_batch': batch, 'observed_sheets': observed})


def assignment_dict(evidence):
    return {pair['physical_id']: pair['target'] for pair in evidence['assignment']}


def editions_by_id(report):
    return {entry['id']: entry for entry in report['editions']}


def positions_by_ref(report):
    return {(p['edition_id'], p['signature'], p['sheet']): p for p in report['positions']}


# ---------------------------------------------------------- scenario builders

A = ref('jia', 1, 1)
B = ref('jia', 1, 2)
Y = ref('yi', 1, 1)
Z = ref('yi', 1, 2)


def swap_batch():
    """Two books of one 8-page signature each: positions a,b (jia) and y,z (yi)."""
    return {'editions': [edition('jia', [{'id': 'A', 'pages': 8}], ['ra'] * 8, cap=8),
                         edition('yi', [{'id': 'B', 'pages': 8}], ['rb'] * 8, cap=8)],
            'legacy_lots': []}


def swap_observed(s_to_y=False):
    """P,Q,R,S printed exactly per a,b,y,z; conveyor allows a few swaps."""
    return [
        sheet('P', [A, Y], A),
        sheet('Q', [B, Y], B),
        sheet('R', [A, B, Z], Y),
        sheet('S', [Y, Z] if s_to_y else [Z], Z),
    ]


class SwapContentionTests(unittest.TestCase):
    """甲 can source correct paper for a and b separately, yet stays defective."""

    def test_defective_book_with_per_position_good_explanations(self):
        report = audit(swap_batch(), swap_observed())
        self.assertEqual(report['schema_version'], 3)
        self.assertEqual(report['logistics'], 'explainable')
        jia, yi = editions_by_id(report)['jia'], editions_by_id(report)['yi']
        self.assertEqual(jia['status'], 'defective')
        self.assertEqual(yi['status'], 'defective')
        # a and b each truly receive their correct sheet in some explanation.
        positions = positions_by_ref(report)
        self.assertEqual(positions[('jia', 1, 1)]['arrivals'],
                         [{'physical_id': 'P', 'qualified': True},
                          {'physical_id': 'R', 'qualified': False}])
        self.assertEqual(positions[('jia', 1, 2)]['arrivals'],
                         [{'physical_id': 'Q', 'qualified': True},
                          {'physical_id': 'R', 'qualified': False}])
        self.assertEqual(positions[('yi', 1, 1)]['arrivals'],
                         [{'physical_id': 'P', 'qualified': False},
                          {'physical_id': 'Q', 'qualified': False}])
        # R declared z on the conveyor, but z is always taken by S: the raw
        # candidate must not be copied into the reachable arrivals.
        self.assertEqual(positions[('yi', 1, 2)]['arrivals'],
                         [{'physical_id': 'S', 'qualified': True}])
        # Deficiency witness: a, b and y compete for P and Q alone.
        self.assertEqual(jia['deficiency'], {'targets': [A, B, Y], 'sheets': ['P', 'Q']})
        self.assertEqual(yi['deficiency'], {'targets': [Y], 'sheets': []})
        # No qualifying explanation exists for jia, so no qualified evidence.
        self.assertNotIn('qualified_evidence', jia)
        # The unqualified evidence is a complete one-to-one assignment and
        # pinpoints the mismatching sheet, position and side.
        evidence = jia['unqualified_evidence']
        self.assertEqual(assignment_dict(evidence), {'P': Y, 'Q': B, 'R': A, 'S': Z})
        mismatch = evidence['mismatch']
        self.assertEqual(mismatch['physical_id'], 'R')
        self.assertEqual(mismatch['target'], A)
        self.assertEqual(mismatch['side'], 'front')

    def test_single_extra_route_turns_defective_into_uncertain(self):
        report = audit(swap_batch(), swap_observed(s_to_y=True))
        jia = editions_by_id(report)['jia']
        self.assertEqual(jia['status'], 'uncertain')
        # The qualifying explanation frees z for R by sending S to y.
        self.assertEqual(assignment_dict(jia['qualified_evidence']),
                         {'P': A, 'Q': B, 'S': Y, 'R': Z})
        # A non-qualifying complete explanation coexists.
        evidence = jia['unqualified_evidence']
        self.assertEqual(assignment_dict(evidence), {'P': Y, 'Q': B, 'R': A, 'S': Z})
        self.assertNotEqual(assignment_dict(evidence),
                            assignment_dict(jia['qualified_evidence']))
        self.assertEqual(evidence['mismatch']['physical_id'], 'R')
        self.assertEqual(evidence['mismatch']['target'], A)
        # R can now truly reach z.
        positions = positions_by_ref(report)
        self.assertEqual(positions[('yi', 1, 2)]['arrivals'],
                         [{'physical_id': 'R', 'qualified': False},
                          {'physical_id': 'S', 'qualified': True}])
        # yi still cannot be assembled correctly: nothing qualified reaches y.
        yi = editions_by_id(report)['yi']
        self.assertEqual(yi['status'], 'defective')
        self.assertEqual(yi['deficiency'], {'targets': [Y], 'sheets': []})


class SplitPlateTests(unittest.TestCase):
    """Sides are judged by content, not by which plate position was cited."""

    def test_same_content_other_plate_passes_other_revision_fails(self):
        # ea is r1 everywhere; eb differs only in the revision of pages 2..3,
        # which sit on the back side. Fronts are identical across positions.
        batch = {'editions': [edition('ea', [{'id': 'S', 'pages': 4}], ['r1'] * 4, cap=4),
                              edition('eb', [{'id': 'S', 'pages': 4}],
                                      ['r1', 'r2', 'r2', 'r1'], cap=4)],
                 'legacy_lots': []}
        a1, b1 = ref('ea', 1, 1), ref('eb', 1, 1)
        observed = [sheet('X', [a1, b1], a1),
                    sheet('Y', [a1, b1], b1)]  # Y carries eb's plates entirely
        report = audit(batch, observed)
        ea, eb = editions_by_id(report)['ea'], editions_by_id(report)['eb']
        self.assertEqual(ea['status'], 'uncertain')
        self.assertEqual(eb['status'], 'uncertain')
        # Y at a1: the front passes although printed from the other position's
        # plate (identical content); the back fails on the revision change.
        mismatch = ea['unqualified_evidence']['mismatch']
        self.assertEqual(mismatch['physical_id'], 'Y')
        self.assertEqual(mismatch['target'], a1)
        self.assertEqual(mismatch['side'], 'back')
        self.assertEqual(mismatch['slot_index'], 0)
        self.assertEqual(mismatch['expected']['logical_page'],
                         {'section_id': 'S', 'page': 2, 'revision': 'r1'})
        self.assertEqual(mismatch['actual']['logical_page'],
                         {'section_id': 'S', 'page': 2, 'revision': 'r2'})
        self.assertEqual(mismatch['expected']['placement'],
                         mismatch['actual']['placement'])
        self.assertEqual(assignment_dict(ea['unqualified_evidence']),
                         {'X': b1, 'Y': a1})
        self.assertEqual(assignment_dict(ea['qualified_evidence']),
                         {'X': a1, 'Y': b1})

    def test_distinct_plate_references_with_identical_sides_verify(self):
        batch = {'editions': [edition('ea', [{'id': 'S', 'pages': 4}], ['r1'] * 4, cap=4),
                              edition('eb', [{'id': 'S', 'pages': 4}], ['r1'] * 4, cap=4)],
                 'legacy_lots': []}
        a1, b1 = ref('ea', 1, 1), ref('eb', 1, 1)
        # Each sheet is printed from the other book's plates; content matches.
        observed = [sheet('X', [a1, b1], b1),
                    sheet('Y', [a1, b1], a1)]
        report = audit(batch, observed)
        ea, eb = editions_by_id(report)['ea'], editions_by_id(report)['eb']
        self.assertEqual(ea['status'], 'verified')
        self.assertEqual(eb['status'], 'verified')
        self.assertNotIn('unqualified_evidence', ea)
        positions = positions_by_ref(report)
        self.assertEqual(positions[('ea', 1, 1)]['arrivals'],
                         [{'physical_id': 'X', 'qualified': True},
                          {'physical_id': 'Y', 'qualified': True}])

    def test_occupancy_elsewhere_excludes_wrong_paper_candidate(self):
        batch = {'editions': [edition('ea', [{'id': 'S', 'pages': 4}], ['r1'] * 4, cap=4),
                              edition('eb', [{'id': 'S', 'pages': 4}], ['r2'] * 4, cap=4)],
                 'legacy_lots': []}
        a1, b1 = ref('ea', 1, 1), ref('eb', 1, 1)
        # Z declares a1 on the conveyor but would be wrong there; X can only
        # take a1, so no complete explanation ever delivers Z to a1.
        observed = [sheet('X', [a1], a1),
                    sheet('Z', [b1, a1], b1)]
        report = audit(batch, observed)
        ea, eb = editions_by_id(report)['ea'], editions_by_id(report)['eb']
        self.assertEqual(ea['status'], 'verified')
        self.assertEqual(eb['status'], 'verified')
        positions = positions_by_ref(report)
        self.assertEqual(positions[('ea', 1, 1)]['arrivals'],
                         [{'physical_id': 'X', 'qualified': True}])
        self.assertEqual(positions[('eb', 1, 1)]['arrivals'],
                         [{'physical_id': 'Z', 'qualified': True}])
        self.assertEqual(assignment_dict(ea['qualified_evidence']),
                         {'X': a1, 'Z': b1})


# ------------------------------------------------------- mixed retained batch

def mixed_batch():
    """Jia recuts 1/8/3 and reuses the retained middle signature; Yi prints."""
    return {'editions': [edition('jia', [{'id': 'P', 'pages': 1}, {'id': 'A', 'pages': 8},
                                         {'id': 'Q', 'pages': 3}], ['r1'] * 12),
                         edition('yi', [{'id': 'A', 'pages': 8}],
                                 ['r1', 'r1', 'r1', 'rX', 'r1', 'r1', 'r1', 'r1'])],
            'legacy_lots': [lot('lot-a', [{'id': 'A', 'pages': 8}], ['r1'] * 8,
                                [stock('s-a', 1)])]}


M1 = ref('jia', 1, 1)   # fresh 1-page segment (padded)
M2 = ref('jia', 2, 1)   # retained signature: never a valid reference
M3 = ref('jia', 3, 1)   # fresh 3-page segment (padded)
M4 = ref('yi', 1, 1)
M5 = ref('yi', 1, 2)


def mixed_observed():
    return [sheet('w1', [M1], M1),
            sheet('w2', [M3], M3),
            sheet('w3', [M4], M4),
            sheet('w4', [M5], M5)]


class MixedRetainedTests(unittest.TestCase):
    def test_mixed_work_order_audits_only_fresh_positions(self):
        batch = mixed_batch()
        # The work order itself is unchanged by the audit: jia reuses s-a.
        order = process_batch(batch)
        self.assertEqual([(s['source'], s.get('stock_id'))
                          for s in order['editions'][0]['signatures']],
                         [('fresh_print', None), ('retained', 's-a'),
                          ('fresh_print', None)])
        report = audit(batch, mixed_observed())
        self.assertEqual(report['logistics'], 'explainable')
        self.assertEqual(sorted(positions_by_ref(report)),
                         [('jia', 1, 1), ('jia', 3, 1), ('yi', 1, 1), ('yi', 1, 2)])
        editions = editions_by_id(report)
        self.assertEqual(editions['jia']['status'], 'verified')
        self.assertEqual(editions['yi']['status'], 'verified')
        self.assertEqual(assignment_dict(editions['jia']['qualified_evidence']),
                         {'w1': M1, 'w2': M3, 'w3': M4, 'w4': M5})

    def test_retained_and_unknown_references_are_input_errors(self):
        base = mixed_observed()
        cases = []
        retained_candidate = json.loads(json.dumps(base))
        retained_candidate[0]['possible_targets'] = [M2]
        cases.append(retained_candidate)
        retained_plate = json.loads(json.dumps(base))
        retained_plate[0]['front_from'] = M2
        cases.append(retained_plate)
        unknown_edition = json.loads(json.dumps(base))
        unknown_edition[0]['possible_targets'] = [ref('bu', 1, 1)]
        cases.append(unknown_edition)
        unknown_signature = json.loads(json.dumps(base))
        unknown_signature[0]['possible_targets'] = [ref('jia', 9, 1)]
        cases.append(unknown_signature)
        unknown_sheet = json.loads(json.dumps(base))
        unknown_sheet[0]['possible_targets'] = [ref('yi', 1, 3)]
        cases.append(unknown_sheet)
        for observed in cases:
            with self.assertRaises(ValueError):
                audit(mixed_batch(), observed)

    def test_count_structure_and_identity_errors(self):
        batch = mixed_batch()
        good = mixed_observed()
        cases = [
            good[:3],                                   # too few records
            good + [sheet('w5', [M1], M1)],             # too many records
            [dict(row, physical_id='w1') for row in good],  # duplicate ids
            [sheet('w1', [], M1)] + good[1:],           # empty candidates
            [sheet('w1', [M1] * 2, M1)] + good[1:],     # duplicate candidate
            [sheet('w1', [M1] * 17, M1)] + good[1:],    # more than 16 candidates
            [sheet('bad id!', [M1], M1)] + good[1:],    # identifier rules
            [{k: v for k, v in good[0].items() if k != 'back_from'}] + good[1:],
            [dict(good[0], extra=1)] + good[1:],        # unexpected field
            [sheet('w1', [{'edition_id': 'jia', 'signature': '1', 'sheet': 1}], M1)]
            + good[1:],                                 # non-integer reference
        ]
        for observed in cases:
            with self.assertRaises(ValueError):
                audit(batch, observed)
        # 17 distinct candidates would be legal length-wise but cannot exist
        # here (only 4 fresh positions); duplicates are caught first.
        with self.assertRaises(ValueError):
            audit(batch, 'not-a-list')
        with self.assertRaises(ValueError):
            process_audit({'proof_batch': batch})
        with self.assertRaises(ValueError):
            process_audit({'proof_batch': batch, 'observed_sheets': good, 'x': 1})


class AllRetainedTests(unittest.TestCase):
    def test_all_retained_batch_has_empty_assignment(self):
        batch = {'editions': [edition('yi', [{'id': 'A', 'pages': 8}], ['r1'] * 8)],
                 'legacy_lots': [lot('lot-a', [{'id': 'A', 'pages': 8}], ['r1'] * 8,
                                     [stock('s-a', 1)])]}
        report = audit(batch, [])
        self.assertEqual(report['logistics'], 'explainable')
        self.assertEqual(report['positions'], [])
        yi = editions_by_id(report)['yi']
        self.assertEqual(yi['status'], 'verified')
        self.assertEqual(yi['qualified_evidence'], {'assignment': []})
        self.assertEqual(report['summary']['physical_sheets'], 0)
        # Any record at all contradicts the zero fresh sheets of the order.
        with self.assertRaises(ValueError):
            audit(batch, [sheet('w1', [ref('yi', 1, 1)], ref('yi', 1, 1))])

    def test_book_without_fresh_paper_is_verified(self):
        batch = {'editions': [edition('jia', [{'id': 'A', 'pages': 12}], ['r1'] * 12),
                              edition('yi', [{'id': 'A', 'pages': 8}], ['r1'] * 8)],
                 'legacy_lots': [lot('lot-a', [{'id': 'A', 'pages': 8}], ['r1'] * 8,
                                     [stock('s-a', 1)])]}
        t1, t2, t3 = ref('jia', 1, 1), ref('jia', 1, 2), ref('jia', 1, 3)
        observed = [sheet('p1', [t1], t1), sheet('p2', [t2], t2), sheet('p3', [t3], t3)]
        report = audit(batch, observed)
        editions = editions_by_id(report)
        self.assertEqual(editions['jia']['status'], 'verified')
        # yi consumes the retained piece, has no fresh position and is
        # verified while logistics is explainable.
        self.assertEqual(editions['yi']['status'], 'verified')
        self.assertEqual(assignment_dict(editions['yi']['qualified_evidence']),
                         {'p1': t1, 'p2': t2, 'p3': t3})


class LogisticsConflictTests(unittest.TestCase):
    def test_gap_diagnosis_uses_unfiltered_candidates(self):
        batch = {'editions': [edition('e', [{'id': 'S', 'pages': 16}], ['r'] * 16, cap=16)],
                 'legacy_lots': []}
        t = [ref('e', 1, i) for i in range(1, 5)]
        observed = [sheet('s1', [t[0]], t[0]),
                    sheet('s2', [t[0]], t[0]),
                    sheet('s3', [t[0], t[1], t[2], t[3]], t[2]),
                    sheet('s4', [t[1], t[2], t[3]], t[3])]
        report = audit(batch, observed)
        self.assertEqual(report['logistics'], 'conflict')
        # No quality conclusions are published for any edition.
        self.assertNotIn('editions', report)
        self.assertNotIn('positions', report)
        # t2, t3, t4 can only be reached by s3 and s4: 2 sheets for 3 targets.
        self.assertEqual(report['conflict']['deficiency'],
                         {'targets': t[1:], 'sheets': ['s3', 's4']})
        self.assertEqual(report['summary'],
                         {'logistics': 'conflict', 'physical_sheets': 4,
                          'target_positions': 4})

    def test_conflict_is_distinct_from_illegal_reference(self):
        batch = {'editions': [edition('e', [{'id': 'S', 'pages': 16}], ['r'] * 16, cap=16)],
                 'legacy_lots': []}
        t = [ref('e', 1, i) for i in range(1, 5)]
        legal = [sheet('s1', [t[0]], t[0]), sheet('s2', [t[0]], t[0]),
                 sheet('s3', [t[0]], t[0]), sheet('s4', [t[0]], t[0])]
        report = audit(batch, legal)  # legal but contradictory: exit-0 diagnosis
        self.assertEqual(report['logistics'], 'conflict')
        illegal = [sheet('s1', [ref('e', 2, 1)], t[0])] + legal[1:]
        with self.assertRaises(ValueError):
            audit(batch, illegal)


class PermutationStabilityTests(unittest.TestCase):
    def test_row_and_candidate_order_do_not_change_the_report(self):
        batch = swap_batch()
        observed = swap_observed(s_to_y=True)
        baseline = audit(batch, observed)
        permuted = [dict(row, possible_targets=list(reversed(row['possible_targets'])))
                    for row in reversed(observed)]
        shuffled = audit(batch, permuted)
        self.assertEqual(baseline, shuffled)
        self.assertEqual(json.dumps(baseline, sort_keys=True),
                         json.dumps(shuffled, sort_keys=True))

    def test_mixed_batch_permutation_stability(self):
        batch = mixed_batch()
        observed = mixed_observed()
        baseline = audit(batch, observed)
        permuted = [dict(row, possible_targets=list(reversed(row['possible_targets'])))
                    for row in [observed[2], observed[0], observed[3], observed[1]]]
        self.assertEqual(baseline, audit(batch, permuted))


class SwapGroupScalingTests(unittest.TestCase):
    def test_forty_independent_swap_groups(self):
        groups = 40
        batch = {'editions': [edition('big', [{'id': 'S', 'pages': groups * 8}],
                                      ['rev'] * (groups * 8), cap=8)],
                 'legacy_lots': []}
        observed = []
        for g in range(1, groups + 1):
            t1, t2 = ref('big', g, 1), ref('big', g, 2)
            observed.append(sheet('p%03d' % (2 * g - 1), [t1, t2], t1))
            observed.append(sheet('p%03d' % (2 * g), [t1, t2], t2))
        report = audit(batch, observed)
        big = editions_by_id(report)['big']
        self.assertEqual(big['status'], 'uncertain')
        # Every position keeps both truly reachable sheets of its group.
        positions = positions_by_ref(report)
        self.assertEqual(len(positions), groups * 2)
        for g in range(1, groups + 1):
            for s in (1, 2):
                arrivals = positions[('big', g, s)]['arrivals']
                self.assertEqual([a['physical_id'] for a in arrivals],
                                 ['p%03d' % (2 * g - 1), 'p%03d' % (2 * g)])
                self.assertEqual([a['qualified'] for a in arrivals],
                                 [s == 1, s == 2])
        # Both directions of evidence exist: identity qualifies, one swap
        # inside the first group does not.
        identity = {'p%03d' % (2 * g - 1): ref('big', g, 1) for g in range(1, groups + 1)}
        identity.update({'p%03d' % (2 * g): ref('big', g, 2) for g in range(1, groups + 1)})
        self.assertEqual(assignment_dict(big['qualified_evidence']), identity)
        swapped = dict(identity)
        swapped['p001'], swapped['p002'] = ref('big', 1, 2), ref('big', 1, 1)
        self.assertEqual(assignment_dict(big['unqualified_evidence']), swapped)
        mismatch = big['unqualified_evidence']['mismatch']
        self.assertEqual(mismatch['physical_id'], 'p002')
        self.assertEqual(mismatch['target'], ref('big', 1, 1))
        self.assertEqual(mismatch['side'], 'front')


class MatchingKernelTests(unittest.TestCase):
    """Cross-check the polynomial matching kernel against brute force."""

    def random_graphs(self, trials, seed):
        rng = random.Random(seed)
        for _ in range(trials):
            n = rng.randint(1, 5)
            adj = []
            for _ in range(n):
                k = rng.randint(1, min(3, n))
                adj.append(sorted(rng.sample(range(n), k)))
            yield n, adj

    def test_allowed_edges_match_brute_force_enumeration(self):
        for n, adj in self.random_graphs(300, 20261122):
            match_t, match_s = _max_matching(n, adj)
            if -1 in match_t:
                continue
            allowed = _allowed_edges(n, adj, match_t, match_s,
                                     [True] * n, [True] * n)
            brute = set()
            for perm in itertools.permutations(range(n)):
                if all(perm[s] in adj[s] for s in range(n)):
                    brute.update((s, perm[s]) for s in range(n))
            self.assertEqual(allowed, brute)

    def test_deficiency_witness_satisfies_hall_deficit(self):
        for n, adj in self.random_graphs(300, 19540712):
            match_t, match_s = _max_matching(n, adj)
            if -1 not in match_t:
                continue
            inv = _invert(n, adj)
            z_targets, z_sheets = _deficiency(n, inv, match_t, match_s)
            neighborhood = {s for t in z_targets for s in inv[t]}
            self.assertEqual(set(z_sheets), neighborhood)
            self.assertLess(len(z_sheets), len(z_targets))

    def test_strong_components_match_naive_reachability(self):
        rng = random.Random(186)
        for _ in range(200):
            n = rng.randint(1, 9)
            successors = [[] for _ in range(n)]
            for node in range(n):
                for other in range(n):
                    if rng.random() < 0.25:
                        successors[node].append(other)
            component = _strong_components(successors)
            reach = [[False] * n for _ in range(n)]
            for start in range(n):
                stack = [start]
                while stack:
                    node = stack.pop()
                    for nxt in successors[node]:
                        if not reach[start][nxt]:
                            reach[start][nxt] = True
                            stack.append(nxt)
            for i in range(n):
                for j in range(n):
                    self.assertEqual(component[i] == component[j],
                                     i == j or (reach[i][j] and reach[j][i]))


class CliAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__file__).resolve().parent / '_tmp_audit'
        self.tmp.mkdir(exist_ok=True)

    def tearDown(self):
        for child in self.tmp.iterdir():
            child.unlink()
        self.tmp.rmdir()

    def _run(self, source_path, output_path, *flags):
        args = [sys.executable, '-m', 'signature_press.cli', '--source', str(source_path),
                '--output', str(output_path), *flags]
        return subprocess.run(args, capture_output=True, text=True)

    def test_audit_cli_publishes_and_never_overwrites(self):
        source = self.tmp / 'audit.json'
        output = self.tmp / 'audit-out.json'
        source.write_text(json.dumps({'proof_batch': swap_batch(),
                                      'observed_sheets': swap_observed(s_to_y=True)}))
        first = self._run(source, output, '--binding-audit')
        self.assertEqual(first.returncode, 0, first.stderr)
        summary = json.loads(first.stdout)
        self.assertEqual(summary['uncertain'], 1)
        self.assertEqual(summary['defective'], 1)
        report = json.loads(output.read_text())
        self.assertEqual(report['schema_version'], 3)
        # Existing target: exit 2, bytes untouched, no temp residue.
        before = output.read_bytes()
        siblings = sorted(p.name for p in self.tmp.iterdir())
        again = self._run(source, output, '--binding-audit')
        self.assertEqual(again.returncode, 2)
        self.assertEqual(output.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), siblings)

    def test_conflict_diagnosis_exits_zero_and_invalid_input_exits_two(self):
        batch = {'editions': [edition('e', [{'id': 'S', 'pages': 16}], ['r'] * 16, cap=16)],
                 'legacy_lots': []}
        t = [ref('e', 1, i) for i in range(1, 5)]
        conflict = {'proof_batch': batch,
                    'observed_sheets': [sheet('s1', [t[0]], t[0]), sheet('s2', [t[0]], t[0]),
                                        sheet('s3', [t[0]], t[0]), sheet('s4', [t[0]], t[0])]}
        source = self.tmp / 'conflict.json'
        output = self.tmp / 'conflict-out.json'
        source.write_text(json.dumps(conflict))
        ok = self._run(source, output, '--binding-audit')
        self.assertEqual(ok.returncode, 0, ok.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report['logistics'], 'conflict')
        self.assertNotIn('editions', report)
        # Invalid input: exit 2 and no file is created.
        bad_source = self.tmp / 'bad.json'
        bad_output = self.tmp / 'bad-out.json'
        bad_source.write_text(json.dumps({'proof_batch': batch,
                                          'observed_sheets': conflict['observed_sheets'][:3]}))
        failed = self._run(bad_source, bad_output, '--binding-audit')
        self.assertEqual(failed.returncode, 2)
        self.assertFalse(bad_output.exists())
        self.assertFalse(any(p.name.startswith('.report-') for p in self.tmp.iterdir()))

    def test_modes_are_mutually_exclusive(self):
        source = self.tmp / 'any.json'
        output = self.tmp / 'any-out.json'
        source.write_text(json.dumps({'proof_batch': swap_batch(), 'observed_sheets': []}))
        both = self._run(source, output, '--proof-batch', '--binding-audit')
        self.assertEqual(both.returncode, 2)
        self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
