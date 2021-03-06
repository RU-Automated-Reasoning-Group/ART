""" Apply input bisection for Neural Network verification / certification / falsification. """

import logging
import sys
from pathlib import Path
from typing import Tuple, Optional, Union

import torch
from torch import Tensor, nn, autograd
from torch.utils.data import DataLoader

from diffabs import AbsDom, AbsData

sys.path.append(str(Path(__file__).resolve().parent.parent))

from art.prop import AbsProp
from art.utils import valid_lb_ub, total_area


def empty_like(t: Tensor) -> Tensor:
    """ Empty tensor (different from torch.empty() for concatenation. """
    return torch.tensor([], device=t.device)


def cat0(*ts: Tensor) -> Tensor:
    """ Usage: simplify `torch.cat((ts1, ts2), dim=0)` to `cat0(ts1, ts2)`. """
    return torch.cat(ts, dim=0)


class Bisecter(object):
    """ NN Verifier using bisection based input refinement and over-approximation.

        Basic algorithm:
        (1) For each input region, it tries to prove either safety or violation by over-approximation.
        (2) If failed, the input region is bisected to a total set of smaller ones, via different heuristics.
        (3) All these input regions are maintained in a work list, algorithm ends when the work list is empty.
    """

    def __init__(self, domain: AbsDom, prop: AbsProp):
        """
        :param domain: the abstract domain module to use
        """
        self.d = domain
        self.prop = prop
        return

    def _grad_dists_of_batch(self, new_lb: Tensor, new_ub: Tensor, new_extra: Optional[Tensor], forward_fn: nn.Module,
                             grad_src: str) -> Tuple[Tensor, Tensor, Tensor]:
        """ Get the gradient value for each abstraction as heuristic, as long as safe distances and violation distances.
        :param loss_src: either 'safe' or 'viol' or 'both'
        """
        new_lb = new_lb.detach().requires_grad_()
        new_ub = new_ub.detach().requires_grad_()
        if new_lb.grad is not None:
            new_lb.grad.zero_()  # in case any previous grads are unexpectedly accumulated
        if new_ub.grad is not None:
            new_ub.grad.zero_()

        ins = self.d.Ele.by_intvl(new_lb, new_ub)
        outs = forward_fn(ins)

        new_safe_dist = self.prop.safe_dist(outs) if new_extra is None else self.prop.safe_dist(outs, new_extra)
        new_viol_dist = self.prop.viol_dist(outs) if new_extra is None else self.prop.viol_dist(outs, new_extra)
        if grad_src == 'safe':
            grad_dist = new_safe_dist
        elif grad_src == 'viol':
            grad_dist = new_viol_dist
        elif grad_src == 'both':
            grad_dist = new_safe_dist + new_viol_dist
        else:
            raise ValueError(f'Invalid grad_src = {grad_src}.')

        ''' Sum safe/viol_dists to get one single value for backprop. Otherwise it needs to pass in 'grad_outputs'
            argument for autograd.grad(). e.g., use ones_like(dists).
            1st order summation will distribute the original output distance to each corresponding input.
            After all, it only needs to relatively compare input grads.
        '''
        assert grad_dist.dim() == 1, 'Do I need to squeeze the losses into <Batch> vector first?'
        losses = grad_dist.sum()

        # back-propagate safety loss to inputs
        grads = autograd.grad(losses, [new_lb, new_ub])
        # Clip grads below. LB should ++. Thus grad for LB should < 0 (LB' = LB - lr * grad). Similar for UB.
        grads[0].clamp_(max=0.)  # LB
        grads[1].clamp_(min=0.)  # UB
        new_grad = sum([g.abs() for g in grads])  # get one value for each abstraction
        return new_grad, new_safe_dist, new_viol_dist

    def _grad_dists_of(self, new_lb: Tensor, new_ub: Tensor, new_extra: Optional[Tensor], forward_fn: nn.Module,
                       grad_src: str, batch_size: int) -> Tuple[Tensor, Tensor, Tensor]:
        """ Dispatch the computation to be batch-by-batch.
        :param batch_size: compute the gradients batch-by-batch, so as to avoid huge memory consumption at once.
        """
        absset = AbsData(new_lb, new_ub, new_extra)
        abs_loader = DataLoader(absset, batch_size=batch_size, shuffle=False)

        split_grads, split_safe_dists, split_viol_dists = [], [], []
        for batch in abs_loader:
            if new_extra is None:
                batch_lb, batch_ub = batch
                batch_extra = None
            else:
                batch_lb, batch_ub, batch_extra = batch
            new_grad, new_safe_dist, new_viol_dist = self._grad_dists_of_batch(batch_lb, batch_ub, batch_extra,
                                                                               forward_fn, grad_src)
            split_grads.append(new_grad)
            split_safe_dists.append(new_safe_dist)
            split_viol_dists.append(new_viol_dist)

        split_grads = torch.cat(split_grads, dim=0)
        split_safe_dists = torch.cat(split_safe_dists, dim=0)
        split_viol_dists = torch.cat(split_viol_dists, dim=0)
        return split_grads, split_safe_dists, split_viol_dists

    def _pick_top(self, top_k: int, wl_lb: Tensor, wl_ub: Tensor, wl_extra: Optional[Tensor], wl_safe_dist: Tensor,
                  wl_viol_dist: Tensor, wl_grad: Tensor, largest: bool) -> Tuple[Tuple, Tuple, Tuple, Tuple, Tuple, Tuple]:
        """ Use safety loss to pick the abstractions among current work list for bisection.
        :param largest: either pick the largest safety loss or smallest safety loss
        :return: the separated parts of LB, UB, extra (if any), safe_dist, viol_dist, grad
        """
        # first separate out the known safe/viol ones
        safe_bits = wl_safe_dist <= 0.
        viol_bits = wl_viol_dist <= 0.
        rem_bits = ~ (safe_bits | viol_bits)

        new_safe_lb, new_viol_lb, rem_lb = wl_lb[safe_bits], wl_lb[viol_bits], wl_lb[rem_bits]
        new_safe_ub, new_viol_ub, rem_ub = wl_ub[safe_bits], wl_ub[viol_bits], wl_ub[rem_bits]
        new_safe_extra = None if wl_extra is None else wl_extra[safe_bits]
        new_viol_extra = None if wl_extra is None else wl_extra[viol_bits]
        rem_extra = None if wl_extra is None else wl_extra[rem_bits]
        rem_safe_dist = wl_safe_dist[rem_bits]
        rem_viol_dist = wl_viol_dist[rem_bits]
        rem_grad = wl_grad[rem_bits]

        if len(rem_lb) == 0:
            # all are confirmed either safe or violation, nothing to bisect
            empty_t = empty_like(wl_lb)
            # new-safe / new-viol / refined / not-refined
            return (new_safe_lb, new_viol_lb, empty_t, empty_t), \
                   (new_safe_ub, new_viol_ub, empty_t, empty_t), \
                   (new_safe_extra, new_viol_extra, rem_extra, rem_extra), \
                   empty_t, empty_t, (empty_t, empty_t)  # use rem_extra twice, it's fine

        top_k = min(top_k, len(rem_lb))

        # If pytorch has mergesort(), do that, right now it's just simpler (and faster) to use topk().
        _, topk_idxs = rem_safe_dist.topk(top_k, largest=largest, sorted=False)  # topk_idxs: size <K>
        # scatter, topk_idxs are 0, others are 1
        other_idxs = torch.ones(len(rem_safe_dist), device=rem_safe_dist.device).byte().scatter_(-1, topk_idxs, 0)
        other_idxs = other_idxs.nonzero().squeeze(dim=-1)  # <Batch-K>

        batch_lb, wl_lb = rem_lb[topk_idxs], rem_lb[other_idxs]
        batch_ub, wl_ub = rem_ub[topk_idxs], rem_ub[other_idxs]
        batch_extra = None if rem_extra is None else rem_extra[topk_idxs]
        wl_extra = None if rem_extra is None else rem_extra[other_idxs]
        wl_safe_dist = rem_safe_dist[other_idxs]  # batch_safe_dist is no longer needed, refinement only needs the grad
        wl_viol_dist = rem_viol_dist[other_idxs]
        batch_grad, wl_grad = rem_grad[topk_idxs], rem_grad[other_idxs]

        return (new_safe_lb, new_viol_lb, batch_lb, wl_lb),\
               (new_safe_ub, new_viol_ub, batch_ub, wl_ub),\
               (new_safe_extra, new_viol_extra, batch_extra, wl_extra),\
               wl_safe_dist, wl_viol_dist, (batch_grad, wl_grad)

    def verify(self, lb: Tensor, ub: Tensor, extra: Optional[Tensor], forward_fn: nn.Module,
               ret_on_cex: bool = True, batch_size: int = 4096) -> Union[Tuple[Tensor, Tensor], Tuple[Tensor, Tensor, Tensor]]:
        """ Considers both safety loss and violation loss to certify or falsify the given property up to some limit.
            If counterexamples are found, return the LB/UB bounds of the cex cube -- all points within are cexs.
            Otherwise, return emtpy tensors.

            The major difference with split() is that verify() does depth-first-search, checking smaller loss
            abstractions first. Otherwise, the memory consumption of BFS style refinement will explode.

            I experimented "what to pick" and "what to grad" using ACAS property3 to see the explored area after
            100 iterations. Results show that:
            *   Pick small violation loss ones is the worst option, perhaps because for most of the time, violation
                losses are always small.
            *   Use gradients from violation loss is highest in the rest 2 picking cases. Having safety loss may or may
                not help. Using safe+viol loss may or may not help. And min(safe, viol) is basically equal to safe loss.
            So for simplicity, I choose to pick small safe loss boxes and bisect based on gradients from violation loss.

        :param lb: Batch x ...
        :param ub: Batch x ...
        :param extra: could contain extra info such as the bit vectors for each LB/UB cube showing which safety property
                      it should satisfy in AndProp; or just None
        :param forward_fn: differentiable forward propagation, not passing in net and call net(input) because different
                           applications may have different net(input, **kwargs)
        :param ret_on_cex: if True, it will stop searching after finding the first counterexample cube
        :return: <LB, UB> tensors with size <Batch x ...> containing counterexample boxes' bounds, if any
        """
        assert valid_lb_ub(lb, ub)
        assert batch_size > 0

        # track how much have been certified/falsified.
        tot_area = total_area(lb, ub)
        assert tot_area > 0
        safes_area = 0.
        viols_area = 0.

        def empty() -> Tensor:
            return empty_like(lb)

        wl_lb, wl_ub = empty(), empty()
        safe_lb, safe_ub = empty(), empty()
        viol_lb, viol_ub = empty(), empty()
        wl_extra = None if extra is None else empty().byte()
        safe_extra = None if extra is None else empty().byte()
        viol_extra = None if extra is None else empty().byte()
        wl_safe_dist, wl_viol_dist, wl_grad = empty(), empty(), empty()

        new_lb, new_ub, new_extra = lb, ub, extra
        iter = 0
        while True:
            iter += 1

            if len(new_lb) > 0:
                # First add the new_lb, new_ub into sorted list, by their corresponding safety loss.
                ''' I also tried using a 'factor' tensor, with LB = LB * factor and UB = UB * factor, to compute gradient
                    w.r.t. 'factor'. However, that is much worse than the grad w.r.t. LB and UB directly. One possible
                    reason is that 'factor' can only shrink the space in one direction towards its mid point. This has
                    little to do with actual bisection later on. Grads w.r.t. LB and UB is more directly related.
                '''
                # experiment on prop2 says this is better than safe, safe+viol, min(safe, viol)
                new_grad, new_safe_dist, new_viol_dist = self._grad_dists_of(new_lb, new_ub, new_extra, forward_fn,
                                                                             'viol', batch_size)
                wl_lb = cat0(wl_lb, new_lb)
                wl_ub = cat0(wl_ub, new_ub)
                wl_extra = None if wl_extra is None else cat0(wl_extra, new_extra)
                wl_safe_dist = cat0(wl_safe_dist, new_safe_dist)
                wl_viol_dist = cat0(wl_viol_dist, new_viol_dist)
                wl_grad = cat0(wl_grad, new_grad)

            if len(wl_lb) == 0:
                # nothing to bisect anymore
                break

            logging.debug(f'At iter {iter}, Safe dist min: {wl_safe_dist.min()}, max: {wl_safe_dist.max()} ' +
                          f'Viol dist min: {wl_viol_dist.min()}, max: {wl_viol_dist.max()}.')

            # Pick small loss boxes to bisect first for verification, otherwise BFS style consumes huge memory
            picked = self._pick_top(batch_size, wl_lb, wl_ub, wl_extra, wl_safe_dist, wl_viol_dist, wl_grad, largest=False)
            new_safe_lb, new_viol_lb, batch_lb, wl_lb = picked[0]
            new_safe_ub, new_viol_ub, batch_ub, wl_ub = picked[1]
            new_safe_extra, new_viol_extra, batch_extra, wl_extra = picked[2]
            wl_safe_dist, wl_viol_dist = picked[3], picked[4]
            batch_grad, wl_grad = picked[5]

            with torch.no_grad():
                safe_lb = cat0(safe_lb, new_safe_lb)
                safe_ub = cat0(safe_ub, new_safe_ub)
                safe_extra = None if safe_extra is None else cat0(safe_extra, new_safe_extra)
                viol_lb = cat0(viol_lb, new_viol_lb)
                viol_ub = cat0(viol_ub, new_viol_ub)
                viol_extra = None if viol_extra is None else cat0(viol_extra, new_viol_extra)

            new_safes_area = total_area(new_safe_lb, new_safe_ub)
            new_viols_area = total_area(new_viol_lb, new_viol_ub)
            safes_area += new_safes_area
            viols_area += new_viols_area

            logging.debug(f'At iter {iter}, another {len(new_safe_lb) + len(new_viol_lb) + len(batch_lb)} boxes are processed, ' +
                          f'in which {len(new_safe_lb)} confirmed to safe, {len(new_viol_lb)} confirmed to violate, ' +
                          f'and total {len(wl_lb) + len(batch_lb)} ready to split.')
            logging.debug(f'+ new safes area {new_safes_area / tot_area * 100 :.2f}%, ' +
                          f'+ new viols area {new_viols_area / tot_area * 100 :.2f}%, ' +
                          f'total processed area {(safes_area + viols_area) / tot_area * 100 :.2f}%.\n')

            if ret_on_cex and len(new_viol_lb) > 0:  # find one cex, stop
                break

            if len(batch_lb) == 0:
                new_lb, new_ub = empty(), empty()
                new_extra = None if extra is None else empty().byte()
            else:
                refined_outs = by_smear(batch_lb, batch_ub, batch_extra, batch_grad)
                new_lb, new_ub = refined_outs[:2]
                new_extra = None if batch_extra is None else refined_outs[2]
        return viol_lb, viol_ub

    def split(self, lb: Tensor, ub: Tensor, extra: Optional[Tensor], forward_fn: nn.Module, batch_size: int,
              stop_on_k_all: int = None,
              stop_on_k_new: int = None,
              stop_on_k_ops: int = None,
              collapse_res: bool = True) -> Union[Tuple[Tensor, Tensor], Tuple[Tensor, Tensor, Tensor]]:
        """ Different from verify(), split() does breadth-first traversal. Its objective is to have roughly even
            abstractions with small safety losses for the optimization later.

        :param lb: could be accessed from props.lbub(), but may need additional normalization
        :param ub: same as @param lb
        :param extra: could contain extra info such as the bit vectors for each LB/UB cube showing which safety property
                      it should satisfy in AndProp; or just None
        :param forward_fn: differentiable forward propagation, not passing in net and call net(input) because different
                           applications may have different net(input, **kwargs)
        :param batch_size: How many to bisect once at most, must provide this granularity.
                           Larger batch_size => faster to compute but less precise / averaged (due to more rushing).
        :param stop_on_k_all: if not None, split() stops after total amount of abstractions exceeds this bar.
        :param stop_on_k_new: if not None, split() stops after the amount of abstractions introduced by this split()
                              call exceeds this bar.
        :param stop_on_k_ops: if not None, split() stops after this many refinement steps have been applied.
        :return: <LB, UB> when extra is None, otherwise <LB, UB, extra>
        """
        # TODO incorporate the tiny_width to old Bisecter as well
        assert valid_lb_ub(lb, ub)
        assert batch_size > 0

        def _validate_stop_criterion(v):
            assert v is None or (isinstance(v, int) and v > 0)
            return
        _validate_stop_criterion(stop_on_k_all)
        _validate_stop_criterion(stop_on_k_new)
        _validate_stop_criterion(stop_on_k_ops)

        def empty() -> Tensor:
            return empty_like(lb)

        n_orig_abs = len(lb)

        wl_lb, wl_ub = empty(), empty()
        safe_lb, safe_ub = empty(), empty()
        viol_lb, viol_ub = empty(), empty()
        wl_extra = None if extra is None else empty().byte()
        safe_extra = None if extra is None else empty().byte()
        viol_extra = None if extra is None else empty().byte()
        wl_safe_dist, wl_viol_dist, wl_grad = empty(), empty(), empty()

        new_lb, new_ub, new_extra = lb, ub, extra
        iter = 0
        while True:
            iter += 1

            if len(new_lb) > 0:
                # by default, just use the safety loss as gradient source  # FIXME use which loss
                new_grad, new_safe_dist, new_viol_dist = self._grad_dists_of(new_lb, new_ub, new_extra, forward_fn,
                                                                             'safe', batch_size)
                wl_lb = cat0(wl_lb, new_lb)
                wl_ub = cat0(wl_ub, new_ub)
                wl_extra = None if wl_extra is None else cat0(wl_extra, new_extra)
                wl_safe_dist = cat0(wl_safe_dist, new_safe_dist)
                wl_viol_dist = cat0(wl_viol_dist, new_viol_dist)
                wl_grad = cat0(wl_grad, new_grad)

            if len(wl_lb) == 0:
                # nothing to bisect anymore
                break

            n_curr_abs = len(safe_lb) + len(viol_lb) + len(wl_lb)
            if stop_on_k_all is not None and n_curr_abs >= stop_on_k_all:
                # has collected enough abstractions
                break
            if stop_on_k_new is not None and n_curr_abs - n_orig_abs >= stop_on_k_new:
                # has collected enough new abstractions
                break
            if stop_on_k_ops is not None and iter > stop_on_k_ops:
                # has run enough refinement iterations
                break

            logging.debug(f'At iter {iter}, Safe dist min: {wl_safe_dist.min()}, max: {wl_safe_dist.max()} ' +
                          f'Viol dist min: {wl_viol_dist.min()}, max: {wl_viol_dist.max()}.')

            # Pick large loss boxes to refine, to make overall safety loss smaller and more even
            picked = self._pick_top(batch_size, wl_lb, wl_ub, wl_extra, wl_safe_dist, wl_viol_dist, wl_grad, largest=True)
            new_safe_lb, new_viol_lb, batch_lb, wl_lb = picked[0]
            new_safe_ub, new_viol_ub, batch_ub, wl_ub = picked[1]
            new_safe_extra, new_viol_extra, batch_extra, wl_extra = picked[2]
            wl_safe_dist, wl_viol_dist = picked[3], picked[4]
            batch_grad, wl_grad = picked[5]

            with torch.no_grad():
                safe_lb = cat0(safe_lb, new_safe_lb)
                safe_ub = cat0(safe_ub, new_safe_ub)
                safe_extra = None if safe_extra is None else cat0(safe_extra, new_safe_extra)
                viol_lb = cat0(viol_lb, new_viol_lb)
                viol_ub = cat0(viol_ub, new_viol_ub)
                viol_extra = None if viol_extra is None else cat0(viol_extra, new_viol_extra)

            logging.debug(f'At iter {iter}, another {len(new_safe_lb) + len(new_viol_lb) + len(batch_lb)} boxes are processed, ' +
                          f'in which {len(new_safe_lb)} confirmed to safe, {len(new_viol_lb)} confirmed to violate, ' +
                          f'and total {len(wl_lb) + len(batch_lb)} ready to split.')

            if len(batch_lb) == 0:
                new_lb, new_ub = empty(), empty()
                new_extra = None if extra is None else empty().byte()
            else:
                refined_outs = by_smear(batch_lb, batch_ub, batch_extra, batch_grad)
                new_lb, new_ub = refined_outs[:2]
                new_extra = None if batch_extra is None else refined_outs[2]
            pass

        logging.debug(f'\nAt the end, split {len(wl_lb)} uncertain (non-zero loss) boxes, ' +
                      f'{len(safe_lb)} safe boxes and {len(viol_lb)} unsafe boxes.')
        if len(wl_lb) > 0:
            logging.debug(f'Non zero loss boxes have safe loss min {wl_safe_dist.min()} ~ max {wl_safe_dist.max()}.')

        if collapse_res:
            with torch.no_grad():
                all_lb = cat0(wl_lb, safe_lb, viol_lb)
                all_ub = cat0(wl_ub, safe_ub, viol_ub)
                all_extra = None if safe_extra is None else cat0(wl_extra, safe_extra, viol_extra)

            if all_extra is None:
                return all_lb, all_ub
            else:
                return all_lb, all_ub, all_extra
        else:
            with torch.no_grad():
                wl_lb = cat0(wl_lb, viol_lb)
                wl_ub = cat0(wl_ub, viol_ub)
                wl_extra = None if wl_extra is None else cat0(wl_extra, viol_extra)
            if wl_extra is None:
                return wl_lb, wl_ub
            else:
                return wl_lb, wl_ub, wl_extra

    def try_certify(self, lb: Tensor, ub: Tensor, extra: Optional[Tensor], forward_fn: nn.Module, batch_size: int,
              stop_on_k_all: int = None) -> bool:
        """
        :return: True if it can successfully certify the property on lb/ub within certain limits
        """
        wl_lb = self.split(lb, ub, extra, forward_fn, batch_size, stop_on_k_all=stop_on_k_all, collapse_res=False)[0]
        # empty => all certified
        return len(wl_lb) == 0
    pass


