#!/usr/bin/env python3
"""
Fetch World Cup 2026 live scores and write worldcup-live.json.
Runs every 5 minutes via GitHub Actions during tournament hours.
Primary source: ESPN scoreboard API (server-side, no CORS restriction).
Fallback:       SofaScore API.
Output:         worldcup-live.json — served via raw.githubusercontent.com
                with Access-Control-Allow-Origin: * so the HTML can read
                it from any context including file:// protocol.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'requests', '-q'])
    import requests

# ---------------------------------------------------------------------------
# Team name normalisation — maps API names to our STATIC_DATA keys
# ---------------------------------------------------------------------------
ALIASES = {
    'United States':                'USA',
    'United States of America':     'USA',
    'US':                           'USA',
    'Bosnia and Herzegovina':       'Bosnia-Herzegovina',
    'Bosnia & Herzegovina':         'Bosnia-Herzegovina',
    'DR Congo':                     'Congo DR',
    'Congo, DR':                    'Congo DR',
    'Democratic Republic of Congo': 'Congo DR',
    "Côte d'Ivoire":                'Ivory Coast',
    "Cote d'Ivoire":                'Ivory Coast',
    'Korea Republic':               'South Korea',
    'Republic of Korea':            'South Korea',
    'Türkiye':                      'Turkey',
    'Curacao':                      'Curaçao',
}

def norm(name):
    return ALIASES.get(name, name)


# ---------------------------------------------------------------------------
# ESPN scoreboard
# ---------------------------------------------------------------------------
ESPN_SCOREBOARD = 'https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard'
ESPN_SUMMARY    = 'https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/summary?event={}'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/125.0.0.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}


def espn_status(state, detail='', completed=False):
    if completed or state == 'post':
        return 'FT'
    if state == 'in':
        low = detail.lower()
        if 'halftime' in low or 'half time' in low:
            return 'HT'
        return 'LIVE'
    return 'NS'


def espn_goals(event_id):
    """Return (goals1, goals2) lists from ESPN summary endpoint."""
    try:
        r = requests.get(ESPN_SUMMARY.format(event_id), headers=HEADERS, timeout=8)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return [], []

    goals1, goals2 = [], []
    for play in data.get('scoringPlays', []):
        scorer = ''
        for ath in play.get('athletesInvolved', []):
            scorer = ath.get('displayName', '')
            break
        clock = play.get('clock', {}).get('displayValue', '')
        minute = clock.split(':')[0] if ':' in clock else ''
        try:
            minute = int(minute)
        except (ValueError, TypeError):
            minute = ''

        is_home = play.get('homeAway') == 'home'
        entry = {'name': scorer}
        if minute != '':
            entry['minute'] = minute
        if play.get('scoringType', {}).get('name') == 'penalty-kick':
            entry['penalty'] = True
        if play.get('scoringType', {}).get('name') == 'own-goal':
            entry['owngoal'] = True

        (goals1 if is_home else goals2).append(entry)

    return goals1, goals2


def fetch_espn():
    r = requests.get(ESPN_SCOREBOARD, headers=HEADERS, timeout=10)
    r.raise_for_status()
    data = r.json()

    matches = []
    for event in data.get('events', []):
        comp = (event.get('competitions') or [{}])[0]
        st   = comp.get('status', {})
        state     = st.get('type', {}).get('state', 'pre')
        detail    = st.get('type', {}).get('detail', '')
        completed = st.get('type', {}).get('completed', False)
        clock     = st.get('clock', 0)

        status = espn_status(state, detail, completed)

        competitors = comp.get('competitors', [])
        if len(competitors) < 2:
            continue

        home = next((c for c in competitors if c.get('homeAway') == 'home'), competitors[0])
        away = next((c for c in competitors if c.get('homeAway') == 'away'), competitors[1])

        t1 = norm(home.get('team', {}).get('displayName') or home.get('team', {}).get('name', ''))
        t2 = norm(away.get('team', {}).get('displayName') or away.get('team', {}).get('name', ''))

        match = {
            'team1':  t1,
            'team2':  t2,
            'date':   event.get('date', '')[:10],
            'status': status,
        }

        if status != 'NS':
            s1 = int(home.get('score') or 0)
            s2 = int(away.get('score') or 0)
            match['score'] = {'ft': [s1, s2]}

        if status == 'LIVE':
            match['live_minute'] = int(clock) if clock else None

        # Fetch goal events for active / finished matches
        if status in ('LIVE', 'FT') and event.get('id'):
            g1, g2 = espn_goals(event['id'])
            match['goals1'] = g1
            match['goals2'] = g2

        matches.append(match)

    return matches


# ---------------------------------------------------------------------------
# SofaScore fallback
# ---------------------------------------------------------------------------
SOFA_URL = 'https://api.sofascore.com/api/v1/sport/football/scheduled-events/{}'
SOFA_HEADERS = {
    **HEADERS,
    'Referer': 'https://www.sofascore.com/',
}

SOFA_STATUS = {
    # code → our status
    6: 'NS', 7: 'LIVE', 62: 'HT', 63: 'LIVE',
    100: 'FT', 120: 'FT', 110: 'FT',
}


def fetch_sofascore(date_str):
    r = requests.get(SOFA_URL.format(date_str), headers=SOFA_HEADERS, timeout=10)
    r.raise_for_status()
    data = r.json()

    matches = []
    for ev in data.get('events', []):
        tourn = ev.get('tournament', {})
        name  = tourn.get('name', '') + tourn.get('uniqueTournament', {}).get('name', '')
        if 'World Cup' not in name and '2026' not in name:
            continue

        t1 = norm(ev.get('homeTeam', {}).get('name', ''))
        t2 = norm(ev.get('awayTeam', {}).get('name', ''))
        code = ev.get('status', {}).get('code', 0)
        status = SOFA_STATUS.get(code, 'NS')

        ts = ev.get('startTimestamp', 0)
        date_match = (datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d')
                      if ts else date_str)

        match = {'team1': t1, 'team2': t2, 'date': date_match, 'status': status}

        if status != 'NS':
            s1 = (ev.get('homeScore') or {}).get('current', 0)
            s2 = (ev.get('awayScore') or {}).get('current', 0)
            if s1 is not None and s2 is not None:
                match['score'] = {'ft': [s1, s2]}

        matches.append(match)

    return matches


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    now     = datetime.now(timezone.utc)
    today   = now.strftime('%Y-%m-%d')
    matches = []
    source  = 'none'
    errors  = []

    # 1. ESPN
    try:
        matches = fetch_espn()
        source  = 'espn'
        print(f'ESPN OK — {len(matches)} match(es)')
    except Exception as e:
        errors.append(f'espn: {e}')
        print(f'ESPN failed: {e}')

    # 2. SofaScore fallback
    if not matches:
        try:
            matches = fetch_sofascore(today)
            source  = 'sofascore'
            print(f'SofaScore OK — {len(matches)} match(es)')
        except Exception as e:
            errors.append(f'sofascore: {e}')
            print(f'SofaScore failed: {e}')

    out_path = Path(__file__).resolve().parent.parent / 'worldcup-live.json'

    # Only write (and therefore only trigger a git commit) when match data changes.
    # The 'updated' timestamp is always refreshed when we do write.
    existing_matches = []
    if out_path.exists():
        try:
            existing_matches = json.loads(out_path.read_text()).get('matches', [])
        except Exception:
            pass

    # Stable comparison: sort by (date, team1, team2) before comparing
    def sort_key(m):
        return (m.get('date', ''), m.get('team1', ''), m.get('team2', ''))

    new_comparable  = json.dumps(sorted(matches,          key=sort_key), ensure_ascii=False, sort_keys=True)
    prev_comparable = json.dumps(sorted(existing_matches, key=sort_key), ensure_ascii=False, sort_keys=True)

    if new_comparable == prev_comparable:
        print('No match data changes — skipping write.')
        return

    output = {
        'updated': now.isoformat(),
        'source':  source,
        'errors':  errors or None,
        'matches': matches,
    }
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(f'Written → {out_path}  ({len(matches)} matches, source={source})')


if __name__ == '__main__':
    main()
