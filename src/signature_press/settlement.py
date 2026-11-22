"""Offline plate-fee settlement (schema version 4).

The plate settlement replays a supplier's plate-fee ledger against real
v2 work orders. Each order entry is independently planned with the existing
v2 planner; its executed fresh-print signatures define the billable sides.
A billable side is one printed side (front/back of one sheet of one
signature of one edition of one order) that contains at least one
non-blank logical page. Retained (legacy) signatures and all-blank sides
can never be billed: referencing them is an input error.

A supplier may declare that several billable sides, potentially coming from
different work orders, share one physical plate inside one billing pool.
Shared-plate claims are checked against the work orders: the paper
width/height must match and, slot by slot from left to right, the revised
logical page (including null blanks) and the complete placement frame must
match. Face names, signature numbers, sheet numbers and folios do not
participate. Sides that merely look identical are never merged without the
supplier's own shared-pool entry.

Ledger events are replayed strictly in array order. ``revise`` events
replace whole pool records (versioned; first appearance is version 1); a
pool may be revoked with zero fen and an empty use list and reopened later
under the same identity with an incremented version. ``close`` events emit
one settlement statement for one order against the ledger exactly as it
stands at that point in the log; later corrections never rewrite an
existing statement. Apportionment uses exact integer arithmetic
(largest-remainder, ASCII order_id tie-break); statements carry signed
per-pool differences so exits and revocations produce refunds.

Only the Python standard library is used.
"""
from fractions import Fraction

from .batch import process_batch
from .checks import array, fields, ident, integer

_SIDES = ('front', 'back')
_MAX_FEN = 10 ** 12


# ----------------------------------------------------------- work-order side index

def _side_fingerprint(slots):
    """Print content of one side: slot-wise revised page plus full frame."""
    token = []
    for slot in slots:
        page = slot['logical_page']
        page_token = None if page is None else (page['section_id'], page['page'],
                                                page['revision'])
        frame = slot['placement']
        token.append((page_token, frame['x_um'], frame['y_um'], frame['width_um'],
                      frame['height_um'], frame['rotation_degrees']))
    return tuple(token)


def _collect_order_sides(proof_batch):
    """Plan one v2 order and index every side of its fresh signatures.

    Returns a mapping ``(edition_id, signature, sheet, side)`` -> side record
    carrying paper size, the print-content fingerprint and the non-blank
    flag, plus the sets of all-blank side keys and retained-side keys.
    Retained signatures are indexed separately so references into them are
    rejected as input errors rather than treated as unknown, matching the
    binding-audit convention.
    """
    order = process_batch(proof_batch)
    sides = {}
    blank = set()
    retained = set()
    for edition in order['editions']:
        geometry = edition['geometry']
        paper = (geometry['sheet_width_um'], geometry['sheet_height_um'])
        for signature in edition['signatures']:
            for sheet in signature['sheets']:
                for side in _SIDES:
                    key = (edition['id'], signature['signature'],
                           sheet['sheet'], side)
                    if signature['source'] != 'fresh_print':
                        retained.add(key)
                        continue
                    slots = sheet['sides'][side]
                    nonblank = any(slot['logical_page'] is not None for slot in slots)
                    sides[key] = {'key': key, 'paper': paper,
                                  'fingerprint': _side_fingerprint(slots),
                                  'nonblank': nonblank}
                    if not nonblank:
                        blank.add(key)
    return sides, blank, retained


def _validate_orders(raw_orders):
    records = array(raw_orders, 1, 32)
    order_ids = []
    index = {}
    for record in records:
        fields(record, ('order_id', 'proof_batch'))
        order_id = ident(record['order_id'])
        if order_id in index:
            raise ValueError(f'duplicate order_id: {order_id}')
        # Each order is an independent v2 plan with its own v2 input; legacy
        # identities never cross orders. An illegal work order is an input error.
        sides, blank, retained = _collect_order_sides(record['proof_batch'])
        index[order_id] = {'sides': sides, 'blank': blank, 'retained': retained}
        order_ids.append(order_id)
    return order_ids, index


def _parse_side_ref(value, orders):
    """Resolve one ``{order_id, edition_id, signature, sheet, side}`` reference."""
    fields(value, ('order_id', 'edition_id', 'signature', 'sheet', 'side'))
    order_id = value['order_id']
    if not isinstance(order_id, str) or not isinstance(value['edition_id'], str) \
            or value['side'] not in _SIDES \
            or type(value['signature']) is not int or type(value['sheet']) is not int \
            or value['signature'] < 1 or value['sheet'] < 1:
        raise ValueError('invalid billing-side reference')
    entry = orders.get(order_id)
    if entry is None:
        raise ValueError(f'unknown order_id in billing reference: {order_id}')
    key = (value['edition_id'], value['signature'], value['sheet'], value['side'])
    if key in entry['retained']:
        raise ValueError(f'reference points into a retained legacy signature: {key!r}')
    if key not in entry['sides']:
        raise ValueError(f'unknown billing-side reference: {(order_id,) + key!r}')
    if key in entry['blank']:
        raise ValueError(f'all-blank side cannot be billed: {(order_id,) + key!r}')
    return (order_id,) + key, entry['sides'][key]


