"""Offline paper-lot recall for executed v2 work orders (schema version 6).

A paper supplier recalls one or more physical print lots.  This module
reconciles that recall against the same offline ledger the press already
uses: the executed v2 work order fixes every edition's current signature
numbers and its fresh-print / retained split, ``print_lots`` say which
physical pieces a lot supplied for one fresh signature, ``bind_spans`` say
which piece went into which copy of which book, ``pack_spans`` say which
copies went into which carton and ``dispatches`` say which whole cartons have
left the factory.  Recall data can never change the work order.

Everything is interval arithmetic.  A bind span maps a contiguous copy range
to a contiguous physical-piece range at step +1 or -1, and a carton maps to a
contiguous copy range for the same reason.  No report, intermediate set or
loop ever expands one entry per copy or per carton, so a 10^9-copy,
10^8-carton recall costs the same as a sixteen-copy one.

The report separates three actions that must not be confused:

* pieces still in the signature stack (affected pieces no bind span consumed),
* whole cartons still in the factory that contain at least one affected book
  (a carton is held only because of an affected book, never because a good
  book happened to share a held carton), and
* per-consignee dispatched cartons to locate plus the truly affected copy
  numbers inside them.

Truly affected copies are counted once even when two signatures affect the
same copy; both incoming-material provenance records are kept.

Only the Python standard library is used.
"""
from collections import defaultdict

from .batch import process_batch
from .checks import array, fields, ident, integer

_MAX_CODE = 10 ** 9
_MAX_ROWS = 20_000


# --------------------------------------------------------------- interval sets
#
# A set of integers is kept as a sorted tuple list of disjoint, inclusive and
# never-adjacent ``(first, last)`` intervals.  Every operation below stays in
# interval space; adding 10^9 consecutive codes is one tuple, not 10^9 ints.

def _merge(items):
    result = []
    for first, last in sorted(items):
        if first > last:
            continue
        if result and first <= result[-1][1] + 1:
            if last > result[-1][1]:
                result[-1] = (result[-1][0], last)
        else:
            result.append((first, last))
    return result


def _union(left, right):
    return _merge(list(left) + list(right))


def _intersect(left, right):
    result = []
    i = j = 0
    while i < len(left) and j < len(right):
        first = max(left[i][0], right[j][0])
        last = min(left[i][1], right[j][1])
        if first <= last:
            result.append((first, last))
        if left[i][1] < right[j][1]:
            i += 1
        else:
            j += 1
    return result


def _subtract(left, right):
    result = []
    j = 0
    for first, last in left:
        current = first
        while j < len(right) and right[j][1] < current:
            j += 1
        k = j
        while k < len(right) and right[k][0] <= last:
            if right[k][0] > current:
                result.append((current, right[k][0] - 1))
            if right[k][1] + 1 > current:
                current = right[k][1] + 1
            if current > last:
                break
            k += 1
        if current <= last:
            result.append((current, last))
    return result


def _size(intervals):
    return sum(last - first + 1 for first, last in intervals)


def _intersect_range(intervals, first, last):
    """Intersection of a sorted interval set with one inclusive range.

    ``intervals`` is disjoint and ascending, so a binary search finds the
    first interval that could touch ``first`` and the scan stops as soon as
    an interval starts beyond ``last``.
    """
    result = []
    low, high = 0, len(intervals)
    while low < high:
        middle = (low + high) // 2
        if intervals[middle][1] < first:
            low = middle + 1
        else:
            high = middle
    index = low
    while index < len(intervals) and intervals[index][0] <= last:
        item_first = max(first, intervals[index][0])
        item_last = min(last, intervals[index][1])
        if item_first <= item_last:
            result.append((item_first, item_last))
        index += 1
    return result


def _subtract_range(intervals, first, last):
    """Parts of one inclusive range that lie outside a sorted interval set."""
    result = []
    current = first
    for hit_first, hit_last in _intersect_range(intervals, first, last):
        if hit_first > current:
            result.append((current, hit_first - 1))
        current = max(current, hit_last + 1)
        if current > last:
            break
    if current <= last:
        result.append((current, last))
    return result


