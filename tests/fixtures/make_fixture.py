"""Generate a realistic PokerNow.club-style CSV export for tests.

Lines are written chronologically here and reversed on output to match the
newest-first ordering of real exports.
"""
import csv
from datetime import datetime, timedelta, timezone

A = '"Alice @ aaa111"'
B = '"Bob @ bbb222"'
C = '"Carol Smith @ ccc333"'
C2 = '"Lady Carol @ ccc333"'  # same player, renamed on rejoin
D = '"Dave @ ddd444"'

lines = [
    f'The player {A} joined the game with a stack of 1000.',
    f'The player {B} joined the game with a stack of 1000.',
    f'The player {C} joined the game with a stack of 1000.',
    f'The admin approved the player {D} participation with a stack of 500.',

    # Hand 1: simple raise, everyone folds -> Bob wins blinds, uncalled bet returned
    f'-- starting hand #1 (id: h1abc)  No Limit Texas Hold\'em (dealer: {A}) --',
    f'Player stacks: #1 {A} (1000) | #2 {B} (1000) | #3 {C} (1000) | #4 {D} (500)',
    'Your hand is 7h, 2c',
    f'{B} posts a small blind of 5',
    f'{C} posts a big blind of 10',
    f'{D} folds',
    f'{A} folds',
    f'{B} raises to 30',
    f'{C} folds',
    f'Uncalled bet of 20 returned to {B}',
    f'{B} collected 20 from pot',
    '-- ending hand #1 --',
    f'{B} shows a 7♦, 2♣.',
    'Undealt cards: 5s, 5d, Kc [Ah, 2d]',

    # Hand 2: 3-bet pot, flop c-bet, fold
    f'-- starting hand #2 (id: h2abc)  No Limit Texas Hold\'em (dealer: {B}) --',
    f'Player stacks: #1 {A} (1000) | #2 {B} (1010) | #3 {C} (990) | #4 {D} (500)',
    'Your hand is Ah, Kd',
    f'{C} posts a small blind of 5',
    f'{D} posts a big blind of 10',
    f'{A} raises to 30',
    f'{B} raises to 90',
    f'{C} folds',
    f'{D} folds',
    f'{A} calls 90',
    'Flop:  [Kh, 7d, 2s]',
    f'{A} checks',
    f'{B} bets 100',
    f'{A} folds',
    f'Uncalled bet of 100 returned to {B}',
    f'{B} collected 195 from pot',
    '-- ending hand #2 --',

    # Hand 3: all-in + showdown with combination, straddle
    f'-- starting hand #3 (id: h3abc)  (No Limit Texas Hold\'em) (dealer: {C}) --',
    f'Player stacks: #1 {A} (910) | #2 {B} (1115) | #3 {C} (985) | #4 {D} (490)',
    'Your hand is Qs, Qd',
    f'{D} posts a small blind of 5',
    f'{A} posts a big blind of 10',
    f'{B} posts a straddle of 20',
    f'{C} calls 20',
    f'{D} raises to 80',
    f'{A} raises to 200',
    f'{B} folds',
    f'{C} folds',
    f'{D} raises to 490 and go all in',
    f'{A} calls 490',
    'Flop:  [Qh, 9c, 4d]',
    'Turn: Q♥, 9♣, 4♦ [10♠]',
    'River: Q♥, 9♣, 4♦, 10♠ [J♣]',
    f'{A} shows a Qs, Qd.',
    f'{D} shows a Ac, Ad.',
    f'{A} collected 1020 from pot with Three of a Kind, Q\'s (combination: Qs, Qd, Qh, Jc, Ts)',
    '-- ending hand #3 --',

    # Dave busts and rebuys; Carol stands up
    f'{D} quits the game with a stack of 0.',
    f'The admin updated the player {B} stack from 1095 to 1295.',
    f'The player {D} joined the game with a stack of 1000.',

    # Hand 4: split pot, checked down to showdown, unknown line
    f'-- starting hand #4 (id: h4abc)  (No Limit Texas Hold\'em) (dealer: {D}) --',
    f'Player stacks: #1 {A} (1440) | #2 {B} (1295) | #3 {C} (965) | #4 {D} (1000)',
    'Your hand is 8s, 8c',
    f'{A} posts a small blind of 5',
    f'{B} posts a big blind of 10',
    f'{C} calls 10',
    f'{D} calls 10',
    f'{A} calls 10',
    f'{B} checks',
    'Flop:  [Ah, Kh, Qh]',
    f'{A} checks',
    f'{B} checks',
    f'{C} bets 20',
    f'{D} calls 20',
    f'{A} folds',
    f'{B} calls 20',
    'Turn: Ah, Kh, Qh [Jh]',
    f'{B} checks',
    f'{C} checks',
    f'{D} checks',
    'River: Ah, Kh, Qh, Jh [Th]',
    f'{B} checks',
    f'{C} checks',
    f'{D} checks',
    f'{B} shows a 3c, 3d.',
    f'{C} shows a 8d, 9d.',
    f'{D} shows a 2s, 2c.',
    f'{B} collected 34 from pot with Royal Flush (combination: Ah, Kh, Qh, Jh, Th)',
    f'{C} collected 33 from pot with Royal Flush (combination: Ah, Kh, Qh, Jh, Th)',
    f'{D} collected 33 from pot with Royal Flush (combination: Ah, Kh, Qh, Jh, Th)',
    'Some future log line we do not understand yet',
    '-- ending hand #4 --',

    # Hand 5: run it twice
    f'-- starting hand #5 (id: h5abc)  (No Limit Texas Hold\'em) (dealer: {A}) --',
    f'Player stacks: #1 {A} (1435) | #2 {B} (1299) | #3 {C} (968) | #4 {D} (1003)',
    'Your hand is Js, Jd',
    f'{B} posts a small blind of 5',
    f'{C} posts a big blind of 10',
    f'{D} folds',
    f'{A} raises to 40',
    f'{B} folds',
    f'{C} raises to 968 and go all in',
    f'{A} calls 968',
    'Flop:  [2h, 5c, 9d]',
    'Turn: 2h, 5c, 9d [Kd]',
    'River: 2h, 5c, 9d, Kd [3s]',
    'Flop (second run):  [As, 8c, 8d]',
    'Turn (second run): As, 8c, 8d [Ac]',
    'River (second run): As, 8c, 8d, Ac [7s]',
    f'{A} shows a Js, Jd.',
    f'{C} shows a Ad, Kc.',
    f'{A} collected 970 from pot with Pair, J\'s (combination: Js, Jd, Kd, 9d, 5c)',
    f'{C} collected 971 from pot with Two Pair, A\'s & 8\'s (combination: As, Ac, Ad, 8c, 8d)',
    '-- ending hand #5 --',
    f'The player {C} quits the game with a stack of 971.',
    f'The player {D} changed the ID from ddd444 to ddd999 because authenticated login.',
    f'The player {C2} joined the game with a stack of 500.',
    'Asking to busted players the rebuy decision.',

    # Hand 6: bomb pot, heads-up-ish, Dave under his new ID
    f'-- starting hand #6 (id: h6abc)  No Limit Texas Hold\'em (dealer: {B}) --',
    f'Player stacks: #1 {A} (1437) | #2 {B} (1294) | #3 {C2} (500) | #4 "Dave @ ddd999" (1003)',
    'Your hand is As, Ad',
    f'{A} posts a bet of 40 (bomb pot bet)',
    f'{B} calls 40 (bomb pot bet)',
    f'{C2} calls 40 (bomb pot bet)',
    '"Dave @ ddd999" calls 40 (bomb pot bet)',
    'Flop:  [Ac, 7h, 7d]',
    f'{A} bets 60',
    f'{B} folds',
    f'{C2} folds',
    '"Dave @ ddd999" calls 60',
    'Turn: Ac, 7h, 7d [2s]',
    f'{A} checks',
    '"Dave @ ddd999" bets 100',
    f'{A} raises to 300',
    '"Dave @ ddd999" folds',
    f'Uncalled bet of 200 returned to {A}',
    f'{A} collected 480 from pot',
    '-- ending hand #6 --',
]

t0 = datetime(2024, 3, 10, 20, 0, 0, tzinfo=timezone.utc)
rows = []
for i, line in enumerate(lines):
    at = t0 + timedelta(seconds=7 * i)
    rows.append((line, at.strftime("%Y-%m-%dT%H:%M:%S.000Z"), 171000000000 + i))

with open("tests/fixtures/sample_log.csv", "w", newline="") as f:
    w = csv.writer(f, quoting=csv.QUOTE_ALL)
    w.writerow(["entry", "at", "order"])
    for row in reversed(rows):
        w.writerow(row)
print(f"wrote {len(rows)} rows")
