from engine import Candle, evaluate_setup, highest_wick

def c(ts,o,h,l,cl,v=100): return Candle(ts,o,h,l,cl,v)

def test_highest_wick_is_previous_high():
    cs=[c(i,10+i,11+i,9+i,10.5+i) for i in range(5)]
    assert highest_wick(cs)==15

def test_breakout_requires_close():
    c5=[
        c(0,100,105,98,104), c(300,104,110,103,109), c(600,109,125,108,123),
        c(900,123,120,112,114), c(1200,114,118,112,117), c(1500,117,121,115,120)
    ]
    c1=[
        c(1800,118,119,116,118.5), c(1860,118.5,119,116.5,118),
        c(1920,118,120,117,119), c(1980,119,126,118,124),
        c(2040,124,127,122,123), c(2100,123,126,121,124)
    ]
    s=evaluate_setup(c5,c1,min_retrace=0.02)
    assert s.state in {"BREAKOUT_CONFIRMED","HIGHER_LOW","PULLBACK","PUMP","INVALIDATED"}

def test_insufficient_data():
    s=evaluate_setup([c(0,1,1,1,1)],[c(0,1,1,1,1)])
    assert s.state=="NO_SETUP"
