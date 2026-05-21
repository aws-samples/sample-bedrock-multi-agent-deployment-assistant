"""Unit tests for agent helper functions, configuration, and constants.

Tests focus on pure functions and data structures that do NOT require
AWS credentials or Bedrock model instantiation.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# We must mock BedrockModel before importing the agent modules, because
# each module instantiates an Agent at import time (module-level singleton).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_bedrock_imports():
    """Patch BedrockModel and Agent so module-level agent creation is a no-op."""
    mock_bedrock_model = MagicMock()
    mock_agent_cls = MagicMock()

    with (
        patch("strands.models.bedrock.BedrockModel", mock_bedrock_model),
        patch("strands.Agent", mock_agent_cls),
    ):
        # Clear cached module imports so patches take effect
        mods_to_clear = [
            k for k in sys.modules if k.startswith("src.agents")
        ]
        for mod in mods_to_clear:
            del sys.modules[mod]
        yield


# ===================================================================
# IaC Agent — _strip_fences helper
# ===================================================================


class TestStripFencesIaC:
    """Tests for src.agents.common.strip_fences (formerly in iac.py)."""

    def test_removes_hcl_fences(self):
        from src.agents.common import strip_fences

        text = '```hcl\nresource "aws_vpc" {}\n```'
        result = strip_fences(text)
        assert result == 'resource "aws_vpc" {}'

    def test_removes_plain_fences(self):
        from src.agents.common import strip_fences

        text = "```\nsome code\n```"
        result = strip_fences(text)
        assert result == "some code"

    def test_no_fences_passthrough(self):
        from src.agents.common import strip_fences

        text = 'resource "aws_vpc" {}'
        result = strip_fences(text)
        assert result == text

    def test_removes_yaml_fences(self):
        from src.agents.common import strip_fences

        text = "```yaml\nAWSTemplateFormatVersion: '2010-09-09'\n```"
        result = strip_fences(text)
        assert result == "AWSTemplateFormatVersion: '2010-09-09'"

    def test_multiline_content(self):
        from src.agents.common import strip_fences

        text = "```hcl\nresource \"aws_vpc\" \"main\" {\n  cidr_block = \"10.0.0.0/16\"\n}\n```"
        result = strip_fences(text)
        assert 'resource "aws_vpc" "main"' in result
        assert "cidr_block" in result
        assert "```" not in result

    def test_empty_string(self):
        from src.agents.common import strip_fences

        result = strip_fences("")
        assert result == ""

    def test_only_opening_fence(self):
        from src.agents.common import strip_fences

        text = "```hcl\nresource {}"
        result = strip_fences(text)
        assert result == "resource {}"

    def test_only_closing_fence(self):
        from src.agents.common import strip_fences

        text = "resource {}\n```"
        result = strip_fences(text)
        assert result == "resource {}"

    def test_whitespace_around_fences(self):
        from src.agents.common import strip_fences

        text = "  ```hcl\ncode here\n```  "
        # .strip() is called first, so leading whitespace on first line is stripped
        result = strip_fences(text)
        assert "```" not in result
        assert "code here" in result


# ===================================================================
# ValidationReport — error classification methods
# ===================================================================


class TestValidationReport:
    """Tests for ValidationReport blocking/non-blocking error classification."""

    def _make_finding(self, layer, severity="error", rule_id="TEST"):
        from src.models.iac import ValidationFinding

        return ValidationFinding(
            layer=layer, severity=severity, rule_id=rule_id, message="test"
        )

    def test_has_blocking_errors_with_structural(self):
        from src.models.iac import ValidationReport

        report = ValidationReport(
            passed=False,
            findings=[self._make_finding("structural")],
            layers_executed=["structural"],
        )
        assert report.has_blocking_errors() is True

    def test_has_blocking_errors_with_cfn_lint(self):
        from src.models.iac import ValidationReport

        report = ValidationReport(
            passed=False,
            findings=[self._make_finding("cfn-lint")],
            layers_executed=["structural", "cfn-lint"],
        )
        assert report.has_blocking_errors() is True

    def test_no_blocking_errors_with_only_checkov(self):
        from src.models.iac import ValidationReport

        report = ValidationReport(
            passed=True,
            findings=[self._make_finding("checkov")],
            layers_executed=["structural", "cfn-lint", "checkov"],
        )
        assert report.has_blocking_errors() is False

    def test_no_blocking_errors_with_only_cfn_guard(self):
        from src.models.iac import ValidationReport

        report = ValidationReport(
            passed=True,
            findings=[self._make_finding("cfn-guard")],
            layers_executed=["structural", "cfn-lint", "checkov", "cfn-guard"],
        )
        assert report.has_blocking_errors() is False

    def test_warnings_are_not_blocking(self):
        from src.models.iac import ValidationReport

        report = ValidationReport(
            passed=True,
            findings=[self._make_finding("structural", severity="warning")],
            layers_executed=["structural"],
        )
        assert report.has_blocking_errors() is False

    def test_blocking_error_count(self):
        from src.models.iac import ValidationReport

        report = ValidationReport(
            passed=False,
            findings=[
                self._make_finding("structural"),
                self._make_finding("cfn-lint"),
                self._make_finding("checkov"),
                self._make_finding("cfn-guard"),
                self._make_finding("structural", severity="warning"),
            ],
            layers_executed=["structural", "cfn-lint", "checkov", "cfn-guard"],
        )
        assert report.blocking_error_count() == 2

    def test_blocking_findings_returns_only_blocking(self):
        from src.models.iac import ValidationReport

        report = ValidationReport(
            passed=False,
            findings=[
                self._make_finding("structural", rule_id="S1"),
                self._make_finding("cfn-lint", rule_id="E1234"),
                self._make_finding("checkov", rule_id="CKV_AWS_1"),
            ],
            layers_executed=["structural", "cfn-lint", "checkov"],
        )
        blocking = report.blocking_findings()
        assert len(blocking) == 2
        assert all(f.layer in ("structural", "cfn-lint") for f in blocking)

    def test_non_blocking_findings_returns_checkov_and_guard(self):
        from src.models.iac import ValidationReport

        report = ValidationReport(
            passed=True,
            findings=[
                self._make_finding("structural", severity="warning"),
                self._make_finding("checkov", rule_id="CKV_AWS_1"),
                self._make_finding("cfn-guard", rule_id="FG_001"),
            ],
            layers_executed=["structural", "cfn-lint", "checkov", "cfn-guard"],
        )
        non_blocking = report.non_blocking_findings()
        assert len(non_blocking) == 2
        assert all(f.layer in ("checkov", "cfn-guard") for f in non_blocking)

    def test_error_count_all_layers(self):
        from src.models.iac import ValidationReport

        report = ValidationReport(
            passed=False,
            findings=[
                self._make_finding("structural"),
                self._make_finding("cfn-lint"),
                self._make_finding("checkov"),
                self._make_finding("cfn-guard", severity="warning"),
            ],
            layers_executed=["structural", "cfn-lint", "checkov", "cfn-guard"],
        )
        assert report.error_count() == 3  # 3 errors, 1 warning

    def test_empty_report(self):
        from src.models.iac import ValidationReport

        report = ValidationReport(
            passed=True, findings=[], layers_executed=[]
        )
        assert report.has_blocking_errors() is False
        assert report.blocking_error_count() == 0
        assert report.blocking_findings() == []
        assert report.non_blocking_findings() == []
        assert report.error_count() == 0


# ===================================================================
# Documentation Agent — prompt file loading
# ===================================================================


class TestDocsPromptFiles:
    """Tests for documentation agent external prompt template files."""

    def test_load_prompt_diagram(self):
        from src.agents.documentation import _load_prompt

        prompt = _load_prompt("docs_diagram.txt")
        assert "{cft_template}" in prompt
        assert "architecture-beta" in prompt

    def test_load_prompt_diagram_fix(self):
        from src.agents.documentation import _load_prompt

        prompt = _load_prompt("docs_diagram_fix.txt")
        assert "{diagram_code}" in prompt
        assert "{validation_errors}" in prompt
        assert "{cft_template}" in prompt

    def test_load_prompt_user_guide(self):
        from src.agents.documentation import _load_prompt

        prompt = _load_prompt("docs_user_guide.txt")
        assert "{design_json}" in prompt
        assert "{requirements_json}" in prompt
        assert "{cft_template}" in prompt

    def test_all_prompt_files_exist(self):
        from pathlib import Path

        prompts_dir = Path(__file__).parent.parent / "src" / "prompts"
        expected = [
            "docs_diagram.txt",
            "docs_diagram_fix.txt",
            "docs_user_guide.txt",
        ]
        for name in expected:
            assert (prompts_dir / name).exists(), f"Missing prompt file: {name}"


# ===================================================================
# Documentation Agent — system prompts
# ===================================================================


class TestDocsSystemPrompts:
    """Tests for documentation agent system prompt constants."""

    def test_diagram_system_prompt_mentions_mermaid(self):
        from src.agents.documentation import _DIAGRAM_SYSTEM_PROMPT

        assert "Mermaid" in _DIAGRAM_SYSTEM_PROMPT

    def test_text_system_prompt_mentions_documentation(self):
        from src.agents.documentation import _TEXT_SYSTEM_PROMPT

        assert "Technical Writer" in _TEXT_SYSTEM_PROMPT

    def test_text_system_prompt_mentions_markdown(self):
        from src.agents.documentation import _TEXT_SYSTEM_PROMPT

        assert "Markdown" in _TEXT_SYSTEM_PROMPT


# ===================================================================
# Documentation Agent — DocumentationOutput model
# ===================================================================


class TestDocumentationOutputModel:
    """Tests for the DocumentationOutput pydantic model."""

    def test_default_values(self):
        from src.models.docs import DocumentationOutput

        output = DocumentationOutput()
        assert output.user_guide == ""
        assert output.architecture_diagram == ""
        assert output.diagram_fix_attempts == 0
        assert output.diagram_validation_passed is False

    def test_with_content(self):
        from src.models.docs import DocumentationOutput

        output = DocumentationOutput(
            user_guide="# Guide",
            architecture_diagram="architecture-beta\n  service s1(aws:ec2)[EC2]",
            diagram_fix_attempts=2,
            diagram_validation_passed=True,
        )
        assert output.diagram_fix_attempts == 2
        assert output.diagram_validation_passed is True
        assert "Guide" in output.user_guide


# ===================================================================
# Mermaid Validator — unit tests
# ===================================================================


class TestMermaidValidator:
    """Tests for src.tools.mermaid_validator.validate_mermaid."""

    def test_empty_input_returns_invalid(self):
        from src.tools.mermaid_validator import validate_mermaid

        valid, error = validate_mermaid("")
        assert valid is False
        assert "Empty" in error

    def test_whitespace_only_returns_invalid(self):
        from src.tools.mermaid_validator import validate_mermaid

        valid, error = validate_mermaid("   \n  ")
        assert valid is False
        assert "Empty" in error

    def test_missing_validator_gracefully_degrades(self):
        """When the Node.js validator script isn't found, treat as valid."""
        from unittest.mock import patch as _patch
        from src.tools.mermaid_validator import validate_mermaid

        with _patch("src.tools.mermaid_validator._VALIDATOR_SCRIPT") as mock_path:
            mock_path.exists.return_value = False
            valid, error = validate_mermaid("architecture-beta\n  service s1(aws:ec2)[EC2]")
            assert valid is True
            assert error == ""

    def test_node_not_found_gracefully_degrades(self):
        """When Node.js is not installed, treat as valid (graceful degradation)."""
        from unittest.mock import patch as _patch
        from src.tools.mermaid_validator import validate_mermaid

        with _patch("src.tools.mermaid_validator._VALIDATOR_SCRIPT") as mock_path:
            mock_path.exists.return_value = True
            with _patch("subprocess.run", side_effect=FileNotFoundError("node not found")):
                valid, error = validate_mermaid("architecture-beta")
                assert valid is True

    def test_timeout_returns_invalid(self):
        """Subprocess timeout should return invalid."""
        import subprocess
        from unittest.mock import patch as _patch
        from src.tools.mermaid_validator import validate_mermaid

        with _patch("src.tools.mermaid_validator._VALIDATOR_SCRIPT") as mock_path:
            mock_path.exists.return_value = True
            with _patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="node", timeout=15)):
                valid, error = validate_mermaid("architecture-beta")
                assert valid is False
                assert "timed out" in error

    def test_valid_json_output_parsed(self):
        """Valid JSON output from validator is parsed correctly."""
        from unittest.mock import patch as _patch
        from src.tools.mermaid_validator import validate_mermaid

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"valid": true}'

        with _patch("src.tools.mermaid_validator._VALIDATOR_SCRIPT") as mock_path:
            mock_path.exists.return_value = True
            with _patch("subprocess.run", return_value=mock_result):
                valid, error = validate_mermaid("architecture-beta")
                assert valid is True
                assert error == ""

    def test_invalid_json_output_parsed(self):
        """Invalid diagram JSON output from validator is parsed correctly."""
        from unittest.mock import patch as _patch
        from src.tools.mermaid_validator import validate_mermaid

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"valid": false, "error": "Parse error at line 3"}'

        with _patch("src.tools.mermaid_validator._VALIDATOR_SCRIPT") as mock_path:
            mock_path.exists.return_value = True
            with _patch("subprocess.run", return_value=mock_result):
                valid, error = validate_mermaid("bad diagram")
                assert valid is False
                assert "Parse error" in error


