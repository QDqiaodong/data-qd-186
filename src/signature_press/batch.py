"""Offline proof-batch planning (schema version 2).

A proof batch covers one or two editions. Every edition is split into
non-empty contiguous segments; each geometrically feasible segment is either
printed fresh as one signature or supplied by a physically equivalent
retained legacy signature. A retained piece may be issued at most once across
the whole batch. Planning minimizes, in order: total fresh sheets, total
blank slots in the bound books (including reused signatures), and total
signature count; remaining ties are settled by the deterministic collation
tuple defined in the data contract.

Only the Python standard library is used.
"""
import copy
import re

from .checks import array, fields, ident, unique
from .geometry import place as place_pages
from .impose import build_signature, signatures
from .model import validate

_REVISION_RE = re.compile(r'[\x21-\x7e]+')
_GEOMETRY_KEYS = ('sheet_width_um', 'sheet_height_um', 'leaf_width_um', 'leaf_height_um',
                  'margin_um', 'gutter_um', 'thickness_um')


def _revision(value):
    if not isinstance(value, str) or not _REVISION_RE.fullmatch(value):
        raise ValueError('revision must be a non-empty printable ASCII identifier')
    return value


def _logical_pages(data):
    """Expand a validated v1 source into ordered logical-page identity dicts."""
    return [{'section_id': s['id'], 'page': p} for s in data['sections']
            for p in range(1, s['pages'] + 1)]


def _revised_pages(data, revisions):
    pages = _logical_pages(data)
    if not isinstance(revisions, list) or len(revisions) != len(pages):
        raise ValueError('revisions must correspond one-to-one to expanded logical pages')
    return [{**page, 'revision': _revision(rev)} for page, rev in zip(pages, revisions)]


def _attach_revisions(sig, revised_by_identity):
    for sheet in sig['sheets']:
        for slots in sheet['sides'].values():
            for slot in slots:
                page = slot['logical_page']
                if page is not None:
                    revised = revised_by_identity.get((page['section_id'], page['page']))
                    if revised is None:
                        raise ValueError('legacy signature references a page absent from its source')
                    slot['logical_page'] = dict(revised)


def _validate_batch(raw):
    fields(raw, ('editions', 'legacy_lots'))
    edition_records = array(raw['editions'], 1, 2)
    lot_records = array(raw['legacy_lots'], 0, 8)
    unique([ident(e['id']) for e in edition_records])
    unique([ident(l['id']) for l in lot_records])

    editions = []
    for record in edition_records:
        fields(record, ('id', 'source', 'revisions'))
        data = validate(record['source'])
        pages = _revised_pages(data, record['revisions'])
        if not 1 <= len(pages) <= 512:
            raise ValueError('each proof edition must contain 1..512 logical pages')
        editions.append({'id': record['id'], 'source': data, 'pages': pages})

    pieces = []
    stock_ids = set()
    for lot in lot_records:
        fields(lot, ('id', 'source', 'revisions', 'retained'))
        data = validate(lot['source'])
        revised_pages = _revised_pages(data, lot['revisions'])
        revised_by_identity = {(p['section_id'], p['page']): p for p in revised_pages}
        retained = array(lot['retained'], 1, 8)
        # Reconstruct the physical book once under the original v1 rules. A
        # geometrically invalid old ticket is itself an input error.
        built = place_pages(signatures(data), data['geometry'])
        built_by_number = {sig['signature']: sig for sig in built}
        lot_numbers = set()
        for record in retained:
            fields(record, ('stock_id', 'signature'))
            stock_id = ident(record['stock_id'])
            if stock_id in stock_ids:
                raise ValueError(f'duplicate stock_id: {stock_id}')
            stock_ids.add(stock_id)
            number = record['signature']
            if type(number) is not int or number < 1:
                raise ValueError('retained signature number must be a positive integer')
            if number in lot_numbers:
                raise ValueError(f'duplicate retained signature number {number} in lot {lot["id"]}')
            lot_numbers.add(number)
            match = built_by_number.get(number)
            if match is None:
                raise ValueError(f"retained signature {number} not found in lot {lot['id']}")
            _attach_revisions(match, revised_by_identity)
            pieces.append({'lot_id': lot['id'], 'stock_id': stock_id,
                           'legacy_signature': number, 'binding': data['binding'],
                           'geometry': data['geometry'], 'sig': match})
    if len(pieces) > 8:
        raise ValueError('at most eight retained pieces across all lots')
    return editions, pieces


