import copy
import itertools
import json
import subprocess
import sys
import unittest
from pathlib import Path

from signature_press.batch import process_batch
from signature_press.rollout import process_rollout

GEOMETRY = {'sheet_width_um': 440000, 'sheet_height_um': 320000, 'leaf_width_um': 200000,
            'leaf_height_um': 280000, 'margin_um': 10000, 'gutter_um': 10000,
            'thickness_um': 120}


def proof_batch(eid, sections, cap=8, binding='left', legacy=None):
    """Build a v2 input; sections is [(section_id, pages, revision), ...]."""
    total = sum(pages for _, pages, _ in sections)
    return {
        'editions': [{'id': eid,
                      'source': {'sections': [{'id': sid, 'pages': pages}
                                              for sid, pages, _ in sections],
                                 'signature_pages': cap, 'binding': binding,
                                 'geometry': GEOMETRY},
                      'revisions': [rev for _, pages, rev in sections
                                    for rev in [rev] * pages]}],
        'legacy_lots': legacy or [],
    }


def snapshot(eid, sections, asset_for_identity, legacy=None):
    """Snapshot with an explicit mapping of every non-blank identity."""
    pb = proof_batch(eid, sections, legacy=legacy)
    rows = [{'section_id': sid, 'page': page, 'revision': rev,
             'asset_id': asset_for_identity[(sid, page, rev)]}
            for sid, pages, rev in sections for page in range(1, pages + 1)]
    return {'proof_batch': pb, 'page_assets': rows}


def asset(asset_id, size, full=True, patches=()):
    return {'asset_id': asset_id, 'size_bytes': size, 'full_available': full,
            'patches': [{'base_asset_id': base, 'wire_bytes': wire}
                        for base, wire in patches]}


def make_input(capacity, batches, assets):
    return {'capacity_bytes': capacity, 'batches': batches, 'assets': assets}


def batch_record(bid, current, nxt):
    return {'batch_id': bid, 'current': current, 'next': nxt}


def snap_single(bid, section_id, pages, rev, asset_id):
    return snapshot(bid, [(section_id, pages, rev)],
                    {(section_id, p, rev): asset_id for p in range(1, pages + 1)})


# ----------------------------------------------------------------- acceptance

