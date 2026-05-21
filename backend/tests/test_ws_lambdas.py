"""Unit tests for WebSocket Lambda handlers.

Covers: ws_connect, ws_disconnect, ws_subscribe, ws_notification_bridge,
ws_heartbeat, ws_authorizer.
"""

import json
import os
import time
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ENV_BASE = {
    "DYNAMODB_TABLE": "test-table",
    "WEBSOCKET_CALLBACK_URL": "https://execute-api.test/prod",
    "AWS_DEFAULT_REGION": "us-east-1",
}


def _ws_event(connection_id="conn-1", body=None, authorizer=None, query_params=None):
    """Build a minimal API Gateway WebSocket event."""
    rc = {"connectionId": connection_id}
    if authorizer is not None:
        rc["authorizer"] = authorizer
    event = {"requestContext": rc}
    if body is not None:
        event["body"] = json.dumps(body) if isinstance(body, dict) else body
    if query_params is not None:
        event["queryStringParameters"] = query_params
    return event


# ===================================================================
# ws_connect
# ===================================================================

class TestWsConnect:
    """Tests for lambdas/ws/ws_connect.handler."""

    @patch.dict(os.environ, ENV_BASE)
    @patch("boto3.resource")
    def test_basic_connect_stores_item(self, mock_resource):
        """put_item is called with correct pk, sk, and a TTL ~2 h in the future."""
        mock_table = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table

        from lambdas.ws.ws_connect import handler

        event = _ws_event("abc-123")
        result = handler(event, {})

        assert result == {"statusCode": 200}
        mock_table.put_item.assert_called_once()

        item = mock_table.put_item.call_args[1]["Item"]
        assert item["pk"] == "WS#abc-123"
        assert item["sk"] == "CONNECTION"
        assert item["connection_id"] == "abc-123"
        # TTL should be approximately 2 hours from now (within 60 s tolerance)
        assert abs(item["ttl"] - (time.time() + 7200)) < 60
        # No auth_tenant_id when authorizer is absent
        assert "auth_tenant_id" not in item

    @patch.dict(os.environ, ENV_BASE)
    @patch("boto3.resource")
    def test_auth_tenant_id_stored(self, mock_resource):
        """When authorizer context contains tenant_id it is persisted."""
        mock_table = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table

        from lambdas.ws.ws_connect import handler

        event = _ws_event("abc-123", authorizer={"tenant_id": "acme-corp"})
        handler(event, {})

        item = mock_table.put_item.call_args[1]["Item"]
        assert item["auth_tenant_id"] == "acme-corp"

    @patch.dict(os.environ, ENV_BASE)
    @patch("boto3.resource")
    def test_custom_tenant_id_stored(self, mock_resource):
        """custom:tenant_id takes precedence over tenant_id."""
        mock_table = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table

        from lambdas.ws.ws_connect import handler

        event = _ws_event(
            "abc-123",
            authorizer={"custom:tenant_id": "cognito-tenant", "tenant_id": "fallback"},
        )
        handler(event, {})

        item = mock_table.put_item.call_args[1]["Item"]
        assert item["auth_tenant_id"] == "cognito-tenant"

    @patch.dict(os.environ, ENV_BASE)
    @patch("boto3.resource")
    def test_missing_authorizer_context(self, mock_resource):
        """Handler succeeds even when requestContext has no authorizer key."""
        mock_table = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table

        from lambdas.ws.ws_connect import handler

        # Event with no 'authorizer' in requestContext at all
        event = {"requestContext": {"connectionId": "conn-no-auth"}}
        result = handler(event, {})

        assert result == {"statusCode": 200}
        item = mock_table.put_item.call_args[1]["Item"]
        assert "auth_tenant_id" not in item


# ===================================================================
# ws_disconnect
# ===================================================================

