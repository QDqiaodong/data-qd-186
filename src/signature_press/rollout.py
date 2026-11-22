"""Offline cache rollout planning (schema version 5).

The digital press keeps several proof work orders online at once; each
rollout switches one whole batch from its current v2 work order to the next
edition. Before a batch is switched its current assets stay printable and
nothing of theirs may be reclaimed; only after a switch does the controller
delete files that no online work order references.

A switch keeps every file still referenced by an online work order (the old
order of the switching batch included). Every asset the new order needs but
does not yet resident is fetched exactly once, either as a full package or
as a patch whose base is resident *when the step begins*: a file produced
inside the same step can never serve as a patch base. Streaming patches do
not consume cache quota; the peak check uses the total full-file size of the
deduplicated resident-and-new set. Reclamation completes before the next
step, so no future-batch assets may be downloaded early and no unreferenced
patch base may be kept on purpose.

The planner searches all batch permutations (there are 1..12 batches) as a
subset dynamic program and minimizes total downloaded bytes; ties break by
the ASCII-lexicographic batch-id sequence. When no complete route exists it
publishes a ``blocked`` business diagnosis: the longest reachable prefix,
then per remaining batch the over-capacity peak (computed as if every
target had arrived) and the targets with no usable source.

Only the Python standard library is used.
"""
import re

from .batch import process_batch
from .checks import array, fields, ident, integer

_REVISION_RE = re.compile(r'[\x21-\x7e]+')
_MAX_BYTES = 10 ** 12


def _revision(value):
    if not isinstance(value, str) or not _REVISION_RE.fullmatch(value):
        raise ValueError('revision must be a non-empty printable ASCII identifier')
    return value


# ------------------------------------------------------------- input validation

def _validate_catalog(raw_assets):
    records = array(raw_assets, 0, 100_000)
    catalog = {}
    asset_ids = set()
    pending_patches = []
    for record in records:
        fields(record, ('asset_id', 'size_bytes', 'full_available', 'patches'))
        asset_id = ident(record['asset_id'])
        if asset_id in asset_ids:
            raise ValueError(f'duplicate asset_id: {asset_id}')
        asset_ids.add(asset_id)
        size = integer(record['size_bytes'], 1, _MAX_BYTES)
        if type(record['full_available']) is not bool:
            raise ValueError('full_available must be a boolean')
        if not isinstance(record['patches'], list):
            raise ValueError(f'asset {asset_id}: patches must be a list')
        seen_bases = set()
        patches = []
        for patch in record['patches']:
            fields(patch, ('base_asset_id', 'wire_bytes'))
            base_id = ident(patch['base_asset_id'])
            if base_id == asset_id:
                raise ValueError(f'asset {asset_id}: a patch base cannot be the target itself')
            if base_id in seen_bases:
                raise ValueError(f'asset {asset_id}: duplicate patch base {base_id}')
            seen_bases.add(base_id)
            wire = integer(patch['wire_bytes'], 1, _MAX_BYTES)
            if wire >= size:
                raise ValueError(f'asset {asset_id}: patch wire_bytes must be below the full size')
            patches.append({'base_asset_id': base_id, 'wire_bytes': wire})
            pending_patches.append((asset_id, base_id))
        catalog[asset_id] = {'asset_id': asset_id, 'size_bytes': size,
                             'full_available': record['full_available'],
                             'patches': sorted(patches, key=lambda p: p['base_asset_id'])}
    # Bases must exist inside the same catalog (checked after every id is known).
    for target_id, base_id in pending_patches:
        if base_id not in catalog:
            raise ValueError(f'asset {target_id}: unknown patch base {base_id}')
    return catalog


def _fresh_identities(order):
    """Non-blank logical-page identities of every fresh-print signature.

    Retained legacy signatures never need digital assets, and blank slots are
    skipped. Each identity is reported once even when two editions place it.
    """
    seen = set()
    identities = []
    for edition in order['editions']:
        for signature in edition['signatures']:
            if signature['source'] != 'fresh_print':
                continue
            for sheet in signature['sheets']:
                for side in ('front', 'back'):
                    for slot in sheet['sides'][side]:
                        page = slot['logical_page']
                        if page is None:
                            continue
                        key = (page['section_id'], page['page'], page['revision'])
                        if key not in seen:
                            seen.add(key)
                            identities.append(key)
    return identities


