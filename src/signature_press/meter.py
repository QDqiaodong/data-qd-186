"""Offline press-meter paper accounting (schema version 7).

The press hall's sheet counters only export intermittent readings of a
wrapping cumulative value, and the press operators' mounting records (which
fresh-print signature of which edition was on which machine during
``[start, end)``) often arrive later than the readings.  This mode
reconciles the two incremental evidence lanes into one archivable paper
slip per shift, per machine, per ``{edition_id, signature}`` key.  Sheets
counted across a job change or a shift handover are never prorated by time:
when the sealed evidence does not determine every key's exact sheet count
the shift stays pending instead of publishing a guess.

One account chain is initialised once (a fixed v2 proof batch, increasing
shift boundaries, and the duty machine configs with their per-second sheet
limits, counter moduli and second-0 initial readings) and then continued
any number of times.  Every call ingests one incremental package — new
readings, new mounting records and per-lane watermarks — and publishes one
versioned artifact holding the slips closed by this call, the diagnosis of
the first shift that cannot be closed (if any), and a self-contained
continuation state.  The state keeps no closed slips and no raw rows from
closed intervals; it keeps only per-machine boundary anchors, the rows
still ahead of the cutoff, and for a reading segment straddling the cutoff
the constraint left by the already archived part.  Continuing from the
latest state alone is identical to recomputing from the complete evidence.

Both evidence lanes are watermark-guarded: a readings watermark of ``w``
certifies that every reading with timestamp strictly below ``w`` has
arrived, a mountings watermark certifies every record starting strictly
below it.  New rows may be unordered and may run ahead of a watermark, but
they may never fall before the lane's previous watermark, and rows can
never be overwritten or retracted.  A reading segment ``(a, x) -> (b, y)``
only says the sheets fed in ``[a, b)`` are nonnegative and congruent to
``y - x`` modulo the counter modulus; zero sheets, one wrap or many wraps
are all a priori possible.  Whenever both lanes are complete over an
adjacent segment and no feasible consumption exists under the mounting
speed limits, the input itself is contradictory and rejected, even for
segments no pending shift touches.

All arithmetic is interval arithmetic over reading segments and mounting
intervals; nothing expands per second, per sheet or per possible counter
value, so a 10^9-second shift costs the same as a ten-second one.  Only
the Python standard library is used.
"""
from bisect import bisect_right
import copy

from .batch import process_batch
from .checks import array, fields, ident, integer, unique

_MAX_TIME = 10 ** 9
_MAX_ROWS_PACKAGE = 2000
_MAX_ROWS_TOTAL = 20_000

_VERIFICATION_NOTE = (
    'verify each segment against the raw counter export and mounting log: '
    '(right_reading[1] - left_reading[1]) mod modulus == residue; '
    'capacity == max_sheets_per_second x mounted seconds of the whole segment '
    '(idle seconds excluded); the segment total lies in '
    '[total_min, total_max] and is congruent to residue modulo modulus. '
    'A key\'s shift sheets are the sum of its per-segment contributions; '
    'archived_before was charged on earlier slips, carried_forward is the '
    'range reserved for later shifts, and archived_before + in_shift_sheets '
    '+ carried_forward decomposes the segment total.'
)


# ------------------------------------------------------------------ chain

def _validate_boundaries(value):
    boundaries = array(value, 2, 65)
    for boundary in boundaries:
        integer(boundary, 0, _MAX_TIME)
    if boundaries[0] != 0:
        raise ValueError('shift boundaries must start at 0')
    for earlier, later in zip(boundaries, boundaries[1:]):
        if earlier >= later:
            raise ValueError('shift boundaries must be strictly increasing')
    return boundaries


def _validate_machine_configs(value):
    machines = array(value, 1, 8)
    for machine in machines:
        fields(machine, ('id', 'max_sheets_per_second', 'modulus', 'initial_reading'))
        ident(machine['id'])
        integer(machine['max_sheets_per_second'], 1, 10_000)
        modulus = integer(machine['modulus'], 2, _MAX_TIME)
        integer(machine['initial_reading'], 0, modulus - 1)
    unique([machine['id'] for machine in machines])
    return machines


