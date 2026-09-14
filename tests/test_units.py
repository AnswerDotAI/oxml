'Measurement conversions.'
from oxml import twips, half_points, eighth_points, emu

def test_units():
    assert (twips(pt=5), twips(inch=1), twips(cm=2.54), twips(px=1)) == (100, 1440, 1440, 15)
    assert (half_points(11.5), eighth_points(0.5)) == (23, 4)
    assert (emu(inch=1), emu(px=4), emu(px=4, dpi=192), emu(pt=1)) == (914400, 38100, 19050, 12700)