class AcceptanceScenarioTests(unittest.TestCase):
    """The exact two-batch scenario described in the task contract."""

    def assets(self, p_full):
        return [
            asset('x', 300), asset('a', 100), asset('b', 300),
            asset('p', 200, full=p_full, patches=[('b', 50)]),
            asset('q', 300),
        ]

    def batches(self):
        cur_a = snap_single('A-e', 'SA', 4, 'r1', 'x')
        # A's current order actually depends on both x and a.
        cur_a['page_assets'][2]['asset_id'] = 'a'
        cur_a['page_assets'][3]['asset_id'] = 'a'
        cur_b = snap_single('B-e', 'SB', 4, 'r1', 'x')
        cur_b['page_assets'][2]['asset_id'] = 'b'
        cur_b['page_assets'][3]['asset_id'] = 'b'
        return [
            batch_record('A', cur_a, snap_single('PA-e', 'PA', 4, 'r2', 'p')),
            batch_record('B', cur_b, snap_single('QB-e', 'QB', 4, 'r2', 'q')),
        ]

    def test_capacity_1000_forces_b_then_a_full_only_for_p(self):
        report = process_rollout(make_input(1000, self.batches(), self.assets(True)))
        self.assertEqual(report['status'], 'complete')
        steps = report['steps']
        self.assertEqual([s['batch_id'] for s in steps], ['B', 'A'])
        self.assertEqual([s['pre_switch_peak_bytes'] for s in steps], [1000, 900])
        self.assertEqual(report['total_download_bytes'], 500)
        # B first: q full (300); b is reclaimed, x stays.
        first, second = steps
        self.assertEqual(first['download_bytes'], 300)
        self.assertEqual(first['acquisitions'][0]['acquisition'], 'full')
        self.assertEqual([r['asset_id'] for r in first['reclaimed_assets']], ['b'])
        self.assertNotIn('b', [r['asset_id'] for r in first['reclaimed_assets']]
                         and [x['asset_id'] for x in first['retained_assets']])
        # p cannot use the b-based patch (b reclaimed): full package only.
        self.assertEqual(second['acquisitions'],
                         [{'asset_id': 'p', 'size_bytes': 200, 'acquisition': 'full',
                           'download_bytes': 200}])
        self.assertEqual(second['download_bytes'], 200)
        self.assertEqual(report['initial_occupancy_bytes'], 700)
        self.assertEqual(report['final_occupancy_bytes'], 500)

    def test_capacity_1100_prefers_cheaper_a_then_b_route(self):
        report = process_rollout(make_input(1100, self.batches(), self.assets(True)))
        self.assertEqual(report['status'], 'complete')
        steps = report['steps']
        # Even though B-first has lower peaks, minimum downloads wins.
        self.assertEqual([s['batch_id'] for s in steps], ['A', 'B'])
        first = steps[0]
        self.assertEqual(first['acquisitions'],
                         [{'asset_id': 'p', 'size_bytes': 200, 'acquisition': 'patch',
                           'base_asset_id': 'b', 'download_bytes': 50}])
        self.assertEqual(report['total_download_bytes'], 350)
        # A's switch must not reclaim x or b which B still uses.
        reclaimed = {r['asset_id'] for r in first['reclaimed_assets']}
        self.assertNotIn('x', reclaimed)
        self.assertNotIn('b', reclaimed)
        self.assertIn('a', reclaimed)
        self.assertEqual(first['post_reclaim_occupancy_bytes'], 800)

    def test_capacity_1000_without_p_full_is_blocked_with_prefix_a(self):
        report = process_rollout(make_input(1000, self.batches(), self.assets(False)))
        self.assertEqual(report['status'], 'blocked')
        self.assertEqual(report['reachable_prefix'], ['A'])
        blockers = {b['batch_id']: b for b in report['remaining_blockers']}
        self.assertEqual(set(blockers), {'B'})
        self.assertEqual(blockers['B']['peak_bytes'], 1100)
        self.assertTrue(blockers['B']['over_capacity'])
        self.assertEqual(blockers['B']['unavailable_targets'], [])

    def test_blocker_reports_missing_source_and_over_capacity_together(self):
        # After switching A, B needs q (no source at all) and the hypothetical
        # footprint with q present exceeds the capacity.
        assets = [a for a in self.assets(False) if a['asset_id'] != 'q']
        assets.append(asset('q', 300, full=False))
        report = process_rollout(make_input(1000, self.batches(), assets))
        self.assertEqual(report['status'], 'blocked')
        self.assertEqual(report['reachable_prefix'], ['A'])
        blocker = report['remaining_blockers'][0]
        self.assertEqual(blocker['batch_id'], 'B')
        self.assertEqual(blocker['unavailable_targets'], ['q'])
        self.assertTrue(blocker['over_capacity'])
        self.assertEqual(blocker['peak_bytes'], 1100)


# ------------------------------------------------------------- work-order semantics

