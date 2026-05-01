"""Unit tests for layer plan models and predefined patterns."""

import pytest

from src.models.layer_plan import (
    LayerExport,
    LayerImport,
    LayerName,
    LayerPlan,
    LayerSpec,
)
from src.services.predefined_layers import (
    auto_scaling_fleet_plan,
    batch_processing_plan,
    distributed_training_plan,
    get_predefined_plan,
    single_instance_plan,
)


# ===================================================================
# LayerPlan.parallelizable_groups()
# ===================================================================


class TestParallelizableGroups:

    def test_independent_layers_single_group(self):
        """Layers with no imports should all be in one group."""
        plan = LayerPlan(
            pattern_name="test",
            description="test",
            layers=[
                LayerSpec(
                    name=LayerName.FOUNDATION,
                    description="a",
                    resource_types=["AWS::EC2::VPC"],
                    imports=[],
                    exports=[],
                ),
                LayerSpec(
                    name=LayerName.SECURITY,
                    description="b",
                    resource_types=["AWS::EC2::SecurityGroup"],
                    imports=[],
                    exports=[],
                ),
            ],
        )
        groups = plan.parallelizable_groups()
        assert len(groups) == 1
        assert len(groups[0]) == 2

    def test_dependent_layers_separate_groups(self):
        """A layer that imports from another must be in a later group."""
        plan = LayerPlan(
            pattern_name="test",
            description="test",
            layers=[
                LayerSpec(
                    name=LayerName.FOUNDATION,
                    description="a",
                    resource_types=["AWS::EC2::VPC"],
                    imports=[],
                    exports=[LayerExport(name="VpcId", resource_logical_id="VPC")],
                ),
                LayerSpec(
                    name=LayerName.SECURITY,
                    description="b",
                    resource_types=["AWS::EC2::SecurityGroup"],
                    imports=[LayerImport(name="VpcId", source_layer=LayerName.FOUNDATION, parameter_name="VpcId")],
                    exports=[LayerExport(name="SGId", resource_logical_id="SG")],
                ),
                LayerSpec(
                    name=LayerName.COMPUTE,
                    description="c",
                    resource_types=["AWS::EC2::Instance"],
                    imports=[
                        LayerImport(name="VpcId", source_layer=LayerName.FOUNDATION, parameter_name="VpcId"),
                        LayerImport(name="SGId", source_layer=LayerName.SECURITY, parameter_name="SGId"),
                    ],
                    exports=[],
                ),
            ],
        )
        groups = plan.parallelizable_groups()
        assert len(groups) == 3
        assert {g.name for g in groups[0]} == {LayerName.FOUNDATION}
        assert {g.name for g in groups[1]} == {LayerName.SECURITY}
        assert {g.name for g in groups[2]} == {LayerName.COMPUTE}


class TestGetLayer:

    def test_get_existing_layer(self):
        plan = single_instance_plan()
        layer = plan.get_layer(LayerName.FOUNDATION)
        assert layer is not None
        assert layer.name == LayerName.FOUNDATION

    def test_get_nonexistent_layer(self):
        plan = single_instance_plan()
        layer = plan.get_layer(LayerName.HA)
        assert layer is None


# ===================================================================
# Predefined plan validation
# ===================================================================


class TestPredefinedPlans:
    """Validate structural integrity of all predefined plans."""

    @pytest.fixture(params=[
        single_instance_plan,
        auto_scaling_fleet_plan,
        batch_processing_plan,
        distributed_training_plan,
    ])
    def plan(self, request) -> LayerPlan:
        return request.param()

    def test_has_foundation_layer(self, plan: LayerPlan):
        assert plan.get_layer(LayerName.FOUNDATION) is not None

    def test_has_security_layer(self, plan: LayerPlan):
        assert plan.get_layer(LayerName.SECURITY) is not None

    def test_has_compute_layer(self, plan: LayerPlan):
        assert plan.get_layer(LayerName.COMPUTE) is not None

    def test_no_circular_imports(self, plan: LayerPlan):
        """Every import must reference a layer that appears earlier in dependency order."""
        groups = plan.parallelizable_groups()
        resolved: set[str] = set()
        for group in groups:
            for layer in group:
                for imp in layer.imports:
                    assert imp.source_layer in resolved, (
                        f"Layer {layer.name} imports from {imp.source_layer} "
                        f"which is not yet resolved (circular dependency)"
                    )
            for layer in group:
                resolved.add(layer.name)

    def test_all_imports_have_matching_exports(self, plan: LayerPlan):
        """Every import name must match an export name in its source layer."""
        export_map: dict[str, set[str]] = {}
        for layer in plan.layers:
            export_map[layer.name] = {exp.name for exp in layer.exports}

        for layer in plan.layers:
            for imp in layer.imports:
                assert imp.source_layer in export_map, (
                    f"Layer {layer.name} imports from {imp.source_layer} "
                    f"which does not exist"
                )
                assert imp.name in export_map[imp.source_layer], (
                    f"Layer {layer.name} imports '{imp.name}' from "
                    f"{imp.source_layer} but that layer does not export it. "
                    f"Available exports: {export_map[imp.source_layer]}"
                )

    def test_foundation_has_no_imports(self, plan: LayerPlan):
        """Foundation layer should never depend on other layers."""
        foundation = plan.get_layer(LayerName.FOUNDATION)
        assert foundation is not None
        assert foundation.imports == []

    def test_all_resource_types_are_aws_prefixed(self, plan: LayerPlan):
        for layer in plan.layers:
            for rt in layer.resource_types:
                assert rt.startswith("AWS::") or rt.startswith("Custom::"), (
                    f"Layer {layer.name} has invalid resource type: {rt}"
                )


# ===================================================================
# get_predefined_plan() fuzzy matching
# ===================================================================


class TestGetPredefinedPlan:

    def test_exact_match(self):
        plan = get_predefined_plan("single-instance")
        assert plan is not None
        assert plan.pattern_name == "single-instance"

    def test_fleet_match(self):
        plan = get_predefined_plan("auto-scaling-fleet")
        assert plan is not None
        assert "auto-scaling" in plan.pattern_name.lower()

    def test_batch_match(self):
        plan = get_predefined_plan("batch")
        assert plan is not None
        assert "batch" in plan.pattern_name.lower()

    def test_training_match(self):
        plan = get_predefined_plan("distributed-training")
        assert plan is not None
        assert "training" in plan.pattern_name.lower()

    def test_alias_match(self):
        plan = get_predefined_plan("dev")
        assert plan is not None
        assert plan.pattern_name == "single-instance"

    def test_substring_match(self):
        plan = get_predefined_plan("multi-node-training-cluster")
        assert plan is not None
        assert "training" in plan.pattern_name.lower()

    def test_unknown_returns_none(self):
        plan = get_predefined_plan("quantum-entanglement-mesh")
        assert plan is None

    def test_normalizes_underscores(self):
        plan = get_predefined_plan("auto_scaling_fleet")
        assert plan is not None