def _covered(intervals, first, last):
    """Whether the inclusive range lies entirely inside one interval set."""
    low, high = 0, len(intervals)
    while low < high:
        middle = (low + high) // 2
        if intervals[middle][0] <= first:
            low = middle + 1
        else:
            high = middle
    index = low - 1
    return index >= 0 and intervals[index][1] >= last


# ------------------------------------------------------------ ledger validation

def _code(value):
    return integer(value, 1, _MAX_CODE)


def _step(value):
    if type(value) is not int or value not in (-1, 1):
        raise ValueError('piece/copy step must be +1 or -1')
    return value


def _progression(start, count, step):
    """Inclusive ``(first, last)`` of ``start, start+step, ...`` ``count`` deep."""
    end = start + (count - 1) * step
    return (end, start) if step < 0 else (start, end)


def _validate_copies(rows, editions):
    records = array(rows, 1, _MAX_ROWS)
    counts = {}
    for row in records:
        fields(row, ('edition_id', 'count'))
        edition_id = ident(row['edition_id'])
        if edition_id in counts:
            raise ValueError(f'duplicate copies row for edition {edition_id}')
        if edition_id not in editions:
            raise ValueError(f'copies row names unknown edition {edition_id}')
        counts[edition_id] = integer(row['count'], 1, _MAX_CODE)
    if set(counts) != set(editions):
        missing = sorted(set(editions) - set(counts))
        raise ValueError(f'copies rows missing editions: {missing}')
    return counts


def _validate_lots(rows, fresh_signatures):
    records = array(rows, 0, _MAX_ROWS)
    lots = {}
    for row in records:
        fields(row, ('lot_id', 'edition_id', 'signature', 'count'))
        lot_id = ident(row['lot_id'])
        if lot_id in lots:
            raise ValueError(f'duplicate lot_id: {lot_id}')
        edition_id = ident(row['edition_id'])
        signature = integer(row['signature'], 1, _MAX_CODE)
        count = integer(row['count'], 1, _MAX_CODE)
        if (edition_id, signature) not in fresh_signatures:
            raise ValueError(f'lot {lot_id} names a signature that is not a fresh-print '
                             f'signature of its edition')
        lots[lot_id] = {'lot_id': lot_id, 'edition_id': edition_id,
                        'signature': signature, 'count': count}
    return lots


def _validate_bindings(rows, lots, counts, fresh_signatures):
    records = array(rows, 0, _MAX_ROWS)
    spans = []
    by_lot = defaultdict(list)
    used_pieces = defaultdict(list)
    supplied = defaultdict(list)
    supplied_size = defaultdict(int)
    for row in records:
        fields(row, ('lot_id', 'first_piece', 'first_copy', 'count', 'piece_step'))
        lot_id = ident(row['lot_id'])
        lot = lots.get(lot_id)
        if lot is None:
            raise ValueError(f'bind span names unknown lot {lot_id}')
        first_piece = _code(row['first_piece'])
        first_copy = _code(row['first_copy'])
        count = _code(row['count'])
        piece_step = _step(row['piece_step'])
        pieces = _progression(first_piece, count, piece_step)
        if pieces[0] < 1 or pieces[1] > lot['count']:
            raise ValueError(f'bind span of lot {lot_id} exceeds the lot piece range')
        copies = (first_copy, first_copy + count - 1)
        edition_count = counts[lot['edition_id']]
        if copies[1] > edition_count:
            raise ValueError(f'bind span of lot {lot_id} exceeds the edition copy count')
        span = {'lot_id': lot_id, 'edition_id': lot['edition_id'],
                'signature': lot['signature'], 'first_piece': first_piece,
                'first_copy': first_copy, 'count': count,
                'piece_step': piece_step, 'pieces': pieces, 'copies': copies}
        spans.append(span)
        by_lot[lot_id].append(span)
        used_pieces[lot_id].append(pieces)
        supplied[(span['edition_id'], span['signature'])].append(copies)
        supplied_size[(span['edition_id'], span['signature'])] += count

    # A physical piece of one lot may be issued at most once, even when the
    # same lot fed several bind runs.
    for lot_id, intervals in used_pieces.items():
        merged = _merge(intervals)
        if _size(merged) != sum(last - first + 1 for first, last in intervals):
            raise ValueError(f'lot {lot_id} reuses a physical piece')
    # Every fresh signature must be fed exactly once for every copy.
    for key in fresh_signatures:
        edition_id, _signature = key
        merged = _merge(supplied.get(key, []))
        if merged != [(1, counts[edition_id])] or \
                supplied_size.get(key, 0) != counts[edition_id]:
            raise ValueError(f'fresh signature {key!r} is not fed exactly once for '
                             f'every copy')
    return spans, by_lot, used_pieces