class WorkOrderSemanticsTests(unittest.TestCase):
    def test_two_editions_sharing_asset_switch_in_one_batch_step(self):
        sections = [('S1', 4, 'r1'), ('S2', 4, 'r1')]
        cur = snapshot('cur', sections,
                       {(s, p, 'r1'): 'shared-old'
                        for s, n, _ in sections for p in range(1, n + 1)})
        next_sections = [('S1', 4, 'r2'), ('S2', 4, 'r2')]
        nxt = snapshot('nxt', next_sections,
                       {(s, p, 'r2'): 'shared-new'
                        for s, n, _ in next_sections for p in range(1, n + 1)})
        batches = [batch_record('B1', cur, nxt)]
        assets = [asset('shared-old', 100), asset('shared-new', 400)]
        report = process_rollout(make_input(1000, batches, assets))
        self.assertEqual(report['status'], 'complete')
        req = report['snapshots'][0]['next']['requirements']
        # One dedup'd asset even though two editions print eight pages.
        self.assertEqual([r['asset_id'] for r in req], ['shared-new'])
        pages = req[0]['logical_pages']
        self.assertEqual(len(pages), 8)
        self.assertEqual(report['steps'][0]['download_bytes'], 400)

    def test_new_target_already_resident_via_other_online_order_is_free(self):
        cur_a = snap_single('A-e', 'SA', 4, 'r1', 'x')
        cur_b = snap_single('B-e', 'SB', 4, 'r1', 'p')
        nxt_a = snap_single('PA-e', 'PA', 4, 'r2', 'p')
        nxt_b = snap_single('PB-e', 'PB', 4, 'r2', 'z')
        batches = [batch_record('A', cur_a, nxt_a),
                   batch_record('B', cur_b, nxt_b)]
        assets = [asset('x', 100), asset('p', 200), asset('z', 300)]
        report = process_rollout(make_input(1000, batches, assets))
        self.assertEqual(report['status'], 'complete')
        first = report['steps'][0]
        # p is already resident for B's still-online current order.
        self.assertEqual(first['retained_assets'],
                         [{'asset_id': 'p', 'size_bytes': 200}])
        self.assertEqual(first['acquisitions'], [])
        self.assertEqual(first['download_bytes'], 0)

    def test_retained_signatures_and_blanks_never_enter_asset_needs(self):
        legacy = {
            'id': 'lot-1',
            'source': {'sections': [{'id': 'C', 'pages': 4}],
                       'signature_pages': 8, 'binding': 'left', 'geometry': GEOMETRY},
            'revisions': ['r1'] * 4,
            'retained': [{'stock_id': 'stock-1', 'signature': 1}],
        }
        # The fresh order prints four pages; its all-blank back side carries no
        # digital material, so only the four front identities are mapped.
        cur_pb = proof_batch('cur', [('C', 4, 'r1')])
        cur = {'proof_batch': cur_pb,
               'page_assets': [{'section_id': 'C', 'page': p, 'revision': 'r1',
                                'asset_id': 'old-only'} for p in range(1, 5)]}
        # The next order reuses the retained legacy signature entirely, so it
        # needs no digital assets at all.
        nxt_pb = proof_batch('nxt', [('C', 4, 'r1')], legacy=[legacy])
        nxt = {'proof_batch': nxt_pb, 'page_assets': []}
        planned = process_batch(nxt_pb)
        self.assertEqual(planned['editions'][0]['signatures'][0]['source'], 'retained')
        batches = [batch_record('B1', cur, nxt)]
        assets = [asset('old-only', 100), asset('new-file', 200)]
        report = process_rollout(make_input(1000, batches, assets))
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['snapshots'][0]['next']['requirements'], [])
        # Only the four current identities map to one dedup'd asset.
        self.assertEqual(
            [r['asset_id'] for r in report['snapshots'][0]['current']['requirements']],
            ['old-only'])

    def test_blank_slots_are_not_mapped_and_padding_pages_excluded(self):
        # One logical page padded with three blanks: exactly one page asset.
        cur_pb = proof_batch('cur', [('C', 1, 'r1')])
        cur = {'proof_batch': cur_pb,
               'page_assets': [{'section_id': 'C', 'page': 1, 'revision': 'r1',
                                'asset_id': 'old-only'}]}
        nxt = snap_single('nxt', 'N', 4, 'r9', 'new-file')
        batches = [batch_record('B1', cur, nxt)]
        assets = [asset('old-only', 100), asset('new-file', 200)]
        report = process_rollout(make_input(1000, batches, assets))
        self.assertEqual(report['status'], 'complete')
        req = report['snapshots'][0]['current']['requirements'][0]
        self.assertEqual(len(req['logical_pages']), 1)

    def test_same_identity_numbers_in_two_snapshots_map_to_different_assets(self):
        cur = snap_single('e1', 'S', 4, 'r1', 'old-file')
        nxt = snap_single('e2', 'S', 4, 'r1', 'new-file')  # same page+revision tuple
        batches = [batch_record('B1', cur, nxt)]
        assets = [asset('old-file', 100), asset('new-file', 100)]
        report = process_rollout(make_input(1000, batches, assets))
        self.assertEqual(report['status'], 'complete')
        cur_req = report['snapshots'][0]['current']['requirements'][0]
        nxt_req = report['snapshots'][0]['next']['requirements'][0]
        self.assertEqual(cur_req['asset_id'], 'old-file')
        self.assertEqual(nxt_req['asset_id'], 'new-file')

    def test_file_produced_in_step_cannot_be_a_patch_base_in_same_step(self):
        # A's next order needs m then n. n has a patch based on m, but m is
        # only produced during this step, so n must take its full package.
        cur = snap_single('e1', 'S', 4, 'r1', 'base-file')
        nxt_pb = proof_batch('e2', [('M', 4, 'r2'), ('N', 4, 'r2')])
        nxt = {'proof_batch': nxt_pb, 'page_assets':
               [{'section_id': 'M', 'page': p, 'revision': 'r2', 'asset_id': 'm'}
                for p in range(1, 5)] +
               [{'section_id': 'N', 'page': p, 'revision': 'r2', 'asset_id': 'n'}
                for p in range(1, 5)]}
        batches = [batch_record('B1', cur, nxt)]
        assets = [
            asset('base-file', 100),
            asset('m', 100, patches=[('base-file', 10)]),
            asset('n', 500, patches=[('m', 20)]),  # base m not resident at step start
        ]
        report = process_rollout(make_input(10000, batches, assets))
        self.assertEqual(report['status'], 'complete')
        acq = {a['asset_id']: a for a in report['steps'][0]['acquisitions']}
        self.assertEqual(acq['m']['acquisition'], 'patch')
        self.assertEqual(acq['m']['base_asset_id'], 'base-file')
        self.assertEqual(acq['n']['acquisition'], 'full')
        self.assertNotIn('base_asset_id', acq['n'])

    def test_identical_asset_id_used_by_two_batches_counts_once(self):
        cur_a = snap_single('a', 'SA', 4, 'r1', 'shared')
        cur_b = snap_single('b', 'SB', 4, 'r1', 'shared')
        nxt_a = snap_single('a2', 'PA', 4, 'r2', 'z')
        nxt_b = snap_single('b2', 'PB', 4, 'r2', 'z')
        batches = [batch_record('A', cur_a, nxt_a),
                   batch_record('B', cur_b, nxt_b)]
        assets = [asset('shared', 300), asset('z', 400)]
        report = process_rollout(make_input(10000, batches, assets))
        self.assertEqual(report['status'], 'complete')
        # Initial residency holds one copy of shared, counted once.
        self.assertEqual(report['initial_occupancy_bytes'], 300)
        first = report['steps'][0]
        self.assertEqual(first['pre_switch_peak_bytes'], 700)
        self.assertEqual(first['retained_assets'], [])  # z was not resident
        second = report['steps'][1]
        # z is already resident after the first switch: zero download.
        self.assertEqual(second['acquisitions'], [])
        self.assertEqual(second['download_bytes'], 0)


