"""Post-binding offline audit of an executed proof batch (schema version 3).

The binding audit runs after a v2 proof batch has been printed and the
binding line has recovered from a jam. The conveyor log cannot pinpoint
where each physical sheet landed: every observed sheet only lists a few
target positions it may have entered, while the plate records state which
position's front and back plates actually printed each side. The audit first
decides which complete binding explanations (one-to-one assignments of every
observed sheet to a fresh-print target position) the logistics evidence
allows, and only then judges quality. Sheet flow alone decides which
explanations exist; a misprinted sheet may genuinely be bound into a book.

A position is qualified inside one complete explanation when the sheet it
received matches the work order on both sides: left-to-right slots must
agree on the revised logical page (including null blanks) and on the
placement frame. Folio numbers, signature numbers and sheet numbers are not
print identity and never participate. A book (one edition) is qualified when
every fresh-print position of it is qualified inside the same explanation.

Per edition the report gives ``verified`` (qualified in every complete
explanation), ``defective`` (no complete explanation qualifies it) or
``uncertain`` (both kinds exist). Every claimed case ships a verifiable
batch-wide one-to-one assignment as evidence; defective books additionally
ship a Hall-deficiency witness (a target set whose remaining neighborhood is
strictly smaller) explaining why no swap can assemble a good book. When no
complete explanation exists at all, the report is a logistics-conflict
diagnosis with the same deficiency shape and carries no quality conclusions.

All matching and reachability work is polynomial (augmenting paths plus
strongly connected components); binding explanations are never enumerated.
Only the Python standard library is used.
"""
from collections import deque

from .batch import process_batch
from .checks import array, fields, ident

_SIDES = ('front', 'back')


# ---------------------------------------------------------------- work order

def _collect_positions(order):
    """Fresh-print target positions of the executed work order, plus lookups.

    Retained signatures are confirmed and already bound into the books: they
    never appear as targets, candidates or plate references. Referencing one
    is an input error rather than an unknown reference, so both lookups are
    returned.
    """
    positions = []
    fresh = {}
    retained = set()
    for edition in order['editions']:
        for signature in edition['signatures']:
            for sheet in signature['sheets']:
                key = (edition['id'], signature['signature'], sheet['sheet'])
                if signature['source'] != 'fresh_print':
                    retained.add(key)
                    continue
                fresh[key] = len(positions)
                positions.append({
                    'edition_id': edition['id'],
                    'signature': signature['signature'],
                    'sheet': sheet['sheet'],
                    'sides': {side: [(slot['logical_page'], slot['placement'])
                                     for slot in sheet['sides'][side]]
                              for side in _SIDES},
                })
    return positions, fresh, retained


def _parse_reference(value, fresh, retained):
    fields(value, ('edition_id', 'signature', 'sheet'))
    edition_id = value['edition_id']
    signature = value['signature']
    sheet = value['sheet']
    if not isinstance(edition_id, str) or type(signature) is not int \
            or type(sheet) is not int or signature < 1 or sheet < 1:
        raise ValueError('target reference must be an edition_id plus positive '
                         'signature and sheet numbers')
    key = (edition_id, signature, sheet)
    if key in retained:
        raise ValueError(f'reference points into a confirmed retained signature: {key!r}')
    index = fresh.get(key)
    if index is None:
        raise ValueError(f'unknown fresh-print target reference: {key!r}')
    return index


def _validate_observed(records_value, fresh, retained):
    records = array(records_value, 0, len(fresh))
    seen = set()
    parsed = []
    for record in records:
        fields(record, ('physical_id', 'possible_targets', 'front_from', 'back_from'))
        physical_id = ident(record['physical_id'])
        if physical_id in seen:
            raise ValueError(f'duplicate physical_id: {physical_id}')
        seen.add(physical_id)
        candidates = array(record['possible_targets'], 1, 16)
        targets = [_parse_reference(candidate, fresh, retained) for candidate in candidates]
        if len(set(targets)) != len(targets):
            raise ValueError(f'duplicate candidate target for {physical_id}')
        parsed.append({'physical_id': physical_id,
                       'targets': sorted(set(targets)),
                       'front_from': _parse_reference(record['front_from'], fresh, retained),
                       'back_from': _parse_reference(record['back_from'], fresh, retained)})
    if len(parsed) != len(fresh):
        raise ValueError('observed sheet count must equal the fresh-print sheet '
                         'count of the work order')
    # Canonical sheet order makes every later choice independent of input row
    # order; candidate lists were canonicalized the same way above.
    parsed.sort(key=lambda record: record['physical_id'])
    return parsed


