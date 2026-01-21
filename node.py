"""Distributed node with Ricart-Agrawala mutex and bully election."""

import argparse
import json
import logging
import os
import random
import signal
import socket
import struct
import sys
import threading
import time
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import boto3

USE_CLOUDWATCH = os.environ.get("USE_CLOUDWATCH", "False").lower() == "true"
CLOUDWATCH_GROUP = "Distributed_System_Logs"
HEARTBEAT_INTERVAL = 2.0
ELECTION_TIMEOUT = 5.0
MUTEX_REPLY_TIMEOUT = 5.0


class MessageType(Enum):
    """Protocol message kinds exchanged between nodes."""
    REQUEST = "REQUEST"
    REPLY = "REPLY"
    ELECTION = "ELECTION"
    ANSWER = "ANSWER"
    COORDINATOR = "COORDINATOR"
    HEARTBEAT = "HEARTBEAT"


class NodeState(Enum):
    """Mutex state used by Ricart-Agrawala mutual exclusion."""
    RELEASED = "RELEASED"
    WANTED = "WANTED"
    HELD = "HELD"


class DistributedNode:
    """Cluster node with TCP messaging, election, and mutex coordination."""

    def __init__(self, node_id: int, peers: Dict[int, Tuple[str, int]], local_port: int) -> None:
        self.node_id: int = node_id
        self.peers: Dict[int, Tuple[str, int]] = peers
        self.port: int = local_port

        self.lamport_clock: int = 0
        self.clock_lock: threading.Lock = threading.Lock()

        self.state: NodeState = NodeState.RELEASED
        self.deferred_replies: List[int] = []
        self.replies_received: set[int] = set()
        self.request_clock: int = 0
        self.mutex_lock: threading.Lock = threading.Lock()
        self.received_replies_event: threading.Event = threading.Event()

        self.coordinator_id: Optional[int] = None
        self.election_in_progress: bool = False
        self.last_heartbeat_time: float = time.time()

        self.peer_connections: Dict[int, socket.socket] = {}
        self.conn_lock: threading.Lock = threading.Lock()
        self.dead_nodes: set[int] = set()
        self.dead_nodes_lock: threading.Lock = threading.Lock()

        self.server_socket: socket.socket = socket.socket(
            socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(
            socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(('', self.port))
        self.server_socket.listen(5)

        self.cw_client = None
        self.cw_sequence_token: Optional[str] = None
        self.logger: logging.Logger = self.setup_logging()

        if USE_CLOUDWATCH:
            try:
                region = os.environ.get('AWS_REGION', 'us-east-1')
                self.cw_client = boto3.client('logs', region_name=region)

                try:
                    self.cw_client.create_log_group(
                        logGroupName=CLOUDWATCH_GROUP)
                except self.cw_client.exceptions.ResourceAlreadyExistsException:
                    pass

                log_stream_name = f"Node_{self.node_id}"
                try:
                    self.cw_client.create_log_stream(
                        logGroupName=CLOUDWATCH_GROUP,
                        logStreamName=log_stream_name
                    )
                except self.cw_client.exceptions.ResourceAlreadyExistsException:
                    pass

                self.logger.info(
                    f"CloudWatch logging enabled in region {region}")
            except Exception as e:
                self.logger.warning(f"Failed to init CloudWatch: {e}")
                self.cw_client = None

        self.running: bool = True

    def _mark_dead(self, node_id: int) -> None:
        with self.dead_nodes_lock:
            self.dead_nodes.add(node_id)

    def _mark_alive(self, node_id: int) -> None:
        with self.dead_nodes_lock:
            self.dead_nodes.discard(node_id)

    def _is_dead(self, node_id: int) -> bool:
        with self.dead_nodes_lock:
            return node_id in self.dead_nodes

    def _dead_nodes_count(self) -> int:
        with self.dead_nodes_lock:
            return len(self.dead_nodes)

    def _dead_nodes_snapshot(self) -> set[int]:
        with self.dead_nodes_lock:
            return set(self.dead_nodes)

    def setup_logging(self) -> logging.Logger:
        """Configure JSON logging to stdout (and optionally CloudWatch)."""
        logger = logging.getLogger(f"Node-{self.node_id}")
        logger.setLevel(logging.INFO)
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(handler)
        return logger

    def log_event(self, event_type: str, message: str, **kwargs: Any) -> None:
        """Emit a structured log entry and forward to CloudWatch when enabled."""
        log_data: dict[str, Any] = {
            "node_id": self.node_id,
            "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "lamport_clock": self.lamport_clock,
            "event_type": event_type,
            "message": message,
            **kwargs
        }
        json_log = json.dumps(log_data)
        self.logger.info(json_log)

        if self.cw_client:
            threading.Thread(target=self._send_to_aws,
                             args=(json_log,), daemon=True).start()

    def _send_to_aws(self, json_log: str) -> None:
        """Push a single log entry to CloudWatch, handling sequence tokens."""
        if not self.cw_client:
            self.logger.warning("CloudWatch client is not initialized.")

            return
        try:
            log_kwargs: dict[str, Any] = {
                "logGroupName": CLOUDWATCH_GROUP,
                "logStreamName": f"Node_{self.node_id}",
                "logEvents": [
                    {"timestamp": int(time.time() * 1000), "message": json_log}
                ]
            }
            if self.cw_sequence_token:
                log_kwargs["sequenceToken"] = self.cw_sequence_token
            response = self.cw_client.put_log_events(**log_kwargs)
            self.cw_sequence_token = response.get("nextSequenceToken")
        except self.cw_client.exceptions.InvalidSequenceTokenException as e:
            msg = e.response.get("Error", {}).get("Message", "")
            token = msg.split()[-1] if msg else None
            if token:
                self.cw_sequence_token = token
                try:
                    response = self.cw_client.put_log_events(
                        logGroupName=CLOUDWATCH_GROUP,
                        logStreamName=f"Node_{self.node_id}",
                        logEvents=[
                            {"timestamp": int(time.time() * 1000),
                             "message": json_log}
                        ],
                        sequenceToken=self.cw_sequence_token
                    )
                    self.cw_sequence_token = response.get("nextSequenceToken")
                    return
                except Exception as retry_error:
                    print(f"CLOUDWATCH ERROR: {retry_error}")
            else:
                print(f"CLOUDWATCH ERROR: {e}")
        except Exception as e:
            print(f"CLOUDWATCH ERROR: {e}")

    def tick(self) -> int:
        with self.clock_lock:
            self.lamport_clock += 1
        return self.lamport_clock

    def update_clock(self, received_time: int) -> None:
        with self.clock_lock:
            self.lamport_clock = max(self.lamport_clock, received_time) + 1

    def _expected_replies(self) -> int:
        return max(0, len(self.peers) - 1 - self._dead_nodes_count())

    def _check_replies_completion(self) -> None:
        if len(self.replies_received) >= self._expected_replies():
            self.received_replies_event.set()

    def _get_connection(self, target_id: int) -> Optional[socket.socket]:
        """Open or reuse a TCP connection to a peer."""
        with self.conn_lock:
            if target_id in self.peer_connections:
                return self.peer_connections[target_id]

            if target_id not in self.peers:
                return None

            target_ip, target_port = self.peers[target_id]
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(2.0)
                s.connect((target_ip, target_port))
                s.settimeout(None)
                self.peer_connections[target_id] = s
                self._mark_alive(target_id)
                return s
            except Exception as e:
                if target_id not in self.dead_nodes:
                    self.log_event(
                        "CONNECTION_ERROR", f"Failed to connect to Node {target_id}", error=str(e))
                return None

    def _send_bytes(self, sock: socket.socket, data: bytes) -> None:
        length_prefix = struct.pack('>I', len(data))
        sock.sendall(length_prefix + data)

    def send_message(self, target_id: int, msg_type: MessageType, **kwargs: Any) -> None:
        if target_id not in self.peers:
            return

        if msg_type == MessageType.HEARTBEAT and self._is_dead(target_id):
            return

        msg: Dict[str, Any] = {
            "sender": self.node_id,
            "type": msg_type.value,
            "timestamp": self.tick(),
            **kwargs
        }
        json_data = json.dumps(msg).encode('utf-8')

        for _ in range(2):
            sock = self._get_connection(target_id)
            if not sock:
                break
            try:
                self._send_bytes(sock, json_data)
                return
            except (BrokenPipeError, ConnectionResetError, socket.error):
                with self.conn_lock:
                    if target_id in self.peer_connections:
                        try:
                            self.peer_connections[target_id].close()
                        except:
                            pass
                        del self.peer_connections[target_id]

        if not self._is_dead(target_id):
            self._mark_dead(target_id)
            self.log_event(
                "NODE_DOWN", f"Failed to send message to {target_id}. Marking as dead.")
            with self.mutex_lock:
                if self.state == NodeState.WANTED:
                    self._check_replies_completion()

    def _recv_exact(self, sock: socket.socket, n: int) -> Optional[bytes]:
        data = b''
        while len(data) < n:
            try:
                packet = sock.recv(n - len(data))
                if not packet:
                    return None
                data += packet
            except socket.error:
                return None
        return data

    def handle_client_connection(self, client_sock: socket.socket, addr: Tuple[str, int]) -> None:
        client_sock.settimeout(None)
        try:
            while self.running:
                len_bytes = self._recv_exact(client_sock, 4)
                if not len_bytes:
                    break
                msg_len = struct.unpack('>I', len_bytes)[0]
                msg_bytes = self._recv_exact(client_sock, msg_len)
                if not msg_bytes:
                    break
                msg = json.loads(msg_bytes.decode('utf-8'))
                self.process_message(msg)
        except Exception:
            pass
        finally:
            client_sock.close()

    def listen(self) -> None:
        while self.running:
            try:
                client, addr = self.server_socket.accept()
                threading.Thread(target=self.handle_client_connection, args=(
                    client, addr), daemon=True).start()
            except OSError:
                break
            except Exception as e:
                self.log_event("LISTENER_ERROR", str(e))

    def process_message(self, msg: Dict[str, Any]) -> None:
        sender: int = msg['sender']
        msg_type: MessageType = MessageType(msg['type'])
        msg_time: int = msg['timestamp']

        self._mark_alive(sender)
        self.update_clock(msg_time)

        if msg_type == MessageType.REQUEST:
            self.handle_request(sender, msg_time)
        elif msg_type == MessageType.REPLY:
            self.handle_reply(sender)
        elif msg_type == MessageType.ELECTION:
            self.handle_election(sender)
        elif msg_type == MessageType.COORDINATOR:
            self.handle_coordinator(sender)
        elif msg_type == MessageType.ANSWER:
            self.handle_answer(sender)
        elif msg_type == MessageType.HEARTBEAT:
            self.handle_heartbeat(sender)

    def request_critical_section(self) -> None:
        """Request entry to the critical section using Ricart-Agrawala."""
        with self.mutex_lock:
            if self.state != NodeState.RELEASED:
                return
            self.state = NodeState.WANTED
            self.request_clock = self.tick()
            self.replies_received.clear()
            self.received_replies_event.clear()
            expected_replies = self._expected_replies()

        self.log_event("MUTEX", "Requesting Critical Section",
                       req_clock=self.request_clock)

        if expected_replies == 0:
            self.enter_critical_section()
            return

        dead_snapshot = self._dead_nodes_snapshot()
        for peer_id in self.peers:
            if peer_id != self.node_id and peer_id not in dead_snapshot:
                self.send_message(peer_id, MessageType.REQUEST)

        if self.received_replies_event.wait(timeout=MUTEX_REPLY_TIMEOUT):
            self.enter_critical_section()
            return

        with self.mutex_lock:
            dead_snapshot = self._dead_nodes_snapshot()
            missing_peers = {pid for pid in self.peers if pid !=
                             self.node_id and pid not in self.replies_received and pid not in dead_snapshot}
            if missing_peers:
                for pid in missing_peers:
                    self._mark_dead(pid)
                self._check_replies_completion()

        if self.received_replies_event.is_set():
            self.enter_critical_section()
        else:
            self.log_event(
                "MUTEX_FAIL", "Timeout waiting for replies. Releasing.")
            with self.mutex_lock:
                self.state = NodeState.RELEASED

    def handle_request(self, sender: int, sender_clock: int) -> None:
        """Queue or grant a mutex reply based on Lamport ordering."""
        reply: bool = False
        with self.mutex_lock:
            my_priority_higher = (self.state == NodeState.HELD) or \
                                 (self.state == NodeState.WANTED and
                                  (self.request_clock < sender_clock or
                                   (self.request_clock == sender_clock and self.node_id < sender)))

            if my_priority_higher:
                self.deferred_replies.append(sender)
            else:
                reply = True

        if reply:
            self.send_message(sender, MessageType.REPLY)

    def handle_reply(self, sender: int) -> None:
        with self.mutex_lock:
            self.replies_received.add(sender)
            self._check_replies_completion()

    def enter_critical_section(self) -> None:
        """Simulated critical section workload."""
        with self.mutex_lock:
            self.state = NodeState.HELD

        self.log_event("CS_ENTER", ">>> ENTERING CRITICAL SECTION <<<")
        time.sleep(random.uniform(0.5, 1.5))
        self.log_event("CS_EXIT", "<<< EXITING CRITICAL SECTION >>>")
        self.exit_critical_section()

    def exit_critical_section(self) -> None:
        with self.mutex_lock:
            self.state = NodeState.RELEASED
            for deferred_node in self.deferred_replies:
                self.send_message(deferred_node, MessageType.REPLY)
            self.deferred_replies.clear()

    def start_election(self) -> None:
        """Run the bully election protocol to select a coordinator."""
        if self.election_in_progress:
            return
        time.sleep(random.uniform(0.1, 0.5))
        self.election_in_progress = True
        self.log_event("ELECTION_START", "Starting Election Process")

        dead_snapshot = self._dead_nodes_snapshot()
        higher_nodes = [pid for pid in self.peers if pid >
                        self.node_id and pid not in dead_snapshot]

        if not higher_nodes:
            self.become_coordinator()
        else:
            for pid in higher_nodes:
                self.send_message(pid, MessageType.ELECTION)
            threading.Thread(
                target=self._wait_for_election_result, daemon=True).start()

    def _wait_for_election_result(self) -> None:
        time.sleep(ELECTION_TIMEOUT)
        if self.election_in_progress:
            self.become_coordinator()

    def handle_election(self, sender: int) -> None:
        self.send_message(sender, MessageType.ANSWER)
        if not self.election_in_progress:
            self.start_election()

    def handle_answer(self, sender: int) -> None:
        self.election_in_progress = True

    def handle_coordinator(self, sender: int) -> None:
        if self.coordinator_id == sender:
            self.last_heartbeat_time = time.time()
            return
        self.coordinator_id = sender
        self.election_in_progress = False
        self.last_heartbeat_time = time.time()
        self.log_event("LEADER_UPDATE", f"New Leader is Node {sender}")

    def become_coordinator(self) -> None:
        self.coordinator_id = self.node_id
        self.election_in_progress = False
        self.log_event("LEADER_SELF", "!!! I am the Coordinator !!!")
        for pid in self.peers:
            if pid != self.node_id:
                self.send_message(pid, MessageType.COORDINATOR)

    def handle_heartbeat(self, sender: int) -> None:
        if self.coordinator_id == sender:
            self.last_heartbeat_time = time.time()
        elif self.coordinator_id is None:
            self.coordinator_id = sender
            self.log_event("LEADER_RECOVER",
                           f"Accepted Leader {sender} via Heartbeat")

    def run_heartbeat_loop(self) -> None:
        """Monitor and emit coordinator heartbeats with jitter."""
        while self.running:
            time.sleep(1.0 + random.uniform(0.0, 0.25))
            if self.coordinator_id == self.node_id:
                for pid in self.peers:
                    if pid != self.node_id:
                        self.send_message(pid, MessageType.HEARTBEAT)
            elif self.coordinator_id is not None:
                if time.time() - self.last_heartbeat_time > (HEARTBEAT_INTERVAL + 4):
                    self.log_event(
                        "LEADER_DEAD", f"Leader {self.coordinator_id} timed out.")
                    self._mark_dead(self.coordinator_id)
                    self.coordinator_id = None
                    self.start_election()

    def shutdown(self) -> None:
        """Close sockets and stop background loops."""
        if not self.running:
            return
        self.running = False
        try:
            self.server_socket.close()
        except Exception:
            pass
        with self.conn_lock:
            for sock in self.peer_connections.values():
                try:
                    sock.close()
                except Exception:
                    pass
            self.peer_connections.clear()
        self.log_event("SYSTEM", "Node shutdown complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", type=int, required=True)
    parser.add_argument("--peers", type=str, default="peers.json")
    args = parser.parse_args()

    try:
        with open(args.peers, 'r') as f:
            config = json.load(f)
            peers_map = {int(k): (v['ip'], v['port'])
                         for k, v in config.items()}

            if args.id not in peers_map:
                print(f"Error: Node ID {args.id} not found in {args.peers}")
                sys.exit(1)

            my_port = peers_map[args.id][1]
    except FileNotFoundError:
        print("Error: peers.json not found.")
        sys.exit(1)

    node = DistributedNode(args.id, peers_map, my_port)

    threading.Thread(target=node.listen, daemon=True).start()
    threading.Thread(target=node.run_heartbeat_loop, daemon=True).start()

    time.sleep(2)

    if node.coordinator_id is None:
        threading.Thread(target=node.start_election, daemon=True).start()

    node.log_event("SYSTEM", f"Node {node.node_id} started.")

    def _handle_shutdown(signum: int, frame: Optional[object]) -> None:
        """Handle SIGINT/SIGTERM with a clean shutdown."""
        node.log_event("SYSTEM", f"Received signal {signum}, shutting down.")
        node.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    try:
        while True:
            cmd = input().strip()
            if cmd == 'req':
                threading.Thread(
                    target=node.request_critical_section, daemon=True).start()
            elif cmd == 'elect':
                node.start_election()
            elif cmd == 'status':
                print(f"Leader: {node.coordinator_id}, State: {node.state}")
            elif cmd in {'quit', 'kill', 'exit'}:
                raise KeyboardInterrupt
            elif cmd == 'help':
                print("Commands: req | elect | status | kill/quit/exit | help")
    except KeyboardInterrupt:
        node.shutdown()
        sys.exit(0)
