import re
from app.clients import bluesky


def test_record_keys_follow_tid_clock_rules_even_if_the_clock_stalls(monkeypatch):
    monkeypatch.setattr(bluesky,"_last_micros",0)
    ticks=iter([1000,1000,0])
    monkeypatch.setattr(bluesky.time,"time_ns",lambda:next(ticks))
    keys=[bluesky.new_record_key() for _ in range(3)]
    assert keys == sorted(set(keys))
    assert all(re.fullmatch(r"[234567abcdefghij][234567abcdefghijklmnopqrstuvwxyz]{12}",key) for key in keys)
    alphabet="234567abcdefghijklmnopqrstuvwxyz"
    micros=[]
    for key in keys:
        value=0
        for char in key: value=value*32+alphabet.index(char)
        micros.append(value>>10)
    assert micros == [1,2,3]