# ---------------------------------------------------------------- pool validation

def _same_plate(records):
    """Whether every side of one pool can really be printed by one plate."""
    first = records[0]
    paper = first['paper']
    fingerprint = first['fingerprint']
    return all(record['paper'] == paper and record['fingerprint'] == fingerprint
               for record in records[1:])


def _validate_pool_record(record, orders):
    fields(record, ('pool_id', 'total_fen', 'uses'))
    pool_id = ident(record['pool_id'])
    total_fen = integer(record['total_fen'], 0, _MAX_FEN)
    if not isinstance(record['uses'], list):
        raise ValueError(f'pool {pool_id}: uses must be a list')
    parsed = []
    seen = set()
    for ref in record['uses']:
        full_key, side_record = _parse_side_ref(ref, orders)
        if full_key in seen:
            raise ValueError(f'duplicate billing side inside pool {pool_id}')
        seen.add(full_key)
        parsed.append((full_key, side_record))
    if not parsed and total_fen != 0:
        raise ValueError(f'pool {pool_id}: empty uses are only legal as a '
                         'zero-fen revocation of an existing pool')
    if parsed and not _same_plate([item[1] for item in parsed]):
        raise ValueError(f'pool {pool_id} groups sides that cannot share one plate')
    return pool_id, total_fen, [item[0] for item in parsed]


# -------------------------------------------------------------------- apportionment

def _apportion(total_fen, face_counts):
    """Largest-remainder split of ``total_fen`` over participating orders.

    Every order first receives the integer part of its exact share; the
    leftover fen go to the largest exact remainders, ties broken by ASCII
    ascending order_id. All arithmetic is exact (``Fraction``); no float.
    """
    total_faces = sum(face_counts.values())
    exact = {order_id: Fraction(total_fen * count, total_faces)
             for order_id, count in face_counts.items()}
    base = {order_id: exact[order_id].numerator // exact[order_id].denominator
            for order_id in face_counts}
    leftover = total_fen - sum(base.values())
    # Largest remainders first; equal remainders break by ASCII order_id.
    ranked = sorted(face_counts, key=lambda oid: (-(exact[oid] - base[oid]), oid))
    for order_id in ranked[:leftover]:
        base[order_id] += 1
    if sum(base.values()) != total_fen:
        raise ValueError('internal: apportionment does not conserve fen')
    return base

def _current_payables(pool, order_ids):
    """Per-order current payable for one pool state.

    A revoked pool (or any pool without current uses) owes every order zero;
    orders that have left a still-active pool likewise get a zero current
    payable while their historical settlements stay on the books, which is
    what surfaces refunds as open differences.
    """
    if pool['revoked'] or not pool['uses']:
        return {order_id: 0 for order_id in order_ids}
    face_counts = {}
    for full_key in pool['uses']:
        face_counts[full_key[0]] = face_counts.get(full_key[0], 0) + 1
    split = _apportion(pool['total_fen'], face_counts)
    return {order_id: split.get(order_id, 0) for order_id in order_ids}


# ---------------------------------------------------------------------- the replay

def _process_events(raw_events, order_ids, orders):
    pools = {}  # pool_id -> current state dict; version starts at 1
    # (order_id, pool_id) -> signed fen already settled on that relationship.
    settled = {}
    statements = []
    used_statement_ids = set()
    used_event_ids = set()

    events = array(raw_events, 0, 2048)
    for event in events:
        if not isinstance(event, dict) or 'event_id' not in event or 'type' not in event:
            raise ValueError('each event requires event_id and type fields')
        if set(event) - {'event_id', 'type', 'pools', 'statement_id', 'order_id'}:
            raise ValueError('unexpected event field')
        if not isinstance(event['type'], str):
            raise ValueError('event type must be a string')
        event_id = ident(event['event_id'])
        if event_id in used_event_ids:
            raise ValueError(f'duplicate event_id: {event_id}')
        used_event_ids.add(event_id)

        if event['type'] == 'revise':
            fields(event, ('event_id', 'type', 'pools'))
            if not isinstance(event['pools'], list) or not event['pools']:
                raise ValueError('a revise event must carry one or more pool records')
            replacements = {}
            for item in event['pools']:
                pool_id, total_fen, uses = _validate_pool_record(item, orders)
                if pool_id in replacements:
                    raise ValueError(f'duplicate pool {pool_id} inside one revise event')
                if not uses and pool_id not in pools:
                    raise ValueError(f'pool {pool_id}: a new pool cannot open as an empty revocation')
                version = 1 if pool_id not in pools else pools[pool_id]['version'] + 1
                replacements[pool_id] = {'pool_id': pool_id, 'version': version,
                                         'total_fen': total_fen, 'uses': uses,
                                         'revoked': not uses}

            # Validate global occupancy against the fully replaced ledger, not
            # pool-by-pool in array order: a face may move between pools or two
            # pools may swap faces inside this one event.
            claimed = {}
            merged = {**pools, **replacements}
            for pool_id, pool in merged.items():
                for full_key in pool['uses']:
                    if full_key in claimed:
                        raise ValueError(
                            f'side {full_key!r} is claimed by more than one pool '
                            f'after the replacement: {claimed[full_key]} and {pool_id}')
                    claimed[full_key] = pool_id
            pools = merged

        elif event['type'] == 'close':
            fields(event, ('event_id', 'type', 'statement_id', 'order_id'))
            statement_id = ident(event['statement_id'])
            order_id = ident(event['order_id'])
            if statement_id in used_statement_ids:
                raise ValueError(f'duplicate statement_id: {statement_id}')
            used_statement_ids.add(statement_id)
            if order_id not in orders:
                raise ValueError(f'unknown order_id: {order_id}')

            # Snapshot the ledger at this point in the log. Only scalar values
            # are copied into the statement, so later pool replacements can
            # never rewrite a published statement.
            lines = []
            signed_total = 0
            for pool_id in sorted(pools):
                pool = pools[pool_id]
                payable_now = _current_payables(pool, order_ids)[order_id]
                before = settled.get((order_id, pool_id), 0)
                difference = payable_now - before
                if difference == 0:
                    continue  # only zero-difference lines are omitted; no netting
                lines.append({'pool_id': pool_id,
                              'pool_version': pool['version'],
                              'previously_settled_fen': before,
                              'payable_fen': payable_now,
                              'difference_fen': difference})
                settled[(order_id, pool_id)] = before + difference
                signed_total += difference
            statements.append({'statement_id': statement_id, 'order_id': order_id,
                               'event_id': event_id,
                               'signed_total_fen': signed_total, 'lines': lines})
        else:
            raise ValueError(f"unknown event type: {event['type']}")
    return pools, settled, statements