# ------------------------------------------------------------- matching core

def _max_matching(target_count, adj):
    """Deterministic maximum bipartite matching by augmenting paths.

    Sheets are processed in ascending (sorted physical-id) order and every
    search visits targets in ascending (work-order) order, so the result does
    not depend on input row or candidate-list order.
    """
    match_t = [-1] * target_count
    match_s = [-1] * len(adj)

    def augment(sheet, seen):
        for target in adj[sheet]:
            if seen[target]:
                continue
            seen[target] = True
            if match_t[target] == -1 or augment(match_t[target], seen):
                match_t[target] = sheet
                match_s[sheet] = target
                return True
        return False

    for sheet in range(len(adj)):
        augment(sheet, [False] * target_count)
    return match_t, match_s


def _strong_components(successors):
    """Iterative Tarjan SCC; returns the component id of every node."""
    node_count = len(successors)
    index = [-1] * node_count
    low = [0] * node_count
    on_stack = [False] * node_count
    stack = []
    component = [-1] * node_count
    counter = 0
    found = 0
    for root in range(node_count):
        if index[root] != -1:
            continue
        work = [(root, 0)]
        while work:
            node, child = work[-1]
            if child == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack[node] = True
            descended = False
            while child < len(successors[node]):
                nxt = successors[node][child]
                child += 1
                if index[nxt] == -1:
                    work[-1] = (node, child)
                    work.append((nxt, 0))
                    descended = True
                    break
                if on_stack[nxt] and index[nxt] < low[node]:
                    low[node] = index[nxt]
            if descended:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                if low[node] < low[parent]:
                    low[parent] = low[node]
            if low[node] == index[node]:
                while True:
                    member = stack.pop()
                    on_stack[member] = False
                    component[member] = found
                    if member == node:
                        break
                found += 1
    return component


def _allowed_edges(target_count, adj, match_t, match_s, alive_t, alive_s):
    """Edges of the alive graph that appear in at least one perfect matching.

    Orient unmatched edges sheet->target and matched edges target->sheet; an
    edge is allowed exactly when it is matched or closes an alternating
    cycle, i.e. when its endpoints share a strongly connected component.
    """
    sheet_count = len(adj)
    successors = [[] for _ in range(sheet_count + target_count)]
    for sheet in range(sheet_count):
        if not alive_s[sheet]:
            continue
        for target in adj[sheet]:
            if not alive_t[target]:
                continue
            if target == match_s[sheet]:
                successors[sheet_count + target].append(sheet)
            else:
                successors[sheet].append(sheet_count + target)
    component = _strong_components(successors)
    allowed = set()
    for sheet in range(sheet_count):
        if not alive_s[sheet]:
            continue
        for target in adj[sheet]:
            if not alive_t[target]:
                continue
            if target == match_s[sheet] \
                    or component[sheet] == component[sheet_count + target]:
                allowed.add((sheet, target))
    return allowed


def _include_edge(adj, match_t, match_s, alive_t, alive_s, sheet, target):
    """Rewire the perfect matching so that it contains (sheet, target).

    The edge must be allowed: an alternating cycle through it exists, found
    as a directed path target ~> sheet. Flipping that cycle keeps every
    vertex matched exactly once.
    """
    if match_s[sheet] == target:
        return
    sheet_count = len(adj)
    start = sheet_count + target
    parent = {start: None}
    queue = deque([start])
    while queue and sheet not in parent:
        node = queue.popleft()
        if node < sheet_count:
            matched = match_s[node]
            for candidate in adj[node]:
                nxt = sheet_count + candidate
                if candidate != matched and alive_t[candidate] and nxt not in parent:
                    parent[nxt] = node
                    queue.append(nxt)
        else:
            mate = match_t[node - sheet_count]
            if mate != -1 and mate not in parent:
                parent[mate] = node
                queue.append(mate)
    if sheet not in parent:
        raise ValueError('internal: allowed edge closes no alternating cycle')
    path = []
    node = sheet
    while node is not None:
        path.append(node)
        node = parent[node]
    path.reverse()
    # The cycle (sheet,target) + path alternates unmatched/matched; after the
    # flip the matched pairs are (sheet,target) plus the path's odd edges.
    pairs = [(sheet, target)]
    for index in range(1, len(path) - 1, 2):
        pairs.append((path[index], path[index + 1] - sheet_count))
    for s, t in pairs:
        match_s[s] = t
        match_t[t] = s


