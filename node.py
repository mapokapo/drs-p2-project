"""Distributed node with Ricart-Agrawala mutex and bully election."""

import argparse
import json
import os
import random
import signal
import socket
import struct
import sys
import threading
import time
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from cloudwatch_logger import CloudWatchLogger

HEARTBEAT_INTERVAL = 2.0
ELECTION_TIMEOUT = 5.0
MUTEX_REPLY_TIMEOUT = 5.0


def send_bytes(sock: socket.socket, data: bytes) -> None:
    length_prefix = struct.pack(">I", len(data))
    sock.sendall(length_prefix + data)


def recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    data = b""
    while len(data) < n:
        try:
            packet = sock.recv(n - len(data))
            if not packet:
                return None
            data += packet
        except socket.error:
            return None
    return data


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


class ThreadSafeSet:
    """Thread-safe set for tracking nodes."""

    def __init__(self) -> None:
        self._set: set[int] = set()
        self._lock: threading.Lock = threading.Lock()

    def add(self, item: int) -> None:
        with self._lock:
            self._set.add(item)

    def discard(self, item: int) -> None:
        with self._lock:
            self._set.discard(item)

    def __contains__(self, item: int) -> bool:
        with self._lock:
            return item in self._set

    def __len__(self) -> int:
        with self._lock:
            return len(self._set)

    def snapshot(self) -> set[int]:
        with self._lock:
            return set(self._set)


