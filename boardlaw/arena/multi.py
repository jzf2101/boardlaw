import time
import pandas as pd
import numpy as np
import torch
from rebar import arrdict, dotdict
from logging import getLogger

log = getLogger(__name__)

def scatter_add_(totals, indices, vals=None):
    assert indices.ndim == 2
    rows, cols = indices.T

    width = totals.shape[1]
    raveled = rows + width*cols

    if vals is None:
        vals = totals.new_ones((len(rows),))
    if isinstance(vals, (int, float)):
        vals = totals.new_full((len(rows),), vals)

    totals.view(-1).scatter_add_(0, raveled, vals)

def live_indices(residual):
    # Want to have residual[i, j] copies of (i, j) in the output

    # More than 100m envs is gonna run into memory issues; it's about 80 bytes/env IIRC.
    assert residual.sum() < 100*1024*1024 

    residual = residual.int()
    idxs = residual.nonzero(as_tuple=False)
    full = idxs[:, None, :].repeat(1, residual.max(), 1)
    counts = residual[tuple(idxs.T)]

    mask = torch.arange(full.shape[1], device=full.device)[None, :] < counts[:, None]

    return full[mask]

class Tracker:

    def __init__(self, n_envs_per, games, max_dispatch=32*1024, device='cuda', verbose=False):
        assert (games.index == games.columns).all()
        self.n_envs_per = n_envs_per
        self.max_dispatch = max_dispatch
        self.names = list(games.index)

        # Counts games that are either in-progress or that have been completed
        # self.games = torch.zeros((len(names), len(names)), dtype=torch.int, device=device)
        games = games.reindex(index=self.names, columns=self.names).values
        games[np.diag_indices_from(games)] = n_envs_per

        self.games = torch.as_tensor(games, device=device).int()
        self.init_games = self.games.sum()

        residual = n_envs_per - self.games
        self.live = live_indices(residual)
        self.verbose = verbose

        self.n_envs = len(self.live)

    def report(self):
        done = self.games.sum() - self.init_games
        remaining = self.games.nelement()*self.n_envs_per - self.games.sum()
        return int(done), int(remaining)

    def update(self, terminal, mask):
        # Kill off the finished games
        masked = torch.zeros_like(mask)
        masked[mask] = terminal

        terminated = self.live[masked]

        self.live[masked] = -1
        if self.verbose:
            log.debug(f'Marked as terminated: {list(masked.cpu().int().numpy())}')

        return terminated

    def finished(self):
        return (self.live == -1).all()

    def suggest(self, seats):
        active = self.live.gather(1, seats.long()[:, None]).squeeze(1)
        live_active = active[active > -1]
        totals = torch.zeros_like(self.games[0])
        totals.scatter_add_(0, live_active, totals.new_ones(live_active.shape))

        suggestion = totals.argmax()
        mask = (active == suggestion)
        mask = mask & (mask.cumsum(0) < self.max_dispatch)
        name = self.names[int(suggestion)]

        if self.verbose:
            log.debug(f'Suggesting agent {name} with mask {list(mask.int().cpu().numpy())}')

        return name, mask, self.live[mask]