def _validate_fresh_list(value):
    entries = array(value, 0, 2)
    for entry in entries:
        fields(entry, ('edition_id', 'signatures'))
        ident(entry['edition_id'])
        signatures = array(entry['signatures'], 0, 512)
        for signature in signatures:
            integer(signature, 1, _MAX_TIME)
        unique(signatures)
    unique([entry['edition_id'] for entry in entries])
    return entries


def _fresh_set(chain):
    return {(entry['edition_id'], signature)
            for entry in chain['fresh_signatures']
            for signature in entry['signatures']}


def _validate_init(raw):
    fields(raw, ('proof_batch', 'shift_boundaries', 'machines'))
    order = process_batch(raw['proof_batch'])
    fresh = [{'edition_id': edition['id'],
              'signatures': [entry['signature'] for entry in edition['signatures']
                             if entry['source'] == 'fresh_print']}
             for edition in order['editions']]
    return {'shift_boundaries': _validate_boundaries(raw['shift_boundaries']),
            'machines': _validate_machine_configs(raw['machines']),
            'fresh_signatures': fresh}


def _initial_state(chain):
    return {'schema_version': 7, 'artifact_sequence': 0, 'chain': chain,
            'next_shift_index': 0, 'total_rows': 0,
            'machines': [{'id': machine['id'], 'reading_watermark': 0,
                          'mounting_watermark': 0,
                          'anchor': {'second': 0,
                                     'value': machine['initial_reading']},
                          'frontier': None, 'readings': [], 'mountings': []}
                         for machine in chain['machines']]}


def _validate_state(raw):
    """Strictly validate a continuation state produced by an earlier artifact."""
    fields(raw, ('schema_version', 'artifact_sequence', 'chain',
                 'next_shift_index', 'total_rows', 'machines'))
    if raw['schema_version'] != 7:
        raise ValueError('continuation state must have schema_version 7')
    integer(raw['artifact_sequence'], 1, 10 ** 12)
    chain = fields(raw['chain'], ('shift_boundaries', 'machines', 'fresh_signatures'))
    boundaries = _validate_boundaries(chain['shift_boundaries'])
    configs = _validate_machine_configs(chain['machines'])
    chain['fresh_signatures'] = _validate_fresh_list(chain['fresh_signatures'])
    horizon = boundaries[-1]
    next_index = integer(raw['next_shift_index'], 0, len(boundaries) - 1)
    cutoff = boundaries[next_index]
    total_rows = integer(raw['total_rows'], 0, _MAX_ROWS_TOTAL)
    fresh = _fresh_set(chain)
    machines = array(raw['machines'], len(configs), len(configs))
    kept_rows = 0
    for machine, config in zip(machines, configs):
        fields(machine, ('id', 'reading_watermark', 'mounting_watermark',
                         'anchor', 'frontier', 'readings', 'mountings'))
        if machine['id'] != config['id']:
            raise ValueError('state machines must follow the chain order')
        modulus = config['modulus']
        rate = config['max_sheets_per_second']
        integer(machine['reading_watermark'], cutoff, horizon + 1)
        integer(machine['mounting_watermark'], cutoff, horizon)
        anchor = fields(machine['anchor'], ('second', 'value'))
        integer(anchor['second'], 0, cutoff)
        integer(anchor['value'], 0, modulus - 1)
        frontier = machine['frontier']
        if frontier is not None:
            fields(frontier, ('left_second', 'left_value', 'right_second',
                              'right_value', 'mounted_seconds', 'archived'))
            integer(frontier['left_second'], 0, cutoff - 1)
            integer(frontier['left_value'], 0, modulus - 1)
            integer(frontier['right_second'], cutoff + 1, horizon)
            integer(frontier['right_value'], 0, modulus - 1)
            span = frontier['right_second'] - frontier['left_second']
            integer(frontier['mounted_seconds'], 0, span)
            integer(frontier['archived'], 0, rate * frontier['mounted_seconds'])
            if (frontier['left_second'], frontier['left_value']) != \
                    (anchor['second'], anchor['value']):
                raise ValueError('frontier left reading must equal the anchor')
            residue = (frontier['right_value'] - frontier['left_value']) % modulus
            if residue > rate * frontier['mounted_seconds']:
                raise ValueError('frontier segment contradicts the speed limit')
        elif anchor['second'] != cutoff:
            raise ValueError('anchor must sit on the cutoff when no frontier exists')
        readings = array(machine['readings'], 0, _MAX_ROWS_TOTAL)
        seconds = set()
        for reading in readings:
            fields(reading, ('second', 'value'))
            integer(reading['second'], cutoff + 1, horizon)
            integer(reading['value'], 0, modulus - 1)
            if reading['second'] in seconds:
                raise ValueError('duplicate reading second in state')
            seconds.add(reading['second'])
        readings.sort(key=lambda reading: reading['second'])
        if frontier is not None:
            if not readings or (readings[0]['second'], readings[0]['value']) != \
                    (frontier['right_second'], frontier['right_value']):
                raise ValueError('the frontier right reading must head the kept readings')
        mountings = array(machine['mountings'], 0, _MAX_ROWS_TOTAL)
        for record in mountings:
            fields(record, ('start', 'end', 'edition_id', 'signature'))
            integer(record['start'], cutoff, horizon)
            integer(record['end'], record['start'] + 1, horizon)
            ident(record['edition_id'])
            integer(record['signature'], 1, _MAX_TIME)
            if (record['edition_id'], record['signature']) not in fresh:
                raise ValueError('state mounting names a signature that is not a '
                                 'fresh-print signature of the work order')
        mountings.sort(key=lambda record: (record['start'], record['end']))
        for earlier, later in zip(mountings, mountings[1:]):
            if earlier['end'] > later['start']:
                raise ValueError('overlapping mounting records in state')
        kept_rows += len(readings) + len(mountings)
    if kept_rows > total_rows:
        raise ValueError('state keeps more rows than the batch row counter allows')
    return raw