def _snapshot_requirements(snapshot, catalog):
    """Validate one proof_batch snapshot against its page_assets mapping.

    The v2 work order is the authority: every non-blank identity appearing in
    a fresh-print signature must be mapped exactly once, and no mapped
    identity may be absent from those signatures. Identities only make sense
    inside this snapshot, so the same page/revision numbers of another
    snapshot never imply the same file.
    """
    fields(snapshot, ('proof_batch', 'page_assets'))
    order = process_batch(snapshot['proof_batch'])
    order_identities = set(_fresh_identities(order))

    rows = array(snapshot['page_assets'], 0, 100_000)
    mapped = {}
    for row in rows:
        fields(row, ('section_id', 'page', 'revision', 'asset_id'))
        section_id = ident(row['section_id'])
        page = integer(row['page'], 1, 10_000)
        revision = _revision(row['revision'])
        asset_id = ident(row['asset_id'])
        if asset_id not in catalog:
            raise ValueError(f'page_assets references unknown asset: {asset_id}')
        key = (section_id, page, revision)
        if key in mapped:
            raise ValueError(f'duplicate page_assets mapping: {key}')
        mapped[key] = asset_id

    if set(mapped) != order_identities:
        missing = sorted(k for k in order_identities if k not in mapped)
        extra = sorted(k for k in mapped if k not in order_identities)
        raise ValueError(f'page_assets mismatch; missing={missing} extra={extra}')

    # Build the provenance from the canonical v2 work order rather than the
    # input row order, so permuting page_assets cannot change the report.
    asset_identities = {}
    for identity in sorted(order_identities):
        asset_identities.setdefault(mapped[identity], []).append(identity)
    requirements = []
    for asset_id in sorted(asset_identities):
        requirements.append({
            'asset_id': asset_id,
            'size_bytes': catalog[asset_id]['size_bytes'],
            'logical_pages': [
                {'section_id': identity[0], 'page': identity[1],
                 'revision': identity[2],
                 'locations': [{'edition_id': edition_id,
                                'signature': signature, 'sheet': sheet,
                                'side': side, 'folio': folio}
                               for edition_id, signature, sheet, side, folio
                               in _page_locations(order, identity)]}
                for identity in asset_identities[asset_id]],
        })
    return order, set(mapped.values()), requirements


def _page_locations(order, identity):
    """Every fresh-print slot of one snapshot that carries the identity."""
    result = set()
    for edition in order['editions']:
        for signature in edition['signatures']:
            if signature['source'] != 'fresh_print':
                continue
            for sheet in signature['sheets']:
                for side in ('front', 'back'):
                    for slot in sheet['sides'][side]:
                        page = slot['logical_page']
                        if page is None:
                            continue
                        if (page['section_id'], page['page'], page['revision']) == identity:
                            result.add((edition['id'], signature['signature'],
                                        sheet['sheet'], side, slot['folio']))
    return sorted(result)


# ------------------------------------------------------------- step simulation

def _size_sum(asset_ids, catalog):
    return sum(catalog[asset_id]['size_bytes'] for asset_id in asset_ids)


def _fetch_plan(target_id, resident, catalog):
    """Cheapest legal acquisition of one not-yet-resident target.

    Full package and patch never tie (wire_bytes is strictly below the full
    size); among equal-price patches the base with the smallest ASCII id
    wins. A base produced earlier in the same step is not in ``resident``
    and therefore cannot be selected.
    """
    entry = catalog[target_id]
    candidates = []
    if entry['full_available']:
        candidates.append((entry['size_bytes'], 'full', None))
    for patch in entry['patches']:
        if patch['base_asset_id'] in resident:
            candidates.append((patch['wire_bytes'], 'patch', patch['base_asset_id']))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[2] or ''))
    cost, kind, base_id = candidates[0]
    return {'kind': kind, 'cost': cost, 'base_asset_id': base_id}


def _evaluate(batch_index, switched_mask, resident, batches, catalog, capacity):
    """Simulate one batch switch from a resident set.

    ``switched_mask`` records which batches already run their next order; it
    decides which assets stay protected after the switch. Returns the fetch
    list (in deterministic asset-id order), the download cost, the
    pre-switch peak, the post-switch resident set and the reclaimed ids, or
    ``None`` when at least one target has no usable source.
    """
    batch = batches[batch_index]
    needed = batch['next_assets']
    fetches = []
    cost = 0
    step_start_resident = set(resident)
    working_resident = set(resident)
    for asset_id in sorted(needed):
        if asset_id in working_resident:
            continue
        # Only files resident when the step began are legal patch bases; the
        # growing working set is used purely for residency/peak bookkeeping.
        plan = _fetch_plan(asset_id, step_start_resident, catalog)
        if plan is None:
            return None
        fetches.append({'asset_id': asset_id, 'plan': plan})
        cost += plan['cost']
        working_resident.add(asset_id)
    # Peak: dedup'd full-file footprint while all originals and new targets
    # coexist, independent of how the bytes actually arrive.
    peak = _size_sum(working_resident, catalog)
    # After the switch the moved batch runs its next order; every other
    # online order keeps whichever revision it currently runs.
    protected = set(batch['next_assets'])
    for index, other in enumerate(batches):
        if index == batch_index:
            continue
        if switched_mask & (1 << index):
            protected |= other['next_assets']
        else:
            protected |= other['current_assets']
    remaining = working_resident & protected
    reclaimed = sorted(working_resident - remaining)
    return {'fetches': fetches, 'download': cost, 'peak': peak,
            'over_capacity': peak > capacity,
            'remaining': remaining, 'reclaimed': reclaimed}