class DistributedNode:
    """Cluster node with TCP messaging, election, and mutex coordination."""

    def __init__(
        self, node_id: int, peers: Dict[int, Tuple[str, int]], local_port: int
    ) -> None:
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
        self.dead_nodes: ThreadSafeSet = ThreadSafeSet()

        self.server_socket: socket.socket = socket.socket(
            socket.AF_INET, socket.SOCK_STREAM
        )
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(("", self.port))
        self.server_socket.listen(5)

        # Initialize CloudWatch logger (checks USE_CLOUDWATCH env var internally)
        use_cloudwatch = os.environ.get("USE_CLOUDWATCH", "False").lower() == "true"
        self.cw_logger = CloudWatchLogger(node_id=self.node_id, enabled=use_cloudwatch)

        self.running: bool = True

    def log_event(self, event_type: str, message: str, **kwargs: Any) -> None:
        """Emit a structured log entry and forward to CloudWatch when enabled."""
        log_data: dict[str, Any] = {
            "node_id": self.node_id,
            "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "lamport_clock": self.lamport_clock,
            "event_type": event_type,
            "message": message,
            **kwargs,
        }
        json_log = json.dumps(log_data)
        self.cw_logger.log(json_log)

    def tick(self) -> int:
        with self.clock_lock:
            self.lamport_clock += 1
        return self.lamport_clock

    def update_clock(self, received_time: int) -> None:
        with self.clock_lock:
            self.lamport_clock = max(self.lamport_clock, received_time) + 1

    def _expected_replies(self) -> int:
        return max(0, len(self.peers) - 1 - len(self.dead_nodes))

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
                self.dead_nodes.discard(target_id)
                return s
            except Exception as e:
                if target_id not in self.dead_nodes:
                    self.log_event(
                        "CONNECTION_ERROR",
                        f"Failed to connect to Node {target_id}",
                        error=str(e),
                    )
                return None

    def send_message(
        self, target_id: int, msg_type: MessageType, **kwargs: Any
    ) -> None:
        if target_id not in self.peers:
            return

        if msg_type == MessageType.HEARTBEAT and target_id in self.dead_nodes:
            return

        msg: Dict[str, Any] = {
            "sender": self.node_id,
            "type": msg_type.value,
            "timestamp": self.tick(),
            **kwargs,
        }
        json_data = json.dumps(msg).encode("utf-8")

        for _ in range(2):
            sock = self._get_connection(target_id)
            if not sock:
                break
            try:
                send_bytes(sock, json_data)
                return
            except (BrokenPipeError, ConnectionResetError, socket.error):
                with self.conn_lock:
                    if target_id in self.peer_connections:
                        try:
                            self.peer_connections[target_id].close()
                        except Exception:
                            pass
                        del self.peer_connections[target_id]

        if target_id not in self.dead_nodes:
            self.dead_nodes.add(target_id)
            self.log_event(
                "NODE_DOWN", f"Failed to send message to {target_id}. Marking as dead."
            )
            with self.mutex_lock:
                if self.state == NodeState.WANTED:
                    self._check_replies_completion()

    def handle_client_connection(
        self, client_sock: socket.socket, addr: Tuple[str, int]
    ) -> None:
        client_sock.settimeout(None)
        try:
            while self.running:
                len_bytes = recv_exact(client_sock, 4)
                if not len_bytes:
                    break
                msg_len = struct.unpack(">I", len_bytes)[0]
                msg_bytes = recv_exact(client_sock, msg_len)
                if not msg_bytes:
                    break
                msg = json.loads(msg_bytes.decode("utf-8"))
                self.process_message(msg)
        except Exception:
            pass
        finally:
            client_sock.close()

    def listen(self) -> None:
        while self.running:
            try:
                client, addr = self.server_socket.accept()
                threading.Thread(
                    target=self.handle_client_connection,
                    args=(client, addr),
                    daemon=True,
                ).start()
            except OSError:
                break
            except Exception as e:
                self.log_event("LISTENER_ERROR", str(e))

    def process_message(self, msg: Dict[str, Any]) -> None:
        sender: int = msg["sender"]
        msg_type: MessageType = MessageType(msg["type"])
        msg_time: int = msg["timestamp"]

        self.dead_nodes.discard(sender)
        self.update_clock(msg_time)

        handlers: Dict[MessageType, Callable[[int, int], None]] = {
            MessageType.REQUEST: self.handle_request,
            MessageType.REPLY: self.handle_reply,
            MessageType.ELECTION: self.handle_election,
            MessageType.COORDINATOR: self.handle_coordinator,
            MessageType.ANSWER: self.handle_answer,
            MessageType.HEARTBEAT: self.handle_heartbeat,
        }
        handlers[msg_type](sender, msg_time)

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

        self.log_event(
            "MUTEX", "Requesting Critical Section", req_clock=self.request_clock
        )

        if expected_replies == 0:
            self.enter_critical_section()
            return

        dead_snapshot = self.dead_nodes.snapshot()
        for peer_id in self.peers:
            if peer_id != self.node_id and peer_id not in dead_snapshot:
                self.send_message(peer_id, MessageType.REQUEST)

        if self.received_replies_event.wait(timeout=MUTEX_REPLY_TIMEOUT):
            self.enter_critical_section()
            return

        with self.mutex_lock:
            dead_snapshot = self.dead_nodes.snapshot()
            missing_peers = {
                pid
                for pid in self.peers
                if pid != self.node_id
                and pid not in self.replies_received
                and pid not in dead_snapshot
            }
            if missing_peers:
                for pid in missing_peers:
                    self.dead_nodes.add(pid)
                self._check_replies_completion()

        if self.received_replies_event.is_set():
            self.enter_critical_section()
        else:
            self.log_event("MUTEX_FAIL", "Timeout waiting for replies. Releasing.")
            with self.mutex_lock:
                self.state = NodeState.RELEASED

    def handle_request(self, sender: int, sender_clock: int) -> None:
        """Queue or grant a mutex reply based on Lamport ordering."""
        reply: bool = False
        with self.mutex_lock:
            my_priority_higher = (self.state == NodeState.HELD) or (
                self.state == NodeState.WANTED
                and (
                    self.request_clock < sender_clock
                    or (self.request_clock == sender_clock and self.node_id < sender)
                )
            )

            if my_priority_higher:
                self.deferred_replies.append(sender)
            else:
                reply = True

        if reply:
            self.send_message(sender, MessageType.REPLY)

    def handle_reply(self, sender: int, _msg_time: Optional[int] = None) -> None:
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

        dead_snapshot = self.dead_nodes.snapshot()
        higher_nodes = [
            pid for pid in self.peers if pid > self.node_id and pid not in dead_snapshot
        ]

        if not higher_nodes:
            self.become_coordinator()
        else:
            for pid in higher_nodes:
                self.send_message(pid, MessageType.ELECTION)
            threading.Thread(target=self._wait_for_election_result, daemon=True).start()

    def _wait_for_election_result(self) -> None:
        time.sleep(ELECTION_TIMEOUT)
        if self.election_in_progress:
            self.become_coordinator()

    def handle_election(self, sender: int, _msg_time: Optional[int] = None) -> None:
        self.send_message(sender, MessageType.ANSWER)
        if not self.election_in_progress:
            self.start_election()

    def handle_answer(self, sender: int, _msg_time: Optional[int] = None) -> None:
        self.election_in_progress = True

    def handle_coordinator(self, sender: int, _msg_time: Optional[int] = None) -> None:
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

    def handle_heartbeat(self, sender: int, _msg_time: Optional[int] = None) -> None:
        if self.coordinator_id == sender:
            self.last_heartbeat_time = time.time()
        elif self.coordinator_id is None:
            self.coordinator_id = sender
            self.log_event("LEADER_RECOVER", f"Accepted Leader {sender} via Heartbeat")

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
                        "LEADER_DEAD", f"Leader {self.coordinator_id} timed out."
                    )
                    self.dead_nodes.add(self.coordinator_id)
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
        with open(args.peers, "r") as f:
            config = json.load(f)
            peers_map = {int(k): (v["ip"], v["port"]) for k, v in config.items()}

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
            if cmd == "req":
                threading.Thread(
                    target=node.request_critical_section, daemon=True
                ).start()
            elif cmd == "elect":
                node.start_election()
            elif cmd == "status":
                print(f"Leader: {node.coordinator_id}, State: {node.state}")
            elif cmd in {"quit", "kill", "exit"}:
                raise KeyboardInterrupt
            elif cmd == "help":
                print("Commands: req | elect | status | kill/quit/exit | help")
    except KeyboardInterrupt:
        node.shutdown()
        sys.exit(0)