# ===================================================================
# Tool Policies
# ===================================================================


class TestToolPolicies:
    """Tests for src.config.tool_policies."""

    def test_design_agent_tools(self):
        from src.config.tool_policies import TOOL_POLICIES

        assert TOOL_POLICIES["design-agent"] == {"kb_search", "evaluate_design_against_wa"}

    def test_interview_agents_have_no_tools(self):
        from src.config.tool_policies import TOOL_POLICIES

        assert TOOL_POLICIES["interview-planner"] == set()
        assert TOOL_POLICIES["interview-executor"] == set()
        assert TOOL_POLICIES["interview-replanner"] == set()

    def test_docs_agents_have_no_tools(self):
        from src.config.tool_policies import TOOL_POLICIES

        assert TOOL_POLICIES["docs-diagram"] == set()
        assert TOOL_POLICIES["docs-diagram-fix"] == set()
        assert TOOL_POLICIES["docs-user-guide"] == set()

    def test_denied_tools_include_dangerous_operations(self):
        from src.config.tool_policies import DENIED_TOOLS

        assert "aws_deploy" in DENIED_TOOLS
        assert "execute_command" in DENIED_TOOLS
        assert "shell" in DENIED_TOOLS
        assert "run_code" in DENIED_TOOLS

    def test_validate_tool_assignment_filters_unauthorized(self):
        from src.config.tool_policies import validate_tool_assignment

        # Use spec=lambda: None to prevent MagicMock from auto-creating
        # a tool_name attribute (hasattr check in validate_tool_assignment)
        mock_tool1 = MagicMock(spec=lambda: None)
        mock_tool1.__name__ = "kb_search"
        mock_tool2 = MagicMock(spec=lambda: None)
        mock_tool2.__name__ = "save_artifact"

        result = validate_tool_assignment("design-agent", [mock_tool1, mock_tool2])
        # save_artifact is NOT in design-agent's policy — should be filtered out
        assert len(result) == 1
        assert result[0].__name__ == "kb_search"

    def test_validate_tool_assignment_blocks_denied_tools(self):
        from src.config.tool_policies import validate_tool_assignment

        mock_tool = MagicMock(spec=lambda: None)
        mock_tool.__name__ = "aws_deploy"

        result = validate_tool_assignment("design-agent", [mock_tool])
        assert len(result) == 0

    def test_validate_tool_assignment_unknown_agent_denies_all(self):
        """Unknown agents get no tools (fail-closed)."""
        from src.config.tool_policies import validate_tool_assignment

        mock_tool = MagicMock(spec=lambda: None)
        mock_tool.__name__ = "some_tool"

        result = validate_tool_assignment("unknown-agent", [mock_tool])
        assert len(result) == 0

    def test_get_authorized_tools_delegates_to_validate(self):
        from src.config.tool_policies import get_authorized_tools

        mock_tool = MagicMock(spec=lambda: None)
        mock_tool.__name__ = "kb_search"

        result = get_authorized_tools("design-agent", [mock_tool])
        assert len(result) == 1

    def test_validate_tool_assignment_uses_tool_name_attr(self):
        """Tools with a tool_name attribute should use that for policy lookup."""
        from src.config.tool_policies import validate_tool_assignment

        mock_tool = MagicMock()
        mock_tool.__name__ = "wrapper_fn"
        mock_tool.tool_name = "kb_search"

        result = validate_tool_assignment("design-agent", [mock_tool])
        assert len(result) == 1