# ----------------------------------------------------------------- tie breaks

class TieBreakTests(unittest.TestCase):
    def test_equal_cost_routes_break_by_ascii_batch_sequence(self):
        # Both orders cost exactly one full package; the lexicographically
        # smaller first batch id ('A' < 'B') must win.
        cur_a = snap_single('a', 'SA', 4, 'r1', 'xa')
        cur_b = snap_single('b', 'SB', 4, 'r1', 'xb')
        nxt_a = snap_single('a2', 'PA', 4, 'r2', 'pa')
        nxt_b = snap_single('b2', 'PB', 4, 'r2', 'pb')
        batches = [batch_record('B', cur_b, nxt_b),
                   batch_record('A', cur_a, nxt_a)]  # input order reversed
        assets = [asset('xa', 100), asset('xb', 100),
                  asset('pa', 100), asset('pb', 100)]
        report = process_rollout(make_input(10000, batches, assets))
        self.assertEqual([s['batch_id'] for s in report['steps']], ['A', 'B'])

    def test_equal_price_patches_break_by_ascii_base_id(self):
        # Both candidate bases are resident when the step begins.
        cur_pb = proof_batch('e1', [('S', 4, 'r1')])
        cur = {'proof_batch': cur_pb, 'page_assets': [
            {'section_id': 'S', 'page': 1, 'revision': 'r1', 'asset_id': 'keep'},
            {'section_id': 'S', 'page': 2, 'revision': 'r1', 'asset_id': 'z-base'},
            {'section_id': 'S', 'page': 3, 'revision': 'r1', 'asset_id': 'a-base'},
            {'section_id': 'S', 'page': 4, 'revision': 'r1', 'asset_id': 'keep'}]}
        nxt = snap_single('e2', 'T', 4, 'r2', 'target')
        batches = [batch_record('B1', cur, nxt)]
        assets = [
            asset('keep', 100),
            asset('z-base', 100), asset('a-base', 100),
            asset('target', 500, full=False,
                  patches=[('z-base', 40), ('a-base', 40)]),
        ]
        report = process_rollout(make_input(10000, batches, assets))
        self.assertEqual(report['status'], 'complete')
        acq = report['steps'][0]['acquisitions'][0]
        self.assertEqual(acq['acquisition'], 'patch')
        self.assertEqual(acq['base_asset_id'], 'a-base')
        self.assertEqual(acq['download_bytes'], 40)

    def test_full_package_beats_more_expensive_patch(self):
        cur = snap_single('e1', 'S', 4, 'r1', 'keep')
        nxt = snap_single('e2', 'T', 4, 'r2', 'target')
        batches = [batch_record('B1', cur, nxt)]
        assets = [asset('keep', 100), asset('base', 100),
                  asset('target', 200, patches=[('base', 150)])]
        report = process_rollout(make_input(10000, batches, assets))
        acq = report['steps'][0]['acquisitions'][0]
        self.assertEqual(acq['acquisition'], 'full')
        self.assertEqual(acq['download_bytes'], 200)


# ------------------------------------------------------------- brute-force check

