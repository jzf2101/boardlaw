import torch

def deinterlace(batch):
    player = batch.inputs.player
    T, B = player.shape

    ts, bs = torch.meshgrid(
            torch.arange(T, device=player.device),
            torch.arange(B, device=player.device))

    ts_inv = torch.full_like(player, -1, dtype=torch.long)
    resets = torch.full_like(player, False, dtype=torch.bool)
    totals = ts.new_zeros(B)
    for p in [0, 1]:
        mask = batch.inputs.player == p
        ts_inv[mask] = (totals[None, :] + mask.cumsum(0) - 1)[mask]
        
        totals += mask.sum(0)
        resets[totals-1, bs[0]] = True
    
    us = torch.full_like(ts, -1)
    us[ts_inv, bs] = ts

    batch = batch[us, bs]
    if 'reset' in batch.inputs:
        batch['inputs']['reset'] = resets | batch.inputs.reset
    else:
        batch['inputs']['reset'] = resets

    return batch