"""Real socket acceptance matrix. Each invocation stays bounded; no full replay suite."""
import argparse
import asyncio
import json
import socket
import subprocess
import tempfile
import sys
from pathlib import Path
from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[2]

class Peer:
    def __init__(self, ws):
        self.ws = ws
        self.counter = 0
        self.messages = []
    async def send(self, message_type, **body):
        self.counter += 1
        m = dict(v=1, type=message_type, correlation_id=f"r{self.counter}", **body)
        await self.ws.send(json.dumps(m))
        return m['correlation_id']
    async def wait(self, kind, predicate=lambda m: True):
        async def receive():
            while True:
                m = json.loads(await self.ws.recv())
                self.messages.append(m)
                if m['type'] == kind and predicate(m):
                    return m
        return await asyncio.wait_for(receive(), 5)

async def peer(url):
    p = Peer(await connect(url, proxy=None, max_size=1048576))
    await p.send('hello', client='test', client_version='1')
    await p.wait('welcome')
    return p

def safe(v, seat):
    forbidden = {'rng_state', 'pending_commands', 'fingerprint', 'command_results',
                 'last_command_seq', 'ledger', 'reserved', 'rejoin_token', 'rate_carries'}
    def walk(x):
        if isinstance(x, dict):
            assert not forbidden.intersection(x), x.keys()
            for y in x.values(): walk(y)
        elif isinstance(x, list):
            for y in x: walk(y)
    walk(v)
    assert v['own_city']['city_id'] == seat
    assert len(v['opponents']) == 3
    assert sum(c['population'] for c in v['opponents']) + v['own_city']['population'] == 4000
    for c in v['opponents']:
        assert 'money_micro' not in c and 'materials_micro' not in c