class TestWsDisconnect:
    """Tests for lambdas/ws/ws_disconnect.handler."""

    @patch.dict(os.environ, ENV_BASE)
    @patch("boto3.resource")
    def test_removes_connection_and_subscriptions(self, mock_resource):
        """All items with pk=WS#<id> are batch-deleted."""
        mock_table = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table

        mock_table.query.return_value = {
            "Items": [
                {"pk": "WS#conn-1", "sk": "CONNECTION"},
                {"pk": "WS#conn-1", "sk": "SUB#default#proj-1"},
            ],
        }

        from lambdas.ws.ws_disconnect import handler

        result = handler(_ws_event("conn-1"), {})

        assert result == {"statusCode": 200}
        mock_table.query.assert_called_once()
        # batch_writer context manager should have been used
        mock_table.batch_writer.assert_called_once()
        batch = mock_table.batch_writer().__enter__()
        assert batch.delete_item.call_count == 2
        batch.delete_item.assert_any_call(Key={"pk": "WS#conn-1", "sk": "CONNECTION"})
        batch.delete_item.assert_any_call(
            Key={"pk": "WS#conn-1", "sk": "SUB#default#proj-1"},
        )

    @patch.dict(os.environ, ENV_BASE)
    @patch("boto3.resource")
    def test_no_items_found(self, mock_resource):
        """Gracefully handles case when connection has no items in DDB."""
        mock_table = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table
        mock_table.query.return_value = {"Items": []}

        from lambdas.ws.ws_disconnect import handler

        result = handler(_ws_event("conn-ghost"), {})
        assert result == {"statusCode": 200}
        mock_table.batch_writer.assert_not_called()

    @patch.dict(os.environ, ENV_BASE)
    @patch("boto3.resource")
    def test_pagination(self, mock_resource):
        """Handles paginated query results."""
        mock_table = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table
        mock_table.query.side_effect = [
            {
                "Items": [{"pk": "WS#conn-1", "sk": "CONNECTION"}],
                "LastEvaluatedKey": {"pk": "WS#conn-1", "sk": "CONNECTION"},
            },
            {
                "Items": [{"pk": "WS#conn-1", "sk": "SUB#t#p"}],
            },
        ]

        from lambdas.ws.ws_disconnect import handler

        result = handler(_ws_event("conn-1"), {})
        assert result == {"statusCode": 200}
        assert mock_table.query.call_count == 2


# ===================================================================
# ws_subscribe
# ===================================================================

class TestWsSubscribe:
    """Tests for lambdas/ws/ws_subscribe.handler."""

    @patch.dict(os.environ, ENV_BASE)
    @patch("boto3.resource")
    def test_creates_subscription_item(self, mock_resource):
        """Subscription item has correct pk/sk/gsi keys."""
        mock_table = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table
        # No auth_tenant_id on connection record
        mock_table.get_item.return_value = {"Item": {}}

        from lambdas.ws.ws_subscribe import handler

        event = _ws_event(
            "conn-1",
            body={"action": "subscribe", "project_id": "proj-42", "tenant_id": "acme"},
        )
        result = handler(event, {})

        assert result == {"statusCode": 200}
        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["pk"] == "WS#conn-1"
        assert item["sk"] == "SUB#acme#proj-42"
        assert item["gsi2pk"] == "SUB#acme#proj-42"
        assert item["gsi2sk"] == "WS#conn-1"
        assert item["project_id"] == "proj-42"
        assert item["tenant_id"] == "acme"

    @patch.dict(os.environ, ENV_BASE)
    @patch("boto3.resource")
    def test_tenant_id_mismatch_returns_403(self, mock_resource):
        """Subscribe is rejected when tenant_id does not match auth_tenant_id."""
        mock_table = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table
        mock_table.get_item.return_value = {
            "Item": {"auth_tenant_id": "real-tenant"},
        }

        from lambdas.ws.ws_subscribe import handler

        event = _ws_event(
            "conn-1",
            body={"action": "subscribe", "project_id": "proj-1", "tenant_id": "evil"},
        )
        result = handler(event, {})

        assert result["statusCode"] == 403
        assert "mismatch" in json.loads(result["body"])["error"]
        mock_table.put_item.assert_not_called()

    @patch.dict(os.environ, ENV_BASE)
    @patch("boto3.resource")
    def test_missing_project_id_returns_400(self, mock_resource):
        """Subscribe fails with 400 when project_id is absent."""
        mock_table = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table

        from lambdas.ws.ws_subscribe import handler

        event = _ws_event("conn-1", body={"action": "subscribe"})
        result = handler(event, {})

        assert result["statusCode"] == 400
        assert "project_id" in json.loads(result["body"])["error"]

    @patch.dict(os.environ, ENV_BASE)
    @patch("boto3.resource")
    def test_invalid_tenant_id_returns_400(self, mock_resource):
        """Subscribe fails with 400 for injection-like tenant_id values."""
        mock_table = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table

        from lambdas.ws.ws_subscribe import handler

        event = _ws_event(
            "conn-1",
            body={"action": "subscribe", "project_id": "proj-1", "tenant_id": "../bad"},
        )
        result = handler(event, {})
        assert result["statusCode"] == 400

    @patch.dict(os.environ, ENV_BASE)
    @patch("boto3.resource")
    def test_default_tenant_id(self, mock_resource):
        """When tenant_id is omitted, 'default' is used."""
        mock_table = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table
        mock_table.get_item.return_value = {"Item": {}}

        from lambdas.ws.ws_subscribe import handler

        event = _ws_event(
            "conn-1",
            body={"action": "subscribe", "project_id": "proj-1"},
        )
        result = handler(event, {})

        assert result == {"statusCode": 200}
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["tenant_id"] == "default"


