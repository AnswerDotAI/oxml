'Unit conversions for WordprocessingML measurements'
__all__ = ['twips', 'half_points', 'eighth_points', 'emu']

def twips(pt=0, inch=0, cm=0, mm=0, px=0):
    "Twentieths of a point, the unit of indents, spacing and table widths; `px` is at 96 dpi"
    return round(pt * 20 + inch * 1440 + cm * 1440 / 2.54 + mm * 1440 / 25.4 + px * 15)

def half_points(pt):
    "Font sizes, such as `w:sz` in run properties"
    return round(pt * 2)

def eighth_points(pt):
    "Border widths, the `w:sz` of a border"
    return round(pt * 8)

def emu(inch=0, cm=0, mm=0, pt=0, px=0, dpi=96):
    "English metric units, the unit of drawing extents; `px` is at `dpi`"
    return round(inch * 914400 + cm * 360000 + mm * 36000 + pt * 12700 + px * 914400 / dpi)