# -------------------------------------------------------------- increments

def _apply_package(state, raw):
    chain = state['chain']
    horizon = chain['shift_boundaries'][-1]
    configs = {machine['id']: machine for machine in chain['machines']}
    machines = {machine['id']: machine for machine in state['machines']}
    watermarks = array(raw.get('watermarks', []), 0, 8)
    readings = array(raw.get('readings', []), 0, _MAX_ROWS_PACKAGE)
    mountings = array(raw.get('mountings', []), 0, _MAX_ROWS_PACKAGE)
    new_rows = len(readings) + len(mountings)
    if new_rows > _MAX_ROWS_PACKAGE:
        raise ValueError('at most 2000 new rows per package')
    if state['total_rows'] + new_rows > _MAX_ROWS_TOTAL:
        raise ValueError('at most 20000 input rows across the whole chain')

    updates = {}
    for row in watermarks:
        fields(row, ('machine',), ('readings', 'mountings'))
        machine_id = ident(row['machine'])
        if machine_id not in machines:
            raise ValueError(f'watermark names unknown machine {machine_id}')
        if machine_id in updates:
            raise ValueError(f'duplicate watermark row for machine {machine_id}')
        if 'readings' not in row and 'mountings' not in row:
            raise ValueError('watermark row must update at least one lane')
        machine = machines[machine_id]
        update = {}
        if 'readings' in row:
            value = integer(row['readings'], 0, horizon + 1)
            if value < machine['reading_watermark']:
                raise ValueError('readings watermark cannot decrease')
            update['readings'] = value
        if 'mountings' in row:
            value = integer(row['mountings'], 0, horizon)
            if value < machine['mounting_watermark']:
                raise ValueError('mountings watermark cannot decrease')
            update['mountings'] = value
        updates[machine_id] = update

    fresh = _fresh_set(chain)
    new_readings = {}
    for row in readings:
        fields(row, ('machine', 'second', 'value'))
        machine_id = ident(row['machine'])
        config = configs.get(machine_id)
        if config is None:
            raise ValueError(f'reading names unknown machine {machine_id}')
        machine = machines[machine_id]
        second = integer(row['second'], 1, horizon)
        value = integer(row['value'], 0, config['modulus'] - 1)
        if second < machine['reading_watermark']:
            raise ValueError('new reading falls before the previous readings watermark')
        new_readings.setdefault(machine_id, []).append({'second': second, 'value': value})

    new_mountings = {}
    for row in mountings:
        fields(row, ('machine', 'start', 'end', 'edition_id', 'signature'))
        machine_id = ident(row['machine'])
        if machine_id not in machines:
            raise ValueError(f'mounting names unknown machine {machine_id}')
        machine = machines[machine_id]
        start = integer(row['start'], 0, horizon)
        end = integer(row['end'], start + 1, horizon)
        if start < machine['mounting_watermark']:
            raise ValueError('new mounting falls before the previous mountings watermark')
        edition_id = ident(row['edition_id'])
        signature = integer(row['signature'], 1, _MAX_TIME)
        if (edition_id, signature) not in fresh:
            raise ValueError(f'mounting names {edition_id} signature {signature}, which is '
                             f'not a fresh-print signature of the work order')
        new_mountings.setdefault(machine_id, []).append(
            {'start': start, 'end': end, 'edition_id': edition_id,
             'signature': signature})

    for machine_id, update in updates.items():
        machine = machines[machine_id]
        if 'readings' in update:
            machine['reading_watermark'] = update['readings']
        if 'mountings' in update:
            machine['mounting_watermark'] = update['mountings']

    for machine_id, rows in new_readings.items():
        machine = machines[machine_id]
        known = {reading['second'] for reading in machine['readings']}
        known.add(machine['anchor']['second'])
        for row in rows:
            if row['second'] in known:
                raise ValueError(f'duplicate reading at second {row["second"]} on '
                                 f'machine {machine_id}')
            known.add(row['second'])
            machine['readings'].append(row)
        machine['readings'].sort(key=lambda reading: reading['second'])

    for machine_id, rows in new_mountings.items():
        machine = machines[machine_id]
        merged = machine['mountings'] + rows
        merged.sort(key=lambda record: (record['start'], record['end']))
        for earlier, later in zip(merged, merged[1:]):
            if earlier['end'] > later['start']:
                raise ValueError(f'overlapping mounting records on machine {machine_id}')
        machine['mountings'] = merged

    state['total_rows'] += new_rows