# ===================================================================
# InterviewProgress model
# ===================================================================


class TestInterviewProgress:
    """Tests for InterviewProgress structured output model."""

    def test_default_values(self):
        from src.models.requirements import InterviewProgress

        progress = InterviewProgress(response_message="Hello")
        assert progress.response_message == "Hello"
        assert progress.complete is False
        assert progress.missing_fields == []
        assert progress.use_cases is None
        assert progress.gpu_budget is None

    def test_to_interview_output_defaults(self):
        from src.models.requirements import InterviewProgress, UseCases, WorkloadResilience

        progress = InterviewProgress(response_message="test", complete=False)
        doc = progress.to_interview_output()
        assert doc.use_cases == ["notknown"]
        assert doc.gpu_budget == "notknown"
        assert doc.availability_requirement == "notknown"
        assert doc.data_sensitivity == "notknown"
        assert doc.compliance == []
        assert doc.solution_description == ""

    def test_to_interview_output_with_values(self):
        from src.models.requirements import (
            InterviewProgress, UseCases, WorkloadResilience, UserInformation
        )

        progress = InterviewProgress(
            response_message="Ready to proceed",
            use_cases=["batch-inference"],
            gpu_budget="high",
            availability_requirement="production-multi-az",
            data_sensitivity="confidential",
            user_info=UserInformation(name="John", experience_on_cloud="advanced"),
            compliance=["pci-dss"],
            solution_description="Batch inference pipeline for ML workloads",
            complete=True,
        )
        doc = progress.to_interview_output()
        assert doc.use_cases == ["batch-inference"]
        assert doc.gpu_budget == "high"
        assert doc.availability_requirement == "production-multi-az"
        assert doc.data_sensitivity == "confidential"
        assert doc.compliance == ["pci-dss"]
        assert doc.solution_description == "Batch inference pipeline for ML workloads"

    def test_to_interview_output_with_use_case_fields(self):
        """Use-case-specific fields are built from use_case_fields via catalog."""
        from src.models.requirements import InterviewProgress

        progress = InterviewProgress(
            response_message="test",
            use_cases=["realtime-inference"],
            use_case_fields={
                "model_size_category": "medium",
                "target_latency_ms": 100,
                "target_throughput_rps": 500,
            },
        )
        doc = progress.to_interview_output()
        assert "realtime-inference" in doc.use_case_details
        assert doc.use_case_details["realtime-inference"]["model_size_category"] == "medium"
        assert doc.use_case_details["realtime-inference"]["target_latency_ms"] == 100

    def test_missing_fields_tracking(self):
        from src.models.requirements import InterviewProgress

        progress = InterviewProgress(
            response_message="Need more info",
            missing_fields=["use_cases", "gpu_budget"],
        )
        assert "use_cases" in progress.missing_fields
        assert "gpu_budget" in progress.missing_fields


