import time
import numpy as np
import torch
from rebar import arrdict
from pavlov import stats, logs, runs, storage, git
from . import hex, mcts, networks, learning, validation, analysis, arena, leagues
from torch.nn import functional as F
from logging import getLogger
from itertools import cycle

log = getLogger(__name__)

@torch.no_grad()
def chunk_stats(chunk, n_new):
    with stats.defer():
        tail = chunk[-n_new:]
        d, t = tail.decisions, tail.transitions
        n_trajs = t.terminal.sum()
        n_inputs = t.terminal.size(0)
        n_samples = t.terminal.nelement()
        n_sims = d.n_sims.sum()
        stats.rate('sample-rate.actor', n_samples)
        stats.mean('traj-length', n_samples, n_trajs)
        stats.cumsum('count.traj', n_trajs)
        stats.cumsum('count.inputs', n_inputs)
        stats.cumsum('count.chunks', 1)
        stats.cumsum('count.samples', n_samples)
        stats.cumsum('count.sims', n_sims)
        stats.rate('step-rate.chunks', 1)
        stats.rate('step-rate.inputs', n_inputs)
        stats.rate('sim-rate', n_sims)
        stats.mean('mcts-n-leaves', d.n_leaves.float().mean())

        rewards = t.rewards.sum(0).sum(0)
        for i, r in enumerate(rewards):
            stats.mean(f'reward.seat-{i}', r, n_trajs)

        d, t = chunk.decisions, chunk.transitions
        v = d.v[t.terminal]
        r = t.rewards[t.terminal]
        stats.mean('progress.terminal-corr', ((v - v.mean())*(r - r.mean())).mean()/(v.var()*r.var())**.5)

        v = d.v[:-1][t.terminal[1:]]
        r = t.rewards[1:][t.terminal[1:]]
        stats.mean('progress.terminal-1-corr', ((v - v.mean())*(r - r.mean())).mean()/(v.var()*r.var())**.5)

def rel_entropy(logits, valid):
    zeros = torch.zeros_like(logits)
    logits = logits.where(valid, zeros)
    probs = logits.exp().where(valid, zeros)
    return (-(logits*probs).sum(-1).mean(), torch.log(valid.sum(-1).float()).mean())

def optimize(network, opt, batch):
    w, d0, t = batch.worlds, batch.decisions, batch.transitions
    mask = d0.is_prime
    d = network(w)

    zeros = torch.zeros_like(d.logits)
    policy_loss = -(d0.logits.exp()*d.logits).where(w.valid, zeros).sum(axis=-1)[mask].mean()

    terminal = torch.stack([t.terminal for _ in range(w.n_seats)], -1)
    target_value = learning.reward_to_go(t.rewards, d0.v, terminal, terminal, gamma=1)
    value_loss = (target_value - d.v).square()[mask].mean()
    
    loss = policy_loss + value_loss 
    
    opt.zero_grad()
    loss.backward()

    old = torch.cat([p.flatten() for p in network.parameters()])
    opt.step()
    new = torch.cat([p.flatten() for p in network.parameters()])

    with stats.defer():
        #TODO: Contract these all based on late-ness
        stats.mean('loss.value', value_loss)
        stats.mean('loss.policy', policy_loss)
        stats.mean('progress.resid-var', (target_value - d.v).pow(2).mean(), target_value.pow(2).mean())
        stats.mean('progress.target-kl-div', (d0.logits - d.logits).mul(d0.logits.exp()).where(w.valid, zeros).sum(-1).mean())
        stats.mean('progress.prior-kl-div', (d0.prior - d.logits).mul(d0.prior.exp()).where(w.valid, zeros).sum(-1).mean())

        stats.mean('rel-entropy.policy', *rel_entropy(d.logits, w.valid)) 
        stats.mean('rel-entropy.targets', *rel_entropy(d0.logits, w.valid))

        stats.mean('v-target.mean', target_value.mean())
        stats.mean('v-target.std', target_value.std())

        stats.rate('sample-rate.learner', t.terminal.nelement())
        stats.rate('step-rate.learner', 1)
        stats.cumsum('count.learner-steps', 1)
        # stats.rel_gradient_norm('rel-norm-grad', agent)

        stats.mean('opt.lr', np.mean([p['lr'] for p in opt.param_groups]))
        stats.mean('opt.step-std', (new - old).pow(2).mean().pow(.5))
        stats.mean('opt.step-max', (new - old).abs().max())