# -------------------------------------------------------------- arithmetic

def _mounted_seconds(records, ends, low, high):
    """Mounted seconds of ``records`` inside ``[low, high)``, total and per key."""
    total = 0
    per_key = {}
    index = bisect_right(ends, low)
    while index < len(records) and records[index]['start'] < high:
        record = records[index]
        left = record['start'] if record['start'] > low else low
        right = record['end'] if record['end'] < high else high
        if left < right:
            total += right - left
            key = (record['edition_id'], record['signature'])
            per_key[key] = per_key.get(key, 0) + right - left
        index += 1
    return total, per_key


def _check_contradictions(state):
    """Reject complete adjacent reading segments with no feasible consumption."""
    configs = {machine['id']: machine for machine in state['chain']['machines']}
    for machine in state['machines']:
        config = configs[machine['id']]
        modulus = config['modulus']
        rate = config['max_sheets_per_second']
        frontier = machine['frontier']
        if frontier is not None:
            previous = (frontier['right_second'], frontier['right_value'])
        else:
            previous = (machine['anchor']['second'], machine['anchor']['value'])
        ends = [record['end'] for record in machine['mountings']]
        for reading in machine['readings']:
            if reading['second'] <= previous[0]:
                continue
            right = reading['second']
            if right < machine['reading_watermark'] and \
                    right <= machine['mounting_watermark']:
                residue = (reading['value'] - previous[1]) % modulus
                capacity = rate * _mounted_seconds(machine['mountings'], ends,
                                                   previous[0], right)[0]
                if residue > capacity:
                    raise ValueError(
                        f'contradictory input: machine {machine["id"]} segment '
                        f'[{previous[0]},{right}) needs at least {residue} sheets '
                        f'modulo {modulus} but the mounting speed allows '
                        f'at most {capacity}')
            previous = (right, reading['value'])