def _validate_packing(rows, counts):
    records = array(rows, 0, _MAX_ROWS)
    packs = []
    packed_size = defaultdict(int)
    packed_intervals = defaultdict(list)
    carton_ranges = defaultdict(list)
    for row in records:
        fields(row, ('edition_id', 'first_copy', 'copy_step', 'carton_series',
                     'first_carton', 'carton_count', 'books_per_carton'))
        edition_id = ident(row['edition_id'])
        if edition_id not in counts:
            raise ValueError(f'pack span names unknown edition {edition_id}')
        first_copy = _code(row['first_copy'])
        copy_step = _step(row['copy_step'])
        series = ident(row['carton_series'])
        first_carton = _code(row['first_carton'])
        carton_count = _code(row['carton_count'])
        books_per_carton = _code(row['books_per_carton'])
        total_books = carton_count * books_per_carton
        copies = _progression(first_copy, total_books, copy_step)
        if copies[0] < 1 or copies[1] > counts[edition_id]:
            raise ValueError(f'pack span of edition {edition_id} exceeds the copy range')
        last_carton = first_carton + carton_count - 1
        if last_carton > _MAX_CODE:
            raise ValueError('pack span exceeds the legal carton range')
        pack = {'edition_id': edition_id, 'first_copy': first_copy,
                'copy_step': copy_step, 'carton_series': series,
                'first_carton': first_carton, 'carton_count': carton_count,
                'books_per_carton': books_per_carton,
                'first_carton_last': last_carton, 'copies': copies}
        packs.append(pack)
        packed_intervals[edition_id].append(copies)
        packed_size[edition_id] += total_books
        carton_ranges[series].append((first_carton, last_carton))

    for edition_id, expected in counts.items():
        merged = _merge(packed_intervals.get(edition_id, []))
        if merged != [(1, expected)] or packed_size.get(edition_id, 0) != expected:
            raise ValueError(f'edition {edition_id} is not packed exactly once for '
                             f'every copy')
    # Inside one carton series every carton number is defined by one row.
    for series, intervals in carton_ranges.items():
        merged = _merge(intervals)
        if _size(merged) != sum(last - first + 1 for first, last in intervals):
            raise ValueError(f'carton series {series} defines a carton twice')
    return packs, carton_ranges


def _validate_dispatches(rows, carton_ranges):
    records = array(rows, 0, _MAX_ROWS)
    defined = {series: _merge(intervals)
               for series, intervals in carton_ranges.items()}
    per_series = defaultdict(list)
    by_series = defaultdict(list)
    for row in records:
        fields(row, ('carton_series', 'first_carton', 'last_carton', 'consignee_id'))
        series = ident(row['carton_series'])
        first_carton = _code(row['first_carton'])
        last_carton = _code(row['last_carton'])
        if first_carton > last_carton:
            raise ValueError('dispatch range has first_carton above last_carton')
        consignee_id = ident(row['consignee_id'])
        if not _covered(defined.get(series, []), first_carton, last_carton):
            raise ValueError(f'dispatch range crosses a carton that does not exist in '
                             f'series {series}')
        record = {'carton_series': series, 'first_carton': first_carton,
                  'last_carton': last_carton, 'consignee_id': consignee_id}
        per_series[series].append((first_carton, last_carton))
        by_series[series].append(record)
    intervals = {}
    for series, ranges in per_series.items():
        merged = _merge(ranges)
        if _size(merged) != sum(last - first + 1 for first, last in ranges):
            raise ValueError(f'a carton in series {series} is dispatched twice')
        intervals[series] = merged
    return intervals, by_series