def _signature_fingerprint(sig, binding, geometry):
    """Physical-equivalence token.

    Two signatures are physically equivalent only when binding direction,
    every geometry field, the sheet count and each layer/side/left-right
    slot's folio, frame and revised logical page (including null blanks)
    match. Legacy signature numbers and legacy global starts never
    participate in equivalence.
    """
    token = [binding, tuple(geometry[key] for key in _GEOMETRY_KEYS), len(sig['sheets'])]
    for sheet in sig['sheets']:
        token.append(sheet['nesting_depth'])
        for side in ('front', 'back'):
            for slot in sheet['sides'][side]:
                frame = slot['placement']
                token.append((side, slot['folio'], _page_token(slot['logical_page']),
                              frame['x_um'], frame['y_um'], frame['width_um'],
                              frame['height_um'], frame['rotation_degrees']))
    return tuple(token)


def _page_token(page):
    if page is None:
        return None
    return (page['section_id'], page['page'], page['revision'])


def _piece_fingerprint(piece):
    return _signature_fingerprint(piece['sig'], piece['binding'], piece['geometry'])


def _is_feasible_size(size, geometry):
    """Creep feasibility depends only on the deepest nesting of the signature."""
    max_depth = size // 4 - 1
    return 2 * max_depth * geometry['thickness_um'] <= geometry['gutter_um']


def _frame_table(size, binding, geometry):
    """Folio order and placement frames for every slot of a signature."""
    proto = build_signature([None] * size, binding, 1, 1)
    _place_frames(proto, geometry)
    table = []
    for sheet in proto['sheets']:
        sides = {}
        for side, slots in sheet['sides'].items():
            sides[side] = [{'folio': slot['folio'], 'placement': dict(slot['placement'])}
                           for slot in slots]
        table.append((sheet['nesting_depth'], sheet['creep_um'], sides))
    return table


def _content_fingerprint(table, content, binding, geometry):
    """Physical-equivalence token built straight from the shared frame table.

    Equivalent to ``_signature_fingerprint`` on a built signature but avoids
    allocating per-segment slot dictionaries during the search.
    """
    token = [binding, tuple(geometry[key] for key in _GEOMETRY_KEYS), len(table)]
    for depth, creep, frame_sides in table:
        token.append(depth)
        for side in ('front', 'back'):
            for frame in frame_sides[side]:
                folio = frame['folio']
                placement = frame['placement']
                token.append((side, folio, _page_token(content[folio - 1]),
                              placement['x_um'], placement['y_um'], placement['width_um'],
                              placement['height_um'], placement['rotation_degrees']))
    return tuple(token)


