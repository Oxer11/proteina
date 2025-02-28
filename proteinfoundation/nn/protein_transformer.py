# SPDX-FileCopyrightText: Copyright (c) 2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


from typing import Dict

import einops
import torch
from torch.utils.checkpoint import checkpoint

from openfold.model.msa import MSARowAttentionWithPairBias
from openfold.model.pair_transition import PairTransition
from openfold.model.structure_module import InvariantPointAttention
from openfold.model.triangular_multiplicative_update import (
    TriangleMultiplicationIncoming,
    TriangleMultiplicationOutgoing,
)
from openfold.utils.rigid_utils import Rigid
from proteinfoundation.nn.feature_factory import FeatureFactory
from proteinfoundation.nn.pair_bias_attn.pair_bias_attn import PairBiasAttention
from proteinfoundation.nn.alphafold3_pytorch_utils.modules import (
    AdaptiveLayerNorm,
    AdaptiveLayerNormOutputScale,
    SwiGLU,
)


class SequenceToCoordinates(torch.nn.Module):
    """Updates coordinates from sequence."""

    def __init__(self, dim_token):
        super().__init__()
        self.ln = torch.nn.LayerNorm(dim_token)
        self.linear = torch.nn.Linear(dim_token, 3)

    def forward(self, seqs, x_t, mask):
        """
        Args:
            seqs: Input sequence representation, shape [b, n, token_dim]
            x_t: Input coordinates, shape [b, n, 3]
            mask: binary mask, shape [b, n]

        Returns:
            Updated (masked) coordinates
        """
        delta_x = self.linear(self.ln(seqs))  # [b, n, 3]
        x_t = x_t + delta_x
        return x_t * mask[..., None]


class CoordinatesToSequenceLinear(torch.nn.Module):
    """Updates sequence using linear layer from coordiates."""

    def __init__(self, dim_token):
        super().__init__()
        self.linear = torch.nn.Linear(3, dim_token, bias=False)
        self.ln = torch.nn.LayerNorm(dim_token)

    def forward(self, seqs, x_t, mask):
        """
        Args:
            seqs: Input sequence representation, shape [b, n, token_dim]
            x_t: Input coordinates, shape [b, n, 3]
            mask: binary mask, shape [b, n]

        Returns:
            Updated (masked) sequence
        """
        delta_seq = self.ln(self.linear(x_t))
        seqs = seqs + delta_seq
        return seqs * mask[..., None]


class CoordinatesToSequenceIPA(torch.nn.Module):
    """Updates sequence using IPA from coordiates (and identity rotations)."""

    def __init__(
        self, dim_token, dim_pair, c_hidden=16, nheads=8, no_qk_points=8, no_v_points=12
    ):
        super().__init__()
        self.ipa = InvariantPointAttention(
            c_s=dim_token,
            c_z=dim_pair,
            c_hidden=c_hidden,
            no_heads=nheads,
            no_qk_points=no_qk_points,
            no_v_points=no_v_points,
        )

    def forward(self, seqs, pair_rep, x_t, mask):
        """
        Args:
            seqs: Input sequence representation, shape [b, n, token_dim]
            pair_rep: Pair represnetation, shape [b, n, n, pair_dim]
            x_t: Input coordinates, shape [b, n, 3]
            mask: binary mask, shape [b, n]

        Returns:
            Updated sequence, shape [b, n, dim_token]
        """
        x_t = x_t * mask[..., None]
        r = Rigid(trans=x_t, rots=None)  # Rotations default to identity
        delta_seqs = self.ipa(s=seqs, z=pair_rep, r=r, mask=mask * 1.0)
        seqs = seqs + delta_seqs
        return seqs * mask[..., None]  # [b, n, token_dim]


class MultiHeadAttention(torch.nn.Module):
    """Typical multi-head self-attention attention using pytorch's module."""

    def __init__(self, dim_token, nheads, dropout=0.0):
        super().__init__()

        self.to_q = torch.nn.Linear(dim_token, dim_token)
        self.to_kv = torch.nn.Linear(dim_token, 2 * dim_token, bias=False)

        self.mha = torch.nn.MultiheadAttention(
            embed_dim=dim_token,
            num_heads=nheads,
            dropout=dropout,
            batch_first=True,
        )

    def forward(self, x, mask):
        """
        Args:
            x: Input sequence, shape [b, n, dim_token]
            mask: binary mask, shape [b, n]

        Returns:
            Updated sequence, shape [b, n, dim_token]
        """
        query = self.to_q(x)  # [b, n, dim_token]
        key, value = self.to_kv(x).chunk(2, dim=-1)  # Each [b, n, dim_token]
        return (
            self.mha(
                query=query,
                key=key,
                value=value,
                key_padding_mask=~mask,  # Indicated what should be ignores with True, that's why the ~
                need_weights=False,
                is_causal=False,
            )[0]
            * mask[..., None]
        )  # [b, n, dim_token]