# ===================================================================
# ws_notification_bridge
# ===================================================================

class TestWsNotificationBridge:
    """Tests for lambdas/ws/ws_notification_bridge.handler."""

    def _stream_record(self, *, sk_prefix="TASK#", new_status="completed",
                       old_status="running", tenant_id="acme",
                       project_id="proj-1", task_id="task-1"):
        """Build a single DynamoDB stream record."""
        return {
            "dynamodb": {
                "Keys": {
                    "pk": {"S": f"TENANT#{tenant_id}#PROJECT#{project_id}"},
                    "sk": {"S": f"{sk_prefix}{task_id}"},
                },
                "NewImage": {
                    "status": {"S": new_status},
                    "tenant_id": {"S": tenant_id},
                    "project_id": {"S": project_id},
                    "task_id": {"S": task_id},
                },
                "OldImage": {
                    "status": {"S": old_status},
                },
            }
        }

    @patch.dict(os.environ, ENV_BASE)
    def test_routes_task_completion(self):
        """TASK# prefix records generate design_complete messages."""
        import lambdas.ws.ws_notification_bridge as bridge

        mock_table = MagicMock()
        mock_apigw = MagicMock()

        bridge._table = mock_table
        bridge._apigw = mock_apigw

        mock_table.query.return_value = {
            "Items": [{"connection_id": "conn-1"}],
        }
        mock_table.get_item.return_value = {"Item": {"result": {"some": "data"}}}

        records = [self._stream_record(sk_prefix="TASK#")]
        bridge.handler(records, {})

        mock_apigw.post_to_connection.assert_called_once()
        posted = json.loads(
            mock_apigw.post_to_connection.call_args[1]["Data"].decode("utf-8"),
        )
        assert posted["type"] == "design_complete"
        assert posted["task_id"] == "task-1"
        assert posted["status"] == "completed"
        assert posted["result"] == {"some": "data"}

    @patch.dict(os.environ, ENV_BASE)
    def test_routes_iac_task_completion(self):
        """IAC_TASK# prefix records generate iac_complete messages."""
        import lambdas.ws.ws_notification_bridge as bridge

        mock_table = MagicMock()
        mock_apigw = MagicMock()

        bridge._table = mock_table
        bridge._apigw = mock_apigw

        mock_table.query.return_value = {
            "Items": [{"connection_id": "conn-2"}],
        }
        mock_table.get_item.return_value = {"Item": {}}

        records = [self._stream_record(sk_prefix="IAC_TASK#")]
        bridge.handler(records, {})

        posted = json.loads(
            mock_apigw.post_to_connection.call_args[1]["Data"].decode("utf-8"),
        )
        assert posted["type"] == "iac_complete"

    @patch.dict(os.environ, ENV_BASE)
    def test_routes_docs_task_completion(self):
        """DOCS_TASK# prefix records generate docs_complete messages."""
        import lambdas.ws.ws_notification_bridge as bridge

        mock_table = MagicMock()
        mock_apigw = MagicMock()

        bridge._table = mock_table
        bridge._apigw = mock_apigw

        mock_table.query.return_value = {
            "Items": [{"connection_id": "conn-3"}],
        }
        mock_table.get_item.return_value = {"Item": {}}

        records = [self._stream_record(sk_prefix="DOCS_TASK#")]
        bridge.handler(records, {})

        posted = json.loads(
            mock_apigw.post_to_connection.call_args[1]["Data"].decode("utf-8"),
        )
        assert posted["type"] == "docs_complete"

    @patch.dict(os.environ, ENV_BASE)
    def test_handles_gone_exception(self):
        """GoneException triggers cleanup of stale connection."""
        import lambdas.ws.ws_notification_bridge as bridge

        mock_table = MagicMock()
        mock_apigw = MagicMock()

        bridge._table = mock_table
        bridge._apigw = mock_apigw

        # Configure GoneException
        gone_exc = type("GoneException", (Exception,), {})
        mock_apigw.exceptions.GoneException = gone_exc
        mock_apigw.post_to_connection.side_effect = gone_exc()

        mock_table.query.side_effect = [
            # First call: GSI2 query returns subscribed connections
            {"Items": [{"connection_id": "stale-conn"}]},
            # Second call: cleanup query for the stale connection
            {"Items": [
                {"pk": "WS#stale-conn", "sk": "CONNECTION"},
                {"pk": "WS#stale-conn", "sk": "SUB#acme#proj-1"},
            ]},
        ]
        mock_table.get_item.return_value = {"Item": {"result": {}}}

        records = [self._stream_record()]
        bridge.handler(records, {})

        # batch_writer should have been called to delete stale items
        mock_table.batch_writer.assert_called()

    @patch.dict(os.environ, ENV_BASE)
    def test_skips_unchanged_status(self):
        """Records where new_status == old_status are skipped."""
        import lambdas.ws.ws_notification_bridge as bridge

        mock_table = MagicMock()
        mock_apigw = MagicMock()

        bridge._table = mock_table
        bridge._apigw = mock_apigw

        records = [self._stream_record(new_status="running", old_status="running")]
        bridge.handler(records, {})

        mock_apigw.post_to_connection.assert_not_called()

    @patch.dict(os.environ, ENV_BASE)
    def test_failed_status_includes_error(self):
        """Failed task records include error message in the payload."""
        import lambdas.ws.ws_notification_bridge as bridge

        mock_table = MagicMock()
        mock_apigw = MagicMock()

        bridge._table = mock_table
        bridge._apigw = mock_apigw

        mock_table.query.return_value = {
            "Items": [{"connection_id": "conn-1"}],
        }

        record = self._stream_record(new_status="failed")
        record["dynamodb"]["NewImage"]["error_message"] = {"S": "something broke"}
        bridge.handler([record], {})

        posted = json.loads(
            mock_apigw.post_to_connection.call_args[1]["Data"].decode("utf-8"),
        )
        assert posted["type"] == "design_failed"
        assert posted["error"] == "something broke"

    @patch.dict(os.environ, {**ENV_BASE, "WEBSOCKET_CALLBACK_URL": ""})
    def test_skips_when_no_callback_url(self):
        """Handler exits early when WEBSOCKET_CALLBACK_URL is not set."""
        import lambdas.ws.ws_notification_bridge as bridge

        bridge._apigw = None  # Reset so lazy-init runs

        result = bridge.handler([], {})
        # Should return None (no crash)
        assert result is None