# ------------------------------------------------------------- subset planning

def _plan_route(batch_count, initial_resident, batches, catalog, capacity):
    """Subset DP over switched batches.

    State value is ``(total_download, id_sequence)``; the sequence tie-break
    is the ASCII lexicographic order required by the contract. Transitions
    that exceed the cache or lack a source are simply absent.
    """
    full = (1 << batch_count) - 1
    best = {0: (0, (), frozenset(initial_resident))}
    for mask in range(full + 1):
        if mask not in best:
            continue
        total, sequence, resident = best[mask]
        for index in range(batch_count):
            bit = 1 << index
            if mask & bit:
                continue
            outcome = _evaluate(index, mask, resident, batches, catalog, capacity)
            if outcome is None or outcome['over_capacity']:
                continue
            new_mask = mask | bit
            candidate = (total + outcome['download'],
                         sequence + (batches[index]['batch_id'],),
                         frozenset(outcome['remaining']))
            old = best.get(new_mask)
            if old is None or (candidate[0], candidate[1]) < (old[0], old[1]):
                best[new_mask] = candidate
    return best, full


def _longest_prefix(best):
    """The reachable state with the most switched batches, ties by id order."""
    winner_mask = None
    winner_value = None
    for reachable, value in best.items():
        if winner_mask is None \
                or reachable.bit_count() > winner_mask.bit_count() \
                or (reachable.bit_count() == winner_mask.bit_count()
                    and value[1] < winner_value[1]):
            winner_mask = reachable
            winner_value = value
    return winner_mask, winner_value


# ------------------------------------------------------------- report building

def _blocked_report(best, batches, catalog, capacity, initial_resident):
    prefix_mask, prefix_value = _longest_prefix(best)
    prefix_ids = list(prefix_value[1])
    step_start_resident = prefix_value[2]
    remaining = [index for index in range(len(batches))
                 if not (prefix_mask & (1 << index))]
    blockers = []
    for index in sorted(remaining, key=lambda i: batches[i]['batch_id']):
        batch = batches[index]
        unavailable = []
        working_resident = set(step_start_resident)
        for asset_id in sorted(batch['next_assets']):
            if asset_id in working_resident:
                continue
            # Same step-start base rule as a real switch: files added below
            # cannot act as bases inside this hypothetical step.
            plan = _fetch_plan(asset_id, step_start_resident, catalog)
            if plan is None:
                unavailable.append(asset_id)
            # Every target counts toward the hypothetical peak, even ones with
            # no usable source, as the contract requires.
            working_resident.add(asset_id)
        hypothetical_peak = _size_sum(working_resident, catalog)
        blockers.append({
            'batch_id': batch['batch_id'],
            'peak_bytes': hypothetical_peak,
            'over_capacity': hypothetical_peak > capacity,
            'unavailable_targets': unavailable,
            'snapshot_requirements': batch['next_requirements'],
        })
    return {
        'schema_version': 5,
        'status': 'blocked',
        'capacity_bytes': capacity,
        'reachable_prefix': prefix_ids,
        'remaining_blockers': blockers,
        'summary': {'status': 'blocked',
                    'switched_batches': prefix_mask.bit_count(),
                    'remaining_batches': len(remaining),
                    'initial_occupancy_bytes': _size_sum(initial_resident, catalog)},
    }