# ===================================================================
# Interview Agent v2 — public API re-exports
# ===================================================================


class TestInterviewAgentExports:
    """Verify the interview agent module re-exports the v2 public API."""

    def test_exports_generate_plan(self):
        from src.agents.interview import generate_plan
        assert callable(generate_plan)

    def test_exports_execute_turn(self):
        from src.agents.interview import execute_turn
        assert callable(execute_turn)

    def test_exports_replan(self):
        from src.agents.interview import replan
        assert callable(replan)


# ===================================================================
# IaC Agent — prompt file loading
# ===================================================================


class TestIaCPromptFiles:
    """Tests for IaC agent prompt files."""

    def test_layer_generate_prompt_exists_and_has_placeholders(self):
        from pathlib import Path

        prompt_path = Path(__file__).parent.parent / "src" / "prompts" / "iac_layer_generate.txt"
        assert prompt_path.exists()
        content = prompt_path.read_text()
        assert "{layer_name}" in content
        assert "{kb_context}" in content
        assert "{resolved_params_json}" in content

    def test_architecture_planner_prompt_exists_and_has_placeholders(self):
        from pathlib import Path

        prompt_path = Path(__file__).parent.parent / "src" / "prompts" / "iac_architecture_planner.txt"
        assert prompt_path.exists()
        content = prompt_path.read_text()
        assert "{kb_context}" in content
        assert "{resolved_params_json}" in content

    def test_layer_fix_prompt_exists_and_has_placeholders(self):
        from pathlib import Path

        prompt_path = Path(__file__).parent.parent / "src" / "prompts" / "iac_layer_fix.txt"
        assert prompt_path.exists()
        content = prompt_path.read_text()
        assert "{layer_name}" in content
        assert "{validation_errors}" in content

    def test_old_monolithic_generate_prompt_deleted(self):
        """iac_generate.txt replaced by iac_layer_generate.txt + iac_architecture_planner.txt."""
        from pathlib import Path

        prompt_path = Path(__file__).parent.parent / "src" / "prompts" / "iac_generate.txt"
        assert not prompt_path.exists()

    def test_compose_prompt_exists_and_has_placeholders(self):
        from pathlib import Path

        prompt_path = Path(__file__).parent.parent / "src" / "prompts" / "iac_compose.txt"
        assert prompt_path.exists()
        content = prompt_path.read_text()
        assert "{snippets_summary}" in content
        assert "{resolved_params_json}" in content

    def test_fix_prompt_exists_and_has_placeholders(self):
        from pathlib import Path

        prompt_path = Path(__file__).parent.parent / "src" / "prompts" / "iac_fix.txt"
        assert prompt_path.exists()
        content = prompt_path.read_text()
        assert "{resource_plan_json}" in content
        assert "{validation_errors}" in content

    def test_parameterize_prompt_deleted(self):
        """Path 1 is now pure Python — the parameterize prompt should not exist."""
        from pathlib import Path

        prompt_path = Path(__file__).parent.parent / "src" / "prompts" / "iac_parameterize.txt"
        assert not prompt_path.exists()


