"""CloudWatch logging integration for distributed nodes."""

import logging
import os
import sys
import threading
import time
from typing import Any, Optional

import boto3


class CloudWatchLogger:
    """Handles CloudWatch Logs integration for structured log forwarding."""

    def __init__(
        self,
        node_id: int,
        log_group: str = "Distributed_System_Logs",
        region: Optional[str] = None,
        enabled: bool = True
    ) -> None:
        """Initialize CloudWatch logger.

        Args:
            node_id: Unique identifier for this node (used in stream name)
            log_group: CloudWatch log group name
            region: AWS region (defaults to AWS_REGION env var or us-east-1)
            enabled: Whether CloudWatch logging should be active
        """
        self.node_id = node_id
        self.log_group = log_group
        self.log_stream = f"Node_{node_id}"
        self.enabled = enabled

        self.cw_client = None
        self.sequence_token: Optional[str] = None

        # Setup console logger for local output
        self.logger = self._setup_console_logger()

        # Initialize CloudWatch client if enabled
        if self.enabled:
            self._initialize_cloudwatch(region)

    def _setup_console_logger(self) -> logging.Logger:
        """Configure JSON logging to stdout."""
        logger = logging.getLogger(f"Node-{self.node_id}")
        logger.setLevel(logging.INFO)
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(handler)
        return logger

    def _initialize_cloudwatch(self, region: Optional[str]) -> None:
        """Initialize CloudWatch client and create log group/stream if needed."""
        try:
            aws_region = region or os.environ.get('AWS_REGION', 'us-east-1')
            self.cw_client = boto3.client('logs', region_name=aws_region)

            # Create log group if it doesn't exist
            try:
                self.cw_client.create_log_group(logGroupName=self.log_group)
            except self.cw_client.exceptions.ResourceAlreadyExistsException:
                pass

            # Create log stream if it doesn't exist
            try:
                self.cw_client.create_log_stream(
                    logGroupName=self.log_group,
                    logStreamName=self.log_stream
                )
            except self.cw_client.exceptions.ResourceAlreadyExistsException:
                pass

            self.logger.info(
                f"CloudWatch logging enabled for Node {self.node_id} in region {aws_region}"
            )
        except Exception as e:
            self.logger.warning(f"Failed to initialize CloudWatch: {e}")
            self.cw_client = None
            self.enabled = False

    def log(self, json_log: str) -> None:
        """Log a JSON message to console and optionally CloudWatch.

        Args:
            json_log: JSON-formatted log message string
        """
        # Always log to console
        self.logger.info(json_log)

        # Forward to CloudWatch in background if enabled
        if self.enabled and self.cw_client:
            threading.Thread(
                target=self._send_to_cloudwatch,
                args=(json_log,),
                daemon=True
            ).start()

    def _send_to_cloudwatch(self, json_log: str) -> None:
        """Push a single log entry to CloudWatch, handling sequence tokens.

        Args:
            json_log: JSON-formatted log message string
        """
        if not self.cw_client:
            return

        try:
            log_kwargs: dict[str, Any] = {
                "logGroupName": self.log_group,
                "logStreamName": self.log_stream,
                "logEvents": [
                    {"timestamp": int(time.time() * 1000), "message": json_log}
                ]
            }

            if self.sequence_token:
                log_kwargs["sequenceToken"] = self.sequence_token

            response = self.cw_client.put_log_events(**log_kwargs)
            self.sequence_token = response.get("nextSequenceToken")

        except self.cw_client.exceptions.InvalidSequenceTokenException as e:
            # Extract the correct token from error message and retry
            msg = e.response.get("Error", {}).get("Message", "")
            token = msg.split()[-1] if msg else None

            if token:
                self.sequence_token = token
                try:
                    response = self.cw_client.put_log_events(
                        logGroupName=self.log_group,
                        logStreamName=self.log_stream,
                        logEvents=[
                            {"timestamp": int(time.time() * 1000),
                             "message": json_log}
                        ],
                        sequenceToken=self.sequence_token
                    )
                    self.sequence_token = response.get("nextSequenceToken")
                    return
                except Exception as retry_error:
                    print(
                        f"CLOUDWATCH ERROR (retry): {retry_error}", file=sys.stderr)
            else:
                print(f"CLOUDWATCH ERROR: {e}", file=sys.stderr)

        except Exception as e:
            print(f"CLOUDWATCH ERROR: {e}", file=sys.stderr)