# ===================================================================
# ws_heartbeat
# ===================================================================

class TestWsHeartbeat:
    """Tests for lambdas/ws/ws_heartbeat.handler."""

    @patch.dict(os.environ, ENV_BASE)
    def test_pings_active_connections(self):
        """Active connections receive heartbeat messages."""
        import lambdas.ws.ws_heartbeat as heartbeat

        mock_table = MagicMock()
        mock_apigw = MagicMock()
        mock_cw = MagicMock()

        heartbeat._table = mock_table
        heartbeat._apigw = mock_apigw
        heartbeat._cw = mock_cw

        mock_table.scan.return_value = {
            "Items": [
                {"pk": "WS#conn-1", "connection_id": "conn-1"},
                {"pk": "WS#conn-2", "connection_id": "conn-2"},
            ],
        }

        heartbeat.handler({}, {})

        assert mock_apigw.post_to_connection.call_count == 2
        mock_apigw.post_to_connection.assert_any_call(
            ConnectionId="conn-1",
            Data=b'{"type":"heartbeat"}',
        )
        mock_apigw.post_to_connection.assert_any_call(
            ConnectionId="conn-2",
            Data=b'{"type":"heartbeat"}',
        )

    @patch.dict(os.environ, ENV_BASE)
    def test_cleans_stale_connections(self):
        """Stale (GoneException) connections are removed from DynamoDB."""
        import lambdas.ws.ws_heartbeat as heartbeat

        mock_table = MagicMock()
        mock_apigw = MagicMock()
        mock_cw = MagicMock()

        heartbeat._table = mock_table
        heartbeat._apigw = mock_apigw
        heartbeat._cw = mock_cw

        gone_exc = type("GoneException", (Exception,), {})
        mock_apigw.exceptions.GoneException = gone_exc
        mock_apigw.post_to_connection.side_effect = gone_exc()

        mock_table.scan.return_value = {
            "Items": [{"pk": "WS#stale", "connection_id": "stale"}],
        }
        # cleanup query
        mock_table.query.return_value = {
            "Items": [
                {"pk": "WS#stale", "sk": "CONNECTION"},
                {"pk": "WS#stale", "sk": "SUB#t#p"},
            ],
        }

        heartbeat.handler({}, {})

        mock_table.batch_writer.assert_called()

    @patch.dict(os.environ, ENV_BASE)
    def test_publishes_cloudwatch_metrics(self):
        """Metrics are published to the AI-Deploy namespace."""
        import lambdas.ws.ws_heartbeat as heartbeat

        mock_table = MagicMock()
        mock_apigw = MagicMock()
        mock_cw = MagicMock()

        heartbeat._table = mock_table
        heartbeat._apigw = mock_apigw
        heartbeat._cw = mock_cw

        gone_exc = type("GoneException", (Exception,), {})
        mock_apigw.exceptions.GoneException = gone_exc

        # 1 active, 1 stale
        mock_apigw.post_to_connection.side_effect = [None, gone_exc()]

        mock_table.scan.return_value = {
            "Items": [
                {"pk": "WS#alive", "connection_id": "alive"},
                {"pk": "WS#dead", "connection_id": "dead"},
            ],
        }
        mock_table.query.return_value = {
            "Items": [{"pk": "WS#dead", "sk": "CONNECTION"}],
        }

        heartbeat.handler({}, {})

        mock_cw.put_metric_data.assert_called_once()
        cw_call = mock_cw.put_metric_data.call_args[1]
        assert cw_call["Namespace"] == "AI-Deploy"
        metrics = {m["MetricName"]: m["Value"] for m in cw_call["MetricData"]}
        assert metrics["WsActiveConnections"] == 1
        assert metrics["WsStaleConnectionsCleaned"] == 1

    @patch.dict(os.environ, {**ENV_BASE, "WEBSOCKET_CALLBACK_URL": ""})
    def test_skips_when_no_callback_url(self):
        """Handler exits early when WEBSOCKET_CALLBACK_URL is not set."""
        import lambdas.ws.ws_heartbeat as heartbeat

        heartbeat._apigw = None  # Reset lazy-init

        result = heartbeat.handler({}, {})
        assert result is None

    @patch.dict(os.environ, ENV_BASE)
    def test_scan_pagination(self):
        """Handler paginates through DynamoDB scan results."""
        import lambdas.ws.ws_heartbeat as heartbeat

        mock_table = MagicMock()
        mock_apigw = MagicMock()
        mock_cw = MagicMock()

        heartbeat._table = mock_table
        heartbeat._apigw = mock_apigw
        heartbeat._cw = mock_cw

        mock_table.scan.side_effect = [
            {
                "Items": [{"pk": "WS#conn-1", "connection_id": "conn-1"}],
                "LastEvaluatedKey": {"pk": "WS#conn-1"},
            },
            {
                "Items": [{"pk": "WS#conn-2", "connection_id": "conn-2"}],
            },
        ]

        heartbeat.handler({}, {})

        assert mock_table.scan.call_count == 2
        assert mock_apigw.post_to_connection.call_count == 2


