def place(signatures, geometry):
    for signature in signatures:
        for sheet in signature['sheets']:
            creep = sheet['nesting_depth'] * geometry['thickness_um']
            if 2 * creep > geometry['gutter_um']:
                raise ValueError('creep would overlap leaves at the fold')
            sheet['creep_um'] = creep
            for slots in sheet['sides'].values():
                for index, slot in enumerate(slots):
                    x = geometry['margin_um'] + index * (geometry['leaf_width_um'] + geometry['gutter_um']) + (creep if index == 0 else -creep)
                    slot['placement'] = {'x_um': x, 'y_um': geometry['margin_um'], 'width_um': geometry['leaf_width_um'], 'height_um': geometry['leaf_height_um'], 'rotation_degrees': 0}
    return signatures
