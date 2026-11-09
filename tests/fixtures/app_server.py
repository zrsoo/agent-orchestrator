"""Deterministic JSON-RPC fault server; runs as a real child process."""
import json
from pathlib import Path
import sys
import time

workspace = None
session = 'fixture-session'
turn_number = 0


def send(msg):
    print(json.dumps(msg), flush=True)


for line in sys.stdin:
    msg = json.loads(line)
    method, params = msg.get('method'), msg.get('params', {})
    if 'id' not in msg:
        continue
    result = {}
    if method == 'initialize':
        result = {'userAgent': 'fixture/1'}
    elif method == 'account/rateLimits/read':
        result = {'rateLimits': {'primary': {'usedPercent': 10, 'windowDurationMins': 300, 'resetsAt': time.time()+100}}}
        if workspace and (workspace/'quota.json').exists():
            result = json.loads((workspace/'quota.json').read_text())
    elif method == 'fixture/notify':
        send({'id':msg['id'],'result':{}})
        send({'method':'account/rateLimits/updated','params':params})
        continue
    elif method in ('thread/start', 'thread/resume'):
        workspace = Path(params['cwd'])
        session = params.get('threadId', session)
        result = {'thread': {'id': session}}
    elif method == 'turn/start':
        turn_number += 1
        if workspace and (workspace/'reject-input').exists():
            (workspace/'rejected-input.json').write_text(json.dumps({
                'requests': turn_number, 'threadId': params['threadId'],
                'prompt_chars': len(params['input'][0]['text'])}))
            send({'id': msg['id'], 'error': {'code': -32600, 'message': 'input_too_large'}})
            continue
        turn = f'fixture-turn-{turn_number}'
        send({'id':msg['id'], 'result':{'turn':{'id':turn,'status':'inProgress'}}})
        prompt = params['input'][0]['text']
        if 'Do not implement it yet' in prompt:
            output = {'tasks':[{'id':'task','workspace':str(workspace),'objective':'write output.txt once',
                                 'acceptance':['output.txt contains done exactly once']}]}
        else:
            marker = workspace / 'output.txt'
            if not marker.exists():
                marker.write_text('done')
            if (workspace/'cutoff-once').exists():
                (workspace/'cutoff-once').unlink()
                send({'method':'turn/completed','params':{'threadId':session,'turn':{'id':turn,'status':'failed',
                      'error':{'message':'Usage limit reached','codexErrorInfo':'usageLimitExceeded'}}}})
                continue
            if (workspace / 'hold').exists():
                send({'method':'item/completed','params':{'threadId':session,'turnId':turn,
                       'item':{'type':'commandExecution','status':'completed','command':'write output.txt'}}})
                time.sleep(60)
            output = {'status':'done','validators':[{'command':['{python}','-c',"from pathlib import Path; assert Path('output.txt').read_text() == 'done'"],'criteria':[0]}]}
        send({'method':'item/completed','params':{'threadId':session,'turnId':turn,
              'item':{'type':'agentMessage','phase':'commentary','text':'Working on it.'}}})
        send({'method':'account/rateLimits/updated','params':{'rateLimits':{'primary':{'usedPercent':10,'windowDurationMins':300,'resetsAt':time.time()+100}}}})
        send({'method':'item/completed','params':{'threadId':session,'turnId':turn,
              'item':{'type':'agentMessage','phase':'final_answer','text':json.dumps(output)}}})
        send({'method':'turn/completed','params':{'threadId':session,'turn':{'id':turn,'status':'completed','error':None}}})
        continue
    send({'id':msg['id'],'result':result})