async def scenario(url, humans):
    ps = [await peer(url) for _ in range(humans)]
    try:
        await ps[0].send('create_lobby')
        credentials = [await ps[0].wait('seat_assigned')]
        code = (await ps[0].wait('lobby_created'))['join_code']
        for p in ps[1:]:
            await p.send('join_lobby', join_code=code)
            credentials.append(await p.wait('seat_assigned'))
        assert len({c['seat'] for c in credentials}) == humans
        await ps[0].send('start_match')
        assert (await ps[0].wait('protocol_error'))['code'] == 'not_ready'
        if humans > 1:
            await ps[-1].send('start_match')
            assert (await ps[-1].wait('protocol_error'))['code'] == 'host_only'
        extra = await peer(url)
        await extra.send('join_lobby', join_code='invalid')
        assert (await extra.wait('protocol_error'))['code'] == 'invalid_join_code'
        if humans == 4:
            await extra.send('join_lobby', join_code=code)
            assert (await extra.wait('protocol_error'))['code'] == 'lobby_full'
        for p in ps:
            await p.send('set_ready', ready=True)
            await p.send('set_ready', ready=True)
        # Round trip on host ensures all other ready messages have been handled.
        while True:
            lobby = await ps[0].wait('lobby_state')
            if sum(s['ready'] for s in lobby['seats']) == humans: break
        await ps[0].send('start_match')
        previous_choices = -1
        while True:
            d = await ps[0].wait('draft_state', lambda m:sum(x is not None for x in m['choices'])>previous_choices)
            previous_choices = sum(x is not None for x in d['choices'])
            if d['turn_seat'] == -1: break
            owner = next(p for p,c in zip(ps,credentials) if c['seat'] == d['turn_seat'])
            await owner.send('choose_foundation', action_id='invalid', foundation='missing')
            assert (await owner.wait('action_ack', lambda m:m['action_id']=='invalid'))['status'] == 'invalid_foundation'
            await owner.send('choose_foundation', action_id='draft', foundation=d['available_foundations'][0])
        await extra.send('join_lobby', join_code=code)
        assert (await extra.wait('protocol_error'))['code'] == 'lobby_started'
        await extra.ws.close()
        initial = []
        for p,c in zip(ps,credentials):
            v = await p.wait('player_view')
            safe(v,c['seat'])
            assert v['tick']==0
            initial.append(v)
            await p.send('submit_action', action_id='policy', kind='set_reduce_waste', payload={'enabled':True})
            ack = await p.wait('action_ack', lambda m:m['action_id']=='policy')
            assert ack['status']=='queued'
        views = []
        for p,c in zip(ps,credentials):
            ack = await p.wait('action_ack', lambda m:m['phase']=='execution' and m['action_id']=='policy')
            assert ack['status']=='applied' and ack['execution_tick']==0
            v = await p.wait('player_view', lambda m:m['tick']==1)
            safe(v,c['seat'])
            assert v['own_city']['reduce_waste']
            views.append(v)
        assert all(v['public']==views[0]['public'] for v in views)
        lobby = next(m for m in reversed(ps[0].messages) if m['type']=='lobby_state')
        assert sum(s['controller']=='bot' for s in lobby['seats'])==4-humans
        p = ps[0]
        for field,value in [('player_id',3),('match_id','fake'),('command_seq',0),('command_id','fake'),('server_received_tick',0),('seat',3)]:
            correlation = await p.send('submit_action',action_id='spoof',kind='set_reduce_waste',payload={'enabled':False},**{field:value})
            error = await p.wait('protocol_error')
            assert error['code']=='invalid_fields' and error['correlation_id']==correlation
        await p.send('submit_action',action_id='policy',kind='set_reduce_waste',payload={'enabled':True})
        assert (await p.wait('action_ack',lambda m:m['action_id']=='policy' and m['phase']=='execution'))['status']=='applied'
        await p.send('submit_action',action_id='policy',kind='set_reduce_waste',payload={'enabled':False})
        assert (await p.wait('action_ack',lambda m:m['action_id']=='policy'))['status']=='action_id_conflict'
        await p.send('advance',seconds=100)
        assert (await p.wait('protocol_error'))['code']=='unknown_message'
        # Close immediately after submission, before observing any admission.
        uncertain_payload = {'building_type':'residential_district','tile':30}
        await p.send('submit_action', action_id='uncertain-build', kind='construct', payload=uncertain_payload)
        # Real disconnect/rejoin, credential rotation and retained acknowledgment recovery.
        await p.ws.close()
        re = await peer(url)
        ps[0] = re
        await re.send('rejoin',lobby_id=credentials[0]['lobby_id'],rejoin_token=credentials[0]['rejoin_token'])
        new = await re.wait('seat_assigned')
        assert new['seat']==credentials[0]['seat'] and new['rejoin_token']!=credentials[0]['rejoin_token']
        v = await re.wait('player_view')
        safe(v,new['seat'])
        assert v['tick']>=1
        assert (await re.wait('action_ack',lambda m:m['action_id']=='policy' and m['phase']=='execution'))['status']=='applied'
        await re.send('submit_action', action_id='uncertain-build', kind='construct', payload=uncertain_payload)
        receipt = await re.wait('action_ack', lambda m:m['action_id']=='uncertain-build' and m['phase']=='execution')
        assert receipt['status']=='applied'
        built = await re.wait('player_view', lambda m:any(b['tile']==30 for b in m['own_city']['buildings']))
        assert sum(b['tile']==30 for b in built['own_city']['buildings']) == 1
        thief = await peer(url)
        await thief.send('rejoin',lobby_id=credentials[0]['lobby_id'],rejoin_token=credentials[0]['rejoin_token'])
        assert (await thief.wait('protocol_error'))['code']=='authentication_failed'
        await thief.ws.close()
        next_view = await re.wait('player_view',lambda m:m['tick']>v['tick'])
        assert next_view['view_seq']>v['view_seq']
        replacement = await peer(url)
        await replacement.send('rejoin', lobby_id=new['lobby_id'], rejoin_token=new['rejoin_token'])
        rotated = await replacement.wait('seat_assigned')
        assert rotated['seat'] == new['seat'] and rotated['generation'] == new['generation']+1
        await asyncio.wait_for(re.ws.wait_closed(), 3)
        assert re.ws.close_code == 1008
        ps[0] = replacement
        safe(await replacement.wait('player_view'), rotated['seat'])
        print(f'PASS humans={humans} bots={4-humans}: draft, actions, views, authority, retries, reconnect',flush=True)
    finally:
        for p in ps: await p.ws.close()

