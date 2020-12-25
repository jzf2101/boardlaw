import numpy as np
import torch
from . import heads
from torch import nn
from rebar import recurrence, arrdict
from torch.nn import functional as F

class Residual(nn.Module):

    def __init__(self, width, gain=1):
        # "Identity Mappings in Deep Residual Networks"
        super().__init__()
        self.w0 = nn.Linear(width, width, bias=False)
        self.n0 = nn.LayerNorm(width)
        self.w1 = nn.Linear(width, width, bias=False)
        self.n1 = nn.LayerNorm(width)

        nn.init.orthogonal_(self.w0.weight)
        nn.init.orthogonal_(self.w1.weight, gain=gain)

    def forward(self, x, *args, **kwargs):
        y = self.n0(x)
        y = F.relu(y)
        y = self.w0(y)
        y = self.n1(y)
        y = F.relu(y)
        y = self.w1(y)
        return x + y

class Network(nn.Module):

    def __init__(self, obs_space, action_space, width=128, layers=8):
        super().__init__()
        self.policy = heads.output(action_space, width)
        self.sampler = self.policy.sample

        blocks = [heads.intake(obs_space, width)]
        for _ in range(layers):
            blocks.append(Residual(width, 1/layers**.5)) 
        self.body = recurrence.Sequential(*blocks)

        self.value = heads.ValueOutput(width)

    # def trace(self, world):
    #     self.policy = torch.jit.trace_module(self.policy, {'forward': (world.obs, world.valid)})
    #     self.vaue = torch.jit.trace_module(self.value, {'forward': (world.obs, world.valid, world.seats)})

    def forward(self, world, value=False):
        neck = self.body(world.obs)
        outputs = arrdict.arrdict(
            logits=self.policy(neck, valid=world.valid))

        if value:
            #TODO: Maybe the env should handle this? 
            # Or there should be an output space for values? 
            outputs['v'] = self.value(neck, valid=world.valid, seats=world.seats)
        return outputs

def check_var():
    from boardlaw.main.common import worldfunc
    import pandas as pd

    worlds = worldfunc(256)
    stds = {}
    for n in range(1, 20):
        net = Network(worlds.obs_space, worlds.action_space, layers=n).cuda()

        obs = torch.rand_like(worlds.obs)
        obs.requires_grad = True            
        
        l = net.body(obs)
        sf = l.std().item()
        
        for p in net.parameters():
            p.grad = None
        
        l.sum().backward()
        
        stds[n] = {'forward': sf, 'backward': obs.grad.std().item()}
    stds = pd.DataFrame(stds).T
