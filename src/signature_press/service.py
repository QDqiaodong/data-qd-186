from .geometry import place
from .impose import signatures
from .model import validate

def process(source):
    data = validate(source)
    result = place(signatures(data), data['geometry'])
    collation = []
    for sig in result:
        placements = [slot for sheet in sig['sheets'] for side in sheet['sides'].values() for slot in side]
        collation.extend(slot['logical_page'] for slot in sorted(placements, key=lambda s: s['folio']) if slot['logical_page'] is not None)
    expected = [{'section_id': s['id'], 'page': p} for s in data['sections'] for p in range(1, s['pages'] + 1)]
    if collation != expected:
        raise ValueError('internal collation conservation failed')
    return {'schema_version': 1, 'binding': data['binding'], 'geometry': data['geometry'], 'signatures': result, 'collation': collation,
            'summary': {'logical_pages': len(expected), 'signatures': len(result), 'physical_sheets': sum(len(s['sheets']) for s in result), 'blank_pages': sum(s['blank_count'] for s in result), 'collation_verified': True}}