def _validate_alerts(rows, lots):
    records = array(rows, 0, _MAX_ROWS)
    alerts = defaultdict(list)
    for row in records:
        fields(row, ('lot_id', 'first_piece', 'last_piece'))
        lot_id = ident(row['lot_id'])
        lot = lots.get(lot_id)
        if lot is None:
            raise ValueError(f'alert names unknown lot {lot_id}')
        first_piece = _code(row['first_piece'])
        last_piece = _code(row['last_piece'])
        if first_piece > last_piece:
            raise ValueError('alert first_piece above last_piece')
        if last_piece > lot['count']:
            raise ValueError(f'alert for lot {lot_id} exceeds the lot piece count')
        alerts[lot_id].append((first_piece, last_piece))
    return {lot_id: _merge(intervals) for lot_id, intervals in alerts.items()}


# ------------------------------------------------------------------- geometry

def _cartons_for_copies(pack, first, last):
    """Carton interval holding an affected copy interval inside one pack row."""
    books = pack['books_per_carton']
    base = pack['first_copy']
    if pack['copy_step'] == 1:
        low = (first - base) // books
        high = (last - base) // books
    else:
        low = (base - last) // books
        high = (base - first) // books
    series_last = pack['first_carton_last']
    return (pack['first_carton'] + low, min(pack['first_carton'] + high, series_last))


def _copies_for_cartons(pack, first_carton, last_carton):
    """Contiguous copy interval of a carton interval inside one pack row."""
    books = pack['books_per_carton']
    base = pack['first_copy']
    offset = pack['first_carton']
    if pack['copy_step'] == 1:
        first = base + (first_carton - offset) * books
        last = base + (last_carton - offset) * books + books - 1
    else:
        first = base - ((last_carton - offset) * books + books - 1)
        last = base - (first_carton - offset) * books
    return (first, last)


# ------------------------------------------------------------------ the recall

def _work_order(raw):
    order = process_batch(raw)
    editions = []
    fresh = set()
    for edition in order['editions']:
        numbers = []
        retained = False
        for signature in edition['signatures']:
            if signature['source'] == 'fresh_print':
                numbers.append(signature['signature'])
                fresh.add((edition['id'], signature['signature']))
            else:
                retained = True
        editions.append({'id': edition['id'], 'fresh': numbers,
                         'retained': retained})
    return editions, fresh