async def edges(url):
    # Schema and lexical rejection over real WebSocket frames.
    p = await peer(url)
    for raw in ['{"v":1,"v":1}', '{"v":1,}', '{"x":1.0}', '{"x":9007199254740992}',
                '{"x":NaN}', '[]', ' '.join(['x']*17000)]:
        await p.ws.send(raw)
        await p.wait('protocol_error')
    await p.ws.send(b'{}')
    assert (await p.wait('protocol_error'))['code']=='binary_frame'
    await p.ws.close()
    p = await peer(url)
    for i in range(21):
        await p.ws.send(json.dumps(dict(v=1,type='set_ready',correlation_id=str(i),ready=True)))
    assert (await p.wait('protocol_error',lambda m:m['code']=='rate_limited'))['fatal']
    await p.ws.close()
    # A departed pre-start host releases its seat, transfers host, invalidates token.
    a,b=await peer(url),await peer(url)
    await a.send('create_lobby')
    old=await a.wait('seat_assigned')
    code=(await a.wait('lobby_created'))['join_code']
    await b.send('join_lobby',join_code=code)
    assigned=await b.wait('seat_assigned')
    await a.ws.close()
    lobby=await b.wait('lobby_state',lambda m:m['host_seat']==assigned['seat'])
    assert lobby['seats'][old['seat']]['controller']=='open'
    c=await peer(url)
    await c.send('rejoin',lobby_id=old['lobby_id'],rejoin_token=old['rejoin_token'])
    assert (await c.wait('protocol_error'))['code']=='authentication_failed'
    await c.send('join_lobby',join_code=code)
    assert (await c.wait('seat_assigned'))['seat']==old['seat']
    await c.send('join_lobby',join_code=code)
    assert (await c.wait('protocol_error'))['code']=='already_seated'
    await b.ws.close(); await c.ws.close()
    print('PASS real sockets: malformed/oversize/binary/rate limits, host transfer, seat reuse',flush=True)

async def marketplace_scenario(url):
    ps = [await peer(url) for _ in range(4)]
    try:
        await ps[0].send('create_lobby')
        credentials = [await ps[0].wait('seat_assigned')]
        code = (await ps[0].wait('lobby_created'))['join_code']
        for p in ps[1:]:
            await p.send('join_lobby', join_code=code)
            credentials.append(await p.wait('seat_assigned'))
        for p in ps: await p.send('set_ready', ready=True)
        while True:
            lobby = await ps[0].wait('lobby_state')
            if sum(s['ready'] for s in lobby['seats']) == 4: break
        await ps[0].send('start_match')
        foundations = ['industrial_legacy','circular_foundation','energy_pioneer','investment_district']
        choices = 0
        while choices < 4:
            draft = await ps[0].wait('draft_state', lambda m:sum(x is not None for x in m['choices']) == choices)
            seat = draft['turn_seat']
            assert foundations[seat] in draft['available_foundations']
            await ps[seat].send('choose_foundation', action_id=f'market-draft-{seat}', foundation=foundations[seat])
            choices += 1
        for p in ps: await p.wait('match_started')

        # Circular Foundation has genuine spare recyclable capacity; Industrial
        # has genuine recyclable-treatment need from its operating Factory.
        seller, buyer, outsider = ps[1], ps[0], ps[2]
        await seller.send('submit_action', action_id='market-offer', kind='publish_offer',
                          payload={'type':'recyclable','rate':10000000,'price':1000000})
        assert (await seller.wait('action_ack', lambda m:m['action_id']=='market-offer'))['status'] == 'queued'
        assert (await seller.wait('action_ack', lambda m:m['action_id']=='market-offer' and m['phase']=='execution'))['status'] == 'applied'
        buyer_view = await buyer.wait('player_view', lambda m:any(o['seller']==1 and o['type']=='recyclable' for o in m['marketplace']['offers']))
        offer = next(o for o in buyer_view['marketplace']['offers'] if o['seller']==1 and o['type']=='recyclable')

        await outsider.send('submit_action', action_id='market-steal', kind='withdraw_offer', payload={'id':offer['id']})
        assert (await outsider.wait('action_ack', lambda m:m['action_id']=='market-steal'))['status'] == 'queued'
        stolen = await outsider.wait('action_ack', lambda m:m['action_id']=='market-steal' and m['phase']=='execution')
        assert stolen['status'] == 'offer_not_owned'

        await buyer.send('submit_action', action_id='market-accept', kind='accept_quote', payload={'id':offer['id']})
        assert (await buyer.wait('action_ack', lambda m:m['action_id']=='market-accept'))['status'] == 'queued'
        assert (await buyer.wait('action_ack', lambda m:m['action_id']=='market-accept' and m['phase']=='execution'))['status'] == 'applied'
        buyer_final = await buyer.wait('player_view', lambda m:any(k.get('delivered',0)>0 and k.get('paid',0)>0 for k in m['own_city']['contracts']))
        seller_final = await seller.wait('player_view', lambda m:any(k.get('delivered',0)>0 and k.get('paid',0)>0 for k in m['own_city']['contracts']))
        contract = next(k for k in buyer_final['own_city']['contracts'] if k['delivered']>0)
        seller_contract = next(k for k in seller_final['own_city']['contracts'] if k['id']==contract['id'])
        assert seller_contract['delivered']==contract['delivered'] and seller_contract['paid']==contract['paid']
        outsider_view = await outsider.wait('player_view', lambda m:any(k['id']==contract['id'] for k in m['marketplace']['contracts']))
        public_contract = next(k for k in outsider_view['marketplace']['contracts'] if k['id']==contract['id'])
        assert 'delivered' not in public_contract and 'paid' not in public_contract and 'booking_micro' not in public_contract
        assert not outsider_view['notifications']
        safe(buyer_final, credentials[0]['seat']); safe(seller_final, credentials[1]['seat']); safe(outsider_view, credentials[2]['seat'])
        print(f"PASS marketplace: contract={contract['id']} delivered={contract['delivered']} paid={contract['paid']}", flush=True)
    finally:
        for p in ps: await p.ws.close()

