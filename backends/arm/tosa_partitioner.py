# Copyright 2023-2025 Arm Limited and/or its affiliates.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-unsafe

import logging
from typing import Callable, List, Optional, Sequence, Tuple

import torch
from executorch.backends.arm.arm_backend import (
    get_tosa_spec,
    is_tosa,
)  # usort: skip
from executorch.backends.arm._passes.arm_pass_utils import get_first_fake_tensor
from executorch.backends.arm.operator_support.tosa_supported_operators import (
    tosa_support_factory,
)
from executorch.backends.arm.tosa_backend import TOSABackend
from executorch.exir.backend.compile_spec_schema import CompileSpec
from executorch.exir.backend.partitioner import (
    DelegationSpec,
    Partitioner,
    PartitionResult,
)
from executorch.exir.backend.utils import tag_constant_data, WhyNoPartitionReporter
from executorch.exir.dialects._ops import ops as exir_ops
from torch.export.exported_program import ExportedProgram
from torch.fx.passes.infra.partitioner import CapabilityBasedPartitioner
from torch.fx.passes.operator_support import OperatorSupportBase


logger = logging.getLogger(__name__)


def is_quant_node(node: torch.fx.node.Node) -> bool:
    return node.target in {
        exir_ops.edge.quantized_decomposed.quantize_per_channel.default,
        exir_ops.edge.quantized_decomposed.quantize_per_tensor.default,
        exir_ops.edge.quantized_decomposed.quantize_per_tensor.tensor,
    }


def is_dequant_node(node: torch.fx.node.Node) -> bool:
    return node.target in {
        exir_ops.edge.quantized_decomposed.dequantize_per_channel.default,
        exir_ops.edge.quantized_decomposed.dequantize_per_tensor.default,
        exir_ops.edge.quantized_decomposed.dequantize_per_tensor.tensor,
    }


class TOSAPartitioner(Partitioner):
    def __init__(
        self,
        compile_spec: List[CompileSpec],
        additional_checks: Optional[Sequence[OperatorSupportBase]] = None,
    ) -> None:
        if not is_tosa(compile_spec):
            raise RuntimeError("compile spec is not targeting TOSA")
        self.delegation_spec = DelegationSpec(TOSABackend.__name__, compile_spec)
        self.additional_checks = additional_checks

    def partition(self, exported_program: ExportedProgram) -> PartitionResult:  # noqa
        # Run the CapabilityBasedPartitioner to return the largest possible
        # subgraphs containing the nodes with the tags

        logger.info("TOSAPartitioner::partition")
        partition_tags = {}

        tosa_spec = get_tosa_spec(self.delegation_spec.compile_specs)

        logger.info(f"Partitioning for {self.delegation_spec.backend_id}: {tosa_spec}")

        reporter = WhyNoPartitionReporter()
        operator_support = tosa_support_factory(
            tosa_spec, exported_program, reporter, self.additional_checks
        )
        capability_partitioner = CapabilityBasedPartitioner(
            exported_program.graph_module,
            operator_support,
            allows_single_node_partition=True,
        )
        partition_list = capability_partitioner.propose_partitions()
        for partition in partition_list:
            tag = f"tag{partition.id}"

            def is_partitioned(node: torch.fx.Node, tag=tag) -> bool:
                return (
                    "delegation_tag" in node.meta and node.meta["delegation_tag"] == tag
                )

            for node in partition.nodes:
                node.meta["delegation_tag"] = tag
                partition_tags[tag] = self.delegation_spec

            # De-tag outmost q-nodes upwards and dq-nodes downwards.
            # De-tag if at least one input/ output is not part of partition.
            for node in exported_program.graph_module.graph.nodes:
                if not is_partitioned(node):
                    continue
                if is_quant_node(node):
                    for input in node.all_input_nodes:
                        if not is_partitioned(input):
                            del node.meta["delegation_tag"]
                            break
                    continue

                if is_dequant_node(node):
                    for user in node.users:
                        if not is_partitioned(user):
                            del node.meta["delegation_tag"]
                            break
                    continue

                if tosa_spec.support_float():
                    continue

                if is_partitioned(node):
                    for input in node.all_input_nodes:
                        if is_partitioned(input):
                            continue
                        if get_first_fake_tensor(input).dtype.is_floating_point:
                            reporter.report_reject(
                                node,
                                f"Was first node in partition and input {input.name} had fp dtype.",
                            )
                            del node.meta["delegation_tag"]
                            break

        tag_constant_data(exported_program)
        logger.info(f"The following nodes were rejected for {tosa_spec}:")
        logger.info("\n" + reporter.get_table_report())
        logger.info("(Placeholders and outputs are not included in this list)")
        return PartitionResult(
            tagged_exported_program=exported_program, partition_tags=partition_tags
        )

    def ops_to_not_decompose(
        self,
        ep: ExportedProgram,
    ) -> Tuple[List[torch._ops.OpOverload], Optional[Callable[[torch.fx.Node], bool]]]:
        ops_to_not_decompose_if_quant_op = [
            torch.ops.aten.hardsigmoid.default,
            torch.ops.aten.hardswish.default,
        ]

        def filter_fn(node: torch.fx.Node) -> bool:
            # This function filters for operators to not decompose where:
            #   - It's target is in ops_to_not_decompose_if_quant_op list.
            #   - All it's inputs/outputs are quantize operators.
            dq = torch.ops.quantized_decomposed.dequantize_per_tensor.default
            q = torch.ops.quantized_decomposed.quantize_per_tensor.default

            if node.target in ops_to_not_decompose_if_quant_op:
                # Assume we should not decompose the operator (it is quantized)
                should_not_decompose = True

                input_nodes = node.all_input_nodes
                ouput_nodes = node.users

                for inp in input_nodes:
                    if inp.target != dq:
                        should_not_decompose = False

                for out in ouput_nodes:
                    if out.target != q:
                        should_not_decompose = False

                return should_not_decompose

            # Be default, do not decompose the operator
            return True

        ops_to_not_decompose = [
            torch.ops.aten.linear.default,
            torch.ops.aten.eye.default,
            torch.ops.aten.linspace.default,
        ] + ops_to_not_decompose_if_quant_op

        tosa_spec = get_tosa_spec(self.delegation_spec.compile_specs)
        if not tosa_spec.is_U55_subset:
            # Tosa operator "RESIZE" is not supported on U55. Since upsample_bilinear2d
            # and upsample_nearest2d decompose into that it will not be possible to
            # delegate those operators on U55. If we have said here to not decompose
            # them there will be an error saying the operator was not decomposed. It
            # will not be possible for it to end up on either CPU or NPU.
            ops_to_not_decompose.append(torch.ops.aten.upsample_nearest2d.vec)
            ops_to_not_decompose.append(torch.ops.aten.upsample_bilinear2d.vec)

        return (ops_to_not_decompose, filter_fn)