class BruteForceOptimalityTests(unittest.TestCase):
    def _brute_force(self, raw):
        """Independent permutation enumeration with its own simulation."""
        catalog = {a['asset_id']: a for a in raw['assets']}
        size = {aid: a['size_bytes'] for aid, a in catalog.items()}
        batches = raw['batches']

        def requirements(snap):
            order_assets = set()
            order = process_batch(snap['proof_batch'])
            mapped = {(r['section_id'], r['page'], r['revision']): r['asset_id']
                      for r in snap['page_assets']}
            for edition in order['editions']:
                for sig in edition['signatures']:
                    if sig['source'] != 'fresh_print':
                        continue
                    for sheet in sig['sheets']:
                        for side in ('front', 'back'):
                            for slot in sheet['sides'][side]:
                                page = slot['logical_page']
                                if page is not None:
                                    order_assets.add(
                                        mapped[(page['section_id'], page['page'],
                                                page['revision'])])
            return order_assets

        cur = [requirements(b['current']) for b in batches]
        nxt = [requirements(b['next']) for b in batches]
        initial = set().union(*cur)
        if sum(size[a] for a in initial) > raw['capacity_bytes']:
            return [], {()}
        feasible = []
        # The empty prefix is always reachable from a valid initial state.
        blocked_prefixes = {()}
        for perm in itertools.permutations(range(len(batches))):
            resident = set(initial)
            switched = set()
            total = 0
            ok = True
            prefix = []
            for i in perm:
                step_start = set(resident)
                working = set(resident)
                for aid in sorted(nxt[i]):
                    if aid in working:
                        continue
                    entry = catalog[aid]
                    choices = []
                    if entry['full_available']:
                        choices.append((entry['size_bytes'], None))
                    for patch in entry['patches']:
                        if patch['base_asset_id'] in step_start:
                            choices.append((patch['wire_bytes'], patch['base_asset_id']))
                    if not choices:
                        ok = False
                        break
                    cost, _ = min(choices, key=lambda c: (c[0], c[1] or ''))
                    total += cost
                    working.add(aid)
                if not ok:
                    break
                if sum(size[a] for a in working) > raw['capacity_bytes']:
                    ok = False
                    break
                protected = set(nxt[i])
                for j in range(len(batches)):
                    if j == i:
                        continue
                    protected |= nxt[j] if j in switched else cur[j]
                resident = working & protected
                switched.add(i)
                prefix.append(batches[i]['batch_id'])
                blocked_prefixes.add(tuple(prefix))
            if ok:
                feasible.append((total, tuple(batches[i]['batch_id'] for i in perm)))
        return feasible, blocked_prefixes

    def test_small_cases_match_enumerated_optimum(self):
        import random
        rng = random.Random(20261122)
        for trial in range(40):
            n = rng.randint(1, 4)
            asset_ids = [f'asset-{i}' for i in range(rng.randint(n, n + 4))]
            assets = []
            for aid in asset_ids:
                patches = []
                bases = [x for x in asset_ids if x != aid]
                for base in rng.sample(bases, min(len(bases), rng.randint(0, 2))):
                    patches.append((base, rng.randint(1, 20)))
                assets.append(asset(aid, rng.randint(50, 300),
                                    full=rng.random() < 0.8, patches=patches))
            batches = []
            for b in range(n):
                cur_ids = rng.sample(asset_ids, rng.randint(1, len(asset_ids)))
                nxt_ids = rng.sample(asset_ids, rng.randint(1, len(asset_ids)))
                cur = snap_single(f'c{b}', f'SC{b}', 4, 'r1', cur_ids[0])
                cur['page_assets'] = [
                    {'section_id': f'SC{b}', 'page': 1, 'revision': 'r1',
                     'asset_id': cur_ids[0]},
                    {'section_id': f'SC{b}', 'page': 2, 'revision': 'r1',
                     'asset_id': cur_ids[1 % len(cur_ids)]},
                    {'section_id': f'SC{b}', 'page': 3, 'revision': 'r1',
                     'asset_id': cur_ids[2 % len(cur_ids)]},
                    {'section_id': f'SC{b}', 'page': 4, 'revision': 'r1',
                     'asset_id': cur_ids[0]}]
                nxt = snap_single(f'n{b}', f'SN{b}', 4, 'r2', nxt_ids[0])
                nxt['page_assets'] = [
                    {'section_id': f'SN{b}', 'page': p, 'revision': 'r2',
                     'asset_id': nxt_ids[(p - 1) % len(nxt_ids)]} for p in range(1, 5)]
                batches.append(batch_record(f'B{b}', cur, nxt))
            raw = make_input(rng.choice([400, 700, 1200, 5000]), batches, assets)
            size_lookup = {a['asset_id']: a['size_bytes'] for a in assets}
            initial_ids = {row['asset_id'] for b in batches
                           for row in b['current']['page_assets']}
            initial_size = sum(size_lookup[aid] for aid in initial_ids)
            if initial_size > raw['capacity_bytes']:
                continue  # contradictory initial state is an exit-2 input, skip
            feasible, _ = self._brute_force(raw)
            report = process_rollout(copy.deepcopy(raw))
            if feasible:
                best_cost, best_seq = min(feasible)
                self.assertEqual(report['status'], 'complete')
                self.assertEqual(report['total_download_bytes'], best_cost)
                self.assertEqual(tuple(s['batch_id'] for s in report['steps']), best_seq)
            else:
                self.assertEqual(report['status'], 'blocked')

    def test_blocked_prefix_is_the_longest_reachable_one(self):
        # Three batches where any first switch works but no second one fits.
        cur_a = snap_single('a', 'SA', 4, 'r1', 'xa')
        cur_b = snap_single('b', 'SB', 4, 'r1', 'xb')
        cur_c = snap_single('c', 'SC', 4, 'r1', 'xc')
        nxt_a = snap_single('a2', 'PA', 4, 'r2', 'pa')
        nxt_b = snap_single('b2', 'PB', 4, 'r2', 'pb')
        nxt_c = snap_single('c2', 'PC', 4, 'r2', 'pc')
        batches = [batch_record('A', cur_a, nxt_a),
                   batch_record('B', cur_b, nxt_b),
                   batch_record('C', cur_c, nxt_c)]
        assets = [asset('xa', 100), asset('xb', 100), asset('xc', 100),
                  asset('pa', 200), asset('pb', 200), asset('pc', 200)]
        # Initial residency 300. The first switch peaks at 500 and reclaims the
        # replaced current asset; a second switch peaks at 600. Capacity 550
        # admits exactly one switch, so the longest reachable prefix has length 1.
        raw = make_input(550, batches, assets)
        feasible, prefixes = self._brute_force(raw)
        self.assertEqual(feasible, [])
        longest = max(len(p) for p in prefixes)
        report = process_rollout(raw)
        self.assertEqual(report['status'], 'blocked')
        self.assertEqual(len(report['reachable_prefix']), longest)
        self.assertEqual(len(report['reachable_prefix']), 1)
        # Lexicographically smallest length-1 prefix.
        self.assertEqual(report['reachable_prefix'], ['A'])
        for blocker in report['remaining_blockers']:
            self.assertTrue(blocker['over_capacity'])