def bisect_by(lb: Tensor, ub: Tensor, idxs: Tensor,
              extra: Tensor = None) -> Union[Tuple[Tensor, Tensor], Tuple[Tensor, Tensor, Tensor]]:
    """ Bisect specific columns.
    :param idxs: <Batch>, as the indices from torch.max()
    :param extra: if not None, it contains the bit vector for each LB/UB piece showing which prop they should obey
    :return: <New LB, New UB> if extra is None, otherwise <New LB, New UB, New Extra>
    """
    # scatter_() to convert indices into one-hot encoding
    split_idxs = idxs.unsqueeze(dim=-1)  # Batch x 1
    onehot_idxs = torch.zeros_like(lb).byte().scatter_(-1, split_idxs, 1)

    # then bisect the specified cols only
    mid = (lb + ub) / 2.0
    lefts_lb = lb
    lefts_ub = torch.where(onehot_idxs, mid, ub)
    rights_lb = torch.where(onehot_idxs, mid, lb)
    rights_ub = ub

    newlb = cat0(lefts_lb, rights_lb)
    newub = cat0(lefts_ub, rights_ub)
    if extra is None:
        return newlb, newub

    newextra = cat0(extra, extra)
    return newlb, newub, newextra


def by_smear(new_rem_lb: Tensor, new_rem_ub: Tensor, new_rem_extra: Optional[Tensor], new_rem_grad: Tensor,
             tiny_width: float = None) -> Union[Tuple[Tensor, Tensor], Tuple[Tensor, Tensor, Tensor]]:
    """
    Experiment shows that smear = grad * dim_width as in ReluVal is the best heuristic tried so far. It's better
    than either one alone, and better than other indirect loss e.g., introduced over-approximated area.
    :return: if new_rem_extra is None, return <refined LB, UB> without extra, otherwise return with extra
    """
    with torch.no_grad():
        width = new_rem_ub - new_rem_lb
        assert new_rem_lb.dim() == 2, 'Otherwise, I need to reduce the >2 dims to compute dim width?'
        smears = new_rem_grad * width / 2

        if tiny_width is not None:
            # consider only those dimensions that are not tiny
            not_tiny_bits = width > tiny_width
            smears = smears * not_tiny_bits.float()

        _, split_idxs = smears.max(dim=-1)
        return bisect_by(new_rem_lb, new_rem_ub, split_idxs, new_rem_extra)