async def draft_disconnect_scenario(url):
    async def start_draft(a, b):
        await a.send('create_lobby')
        first = await a.wait('seat_assigned')
        code = (await a.wait('lobby_created'))['join_code']
        await b.send('join_lobby', join_code=code)
        second = await b.wait('seat_assigned')
        await a.send('set_ready', ready=True); await b.send('set_ready', ready=True)
        while True:
            lobby = await a.wait('lobby_state')
            if sum(s['ready'] for s in lobby['seats']) == 2: break
        await a.send('start_match')
        draft = await a.wait('draft_state', lambda m:m['turn_seat'] in [first['seat'],second['seat']])
        return {first['seat']:(a,first), second['seat']:(b,second)}, draft

    a, b = await peer(url), await peer(url)
    peers = [a,b]
    try:
        owners, draft = await start_draft(a,b)
        disconnected, credential = owners[draft['turn_seat']]
        survivor = b if disconnected is a else a
        await disconnected.ws.close()
        cancelled = await survivor.wait('match_cancelled')
        assert cancelled['reason']=='draft_rejoin_timeout' and cancelled['seat']==credential['seat']
        await survivor.send('choose_foundation', action_id='stale-draft', foundation=draft['available_foundations'][0])
        assert (await survivor.wait('protocol_error'))['code']=='not_seated'
        thief = await peer(url); peers.append(thief)
        await thief.send('rejoin', lobby_id=credential['lobby_id'], rejoin_token=credential['rejoin_token'])
        assert (await thief.wait('protocol_error'))['code']=='authentication_failed'
        await thief.ws.close()

        replacement = await peer(url); peers.append(replacement)
        owners, draft = await start_draft(survivor,replacement)
        disconnected, credential = owners[draft['turn_seat']]
        await disconnected.ws.close()
        rejoined = await peer(url); peers.append(rejoined)
        await rejoined.send('rejoin', lobby_id=credential['lobby_id'], rejoin_token=credential['rejoin_token'])
        assigned = await rejoined.wait('seat_assigned')
        assert assigned['seat']==credential['seat'] and assigned['rejoin_token']!=credential['rejoin_token']
        resumed = await rejoined.wait('draft_state')
        assert resumed['turn_seat']==assigned['seat']
        await rejoined.send('choose_foundation', action_id='resumed-draft', foundation=resumed['available_foundations'][0])
        assert (await rejoined.wait('action_ack', lambda m:m['action_id']=='resumed-draft'))['status']=='applied'
        print('PASS draft disconnect: timeout cancellation, cleanup, secure rejoin and resumed selection', flush=True)
    finally:
        for p in peers:
            await p.ws.close()

