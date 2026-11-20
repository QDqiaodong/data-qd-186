def signatures(data):
    pages = [{'section_id': s['id'], 'page': p} for s in data['sections'] for p in range(1, s['pages'] + 1)]
    capacity = data['signature_pages']
    result = []
    for start in range(0, len(pages), capacity):
        logical = pages[start:start + capacity]
        size = ((len(logical) + 3) // 4) * 4
        padded = logical + [None] * (size - len(logical))
        sheets = []
        for depth in range(size // 4):
            folios = {'front': [size - 2 * depth, 1 + 2 * depth], 'back': [2 + 2 * depth, size - 1 - 2 * depth]}
            if data['binding'] == 'right':
                folios = {side: pair[::-1] for side, pair in folios.items()}
            sides = {side: [{'folio': f, 'logical_page': padded[f - 1]} for f in pair] for side, pair in folios.items()}
            sheets.append({'sheet': depth + 1, 'nesting_depth': depth, 'sides': sides})
        result.append({'signature': len(result) + 1, 'logical_start': start + 1, 'folio_count': size, 'blank_count': size - len(logical), 'sheets': sheets})
    return result
