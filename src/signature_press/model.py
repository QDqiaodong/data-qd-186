from .checks import array, fields, ident, integer, unique

def validate(data):
    fields(data, ('sections', 'signature_pages', 'binding', 'geometry'))
    sections = array(data['sections'], 1, 200)
    total = 0
    for section in sections:
        fields(section, ('id', 'pages'))
        ident(section['id'])
        total += integer(section['pages'], 1, 10000)
    unique([s['id'] for s in sections])
    if total > 10000:
        raise ValueError('at most 10000 logical pages')
    capacity = integer(data['signature_pages'], 4, 64)
    if capacity % 4:
        raise ValueError('signature_pages must be a multiple of four')
    if data['binding'] not in ('left', 'right'):
        raise ValueError('binding must be left or right')
    g = fields(data['geometry'], ('sheet_width_um', 'sheet_height_um', 'leaf_width_um', 'leaf_height_um', 'margin_um', 'gutter_um', 'thickness_um'))
    for key, value in g.items():
        integer(value, 0 if key in ('margin_um', 'gutter_um', 'thickness_um') else 1, 2000000)
    if 2 * g['margin_um'] + 2 * g['leaf_width_um'] + g['gutter_um'] > g['sheet_width_um']:
        raise ValueError('leaves exceed sheet width')
    if 2 * g['margin_um'] + g['leaf_height_um'] > g['sheet_height_um']:
        raise ValueError('leaves exceed sheet height')
    return data