async def terminal_scenario(url):
    with tempfile.TemporaryDirectory(prefix='equilibrium-cli-') as directory:
        credentials = Path(directory) / 'session.json'
        credentials.write_text('{}')
        credentials.chmod(0o644)
        client = await asyncio.create_subprocess_exec(
            sys.executable, str(ROOT / 'client/terminal/client.py'),
            '--url', url, '--create', '--session-file', str(credentials), '--json',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        selected, policy_id = False, None
        try:
            while True:
                raw = await asyncio.wait_for(client.stdout.readline(), 5)
                assert raw, 'terminal exited before action execution'
                if not raw.startswith(b'{'): continue
                m = json.loads(raw)
                assert 'rejoin_token' not in m
                if m['type'] == 'lobby_created':
                    client.stdin.write(b'ready\nstart\n')
                if m['type'] == 'draft_state' and m['turn_seat'] != -1 and not selected:
                    client.stdin.write(('foundation '+m['available_foundations'][0]+'\n').encode())
                    selected = True
                if m['type'] == 'match_started':
                    client.stdin.write(b'action set_reduce_waste {"enabled":true}\n')
                if m['type'] == 'action_ack':
                    if m['status'] == 'queued': policy_id = m['action_id']
                    if m['action_id'] == policy_id and m['phase'] == 'execution':
                        assert m['status'] == 'applied'
                        break
                await client.stdin.drain()
            assert credentials.stat().st_mode & 0o777 == 0o600
            assert json.loads(credentials.read_text())['rejoin_token']
            client.stdin.write(b'quit\n')
            await client.stdin.drain()
            status = await asyncio.wait_for(client.wait(), 3)
            assert status == 0, (await client.stderr.read()).decode()
            print('PASS terminal CLI: lobby, draft, action, private credentials, clean quit', flush=True)
        finally:
            if client.returncode is None:
                client.kill()
                await client.wait()

async def main(humans, terminal=False, marketplace=False, draft_disconnect=False):
    with socket.socket() as s:
        s.bind(('127.0.0.1',0)); port=s.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix='equilibrium-net-') as directory:
        d=Path(directory)
        with (d/'server.log').open('w') as log:
            command = ['godot','--headless','--path',str(ROOT),'--log-file',str(d/'godot.log'),'--script','server/main.gd','--','--port',str(port),'--stop-file',str(d/'stop')]
            if draft_disconnect: command += ['--draft-grace-ms','750']
            process=subprocess.Popen(command,stdout=log,stderr=log)
            try:
                for _ in range(100):
                    if 'ONLINE_READY' in (d/'server.log').read_text(): break
                    if process.poll() is not None: raise RuntimeError((d/'server.log').read_text())
                    await asyncio.sleep(.02)
                url = f'ws://127.0.0.1:{port}'
                if terminal: await terminal_scenario(url)
                elif marketplace: await marketplace_scenario(url)
                elif draft_disconnect: await draft_disconnect_scenario(url)
                elif humans == 0: await edges(url)
                else: await scenario(url, humans)
            finally:
                (d/'stop').touch()
                try: process.wait(timeout=3)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
                text=(d/'server.log').read_text()
                assert 'SCRIPT ERROR' not in text, text
                assert process.returncode==0, text

if __name__=='__main__':
    p = argparse.ArgumentParser()
    selection = p.add_mutually_exclusive_group(required=True)
    selection.add_argument('--humans', type=int, choices=range(0,5))
    selection.add_argument('--terminal', action='store_true')
    selection.add_argument('--marketplace', action='store_true')
    selection.add_argument('--draft-disconnect', action='store_true')
    args = p.parse_args()
    asyncio.run(main(args.humans, args.terminal, args.marketplace, args.draft_disconnect))
