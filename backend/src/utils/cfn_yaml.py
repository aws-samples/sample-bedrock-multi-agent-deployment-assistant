"""CFN-aware YAML loader/dumper — handles !Ref, !Sub, !GetAtt, etc.

Extracted from src/agents/iac.py for shared use across IaC generation
and template validation.
"""

import yaml as _yaml


class CfnTag:
    """Wrapper to preserve a CloudFormation YAML tag through load -> dump."""

    def __init__(self, tag: str, value):
        self.tag = tag
        self.value = value

    def __repr__(self) -> str:
        return f"CfnTag({self.tag!r}, {self.value!r})"


class CfnLoader(_yaml.SafeLoader):
    pass


class CfnDumper(_yaml.SafeDumper):
    pass


# Register constructors for all CloudFormation intrinsic functions
CFN_TAGS = [
    "!Ref", "!Sub", "!GetAtt", "!If", "!Select", "!FindInMap",
    "!Base64", "!Join", "!Split", "!Equals", "!Condition", "!Not",
    "!And", "!Or", "!ImportValue", "!GetAZs", "!Cidr",
]

for _tag in CFN_TAGS:
    CfnLoader.add_constructor(
        _tag,
        lambda loader, node, tag=_tag: CfnTag(
            tag,
            loader.construct_scalar(node)
            if isinstance(node, _yaml.ScalarNode)
            else (
                loader.construct_sequence(node)
                if isinstance(node, _yaml.SequenceNode)
                else loader.construct_mapping(node)
            ),
        ),
    )


_TAG_TO_LONGFORM = {"!Ref": "Ref", "!Condition": "Condition"}


def _cfn_tag_to_longform(tag: str) -> str:
    """Convert a YAML tag to its CloudFormation long-form key.

    ``!Sub`` → ``Fn::Sub``, ``!Ref`` → ``Ref``, etc.
    """
    return _TAG_TO_LONGFORM.get(tag, f"Fn::{tag.lstrip('!')}")


def _cfn_tag_representer(dumper: CfnDumper, data: CfnTag):
    if isinstance(data.value, str):
        return dumper.represent_scalar(data.tag, data.value)
    elif isinstance(data.value, list):
        return dumper.represent_sequence(data.tag, data.value)
    elif isinstance(data.value, CfnTag):
        # Nested intrinsic: YAML doesn't allow stacking two tags on one node,
        # so convert the inner CfnTag to its CloudFormation long-form dict.
        # e.g. CfnTag("!Base64", CfnTag("!Sub", "..."))
        #   → !Base64 { Fn::Sub: "..." }
        longform_key = _cfn_tag_to_longform(data.value.tag)
        return dumper.represent_mapping(data.tag, {longform_key: data.value.value})
    elif isinstance(data.value, dict):
        return dumper.represent_mapping(data.tag, data.value)
    else:
        # int, bool, float, or other scalars
        return dumper.represent_scalar(data.tag, str(data.value))


CfnDumper.add_representer(CfnTag, _cfn_tag_representer)


def cfn_load(text: str):
    """Parse CloudFormation YAML preserving intrinsic function tags."""
    return _yaml.load(text, Loader=CfnLoader)


def cfn_dump(data) -> str:
    """Dump CloudFormation-aware data back to YAML."""
    return _yaml.dump(
        data, Dumper=CfnDumper,
        default_flow_style=False, sort_keys=False, allow_unicode=True,
    )