# ------------------------------------------------------------- permutation invariance

class DeterminismTests(unittest.TestCase):
    def _scenario(self):
        cur_a = snap_single('a', 'SA', 4, 'r1', 'x')
        cur_b = snap_single('b', 'SB', 4, 'r1', 'b')
        nxt_a = snap_single('a2', 'PA', 4, 'r2', 'p')
        nxt_b = snap_single('b2', 'PB', 4, 'r2', 'q')
        batches = [batch_record('A', cur_a, nxt_a),
                   batch_record('B', cur_b, nxt_b)]
        assets = [
            asset('q', 300), asset('p', 200, patches=[('b', 50)]),
            asset('b', 300), asset('x', 300),
        ]
        return make_input(1100, batches, assets)

    def test_record_order_permutations_keep_report_identical(self):
        base = self._scenario()
        baseline = process_rollout(copy.deepcopy(base))
        shuffled = copy.deepcopy(base)
        shuffled['batches'].reverse()
        shuffled['assets'].reverse()
        for a in shuffled['assets']:
            a['patches'].reverse()
        for b in shuffled['batches']:
            b['current']['page_assets'].reverse()
            b['next']['page_assets'].reverse()
        report = process_rollout(shuffled)
        self.assertEqual(json.dumps(report, sort_keys=True),
                         json.dumps(baseline, sort_keys=True))


# ------------------------------------------------------------- validation / CLI