class MultiHeadBiasedAttention(torch.nn.Module):
    """Multi-head self-attention with pair bias, based on openfold."""

    def __init__(self, dim_token, dim_pair, nheads, dropout=0.0):
        super().__init__()

        self.row_attn_pair_bias = MSARowAttentionWithPairBias(
            c_m=dim_token,
            c_z=dim_pair,
            c_hidden=int(dim_token // nheads),  # Per head dimension
            no_heads=nheads,
        )

    def forward(self, x, pair_rep, mask):
        """
        Args:
            x: Input sequence, shape [b, n, dim_token]
            pair_rep: Pair representation, shape [b, n, n, dim_pair]
            mask: Binary mask, shape [b, n]

        Returns:
            Updated sequence representation, shape [b, n, dim_token]
        """
        # Add extra dimension for MSA, unused here but required by openfold
        x = einops.rearrange(x, "b n d -> b () n d")  # [b, 1, n, dim_token]
        mask = einops.rearrange(mask, "b n -> b () n") * 1.0  # float [b, 1, n]
        x = self.row_attn_pair_bias(x, pair_rep, mask)  # [b, 1, n, dim_token]
        x = x * mask[..., None]
        x = einops.rearrange(
            x, "b () n c -> b n c"
        )  # Remove extra dimension [b, n, dim_token]
        return x


class MultiHeadAttentionADALN(torch.nn.Module):
    """Typical multi-head self-attention with adaptive layer norm applied to input
    and adaptive scaling applied to output."""

    def __init__(self, dim_token, nheads, dim_cond, dropout=0.0):
        super().__init__()
        self.adaln = AdaptiveLayerNorm(dim=dim_token, dim_cond=dim_cond)
        self.mha = MultiHeadAttention(
            dim_token=dim_token, nheads=nheads, dropout=dropout
        )
        self.scale_output = AdaptiveLayerNormOutputScale(
            dim=dim_token, dim_cond=dim_cond
        )

    def forward(self, x, cond, mask):
        """
        Args:
            x: Input sequence representation, shape [b, n, dim_token]
            cond: Conditioning variables, shape [b, n, dim_cond]
            mask: Binary mask, shape [b, n]

        Returns:
            Updated sequence representation, shape [b, n, dim_token].
        """
        x = self.adaln(x, cond, mask)
        x = self.mha(x, mask)
        x = self.scale_output(x, cond, mask)
        return x * mask[..., None]


class MultiHeadBiasedAttentionADALN(torch.nn.Module):
    """Pair biased multi-head self-attention with adaptive layer norm applied to input
    and adaptive scaling applied to output."""

    def __init__(self, dim_token, dim_pair, nheads, dim_cond, dropout=0.0):
        super().__init__()
        self.adaln = AdaptiveLayerNorm(dim=dim_token, dim_cond=dim_cond)
        self.mha = MultiHeadBiasedAttention(
            dim_token=dim_token, dim_pair=dim_pair, nheads=nheads, dropout=dropout
        )
        self.scale_output = AdaptiveLayerNormOutputScale(
            dim=dim_token, dim_cond=dim_cond
        )

    def forward(self, x, pair_rep, cond, mask):
        """
        Args:
            x: Input sequence representation, shape [b, n, dim_token]
            cond: Conditioning variables, shape [b, n, dim_cond]
            pair_rep: Pair represnetation, shape [b, n, n, dim_pair]
            mask: Binary mask, shape [b, n]

        Returns:
            Updated sequence representation, shape [b, n, dim_token].
        """
        x = self.adaln(x, cond, mask)
        x = self.mha(x, pair_rep, mask)
        x = self.scale_output(x, cond, mask)
        return x * mask[..., None]


class MultiHeadBiasedAttentionADALN_MM(torch.nn.Module):
    """Pair biased multi-head self-attention with adaptive layer norm applied to input
    and adaptive scaling applied to output."""

    def __init__(self, dim_token, dim_pair, nheads, dim_cond, use_qkln):
        super().__init__()
        dim_head = int(dim_token // nheads)
        self.adaln = AdaptiveLayerNorm(dim=dim_token, dim_cond=dim_cond)
        self.mha = PairBiasAttention(
            node_dim=dim_token,
            dim_head=dim_head,
            heads=nheads,
            bias=True,
            dim_out=dim_token,
            qkln=use_qkln,
            pair_dim=dim_pair,
        )
        self.scale_output = AdaptiveLayerNormOutputScale(
            dim=dim_token, dim_cond=dim_cond
        )

    def forward(self, x, pair_rep, cond, mask):
        """
        Args:
            x: Input sequence representation, shape [b, n, dim_token]
            cond: Conditioning variables, shape [b, n, dim_cond]
            pair_rep: Pair represnetation, shape [b, n, n, dim_pair]
            mask: Binary mask, shape [b, n]

        Returns:
            Updated sequence representation, shape [b, n, dim_token].
        """
        pair_mask = mask[:, :, None] * mask[:, None, :]  # [b, n, n]
        x = self.adaln(x, cond, mask)
        x = self.mha(node_feats=x, pair_feats=pair_rep, mask=pair_mask)
        x = self.scale_output(x, cond, mask)
        return x * mask[..., None]


# Code from Lucidrain's implementation of AF3
# https://github.com/lucidrains/alphafold3-pytorch
class Transition(torch.nn.Module):
    """Transition layer."""

    def __init__(self, dim, expansion_factor=4, layer_norm=False):
        super().__init__()

        dim_inner = int(dim * expansion_factor)

        self.use_layer_norm = layer_norm
        if self.use_layer_norm:
            self.ln = torch.nn.LayerNorm(dim)

        self.swish_linear = torch.nn.Sequential(
            torch.nn.Linear(dim, dim_inner * 2, bias=False),
            SwiGLU(),
        )
        self.linear_out = torch.nn.Linear(dim_inner, dim, bias=False)

    def forward(self, x, mask):
        """
        Args:
            x: Input sequence representation, shape [b, n, dim]
            mask: binary, shape [b, n]

        Returns:
            Updated sequence representation, shape [b, n, dim]
        """
        if self.use_layer_norm:
            x = self.ln(x)
        x = self.linear_out(self.swish_linear(x))
        return x * mask[..., None]


class TransitionADALN(torch.nn.Module):
    """Transition layer with adaptive layer norm applied to input and adaptive
    scaling aplied to output."""

    def __init__(self, *, dim, dim_cond, expansion_factor=4):
        super().__init__()
        self.adaln = AdaptiveLayerNorm(dim=dim, dim_cond=dim_cond)
        self.transition = Transition(
            dim=dim, expansion_factor=expansion_factor, layer_norm=False
        )
        self.scale_output = AdaptiveLayerNormOutputScale(dim=dim, dim_cond=dim_cond)

    def forward(self, x, cond, mask):
        """
        Args:
            x: Input sequence representation, shape [b, n, dim]
            cond: conditioning variables, shape [b, n, dim_cond]
            mask: binary mask, shape [b, n]

        Returns:
            Updated sequence representation, shape [b, n, dim]
        """
        x = self.adaln(x, cond, mask)  # [b, n, dim]
        x = self.transition(x, mask)  # [b, n, dim]
        x = self.scale_output(x, cond, mask)  # [b, n, dim]
        return x * mask[..., None]  # [b, n, dim]


class MultiheadAttnAndTransition(torch.nn.Module):
    """Layer that applies mha and transition to a sequence representation. Both layers are their adaptive versions
    which rely on conditining variables (see above).

    Args:
        dim_token: Token dimension in sequence representation.
        dim_pair: Dimension of pair representation.
        nheads: Number of attention heads.
        dim_cond: Dimension of conditioning variables.
        residual_mha: Whether to use a residual connection in the mha layer.
        residual_transition: Whether to use a residual connection in the transition layer.
        parallel_mha_transition: Whether to run mha and transition in parallel or sequentially.
        use_attn_pair_bias: Whether to use a pair represnetation to bias attention.
        use_qkln: Whether to use layer norm on keyus and queries for attention.
        dropout: droput use in the self-attention layer.
    """

    def __init__(
        self,
        dim_token,
        dim_pair,
        nheads,
        dim_cond,
        residual_mha,
        residual_transition,
        parallel_mha_transition,
        use_attn_pair_bias,
        use_qkln,
        dropout=0.0,
        expansion_factor=4,
    ):
        super().__init__()
        self.parallel = parallel_mha_transition
        self.use_attn_pair_bias = use_attn_pair_bias

        # If parallel do not allow both layers to have a residual connection since it leads to adding x twice
        if self.parallel and residual_mha and residual_transition:
            # logger.info("MHA and transition are residual, but with parallel track. Setting transition to non-residual.")
            residual_transition = False

        self.residual_mha = residual_mha
        self.residual_transition = residual_transition

        self.mhba = MultiHeadBiasedAttentionADALN_MM(
            dim_token=dim_token,
            dim_pair=dim_pair,
            nheads=nheads,
            dim_cond=dim_cond,
            use_qkln=use_qkln,
        )

        self.transition = TransitionADALN(
            dim=dim_token, dim_cond=dim_cond, expansion_factor=expansion_factor
        )

    def _apply_mha(self, x, pair_rep, cond, mask):
        x_attn = self.mhba(x, pair_rep, cond, mask)
        if self.residual_mha:
            x_attn = x_attn + x
        return x_attn * mask[..., None]

    def _apply_transition(self, x, cond, mask):
        x_tr = self.transition(x, cond, mask)
        if self.residual_transition:
            x_tr = x_tr + x
        return x_tr * mask[..., None]

    def forward(self, x, pair_rep, cond, mask):
        """
        Args:
            x: Input sequence representation, shape [b, n, dim_token]
            cond: conditioning variables, shape [b, n, dim_cond]
            mask: binary mask, shape [b, n]
            pair_rep: Pair representation (if provided, if no bias will be ignored), shape [b, n, n, dim_pair] or None

        Returns:
            Updated sequence representation, shape [b, n, dim].
        """
        x = x * mask[..., None]
        if self.parallel:
            x = self._apply_mha(x, pair_rep, cond, mask) + self._apply_transition(
                x, cond, mask
            )
        else:
            x = self._apply_mha(x, pair_rep, cond, mask)
            x = self._apply_transition(x, cond, mask)
        return x * mask[..., None]


class PairReprUpdate(torch.nn.Module):
    """Layer to update the pair representation."""

    def __init__(
        self,
        token_dim,
        pair_dim,
        expansion_factor_transition=2,
        use_tri_mult=False,
        tri_mult_c=196,
    ):
        super().__init__()

        self.use_tri_mult = use_tri_mult
        self.layer_norm_in = torch.nn.LayerNorm(token_dim)
        self.linear_x = torch.nn.Linear(token_dim, int(2 * pair_dim), bias=False)

        # self.transition_in = PairTransition(c_z=pair_dim, n=expansion_factor_transition)
        if use_tri_mult:
            tri_mult_c = min(pair_dim, tri_mult_c)
            self.tri_mult_out = TriangleMultiplicationOutgoing(
                c_z=pair_dim, c_hidden=tri_mult_c
            )
            self.tri_mult_in = TriangleMultiplicationIncoming(
                c_z=pair_dim, c_hidden=tri_mult_c
            )
        self.transition_out = PairTransition(
            c_z=pair_dim, n=expansion_factor_transition
        )

    def _apply_mask(self, pair_rep, pair_mask):
        """
        pair_rep has shape [b, n, n, pair_dim]
        pair_mask has shape [b, n, n]
        """
        return pair_rep * pair_mask[..., None]

    def forward(self, x, pair_rep, mask):
        """
        Args:
            x: Input sequence, shape [b, n, token_dim]
            pair_rep: Input pair representation, shape [b, n, n, pair_dim]
            mask: binary mask, shape [b, n]

        Returns:
            Updated pair representation, shape [b, n, n, pair_dim].
        """
        pair_mask = mask[:, None, :] * mask[:, :, None]  # [b, n, n]
        x = x * mask[..., None]  # [b, n, token_dim]
        x_proj_1, x_proj_2 = self.linear_x(self.layer_norm_in(x)).chunk(
            2, dim=-1
        )  # [b, n, pair_dim] each
        pair_rep = (
            pair_rep + x_proj_1[:, None, :, :] + x_proj_2[:, :, None, :]
        )  # [b, n, n, pair_dim]
        pair_rep = self._apply_mask(pair_rep, pair_mask)  # [b, n, n, pair_dim]
        # pair_rep = pair_rep + checkpoint(self.transition_in, *(pair_rep, pair_mask * 1.0))
        # pair_rep = self._apply_mask(pair_rep, pair_mask)  # [b, n, n, pair_dim]
        if self.use_tri_mult:
            pair_rep = pair_rep + checkpoint(
                self.tri_mult_out, *(pair_rep, pair_mask * 1.0)
            )
            pair_rep = self._apply_mask(pair_rep, pair_mask)  # [b, n, n, pair_dim]
            pair_rep = pair_rep + checkpoint(
                self.tri_mult_in, *(pair_rep, pair_mask * 1.0)
            )
            pair_rep = self._apply_mask(pair_rep, pair_mask)  # [b, n, n, pair_dim]
        pair_rep = pair_rep + checkpoint(
            self.transition_out, *(pair_rep, pair_mask * 1.0)
        )
        pair_rep = self._apply_mask(pair_rep, pair_mask)  # [b, n, n, pair_dim]
        return pair_rep


class PairReprBuilder(torch.nn.Module):
    """
    Builds initial pair representation. Essentially the pair feature factory, but potentially with
    an adaptive layer norm layer as well.
    """

    def __init__(self, feats_repr, feats_cond, dim_feats_out, dim_cond_pair, **kwargs):
        super().__init__()

        self.init_repr_factory = FeatureFactory(
            feats=feats_repr,
            dim_feats_out=dim_feats_out,
            use_ln_out=True,
            mode="pair",
            **kwargs,
        )

        self.cond_factory = None  # Build a pair feature for conditioning and use it for adaln the pair representation
        if feats_cond is not None:
            if len(feats_cond) > 0:
                self.cond_factory = FeatureFactory(
                    feats=feats_cond,
                    dim_feats_out=dim_cond_pair,
                    use_ln_out=True,
                    mode="pair",
                    **kwargs,
                )
                self.adaln = AdaptiveLayerNorm(
                    dim=dim_feats_out, dim_cond=dim_cond_pair
                )

    def forward(self, batch_nn):
        mask = batch_nn["mask"]  # [b, n]
        pair_mask = mask[:, :, None] * mask[:, None, :]  # [b, n, n]
        repr = self.init_repr_factory(batch_nn)  # [b, n, n, dim_feats_out]
        if self.cond_factory is not None:
            cond = self.cond_factory(batch_nn)  # [b, n, n, dim_cond]
            repr = self.adaln(repr, cond, pair_mask)
        return repr


class ProteinTransformerAF3(torch.nn.Module):
    """
    Final neural network mimicking the one used in AF3 diffusion. It consists of:

    (1) Input preparation
    (1.a) Initial sequence representation from features
    (1.b) Embed coordaintes and add to initial sequence representation
    (1.c) Conditioning variables from features

    (2) Main trunk
    (2.a) A sequence of layers similar to algorithm 23 of AF3 (multi head attn, transition) using adaptive layer norm
    and adaptive output scaling (also from adaptive layer norm paper)

    (3) Recovering 3D coordinates
    (3.a) A layer that takes as input tokens and produces coordinates
    """

    def __init__(self, **kwargs):
        """
        Initializes the NN. The seqs and pair representations used are just zero in case
        no features are required."""
        super(ProteinTransformerAF3, self).__init__()
        self.use_attn_pair_bias = kwargs["use_attn_pair_bias"]
        self.nlayers = kwargs["nlayers"]
        self.token_dim = kwargs["token_dim"]
        self.pair_repr_dim = kwargs["pair_repr_dim"]
        self.update_coors_on_the_fly = kwargs.get(
            "update_coors_on_the_fly", False
        )  # For backward compat
        self.update_seq_with_coors = kwargs.get(
            "update_seq_with_coors", None
        )  # For backward compat, None, linear or IPA, only used if coors on the fly
        self.update_pair_repr = kwargs.get(
            "update_pair_repr", False
        )  # For backward compat
        self.update_pair_repr_every_n = kwargs.get(
            "update_pair_repr_every_n", 2
        )  # For backward compat
        self.use_tri_mult = kwargs.get("use_tri_mult", False)  # For backward compat
        self.feats_pair_cond = kwargs.get("feats_pair_cond", [])  # For backward compat
        self.use_qkln = kwargs.get("use_qkln", False)  # For backward compat
        self.num_buckets_predict_pair = kwargs.get(
            "num_buckets_predict_pair", None
        )  # For backward compat

        # Registers
        self.num_registers = kwargs.get("num_registers", None)  # For backward compat
        if self.num_registers is None or self.num_registers <= 0:
            self.num_registers = 0
            self.registers = None
        else:
            self.num_registers = int(self.num_registers)
            self.registers = torch.nn.Parameter(
                torch.randn(self.num_registers, self.token_dim) / 20.0
            )

        # To encode corrupted 3d positions
        self.linear_3d_embed = torch.nn.Linear(3, kwargs["token_dim"], bias=False)

        # To form initial representation
        self.init_repr_factory = FeatureFactory(
            feats=kwargs["feats_init_seq"],
            dim_feats_out=kwargs["token_dim"],
            use_ln_out=False,
            mode="seq",
            **kwargs,
        )

        # To get conditioning variables
        self.cond_factory = FeatureFactory(
            feats=kwargs["feats_cond_seq"],
            dim_feats_out=kwargs["dim_cond"],
            use_ln_out=False,
            mode="seq",
            **kwargs,
        )

        self.transition_c_1 = Transition(kwargs["dim_cond"], expansion_factor=2)
        self.transition_c_2 = Transition(kwargs["dim_cond"], expansion_factor=2)

        # To get pair representation
        if self.use_attn_pair_bias:
            self.pair_repr_builder = PairReprBuilder(
                feats_repr=kwargs["feats_pair_repr"],
                feats_cond=kwargs["feats_pair_cond"],
                dim_feats_out=kwargs["pair_repr_dim"],
                dim_cond_pair=kwargs["dim_cond"],
                **kwargs,
            )
        else:
            # If no pair bias no point in having a pair representation
            self.update_pair_repr = False

        # Trunk layers
        self.transformer_layers = torch.nn.ModuleList(
            [
                MultiheadAttnAndTransition(
                    dim_token=kwargs["token_dim"],
                    dim_pair=kwargs["pair_repr_dim"],
                    nheads=kwargs["nheads"],
                    dim_cond=kwargs["dim_cond"],
                    residual_mha=kwargs["residual_mha"],
                    residual_transition=kwargs["residual_transition"],
                    parallel_mha_transition=kwargs["parallel_mha_transition"],
                    use_attn_pair_bias=kwargs["use_attn_pair_bias"],
                    use_qkln=self.use_qkln,
                )
                for _ in range(self.nlayers)
            ]
        )

        # # Coors on the fly
        # if self.update_coors_on_the_fly:
        #     self.seq_to_coors = torch.nn.ModuleList([
        #         SequenceToCoordinates(dim_token=kwargs["token_dim"]) for _ in range(self.nlayers)
        #     ])

        #     # Coors to seq
        #     if self.update_seq_with_coors == "linear" or self.update_seq_with_coors == "linearipa":
        #         self.coors_to_seq_linear = torch.nn.ModuleList([
        #             CoordinatesToSequenceLinear(dim_token=kwargs["token_dim"]) for _ in range(self.nlayers)
        #         ])

        #     if self.update_seq_with_coors == "ipa" or self.update_seq_with_coors == "linearipa":
        #         self.coors_to_seq_ipa = torch.nn.ModuleList([
        #             CoordinatesToSequenceIPA(dim_token=kwargs["token_dim"], dim_pair=kwargs["pair_repr_dim"]) for _ in range(self.nlayers)
        #         ])

        # To update pair representations if needed
        if self.update_pair_repr:
            self.pair_update_layers = torch.nn.ModuleList(
                [
                    (
                        PairReprUpdate(
                            token_dim=kwargs["token_dim"],
                            pair_dim=kwargs["pair_repr_dim"],
                            use_tri_mult=self.use_tri_mult,
                        )
                        if i % self.update_pair_repr_every_n == 0
                        else None
                    )
                    for i in range(self.nlayers - 1)
                ]
            )
            # For distogram pair prediction
            if self.num_buckets_predict_pair is not None:
                self.pair_head_prediction = torch.nn.Sequential(
                    torch.nn.LayerNorm(kwargs["pair_repr_dim"]),
                    torch.nn.Linear(
                        kwargs["pair_repr_dim"], self.num_buckets_predict_pair
                    ),
                )

        self.coors_3d_decoder = torch.nn.Sequential(
            torch.nn.LayerNorm(kwargs["token_dim"]),
            torch.nn.Linear(kwargs["token_dim"], 3, bias=False),
        )

    def _extend_w_registers(self, seqs, pair, mask, cond_seq):
        """
        Extends the sequence representation, pair representation, mask and indices with registers.

        Args:
            - seqs: sequence representation, shape [b, n, dim_token]
            - pair: pair representation, shape [b, n, n, dim_pair]
            - mask: binary mask, shape [b, n]
            - cond_seq: tensor of shape [b, n, dim_cond]

        Returns:
            All elements above extended with registers / zeros.
        """
        if self.num_registers == 0:
            return seqs, pair, mask, cond_seq  # Do nothing

        b, n, _ = seqs.shape
        dim_pair = pair.shape[-1]
        r = self.num_registers
        dim_cond = cond_seq.shape[-1]

        # Concatenate registers to sequence
        reg_expanded = self.registers[None, :, :]  # [1, r, dim_token]
        reg_expanded = reg_expanded.expand(b, -1, -1)  # [b, r, dim_token]
        seqs = torch.cat([reg_expanded, seqs], dim=1)  # [b, r+n, dim_token]

        # Extend mask
        true_tensor = torch.ones(b, r, dtype=torch.bool, device=seqs.device)  # [b, r]
        mask = torch.cat([true_tensor, mask], dim=1)  # [b, r+n]

        # Extend pair representation with zeros; pair has shape [b, n, n, pair_dim] -> [b, r+n, r+n, pair_dim]
        # [b, n, n, pair_dim] -> [b, r+n, n, pair_dim]
        zero_pad_top = torch.zeros(
            b, r, n, dim_pair, device=seqs.device
        )  # [b, r, n, dim_pair]
        pair = torch.cat([zero_pad_top, pair], dim=1)  # [b, r+n, n, dim_pair]
        # [b, r+n, n, pair_dim] -> [b, r+n, r+n, pair_dim]
        zero_pad_left = torch.zeros(
            b, r + n, r, dim_pair, device=seqs.device
        )  # [b, r+n, r, dim_pair]
        pair = torch.cat([zero_pad_left, pair], dim=2)  # [b, r+n, r+n, dim+pair]

        # Extend cond
        zero_tensor = torch.zeros(
            b, r, dim_cond, device=seqs.device
        )  # [b, r, dim_cond]
        cond_seq = torch.cat([zero_tensor, cond_seq], dim=1)  # [b, r+n, dim_cond]

        return seqs, pair, mask, cond_seq

    def _undo_registers(self, seqs, pair, mask):
        """
        Undoes register padding.

        Args:
            - seqs: sequence representation, shape [b, r+n, dim_token]
            - pair: pair representation, shape [b, r+n, r+n, dim_pair]
            - mask: binary mask, shape [b, r+n]

        Returns:
            All three elements with the register padding removed.
        """
        if self.num_registers == 0:
            return seqs, pair, mask
        r = self.num_registers
        return seqs[:, r:, :], pair[:, r:, r:, :], mask[:, r:]

    # @torch.compile
    def forward(self, batch_nn: Dict[str, torch.Tensor]):
        """
        Runs the network.

        Args:
            batch_nn: dictionary with keys
                - "x_t": tensor of shape [b, n, 3]
                - "t": tensor of shape [b]
                - "mask": binary tensor of shape [b, n]
                - "x_sc" (optional): tensor of shape [b, n, 3]
                - "cath_code" (optional): list of cath codes [b, ?]
                - And potentially others... All in the data batch.

        Returns:
            Predicted clean coordinates, shape [b, n, 3].
        """
        mask = batch_nn["mask"]

        # Conditioning variables
        c = self.cond_factory(batch_nn)  # [b, n, dim_cond]
        c = self.transition_c_2(self.transition_c_1(c, mask), mask)  # [b, n, dim_cond]

        # Prepare input - coordinates and initial sequence representation from features
        coors_3d = batch_nn["x_t"] * mask[..., None]  # [b, n, 3]
        coors_embed = (
            self.linear_3d_embed(coors_3d) * mask[..., None]
        )  # [b, n, token_dim]
        seq_f_repr = self.init_repr_factory(batch_nn)  # [b, n, token_dim]
        seqs = coors_embed + seq_f_repr  # [b, n, token_dim]
        seqs = seqs * mask[..., None]  # [b, n, token_dim]

        # Pair representation
        pair_rep = None
        if self.use_attn_pair_bias:
            pair_rep = self.pair_repr_builder(batch_nn)  # [b, n, n, pair_dim]

        # Apply registers
        seqs, pair_rep, mask, c = self._extend_w_registers(seqs, pair_rep, mask, c)

        # Run trunk
        for i in range(self.nlayers):
            seqs = self.transformer_layers[i](
                seqs, pair_rep, c, mask
            )  # [b, n, token_dim]

            # # Coors on the fly
            # if self.update_coors_on_the_fly:
            #     coors_3d = self.seq_to_coors[i](seqs, coors_3d, mask)

            #     # Update sequence with coordinates
            #     if self.update_seq_with_coors == "linear" or self.update_seq_with_coors == "linearipa":
            #         seqs = self.coors_to_seq_linear[i](seqs, coors_3d, mask)

            #     if self.update_seq_with_coors == "ipa" or self.update_seq_with_coors == "linearipa":
            #         seqs = self.coors_to_seq_ipa[i](seqs, pair_rep, coors_3d, mask)

            if self.update_pair_repr:
                if i < self.nlayers - 1:
                    if self.pair_update_layers[i] is not None:
                        pair_rep = self.pair_update_layers[i](
                            seqs, pair_rep, mask
                        )  # [b, n, n, pair_dim]

        # Undo registers
        seqs, pair_rep, mask = self._undo_registers(seqs, pair_rep, mask)

        # Get final coordinates
        final_coors = self.coors_3d_decoder(seqs) * mask[..., None]  # [b, n, 3]
        # if self.update_coors_on_the_fly:
        #     final_coors = final_coors * 0.0 + coors_3d  # Ignore coordinates from final seuqence [b, n, 3]
        nn_out = {}
        if self.update_pair_repr and self.num_buckets_predict_pair is not None:
            pair_pred = self.pair_head_prediction(pair_rep)
            final_coors = (
                final_coors + torch.mean(pair_pred) * 0.0
            )  # Does not affect loss but pytorch does not complain for unused params
            final_coors = final_coors * mask[..., None]
            # If we actually end up using this we'll have to change what the NN returns here... And use it in the loss computation
            nn_out["pair_pred"] = pair_pred
        nn_out["coors_pred"] = final_coors
        return nn_out

    def nflops_computer(self, b, n):
        """Approximately how many flops used, for the main transformer layers. Protein length n.

        Final equation per layer per sample:
            12 * n * dim^2 + (dim + 1) * n^2 + 4 * n * 4 + [pair_dim * n^2]

        where the term in brackets [...] is used only with pair biased attn.

        The final number is obtained by multiplying by batch size and number of layers.

        Decomposing the number:
            - MHA:
                - Computing QKV with linear layers: 3n * dim^2
                - Computing attn logits: dim * n^2
                - Softmax: n^2
                - Attention: dim * n^2

            - Transition (feed forward, single hidden layer with expansion factor 4)
                - 1st linear layer: 4n * dim^2
                - activations: 4n * dim
                - 2nd linear layer: 4n * dim^2

            - Pair bias
                - (dim + 1) * n^2
        """
        # MHA
        # QKV
        nflops = 3 * n * self.token_dim**2
        # Attn logits
        nflops = nflops + self.token_dim * n**2
        # softmax
        nflops = nflops + n**2
        # attn
        nflops = nflops + self.token_dim * n**2
        # Some linear layer I might be missing

        # FF in transition, assumes ff has expansion_factor=4
        # Linear
        nflops = nflops + n * self.token_dim * 4 * self.token_dim
        # activation
        nflops = nflops + n * 4 * self.token_dim
        # Linear
        nflops = nflops + n * self.token_dim * 4 * self.token_dim

        # Pair bias
        if self.use_attn_pair_bias:
            # Computing and adding bias to attn logits
            nflops = nflops + (self.pair_repr_dim + 1) * n**2

        # Ignoring updating pair representation when used

        return b * nflops * self.nlayers  # Adjust for batch size and layers
