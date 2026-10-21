import pytest
from arithmetic import divide

@pytest.mark.parametrize('value', range(500))
def test_exact(value):
    assert divide(value*2, 2) == value

@pytest.mark.parametrize('a,b,expected', [(3,2,1.5), (-3,2,-1.5), (1,4,0.25)])
def test_fraction(a,b,expected):
    assert divide(a,b) == expected

def test_zero():
    with pytest.raises(ZeroDivisionError):
        divide(1,0)