class ValidationTests(unittest.TestCase):
    def _good(self):
        cur = snap_single('e1', 'S', 4, 'r1', 'old')
        nxt = snap_single('e2', 'T', 4, 'r2', 'new')
        return make_input(1000, [batch_record('B1', cur, nxt)],
                          [asset('old', 100), asset('new', 200)])

    def test_initial_occupancy_above_capacity_is_exit_two(self):
        raw = self._good()
        raw['capacity_bytes'] = 50  # initial residency is 100
        with self.assertRaises(ValueError):
            process_rollout(raw)

    def test_unknown_asset_reference_rejected(self):
        raw = self._good()
        raw['assets'] = [asset('old', 100)]
        with self.assertRaises(ValueError):
            process_rollout(raw)

    def test_missing_and_extra_page_mapping_rejected(self):
        raw = self._good()
        raw['batches'][0]['next']['page_assets'].pop()
        with self.assertRaises(ValueError):
            process_rollout(raw)
        raw2 = self._good()
        raw2['batches'][0]['next']['page_assets'].append(
            {'section_id': 'T', 'page': 9, 'revision': 'r2', 'asset_id': 'new'})
        with self.assertRaises(ValueError):
            process_rollout(raw2)

    def test_duplicate_mapping_rejected(self):
        raw = self._good()
        raw['batches'][0]['next']['page_assets'][1] = dict(
            raw['batches'][0]['next']['page_assets'][0])
        with self.assertRaises(ValueError):
            process_rollout(raw)

    def test_invalid_work_order_rejected(self):
        raw = self._good()
        raw['batches'][0]['current']['proof_batch']['editions'][0]['source'][
            'signature_pages'] = 6  # not a multiple of four
        with self.assertRaises(ValueError):
            process_rollout(raw)

    def test_patch_rules_enforced(self):
        raw = self._good()
        # Unknown base.
        bad = copy.deepcopy(raw)
        bad['assets'][1]['patches'] = [('ghost', 10)]
        with self.assertRaises(ValueError):
            process_rollout(bad)
        # Self base.
        bad = copy.deepcopy(raw)
        bad['assets'][1]['patches'] = [('new', 10)]
        with self.assertRaises(ValueError):
            process_rollout(bad)
        # Duplicate base.
        bad = copy.deepcopy(raw)
        bad['assets'][1]['patches'] = [('old', 10), ('old', 12)]
        with self.assertRaises(ValueError):
            process_rollout(bad)
        # Wire bytes not below full size.
        bad = copy.deepcopy(raw)
        bad['assets'][1]['patches'] = [('old', 200)]
        with self.assertRaises(ValueError):
            process_rollout(bad)

    def test_duplicate_asset_and_batch_ids_rejected(self):
        raw = self._good()
        raw['assets'].append(asset('old', 50))
        with self.assertRaises(ValueError):
            process_rollout(raw)
        raw2 = self._good()
        raw2['batches'].append(raw2['batches'][0])
        with self.assertRaises(ValueError):
            process_rollout(raw2)

    def test_bounds_and_types_enforced(self):
        raw = self._good()
        raw['capacity_bytes'] = 0
        with self.assertRaises(ValueError):
            process_rollout(raw)
        raw = self._good()
        raw['assets'][0]['full_available'] = 'yes'
        with self.assertRaises(ValueError):
            process_rollout(raw)
        raw = self._good()
        raw['batches'] = raw['batches'] * 13
        with self.assertRaises(ValueError):
            process_rollout(raw)
        raw = self._good()
        raw['unexpected'] = 1
        with self.assertRaises(ValueError):
            process_rollout(raw)