def worldfunc(n_envs, device='cuda'):
    return hex.Hex.initial(n_envs=n_envs, boardsize=9, device=device)

def agentfunc(device='cuda', n_opponents=0):
    worlds = worldfunc(n_envs=1, device=device)
    network = networks.SimpleNetwork(worlds.obs_space, worlds.action_space).to(worlds.device)
    # network.trace(worlds)
    return mcts.MCTSAgent(network, n_nodes=64)

def warm_start(agent, opt, parent):
    if parent:
        parent = runs.resolve(parent)
        sd = storage.load_latest(parent, device='cuda')
        agent.load_state_dict(sd['agent'])
        opt.load_state_dict(sd['opt'])
    return parent

def run(device='cuda'):
    buffer_length = 32 
    batch_size = 64*1024
    n_envs = 4*1024
    buffer_inc = batch_size//n_envs

    worlds = worldfunc(n_envs, device=device)
    agent = agentfunc(device, n_opponents=4)
    opt = torch.optim.Adam(agent.evaluator.prime.parameters(), lr=1e-2, amsgrad=True)
    sched = torch.optim.lr_scheduler.LambdaLR(opt, lambda e: min(e/100, 1))
    # league = leagues.SimpleLeague(32, device=device)

    parent = warm_start(agent, opt, '')

    run = runs.new_run('league-net-test-no-oppo', boardsize=worlds.boardsize, parent=parent)

    git.tag(run, error=False)

    buffer = []
    idxs = cycle(learning.batch_indices(buffer_length, n_envs, batch_size, worlds.device))
    with logs.to_run(run), stats.to_run(run), \
            arena.monitor(run, worldfunc, agentfunc, device=worlds.device):
        while True:

            while len(buffer) < buffer_length:
                # league.update(agent)
                with torch.no_grad():
                    decisions = agent(worlds, value=True)
                new_worlds, transition = worlds.step(decisions.actions)
                buffer.append(arrdict.arrdict(
                    worlds=worlds,
                    decisions=decisions,
                    transitions=transition).detach())
                worlds = new_worlds
                # league.record(transition)
                log.info('actor stepped')
                
            chunk = arrdict.stack(buffer)
            chunk_stats(chunk, buffer_inc)

            optimize(agent.evaluator.prime, opt, chunk[:, next(idxs)])
            sched.step()
            log.info('learner stepped')
            
            buffer = buffer[buffer_inc:]

            sd = storage.state_dicts(agent=agent, opt=opt)
            storage.throttled_latest(run, sd, 60)
            storage.throttled_snapshot(run, sd, 900)
            stats.gpu(worlds.device, 15)

def benchmark_experience_collection(n_envs=8192, T=16):
    import pandas as pd

    if n_envs is None:
        ns = np.logspace(0, 15, 16, base=2, dtype=int)
        return pd.Series({n: benchmark_experience_collection(n) for n in ns})

    torch.manual_seed(0)
    worlds = worldfunc(n_envs)
    agent = agentfunc(n_opponents=4)

    agent(worlds) # warmup

    torch.cuda.synchronize()
    start = time.time()
    for _ in range(T):
        decisions = agent(worlds)
        new_worlds, transition = worlds.step(decisions.actions)
        worlds = new_worlds
        log.info('actor stepped')
    torch.cuda.synchronize()
    rate = (T*n_envs)/(time.time() - start)
    print(f'{n_envs}: {rate}/sample')

    return rate