# ------------------------------------------------------------------------ report

def _reference_output(full_key):
    order_id, edition_id, signature, sheet, side = full_key
    return {'order_id': order_id, 'edition_id': edition_id,
            'signature': signature, 'sheet': sheet, 'side': side}


def _build_report(order_ids, orders, pools, settled, statements):
    pool_entries = []
    for pool_id in sorted(pools):
        pool = pools[pool_id]
        payables = _current_payables(pool, order_ids)
        face_counts = {}
        for full_key in pool['uses']:
            face_counts[full_key[0]] = face_counts.get(full_key[0], 0) + 1
        participants = [{'order_id': order_id,
                         'face_count': face_counts.get(order_id, 0),
                         'payable_fen': payables[order_id]}
                        for order_id in sorted(face_counts)]
        pool_entries.append({
            'pool_id': pool_id, 'version': pool['version'],
            'total_fen': pool['total_fen'], 'revoked': pool['revoked'],
            'uses': [_reference_output(full_key)
                     for full_key in sorted(pool['uses'])],
            'participants': participants,
        })

    outstanding = []
    for order_id in sorted(order_ids):
        entries = []
        for pool_id in sorted(pools):
            pool = pools[pool_id]
            payable_now = _current_payables(pool, order_ids)[order_id]
            already = settled.get((order_id, pool_id), 0)
            participates = any(full_key[0] == order_id for full_key in pool['uses'])
            # Keep the relationship visible while it carries current
            # participation, historical settlement, or an open balance; exited
            # pools with refunds still pending therefore never disappear.
            if not participates and already == 0 and payable_now == 0:
                continue
            entries.append({'pool_id': pool_id,
                            'payable_fen': payable_now,
                            'settled_fen': already,
                            'open_difference_fen': payable_now - already})
        outstanding.append({'order_id': order_id, 'pools': entries})

    # Conservation checks before anything is published.
    for entry in pool_entries:
        if sum(p['payable_fen'] for p in entry['participants']) != entry['total_fen']:
            raise ValueError('internal: apportionment does not conserve the supplier amount')
    open_lookup = {item['order_id']: {p['pool_id']: p for p in item['pools']}
                   for item in outstanding}
    for order_id in order_ids:
        for pool_id, pool in pools.items():
            payable_now = _current_payables(pool, order_ids)[order_id]
            already = settled.get((order_id, pool_id), 0)
            open_difference = open_lookup[order_id].get(pool_id, {}).get(
                'open_difference_fen', 0)
            if already + open_difference != payable_now:
                raise ValueError('internal: settled plus open must equal the current payable')

    return {'schema_version': 4,
            'orders': list(order_ids),
            'pools': pool_entries,
            'statements': statements,
            'outstanding': outstanding,
            'summary': {'orders': len(order_ids),
                        'pools': len(pool_entries),
                        'active_pools': sum(1 for entry in pool_entries
                                            if not entry['revoked']),
                        'revoked_pools': sum(1 for entry in pool_entries
                                             if entry['revoked']),
                        'statements': len(statements)}}


def process_settlement(raw):
    """Replay the plate-fee ledger and return the schema-version-4 report.

    Raises ValueError on any invalid input or failed conservation check; the
    CLI converts that into exit 2 and publishes no settlement result.
    """
    fields(raw, ('orders', 'events'))
    order_ids, orders = _validate_orders(raw['orders'])
    pools, settled, statements = _process_events(raw['events'], order_ids, orders)
    return _build_report(order_ids, orders, pools, settled, statements)