def _provenance_entries(editions, alerts, spans_by_lot):
    """Affected copies per edition plus the incoming-material evidence.

    Spans of one lot cover disjoint physical-piece intervals and merged alerts
    are disjoint too, so a two-pointer sweep per lot intersects both families
    in linear time.  For one lot/signature/direction each contiguous run of
    affected copies is reported with the matching physical-piece interval.
    The piece interval is always written ascending; ``piece_step`` says whether
    copy numbers grow with (+1) or against (-1) piece numbers.
    """
    affected = defaultdict(list)
    evidence = {}
    edition_rank = {edition['id']: index for index, edition in enumerate(editions)}
    for lot_id in sorted(alerts):
        ranges = alerts[lot_id]
        spans = sorted(spans_by_lot.get(lot_id, []),
                       key=lambda span: (span['pieces'][0], span['pieces'][1]))
        index = 0
        for span in spans:
            low, high = span['pieces']
            while index < len(ranges) and ranges[index][1] < low:
                index += 1
            cursor = index
            while cursor < len(ranges) and ranges[cursor][0] <= high:
                piece_low = max(low, ranges[cursor][0])
                piece_high = min(high, ranges[cursor][1])
                if piece_low <= piece_high:
                    step = span['piece_step']
                    if step == 1:
                        copy_first = span['first_copy'] + (piece_low - span['first_piece'])
                        copy_last = span['first_copy'] + (piece_high - span['first_piece'])
                    else:
                        copy_first = span['first_copy'] + (span['first_piece'] - piece_high)
                        copy_last = span['first_copy'] + (span['first_piece'] - piece_low)
                    affected[span['edition_id']].append((copy_first, copy_last))
                    key = (edition_rank[span['edition_id']], span['signature'],
                           lot_id, step)
                    evidence.setdefault(key, []).append({
                        'copy_first': copy_first, 'copy_last': copy_last,
                        'piece_first': piece_low, 'piece_last': piece_high})
                cursor += 1
    affected = {edition_id: _merge(intervals)
                for edition_id, intervals in affected.items()}
    merged_evidence = {}
    for key in sorted(evidence):
        step = key[3]
        records = sorted(evidence[key],
                         key=lambda item: (item['copy_first'], item['copy_last'],
                                           item['piece_first']))
        runs = []
        for record in records:
            if runs:
                previous = runs[-1]
                contiguous_copies = previous['copy_last'] + 1 == record['copy_first']
                if step == 1:
                    contiguous_pieces = previous['piece_last'] + 1 == record['piece_first']
                else:
                    contiguous_pieces = previous['piece_first'] - 1 == record['piece_last']
                if contiguous_copies and contiguous_pieces:
                    previous['copy_last'] = record['copy_last']
                    previous['piece_first'] = min(previous['piece_first'],
                                                  record['piece_first'])
                    previous['piece_last'] = max(previous['piece_last'],
                                                 record['piece_last'])
                    continue
            runs.append(dict(record))
        merged_evidence[key] = runs
    return affected, merged_evidence


def _pack_analysis(packs, carton_ranges, dispatch_intervals, dispatch_records,
                   affected):
    """Held factory cartons, held-good copies, and dispatched affected copies.

    Cartons of one series defined by different pack rows never overlap, so a
    single carton-ordered sweep per series intersects the affected-carton
    ranges with the dispatched segments in linear time.  A carton is held only
    when it still is in the factory and holds at least one affected book;
    good copies inside such a held carton are reported separately and never
    cause another carton to be held.
    """
    factory = {}
    for series, defined in carton_ranges.items():
        factory[series] = _subtract(_merge(defined),
                                    dispatch_intervals.get(series, []))
    held_per_series = defaultdict(list)
    held_good = defaultdict(list)
    in_factory = defaultdict(list)
    dispatched = defaultdict(list)
    consignee_cartons = defaultdict(lambda: defaultdict(list))
    consignee_copies = defaultdict(lambda: defaultdict(list))

    rows_by_series = defaultdict(list)
    for pack in packs:
        rows_by_series[pack['carton_series']].append(pack)
    for series in rows_by_series:
        rows_by_series[series].sort(key=lambda pack: pack['first_carton'])

    for series, rows in rows_by_series.items():
        dispatched_ranges = sorted(
            ((record['first_carton'], record['last_carton'], record['consignee_id'])
             for record in dispatch_records.get(series, [])),
            key=lambda item: (item[0], item[1]))
        cursor = 0
        for pack in rows:
            edition_id = pack['edition_id']
            edition_affected = affected.get(edition_id, [])
            hit = _intersect_range(edition_affected, pack['copies'][0],
                                   pack['copies'][1])
            affected_cartons = _merge(
                [_cartons_for_copies(pack, first, last) for first, last in hit])
            held = _intersect(affected_cartons, factory[series])
            held_copies = []
            for first, last in held:
                held_copies.append(_copies_for_cartons(pack, first, last))
            held_copies = _merge(held_copies)
            for first, last in held:
                held_per_series[series].append((first, last))
            for first, last in held_copies:
                in_factory[edition_id].extend(
                    _intersect_range(edition_affected, first, last))
                held_good[edition_id].extend(
                    _subtract_range(edition_affected, first, last))

            # Advance to the first dispatched segment that could still touch
            # this row; rows and dispatch segments are both carton-ordered.
            pack_low = pack['first_carton']
            while cursor < len(dispatched_ranges) and \
                    dispatched_ranges[cursor][1] < pack_low:
                cursor += 1
            position = cursor
            while position < len(dispatched_ranges) and \
                    dispatched_ranges[position][0] <= pack['first_carton_last']:
                dispatch_low, dispatch_high, consignee = dispatched_ranges[position]
                shared = _intersect(affected_cartons,
                                    [(dispatch_low, dispatch_high)])
                for first, last in shared:
                    consignee_cartons[consignee][series].append((first, last))
                    copies = _copies_for_cartons(pack, first, last)
                    recalled = _intersect_range(edition_affected, copies[0], copies[1])
                    if recalled:
                        dispatched[edition_id].extend(recalled)
                        consignee_copies[consignee][edition_id].extend(recalled)
                position += 1

    return {
        'held_cartons': {series: _merge(intervals)
                         for series, intervals in held_per_series.items()},
        'held_good': {edition_id: _merge(intervals)
                      for edition_id, intervals in held_good.items()},
        'in_factory': {edition_id: _merge(intervals)
                       for edition_id, intervals in in_factory.items()},
        'dispatched': {edition_id: _merge(intervals)
                       for edition_id, intervals in dispatched.items()},
        'consignee_cartons': {consignee: {series: _merge(intervals)
                                          for series, intervals in by_series.items()}
                              for consignee, by_series in consignee_cartons.items()},
        'consignee_copies': {consignee: {edition_id: _merge(intervals)
                                         for edition_id, intervals in by_edition.items()}
                             for consignee, by_edition in consignee_copies.items()},
    }