# ===================================================================
# IaC Agent — ResourcePlan models
# ===================================================================


class TestResourcePlanModels:
    """Tests for src.models.resource_plan Pydantic models."""

    def test_resource_plan_requires_at_least_one_resource(self):
        import pytest
        from src.models.resource_plan import ResourcePlan

        with pytest.raises(Exception):
            ResourcePlan(resources=[])

    def test_resource_plan_rejects_duplicate_logical_ids(self):
        import pytest
        from src.models.resource_plan import CfnResource, ResourcePlan

        with pytest.raises(Exception, match="Duplicate"):
            ResourcePlan(resources=[
                CfnResource(logical_id="VPC", type="AWS::EC2::VPC", properties={}),
                CfnResource(logical_id="VPC", type="AWS::EC2::VPC", properties={}),
            ])

    def test_cfn_resource_validates_type_prefix(self):
        import pytest
        from src.models.resource_plan import CfnResource

        with pytest.raises(Exception, match="AWS::"):
            CfnResource(logical_id="Bad", type="EC2::VPC", properties={})

    def test_cfn_resource_allows_custom_prefix(self):
        from src.models.resource_plan import CfnResource

        r = CfnResource(logical_id="Custom", type="Custom::MyResource", properties={})
        assert r.type == "Custom::MyResource"

    def test_resource_plan_serialisation_roundtrip(self):
        from src.models.resource_plan import CfnParameter, CfnResource, ResourcePlan

        plan = ResourcePlan(
            description="Test",
            parameters=[CfnParameter(logical_id="P1", type="String", default="val")],
            resources=[CfnResource(logical_id="R1", type="AWS::EC2::VPC", properties={"CidrBlock": "10.0.0.0/16"})],
        )
        json_str = plan.model_dump_json()
        restored = ResourcePlan.model_validate_json(json_str)
        assert restored.resources[0].logical_id == "R1"
        assert restored.parameters[0].default == "val"