def _lexmin_assignment(target_count, adj, inv_adj, match_t, match_s, forced=()):
    """Deterministic lexicographically smallest perfect matching.

    Targets are fixed in work-order order; each receives the smallest sheet
    (sorted physical-id order) that still allows the remainder to be
    completed. ``forced`` (sheet, target) edges are fixed first and must be
    allowed. Consumes the given matching.
    """
    sheet_count = len(adj)
    alive_t = [True] * target_count
    alive_s = [True] * sheet_count
    assignment = [-1] * target_count
    for sheet, target in forced:
        _include_edge(adj, match_t, match_s, alive_t, alive_s, sheet, target)
        assignment[target] = sheet
        alive_t[target] = False
        alive_s[sheet] = False
    for target in range(target_count):
        if not alive_t[target]:
            continue
        allowed = _allowed_edges(target_count, adj, match_t, match_s, alive_t, alive_s)
        choice = next((s for s in inv_adj[target]
                       if alive_s[s] and (s, target) in allowed), None)
        if choice is None:
            raise ValueError('internal: target lost every allowed sheet')
        _include_edge(adj, match_t, match_s, alive_t, alive_s, choice, target)
        assignment[target] = choice
        alive_t[target] = False
        alive_s[choice] = False
    return assignment


def _invert(target_count, adj):
    """Target -> adjacent sheets, sheets kept in ascending (sorted) order."""
    inv = [[] for _ in range(target_count)]
    for sheet, targets in enumerate(adj):
        for target in targets:
            inv[target].append(sheet)
    return inv


def _deficiency(target_count, inv_adj, match_t, match_s):
    """Hall witness for a graph with no perfect matching.

    Targets reachable from unmatched targets via alternating paths, together
    with their whole neighborhood. The neighborhood is exactly the visited
    sheets and is strictly smaller than the target set.
    """
    seen_t = [False] * target_count
    seen_s = [False] * len(match_s)
    queue = deque()
    for target in range(target_count):
        if match_t[target] == -1:
            seen_t[target] = True
            queue.append(target)
    while queue:
        target = queue.popleft()
        for sheet in inv_adj[target]:
            if seen_s[sheet] or match_s[sheet] == target:
                continue
            seen_s[sheet] = True
            mate = match_s[sheet]
            if mate != -1 and not seen_t[mate]:
                seen_t[mate] = True
                queue.append(mate)
    return ([target for target in range(target_count) if seen_t[target]],
            [sheet for sheet in range(len(match_s)) if seen_s[sheet]])


# ------------------------------------------------------------------- quality

def _printed_side(record, positions, side):
    """The side content a sheet actually carries, via its plate reference."""
    plate = record['front_from'] if side == 'front' else record['back_from']
    return positions[plate]['sides'][side]


def _qualified(record, position, positions):
    """Whether the sheet would be qualified at the position.

    Both sides, left to right: revised logical page (including null blanks)
    and placement frame must equal the work order. Different plate references
    may still print an identical side. Folio, signature and sheet numbers are
    not print identity.
    """
    return all(_printed_side(record, positions, side) == position['sides'][side]
               for side in _SIDES)


# -------------------------------------------------------------------- report

def _reference(position):
    return {'edition_id': position['edition_id'],
            'signature': position['signature'],
            'sheet': position['sheet']}


def _assignment_output(assignment, records, positions):
    pairs = [{'physical_id': records[sheet]['physical_id'],
              'target': _reference(positions[target])}
             for target, sheet in enumerate(assignment)]
    pairs.sort(key=lambda pair: pair['physical_id'])
    return pairs


def _mismatch_detail(record, position, positions):
    """First mismatching slot of the first mismatching side."""
    for side in _SIDES:
        actual = _printed_side(record, positions, side)
        for index, (expected, got) in enumerate(zip(position['sides'][side], actual)):
            if expected != got:
                return {'physical_id': record['physical_id'],
                        'target': _reference(position),
                        'side': side,
                        'slot_index': index,
                        'expected': {'logical_page': expected[0], 'placement': expected[1]},
                        'actual': {'logical_page': got[0], 'placement': got[1]}}
    raise ValueError('internal: unqualified sheet shows no mismatching slot')


def _deficiency_output(z_targets, z_sheets, records, positions):
    return {'targets': [_reference(positions[target]) for target in z_targets],
            'sheets': [records[sheet]['physical_id'] for sheet in z_sheets]}


