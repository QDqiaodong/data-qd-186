def build_signature(padded, binding, number, logical_start):
    """Build one signature from an already padded logical-page list.

    ``padded`` contains logical-page dicts and ``None`` blanks; its length is
    the folio count and must be a positive multiple of four. The layout is the
    v1 nested-sheet model, shared by fixed-cut v1 jobs and proof-batch
    segments.
    """
    size = len(padded)
    sheets = []
    for depth in range(size // 4):
        folios = {'front': [size - 2 * depth, 1 + 2 * depth], 'back': [2 + 2 * depth, size - 1 - 2 * depth]}
        if binding == 'right':
            folios = {side: pair[::-1] for side, pair in folios.items()}
        sides = {side: [{'folio': f, 'logical_page': padded[f - 1]} for f in pair] for side, pair in folios.items()}
        sheets.append({'sheet': depth + 1, 'nesting_depth': depth, 'sides': sides})
    return {'signature': number, 'logical_start': logical_start, 'folio_count': size,
            'blank_count': sum(1 for slot in padded if slot is None), 'sheets': sheets}


def signatures(data):
    pages = [{'section_id': s['id'], 'page': p} for s in data['sections'] for p in range(1, s['pages'] + 1)]
    capacity = data['signature_pages']
    result = []
    for start in range(0, len(pages), capacity):
        logical = pages[start:start + capacity]
        size = ((len(logical) + 3) // 4) * 4
        padded = logical + [None] * (size - len(logical))
        result.append(build_signature(padded, data['binding'], len(result) + 1, start + 1))
    return result