class Evaluator:
    # Idea: keep lots and lots of envs in memory at once, play 
    # every agent against every agent simultaneously
    
    def __init__(self, worldfunc, agents, games=None, n_envs_per=1024, device='cuda'):
        self.agents = agents

        if games is None:
            games = pd.DataFrame(0, list(agents), list(agents))
        else:
            assert set(agents) == set(games.index)
            assert set(agents) == set(games.columns)

        self.tracker = Tracker(n_envs_per, games, device=device)
        self.worlds = worldfunc(self.tracker.n_envs).to(device)

        n_agents = len(agents)
        self.stats = arrdict.arrdict(
            indices=torch.stack(torch.meshgrid(torch.arange(n_agents), torch.arange(n_agents)), -1),
            wins=torch.zeros((n_agents, n_agents, self.worlds.n_seats), dtype=torch.int),
            moves=torch.zeros((n_agents, n_agents,), dtype=torch.int),
            times=torch.zeros((n_agents, n_agents), dtype=torch.float)).to(device)

        self.steps = 0
        self.bad_masks = 0

        self.start = time.time()

    def finished(self):
        return self.tracker.finished()

    def record(self, transitions, live, start, end):
        #TODO: Figure out how to get scatter_add_ to work on vector-valued vals
        wins = (transitions.rewards == 1).int()
        scatter_add_(self.stats.wins[:, :, 0], live, wins[:, 0])
        scatter_add_(self.stats.wins[:, :, 1], live, wins[:, 1])
        scatter_add_(self.stats.moves, live, 1) 
        scatter_add_(self.stats.times, live, (end - start)/transitions.terminal.size(0))

        done = self.stats.wins.sum(-1) == self.tracker.n_envs_per
        stats = self.stats[done].cpu()
        results = []
        for idx in range(stats.indices.size(0)):
            item = stats[idx]
            names = tuple(self.tracker.names[i] for i in item.indices)
            results.append(dotdict.dotdict(
                        names=names,
                        wins=tuple(map(float, item.wins)),
                        moves=float(item.moves),
                        games=float(sum(item.wins)),
                        times=float(item.times),
                        boardsize=self.worlds.boardsize))
        
        self.stats.wins[done] = -1

        return results
    
    def report(self):
        duration = time.time() - self.start
        done, remaining = self.tracker.report()
        to_go = pd.to_timedelta(duration/done*remaining, unit='s')
        forecast = pd.Timestamp.now(None) + to_go

        game_rate = done/duration
        match_rate = game_rate/self.tracker.n_envs_per
        move_rate = self.stats.moves.sum()/duration

        mask_size = self.stats.moves.sum()/self.steps
        bad_rate = self.bad_masks/self.steps

        rem = to_go.components
        print(f'{done/(done+remaining):.1%} done, {rem.days}d{rem.hours:02d}h{rem.minutes:02d}m to go, will finish {forecast:%a %d %b %H:%M}.')
        print(f'{move_rate:.0f} moves/sec, {game_rate:.0f} games/sec, {60*match_rate:.0f} matchups/min.')
        print(f'{mask_size:.0f} average mask size, {bad_rate:.0%} bad.')

    def step(self):
        name, mask, live = self.tracker.suggest(self.worlds.seats)
        
        self.steps += 1
        self.bad_masks += int(mask.sum() < 8*1024)
        
        start = time.time()
        decisions = self.agents[name](self.worlds[mask])
        self.worlds[mask], transitions = self.worlds[mask].step(decisions.actions)
        end = time.time()

        self.tracker.update(transitions.terminal, mask)

        results = self.record(transitions, live, start, end)
        return results

class MockAgent:

    def __init__(self, id):
        self.id = id

    def __call__(self, world):
        id = torch.full((world.n_envs,), self.id, device=world.device, dtype=torch.long)
        return arrdict.arrdict(actions=id)

class MockGame(arrdict.namedarrtuple(fields=('count', 'history'))):

    @classmethod
    def initial(cls, n_envs=1, length=4, device='cuda'):
        return cls(
            history=torch.full((n_envs, length), -1, dtype=torch.long, device=device),
            count=torch.full((n_envs,), 0, dtype=torch.long, device=device))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not isinstance(self.count, torch.Tensor):
            return 

        self.n_envs = self.count.shape[0]
        self.device = self.count.device 
        self.n_seats = 2

        self.valid = torch.ones(self.count.shape + (2,), dtype=torch.bool, device=self.device)

    @property
    def seats(self):
        return self.count % self.n_seats

    def step(self, actions):
        history = self.history.clone()
        history.scatter_(1, self.count[:, None], actions[:, None])

        count = self.count + 1
        terminal = (count == self.history.shape[1])
        transition = arrdict.arrdict(terminal=terminal)

        count[terminal] = 0
        
        world = type(self)(count=count, history=history)

        return world, transition, list(history[terminal])

def test_tracker():
    n_envs_per = 4
    length = 8

    agents = {i: MockAgent(i) for i in range(16)}
    games = pd.DataFrame(0, list(agents), list(agents))
    tracker = Tracker(n_envs_per, games, device='cpu')

    worlds = MockGame.initial(tracker.n_envs, length=length, device=tracker.games.device)

    hists = []
    while not tracker.finished():
        name, mask, _ = tracker.suggest(worlds.seats)
        
        decisions = agents[name](worlds)
        worlds[mask], transitions, hist = worlds[mask].step(decisions.actions)
        hists.extend(hist)

        tracker.update(transitions.terminal, mask)
    hists = torch.stack(hists).cpu().numpy()

    from collections import defaultdict
    counts = defaultdict(lambda: 0)
    for h in hists:
        assert len(set(h)) <= 2
        counts[tuple(h[:2])] += 1

    assert len(counts) == len(agents)*(len(agents)-1)
    assert set(counts.values()) == {n_envs_per}


def test_evaluator():
    from pavlov import runs, storage
    from boardlaw.arena import common

    df = runs.pandas(description='cat/nodes')
    worlds = common.worlds(df.index[0], 256*1024, device='cuda')
    agents = {}
    for r in df.index:
        snaps = storage.snapshots(r)
        for i in snaps:
            agents[f'{r}.{i}'] = common.agent(r, i, worlds.device)

    evaluator = Evaluator(worlds, agents, 512)

    from IPython import display

    results = []
    while not evaluator.finished():
        results.extend(evaluator.step())
        
        display.clear_output(wait=True)
        evaluator.report()