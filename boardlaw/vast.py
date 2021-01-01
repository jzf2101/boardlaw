# Build and push image to DockerHub (might have to do this manually)
# Find an appropriate vast machine
# Create a machine using their commandline
# Pass onstart script to start the job
# Use vast, ssh, rsync to monitor things (using fabric and patchwork?)
# Manual destroy
import pandas as pd
import json
from subprocess import check_output
from pathlib import Path
import aljpy
from fabric import Connection
from patchwork.transfers import rsync

DISK = 10
MAX_DPH = .5
MAX_INSTANCES = 3

def set_key():
    target = Path('~/.vast_api_key').expanduser()
    if not target.exists():
        key = json.loads(Path('credentials.json').read_text())['vast']
        target.write_text(key)

def invoke(command):
    set_key()
    return check_output(f'vast {command}', shell=True).decode()

def offers():
    js = json.loads(invoke(f'search offers --raw --storage {DISK}'))
    return pd.DataFrame.from_dict(js)

def suggest():
    o = offers()
    viable = o.query('gpu_name == "RTX 2080 Ti" & num_gpus == 1')
    return viable.sort_values('dph_total').iloc[0]

def launch():
    s = suggest()
    assert s.dph_total < MAX_DPH
    assert status() is None or len(status()) < MAX_INSTANCES
    label = aljpy.humanhash(n=2)
    resp = invoke(f'create instance {s.id}'
        ' --image andyljones/boardlaw'
        ' --onstart-cmd "tini -- dev.sh"'
        f' --disk {DISK}'
        f' --label {label}'
        ' --raw') 
    # Need to slice off the first chars of 'Started.', which are for some reason
    # printed along with the json. 
    # resp = resp[8:]
    resp = json.loads(resp)
    assert resp['success']
    return label

def destroy(label):
    id = status(label).id
    resp = invoke(f'destroy instance {id} --raw')
    assert resp.startswith('destroying instance')

def status(label=None):
    if label:
        s = status()
        if s is None: 
            raise ValueError('No instances')
        else:
            return s.loc[label]
    js = json.loads(invoke('show instances --raw'))
    if js:
        return pd.DataFrame.from_dict(js).set_index('label')

def connection(label):
    # Get the vast key into place: `docker cp ~/.ssh/boardlaw_rsa boardlaw:/root/.ssh/`
    # Would be better to use SSH agent forwarding, if vscode's worked reliably :(
    s = status(label)
    return Connection(
        host=s.ssh_host, 
        user='root', 
        port=int(s.ssh_port), 
        connect_kwargs={'key_filename': ['/root/.ssh/vast_rsa']})
    
def ssh_command(label):
    s = status(label)
    print(f'SSH_AUTH_SOCK="" ssh root@{s.ssh_host} -p {s.ssh_port} -o StrictHostKeyChecking=no -i /root/.ssh/vast_rsa')

def setup(label):
    conn = connection(label)
    conn.run('touch /root/.no_auto_tmux')
    conn.run('rm /etc/banner')
    
def deploy(label):
    conn = connection(label)
    rsync(conn, 
        source='.',
        target='/code',
        exclude=('.*', 'output', '**/__pycache__', '*.egg-info'),
        include=('output/arena/mohex-*',),
        strict_host_keys=False)

def install(label):
    conn = connection(label)
    conn.run('pip install -e /code/rebar')
    conn.run('pip install -e /code/activelo')

def run(label):
    conn = connection(label)
    conn.run('cd /code && python -c "from boardlaw.main import *; run()"')

def fetch(label):
    # TODO
    pass

def demo():
    label = launch()

    setup(label)
    deploy(label)
    install(label)
    run(label)