class ExtraEdgeCaseTests(unittest.TestCase):
    def test_patch_chain_across_steps_uses_newly_reclaimed_state(self):
        # Step 1 produces m from a patch; once B is also switched, m is
        # resident at step start and can serve as n's patch base.
        cur_a = snap_single('a', 'SA', 4, 'r1', 'base')
        cur_b = snap_single('b', 'SB', 4, 'r1', 'keep')
        nxt_a = snap_single('a2', 'PA', 4, 'r2', 'm')
        nxt_b = snap_single('b2', 'PB', 4, 'r2', 'n')
        batches = [batch_record('A', cur_a, nxt_a),
                   batch_record('B', cur_b, nxt_b)]
        assets = [
            asset('base', 100), asset('keep', 100),
            asset('m', 200, full=False, patches=[('base', 20)]),
            asset('n', 300, full=False, patches=[('m', 30)]),
        ]
        report = process_rollout(make_input(10000, batches, assets))
        self.assertEqual(report['status'], 'complete')
        order = [s['batch_id'] for s in report['steps']]
        self.assertEqual(order, ['A', 'B'])
        first, second = report['steps']
        self.assertEqual(first['acquisitions'][0]['base_asset_id'], 'base')
        # At the start of step 2 m is protected (A already switched) and
        # resident, so n's patch on m is legal now.
        self.assertEqual(second['acquisitions'][0]['acquisition'], 'patch')
        self.assertEqual(second['acquisitions'][0]['base_asset_id'], 'm')
        self.assertEqual(report['total_download_bytes'], 50)

    def test_twelve_batches_plans_fast_and_keeps_final_residency(self):
        batches = []
        assets = [asset('start', 100)]
        for i in range(12):
            cur = snap_single(f'c{i}', f'SC{i}', 4, 'r1', 'start')
            nid = f'new-{i:02d}'
            assets.append(asset(nid, 100))
            nxt = snap_single(f'n{i}', f'SN{i}', 4, 'r2', nid)
            batches.append(batch_record(f'B{i:02d}', cur, nxt))
        report = process_rollout(make_input(10_000, batches, assets))
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(len(report['steps']), 12)
        # Every batch ends on its own new asset; the shared 'start' file loses
        # its last reference only at the final switch.
        self.assertEqual(report['final_occupancy_bytes'], 1200)
        self.assertEqual(report['total_download_bytes'], 12 * 100)

    def test_step_with_zero_download_still_reports_peak_and_reclaim(self):
        cur_a = snap_single('a', 'SA', 4, 'r1', 'shared')
        cur_b = snap_single('b', 'SB', 4, 'r1', 'other')
        nxt_a = snap_single('a2', 'PA', 4, 'r2', 'shared')  # unchanged file
        nxt_b = snap_single('b2', 'PB', 4, 'r2', 'z')
        batches = [batch_record('A', cur_a, nxt_a),
                   batch_record('B', cur_b, nxt_b)]
        assets = [asset('shared', 100), asset('other', 100), asset('z', 200)]
        report = process_rollout(make_input(10000, batches, assets))
        self.assertEqual(report['status'], 'complete')
        first = report['steps'][0]
        self.assertEqual(first['download_bytes'], 0)
        self.assertEqual(first['acquisitions'], [])
        self.assertEqual(first['retained_assets'],
                         [{'asset_id': 'shared', 'size_bytes': 100}])
        self.assertEqual(first['pre_switch_peak_bytes'], 200)


class CliRolloutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__file__).resolve().parent / '_tmp_rollout'
        self.tmp.mkdir(exist_ok=True)

    def tearDown(self):
        for child in self.tmp.iterdir():
            child.unlink()
        self.tmp.rmdir()

    def _run(self, source_path, output_path, *flags):
        args = [sys.executable, '-m', 'signature_press.cli', '--source', str(source_path),
                '--output', str(output_path), *flags]
        return subprocess.run(args, capture_output=True, text=True)

    def test_cli_fixture_success_then_refuses_overwrite(self):
        fixture = Path(__file__).resolve().parents[1] / 'fixtures' / 'cache-rollout.json'
        output = self.tmp / 'rollout.json'
        first = self._run(fixture, output, '--cache-rollout')
        self.assertEqual(first.returncode, 0, first.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report['schema_version'], 5)
        self.assertEqual(report['status'], 'complete')
        before = output.read_bytes()
        again = self._run(fixture, output, '--cache-rollout')
        self.assertEqual(again.returncode, 2)
        self.assertEqual(output.read_bytes(), before)

    def test_blocked_input_exits_zero(self):
        cur = snap_single('a', 'SA', 4, 'r1', 'x')
        cur_b = snap_single('b', 'SB', 4, 'r1', 'b')
        nxt = snap_single('a2', 'PA', 4, 'r2', 'p')
        nxt_b = snap_single('b2', 'PB', 4, 'r2', 'q')
        batches = [batch_record('A', cur, nxt), batch_record('B', cur_b, nxt_b)]
        assets = [asset('x', 300), asset('a', 100), asset('b', 300),
                  asset('p', 200, full=False, patches=[('b', 50)]),
                  asset('q', 300, full=False)]
        source = self.tmp / 'blocked.json'
        source.write_text(json.dumps(make_input(1000, batches, assets)))
        output = self.tmp / 'blocked-out.json'
        run = self._run(source, output, '--cache-rollout')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(output.read_text())['status'], 'blocked')

    def test_invalid_input_exits_two_and_publishes_nothing(self):
        source = self.tmp / 'bad.json'
        output = self.tmp / 'bad-out.json'
        source.write_text(json.dumps({'capacity_bytes': 1, 'batches': [], 'assets': []}))
        run = self._run(source, output, '--cache-rollout')
        self.assertEqual(run.returncode, 2)
        self.assertFalse(output.exists())

    def test_mode_flags_are_mutually_exclusive(self):
        fixture = Path(__file__).resolve().parents[1] / 'fixtures' / 'cache-rollout.json'
        output = self.tmp / 'x.json'
        run = self._run(fixture, output, '--cache-rollout', '--proof-batch')
        self.assertEqual(run.returncode, 2)
        self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