def _ranges(intervals):
    return [{'first': first, 'last': last} for first, last in intervals]


def _build_report(editions, counts, lots, alerts, used_pieces, affected, evidence,
                  packing):
    report_editions = []
    total_affected = total_in_factory = total_dispatched = 0
    total_good = 0
    by_rank = defaultdict(list)
    for key, records in evidence.items():
        by_rank[key[0]].append((key, records))
    for index, edition in enumerate(editions):
        edition_id = edition['id']
        copies = affected.get(edition_id, [])
        in_factory = packing['in_factory'].get(edition_id, [])
        dispatched = packing['dispatched'].get(edition_id, [])
        good = packing['held_good'].get(edition_id, [])
        # The affected set must split cleanly into factory and dispatched halves.
        if _intersect(in_factory, dispatched) or \
                _union(in_factory, dispatched) != copies:
            raise ValueError(f'internal: affected copies of edition {edition_id} '
                             f'do not split into factory and dispatched parts')
        if _intersect(good, copies):
            raise ValueError('internal: held-good copies overlap affected copies')
        provenance = []
        for (rank, signature, lot_id, step), records in by_rank.get(index, ()):
            for record in records:
                provenance.append({
                    'edition_id': edition_id,
                    'signature': signature,
                    'lot_id': lot_id,
                    'copy_first': record['copy_first'],
                    'copy_last': record['copy_last'],
                    'piece_first': record['piece_first'],
                    'piece_last': record['piece_last'],
                    'piece_step': step,
                })
        provenance.sort(key=lambda item: (item['signature'], item['copy_first'],
                                          item['lot_id'], item['piece_step']))
        report_editions.append({
            'edition_id': edition_id,
            'copy_count': counts[edition_id],
            'affected_copies': _ranges(copies),
            'in_factory_affected': _ranges(in_factory),
            'dispatched_affected': _ranges(dispatched),
            'held_good_copies': _ranges(good),
            'incoming_evidence': provenance,
        })
        total_affected += _size(copies)
        total_in_factory += _size(in_factory)
        total_dispatched += _size(dispatched)
        total_good += _size(good)

    unbound = []
    total_unbound = 0
    for lot_id in sorted(alerts):
        remaining = _subtract(alerts[lot_id], _merge(used_pieces.get(lot_id, [])))
        if not remaining:
            continue
        lot = lots[lot_id]
        unbound.append({'lot_id': lot_id, 'edition_id': lot['edition_id'],
                        'signature': lot['signature'],
                        'pieces': [{'first_piece': first, 'last_piece': last}
                                   for first, last in remaining]})
        total_unbound += _size(remaining)

    held_cartons = [{'carton_series': series,
                     'ranges': [{'first_carton': first, 'last_carton': last}
                                for first, last in intervals]}
                    for series, intervals in sorted(packing['held_cartons'].items())
                    if intervals]
    total_held_cartons = sum(_size(intervals)
                             for intervals in packing['held_cartons'].values())

    rank = {edition['id']: index for index, edition in enumerate(editions)}
    consignees = []
    total_recalled = 0
    for consignee in sorted(packing['consignee_copies']):
        cartons = [{'carton_series': series,
                    'ranges': [{'first_carton': first, 'last_carton': last}
                               for first, last in intervals]}
                   for series, intervals in
                   sorted(packing['consignee_cartons'][consignee].items())
                   if intervals]
        by_edition = []
        for edition_id in sorted(packing['consignee_copies'][consignee],
                                 key=lambda value: (rank[value], value)):
            copies = packing['consignee_copies'][consignee][edition_id]
            if not copies:
                continue
            by_edition.append({'edition_id': edition_id,
                               'recall_copies': _ranges(copies)})
            total_recalled += _size(copies)
        consignees.append({'consignee_id': consignee, 'cartons': cartons,
                           'editions': by_edition})

    if total_recalled != total_dispatched:
        raise ValueError('internal: consignee recall counts do not match dispatched '
                         'affected copies')
    if total_affected != total_in_factory + total_dispatched:
        raise ValueError('internal: affected copies do not conserve')

    return {
        'schema_version': 6,
        'editions': report_editions,
        'unbound_pieces': unbound,
        'held_cartons': held_cartons,
        'consignees': consignees,
        'summary': {
            'affected_copies': total_affected,
            'in_factory_affected_copies': total_in_factory,
            'dispatched_affected_copies': total_dispatched,
            'held_good_copies': total_good,
            'held_cartons': total_held_cartons,
            'recalled_copies': total_recalled,
            'unbound_pieces': total_unbound,
            'consignees': len(consignees),
        },
    }