def _step_report(step_no, batch_id, resident, outcome, batches, catalog):
    batch = next(b for b in batches if b['batch_id'] == batch_id)
    retained = sorted(batch['next_assets'] & resident)
    acquisitions = []
    for fetch in outcome['fetches']:
        entry = {'asset_id': fetch['asset_id'],
                 'size_bytes': catalog[fetch['asset_id']]['size_bytes']}
        if fetch['plan']['kind'] == 'full':
            entry['acquisition'] = 'full'
            entry['download_bytes'] = fetch['plan']['cost']
        else:
            entry['acquisition'] = 'patch'
            entry['base_asset_id'] = fetch['plan']['base_asset_id']
            entry['download_bytes'] = fetch['plan']['cost']
        acquisitions.append(entry)
    return {
        'step': step_no,
        'batch_id': batch_id,
        'retained_assets': [
            {'asset_id': asset_id,
             'size_bytes': catalog[asset_id]['size_bytes']}
            for asset_id in retained],
        'acquisitions': acquisitions,
        'download_bytes': outcome['download'],
        'pre_switch_peak_bytes': outcome['peak'],
        'reclaimed_assets': [
            {'asset_id': asset_id,
             'size_bytes': catalog[asset_id]['size_bytes']}
            for asset_id in outcome['reclaimed']],
        'post_reclaim_occupancy_bytes': _size_sum(outcome['remaining'], catalog),
    }


def _success_report(sequence, initial_resident, batches, catalog, capacity):
    steps = []
    resident = set(initial_resident)
    switched_mask = 0
    total_download = 0
    for step_no, batch_id in enumerate(sequence, start=1):
        index = next(i for i, b in enumerate(batches) if b['batch_id'] == batch_id)
        outcome = _evaluate(index, switched_mask, resident, batches, catalog, capacity)
        if outcome is None or outcome['over_capacity']:
            raise ValueError('internal: published route became infeasible')
        steps.append(_step_report(step_no, batch_id, resident, outcome, batches, catalog))
        total_download += outcome['download']
        resident = outcome['remaining']
        switched_mask |= 1 << index
    snapshots = []
    # Snapshot provenance is keyed by batch id and sorted canonically, so the
    # input order of batches can never change the published report.
    for batch in sorted(batches, key=lambda b: b['batch_id']):
        snapshots.append({
            'batch_id': batch['batch_id'],
            'current': {'work_order': batch['current_order'],
                        'requirements': batch['current_requirements']},
            'next': {'work_order': batch['next_order'],
                     'requirements': batch['next_requirements']},
        })
    return {
        'schema_version': 5,
        'status': 'complete',
        'capacity_bytes': capacity,
        'initial_occupancy_bytes': _size_sum(initial_resident, catalog),
        'final_occupancy_bytes': _size_sum(resident, catalog),
        'total_download_bytes': total_download,
        'steps': steps,
        'snapshots': snapshots,
        'summary': {'status': 'complete',
                    'batches': len(batches),
                    'steps': len(steps),
                    'total_download_bytes': total_download,
                    'initial_occupancy_bytes': _size_sum(initial_resident, catalog),
                    'final_occupancy_bytes': _size_sum(resident, catalog)},
    }


# ------------------------------------------------------------- entry point

def process_rollout(raw):
    """Plan a cache rollout and return the schema-version-5 report.

    Raises ValueError on any invalid input (including an initial occupancy
    above capacity). A legal input without a complete route returns a
    ``blocked`` report instead; both outcomes are normal exit-0 results.
    """
    fields(raw, ('capacity_bytes', 'batches', 'assets'))
    capacity = integer(raw['capacity_bytes'], 1, _MAX_BYTES)
    catalog = _validate_catalog(raw['assets'])
    batch_records = array(raw['batches'], 1, 12)

    batch_ids = []
    batches = []
    for record in batch_records:
        fields(record, ('batch_id', 'current', 'next'))
        batch_id = ident(record['batch_id'])
        if batch_id in batch_ids:
            raise ValueError(f'duplicate batch_id: {batch_id}')
        batch_ids.append(batch_id)
        current_order, current_assets, current_requirements = _snapshot_requirements(
            record['current'], catalog)
        next_order, next_assets, next_requirements = _snapshot_requirements(
            record['next'], catalog)
        batches.append({'batch_id': batch_id,
                        'current_order': current_order,
                        'next_order': next_order,
                        'current_assets': current_assets,
                        'next_assets': next_assets,
                        'current_requirements': current_requirements,
                        'next_requirements': next_requirements})

    # The machine starts with exactly the union of the current work orders'
    # assets; exceeding the cache is a contradictory input (exit 2).
    initial_resident = set()
    for batch in batches:
        initial_resident |= batch['current_assets']
    initial_occupancy = _size_sum(initial_resident, catalog)
    if initial_occupancy > capacity:
        raise ValueError(
            f'initial occupancy {initial_occupancy} exceeds capacity {capacity}')

    best, full = _plan_route(len(batches), initial_resident, batches, catalog, capacity)
    if full not in best:
        return _blocked_report(best, batches, catalog, capacity, initial_resident)
    sequence = best[full][1]
    return _success_report(sequence, initial_resident, batches, catalog, capacity)
