"""DynamoDB + S3 project store for production."""

import json
import logging
from datetime import UTC, datetime

import boto3
from botocore.exceptions import ClientError

from src.config.settings import settings
from src.models.design import DesignTask
from src.models.docs import DocsTask
from src.models.iac import IaCTask
from src.models.project import Project
from src.storage.protocol import STEP_NEXT_MAP, STEP_STATUS_MAP, VALID_STEPS
from src.utils.validation import validate_safe_id

logger = logging.getLogger(__name__)

# Steps where data is small enough for DynamoDB inline storage
INLINE_STEPS = {"requirements", "design"}
# Steps where data goes to S3 (can exceed 400KB DynamoDB limit)
S3_STEPS = {"iac", "docs"}


class DynamoS3ProjectStore:
    """DynamoDB single-table + S3 for large outputs."""

    def __init__(self):
        self._ddb = boto3.resource("dynamodb", region_name=settings.aws_region)
        self._table = self._ddb.Table(settings.dynamodb_table)
        self._s3 = boto3.client("s3", region_name=settings.aws_region)

    def _pk(self, tenant_id: str) -> str:
        validate_safe_id(tenant_id, "tenant_id")
        return f"TENANT#{tenant_id}"

    def _sk(self, project_id: str) -> str:
        validate_safe_id(project_id, "project_id")
        return f"PROJECT#{project_id}"

    def _s3_key(self, tenant_id: str, project_id: str, step: str) -> str:
        validate_safe_id(tenant_id, "tenant_id")
        validate_safe_id(project_id, "project_id")
        return f"{tenant_id}/{project_id}/state/{step}.json"

    def create_project(self, tenant_id: str, project_id: str, name: str) -> Project:
        project = Project(tenant_id=tenant_id, project_id=project_id, name=name)
        self._table.put_item(
            Item={
                "pk": self._pk(tenant_id),
                "sk": self._sk(project_id),
                "gsi1pk": self._pk(tenant_id),
                "gsi1sk": f"STATUS#{project.status.value}#{project.updated_at}",
                **project.model_dump(),
            }
        )
        return project

    def _item_to_project(self, item: dict) -> Project:
        """Convert a DynamoDB item to a Project model."""
        return Project(
            tenant_id=item["tenant_id"],
            project_id=item["project_id"],
            name=item["name"],
            mode=item.get("mode", "wizard"),
            status=item.get("status", "requirements"),
            current_step=item.get("current_step", "requirements"),
            use_case=item.get("use_case"),
            approved_design_index=item.get("approved_design_index"),
            active_design_task_id=item.get("active_design_task_id"),
            active_iac_task_id=item.get("active_iac_task_id"),
            active_docs_task_id=item.get("active_docs_task_id"),
            created_at=item["created_at"],
            updated_at=item["updated_at"],
        )

    def get_project(self, tenant_id: str, project_id: str) -> Project | None:
        resp = self._table.get_item(
            Key={"pk": self._pk(tenant_id), "sk": self._sk(project_id)}
        )
        item = resp.get("Item")
        return self._item_to_project(item) if item else None

    def list_projects(self, tenant_id: str) -> list[Project]:
        projects = []
        query_params = {
            "IndexName": "GSI1",
            "KeyConditionExpression": "gsi1pk = :pk",
            "ExpressionAttributeValues": {":pk": self._pk(tenant_id)},
            "ScanIndexForward": False,
        }
        while True:
            resp = self._table.query(**query_params)
            projects.extend(self._item_to_project(item) for item in resp.get("Items", []))
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
            query_params["ExclusiveStartKey"] = last_key
        return projects

    def delete_project(self, tenant_id: str, project_id: str) -> None:
        self._table.delete_item(
            Key={"pk": self._pk(tenant_id), "sk": self._sk(project_id)}
        )
        self._delete_s3_state_files(tenant_id, project_id)

    def _delete_s3_state_files(self, tenant_id: str, project_id: str) -> None:
        """Clean up S3 state files using paginated listing and batch deletion."""
        prefix = f"{tenant_id}/{project_id}/state/"
        try:
            paginator = self._s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(
                Bucket=settings.s3_artifacts_bucket, Prefix=prefix
            ):
                objects = page.get("Contents", [])
                if objects:
                    delete_keys = [{"Key": obj["Key"]} for obj in objects]
                    self._s3.delete_objects(
                        Bucket=settings.s3_artifacts_bucket,
                        Delete={"Objects": delete_keys, "Quiet": True},
                    )
        except ClientError:
            logger.warning("Failed to clean up S3 state files", exc_info=True)

    def update_project(self, project: Project) -> None:
        project.updated_at = datetime.now(UTC).isoformat()
        self._table.update_item(
            Key={"pk": self._pk(project.tenant_id), "sk": self._sk(project.project_id)},
            UpdateExpression=(
                "SET #st = :status, current_step = :cs, use_case = :uc, "
                "approved_design_index = :adi, active_design_task_id = :adti, "
                "active_iac_task_id = :aiti, active_docs_task_id = :adoti, "
                "updated_at = :now, gsi1sk = :gsi, #nm = :name"
            ),
            ExpressionAttributeNames={
                "#st": "status",
                "#nm": "name",
            },
            ExpressionAttributeValues={
                ":status": project.status.value,
                ":cs": project.current_step,
                ":uc": project.use_case,
                ":adi": project.approved_design_index,
                ":adti": project.active_design_task_id,
                ":aiti": project.active_iac_task_id,
                ":adoti": project.active_docs_task_id,
                ":now": project.updated_at,
                ":gsi": f"STATUS#{project.status.value}#{project.updated_at}",
                ":name": project.name,
            },
        )

    def save_step(self, tenant_id: str, project_id: str, step: str, data: dict, *, advance: bool = True) -> None:
        if step not in VALID_STEPS:
            raise ValueError(f"Invalid step: {step}")

        if step in INLINE_STEPS:
            self._save_inline_step(tenant_id, project_id, step, data, advance=advance)
        else:
            self._save_s3_step(tenant_id, project_id, step, data, advance=advance)

    def _save_inline_step(
        self, tenant_id: str, project_id: str, step: str, data: dict, *, advance: bool = True,
    ) -> None:
        """Save small steps (requirements, design) inline in DynamoDB."""
        pk = self._pk(tenant_id)
        sk = self._sk(project_id)
        now = datetime.now(UTC).isoformat()

        if advance:
            new_status = STEP_STATUS_MAP[step]
            new_step = STEP_NEXT_MAP[step]
            self._table.update_item(
                Key={"pk": pk, "sk": sk},
                UpdateExpression=(
                    "SET #step_data = :data, #st = :status, current_step = :cs, "
                    "gsi1sk = :gsi, updated_at = :now"
                ),
                ExpressionAttributeNames={
                    "#step_data": f"{step}_json",
                    "#st": "status",
                },
                ExpressionAttributeValues={
                    ":data": json.dumps(data),
                    ":status": new_status.value,
                    ":cs": new_step,
                    ":gsi": f"STATUS#{new_status.value}#{now}",
                    ":now": now,
                },
            )
        else:
            self._table.update_item(
                Key={"pk": pk, "sk": sk},
                UpdateExpression="SET #step_data = :data, updated_at = :now",
                ExpressionAttributeNames={"#step_data": f"{step}_json"},
                ExpressionAttributeValues={
                    ":data": json.dumps(data),
                    ":now": now,
                },
            )

    def _save_s3_step(
        self, tenant_id: str, project_id: str, step: str, data: dict, *, advance: bool = True,
    ) -> None:
        """Save large steps (iac, docs) to S3 with pointer in DynamoDB."""
        pk = self._pk(tenant_id)
        sk = self._sk(project_id)
        now = datetime.now(UTC).isoformat()
        s3_key = self._s3_key(tenant_id, project_id, step)

        self._s3.put_object(
            Bucket=settings.s3_artifacts_bucket,
            Key=s3_key,
            Body=json.dumps(data).encode("utf-8"),
            ContentType="application/json",
            ServerSideEncryption="aws:kms",
        )

        try:
            if advance:
                new_status = STEP_STATUS_MAP[step]
                new_step = STEP_NEXT_MAP[step]
                self._table.update_item(
                    Key={"pk": pk, "sk": sk},
                    UpdateExpression=(
                        "SET #s3_key = :key, #st = :status, current_step = :cs, "
                        "gsi1sk = :gsi, updated_at = :now"
                    ),
                    ExpressionAttributeNames={
                        "#s3_key": f"{step}_s3_key",
                        "#st": "status",
                    },
                    ExpressionAttributeValues={
                        ":key": s3_key,
                        ":status": new_status.value,
                        ":cs": new_step,
                        ":gsi": f"STATUS#{new_status.value}#{now}",
                        ":now": now,
                    },
                )
            else:
                self._table.update_item(
                    Key={"pk": pk, "sk": sk},
                    UpdateExpression="SET #s3_key = :key, updated_at = :now",
                    ExpressionAttributeNames={"#s3_key": f"{step}_s3_key"},
                    ExpressionAttributeValues={":key": s3_key, ":now": now},
                )
        except Exception:
            logger.error(
                "DynamoDB update failed after S3 write — deleting orphaned object %s",
                s3_key,
                exc_info=True,
            )
            try:
                self._s3.delete_object(
                    Bucket=settings.s3_artifacts_bucket, Key=s3_key
                )
            except ClientError:
                logger.warning("Failed to clean up orphaned S3 object %s", s3_key, exc_info=True)
            raise

    def load_step(self, tenant_id: str, project_id: str, step: str) -> dict | None:
        if step not in VALID_STEPS:
            return None
        return (
            self._load_inline_step(tenant_id, project_id, step)
            if step in INLINE_STEPS
            else self._load_s3_step(tenant_id, project_id, step)
        )

    def _load_inline_step(self, tenant_id: str, project_id: str, step: str) -> dict | None:
        """Load small steps from DynamoDB inline storage."""
        resp = self._table.get_item(
            Key={"pk": self._pk(tenant_id), "sk": self._sk(project_id)},
            ProjectionExpression=f"{step}_json",
        )
        item = resp.get("Item", {})
        raw = item.get(f"{step}_json")
        return json.loads(raw) if raw else None

    def _load_s3_step(self, tenant_id: str, project_id: str, step: str) -> dict | None:
        """Load large steps from S3 via pointer."""
        resp = self._table.get_item(
            Key={"pk": self._pk(tenant_id), "sk": self._sk(project_id)},
            ProjectionExpression=f"{step}_s3_key",
        )
        item = resp.get("Item", {})
        s3_key = item.get(f"{step}_s3_key")
        if not s3_key:
            return None
        try:
            obj = self._s3.get_object(
                Bucket=settings.s3_artifacts_bucket, Key=s3_key
            )
            return json.loads(obj["Body"].read().decode("utf-8"))
        except ClientError:
            logger.warning("Failed to load step %s from S3", step, exc_info=True)
            return None

    # --- Design task methods ---

    def _task_sk(self, task_id: str) -> str:
        validate_safe_id(task_id, "task_id")
        return f"TASK#{task_id}"

    def create_task(self, tenant_id: str, task: DesignTask) -> None:
        self._table.put_item(
            Item={
                "pk": self._pk(tenant_id),
                "sk": self._task_sk(task.task_id),
                "gsi1pk": self._pk(tenant_id),
                "gsi1sk": f"TASK#{task.status.value}#{task.submitted_at}",
                **json.loads(task.model_dump_json()),
            }
        )

    def get_task(self, tenant_id: str, task_id: str) -> DesignTask | None:
        resp = self._table.get_item(
            Key={"pk": self._pk(tenant_id), "sk": self._task_sk(task_id)}
        )
        item = resp.get("Item")
        if not item:
            return None
        return DesignTask.model_validate(item)

    def _update_task_generic(
        self, tenant_id: str, task_id: str, updates: dict, sk: str, gsi_prefix: str
    ) -> None:
        pk = self._pk(tenant_id)

        expr_parts: list[str] = []
        attr_names: dict[str, str] = {}
        attr_values: dict[str, str] = {}

        for i, (key, value) in enumerate(updates.items()):
            placeholder_name = f"#k{i}"
            placeholder_value = f":v{i}"
            attr_names[placeholder_name] = key
            attr_values[placeholder_value] = value
            expr_parts.append(f"{placeholder_name} = {placeholder_value}")

        if "status" in updates:
            now = datetime.now(UTC).isoformat()
            attr_names["#gsi1sk"] = "gsi1sk"
            attr_values[":gsi1sk"] = f"{gsi_prefix}{updates['status']}#{now}"
            expr_parts.append("#gsi1sk = :gsi1sk")

        self._table.update_item(
            Key={"pk": pk, "sk": sk},
            UpdateExpression="SET " + ", ".join(expr_parts),
            ExpressionAttributeNames=attr_names,
            ExpressionAttributeValues=attr_values,
        )

    def update_task(self, tenant_id: str, task_id: str, updates: dict) -> None:
        self._update_task_generic(
            tenant_id, task_id, updates, self._task_sk(task_id), "TASK#"
        )

    # --- IaC task methods ---

    def _iac_task_sk(self, task_id: str) -> str:
        validate_safe_id(task_id, "task_id")
        return f"IAC_TASK#{task_id}"

    def create_iac_task(self, tenant_id: str, task: IaCTask) -> None:
        """Create IaC task in DynamoDB. PK: TENANT#{tenant_id}, SK: IAC_TASK#{task_id}."""
        self._table.put_item(
            Item={
                "pk": self._pk(tenant_id),
                "sk": self._iac_task_sk(task.task_id),
                "gsi1pk": f"PROJECT#{tenant_id}#{task.project_id}",
                "gsi1sk": f"IAC_TASK#{task.submitted_at}",
                **json.loads(task.model_dump_json()),
            }
        )

    def get_iac_task(self, tenant_id: str, task_id: str) -> IaCTask | None:
        """Retrieve IaC task from DynamoDB."""
        resp = self._table.get_item(
            Key={"pk": self._pk(tenant_id), "sk": self._iac_task_sk(task_id)}
        )
        item = resp.get("Item")
        if not item:
            return None
        return IaCTask.model_validate(item)

    def update_iac_task(self, tenant_id: str, task_id: str, updates: dict) -> None:
        """Update IaC task status/result in DynamoDB."""
        self._update_task_generic(
            tenant_id, task_id, updates, self._iac_task_sk(task_id), "IAC_TASK#"
        )

    # --- Docs task methods ---

    def _docs_task_sk(self, task_id: str) -> str:
        validate_safe_id(task_id, "task_id")
        return f"DOCS_TASK#{task_id}"

    def create_docs_task(self, tenant_id: str, task: DocsTask) -> None:
        """Create docs task in DynamoDB. PK: TENANT#{tenant_id}, SK: DOCS_TASK#{task_id}."""
        self._table.put_item(
            Item={
                "pk": self._pk(tenant_id),
                "sk": self._docs_task_sk(task.task_id),
                "gsi1pk": f"PROJECT#{tenant_id}#{task.project_id}",
                "gsi1sk": f"DOCS_TASK#{task.submitted_at}",
                **json.loads(task.model_dump_json()),
            }
        )

    def get_docs_task(self, tenant_id: str, task_id: str) -> DocsTask | None:
        """Retrieve docs task from DynamoDB."""
        resp = self._table.get_item(
            Key={"pk": self._pk(tenant_id), "sk": self._docs_task_sk(task_id)}
        )
        item = resp.get("Item")
        if not item:
            return None
        return DocsTask.model_validate(item)

    def update_docs_task(self, tenant_id: str, task_id: str, updates: dict) -> None:
        """Update docs task status/result in DynamoDB."""
        self._update_task_generic(
            tenant_id, task_id, updates, self._docs_task_sk(task_id), "DOCS_TASK#"
        )