def _audit_editions(order, positions, records, adj, inv_adj, match_t, match_s,
                    arrivals, qualified):
    target_count = len(positions)
    entries = []
    counts = {'verified': 0, 'uncertain': 0, 'defective': 0}
    for edition in order['editions']:
        own = [target for target in range(target_count)
               if positions[target]['edition_id'] == edition['id']]
        own_set = set(own)
        # verified: every sheet that can truly arrive at one of this book's
        # positions is qualified there, so every complete explanation passes.
        verified = all(qualified[sheet][target]
                       for target in own for sheet in arrivals[target])
        # Reduced graph: this book keeps only qualified pairings; every other
        # book keeps its full logistics candidates. A perfect matching of it
        # is exactly a complete explanation that qualifies this book.
        reduced_adj = [[target for target in sheet_targets
                        if target not in own_set or qualified[sheet][target]]
                       for sheet, sheet_targets in enumerate(adj)]
        r_match_t, r_match_s = _max_matching(target_count, reduced_adj)
        defective = any(mate == -1 for mate in r_match_t)
        entry = {'id': edition['id']}
        if defective:
            r_inv = _invert(target_count, reduced_adj)
            z_targets, z_sheets = _deficiency(target_count, r_inv, r_match_t, r_match_s)
            entry['deficiency'] = _deficiency_output(z_targets, z_sheets, records, positions)
        else:
            r_inv = _invert(target_count, reduced_adj)
            assignment = _lexmin_assignment(target_count, reduced_adj, r_inv,
                                            r_match_t, r_match_s)
            entry['qualified_evidence'] = {
                'assignment': _assignment_output(assignment, records, positions)}
        if not verified:
            sheet, target = next((s, t) for t in own for s in arrivals[t]
                                 if not qualified[s][t])
            assignment = _lexmin_assignment(target_count, adj, inv_adj,
                                            list(match_t), list(match_s),
                                            forced=[(sheet, target)])
            entry['unqualified_evidence'] = {
                'assignment': _assignment_output(assignment, records, positions),
                'mismatch': _mismatch_detail(records[sheet], positions[target], positions)}
        status = 'defective' if defective else ('verified' if verified else 'uncertain')
        entry['status'] = status
        counts[status] += 1
        entries.append(entry)
    return entries, counts


def process_audit(raw):
    """Audit an executed v2 work order against post-binding observations.

    Returns the schema-version-3 report. Raises ValueError on any invalid
    input; a legal but self-contradictory conveyor log is a normal audit
    result reported as a logistics conflict.
    """
    fields(raw, ('proof_batch', 'observed_sheets'))
    # The executed work order; audit data can never change it.
    order = process_batch(raw['proof_batch'])
    positions, fresh, retained = _collect_positions(order)
    records = _validate_observed(raw['observed_sheets'], fresh, retained)
    target_count = len(positions)
    adj = [record['targets'] for record in records]
    inv_adj = _invert(target_count, adj)
    match_t, match_s = _max_matching(target_count, adj)
    if any(mate == -1 for mate in match_t):
        # No complete explanation: diagnose the raw logistics candidates
        # without any quality filtering and publish no quality conclusions.
        z_targets, z_sheets = _deficiency(target_count, inv_adj, match_t, match_s)
        return {
            'schema_version': 3,
            'logistics': 'conflict',
            'conflict': {'deficiency': _deficiency_output(z_targets, z_sheets,
                                                          records, positions)},
            'summary': {'logistics': 'conflict',
                        'physical_sheets': len(records),
                        'target_positions': target_count},
        }
    allowed = _allowed_edges(target_count, adj, match_t, match_s,
                             [True] * target_count, [True] * len(records))
    arrivals = [[] for _ in range(target_count)]
    for sheet in range(len(records)):
        for target in adj[sheet]:
            if (sheet, target) in allowed:
                arrivals[target].append(sheet)
    qualified = [{target: _qualified(record, positions[target], positions)
                  for target in record['targets']} for record in records]
    position_entries = [
        {**_reference(position),
         'arrivals': [{'physical_id': records[sheet]['physical_id'],
                       'qualified': qualified[sheet][target]}
                      for sheet in arrivals[target]]}
        for target, position in enumerate(positions)]
    edition_entries, counts = _audit_editions(order, positions, records, adj, inv_adj,
                                              match_t, match_s, arrivals, qualified)
    return {
        'schema_version': 3,
        'logistics': 'explainable',
        'positions': position_entries,
        'editions': edition_entries,
        'summary': {'logistics': 'explainable',
                    'physical_sheets': len(records),
                    'target_positions': target_count,
                    'verified': counts['verified'],
                    'uncertain': counts['uncertain'],
                    'defective': counts['defective']},
    }