# ------------------------------------------------------------- shift close

def _machine_problems(machine, config, ends, start, end):
    """Reading-segment problems covering ``[start, end)`` for one machine.

    Returns ``(problems, missing)``; ``missing`` describes the first
    completeness gap when the shift cannot be covered with sealed evidence.
    """
    rate = config['max_sheets_per_second']
    modulus = config['modulus']
    problems = []
    frontier = machine['frontier']
    if frontier is not None:
        residue = (frontier['right_value'] - frontier['left_value']) % modulus
        capacity_total = rate * frontier['mounted_seconds']
        max_total = residue + modulus * ((capacity_total - residue) // modulus)
        capacity = rate * _mounted_seconds(machine['mountings'], ends, start,
                                           frontier['right_second'])[0]
        low = (residue - frontier['archived']) % modulus
        high_bound = min(capacity, max_total - frontier['archived'])
        high = low + modulus * ((high_bound - low) // modulus)
        if high < low:
            raise ValueError('internal: frontier segment remainder is infeasible')
        problems.append({'a': start, 'b': frontier['right_second'],
                         'lo': low, 'hi': high, 'cap': capacity,
                         'left_reading': (frontier['left_second'],
                                          frontier['left_value']),
                         'right_reading': (frontier['right_second'],
                                           frontier['right_value']),
                         'archived_before': frontier['archived'],
                         'mounted_seconds': frontier['mounted_seconds'],
                         'residue_orig': residue, 'max_total_orig': max_total,
                         'cap_orig': capacity_total})
        previous = (frontier['right_second'], frontier['right_value'])
        index = 1  # the frontier right reading heads the kept readings
    else:
        previous = (machine['anchor']['second'], machine['anchor']['value'])
        index = 0
    readings = machine['readings']
    while previous[0] < end:
        if index >= len(readings):
            latest = readings[-1]['second'] if readings else machine['anchor']['second']
            return None, [{'machine': machine['id'], 'lane': 'readings',
                           'reason': 'missing_right_reading', 'shift_end': end,
                           'latest_reading': latest}]
        current = readings[index]
        index += 1
        right = current['second']
        if right >= machine['reading_watermark'] or \
                right > machine['mounting_watermark']:
            missing = []
            if right >= machine['reading_watermark']:
                missing.append({'machine': machine['id'], 'lane': 'readings',
                                'reason': 'readings_not_sealed',
                                'segment_right': right,
                                'reading_watermark': machine['reading_watermark']})
            if right > machine['mounting_watermark']:
                missing.append({'machine': machine['id'], 'lane': 'mountings',
                                'reason': 'mountings_not_sealed',
                                'segment_right': right,
                                'mounting_watermark': machine['mounting_watermark']})
            return None, missing
        residue = (current['value'] - previous[1]) % modulus
        mounted = _mounted_seconds(machine['mountings'], ends, previous[0], right)[0]
        capacity = rate * mounted
        high = residue + modulus * ((capacity - residue) // modulus)
        if high < residue:
            raise ValueError('internal: complete segment is infeasible')
        problems.append({'a': previous[0], 'b': right, 'lo': residue, 'hi': high,
                         'cap': capacity,
                         'left_reading': (previous[0], previous[1]),
                         'right_reading': (right, current['value']),
                         'archived_before': 0, 'mounted_seconds': mounted,
                         'residue_orig': residue, 'max_total_orig': high,
                         'cap_orig': capacity})
        previous = (right, current['value'])
    return problems, []


def _compute_keys(machine, config, ends, start, end, problems):
    """Per-key exact-or-interval shift totals over the covering problems.

    Within one problem the sheets of key ``K`` inside the shift are bounded
    by ``max(0, lo - (cap - inside_cap(K)))`` and ``min(inside_cap(K), hi)``:
    the segment total couples the inside with everything outside, so the
    same sheet can never be charged to both sides of the shift boundary.
    Problems are independent, so per-key shift bounds add up.
    """
    rate = config['max_sheets_per_second']
    keys = {}
    for problem in problems:
        _total, inside = _mounted_seconds(
            machine['mountings'], ends,
            max(problem['a'], start), min(problem['b'], end))
        contributions = {}
        for key, seconds in inside.items():
            inside_cap = rate * seconds
            low = max(0, problem['lo'] - (problem['cap'] - inside_cap))
            high = min(inside_cap, problem['hi'])
            contributions[key] = (low, high)
            bounds = keys.setdefault(key, [0, 0])
            bounds[0] += low
            bounds[1] += high
        problem['contrib'] = contributions
    return keys


def _carried_forward(machine, config, ends, problem, end):
    """Remaining total range of a segment that crosses the new cutoff."""
    if problem['b'] <= end:
        return {'min_sheets': 0, 'max_sheets': 0}
    modulus = config['modulus']
    rate = config['max_sheets_per_second']
    archived = problem['archived_before'] + \
        sum(low for low, _high in problem['contrib'].values())
    capacity = rate * _mounted_seconds(machine['mountings'], ends,
                                       end, problem['b'])[0]
    low = (problem['residue_orig'] - archived) % modulus
    high_bound = min(capacity, problem['max_total_orig'] - archived)
    return {'min_sheets': low,
            'max_sheets': low + modulus * ((high_bound - low) // modulus)}


def _build_slip(state, index, per_machine):
    boundaries = state['chain']['shift_boundaries']
    start, end = boundaries[index], boundaries[index + 1]
    machines = []
    total = 0
    for machine, config, problems, keys, ends in per_machine:
        segments = []
        for problem in problems:
            contributions = [{'edition_id': key[0], 'signature': key[1],
                              'sheets': low}
                             for key, (low, _high) in sorted(problem['contrib'].items())]
            in_shift = sum(item['sheets'] for item in contributions)
            segments.append({
                'left_reading': list(problem['left_reading']),
                'right_reading': list(problem['right_reading']),
                'residue': problem['residue_orig'],
                'capacity': problem['cap_orig'],
                'total_min': problem['residue_orig'],
                'total_max': problem['max_total_orig'],
                'archived_before': problem['archived_before'],
                'contributions': contributions,
                'in_shift_sheets': in_shift,
                'carried_forward': _carried_forward(machine, config, ends,
                                                    problem, end),
            })
        key_rows = [{'edition_id': key[0], 'signature': key[1], 'sheets': bounds[0]}
                    for key, bounds in sorted(keys.items())]
        machine_total = sum(row['sheets'] for row in key_rows)
        machines.append({'machine': machine['id'],
                         'max_sheets_per_second': config['max_sheets_per_second'],
                         'modulus': config['modulus'],
                         'keys': key_rows,
                         'machine_total': machine_total,
                         'segments': segments})
        total += machine_total
    return {'shift_index': index, 'shift_start': start, 'shift_end': end,
            'machines': machines, 'total_sheets': total,
            'verification_note': _VERIFICATION_NOTE}


def _advance(machine, end, problems):
    """Move one machine's frontier/anchor across the new cutoff and prune."""
    last = problems[-1]
    if last['b'] > end:
        archived = last['archived_before'] + \
            sum(low for low, _high in last['contrib'].values())
        machine['frontier'] = {'left_second': last['left_reading'][0],
                               'left_value': last['left_reading'][1],
                               'right_second': last['right_reading'][0],
                               'right_value': last['right_reading'][1],
                               'mounted_seconds': last['mounted_seconds'],
                               'archived': archived}
        machine['anchor'] = {'second': last['left_reading'][0],
                             'value': last['left_reading'][1]}
    else:
        machine['frontier'] = None
        machine['anchor'] = {'second': end, 'value': last['right_reading'][1]}
    machine['readings'] = [reading for reading in machine['readings']
                           if reading['second'] > end]
    mountings = []
    for record in machine['mountings']:
        if record['end'] <= end:
            continue
        if record['start'] < end:
            record = dict(record, start=end)
        mountings.append(record)
    machine['mountings'] = mountings


def _ambiguous(index, start, end, per_machine):
    keys_out = []
    causes = []
    for machine, _config, problems, keys, _ends in per_machine:
        for key, bounds in sorted(keys.items()):
            keys_out.append({'machine': machine['id'], 'edition_id': key[0],
                             'signature': key[1], 'min_sheets': bounds[0],
                             'max_sheets': bounds[1]})
        for problem in problems:
            loose = [{'edition_id': key[0], 'signature': key[1],
                      'min_sheets': low, 'max_sheets': high}
                     for key, (low, high) in sorted(problem['contrib'].items())
                     if low < high]
            if loose:
                causes.append({'machine': machine['id'],
                               'left_reading': list(problem['left_reading']),
                               'right_reading': list(problem['right_reading']),
                               'loose_keys': loose})
    return {'kind': 'ambiguous', 'shift_index': index, 'shift_start': start,
            'shift_end': end, 'keys': keys_out, 'causes': causes}


def _close(state):
    chain = state['chain']
    boundaries = chain['shift_boundaries']
    configs = {machine['id']: machine for machine in chain['machines']}
    slips = []
    diagnosis = None
    while state['next_shift_index'] < len(boundaries) - 1:
        index = state['next_shift_index']
        start, end = boundaries[index], boundaries[index + 1]
        per_machine = []
        missing = []
        for machine in state['machines']:
            config = configs[machine['id']]
            ends = [record['end'] for record in machine['mountings']]
            problems, gaps = _machine_problems(machine, config, ends, start, end)
            if gaps:
                missing.extend(gaps)
                continue
            keys = _compute_keys(machine, config, ends, start, end, problems)
            per_machine.append((machine, config, problems, keys, ends))
        if missing:
            diagnosis = {'kind': 'incomplete', 'shift_index': index,
                         'shift_start': start, 'shift_end': end,
                         'missing': missing}
            break
        tight = all(low == high
                    for _machine, _config, _problems, keys, _ends in per_machine
                    for low, high in keys.values())
        if not tight:
            diagnosis = _ambiguous(index, start, end, per_machine)
            break
        slips.append(_build_slip(state, index, per_machine))
        for machine, _config, problems, _keys, _ends in per_machine:
            _advance(machine, end, problems)
        state['next_shift_index'] += 1
    return slips, diagnosis


# ------------------------------------------------------------------ entry

def process_meter(raw):
    """Ingest one incremental package and publish the versioned artifact."""
    fields(raw, (), ('init', 'state', 'watermarks', 'readings', 'mountings'))
    has_init = 'init' in raw
    has_state = 'state' in raw
    if has_init == has_state:
        raise ValueError('exactly one of init and state is required')
    # Deep-copy everything the artifact may keep: a published continuation
    # must stay usable for alternative continuations and never alias the
    # caller's package.
    if has_init:
        state = _initial_state(_validate_init(copy.deepcopy(raw['init'])))
    else:
        state = _validate_state(copy.deepcopy(raw['state']))
    _apply_package(state, raw)
    _check_contradictions(state)
    slips, diagnosis = _close(state)
    state['artifact_sequence'] += 1
    boundaries = state['chain']['shift_boundaries']
    return {
        'schema_version': 7,
        'artifact_sequence': state['artifact_sequence'],
        'new_slips': slips,
        'diagnosis': diagnosis,
        'continuation': state,
        'summary': {
            'artifact_sequence': state['artifact_sequence'],
            'new_slips': len(slips),
            'issued_shifts_total': state['next_shift_index'],
            'shifts_total': len(boundaries) - 1,
            'diagnosis': diagnosis['kind'] if diagnosis is not None else 'none',
            'sheets_this_call': sum(slip['total_sheets'] for slip in slips),
        },
    }