def process_recall(raw):
    """Reconcile a paper-lot recall with the offline ledger (schema version 6)."""
    fields(raw, ('proof_batch', 'copies', 'print_lots', 'bind_spans',
                 'pack_spans', 'dispatches', 'alerts'))
    editions, fresh = _work_order(raw['proof_batch'])
    edition_ids = [edition['id'] for edition in editions]
    counts = _validate_copies(raw['copies'], edition_ids)
    for edition in editions:
        if edition['retained'] and counts[edition['id']] != 1:
            raise ValueError(f'edition {edition["id"]} reused a retained signature and '
                             f'therefore has exactly one copy')
    lots = _validate_lots(raw['print_lots'], fresh)
    _spans, spans_by_lot, used_pieces = _validate_bindings(
        raw['bind_spans'], lots, counts, fresh)
    packs, carton_ranges = _validate_packing(raw['pack_spans'], counts)
    dispatch_intervals, dispatched_by_series = _validate_dispatches(
        raw['dispatches'], carton_ranges)
    alerts = _validate_alerts(raw['alerts'], lots)
    affected, evidence = _provenance_entries(editions, alerts, spans_by_lot)
    packing = _pack_analysis(packs, carton_ranges, dispatch_intervals,
                             dispatched_by_series, affected)
    return _build_report(editions, counts, lots, alerts, used_pieces, affected,
                         evidence, packing)