def _enumerate_segments(edition, piece_fingerprints, piece_groups):
    """All geometrically feasible segments as lightweight search descriptors.

    Full signatures are only instantiated for the segments of the winning plan.
    """
    data = edition['source']
    pages = edition['pages']
    capacity = data['signature_pages']
    geometry = data['geometry']
    binding = data['binding']
    feasible_sizes = {size for size in range(4, capacity + 1, 4)
                      if _is_feasible_size(size, geometry)}
    # The physical layout (folios, nesting, frames) only depends on size and
    # binding; build one frame table per feasible size.
    tables = {size: _frame_table(size, binding, geometry) for size in feasible_sizes}
    geometry_key = tuple(geometry[key] for key in _GEOMETRY_KEYS)
    segments = []
    for start in range(len(pages)):
        for length in range(1, min(capacity, len(pages) - start) + 1):
            size = ((length + 3) // 4) * 4
            if size not in feasible_sizes:
                continue
            table = tables[size]
            content = pages[start:start + length] + [None] * (size - length)
            sheet_count = len(table)
            # Only structurally compatible pieces need the full fingerprint.
            candidates = piece_groups.get((binding, geometry_key, sheet_count, size), [])
            if candidates:
                fingerprint = _content_fingerprint(table, content, binding, geometry)
                piece_indices = [i for i in candidates if piece_fingerprints[i] == fingerprint]
            else:
                piece_indices = []
            segments.append({'start': start, 'end': start + length, 'size': size,
                             'sheets': sheet_count, 'blanks': size - length,
                             'table': table, 'content': content,
                             'piece_indices': piece_indices})
    return segments


def _build_segment_signature(segment, binding):
    """Instantiate the full placed signature of a chosen winning segment."""
    table = segment['table']
    content = segment['content']
    sheets = []
    for depth, creep, frame_sides in table:
        sides = {}
        for side, frames in frame_sides.items():
            sides[side] = [{'folio': frame['folio'],
                            'logical_page': _clone_page(content[frame['folio'] - 1]),
                            'placement': dict(frame['placement'])} for frame in frames]
        sheets.append({'sheet': depth + 1, 'nesting_depth': depth,
                       'creep_um': creep, 'sides': sides})
    return {'signature': 1, 'logical_start': segment['start'] + 1,
            'folio_count': segment['size'], 'blank_count': segment['blanks'], 'sheets': sheets}


def _clone_page(page):
    return None if page is None else dict(page)


def _place_frames(sig, geometry):
    for sheet in sig['sheets']:
        creep = sheet['nesting_depth'] * geometry['thickness_um']
        sheet['creep_um'] = creep
        for slots in sheet['sides'].values():
            for index, slot in enumerate(slots):
                x = geometry['margin_um'] + index * (geometry['leaf_width_um'] + geometry['gutter_um'])
                x += creep if index == 0 else -creep
                slot['placement'] = {'x_um': x, 'y_um': geometry['margin_um'],
                                     'width_um': geometry['leaf_width_um'],
                                     'height_um': geometry['leaf_height_um'],
                                     'rotation_degrees': 0}


def _state_less(candidate, old):
    """Scalar totals dominate; the flat collation tie tuple is consulted last."""
    for index in (0, 1, 2, 3):
        if candidate[index] != old[index]:
            return candidate[index] < old[index]
    return False


def _plan(editions, pieces):
    # Cheap structural key narrows physical-equivalence checks to pieces that
    # share binding, geometry, sheet count and folio count.
    piece_groups = {}
    for index, piece in enumerate(pieces):
        sig = piece['sig']
        key = (piece['binding'], tuple(piece['geometry'][k] for k in _GEOMETRY_KEYS),
               len(sig['sheets']), sig['folio_count'])
        piece_groups.setdefault(key, []).append(index)
    piece_fingerprints = [_piece_fingerprint(piece) for piece in pieces]
    stock_ids = [piece['stock_id'] for piece in pieces]
    # layers[e][position][mask] = (fresh, blanks, sigs, tie, previous_mask, option)
    # The scalar totals are compared first; the flat tie tuple only breaks ties
    # when all three scalar totals agree, matching the contract ordering.
    layers = []
    incoming = {0: (0, 0, 0, ())}
    for edition in editions:
        segments = _enumerate_segments(edition, piece_fingerprints, piece_groups)
        page_count = len(edition['pages'])
        # Seed every mask carried over from earlier editions at position zero.
        dp = {0: {mask: (fresh, blanks, sigs, tie, None, None)
                  for mask, (fresh, blanks, sigs, tie) in incoming.items()}}
        for position in range(page_count):
            bucket = dp.get(position)
            if not bucket:
                continue
            for segment in segments:
                if segment['start'] != position:
                    continue
                target = dp.setdefault(segment['end'], {})
                end = segment['end']
                add_sheets = segment['sheets']
                add_blanks = segment['blanks']
                fresh_tie = (end, 1, '')
                for mask, state in bucket.items():
                    fresh, blanks, sigs, tie = state[0], state[1], state[2], state[3]
                    # Option 1: print the whole segment fresh.
                    candidate = (fresh + add_sheets, blanks + add_blanks, sigs + 1,
                                 tie + (fresh_tie,), mask, ('fresh', segment))
                    old = target.get(mask)
                    if old is None or _state_less(candidate, old):
                        target[mask] = candidate
                    # Option 2: consume each physically equivalent retained piece.
                    for index in segment['piece_indices']:
                        bit = 1 << index
                        if mask & bit:
                            continue  # a physical piece can only be issued once
                        new_mask = mask | bit
                        candidate = (fresh, blanks + add_blanks, sigs + 1,
                                     tie + ((end, 0, stock_ids[index]),),
                                     mask, ('reuse', segment, index))
                        old = target.get(new_mask)
                        if old is None or _state_less(candidate, old):
                            target[new_mask] = candidate
        final = dp.get(page_count)
        if not final:
            raise ValueError(f"no feasible segmentation covers edition {edition['id']}")
        layers.append(dp)
        incoming = {mask: (state[0], state[1], state[2], state[3])
                    for mask, state in final.items()}
    best_mask = min(final, key=lambda mask: (final[mask][0], final[mask][1],
                                             final[mask][2], final[mask][3]))
    best_state = final[best_mask]
    best_objective = (best_state[0], best_state[1], best_state[2], best_state[3])

    # Backtrack through the layers to recover the chosen options.
    choices_per_edition = []
    mask = best_mask
    for layer_index in range(len(editions) - 1, -1, -1):
        dp = layers[layer_index]
        position = len(editions[layer_index]['pages'])
        choices = []
        while position > 0:
            state = dp[position][mask]
            previous_mask, option = state[4], state[5]
            choices.append(option)
            mask = previous_mask
            position = option[1]['start']
        choices.reverse()
        choices_per_edition.append(choices)
    choices_per_edition.reverse()
    return choices_per_edition, best_objective


def _output_sheets(sig):
    return copy.deepcopy(sig['sheets'])


def _build_report(editions, pieces, choices_per_edition, best_objective):
    report_editions = []
    used_stock = []
    total_fresh = total_blanks = total_signatures = 0
    for edition, choices in zip(editions, choices_per_edition):
        entries = []
        for sequence, option in enumerate(choices, start=1):
            kind, segment = option[0], option[1]
            start, end = segment['start'], segment['end']
            entry = {'signature': sequence, 'logical_start': start + 1, 'logical_end': end,
                     'folio_count': segment['size'], 'blank_count': segment['blanks']}
            if kind == 'fresh':
                entry['source'] = 'fresh_print'
                entry['sheets'] = _output_sheets(
                    _build_segment_signature(segment, edition['source']['binding']))
                total_fresh += segment['sheets']
            else:
                piece = pieces[option[2]]
                # Re-derive equivalence rather than trusting the planner index.
                expected = _content_fingerprint(segment['table'], segment['content'],
                                                edition['source']['binding'],
                                                edition['source']['geometry'])
                if _piece_fingerprint(piece) != expected:
                    raise ValueError('retained piece is not physically equivalent to its segment')
                entry['source'] = 'retained'
                entry['stock_id'] = piece['stock_id']
                entry['lot_id'] = piece['lot_id']
                entry['legacy_signature'] = piece['legacy_signature']
                entry['sheets'] = _output_sheets(piece['sig'])
                used_stock.append(piece['stock_id'])
            entries.append(entry)
            total_blanks += segment['blanks']
            total_signatures += 1
        _verify_edition(edition, entries)
        report_editions.append({'id': edition['id'], 'binding': edition['source']['binding'],
                                'geometry': edition['source']['geometry'],
                                'signatures': entries,
                                'collation': _collation(entries),
                                'collation_verified': True})
    if len(used_stock) != len(set(used_stock)):
        raise ValueError('a retained piece was issued more than once')
    fresh, blanks, sig_count, _tie = best_objective
    if (fresh, blanks, sig_count) != (total_fresh, total_blanks, total_signatures):
        raise ValueError('optimization totals do not match the published work order')
    return {'schema_version': 2, 'editions': report_editions,
            'summary': {'fresh_sheets': total_fresh, 'blank_pages': total_blanks,
                        'signatures': total_signatures},
            'optimization_verified': True}


def _collation(entries):
    pages = []
    for entry in entries:
        slots = [slot for sheet in entry['sheets'] for side in sheet['sides'].values()
                 for slot in side]
        pages.extend(slot['logical_page'] for slot in sorted(slots, key=lambda s: s['folio'])
                     if slot['logical_page'] is not None)
    return pages


def _verify_edition(edition, entries):
    # Coverage: contiguous, non-empty segments spanning every logical page once.
    if not entries:
        raise ValueError('edition has no signatures')
    expected_start = 1
    for entry in entries:
        if entry['logical_start'] != expected_start:
            raise ValueError('segmentation leaves a gap or overlaps')
        if entry['logical_end'] < entry['logical_start']:
            raise ValueError('empty segment is not allowed')
        expected_start = entry['logical_end'] + 1
    if expected_start != len(edition['pages']) + 1:
        raise ValueError('segmentation does not cover every logical page')
    # Collation with revisions must reproduce the source page for page.
    if _collation(entries) != edition['pages']:
        raise ValueError('collation does not match the revised source pages')
    # Paper and blank conservation for every signature.
    real_pages = sum(e['logical_end'] - e['logical_start'] + 1 for e in entries)
    folios = sum(e['folio_count'] for e in entries)
    blank_pages = sum(e['blank_count'] for e in entries)
    if real_pages + blank_pages != folios:
        raise ValueError('real pages and blank slots do not conserve folios')
    for entry in entries:
        sheets = entry['sheets']
        if 4 * len(sheets) != entry['folio_count']:
            raise ValueError('sheet count does not conserve paper')
        if sum(1 for sheet in sheets for side in sheet['sides'].values()
               for slot in side if slot['logical_page'] is None) != entry['blank_count']:
            raise ValueError('blank slot count is inconsistent')


def process_batch(raw):
    """Plan a proof batch and return the verified schema-version-2 work order."""
    editions, pieces = _validate_batch(raw)
    choices_per_edition, best_objective = _plan(editions, pieces)
    return _build_report(editions, pieces, choices_per_edition, best_objective)