# ===================================================================
# ws_authorizer
# ===================================================================

class TestWsAuthorizer:
    """Tests for lambdas/ws/ws_authorizer.handler."""

    @patch.dict(os.environ, {"COGNITO_USER_POOL_ID": "", "COGNITO_CLIENT_ID": ""})
    def test_allows_all_when_no_cognito(self):
        """Local dev mode: allow all connections with default tenant."""
        from lambdas.ws.ws_authorizer import handler

        event = {"methodArn": "arn:aws:execute-api:us-east-1:123:abc/$connect"}
        result = handler(event, {})

        assert result["principalId"] == "local-dev"
        stmt = result["policyDocument"]["Statement"][0]
        assert stmt["Effect"] == "Allow"
        assert result["context"]["tenant_id"] == "default"

    @patch.dict(os.environ, {
        "COGNITO_USER_POOL_ID": "us-east-1_ABCdef123",
        "COGNITO_CLIENT_ID": "client-id-123",
    })
    def test_denies_when_no_token(self):
        """Cognito configured but no token in query string => Deny."""
        from lambdas.ws.ws_authorizer import handler

        event = {
            "methodArn": "arn:aws:execute-api:us-east-1:123:abc/$connect",
            "queryStringParameters": {},
        }
        result = handler(event, {})

        assert result["principalId"] == "anonymous"
        stmt = result["policyDocument"]["Statement"][0]
        assert stmt["Effect"] == "Deny"

    @patch.dict(os.environ, {
        "COGNITO_USER_POOL_ID": "us-east-1_ABCdef123",
        "COGNITO_CLIENT_ID": "client-id-123",
    })
    def test_denies_when_no_query_params(self):
        """Cognito configured, queryStringParameters is None => Deny."""
        from lambdas.ws.ws_authorizer import handler

        event = {
            "methodArn": "arn:aws:execute-api:us-east-1:123:abc/$connect",
            "queryStringParameters": None,
        }
        result = handler(event, {})

        stmt = result["policyDocument"]["Statement"][0]
        assert stmt["Effect"] == "Deny"

    @patch.dict(os.environ, {"COGNITO_USER_POOL_ID": "", "COGNITO_CLIENT_ID": ""})
    def test_policy_document_structure(self):
        """Returned policy document follows IAM policy schema."""
        from lambdas.ws.ws_authorizer import handler

        arn = "arn:aws:execute-api:us-east-1:123:abc/$connect"
        event = {"methodArn": arn}
        result = handler(event, {})

        assert "principalId" in result
        pd = result["policyDocument"]
        assert pd["Version"] == "2012-10-17"
        assert len(pd["Statement"]) == 1
        stmt = pd["Statement"][0]
        assert stmt["Action"] == "execute-api:Invoke"
        assert stmt["Resource"] == arn
        assert stmt["Effect"] in ("Allow", "Deny")

    @patch.dict(os.environ, {"COGNITO_USER_POOL_ID": "", "COGNITO_CLIENT_ID": ""})
    def test_uses_route_arn_fallback(self):
        """When methodArn is absent, falls back to routeArn."""
        from lambdas.ws.ws_authorizer import handler

        event = {"routeArn": "arn:aws:execute-api:us-east-1:123:abc/$connect"}
        result = handler(event, {})

        stmt = result["policyDocument"]["Statement"][0]
        assert stmt["Resource"] == "arn:aws:execute-api:us-east-1:123:abc/$connect"

    @patch.dict(os.environ, {
        "COGNITO_USER_POOL_ID": "us-east-1_ABCdef123",
        "COGNITO_CLIENT_ID": "client-id-123",
    })
    def test_denies_invalid_jwt(self):
        """A malformed token results in Deny."""
        from lambdas.ws.ws_authorizer import handler

        event = {
            "methodArn": "arn:aws:execute-api:us-east-1:123:abc/$connect",
            "queryStringParameters": {"token": "not.a.valid.jwt"},
        }
        result = handler(event, {})

        stmt = result["policyDocument"]["Statement"][0]
        assert stmt["Effect"] == "Deny"
